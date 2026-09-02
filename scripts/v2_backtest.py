"""v2 池选历史回测：离线重建每个交易日的 v2 池（pool → 排雷 → 标签），统计次日命中率。

用法：
    python -X utf8 scripts/v2_backtest.py                # 全部历史
    python -X utf8 scripts/v2_backtest.py --days 30      # 最近 30 天
    python -X utf8 scripts/v2_backtest.py --start 2026-08-01 --end 2026-08-31
    python -X utf8 scripts/v2_backtest.py --detail       # 逐日明细

口径
----
- 次日涨幅 = 下一交易日收盘 / 信号日收盘 - 1（与 next_day ≥7% 校准口径一致）。
- 命中 hit7 = 次日涨幅 ≥ 7%。
- v2 池选无评分门槛（安全池全量入选），所以本回测衡量的是「排雷层 + 标签」的筛选价值：
  对比组 = 全榜 GEM 基线 / 被排雷剔除组 / v1 落库类别（recommendations 冻结行）。

保真缺口（与 scanner/historical_rescan.py 同源，见其 docstring）
----
- 排雷仅含纯 K 线信号（bias20 过高 / 冲高回落 / 翻绿+高开回落）；主力出货（资金流）
  与财务风险两个实时信号不可离线复现 → 重扫池 ≥ 线上实际池，线上命中率应≥本结果。
- rank_change 恒 0、无历史市值（MAX_MARKET_CAP 过滤缺失）。
- v1 对比用落库冻结行（含盘中实时语义），口径略宽于离线重建，仅供粗对比。
"""

from __future__ import annotations

import argparse
import sqlite3
import statistics
import sys
from bisect import bisect_right
from collections import defaultdict
from datetime import date, timedelta
from typing import Any, cast

from scanner.candidates import filter_gem_stocks
from scanner.config import MAX_STOCK_PRICE
from scanner.danger import KLINE_DANGER_SIGNALS, evaluate_pool, hard_flags
from scanner.db.dal import get_prev_ranks
from scanner.historical_rescan import _MIN_KLINE_BARS, _load_all_klines
from scanner.matcher import label_all_candidates
from scanner.models import V2_CATEGORY, Candidate
from scanner.orchestrator import _v2_kline_summary
from scanner.pool import build_pool

V1_CATEGORIES = ("new_face", "known_new_face", "momentum", "rebound", "short_term")


def _pos_float(x) -> float | None:
    """DB 数值安全转正浮点（脏值/非数 → None）。"""
    if not isinstance(x, (int, float)) or isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError, OverflowError):
        return None
    return v if v > 0 else None


def _load_price_maps(conn: sqlite3.Connection) -> tuple[dict, dict]:
    """(symbol,date) -> close / open 查找表（来自 daily_kline，脏值剔除）。"""
    close_map: dict[tuple[str, str], float] = {}
    open_map: dict[tuple[str, str], float] = {}
    for sym, d, o, c in conn.execute("SELECT symbol, date, open, close FROM daily_kline"):
        cv = _pos_float(c)
        ov = _pos_float(o)
        if cv is not None:
            close_map[(sym, d)] = cv
        if ov is not None:
            open_map[(sym, d)] = ov
    return close_map, open_map


def _next_pct(
    close_map: dict,
    open_map: dict,
    sym: str,
    d: str,
    next_d: str,
    buy_at: str,
) -> float | None:
    """次日期望收益：close 口径 = 次收/今收-1；open 口径 = 次日开盘买入→次日收盘。"""
    if buy_at == "open":
        o2 = open_map.get((sym, next_d))
        c2 = close_map.get((sym, next_d))
        if not o2 or not c2:
            return None
        return (c2 / o2 - 1) * 100.0
    c1 = close_map.get((sym, d))
    c2 = close_map.get((sym, next_d))
    if not c1 or not c2:
        return None
    return (c2 / c1 - 1) * 100.0


