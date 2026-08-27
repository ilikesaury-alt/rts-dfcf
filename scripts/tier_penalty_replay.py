#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""档位「级联一票否决」vs「扣分制」对照回放（2026-08-26）。

背景：现行 _entry_tier 是警示因子短路链——命中任意一个警示因子直接落档3，
多个警示因子与单个警示因子结果相同。扣分制假设：多因子叠加的票应比单因子
边缘票更劣后。本脚本用同一批历史样本测量两种方案的分离度，回答「是否值得切换」。

方法：
  - 逐日重建推荐（today_report 同源管线），对每票取现行档位（_entry_tier）+
    警示因子命中（entry_tier_reasons 单源），按扣分权重求和：
    过热 3 / 超买 2 / 涨幅带死区·陷阱 2 / 小板块共振 1 / 主力净流出 1。
  - 对照三组口径：
    A）现行：档3（级联，命中任一因子即劣后）vs 档2；
    B）扣分制：扣 0 分 / 扣 1 分 / 扣 2 分 / 扣 ≥3 分 四桶的次日表现单调性；
    C）交叉：现行档3 内按扣分深度拆分（验证「档3 差是否全由深扣票拖累」）。
  - 表现 = 落库 next_day_pct；统计复用 prevday_perf._stats（主决策口径）。

