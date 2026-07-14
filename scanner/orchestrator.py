import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace as dataclass_replace
from datetime import date, timedelta

import requests

from scanner.analysis import analyze_momentum, analyze_new_face, analyze_pullback
from scanner.validator import validate
from scanner.api import (
    analyze_intraday,
    analyze_opening_strength,
    compute_surge_sentiment,
    estimate_live_volume,
    fetch_biaosheng,
    fetch_kline,
    fetch_market_caps_batch,
    fetch_market_index,
    make_session,
)
from scanner.candidate_pool import ScanSession
from scanner.config import (
    now_beijing,
    FIRST_BREAKOUT_BONUS,
    FIRST_BREAKOUT_RANK_CHANGE,
    FIRST_BREAKOUT_VOL_RATIO,
    FIRST_TODAY_BONUS,
    KLINE_FETCH_DAYS,
    KLINE_MIN_LENGTH,
    MAX_MARKET_CAP,
    MAX_STOCK_PRICE,
    MOMENTUM_MIN_SCORE,
    NEW_FACE_LOOKBACK_DAYS,
    NEW_FACE_MIN_SCORE,
    PULLBACK_MIN_SCORE,
    RPS_BONUS_HIGH,
    RPS_BONUS_LOW,
    RPS_BONUS_MEDIUM,
    RPS_PCTILE_HIGH,
    RPS_PCTILE_LOW,
    RPS_PCTILE_MEDIUM,
)
from scanner.database import (
    get_cached_kline,
    get_symbol_appearances,
    record_appearances,
    save_kline_to_db,
)
from scanner.enhancer import (
    accumulate_final_score,
    apply_all_bonuses,
    compute_market_env_bonus,
    compute_time_bonus,
)
from scanner.models import Candidate, KlineSummary, StockInfo
from scanner.rank_trend import update_rank_history
from scanner.sector import get_sector_clusters
from scanner.trading_session import is_trading_day, is_trading_time
from scanner.utils import is_gem, is_hk_stock, is_st

_thread_local = threading.local()


def _get_session() -> requests.Session:
    if not hasattr(_thread_local, "session"):
        _thread_local.session = make_session()
    return _thread_local.session

_session_state = ScanSession()


def _fetch_all_klines(conn: sqlite3.Connection, session: requests.Session, stocks: list[StockInfo]) -> dict[str, list[dict] | None]:
    result: dict[str, list[dict] | None] = {}
    needs_fetch: list[str] = []
    stale_cache: dict[str, list[dict]] = {}

    for s in stocks:
        cached = get_cached_kline(conn, s.symbol)
        if cached:
            if len(cached) < KLINE_MIN_LENGTH:
                stale_cache[s.symbol] = cached
                needs_fetch.append(s.symbol)
                continue
            max_date_str = max(k["date"] for k in cached)
            max_date = date.fromisoformat(max_date_str)
            cursor = max_date + timedelta(days=1)
            trading_days_missing = 0
            while cursor < now_beijing().date():
                if is_trading_day(cursor):
                    trading_days_missing += 1
                cursor += timedelta(days=1)
            if not is_trading_time() or trading_days_missing == 0:
                result[s.symbol] = cached
                continue
            stale_cache[s.symbol] = cached
        needs_fetch.append(s.symbol)

    if not needs_fetch:
        return result

    def _fetch_one(sym: str) -> tuple[str, list[dict] | None]:
        sess = _get_session()
        kline = fetch_kline(sess, sym, KLINE_FETCH_DAYS)
        return sym, kline

    with ThreadPoolExecutor(max_workers=8) as pool:
        fut_map = {pool.submit(_fetch_one, sym): sym for sym in needs_fetch}
        for fut in as_completed(fut_map):
            sym = fut_map[fut]
            try:
                sym, kline = fut.result()
                if kline:
                    result[sym] = kline
                    save_kline_to_db(conn, sym, kline)
                elif sym in stale_cache:
                    result[sym] = stale_cache[sym]
            except Exception as e:
                print(f"  [!] K线获取失败 {sym}: {e}")
                if sym in stale_cache:
                    result[sym] = stale_cache[sym]

    for sym in needs_fetch:
        if sym not in result and sym in stale_cache:
            result[sym] = stale_cache[sym]

    return result


