from datetime import date

from scanner.models import StockInfo, KlineSummary
from scanner.config import NEW_FACE_WEIGHTS, MOMENTUM_WEIGHTS

# Weak-form filter thresholds
_WEAK_FORM_MIN_DOWN_DAYS = 3
_WEAK_FORM_MAX_ACCUM = 5
_WEAK_FORM_MIN_ACCUM = -5
_WEAK_FORM_MAX_TODAY_PCT = 3
_WEAK_FORM_CRASH_THRESHOLD = -10
_WEAK_FORM_BIG_UP_THRESHOLD = 10

# Gap-up thresholds
_GAP_UP_STRONG = 2.0
_GAP_UP_MEDIUM = 1.0
_GAP_UP_WEAK = 0.5
_GAP_UP_STRONG_PTS = 8
_GAP_UP_MEDIUM_PTS = 5
_GAP_UP_WEAK_PTS = 3

# Volume-rank combo thresholds
_VOL_RANK_VOL_THRESHOLD = 1.15
_VOL_RANK_STRONG = 2000
_VOL_RANK_MEDIUM = 1000
_VOL_RANK_WEAK = 500
_VOL_RANK_STRONG_PTS = 15
_VOL_RANK_MEDIUM_PTS = 12
_VOL_RANK_WEAK_PTS = 8

# Bottom confirmation thresholds
_BOTTOM_MAX_LOSS = -3.0
_BOTTOM_VOL_SURGE = 1.15
_BOTTOM_NEAR_LOW_PCT = 0.08

# Crash detection thresholds
_CRASH_THRESHOLD = -7.0
_RECENT_2_RETURN_THRESHOLD = -3.0
_MOMENTUM_VOL_HEALTHY_MIN = 0.7
_MOMENTUM_VOL_HEALTHY_MAX = 2.0

# MA alignment thresholds
_MA_BULL_3_TIER_SCORE = 6
_MA_BULL_2_TIER_SCORE = 3
_MA_BEAR_SCORE = -3


def _ma_bull_score(closes: list[float]) -> int:
    if len(closes) < 10:
        return 0
    ma5 = sum(closes[-5:]) / 5
    ma10 = sum(closes[-10:]) / 10
    if len(closes) >= 20:
        ma20 = sum(closes[-20:]) / 20
        if ma5 > ma10 > ma20:
            return _MA_BULL_3_TIER_SCORE
    if ma5 > ma10:
        return _MA_BULL_2_TIER_SCORE
    return _MA_BEAR_SCORE


def _detect_gap_up(today_current: float, kline: list[dict]) -> tuple[float, int]:
    yesterday_close = None
    today_str = date.today().isoformat()
    for k in reversed(kline):
        if k["date"] != today_str:
            yesterday_close = k["close"]
            break
    if yesterday_close is None or yesterday_close <= 0:
        return 0.0, 0
    gap_pct = (today_current - yesterday_close) / yesterday_close * 100
    if gap_pct > _GAP_UP_STRONG:
        return round(gap_pct, 2), _GAP_UP_STRONG_PTS
    if gap_pct > _GAP_UP_MEDIUM:
        return round(gap_pct, 2), _GAP_UP_MEDIUM_PTS
    if gap_pct > _GAP_UP_WEAK:
        return round(gap_pct, 2), _GAP_UP_WEAK_PTS
    return round(gap_pct, 2), 0


def _vol_rank_combo_score(vol_ratio: float, rank_change: int) -> int:
    if vol_ratio > _VOL_RANK_VOL_THRESHOLD and rank_change >= _VOL_RANK_STRONG:
        return _VOL_RANK_STRONG_PTS
    if vol_ratio > _VOL_RANK_VOL_THRESHOLD and rank_change >= _VOL_RANK_MEDIUM:
        return _VOL_RANK_MEDIUM_PTS
    if vol_ratio > _VOL_RANK_VOL_THRESHOLD and rank_change >= _VOL_RANK_WEAK:
        return _VOL_RANK_WEAK_PTS
    return 0


