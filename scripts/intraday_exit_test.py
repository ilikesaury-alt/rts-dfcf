"""盘中口径回测：当日推荐时刻买入 → 次日 10:00 卖出。

与既有 next_day_pct（收盘→收盘）口径的区别：
  - 买入价 = 推荐时刻附近的真实成交价（5 分钟 bar 收盘，非当日收盘价）
  - 卖出价 = 次一交易日 10:00 的 5 分钟 bar 收盘价

数据源：scripts/.cache_m5.sqlite3（由 scripts/fetch_m5.py 从东财 klt=5 抓取）。
东财该接口最多回溯约 31 个交易日，样本窗口受此限制（约 2026-07-22 起）。

用法: python scripts/intraday_exit_test.py [--since 2026-07-22]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "scanner.db"
CACHE = ROOT / "scripts" / ".cache_m5.sqlite3"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SELL_TIME = "10:00"
BUY_LIMIT_PCT = 19.0  # 创业板 ±20%，扫描时已贴近涨停视为不可成交

GOOD_TREND = {"企稳回升", "主线回调", "回踩·到买点", "温和放量", "震荡整理", "低位企稳", "整理"}
BAD_CAT = {"momentum", "pullback"}


def load_bars(symbols: list[str]) -> dict[str, dict[str, dict[str, tuple]]]:
    """bars[symbol][date][time] = (close, open, high, low)。"""
    conn = sqlite3.connect(CACHE)
    q = "SELECT symbol, date, time, close, open, high, low FROM m5 WHERE symbol=?"
    out: dict[str, dict[str, dict[str, tuple]]] = {}
    for s in symbols:
        byday: dict[str, dict[str, tuple]] = defaultdict(dict)
        for _, d, t, c, o, h, l in conn.execute(q, (s,)):
            byday[d][t] = (c, o, h, l)
        out[s] = dict(byday)
    conn.close()
    return out


def trading_dates(bars) -> list[str]:
    ds = set()
    for m in bars.values():
        ds.update(m.keys())
    return sorted(ds)


def price_at(barday: dict[str, tuple], when: str):
    """取 <= when 的最后一根 bar 的收盘价。

    早于首根 bar（如 09:31 扫描、首根 bar 为 09:35）时，退回首根 bar 的**开盘价**
    （即 09:30 的价格）——这批是开盘即推荐的样本，直接丢弃会让样本偏向午后。
    """
    if not barday:
        return None
    ts = sorted(barday)
    if when >= "15:00":
        return barday[ts[-1]][0]
    pick = None
    for t in ts:
        if t <= when:
            pick = t
        else:
            break
    if pick is None:
        return barday[ts[0]][1] or barday[ts[0]][0]  # 首根 bar 开盘价
    return barday[pick][0]


def last_close(barday: dict[str, tuple]):
    if not barday:
        return None
    return barday[sorted(barday)[-1]][0]


def build(since: str) -> tuple[list[dict], list[str]]:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT * FROM recommendations
           WHERE date >= ? AND (excluded IS NULL OR excluded = 0)
             AND symbol IS NOT NULL AND symbol != ''
           ORDER BY date, time, id""", (since,)).fetchall()
    conn.close()

    first: dict[tuple, sqlite3.Row] = {}
    last: dict[tuple, sqlite3.Row] = {}
    for r in rows:
        k = (r["date"], r["symbol"])
        first.setdefault(k, r)
        last[k] = r

    bars = load_bars(sorted({r["symbol"] for r in rows}))
    dates = trading_dates(bars)
    dset = set(dates)

    def nxt(d: str):
        for x in dates:
            if x > d:
                return x
        return None

    nxt_map = {d: nxt(d) for d in dates}

    out: list[dict] = []
    miss = defaultdict(int)
    for k, r in sorted(first.items()):
        d, sym = k
        bd = bars.get(sym) or {}
        d0 = bd.get(d)
        d1 = bd.get(nxt_map.get(d) or "")
        if not d0:
            miss["T日无分钟数据"] += 1
            continue
        if not d1:
            miss["T+1无分钟数据(停牌/退市)"] += 1
            continue
        s10 = d1.get(SELL_TIME)
        if s10 is None:
            miss["T+1无10:00 bar"] += 1
            continue

        t_first = (r["time"] or "")[:8]
        r_last = last[k]
        t_last = (r_last["time"] or "")[:8]

        p_first = price_at(d0, t_first)
        p_last = price_at(d0, t_last)
        p_close = last_close(d0)
        s_close = last_close(d1)
        if not (p_first and p_close and s_close):
            miss["价格缺失"] += 1
            continue

        rec = dict(r)
        try:
            b = json.loads(r["score_breakdown"] or "{}")
        except (ValueError, TypeError):
            b = {}
        rec["_bd"] = b if isinstance(b, dict) else {}
        rec["_time"] = t_first
        rec["_time_last"] = t_last
        rec["_p_first"] = p_first
        rec["_p_last"] = p_last or p_first
        rec["_p_close"] = p_close
        rec["_s10"] = s10[0]
        rec["_s_close"] = s_close
        rec["_nxt"] = nxt_map.get(d)

        # 四种子口径
        rec["r_scan_10"] = (s10[0] / p_first - 1) * 100
        rec["r_scan_close"] = (s_close / p_first - 1) * 100
        rec["r_close_10"] = (s10[0] / p_close - 1) * 100
        rec["r_close_close"] = (s_close / p_close - 1) * 100
        # 末推口径（与上轮组合分析可比）
        rec["r_last_10"] = (s10[0] / (p_last or p_first) - 1) * 100
        rec["_limitup"] = (r["percent"] or 0) >= BUY_LIMIT_PCT
        out.append(rec)

    notes = [f"{v}  {k}" for k, v in miss.items()]
    return out, notes


