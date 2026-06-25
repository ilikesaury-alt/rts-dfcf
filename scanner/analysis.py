from datetime import date

from scanner.config import (
    MAX_MOMENTUM_TODAY_PCT,
    MAX_NEW_FACE_TODAY_PCT,
    MOMENTUM_WEIGHTS,
    NEW_FACE_WEIGHTS,
    PULLBACK_WEIGHTS,
    VOL_RANK_MEDIUM_PTS,
    VOL_RANK_MEDIUM_RC,
    VOL_RANK_STRONG_PTS,
    VOL_RANK_STRONG_RC,
    VOL_RANK_VOL_THRESHOLD,
    VOL_RANK_WEAK_PTS,
    VOL_RANK_WEAK_RC,
)
from scanner.indicators import compute_kdj, compute_macd, compute_rsi
from scanner.models import KlineSummary, StockInfo

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

# Bottom confirmation thresholds
_BOTTOM_MAX_LOSS = -3.0
_BOTTOM_VOL_SURGE = 1.15
_BOTTOM_NEAR_LOW_PCT = 0.08

# Crash detection thresholds
_CRASH_THRESHOLD = -12.0
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


def _detect_gap_up(today_current: float, kline: list[dict], today_str: str | None = None) -> tuple[float, int]:
    yesterday_close = None
    today_str = today_str or date.today().isoformat()
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
    if vol_ratio > VOL_RANK_VOL_THRESHOLD and rank_change >= VOL_RANK_STRONG_RC:
        return VOL_RANK_STRONG_PTS
    if vol_ratio > VOL_RANK_VOL_THRESHOLD and rank_change >= VOL_RANK_MEDIUM_RC:
        return VOL_RANK_MEDIUM_PTS
    if vol_ratio > VOL_RANK_VOL_THRESHOLD and rank_change >= VOL_RANK_WEAK_RC:
        return VOL_RANK_WEAK_PTS
    return 0


def _score_today_pct(today_pct: float, W: dict, prefix: str) -> tuple[int, str, int]:
    if today_pct < 0.5:
        return W["today_pct_lt_0_5"], f"{prefix}_today_pct", W["today_pct_lt_0_5"]
    elif today_pct < 1:
        return W["today_pct_0_5_1"], f"{prefix}_today_pct", W["today_pct_0_5_1"]
    elif today_pct < 2:
        return W["today_pct_1_2"], f"{prefix}_today_pct", W["today_pct_1_2"]
    elif today_pct <= 6:
        return W["today_pct_2_6"], f"{prefix}_today_pct", W["today_pct_2_6"]
    elif today_pct <= 7:
        return W["today_pct_6_7"], f"{prefix}_today_pct", W["today_pct_6_7"]
    else:
        return W["today_pct_7_12"], f"{prefix}_today_pct", W["today_pct_7_12"]



def _compute_new_face_indicators(closes: list[float], historical_kline: list[dict],
                                 W: dict) -> tuple[int, dict]:
    """New face specific indicator scoring (oversold reversal signals)."""
    rsi_val = compute_rsi(closes, period=6)
    kdj_val = compute_kdj([k["high"] for k in historical_kline],
                          [k["low"] for k in historical_kline], closes)
    macd_val = compute_macd(closes)

    bonus = 0
    dims: dict[str, float] = {}

    if rsi_val is not None:
        if rsi_val < 20:
            bonus += W["rsi_bonus"] * 2
        elif rsi_val < 30:
            bonus += W["rsi_bonus"]
        dims["new_face_rsi"] = round(rsi_val, 1)
    if kdj_val is not None:
        if kdj_val["K"] < 20 and kdj_val["K"] > kdj_val["D"]:
            bonus += W["kdj_bonus"]
        if kdj_val["J"] < 0:
            bonus += W["kdj_bonus"]
        dims["new_face_kdj"] = round(kdj_val["J"], 1)
    if macd_val is not None:
        if macd_val["histogram"] > 0 and macd_val["histogram_prev"] <= 0:
            bonus += W["macd_bonus"]
        if macd_val["macd"] > macd_val["signal"]:
            bonus += W["macd_bonus"]
        dims["new_face_macd"] = round(macd_val["histogram"], 4)

    return bonus, dims


