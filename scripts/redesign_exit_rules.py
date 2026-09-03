"""新系统设计验证（B）：出场纪律 —— 止盈/止损/追踪，是否比固定时点更优。

A 组已证明：持有期单调衰减，没有 alpha。但 A2 显示日内振幅极大
（日内最高/开盘 +4.07%、最低/开盘 −3.14%，触及±3% 的占比 46.7%/41.3%）。
这意味着**怎么卖**可能比**买什么**更值钱。

用 5 分钟数据判断触发顺序（先摸止盈还是先摸止损），31 个交易日。

用法: python scripts/redesign_exit_rules.py
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
CACHE = ROOT / "scripts" / ".cache_m5.sqlite3"

from intraday_exit_test import BAD_CAT, GOOD_TREND, build  # noqa: E402


def portfolio(rows, valfn):
    """按天等权组合。valfn(r) -> 收益% 或 None。"""
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
    return {
        "days": len(dailies),
        "daily": statistics.fmean(dailies),
        "win": 100 * sum(1 for d in dailies if d > 0) / len(dailies),
        "cum": (cum - 1) * 100,
        "mdd": mdd * 100,
        "avg_n": statistics.fmean([len(v) for v in byday.values()]),
    }

TIMES = ["09:35", "10:00", "10:30", "11:00", "11:30", "13:30",
         "14:00", "14:30", "15:00"]


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


def sim_exit(bars, base, tp, sl, trail=None):
    """模拟出场：base=入场价，tp=止盈%(正), sl=止损%(负), trail=追踪回撤%。
    按 bar 顺序推进，用 bar 的 high/low 判断触发；同 bar 内先判止损(保守)。
    返回收益%。
    """
    if not bars:
        return None
    peak = base
    for t in sorted(bars):
        o, h, lo, c = bars[t]
        # 保守：同一根 bar 内若同时触及，按止损算
        if sl is not None and lo <= base * (1 + sl / 100):
            return sl
        if tp is not None and h >= base * (1 + tp / 100):
            return tp
        if trail is not None:
            peak = max(peak, h)
            if c <= peak * (1 - trail / 100):
                return (peak * (1 - trail / 100) / base - 1) * 100
    # 未触发：收盘卖
    last = bars[sorted(bars)[-1]][3]
    return (last / base - 1) * 100


def main():
    rows, notes = build("2026-07-22")
    nb = load_next_bars([(r["symbol"], r["_nxt"]) for r in rows])
    for r in rows:
        r["_nb"] = nb.get((r["symbol"], r["_nxt"])) or {}
    rows = [r for r in rows if r["_nb"]]
    print("=" * 108)
    print(f"出场纪律验证  样本 {len(rows)} 条 / {len({r['date'] for r in rows})} 个交易日")
    print(f"入场 = 推荐时刻买入（{notes}）")
    print("=" * 108)

    r5 = [r for r in rows if r["trend"] in GOOD_TREND
          and r["category"] not in BAD_CAT and (r["percent"] or 0) < 10]

    for label, sub in (("全池", rows), ("R5", r5)):
        print(f"\n【{label}】样本 {len(sub)}")
        print(f"  {'策略':<28s} {'日均':>7s} {'日胜率':>7s} {'累计':>9s} {'回撤':>7s}")
        cases = [
            ("固定 10:00 卖", None),
            ("固定 15:00 卖", None),
            ("止盈+3 / 止损无", (3.0, None, None)),
            ("止盈+5 / 止损无", (5.0, None, None)),
            ("止盈无 / 止损-3", (None, -3.0, None)),
            ("止盈+3 / 止损-3", (3.0, -3.0, None)),
            ("止盈+5 / 止损-3", (5.0, -3.0, None)),
            ("止盈+5 / 止损-5", (5.0, -5.0, None)),
            ("止盈+3 / 止损-2", (3.0, -2.0, None)),
            ("止盈+2 / 止损-5", (2.0, -5.0, None)),
            ("追踪回撤 3%（收盘卖兜底）", (None, None, 3.0)),
            ("追踪回撤 5%（收盘卖兜底）", (None, None, 5.0)),
        ]
        for name, cfg in cases:
            if cfg is None:
                if name.endswith("10:00 卖"):
                    def f(r):
                        b = r["_nb"].get("10:00")
                        return (b[3] / r["_p_first"] - 1) * 100 if b else None
                else:
                    def f(r):
                        return (r["_nb"][sorted(r["_nb"])[-1]][3] / r["_p_first"] - 1) * 100
            else:
                tp, sl, tr = cfg

                def f(r, tp=tp, sl=sl, tr=tr):
                    return sim_exit(r["_nb"], r["_p_first"], tp, sl, tr)
            res = portfolio(sub, f)
            if res:
                print(f"  {name:<28s} {res['daily']:+7.3f}% {res['win']:6.1f}% "
                      f"{res['cum']:+8.1f}% {res['mdd']:6.1f}%")

    print("\n【触发率诊断：R5 子集，先看谁先被摸到】")
    stat = defaultdict(int)
    n = 0
    for r in r5:
        bars = r["_nb"]
        base = r["_p_first"]
        hit = None
        for t in sorted(bars):
            o, h, lo, c = bars[t]
            if lo <= base * 0.97:
                hit = "先止损-3"
                break
            if h >= base * 1.03:
                hit = "先止盈+3"
                break
        stat[hit or "都没触及"] += 1
        n += 1
    print(f"  样本 {n}")
    for k in ("先止盈+3", "先止损-3", "都没触及"):
        print(f"    {k:<12s} {stat[k]:>5d}  {100*stat[k]/n:5.1f}%")


if __name__ == "__main__":
    main()
