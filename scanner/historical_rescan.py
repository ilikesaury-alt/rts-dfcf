"""历史重扫（gold standard 验证 Step 2 / P0）。

为什么需要它
------------
`recommendations` 表里存的是「写库当时用旧权重算出的冻结分」，
改 config 不会 retrospective 生效。所以 `portfolio_backtest` replay 的永远是旧权重分，
无法验证「改权重到底有没有改善 P&L」。

本模块用**当前（新）config 权重**，从最原始的历史输入重跑各策略引擎：
- 信号来源：`appearances` 表（每个交易日真实的雪球热榜快照：symbol/name/rank/percent/value）
- 行情来源：`daily_kline`（截至信号日的完整日线，已是收盘量能，无需盘中投影）
- 引擎：`analysis.analyze_*` + `validator.validate_*`（与实时 orchestrator 同口径）
- 板块簇：用 `sector.classify_sector(name)` 对当日热榜名重建（与实时一致）

输出与 `portfolio_backtest._load_signals` 同构的 `Signal` 列表，使 `run_backtest`
可以直接 replay「新权重生成的信号宇宙」，与「冻结旧分」做苹果对苹果对比。

可忠实重建的类别（仅依赖 appearances + daily_kline）：
    new_face / known_new_face / momentum / short_term / rebound
    —— 这些类别的引擎输入（percent/value/rank/rank_change/kline）都能从历史表重建。

不可忠实重建、保持冻结分的类别：
    comeback（回马枪）：掉榜 off-list 变体，需实时 adapter / watch_pool / 资金流，
    无法仅从 appearances + daily_kline 重建，故重扫宇宙不含它（沿用 recommendations 冻结分）。
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import date, timedelta
from typing import Iterable

from scanner.analysis import (
    analyze_momentum,
    analyze_new_face,
    analyze_rebound,
    analyze_short_term,
)
from scanner.models import KlineSummary, StockInfo
from scanner.portfolio_backtest import Signal, _assign_rank_scores
from scanner.sector import classify_sector
from scanner.trading_session import is_trading_day
from scanner.validator import (
    validate_momentum,
    validate_nf,
    validate_rebound,
    validate_short_term,
)

# 可忠实重扫的类别（comeback 为 off-list 变体，无法从 appearances 重建，保持冻结分）
RESCANABLE_CATEGORIES = ("new_face", "known_new_face", "momentum", "short_term", "rebound")

_ANALYZER_REGISTRY = {
    "new_face": analyze_new_face,
    "momentum": analyze_momentum,
    "short_term": analyze_short_term,
    "rebound": analyze_rebound,
}
_VALIDATOR_REGISTRY = {
    "new_face": validate_nf,
    "momentum": validate_momentum,
    "short_term": validate_short_term,
    "rebound": validate_rebound,
}


def _classify_rescanned(passing: dict[str, KlineSummary],
                        first_seen: bool) -> tuple[str, KlineSummary] | None:
    """从「通过的引擎集合」挑一个类别标签，与 orchestrator._classify_category 同优先级。

    - 新票(first_seen)：new_face > rebound > short_term > momentum
    - 老票(非 first_seen)：rebound > short_term > momentum > known_new_face
      （new_face 引擎通过但属老票时，标签落 known_new_face）
    """
    if first_seen:
        order = [("new_face", "new_face"), ("rebound", "rebound"),
                 ("short_term", "short_term"), ("momentum", "momentum")]
    else:
        order = [("rebound", "rebound"), ("short_term", "short_term"),
                 ("momentum", "momentum"), ("new_face", "known_new_face")]
    for key, label in order:
        if key in passing:
            return label, passing[key]
    return None


def _nth_trading_day_after(d: date, n: int) -> date | None:
    """返回 d 之后第 n 个交易日（不含 d）。与 portfolio_backtest 同口径。"""
    cursor = d
    max_iter = max(n * 10, 365)
    for _ in range(n):
        cursor += timedelta(days=1)
        while not is_trading_day(cursor):
            cursor += timedelta(days=1)
            max_iter -= 1
            if max_iter <= 0:
                return None
    return cursor


def _get_kline_upto(conn: sqlite3.Connection, symbol: str, d: str) -> list[dict]:
    """返回 symbol 截至日期 d（含）的完整日线。"""
    rows = conn.execute(
        "SELECT date, open, close, high, low, volume, percent FROM daily_kline "
        "WHERE symbol=? AND date <= ? ORDER BY date",
        (symbol, d),
    ).fetchall()
    return [
        {"date": r[0], "open": r[1], "close": r[2], "high": r[3],
         "low": r[4], "volume": r[5], "percent": r[6]}
        for r in rows
    ]


def rescan_all_signals(conn: sqlite3.Connection, cfg, calendar: list[str],
                       cal_index: dict[str, int], cal_end: str,
                       categories: Iterable[str] | None = None) -> list[Signal]:
    """用当前 config 权重重扫所有可重建类别，返回与 _load_signals 同构的 Signal 列表。

    cfg 提供：buy_delay / hold_days（执行锚定）、start / end / days（信号日窗口）。
    categories：限定重扫类别（默认 RESCANABLE_CATEGORIES）。comeback 等非重建类别须排除。
    """
    if categories is None:
        categories = RESCANABLE_CATEGORIES
    categories = set(categories)

    rows = conn.execute(
        "SELECT date, symbol, name, rank, percent, value FROM appearances ORDER BY date"
    ).fetchall()

    by_date: dict[str, list[dict]] = defaultdict(list)
    for d, sym, name, rank, pct, val in rows:
        by_date[d].append({"symbol": sym, "name": name, "rank": rank,
                           "percent": pct, "value": val})

    # rank_change = 当日 rank − 上一次上榜 rank；first_seen：该票此日期是否首次上榜
    prev_rank: dict[str, int] = {}
    rc_map: dict[tuple[str, str], int] = {}
    first_seen: dict[tuple[str, str], bool] = {}
    for d in sorted(by_date):
        for a in by_date[d]:
            sym = a["symbol"]
            first_seen[(d, sym)] = sym not in prev_rank
            rc_map[(d, sym)] = a["rank"] - prev_rank[sym] if sym in prev_rank else 0
            prev_rank[sym] = a["rank"]

    # 当日板块簇：按热榜名分类聚合（与实时 orchestrator.get_sector_clusters 同口径）
    clusters_by_date: dict[str, dict[str, list[str]]] = {}
    for d, apps in by_date.items():
        cl: dict[str, list[str]] = defaultdict(list)
        for a in apps:
            cl[classify_sector(a["name"])].append(a["symbol"])
        clusters_by_date[d] = dict(cl)

    signals: list[Signal] = []
    for d in sorted(by_date):
        apps = by_date[d]
        clusters = clusters_by_date.get(d)
        for a in apps:
            kline = _get_kline_upto(conn, a["symbol"], d)
            if not kline or len(kline) < 5:
                continue
            stock = StockInfo(
                symbol=a["symbol"], name=a["name"], code=a["symbol"],
                percent=float(a["percent"]),
                current=kline[-1]["close"],
                value=float(a["value"]),
                rank_change=rc_map.get((d, a["symbol"]), 0),
                rank=a["rank"],
                # 注：short_term 用 market_cap 选小/中盘，appearances 无此字段，
                # 重建为 0 会使 value_small_cap/value_mid_cap 维度不触发（已知保真缺口）。
                market_cap=0.0,
            )
            historical_kline = [k for k in kline if k["date"] != d]
            closes = [k["close"] for k in historical_kline]

            # 并行跑各引擎，收集通过的类别（与 orchestrator 同口径：每票每引擎独立评分）
            passing: dict[str, KlineSummary] = {}
            for cat in ("new_face", "momentum", "short_term", "rebound"):
                if cat not in categories:
                    continue
                summary = _ANALYZER_REGISTRY[cat](stock, kline, today_str=d)
                if summary is None:
                    continue
                validator = _VALIDATOR_REGISTRY[cat]
                passed, _total, _dims = validator(stock, summary, closes, historical_kline, clusters)
                if passed:
                    passing[cat] = summary

            if not passing:
                continue
            chosen = _classify_rescanned(passing, first_seen.get((d, a["symbol"]), True))
            if chosen is None:
                continue
            cat, summary = chosen
            sig = Signal(rec_date=d, symbol=a["symbol"], name=a["name"],
                         category=cat, score=summary.score)
            buy_d = _nth_trading_day_after(date.fromisoformat(d), cfg.buy_delay)
            if buy_d is None or buy_d.isoformat() > cal_end:
                continue
            buy_str = buy_d.isoformat()
            if buy_str not in cal_index:
                continue
            sig.buy_date = buy_str
            sig.buy_index = cal_index[buy_str]
            exit_idx = sig.buy_index + cfg.hold_days
            if exit_idx >= len(calendar):
                exit_idx = len(calendar) - 1
            sig.exit_index = exit_idx
            signals.append(sig)

    # 信号日窗口过滤（与 _load_signals 同口径）
    if signals:
        rec_dates = [s.rec_date for s in signals]
        max_rec = max(rec_dates)
        min_rec = min(rec_dates)
        end_date = cfg.end or max_rec
        if cfg.days > 0:
            start_date = (date.fromisoformat(max_rec) - timedelta(days=cfg.days)).isoformat()
        else:
            start_date = cfg.start or min_rec
        signals = [s for s in signals if start_date <= s.rec_date <= end_date]

    _assign_rank_scores(signals)
    return signals


def rescan_new_face_signals(conn: sqlite3.Connection, cfg, calendar: list[str],
                             cal_index: dict[str, int], cal_end: str) -> list[Signal]:
    """兼容旧接口：仅重扫 new_face / known_new_face（Step 2 验证用）。"""
    return rescan_all_signals(conn, cfg, calendar, cal_index, cal_end,
                              categories=("new_face", "known_new_face"))
