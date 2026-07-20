from scanner.config import now_beijing

from scanner.config import (
    MA_BULL_EXTRA_BONUS,
    MAX_MOMENTUM_TODAY_PCT,
    MAX_NEW_FACE_TODAY_PCT,
    PULLBACK_MAX_TODAY_PCT,
    SHORT_TERM_MIN_TODAY_PCT,
    SHORT_TERM_MAX_TODAY_PCT,
    ST_SMALL_CAP,
    ST_MID_CAP,
    ST_DIVERGE_UPPER_SHADOW,
    ST_DIVERGE_CLOSE_WEAK,
    ST_BOMB_HIGH,
    ST_BOMB_CLOSE,
    MOMENTUM_WEIGHTS,
    NEW_FACE_WEIGHTS,
    PULLBACK_WEIGHTS,
    SHORT_TERM_WEIGHTS,
    ST_OVERBOUGHT_BOLL, ST_OVERBOUGHT_BOLL_PENALTY,
    ST_OVERBOUGHT_KDJ, ST_OVERBOUGHT_KDJ_PENALTY,
    PULLBACK_20D_GAIN_WARN, PULLBACK_20D_GAIN_EXTREME,
    PULLBACK_20D_WARN_PENALTY, PULLBACK_20D_EXTREME_PENALTY,
    PULLBACK_VOL_LOW, PULLBACK_VOL_HEALTHY, PULLBACK_VOL_HIGH,
    VOL_PEAK_LOOKBACK,
    VOL_PEAK_MOMENTUM_WARN,
    VOL_PEAK_NEW_FACE_MIN,
    VOL_PEAK_NEW_FACE_PENALTY,
    VOL_PEAK_MOMENTUM_PENALTY,
    VOL_PEAK_PULLBACK_CONFIRM,
    VOL_PEAK_PULLBACK_BONUS,
    VOL_RANK_HIGH_ACCUM_OVERLAP_MIN_RANK,
    VOL_RANK_HIGH_ACCUM_OVERLAP_MIN_ACCUM,
    VOL_RANK_HIGH_ACCUM_OVERLAP_PENALTY,
    VOL_RANK_MEDIUM_PTS,
    VOL_RANK_MEDIUM_RC,
    VOL_RANK_STRONG_PTS,
    VOL_RANK_STRONG_RC,
    VOL_RANK_VOL_THRESHOLD,
    VOL_RANK_WEAK_PTS,
    VOL_RANK_WEAK_RC,
    WEAK_FORM_MIN_DOWN_DAYS,
    WEAK_FORM_MAX_ACCUM,
    WEAK_FORM_MIN_ACCUM,
    WEAK_FORM_MAX_TODAY_PCT,
    WEAK_FORM_CRASH_THRESHOLD,
    GAP_UP_STRONG,
    GAP_UP_MEDIUM,
    GAP_UP_WEAK,
    GAP_UP_STRONG_PTS,
    GAP_UP_MEDIUM_PTS,
    GAP_UP_WEAK_PTS,
    BOTTOM_MAX_LOSS,
    BOTTOM_VOL_SURGE,
    BOTTOM_NEAR_LOW_PCT,
    CRASH_THRESHOLD,
    RECENT_2_RETURN_THRESHOLD,
    NO_CRASH_SAFE_BONUS,
    RECENT_2D_BONUS,
    MOMENTUM_VOL_HEALTHY_MIN,
    MOMENTUM_VOL_HEALTHY_MAX,
    MA_BULL_3_TIER_SCORE,
    MA_BULL_2_TIER_SCORE,
    MA_BEAR_SCORE,
)
from scanner.indicators import (
    compute_adx, compute_atr, compute_bollinger_bands, compute_kdj,
    compute_macd, compute_ma, compute_obv, compute_rsi,
)
from scanner.models import KlineSummary, StockInfo
from scanner.patterns import (
    detect_momentum_patterns,
    detect_new_face_patterns,
    detect_pullback_patterns,
    detect_short_term_patterns,
)


def _ma_bull_score(closes: list[float]) -> int:
    # 使用 EMA（从最近 N 根收盘价播种），创业板高波动下比 SMA 噪声更小。
    # 注意：与 compute_macd 内部 EMA（从 closes[0] 播种）并非同一序列，
    # 此处仅用于 MA 多头结构判定，与 MACD 指标分属不同用途。
    if len(closes) < 10:
        return 0
    ma5 = compute_ma(closes, 5, ema=True)
    ma10 = compute_ma(closes, 10, ema=True)
    if ma5 is None or ma10 is None:
        return 0
    if len(closes) >= 20:
        ma20 = compute_ma(closes, 20, ema=True)
        if ma20 is not None and ma5 > ma10 > ma20:
            return MA_BULL_3_TIER_SCORE
    if ma5 > ma10:
        return MA_BULL_2_TIER_SCORE
    return MA_BEAR_SCORE


