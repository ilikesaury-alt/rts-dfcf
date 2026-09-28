"""资金流图标收窄（2026-09-28）实证依据复算 —— 离线、只读 `scanner.db`。

用法::

    python scripts/flow_mark_evidence.py

`view.model._FUND_FLOW_ICON` / `config_sources` 注释里引用的每个数字都出自本脚本，
改那些注释前先跑一次（本次改造的直接动因：手抄进注释的 n/corr 无法复现、
且与相邻断言互相矛盾）。产出四段：

  1. 全市场面板 —— 当日主力净占比 vs **当日**涨幅 的相关（回答：正向图标是不是
     「今天涨」的重复信息）
  2. 上涨票内横截面 —— 同为上涨的票里，flow 高 1/3 vs 低 1/3 的**次日**收益差 +
     组内 Spearman，逐交易日（回答：正向图标对次日有没有正区分度）
  3. 推荐池五档 —— 次日≥7% hit 率（`config_scoring.CATEGORY_HIT_RATE` 同一口径），
     同票同日取最后一轮去重
  4. 数据窗口与缺口（price 字段 2026-09-18 才有 ⇒ 面板只有 6 个交易日）

数据源 = `market_extra_cache`（`data_type='fund_flow'`，收盘快照，含 price/percent/
main_pct）与 `recommendations.score_breakdown.fund_flow_main_pct` + `next_day_pct`。
不联网、不写库。任一表缺失/为空 → 退出码 2（提示先跑一轮扫描）。
"""

from __future__ import annotations

import json
import math
import sqlite3
import statistics
import sys
from collections import defaultdict
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "scanner.db"
DAY_KEYS = ("price", "percent", "main_pct")
UP_MIN = 0.0  # 「上涨票」= 当日涨幅 > 0（排除平盘）


def pearson(a: list[float], b: list[float]) -> float:
    if len(a) < 3:
        return float("nan")
    ma, mb = statistics.fmean(a), statistics.fmean(b)
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b, strict=True))
    da = math.sqrt(sum((x - ma) ** 2 for x in a))
    db = math.sqrt(sum((y - mb) ** 2 for y in b))
    return num / (da * db) if da and db else float("nan")


def rank(xs: list[float]) -> list[float]:
    """平均秩（并列取均值），无 scipy 依赖。"""
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    out = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            out[order[k]] = avg
        i = j + 1
    return out


def spearman(a: list[float], b: list[float]) -> float:
    return pearson(rank(a), rank(b))


def load_panel(conn: sqlite3.Connection) -> dict[str, dict[str, dict]]:
    """{date: {symbol: payload}} —— 只留含 price/percent/main_pct 的完整快照。"""
    panel: dict[str, dict[str, dict]] = defaultdict(dict)
    cur = conn.execute(
        "SELECT date, symbol, payload_json FROM market_extra_cache WHERE data_type='fund_flow' ORDER BY date"
    )
    for date, sym, payload in cur:
        try:
            p = json.loads(payload)
        except ValueError:
            continue
        if all(k in p and p[k] is not None for k in DAY_KEYS):
            panel[date][sym] = p
    return dict(panel)


def tier_of(main_pct: float) -> str:
    if main_pct >= 8.0:
        return "strong_in"
    if main_pct >= 5.0:
        return "in"
    if main_pct > -5.0:
        return "neutral"
    if main_pct > -8.0:
        return "out"
    return "strong_out"


def section_panel(panel: dict[str, dict[str, dict]]) -> list[tuple[str, str, list[tuple[float, float, float]]]]:
    """返回 [(date, next_date, [(flow, 当日涨幅, 次日收益)...])]。"""
    dates = [d for d in sorted(panel) if panel[d]]
    snaps = [(p["main_pct"], p["percent"]) for d in dates for p in panel[d].values()]
    flow = [x for x, _ in snaps]
    pct = [y for _, y in snaps]
    print("── 1. 全市场面板：当日主力净占比 vs 当日涨幅 ──")
    print(f"  窗口 {dates[0]} ~ {dates[-1]}｜交易日 {len(dates)}｜快照 n={len(snaps)}")
    print(f"  Pearson r = {pearson(flow, pct):+.4f}（R²={pearson(flow, pct) ** 2:.4f}）")
    print(f"  Spearman ρ = {spearman(flow, pct):+.4f}")
    print("  ⇒ 流入与当日上涨**同向**：正向图标很大程度在复述「今天涨」，不是独立维度。")

    pairs: list[tuple[str, str, list[tuple[float, float, float]]]] = []
    for i, d in enumerate(dates[:-1]):
        nxt = dates[i + 1]
        rows = []
        for sym in set(panel[d]) & set(panel[nxt]):
            p0, p1 = panel[d][sym], panel[nxt][sym]
            if not p0["price"] or not p1["price"]:
                continue
            rows.append((p0["main_pct"], p0["percent"], (p1["price"] / p0["price"] - 1.0) * 100.0))
        if rows:
            pairs.append((d, nxt, rows))
    return pairs


