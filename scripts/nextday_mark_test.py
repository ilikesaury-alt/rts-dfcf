"""🎯 标记与档位的实际预测力验证。

🎯 / 档位不落库（纯排序层），因此用 scanner.ranking 的原函数对历史行重算，
保证口径与线上完全一致，再对次日收益归因。

用法: python scripts/nextday_mark_test.py [--days 90]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
from collections import defaultdict
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "scanner.db"

from scanner.ranking import _entry_tier, _is_nextday_marked  # noqa: E402


def load(days: int) -> list[dict]:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    sql = """
        SELECT * FROM recommendations
        WHERE excluded = 0 AND next_day_pct IS NOT NULL
          AND date >= date((SELECT max(date) FROM recommendations), ?)
        ORDER BY date, rowid
    """
    raw = conn.execute(sql, (f"-{days - 1} days",)).fetchall()
    conn.close()
    latest: dict[tuple[str, str], sqlite3.Row] = {}
    for r in raw:
        latest[(r["date"], r["symbol"] or "__nosym__")] = r
    return [dict(r) for r in latest.values()]


def summarize(rows: list[dict], label: str) -> None:
    if not rows:
        print(f"{label:<26s} 空集")
        return
    vals = [r["next_day_pct"] for r in rows]
    n = len(vals)
    mean = statistics.fmean(vals)
    sd = statistics.pstdev(vals) if n > 1 else 0
    t = mean / (sd / n**0.5) if sd > 0 else 0
    days = len({r["date"] for r in rows})
    print(f"{label:<26s} n={n:<5d} 日均{n / max(days, 1):5.1f}只  "
          f"收益{mean:+6.2f}% 中位{statistics.median(vals):+6.2f}%  "
          f"胜率{100 * sum(1 for v in vals if v > 0) / n:5.1f}%  "
          f"hit7 {100 * sum(1 for v in vals if v >= 7) / n:5.1f}%  t={t:+5.2f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    args = ap.parse_args()

    rows = load(args.days)
    conn = sqlite3.connect(DB)

    for r in rows:
        try:
            bd = json.loads(r["score_breakdown"] or "{}")
        except (ValueError, TypeError):
            bd = {}
        r["_bd"] = bd if isinstance(bd, dict) else {}
        accum = r["_bd"].get("accumulated_incl_today")
        try:
            accum = float(accum) if accum is not None else None
        except (TypeError, ValueError):
            accum = None
        r["_accum"] = accum
        entry = {
            "category": r["category"],
            "symbol": r["symbol"],
            "percent": r["percent"],
            "score_breakdown": r["_bd"],
            "_candidate": None,
        }
        try:
            r["_mark"] = _is_nextday_marked(entry, conn, accum=accum)
        except Exception as e:  # 判定失败不应中断归因
            r["_mark"] = None
            r["_mark_err"] = type(e).__name__
        try:
            r["_tier"] = _entry_tier(entry, conn, accum=accum, marked=r["_mark"])
        except Exception:
            r["_tier"] = None
    conn.close()

    errs = sum(1 for r in rows if r.get("_mark_err"))
    print("=" * 92)
    print(f"🎯 标记 / 档位 预测力验证   样本 {len(rows)}  判定异常 {errs}")
    print("=" * 92)

    print("\n【🎯 次日大涨画像】")
    summarize([r for r in rows if r["_mark"] is True], "带 🎯（档0置顶）")
    summarize([r for r in rows if r["_mark"] is False], "不带 🎯")
    print()
    mk = [r["next_day_pct"] for r in rows if r["_mark"] is True]
    nm = [r["next_day_pct"] for r in rows if r["_mark"] is False]
    if mk and nm:
        print(f"  🎯 差值: {statistics.fmean(mk) - statistics.fmean(nm):+.2f}%")

    print("\n【综合排序档位】（档0=🎯 档1=rebound 档2=普通 档3=警示劣后）")
    by_tier: dict[int, list[dict]] = defaultdict(list)
    for r in rows:
        if r["_tier"] is not None:
            by_tier[r["_tier"]].append(r)
    for t in sorted(by_tier):
        summarize(by_tier[t], f"档{t}")

    print("\n【🎯 有效性 — 样本外检验】")
    rows.sort(key=lambda r: (r["date"], r["id"]))
    dates = sorted({r["date"] for r in rows})
    cut = dates[int(len(dates) * 0.6)]
    oos = [r for r in rows if r["date"] >= cut]
    print(f"  切分日 {cut}  OOS n={len(oos)}")
    summarize([r for r in oos if r["_mark"] is True], "OOS 带🎯")
    summarize([r for r in oos if r["_mark"] is False], "OOS 不带🎯")
    print()
    ot: dict[int, list[dict]] = defaultdict(list)
    for r in oos:
        if r["_tier"] is not None:
            ot[r["_tier"]].append(r)
    for t in sorted(ot):
        summarize(ot[t], f"OOS 档{t}")

    print("\n【每日若只买带🎯的票：逐日合计】")
    byday: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if r["_mark"] is True:
            byday[r["date"]].append(r)
    if byday:
        dailies = [statistics.fmean([r["next_day_pct"] for r in v]) for v in byday.values()]
        win_days = sum(1 for d in dailies if d > 0)
        print(f"  覆盖 {len(byday)} 天，日均持仓 {statistics.fmean([len(v) for v in byday.values()]):.1f} 只")
        print(f"  组合日收益 均值 {statistics.fmean(dailies):+.2f}%  "
              f"中位 {statistics.median(dailies):+.2f}%  日胜率 {100 * win_days / len(dailies):.1f}%")
        cum = 1.0
        for d in dailies:
            cum *= (1 + d / 100)
        print(f"  等权复利累计 {(cum - 1) * 100:+.1f}%（{len(dailies)} 个交易日）")


if __name__ == "__main__":
    main()