def portfolio(rows: list[dict], key: str, topk: int | None = None,
              sort_key=None, label: str = "") -> dict:
    byday: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        byday[r["date"]].append(r)
    dailies = []
    for d, rs in sorted(byday.items()):
        if topk and sort_key:
            rs = sorted(rs, key=sort_key)[:topk]
        dailies.append(statistics.fmean([r[key] for r in rs]))
    if not dailies:
        return {}
    cum = peak = 1.0
    mdd = 0.0
    for d in dailies:
        cum *= (1 + d / 100)
        peak = max(peak, cum)
        mdd = max(mdd, (peak - cum) / peak)
    return {
        "days": len(dailies), "daily": statistics.fmean(dailies),
        "median": statistics.median(dailies),
        "win": 100 * sum(1 for d in dailies if d > 0) / len(dailies),
        "cum": (cum - 1) * 100, "mdd": mdd * 100,
        "avg_n": statistics.fmean([len(v) for v in byday.values()]),
    }


def show(name: str, res: dict) -> None:
    if not res:
        print(f"{name:<36s} 空集")
        return
    print(f"{name:<36s} {res['days']:>3d}天 {res['avg_n']:>5.1f}只/日  "
          f"日均{res['daily']:+6.2f}%  日胜率{res['win']:5.1f}%  "
          f"累计{res['cum']:+8.1f}%  回撤{res['mdd']:5.1f}%")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-07-22")
    args = ap.parse_args()

    rows, notes = build(args.since)
    if not rows:
        print("无样本。")
        return

    # ---------- 口径校验 ----------
    print("=" * 112)
    print("【0】口径校验：自建 收盘→收盘 应等于库里的 next_day_pct")
    print("=" * 112)
    diff = [abs(r["r_close_close"] - r["next_day_pct"]) for r in rows
            if r["next_day_pct"] is not None]
    if diff:
        print(f"  样本 {len(diff)}  平均绝对偏差 {statistics.fmean(diff):.3f}%  "
              f"中位 {statistics.median(diff):.3f}%  "
              f">0.5% 的占比 {100 * sum(1 for x in diff if x > 0.5) / len(diff):.1f}%")
        print("  （偏差来自 5 分钟数据前复权与日线口径差异，量级小即可信）")

    print(f"\n样本 {len(rows)} 条  {min(r['date'] for r in rows)} ~ "
          f"{max(r['date'] for r in rows)}  交易日 "
          f"{len({r['date'] for r in rows})}")
    if notes:
        print("剔除情况：")
        for n in notes:
            print("   ", n)

    # ---------- 主问题 ----------
    print("\n" + "=" * 112)
    print("【1】主问题：当日推荐时刻买入 → 次日 10:00 卖出（按天等权组合，等权=每天全买）")
    print("=" * 112)
    print(f"{'口径':<36s} {'天数':>4s} {'只/日':>6s} {'日均':>8s} {'日胜率':>8s} {'累计':>10s} {'回撤':>7s}")
    for key, name in [
        ("r_scan_10", "①推荐时刻买→次日10:00卖  【问题口径】"),
        ("r_scan_close", "②推荐时刻买→次日收盘卖"),
        ("r_close_10", "③当日收盘买→次日10:00卖"),
        ("r_close_close", "④当日收盘买→次日收盘卖 (≈next_day_pct)"),
        ("r_last_10", "⑤当日末推时刻买→次日10:00卖"),
    ]:
        show(name, portfolio(rows, key))

    print("\n  【逐票平均 vs 组合累计】（两者会分叉，组合才是真实账户）")
    for key, name in [("r_scan_10", "①推荐时刻买→次日10:00卖"),
                      ("r_close_close", "④收盘买→收盘卖")]:
        vals = [r[key] for r in rows]
        print(f"    {name:<26s} 逐票均 {statistics.fmean(vals):+6.2f}%  "
              f"中位 {statistics.median(vals):+6.2f}%  "
              f"单票胜率 {100 * sum(1 for v in vals if v > 0) / len(vals):5.1f}%")

    # ---------- 推荐时刻分层 ----------
    print("\n" + "=" * 112)
    print("【2】推荐时刻早晚的影响（①号口径：推荐时刻买→次日10:00卖）")
    print("=" * 112)
    buckets = [("09:30-10:30", "09:30", "10:30"), ("10:30-11:30", "10:30", "11:30"),
               ("13:00-14:00", "13:00", "14:00"), ("14:00-15:00", "14:00", "15:00")]
    for nm, lo, hi in buckets:
        sub = [r for r in rows if lo <= r["_time"] < hi]
        if sub:
            show(f"  推荐于 {nm}", portfolio(sub, "r_scan_10"))

    # ---------- 类别 ----------
    print("\n" + "=" * 112)
    print("【3】分类别（①号口径）")
    print("=" * 112)
    bycat = defaultdict(list)
    for r in rows:
        bycat[r["category"]].append(r)
    for cat, sub in sorted(bycat.items(), key=lambda kv: -len(kv[1])):
        show(f"  {cat} (n={len(sub)})", portfolio(sub, "r_scan_10"))

    # ---------- 🎯 / 档位 / 规则 ----------
    print("\n" + "=" * 112)
    print("【4】选法对比（①号口径：推荐时刻买→次日10:00卖）")
    print("=" * 112)
    try:
        from scanner.ranking import _entry_tier, _is_nextday_marked
        c2 = sqlite3.connect(DB)
        for r in rows:
            try:
                accum = float(r["_bd"].get("accumulated_incl_today"))
            except (TypeError, ValueError):
                accum = None
            entry = {"category": r["category"], "symbol": r["symbol"],
                     "percent": r["percent"], "score_breakdown": r["_bd"],
                     "_candidate": None}
            try:
                r["_mark"] = _is_nextday_marked(entry, c2, accum=accum)
                r["_tier"] = _entry_tier(entry, c2, accum=accum, marked=r["_mark"])
            except Exception:
                r["_mark"] = r["_tier"] = None
        c2.close()
        show("  全买（基线）", portfolio(rows, "r_scan_10"))
        show("  只买 🎯", portfolio([r for r in rows if r["_mark"] is True], "r_scan_10"))
        show("  不买 🎯", portfolio([r for r in rows if r["_mark"] is not True], "r_scan_10"))
        for t in (1, 2, 3):
            show(f"  只买档{t}", portfolio([r for r in rows if r["_tier"] == t], "r_scan_10"))
    except Exception as e:  # noqa: BLE001
        print(f"  [!] 🎯/档位重算跳过：{e}")

    r5 = [r for r in rows if r["trend"] in GOOD_TREND
          and r["category"] not in BAD_CAT and (r["percent"] or 0) < 10]
    show("  R5 好trend+排坏类+涨幅<10", portfolio(r5, "r_scan_10"))

    nolimit = [r for r in rows if not r["_limitup"]]
    print("\n  【可成交性修正】剔除扫描时已贴近涨停(>=19%)无法买入的票")
    show("  全买（可成交）", portfolio(nolimit, "r_scan_10"))
    show("  R5（可成交）", portfolio([r for r in r5 if not r["_limitup"]], "r_scan_10"))

    # ---------- 样本外 ----------
    print("\n" + "=" * 112)
    print("【5】样本外检验（前 60% 交易日 = IS，后 40% = OOS）")
    print("=" * 112)
    ds = sorted({r["date"] for r in rows})
    cut = ds[int(len(ds) * 0.6)]
    isr = [r for r in rows if r["date"] < cut]
    oos = [r for r in rows if r["date"] >= cut]
    print(f"  IS {min(r['date'] for r in isr)}~{max(r['date'] for r in isr)} ({len(ds[:ds.index(cut)])}天)  "
          f"OOS {min(r['date'] for r in oos)}~{max(r['date'] for r in oos)} ({len(ds) - ds.index(cut)}天)")
    for nm, sub in [("全买", rows), ("R5", r5),
                    ("只买🎯", [r for r in rows if r.get("_mark") is True])]:
        a, b = portfolio([r for r in sub if r["date"] < cut], "r_scan_10"), \
               portfolio([r for r in sub if r["date"] >= cut], "r_scan_10")
        if a and b:
            print(f"  {nm:<8s} IS 日均{a['daily']:+6.2f}% 累计{a['cum']:+7.1f}%  |  "
                  f"OOS 日均{b['daily']:+6.2f}% 累计{b['cum']:+7.1f}%  "
                  f"日胜率{b['win']:5.1f}%")

    print("\n【成本】创业板双边约 0.15%~0.30%（含印花税卖出 0.05%），上表日均需扣减。")


if __name__ == "__main__":
    main()