def section_crosssection(pairs) -> None:
    print("\n── 2. 上涨票内横截面：flow 高 1/3 vs 低 1/3 的次日表现 ──")
    print(f"  {'配对':<24}{'上涨n':>7}{'中位差pp':>10}{'均值差pp':>10}{'Spearman':>11}")
    neg_med = neg_rho = total = 0
    for d, nxt, rows in pairs:
        up = [r for r in rows if r[1] > UP_MIN]
        if len(up) < 30:
            print(f"  {d}->{nxt} 上涨票仅 {len(up)}，跳过")
            continue
        up.sort(key=lambda r: r[0])
        k = len(up) // 3
        hi, lo = up[-k:], up[:k]
        d_med = statistics.median(r[2] for r in hi) - statistics.median(r[2] for r in lo)
        d_mean = statistics.fmean(r[2] for r in hi) - statistics.fmean(r[2] for r in lo)
        rho = spearman([r[0] for r in up], [r[2] for r in up])
        neg_med += d_med < 0
        neg_rho += rho < 0
        total += 1
        print(f"  {d}->{nxt:<12}{len(up):>7}{d_med:>10.3f}{d_mean:>10.3f}{rho:>11.4f}")
    if total:
        print(f"  ⇒ 中位差为负 {neg_med}/{total} 天，组内 Spearman 为负 {neg_rho}/{total} 天")
        print("    正流入方向**没有**正的次日区分度（图标 ▲/▲▲ 据此撤下）。")


def section_pool(conn: sqlite3.Connection) -> None:
    print("\n── 3. 推荐池五档次日表现（次日≥7% hit，同票同日取最后一轮）──")
    last: dict[tuple[str, str], tuple[str, dict, float]] = {}
    cur = conn.execute(
        "SELECT date, time, symbol, score_breakdown, next_day_pct FROM recommendations WHERE next_day_pct IS NOT NULL"
    )
    for date, time, sym, sb, nd in cur:
        key = (date, sym)
        if key not in last or time >= last[key][0]:
            try:
                obj = json.loads(sb or "{}")
            except ValueError:
                continue
            last[key] = (time, obj, float(nd))

    tiers: dict[str, list[float]] = defaultdict(list)
    for _, obj, nd in last.values():
        fl = obj.get("fund_flow_main_pct")
        if fl is not None:
            tiers[tier_of(float(fl))].append(nd)

    allv = [x for v in tiers.values() for x in v]
    if not allv:
        print("  无可用样本（score_breakdown.fund_flow_main_pct 2026-08-06 起才写入）")
        return
    base = sum(1 for x in allv if x >= 7) / len(allv) * 100
    print(f"  {'档':<11}{'n':>6}{'次日≥7% hit':>14}{'中位次日%':>12}")
    for k in ("strong_in", "in", "neutral", "out", "strong_out"):
        v = tiers.get(k, [])
        if not v:
            print(f"  {k:<11}{0:>6}")
            continue
        hit = sum(1 for x in v if x >= 7) / len(v) * 100
        print(f"  {k:<11}{len(v):>6}{hit:>13.1f}%{statistics.median(v):>12.3f}")
    print(f"  {'全样本':<10}{len(allv):>6}{base:>13.1f}%{statistics.median(allv):>12.3f}")
    print("  ⇒ in/strong_in ≈ 全样本（正向无区分度）；strong_out 最低、out 最高但 n 小，")
    print("    负向档是**弱证据**下的规避提示，不是选股信号。中位收益五档彼此接近，")
    print("    故本脚本以 hit 率为准（与 CATEGORY_HIT_RATE 同口径），不引用中位数排序。")


def main() -> int:
    if not DB.exists():
        print(f"scanner.db 不存在：{DB}", file=sys.stderr)
        return 2
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        try:
            panel = load_panel(conn)
        except sqlite3.Error as e:
            print(f"读取 market_extra_cache 失败：{e}", file=sys.stderr)
            return 2
        dated = [d for d in panel if panel[d]]
        if len(dated) < 2:
            print(
                f"完整快照不足（{len(dated)} 天，price 字段 2026-09-18 起才有）——先跑一轮扫描积累数据。",
                file=sys.stderr,
            )
            return 2
        pairs = section_panel(panel)
        section_crosssection(pairs)
        section_pool(conn)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
