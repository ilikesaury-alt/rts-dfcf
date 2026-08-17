"""🎯 分型前后命中率对比验证（2026-08-17，临时诊断脚本，不进入扫描路径）。

旧判定：所有类别 = 甜蜜带 + 非超买 + 累计门槛(rebound/short_term 豁免)
新判定：short_term = 弱转强 + 非超买（不看甜蜜带）；其余 = 甜蜜带 + 非超买 + 累计门槛

用 recommendations 落库 score_breakdown 模拟两版判定，对比 next_day≥7% 命中率。
"""
import json
import sqlite3

THRESHOLD = 7.0


def _load(conn):
    recs = []
    for r in conn.execute(
        "SELECT symbol, date, category, percent, next_day_pct, score_breakdown "
        "FROM recommendations WHERE category NOT IN ('pullback') AND excluded = 0"
    ):
        d = {"symbol": r[0], "date": r[1], "category": r[2], "percent": r[3],
             "next_day_pct": r[4]}
        try:
            d["sb"] = json.loads(r[5]) if r[5] else {}
        except Exception:
            d["sb"] = {}
        recs.append(d)
    best = {}
    for d in recs:
        k = (d["date"], d["symbol"])
        if k not in best:
            best[k] = d
    return list(best.values())


def _sweet(p):
    if p is None:
        return False
    return 0.0 <= p < 2.0 or 4.0 <= p < 8.0


def _overbought(d):
    sb = d["sb"]
    return bool(sb.get("v_st_overbought") or sb.get("v_mo_overbought")
                or sb.get("st_overbought_flag") or sb.get("mo_overbought_flag"))


def _weak(d):
    sb = d["sb"]
    return bool(sb.get("st_weak_to_strong") or sb.get("v_st_weak"))


def _accum_ok(d):
    cat = d["category"]
    if cat in ("rebound", "short_term"):
        return True
    a = d["sb"].get("accumulated_incl_today")
    if a is None:
        a = d["sb"].get("accumulated_pct")
    if a is None:
        a = d.get("accumulated_pct")
    return a is None or a >= 6.0


def _mark_old(d):
    return _sweet(d["percent"]) and not _overbought(d) and _accum_ok(d)


def _mark_new(d):
    if d["category"] == "short_term":
        return _weak(d) and not _overbought(d)
    return _sweet(d["percent"]) and not _overbought(d) and _accum_ok(d)


def _stats(recs, pred):
    sub = [d for d in recs if pred(d)]
    n = len(sub)
    nd = [d["next_day_pct"] for d in sub if d["next_day_pct"] is not None]
    hit = sum(1 for v in nd if v >= THRESHOLD)
    avg = sum(nd) / len(nd) if nd else 0.0
    return n, hit, hit / n * 100 if n else 0.0, avg


def main():
    conn = sqlite3.connect("scanner.db")
    recs = _load(conn)
    conn.close()
    print(f"去重样本: {len(recs)}")
    for name, fn in (("旧判定(甜蜜带+非超买)", _mark_old), ("新判定(short_term 弱转强分型)", _mark_new)):
        n, hit, rate, avg = _stats(recs, fn)
        print(f"{name:<30} n={n:>5} hit={hit:>4} hit率={rate:>5.1f}%  均次日={avg:>+6.2f}")
    # 新判定分拆：short_term 弱转强 vs 其余甜蜜带
    n, hit, rate, avg = _stats([d for d in recs if d["category"] == "short_term"], _mark_new)
    print(f"  其中 short_term(弱转强): n={n:>4} hit率={rate:>5.1f}%  均次日={avg:>+6.2f}")
    n, hit, rate, avg = _stats([d for d in recs if d["category"] != "short_term"], _mark_new)
    print(f"  其余类别(甜蜜带):       n={n:>4} hit率={rate:>5.1f}%  均次日={avg:>+6.2f}")


if __name__ == "__main__":
    main()
