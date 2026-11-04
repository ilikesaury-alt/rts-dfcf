# -*- coding: utf-8 -*-
"""概率常数变更的排序 A/B 验证台（audit §B1/§B2 配套，2026-09-13 新增）。

用途：任何 `nextday_prob.py` 常数改动**上生产前**，先用历史样本量化它对排序的影响。
影响面说明（2026-09-16 更新）：这些常数只参与 `final_pick` 的 `_p` —— 而 `_p` 自
2026-09-16 起**已不是终选排序键**（排序改由 `final_pick._final_sort_key` 意图驱动）。
故本台现在度量的是「参考展示值 `_p` 的相对排序」，**不再等价于终选顺序**；作为
常数改动的影响面证据时须据此打折。`ranking.entry_tier` 不吃这些常数（🎯 亦已降为
纯展示标记，不再提档）。

指标口径（代理终选）：
  - rank-IC      ：全体样本 Spearman(_p, next_day_pct)（排序量整体单调性）
  - top-1 / top-2：逐日按 _p 取前 1 / 前 2 的次日 hit≥7% 命中率
  - picks_avg_nd ：上述 top-2 的平均次日涨幅
  - 改选日        ：终选（类别序列）相对基线发生变化的交易日数

⚠ 代理口径须与生产一致：2026-09-14 起 `final_pick` 已删除 `category != "momentum"`
的无条件剔除（目标函数统一为 hit 率），本台的 pool 过滤同步删除，否则测的不是线上。

⚠ 这是**样本内**验证：候选常数的值本身是从同一批样本算出来的，因此对「按实测值
重算」的变体天然有利，不能当作样本外证据。它的正确用法是**证伪**——若某个改动
在样本内都不改善头部指标，就没有理由上生产（2026-09-13 涨幅带 4 项即因此被否决）。

用法：
    python scripts/nextday_calib_ab.py
    python scripts/nextday_calib_ab.py --db scanner.db --days 0
"""

import argparse
import json
import sqlite3
import sys

sys.path.insert(0, ".")
# E402 为刻意设计：必须先 sys.path.insert(0, ".") 才能 import scanner.*
import scanner.nextday_prob as npb  # noqa: E402
from scanner import nextday_calib as nc  # noqa: E402
from scanner.backtest import spearman  # noqa: E402
from scanner.config import DB_PATH, NEXTDAY_HIT_THRESHOLD  # noqa: E402
from scanner.models import parse_score_breakdown  # noqa: E402
from scanner.nextday_attribution import attach_prominence, load_dedup  # noqa: E402

TH = NEXTDAY_HIT_THRESHOLD

# 常数家族（用于分项 A/B：一次只改一族，定位是哪个常数在起作用）
# 基线 = 当前代码常数；下列变体 = 基线 + 把该族常数换成快照里的实测值。
FAMILIES = {
    # 2026-09-16：OR_MARKED 已随 🎯 降级为纯展示标记而删除，不再有该因子族
    "改 OR_OVERBOUGHT→实测": ["OR_OVERBOUGHT"],
    "改 涨幅带→实测": ["OR_BAND_SWEET_LOW", "OR_BAND_DEAD", "OR_BAND_MID", "OR_BAND_TRAP"],
    "改 辨识度/流出/板块→实测": ["OR_PROMINENCE", "OR_OUTFLOW", "OR_SMALL_SECTOR"],
}


def _originals() -> dict:
    return {k: getattr(npb, k) for k in nc.CONSTANT_BY_KEY} | {
        "_rates": dict(npb.BASE_RATE_BY_CAT),
        "_default": npb.BASE_RATE_DEFAULT,
    }


def _restore(orig: dict) -> None:
    for k in nc.CONSTANT_BY_KEY:
        setattr(npb, k, orig[k])
    npb.BASE_RATE_BY_CAT.clear()
    npb.BASE_RATE_BY_CAT.update(orig["_rates"])
    npb.BASE_RATE_DEFAULT = orig["_default"]


def _load(conn, days: int) -> list[dict]:
    raw = load_dedup(conn, days=days)
    attach_prominence(conn, raw)
    for r in raw:
        r["score_breakdown"] = parse_score_breakdown(r.get("breakdown"))
        r["_candidate"] = None
    return [r for r in raw if r["next_day"] is not None]


