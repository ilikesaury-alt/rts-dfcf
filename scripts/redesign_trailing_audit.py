"""新系统设计验证（C）：追踪止损的严格证伪。

B 组发现「追踪回撤 3%」把全池从 −0.23%/日 翻正到 +0.41%/日。太漂亮了，
必须排除三种假象：
  C1 参数过拟合 —— 只有 3% 好，还是 2~8% 都好？
  C2 前视偏差 —— peak 用 bar 的 high 更新、同 bar close 成交，是否偷看了未来？
                  保守口径：peak 只用已完结 bar 的 high；触发后用**下一根 bar 的开盘**成交。
  C3 时间过拟合 —— 前后半段 / 逐周 是否同号？

用法: python scripts/redesign_trailing_audit.py
"""

from __future__ import annotations

import sqlite3
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from intraday_exit_test import BAD_CAT, GOOD_TREND, build  # noqa: E402

CACHE = ROOT / "scripts" / ".cache_m5.sqlite3"


def load_next_bars(pairs):
    conn = sqlite3.connect(CACHE)
    out = defaultdict(dict)
    for sym, d in pairs:
        for t, o, h, lo, c in conn.execute(
                "SELECT time,open,high,low,close FROM m5 WHERE symbol=? AND date=?",
                (sym, d)):
            out[(sym, d)][t] = (o, h, lo, c)
    conn.close()
    return out


def sim_trail(bars, base, trail, strict=True):
    """追踪止损。strict=True → 无前视口径：
       peak 只由**已完结**的 bar 更新；触发信号在下一根 bar 的开盘价成交。
       未触发 → 收盘卖。
    """
    ts = sorted(bars)
    if not ts:
        return None
    peak = base
    pending = False
    for i, t in enumerate(ts):
        o, h, lo, c = bars[t]
        if pending:
            return (o / base - 1) * 100
        if strict:
            # 用上一根(含之前)的 high 形成的 peak，判断本 bar close 是否跌破
            if c <= peak * (1 - trail / 100):
                pending = True
                continue
            peak = max(peak, h)
        else:
            peak = max(peak, h)
            if c <= peak * (1 - trail / 100):
                return (peak * (1 - trail / 100) / base - 1) * 100
    last = bars[ts[-1]][3]
    return (last / base - 1) * 100


def sim_fixed(bars, base, when=None):
    ts = sorted(bars)
    if not ts:
        return None
    if when and when in bars:
        return (bars[when][3] / base - 1) * 100
    return (bars[ts[-1]][3] / base - 1) * 100


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
    return {"days": len(dailies), "daily": statistics.fmean(dailies),
            "win": 100 * sum(1 for d in dailies if d > 0) / len(dailies),
            "cum": (cum - 1) * 100, "mdd": mdd * 100}


def show(name, res):
    if not res:
        print(f"  {name:<34s} 空集")
        return
    print(f"  {name:<34s} {res['days']:>3d}天 日收益{res['daily']:+7.3f}%  "
          f"日胜率{res['win']:5.1f}%  累计{res['cum']:+8.1f}%  回撤{res['mdd']:5.1f}%")


def main():
    rows, notes = build("2026-07-22")
    nb = load_next_bars([(r["symbol"], r["_nxt"]) for r in rows])
    for r in rows:
        r["_nb"] = nb.get((r["symbol"], r["_nxt"])) or {}
    rows = [r for r in rows if r["_nb"]]
    r5 = [r for r in rows if r["trend"] in GOOD_TREND
          and r["category"] not in BAD_CAT and (r["percent"] or 0) < 10]
    dates = sorted({r["date"] for r in rows})
    print("=" * 104)
    print(f"追踪止损严格证伪  样本 {len(rows)} 条 / {len(dates)} 天  "
          f"{dates[0]} ~ {dates[-1]}")
    print("=" * 104)

    print("\n【C1. 参数敏感性：追踪回撤 X%】（无前视口径）")
    for label, sub in (("全池", rows), ("R5", r5)):
        print(f"  --- {label}（{len(sub)} 条）---")
        for tr in (1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0):
            show(f"    追踪 {tr:g}%",
                 portfolio(sub, lambda r, tr=tr: sim_trail(r["_nb"], r["_p_first"], tr)))
        show("    固定 15:00 卖", portfolio(sub, lambda r: sim_fixed(r["_nb"], r["_p_first"])))

    print("\n【C2. 前视偏差：宽松口径 vs 无前视口径（下一根 bar 开盘成交）】")
    for label, sub in (("全池", rows), ("R5", r5)):
        print(f"  --- {label} ---")
        for tr in (2.0, 3.0, 5.0):
            loose = portfolio(sub, lambda r, tr=tr: sim_trail(
                r["_nb"], r["_p_first"], tr, strict=False))
            strict = portfolio(sub, lambda r, tr=tr: sim_trail(
                r["_nb"], r["_p_first"], tr, strict=True))
            show(f"    追踪{tr:g}% 宽松(有前视)", loose)
            show(f"    追踪{tr:g}% 保守(无前视)", strict)

    print("\n【C3. 时间稳定性：前半段 vs 后半段】")
    mid = len(dates) // 2
    d1, d2 = set(dates[:mid]), set(dates[mid:])
    for label, sub in (("全池", rows), ("R5", r5)):
        print(f"  --- {label} ---")
        for tr in (2.0, 3.0, 5.0):
            a = portfolio([r for r in sub if r["date"] in d1],
                          lambda r, tr=tr: sim_trail(r["_nb"], r["_p_first"], tr))
            b = portfolio([r for r in sub if r["date"] in d2],
                          lambda r, tr=tr: sim_trail(r["_nb"], r["_p_first"], tr))
            print(f"    追踪{tr:g}%:  前段 日均{a['daily']:+.3f}% 胜{a['win']:.0f}% | "
                  f"后段 日均{b['daily']:+.3f}% 胜{b['win']:.0f}%  "
                  f"{'✓同号' if a['daily'] * b['daily'] > 0 else '✗异号'}")

    print("\n【C4. 逐周稳定性：R5 + 追踪3%（无前视）】")
    wk = defaultdict(list)
    for i, d in enumerate(dates):
        wk[i // 5].append(d)
    for k in sorted(wk):
        ds = set(wk[k])
        sub = [r for r in r5 if r["date"] in ds]
        if not sub:
            continue
        res = portfolio(sub, lambda r: sim_trail(r["_nb"], r["_p_first"], 3.0))
        fx = portfolio(sub, lambda r: sim_fixed(r["_nb"], r["_p_first"]))
        print(f"    第{k+1}周 {min(ds)}~{max(ds)}  "
              f"追踪3% 日均{res['daily']:+.3f}% | 收盘卖 日均{fx['daily']:+.3f}%  "
              f"差{res['daily']-fx['daily']:+.3f}%")


if __name__ == "__main__":
    main()
