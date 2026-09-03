"""选票边际分析：在每日几十只产出中，哪些可观测特征能真正分出胜负？

口径（对齐 rts-dfcf-audit 五大陷阱）：
  - excluded=0（被风险标签判"不该买"的票不计成绩）
  - 按 (date, symbol) 去重取 rowid 最大者（6-13 前是每轮追加，需去重）
  - next_day_pct IS NOT NULL
  - 日期中性化：所有收益减去当日全体均值，消除大盘环境影响

用法: python scripts/pick_edge_analysis.py [--days 60] [--min-n 20]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
from collections import defaultdict
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "scanner.db"


def load_rows(days: int) -> list[dict]:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    sql = """
        SELECT r.* FROM recommendations r
        WHERE r.excluded = 0
          AND r.next_day_pct IS NOT NULL
          AND r.date >= date((SELECT max(date) FROM recommendations), ?)
        ORDER BY r.date, r.rowid
    """
    raw = conn.execute(sql, (f"-{days - 1} days",)).fetchall()
    conn.close()

    # 按 (date, symbol) 去重，取 rowid 最大（最后一轮）
    latest: dict[tuple[str, str], sqlite3.Row] = {}
    for r in raw:
        key = (r["date"], r["symbol"] or "__nosym__")
        latest[key] = r

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


def stats(vals: list[float]) -> dict:
    if not vals:
        return {}
    n = len(vals)
    mean = statistics.fmean(vals)
    sd = statistics.pstdev(vals) if n > 1 else 0.0
    se = sd / (n**0.5) if n > 1 else 0.0
    t = mean / se if se > 0 else 0.0
    wins = sum(1 for v in vals if v > 0)
    return {
        "n": n,
        "mean": mean,
        "median": statistics.median(vals),
        "win": 100.0 * wins / n,
        "hit7": 100.0 * sum(1 for v in vals if v >= 7) / n,
        "t": t,
        "sd": sd,
    }


def neutralize(rows: list[dict], field: str = "next_day_pct") -> None:
    """日期中性化：减去当日全体均值，写入 _adj。"""
    by_date: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        if r[field] is not None:
            by_date[r["date"]].append(r[field])
    day_mean = {d: statistics.fmean(v) for d, v in by_date.items()}
    for r in rows:
        base = r[field]
        r["_adj"] = None if base is None else base - day_mean.get(r["date"], 0.0)


def bucket_numeric(rows, keyfn, edges, label) -> list[tuple]:
    """按数值分桶。"""
    buckets: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        v = keyfn(r)
        if v is None:
            continue
        placed = False
        for name, lo, hi in edges:
            if lo <= v < hi:
                buckets[name].append(r["_adj"])
                placed = True
                break
        if not placed:
            continue
    return [(f"{label}:{k}", stats(v)) for k, v in sorted(buckets.items())]


def bucket_flag(rows, keyfn, label) -> list[tuple]:
    """按布尔特征分桶。"""
    yes: list[float] = []
    no: list[float] = []
    for r in rows:
        v = keyfn(r)
        if v is None:
            continue
        (yes if v else no).append(r["_adj"])
    return [(f"{label}:有", stats(yes)), (f"{label}:无", stats(no))]


def bucket_cat(rows, keyfn, label) -> list[tuple]:
    buckets: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        v = keyfn(r)
        if v is None:
            continue
        buckets[str(v)].append(r["_adj"])
    return [(f"{label}:{k}", stats(v)) for k, v in sorted(buckets.items())]


def bd(r, key):
    return r["_bd"].get(key)


def fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def build_features(rows: list[dict]) -> list[tuple]:
    """返回 [(特征名, [(桶名, stats)])]"""
    feats = []

    feats.append(("类别", bucket_cat(rows, lambda r: r["category"], "类别")))

    feats.append((
        "当日涨幅 percent",
        bucket_numeric(rows, lambda r: fnum(r["percent"]),
                       [("<5%", -99, 5), ("5-10%", 5, 10), ("10-15%", 10, 15),
                        ("15-20%", 15, 20), (">=20%", 20, 999)], "涨幅"),
    ))

    feats.append((
        "评分 score",
        bucket_numeric(rows, lambda r: fnum(r["score"]),
                       [("<40", -99, 40), ("40-55", 40, 55), ("55-70", 55, 70),
                        ("70-85", 70, 85), (">=85", 85, 999)], "score"),
    ))

    feats.append((
        "主力资金净占比",
        bucket_numeric(rows, lambda r: fnum(bd(r, "fund_flow_main_pct")),
                       [("<-5%", -99, -5), ("-5~0", -5, 0), ("0~5", 0, 5),
                        ("5~15", 5, 15), (">=15", 15, 999)], "资金"),
    ))

    feats.append((
        "5日累计涨幅(含今日)",
        bucket_numeric(rows, lambda r: fnum(bd(r, "accumulated_incl_today")),
                       [("<10%", -99, 10), ("10-25%", 10, 25), ("25-45%", 25, 45),
                        (">=45%", 45, 999)], "累涨"),
    ))

    feats.append((
        "板块共振数 v_st_sector_count",
        bucket_numeric(rows, lambda r: fnum(bd(r, "v_st_sector_count")),
                       [("0", -0.5, 0.5), ("1", 0.5, 1.5), ("2", 1.5, 2.5),
                        (">=3", 2.5, 999)], "共振"),
    ))

    feats.append((
        "换手率分 turnover_bonus",
        bucket_numeric(rows, lambda r: fnum(bd(r, "turnover_bonus")),
                       [("负", -99, -0.01), ("0", -0.01, 0.01), ("正", 0.01, 999)], "换手"),
    ))

    feats.append((
        "RPS分 rps_bonus",
        bucket_numeric(rows, lambda r: fnum(bd(r, "rps_bonus")),
                       [("负", -99, -0.01), ("0", -0.01, 0.01), ("正", 0.01, 999)], "RPS"),
    ))

    feats.append((
        "市值分 market_cap_bonus",
        bucket_numeric(rows, lambda r: fnum(bd(r, "market_cap_bonus")),
                       [("负", -99, -0.01), ("0", -0.01, 0.01), ("正", 0.01, 999)], "市值"),
    ))

    feats.append((
        "信号时点 time_bonus",
        bucket_numeric(rows, lambda r: fnum(bd(r, "time_bonus")),
                       [("负", -99, -0.01), ("0", -0.01, 0.01), ("正", 0.01, 999)], "时点"),
    ))

    feats.append((
        "连榜天数分 list_streak_bonus",
        bucket_numeric(rows, lambda r: fnum(bd(r, "list_streak_bonus")),
                       [("负", -99, -0.01), ("0", -0.01, 0.01), ("正", 0.01, 999)], "连榜"),
    ))

    feats.append((
        "排名动量 list_momentum_bonus",
        bucket_numeric(rows, lambda r: fnum(bd(r, "list_momentum_bonus")),
                       [("负", -99, -0.01), ("0", -0.01, 0.01), ("正", 0.01, 999)], "榜动量"),
    ))

    feats.append((
        "趋势 trend",
        bucket_cat(rows, lambda r: r["trend"] if r["trend"] else None, "trend"),
    ))

    feats.append((
        "板块封顶 sector_capped",
        bucket_flag(rows, lambda r: r["sector_capped"], "板块封顶"),
    ))

    feats.append((
        "K线过期 stale_kline",
        bucket_flag(rows, lambda r: r["stale_kline"], "K线过期"),
    ))

    feats.append((
        "超买标记 v_st_overbought",
        bucket_flag(rows, lambda r: bd(r, "v_st_overbought"), "超买"),
    ))

    feats.append((
        "弱转强 v_st_weak",
        bucket_flag(rows, lambda r: bd(r, "v_st_weak"), "弱转强"),
    ))

    return feats


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--min-n", type=int, default=15)
    args = ap.parse_args()

    rows = load_rows(args.days)
    if not rows:
        print("无样本")
        return

    neutralize(rows)
    vals = [r["_adj"] for r in rows if r["_adj"] is not None]
    raws = [r["next_day_pct"] for r in rows if r["next_day_pct"] is not None]

    print("=" * 78)
    print(f"选票边际分析  样本 {len(vals)} 条  (excluded=0, 按(date,symbol)去重)")
    print("=" * 78)
    s = stats(raws)
    print(f"\n【全体基线】次日收益 均值 {s['mean']:+.2f}%  中位 {s['median']:+.2f}%  "
          f"胜率 {s['win']:.1f}%  hit7 {s['hit7']:.1f}%  波动 {s['sd']:.2f}%")
    print("（下表为日期中性化后的超额收益 adj，已减去当日全体均值）\n")

    for name, buckets in build_features(rows):
        print(f"── {name} " + "─" * (70 - len(name) * 2))
        shown = 0
        for bname, st in buckets:
            if not st or st["n"] < args.min_n:
                continue
            shown += 1
            flag = " *" if abs(st["t"]) >= 2.0 else "  "
            print(f"  {bname:<26s} n={st['n']:<4d} 超额{st['mean']:+6.2f}%  "
                  f"中位{st['median']:+6.2f}%  胜率{st['win']:5.1f}%  "
                  f"hit7 {st['hit7']:4.1f}%  t={st['t']:+5.2f}{flag}")
        if shown == 0:
            print("  (样本不足)")
        print()

    print("说明: t 绝对值 >= 2.0 视为统计显著(*)。分桶越多越易假阳性，需结合逻辑一致性判断。")


if __name__ == "__main__":
    main()
