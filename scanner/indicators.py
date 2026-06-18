def compute_rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    gains, losses = 0.0, 0.0
    for i in range(len(closes) - period, len(closes)):
        diff = closes[i] - closes[i - 1]
        if diff > 0:
            gains += diff
        else:
            losses -= diff
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


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
