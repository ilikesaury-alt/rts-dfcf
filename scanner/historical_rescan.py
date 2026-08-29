"""历史重扫：用**当前 config 权重**复现历史上每个交易日的推荐榜。

为什么需要它
------------
`recommendations` 表里存的是「写库当时用旧权重算出的冻结分」，改 config 不会
retrospective 生效。所以 `portfolio_backtest` replay 的永远是旧权重分，
无法回答「改权重到底有没有改善 P&L」。

保真度原则（2026-08-08 重写）
-----------------------------
本模块**不再手写**一套 analyzer→validator→分类逻辑。早期版本那么做过，结果与线上
`orchestrator` 漂移得非常厉害（is_new 用「有史以来首次」而非「近 3 日无上榜」、
漏掉 MIN_SCORE 门槛、丢掉 validation_bonus、分类漏了 st_weak_to_strong 条件、
没有创业板过滤），导致重扫宇宙的类别构成与线上完全对不上（new_face 1019 → 28），
基于它得出的 P&L 结论全部作废。

现在的做法：**直接复用 orchestrator 的真实流水线**
    filter_gem_stocks → score_stock（内含 5 路 analyze_* + HIGH_RISK_TRENDS
    + MIN_SCORE + validate/validation_bonus + classify_category）
    只把数据来源从「实时 API」换成「历史表」。逻辑只有一份，不会再漂移。
    （同板块上限 2026-08-12 已整体移除，此处无需复现。）

数据来源
--------
- 信号池：`appearances`（每个交易日真实的雪球热榜快照：symbol/name/rank/percent/value）
- 行情：`daily_kline`（截至信号日的完整日线，一次性预载进内存后按日期切片）
- is_new / first_date：`get_symbol_appearances(..., as_of=信号日)`，与线上同一函数同一口径

已知保真缺口（无法从历史表重建，均已在代码中标注）
--------------------------------------------------
1. `rank_change`：雪球 API 自带的全市场热度排名变动（阈值 500/1000/2000），
   `appearances` 只存 1~100 的榜内名次，无法反推 → 恒为 0。
   影响：`_vol_rank_combo_score` 与 `first_breakout_bonus` 在重扫中不触发。
   （旧版本用「当日 rank − 上次 rank」伪造，量级只有 ±100 且符号相反，比置 0 更糟。）
2. `market_cap`：无历史市值表 → 恒为 0。影响 short_term 的 value_small_cap /
   value_mid_cap 维度不触发，以及 MAX_MARKET_CAP 大市值过滤缺失。
   （MAX_STOCK_PRICE 价格过滤可用当日收盘价重建，已实现。）
3. 实时增强项：资金流、涨停池、盘中/开盘分、RPS、板块情绪、time_bonus、
   list_streak 等 `apply_all_bonuses` / `accumulate_final_score` 的加分，
   以及依赖风险标签的 `candidate_excluded_by_risk` 硬过滤 —— 均需实时 API，
   重扫不复现。这些是**跨候选普遍加分**，对同日排序的扰动小于上面两项。
4. `comeback`（回马枪）：掉榜 off-list 变体，需实时 watch_pool / adapter，
   不在重扫宇宙内（由调用方合并 `recommendations` 的冻结分补上）。

输出与 `portfolio_backtest._load_signals` 同构的 `Signal` 列表。
"""

from __future__ import annotations

import sqlite3
from bisect import bisect_right
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Iterable

from scanner.candidate_pool import ScanSession
from scanner.candidates import filter_gem_stocks, score_stock

# 可忠实重扫的类别（comeback 为 off-list 变体，无法从 appearances 重建，保持冻结分）。
# 2026-08-20 收敛：单一事实来源见 scanner/categories.RESCANABLE_CATEGORIES。
from scanner.categories import RESCANABLE_CATEGORIES  # noqa: E402
from scanner.config import MAX_STOCK_PRICE
from scanner.models import Candidate, KlineBar, make_kline_bar
from scanner.portfolio_backtest import Signal, _assign_rank_scores, _dedup_signals
from scanner.sector import get_sector_clusters
from scanner.trading_session import _nth_trading_day_after

# K 线最少根数：低于此值所有 analyze_* 直接返回 None，提前跳过省掉切片开销
_MIN_KLINE_BARS = 5



def _post_close(d: str) -> datetime:
    """信号日收盘后的时刻（15:30）。

    传给 `score_stock` → `analyze_*` → `_project_today_vol`，使盘中量能投影关闭
    （elapsed=240，倍数恒为 1）。不传的话会用「跑回测那一刻」的真实时间算 elapsed，
    早盘跑回测会把历史上已收盘的完整量能再放大 ~10 倍，结果随运行时刻漂移。
    """
    y, m, dd = (int(x) for x in d.split("-"))
    return datetime(y, m, dd, 15, 30)


