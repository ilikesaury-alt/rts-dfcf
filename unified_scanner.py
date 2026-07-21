"""
雪球双源融合扫描器（飙升榜 + 热搜榜）

雪球为主数据源，热搜榜做交叉校验：
  - 同时出现在两个榜单 → 额外加分
  - 仅飙升榜 → 正常参与分析
  - 仅热搜榜 → 不纳入候选
"""
import sys
import time

import requests

from scanner.api import fetch_biaosheng, fetch_xueqiu_hot_list, make_session
from scanner.config import (
    CROSS_SOURCE_BONUS,
    DB_PATH,
    NEW_FACE_LOOKBACK_DAYS,
    REFRESH_INTERVAL,
    now_beijing,
)
from scanner.backtest import backfill_outcomes
from scanner.database import init_db, save_recommendations
from scanner.display import display
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


def main():
    import argparse
    parser = argparse.ArgumentParser(description="双源融合创业板飙升扫描器")
    parser.add_argument("interval", nargs="?", type=int, default=REFRESH_INTERVAL,
                        help="刷新间隔（秒）")
    parser.add_argument("--no-feishu", action="store_true", help="禁用飞书推送")
    args = parser.parse_args()

    interval = max(60, args.interval)

    conn = init_db()
    xq_session = make_session()

    print(f"  雪球双源融合扫描器  |  每{interval}s刷新  |  DB: {DB_PATH}")
    print(f"  主源: 飙升榜  |  校验: 热搜榜  |  双源一致额外 +{CROSS_SOURCE_BONUS} 分")
    print(f"  新面孔: 过去{NEW_FACE_LOOKBACK_DAYS}天未出现 = 新  |  交易时段: 09:30-11:30 / 13:00-15:00")
    print(f"  {'='*60}")

    last_ranks: dict[str, int] = {}

    try:
        while True:
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

            try:
                xq_raw = fetch_biaosheng(xq_session)
                if not xq_raw:
                    print(f"\r  [!] 飙升榜数据为空，等待刷新...", end="", flush=True)
                    time.sleep(interval)
                    continue

                hot_list = fetch_xueqiu_hot_list(xq_session)
                hot_symbols = {i["symbol"] for i in (hot_list or []) if i.get("symbol")}

                for item in xq_raw:
                    sym = item.get("symbol", "")
                    item["source_tag"] = "both" if sym in hot_symbols else "xueqiu"

                both_count = sum(1 for i in xq_raw if i.get("source_tag") == "both")
                print(f"\r  📡 飙升榜{len(xq_raw)}只 (双榜{both_count}只)", end="", flush=True)

                new_faces, momentum, pullback_list, short_term_list, stale_candidates, all_gem, filtered_large_cap = (
                    scan_with_raw(xq_raw, conn, xq_session))

                new_faces.sort(key=lambda x: -x.score)
                momentum.sort(key=lambda x: -x.score)
                pullback_list.sort(key=lambda x: -x.score)
                short_term_list.sort(key=lambda x: -x.score)

                current_rank_map = {s.symbol: s.rank for s in all_gem}
                display(new_faces, momentum, len(all_gem), interval,
                        filtered_large_cap=filtered_large_cap, last_ranks=last_ranks,
                        stale_candidates=stale_candidates, pullback_list=pullback_list,
                        current_rank_map=current_rank_map, short_term_list=short_term_list)
                log_results(new_faces, momentum + pullback_list + short_term_list)
                if not args.no_feishu:
                    pushed = push_feishu(new_faces, momentum, pullback_list, stale_candidates,
                                        len(all_gem), filtered_large_cap=filtered_large_cap,
                                        current_rank_map=current_rank_map,
                                        short_term_list=short_term_list)
                    if not pushed and (new_faces or momentum or pullback_list or short_term_list):
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
                if short_term_list:
                    top_s = short_term_list[0]
                    src = _SOURCE_LABELS.get(top_s.stock.source_tag, top_s.stock.source_tag)
                    print(f"  ▶ 超短次日首选: {top_s.stock.name}({top_s.stock.symbol}) [{src}] "
                          f"{top_s.stock.percent:+.2f}% | RPS:{top_s.rps_bonus}")

                save_recommendations(conn, new_faces, momentum + pullback_list + short_term_list)
                try:
                    n = backfill_outcomes(conn)
                    if n:
                        print(f"  📊 回填 {n} 条收益数据", end="", flush=True)
                except Exception as e:
                    print(f"    [!] 回填失败: {type(e).__name__}: {e}", flush=True)

            except requests.RequestException as e:
                print(f"\n  [!] 网络错误: {e}")
            except Exception as e:
                print(f"\n  [!] 错误: {type(e).__name__}: {e}")

            for remaining in range(interval, 0, -5):
                if not is_trading_time():
                    break
                print(f"\r  ⏳ 下次刷新还有 {remaining}s ...", end="", flush=True)
                time.sleep(5)
            print()
    except KeyboardInterrupt:
        print("\n  👋 扫描器已停止")
    finally:
        conn.close()
        xq_session.close()


if __name__ == "__main__":
    main()
