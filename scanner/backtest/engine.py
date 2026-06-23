import sqlite3
import logging
from datetime import date, timedelta

from scanner.models import StockInfo
from scanner.config import (
    NEW_FACE_WEIGHTS, MOMENTUM_WEIGHTS, PULLBACK_WEIGHTS,
    MAX_STOCK_PRICE,
    SECTOR_CLUSTER_BONUS_5, SECTOR_CLUSTER_BONUS_4,
    SECTOR_CLUSTER_BONUS_3, SECTOR_CLUSTER_BONUS_2,
)
from scanner.analysis import analyze_new_face, analyze_momentum, analyze_pullback
from scanner.sector import classify_sector
from scanner.trading_session import is_trading_day

logger = logging.getLogger(__name__)


TOP_N_FILTER = 40

DEFAULT_NEW_FACE_WEIGHTS = dict(NEW_FACE_WEIGHTS)
DEFAULT_MOMENTUM_WEIGHTS = dict(MOMENTUM_WEIGHTS)
DEFAULT_PULLBACK_WEIGHTS = dict(PULLBACK_WEIGHTS)
DEFAULT_NEW_FACE_MIN = 18
DEFAULT_MOMENTUM_MIN = 15
DEFAULT_PULLBACK_MIN = 18


def _n_trading_days_ago(n: int, from_date: date) -> str:
    cursor = from_date
    trading_days = 0
    while trading_days < n:
        cursor -= timedelta(days=1)
        if is_trading_day(cursor):
            trading_days += 1
    return cursor.isoformat()


def load_data(db_path="scanner.db"):
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        app_rows = conn.execute(
            "SELECT symbol, name, date, rank, percent, value FROM appearances ORDER BY date, rank"
        ).fetchall()

        kline_rows = conn.execute(
            "SELECT symbol, date, close, high, low, open, percent, volume FROM daily_kline ORDER BY symbol, date"
        ).fetchall()

        appearances_by_date: dict[str, list[dict]] = {}
        for r in app_rows:
            d = r["date"]
            if d not in appearances_by_date:
                appearances_by_date[d] = []
            appearances_by_date[d].append(dict(r))

        kline_by_symbol: dict[str, list[dict]] = {}
        for r in kline_rows:
            sym = r["symbol"]
            if sym not in kline_by_symbol:
                kline_by_symbol[sym] = []
            kline_by_symbol[sym].append({
                "date": r["date"],
                "close": r["close"],
                "high": r["high"],
                "low": r["low"],
                "open": r["open"],
                "volume": r["volume"],
                "percent": r["percent"],
            })

        return appearances_by_date, kline_by_symbol
    except sqlite3.Error as e:
        logger.error(f"Database error in load_data: {e}")
        raise
    finally:
        if conn:
            conn.close()


def get_kline_up_to(kline_list, end_date):
    return [k for k in kline_list if k["date"] <= end_date]


def _next_trading_open(kline_list, entry_date, entry_close):
    """Get next trading day's open price for entry.

    Falls back to entry_close if no future kline available,
    maintaining backward compatibility.
    """
    future = [k for k in kline_list if k["date"] > entry_date]
    if future:
        return future[0]["open"]
    return entry_close


def forward_return(kline_list, entry_date, entry_price, days):
    future = [k for k in kline_list if k["date"] > entry_date]
    target_idx = days - 1
    if target_idx < len(future):
        future_close = future[target_idx]["close"]
        return (future_close - entry_price) / entry_price
    return None