def _build_candidate(stock: StockInfo, kline_summary: KlineSummary | None, category: str,
                     is_first_today: bool, first_date: str, kline: list[dict] | None) -> Candidate:
    first_breakout = (stock.rank_change >= FIRST_BREAKOUT_RANK_CHANGE
                      and kline_summary.volume_ratio > FIRST_BREAKOUT_VOL_RATIO)
    return Candidate(
        stock=stock, category=category, score=kline_summary.score,
        reason=kline_summary.trend, kline=kline_summary,
        first_seen=first_date,
        first_today_bonus=FIRST_TODAY_BONUS if is_first_today else 0,
        first_breakout_bonus=FIRST_BREAKOUT_BONUS if first_breakout else 0,
        history_pct=[k["percent"] for k in kline] if kline else [],
    )


def _try_candidate(stock: StockInfo, kline_summary: KlineSummary | None, category: str,
                   is_first_today: bool, first_date: str, kline: list[dict] | None,
                   closes: list[float], historical: list[dict],
                   clusters: dict[str, list[str]] | None) -> Candidate | None:
    if kline_summary is None:
        return None
    min_score = {
        "new_face": NEW_FACE_MIN_SCORE,
        "known_new_face": NEW_FACE_MIN_SCORE,
        "momentum": MOMENTUM_MIN_SCORE,
        "pullback": PULLBACK_MIN_SCORE,
    }[category]
    if kline_summary.score < min_score:
        return None
    passed, bonus, dims = validate(category, stock, kline_summary, closes, historical, clusters)
    if not passed:
        return None
    kline_summary.score += bonus
    kline_summary.dimensions["validation_bonus"] = bonus
    kline_summary.dimensions.update(dims)
    return _build_candidate(stock, kline_summary, category, is_first_today, first_date, kline)


def _classify_category(stock: StockInfo, is_new: bool,
                       c_pb: Candidate | None, c_mo: Candidate | None,
                       c_nf: Candidate | None) -> str | None:
    """按价格结构（而非尝试顺序）选最贴合的策略标签。"""
    today_pct = stock.percent
    if is_new:
        if c_nf is not None:
            return "new_face"
        if c_mo is not None:
            return "momentum"
        if c_pb is not None:
            return "pullback"
        return None
    # 老股：今日下跌优先归回调；否则上升动能优先；浅回踩/震荡上行归回调；兜底新面孔
    if c_pb is not None and today_pct <= 0:
        return "pullback"
    if c_mo is not None:
        return "momentum"
    if c_pb is not None:
        return "pullback"
    if c_nf is not None:
        return "known_new_face"
    return None


def _filter_gem_stocks(raw: list[dict]) -> list[StockInfo]:
    gem_stocks: list[StockInfo] = []
    for i, item in enumerate(raw, 1):
        symbol = item.get("symbol", "")
        code = item.get("code", "")
        name = item.get("name", "")
        if is_hk_stock(symbol) or not is_gem(code) or is_st(name):
            continue
        gem_stocks.append(StockInfo(
            symbol=symbol, name=name, code=code,
            percent=item.get("percent") or 0.0,
            current=item.get("current") or 0.0,
            value=item.get("value") or 0.0,
            rank_change=item.get("rank_change") or 0,
            rank=item.get("rank", i),
            source_tag=item.get("source_tag", "xueqiu"),
        ))
    return gem_stocks