def _compute_momentum_indicators(closes: list[float], historical_kline: list[dict],
                                 W: dict) -> tuple[int, dict]:
    """Momentum specific indicator scoring (trend confirmation signals)."""
    rsi_val = compute_rsi(closes, period=6)
    kdj_val = compute_kdj([k["high"] for k in historical_kline],
                          [k["low"] for k in historical_kline], closes)
    macd_val = compute_macd(closes)

    bonus = 0
    dims: dict[str, float] = {}

    if rsi_val is not None:
        if 50 <= rsi_val <= 70:
            bonus += W["rsi_bonus"]
        elif rsi_val > 80:
            bonus -= W["rsi_bonus"]
        dims["momentum_rsi"] = round(rsi_val, 1)
    if kdj_val is not None:
        dims["momentum_kdj"] = round(kdj_val["J"], 1)
    if macd_val is not None:
        if macd_val["histogram"] > 0:
            bonus += W["macd_bonus"]
        if macd_val["histogram"] > 0 and macd_val["histogram"] > macd_val["histogram_prev"]:
            bonus += W["macd_bonus"]
        elif macd_val["histogram"] > 0 and macd_val["histogram"] < macd_val["histogram_prev"]:
            bonus -= W["macd_bonus"]
        dims["momentum_macd"] = round(macd_val["histogram"], 4)

    return bonus, dims


def analyze_new_face(stock: StockInfo, kline: list[dict] | None,
                     today_str: str | None = None) -> KlineSummary | None:
    if not kline or len(kline) < 5:
        return None

    W = NEW_FACE_WEIGHTS

    today_pct = stock.percent

    if today_pct <= 0:
        return None

    if today_pct > MAX_NEW_FACE_TODAY_PCT:
        return None

    today_str = today_str or date.today().isoformat()
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
    else:
        trend = "震荡整理"

    # Score and dims in one pass
    dims: dict[str, int | float] = {}

    today_score, today_dim_key, today_dim_val = _score_today_pct(today_pct, W, "new_face")
    score = today_score
    dims[today_dim_key] = today_dim_val

    # Accumulated
    if -5 < accumulated <= 10:
        acc_score = W["accum_neg5_10"]
        dims["new_face_accumulated"] = W["accum_neg5_10"]
    elif accumulated <= -5:
        acc_score = W["accum_lt_neg5"]
        dims["new_face_accumulated"] = W["accum_lt_neg5"]
    elif accumulated <= 15:
        acc_score = W["accum_10_15"]
        dims["new_face_accumulated"] = W["accum_10_15"]
    elif accumulated < 25:
        acc_score = W["accum_15_25"]
        dims["new_face_accumulated"] = W["accum_15_25"]
    else:
        acc_score = W["accum_gt_25"]
        dims["new_face_accumulated"] = W["accum_gt_25"]
    score += acc_score

    # Bottom / V-shape / volume
    if bottom_confirmed:
        score += W["bottom_confirmed"]
        dims["new_face_bottom"] = W["bottom_confirmed"]
    elif v_shape_reversal:
        score += W["v_shape"]
    elif volume_surge:
        score += W["volume_surge"]
        dims["new_face_volume"] = W["volume_surge"]

    vol_rank = _vol_rank_combo_score(vol_ratio, stock.rank_change)
    score += vol_rank
    if vol_rank:
        dims["new_face_vol_rank"] = vol_rank
    if vol_rank >= 12 and accumulated >= 20:
        score -= 10

    gap_pct, gap_pts = _detect_gap_up(stock.current, kline, today_str)
    score += gap_pts
    if gap_pts:
        dims["new_face_gap_up"] = gap_pts

    if stock.value >= 10000:
        score += W["value_gte_10000"]
        dims["new_face_value"] = W["value_gte_10000"]
    elif stock.value >= 5000:
        score += W["value_gte_5000"]
        dims["new_face_value"] = W["value_gte_5000"]

    ma_bull = _ma_bull_score(closes)
    score += ma_bull
    if ma_bull:
        dims["new_face_ma_bull"] = ma_bull

    indicator_bonus, indicator_dims = _compute_new_face_indicators(closes, historical_kline, W)
    score += indicator_bonus
    dims.update(indicator_dims)

    return KlineSummary(trend=trend, accumulated_pct=round(accumulated, 2),
                        volume_ratio=round(vol_ratio, 2), bottom_confirmed=bottom_confirmed,
                        score=score, dimensions=dims, avg_volume=round(avg_vol, 2))