def run_backtest(new_face_overrides=None, momentum_overrides=None,
                 pullback_overrides=None,
                 new_face_min=None, momentum_min=None, pullback_min=None,
                 db_path="scanner.db", session=None, live=False):
    nf_w = {**NEW_FACE_WEIGHTS, **(new_face_overrides or {})}
    mo_w = {**MOMENTUM_WEIGHTS, **(momentum_overrides or {})}
    pb_w = {**PULLBACK_WEIGHTS, **(pullback_overrides or {})}
    nf_min = new_face_min if new_face_min is not None else DEFAULT_NEW_FACE_MIN
    mo_min = momentum_min if momentum_min is not None else DEFAULT_MOMENTUM_MIN
    pb_min = pullback_min if pullback_min is not None else DEFAULT_PULLBACK_MIN

    appearances_by_date, kline_by_symbol = load_data(db_path)
    sorted_dates = sorted(appearances_by_date.keys())

    recs = []
    BacktestRec = _make_rec_class()

    for current_date_str in sorted_dates:
        current_date = date.fromisoformat(current_date_str)
        today_apps = appearances_by_date[current_date_str]

        for app in today_apps:
            stock_rank = app["rank"]
            if stock_rank > TOP_N_FILTER:
                continue

            symbol = app["symbol"]
            name = app["name"]
            today_pct = app["percent"]
            value = app.get("value") or 0

            if symbol not in kline_by_symbol:
                continue

            full_kline = kline_by_symbol[symbol]
            kline = get_kline_up_to(full_kline, current_date_str)

            if len(kline) < 5:
                continue

            entry_close = kline[-1]["close"]

            if entry_close > MAX_STOCK_PRICE:
                continue

            prev_apps = [
                a for a_date, apps in appearances_by_date.items()
                if a_date < current_date_str
                for a in apps if a["symbol"] == symbol
            ]
            lookback_str = _n_trading_days_ago(3, current_date)
            prev_in_window = [
                a for a in prev_apps
                if a["date"] >= lookback_str
            ]
            is_new = len(prev_in_window) == 0

            prev_rank = prev_apps[-1]["rank"] if prev_apps else 0
            rank_change = prev_rank - stock_rank

            stock = StockInfo(
                symbol=symbol, name=name, code=symbol,
                percent=today_pct, current=entry_close,
                value=value, rank_change=rank_change, rank=stock_rank,
            )

            score = 0
            cat = None

            if is_new:
                result = analyze_new_face(stock, kline, nf_w, current_date_str)
                if result and result.score >= nf_min:
                    score = result.score
                    cat = "new_face"
                else:
                    result = analyze_momentum(stock, kline, mo_w, current_date_str)
                    if result and result.score >= mo_min:
                        score = result.score
                        cat = "momentum"
            else:
                result = analyze_momentum(stock, kline, mo_w, current_date_str)
                if result and result.score >= mo_min:
                    score = result.score
                    cat = "momentum"
                else:
                    result = analyze_pullback(stock, kline, pb_w, current_date_str)
                    if result and result.score >= pb_min:
                        score = result.score
                        cat = "pullback"
                    else:
                        result = analyze_new_face(stock, kline, nf_w, current_date_str)
                        if result and result.score >= nf_min:
                            score = result.score
                            cat = "known_new_face"

            if cat is None:
                continue

            # Use next trading day's open as entry price (consistent with tracker)
            entry_price = _next_trading_open(full_kline, current_date_str, entry_close)

            rec = BacktestRec(
                date=current_date_str, symbol=symbol, name=name,
                rank=stock_rank, percent=today_pct, value=value,
                category=cat, score=score, entry_close=entry_price,
            )
            rec.fwd_1d = forward_return(full_kline, current_date_str, entry_price, 1)
            rec.fwd_3d = forward_return(full_kline, current_date_str, entry_price, 3)
            rec.fwd_5d = forward_return(full_kline, current_date_str, entry_price, 5)
            recs.append(rec)

    for rec in recs:
        sector = classify_sector(rec.name)
        if sector != "其他":
            same_sector = sum(
                1 for r in recs if r.date == rec.date and classify_sector(r.name) == sector
            )
            if same_sector >= 5:
                rec.score += SECTOR_CLUSTER_BONUS_5
            elif same_sector >= 4:
                rec.score += SECTOR_CLUSTER_BONUS_4
            elif same_sector >= 3:
                rec.score += SECTOR_CLUSTER_BONUS_3
            elif same_sector >= 2:
                rec.score += SECTOR_CLUSTER_BONUS_2

    new_recs = [r for r in recs if r.category in ("new_face", "known_new_face")]
    momentum_recs = [r for r in recs if r.category == "momentum"]
    pullback_recs = [r for r in recs if r.category == "pullback"]

    return new_recs, momentum_recs, pullback_recs


def _make_rec_class():
    from dataclasses import dataclass

    @dataclass
    class BacktestRec:
        date: str
        symbol: str
        name: str
        rank: int
        percent: float
        value: float
        category: str
        score: int
        entry_close: float
        fwd_1d: float | None = None
        fwd_3d: float | None = None
        fwd_5d: float | None = None

    return BacktestRec
