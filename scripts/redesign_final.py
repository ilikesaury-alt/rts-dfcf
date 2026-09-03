"""新系统设计验证（E）：最终门控组合 + 样本外定生死。

前四组结论：
  - 无绝对 alpha，有相对 alpha（R5 超额 +0.535%/日，t=2.75）
  - 持有期单调衰减 → 隔夜是敌人
  - 止盈有害、追踪止损是前视假象、分数无区分度
  - 池子宽度是强择时信号（但内生，需交叉验证）

E1 市场状态：池子宽度 vs 指数趋势（客观性交叉验证）
E2 完整门控 L0(市场) × L1(个股)，IS/OOS 定生死
E3 参数冻结：给出可直接落地的最终配置

用法: python scripts/redesign_final.py
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
    for r in rows:
        try:
            import json
            r["_bd"] = json.loads(r["score_breakdown"] or "{}") or {}
        except ValueError:
            r["_bd"] = {}

    # 找指数
    idxs = [r[0] for r in conn.execute(
        "SELECT DISTINCT symbol FROM daily_kline WHERE length(symbol)<=6 "
        "AND (symbol LIKE '399%' OR symbol LIKE '000%' OR symbol LIKE 'SH000%') "
        "LIMIT 40")]
    bench = {}
    for sym in idxs:
        series = conn.execute(
            "SELECT date,close FROM daily_kline WHERE symbol=? ORDER BY date", (sym,)).fetchall()
        if len(series) > 50:
            bench[sym] = dict(series)
    conn.close()
    return rows, bench


def accum5(r):
    try:
        return float(r["_bd"].get("accumulated_incl_today"))
    except (TypeError, ValueError):
        return None


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
            "cum": (cum - 1) * 100, "mdd": mdd * 100, "n": len(sub)}


def show(name, res, note=""):
    if not res:
        print(f"  {name:<36s} 空集")
        return
    print(f"  {name:<36s} {res['days']:>3d}天 日收益{res['daily']:+7.3f}%  "
          f"日胜率{res['win']:5.1f}%  累计{res['cum']:+8.1f}%  回撤{res['mdd']:5.1f}%  {note}")


def main():
    rows, bench = load()
    dates = sorted({r["date"] for r in rows})
    print("=" * 108)
    print(f"最终门控验证  样本 {len(rows)} 条 / {len(dates)} 天  {dates[0]} ~ {dates[-1]}")
    print(f"可用指数: {list(bench)[:8]}")
    print("=" * 108)

    # ---------- E1 市场状态 ----------
    print("\n【E1. 市场状态：池子宽度 vs 指数趋势】")
    byd = defaultdict(list)
    for r in rows:
        byd[r["date"]].append(r)
    feats = {}
    for d, rs in byd.items():
        r5n = len([r for r in rs if r["trend"] in GOOD_TREND
                   and r["category"] not in BAD_CAT and (r["percent"] or 0) < 10])
        feats[d] = {"n": len(rs), "r5n": r5n,
                    "nxt": statistics.fmean([r["next_day_pct"] for r in rs])}
    # 指数：找样本最多的那个
    if bench:
        main_idx = max(bench, key=lambda k: len(bench[k]))
        series = bench[main_idx]
        sd = sorted(series)
        print(f"  使用指数 {main_idx}（{len(series)} 天）")
        ok = 0
        for d in dates:
            if d not in sd:
                continue
            i = sd.index(d)
            if i >= 5:
                feats[d]["mkt5"] = (series[d] / series[sd[i - 5]] - 1) * 100
                ok += 1
        print(f"  可计算 5 日指数动量的天数: {ok}")
        if ok:
            for label, key in (("指数5日动量", "mkt5"), ("池子宽度", "n"), ("R5命中数", "r5n")):
                sub = [f for f in feats.values() if key in f]
                vals = [f[key] for f in sub]
                q = statistics.quantiles(vals, n=3)
                print(f"  --- {label} 分三档 ---")
                for i, (lo, hi) in enumerate(zip([-1e9] + q, q + [1e9])):
                    g = [f for f in sub if lo <= f[key] < hi]
                    if not g:
                        continue
                    v = [f["nxt"] for f in g]
                    print(f"    档{i+1} [{lo:6.2f},{hi:6.2f})  {len(g):>3d}天  "
                          f"次日日均{statistics.fmean(v):+.3f}%  胜率{100*sum(1 for x in v if x>0)/len(v):5.1f}%")
    else:
        print("  无指数数据")

    # ---------- E2 完整门控 ----------
    print("\n【E2. 完整门控：L0 市场闸门 × L1 个股 gate】")
    cut = int(len(dates) * 0.6)
    d_is, d_oo = set(dates[:cut]), set(dates[cut:])
    print(f"  IS {len(d_is)} 天（{min(d_is)}~{max(d_is)}） / "
          f"OOS {len(d_oo)} 天（{min(d_oo)}~{max(d_oo)}）")

    def l1(r):
        """个股硬 gate。"""
        if r["trend"] not in GOOD_TREND:
            return False
        if r["category"] in BAD_CAT:
            return False
        if (r["percent"] or 0) >= 10:
            return False
        a = accum5(r)
        if a is not None and a >= 10:
            return False
        hh = (r["time"] or "")[:2]
        if hh >= "14":
            return False
        return True

    r5 = [r for r in rows if l1(r)]
    print(f"  L1 通过 {len(r5)} 条（{100*len(r5)/len(rows):.1f}%）")

    # L0：用 IS 期确定阈值（只用 IS 数据，避免偷看）
    is_feats = {d: f for d, f in feats.items() if d in d_is}
    for key in ("n", "r5n"):
        vals = sorted(f[key] for f in is_feats.values())
        thr = vals[int(len(vals) * 0.5)]
        print(f"  L0 阈值（IS 中位数）{key} >= {thr:g}")

    def l0_on(d, key="n", thr=0):
        return feats.get(d, {}).get(key, 0) >= thr

    n_vals = sorted(f["n"] for f in is_feats.values())
    n_thr = n_vals[int(len(n_vals) * 0.5)]
    r5n_vals = sorted(f["r5n"] for f in is_feats.values())
    r5n_thr = r5n_vals[int(len(r5n_vals) * 0.5)]

    print("\n  --- 各配置 IS / OOS 对比 ---")
    configs = [
        ("全池（基线）", rows, None),
        ("L1 个股 gate", r5, None),
        ("L0 池子宽度 + 全池", rows, ("n", n_thr)),
        ("L0 池子宽度 + L1", r5, ("n", n_thr)),
        ("L0 R5命中数 + L1", r5, ("r5n", r5n_thr)),
    ]
    for name, sub, l0 in configs:
        if l0:
            key, thr = l0
            s = [r for r in sub if l0_on(r["date"], key, thr)]
        else:
            s = sub
        a = portfolio([r for r in s if r["date"] in d_is])
        b = portfolio([r for r in s if r["date"] in d_oo])
        note = ""
        if a and b:
            note = "✓同号" if a["daily"] * b["daily"] > 0 else "✗异号"
        print(f"  {name}")
        show("    IS ", a)
        show("    OOS", b, note)

    print("\n【E3. 参数冻结：L0+L1 在不同 L0 阈值下的 OOS 表现】")
    for pct in (0.0, 0.25, 0.4, 0.5, 0.6, 0.75):
        thr = n_vals[int(len(n_vals) * pct)]
        s = [r for r in r5 if l0_on(r["date"], "n", thr)]
        a = portfolio([r for r in s if r["date"] in d_is])
        b = portfolio([r for r in s if r["date"] in d_oo])
        if a and b:
            print(f"  池子宽度 ≥ {thr:5.0f} 只（IS 分位{pct:.0%}）: "
                  f"IS {a['daily']:+.3f}%({a['days']}天) | "
                  f"OOS {b['daily']:+.3f}%({b['days']}天)  "
                  f"{'✓' if a['daily']*b['daily']>0 else '✗'}")


if __name__ == "__main__":
    main()
