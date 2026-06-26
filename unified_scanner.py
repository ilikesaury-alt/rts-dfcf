"""
雪球 + 同花顺 双源融合扫描器

雪球为主数据源，同花顺仅做交叉校验：
  - 同时出现在两个榜单 → 额外加分
  - 仅雪球 → 正常参与分析
  - 仅同花顺 → 不纳入候选
"""
import sys
import time

import requests

from scanner.api import fetch_biaosheng, make_session
from scanner.config import (
    CROSS_SOURCE_BONUS,
    DB_PATH,
    NEW_FACE_LOOKBACK_DAYS,
    REFRESH_INTERVAL,
    now_beijing,
)
from scanner.cross_validation import cross_validate, print_validation_summary
from scanner.database import init_db, save_recommendations
from scanner.display import display
from scanner.log_utils import log_results
from scanner.orchestrator import scan_with_raw
from scanner.ths_api import fetch_ths_hot_list, make_ths_session
from scanner.trading_session import is_trading_time, next_session_label, seconds_until_next_session

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")


_SOURCE_LABELS = {
    "xueqiu": "雪球",
    "tonghuashun": "同花顺",
}


def main():
    import argparse
    parser = argparse.ArgumentParser(description="双源融合创业板飙升扫描器")
    parser.add_argument("interval", nargs="?", type=int, default=REFRESH_INTERVAL,
                        help="刷新间隔（秒）")
    args = parser.parse_args()

    interval = max(60, args.interval)

    conn = init_db()
    xq_session = make_session()
    ths_session = make_ths_session()

    print(f"  双源融合扫描器  |  每{interval}s刷新  |  DB: {DB_PATH}")
    print(f"  主源: 雪球飙升榜  |  校验: 同花顺热股榜  |  双源一致额外 +{CROSS_SOURCE_BONUS} 分")
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
                for _ in range(min(wait, interval), 0, -60):
                    time.sleep(60)
                    if is_trading_time():
                        break
                continue

            try:
                xq_raw = fetch_biaosheng(xq_session)
                if not xq_raw:
                    print(f"\r  [!] 雪球飙升榜数据为空，等待刷新...", end="", flush=True)
                    time.sleep(interval)
                    continue

                ths_raw = fetch_ths_hot_list(ths_session)
                ths_symbols = {i["symbol"] for i in (ths_raw or []) if i.get("symbol")}

                for item in xq_raw:
                    sym = item.get("symbol", "")
                    item["source_tag"] = "both" if sym in ths_symbols else "xueqiu"

                both_count = sum(1 for i in xq_raw if i.get("source_tag") == "both")
                print(f"\r  📡 雪球{len(xq_raw)}只 (双源{both_count}只)", end="", flush=True)

                new_faces, momentum, pullback_list, stale_candidates, all_gem, filtered_large_cap = (
                    scan_with_raw(xq_raw, conn, xq_session))

                for c in new_faces + momentum + pullback_list:
                    if c.stock.source_tag == "both":
                        c.score += CROSS_SOURCE_BONUS

                new_faces.sort(key=lambda x: -x.score)
                momentum.sort(key=lambda x: -x.score)
                pullback_list.sort(key=lambda x: -x.score)

                display(new_faces, momentum + pullback_list, len(all_gem), interval,
                        filtered_large_cap=filtered_large_cap, last_ranks=last_ranks,
                        stale_candidates=stale_candidates)
                log_results(new_faces, momentum + pullback_list)

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

                save_recommendations(conn, new_faces, momentum + pullback_list)
                cross_validate()
                print_validation_summary()

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
