import sqlite3
from datetime import date

from scanner.api import fetch_kline
from scanner.database import DB_PATH, save_kline_to_db

KLINE_DAYS = 60
MIN_KLINE_LEN = 30

WATCH_MIN_SCORE = 25


def _ma(data: list[float], period: int) -> float | None:
    if len(data) < period:
        return None
    return sum(data[-period:]) / period


def _avg_volume(kline: list[dict], period: int) -> float | None:
    vols = [k["volume"] for k in kline[-period:]]
    if len(vols) < period:
        return None
    return sum(vols) / period


def score_stock(symbol: str, kline: list[dict], is_bottleneck: bool,
                today_pct: float | None = None) -> dict:
    closes = [k["close"] for k in kline]

    if len(closes) < MIN_KLINE_LEN:
        return {"score": 0, "signals": ["数据不足"]}

    score = 10
    signals = ["异动入池+10"]

    ma20 = _ma(closes, 20)
    ma60 = _ma(closes, 60)

    if ma20 is not None and ma60 is not None:
        if ma20 > ma60:
            diff_pct = (ma20 - ma60) / ma60 * 100
            if diff_pct > 5:
                score += 15
                signals.append("多头排列+15")
            elif diff_pct > 0:
                score += 8
                signals.append("短多排列+8")
        else:
            diff_pct = (ma60 - ma20) / ma60 * 100
            score -= 5
            signals.append(f"短空-5")

    recent_high_20 = max(closes[-20:]) if len(closes) >= 20 else max(closes)
    current = closes[-1]
    pullback_pct = (recent_high_20 - current) / recent_high_20 * 100

    if 3 <= pullback_pct <= 10:
        score += 8
        signals.append(f"健康回踩{pullback_pct:.0f}%+8")
    elif pullback_pct < 3:
        score += 5
        signals.append(f"强势{pullback_pct:.0f}%+5")
    elif pullback_pct > 15:
        score -= 5
        signals.append(f"回撤过大{pullback_pct:.0f}%-5")

    vol_60 = _avg_volume(kline, 60)
    vol_120 = _avg_volume(kline, 120)
    if vol_60 is not None and vol_120 is not None and vol_120 > 0:
        if vol_60 > vol_120:
            score += 10
            signals.append("量增+10")

    if is_bottleneck:
        score += 8
        signals.append("瓶颈环节+8")

    if today_pct is not None:
        if today_pct > 15:
            score -= 5
            signals.append(f"当日大涨{today_pct:.0f}%-5")
        elif today_pct > 12:
            score -= 3
            signals.append(f"当日偏热{today_pct:.0f}%-3")
        elif today_pct > 5:
            score += 3
            signals.append(f"温和放量{today_pct:.0f}%+3")

    if len(closes) >= 20:
        pct_20d = (closes[-1] - closes[-20]) / closes[-20] * 100
        if pct_20d > 40:
            score -= 5
            signals.append(f"20日涨{pct_20d:.0f}%过热-5")
        elif pct_20d < -15:
            score += 5
            signals.append(f"超跌{pct_20d:.0f}%反弹+5")

    if len(closes) >= 5:
        pct_5d = (closes[-1] - closes[-5]) / closes[-5] * 100
        if 5 <= pct_5d <= 25:
            score += 5
            signals.append(f"5日合理涨幅+5")

    score = max(0, min(100, score))
    return {"score": score, "signals": signals}


def fetch_kline_for_symbol(symbol: str,
                           conn: sqlite3.Connection | None = None,
                           session=None) -> list[dict] | None:
    close_conn = False
    close_session = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        close_conn = True

    try:
        cutoff = date.today().isoformat()
        rows = conn.execute(
            "SELECT date, close, volume FROM daily_kline WHERE symbol = ? AND date <= ? ORDER BY date DESC LIMIT ?",
            (symbol, cutoff, KLINE_DAYS)
        ).fetchall()

        if len(rows) >= MIN_KLINE_LEN:
            result = []
            for r in reversed(rows):
                result.append({
                    "date": r[0],
                    "close": r[1],
                    "volume": r[2] or 0,
                })
            return result

        if session is not None and len(rows) < MIN_KLINE_LEN:
            if session is None:
                from scanner.api import make_session
                session = make_session()
                close_session = True
            kline = fetch_kline(session, symbol, days=KLINE_DAYS)
            if kline and len(kline) >= MIN_KLINE_LEN:
                save_kline_to_db(conn, symbol, kline)
                return kline

        return None
    finally:
        if close_conn:
            conn.close()
        if close_session and session:
            session.close()
