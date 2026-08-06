"""
雪球双源融合扫描器（飙升榜 + 热搜榜）

雪球为主数据源，热搜榜做交叉校验：
  - 同时出现在两个榜单 → 额外加分
  - 仅飙升榜 → 正常参与分析
  - 仅热搜榜 → 不纳入候选

长跑健壮性（P-robust）：
  - 主循环整个迭代体被 try/except 保护，任何意外异常（含非交易时段等待分支、
    倒计时打印）都打印告警并自动续跑，不杀进程
  - 每轮 DB 健康检查 + 连接自愈（失败重建）
  - 输出管道/终端异常时 stdout 降级到 devnull，不崩溃
  - --supervise 模式：父进程拉起子进程，子进程意外退出（非 0）后指数退避自动重启
"""
import os
import sys
import time

import requests

from scanner.data_source import get_adapter
from scanner.config import (
    CROSS_SOURCE_BONUS,
    DB_PATH,
    LOG_DIR,
    NEW_FACE_LOOKBACK_DAYS,
    REFRESH_INTERVAL,
    SUPERVISE_LOG_FILE,
    SUPERVISE_RESTART_DELAY,
    SUPERVISE_RESTART_MAX_DELAY,
    SUPERVISE_RESET_AFTER_SECONDS,
    now_beijing,
)
from scanner.backtest import backfill_outcomes
from scanner.database import get_today_recommendations, init_db, save_recommendations
from scanner.display import display
from scanner.feishu import push_feishu
from scanner.log_utils import log_results
from scanner.orchestrator import scan_with_raw
from scanner.tracker import track_recent_recommendations
from scanner.trading_session import is_trading_time, next_session_label, seconds_until_next_session

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")


