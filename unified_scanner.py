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

from scanner.backtest import backfill_outcomes
from scanner.config import (
    CROSS_SOURCE_BONUS,
    DB_PATH,
    LOG_DIR,
    NEW_FACE_LOOKBACK_DAYS,
    REFRESH_INTERVAL,
    SUPERVISE_CHILD_GRACE,
    SUPERVISE_CHILD_TIMEOUT,
    SUPERVISE_LOG_FILE,
    SUPERVISE_RESET_AFTER_SECONDS,
    SUPERVISE_RESTART_DELAY,
    SUPERVISE_RESTART_MAX_DELAY,
    now_beijing,
)
from scanner.data_source import get_adapter
from scanner.database import (
    get_today_recommendations,
    init_db,
    mark_reversed_recommendations,
    save_recommendations,
)
from scanner.display import clear_screen, display
from scanner.feishu import push_feishu
from scanner.log_utils import log_results
from scanner.orchestrator import scan_with_raw
from scanner.trading_session import is_trading_time, next_session_label, seconds_until_next_session

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")


_SOURCE_LABELS = {
    "xueqiu": "雪球",
    "both": "双榜",
}

_STDOUT_SILENCED = False

# 子进程心跳文件：父进程 supervisor 据此判定子进程是否假死（冻结）。
# 非 --supervise 直跑时也写（无害），保证 watchdog 语义统一。
HEARTBEAT_FILE = os.path.join(LOG_DIR, "scanner_heartbeat")


def _touch_heartbeat():
    """子进程心跳：主循环每轮更新文件 mtime，供父进程 watchdog 判定假死。"""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        # touch() 创建（若不存在）或更新 mtime，等价于原 open("a")+os.utime，更简洁。
        from pathlib import Path
        Path(HEARTBEAT_FILE).touch()
    except Exception:
        pass


def _heartbeat_age() -> float | None:
    """心跳文件距今秒数；文件不存在返回 None（子进程尚未写任何心跳）。"""
    try:
        return time.time() - os.path.getmtime(HEARTBEAT_FILE)
    except OSError:
        return None


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

    # 上一轮扫描的榜单排名快照：综合排序「排名」列据此显示雪球榜单排名变化（+N 升 / -N 降）。
    last_ranks: dict[str, int] = {}

    try:
        while True:
            try:
                # 每轮迭代（含非交易时段）开头清屏，保证每次输出都是干净终端：
                # 表头/状态行/回马枪等不再与上一轮残留叠加。display() 内部仍会再
                # 清一次（保持「清屏→渲染」自包含语义），此处覆盖所有输出路径。
                clear_screen()
                now = now_beijing()
                if not is_trading_time(now):
                    wait = seconds_until_next_session(now)
                    label = next_session_label(now)
                    print(f"\r  🌙 非交易时段 | {label} ({wait // 60}分后)  ", end="", flush=True)
                    # 分段 sleep，每段最多 60 秒，剩余不足 60 秒时按实际剩余时间睡，
                    # 避免 wait<60 时仍睡 60 秒错过开盘第一分钟数据。
                    remaining = wait
                    while remaining > 0:
                        _touch_heartbeat()
                        sleep_secs = min(60, remaining)
                        time.sleep(sleep_secs)
                        remaining -= sleep_secs
                        if is_trading_time():
                            break
                    continue

                _touch_heartbeat()
                conn = _ensure_conn(conn)

                xq_raw = adapter.fetch_biaosheng()
                if not xq_raw:
                    print("\r  [!] 飙升榜数据为空，等待刷新...", end="", flush=True)
                    time.sleep(interval)
                    continue

                hot_list = adapter.fetch_hot_list()
                hot_symbols = {i["symbol"] for i in (hot_list or []) if i.get("symbol")}

                for item in xq_raw:
                    sym = item.get("symbol", "")
                    item["source_tag"] = "both" if sym in hot_symbols else "xueqiu"

                both_count = sum(1 for i in xq_raw if i.get("source_tag") == "both")
                print(f"\r  📡 飙升榜{len(xq_raw)}只 (双榜{both_count}只)", end="", flush=True)

                res = scan_with_raw(xq_raw, conn, adapter)
                new_faces = res.new_faces
                momentum = res.momentum
                rebound_list = res.rebound
                short_term_list = res.short_term
                comeback_list = res.comeback
                stale_candidates = res.stale_candidates
                all_gem = res.gem_stocks
                filtered_large_cap = res.filtered_large_cap
                current_quotes = res.current_quotes
                # 各桶已在 scan_with_raw 内排序（new_face 用 _new_face_sort_key，其余按 score 降序）

                current_rank_map = {s.symbol: s.rank for s in all_gem}

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
                            live_quotes[sym] = {"percent": d.get("percent", 0.0),
                                                "current": d.get("current", 0.0),
                                                "high_pct": d.get("high_pct")}
                except Exception as e:
                    print(f"  [!] 补拉推荐票行情失败: {e}")

                # 2026-08-13 反转盲区修复：今日已推荐（榜上主类别）但当前不在候选池的票，满足
                # ①已转负且回落≥1.5 或 ②回落≥5（无论红绿）即标 excluded=1 移出综合排序（保留
                # 落库记录）——大幅回吐即使未转负也"不敢买"（如 +8%→+2%）。回马枪跟踪池不参与。
                # 硬过滤只评估当前轮次候选，够不着掉出候选池的旧推荐。excluded 按最新轮次刷新：
                # 重新成为候选的票由 orchestrator 的 passed_syms 置回 0。
                try:
                    active_syms = {c.stock.symbol for c in (
                        new_faces + momentum + rebound_list + short_term_list + comeback_list)}
                    reversed_syms = mark_reversed_recommendations(conn, today_recs, active_syms, live_quotes)
                    if reversed_syms:
                        _names = "、".join(reversed_syms[:8])
                        _more = f" 等{len(reversed_syms)}只" if len(reversed_syms) > 8 else ""
                        print(f"  [反转移出] {len(reversed_syms)} 只推荐后回落过大，移出综合排序：{_names}{_more}")
                except Exception as e:
                    print(f"  [!] 推荐后回落移出失败: {e}")

                # 历史推荐跟踪已并入回马枪（2026-08-07）：tracker 模块删除，不再单独查询
                display(len(all_gem), interval,
                        filtered_large_cap=filtered_large_cap,
                        conn=conn, live_quotes=live_quotes,
                        rank_map=current_rank_map,
                        today_pool=res.today_pool,
                        last_ranks=last_ranks)
                # 快照本轮榜单排名供下一轮展示排名变化（上一轮为 None 时显示纯名次）。
                last_ranks = dict(current_rank_map)
                log_results(new_faces, momentum + rebound_list + short_term_list + comeback_list)
                if not no_feishu:
                    pushed = push_feishu(new_faces, momentum, stale_candidates,
                                        len(all_gem), filtered_large_cap=filtered_large_cap,
                                        current_rank_map=current_rank_map,
                                        short_term_list=short_term_list,
                                        rebound_list=rebound_list,
                                        comeback_list=comeback_list)
                    if not pushed and (new_faces or momentum or rebound_list or short_term_list or comeback_list):
                        print("\r  📤 飞书推送跳过（冷却中/无变化）", end="", flush=True)

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
                if comeback_list:
                    top_c = comeback_list[0]
                    print(f"  ▶ 回马枪首选: {top_c.stock.name}({top_c.stock.symbol}) "
                          f"[{top_c.comeback_variant}] {top_c.stock.percent:+.2f}% "
                          f"| {top_c.kline.trend if top_c.kline else ''}")
                if short_term_list:
                    top_s = short_term_list[0]
                    src = _SOURCE_LABELS.get(top_s.stock.source_tag, top_s.stock.source_tag)
                    print(f"  ▶ 超短次日首选: {top_s.stock.name}({top_s.stock.symbol}) [{src}] "
                          f"{top_s.stock.percent:+.2f}% | RPS:{top_s.rps_bonus}")

                save_recommendations(conn, new_faces,
                                     momentum + rebound_list + short_term_list + comeback_list)
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


