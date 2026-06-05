from scanner.models import StockInfo, KlineSummary


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
    if not has_crash_day and down_days >= 3 and sum(recent_5_pcts) < 5 and sum(recent_5_pcts) > -5 and today_pct < 5:
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

    if accumulated < 15 and accumulated > -5:
        score += 15
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

    if stock.rank_change >= 2000:
        score += 12
    elif stock.rank_change >= 1000:
        score += 6
    if stock.value >= 10000:
        score += 5
    elif stock.value >= 5000:
        score += 2

    if today_pct <= 5 and accumulated < 8:
        score += 8

    return KlineSummary(trend=trend, accumulated_pct=round(accumulated, 2),
                        volume_ratio=round(vol_ratio, 2), bottom_confirmed=bottom_confirmed, score=score)


def analyze_old_face(stock: StockInfo, kline: list[dict] | None) -> KlineSummary | None:
    if not kline or len(kline) < 5:
        return None

    today_pct = stock.percent

    if today_pct > 8 or today_pct < -8:
        return None

    pcts = [k["percent"] for k in kline]
    closes = [k["close"] for k in kline]

    has_crash_day = any(p <= -10 for p in pcts[-5:])
    if not has_crash_day and sum(1 for p in pcts[-5:] if p < 0) >= 3 and sum(pcts[-5:]) < 5 and sum(pcts[-5:]) > -5 and today_pct < 5:
        return None

    accumulated = sum(pcts[-5:])

    is_pullback = today_pct < 2

    recent_10_closes = closes[-10:] if len(closes) >= 10 else closes
    ten_day_low = min(recent_10_closes)
    near_low = (recent_10_closes[-1] - ten_day_low) / max(ten_day_low, 0.01) < 0.15
    not_broken = near_low and recent_10_closes[-1] >= ten_day_low

    volumes = [k["volume"] for k in kline]
    vol_window = volumes[-11:-1] if len(volumes) >= 11 else volumes[:-1]
    avg_vol = sum(vol_window) / max(len(vol_window), 1)
    today_vol = volumes[-1] if volumes else 0
    vol_ratio = today_vol / avg_vol if avg_vol > 0 else 1.0
    shrinking_volume = vol_ratio < 1.1

    if today_pct < -3:
        trend = "大幅回调⚠️"
    elif today_pct < 0:
        trend = "缩量回调" if shrinking_volume else "放量回调⚠️"
    elif today_pct < 2:
        trend = "横盘"
    else:
        trend = "再次拉升"

    score = 0
    if is_pullback:
        score += 20
    if not_broken:
        score += 15
    if shrinking_volume:
        score += 12
    if stock.value >= 10000:
        score += 10
    elif stock.value >= 5000:
        score += 5
    if today_pct < 0 and today_pct >= -3:
        score += 8
    elif today_pct < -3:
        score -= 10
    if stock.rank_change >= 2000:
        score += 8
    elif stock.rank_change >= 1000:
        score += 4
    if vol_ratio < 0.4:
        score -= 8

    if accumulated >= 25:
        score -= 15
        trend = "涨多⚠️" + trend

    low_20d = min(closes[-20:]) if len(closes) >= 20 else min(closes)
    pct_from_low = (closes[-1] - low_20d) / max(low_20d, 0.01) * 100
    if pct_from_low >= 50:
        score -= 20
        trend = "暴涨⚠️" + trend
    elif pct_from_low >= 30:
        score -= 10
        trend = "涨幅已大⚠️" + trend

    return KlineSummary(trend=trend, accumulated_pct=round(accumulated, 2),
                        volume_ratio=round(vol_ratio, 2), bottom_confirmed=not_broken and is_pullback, score=score)


def analyze_momentum(stock: StockInfo, kline: list[dict] | None) -> KlineSummary | None:
    if not kline or len(kline) < 5:
        return None

    today_pct = stock.percent
    if today_pct <= 0:
        return None

    pcts = [k["percent"] for k in kline]
    accumulated = sum(pcts[-5:])

    if accumulated < 10:
        return None

    volumes = [k["volume"] for k in kline]
    vol_window = volumes[-11:-1] if len(volumes) >= 11 else volumes[:-1]
    avg_vol = sum(vol_window) / max(len(vol_window), 1)
    today_vol = volumes[-1] if volumes else 0
    vol_ratio = today_vol / avg_vol if avg_vol > 0 else 1.0

    score = 0

    if 2 <= today_pct <= 8:
        score += 20
    elif today_pct < 2:
        score += 5
    elif today_pct > 8:
        return None

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

    if stock.rank_change >= 2000:
        score += 12
    elif stock.rank_change >= 1000:
        score += 6
    if stock.value >= 10000:
        score += 5
    elif stock.value >= 5000:
        score += 2

    return KlineSummary(trend=trend, accumulated_pct=round(accumulated, 2),
                        volume_ratio=round(vol_ratio, 2), bottom_confirmed=no_crash, score=score)