def _detect_gap_up(today_current: float, kline: list[dict], today_str: str | None = None) -> tuple[float, int]:
    yesterday_close = None
    today_str = today_str or now_beijing().date().isoformat()
    for k in reversed(kline):
        if k["date"] != today_str:
            yesterday_close = k["close"]
            break
    if yesterday_close is None or yesterday_close <= 0:
        return 0.0, 0
    gap_pct = (today_current - yesterday_close) / yesterday_close * 100
    if gap_pct > GAP_UP_STRONG:
        return round(gap_pct, 2), GAP_UP_STRONG_PTS
    if gap_pct > GAP_UP_MEDIUM:
        return round(gap_pct, 2), GAP_UP_MEDIUM_PTS
    if gap_pct > GAP_UP_WEAK:
        return round(gap_pct, 2), GAP_UP_WEAK_PTS
    return round(gap_pct, 2), 0


def _vol_rank_combo_score(vol_ratio: float, rank_change: int) -> int:
    if vol_ratio > VOL_RANK_VOL_THRESHOLD and rank_change >= VOL_RANK_STRONG_RC:
        return VOL_RANK_STRONG_PTS
    if vol_ratio > VOL_RANK_VOL_THRESHOLD and rank_change >= VOL_RANK_MEDIUM_RC:
        return VOL_RANK_MEDIUM_PTS
    if vol_ratio > VOL_RANK_VOL_THRESHOLD and rank_change >= VOL_RANK_WEAK_RC:
        return VOL_RANK_WEAK_PTS
    return 0


def _vol_peak_ratio(volumes: list[float], lookback: int = VOL_PEAK_LOOKBACK) -> float:
    window = volumes[-lookback:] if len(volumes) >= lookback else volumes
    peak = max(window)
    return volumes[-1] / peak if peak > 0 else 1.0


def _score_today_pct(today_pct: float, W: dict, prefix: str) -> tuple[int, str, int]:
    # 调用方已显式处理 today_pct >= 6（走 today_pct_6_8 / today_pct_gt_8），
    # 故本函数仅覆盖 < 6 区间，不存在对未定义权重键 today_pct_6_7 / today_pct_7_12 的引用。
    if today_pct < 0.5:
        return W["today_pct_lt_0_5"], f"{prefix}_today_pct", W["today_pct_lt_0_5"]
    elif today_pct < 1:
        return W["today_pct_0_5_1"], f"{prefix}_today_pct", W["today_pct_0_5_1"]
    elif today_pct < 2:
        return W["today_pct_1_2"], f"{prefix}_today_pct", W["today_pct_1_2"]
    else:  # 2 <= today_pct < 6
        return W["today_pct_2_6"], f"{prefix}_today_pct", W["today_pct_2_6"]



def _compute_new_face_indicators(closes: list[float], historical_kline: list[dict],
                                 W: dict) -> tuple[int, dict]:
    """New face specific indicator scoring (oversold reversal signals)."""
    rsi_val = compute_rsi(closes, period=6)
    rsi14_val = compute_rsi(closes, period=14)
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
    # RSI(14) 仅作"超卖但未触发 RSI(6) 极端"的补充确认，避免与 RSI(6) 共线放大超卖信号。
    if rsi14_val is not None and rsi14_val < 30 and not (rsi_val is not None and rsi_val < 30):
        bonus += W["rsi14_oversold_bonus"]
        dims["new_face_rsi14"] = round(rsi14_val, 1)
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

    boll = compute_bollinger_bands(closes)
    if boll is not None and boll["b_pct"] < 0:
        bonus += W["bollinger_oversold"]
        dims["new_face_bollinger"] = round(boll["b_pct"], 2)

    # ATR/OBV 增量确认：低波动蓄势 + OBV 未转负（底背离资金吸筹）
    highs = [k["high"] for k in historical_kline]
    lows = [k["low"] for k in historical_kline]
    volumes = [k["volume"] for k in historical_kline]
    atr = compute_atr(highs, lows, closes, period=14)
    if atr is not None and closes:
        atr_pct = atr / closes[-1] * 100
        dims["new_face_atr_pct"] = round(atr_pct, 2)
        if atr_pct < 3:
            bonus += W["atr_contraction"]
    obv = compute_obv(closes, volumes)
    if obv is not None:
        dims["new_face_obv_trend"] = obv["obv_trend"]
        if obv["obv_trend"] >= 0:
            bonus += W["obv_not_negative"]

    return bonus, dims


