import sqlite3
from datetime import date, timedelta

from scanner.analysis import analyze_new_face, analyze_momentum
from scanner.models import StockInfo
from scanner.config import (
    NEW_FACE_MIN_SCORE, MOMENTUM_MIN_SCORE, MAX_STOCK_PRICE,
    SECTOR_CLUSTER_BONUS_5, SECTOR_CLUSTER_BONUS_4,
    SECTOR_CLUSTER_BONUS_3, SECTOR_CLUSTER_BONUS_2,
)
from scanner.sector import classify_sector
from scanner.trading_session import is_trading_day


TOP_N_FILTER = 40


def _n_trading_days_ago(n: int, from_date: date) -> str:
    cursor = from_date
    trading_days = 0
    while trading_days < n:
        cursor -= timedelta(days=1)
        if is_trading_day(cursor):
            trading_days += 1
    return cursor.isoformat()


def load_data(db_path="scanner.db"):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    app_rows = conn.execute(
        "SELECT symbol, name, date, rank, percent, value FROM appearances ORDER BY date, rank"
    ).fetchall()

    kline_rows = conn.execute(
        "SELECT symbol, date, close, high, low, open, percent, volume FROM daily_kline ORDER BY symbol, date"
    ).fetchall()

    conn.close()

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


def get_kline_up_to(kline_list, end_date):
    return [k for k in kline_list if k["date"] <= end_date]


def forward_return(kline_list, entry_date, entry_close, days):
    future = [k for k in kline_list if k["date"] > entry_date]
    target_idx = days - 1
    if target_idx < len(future):
        future_close = future[target_idx]["close"]
        return (future_close - entry_close) / entry_close
    return None


def run_backtest(db_path="scanner.db"):
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
                symbol=symbol,
                name=name,
                code=symbol[-6:] if len(symbol) >= 6 else symbol,
                percent=today_pct,
                current=entry_close,
                value=value,
                rank_change=rank_change,
                rank=stock_rank,
            )

            kline_summary = None
            cat = None

            if is_new:
                kline_summary = analyze_new_face(stock, kline, today_str=current_date_str)
                if kline_summary and kline_summary.score >= NEW_FACE_MIN_SCORE:
                    cat = "new_face"
                else:
                    kline_summary = analyze_momentum(stock, kline, today_str=current_date_str)
                    if kline_summary and kline_summary.score >= MOMENTUM_MIN_SCORE:
                        cat = "momentum"
            else:
                kline_summary = analyze_momentum(stock, kline, today_str=current_date_str)
                if kline_summary and kline_summary.score >= MOMENTUM_MIN_SCORE:
                    cat = "momentum"
                else:
                    kline_summary = analyze_new_face(stock, kline, today_str=current_date_str)
                    if kline_summary and kline_summary.score >= NEW_FACE_MIN_SCORE:
                        cat = "known_new_face"

            if kline_summary is None or cat is None:
                continue

            rec = BacktestRec(
                date=current_date_str, symbol=symbol, name=name,
                rank=stock_rank, percent=today_pct, value=value,
                category=cat, score=kline_summary.score, entry_close=entry_close,
            )
            rec.fwd_1d = forward_return(full_kline, current_date_str, entry_close, 1)
            rec.fwd_3d = forward_return(full_kline, current_date_str, entry_close, 3)
            rec.fwd_5d = forward_return(full_kline, current_date_str, entry_close, 5)
            recs.append(rec)

    # Apply sector bonus (same logic as production enhancer)
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

    return new_recs, momentum_recs


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
