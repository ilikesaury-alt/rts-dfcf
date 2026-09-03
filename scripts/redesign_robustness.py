"""新系统设计验证（D）：R5 到底稳不稳 + 择时空仓能不能救。

暴露的矛盾：
  A 组（64 天，T日收盘买→T+1收盘卖）R5 = −0.114%/日
  C 组（31 天，推荐时刻买→T+1收盘卖）R5 = +0.471%/日
同一规则两个窗口差 0.59%/日。必须先证伪 R5，否则整个方案建在沙子上。

D1 R5 在 64 天窗口的逐月稳定性
D2 超额（R5 − 全池当月）是否稳定为正 —— 剔除市场 beta
D3 择时空仓：能否识别坏日子
D4 仓位曲线：每天买几只最优

用法: python scripts/redesign_robustness.py
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
    raw = conn.execute("""
        SELECT * FROM recommendations
        WHERE excluded = 0 AND next_day_pct IS NOT NULL
          AND symbol IS NOT NULL AND symbol != ''
        ORDER BY date, rowid
    """).fetchall()
    latest = {}
    for r in raw:
        latest[(r["date"], r["symbol"])] = r
    rows = [dict(r) for r in latest.values()]
    # 创业板指（399006）作为市场基准
    bench = {}
    for d, c in conn.execute(
            "SELECT date,close FROM daily_kline WHERE symbol IN ('399006','SZ399006')"):
        bench[d] = c
    conn.close()
    return rows, bench


def portfolio(sub, valfn=lambda r: r["next_day_pct"], topk=None, sort_key=None):
    byday = defaultdict(list)
    for r in sub:
        v = valfn(r)
        if v is not None:
            byday[r["date"]].append(r)
    dailies = []
    for d, rs in sorted(byday.items()):
        if topk and sort_key:
            rs = sorted(rs, key=sort_key)[:topk]
        dailies.append(statistics.fmean([valfn(r) for r in rs]))
    if not dailies:
        return None
    cum, peak, mdd = 1.0, 1.0, 0.0
    for d in dailies:
        cum *= (1 + d / 100)
        peak = max(peak, cum)
        mdd = max(mdd, (peak - cum) / peak)
    return {"days": len(dailies), "daily": statistics.fmean(dailies),
            "win": 100 * sum(1 for d in dailies if d > 0) / len(dailies),
            "cum": (cum - 1) * 100, "mdd": mdd * 100,
            "avg_n": statistics.fmean([len(v) for v in byday.values()])}


def main():
    rows, bench = load()
    r5 = [r for r in rows if r["trend"] in GOOD_TREND
          and r["category"] not in BAD_CAT and (r["percent"] or 0) < 10]
    dates = sorted({r["date"] for r in rows})
    print("=" * 106)
    print(f"稳健性验证  样本 {len(rows)} 条 / {len(dates)} 天  {dates[0]} ~ {dates[-1]}")
    print(f"R5 子集 {len(r5)} 条（{100*len(r5)/len(rows):.1f}%）")
    print("=" * 106)

    print("\n【D1. 逐月稳定性：全池 vs R5 vs 超额】")
    bym = defaultdict(list)
    for d in dates:
        bym[d[:7]].append(d)
    print(f"  {'月份':<10s} {'天数':>4s} {'全池日均':>9s} {'R5日均':>9s} "
          f"{'超额':>8s} {'R5胜率':>7s} {'全池胜率':>8s}")
    rows_out = []
    for m in sorted(bym):
        ds = set(bym[m])
        a = portfolio([r for r in rows if r["date"] in ds])
        b = portfolio([r for r in r5 if r["date"] in ds])
        if not a or not b:
            continue
        ex = b["daily"] - a["daily"]
        rows_out.append(ex)
        print(f"  {m:<10s} {b['days']:>4d} {a['daily']:+9.3f}% {b['daily']:+9.3f}% "
              f"{ex:+8.3f}% {b['win']:6.1f}% {a['win']:7.1f}%")
    if rows_out:
        pos = sum(1 for x in rows_out if x > 0)
        print(f"  → 超额为正的月份：{pos}/{len(rows_out)}   "
              f"均值 {statistics.fmean(rows_out):+.3f}%  "
              f"中位数 {statistics.median(rows_out):+.3f}%")

    print("\n【D2. 剔除市场 beta：R5 相对创业板指的超额】")
    # 用全池当日均值作为市场代理（更贴近：这批票就是当天最热的票）
    excess = []
    byd_all = defaultdict(list)
    byd_r5 = defaultdict(list)
    for r in rows:
        byd_all[r["date"]].append(r["next_day_pct"])
    for r in r5:
        byd_r5[r["date"]].append(r["next_day_pct"])
    for d in sorted(byd_r5):
        if d in byd_all:
            excess.append(statistics.fmean(byd_r5[d]) - statistics.fmean(byd_all[d]))
    cum = 1.0
    for x in excess:
        cum *= (1 + x / 100)
    print(f"  R5 − 全池同日均值：日均 {statistics.fmean(excess):+.3f}%  "
          f"胜率 {100*sum(1 for x in excess if x > 0)/len(excess):.1f}%  "
          f"累计 {(cum-1)*100:+.1f}%  （{len(excess)} 天）")
    t = statistics.fmean(excess) / (statistics.stdev(excess) / len(excess) ** 0.5)
    print(f"  t 值 = {t:.2f}  {'✓ 显著(>2)' if abs(t) > 2 else '✗ 不显著'}")

    print("\n【D3. 择时空仓：池子特征能否预测次日好坏】")
    feats = []
    byd = defaultdict(list)
    for r in rows:
        byd[r["date"]].append(r)
    for d, rs in sorted(byd.items()):
        feats.append({
            "date": d, "n": len(rs),
            "avg_pct": statistics.fmean([r["percent"] or 0 for r in rs]),
            "nxt": statistics.fmean([r["next_day_pct"] for r in rs]),
            "r5n": len([r for r in rs if r["trend"] in GOOD_TREND
                        and r["category"] not in BAD_CAT and (r["percent"] or 0) < 10]),
        })
    for key, label in (("n", "当日推荐只数"), ("avg_pct", "池子平均当日涨幅"),
                       ("r5n", "R5命中只数")):
        vals = [f[key] for f in feats]
        edges = statistics.quantiles(vals, n=4)
        print(f"  --- 按{label}分四档 ---")
        for i, (lo, hi) in enumerate(zip([-1e9] + edges, edges + [1e9])):
            sub = [f for f in feats if lo <= f[key] < hi]
            if not sub:
                continue
            d = [f["nxt"] for f in sub]
            print(f"    档{i+1} [{lo:7.1f},{hi:7.1f})  {len(sub):>3d}天  "
                  f"次日日均{statistics.fmean(d):+.3f}%  "
                  f"胜率{100*sum(1 for x in d if x>0)/len(d):5.1f}%")

    print("\n【D4. 仓位曲线：R5 每天买几只最优（按分数降序取 Top-K）】")
    sk = lambda r: -(r["score"] or 0)  # noqa: E731
    for k in (1, 2, 3, 5, 8, 12, 999):
        res = portfolio(r5, topk=k, sort_key=sk)
        lbl = "全买" if k == 999 else f"Top{k}"
        if res:
            print(f"  {lbl:<6s} 日均{res['avg_n']:4.1f}只  日收益{res['daily']:+7.3f}%  "
                  f"日胜率{res['win']:5.1f}%  累计{res['cum']:+8.1f}%  回撤{res['mdd']:5.1f}%")


if __name__ == "__main__":
    main()
