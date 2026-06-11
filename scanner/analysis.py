from scanner.models import StockInfo, KlineSummary


def _ma_bull_score(closes: list[float]) -> int:
    if len(closes) < 10:
        return 0
    ma5 = sum(closes[-5:]) / 5
    ma10 = sum(closes[-10:]) / 10
    if len(closes) >= 20:
        ma20 = sum(closes[-20:]) / 20
        if ma5 > ma10 > ma20:
            return 6
    if ma5 > ma10:
        return 3
    return -3


def _candle_quality_score(kline: list[dict]) -> int:
    score = 0
    recent = kline[-3:]
    for k in recent:
        o, c, h, l = k["open"], k["close"], k["high"], k["low"]
        if h == l:
            continue
        body = abs(c - o)
        total_range = h - l
        ratio = body / total_range
        if c > o and ratio > 0.6:
            score += 3
        elif c < o and ratio > 0.6:
            score -= 2
        upper_shadow = h - max(o, c)
        if upper_shadow > 2 * body:
            score -= 2
    return max(-6, min(6, score))


def _rank_change_score(rc: int) -> int:
    if rc >= 2000: return 12
    if rc >= 1000: return 6
    return 0


def analyze_new_face(stock: StockInfo, kline: list[dict] | None) -> KlineSummary | None:
    if not kline or len(kline) < 5:
        return None

    today_pct = stock.percent

    if today_pct <= 0:
        return None

    pcts = [k["percent"] for k in kline]

    recent_5_pcts = pcts[-5:]
    down_days = sum(1 for p in recent_5_pcts if p < 0)
    has_crash_day = any(p <= -10 for p in recent_5_pcts)
    has_big_up_day = any(p >= 10 for p in recent_5_pcts)
    if not has_crash_day and not has_big_up_day and down_days >= 3 and sum(recent_5_pcts) < 5 and sum(recent_5_pcts) > -5 and today_pct < 5:
        return None

    accumulated = sum(pcts[-5:])

    volumes = [k["volume"] for k in kline]
    vol_window = volumes[-11:-1] if len(volumes) >= 11 else volumes[:-1]
    avg_vol = sum(vol_window) / max(len(vol_window), 1)
    today_vol = volumes[-1] if volumes else 0
    vol_ratio = today_vol / avg_vol if avg_vol > 0 else 1.0

    closes = [k["close"] for k in kline]
    recent_3_pcts = pcts[-3:] if len(pcts) >= 3 else pcts
    no_heavy_loss = all(p > -3 for p in recent_3_pcts)
    volume_surge = vol_ratio > 1.3
    near_20d_low = (closes[-1] - min(closes[-20:])) / max(min(closes[-20:]), 0.01) < 0.05 if len(closes) >= 20 else True
    bottom_confirmed = no_heavy_loss and volume_surge and near_20d_low

    v_shape_reversal = (
        accumulated < -8
        and volume_surge
        and today_pct > 3
    )

    if bottom_confirmed:
        trend = "⚡底部启动"
    elif v_shape_reversal:
        trend = "V型反转"
    elif no_heavy_loss:
        trend = "企稳回升"
    elif accumulated < -8:
        trend = "仍在探底"
    else:
        trend = "震荡整理"

    score = 0
    if 2 <= today_pct <= 6:
        score += 20
    elif today_pct < 2:
        score += 5
    elif today_pct > 8:
        score -= 15
    elif today_pct > 6:
        score += 5

    if -5 < accumulated < 15:
        score += 15
    elif accumulated <= -5:
        score -= 10
    elif accumulated >= 15:
        score -= 10
    if accumulated >= 25:
        score -= 10

    if bottom_confirmed:
        score += 15
    if v_shape_reversal:
        score += 12
    if volume_surge:
        score += 10

    score += _rank_change_score(stock.rank_change)
    if stock.value >= 10000:
        score += 5
    elif stock.value >= 5000:
        score += 2

    ma_bull = _ma_bull_score(closes)
    score += ma_bull
    candle = _candle_quality_score(kline)
    score += candle

    dims = {}
    td = stock.percent
    if 2 <= td <= 6: dims["new_face_today_pct"] = 20
    elif td < 2: dims["new_face_today_pct"] = 5
    elif td > 8: dims["new_face_today_pct"] = -15
    else: dims["new_face_today_pct"] = 5
    dims["new_face_accumulated"] = 15 if -5 < accumulated < 15 else (-10 if accumulated >= 15 else 0)
    if accumulated >= 25: dims["new_face_accumulated"] = -20
    if bottom_confirmed: dims["new_face_bottom"] = 15
    if volume_surge: dims["new_face_volume"] = 10
    rc_score = _rank_change_score(stock.rank_change)
    if rc_score: dims["new_face_rank_change"] = rc_score
    if stock.value >= 10000: dims["new_face_value"] = 5
    elif stock.value >= 5000: dims["new_face_value"] = 2
    if ma_bull: dims["new_face_ma_bull"] = ma_bull
    if candle: dims["new_face_candle"] = candle

    return KlineSummary(trend=trend, accumulated_pct=round(accumulated, 2),
                        volume_ratio=round(vol_ratio, 2), bottom_confirmed=bottom_confirmed,
                        score=score, dimensions=dims, avg_volume=round(avg_vol, 2))


