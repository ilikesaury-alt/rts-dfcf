import copy
import sqlite3
from datetime import date, timedelta

from .scoring import score_new_face, score_momentum, classify_sector
from scanner.api import fetch_kline
from scanner.config import MAX_MARKET_CAP, MAX_STOCK_PRICE


DEFAULT_PARAMS = {
    "new_face": {
        "min_score": 20,
        "today_pct": {
            "golden_min": 2.0, "golden_max": 6.0, "golden_score": 20,
            "low_score": 5,
            "high_min": 6.0, "high_max": 8.0, "high_score": 5,
            "overheat_score": -15,
        },
        "accumulated": {
            "sweet_min": -5.0, "sweet_max": 15.0, "sweet_score": 15,
            "warn_threshold": 15.0, "warn_penalty": -10,
            "danger_threshold": 25.0, "danger_penalty": -10,
        },
        "bottom": {
            "max_daily_loss": -3.0,
            "min_vol_ratio": 1.3,
            "near_low_pct": 0.05,
            "confirmed_score": 15,
            "volume_surge_score": 10,
        },
        "rank_change": {
            "strong_threshold": 2000, "strong_score": 12,
            "medium_threshold": 1000, "medium_score": 6,
        },
        "value": {
            "high_threshold": 10000, "high_score": 5,
            "medium_threshold": 5000, "medium_score": 2,
        },
        "combo": {
            "max_today_pct": 5.0, "max_accumulated": 8.0, "score": 8,
        },
    },
    "momentum": {
        "min_score": 15,
        "today_pct": {
            "golden_min": 2.0, "golden_max": 6.0, "golden_score": 26,
            "low_score": 5,
            "overheat_threshold": 8.0, "overheat_score": 0,
        },
        "accumulated": {
            "sweet_min": 10, "sweet_score": 19,
            "mid_threshold": 15, "mid_score": 10,
            "high_threshold": 20, "high_score": 5,
            "danger_threshold": 30, "danger_score": -15,
        },
        "volume": {
            "healthy_min": 0.7, "healthy_max": 2.0, "healthy_score": 5,
            "surge_min": 2.0, "surge_score": -4,
            "low_max": 0.7, "low_score": -5,
        },
        "no_crash": {
            "crash_threshold": -7, "recent_2_return": -3, "score": 13,
        },
        "rank_change": {
            "strong_threshold": 2000, "strong_score": 8,
            "medium_threshold": 1000, "medium_score": 4,
        },
        "value": {
            "high_threshold": 10000, "high_score": 5,
            "medium_threshold": 5000, "medium_score": 2,
        },
    },
    "top_n": 40,
    "lookback_days": 3,
    "max_market_cap": 500 * 100_000_000,
    "max_stock_price": 100.0,
    "sector_bonus": {3: 8, 2: 4},
    "rank_proxy": {5: 2000, 15: 1000},
}