def _compute_momentum_indicators(closes: list[float], historical_kline: list[dict],
                                 W: dict) -> tuple[int, dict]:
    """Momentum specific indicator scoring (trend confirmation signals)."""
    rsi_val = compute_rsi(closes, period=6)
    kdj_val = compute_kdj([k["high"] for k in historical_kline],
                          [k["low"] for k in historical_kline], closes)
    macd_val = compute_macd(closes)
    adx_val = compute_adx(
        [k["high"] for k in historical_kline],
        [k["low"] for k in historical_kline], closes,
    )

    bonus = 0
    dims: dict[str, float] = {}

    if rsi_val is not None:
        if 50 <= rsi_val <= 70:
            bonus += W["rsi_bonus"]
        elif rsi_val > 85:
            bonus -= W["rsi_bonus"] * 3
        elif rsi_val > 80:
            bonus -= W["rsi_bonus"] * 2
        elif rsi_val > 70:
            bonus -= W["rsi_bonus"]
        dims["momentum_rsi"] = round(rsi_val, 1)
    if kdj_val is not None:
        if kdj_val["J"] > 100:
            bonus -= W["kdj_bonus"] * 2
        elif kdj_val["J"] < 20:
            bonus += W["kdj_bonus"]
        elif kdj_val["K"] > kdj_val["D"]:
            bonus += W["kdj_bonus"] // 2
        dims["momentum_kdj"] = round(kdj_val["J"], 1)
    if macd_val is not None:
        if macd_val["histogram"] > 0:
            bonus += W["macd_bonus"]
        if macd_val["histogram"] > 0 and macd_val["histogram"] > macd_val["histogram_prev"]:
            bonus += W["macd_bonus"]
        elif macd_val["histogram"] > 0 and macd_val["histogram"] < macd_val["histogram_prev"]:
            bonus -= W["macd_bonus"]
        dims["momentum_macd"] = round(macd_val["histogram"], 4)
    if adx_val is not None:
        if adx_val["adx"] > 25:
            bonus += W["adx_bonus"]
            dims["momentum_adx"] = W["adx_bonus"]
        elif adx_val["adx"] < 20:
            bonus += W["adx_weak"]
            dims["momentum_adx"] = W["adx_weak"]

    # ATR/OBV 增量确认：波动率适中=趋势健康，过高=过热；OBV 上行=趋势资金确认
    highs = [k["high"] for k in historical_kline]
    lows = [k["low"] for k in historical_kline]
    volumes = [k["volume"] for k in historical_kline]
    atr = compute_atr(highs, lows, closes, period=14)
    if atr is not None and closes:
        atr_pct = atr / closes[-1] * 100
        dims["momentum_atr_pct"] = round(atr_pct, 2)
        if 2 <= atr_pct <= 6:
            bonus += W["atr_healthy"]
        elif atr_pct > 10:
            bonus += W["atr_overheated"]
    obv = compute_obv(closes, volumes)
    if obv is not None and obv["obv_trend"] == 1:
        bonus += W["obv_uptrend"]
        dims["momentum_obv_trend"] = obv["obv_trend"]

    return bonus, dims


