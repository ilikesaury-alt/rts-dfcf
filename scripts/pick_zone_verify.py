"""精选决策区规则历史回放验证（2026-08-17，临时诊断脚本，不进入扫描路径）。

对全部历史推荐（去重口径与 nextday_attribution 一致：同 date+symbol 保留最高分），
用 display._analyze_entry（当前线上规则）判定 推荐/参考/回避，统计各组：
  - next_day≥7% 命中率（次日大涨口径）
  - 平均次日收益
  - cum_3d 均值（持有 2-3 天口径，部分行落库缺失）

回答：精选决策规则把「推荐组」选出来的票是否真的优于全样本/参考/回避组。
"""
import json
import sqlite3
import sys

sys.path.insert(0, ".")
from scanner.display import _analyze_entry  # noqa: E402

THRESHOLD = 7.0


def _dedup(conn):
    recs = []
    for r in conn.execute(
        "SELECT symbol, date, category, score, percent, trend, next_day_pct, cum_3d, "
        "score_breakdown FROM recommendations "
        "WHERE category NOT IN ('pullback') AND COALESCE(excluded, 0) = 0"
    ):
        sb = {}
        if r[8]:
            try:
                sb = json.loads(r[8])
            except Exception:
                sb = {}
        recs.append({
            "symbol": r[0], "date": r[1], "category": r[2], "score": r[3],
            "percent": r[4], "trend": r[5], "next_day_pct": r[6], "cum_3d": r[7],
            "score_breakdown": sb,
        })
    best = {}
    for d in recs:
        k = (d["date"], d["symbol"])
        if k not in best or d["score"] > best[k]["score"]:
            best[k] = d
    return list(best.values())


def _stats(sub, label):
    n = len(sub)
    if n == 0:
        print(f"{label:<14} n=0")
        return
    nd = [d["next_day_pct"] for d in sub if d["next_day_pct"] is not None]
    c3 = [d["cum_3d"] for d in sub if d["cum_3d"] is not None]
    hit = sum(1 for v in nd if v >= THRESHOLD)
    avg_nd = sum(nd) / len(nd) if nd else float("nan")
    avg_c3 = sum(c3) / len(c3) if c3 else float("nan")
    print(f"{label:<14} n={n:>5} hit={hit:>4} ({hit / n * 100:>5.1f}%) "
          f"均次日={avg_nd:>+6.2f}  cum_3d={avg_c3:>+6.2f}  (cum_3d样本{len(c3)})")


def main():
    conn = sqlite3.connect("scanner.db")
    recs = _dedup(conn)
    conn.close()
    print(f"去重样本: {len(recs)}")
    print(f"{'组':<14}{'样本':>6}{'hit':>5} {'hit率':>8}{'均次日':>9}{'cum_3d':>9}")
    print("-" * 60)

    groups = {"推荐": [], "参考": [], "回避": []}
    for d in recs:
        e = {"symbol": d["symbol"], "category": d["category"], "_candidate": None,
             "score_breakdown": d["score_breakdown"], "percent": d["percent"] or 0.0,
             "date": d["date"], "accumulated_pct": None}
        advice, _ = _analyze_entry(e, conn)
        groups[advice].append(d)
    _stats(recs, "全样本基准")
    for g in ("推荐", "参考", "回避"):
        _stats(groups[g], g)

    # 推荐组内按类别细分
    print("\n--- 推荐组按类别 ---")
    for cat in ("rebound", "comeback", "short_term", "momentum", "known_new_face", "new_face"):
        sub = [d for d in groups["推荐"] if d["category"] == cat]
        _stats(sub, cat)
    # 回避组内按原因细分（取最常见原因）
    print("\n--- 回避组按首原因 ---")
    from collections import Counter
    reason_cnt = Counter()
    for d in recs:
        e = {"symbol": d["symbol"], "category": d["category"], "_candidate": None,
             "score_breakdown": d["score_breakdown"], "percent": d["percent"] or 0.0,
             "date": d["date"], "accumulated_pct": None}
        advice, reasons = _analyze_entry(e, conn)
        if advice == "回避" and reasons:
            reason_cnt[reasons[0]] += 1
    for reason, cnt in reason_cnt.most_common(8):
        sub = []
        for d in recs:
            e = {"symbol": d["symbol"], "category": d["category"], "_candidate": None,
                 "score_breakdown": d["score_breakdown"], "percent": d["percent"] or 0.0,
                 "date": d["date"], "accumulated_pct": None}
            advice, reasons = _analyze_entry(e, conn)
            if advice == "回避" and reasons and reasons[0] == reason:
                sub.append(d)
        _stats(sub, reason)


if __name__ == "__main__":
    main()