def load_data(db_path="scanner.db"):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    app_rows = conn.execute(
        "SELECT symbol, name, date, rank, percent, value FROM appearances ORDER BY date, rank"
    ).fetchall()

    kline_rows = conn.execute(
        "SELECT symbol, date, close, percent, volume FROM daily_kline ORDER BY symbol, date"
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
        kline_by_symbol[sym].append(dict(r))

    return appearances_by_date, kline_by_symbol


def get_kline_up_to(kline_list, end_date):
    return [k for k in kline_list if k["date"] <= end_date]


def ensure_kline_full(symbol, kline_by_symbol, session=None, live=False):
    kline = kline_by_symbol.get(symbol, [])
    if not kline or live:
        if session and live:
            fresh = fetch_kline(session, symbol)
            if fresh:
                kline = fresh
    return kline


def forward_return(kline_list, entry_date, entry_close, days):
    future = [k for k in kline_list if k["date"] > entry_date]
    target_idx = days - 1
    if target_idx < len(future):
        future_close = future[target_idx]["close"]
        return (future_close - entry_close) / entry_close
    return None


def _deep_merge(base, override):
    for k, v in override.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


def run_backtest(params=None, db_path="scanner.db", session=None, live=False):
    if params is None:
        params = copy.deepcopy(DEFAULT_PARAMS)

    appearances_by_date, kline_by_symbol = load_data(db_path)
    sorted_dates = sorted(appearances_by_date.keys())

    recs = []
    BacktestRec = _make_rec_class()

    for di, current_date in enumerate(sorted_dates):
        today_apps = appearances_by_date[current_date]

        for app in today_apps:
            stock_rank = app["rank"]
            if stock_rank > params["top_n"]:
                continue

            symbol = app["symbol"]
            name = app["name"]
            today_pct = app["percent"]
            value = app.get("value") or 0

            if symbol not in kline_by_symbol:
                continue

            full_kline = kline_by_symbol[symbol]
            kline = get_kline_up_to(full_kline, current_date)

            if len(kline) < 5:
                continue

            entry_close = kline[-1]["close"]

            if entry_close > params["max_stock_price"]:
                continue

            prev_apps = [
                a for a_date, apps in appearances_by_date.items()
                if a_date < current_date
                for a in apps if a["symbol"] == symbol
            ]
            lookback = date.fromisoformat(current_date) - timedelta(days=params["lookback_days"])
            prev_in_window = [a for a in prev_apps if date.fromisoformat(a["date"]) >= lookback]
            is_new = len(prev_in_window) == 0

            if not is_new:
                strong_prev = any(
                    a["percent"] >= 5 for a in prev_apps
                    if date.fromisoformat(a["date"]) >= lookback
                )
                if not strong_prev:
                    continue

            pcts = [k["percent"] for k in kline]
            volumes = [k["volume"] for k in kline]
            closes = [k["close"] for k in kline]

            recent_5 = pcts[-6:-1] if len(pcts) >= 6 else pcts[:-1]
            if not recent_5:
                recent_5 = [0]
            accumulated = sum(recent_5)

            vol_window = volumes[-11:-1] if len(volumes) >= 11 else volumes[:-1]
            avg_vol = sum(vol_window) / max(len(vol_window), 1)
            today_vol = volumes[-1] if volumes else 0
            vol_ratio = today_vol / avg_vol if avg_vol > 0 else 1.0

            if is_new:
                if today_pct <= 0:
                    continue
                recent_3_pcts = pcts[-3:] if len(pcts) >= 3 else pcts
                score_val = score_new_face(today_pct, accumulated, vol_ratio,
                                           recent_3_pcts, stock_rank, value, params)
                if score_val >= params["new_face"]["min_score"]:
                    rec = BacktestRec(date=current_date, symbol=symbol, name=name,
                                      rank=stock_rank, percent=today_pct, value=value,
                                      category="new_face", score=score_val, entry_close=entry_close)
                elif accumulated >= 10:
                    m_score = score_momentum(today_pct, accumulated, vol_ratio,
                                             recent_5, stock_rank, value, params)
                    if m_score >= params["momentum"]["min_score"]:
                        rec = BacktestRec(date=current_date, symbol=symbol, name=name,
                                          rank=stock_rank, percent=today_pct, value=value,
                                          category="momentum", score=m_score, entry_close=entry_close)
                    else:
                        continue
                else:
                    continue
            else:
                if accumulated < 10:
                    continue
                m_score = score_momentum(today_pct, accumulated, vol_ratio,
                                         recent_5, stock_rank, value, params)
                if m_score >= params["momentum"]["min_score"]:
                    rec = BacktestRec(date=current_date, symbol=symbol, name=name,
                                      rank=stock_rank, percent=today_pct, value=value,
                                      category="momentum", score=m_score, entry_close=entry_close)
                else:
                    new_score = score_new_face(today_pct, accumulated, vol_ratio,
                                               pcts[-3:], stock_rank, value, params)
                    if new_score >= params["new_face"]["min_score"]:
                        rec = BacktestRec(date=current_date, symbol=symbol, name=name,
                                          rank=stock_rank, percent=today_pct, value=value,
                                          category="known_new_face", score=new_score,
                                          entry_close=entry_close)
                    else:
                        continue

            fwd_kline = ensure_kline_full(symbol, kline_by_symbol, session, live)
            rec.fwd_1d = forward_return(fwd_kline, current_date, entry_close, 1)
            rec.fwd_3d = forward_return(fwd_kline, current_date, entry_close, 3)
            rec.fwd_5d = forward_return(fwd_kline, current_date, entry_close, 5)
            recs.append(rec)

    for rec in recs:
        sector = classify_sector(rec.name)
        if sector != "其他":
            same_sector = sum(1 for r in recs if r.date == rec.date
                              and classify_sector(r.name) == sector)
            sb_map = params["sector_bonus"]
            for threshold in sorted(sb_map.keys(), reverse=True):
                if same_sector >= threshold:
                    rec.score += sb_map[threshold]
                    break

    new_recs = [r for r in recs if r.category in ("new_face", "known_new_face")]
    momentum_recs = [r for r in recs if r.category == "momentum"]

    return new_recs, momentum_recs


def _make_rec_class():
    from dataclasses import dataclass, field

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
