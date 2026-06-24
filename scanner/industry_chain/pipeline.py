import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests

from scanner.api import (
    fetch_biaosheng,
    fetch_kline,
    make_session,
)
from scanner.config import KLINE_FETCH_DAYS, KLINE_MIN_LENGTH, MAX_STOCK_PRICE
from scanner.database import DB_PATH, save_kline_to_db
from scanner.industry_chain.chains import match_chains
from scanner.industry_chain.chokepoint_scorer import score_chokepoint_stocks
from scanner.industry_chain.models import ChokepointCandidate, ChainTrend, IndustryScanSession
from scanner.industry_chain.trend_judge import judge_chain_trends
from scanner.utils import is_gem, is_hk_stock, is_st

_thread_local = threading.local()


def _get_session() -> requests.Session:
    if not hasattr(_thread_local, "session"):
        _thread_local.session = make_session()
    return _thread_local.session


def _filter_gem_stocks(raw: list[dict]) -> list:
    gem_stocks = []
    for i, item in enumerate(raw, 1):
        symbol = item.get("symbol", "")
        code = item.get("code", "")
        name = item.get("name", "")
        if is_hk_stock(symbol) or not is_gem(code) or is_st(name):
            continue
        current = item.get("current") or 0
        if current > 0 and current > MAX_STOCK_PRICE:
            continue
        gem_stocks.append({
            "symbol": symbol,
            "name": name,
            "code": code,
            "percent": item.get("percent") or 0.0,
            "current": current,
            "value": item.get("value") or 0.0,
            "rank_change": item.get("rank_change") or 0,
            "rank": i,
        })
    return gem_stocks


def _fetch_klines(session: requests.Session, gem_stocks: list) -> dict[str, list[dict] | None]:
    conn = sqlite3.connect(DB_PATH)
    result: dict[str, list[dict] | None] = {}
    needs_fetch: list[str] = []

    for s in gem_stocks:
        cached = _get_cached_kline(conn, s["symbol"])
        if cached and len(cached) >= KLINE_MIN_LENGTH:
            result[s["symbol"]] = cached
        else:
            needs_fetch.append(s["symbol"])

    if needs_fetch:
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
                except Exception:
                    pass

    conn.close()
    return result


def _get_cached_kline(conn: sqlite3.Connection, symbol: str) -> list[dict] | None:
    from datetime import date, timedelta
    lookback = (date.today() - timedelta(days=60)).isoformat()
    cur = conn.execute(
        "SELECT date, open, close, high, low, volume, percent FROM daily_kline "
        "WHERE symbol = ? AND date >= ? ORDER BY date",
        (symbol, lookback),
    )
    rows = cur.fetchall()
    if rows:
        return [
            {"date": r[0], "open": r[1], "close": r[2], "high": r[3],
             "low": r[4], "volume": r[5], "percent": r[6]}
            for r in rows
        ]
    return None


def scan(
    conn: sqlite3.Connection,
    session: requests.Session,
    session_state: IndustryScanSession,
    scan_id: str | None = None,
) -> tuple[list[ChokepointCandidate], dict[str, ChainTrend]]:
    scan_id = scan_id or datetime.now().strftime("%Y%m%d%H%M%S")

    raw = fetch_biaosheng(session)
    if not raw:
        return [], {}

    gem_stocks = _filter_gem_stocks(raw)
    if not gem_stocks:
        return [], {}

    chain_trend_results = judge_chain_trends(raw, conn, session_state, scan_id)
    if not chain_trend_results:
        return [], chain_trend_results

    klines = _fetch_klines(session, gem_stocks)

    candidates = score_chokepoint_stocks(chain_trend_results, gem_stocks, klines)

    _save_recommendations(conn, candidates, scan_id)

    return candidates, chain_trend_results


def _save_recommendations(conn: sqlite3.Connection, candidates: list[ChokepointCandidate], scan_id: str):
    from datetime import date, datetime
    today = date.today().isoformat()
    now = datetime.now().strftime("%H:%M:%S")

    for c in candidates:
        try:
            conn.execute(
                "INSERT INTO chokepoint_recommendations "
                "(date, time, symbol, name, chain_name, node_name, is_bottleneck, "
                "chain_phase, score, percent, current, rank, rank_change, signals) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    today, now, c.symbol, c.name, c.chain_name, c.node_name,
                    1 if c.is_bottleneck else 0, c.chain_phase, c.score,
                    c.percent, c.current, c.rank, c.rank_change,
                    "|".join(c.signals),
                ),
            )
        except Exception as e:
            print(f"  [!] 保存链推荐失败 {c.symbol}: {e}")
    conn.commit()