def _momentum_overbought_penalty(closes: list[float], historical_kline: list[dict]
                                  ) -> tuple[int, dict]:
    """动量末周期超买软惩罚（鱼尾段）。

    与 short_term 同构但更温和：momentum 是趋势延续策略，高 BOLL%/J 部分属
    正常强势特征，故此处仅做软惩罚、不硬否决（硬门禁留给 validator 的标记/压制）。
    注意：用历史 closes（不含今日），与 short_term 分析侧口径一致；
    validator 侧会再 append stock.current 做更完整的超买判定。
    """
    pen = 0
    dims: dict[str, float] = {}

    if len(closes) >= 20:
        boll = compute_bollinger_bands(closes)
        if boll is not None and boll["b_pct"] > ST_OVERBOUGHT_BOLL:
            pen += ST_OVERBOUGHT_BOLL_PENALTY
            dims["mo_overbought_boll"] = round(boll["b_pct"], 2)

    if len(closes) >= 9:
        kdj_val = compute_kdj(
            [k["high"] for k in historical_kline],
            [k["low"] for k in historical_kline],
            closes,
        )
        if kdj_val is not None and kdj_val["J"] > ST_OVERBOUGHT_KDJ:
            pen += ST_OVERBOUGHT_KDJ_PENALTY
            dims["mo_overbought_kdj"] = round(kdj_val["J"], 1)

    if len(closes) >= 21:
        gain_20d = (closes[-1] - closes[-21]) / closes[-21] * 100
        if gain_20d > PULLBACK_20D_GAIN_EXTREME:
            pen += PULLBACK_20D_EXTREME_PENALTY
            dims["mo_overbought_20d"] = round(gain_20d, 1)
        elif gain_20d > PULLBACK_20D_GAIN_WARN:
            pen += PULLBACK_20D_WARN_PENALTY
            dims["mo_overbought_20d"] = round(gain_20d, 1)

    return pen, dims


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

    today_str = today_str or now_beijing().date().isoformat()
    historical_kline = [k for k in kline if k["date"] != today_str]
    pcts = [k["percent"] for k in historical_kline]
    closes = [k["close"] for k in historical_kline]

    recent_5_pcts = pcts[-5:]
    down_days = sum(1 for p in recent_5_pcts if p < 0)
    has_crash_day = any(p <= WEAK_FORM_CRASH_THRESHOLD for p in recent_5_pcts)
    sum_5 = sum(recent_5_pcts)
    if (not has_crash_day
            and down_days >= WEAK_FORM_MIN_DOWN_DAYS
            and WEAK_FORM_MIN_ACCUM < sum_5 <= WEAK_FORM_MAX_ACCUM
            and today_pct < WEAK_FORM_MAX_TODAY_PCT):
        return None

    if len(closes) >= 6:
        accumulated = (closes[-1] - closes[-6]) / closes[-6] * 100
    else:
        accumulated = sum(pcts[-5:])
    if accumulated < -8:
        return None
    if accumulated > 20:
        return None

    volumes = [k["volume"] for k in kline]
    vol_window = volumes[-11:-1] if len(volumes) >= 11 else volumes[:-1]
    avg_vol = sum(vol_window) / max(len(vol_window), 1)
    today_vol = volumes[-1] if volumes else 0
    vol_ratio = today_vol / avg_vol if avg_vol > 0 else 1.0
    vol_ratio = round(vol_ratio, 2)
    recent_3_pcts = pcts[-3:] if len(pcts) >= 3 else pcts
    no_heavy_loss = all(p > BOTTOM_MAX_LOSS for p in recent_3_pcts)
    volume_surge = vol_ratio > BOTTOM_VOL_SURGE
    near_20d_low = (closes[-1] - min(closes[-20:])) / max(min(closes[-20:]), 0.01) < BOTTOM_NEAR_LOW_PCT if len(closes) >= 20 else False
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

    # 今日涨幅 >= 6% 由显式分支处理（对齐 STRATEGY.md：6~8%→+5、>8%→-15），
    # 不进入 _score_today_pct，避免其 today_pct_6_7 / today_pct_7_12 分支被覆盖却仍被读取。
    if today_pct >= 6:
        if today_pct > 8:
            today_score = W["today_pct_gt_8"]
        else:
            today_score = W["today_pct_6_8"]
        today_dim_key = "new_face_today_pct"
        today_dim_val = today_score
    else:
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
    elif accumulated <= 20:
        acc_score = W["accum_15_20"]
        dims["new_face_accumulated"] = W["accum_15_20"]
    score += acc_score

    # Volume surge (additive: bottom confirmation or v-shape still get this)
    if volume_surge:
        score += W["volume_surge"]
        dims["new_face_volume"] = W["volume_surge"]

    if bottom_confirmed:
        score += W["bottom_confirmed"]
        dims["new_face_bottom"] = W["bottom_confirmed"]
    elif v_shape_reversal:
        score += W["v_shape"]
        dims["new_face_v_shape"] = W["v_shape"]

    vol_peak = _vol_peak_ratio(volumes)
    if vol_peak < VOL_PEAK_NEW_FACE_MIN:
        score += VOL_PEAK_NEW_FACE_PENALTY
        dims["new_face_vol_peak"] = round(vol_peak, 2)

    vol_rank = _vol_rank_combo_score(vol_ratio, stock.rank_change)
    score += vol_rank
    if vol_rank:
        dims["new_face_vol_rank"] = vol_rank
    if vol_rank >= VOL_RANK_HIGH_ACCUM_OVERLAP_MIN_RANK and accumulated >= VOL_RANK_HIGH_ACCUM_OVERLAP_MIN_ACCUM:
        score += VOL_RANK_HIGH_ACCUM_OVERLAP_PENALTY

    # new_face_gap_up 已清零：回测 IC=-0.180（n=136），高开在 new_face 次日多为
    # 冲高回落，不再对 new_face 加高开加分。momentum 侧保留（小样本另行评估）。
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

    pattern_score, pattern_dims = detect_new_face_patterns(historical_kline)
    score += pattern_score
    dims.update(pattern_dims)

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

    today_str = today_str or now_beijing().date().isoformat()
    historical_kline = [k for k in kline if k["date"] != today_str]
    pcts = [k["percent"] for k in historical_kline]
    closes = [k["close"] for k in historical_kline]
    if len(closes) >= 6:
        accumulated = (closes[-1] - closes[-6]) / closes[-6] * 100
    else:
        accumulated = sum(pcts[-5:])

    if accumulated < 10:
        return None

    volumes = [k["volume"] for k in kline]
    vol_window = volumes[-11:-1] if len(volumes) >= 11 else volumes[:-1]
    avg_vol = sum(vol_window) / max(len(vol_window), 1)
    today_vol = volumes[-1] if volumes else 0
    vol_ratio = today_vol / avg_vol if avg_vol > 0 else 1.0
    vol_ratio = round(vol_ratio, 2)

    score = 0
    dims: dict[str, int | float] = {}

    if today_pct > MAX_MOMENTUM_TODAY_PCT:
        return None

    # 今日涨幅 >= 6% 由显式分支处理（对齐 STRATEGY.md：6~8%→+5；>8% 已被上游门限跳过），
    # 不进入 _score_today_pct，避免其 today_pct_6_7 / today_pct_7_12 分支被覆盖却仍被读取。
    if today_pct >= 6:
        today_score = W["today_pct_6_8"]
        today_dim_key = "momentum_today_pct"
        today_dim_val = today_score
    else:
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
    if MOMENTUM_VOL_HEALTHY_MIN < vol_ratio < MOMENTUM_VOL_HEALTHY_MAX:
        score += W["vol_healthy"]
        dims["momentum_volume"] = W["vol_healthy"]
    elif vol_ratio >= MOMENTUM_VOL_HEALTHY_MAX:
        score += W["vol_surge"]
        dims["momentum_volume"] = W["vol_surge"]
    elif vol_ratio < MOMENTUM_VOL_HEALTHY_MIN:
        score += W["vol_low"]
        dims["momentum_volume"] = W["vol_low"]

    vol_peak = _vol_peak_ratio(volumes)
    if vol_peak < VOL_PEAK_MOMENTUM_WARN:
        score += VOL_PEAK_MOMENTUM_PENALTY
        dims["momentum_vol_peak"] = round(vol_peak, 2)

    # Crash check — split: base safety + recent 2d bonus
    if len(pcts) >= 2:
        has_crash_day = any(p <= CRASH_THRESHOLD for p in pcts[-5:])
        recent_2_return = pcts[-2] + pcts[-1]
    else:
        has_crash_day = False
        recent_2_return = 0.0
    if not has_crash_day:
        score += NO_CRASH_SAFE_BONUS
        dims["momentum_no_crash_safe"] = NO_CRASH_SAFE_BONUS
        if recent_2_return > RECENT_2_RETURN_THRESHOLD:
            score += RECENT_2D_BONUS
            dims["momentum_recent_2d"] = RECENT_2D_BONUS

    vol_rank = _vol_rank_combo_score(vol_ratio, stock.rank_change)
    score += vol_rank
    if vol_rank:
        dims["momentum_vol_rank"] = vol_rank

    gap_pct, gap_pts = _detect_gap_up(stock.current, kline, today_str)
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

    pattern_score, pattern_dims = detect_momentum_patterns(historical_kline)
    score += pattern_score
    dims.update(pattern_dims)

    # 末周期超买软惩罚（鱼尾段）：与 short_term 同构但更温和——momentum 是趋势
    # 延续策略，高 BOLL%/J 部分属正常强势，故仅软惩罚、不硬否决；validator 侧
    # 再做标记 + 验证压制（仍保留 passed 门禁，不挡正常主升浪）。
    ob_pen, ob_dims = _momentum_overbought_penalty(closes, historical_kline)
    score += ob_pen
    dims.update(ob_dims)
    if ob_pen:
        dims["mo_overbought_penalty"] = ob_pen

    return KlineSummary(trend=trend, accumulated_pct=round(accumulated, 2),
                        volume_ratio=round(vol_ratio, 2), bottom_confirmed=not has_crash_day,
                        score=score, dimensions=dims, avg_volume=round(avg_vol, 2))


