# -*- coding: utf-8 -*-
"""次日大涨规则挖掘: 命中/未命中特征对比 + 规则样本内/样本外双窗口评估。

用法:
    python rule_eval.py                          # 运行内置 RULES + 特征对比
    python rule_eval.py --since 2026-06-01       # 自定义样本内起点
    python rule_eval.py --list                   # 只打印数据概况

自定义规则: 复制本文件, 修改 WIN_HOT / BAD_CAT / RULES 后运行。
注意: 必须以文件方式运行(python rule_eval.py), 不要通过 PowerShell 管道喂代码,
否则中文字面量乱码导致 trend 集合匹配静默失败。
"""

import argparse
import json
import sqlite3
import statistics as stat
import sys
from collections import Counter
from pathlib import Path

reconfigure = getattr(sys.stdout, "reconfigure", None)
if reconfigure is not None:
    reconfigure(encoding="utf-8")

DB = Path(__file__).resolve().parents[4] / "scanner.db"  # .pi/skills/<skill>/scripts/ -> 仓库根
HIT_THRESHOLD = 7.0  # next_day_pct >= 7% 视为命中

# 走强形态(双窗口验证为正)与弱类别, 修改规则时从这里取
WIN_HOT = {"加速启动", "动量延续", "低位企稳", "超跌企稳", "放量反弹"}
BAD_CAT = {"short_term", "comeback", "pool_pick", "pullback"}

# 内置规则集: (名称, 规则kwargs)。kwargs 见 rule() 签名。
RULES: list[tuple[str, dict]] = [
    ("trend in 走强形态", {"trend_hot": True}),
    ("排除弱类别", {"nocat": True}),
    ("小盘(cap<=1)", {"cap": True}),
    ("组合: 走强+排除弱类别", {"trend_hot": True, "nocat": True}),
    ("组合: 走强+排除弱类别+小盘", {"trend_hot": True, "nocat": True, "cap": True}),
    ("当日涨幅>=5%", {"pct": 5}),
    ("资金主流占比>=4", {"ff": 4}),
    ("上榜动量bonus>=12 (样本外已证伪)", {"lm": 12}),
]


def load_data():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        """SELECT date,time,symbol,name,category,score,percent,trend,
           accumulated_pct,score_breakdown,next_day_pct
           FROM recommendations WHERE next_day_pct IS NOT NULL ORDER BY date,time"""
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    # 去重: 每 (date, symbol) 取最后一行, 偏好有当日涨幅的
    best: dict = {}
    for r in rows:
        k = (r["date"], r["symbol"])
        b = best.get(k)
        if b is None or (r["percent"] is not None) >= (b["percent"] is not None):
            best[k] = r
    data = list(best.values())
    for r in data:
        try:
            r["P"] = json.loads(r["score_breakdown"] or "{}")
        except Exception:
            r["P"] = {}
    return data


def rule(r, trend_hot=False, nocat=False, cap=False, pct=None, ff=None, lm=None, streak=None, cat=None):
    p = r["P"]
    checks = []
    if trend_hot:
        checks.append(r["trend"] in WIN_HOT)
    if nocat:
        checks.append(r["category"] not in BAD_CAT)
    if cap:
        v = p.get("market_cap_bonus")
        checks.append(isinstance(v, (int, float)) and v <= 1)
    if pct is not None:
        v = r["percent"]
        checks.append(v is not None and v >= pct)
    if ff is not None:
        v = p.get("fund_flow_main_pct")
        checks.append(isinstance(v, (int, float)) and v >= ff)
    if lm is not None:
        v = p.get("list_momentum_bonus")
        checks.append(isinstance(v, (int, float)) and v >= lm)
    if streak is not None:
        v = p.get("list_streak_bonus")
        checks.append(isinstance(v, (int, float)) and v >= streak)
    if cat is not None:
        checks.append(r["category"] in cat)
    return all(checks) if checks else False


FEATURES = [
    "fund_flow_main_pct",
    "list_momentum_bonus",
    "list_streak_bonus",
    "list_top40_bonus",
    "market_cap_bonus",
    "turnover_bonus",
    "v_st_vol",
    "v_st_sector",
    "validation_bonus",
    "rps_bonus",
    "live_vol_bonus",
    "opening_score",
    "intraday_score",
]