class _Stats:
    def __init__(self, label: str) -> None:
        self.label = label
        self.pcts: list[float] = []

    def add(self, pct: float | None) -> None:
        if pct is not None:
            self.pcts.append(pct)

    @property
    def n(self) -> int:
        return len(self.pcts)

    def line(self) -> str:
        p = self.pcts
        if not p:
            return f"{self.label:<28} n=0"
        hit7 = sum(1 for x in p if x >= 7)
        hit3 = sum(1 for x in p if x >= 3)
        neg = sum(1 for x in p if x < 0)
        return (
            f"{self.label:<28} n={len(p):>5}  hit7={hit7 / len(p) * 100:5.1f}%  "
            f"hit3={hit3 / len(p) * 100:5.1f}%  负={neg / len(p) * 100:5.1f}%  "
            f"avg={statistics.mean(p):+5.2f}%  med={statistics.median(p):+5.2f}%"
        )


def main() -> None:
    ap = argparse.ArgumentParser(description="v2 池选历史回测（次日命中率）")
    ap.add_argument("--start", help="信号日起始 YYYY-MM-DD")
    ap.add_argument("--end", help="信号日结束 YYYY-MM-DD")
    ap.add_argument("--days", type=int, default=0, help="仅最近 N 天（0=全部）")
    ap.add_argument(
        "--buy-at",
        choices=("close", "open"),
        default="close",
        help="收益口径：close=次日收盘/今收；open=次日开盘买入→次日收盘",
    )
    ap.add_argument("--detail", action="store_true", help="逐日明细")
    args = ap.parse_args()
    buy_at = args.buy_at

    if hasattr(sys.stdout, "reconfigure"):
        cast("Any", sys.stdout).reconfigure(encoding="utf-8")
    conn = sqlite3.connect("scanner.db")

    # 信号池：appearances 每日首条快照（与 historical_rescan 同口径）
    by_date: dict[str, list[tuple]] = defaultdict(list)
    seen: dict[str, set[str]] = defaultdict(set)
    for d, sym, name, rank, pct, val in conn.execute(
        "SELECT date, symbol, name, rank, percent, value FROM appearances ORDER BY date, rank"
    ):
        if sym in seen[d]:
            continue
        seen[d].add(sym)
        by_date[d].append((sym, name, rank, pct, val))

    close_map, open_map = _load_price_maps(conn)
    kline_store = _load_all_klines(conn)
    calendar = sorted({d for (_s, d) in close_map})

    # 时间窗口（信号日口径，循环前算好）
    rec_dates = sorted(by_date)
    if not rec_dates:
        print("appearances 无数据")
        return
    end = args.end or rec_dates[-1]
    if args.days > 0:
        start = (date.fromisoformat(rec_dates[-1]) - timedelta(days=args.days)).isoformat()
    else:
        start = args.start or rec_dates[0]

    v2 = _Stats("v2 池选(硬排雷后)")
    board = _Stats("全榜GEM基线(排雷前)")
    dropped = _Stats("被硬排雷剔除组")
    soft_g = _Stats("软标记组(K线信号)")
    v1_all = _Stats("v1五桶综合(落库)")
    v1_by_cat: dict[str, _Stats] = {}
    label_stats: dict[str, _Stats] = defaultdict(lambda: _Stats(""))
    flag_stats: dict[str, _Stats] = defaultdict(lambda: _Stats(""))  # 被剔除组按排雷信号分型

    daily_v2: list[tuple[str, int, float]] = []  # (date, n, hit7%)

    for d in rec_dates:
        if not (start <= d <= end):
            continue
        idx = bisect_right(calendar, d)
        if idx >= len(calendar):
            continue  # 无次日行情
        next_d = calendar[idx]

        raw = [
            {
                "symbol": sym,
                "code": sym,
                "name": name,
                "percent": pct or 0.0,
                "value": val or 0.0,
                "rank_change": 0,  # 历史不可重建，见 historical_rescan 缺口 1
                "rank": rank,
            }
            for sym, name, rank, pct, val in by_date[d]
        ]
        stocks = filter_gem_stocks(raw)

        klines: dict[str, list] = {}
        usable = []
        for s in stocks:
            entry = kline_store.get(s.symbol)
            if entry is None:
                continue
            dates, bars = entry
            cut = bisect_right(dates, d)
            if cut < _MIN_KLINE_BARS:
                continue
            sliced = bars[:cut]
            if sliced[-1]["date"] != d:
                continue
            s.current = sliced[-1]["close"]
            if s.current > 0 and s.current > MAX_STOCK_PRICE:
                continue
            klines[s.symbol] = sliced
            usable.append(s)
        if not usable:
            continue

        pool_rows = build_pool(usable, klines, d, get_prev_ranks(conn, d))
        danger_map = evaluate_pool(pool_rows, klines, {}, {})
        danger_syms = {sym for sym, fl in danger_map.items() if hard_flags(fl)}
        soft_syms = {
            sym
            for sym, fl in danger_map.items()
            if sym not in danger_syms and any(f in KLINE_DANGER_SIGNALS for f in fl)
        }

        # 标签（只对安全池打，与线上一致）
        stock_by_sym = {s.symbol: s for s in usable}
        cands = []
        for row in pool_rows:
            if row.symbol in danger_syms:
                continue
            stock = stock_by_sym.get(row.symbol)
            if not stock:
                continue
            cands.append(
                Candidate(
                    stock=stock,
                    category=V2_CATEGORY,
                    score=0,
                    reason="池选",
                    kline=_v2_kline_summary(row, klines.get(row.symbol), d),
                    first_seen="",
                )
            )
        label_all_candidates(cands, klines, d)

        for row in pool_rows:
            pct = _next_pct(close_map, open_map, row.symbol, d, next_d, buy_at)
            board.add(pct)
            if row.symbol in danger_syms:
                dropped.add(pct)
            elif row.symbol in soft_syms:
                soft_g.add(pct)
                for fl in danger_map.get(row.symbol) or []:
                    flag_stats[str(fl)].add(pct)
        day_pcts: list[float] = []
        for c in cands:
            pct = _next_pct(close_map, open_map, c.stock.symbol, d, next_d, buy_at)
            v2.add(pct)
            if pct is not None:
                day_pcts.append(pct)
            labels_raw = c.kline.dimensions.get("dip_labels") if c.kline else None
            labels = [str(x) for x in labels_raw] if isinstance(labels_raw, (list, tuple)) else []
            for lb in labels:
                label_stats[str(lb)].add(pct)
        if day_pcts:
            daily_v2.append((d, len(day_pcts), sum(1 for x in day_pcts if x >= 7) / len(day_pcts) * 100))
            if args.detail:
                print(
                    f"    {d}  池选 {len(day_pcts):>3}  hit7={daily_v2[-1][2]:5.1f}%"
                    f"  avg={statistics.mean(day_pcts):+.2f}%"
                )

        # v1 对比：recommendations 冻结行（excluded=0），同日次日涨幅
        for cat, sym in conn.execute(
            "SELECT category, symbol FROM recommendations WHERE date=? AND excluded=0 AND category!='pool_pick'",
            (d,),
        ):
            if cat == "comeback":
                continue
            v1_all.add(_next_pct(close_map, open_map, sym, d, next_d, buy_at))
            v1_by_cat.setdefault(cat, _Stats(f"v1 {cat}")).add(_next_pct(close_map, open_map, sym, d, next_d, buy_at))

    print(f"(收益口径: {'次日开盘买→次日收盘' if buy_at == 'open' else '次日收盘/今收'})")
    print()
    print("=" * 100)
    for st in [v2, board, dropped, soft_g, v1_all] + [v1_by_cat[c] for c in V1_CATEGORIES if c in v1_by_cat]:
        print(st.line())
    if flag_stats:
        print("-" * 100)
        print("被排雷剔除组按信号分型（一只票可命中多信号）:")
        for fl in sorted(flag_stats, key=lambda k: -flag_stats[k].n):
            st_fl: _Stats = flag_stats[fl]
            st_fl.label = f"  [{fl}]"
            print(st_fl.line())
    if label_stats:
        print("-" * 100)
        print("v2 标签切片（dip_labels，一只票可带多标签）:")
        for lb in sorted(label_stats, key=lambda k: -label_stats[k].n):
            st_lb: _Stats = label_stats[lb]
            st_lb.label = f"  [{lb}]"
            print(st_lb.line())
    print("=" * 100)
    if daily_v2:
        n_days = len(daily_v2)
        avg_hit = statistics.mean(h for _d, _n, h in daily_v2)
        avg_n = statistics.mean(n for _d, n, _h in daily_v2)
        best = max(daily_v2, key=lambda x: x[2])
        print(
            f"信号日 {n_days} 天 | 日均池选 {avg_n:.1f} 只 | 日均 hit7 {avg_hit:.1f}%"
            f" | 最佳日 {best[0]} ({best[2]:.0f}%, n={best[1]})"
        )
    conn.close()


if __name__ == "__main__":
    main()
