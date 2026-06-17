from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta

from scanner.api import fetch_biaosheng, fetch_kline, fetch_market_caps_batch, analyze_intraday, estimate_live_volume, fetch_market_index
from scanner.analysis import analyze_new_face, analyze_momentum
from scanner.config import (
    NEW_FACE_LOOKBACK_DAYS,
    NEW_FACE_MIN_SCORE, MOMENTUM_MIN_SCORE,
    MAX_STOCK_PRICE, MAX_MARKET_CAP,
    EARLY_TRADE_CUTOFF, LATE_TRADE_START,
    EARLY_BONUS, LATE_BONUS,
    STALE_TIMEOUT_MINUTES,
    NEW_FACE_DIM_TO_WEIGHT_KEY, MOMENTUM_DIM_TO_WEIGHT_KEY,
)
from scanner.database import record_appearances, get_symbol_appearances, get_cached_kline, save_kline_to_db, get_active_weights
from scanner.models import StockInfo, Candidate
from scanner.sector import get_sector_clusters, classify_sector
from scanner.rank_trend import rank_streak_score, update_rank_history
from scanner.trading_session import is_trading_day
from scanner.utils import is_hk_stock, is_gem, is_st

_seen_today: set[str] = set()
_last_today: str = ""
_today_pool: dict[str, Candidate] = {}


def _fetch_all_klines(conn, session, stocks: list[StockInfo]) -> dict[str, list[dict] | None]:
    result: dict[str, list[dict] | None] = {}
    needs_fetch: list[str] = []
    stale_cache: dict[str, list[dict]] = {}

    for s in stocks:
        cached = get_cached_kline(conn, s.symbol)
        if cached:
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
        fut_map = {pool.submit(fetch_kline, session, sym): sym for sym in needs_fetch}
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


