from datetime import date, datetime

from scanner.api import fetch_biaosheng, fetch_market_caps_batch, analyze_intraday, estimate_live_volume
from scanner.analysis import analyze_new_face, analyze_momentum
from scanner.config import (
    NEW_FACE_LOOKBACK_DAYS,
    NEW_FACE_MIN_SCORE, MOMENTUM_MIN_SCORE,
    ULTRA_NEW_FACE_MIN_SCORE, ULTRA_MOMENTUM_MIN_SCORE,
    ULTRA_MIN_INTRADAY_SCORE, MIN_INTRADAY_SCORE,
    MAX_STOCK_PRICE, MAX_MARKET_CAP,
)
from scanner.database import record_appearances, get_symbol_appearances, ensure_kline
from scanner.models import StockInfo, Candidate
from scanner.sector import get_sector_clusters, classify_sector
from scanner.rank_trend import rank_streak_score, update_rank_history
from scanner.utils import is_hk_stock, is_gem, is_st


def scan(conn, session, ultra=False):
    today = date.today().isoformat()

    if ultra:
        new_face_min = ULTRA_NEW_FACE_MIN_SCORE
        momentum_min = ULTRA_MOMENTUM_MIN_SCORE
        intraday_min = ULTRA_MIN_INTRADAY_SCORE
    else:
        new_face_min = NEW_FACE_MIN_SCORE
        momentum_min = MOMENTUM_MIN_SCORE
        intraday_min = MIN_INTRADAY_SCORE

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

    raw_new_faces: list[Candidate] = []
    raw_momentum: list[Candidate] = []

    for stock in gem_top:
        if stock.current > 0 and stock.current > MAX_STOCK_PRICE:
            continue

        app_history = get_symbol_appearances(conn, stock.symbol, NEW_FACE_LOOKBACK_DAYS)
        previous_dates = [a["date"] for a in app_history]
        is_new = len(previous_dates) == 0
        first_date = previous_dates[0] if previous_dates else today

        kline = ensure_kline(conn, session, stock.symbol)

        if is_new:
            kline_summary = analyze_new_face(stock, kline, ultra=ultra)
            if kline_summary and kline_summary.score >= new_face_min:
                raw_new_faces.append(Candidate(
                    stock=stock, category="new_face", score=kline_summary.score,
                    reason=kline_summary.trend, kline=kline_summary,
                    first_seen=first_date,
                    history_pct=[k["percent"] for k in kline] if kline else [],
                ))
            else:
                momentum_result = analyze_momentum(stock, kline, ultra=ultra)
                if momentum_result and momentum_result.score >= momentum_min:
                    raw_momentum.append(Candidate(
                        stock=stock, category="momentum", score=momentum_result.score,
                        reason=momentum_result.trend, kline=momentum_result,
                        first_seen=first_date,
                        history_pct=[k["percent"] for k in kline] if kline else [],
                    ))
        else:
            momentum_result = analyze_momentum(stock, kline, ultra=ultra)
            if momentum_result and momentum_result.score >= momentum_min:
                raw_momentum.append(Candidate(
                    stock=stock, category="momentum", score=momentum_result.score,
                    reason=momentum_result.trend, kline=momentum_result,
                    first_seen=first_date,
                    history_pct=[k["percent"] for k in kline] if kline else [],
                ))

    all_raw = raw_new_faces + raw_momentum
    if all_raw:
        cand_symbols = list(set(c.stock.symbol for c in all_raw))
        market_caps = fetch_market_caps_batch(session, cand_symbols)
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

        if c.category == "new_face":
            new_faces.append(c)
        else:
            momentum.append(c)

    clusters = get_sector_clusters(gem_top)

    for c in new_faces + momentum:
        c.rank_trend_bonus = rank_streak_score(c.stock.symbol)
        sec = classify_sector(c.stock.name)
        c.sector = sec
        if sec != "其他":
            cluster_count = len(clusters.get(sec, []))
            if cluster_count >= 3:
                c.sector_bonus = 8
            elif cluster_count >= 2:
                c.sector_bonus = 4
        intra = analyze_intraday(session, c.stock.symbol)
        if intra is not None:
            c.intraday_score = intra
            factor = 1 + intra / 20
            c.score = max(1, int(c.score * factor))

        live_vol = estimate_live_volume(session, c.stock.symbol)
        if live_vol is not None and c.kline and c.kline.avg_volume > 0:
            live_vol_ratio = live_vol / c.kline.avg_volume
            if live_vol_ratio > 1.3:
                c.live_vol_bonus = 5
        c.score += c.rank_trend_bonus + c.sector_bonus + c.live_vol_bonus

    new_faces = [c for c in new_faces if c.intraday_score >= intraday_min]
    momentum = [c for c in momentum if c.intraday_score >= intraday_min]

    update_rank_history({s.symbol: s.rank for s in gem_stocks})

    new_faces.sort(key=lambda c: -c.score)
    momentum.sort(key=lambda c: -c.score)
    return new_faces, momentum, gem_stocks, filtered_large_cap
