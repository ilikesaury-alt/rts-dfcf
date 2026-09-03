"""组合级样本外检验：确认选票规则不是过拟合。

时间切分：前 60% 日期 = IS（用于发现规则），后 40% = OOS（从未参与规则挑选）。

用法: python scripts/pick_oos_check.py [--days 90] [--topk 1 2 3 5]
"""

from __future__ import annotations

import argparse
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pick_portfolio import (  # noqa: E402
    BAD_CAT,
    GOOD_TREND,
    bdnum,
    load,
    portfolio,
    show,
)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--topk", type=int, nargs="*", default=[1, 2, 3, 5])
    args = ap.parse_args()

    rows = load(args.days)
    rows.sort(key=lambda r: (r["date"], r["id"]))
    dates = sorted({r["date"] for r in rows})
    cut = dates[int(len(dates) * 0.6)]
    isr = [r for r in rows if r["date"] < cut]
    oos = [r for r in rows if r["date"] >= cut]

    print("=" * 108)
    print(f"组合级样本外检验   切分日 {cut}")
    print(f"  IS  {len(isr)} 条  {dates[0]} ~ {dates[int(len(dates) * 0.6) - 1]}")
    print(f"  OOS {len(oos)} 条  {cut} ~ {dates[-1]}  ← 该段未参与任何规则挑选")
    print("=" * 108)

    def r5set(rs):
        return [r for r in rs if r["trend"] in GOOD_TREND
                and r["category"] not in BAD_CAT and (r["percent"] or 0) < 10]

    def r7set(rs):
        return [r for r in r5set(rs)
                if (bdnum(r, "time_bonus") is None or bdnum(r, "time_bonus") <= 0)
                and (bdnum(r, "rps_bonus") is None or bdnum(r, "rps_bonus") <= 0)]

    sk = lambda r: (r["_tier"] if r["_tier"] is not None else 9, -(r["percent"] or 0))  # noqa: E731

    for tag, rs in (("IS", isr), ("OOS", oos)):
        print(f"\n────── {tag} ──────")
        show("全部买入", portfolio(rs))
        show("只买 🎯（档0）", portfolio([r for r in rs if r["_mark"] is True]))
        show("排除档3", portfolio([r for r in rs if r["_tier"] is not None and r["_tier"] < 3]))
        show("R5 好trend+排坏类+涨幅<10", portfolio(r5set(rs)))
        show("R7 = R5+非尾盘+非高RPS", portfolio(r7set(rs)))
        for k in args.topk:
            show(f"Top{k} in R7", portfolio(r7set(rs), topk=k, sort_key=sk))

    print("\n" + "=" * 108)
    print("过拟合判据：IS 与 OOS 同号且量级接近 → 规则有真实边际；")
    print("           OOS 大幅衰减或反号 → IS 结果多为噪音，不可采用。")
    print("=" * 108)

    d = portfolio(oos)
    if d:
        print(f"\nOOS 区间基准：全买累计 {d['cum']:+.1f}%，日胜率 {d['win']:.1f}%")


if __name__ == "__main__":
    main()
