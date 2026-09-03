"""新系统设计验证（A）：持有期扫描 —— 决定目标函数。

核心疑问：隔夜跳空 −0.52% 是系统性亏损源，那么「次日卖」这个周期
本身就是站在不利的一边。必须先看清楚：持有多久才不亏？

用 daily_kline 全历史（70 交易日，比 5 分钟数据的 31 天更宽），
买入 = T 日收盘，卖出 = T+N 日收盘，按天等权组合口径。

用法: python scripts/redesign_hold_period.py
"""

from __future__ import annotations

import sqlite3
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DB = ROOT / "scanner.db"

BAD_CAT = {"momentum", "pullback"}
GOOD_TREND = {"企稳回升", "主线回调", "回踩·到买点", "温和放量",
              "震荡整理", "低位企稳", "整理"}


def load():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    sql = """
        SELECT * FROM recommendations
        WHERE excluded = 0 AND next_day_pct IS NOT NULL
          AND symbol IS NOT NULL AND symbol != ''
        ORDER BY date, rowid
    """
    raw = conn.execute(sql).fetchall()
    latest: dict[tuple, sqlite3.Row] = {}
    for r in raw:
        latest[(r["date"], r["symbol"])] = r
    rows = [dict(r) for r in latest.values()]

    # 交易日历
    dates = [d for (d,) in conn.execute(
        "SELECT DISTINCT date FROM daily_kline ORDER BY date")]
    idx = {d: i for i, d in enumerate(dates)}

    # 日线收盘
    kline: dict[tuple, tuple] = {}
    for sym, d, o, h, lo, c in conn.execute(
            "SELECT symbol,date,open,high,low,close FROM daily_kline"):
        kline[(sym, d)] = (o, h, lo, c)
    conn.close()

    out = []
    for r in rows:
        i = idx.get(r["date"])
        if i is None:
            continue
        r["_i"] = i
        r["_dates"] = None
        out.append(r)
    return out, dates, idx, kline


def ret_over(kline, idx, dates, sym, i, n):
    """T 日收盘买 → T+n 日收盘卖 的收益（%）。"""
    d0 = dates[i]
    d1 = dates[i + n] if i + n < len(dates) else None
    a = kline.get((sym, d0))
    b = kline.get((sym, d1)) if d1 else None
    if not a or not b or not a[3] or not b[3]:
        return None
    return (b[3] / a[3] - 1) * 100


def portfolio(rows, valfn):
    byday = defaultdict(list)
    for r in rows:
        v = valfn(r)
        if v is not None:
            byday[r["date"]].append(v)
    if not byday:
        return None
    dailies = [statistics.fmean(v) for _, v in sorted(byday.items())]
    cum, peak, mdd = 1.0, 1.0, 0.0
    for d in dailies:
        cum *= (1 + d / 100)
        peak = max(peak, cum)
        mdd = max(mdd, (peak - cum) / peak)
    pos = [d for d in dailies if d > 0]
    return {
        "days": len(dailies),
        "daily": statistics.fmean(dailies),
        "win": 100 * len(pos) / len(dailies),
        "cum": (cum - 1) * 100,
        "mdd": mdd * 100,
        "avg_n": statistics.fmean([len(v) for v in byday.values()]),
        "best": max(dailies), "worst": min(dailies),
    }


def show(name, res, note=""):
    if not res:
        print(f"{name:<30s} 空集")
        return
    print(f"{name:<26s} {res['days']:>3d}天 日均{res['avg_n']:4.1f}只  "
          f"日收益{res['daily']:+6.3f}%  日胜率{res['win']:5.1f}%  "
          f"累计{res['cum']:+8.1f}%  回撤{res['mdd']:5.1f}%  {note}")


def main():
    rows, dates, idx, kline = load()
    print("=" * 112)
    print(f"持有期扫描（买入=T日收盘，按天等权组合）  样本 {len(rows)} 条  "
          f"{min(r['date'] for r in rows)} ~ {max(r['date'] for r in rows)}")
    print("=" * 112)

    print("\n【A1. 全池：持有 N 个交易日卖出】")
    for n in (1, 2, 3, 5, 8, 10):
        res = portfolio(rows, lambda r, n=n: ret_over(
            kline, idx, dates, r["symbol"], r["_i"], n))
        show(f"  T+{n} 收盘卖", res)

    print("\n【A2. 拆开看：T+1 到底亏在哪一段】（T日收盘 → T+1各时点）")
    # 用日线 open/high/low/close 拆三段
    def seg(r):
        a = kline.get((r["symbol"], dates[r["_i"]]))
        b = kline.get((r["symbol"], dates[r["_i"] + 1])) if r["_i"] + 1 < len(dates) else None
        if not a or not b:
            return None
        return ((b[0] / a[3] - 1) * 100,          # 隔夜跳空
                (b[1] / b[0] - 1) * 100,          # 日内最高相对开盘
                (b[2] / b[0] - 1) * 100,          # 日内最低相对开盘
                (b[3] / b[0] - 1) * 100)          # 开盘到收盘
    segs = [s for s in (seg(r) for r in rows) if s]
    print(f"  样本 {len(segs)}")
    print(f"  隔夜跳空(收盘→次日开盘)  日均 {statistics.fmean([s[0] for s in segs]):+.3f}%")
    print(f"  日内最高/开盘            日均 {statistics.fmean([s[1] for s in segs]):+.3f}%")
    print(f"  日内最低/开盘            日均 {statistics.fmean([s[2] for s in segs]):+.3f}%")
    print(f"  开盘→收盘                日均 {statistics.fmean([s[3] for s in segs]):+.3f}%")
    up = sum(1 for s in segs if s[1] >= 3.0)
    dn = sum(1 for s in segs if s[2] <= -3.0)
    print(f"  → 日内触及 +3% 的占比 {100*up/len(segs):.1f}%  触及 −3% 的占比 {100*dn/len(segs):.1f}%")

    print("\n【A3. R5 子集：持有 N 日】")
    r5 = [r for r in rows if r["trend"] in GOOD_TREND
          and r["category"] not in BAD_CAT and (r["percent"] or 0) < 10]
    print(f"  R5 样本 {len(r5)}（占全池 {100*len(r5)/len(rows):.1f}%）")
    for n in (1, 2, 3, 5, 8, 10):
        res = portfolio(r5, lambda r, n=n: ret_over(
            kline, idx, dates, r["symbol"], r["_i"], n))
        show(f"  R5 T+{n} 收盘卖", res)

    print("\n【A4. 关键：日收益是否随持有期线性放大？（判断是否真有 alpha）】")
    for label, sub in (("全池", rows), ("R5", r5)):
        line = []
        for n in (1, 2, 3, 5, 8, 10):
            res = portfolio(sub, lambda r, n=n: ret_over(
                kline, idx, dates, r["symbol"], r["_i"], n))
            if res:
                line.append((n, res["daily"], res["cum"], res["win"]))
        print(f"  {label}: " + "  ".join(
            f"T+{n}={d:+.2f}%(胜{w:.0f}%)" for n, d, c, w in line))


if __name__ == "__main__":
    main()
