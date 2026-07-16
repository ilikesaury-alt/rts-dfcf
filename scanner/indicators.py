def compute_rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    gains, losses = 0.0, 0.0
    for i in range(1, period + 1):
        diff = closes[i] - closes[i - 1]
        if diff > 0:
            gains += diff
        else:
            losses -= diff
    avg_gain = gains / period
    avg_loss = losses / period
    for i in range(period + 1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gain = diff if diff > 0 else 0
        loss = -diff if diff < 0 else 0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def compute_ma(closes: list[float], period: int, ema: bool = False) -> float | None:
    """Simple or exponential moving average of the last `period` closes.

    EMA is preferred for high-volatility GEM stocks (less noise than SMA),
    and aligns with the EMA used inside MACD so the whole indicator stack
    uses one moving-average convention.
    """
    if len(closes) < period:
        return None
    window = closes[-period:]
    if not ema:
        return sum(window) / period
    m = 2 / (period + 1)
    result = window[0]
    for v in window[1:]:
        result = (v - result) * m + result
    return result


def compute_kdj(highs: list[float], lows: list[float],
                closes: list[float], n: int = 9,
                k_smooth: int = 3, d_smooth: int = 3) -> dict | None:
    if len(closes) < n:
        return None
    rsv_list = []
    for i in range(n - 1, len(closes)):
        hh = max(highs[i - n + 1:i + 1])
        ll = min(lows[i - n + 1:i + 1])
        if hh == ll:
            rsv_list.append(50.0)
        else:
            rsv = (closes[i] - ll) / (hh - ll) * 100
            rsv_list.append(rsv)
    k, d = 50.0, 50.0
    for rsv in rsv_list:
        k = (k_smooth - 1) / k_smooth * k + (1 / k_smooth) * rsv
        d = (d_smooth - 1) / d_smooth * d + (1 / d_smooth) * k
    j = 3 * k - 2 * d
    return {"K": round(k, 2), "D": round(d, 2), "J": round(j, 2)}


def compute_macd(closes: list[float], fast: int = 12,
                 slow: int = 26, signal: int = 9) -> dict | None:
    if len(closes) < slow + signal - 1:
        return None

    def ema(data: list[float], period: int) -> list[float]:
        result = [data[0]]
        m = 2 / (period + 1)
        for v in data[1:]:
            result.append((v - result[-1]) * m + result[-1])
        return result

    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)
    macd_line = [f - s for f, s in zip(ema_fast, ema_slow)]
    signal_line = ema(macd_line, signal)
    histogram = [m - s for m, s in zip(macd_line, signal_line)]

    return {
        "macd": round(macd_line[-1], 4),
        "signal": round(signal_line[-1], 4),
        "histogram": round(histogram[-1], 4),
        "histogram_prev": round(histogram[-2], 4) if len(histogram) >= 2 else 0,
    }


def compute_adx(highs: list[float], lows: list[float],
                closes: list[float], period: int = 14) -> dict | None:
    if len(closes) < period * 2:
        return None

    tr_list, plus_dm_list, minus_dm_list = [], [], []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i],
                 abs(highs[i] - closes[i - 1]),
                 abs(lows[i] - closes[i - 1]))
        tr_list.append(tr)
        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]
        plus_dm = up_move if up_move > down_move and up_move > 0 else 0
        minus_dm = down_move if down_move > up_move and down_move > 0 else 0
        plus_dm_list.append(plus_dm)
        minus_dm_list.append(minus_dm)

    def _smooth(data: list[float], p: int) -> list[float]:
        result = [sum(data[:p]) / p]
        for v in data[p:]:
            result.append((result[-1] * (p - 1) + v) / p)
        return result

    atr = _smooth(tr_list, period)
    plus_di = _smooth(plus_dm_list, period)
    minus_di = _smooth(minus_dm_list, period)

    di_sum = plus_di[-1] + minus_di[-1]
    dx = abs(plus_di[-1] - minus_di[-1]) / max(di_sum, 0.001) * 100
    adx_list = _smooth([
        abs(p - m) / max(p + m, 0.001) * 100
        for p, m in zip(plus_di, minus_di)
    ], period)

    return {
        "adx": round(adx_list[-1], 2),
        "plus_di": round(plus_di[-1] / max(atr[-1], 0.001) * 100, 2),
        "minus_di": round(minus_di[-1] / max(atr[-1], 0.001) * 100, 2),
    }


def compute_bollinger_bands(closes: list[float], period: int = 20,
                             std_mult: float = 2.0) -> dict | None:
    if len(closes) < period:
        return None
    window = closes[-period:]
    ma = sum(window) / period
    variance = sum((x - ma) ** 2 for x in window) / period
    std = variance ** 0.5
    upper = ma + std_mult * std
    lower = ma - std_mult * std
    current = closes[-1]
    bandwidth = (upper - lower) / max(ma, 0.001)
    if upper == lower:
        b_pct = 0.5
    else:
        b_pct = (current - lower) / (upper - lower)
    return {
        "upper": round(upper, 4),
        "middle": round(ma, 4),
        "lower": round(lower, 4),
        "bandwidth": round(bandwidth, 4),
        "b_pct": round(b_pct, 4),
    }


def compute_atr(highs: list[float], lows: list[float],
                closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    tr_list = []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i],
                 abs(highs[i] - closes[i - 1]),
                 abs(lows[i] - closes[i - 1]))
        tr_list.append(tr)
    atr = sum(tr_list[:period]) / period
    for v in tr_list[period:]:
        atr = (atr * (period - 1) + v) / period
    return round(atr, 4)


def compute_obv(closes: list[float], volumes: list[float]) -> dict | None:
    if len(closes) < 2 or len(volumes) < 2:
        return None
    n = min(len(closes), len(volumes))
    obv = 0
    obv_history = [0]
    for i in range(1, n):
        if closes[i] > closes[i - 1]:
            obv += volumes[i]
        elif closes[i] < closes[i - 1]:
            obv -= volumes[i]
        obv_history.append(obv)
    recent_5 = obv_history[-5:]
    uptrend = all(recent_5[i] <= recent_5[i + 1] for i in range(len(recent_5) - 1))
    downtrend = all(recent_5[i] >= recent_5[i + 1] for i in range(len(recent_5) - 1))
    trend = 1 if uptrend else (-1 if downtrend else 0)
    return {"obv": obv, "obv_trend": trend}
