"""
雪球飙升榜扫描器

雪球为主数据源。

长跑健壮性（P-robust）：
  - 主循环整个迭代体被 try/except 保护，任何意外异常（含非交易时段等待分支、
    倒计时打印）都打印告警并自动续跑，不杀进程
  - 每轮 DB 健康检查 + 连接自愈（失败重建）
  - 输出管道/终端异常时 stdout 降级到 devnull，不崩溃
"""

import os
import sys
import time
from datetime import datetime, timedelta

import requests

from scanner.backtest import backfill_outcomes
from scanner.config import (
    AFTERNOON_END,
    DB_PATH,
    KLINE_FETCH_DAYS,
    KLINE_FETCH_DEADLINE,
    LOG_DIR,
    NEW_FACE_LOOKBACK_DAYS,
    REFRESH_INTERVAL,
    now_beijing,
)
from scanner.data_source import get_adapter
from scanner.database import (
    get_today_recommendations,
    init_db,
    mark_reversed_recommendations,
    record_leaderboard_log,
    save_kline_to_db,
    save_recommendations,
)
from scanner.display import display
from scanner.feishu import push_feishu
from scanner.log_utils import log_results
from scanner.orchestrator import scan_with_raw
from scanner.ranking_snapshot import persist_ranking_snapshot
from scanner.trading_session import (
    is_trading_day,
    is_trading_time,
    next_session_label,
    seconds_until_next_session,
)
from scanner.utils import clear_screen

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")


_SOURCE_LABELS = {
    "xueqiu": "雪球",
}

_STDOUT_SILENCED = False


def _silence_stdout():
    """输出管道关闭/磁盘满等 OSError 后把 stdout 降级到 devnull（只降级一次）。"""
    global _STDOUT_SILENCED
    if _STDOUT_SILENCED:
        return
    _STDOUT_SILENCED = True
    try:
        # open(..., encoding=...) 已返回 TextIOWrapper，不可再包一层（write 会收到 bytes）
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    except Exception:
        pass