def _calc_pullback_base_metrics(kline: list[dict], today_str: str) -> tuple[list, list, float, float, float, list, list]:
    """Calculate base metrics for pullback analysis.

    Returns:
        (pcts, closes, accumulated, vol_ratio, avg_vol, historical_kline, volumes) tuple
    """
    today_str = today_str or now_beijing().date().isoformat()
    historical_kline = [k for k in kline if k["date"] != today_str]
    pcts = [k["percent"] for k in historical_kline]
    closes = [k["close"] for k in historical_kline]
    if len(closes) >= 6:
        accumulated = (closes[-1] - closes[-6]) / closes[-6] * 100
    else:
        accumulated = sum(pcts[-5:])

    volumes = [k["volume"] for k in kline]
    vol_window = volumes[-11:-1] if len(volumes) >= 11 else volumes[:-1]
    avg_vol = sum(vol_window) / max(len(vol_window), 1)
    today_vol = volumes[-1] if volumes else 0
    vol_ratio = today_vol / avg_vol if avg_vol > 0 else 1.0

    return pcts, closes, accumulated, vol_ratio, avg_vol, historical_kline, volumes


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

    if vol_ratio < PULLBACK_VOL_LOW:
        score += W["vol_low"]
        dims["pullback_volume"] = W["vol_low"]
    elif vol_ratio <= PULLBACK_VOL_HEALTHY:
        score += W["vol_healthy"]
        dims["pullback_volume"] = W["vol_healthy"]
    elif vol_ratio > PULLBACK_VOL_HIGH:
        score += W["vol_surge"]
        dims["pullback_volume"] = W["vol_surge"]
    else:
        # PULLBACK_VOL_HEALTHY < vol_ratio <= PULLBACK_VOL_HIGH: 中性量能
        dims["pullback_volume"] = 0

    return score, dims