def analyze_momentum(stock: StockInfo, kline: list[dict] | None,
                     today_str: str | None = None) -> KlineSummary | None:
    if not kline or len(kline) < 5:
        return None

    W = MOMENTUM_WEIGHTS

    today_pct = stock.percent
    if today_pct <= 0:
        return None

    today_str = today_str or date.today().isoformat()
    historical_kline = [k for k in kline if k["date"] != today_str]
    pcts = [k["percent"] for k in historical_kline]
    closes = [k["close"] for k in historical_kline]
    accumulated = sum(pcts[-5:])

    if accumulated < 8:
        return None

    volumes = [k["volume"] for k in kline]
    vol_window = volumes[-11:-1] if len(volumes) >= 11 else volumes[:-1]
    avg_vol = sum(vol_window) / max(len(vol_window), 1)
    today_vol = volumes[-1] if volumes else 0
    vol_ratio = today_vol / avg_vol if avg_vol > 0 else 1.0

    score = 0
    dims: dict[str, int | float] = {}

    if today_pct > MAX_MOMENTUM_TODAY_PCT:
        return None

    today_score, today_dim_key, today_dim_val = _score_today_pct(today_pct, W, "momentum")
    score += today_score
    dims[today_dim_key] = today_dim_val

    # Accumulated
    if accumulated >= 30:
        score += W["accum_gte_30"]
        dims["momentum_accumulated"] = W["accum_gte_30"]
        trend = "涨多⚠️"
    elif accumulated >= 20:
        score += W["accum_20_30"]
        dims["momentum_accumulated"] = W["accum_20_30"]
        trend = "动量延续"
    elif accumulated >= 15:
        score += W["accum_15_20"]
        dims["momentum_accumulated"] = W["accum_15_20"]
        trend = "动量启动"
    else:
        score += W["accum_10_15"]
        dims["momentum_accumulated"] = W["accum_10_15"]
        trend = "加速启动"

    # Volume
    if 0.7 < vol_ratio < 2.0:
        score += W["vol_healthy"]
        dims["momentum_volume"] = W["vol_healthy"]
    elif vol_ratio >= 2.0:
        score += W["vol_surge"]
        dims["momentum_volume"] = W["vol_surge"]
    elif vol_ratio < 0.7:
        score += W["vol_low"]
        dims["momentum_volume"] = W["vol_low"]

    # Crash check
    if len(pcts) >= 2:
        has_crash_day = any(p <= _CRASH_THRESHOLD for p in pcts[-5:])
        recent_2_return = pcts[-2] + pcts[-1]
        no_crash = not has_crash_day and recent_2_return > _RECENT_2_RETURN_THRESHOLD
    else:
        no_crash = True
    if no_crash:
        score += W["no_crash"]
        dims["momentum_no_crash"] = W["no_crash"]

    vol_rank = _vol_rank_combo_score(vol_ratio, stock.rank_change)
    score += vol_rank
    if vol_rank:
        dims["momentum_vol_rank"] = vol_rank

    gap_pct, gap_pts = _detect_gap_up(stock.current, kline, today_str)
    score += gap_pts
    if gap_pts:
        dims["momentum_gap_up"] = gap_pts

    if stock.value >= 10000:
        score += W["value_gte_10000"]
        dims["momentum_value"] = W["value_gte_10000"]
    elif stock.value >= 5000:
        score += W["value_gte_5000"]
        dims["momentum_value"] = W["value_gte_5000"]

    ma_bull = _ma_bull_score(closes)
    score += ma_bull
    if ma_bull:
        dims["momentum_ma_bull"] = ma_bull

    indicator_bonus, indicator_dims = _compute_momentum_indicators(closes, historical_kline, W)
    score += indicator_bonus
    dims.update(indicator_dims)

    return KlineSummary(trend=trend, accumulated_pct=round(accumulated, 2),
                        volume_ratio=round(vol_ratio, 2), bottom_confirmed=no_crash,
                        score=score, dimensions=dims, avg_volume=round(avg_vol, 2))


