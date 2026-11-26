"""persist × pct_rank 深度交叉检验（只读、离线；配套前两个脚本）。

要点：
- persist 在盘中单调递增（当日累计 p95+ 轮次），末轮终值 ≠ 盘中可见值。
- pct_rank 是当轮全市场复合分位，落库样本全在 [0.95,1.0]，区分度要靠窄带内细分。
- ①3x3 交叉表看两者的边际/交互；②首现时刻分层；③盘中时点模拟（截至 10:30/11:30/14:00 的 persist）。
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict


def load_all():
    conn = sqlite3.connect("scanner.db", timeout=15)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT date,round_seq,symbol,pct_rank,percent,next_day_pct FROM crosssection_signal "
        "WHERE next_day_pct IS NOT NULL ORDER BY date,round_seq"
    )]
    conn.close()
    return rows


def hit(grp):
    return (sum(r["next_day_pct"] >= 7 for r in grp) / len(grp) * 100,
            sum(r["next_day_pct"] for r in grp) / len(grp), len(grp))


def main():
    rows = load_all()
    by_ds = defaultdict(list)
    for r in rows:
        by_ds[(r["date"], r["symbol"])].append(r)

    recs = []
    for (d, sym), rs in by_ds.items():
        rs.sort(key=lambda r: r["round_seq"])
        last = rs[-1]
        pval = [r["pct_rank"] for r in rs if r["pct_rank"] is not None]
        if not pval:
            continue
        recs.append({
            "date": d, "symbol": sym,
            "persist_full": len(rs),
            "pct_last": last["pct_rank"],
            "pct_mean": sum(pval) / len(pval),
            "pct_max": max(pval),
            "first_seq": rs[0]["round_seq"],
            "last_seq": last["round_seq"],
            "nd": last["next_day_pct"],
            "next_day_pct": last["next_day_pct"],
            "rs": rs,
        })
    base = hit(recs)
    print(f"样本 {len(recs)} | 基准 hit={base[0]:.2f}% avg={base[1]:+.2f}%")

    # ① 3x3 交叉表（末轮 pct_rank 在 [0.95,1] 内按分位切；persist 按 5/13 切）
    print("\n=== ① persist × 末轮 pct_rank 交叉表（行=persist, 列=pct_rank 窄带内分位）===")
    def pq(r):
        v = r["pct_last"]
        return "高(≥0.99)" if v >= 0.99 else ("中(0.97~0.99)" if v >= 0.97 else "低(0.95~0.97)")
    def bq(r):
        p = r["persist_full"]
        return "多(≥13)" if p >= 13 else ("中(5~12)" if p >= 5 else "少(<5)")
    for pb in ["多(≥13)", "中(5~12)", "少(<5)"]:
        for qb in ["高(≥0.99)", "中(0.97~0.99)", "低(0.95~0.97)"]:
            g = [r for r in recs if bq(r) == pb and pq(r) == qb]
            if len(g) < 30:
                continue
            h, a, n = hit(g)
            print(f"  persist {pb:9s} × pct {qb:14s}: n={n:5d} hit={h:5.2f}% avg={a:+.2f}%")

    # ② 首现时刻分层（首轮 round_seq，HHMMSS）
    print("\n=== ② 首现时刻 × persist 交叉 ===")
    def tq(r):
        s = r["first_seq"]
        return "早盘(<10:30)" if s < 103000 else ("中盘(10:30~13:30)" if s < 133000 else "尾盘(≥13:30)")
    for t in ["早盘(<10:30)", "中盘(10:30~13:30)", "尾盘(≥13:30)"]:
        g = [r for r in recs if tq(r) == t]
        if not g:
            continue
        h, a, n = hit(g)
        hg = [r for r in g if r["persist_full"] >= 13]
        hh = hit(hg) if len(hg) >= 20 else None
        print(f"  {t:18s}: n={n:5d} hit={h:5.2f}% avg={a:+.2f}% | 其中persist≥13: n={len(hg):4d} "
              + (f"hit={hh[0]:.2f}%" if hh else "—"))

    # ③ 盘中时点模拟：截至某时刻的 persist（该时刻前已进 p95 的轮数）
    print("\n=== ③ 盘中时点模拟：截至 T 的 persist（真实盘中可见值）===")
    for cutoff in [100000, 103000, 113000, 130000, 140000, 143000]:
        sub = []
        for r in recs:
            cnt = sum(1 for x in r["rs"] if x["round_seq"] <= cutoff)
            if cnt >= 3:  # 至少攒够 3 轮才有意义
                sub.append((cnt, r))
        if not sub:
            continue
        h_all, a_all, n_all = hit([r for _, r in sub])
        # 按截至时刻的 persist 分层
        hi = [r for c, r in sub if c >= 10]
        lo = [r for c, r in sub if c < 5]
        h_hi = hit(hi) if len(hi) >= 20 else None
        h_lo = hit(lo) if len(lo) >= 20 else None
        print(f"  T={cutoff//10000:02d}:{cutoff//100%100:02d} 可见样本={n_all:5d} hit={h_all:5.2f}% | "
              f"persist≥10: n={len(hi):4d} " + (f"hit={h_hi[0]:5.2f}%" if h_hi else "—")
              + f" | persist<5: n={len(lo):4d} " + (f"hit={h_lo[0]:5.2f}%" if h_lo else "—"))

    # ④ 控制 persist 后 pct_rank 的边际（在 persist>=13 组内再切 pct）
    print("\n=== ④ persist>=13 组内，末轮 pct_rank 是否仍有边际区分 ===")
    hi_p = [r for r in recs if r["persist_full"] >= 13]
    for qb in ["高(≥0.99)", "中(0.97~0.99)", "低(0.95~0.97)"]:
        g = [r for r in hi_p if pq(r) == qb]
        if len(g) < 20:
            continue
        h, a, n = hit(g)
        print(f"  {qb:14s}: n={n:5d} hit={h:5.2f}% avg={a:+.2f}%")


if __name__ == "__main__":
    main()