def scan(conn, session):
    global _seen_today, _last_today, _today_pool
    today = date.today().isoformat()
    if today != _last_today:
        _seen_today.clear()
        _today_pool.clear()
        _last_today = today
    new_face_min = NEW_FACE_MIN_SCORE
    momentum_min = MOMENTUM_MIN_SCORE
    active_weights_raw: dict = get_active_weights(conn)
    new_face_overrides = {NEW_FACE_DIM_TO_WEIGHT_KEY[k]: v for k, v in active_weights_raw.items() if k in NEW_FACE_DIM_TO_WEIGHT_KEY}
    momentum_overrides = {MOMENTUM_DIM_TO_WEIGHT_KEY[k]: v for k, v in active_weights_raw.items() if k in MOMENTUM_DIM_TO_WEIGHT_KEY}
    if active_weights_raw:
        print(f"  [进化] 加载活跃参数, 新面孔{len(new_face_overrides)} 动量{len(momentum_overrides)} 维度已覆盖")

    raw = fetch_biaosheng(session)

    gem_stocks: list[StockInfo] = []
    for i, item in enumerate(raw, 1):
        symbol = item.get("symbol", "")
        code = item.get("code", "")
        name = item.get("name", "")
        if is_hk_stock(symbol) or not is_gem(code) or is_st(name):
            continue
        gem_stocks.append(StockInfo(
            symbol=symbol,
            name=item.get("name", ""),
            code=code,
            percent=item.get("percent") or 0.0,
            current=item.get("current") or 0.0,
            value=item.get("value") or 0.0,
            rank_change=item.get("rank_change") or 0,
            rank=i,
        ))

    gem_top = gem_stocks

    record_appearances(conn, [
        {"symbol": s.symbol, "name": s.name, "percent": s.percent, "value": s.value}
        for s in gem_top
    ])

    klines = _fetch_all_klines(conn, session, gem_top)

    raw_new_faces: list[Candidate] = []
    raw_momentum: list[Candidate] = []

    for stock in gem_top:
        is_first_today = stock.symbol not in _seen_today
        _seen_today.add(stock.symbol)

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
                    first_breakout = stock.rank_change >= 500 and kline_summary.volume_ratio > 1.15
                    raw_new_faces.append(Candidate(
                        stock=stock, category="new_face", score=kline_summary.score,
                        reason=kline_summary.trend, kline=kline_summary,
                        first_seen=first_date,
                        first_today_bonus=5 if is_first_today else 0,
                        first_breakout_bonus=12 if first_breakout else 0,
                        history_pct=[k["percent"] for k in kline] if kline else [],
                    ))
            else:
                momentum_result = analyze_momentum(stock, kline, weight_overrides=momentum_overrides)
                if momentum_result and momentum_result.score >= momentum_min:
                    first_breakout = stock.rank_change >= 500 and momentum_result.volume_ratio > 1.15
                    raw_momentum.append(Candidate(
                        stock=stock, category="momentum", score=momentum_result.score,
                        reason=momentum_result.trend, kline=momentum_result,
                        first_seen=first_date,
                        first_today_bonus=5 if is_first_today else 0,
                        first_breakout_bonus=12 if first_breakout else 0,
                        history_pct=[k["percent"] for k in kline] if kline else [],
                    ))
        else:
            momentum_result = analyze_momentum(stock, kline, weight_overrides=momentum_overrides)
            if momentum_result and momentum_result.score >= momentum_min:
                first_breakout = stock.rank_change >= 500 and momentum_result.volume_ratio > 1.15
                raw_momentum.append(Candidate(
                    stock=stock, category="momentum", score=momentum_result.score,
                    reason=momentum_result.trend, kline=momentum_result,
                    first_seen=first_date,
                    first_today_bonus=5 if is_first_today else 0,
                    first_breakout_bonus=12 if first_breakout else 0,
                    history_pct=[k["percent"] for k in kline] if kline else [],
                ))
            else:
                new_face_fallback = analyze_new_face(stock, kline, weight_overrides=new_face_overrides)
                if new_face_fallback and new_face_fallback.score >= new_face_min:
                    first_breakout = stock.rank_change >= 500 and new_face_fallback.volume_ratio > 1.15
                    raw_new_faces.append(Candidate(
                        stock=stock, category="known_new_face", score=new_face_fallback.score,
                        reason=new_face_fallback.trend, kline=new_face_fallback,
                        first_seen=first_date,
                        first_today_bonus=5 if is_first_today else 0,
                        first_breakout_bonus=12 if first_breakout else 0,
                        history_pct=[k["percent"] for k in kline] if kline else [],
                    ))

    all_raw = raw_new_faces + raw_momentum
    stale_syms = [sym for sym, c in _today_pool.items() if c.is_stale]
    all_syms = list(set(c.stock.symbol for c in all_raw) | set(stale_syms))
    if all_syms:
        market_caps = fetch_market_caps_batch(session, all_syms)
    else:
        market_caps = {}

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

    clusters = get_sector_clusters(gem_top)

    all_candidates = new_faces + momentum
    intraday_scores: dict[str, float | None] = {}
    live_volumes: dict[str, float | None] = {}

    if all_candidates:
        with ThreadPoolExecutor(max_workers=5) as pool:
            intra_futs = {
                pool.submit(analyze_intraday, session, c.stock.symbol): c.stock.symbol
                for c in all_candidates
            }
            for fut in as_completed(intra_futs):
                sym = intra_futs[fut]
                try:
                    intraday_scores[sym] = fut.result()
                except Exception as e:
                    print(f"  [!] 分时强度失败 {sym}: {e}")
                    intraday_scores[sym] = None

            vol_futs = {
                pool.submit(estimate_live_volume, session, c.stock.symbol): c.stock.symbol
                for c in all_candidates
            }
            for fut in as_completed(vol_futs):
                sym = vol_futs[fut]
                try:
                    live_volumes[sym] = fut.result()
                except Exception as e:
                    print(f"  [!] 实时量比失败 {sym}: {e}")
                    live_volumes[sym] = None

    market_idx_pct = fetch_market_index(session)
    market_env_bonus = 0
    if market_idx_pct is not None:
        if market_idx_pct > 0.5:
            market_env_bonus = 3
        elif market_idx_pct < -1:
            market_env_bonus = -3

    now_minutes = datetime.now().hour * 60 + datetime.now().minute
    if now_minutes < EARLY_TRADE_CUTOFF:
        time_bonus = EARLY_BONUS
    elif now_minutes >= LATE_TRADE_START:
        time_bonus = LATE_BONUS
    else:
        time_bonus = 0

    for c in all_candidates:
        c.rank_trend_bonus = rank_streak_score(c.stock.symbol)
        sec = classify_sector(c.stock.name)
        c.sector = sec
        if sec != "其他":
            cluster_count = len(clusters.get(sec, []))
            if cluster_count >= 3:
                c.sector_bonus = 8
            elif cluster_count >= 2:
                c.sector_bonus = 4

        intra = intraday_scores.get(c.stock.symbol)
        if intra is not None:
            c.intraday_score = intra

        live_vol = live_volumes.get(c.stock.symbol)
        if live_vol is not None and c.kline and c.kline.avg_volume > 0:
            live_vol_ratio = live_vol / c.kline.avg_volume
            if live_vol_ratio > 1.3:
                c.live_vol_bonus = 5
        if c.market_cap > 0:
            tr = market_caps.get(c.stock.symbol, {}).get("turnover_rate")
            if tr is not None:
                if tr > 8:
                    c.turnover_bonus = -3
                elif tr > 4:
                    c.turnover_bonus = 3
                elif tr > 2:
                    c.turnover_bonus = 5

        c.time_bonus = time_bonus
        if c.kline and c.kline.dimensions:
            gap_key = "new_face_gap_up" if c.category in ("new_face", "known_new_face") else "momentum_gap_up"
            c.gap_up_bonus = c.kline.dimensions.get(gap_key, 0)

        intra_bonus = int(round(c.intraday_score)) if c.intraday_score else 0
        c.score += (c.rank_trend_bonus + c.sector_bonus + c.live_vol_bonus
                    + c.first_today_bonus + c.first_breakout_bonus
                    + market_env_bonus + c.turnover_bonus + c.time_bonus
                    + intra_bonus)

        if c.kline and c.kline.dimensions is not None:
            c.kline.dimensions["rank_trend_bonus"] = c.rank_trend_bonus
            c.kline.dimensions["sector_bonus"] = c.sector_bonus
            c.kline.dimensions["live_vol_bonus"] = c.live_vol_bonus
            c.kline.dimensions["intraday_score"] = round(c.intraday_score, 1)
            if c.first_today_bonus:
                c.kline.dimensions["first_today_bonus"] = c.first_today_bonus
            if c.first_breakout_bonus:
                c.kline.dimensions["first_breakout_bonus"] = c.first_breakout_bonus
            if market_env_bonus:
                c.kline.dimensions["market_env_bonus"] = market_env_bonus
            if c.turnover_bonus:
                c.kline.dimensions["turnover_bonus"] = c.turnover_bonus
            if c.time_bonus:
                c.kline.dimensions["time_bonus"] = c.time_bonus

    update_rank_history({s.symbol: s.rank for s in gem_stocks})

    # ── 持久候选池管理 ──
    current_symbols = {c.stock.symbol for c in all_candidates}
    now_dt = datetime.now()

    for c in all_candidates:
        if c.stock.symbol in _today_pool and not _today_pool[c.stock.symbol].is_stale:
            old = _today_pool[c.stock.symbol]
            c.first_seen = old.first_seen
        else:
            c.first_seen = now_dt.strftime("%H:%M")
        _today_pool[c.stock.symbol] = c

    stale_candidates: list[Candidate] = []
    for sym, c in list(_today_pool.items()):
        if sym not in current_symbols and not c.is_stale:
            c.is_stale = True
            c.stale_since = now_dt.strftime("%H:%M")
        if c.is_stale:
            cap_data = market_caps.get(sym)
            if cap_data and cap_data.get("current"):
                c.stock.current = cap_data["current"]
                c.stock.percent = cap_data["percent"]
            stale_candidates.append(c)

    stale_cutoff = now_dt - timedelta(minutes=STALE_TIMEOUT_MINUTES)
    stale_keep = []
    for c in stale_candidates:
        stale_dt = datetime.strptime(f"{today} {c.stale_since}", "%Y-%m-%d %H:%M")
        if stale_dt < stale_cutoff:
            _today_pool.pop(c.stock.symbol, None)
        else:
            stale_keep.append(c)
    stale_candidates = stale_keep
    stale_candidates.sort(key=lambda c: -c.score)

    new_faces.sort(key=lambda c: -c.score)
    momentum.sort(key=lambda c: -c.score)
    return new_faces, momentum, stale_candidates, gem_stocks, filtered_large_cap
