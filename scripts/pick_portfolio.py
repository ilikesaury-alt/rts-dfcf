"""组合级验证：按天操作的真实体验（用户每天选几只买，不是买下全部）。

关键区别：逐票平均 ≠ 组合收益。每日票数不同时两者会分叉，
而用户是「每天挑几只」，所以必须按天等权口径评估。

输出：累计复利 / 日胜率 / 最大回撤 / 日均持仓数，并对比多种选法与 Top-K。

用法: python scripts/pick_portfolio.py [--days 90] [--topk 1 2 3 5]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
from collections import defaultdict
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "scanner.db"

from scanner.ranking import _entry_tier, _is_nextday_marked  # noqa: E402

GOOD_TREND = {"企稳回升", "主线回调", "回踩·到买点", "温和放量", "震荡整理", "低位企稳", "整理"}
BAD_TREND = {"回踩整理", "动量启动", "阴跌企稳", "加速启动", "弱转强", "动量延续"}
BAD_CAT = {"momentum", "pullback"}


def load(days: int) -> list[dict]:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    sql = """
        SELECT * FROM recommendations
        WHERE excluded = 0 AND next_day_pct IS NOT NULL
          AND date >= date((SELECT max(date) FROM recommendations), ?)
        ORDER BY date, rowid
    """
    raw = conn.execute(sql, (f"-{days - 1} days",)).fetchall()
    conn.close()
    latest: dict[tuple[str, str], sqlite3.Row] = {}
    for r in raw:
        latest[(r["date"], r["symbol"] or "__nosym__")] = r
    rows = [dict(r) for r in latest.values()]

    conn = sqlite3.connect(DB)
    for r in rows:
        try:
            bd = json.loads(r["score_breakdown"] or "{}")
        except (ValueError, TypeError):
            bd = {}
        r["_bd"] = bd if isinstance(bd, dict) else {}
        try:
            accum = float(r["_bd"].get("accumulated_incl_today"))
        except (TypeError, ValueError):
            accum = None
        r["_accum"] = accum
        entry = {
            "category": r["category"], "symbol": r["symbol"],
            "percent": r["percent"], "score_breakdown": r["_bd"], "_candidate": None,
        }
        try:
            r["_mark"] = _is_nextday_marked(entry, conn, accum=accum)
            r["_tier"] = _entry_tier(entry, conn, accum=accum, marked=r["_mark"])
        except Exception:
            r["_mark"] = None
            r["_tier"] = None
    conn.close()
    return rows


def bdnum(r, key):
    try:
        return float(r["_bd"].get(key))
    except (TypeError, ValueError):
        return None


def portfolio(rows: list[dict], topk: int | None = None,
              sort_key=None) -> dict:
    """按天等权组合。topk=None 表示全买。"""
    byday: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        byday[r["date"]].append(r)
    dailies = []
    for d, rs in sorted(byday.items()):
        if sort_key and topk:
            rs = sorted(rs, key=sort_key)[:topk]
        elif topk:
            rs = rs[:topk]
        dailies.append(statistics.fmean([r["next_day_pct"] for r in rs]))
    if not dailies:
        return {}
    cum = 1.0
    peak = 1.0
    mdd = 0.0
    for d in dailies:
        cum *= (1 + d / 100)
        peak = max(peak, cum)
        mdd = max(mdd, (peak - cum) / peak)
    return {
        "days": len(dailies),
        "daily": statistics.fmean(dailies),
        "median": statistics.median(dailies),
        "win": 100 * sum(1 for d in dailies if d > 0) / len(dailies),
        "cum": (cum - 1) * 100,
        "mdd": mdd * 100,
        "avg_n": statistics.fmean([len(v) for v in byday.values()]),
    }


def show(name: str, res: dict) -> None:
    if not res:
        print(f"{name:<34s} 空集")
        return
    print(f"{name:<34s} {res['days']:>3d}天 日均{res['avg_n']:5.1f}只  "
          f"日收益{res['daily']:+6.2f}% 日中位{res['median']:+6.2f}%  "
          f"日胜率{res['win']:5.1f}%  累计{res['cum']:+8.1f}%  回撤{res['mdd']:6.1f}%")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--topk", type=int, nargs="*", default=[1, 2, 3, 5])
    args = ap.parse_args()

    rows = load(args.days)
    rows.sort(key=lambda r: (r["date"], r["id"]))
    print("=" * 108)
    print(f"组合级验证（按天等权，模拟「每天挑几只买」）  样本 {len(rows)}  "
          f"{min(r['date'] for r in rows)} ~ {max(r['date'] for r in rows)}")
    print("=" * 108)

    print("\n【基线：全买 vs 各档位】")
    show("全部买入（无筛选）", portfolio(rows))
    show("只买 🎯（档0）", portfolio([r for r in rows if r["_mark"] is True]))
    for t in (1, 2, 3):
        show(f"只买档{t}", portfolio([r for r in rows if r["_tier"] == t]))
    show("排除档3（档0+1+2）", portfolio([r for r in rows if r["_tier"] is not None and r["_tier"] < 3]))

    print("\n【规则子集】")
    r_trend = [r for r in rows if r["trend"] in GOOD_TREND]
    show("好trend", portfolio(r_trend))
    r5 = [r for r in rows if r["trend"] in GOOD_TREND and r["category"] not in BAD_CAT
          and (r["percent"] or 0) < 10]
    show("R5 好trend+排坏类+涨幅<10", portfolio(r5))
    r7 = [r for r in r5
          if (bdnum(r, "time_bonus") is None or bdnum(r, "time_bonus") <= 0)
          and (bdnum(r, "rps_bonus") is None or bdnum(r, "rps_bonus") <= 0)]
    show("R7 R5+非尾盘+非高RPS", portfolio(r7))
    r8 = [r for r in r7 if (bdnum(r, "accumulated_incl_today") is None
                            or bdnum(r, "accumulated_incl_today") < 10)]
    show("R8 R7+5日累涨<10", portfolio(r8))

    print("\n【每日只买 Top-K（按档位升序，档位即主表排序结果）】")
    sk = lambda r: (r["_tier"] if r["_tier"] is not None else 9, -(r["percent"] or 0))  # noqa: E731
    for k in args.topk:
        show(f"Top{k} 全体", portfolio(rows, topk=k, sort_key=sk))
    for k in args.topk:
        show(f"Top{k} 在 R8 子集内", portfolio(r8, topk=k,
                                           sort_key=lambda r: (r["_tier"] if r["_tier"] is not None else 9,
                                                               -(r["percent"] or 0))))

    print("\n【成本敏感性】次日买入持有一天，双边成本约 0.15%~0.30%")
    print("  上表日收益需扣除该成本后再判断是否可执行。")


if __name__ == "__main__":
    main()