def analyze_momentum(stock: StockInfo, kline: list[dict] | None) -> KlineSummary | None:
    if not kline or len(kline) < 5:
        return None

    today_pct = stock.percent
    if today_pct <= 0:
        return None

    pcts = [k["percent"] for k in kline]
    closes = [k["close"] for k in kline]
    accumulated = sum(pcts[-5:])

    if accumulated < 10:
        return None

    volumes = [k["volume"] for k in kline]
    vol_window = volumes[-11:-1] if len(volumes) >= 11 else volumes[:-1]
    avg_vol = sum(vol_window) / max(len(vol_window), 1)
    today_vol = volumes[-1] if volumes else 0
    vol_ratio = today_vol / avg_vol if avg_vol > 0 else 1.0

    score = 0

    if today_pct > 8:
        return None

    if 2 <= today_pct <= 8:
        score += 20
    elif today_pct < 2:
        score += 5
    else:
        score += 5

    if accumulated >= 30:
        score -= 15
        trend = "涨多⚠️"
    elif accumulated >= 20:
        score += 5
        trend = "动量延续"
    elif accumulated >= 15:
        score += 10
        trend = "动量启动"
    else:
        score += 15
        trend = "加速启动"

    if 0.7 < vol_ratio < 2.0:
        score += 10
    elif vol_ratio >= 2.0:
        score -= 8
    elif vol_ratio < 0.7:
        score -= 5

    if len(pcts) >= 2:
        recent_2_return = pcts[-2] + pcts[-1]
        no_crash = recent_2_return > -3
    else:
        no_crash = True
    if no_crash:
        score += 15

    score += _rank_change_score(stock.rank_change)
    if stock.value >= 10000:
        score += 5
    elif stock.value >= 5000:
        score += 2

    ma_bull = _ma_bull_score(closes)
    score += ma_bull
    candle = _candle_quality_score(kline)
    score += candle

    dims = {}
    td = stock.percent
    if 2 <= td <= 8: dims["momentum_today_pct"] = 20
    elif td < 2: dims["momentum_today_pct"] = 5
    if accumulated >= 30: dims["momentum_accumulated"] = -15
    elif accumulated >= 20: dims["momentum_accumulated"] = 5
    elif accumulated >= 15: dims["momentum_accumulated"] = 10
    else: dims["momentum_accumulated"] = 15
    if 0.7 < vol_ratio < 2.0: dims["momentum_volume"] = 10
    elif vol_ratio >= 2.0: dims["momentum_volume"] = -8
    elif vol_ratio < 0.7: dims["momentum_volume"] = -5
    if no_crash: dims["momentum_no_crash"] = 15
    rc_score = _rank_change_score(stock.rank_change)
    if rc_score: dims["momentum_rank_change"] = rc_score
    if stock.value >= 10000: dims["momentum_value"] = 5
    elif stock.value >= 5000: dims["momentum_value"] = 2
    if ma_bull: dims["momentum_ma_bull"] = ma_bull
    if candle: dims["momentum_candle"] = candle

    return KlineSummary(trend=trend, accumulated_pct=round(accumulated, 2),
                        volume_ratio=round(vol_ratio, 2), bottom_confirmed=no_crash,
                        score=score, dimensions=dims, avg_volume=round(avg_vol, 2))
