"""选票规则验证：把分层发现压成可执行规则，并做样本外检验。

时间切分：按日期排序，前 60% 为 in-sample（IS），后 40% 为 out-of-sample（OOS）。
规则分两类：
  - 用户可见（终端/飞书能看到）：category / trend / percent / score  → 可直接执行
  - 内部维度（终端看不到）：time_bonus / rps_bonus / accumulated → 需要改代码才能用

用法: python scripts/pick_rule_test.py [--days 90]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
from collections import defaultdict
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "scanner.db"

BAD_TREND = {"回踩整理", "动量启动", "阴跌企稳", "加速启动", "弱转强", "动量延续"}
GOOD_TREND = {"企稳回升", "主线回调", "回踩·到买点", "温和放量", "震荡整理", "低位企稳", "整理"}
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
    out = []
    for r in latest.values():
        d = dict(r)
        try:
            d["_bd"] = json.loads(r["score_breakdown"] or "{}")
        except (ValueError, TypeError):
            d["_bd"] = {}
        if not isinstance(d["_bd"], dict):
            d["_bd"] = {}
        out.append(d)
    return out


def neutralize(rows) -> None:
    by_date: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        by_date[r["date"]].append(r["next_day_pct"])
    dm = {d: statistics.fmean(v) for d, v in by_date.items()}
    for r in rows:
        r["_adj"] = r["next_day_pct"] - dm[r["date"]]


def bdnum(r, key):
    try:
        return float(r["_bd"].get(key))
    except (TypeError, ValueError):
        return None


def summarize(rows: list[dict], label: str) -> None:
    if not rows:
        print(f"{label:<30s} 空集")
        return
    vals = [r["next_day_pct"] for r in rows]
    adj = [r["_adj"] for r in rows]
    n = len(vals)
    mean = statistics.fmean(vals)
    sd = statistics.pstdev(vals) if n > 1 else 0
    t = mean / (sd / n**0.5) if sd > 0 else 0
    days = len({r["date"] for r in rows})
    print(f"{label:<30s} n={n:<5d} 日均{n / max(days, 1):5.1f}只  "
          f"收益{mean:+6.2f}% 中位{statistics.median(vals):+6.2f}%  "
          f"胜率{100 * sum(1 for v in vals if v > 0) / n:5.1f}%  "
          f"hit7 {100 * sum(1 for v in vals if v >= 7) / n:4.1f}%  "
          f"超额{statistics.fmean(adj):+6.2f}%  t={t:+5.2f}")


RULES = {
    "R0 全体基线": lambda r: True,
    "R1 排除坏trend": lambda r: r["trend"] not in BAD_TREND,
    "R2 排除坏类别": lambda r: r["category"] not in BAD_CAT,
    "R3 只要好trend": lambda r: r["trend"] in GOOD_TREND,
    "R4 好trend+排除坏类别": lambda r: r["trend"] in GOOD_TREND and r["category"] not in BAD_CAT,
    "R5 R4+当日涨幅<10%": lambda r: (
        r["trend"] in GOOD_TREND and r["category"] not in BAD_CAT
        and (r["percent"] or 0) < 10
    ),
    "R6 R5+非尾盘信号(内部)": lambda r: (
        r["trend"] in GOOD_TREND and r["category"] not in BAD_CAT
        and (r["percent"] or 0) < 10
        and (bdnum(r, "time_bonus") is None or bdnum(r, "time_bonus") <= 0)
    ),
    "R7 R6+非高RPS(内部)": lambda r: (
        r["trend"] in GOOD_TREND and r["category"] not in BAD_CAT
        and (r["percent"] or 0) < 10
        and (bdnum(r, "time_bonus") is None or bdnum(r, "time_bonus") <= 0)
        and (bdnum(r, "rps_bonus") is None or bdnum(r, "rps_bonus") <= 0)
    ),
    "R8 R7+5日累涨<10%(内部)": lambda r: (
        r["trend"] in GOOD_TREND and r["category"] not in BAD_CAT
        and (r["percent"] or 0) < 10
        and (bdnum(r, "time_bonus") is None or bdnum(r, "time_bonus") <= 0)
        and (bdnum(r, "rps_bonus") is None or bdnum(r, "rps_bonus") <= 0)
        and (bdnum(r, "accumulated_incl_today") is None
             or bdnum(r, "accumulated_incl_today") < 10)
    ),
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    args = ap.parse_args()

    rows = load(args.days)
    if not rows:
        print("无样本")
        return
    neutralize(rows)
    rows.sort(key=lambda r: (r["date"], r["id"]))

    dates = sorted({r["date"] for r in rows})
    cut = dates[int(len(dates) * 0.6)]
    isr = [r for r in rows if r["date"] < cut]
    oos = [r for r in rows if r["date"] >= cut]

    print("=" * 100)
    print(f"选票规则验证  总样本 {len(rows)}  切分日 {cut}")
    print(f"  IS  {len(isr)} 条 ({dates[0]} ~ {dates[int(len(dates) * 0.6) - 1]})")
    print(f"  OOS {len(oos)} 条 ({cut} ~ {dates[-1]})")
    print("=" * 100)

    for label, fn in RULES.items():
        print(f"\n【{label}】")
        print("  IS  ", end="")
        summarize([r for r in isr if fn(r)], "")
        print("  OOS ", end="")
        summarize([r for r in oos if fn(r)], "")

    print("\n" + "=" * 100)
    print("逐条规则单独效果（OOS，仅统计通过该条的记录 vs 被过滤掉的记录）")
    print("=" * 100)
    singles = {
        "排除 time_bonus>0": lambda r: bdnum(r, "time_bonus") is None or bdnum(r, "time_bonus") <= 0,
        "排除 rps_bonus>0": lambda r: bdnum(r, "rps_bonus") is None or bdnum(r, "rps_bonus") <= 0,
        "排除 换手bonus>0": lambda r: bdnum(r, "turnover_bonus") is None or bdnum(r, "turnover_bonus") <= 0,
        "排除 5日累涨>=10": lambda r: (bdnum(r, "accumulated_incl_today") is None
                                   or bdnum(r, "accumulated_incl_today") < 10),
        "排除 连榜bonus<0": lambda r: bdnum(r, "list_streak_bonus") is None or bdnum(r, "list_streak_bonus") >= 0,
        "排除 榜动量<0": lambda r: bdnum(r, "list_momentum_bonus") is None or bdnum(r, "list_momentum_bonus") >= 0,
        "排除 涨幅>=10%": lambda r: (r["percent"] or 0) < 10,
    }
    for label, fn in singles.items():
        keep = [r for r in oos if fn(r)]
        drop = [r for r in oos if not fn(r)]
        if len(keep) < 10 or len(drop) < 10:
            continue
        km = statistics.fmean([r["next_day_pct"] for r in keep])
        dm = statistics.fmean([r["next_day_pct"] for r in drop])
        print(f"  {label:<22s} 保留 n={len(keep):<4d} 收益 {km:+.2f}%   "
              f"剔除 n={len(drop):<4d} 收益 {dm:+.2f}%   差值 {km - dm:+.2f}%")


if __name__ == "__main__":
    main()