def _check_crash_day(pcts: list) -> tuple[bool, int, dict]:
    """Check crash day + recent 2d return for pullback.

    Returns:
        (has_crash_day, score, dimensions)
    """
    has_crash_day = any(p <= CRASH_THRESHOLD for p in pcts[-5:])
    score = 0
    dims: dict[str, int | float] = {}

    if not has_crash_day:
        score += NO_CRASH_SAFE_BONUS
        dims["pullback_no_crash_safe"] = NO_CRASH_SAFE_BONUS
        if len(pcts) >= 2:
            recent_2_return = pcts[-2] + pcts[-1]
            if recent_2_return > RECENT_2_RETURN_THRESHOLD:
                score += RECENT_2D_BONUS
                dims["pullback_recent_2d"] = RECENT_2D_BONUS

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
        pct_from_ma10 = (current_close - result["ma10"]) / max(result["ma10"], 0.01) * 100
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
            result["ma_bull_extra"] = MA_BULL_EXTRA_BONUS
            result["score"] += result["ma_bull_extra"]
            result["dimensions"]["pullback_ma_bull"] = result["ma_bull_extra"]
    elif len(closes) >= 10:
        if result["ma_support"] and result["ma10"] is not None:
            ma5 = sum(closes[-5:]) / 5
            if ma5 > result["ma10"]:
                result["ma_bull_extra"] = MA_BULL_EXTRA_BONUS
                result["score"] += result["ma_bull_extra"]
                result["dimensions"]["pullback_ma_bull"] = result["ma_bull_extra"]

    if not result["ma_support"] and not result["ma_broken"] and result["ma_bull_extra"] == 0:
        result["dimensions"]["pullback_ma_support"] = 0

    return result


def _score_pullback_indicators(closes: list, historical_kline: list[dict],
                               W: dict) -> tuple[int, dict]:
    """Score based on technical indicators (RSI, MACD, KDJ, Bollinger).

    Returns:
        (score, dimensions) tuple
    """
    score = 0
    dims: dict[str, int | float] = {}

    rsi_val = compute_rsi(closes, period=6)
    macd_val = compute_macd(closes)
    kdj_val = compute_kdj(
        [k["high"] for k in historical_kline],
        [k["low"] for k in historical_kline], closes,
    )

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

    if kdj_val is not None and kdj_val["J"] < 0:
        score += W["kdj_bonus"]
        dims["pullback_kdj"] = round(kdj_val["J"], 1)

    boll = compute_bollinger_bands(closes)
    if boll is not None:
        dist_to_mid = abs(closes[-1] - boll["middle"]) / max(boll["middle"], 0.01) * 100
        if dist_to_mid < 0.5:
            score += W["bollinger_mid_support"]
            dims["pullback_bollinger"] = round(boll["middle"], 2)
        elif boll["b_pct"] < 0.05:
            dims["pullback_bollinger"] = round(boll["lower"], 2)
        else:
            dims["pullback_bollinger"] = round(boll["b_pct"], 2)

    return score, dims


def _pullback_20day_gain_penalty(closes: list) -> tuple[int, str]:
    """20日累计涨幅过大 → pullback 高风险惩罚（生命周期保护）"""
    if len(closes) < 21:
        return 0, "data_short"
    gain_20d = (closes[-1] - closes[-21]) / closes[-21] * 100
    if gain_20d > PULLBACK_20D_GAIN_EXTREME:
        return PULLBACK_20D_EXTREME_PENALTY, f"20d_gain_{gain_20d:.0f}%_extreme"
    if gain_20d > PULLBACK_20D_GAIN_WARN:
        return PULLBACK_20D_WARN_PENALTY, f"20d_gain_{gain_20d:.0f}%_warn"
    return 0, f"20d_gain_{gain_20d:.0f}%_ok"


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
    if today_pct <= -8 or today_pct > PULLBACK_MAX_TODAY_PCT:
        return None

    pcts, closes, accumulated, vol_ratio, avg_vol, historical_kline, volumes = _calc_pullback_base_metrics(kline, today_str)

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

    vol_peak = _vol_peak_ratio(volumes)
    if vol_peak < VOL_PEAK_PULLBACK_CONFIRM:
        score += VOL_PEAK_PULLBACK_BONUS
        dims["pullback_vol_peak"] = round(vol_peak, 2)

    has_crash_day, crash_score, crash_dims = _check_crash_day(pcts)
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

    indicator_score, indicator_dims = _score_pullback_indicators(closes, historical_kline, W)
    score += indicator_score
    dims.update(indicator_dims)

    pattern_score, pattern_dims = detect_pullback_patterns(historical_kline, vol_ratio)
    score += pattern_score
    dims.update(pattern_dims)

    gain_penalty, gain_detail = _pullback_20day_gain_penalty(closes)
    score += gain_penalty
    if gain_penalty:
        dims["pullback_20d_gain"] = gain_penalty
        dims["pullback_20d_gain_detail"] = gain_detail

    trend = _classify_pullback_trend(ma_result["ma_support"], ma_result["ma_broken"], today_pct)

    return KlineSummary(trend=trend, accumulated_pct=round(accumulated, 2),
                        volume_ratio=round(vol_ratio, 2), bottom_confirmed=not has_crash_day,
                        score=score, dimensions=dims, avg_volume=round(avg_vol, 2))


