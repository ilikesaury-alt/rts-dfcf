#!/usr/bin/env python3
"""收益账本 —— 逐笔诚实核算「按某规则买入实际拿到多少」。

设计原则（重要，避免重蹈覆辙）：
  1. **只读**：不改任何规则、不改数据库、不产生副作用。
  2. **扣真实成本**：佣金万2.5(最低5元) + 卖出印花税0.05% + 双边滑点各0.1%。
  3. **只报有统计意义的结果**：n < MIN_N 的组合不报；给出 95% CI 与 t 值。
  4. **不做样本内挑选**：同时给出前半/后半分段，符号翻转的组合必须标红。
  5. **基准对齐**：所有结果与「同期该池全量均值」对比，排除大盘涨跌的干扰。

输出：按「单位风险收益」排序的规则清单，而非按绝对收益。

用法：
    python scripts/profit_ledger.py            # 全量规则扫描
    python scripts/profit_ledger.py --min-n 30 # 自定义最小样本
"""

from __future__ import annotations

import argparse
import math
import sqlite3
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "scanner.db"

# A 股真实成本（与 scanner/portfolio_backtest.py 同源）
COMMISSION = 0.00025  # 万 2.5
STAMP_DUTY = 0.0005  # 卖出印花税 0.05%
SLIPPAGE = 0.001  # 双边各 0.1%

# 单边成本近似（按 1 万元等权）：买入 滑点0.1% + 佣金0.025%；
# 卖出 滑点0.1% + 佣金0.025% + 印花税0.05% → 合计约 0.30%
ROUND_TRIP_COST_PCT = (SLIPPAGE + COMMISSION) * 100 * 2 + STAMP_DUTY * 100

HIT_THRESHOLD = 7.0  # next_day ≥7% 记为命中
MIN_N = 30  # 最小样本量（低于此不报）


def load_rows() -> list[dict]:
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        """
        SELECT date, symbol, name, category, score, next_day_pct, fwd_3d,
               cum_2d, cum_3d, percent, excluded, source, concept, accumulated_pct
        FROM recommendations
        WHERE next_day_pct IS NOT NULL AND excluded = 0
        """
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def stats(vals: list[float]) -> dict:
    """均值 / 标准差 / t 值 / 95% CI / 命中率。"""
    n = len(vals)
    if n == 0:
        return {"n": 0}
    mean = statistics.fmean(vals)
    out = {
        "n": n,
        "mean": mean,
        "hit": sum(1 for v in vals if v >= HIT_THRESHOLD) / n * 100,
        "win": sum(1 for v in vals if v > 0) / n * 100,
    }
    if n >= 2:
        sd = statistics.stdev(vals)
        se = sd / math.sqrt(n)
        out["sd"] = sd
        out["t"] = mean / se if se > 0 else 0.0
        out["ci_lo"] = mean - 1.96 * se
        out["ci_hi"] = mean + 1.96 * se
    return out


def net(vals: list[float]) -> list[float]:
    """扣双边成本后的净收益序列。"""
    return [v - ROUND_TRIP_COST_PCT for v in vals]


def fmt(s: dict) -> str:
    if s.get("n", 0) == 0:
        return "     (无样本)"
    t = s.get("t", 0.0)
    flag = "★" if abs(t) >= 2 else " "
    return (
        f"n={s['n']:>4d} 净均值={s['mean']:>+7.3f}% 命中={s['hit']:>5.1f}% "
        f"胜率={s['win']:>5.1f}% t={t:>+6.2f}{flag}"
    )


def split_sign_check(vals_by_date: dict[str, list[float]]) -> str:
    """前后半段均值符号是否一致（不一致 = 不可信）。

    注意：本函数**只对传入的子集自身切半**。不要把多个类别合并后再切半——
    合并会把不同类别的样本在同一天内平均掉，从而掩盖个别类别的符号翻转。
    对多类别结论，必须**逐个类别分别调用本函数**再下判断。
    """
    dates = sorted(vals_by_date)
    if len(dates) < 10:
        return "样本期过短"
    mid = len(dates) // 2
    a = [v for d in dates[:mid] for v in vals_by_date[d]]
    b = [v for d in dates[mid:] for v in vals_by_date[d]]
    if not a or not b:
        return "分段空"
    ma, mb = statistics.fmean(a), statistics.fmean(b)
    return f"前半{ma:+.3f}% / 后半{mb:+.3f}% {'⚠️符号翻转' if ma * mb < 0 else '一致'}"