def _log_exception(message: str, exc: BaseException | None = None):
    """异常详情追加到 logs/scanner_error.log，供长跑后排查（控制台可能被清屏）。"""
    try:
        import traceback

        os.makedirs(LOG_DIR, exist_ok=True)
        with open(os.path.join(LOG_DIR, "scanner_error.log"), "a", encoding="utf-8") as f:
            f.write(f"\n[{now_beijing().strftime('%Y-%m-%d %H:%M:%S')}] {message}")
            if exc is not None:
                f.write("\n" + "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    except Exception:
        pass


def _ensure_conn(conn):
    """DB 健康检查：SELECT 1 失败则重建连接，实现 SQLite 锁死/连接损坏自愈。"""
    try:
        conn.execute("SELECT 1")
        return conn
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        print("  [!] 数据库连接异常，已重建")
        return init_db()


# 收盘定稿标记：记录已完成「今日 bar → 最终收盘 bar」覆盖的日期，防止重复拉取。
_finalize_date: str | None = None


def _finalize_today_klines(conn, adapter) -> None:
    """收盘后自动定稿今日 K 线（每个交易日只执行一次，fail-open）。

    盘中扫描把「未收盘的今日 bar」（盘中价 + 部分量能）写入 daily_kline，若收盘后
    无定稿覆盖，残留 bar 永久留在库里——次日复盘/回测/个股报告读到错误收盘价
    （2026-08-18 拓斯达案例：盘中 36.27 残留，真实收盘 37.90，DB 一度显示 -3.36%）。
    backfill_kline.py 收盘后手动跑也能兜底，这里在主循环非交易时段自动做一次。
    """
    global _finalize_date
    now = now_beijing()
    today = now.date()
    if _finalize_date == today.isoformat():
        return
    if not is_trading_day(today) or is_trading_time(now):
        return
    if now.time() < AFTERNOON_END:
        return  # 开盘前/午间不算收盘后，跳过
    # 等数据源完成收盘结算（一般 15:00 即定稿，留 2 分钟余量防尾盘最后一笔延迟）
    target = datetime.combine(today, AFTERNOON_END, tzinfo=now.tzinfo) + timedelta(minutes=2)
    remaining = (target - now).total_seconds()
    if remaining > 0:
        print(f"\r  🌙 非交易时段 | 等待收盘定稿 ({int(remaining)}s)  ", end="", flush=True)
        time.sleep(min(remaining, 120))
    symbols = [
        r[0]
        for r in conn.execute("SELECT DISTINCT symbol FROM daily_kline WHERE date=?", (today.isoformat(),)).fetchall()
    ]
    if not symbols:
        return
    # 定稿前快照：记录今日 bar 现有收盘价，定稿后统计「修正」数（与盘中残留不同的票数），
    # 写入 logs/finalize.log 供审计——某日修正数异常大 = 盘中残留集中（数据源/机制异常信号）。
    old_closes = {
        r[0]: r[1]
        for r in conn.execute("SELECT symbol, close FROM daily_kline WHERE date=?", (today.isoformat(),)).fetchall()
    }
    deadline = now_beijing().timestamp() + KLINE_FETCH_DEADLINE
    refreshed = 0
    corrected = 0
    for sym in symbols:
        if now_beijing().timestamp() >= deadline:
            break
        try:
            kline = adapter.fetch_kline(sym, KLINE_FETCH_DAYS)
            if kline:
                today_bars = [k for k in kline if k["date"] == today.isoformat()]
                if today_bars:
                    save_kline_to_db(conn, sym, today_bars)
                    refreshed += 1
                    new_close = today_bars[-1]["close"]
                    if old_closes.get(sym) is not None and abs(old_closes[sym] - new_close) > 0.011:
                        corrected += 1
        except Exception:
            continue  # fail-open：单只失败跳过，backfill_kline 手动兜底
    line = (
        f"{today.isoformat()} {now_beijing().strftime('%H:%M:%S')} "
        f"refreshed={refreshed}/{len(symbols)} corrected={corrected}"
    )
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(os.path.join(LOG_DIR, "finalize.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    # 全部工作完成后才标记当日已定稿：此前置位若中途抛异常（如 DB 锁/`conn.execute`
    # 未包 try），当日会被误标为已定稿而实际零写入，且进程不重启不再重试。
    _finalize_date = today.isoformat()
    print(f"\r  [收盘定稿] 覆盖 {refreshed}/{len(symbols)} 只今日K线为最终收盘价（修正 {corrected} 只）  ", flush=True)


# 档位快照落库标记：记录已完成当日 ranking_snapshot 写入的日期（每交易日一次）。
_snapshot_done_date: str | None = None


def _persist_ranking_snapshot_once(conn) -> None:
    """收盘后写当日综合排序档位快照（每交易日一次，fail-open）。

    在 _finalize_today_klines 之后调用（K 线定稿先落，快照回放用的 accum 口径
    才是收盘定稿值）。任何异常只告警 + 记 finalize.log，不杀主循环——快照缺失
    的日期由消费端回退现算兜底。
    """
    global _snapshot_done_date
    now = now_beijing()
    today = now.date().isoformat()
    if _snapshot_done_date == today:
        return
    if not is_trading_day(now.date()) or is_trading_time(now):
        return
    if now.time() < AFTERNOON_END:
        return  # 开盘前/午间不算收盘后，跳过
    try:
        n = persist_ranking_snapshot(conn, today)
        _snapshot_done_date = today
        line = f"{today} {now.strftime('%H:%M:%S')} ranking_snapshot rows={n}"
        print(f"\r  [档位快照] 落库 {n} 行  ", flush=True)
    except Exception as e:
        line = f"{today} {now.strftime('%H:%M:%S')} ranking_snapshot FAILED: {e}"
        print(f"\r  [!] 档位快照落库失败（消费端将回退现算）: {e}  ", flush=True)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(os.path.join(LOG_DIR, "finalize.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def run_scanner(interval: int, no_feishu: bool) -> None:
    """单进程扫描循环。"""
    conn = init_db()
    adapter = get_adapter()

    print(f"  雪球飙升榜扫描器  |  每{interval}s刷新  |  DB: {DB_PATH}")
    print(f"  新面孔: 过去{NEW_FACE_LOOKBACK_DAYS}天未出现 = 新  |  交易时段: 09:30-11:30 / 13:00-15:00")
    print(f"  {'=' * 60}")

    # 上一轮扫描的榜单排名快照：综合排序「排名」列据此显示雪球榜单排名变化（+N 升 / -N 降）。
    last_ranks: dict[str, int] = {}
    # 榜单可观测性：上一轮飙升榜成员集合，用于算本轮重叠率（探测上游样本口径抖动）。
    prev_board_syms: set[str] = set()

    try:
        while True:
            try:
                # 每轮迭代（含非交易时段）开头清屏，保证每次输出都是干净终端：
                # 表头/状态行/回马枪等不再与上一轮残留叠加。display() 内部仍会再
                # 清一次（保持「清屏→渲染」自包含语义），此处覆盖所有输出路径。
                clear_screen()
                now = now_beijing()
                if not is_trading_time(now):
                    # 收盘后自动定稿今日K线（每个交易日一次）：盘中残留的部分 bar 用
                    # 最终收盘 bar 覆盖，防止次日复盘/回测读到脏数据（拓斯达案例）。
                    _finalize_today_klines(conn, adapter)
                    # 定稿后写当日综合排序档位快照（每交易日一次，fail-open）：
                    # 历史归因存证，ranking 代码演进不篡改历史（见 ranking_snapshot.py）。
                    _persist_ranking_snapshot_once(conn)
                    wait = seconds_until_next_session(now)
                    label = next_session_label(now)
                    print(f"\r  🌙 非交易时段 | {label} ({wait // 60}分后)  ", end="", flush=True)
                    # 分段 sleep，每段最多 60 秒，剩余不足 60 秒时按实际剩余时间睡，
                    # 避免 wait<60 时仍睡 60 秒错过开盘第一分钟数据。
                    remaining = wait
                    while remaining > 0:
                        sleep_secs = min(60, remaining)
                        time.sleep(sleep_secs)
                        remaining -= sleep_secs
                        if is_trading_time():
                            break
                    continue

                conn = _ensure_conn(conn)

                xq_raw = adapter.fetch_biaosheng()
                if not xq_raw:
                    print("\r  [!] 飙升榜数据为空，等待刷新...", end="", flush=True)
                    time.sleep(interval)
                    continue

                for item in xq_raw:
                    sym = item.get("symbol", "")
                    item["source_tag"] = "xueqiu"

                # 榜单可观测性（2026-08-19）：每轮把飙升榜成分+排名分布落库。
                # 探测雪球上游口径漂移（排序键/分页/样本过滤变更 → 中位数/重叠率/涨跌结构突变）。
                # fail-open：落库失败不阻塞扫描主流程。
                try:
                    prev_board_syms = record_leaderboard_log(conn, "biaosheng", xq_raw, prev_board_syms)
                except Exception as e:
                    print(f"  [!] 榜单可观测性落库失败: {e}")

                res = scan_with_raw(xq_raw, conn, adapter)
                new_faces = res.new_faces
                momentum = res.momentum
                rebound_list = res.rebound
                short_term_list = res.short_term
                comeback_list = res.comeback
                all_gem = res.gem_stocks
                filtered_large_cap = res.filtered_large_cap
                current_quotes = res.current_quotes
                # 各桶已在 scan_with_raw 内排序（new_face 用 candidates.new_face_sort_key，其余按 score 降序）

                current_rank_map = {s.symbol: s.rank for s in all_gem}

                # 先落库再展示（2026-08-21 审查修复）：save_recommendations 原在
                # display() 之后，而综合排序（display_priority）从 DB 读今日推荐——
                # 导致终端恒渲染上一轮数据（当日首轮推荐不显示、之后每轮滞后一个
                # 刷新周期），与同轮已实时推送的飞书不一致。移到读取
                # today_recs/mark_reversed/display 之前，终端与本轮扫描同源。
                # 本轮候选随后由 mark_reversed 经 active_syms 跳过、不被反转评估，
                # 行为等价；orchestrator 的 excluded 置 0/1 更新仍在其内部先行完成。
                save_recommendations(conn, new_faces, momentum + rebound_list + short_term_list + comeback_list)

                # 为综合推荐补拉今日曾推荐但不在 current_quotes 中的票的实时行情
                live_quotes: dict[str, dict] = {}
                live_quotes.update(current_quotes)
                today_recs: list[dict] = []
                today_syms: set[str] = set()
                try:
                    today_recs = get_today_recommendations(conn)
                    today_syms = {r["symbol"] for r in today_recs}
                    missing = list(today_syms - set(current_quotes.keys()))
                    if missing:
                        extra = adapter.fetch_market_caps_batch(missing)
                        for sym, d in extra.items():
                            # 行情降级条目（current<=0，如停牌/字段缺失被强转 0）不入
                            # live_quotes：0.00% 会被 mark_reversed 误当"已转负"、被
                            # display 误显为真实涨幅（2026-08-14 fail-open 修复）。
                            if not d.get("current"):
                                continue
                            live_quotes[sym] = {
                                "percent": d.get("percent", 0.0),
                                "current": d.get("current", 0.0),
                                "high_pct": d.get("high_pct"),
                            }
                except Exception as e:
                    print(f"  [!] 补拉推荐票行情失败: {e}")

                # 2026-08-13 反转盲区修复：今日已推荐（榜上主类别）但当前不在候选池的票，满足
                # ①已转负且回落≥REVERSAL_TURNED_RED_DROP(5.0) 或 ②回落≥REVERSAL_OVERSHOOT_DROP(10.0)
                # （无论红绿）即标 excluded=1 移出综合排序（保留落库记录）——大幅回吐即使未转负也
                # "不敢买"（如 +12%→+2%）。回马枪跟踪池不参与。
                # 硬过滤只评估当前轮次候选，够不着掉出候选池的旧推荐。excluded 按最新轮次刷新：
                # 重新成为候选的票由 orchestrator 的 passed_syms 置回 0。
                try:
                    active_syms = {
                        c.stock.symbol for c in (new_faces + momentum + rebound_list + short_term_list + comeback_list)
                    }
                    reversed_syms = mark_reversed_recommendations(conn, today_recs, active_syms, live_quotes)
                    if reversed_syms:
                        _names = "、".join(reversed_syms[:8])
                        _more = f" 等{len(reversed_syms)}只" if len(reversed_syms) > 8 else ""
                        print(f"  [反转移出] {len(reversed_syms)} 只推荐后回落过大，移出综合排序：{_names}{_more}")
                except Exception as e:
                    print(f"  [!] 推荐后回落移出失败: {e}")

                # 历史推荐跟踪已并入回马枪（2026-08-07）：tracker 模块删除，不再单独查询
                display(
                    len(all_gem),
                    interval,
                    filtered_large_cap=filtered_large_cap,
                    conn=conn,
                    live_quotes=live_quotes,
                    rank_map=current_rank_map,
                    today_pool=res.today_pool,
                    last_ranks=last_ranks,
                )
                # 快照本轮榜单排名供下一轮展示排名变化（上一轮为 None 时显示纯名次）。
                last_ranks = dict(current_rank_map)
                log_results(new_faces, momentum + rebound_list + short_term_list + comeback_list)
                if not no_feishu:
                    pushed = push_feishu(
                        new_faces,
                        momentum,
                        len(all_gem),
                        filtered_large_cap=filtered_large_cap,
                        short_term_list=short_term_list,
                        rebound_list=rebound_list,
                        comeback_list=comeback_list,
                    )
                    if not pushed and (new_faces or momentum or rebound_list or short_term_list or comeback_list):
                        print("\r  📤 飞书推送跳过（冷却中/无变化）", end="", flush=True)

                if new_faces:
                    top = new_faces[0]
                    src = _SOURCE_LABELS.get(top.stock.source_tag, top.stock.source_tag)
                    print(
                        f"  ▶ 新面孔首选: {top.stock.name}({top.stock.symbol}) [{src}] "
                        f"{top.stock.percent:+.2f}% | {top.kline.trend if top.kline else ''}"
                    )
                if momentum:
                    top_m = momentum[0]
                    src = _SOURCE_LABELS.get(top_m.stock.source_tag, top_m.stock.source_tag)
                    print(
                        f"  ▶ 动量延续首选: {top_m.stock.name}({top_m.stock.symbol}) [{src}] "
                        f"{top_m.stock.percent:+.2f}% | {top_m.kline.trend if top_m.kline else ''}"
                    )
                if rebound_list:
                    top_r = rebound_list[0]
                    src = _SOURCE_LABELS.get(top_r.stock.source_tag, top_r.stock.source_tag)
                    print(
                        f"  ▶ 超跌反弹首选: {top_r.stock.name}({top_r.stock.symbol}) [{src}] "
                        f"{top_r.stock.percent:+.2f}% | {top_r.kline.trend if top_r.kline else ''}"
                    )
                if comeback_list:
                    top_c = comeback_list[0]
                    print(
                        f"  ▶ 回马枪首选: {top_c.stock.name}({top_c.stock.symbol}) "
                        f"[{top_c.comeback_variant}] {top_c.stock.percent:+.2f}% "
                        f"| {top_c.kline.trend if top_c.kline else ''}"
                    )
                if short_term_list:
                    top_s = short_term_list[0]
                    src = _SOURCE_LABELS.get(top_s.stock.source_tag, top_s.stock.source_tag)
                    print(
                        f"  ▶ 超短次日首选: {top_s.stock.name}({top_s.stock.symbol}) [{src}] "
                        f"{top_s.stock.percent:+.2f}% | RPS:{top_s.rps_bonus}"
                    )

                try:
                    n = backfill_outcomes(conn)
                    if n:
                        print(f"  📊 回填 {n} 条收益数据", end="", flush=True)
                except Exception as e:
                    print(f"    [!] 回填失败: {type(e).__name__}: {e}", flush=True)

                for remaining in range(interval, 0, -5):
                    if not is_trading_time():
                        break
                    print(f"\r  ⏳ 下次刷新还有 {remaining}s ...", end="", flush=True)
                    time.sleep(5)
                print()
            except KeyboardInterrupt:
                raise
            except requests.RequestException as e:
                print(f"\n  [!] 网络错误: {e}")
                _log_exception(f"网络错误: {e}")
                time.sleep(min(interval, 60))
            except (BrokenPipeError, OSError) as e:
                _log_exception(f"输出异常（stdout 已降级）: {e}")
                _silence_stdout()
                time.sleep(min(interval, 30))
            except Exception as e:
                print(f"\n  [!] 循环异常，已自动续跑: {type(e).__name__}: {e}")
                _log_exception("循环异常", e)
                time.sleep(min(interval, 60))
    finally:
        try:
            conn.close()
        except Exception:
            pass


def main():
    import argparse

    parser = argparse.ArgumentParser(description="双源融合创业板飙升扫描器")
    parser.add_argument("interval", nargs="?", type=int, default=REFRESH_INTERVAL, help="刷新间隔（秒）")
    parser.add_argument("--no-feishu", action="store_true", help="禁用飞书推送")
    args = parser.parse_args()

    interval = max(60, args.interval)

    try:
        run_scanner(interval, args.no_feishu)
    except KeyboardInterrupt:
        print("\n  👋 扫描器已停止")


if __name__ == "__main__":
    main()
