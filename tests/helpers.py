def _kline(pcts, volumes=None, body_ratio=None):
    N = len(pcts)
    volumes = volumes or [1.0] * N
    closes = [100.0]
    for p in pcts:
        closes.append(closes[-1] * (1 + p / 100))

    result = []
    for i in range(N):
        o = closes[i]
        c = closes[i + 1]
        if body_ratio is not None and o != c:
            body = c - o
            total_range = abs(body) / body_ratio
            wiggle = (total_range - abs(body)) / 2
            h = max(o, c) + wiggle
            lo = min(o, c) - wiggle
        else:
            h = max(o, c) * 1.02 if max(o, c) > 0 else o + 1
            lo = min(o, c) * 0.98 if min(o, c) > 0 else o - 1
        result.append({
            "date": f"2026-01-{i+1:02d}",
            "open": o, "close": c, "high": h, "low": lo,
            "volume": volumes[i], "percent": pcts[i],
        })
    return result
