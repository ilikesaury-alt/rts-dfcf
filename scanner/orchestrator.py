from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace as dataclass_replace
from datetime import date, datetime, timedelta

from scanner.api import (
    fetch_biaosheng, fetch_kline, fetch_market_caps_batch,
    analyze_intraday, analyze_opening_strength, estimate_live_volume,
    fetch_market_index, compute_surge_sentiment,
)
from scanner.analysis import analyze_new_face, analyze_momentum
from scanner.config import (
    NEW_FACE_LOOKBACK_DAYS,
    NEW_FACE_MIN_SCORE, MOMENTUM_MIN_SCORE,
    MAX_STOCK_PRICE, MAX_MARKET_CAP,
    NEW_FACE_DIM_TO_WEIGHT_KEY, MOMENTUM_DIM_TO_WEIGHT_KEY,
    RPS_PCTILE_HIGH, RPS_PCTILE_MEDIUM, RPS_PCTILE_LOW,
    RPS_BONUS_HIGH, RPS_BONUS_MEDIUM, RPS_BONUS_LOW,
)
from scanner.database import (
    record_appearances, get_symbol_appearances, get_cached_kline,
    save_kline_to_db, get_active_weights,
)
from scanner.models import StockInfo, Candidate
from scanner.sector import get_sector_clusters
from scanner.rank_trend import update_rank_history
from scanner.trading_session import is_trading_day
from scanner.utils import is_hk_stock, is_gem, is_st
from scanner.candidate_pool import ScanSession
from scanner.enhancer import (
    apply_all_bonuses, compute_time_bonus, compute_market_env_bonus,
    accumulate_final_score, FIRST_TODAY_BONUS, FIRST_BREAKOUT_BONUS,
    FIRST_BREAKOUT_RANK_CHANGE, FIRST_BREAKOUT_VOL_RATIO,
)

_session_state = ScanSession()


def _load_weight_overrides(conn) -> tuple[dict, dict, dict]:
    active_weights_raw: dict = get_active_weights(conn)
    new_face_overrides = {
        NEW_FACE_DIM_TO_WEIGHT_KEY[k]: v
        for k, v in active_weights_raw.items()
        if k in NEW_FACE_DIM_TO_WEIGHT_KEY
    }
    momentum_overrides = {
        MOMENTUM_DIM_TO_WEIGHT_KEY[k]: v
        for k, v in active_weights_raw.items()
        if k in MOMENTUM_DIM_TO_WEIGHT_KEY
    }
    thresholds = {
        k: v for k, v in active_weights_raw.items()
        if k in ("new_face_min_score", "momentum_min_score")
    }
    if active_weights_raw:
        print(f"  [进化] 加载活跃参数, 新面孔{len(new_face_overrides)} 动量{len(momentum_overrides)} 维度已覆盖")
    return new_face_overrides, momentum_overrides, thresholds


def _fetch_all_klines(conn, session, stocks: list[StockInfo]) -> dict[str, list[dict] | None]:
    result: dict[str, list[dict] | None] = {}
    needs_fetch: list[str] = []
    stale_cache: dict[str, list[dict]] = {}

    KLINE_DAYS = 45
    MIN_KLINE_LEN = 34

    for s in stocks:
        cached = get_cached_kline(conn, s.symbol)
        if cached:
            if len(cached) < MIN_KLINE_LEN:
                stale_cache[s.symbol] = cached
                needs_fetch.append(s.symbol)
                continue
            max_date_str = max(k["date"] for k in cached)
            max_date = date.fromisoformat(max_date_str)
            cursor = max_date + timedelta(days=1)
            trading_days_missing = 0
            while cursor < date.today():
                if is_trading_day(cursor):
                    trading_days_missing += 1
                cursor += timedelta(days=1)
            if trading_days_missing <= 2:
                result[s.symbol] = cached
                continue
            stale_cache[s.symbol] = cached
        needs_fetch.append(s.symbol)

    if not needs_fetch:
        return result

    with ThreadPoolExecutor(max_workers=8) as pool:
        fut_map = {pool.submit(fetch_kline, session, sym, KLINE_DAYS): sym for sym in needs_fetch}
        for fut in as_completed(fut_map):
            sym = fut_map[fut]
            try:
                kline = fut.result()
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


def _build_candidate(stock: StockInfo, kline_summary, category: str,
                     is_first_today: bool, first_date: str, kline) -> Candidate:
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


