#!/usr/bin/env python
"""次日大涨高概率规则 — 历史验证脚本。

用法:
    python scripts/nextday_rule_scan.py              # 默认：H1/H2 分半验证
    python scripts/nextday_rule_scan.py --epoch       # 按时间段分 3 段
    python scripts/nextday_rule_scan.py --today       # 仅扫描最新一天
    python scripts/nextday_rule_scan.py --ma5r 6 --atrpct 8 --ret20 40  # 自定义阈值

特征计算复用 scanner/nextday_rule._compute_features（单实现，杜绝双份口径漂移），
阈值默认取 config（NEXTDAY_RULE_*）。bars 窗口固定为模块常量 NEXTDAY_RULE_BARS。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scanner.config import (  # noqa: E402
    NEXTDAY_HIT_THRESHOLD,
    NEXTDAY_RULE_ATRPCT_MIN,
    NEXTDAY_RULE_MA5R_MIN,
    NEXTDAY_RULE_RET20_MAX,
)
from scanner.nextday_rule import _compute_features  # noqa: E402


def _load_data(conn: sqlite3.Connection):
    """加载 appearances + daily_kline，返回 (rows, hist, kline_lists, app)。

    rows:        {symbol: {date: {o, c, h, l, v, p}}}
    hist:        {symbol: sorted list of dates}
    kline_lists: {symbol: [KlineBar 形状 dict]（按 date 升序，喂给模块特征函数）}
    app:         {date: set of symbols}
    """
    rows: dict[str, dict] = {}
    for sym, _ts, d, o, cl, h, lo, v, p in conn.execute(
        "SELECT symbol, timestamp, date, open, close, high, low, volume, percent "
        "FROM daily_kline WHERE close IS NOT NULL"
    ):
        rows.setdefault(sym, {})[d] = {
            "o": o, "c": cl, "h": h, "l": lo, "v": v or 0.0, "p": p,
        }
    hist = {s: sorted(m) for s, m in rows.items()}
    kline_lists = {
        s: [
            {
                "date": d,
                "open": m[d]["o"],
                "high": m[d]["h"],
                "low": m[d]["l"],
                "close": m[d]["c"],
                "volume": m[d]["v"],
                "percent": m[d]["p"],
            }
            for d in hist[s]
        ]
        for s, m in rows.items()
    }

    app = defaultdict(set)
    for dt, sym in conn.execute("SELECT date, symbol FROM appearances"):
        app[dt].add(sym)

    return rows, hist, kline_lists, app


def _feat(kline_lists: dict, hist: dict, sym: str, dt: str):
    """模块特征函数的薄包装：定位 dt 索引并要求存在次日 bar（算次日收益用）。"""
    dd = hist.get(sym)
    if not dd:
        return None
    try:
        i = dd.index(dt)
    except ValueError:
        return None
    if i + 1 >= len(dd):
        return None
    return _compute_features(kline_lists[sym], i)


def _next_day_yield(rows: dict, hist: dict, sym: str, dt: str) -> float | None:
    """close[T] → close[T+1] 涨幅（%）；缺次日 bar 返回 None。"""
    dd = hist.get(sym)
    if not dd:
        return None
    try:
        i = dd.index(dt)
    except ValueError:
        return None
    if i + 1 >= len(dd):
        return None
    m = rows[sym]
    c1 = m[dd[i + 1]]["c"]
    cT = m[dd[i]]["c"]
    if not c1 or not cT:
        return None
    return (c1 / cT - 1) * 100


def run(
    ma5r_min: float = NEXTDAY_RULE_MA5R_MIN,
    atrpct_min: float = NEXTDAY_RULE_ATRPCT_MIN,
    ret20_max: float = NEXTDAY_RULE_RET20_MAX,
    epoch: bool = False,
    today_only: bool = False,
):
    db_path = ROOT / "scanner.db"
    if not db_path.exists():
        print(f"ERROR: {db_path} not found")
        return

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rows, hist, kline_lists, app = _load_data(conn)

    def _passes(f) -> bool:
        return (
            f is not None
            and f[0] >= ma5r_min
            and f[1] >= atrpct_min
            and f[2] <= ret20_max
        )

    all_dates = sorted(app.keys())

    if today_only:
        dt = all_dates[-1] if all_dates else None
        if not dt:
            print("No data")
            return
        syms = app[dt]
        picks = []
        for s in syms:
            f = _feat(kline_lists, hist, s, dt)
            if _passes(f):
                picks.append((s, f[0], f[1], f[2]))
        print(f"=== {dt} — 规则命中 {len(picks)}/{len(syms)} 只 ===")
        print(f"  规则: ma5r≥{ma5r_min}% & atrpct≥{atrpct_min}% & ret20≤{ret20_max}%")
        for s, m, a, r in sorted(picks, key=lambda x: -x[2]):
            print(f"  {s:12s}  ma5r={m:+.1f}%  atrpct={a:.1f}%  ret20={r:+.1f}%")
        return

    # ── 分半 / 分段验证 ──
    if epoch:
        e1 = [d for d in all_dates if d < "2026-08-07"]
        e2 = [d for d in all_dates if "2026-08-07" <= d <= "2026-08-10"]
        e3 = [d for d in all_dates if d > "2026-08-10"]
        splits = [("E1", e1), ("E2", e2), ("E3", e3)]
    else:
        half = len(all_dates) // 2
        splits = [("H1", all_dates[:half]), ("H2", all_dates[half:])]

    # 基准（榜单全体次日 ≥ 阈值占比）
    base = {}
    for hk, dates in splits:
        total = 0
        hits = 0
        for d in dates:
            for s in app[d]:
                y = _next_day_yield(rows, hist, s, d)
                if y is None:
                    continue
                total += 1
                if y >= NEXTDAY_HIT_THRESHOLD:
                    hits += 1
        base[hk] = (total, hits / total if total else 0)

    print(f"=== 规则: ma5r≥{ma5r_min}% & atrpct≥{atrpct_min}% & ret20≤{ret20_max}% ===")
    print(f"    threshold={NEXTDAY_HIT_THRESHOLD}%")
    print()

    for hk, dates in splits:
        b_total, b_rate = base[hk]
        picks = []
        for d in dates:
            for s in app[d]:
                f = _feat(kline_lists, hist, s, d)
                if not _passes(f):
                    continue
                y = _next_day_yield(rows, hist, s, d)
                if y is not None:
                    picks.append((d, s, y))

        n = len(picks)
        n_dates = len(dates)
        per_day = n / n_dates if n_dates else 0
        hit = sum(1 for _, _, y in picks if y >= NEXTDAY_HIT_THRESHOLD) / n if n else 0
        mean = sum(y for _, _, y in picks) / n if n else 0
        dn = sum(1 for _, _, y in picks if y <= -NEXTDAY_HIT_THRESHOLD) / n if n else 0
        lift = hit / b_rate if b_rate else 0

        print(f"  {hk} ({n_dates} 天, board base={b_rate * 100:.2f}% over {b_total} rows)")
        print(f"    n={n}  {per_day:.1f}/day  hit={hit * 100:.2f}%  LIFT={lift:.2f}x  "
              f"mean={mean:+.2f}%  dn7={dn * 100:.1f}%")
        print(f"    net@0.30%%={mean - 0.30:+.2f}%  net@0.50%%={mean - 0.50:+.2f}%")
        print()


def main():
    parser = argparse.ArgumentParser(description="次日大涨高概率规则验证")
    parser.add_argument("--ma5r", type=float, default=NEXTDAY_RULE_MA5R_MIN)
    parser.add_argument("--atrpct", type=float, default=NEXTDAY_RULE_ATRPCT_MIN)
    parser.add_argument("--ret20", type=float, default=NEXTDAY_RULE_RET20_MAX)
    parser.add_argument("--epoch", action="store_true", help="按时间段分 3 段")
    parser.add_argument("--today", action="store_true", help="仅扫描最新一天")
    args = parser.parse_args()
    run(
        ma5r_min=args.ma5r,
        atrpct_min=args.atrpct,
        ret20_max=args.ret20,
        epoch=args.epoch,
        today_only=args.today,
    )


if __name__ == "__main__":
    main()