def _calc_pullback_base_metrics(kline: list[dict], today_str: str) -> tuple[list, list, float, float, float]:
    """Calculate base metrics for pullback analysis.

    Returns:
        (pcts, closes, accumulated, vol_ratio, avg_vol) tuple
    """
    today_str = today_str or date.today().isoformat()
    historical_kline = [k for k in kline if k["date"] != today_str]
    pcts = [k["percent"] for k in historical_kline]
    closes = [k["close"] for k in historical_kline]
    accumulated = sum(pcts[-5:])

    volumes = [k["volume"] for k in kline]
    vol_window = volumes[-11:-1] if len(volumes) >= 11 else volumes[:-1]
    avg_vol = sum(vol_window) / max(len(vol_window), 1)
    today_vol = volumes[-1] if volumes else 0
    vol_ratio = today_vol / avg_vol if avg_vol > 0 else 1.0

    return pcts, closes, accumulated, vol_ratio, avg_vol


def _score_pullback_today_pct(today_pct: float, W: dict) -> tuple[int, dict]:
    """Score based on today's percentage change.

    Returns:
        (score, dimensions) tuple
    """
    score = 0
    dims: dict[str, int | float] = {}

    if today_pct <= 0:
        if today_pct > -1:
            score += W["today_neg1_0"]
            dims["pullback_today_pct"] = W["today_neg1_0"]
        elif today_pct > -3:
            score += W["today_neg3_neg1"]
            dims["pullback_today_pct"] = W["today_neg3_neg1"]
        else:
            score += W["today_neg5_neg3"]
            dims["pullback_today_pct"] = W["today_neg5_neg3"]
    else:
        score += W["today_pos0_2"]
        dims["pullback_today_pct"] = W["today_pos0_2"]

    return score, dims


def _score_pullback_accumulated(accumulated: float, W: dict) -> tuple[int, dict]:
    """Score based on accumulated percentage change.

    Returns:
        (score, dimensions) tuple
    """
    score = 0
    dims: dict[str, int | float] = {}

    if accumulated < 10:
        score += W["accum_5_10"]
        dims["pullback_accumulated"] = W["accum_5_10"]
    elif accumulated < 20:
        score += W["accum_10_20"]
        dims["pullback_accumulated"] = W["accum_10_20"]
    elif accumulated < 30:
        score += W["accum_20_30"]
        dims["pullback_accumulated"] = W["accum_20_30"]
    else:
        score += W["accum_gte_30"]
        dims["pullback_accumulated"] = W["accum_gte_30"]

    return score, dims


def _score_pullback_volume(vol_ratio: float, W: dict) -> tuple[int, dict]:
    """Score based on volume ratio.

    Returns:
        (score, dimensions) tuple
    """
    score = 0
    dims: dict[str, int | float] = {}

    if vol_ratio < 0.4:
        score += W["vol_low"]
        dims["pullback_volume"] = W["vol_low"]
    elif vol_ratio <= 0.9:
        score += W["vol_healthy"]
        dims["pullback_volume"] = W["vol_healthy"]
    elif vol_ratio > 1.3:
        score += W["vol_surge"]
        dims["pullback_volume"] = W["vol_surge"]
    else:
        # 0.9 < vol_ratio <= 1.3: moderate volume, neutral for pullback
        dims["pullback_volume"] = 0

    return score, dims


def _check_crash_day(pcts: list, W: dict) -> tuple[bool, int, dict]:
    """Check if there's been a crash day in the last 5 days.

    Returns:
        (has_crash_day, score, dimensions) tuple
    """
    has_crash_day = any(p <= _CRASH_THRESHOLD for p in pcts[-5:])
    score = 0
    dims: dict[str, int | float] = {}

    if not has_crash_day:
        score = W["no_crash"]
        dims["pullback_no_crash"] = W["no_crash"]

    return has_crash_day, score, dims