def _score_stock(stock: StockInfo, conn: sqlite3.Connection, klines: dict[str, list[dict] | None],
                 today: str, session_state: ScanSession,
                 clusters: dict[str, list[str]] | None = None
                 ) -> tuple[Candidate | None, Candidate | None, Candidate | None]:
    is_first_today = session_state.mark_seen(stock.symbol)
    app_history = get_symbol_appearances(conn, stock.symbol, NEW_FACE_LOOKBACK_DAYS)
    previous_dates = [a["date"] for a in app_history]
    is_new = len(previous_dates) == 0
    first_date = previous_dates[0] if previous_dates else today
    kline = klines.get(stock.symbol)

    new_face_result: Candidate | None = None
    momentum_result: Candidate | None = None
    pullback_result: Candidate | None = None

    nk = analyze_new_face(stock, kline)
    mk = analyze_momentum(stock, kline)
    pk = analyze_pullback(stock, kline)

    historical = [k for k in (kline or []) if k["date"] != today]
    closes = [k["close"] for k in historical]

    # 三策略独立打分 + 各自交叉验证，再按价格结构选最贴合的标签
    c_nf = _try_candidate(stock, nk, "new_face" if is_new else "known_new_face",
                          is_first_today, first_date, kline, closes, historical, clusters)
    c_mo = _try_candidate(stock, mk, "momentum",
                          is_first_today, first_date, kline, closes, historical, clusters)
    c_pb = _try_candidate(stock, pk, "pullback",
                          is_first_today, first_date, kline, closes, historical, clusters)

    category = _classify_category(stock, is_new, c_pb, c_mo, c_nf)
    if category in ("new_face", "known_new_face"):
        return c_nf, None, None
    if category == "momentum":
        return None, c_mo, None
    if category == "pullback":
        return None, None, c_pb
    return None, None, None


def _compute_rps(candidates: list[Candidate]) -> dict[str, int]:
    scores: dict[str, int] = {}
    if len(candidates) < 2:
        return {c.stock.symbol: 0 for c in candidates}
    accum = [(c.stock.symbol, c.kline.accumulated_pct if c.kline else 0) for c in candidates]
    sorted_by_accum = sorted(accum, key=lambda x: x[1])
    total = len(sorted_by_accum)
    for rank, (sym, _) in enumerate(sorted_by_accum):
        pctile = (rank + 1) * 100 // total
        if pctile >= RPS_PCTILE_HIGH:
            scores[sym] = RPS_BONUS_HIGH
        elif pctile >= RPS_PCTILE_MEDIUM:
            scores[sym] = RPS_BONUS_MEDIUM
        elif pctile < RPS_PCTILE_LOW:
            scores[sym] = RPS_BONUS_LOW
        else:
            scores[sym] = 0
    return scores


