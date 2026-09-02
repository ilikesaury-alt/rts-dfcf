#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""单日综合排序 → 次日表现 验证（反推是否符合预期）。

取指定交易日(默认=最近一个有次日表现的交易日，即"昨日榜单→今日表现"视角)的
「综合排序」各组(档0🎯/档1/档2/档3/回马枪/核心低吸/被移出)，回放其次日表现
(next_day_pct)，逐项校验排序是否符合预期：

  - 档位单调性：均次日 档0 ≥ 档1 ≥ 档2 ≥ 档3
  - 🎯 命中率：档0 内 ≥+7% 比例应显著高于其他档
  - 避雷有效性：被移出组次日应最差
  - 回马枪/核心低吸 是否有独立价值

复用 today_report._build_report（与今日报告同源、档位/🎯 判定防漂移）+ prevday_perf._stats
（主决策口径统计，防漂移）。定位：综合排序展示层质量自检尺，非调参、不进扫描路径。

用法：
    python scripts/ranking_validate_day.py                  # 最近一个有次日表现的交易日
    python scripts/ranking_validate_day.py --date 2026-08-26
"""

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import Any, cast

cast(Any, sys.stdout).reconfigure(encoding="utf-8")  # Windows 中文输出

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 项目根

from prevday_perf import DIMS_COMPLETE_SINCE, _gem_market_avg, _next_day_map, _stats  # noqa: E402
from scanner.config import DB_PATH  # noqa: E402
from today_report import _build_report  # noqa: E402

GROUPS = ("tier0", "tier1", "tier2", "tier3", "comeback", "core_dip", "excluded")
GROUP_LABEL = {
    "tier0": "档0 🎯",
    "tier1": "档1 强信号",
    "tier2": "档2 普通",
    "tier3": "档3 警示劣后",
    "comeback": "回马枪",
    "core_dip": "核心低吸",
    "excluded": "被移出",
}


def _fmt_pct(v, width=7):
    if v is None:
        return "—".rjust(width)
    return f"{v:+.2f}%".rjust(width)


def _fmt2(v, n):
    if not n or v is None:
        return "—"
    return "%.1f%%" % v


def _resolve_target(conn: sqlite3.Connection, date_arg):
    dates = [r[0] for r in conn.execute("SELECT DISTINCT date FROM recommendations ORDER BY date")]
    valid = [d for d in dates if _next_day_map(conn, d)]
    if date_arg:
        return date_arg if date_arg in valid else None
    return valid[-1] if valid else None


def main():
    ap = argparse.ArgumentParser(description="单日综合排序 → 次日表现 验证")
    ap.add_argument("--date", default=None, help="目标交易日 YYYY-MM-DD（默认最近一个有次日表现的交易日）")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH, timeout=15)
    target = _resolve_target(conn, args.date)
    if target is None:
        print("无可用目标日（recommendations 缺次日回填）。")
        conn.close()
        return

    rep = _build_report(conn, target, 0)
    if rep.get("empty"):
        print(f"{target} 综合排序为空，无法验证。")
        conn.close()
        return

    nd_map = _next_day_map(conn, target)
    mkt = _gem_market_avg(conn, target)

    print("=" * 78)
    print(f"综合排序 → 次日表现 验证  目标日 {target} / 次日 {target}→")
    print("=" * 78)
    print("  数据源：today_report._build_report 同源重建；表现=次日收盘(next_day_pct)")
    print(
        f"  市场环境(推荐日 GEM 均值)：{_fmt_pct(mkt, 0)}"
        f"  | 维度完整性：{'完整' if target >= DIMS_COMPLETE_SINCE else '退化(档0判定不可信)'}"
    )
    print(f"  命中阈值：≥+{7:.0f}%（与主决策口径一致）\n")

    # 各组统计
    agg = {}
    for g in GROUPS:
        key = "comeback_flow" if g == "comeback" else g
        syms = [e["symbol"] for e in rep.get(key, [])]
        pcts = [nd_map[s] for s in syms if s in nd_map]
        agg[g] = pcts
        n, avg, hit, win, med = _stats(pcts)
        label = GROUP_LABEL[g]
        line = f"  {label:<14} n={n:>3}  均次日 {_fmt_pct(avg, 8)}  "
        line += f"hit≥7% {_fmt2(hit, n):>7}  胜率 {_fmt2(win, n):>7}  中位 {_fmt_pct(med, 8)}"
        print(line)

    # 🎯 档0 内部类别拆分
    t0_cats: dict[str, list] = {}
    for a in rep.get("tier0", []):
        if a["category"] == "short_term" and a.get("pos") == "弱转强低位":
            key = "short_term·弱转强"
        elif a["category"] == "short_term":
            key = "short_term·其他"
        else:
            key = a["category"]
        p = nd_map.get(a["symbol"])
        if p is not None:
            t0_cats.setdefault(key, []).append(p)
    if t0_cats:
        print("\n  🎯 档0 内部（类别 × 次日）：")
        for k, v in sorted(t0_cats.items()):
            n, avg, hit, _, _ = _stats(v)
            print(f"    {k:<18} n={n:>2}  均 {_fmt_pct(avg, 8)}  hit≥7% {_fmt2(hit, n)}")

    # 逐票明细（档0/档3 + 回马枪）
    def _dump(group_key, label):
        items = []
        for e in rep.get(group_key, []):
            nd = nd_map.get(e["symbol"])
            if nd is None:
                continue
            try:
                nd_f = float(nd)
            except (TypeError, ValueError):
                continue
            items.append((e["symbol"], e.get("name", ""), e["score"], nd_f))
        if not items:
            return
        print(f"\n  {label}（按次日涨幅降序）：")
        for sym, name, score, nd in sorted(items, key=lambda x: x[3], reverse=True):
            print(f"    {sym} {str(name)[:6]:<6} 评分{score:>3}  次日 {_fmt_pct(nd)}")

    _dump("tier0", "■ 档0 🎯 明细")
    _dump("tier3", "■ 档3 警示 明细")
    _dump("comeback_flow", "■ 回马枪 明细")

    # 符合预期判定
    print("\n" + "-" * 78)
    print("  符合预期判定：")
    checks = []
    t0, t1, t2, t3 = (
        _stats(agg["tier0"])[1],
        _stats(agg["tier1"])[1],
        _stats(agg["tier2"])[1],
        _stats(agg["tier3"])[1],
    )
    # 单调性
    mono_pairs = [("档0≥档1", t0, t1), ("档1≥档2", t1, t2), ("档2≥档3", t2, t3)]
    for name, hi, lo in mono_pairs:
        if hi is None or lo is None:
            checks.append((name, "样本不足", "—"))
        else:
            ok = hi >= lo
            checks.append((name, "✓ 符合" if ok else "✗ 不符合", f"{hi:+.2f}% vs {lo:+.2f}%"))
    # 🎯 命中率高于其他档
    h0 = _stats(agg["tier0"])[2]
    hothers = []
    for g in ("tier1", "tier2", "tier3"):
        n, _a, hit, _w, _m = _stats(agg[g])
        if n > 0 and hit is not None:
            hothers.append(hit)
    if h0 is not None and hothers:
        hmax = max(hothers)
        checks.append(
            ("🎯命中率最高", "✓ 符合" if h0 >= hmax else "✗ 不符合", f"档0 {h0:.1f}% vs 其余最高 {hmax:.1f}%")
        )
    # 被移出最差
    ex = _stats(agg["excluded"])[1]
    if ex is not None:
        others = [v for v in (t0, t1, t2, t3) if v is not None]
        worst = min(others) if others else None
        if worst is not None:
            checks.append(
                ("被移出最差", "✓ 符合" if ex <= worst else "✗ 不符合", f"被移出 {ex:+.2f}% vs 主表最低 {worst:+.2f}%")
            )

    for name, verdict, detail in checks:
        print(f"    [{verdict}] {name}：{detail}")

    passed = sum(1 for _, v, _ in checks if v.startswith("✓"))
    total = len(checks)
    print(
        f"\n  结论：{passed}/{total} 项符合预期"
        + (" —— 综合排序当日有效 ✅" if passed == total else " —— 存在偏差，详见上（单日样本小，结论仅供参考）")
    )
    conn.close()


if __name__ == "__main__":
    main()
