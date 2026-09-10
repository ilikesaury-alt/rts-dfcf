# -*- coding: utf-8 -*-
"""美感门（日线6硬门 + 分时 intraday_score）对次日大涨的预测力验证。

回答的问题：美感门能否替换现有筛门（追涨门 / momentum 禁入 / 涨幅带）？

方法（防前视）：
  - 日线美感：只用推荐日**之前**的 bar（date < rec_date）——推荐时刻已知信息，
    无前视偏差。与实盘门含当日部分 bar 的口径差异由分时维度覆盖（分时就是当日信息）。
  - 分时美感：落库 score_breakdown 的 intraday_score（推荐时刻快照，与实盘门同源）。
  - 判定单源：直接 import scanner.trend_beauty（与实盘门完全同一实现）。
  - 命中口径：next_day_pct >= 7（与规则挖掘一致）。
  - 双窗口：样本内 >= --since，样本外 < --since（不可省，防过拟合）。

用法：
    python .pi/skills/nextday-rule-mining/scripts/beauty_gate_eval.py
    python .pi/skills/nextday-rule-mining/scripts/beauty_gate_eval.py --since 2026-08-01
"""

import argparse
import json
import sqlite3
import statistics as stat
import sys
from pathlib import Path

reconfigure = getattr(sys.stdout, "reconfigure", None)
if reconfigure is not None:
    reconfigure(encoding="utf-8")

DB = Path(__file__).resolve().parents[4] / "scanner.db"
HIT = 7.0

sys.path.insert(0, str(DB.parent))
from scanner.trend_beauty import evaluate_daily_trend, evaluate_intraday_beauty  # noqa: E402