判读标准（切换门槛）：扣分桶单调（0 > 1 > 2 > ≥3 的 hit 依次下降或持平）
且 ≥3 桶显著差于现行档3 整体 → 才值得改 _entry_tier 实现；否则维持现状。定位：离线测量工具，不调参不落库不进扫描路径。
"""

import argparse
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Any, cast

cast(Any, sys.stdout).reconfigure(encoding="utf-8")  # Windows 中文输出

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 项目根（脚本目录运行）

from prevday_perf import DIMS_COMPLETE_SINCE, _gem_market_avg, _next_day_map, _stats  # noqa: E402
from scanner.config import DB_PATH  # noqa: E402
from scanner.data_health import check_kline_health, health_banner  # noqa: E402
from scanner.database import get_today_recommendations  # noqa: E402
from scanner.ranking import (  # noqa: E402
    TIER3_REASONS,
    TIER_REASON_BAND,
    TIER_REASON_FUND_OUTFLOW,
    TIER_REASON_OVERBOUGHT,
    TIER_REASON_OVERHEAT,
    TIER_REASON_SECTOR,
    _entry_dims,
    _entry_tier,
    _is_nextday_marked,
    build_accum_map,
    entry_tier_reasons,
)

# 扣分权重（脚本本地常量：本工具只测量不调参，权重若经数据支持升级为正式实现，
# 届时才迁入 config 并改 _entry_tier——见模块 docstring 判读标准）
PENALTY_WEIGHTS = {
    TIER_REASON_OVERHEAT: 3,
    TIER_REASON_OVERBOUGHT: 2,
    TIER_REASON_BAND: 2,
    TIER_REASON_SECTOR: 1,
    TIER_REASON_FUND_OUTFLOW: 1,
}
PENALTY_BUCKETS = ("扣0", "扣1", "扣2", "扣≥3")


def _penalty(reasons):
    return sum(PENALTY_WEIGHTS.get(r, 0) for r in reasons)


def _bucket(penalty):
    if penalty <= 0:
        return PENALTY_BUCKETS[0]
    if penalty == 1:
        return PENALTY_BUCKETS[1]
    if penalty == 2:
        return PENALTY_BUCKETS[2]
    return PENALTY_BUCKETS[3]


def _flow_of(e, d, flow_map):
    flow = d.get("fund_flow_main_pct")
    if flow is None:
        flow = flow_map.get(e["symbol"])
    try:
        return float(flow) if flow is not None else None
    except (TypeError, ValueError):
        return None


def collect(conn, dates):
    """→ 行列表 [{date, pct, mbucket, tier, penalty, reasons}] + 有效交易日数。"""
    rows = []
    n_days = 0
    for dt in dates:
        recs = cast("list[dict]", get_today_recommendations(conn, as_of=dt))
        if not recs:
            continue
        nd_map = _next_day_map(conn, dt)
        if not nd_map:
            continue
        n_days += 1
        accum_map = build_accum_map(conn, recs)
        flow_map = {}
        try:
            from scanner.database import get_fund_flow_pct_map

            flow_map = get_fund_flow_pct_map(conn, [e["symbol"] for e in recs], as_of=dt)
        except Exception:
            pass
        mkt = _gem_market_avg(conn, dt)
        mbucket = (
            "普涨" if (mkt is not None and mkt >= 1.0) else "普跌" if (mkt is not None and mkt <= -1.0) else "震荡"
        )
        for e in recs:
            if e["category"] in ("comeback", "core_dip"):
                continue
            p = nd_map.get(e["symbol"])
            if p is None:
                continue
            marked = _is_nextday_marked(e, conn, accum_map=accum_map)
            tier = _entry_tier(e, conn, accum_map=accum_map, marked=marked)
            d = _entry_dims(e)
            flow = _flow_of(e, d, flow_map)
            reasons = entry_tier_reasons(e, accum=accum_map.get(e["symbol"]), marked=marked, flow=flow)
            rows.append(
                {
                    "date": dt,
                    "pct": p,
                    "mbucket": mbucket,
                    "tier": tier,
                    "penalty": _penalty(reasons),
                    "reasons": reasons,
                    # 可评估警示因子的人群（🎯 档0/rebound 档1/comeback 豁免票不评估
                    # 警示因子、天然扣0——混入会稀释扣分桶对照）
                    "evaluable": (not marked) and e["category"] not in ("rebound", "comeback"),
                }
            )
    return rows, n_days


def render(rows, n_days):
    out = [f"\n◆ 档位级联 vs 扣分制对照回放（近端窗口 {DIMS_COMPLETE_SINCE} 起 / 共 {n_days} 个有效交易日）"]

    def table(title, subset):
        out.append(f"\n{title}")
        out.append(f"  {'分组':<18}{'n':>5} {'均次日':>8} {'hit≥7%':>8} {'胜率':>7} {'中位':>8}")
        out.append("  " + "-" * 60)

        def line(label, items):
            n, avg, hit, win, med = _stats([r["pct"] for r in items])
            if n == 0:
                out.append(f"  {label:<18}{0:>5} {'—':>8} {'—':>8} {'—':>7} {'—':>8}")
                return
            fmt = lambda v: "—".rjust(8) if v is None else f"{v:+.2f}%".rjust(8)  # noqa: E731
            h = "—".rjust(8) if hit is None else f"{hit:.1f}%".rjust(8)
            w = "—".rjust(7) if win is None else f"{win:.1f}%".rjust(7)
            out.append(f"  {label:<18}{n:>5} {fmt(avg)} {h} {w} {fmt(med)}")

        line("现行档2（无警示）", [r for r in subset if r["tier"] == 2])
        line("现行档3（任一命中）", [r for r in subset if r["tier"] == 3])
        ev = [r for r in subset if r["evaluable"]]
        for b in PENALTY_BUCKETS:
            line(f"扣分桶[{b}]", [r for r in ev if _bucket(r["penalty"]) == b])

    recent = [r for r in rows if r["date"] >= DIMS_COMPLETE_SINCE] or rows
    table("一、近端窗口（维度完整）", recent)
    if rows != recent:
        table("二、全期对照", rows)

    # 三、现行档3 内按扣分深度拆分
    t3 = [r for r in recent if r["tier"] == 3]
    out.append("\n三、现行档3 内部 × 扣分深度（验证「档3 差由谁拖累」）")
    if t3:
        dist = Counter(_bucket(r["penalty"]) for r in t3)
        out.append(f"  分布：{' '.join(f'{b}={dist.get(b, 0)}' for b in PENALTY_BUCKETS)}")
        for b in PENALTY_BUCKETS:
            items = [r for r in t3 if _bucket(r["penalty"]) == b]
            if items:
                s = _stats([r["pct"] for r in items])
                h = f"{s[2]:.1f}%" if s[2] is not None else "—"
                avg = f"{s[1]:+.2f}%" if s[1] is not None else "—"
                out.append(f"  [{b}] n={s[0]} 均{avg} hit {h}")
    else:
        out.append("  （近端窗口无档3 样本）")

    # 四、单因子速览（各原因独立成桶，一票多原因重复计入）
    out.append("\n四、单因子速览（近端窗口，多因子票重复计入）")
    for r_name in TIER3_REASONS:
        items = [r for r in recent if r_name in r["reasons"]]
        if items:
            s = _stats([r["pct"] for r in items])
            h = f"{s[2]:.1f}%" if s[2] is not None else "—"
            avg = f"{s[1]:+.2f}%" if s[1] is not None else "—"
            out.append(f"  {r_name:<14}(权重{PENALTY_WEIGHTS[r_name]}) n={s[0]} 均{avg} hit {h}")

    # 五、单调性判定（仅在可评估警示因子的人群内）
    out.append("\n五、扣分桶单调性（近端窗口·仅可评估人群）")
    ev_recent = [r for r in recent if r["evaluable"]]
    hits = []
    for b in PENALTY_BUCKETS:
        s = _stats([r["pct"] for r in ev_recent if _bucket(r["penalty"]) == b])
        hits.append(s[2])
    known = [(b, h) for b, h in zip(PENALTY_BUCKETS, hits) if h is not None]
    mono = all(known[i][1] >= known[i + 1][1] for i in range(len(known) - 1)) if len(known) >= 3 else False
    ns = {b: _stats([r["pct"] for r in ev_recent if _bucket(r["penalty"]) == b])[0] for b in PENALTY_BUCKETS}
    min_n = min(ns.values()) if ns else 0
    if len(known) < 3 or min_n < 30:
        out.append(f"  样本不足（min桶 n={min_n}），暂无法判定——积累样本后重跑本脚本")
    elif mono:
        out.append(
            "  ✅ 单调成立：扣分越深次日越差——扣分制分离度优于现行二值级联，样本复核达标后可评估切换 _entry_tier 实现"
        )
    else:
        out.append(
            f"  ❌ 单调不成立（{'/'.join(f'{b}:{h:.1f}%' if h is not None else '—' for b, h in known)}）"
            "——维持现行级联，差异留档本脚本输出供后续复查"
        )
    out.append("\n  说明：离线测量，不调参不落库；扣分为脚本本地权重（见 PENALTY_WEIGHTS），非生产实现。")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description="档位级联 vs 扣分制对照回放")
    ap.add_argument("--days", type=int, default=30, help="最近 N 个交易日（0=全期）")
    ap.add_argument("--force", action="store_true", help="跳过数据健康检查")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH, timeout=15)
    dates = [r[0] for r in conn.execute("SELECT DISTINCT date FROM recommendations ORDER BY date").fetchall()]
    dates = [d for d in dates if _next_day_map(conn, d)]
    if args.days > 0:
        dates = dates[-args.days :]
    if not args.force:
        rep = check_kline_health(conn, dates=dates or None)
        b = health_banner(rep)
        if b:
            print(b)
        if rep.blocked:
            print("  [中止] 数据疑似污染，先跑 python repair_kline.py")
            conn.close()
            return
    rows, n_days = collect(conn, dates)
    conn.close()
    print(render(rows, n_days))


if __name__ == "__main__":
    main()