def _wait_or_kill(proc, grace: int = SUPERVISE_CHILD_GRACE) -> int:
    """轮询子进程：正常退出返回 exit code；心跳超时判定假死 → kill 返回 -9。

    原实现用 subprocess.run(cmd) 阻塞等待，子进程冻结（网络栈/DB 锁死等
    无法被主循环 try/except 兜住的挂起）时父进程永挂、无法重启。
    现改为：Popen + 每 5s poll + 心跳 mtime 监控，超时强杀视为崩溃重启。

    启动宽限期 grace：Popen 后的 grace 秒内不判心跳超时，原因有二：
      1) 子进程完成导入/建连需要时间，宽限内无论心跳文件状态（含父进程刚清理的
         陈旧文件）都不强杀，避免与子进程首拍 touch 竞态；
      2) 宽限期满后若 age is None（从未写过心跳）→ 视为启动即冻结，同样强杀，
         不留"冻结于启动、父进程永久等待"的死角。
    """
    loop_start = time.time()
    while True:
        code = proc.poll()
        if code is not None:
            return code
        # 宽限期内只 poll/sleep，不判心跳，给子进程启动与首拍留时间。
        if time.time() - loop_start > grace:
            age = _heartbeat_age()
            if age is None:
                _supervise_log(f"子进程启动超 {grace}s 仍未写心跳，判定启动冻结，强制终止")
                try:
                    proc.kill()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=10)
                except Exception:
                    pass
                return -9
            if age > SUPERVISE_CHILD_TIMEOUT:
                _supervise_log(f"子进程心跳超时({int(age)}s)，判定假死，强制终止")
                try:
                    proc.kill()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=10)
                except Exception:
                    pass
                return -9
        try:
            time.sleep(5)
        except KeyboardInterrupt:
            raise


def _supervise(interval: int, no_feishu: bool) -> int:
    """父进程监督模式：拉起子进程，崩溃（非 0 退出）或心跳超时（假死）后指数退避重启。"""
    import subprocess

    delay = SUPERVISE_RESTART_DELAY
    while True:
        cmd = _build_child_cmd(interval, no_feishu)
        start = time.time()
        _supervise_log(f"启动子进程: {' '.join(cmd)}")
        try:
            # 启动前清理上一轮可能残留的陈旧心跳文件：其旧 mtime 会让父进程在
            # 首轮 poll 即判"心跳超时"并强杀刚启动的健康子进程，导致重启死循环。
            # 清理后 _heartbeat_age() 返回 None，直至新子进程写出首个心跳。
            try:
                os.remove(HEARTBEAT_FILE)
            except OSError:
                pass
            proc = subprocess.Popen(cmd)
            code = _wait_or_kill(proc)
        except KeyboardInterrupt:
            _supervise_log("收到中断，停止监督")
            try:
                proc.kill()
            except Exception:
                pass
            return 0
        except Exception as e:
            _supervise_log(f"启动子进程失败: {e}")
            time.sleep(delay)
            continue
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