_SOURCE_LABELS = {
    "xueqiu": "雪球",
    "both": "双榜",
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


def run_scanner(interval: int, no_feishu: bool) -> None:
    """单进程扫描循环（可被子进程 supervise 拉起）。"""
    conn = init_db()
    adapter = get_adapter()

    print(f"  雪球双源融合扫描器  |  每{interval}s刷新  |  DB: {DB_PATH}")
    print(f"  主源: 飙升榜  |  校验: 热搜榜  |  双源一致额外 +{CROSS_SOURCE_BONUS} 分")
    print(f"  新面孔: 过去{NEW_FACE_LOOKBACK_DAYS}天未出现 = 新  |  交易时段: 09:30-11:30 / 13:00-15:00")
    print(f"  {'='*60}")

    last_ranks: dict[str, int] = {}

    try:
        while True:
            try:
                now = now_beijing()
                if not is_trading_time(now):
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
                    print(f"\r  [!] 飙升榜数据为空，等待刷新...", end="", flush=True)
                    time.sleep(interval)
                    continue

                hot_list = adapter.fetch_hot_list()
                hot_symbols = {i["symbol"] for i in (hot_list or []) if i.get("symbol")}

                for item in xq_raw:
                    sym = item.get("symbol", "")
                    item["source_tag"] = "both" if sym in hot_symbols else "xueqiu"

                both_count = sum(1 for i in xq_raw if i.get("source_tag") == "both")
                print(f"\r  📡 飙升榜{len(xq_raw)}只 (双榜{both_count}只)", end="", flush=True)

                new_faces, momentum, pullback_list, rebound_list, short_term_list, stale_candidates, all_gem, filtered_large_cap, current_quotes = (
                    scan_with_raw(xq_raw, conn, adapter))

                new_faces.sort(key=lambda x: -x.score)
                momentum.sort(key=lambda x: -x.score)
                pullback_list.sort(key=lambda x: -x.score)
                rebound_list.sort(key=lambda x: -x.score)
                short_term_list.sort(key=lambda x: -x.score)

                current_rank_map = {s.symbol: s.rank for s in all_gem}

                # 为综合推荐补拉今日曾推荐但不在 current_quotes 中的票的实时行情
                live_quotes: dict[str, dict] = {}
                live_quotes.update(current_quotes)
                try:
                    today_recs = get_today_recommendations(conn)
                    today_syms = {r["symbol"] for r in today_recs}
                    missing = list(today_syms - set(current_quotes.keys()))
                    if missing:
                        extra = adapter.fetch_market_caps_batch(missing)
                        for sym, d in extra.items():
                            live_quotes[sym] = {"percent": d.get("percent", 0.0), "current": d.get("current", 0.0)}
                except Exception as e:
                    print(f"  [!] 补拉推荐票行情失败: {e}")

                # 历史推荐跟踪：查近5天推荐的实时表现
                try:
                    tracked = track_recent_recommendations(conn, adapter)
                except Exception as e:
                    tracked = []
                    print(f"  [!] 历史推荐跟踪失败: {e}")
                display(new_faces, momentum, len(all_gem), interval,
                        filtered_large_cap=filtered_large_cap, last_ranks=last_ranks,
                        pullback_list=pullback_list,
                        short_term_list=short_term_list,
                        rebound_list=rebound_list, tracked_recs=tracked,
                        conn=conn, live_quotes=live_quotes,
                        rank_map=current_rank_map)
                log_results(new_faces, momentum + pullback_list + rebound_list + short_term_list)
                if not no_feishu:
                    pushed = push_feishu(new_faces, momentum, pullback_list, stale_candidates,
                                        len(all_gem), filtered_large_cap=filtered_large_cap,
                                        current_rank_map=current_rank_map,
                                        short_term_list=short_term_list,
                                        rebound_list=rebound_list)
                    if not pushed and (new_faces or momentum or pullback_list or rebound_list or short_term_list):
                        print(f"\r  📤 飞书推送跳过（冷却中/无变化）", end="", flush=True)

                last_ranks.clear()
                for s in all_gem:
                    last_ranks[s.symbol] = s.rank

                if new_faces:
                    top = new_faces[0]
                    src = _SOURCE_LABELS.get(top.stock.source_tag, top.stock.source_tag)
                    print(f"  ▶ 新面孔首选: {top.stock.name}({top.stock.symbol}) [{src}] "
                          f"{top.stock.percent:+.2f}% | {top.kline.trend if top.kline else ''}")
                if momentum:
                    top_m = momentum[0]
                    src = _SOURCE_LABELS.get(top_m.stock.source_tag, top_m.stock.source_tag)
                    print(f"  ▶ 动量延续首选: {top_m.stock.name}({top_m.stock.symbol}) [{src}] "
                          f"{top_m.stock.percent:+.2f}% | {top_m.kline.trend if top_m.kline else ''}")
                if rebound_list:
                    top_r = rebound_list[0]
                    src = _SOURCE_LABELS.get(top_r.stock.source_tag, top_r.stock.source_tag)
                    print(f"  ▶ 超跌反弹首选: {top_r.stock.name}({top_r.stock.symbol}) [{src}] "
                          f"{top_r.stock.percent:+.2f}% | {top_r.kline.trend if top_r.kline else ''}")
                if short_term_list:
                    top_s = short_term_list[0]
                    src = _SOURCE_LABELS.get(top_s.stock.source_tag, top_s.stock.source_tag)
                    print(f"  ▶ 超短次日首选: {top_s.stock.name}({top_s.stock.symbol}) [{src}] "
                          f"{top_s.stock.percent:+.2f}% | RPS:{top_s.rps_bonus}")

                save_recommendations(conn, new_faces,
                                     momentum + pullback_list + rebound_list + short_term_list)
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


def _supervise_log(msg: str):
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(SUPERVISE_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{now_beijing().strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def _build_child_cmd(interval: int, no_feishu: bool) -> list[str]:
    cmd = [sys.executable, os.path.abspath(__file__), str(interval)]
    if no_feishu:
        cmd.append("--no-feishu")
    return cmd


def _should_restart(exit_code: int) -> bool:
    """子进程退出码判定：0（Ctrl+C 手动停止/正常退出）不重启，非 0 视为崩溃需重启。"""
    return exit_code != 0


def _supervise(interval: int, no_feishu: bool) -> int:
    """父进程监督模式：拉起子进程，崩溃（非 0 退出）后指数退避重启。"""
    import subprocess

    delay = SUPERVISE_RESTART_DELAY
    while True:
        cmd = _build_child_cmd(interval, no_feishu)
        start = time.time()
        _supervise_log(f"启动子进程: {' '.join(cmd)}")
        try:
            proc = subprocess.run(cmd)
        except KeyboardInterrupt:
            _supervise_log("收到中断，停止监督")
            return 0
        except Exception as e:
            _supervise_log(f"启动子进程失败: {e}")
            time.sleep(delay)
            continue
        code = proc.returncode
        uptime = time.time() - start
        _supervise_log(f"子进程退出 code={code} uptime={int(uptime)}s")
        if not _should_restart(code):
            return 0
        if uptime >= SUPERVISE_RESET_AFTER_SECONDS:
            delay = SUPERVISE_RESTART_DELAY
        _supervise_log(f"{delay}s 后重启")
        time.sleep(delay)
        delay = min(delay * 3, SUPERVISE_RESTART_MAX_DELAY)
    return 0


def main():
    import argparse
    parser = argparse.ArgumentParser(description="双源融合创业板飙升扫描器")
    parser.add_argument("interval", nargs="?", type=int, default=REFRESH_INTERVAL,
                        help="刷新间隔（秒）")
    parser.add_argument("--no-feishu", action="store_true", help="禁用飞书推送")
    parser.add_argument("--supervise", action="store_true", help="崩溃后自动重启（父进程监督模式）")
    args = parser.parse_args()

    interval = max(60, args.interval)

    if args.supervise:
        sys.exit(_supervise(interval, args.no_feishu))

    try:
        run_scanner(interval, args.no_feishu)
    except KeyboardInterrupt:
        print("\n  👋 扫描器已停止")


if __name__ == "__main__":
    main()