def _load_all_klines(conn: sqlite3.Connection) -> dict[str, tuple[list[str], list[KlineBar]]]:
    """一次性预载全部日线，返回 {symbol: (已排序日期列表, bar 列表)}。

    早期版本对每个 (date, symbol) 发一次 SQL 且每次都取全量历史，
    4700+ 次查询是重扫耗时的主因。改为单次全表扫描 + 内存 bisect 切片。
    bar 统一走 make_kline_bar 契约（与实时扫描 fetch_kline/get_cached_kline 同源），
    保证重扫与实时评分口径一致，不因数据源不同产生漂移。
    """
    rows = conn.execute(
        "SELECT symbol, date, open, close, high, low, volume, percent "
        "FROM daily_kline ORDER BY symbol, date"
    ).fetchall()
    out: dict[str, tuple[list[str], list[KlineBar]]] = {}
    cur_sym: str | None = None
    dates: list[str] = []
    bars: list[KlineBar] = []
    for sym, d, o, c, h, low, vol, pct in rows:
        if sym != cur_sym:
            if cur_sym is not None:
                out[cur_sym] = (dates, bars)
            cur_sym, dates, bars = sym, [], []
        bar = make_kline_bar({"date": d, "open": o, "close": c, "high": h,
                              "low": low, "volume": vol, "percent": pct})
        if bar is None:
            continue  # 脏 bar（close<=0/date 非法）剔除，与实时链路一致
        dates.append(d)
        bars.append(bar)
    if cur_sym is not None:
        out[cur_sym] = (dates, bars)
    return out


def _primary_candidate(nf: Candidate | None, mo: Candidate | None,
                       rb: Candidate | None, st: Candidate | None) -> Candidate | None:
    """从 `score_stock` 的返回桶中还原 `classify_category` 选中的那一个标签。

    `score_stock` 对「首板票同时满足超短」会双挂（同时返回 nf 与 st），线上两个桶
    都会落库；但组合回测里同一票同一天只能建一个仓位，所以取分类主标签。
    桶的互斥性保证了这个优先级能唯一还原分类结果。
    （同板块上限已移除，st 不再有保留/丢弃分支。）
    """
    if nf is not None:
        return nf
    if mo is not None:
        return mo
    if rb is not None:
        return rb
    if st is not None:
        return st
    return None


def rescan_all_signals(conn: sqlite3.Connection, cfg, calendar: list[str],
                       cal_index: dict[str, int], cal_end: str,
                       categories: Iterable[str] | None = None) -> list[Signal]:
    """用当前 config 权重重扫历史，返回与 `_load_signals` 同构的 Signal 列表。

    cfg 提供：buy_delay / hold_days（执行锚定）、start / end / days（信号日窗口）。
    categories：限定保留的类别标签（默认 RESCANABLE_CATEGORIES）。
    """
    if categories is None:
        categories = RESCANABLE_CATEGORIES
    categories = set(categories)

    app_rows = conn.execute(
        "SELECT date, symbol, name, rank, percent, value FROM appearances ORDER BY date, rank"
    ).fetchall()
    by_date: dict[str, list[tuple]] = defaultdict(list)
    seen_on_date: dict[str, set[str]] = defaultdict(set)
    for d, sym, name, rank, pct, val in app_rows:
        # 同一天同一票可能有多条快照（盘中每轮扫描各记一次），只保留首条
        if sym in seen_on_date[d]:
            continue
        seen_on_date[d].add(sym)
        by_date[d].append((sym, name, rank, pct, val))

    kline_store = _load_all_klines(conn)

    signals: list[Signal] = []
    for d in sorted(by_date):
        now_ref = _post_close(d)

        # 1) 复用线上的创业板/港股/ST 过滤与去重
        raw = [
            {"symbol": sym, "code": sym, "name": name,
             "percent": pct or 0.0, "value": val or 0.0,
             # rank_change 无法从 appearances 重建，见模块 docstring 缺口 1
             "rank_change": 0, "rank": rank}
            for sym, name, rank, pct, val in by_date[d]
        ]
        stocks = filter_gem_stocks(raw)

        # 2) 按信号日切片 K 线；顺带用当日收盘价补 current 并复现价格上限过滤
        klines: dict[str, list[KlineBar] | None] = {}
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
                continue  # 信号日无行情（停牌等），线上也拿不到今日 bar
            s.current = sliced[-1]["close"]
            if s.current > 0 and s.current > MAX_STOCK_PRICE:
                continue  # 与 scan_with_raw 的 MAX_STOCK_PRICE 过滤一致
            klines[s.symbol] = sliced
            usable.append(s)
        if not usable:
            continue

        clusters = get_sector_clusters(usable)
        session = ScanSession()
        session.reset_if_new_day(d)

        scored: list[tuple] = []
        for s in usable:
            nf, mo, rb, st = score_stock(
                s, conn, klines, d, session, clusters, now=now_ref)
            if nf is None and mo is None and rb is None and st is None:
                continue
            scored.append((nf, mo, rb, st))

        for nf, mo, rb, st in scored:
            cand = _primary_candidate(nf, mo, rb, st)
            if cand is None or cand.category not in categories:
                continue
            sig = Signal(rec_date=d, symbol=cand.stock.symbol, name=cand.stock.name,
                         category=cand.category, score=cand.score)
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
            if exit_idx <= sig.buy_index:
                # 与 _load_signals 同族：买入日已是日历末尾时 clamp 产生
                # 当日买入当日卖出的 T+0 假交易，无法持有 ≥1 交易日则跳过。
                continue
            sig.exit_index = exit_idx
            signals.append(sig)

    # 4) 信号日窗口过滤（与 _load_signals 同口径）
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

    signals = _dedup_signals(signals)
    _assign_rank_scores(signals)
    return signals