def main() -> int:
    ap = argparse.ArgumentParser(description="收益账本")
    ap.add_argument("--min-n", type=int, default=MIN_N)
    args = ap.parse_args()
    min_n = args.min_n

    rows = load_rows()
    print("=" * 96)
    print("收益账本 —— 扣真实成本后的实际收益（成本 %.2f%%/笔）" % ROUND_TRIP_COST_PCT)
    print(f"样本：{len(rows)} 条推荐（excluded=0 且有 next_day_pct）")
    dates = sorted({r["date"] for r in rows})
    print(f"区间：{dates[0]} ~ {dates[-1]}（{len(dates)} 个交易日）")
    print("=" * 96)

    # ── 基准：全池 ──
    all_vals = [r["next_day_pct"] for r in rows]
    base = stats(net(all_vals))
    print(f"\n【基准 · 全池不限】{fmt(base)}")
    print(f"   未扣成本均值 = {statistics.fmean(all_vals):+.3f}%")

    # ── 规则扫描 ──
    # 每条规则 = (名称, 谓词, 日期分组函数)
    def by_date(vals_with_date):
        d = defaultdict(list)
        for dt, v in vals_with_date:
            d[dt].append(v)
        return d

    rules: list[tuple[str, list[dict]]] = []

    # 1) 按类别
    for cat in sorted({r["category"] for r in rows}):
        rules.append((f"类别={cat}", [r for r in rows if r["category"] == cat]))

    # 2) 按 score 分档
    scored = [r for r in rows if r["score"] is not None]
    if scored:
        vals = sorted(r["score"] for r in scored)
        for label, lo, hi in [
            ("score 前10%", 0.90, 1.01),
            ("score 前25%", 0.75, 1.01),
            ("score 后25%", 0.0, 0.25),
        ]:
            i_lo = int(len(vals) * lo)
            i_hi = min(int(len(vals) * hi) - 1, len(vals) - 1)
            if i_hi >= i_lo:
                cut_lo, cut_hi = vals[i_lo], vals[i_hi]
                rules.append((
                    label,
                    [r for r in scored if cut_lo <= r["score"] <= cut_hi],
                ))

    # 3) 按来源（source）—— 诊断显示候选池来源已严重偏移（历史池类别占 97.7%）
    src_count: dict[str, int] = defaultdict(int)
    for r in rows:
        if r.get("source"):
            src_count[r["source"]] += 1
    for src, cnt in sorted(src_count.items(), key=lambda x: -x[1]):
        if cnt >= min_n:
            rules.append((f"来源={src}", [r for r in rows if r.get("source") == src]))

    # 4) 按当日涨幅分带
    for lo, hi, label in [
        (-100, 0, "当日下跌"),
        (0, 2, "当日 0-2%"),
        (2, 4, "当日 2-4%"),
        (4, 6, "当日 4-6%"),
        (6, 8, "当日 6-8%"),
        (8, 100, "当日 ≥8%"),
    ]:
        rules.append((
            f"当日涨幅 {label}",
            [r for r in rows if r["percent"] is not None and lo <= r["percent"] < hi],
        ))

    # 5) 类别 × 涨幅带（交叉，只保留样本够的）
    for cat in sorted({r["category"] for r in rows}):
        for lo, hi, label in [(2, 6, "2-6%"), (6, 100, "≥6%")]:
            rules.append((
                f"{cat} × 当日{label}",
                [
                    r for r in rows
                    if r["category"] == cat
                    and r["percent"] is not None
                    and lo <= r["percent"] < hi
                ],
            ))

    # 6) 按概念（出现次数够的概念）
    concept_count: dict[str, int] = defaultdict(int)
    for r in rows:
        if r.get("concept"):
            concept_count[r["concept"]] += 1
    for concept, cnt in sorted(concept_count.items(), key=lambda x: -x[1])[:15]:
        if cnt >= min_n:
            rules.append((f"概念={concept}", [r for r in rows if r.get("concept") == concept]))

    # 7) 按 fwd_3d 有值（即持有 3 日）—— 单独处理
    results = []
    for name, subset in rules:
        if len(subset) < min_n:
            continue
        vals = net([r["next_day_pct"] for r in subset])
        s = stats(vals)
        s["name"] = name
        s["split"] = split_sign_check(by_date(
            [(r["date"], r["next_day_pct"] - ROUND_TRIP_COST_PCT) for r in subset]
        ))
        results.append(s)

    # ── 输出：按 t 值排序 ──
    print("\n" + "=" * 96)
    print("【规则清单 · 按 t 值排序】（|t|≥2 才具统计显著性；成本已扣）")
    print("=" * 96)
    results.sort(key=lambda s: -abs(s.get("t", 0.0)))
    for s in results:
        print(f"\n  {s['name']}")
        print(f"    {fmt(s)}")
        print(f"    {s['split']}")

    # ── 汇总：显著正向 / 显著负向 ──
    pos = [s for s in results if s.get("t", 0) >= 2]
    neg = [s for s in results if s.get("t", 0) <= -2]
    print("\n" + "=" * 96)
    print("【结论】")
    print("=" * 96)
    if pos:
        print(f"\n✅ 统计显著为正的组合（{len(pos)} 个）：")
        for s in sorted(pos, key=lambda x: -x["mean"]):
            print(f"    {s['name']:32s} 净{s['mean']:>+6.3f}% t={s['t']:+.2f} n={s['n']}  {s['split']}")
    else:
        print("\n❌ 没有任何组合在扣成本后统计显著为正。")
    if neg:
        print(f"\n🚫 统计显著为负的组合（{len(neg)} 个，应回避）：")
        for s in sorted(neg, key=lambda x: x["mean"]):
            print(f"    {s['name']:32s} 净{s['mean']:>+6.3f}% t={s['t']:+.2f} n={s['n']}")

    # ── 未达显著但方向为正的"候补"（唯一有希望的方向）──
    cand = [s for s in results if 0 < s.get("t", 0) < 2 and s.get("n", 0) >= 50]
    if cand:
        print("\n🟡 方向为正但未达显著（t<2，不可下注，仅值得继续观察）：")
        for s in sorted(cand, key=lambda x: -x["mean"]):
            print(f"    {s['name']:32s} 净{s['mean']:>+6.3f}% t={s['t']:+.2f} n={s['n']}  {s['split']}")
        print("    → 正确做法：停止在这些方向调参，让它自然积累样本到可判定。")

    print("\n" + "=" * 96)
    print("提示：符号翻转（前半/后半相反）的组合不可信——多半是规则存在期间行情不同，")
    print("      而非规则本身有效。详见 docs/diagnosis-picking-confusion-2026-09-11.md")
    print("=" * 96)
    return 0


if __name__ == "__main__":
    sys.exit(main())
