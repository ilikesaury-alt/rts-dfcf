"""特征有效性的日期分层稳健性检验（配套 cs_feature_research.py）。

检查：①每个特征在 5 个交易日里 Q5-Q1 的 hit 差是否稳定同号；
②「高位≥12%」的极端 hit 是否由单日主导；③组合特征的增量区分度。
"""
from __future__ import annotations

from collections import defaultdict

from cs_feature_research import add_features, load_rows


def q5q1_by_day(rows, key):
    by_day = defaultdict(list)
    for r in rows:
        if isinstance(r.get(key), (int, float)):
            by_day[r["date"]].append(r)
    print(f"\n=== {key}: 逐日 Q5(高) vs Q1(低) hit≥7% ===")
    diffs = []
    for day in sorted(by_day):
        grp = sorted(by_day[day], key=lambda r: r[key])
        n = len(grp)
        lo = grp[:max(n // 5, 1)]
        hi = grp[-max(n // 5, 1):]
        hlo = sum(r["next_day_pct"] >= 7 for r in lo) / len(lo) * 100
        hhi = sum(r["next_day_pct"] >= 7 for r in hi) / len(hi) * 100
        base = sum(r["next_day_pct"] >= 7 for r in grp) / n * 100
        diffs.append(hhi - hlo)
        print(f"  {day} n={n:4d} base={base:5.2f}% Q1={hlo:5.2f}% Q5={hhi:5.2f}% 差={hhi - hlo:+6.2f}pp")
    print(f"  → 同号天数 {sum(d > 0 for d in diffs)}/{len(diffs)} 为正, 均值 {sum(diffs)/len(diffs):+.2f}pp")


def high_pos_by_day(rows):
    print("\n=== 高位≥12% 的逐日分布 ===")
    by_day = defaultdict(lambda: [0, 0, 0.0])  # [n, hit, sum_pct]
    for r in rows:
        if isinstance(r.get("percent"), (int, float)) and r["percent"] >= 12:
            b = by_day[r["date"]]
            b[0] += 1
            b[1] += r["next_day_pct"] >= 7
            b[2] += r["next_day_pct"]
    for day in sorted(by_day):
        n, h, s = by_day[day]
        print(f"  {day}: n={n} hit={h/n*100:.2f}% avg={s/n:+.2f}%")


def combo(rows):
    print("\n=== 组合条件（末轮快照）===")
    def stat(label, pred):
        grp = [r for r in rows if pred(r)]
        if len(grp) < 30:
            print(f"  {label}: n={len(grp)} (不足)")
            return
        hit = sum(r["next_day_pct"] >= 7 for r in grp) / len(grp) * 100
        avg = sum(r["next_day_pct"] for r in grp) / len(grp)
        print(f"  {label}: n={len(grp):5d} hit={hit:5.2f}% avg={avg:+.2f}%")
    def p90_util(r):
        return r.get("pct_rank_rank") is not None and r["pct_rank_rank"] >= 0.8
    def persist_hi(r):
        return r["persist"] >= 13
    stat("pct_rank Top20%", p90_util)
    stat("persist>=13", persist_hi)
    stat("pct_rank Top20% & persist>=13", lambda r: p90_util(r) and persist_hi(r))
    stat("pct_rank Top20% & persist<5", lambda r: p90_util(r) and r["persist"] < 5)
    stat("当日涨幅Top20% & 主力Top20%", lambda r: (r.get("percent_rank") or 0) >= 0.8 and (r.get("main_net_rank") or 0) >= 0.8)
    stat("高位≥12% & 三窗全正", lambda r: isinstance(r.get("percent"), (int, float)) and r["percent"] >= 12 and r["cons_pos"])
    stat("低位蓄势<3% & persist>=13", lambda r: isinstance(r.get("percent"), (int, float)) and r["percent"] < 3 and r["persist"] >= 13)
    stat("放量滞涨(额涌Top20% & 1轮速Bottom20%)", lambda r: (r.get("amount_surge_rank") or 0) >= 0.8 and (r.get("speed_1_rank") or 1) <= 0.2)


def main():
    rows = add_features(load_rows())
    for key in ["pct_rank_rank", "persist", "percent_rank", "main_net_rank", "vol_ratio_rank", "speed_3_rank", "price"]:
        q5q1_by_day(rows, key)
    high_pos_by_day(rows)
    combo(rows)


if __name__ == "__main__":
    main()