def _evaluate(recs: list[dict]) -> dict:
    scored = [
        (
            r["date"][:10],
            r["category"],
            npb.next_day_hit_probability(r, prominence=r.get("_prominent"), flow=None),
            float(r["next_day"]),
        )
        for r in recs
    ]
    ic = spearman([s[2] for s in scored], [s[3] for s in scored]) or 0.0
    by_day: dict[str, list] = {}
    for d, cat, p, nd in scored:
        by_day.setdefault(d, []).append((p, nd, cat))
    t1h = t1n = t2h = t2n = 0
    nd_sum = 0.0
    picks: dict[str, tuple] = {}
    for d, lst in by_day.items():
        pool = sorted(lst, key=lambda x: -x[0])
        if pool:
            t1n += 1
            t1h += pool[0][1] >= TH
        top2 = pool[:2]
        if top2:
            t2n += 1
            t2h += sum(1 for x in top2 if x[1] >= TH)
            nd_sum += sum(x[1] for x in top2) / len(top2)
        picks[d] = tuple(x[2] for x in top2)
    return {
        "rank_ic": round(ic, 4),
        "top1_hit": round(t1h / max(t1n, 1), 4),
        "top2_hit": round(t2h / max(t2n, 1), 4),
        "picks_avg_next_day": round(nd_sum / max(t2n, 1), 4),
        "days": t2n,
        "picks": picks,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="概率常数变更的排序 A/B 验证台")
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--days", type=int, default=0)
    ap.add_argument("--json", action="store_true", help="输出机器可读结果")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    try:
        recs = _load(conn, args.days)
    finally:
        conn.close()
    if not recs:
        print("  [中止] 无可用样本")
        return 1

    orig = _originals()
    snap = nc.load_snapshot()
    measured = {k: v["measured"] for k, v in snap["factors"].items() if v["measured"]}
    measured_rates = {
        cat: v["measured"] for cat, v in snap["base_rates"].items() if v["measured"]
    }

    results: dict[str, dict] = {}
    # 基线 = 当前代码常数
    _restore(orig)
    results["基线(当前代码)"] = _evaluate(recs)

    for label, keys in FAMILIES.items():
        _restore(orig)
        for k in keys:
            if k in measured:
                setattr(npb, k, measured[k])
        results[label] = _evaluate(recs)

    _restore(orig)
    for cat, val in measured_rates.items():
        if cat == "_default":
            npb.BASE_RATE_DEFAULT = val
        else:
            npb.BASE_RATE_BY_CAT[cat] = val
    results["仅 base rate→实测"] = _evaluate(recs)

    _restore(orig)
    for k, v in measured.items():
        setattr(npb, k, v)
    for cat, val in measured_rates.items():
        if cat == "_default":
            npb.BASE_RATE_DEFAULT = val
        else:
            npb.BASE_RATE_BY_CAT[cat] = val
    results["全部按实测重算"] = _evaluate(recs)
    _restore(orig)

    base_picks = results["基线(当前代码)"]["picks"]
    if args.json:
        print(json.dumps(
            {k: {kk: vv for kk, vv in v.items() if kk != "picks"} for k, v in results.items()},
            ensure_ascii=False, indent=2,
        ))
        return 0

    print(f"样本 {len(recs)}（threshold≥{TH:.0f}%，代理终选=按 _p 取前 2）\n")
    print(f"{'变体':<22}{'rank-IC':>9}{'top1':>8}{'top2':>8}{'picks_nd':>10}{'改选日':>9}")
    print("-" * 68)
    for name, res in results.items():
        chg = sum(1 for d in res["picks"] if res["picks"][d] != base_picks.get(d))
        print(f"{name:<22}{res['rank_ic']:>9.4f}{res['top1_hit'] * 100:>7.1f}%"
              f"{res['top2_hit'] * 100:>7.1f}%{res['picks_avg_next_day']:>9.2f}%"
              f"{chg:>6}/{res['days']}")
    print("\n  ⚠ 样本内验证：对「按实测重算」的变体天然有利，只能用于证伪——")
    print("    样本内都不改善头部指标的改动没有理由上生产。改常数仍须过样本外验证（§B1）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