def feature_comparison(hits, misses):
    print(f"\n--- 特征对比 (hit n={len(hits)}, miss n={len(misses)}, 中位数) ---")
    print(f"{'feature':<24}{'hit':>9}{'miss':>9}{'差':>8}")
    for f in FEATURES:
        hv = [r["P"].get(f) for r in hits]
        mv = [r["P"].get(f) for r in misses]
        hv = [v for v in hv if isinstance(v, (int, float))]
        mv = [v for v in mv if isinstance(v, (int, float))]
        if len(hv) < 5 or len(mv) < 5:
            print(f"{f:<24}{'样本不足':>9}{len(mv):>9}")
            continue
        d = stat.median(hv) - stat.median(mv)
        print(f"{f:<24}{stat.median(hv):>9.2f}{stat.median(mv):>9.2f}{d:>+8.2f}")


def dist_report(sub, label):
    hits = [r for r in sub if r["next_day_pct"] >= HIT_THRESHOLD]
    print(f"\n--- {label}: trend 命中率 (hit n={len(hits)}) ---")
    ch = Counter(r["trend"] for r in hits)
    cm = Counter(r["trend"] for r in sub if r["next_day_pct"] < HIT_THRESHOLD)
    for t in sorted(set(ch) | set(cm), key=lambda t: -(ch.get(t, 0))):
        nh, nm = ch.get(t, 0), cm.get(t, 0)
        if nh == 0 and nm < 10:
            continue
        print(f"{t:<16} hit={nh:>3} miss={nm:>4} 命中率={nh / (nh + nm) * 100:>5.1f}%")
    print(f"\n--- {label}: category 命中率 ---")
    ch = Counter(r["category"] for r in hits)
    cm = Counter(r["category"] for r in sub if r["next_day_pct"] < HIT_THRESHOLD)
    for t in sorted(set(ch) | set(cm), key=lambda t: -(ch.get(t, 0))):
        nh, nm = ch.get(t, 0), cm.get(t, 0)
        print(f"{t:<16} hit={nh:>3} miss={nm:>4} 命中率={nh / (nh + nm) * 100:>5.1f}%")


def evaluate(name, data, a, b, **kw):
    sub = [r for r in data if a <= r["date"] <= b]
    if not sub:
        return
    sel = [r for r in sub if rule(r, **kw)]
    nh_base = sum(1 for r in sub if r["next_day_pct"] >= HIT_THRESHOLD)
    base = nh_base / len(sub) * 100
    if sel:
        nh = sum(1 for r in sel if r["next_day_pct"] >= HIT_THRESHOLD)
        hr = nh / len(sel) * 100
        avg = stat.mean(r["next_day_pct"] for r in sel)
        print(
            f"{name:<40} 选出{len(sel):>4} 命中{nh:>3} {hr:>5.1f}% (基线{base:>4.1f}%, {hr - base:+5.1f}pp) 平均次日{avg:+6.2f}%"
        )
    else:
        print(f"{name:<40} 选出   0")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-08-01", help="样本内窗口起点")
    ap.add_argument("--list", action="store_true", help="只打印数据概况")
    args = ap.parse_args()

    data = load_data()
    dates = sorted(r["date"] for r in data)
    print(f"去重后共 {len(data)} 条 ({dates[0]} ~ {dates[-1]}), DB={DB}")
    if args.list:
        return

    # 样本外窗口 = 数据最早日 ~ 样本内起点前一天
    out_a, out_b = dates[0], args.since
    in_a, in_b = args.since, dates[-1]
    for label, a, b in [(f"样本内 {in_a}~{in_b}", in_a, in_b), (f"样本外 {out_a}~{out_b}", out_a, out_b)]:
        print(f"\n===== {label} =====")
        for name, kw in RULES:
            evaluate(name, data, a, b, **kw)
        sub = [r for r in data if a <= r["date"] <= b]
        nh = sum(1 for r in sub if r["next_day_pct"] >= HIT_THRESHOLD)
        print(f"基线: 总{len(sub)} 命中{nh} ({nh / len(sub) * 100:.1f}%)")

    sub_in = [r for r in data if in_a <= r["date"] <= in_b]
    hits = [r for r in sub_in if r["next_day_pct"] >= HIT_THRESHOLD]
    misses = [r for r in sub_in if r["next_day_pct"] < HIT_THRESHOLD]
    feature_comparison(hits, misses)
    dist_report(sub_in, "样本内")
    sub_out = [r for r in data if out_a <= r["date"] <= out_b]
    dist_report(sub_out, "样本外")


if __name__ == "__main__":
    main()