def _analyze_pullback_ma(closes: list, W: dict) -> dict:
    """Analyze moving averages for pullback strategy.

    Returns:
        Dictionary containing MA analysis results
    """
    result = {
        "ma10": None,
        "ma_support": False,
        "ma_broken": False,
        "ma_bull_extra": 0,
        "score": 0,
        "dimensions": {}
    }

    if len(closes) >= 10:
        result["ma10"] = sum(closes[-10:]) / 10
        current_close = closes[-1]
        pct_from_ma10 = (current_close - result["ma10"]) / result["ma10"] * 100
        if abs(pct_from_ma10) <= 2:
            result["ma_support"] = True
            result["score"] += W["ma_support"]
            result["dimensions"]["pullback_ma_support"] = W["ma_support"]

    if len(closes) >= 20:
        ma20 = sum(closes[-20:]) / 20
        if closes[-1] < ma20:
            result["ma_broken"] = True
            result["score"] += W["ma_broken"]
            result["dimensions"]["pullback_ma_broken"] = W["ma_broken"]
        ma5 = sum(closes[-5:]) / 5
        if result["ma10"] is not None and ma5 > result["ma10"] and result["ma10"] > ma20:
            result["ma_bull_extra"] = 5
            result["score"] += result["ma_bull_extra"]
            result["dimensions"]["pullback_ma_bull"] = result["ma_bull_extra"]
    elif len(closes) >= 10:
        if result["ma_support"] and result["ma10"] is not None:
            ma5 = sum(closes[-5:]) / 5
            if ma5 > result["ma10"]:
                result["ma_bull_extra"] = 5
                result["score"] += result["ma_bull_extra"]
                result["dimensions"]["pullback_ma_bull"] = result["ma_bull_extra"]

    if not result["ma_support"] and not result["ma_broken"] and result["ma_bull_extra"] == 0:
        result["dimensions"]["pullback_ma_support"] = 0

    return result


def _score_pullback_indicators(closes: list, W: dict) -> tuple[int, dict]:
    """Score based on technical indicators (RSI, MACD).

    Returns:
        (score, dimensions) tuple
    """
    score = 0
    dims: dict[str, int | float] = {}

    rsi_val = compute_rsi(closes, period=6)
    macd_val = compute_macd(closes)

    if rsi_val is not None:
        if rsi_val < 30:
            score += W["rsi_oversold"]
            dims["pullback_rsi"] = round(rsi_val, 1)
        elif rsi_val < 45:
            score += W["rsi_mid"]
            dims["pullback_rsi"] = round(rsi_val, 1)
        else:
            dims["pullback_rsi"] = round(rsi_val, 1)

    if macd_val is not None:
        if macd_val["macd"] > macd_val["signal"]:
            score += W["macd_bonus"]
            dims["pullback_macd"] = round(macd_val["histogram"], 4)

    return score, dims


def _classify_pullback_trend(ma_support: bool, ma_broken: bool, today_pct: float) -> str:
    """Classify the pullback trend based on conditions.

    Returns:
        Trend classification string
    """
    if ma_support and not ma_broken and (-5 < today_pct < 0):
        return "缩量回调"
    elif ma_broken:
        return "破位回调"
    else:
        return "回踩整理"


def analyze_pullback(stock: StockInfo, kline: list[dict] | None,
                     today_str: str | None = None) -> KlineSummary | None:
    if not kline or len(kline) < 5:
        return None

    W = PULLBACK_WEIGHTS

    today_pct = stock.percent
    if today_pct <= -8 or today_pct > 2:
        return None

    pcts, closes, accumulated, vol_ratio, avg_vol = _calc_pullback_base_metrics(kline, today_str)

    if accumulated < 5:
        return None

    score = 0
    dims: dict[str, int | float] = {}

    # Score each component
    today_score, today_dims = _score_pullback_today_pct(today_pct, W)
    score += today_score
    dims.update(today_dims)

    accum_score, accum_dims = _score_pullback_accumulated(accumulated, W)
    score += accum_score
    dims.update(accum_dims)

    vol_score, vol_dims = _score_pullback_volume(vol_ratio, W)
    score += vol_score
    dims.update(vol_dims)

    has_crash_day, crash_score, crash_dims = _check_crash_day(pcts, W)
    score += crash_score
    dims.update(crash_dims)

    ma_result = _analyze_pullback_ma(closes, W)
    score += ma_result["score"]
    dims.update(ma_result["dimensions"])

    rank = stock.rank
    if rank <= 10:
        score += W["rank_top10"]
        dims["pullback_rank"] = W["rank_top10"]
    elif rank <= 30:
        score += W["rank_top30"]
        dims["pullback_rank"] = W["rank_top30"]

    indicator_score, indicator_dims = _score_pullback_indicators(closes, W)
    score += indicator_score
    dims.update(indicator_dims)

    trend = _classify_pullback_trend(ma_result["ma_support"], ma_result["ma_broken"], today_pct)

    return KlineSummary(trend=trend, accumulated_pct=round(accumulated, 2),
                        volume_ratio=round(vol_ratio, 2), bottom_confirmed=not has_crash_day,
                        score=score, dimensions=dims, avg_volume=round(avg_vol, 2))
