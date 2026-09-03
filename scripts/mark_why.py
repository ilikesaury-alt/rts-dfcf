"""拆解 🎯 为什么在样本外是负收益。

🎯 判定三条件：涨幅甜蜜带(<2% 或 4~8%) + 非超买 + 5日累计≥NEXTDAY_ACCUM_MIN。
逐条件对比其后次日表现，定位是哪一条引入了负 alpha。

用法: python scripts/mark_why.py [--days 90]
"""

from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pick_portfolio import load, portfolio, show  # noqa: E402

from scanner.config import NEXTDAY_ACCUM_MIN  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    args = ap.parse_args()

    rows = load(args.days)
    mk = [r for r in rows if r["_mark"] is True]
    nm = [r for r in rows if r["_mark"] is False]

    print("=" * 100)
    print(f"🎯 拆解   带🎯 n={len(mk)}  不带 n={len(nm)}  "
          f"NEXTDAY_ACCUM_MIN={NEXTDAY_ACCUM_MIN}")
    print("=" * 100)

    print("\n【1. 5日累计涨幅分布：🎯 是否系统性挑了累涨高的票】")
    for tag, rs in (("带🎯", mk), ("不带🎯", nm)):
        acc = [r["_accum"] for r in rs if r["_accum"] is not None]
        if acc:
            print(f"  {tag:<8s} n={len(acc):<5d} 累涨 均值 {statistics.fmean(acc):+6.2f}%  "
                  f"中位 {statistics.median(acc):+6.2f}%  "
                  f"≥10%占比 {100 * sum(1 for a in acc if a >= 10) / len(acc):5.1f}%")

    print("\n【2. 带🎯 内部按累涨分层（组合口径）】")
    show("带🎯 且 累涨<10", portfolio([r for r in mk
                                    if r["_accum"] is not None and r["_accum"] < 10]))
    show("带🎯 且 累涨≥10", portfolio([r for r in mk
                                    if r["_accum"] is not None and r["_accum"] >= 10]))
    show("带🎯 且 累涨≥15", portfolio([r for r in mk
                                    if r["_accum"] is not None and r["_accum"] >= 15]))

    print("\n【3. 带🎯 内部按当日涨幅分层】")
    for lo, hi, lbl in ((-99, 2, "<2% 低吸潜伏"), (2, 4, "2-4% 死区"),
                        (4, 8, "4-8% 中段启动"), (8, 999, "≥8%")):
        sub = [r for r in mk if r["percent"] is not None and lo <= r["percent"] < hi]
        show(f"带🎯 涨幅{lbl}", portfolio(sub))

    print("\n【4. 对照组：不带🎯 的票按同样条件分】")
    show("不带🎯 且 累涨<10", portfolio([r for r in nm
                                     if r["_accum"] is not None and r["_accum"] < 10]))
    show("不带🎯 且 累涨≥10", portfolio([r for r in nm
                                     if r["_accum"] is not None and r["_accum"] >= 10]))

    print("\n【5. 结论性对比：🎯 加了 R5 过滤后是否变好】")
    GOOD = {"企稳回升", "主线回调", "回踩·到买点", "温和放量", "震荡整理", "低位企稳", "整理"}
    BAD_CAT = {"momentum", "pullback"}
    r5mk = [r for r in mk if r["trend"] in GOOD and r["category"] not in BAD_CAT
            and (r["percent"] or 0) < 10]
    show("带🎯 全部", portfolio(mk))
    show("带🎯 + R5过滤", portfolio(r5mk))
    show("不带🎯 + R5过滤", portfolio([r for r in nm if r["trend"] in GOOD
                                   and r["category"] not in BAD_CAT
                                   and (r["percent"] or 0) < 10]))

    print("\n【6. 关键：累涨门槛 NEXTDAY_ACCUM_MIN 的方向性检验】")
    for thr in (0, 3, 6, 10, 15):
        sub = [r for r in rows if r["_accum"] is not None and r["_accum"] >= thr]
        show(f"全样本 累涨≥{thr}%", portfolio(sub))


if __name__ == "__main__":
    main()