def load_recs():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT date,time,symbol,name,category,score,percent,trend,
           accumulated_pct,score_breakdown,next_day_pct
           FROM recommendations WHERE next_day_pct IS NOT NULL ORDER BY date,time"""
    ).fetchall()
    klines = {}
    for sym, dt, o, h, low, c, pct in conn.execute(
        "SELECT symbol, date, open, high, low, close, percent FROM daily_kline ORDER BY symbol, date"
    ):
        klines.setdefault(sym, []).append({"date": dt, "open": o, "high": h, "low": low, "close": c, "percent": pct})
    conn.close()

    # 去重：每 (date, symbol) 取最后一轮，偏好 percent 非空
    def _pref(d):
        return (1 if d["percent"] is not None else 0, d["time"])

    best = {}
    for r in rows:
        d = dict(r)
        try:
            d["P"] = json.loads(d["score_breakdown"] or "{}")
        except Exception:
            d["P"] = {}
        k = (d["date"], d["symbol"])
        b = best.get(k)
        if b is None or _pref(d) > _pref(b):
            best[k] = d
    data = list(best.values())
    # 日线美感（T-1 及更早，无前视）+ 分时美感（落库快照）
    for d in data:
        hist = [k for k in klines.get(d["symbol"], []) if k["date"] < d["date"]]
        d["daily_fail"], _s, d["daily_detail"] = evaluate_daily_trend(hist)
        d["intra_fail"], d["intra_detail"] = evaluate_intraday_beauty({"score_breakdown": d["score_breakdown"]}, None)
        # 综合门（与实盘终选/决策同口径）：任一可判定的丑 → 拦
        d["blocked"] = bool(d["daily_fail"] or d["intra_fail"])
        d["hit"] = d["next_day_pct"] >= HIT
    return data


def fmt(rows, label):
    n = len(rows)
    if n == 0:
        return f"{label:<28} n=0"
    hits = sum(1 for r in rows if r["hit"])
    nds = [r["next_day_pct"] for r in rows]
    return f"{label:<28} n={n:>5}  hit={hits / n:>5.1%}  avg={stat.mean(nds):+5.2f}%  med={stat.median(nds):+5.2f}%"


def nds(rows):
    return [r["next_day_pct"] for r in rows]


def bucket(v):
    if v is None:
        return "缺"
    if v <= -3:
        return "≤-3"
    if v <= -1:
        return "-3~-1"
    if v < 1:
        return "-1~+1"
    if v < 2.5:
        return "+1~+2.5"
    if v < 5:
        return "+2.5~+5"
    return "≥+5"


def pct_band(p):
    if p is None:
        return "缺"
    if p < 0:
        return "<0"
    if p < 2:
        return "0-2"
    if p < 4:
        return "2-4"
    if p < 6:
        return "4-6"
    if p < 8:
        return "6-8"
    return "≥8"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-08-01", help="样本内起点（之前的为样本外）")
    args = ap.parse_args()
    since = args.since

    data = load_recs()
    print(f"样本：去重后 {len(data)} 条（有 next_day_pct 回填）")

    # 字段存在率
    n_intra = sum(
        1 for d in data if isinstance(d["P"].get("intraday_score"), (int, float)) and d["P"]["intraday_score"] != 0.0
    )
    print(f"intraday_score 非缺/非零：{n_intra}/{len(data)}")
    dates = sorted({d["date"] for d in data})
    print(f"日期范围：{dates[0]} ~ {dates[-1]}")

    for win, label in (
        (lambda d: d["date"] >= since, f"样本内(>={since})"),
        (lambda d: d["date"] < since, f"样本外(<{since})"),
    ):
        sub = [d for d in data if win(d)]
        print(f"\n{'=' * 78}\n【{label}】基线：" + fmt(sub, "全部"))
        if not sub:
            continue
        print(fmt([d for d in sub if not d["blocked"]], "美感门放行"))
        print(fmt([d for d in sub if d["blocked"]], "美感门拦截"))
        print(
            fmt(
                [
                    d
                    for d in sub
                    if not d["blocked"]
                    and d["daily_fail"] is None
                    and d["intra_fail"] is None
                    and d["daily_detail"] != "日线不足"
                    and d["intra_detail"] != "分时缺失"
                ],
                "双门均可判且过",
            )
        )

        # 分时分数分桶
        print("\n— 分时 intraday_score 分桶 —")
        groups = {}
        for d in sub:
            v = d["P"].get("intraday_score")
            v = v if isinstance(v, (int, float)) and v != 0.0 else None
            groups.setdefault(bucket(v), []).append(d)
        for k in sorted(groups, key=lambda x: (x == "缺", x)):
            print(fmt(groups[k], f"  分时{k}"))

        # 美感门 × 类别（momentum 反例检验）
        print("\n— 美感门放行内 × 类别 —")
        passed = [d for d in sub if not d["blocked"]]
        groups = {}
        for d in passed:
            groups.setdefault(d["category"], []).append(d)
        for k, g in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            print(fmt(g, f"  {k}"))

        # 美感门 × 当日涨幅带（能否替代追涨门/死区？）
        print("\n— 美感门放行内 × 当日涨幅带 —")
        groups = {}
        for d in passed:
            groups.setdefault(pct_band(d["percent"]), []).append(d)
        for k in ["<0", "0-2", "2-4", "4-6", "6-8", "≥8", "缺"]:
            if k in groups:
                print(fmt(groups[k], f"  涨幅{k}"))

        # 替换检验 A：放行内追涨门还有无增量
        p8 = [d for d in passed if d["percent"] is not None and d["percent"] > 8]
        print(fmt(p8, "  [检验A] 放行内 涨幅>8（追涨门该拦的）"))

        # 替换检验 B：放行内 momentum 禁入还有无增量
        mom = [d for d in passed if d["category"] == "momentum"]
        print(fmt(mom, "  [检验B] 放行内 momentum（负先验该拦的）"))

        # 拦截面拆解：日线 vs 分时各拦了多少、各自质量
        print("\n— 拦截面拆解 —")
        print(fmt([d for d in sub if d["daily_fail"]], "  仅日线拦"))
        print(fmt([d for d in sub if d["intra_fail"]], "  仅分时拦"))
        print(fmt([d for d in sub if d["daily_fail"] and d["intra_fail"]], "  双拦"))


if __name__ == "__main__":
    main()