def scan_with_raw(raw: list[dict], conn: sqlite3.Connection,
                  session: requests.Session) -> tuple[
                      list[Candidate], list[Candidate], list[Candidate],
                      list[Candidate], list[StockInfo], int]:
    global _session_state
    session_state = _session_state
    today = now_beijing().date().isoformat()
    session_state.reset_if_new_day(today)

    sentiment_info = compute_surge_sentiment(raw)
    gem_stocks = _filter_gem_stocks(raw)

    record_appearances(conn, [
        {"symbol": s.symbol, "name": s.name, "percent": s.percent, "value": s.value}
        for s in gem_stocks
    ])
    session_state.update_list_presence({s.symbol for s in gem_stocks})

    stale_syms = list(sym for sym, c in session_state.today_pool.items() if c.is_stale)
    mc_syms = list(set(s.symbol for s in gem_stocks) | set(stale_syms))
    market_caps = fetch_market_caps_batch(session, mc_syms) if mc_syms else {}

    gem_stocks_filtered: list[StockInfo] = []
    filtered_large_cap = 0
    for s in gem_stocks:
        cap_data = market_caps.get(s.symbol, {})
        cap_current = cap_data.get("current", 0)
        if cap_current and s.current == 0:
            s.current = cap_current
        if s.current > 0 and s.current > MAX_STOCK_PRICE:
            continue
        mc = cap_data.get("market_cap", 0)
        if mc > 0 and mc > MAX_MARKET_CAP:
            filtered_large_cap += 1
            continue
        gem_stocks_filtered.append(s)

    klines = _fetch_all_klines(conn, session, gem_stocks_filtered)

    clusters = get_sector_clusters(gem_stocks_filtered)

    new_faces: list[Candidate] = []
    momentum: list[Candidate] = []
    pullback_list: list[Candidate] = []

    for stock in gem_stocks_filtered:
        nf, mo, pb = _score_stock(stock, conn, klines, today, session_state, clusters)
        if nf:
            new_faces.append(nf)
        if mo:
            momentum.append(mo)
        if pb:
            pullback_list.append(pb)

    all_candidates = new_faces + momentum + pullback_list
    for c in all_candidates:
        cap_data = market_caps.get(c.stock.symbol, {})
        c.market_cap = cap_data.get("market_cap", 0)
        c.circ_market_cap = cap_data.get("circ_market_cap", 0)

    rps_scores: dict[str, int] = {}
    rps_scores.update(_compute_rps(all_candidates))

    intraday_scores: dict[str, float | None] = {}
    opening_scores: dict[str, float | None] = {}
    live_volumes: dict[str, float | None] = {}

    if all_candidates:
        with ThreadPoolExecutor(max_workers=6) as pool:
            _parallel_fetch(pool, session, all_candidates,
                            intraday_scores, opening_scores, live_volumes)

    market_idx_pct = fetch_market_index(session)
    market_env_bonus = compute_market_env_bonus(market_idx_pct)
    time_bonus = compute_time_bonus()

    apply_all_bonuses(all_candidates, gem_stocks_filtered, intraday_scores,
                      opening_scores, live_volumes, market_caps,
                      clusters, market_idx_pct, time_bonus,
                      sentiment_info=sentiment_info, rps_scores=rps_scores,
                      list_streaks=session_state.list_presence,
                      conn=conn)

    for i, c in enumerate(all_candidates):
        extra = accumulate_final_score(c, market_env_bonus, opening_scores)
        all_candidates[i] = dataclass_replace(c, score=c.score + extra)

    update_rank_history({s.symbol: s.rank for s in gem_stocks_filtered})

    session_state.update_pool(all_candidates)
    stale_candidates = session_state.get_stale_candidates()
    session_state.update_stale_quotes(stale_candidates, market_caps)

    new_faces.sort(key=lambda c: -c.score)
    momentum.sort(key=lambda c: -c.score)
    pullback_list.sort(key=lambda c: -c.score)
    return new_faces, momentum, pullback_list, stale_candidates, gem_stocks_filtered, filtered_large_cap


def scan(conn: sqlite3.Connection, session: requests.Session) -> tuple[list[Candidate], list[Candidate], list[Candidate], list[Candidate], list[StockInfo], int]:
    raw = fetch_biaosheng(session)
    return scan_with_raw(raw, conn, session)


def _parallel_fetch(pool: ThreadPoolExecutor, base_session: requests.Session,
                    candidates: list[Candidate],
                    intraday_scores: dict[str, float | None],
                    opening_scores: dict[str, float | None],
                    live_volumes: dict[str, float | None]):
    intra_futs = {}
    for c in candidates:
        sym = c.stock.symbol
        def _do(sym=sym, fn=analyze_intraday):
            return fn(_get_session(), sym)
        intra_futs[pool.submit(_do)] = sym
    for fut in as_completed(intra_futs):
        sym = intra_futs[fut]
        try:
            intraday_scores[sym] = fut.result()
        except Exception as e:
            print(f"  [!] 分时强度失败 {sym}: {e}")
            intraday_scores[sym] = None

    open_futs = {}
    for c_ in candidates:
        sym = c_.stock.symbol
        def _open(sym=sym):
            return analyze_opening_strength(_get_session(), sym)
        open_futs[pool.submit(_open)] = sym
    for fut in as_completed(open_futs):
        sym = open_futs[fut]
        try:
            opening_scores[sym] = fut.result()
        except Exception:
            opening_scores[sym] = None

    vol_futs = {}
    for c_ in candidates:
        sym = c_.stock.symbol
        def _vol(sym=sym):
            return estimate_live_volume(_get_session(), sym)
        vol_futs[pool.submit(_vol)] = sym
    for fut in as_completed(vol_futs):
        sym = vol_futs[fut]
        try:
            live_volumes[sym] = fut.result()
        except Exception as e:
            print(f"  [!] 实时量比失败 {sym}: {e}")
            live_volumes[sym] = None
