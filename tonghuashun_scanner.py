"""
同花顺飙升榜 → 创业板智能扫描
策略: 只盯300xxx，沿用新面孔/动量延续/回调介入三策略
"""
import atexit
import sys
import time
from datetime import datetime

import requests

from scanner.api import make_session as make_xueqiu_session
from scanner.ths_api import fetch_ths_hot_list, make_ths_session
from scanner.config import DB_PATH, NEW_FACE_LOOKBACK_DAYS, REFRESH_INTERVAL
from scanner.database import init_db, save_recommendations
from scanner.display import display
from scanner.log_utils import log_results
from scanner.orchestrator import scan_with_raw
from scanner.trading_session import is_trading_time, next_session_label, seconds_until_next_session

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="同花顺飙升榜扫描器")
    parser.add_argument("interval", nargs="?", type=int, default=REFRESH_INTERVAL,
                        help="刷新间隔（秒）")
    args = parser.parse_args()

    interval = max(60, args.interval)

    conn = init_db()
    atexit.register(conn.close)
    ths_session = make_ths_session()
    # scan_with_raw internally calls xueqiu APIs (market caps, klines, index)
    xq_session = make_xueqiu_session()

    print(f"  同花顺飙升榜扫描器  |  每{interval}s刷新  |  DB: {DB_PATH}")
    print(f"  新面孔: 过去{NEW_FACE_LOOKBACK_DAYS}天未出现 = 新 | 旧面孔: 出现过 = 旧")
    print("  交易时段: 09:30-11:30 / 13:00-15:00  |  非交易时段自动休眠")
    print(f"  {'='*60}")

    last_ranks: dict[str, int] = {}

    try:
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
                raw = fetch_ths_hot_list(ths_session)
                if not raw:
                    print(f"\r  [!] 同花顺热榜数据为空，等待刷新...", end="", flush=True)
                    time.sleep(interval)
                    continue

                new_faces, momentum, stale_candidates, all_gem, filtered_large_cap = (
                    scan_with_raw(raw, conn, xq_session))

                display(new_faces, momentum, len(all_gem), interval,
                        filtered_large_cap=filtered_large_cap, last_ranks=last_ranks,
                        stale_candidates=stale_candidates)
                log_results(new_faces, momentum)

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
                save_recommendations(conn, new_faces, momentum)

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


if __name__ == "__main__":
    main()