def scan(conn, session) -> tuple[list[Candidate], list[Candidate], list[Candidate], list[StockInfo], int]:
    global _session_state
    session_state = _session_state
    today = date.today().isoformat()
    session_state.reset_if_new_day(today)

    new_face_overrides, momentum_overrides, thresholds = _load_weight_overrides(conn)
    new_face_min = thresholds.get("new_face_min_score", NEW_FACE_MIN_SCORE)
    momentum_min = thresholds.get("momentum_min_score", MOMENTUM_MIN_SCORE)

    raw = fetch_biaosheng(session)
    sentiment_info = compute_surge_sentiment(raw)

    gem_stocks: list[StockInfo] = []
    for i, item in enumerate(raw, 1):
        symbol = item.get("symbol", "")
        code = item.get("code", "")
        name = item.get("name", "")
        if is_hk_stock(symbol) or not is_gem(code) or is_st(name):
            continue
        gem_stocks.append(StockInfo(
            symbol=symbol,
            name=name,
            code=code,
            percent=item.get("percent") or 0.0,
            current=item.get("current") or 0.0,
            value=item.get("value") or 0.0,
            rank_change=item.get("rank_change") or 0,
            rank=i,
        ))

    record_appearances(conn, [
        {"symbol": s.symbol, "name": s.name, "percent": s.percent, "value": s.value}
        for s in gem_stocks
    ])

    session_state.update_list_presence({s.symbol for s in gem_stocks})

    klines = _fetch_all_klines(conn, session, gem_stocks)

    raw_new_faces: list[Candidate] = []
    raw_momentum: list[Candidate] = []

    for stock in gem_stocks:
        is_first_today = session_state.mark_seen(stock.symbol)

        if stock.current > 0 and stock.current > MAX_STOCK_PRICE:
            continue

        app_history = get_symbol_appearances(conn, stock.symbol, NEW_FACE_LOOKBACK_DAYS)
        previous_dates = [a["date"] for a in app_history]
        is_new = len(previous_dates) == 0
        first_date = previous_dates[0] if previous_dates else today

        kline = klines.get(stock.symbol)

        if is_new:
            kline_summary = analyze_new_face(stock, kline, weight_overrides=new_face_overrides)
            if kline_summary and kline_summary.score >= new_face_min:
                raw_new_faces.append(_build_candidate(
                    stock, kline_summary, "new_face", is_first_today, first_date, kline))
            else:
                momentum_result = analyze_momentum(stock, kline, weight_overrides=momentum_overrides)
                if momentum_result and momentum_result.score >= momentum_min:
                    raw_momentum.append(_build_candidate(
                        stock, momentum_result, "momentum", is_first_today, first_date, kline))
        else:
            momentum_result = analyze_momentum(stock, kline, weight_overrides=momentum_overrides)
            if momentum_result and momentum_result.score >= momentum_min:
                raw_momentum.append(_build_candidate(
                    stock, momentum_result, "momentum", is_first_today, first_date, kline))
            else:
                new_face_fallback = analyze_new_face(stock, kline, weight_overrides=new_face_overrides)
                if new_face_fallback and new_face_fallback.score >= new_face_min:
                    raw_new_faces.append(_build_candidate(
                        stock, new_face_fallback, "known_new_face", is_first_today, first_date, kline))

    all_raw = raw_new_faces + raw_momentum
    all_syms = list(set(c.stock.symbol for c in all_raw)
                    | set(sym for sym, c in session_state.today_pool.items() if c.is_stale))
    market_caps = fetch_market_caps_batch(session, all_syms) if all_syms else {}

    new_faces: list[Candidate] = []
    momentum: list[Candidate] = []
    filtered_large_cap = 0

    for c in all_raw:
        cap_data = market_caps.get(c.stock.symbol, {})
        market_cap = cap_data.get("market_cap", 0)
        if market_cap > 0 and market_cap > MAX_MARKET_CAP:
            filtered_large_cap += 1
            continue
        c.market_cap = market_cap
        c.circ_market_cap = cap_data.get("circ_market_cap", 0)
        if c.category in ("new_face", "known_new_face"):
            new_faces.append(c)
        else:
            momentum.append(c)

    clusters = get_sector_clusters(gem_stocks)

    all_candidates = new_faces + momentum

    def _compute_rps(candidates: list[Candidate]) -> dict[str, int]:
        scores: dict[str, int] = {}
        if len(candidates) < 2:
            return {c.stock.symbol: 0 for c in candidates}
        accum = [(c.stock.symbol, c.kline.accumulated_pct if c.kline else 0)
                 for c in candidates]
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

    rps_scores: dict[str, int] = {}
    rps_scores.update(_compute_rps(new_faces))
    rps_scores.update(_compute_rps(momentum))

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

    apply_all_bonuses(all_candidates, gem_stocks, intraday_scores,
                      opening_scores, live_volumes, market_caps,
                      clusters, market_idx_pct, time_bonus,
                      sentiment_info=sentiment_info, rps_scores=rps_scores,
                      list_streaks=session_state.list_presence)

    for i, c in enumerate(all_candidates):
        extra = accumulate_final_score(c, market_env_bonus, opening_scores)
        all_candidates[i] = dataclass_replace(c, score=c.score + extra)

    update_rank_history({s.symbol: s.rank for s in gem_stocks})

    session_state.update_pool(all_candidates)
    stale_candidates = session_state.get_stale_candidates()
    session_state.update_stale_quotes(stale_candidates, market_caps)

    new_faces.sort(key=lambda c: -c.score)
    momentum.sort(key=lambda c: -c.score)
    return new_faces, momentum, stale_candidates, gem_stocks, filtered_large_cap


def _parallel_fetch(pool, session, candidates, intraday_scores, opening_scores, live_volumes):
    intra_futs = {
        pool.submit(analyze_intraday, session, c.stock.symbol): c.stock.symbol
        for c in candidates
    }
    for fut in as_completed(intra_futs):
        sym = intra_futs[fut]
        try:
            intraday_scores[sym] = fut.result()
        except Exception as e:
            print(f"  [!] 分时强度失败 {sym}: {e}")
            intraday_scores[sym] = None

    open_futs = {
        pool.submit(analyze_opening_strength, session, c.stock.symbol): c.stock.symbol
        for c in candidates
    }
    for fut in as_completed(open_futs):
        sym = open_futs[fut]
        try:
            opening_scores[sym] = fut.result()
        except Exception as e:
            opening_scores[sym] = None

    vol_futs = {
        pool.submit(estimate_live_volume, session, c.stock.symbol): c.stock.symbol
        for c in candidates
    }
    for fut in as_completed(vol_futs):
        sym = vol_futs[fut]
        try:
            live_volumes[sym] = fut.result()
        except Exception as e:
            print(f"  [!] 实时量比失败 {sym}: {e}")
            live_volumes[sym] = None
