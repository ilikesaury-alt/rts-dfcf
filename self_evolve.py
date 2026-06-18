"""
Self-evolution entry point.
Run weekly (or on-demand) to analyze performance and suggest parameter improvements.

Usage:
    python self_evolve.py                      # Full report + suggestions
    python self_evolve.py --apply              # Auto-apply suggested changes
    python self_evolve.py --days 60            # Analyze last 60 days
    python self_evolve.py --backfill           # Only backfill missing outcomes
"""
import argparse
import sqlite3

from scanner.database import DB_PATH
from scanner.evolution.tracker import backfill_outcomes, tracking_stats
from scanner.evolution.analytics import dimension_ic, weekly_report
from scanner.evolution.optimizer import generate_optimization_report, apply_params, BASE_PARAMS


def main():
    parser = argparse.ArgumentParser(description="策略自进化工具")
    parser.add_argument("--apply", action="store_true", help="自动应用建议的参数调整")
    parser.add_argument("--days", type=int, default=30, help="分析窗口天数 (默认30)")
    parser.add_argument("--backfill", action="store_true", help="仅填补缺失的outcome数据")
    parser.add_argument("--report", action="store_true", help="生成周报")
    parser.add_argument("--week-start", type=str, default="", help="周报起始日期 YYYY-MM-DD")
    parser.add_argument("--week-end", type=str, default="", help="周报结束日期 YYYY-MM-DD")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    if args.backfill:
        print("正在填补缺失的 outcome 数据...")
        result = backfill_outcomes(conn)
        print(f"  填补 {result['filled']}/{result['total']} 条, 跳过 {result['skipped']} 条")

        stats = tracking_stats(conn)
        all_s = stats.get("all")
        if all_s:
            print(f"  当前总胜率: {all_s['wins_1d']}/{all_s['total']} ({all_s['wins_1d']*100//max(all_s['total'],1)}%)")
        conn.close()
        return

    if args.report:
        ws = args.week_start or "2026-06-01"
        we = args.week_end or "2026-06-08"
        print(weekly_report(conn, ws, we))
        conn.close()
        return

    # Backfill first
    result = backfill_outcomes(conn)
    if result["filled"] > 0:
        print(f"[进化] 填补 {result['filled']}/{result['total']} 条 outcome")

    print(generate_optimization_report(conn, window_days=args.days))

    if args.apply:
        print()
        print("正在自动应用参数调整 (基于 IC 分析)...")
        ic_data = dimension_ic(conn, args.days)
        adjustments = {}
        for dim, info in ic_data.items():
            base = BASE_PARAMS["weights"].get(dim)
            if base is None or info["count"] < 15:
                continue
            ic = info["ic"]
            if ic > 0.15:
                adjustments[dim] = int(base * 1.3)
            elif ic < -0.1:
                adjustments[dim] = max(-20, -base)
            elif ic < -0.05 and base > 0:
                adjustments[dim] = max(0, int(base * 0.5))

        if adjustments:
            new_params = dict(BASE_PARAMS)
            new_params["weights"].update(adjustments)
            notes = "自动调整 %d 个维度权重" % len(adjustments)
            apply_params(conn, new_params, notes=notes)
            print("  已调整 %d 个维度" % len(adjustments))
            for d, w in adjustments.items():
                print("    %s: %s -> %s" % (d, BASE_PARAMS["weights"].get(d, 0), w))
        else:
            print("  无需要调整的维度")

    conn.close()


if __name__ == "__main__":
    main()
