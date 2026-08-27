#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""档3 警示劣后 → 按「劣后原因」拆开的次日表现归因。

背景（2026-08-24 用户反馈）：用户凭感觉常倾向在档3 里选票。prevday_perf 显示档3
整体最差，但市场分层里震荡日档3 反超档0、历史测量里 8-10% 陷阱带近端曾反转——
说明「档3 差」可能由个别原因子集拖累，其余子集未必差。本脚本把档3 单票逐日重建
（today_report 同源管线），按命中原因拆桶统计次日表现，验证用户直觉对应哪个子集。

口径：
  - 档位/原因判定复用 ranking/today_report 同源函数（_entry_tier/_entry_overbought/
    _entry_band/_entry_dims），与今日报告防漂移；一票多原因时计入每桶。
  - 表现 = 落库 next_day_pct；统计复用 prevday_perf._stats（内部走主决策口径
    nextday_attribution._hit_stats）。
  - 定位：离线测量工具，不调参不落库不进扫描路径；样本 <30 的桶只看方向不下结论。
"""

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import Any, cast

cast(Any, sys.stdout).reconfigure(encoding="utf-8")  # Windows 中文输出（同 prevday_perf）

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 项目根（脚本目录运行）

from prevday_perf import DIMS_COMPLETE_SINCE, _gem_market_avg, _next_day_map, _stats  # noqa: E402
from scanner.config import DB_PATH  # noqa: E402
from scanner.data_health import check_kline_health, health_banner  # noqa: E402
from scanner.database import get_today_recommendations  # noqa: E402
from scanner.ranking import (  # noqa: E402
    TIER3_REASONS,
    _entry_dims,
    _entry_tier,
    _is_nextday_marked,
    build_accum_map,
    entry_tier_reasons,
)
from scanner.ranking_snapshot import load_ranking_snapshot  # noqa: E402

REASONS = TIER3_REASONS


def _flow_of(e, d, flow_map):
    flow = d.get("fund_flow_main_pct")
    if flow is None:
        flow = flow_map.get(e["symbol"])
    try:
        return float(flow) if flow is not None else None
    except (TypeError, ValueError):
        return None


def collect(conn, dates):
    """→ {reason: [next_day_pct]}（近端窗口）+ 全期对照 + 市场分层。"""
    agg_recent: dict[str, list] = {}
    agg_all: dict[str, list] = {}
    market: dict[str, dict[str, list]] = {}  # reason -> {普涨/震荡/普跌: [pct]}
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
        snap_rows = load_ranking_snapshot(conn, dt)
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
            # 档位/原因优先读当日 ranking_snapshot 存证（2026-08-26 起收盘定稿落库）——
            # ranking 判定代码日后演进时历史归因不被「最新代码重放」篡改；
            # 无快照日期（功能上线前）回退现算（entry_tier_reasons 单源，与档位判定/
            # today_report 避雷汇总同源；原本地副本小板块共振漏 v_*_sector 成员门已对齐）。
            snap = snap_rows.get((e["symbol"], e["category"]))
            if snap is not None:
                tier = snap["tier"]
                reasons = list(snap["reasons"])
            else:
                marked = _is_nextday_marked(e, conn, accum_map=accum_map)
                tier = _entry_tier(e, conn, accum_map=accum_map, marked=marked)
                d = _entry_dims(e)
                flow = _flow_of(e, d, flow_map)
                reasons = entry_tier_reasons(e, accum=accum_map.get(e["symbol"]), marked=marked, flow=flow)
            if tier != 3:
                continue
            recent_ok = dt >= DIMS_COMPLETE_SINCE
            for r in reasons or ["(无原因·兜底)"]:
                agg_all.setdefault(r, []).append(p)
                market.setdefault(r, {}).setdefault(mbucket, []).append(p)
                if recent_ok:
                    agg_recent.setdefault(r, []).append(p)
    return agg_recent, agg_all, market, n_days


def render(agg_recent, agg_all, market, n_days):
    out = [f"\n◆ 档3 劣后原因 × 次日表现（近端窗口 {DIMS_COMPLETE_SINCE} 起 / 共 {n_days} 个有效交易日）"]

    def table(title, agg):
        out.append(f"\n{title}")
        out.append(f"  {'劣后原因':<16}{'n':>5} {'均次日':>8} {'hit≥7%':>8} {'胜率':>7} {'中位':>8}")
        out.append("  " + "-" * 56)
        rows = [(r, _stats(agg.get(r, []))) for r in REASONS if r in agg]
        rows += [("(无原因·兜底)", _stats(agg["(无原因·兜底)"]))] if "(无原因·兜底)" in agg else []
        for label, s in sorted(rows, key=lambda x: -(x[1][1] or -99)):
            n, avg, hit, win, med = s
            if n == 0:
                continue

            def fmt(v):
                return "—".rjust(8) if v is None else f"{v:+.2f}%".rjust(8)

            h = "—".rjust(8) if hit is None else f"{hit:.1f}%".rjust(8)
            w = "—".rjust(7) if win is None else f"{win:.1f}%".rjust(7)
            out.append(f"  {label:<16}{n:>5} {fmt(avg)} {h} {w} {fmt(med)}")

    table("一、维度完整窗口（主表）", agg_recent) if agg_recent else out.append("\n（近端窗口无样本）")
    if agg_all and agg_all != agg_recent:
        table("二、全期对照", agg_all)

    out.append("\n三、原因 × 市场环境 hit%（n）")
    out.append(f"  {'劣后原因':<16}{'普涨日':>14}{'震荡日':>14}{'普跌日':>14}")
    out.append("  " + "-" * 58)
    for r in REASONS:
        if r not in market:
            continue
        cells = []
        for b in ("普涨", "震荡", "普跌"):
            s = _stats(market[r].get(b, []))
            cells.append(f"{s[2]:.1f}%({s[0]})" if s[0] else "—")
        out.append(f"  {r:<16}{cells[0]:>14}{cells[1]:>14}{cells[2]:>14}")

    out.append("\n  说明：一票多原因重复计入；n<30 的桶只看方向。离线测量，不调参不落库。")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description="档3 劣后原因 × 次日表现归因")
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
    agg_recent, agg_all, market, n_days = collect(conn, dates)
    conn.close()
    print(render(agg_recent, agg_all, market, n_days))


if __name__ == "__main__":
    main()