def analyze_new_face(stock: StockInfo, kline: list[dict] | None,
                     weight_overrides: dict | None = None) -> KlineSummary | None:
    if not kline or len(kline) < 5:
        return None

    W = {**NEW_FACE_WEIGHTS, **(weight_overrides or {})}

    today_pct = stock.percent

    if today_pct <= 0:
        return None

    today_str = date.today().isoformat()
    historical_kline = [k for k in kline if k["date"] != today_str]
    pcts = [k["percent"] for k in historical_kline]
    closes = [k["close"] for k in historical_kline]

    recent_5_pcts = pcts[-5:]
    down_days = sum(1 for p in recent_5_pcts if p < 0)
    has_crash_day = any(p <= _WEAK_FORM_CRASH_THRESHOLD for p in recent_5_pcts)
    has_big_up_day = any(p >= _WEAK_FORM_BIG_UP_THRESHOLD for p in recent_5_pcts)
    sum_5 = sum(recent_5_pcts)
    if (not has_crash_day and not has_big_up_day
            and down_days >= _WEAK_FORM_MIN_DOWN_DAYS
            and _WEAK_FORM_MIN_ACCUM < sum_5 < _WEAK_FORM_MAX_ACCUM
            and today_pct < _WEAK_FORM_MAX_TODAY_PCT):
        return None

    accumulated = sum(pcts[-5:])
    if accumulated < -8:
        return None

    volumes = [k["volume"] for k in kline]
    vol_window = volumes[-11:-1] if len(volumes) >= 11 else volumes[:-1]
    avg_vol = sum(vol_window) / max(len(vol_window), 1)
    today_vol = volumes[-1] if volumes else 0
    vol_ratio = today_vol / avg_vol if avg_vol > 0 else 1.0
    recent_3_pcts = pcts[-3:] if len(pcts) >= 3 else pcts
    no_heavy_loss = all(p > _BOTTOM_MAX_LOSS for p in recent_3_pcts)
    volume_surge = vol_ratio > _BOTTOM_VOL_SURGE
    near_20d_low = (closes[-1] - min(closes[-20:])) / max(min(closes[-20:]), 0.01) < _BOTTOM_NEAR_LOW_PCT if len(closes) >= 20 else True
    bottom_confirmed = no_heavy_loss and volume_surge and near_20d_low

    v_shape_reversal = (
        accumulated < -5
        and volume_surge
        and today_pct > 2
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
        score += W["today_pct_2_6"]
    elif today_pct < 0.5:
        score += W["today_pct_lt_0_5"]
    elif today_pct < 1:
        score += W["today_pct_0_5_1"]
    elif today_pct < 2:
        score += W["today_pct_1_2"]
    elif today_pct > 8:
        score += W["today_pct_gt_8"]
    else:
        score += W["today_pct_6_8"]

    if -5 < accumulated <= 10:
        score += W["accum_neg5_10"]
    elif accumulated <= -5:
        score += W["accum_lt_neg5"]
    elif accumulated <= 15:
        score += W["accum_10_15"]
    elif accumulated < 25:
        score += W["accum_15_25"]
    else:
        score += W["accum_gt_25"]

    if bottom_confirmed:
        score += W["bottom_confirmed"]
    elif v_shape_reversal:
        score += W["v_shape"]
    elif volume_surge:
        score += W["volume_surge"]

    vol_rank = _vol_rank_combo_score(vol_ratio, stock.rank_change)
    score += vol_rank
    if vol_rank >= 12 and accumulated >= 20:
        score -= 10

    gap_pct, gap_pts = _detect_gap_up(stock.current, kline)
    score += gap_pts

    if stock.value >= 10000:
        score += W["value_gte_10000"]
    elif stock.value >= 5000:
        score += W["value_gte_5000"]

    ma_bull = _ma_bull_score(closes)
    score += ma_bull

    dims = {}
    td = stock.percent
    if 2 <= td <= 6: dims["new_face_today_pct"] = W["today_pct_2_6"]
    elif td < 0.5: dims["new_face_today_pct"] = W["today_pct_lt_0_5"]
    elif td < 1: dims["new_face_today_pct"] = W["today_pct_0_5_1"]
    elif td < 2: dims["new_face_today_pct"] = W["today_pct_1_2"]
    elif td > 8: dims["new_face_today_pct"] = W["today_pct_gt_8"]
    else: dims["new_face_today_pct"] = W["today_pct_6_8"]
    if accumulated <= -5:
        dims["new_face_accumulated"] = W["accum_lt_neg5"]
    elif accumulated <= 10:
        dims["new_face_accumulated"] = W["accum_neg5_10"]
    elif accumulated <= 15:
        dims["new_face_accumulated"] = W["accum_10_15"]
    elif accumulated < 25:
        dims["new_face_accumulated"] = W["accum_15_25"]
    else:
        dims["new_face_accumulated"] = W["accum_gt_25"]
    if bottom_confirmed: dims["new_face_bottom"] = W["bottom_confirmed"]
    if volume_surge: dims["new_face_volume"] = W["volume_surge"]
    if vol_rank: dims["new_face_vol_rank"] = vol_rank
    if gap_pts: dims["new_face_gap_up"] = gap_pts
    if stock.value >= 10000: dims["new_face_value"] = W["value_gte_10000"]
    elif stock.value >= 5000: dims["new_face_value"] = W["value_gte_5000"]
    if ma_bull: dims["new_face_ma_bull"] = ma_bull

    return KlineSummary(trend=trend, accumulated_pct=round(accumulated, 2),
                        volume_ratio=round(vol_ratio, 2), bottom_confirmed=bottom_confirmed,
                        score=score, dimensions=dims, avg_volume=round(avg_vol, 2))


def analyze_momentum(stock: StockInfo, kline: list[dict] | None,
                     weight_overrides: dict | None = None) -> KlineSummary | None:
    if not kline or len(kline) < 5:
        return None

    W = {**MOMENTUM_WEIGHTS, **(weight_overrides or {})}

    today_pct = stock.percent
    if today_pct <= 0:
        return None

    today_str = date.today().isoformat()
    historical_kline = [k for k in kline if k["date"] != today_str]
    pcts = [k["percent"] for k in historical_kline]
    closes = [k["close"] for k in historical_kline]
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

    if 2 <= today_pct <= 6:
        score += W["today_pct_2_6"]
    elif today_pct < 0.5:
        score += W["today_pct_lt_0_5"]
    elif today_pct < 1:
        score += W["today_pct_0_5_1"]
    elif today_pct < 2:
        score += W["today_pct_1_2"]
    else:
        score += W["today_pct_6_8"]

    if accumulated >= 30:
        score += W["accum_gte_30"]
        trend = "涨多⚠️"
    elif accumulated >= 20:
        score += W["accum_20_30"]
        trend = "动量延续"
    elif accumulated >= 15:
        score += W["accum_15_20"]
        trend = "动量启动"
    else:
        score += W["accum_10_15"]
        trend = "加速启动"

    if 0.7 < vol_ratio < 2.0:
        score += W["vol_healthy"]
    elif vol_ratio >= 2.0:
        score += W["vol_surge"]
    elif vol_ratio < 0.7:
        score += W["vol_low"]

    if len(pcts) >= 2:
        has_crash_day = any(p <= _CRASH_THRESHOLD for p in pcts[-5:])
        recent_2_return = pcts[-2] + pcts[-1]
        no_crash = not has_crash_day and recent_2_return > _RECENT_2_RETURN_THRESHOLD
    else:
        no_crash = True
    if no_crash:
        score += W["no_crash"]

    vol_rank = _vol_rank_combo_score(vol_ratio, stock.rank_change)
    score += vol_rank

    gap_pct, gap_pts = _detect_gap_up(stock.current, kline)
    score += gap_pts

    if stock.value >= 10000:
        score += W["value_gte_10000"]
    elif stock.value >= 5000:
        score += W["value_gte_5000"]

    ma_bull = _ma_bull_score(closes)
    score += ma_bull

    dims = {}
    td = stock.percent
    if 2 <= td <= 6: dims["momentum_today_pct"] = W["today_pct_2_6"]
    elif td < 0.5: dims["momentum_today_pct"] = W["today_pct_lt_0_5"]
    elif td < 1: dims["momentum_today_pct"] = W["today_pct_0_5_1"]
    elif td < 2: dims["momentum_today_pct"] = W["today_pct_1_2"]
    else: dims["momentum_today_pct"] = W["today_pct_6_8"]
    if accumulated >= 30: dims["momentum_accumulated"] = W["accum_gte_30"]
    elif accumulated >= 20: dims["momentum_accumulated"] = W["accum_20_30"]
    elif accumulated >= 15: dims["momentum_accumulated"] = W["accum_15_20"]
    else: dims["momentum_accumulated"] = W["accum_10_15"]
    if 0.7 < vol_ratio < 2.0: dims["momentum_volume"] = W["vol_healthy"]
    elif vol_ratio >= 2.0: dims["momentum_volume"] = W["vol_surge"]
    elif vol_ratio < 0.7: dims["momentum_volume"] = W["vol_low"]
    if no_crash: dims["momentum_no_crash"] = W["no_crash"]
    if vol_rank: dims["momentum_vol_rank"] = vol_rank
    if gap_pts: dims["momentum_gap_up"] = gap_pts
    if stock.value >= 10000: dims["momentum_value"] = W["value_gte_10000"]
    elif stock.value >= 5000: dims["momentum_value"] = W["value_gte_5000"]
    if ma_bull: dims["momentum_ma_bull"] = ma_bull

    return KlineSummary(trend=trend, accumulated_pct=round(accumulated, 2),
                        volume_ratio=round(vol_ratio, 2), bottom_confirmed=no_crash,
                        score=score, dimensions=dims, avg_volume=round(avg_vol, 2))