def analyze_short_term(stock: StockInfo, kline: list[dict] | None,
                       today_str: str | None = None) -> KlineSummary | None:
    if not kline or len(kline) < 5:
        return None

    W = SHORT_TERM_WEIGHTS

    today_pct = stock.percent
    if today_pct < SHORT_TERM_MIN_TODAY_PCT or today_pct > SHORT_TERM_MAX_TODAY_PCT:
        return None

    today_str = today_str or now_beijing().date().isoformat()
    historical_kline = [k for k in kline if k["date"] != today_str]
    pcts = [k["percent"] for k in historical_kline]
    closes = [k["close"] for k in historical_kline]

    if len(closes) >= 6:
        accumulated = (closes[-1] - closes[-6]) / closes[-6] * 100
    else:
        accumulated = sum(pcts[-5:])

    volumes = [k["volume"] for k in kline]
    vol_window = volumes[-11:-1] if len(volumes) >= 11 else volumes[:-1]
    avg_vol = sum(vol_window) / max(len(vol_window), 1)
    today_vol = volumes[-1] if volumes else 0
    vol_ratio = today_vol / avg_vol if avg_vol > 0 else 1.0
    vol_ratio = round(vol_ratio, 2)

    score = 0
    dims: dict[str, int | float] = {}

    if today_pct >= 6:
        score += W["today_pct_6_8"]
        dims["st_today_pct"] = W["today_pct_6_8"]
    elif today_pct >= 4:
        score += W["today_pct_4_6"]
        dims["st_today_pct"] = W["today_pct_4_6"]
    else:
        score += W["today_pct_2_4"]
        dims["st_today_pct"] = W["today_pct_2_4"]

    if accumulated < 0:
        score += W["accum_lt_0"]
        dims["st_accumulated"] = W["accum_lt_0"]
    elif accumulated >= 20:
        score += W["accum_gte_20"]
        dims["st_accumulated"] = W["accum_gte_20"]
    elif accumulated >= 15:
        score += W["accum_15_20"]
        dims["st_accumulated"] = W["accum_15_20"]
    elif accumulated >= 10:
        score += W["accum_10_15"]
        dims["st_accumulated"] = W["accum_10_15"]
    elif accumulated >= 5:
        score += W["accum_5_10"]
        dims["st_accumulated"] = W["accum_5_10"]

    if vol_ratio >= 1.5:
        score += W["vol_surge"]
        dims["st_volume"] = W["vol_surge"]
    elif vol_ratio >= 1.0:
        score += W["vol_healthy"]
        dims["st_volume"] = W["vol_healthy"]
    else:
        score += W["vol_low"]
        dims["st_volume"] = W["vol_low"]

    has_crash = any(p <= -10 for p in pcts[-5:])
    if not has_crash:
        score += NO_CRASH_SAFE_BONUS
        dims["st_no_crash_safe"] = NO_CRASH_SAFE_BONUS
        if len(pcts) >= 2:
            recent_2_return = pcts[-2] + pcts[-1]
            if recent_2_return > RECENT_2_RETURN_THRESHOLD:
                score += RECENT_2D_BONUS
                dims["st_recent_2d"] = RECENT_2D_BONUS

    # 超短偏好小市值：流通盘轻、拉升阻力小（market_cap 单位：亿元，流通市值优先）
    if stock.market_cap > 0:
        if stock.market_cap <= ST_SMALL_CAP:
            score += W["value_small_cap"]
            dims["st_value_small"] = W["value_small_cap"]
        elif stock.market_cap <= ST_MID_CAP:
            score += W["value_mid_cap"]
            dims["st_value_mid"] = W["value_mid_cap"]
        # 超大市值(>ST_MID_CAP)超短弹性差，不加分
    # market_cap<=0 视为未知，不加分

    rank = stock.rank
    if rank <= 10:
        score += W["rank_top10"]
        dims["st_rank"] = W["rank_top10"]
    elif rank <= 20:
        score += W["rank_top20"]
        dims["st_rank"] = W["rank_top20"]
    elif rank <= 30:
        score += W["rank_top30"]
        dims["st_rank"] = W["rank_top30"]

    rsi_val = compute_rsi(closes, period=6)
    if rsi_val is not None:
        if 50 <= rsi_val < 70:
            score += W["rsi_bonus"]
            dims["st_rsi"] = round(rsi_val, 1)
        elif rsi_val > 80:
            score -= W["rsi_bonus"]

    kdj_val = compute_kdj(
        [k["high"] for k in historical_kline],
        [k["low"] for k in historical_kline],
        closes,
    )
    if kdj_val is not None:
        if kdj_val["K"] > kdj_val["D"] and 50 <= kdj_val["K"] <= 80 and kdj_val["J"] < 100:
            score += W["kdj_bonus"]
            dims["st_kdj"] = round(kdj_val["J"], 1)

    macd_val = compute_macd(closes)
    if macd_val is not None:
        if macd_val["histogram"] > 0:
            score += W["macd_bonus"]
            dims["st_macd"] = round(macd_val["histogram"], 4)

    # 末周期超买防护（鱼尾段）：软惩罚，幅度克制以免单独把正常弱转强压到 <15。
    # 真正的硬性否决由 validator 承担（超买时弱转强需非-sector 维度达标）。
    ob_pen = 0
    if len(closes) >= 20:
        boll_ob = compute_bollinger_bands(closes)
        if boll_ob is not None and boll_ob["b_pct"] > ST_OVERBOUGHT_BOLL:
            ob_pen += ST_OVERBOUGHT_BOLL_PENALTY
            dims["st_overbought_boll"] = round(boll_ob["b_pct"], 2)
    if kdj_val is not None and kdj_val["J"] > ST_OVERBOUGHT_KDJ:
        ob_pen += ST_OVERBOUGHT_KDJ_PENALTY
        dims["st_overbought_kdj"] = round(kdj_val["J"], 1)
    if len(closes) >= 21:
        gain_20d = (closes[-1] - closes[-21]) / closes[-21] * 100
        if gain_20d > PULLBACK_20D_GAIN_EXTREME:
            ob_pen += PULLBACK_20D_EXTREME_PENALTY
            dims["st_overbought_20d"] = round(gain_20d, 1)
        elif gain_20d > PULLBACK_20D_GAIN_WARN:
            ob_pen += PULLBACK_20D_WARN_PENALTY
            dims["st_overbought_20d"] = round(gain_20d, 1)
    score += ob_pen
    if ob_pen:
        dims["st_overbought_penalty"] = ob_pen

    pattern_score, pattern_dims = detect_short_term_patterns(historical_kline)
    score += pattern_score
    dims.update(pattern_dims)

    # 弱转强（分歧转一致）：昨日大分歧/烂板/炸板 + 今日在 2~8% 内转强
    yest_divergence = False
    if len(historical_kline) >= 2:
        yest = historical_kline[-1]
        yo = yest.get("open", 0)
        yh = yest.get("high", 0)
        yc = yest.get("close", 0)
        if yc > 0:
            upper_shadow = (yh - max(yo, yc)) / yc
            close_to_high = yc / yh - 1 if yh > 0 else 0
            yest_divergence = (upper_shadow > ST_DIVERGE_UPPER_SHADOW
                                and close_to_high < ST_DIVERGE_CLOSE_WEAK)
            prev_close = historical_kline[-2].get("close", 0)
            if prev_close > 0 and (yh / prev_close - 1) >= ST_BOMB_HIGH and (yc / prev_close - 1) < ST_BOMB_CLOSE:
                yest_divergence = True  # 曾触板但收盘大回落 = 炸板/烂板
    gap_pct, gap_pts = _detect_gap_up(stock.current, kline, today_str)
    if yest_divergence:
        score += W["st_weak_to_strong"]
        dims["st_weak_to_strong"] = W["st_weak_to_strong"]
        if gap_pts > 0:
            score += W["st_wts_gap"]
            dims["st_wts_gap"] = W["st_wts_gap"]
        trend = "弱转强"
    else:
        trend = "放量启动" if vol_ratio > 1.3 else "温和放量" if vol_ratio > 1.0 else "缩量"

    return KlineSummary(trend=trend, accumulated_pct=round(accumulated, 2),
                        volume_ratio=round(vol_ratio, 2), bottom_confirmed=False,
                        score=score, dimensions=dims, avg_volume=round(avg_vol, 2))
