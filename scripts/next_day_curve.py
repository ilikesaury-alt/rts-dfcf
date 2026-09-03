"""次日日内收益曲线：买入后在不同时点卖出，收益分别如何。

目的：解释「次日 10:00 卖出」为何劣于「次日收盘卖出」——
如果 10:00 系统性处于全天低点，说明卖早了，而不是选股问题。

买入基准：T 日收盘（最干净，不含推荐时刻的日内噪声）。
卖出时点：T+1 的 09:35 / 10:00 / 10:30 / 11:30 / 14:00 / 15:00。

用法: python scripts/next_day_curve.py
"""

from __future__ import annotations

import sqlite3
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from intraday_exit_test import build  # noqa: E402

CACHE = ROOT / "scripts" / ".cache_m5.sqlite3"

TIMES = ["09:35", "09:50", "10:00", "10:30", "11:00", "11:30",
         "13:30", "14:00", "14:30", "15:00"]


def load_next_bars(symbols, date_pairs):
    """取 (symbol, date) -> {time: close}，只取需要的日期。"""
    conn = sqlite3.connect(CACHE)
    out: dict[tuple, dict] = defaultdict(dict)
    for sym, d in date_pairs:
        for t, c in conn.execute(
                "SELECT time, close FROM m5 WHERE symbol=? AND date=?", (sym, d)):
            out[(sym, d)][t] = c
    conn.close()
    return out


def main() -> None:
    rows, notes = build("2026-07-22")
    pairs = [(r["symbol"], r["_nxt"]) for r in rows]
    nb = load_next_bars(sorted({r["symbol"] for r in rows}), pairs)

    print("=" * 104)
    print("次日日内收益曲线（买入=T日收盘，卖出=T+1 各时点；按天等权组合）")
    print(f"样本 {len(rows)} 条 / {len({r['date'] for r in rows})} 个交易日")
    print("=" * 104)
    print(f"{'卖出时点':<12s} {'覆盖样本':>8s} {'日均':>9s} {'中位':>9s} "
          f"{'日胜率':>8s} {'累计':>10s}")
    print("-" * 104)

    results = {}
    for t in TIMES:
        byday: dict[str, list[float]] = defaultdict(list)
        n = 0
        for r in rows:
            bars = nb.get((r["symbol"], r["_nxt"])) or {}
            c = bars.get(t)
            if c is None:
                continue
            byday[r["date"]].append((c / r["_p_close"] - 1) * 100)
            n += 1
        if not byday:
            continue
        dailies = [statistics.fmean(v) for v in byday.values()]
        cum = 1.0
        for d in dailies:
            cum *= (1 + d / 100)
        med = statistics.median(
            [(statistics.fmean(v)) for v in byday.values()])
        results[t] = (n, statistics.fmean(dailies), cum)
        print(f"T+1 {t:<7s} {n:>8d} {statistics.fmean(dailies):>+8.3f}% "
              f"{med:>+8.3f}% {100 * sum(1 for d in dailies if d > 0) / len(dailies):>7.1f}% "
              f"{(cum - 1) * 100:>+9.2f}%")

    print("\n" + "=" * 104)
    print("结论性对比")
    print("=" * 104)
    if "10:00" in results and "15:00" in results:
        a = results["10:00"][1]
        b = results["15:00"][1]
        print(f"  次日 10:00 卖 日均 {a:+.3f}%   次日 15:00 卖 日均 {b:+.3f}%   "
              f"差 {b - a:+.3f}%/日（31 天放大约 {((1 + (b - a) / 100) ** 31 - 1) * 100:+.1f}%）")
    if "09:35" in results and "15:00" in results:
        print(f"  开盘 09:35 卖 日均 {results['09:35'][1]:+.3f}%  ← 隔夜跳空后的第一根 bar")

    # 最低点/最高点时点分布
    print("\n【各时点相对强弱排名】（日均从高到低）")
    for i, (t, v) in enumerate(sorted(results.items(), key=lambda kv: -kv[1][1]), 1):
        print(f"  {i:>2d}. T+1 {t}  日均 {v[1]:+.3f}%")
    print("\n  若 10:00 排名靠后 → 说明是「卖早了」而非「选错了」。")


if __name__ == "__main__":
    main()
