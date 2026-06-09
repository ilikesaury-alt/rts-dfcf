"""
雪球飙升榜 → 创业板智能扫描
策略: 只盯300xxx，区分"新面孔(底部异动)"和"旧面孔(盘整二波)"
"""
import sys
import time
from datetime import datetime

import requests

from scanner.config import REFRESH_INTERVAL, DB_PATH, NEW_FACE_LOOKBACK_DAYS
from scanner.database import init_db, update_recommendation_results, save_recommendations, get_tracking_summary
from scanner.api import make_session
from scanner.orchestrator import scan
from scanner.trading_session import is_trading_time, seconds_until_next_session, next_session_label
from scanner.feishu import push_feishu
from scanner.display import display
from scanner.logging import log_results

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="创业板飙升扫描器")
    parser.add_argument("interval", nargs="?", type=int, default=REFRESH_INTERVAL,
                        help="刷新间隔（秒）")
    parser.add_argument("--ultra", action="store_true", help="超短模式：更高门槛、rank_change驱动")
    args = parser.parse_args()

    interval = max(60, args.interval)
    ultra = args.ultra

    conn = init_db()
    session = make_session()

    print(f"  创业板飙升扫描器  |  每{interval}s刷新  |  DB: {DB_PATH}")
    if ultra:
        print(f"  🔥 超短模式 | rank_change驱动 | 高置信度过滤")
    print(f"  新面孔: 过去{NEW_FACE_LOOKBACK_DAYS}天未出现 = 新 | 旧面孔: 出现过 = 旧")
    print(f"  交易时段: 09:30-11:45 / 13:00-15:00  |  非交易时段自动休眠")
    print(f"  {'='*60}")

    tracking = get_tracking_summary(conn)
    if tracking:
        print(tracking)
    print()

    last_ranks: dict[str, int] = {}

    while True:
        now = datetime.now()
        if not is_trading_time(now):
            wait = seconds_until_next_session(now)
            label = next_session_label(now)
            print(f"\r  🌙 非交易时段 | {label} ({wait // 60}分后)  ", end="", flush=True)
            for _ in range(min(wait, interval), 0, -60):
                time.sleep(60)
                if is_trading_time():
                    break
            continue

        try:
            update_recommendation_results(conn, session)

            new_faces, old_faces, momentum, all_gem, filtered_large_cap = scan(conn, session, ultra=ultra)

            display(new_faces, old_faces, momentum, len(all_gem), interval,
                    filtered_large_cap=filtered_large_cap, last_ranks=last_ranks)
            log_results(new_faces, old_faces, momentum)

            last_ranks.clear()
            for s in all_gem:
                last_ranks[s.symbol] = s.rank

            if new_faces:
                top = new_faces[0]
                print(f"  ▶ 新面孔首选: {top.stock.name}({top.stock.symbol}) "
                      f"{top.stock.percent:+.2f}% | {top.kline.trend if top.kline else ''}")
                if top.score >= 20:
                    print(f"  ⚠️  底部异动信号! {top.stock.name} 评分{top.score}")
            if momentum:
                top_m = momentum[0]
                print(f"  ▶ 动量延续首选: {top_m.stock.name}({top_m.stock.symbol}) "
                      f"{top_m.stock.percent:+.2f}% | {top_m.kline.trend if top_m.kline else ''}")
            if old_faces:
                top_o = old_faces[0]
                print(f"  ▶ 旧面孔首选: {top_o.stock.name}({top_o.stock.symbol}) "
                      f"{top_o.stock.percent:+.2f}% | {top_o.kline.trend if top_o.kline else ''}")

            save_recommendations(conn, new_faces, old_faces, momentum)
            # push_feishu(new_faces, old_faces, momentum, len(all_gem), conn)

        except requests.RequestException as e:
            print(f"\n  [!] 网络错误: {e}")
        except Exception as e:
            print(f"\n  [!] 错误: {type(e).__name__}: {e}")

        for remaining in range(interval, 0, -1):
            if not is_trading_time():
                break
            if remaining % 10 == 0 or remaining <= 10:
                print(f"\r  ⏳ 下次刷新还有 {remaining}s ...", end="", flush=True)
            time.sleep(1)
        print()


if __name__ == "__main__":
    main()
