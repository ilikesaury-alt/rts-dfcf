from scanner.config import (
    BOTTOM_MAX_LOSS,
    BOTTOM_NEAR_LOW_PCT,
    BOTTOM_VOL_SURGE,
    COMEBACK_MAX_TODAY_PCT,
    COMEBACK_MIN_TODAY_PCT,
    CRASH_THRESHOLD,
    GAP_UP_MEDIUM,
    GAP_UP_MEDIUM_PTS,
    GAP_UP_STRONG,
    GAP_UP_STRONG_PTS,
    GAP_UP_WEAK,
    GAP_UP_WEAK_PTS,
    MA_BEAR_SCORE,
    MA_BULL_2_TIER_SCORE,
    MA_BULL_3_TIER_SCORE,
    MAX_MOMENTUM_TODAY_PCT,
    MAX_NEW_FACE_TODAY_PCT,
    MOMENTUM_LAUNCH_ACCUM_MAX,
    MOMENTUM_LAUNCH_ACCUM_MIN,
    MOMENTUM_LAUNCH_TODAY_MAX,
    MOMENTUM_LAUNCH_TODAY_MIN,
    MOMENTUM_LAUNCH_VOL,
    MOMENTUM_LAUNCH_WORD,
    MOMENTUM_VOL_HEALTHY_MAX,
    MOMENTUM_VOL_HEALTHY_MIN,
    MOMENTUM_WEIGHTS,
    NEW_FACE_WEIGHTS,
    NO_CRASH_SAFE_BONUS,
    REBOUND_5D_DROP_THRESHOLD,
    REBOUND_CRASH_THRESHOLD,
    REBOUND_MAX_TODAY_PCT,
    REBOUND_MIN_TODAY_PCT,
    REBOUND_NEAR_LOW_PCT,
    REBOUND_WEIGHTS,
    RECENT_2_RETURN_THRESHOLD,
    RECENT_2D_BONUS,
    SHORT_TERM_MAX_TODAY_PCT,
    SHORT_TERM_MIN_TODAY_PCT,
    SHORT_TERM_WEIGHTS,
    ST_BOMB_CLOSE,
    ST_BOMB_HIGH,
    ST_DIVERGE_CLOSE_WEAK,
    ST_DIVERGE_UPPER_SHADOW,
    ST_MID_CAP,
    ST_SMALL_CAP,
    VOL_PEAK_LOOKBACK,
    VOL_PEAK_MOMENTUM_PENALTY,
    VOL_PEAK_MOMENTUM_WARN,
    VOL_PEAK_NEW_FACE_MIN,
    VOL_PEAK_NEW_FACE_PENALTY,
    VOL_RANK_MEDIUM_PTS,
    VOL_RANK_MEDIUM_RC,
    VOL_RANK_STRONG_PTS,
    VOL_RANK_STRONG_RC,
    VOL_RANK_VOL_THRESHOLD,
    VOL_RANK_WEAK_PTS,
    VOL_RANK_WEAK_RC,
    WEAK_FORM_CRASH_THRESHOLD,
    WEAK_FORM_MAX_ACCUM,
    WEAK_FORM_MAX_TODAY_PCT,
    WEAK_FORM_MIN_ACCUM,
    WEAK_FORM_MIN_DOWN_DAYS,
    now_beijing,
)
from scanner.features import build_features
from scanner.indicators import compute_ma
from scanner.models import KlineBar, KlineSummary, StockInfo
from scanner.patterns import (
    detect_momentum_patterns,
    detect_new_face_patterns,
    detect_rebound_patterns,
    detect_short_term_patterns,
)
from scanner.trading_session import trading_minutes_elapsed
from scanner.validator import _mo_divergence

# 盘中把今日部分量能投影为全天量能的最大倍数（9:31 开盘瞬间量能爆表时封顶）。
# 保持 10：A股量能呈 U 型（开盘聚集），普通票前5分钟约占全天 5%，投影 5%×10=0.5
# 仍低于 short_term 1.0 硬门；若提到 24，普通票 09:35-09:54 会投影至 1.2 误入池。
# 真正爆量票（早盘速率≥2x 正常）在 cap=10 下仍可达 0.5~1.0+，不会被封顶压制。
MAX_VOL_PROJECTION = 10.0


def _project_today_vol(kline: list[KlineBar], today_str: str, now=None) -> float:
    """今日盘中部分量能投影为全天量能（与 _compute_volume_metrics 同口径）。

    无今日 bar / 已收盘 / 开盘前 → 不投影（返回末根原始量能）。
    供 _compute_volume_metrics 与 _vol_peak_ratio 共用，保证 vol_ratio 与
    vol_peak 早盘口径一致（此前 _vol_peak_ratio 用裸今日量，早盘必吃 -5/-8 惩罚）。
    """
    if not kline:
        return 0.0
    today_vol = kline[-1]["volume"]
    if kline[-1]["date"] == today_str:
        elapsed = trading_minutes_elapsed(now)
        if 0 < elapsed < 240:
            factor = min(240 / elapsed, MAX_VOL_PROJECTION)
            today_vol = today_vol * factor
    return today_vol


def _compute_volume_metrics(kline: list[KlineBar], today_str: str,
                            now=None) -> tuple[float, float]:
    """统一计算 vol_ratio 与 avg_volume（各 analyze_* 共用，分析/验证端口径一致）。

    早盘偏置修复：当末根 bar 是今日盘中部分量能时，按已交易分钟数投影为全天量能
    （today_vol * 240 / elapsed，上限 MAX_VOL_PROJECTION），避免 vol_ratio 天然 <1.0。
    - 无今日 bar（末根为昨日全天量）→ 不投影
    - 已收盘 / 开盘前（elapsed>=240 或 elapsed<=0）→ 不投影
    - 收盘后今日 bar 已是完整量能，投影倍数恒为 1.0，行为不变
    """
    volumes = [k["volume"] for k in kline]
    vol_window = volumes[-11:-1] if len(volumes) >= 11 else volumes[:-1]
    avg_vol = sum(vol_window) / max(len(vol_window), 1)
    today_vol = _project_today_vol(kline, today_str, now)

    vol_ratio = today_vol / avg_vol if avg_vol > 0 else 1.0
    return round(vol_ratio, 2), round(avg_vol, 2)


def _split_today(kline: list[KlineBar], today_str: str) -> tuple[list[KlineBar], list[float], list[float]]:
    """剔除今日 bar，返回 (historical_kline, pcts, closes)。

    各 analyze_* 共用：今日 bar 切分逻辑完全一致，抽成单点避免 5 处样板重复。
    """
    historical_kline = [k for k in kline if k["date"] != today_str]
    pcts = [k["percent"] for k in historical_kline]
    closes = [k["close"] for k in historical_kline]
    return historical_kline, pcts, closes


def _accum_incl_today(kline: list[KlineBar], today_str: str, today_pct: float) -> float:
    """5 日累计涨幅（含今日 bar，复利口径），存入 accumulated_incl_today 维度。

    用途：次日大涨 🎯 门槛 NEXTDAY_ACCUM_MIN 校准于「含推荐日」口径（含今日 bar），
    但 new_face/momentum/rebound 的 accumulated_pct 为历史口径（_split_today 剔除今日）。
    此处另算含今日值供展示层判定，消除门槛口径错位（short_term 的 accumulated_pct
    本身即此口径，维度值与之相等，统一存放）。今日 bar 缺失（旧缓存/补拉失败）时
    all_closes 末根为昨日，值回退为历史口径——与 short_term 同行为，不额外处理。
    """
    all_closes = [k["close"] for k in kline]
    if len(all_closes) >= 6:
        return (all_closes[-1] - all_closes[-6]) / all_closes[-6] * 100
    hist = [k for k in kline if k["date"] != today_str]
    pcts = [k["percent"] for k in hist]
    return sum(pcts[-5:]) + today_pct


def _get_features(closes: list[float], historical_kline: list[KlineBar],
                  features: dict | None = None) -> dict:
    """构建特征（调用方已预计算则复用，否则从 historical_kline 抽取 high/low/volume 现算）。"""
    if features is not None:
        return features
    return build_features(
        closes,
        [k["high"] for k in historical_kline],
        [k["low"] for k in historical_kline],
        [k["volume"] for k in historical_kline],
    )


def _ma_bull_score(closes: list[float], feats: dict | None = None) -> int:
    # 使用 EMA（从最近 N 根收盘价播种），创业板高波动下比 SMA 噪声更小。
    # 注意：与 compute_macd 内部 EMA（从 closes[0] 播种）并非同一序列，
    # 此处仅用于 MA 多头结构判定，与 MACD 指标分属不同用途。
    if feats is not None:
        ma5 = feats.get("ma5_ema")
        ma10 = feats.get("ma10_ema")
        ma20 = feats.get("ma20_ema")
    else:
        if len(closes) < 10:
            return 0
        ma5 = compute_ma(closes, 5, ema=True)
        ma10 = compute_ma(closes, 10, ema=True)
        ma20 = compute_ma(closes, 20, ema=True) if len(closes) >= 20 else None
    if ma5 is None or ma10 is None:
        return 0
    if ma20 is not None and ma5 > ma10 > ma20:
        return MA_BULL_3_TIER_SCORE
    if ma5 > ma10:
        return MA_BULL_2_TIER_SCORE
    return MA_BEAR_SCORE


def _detect_gap_up(today_current: float, kline: list[KlineBar], today_str: str | None = None) -> tuple[float, int]:
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


def _crash_safety_block(pcts: list[float], prefix: str) -> tuple[bool, int, dict]:
    """无暴跌日安全分 + 近2日不差附加分（momentum/short_term 共用样板）。

    分析入口均要求 len(kline)>=5（历史必>=4 根），len(pcts)<2 分支在实际中不可达，
    保留纯防御。dims key 用 prefix 区分策略（momentum_* / st_*）。
    返回 (has_crash_day, score_delta, dims)。
    """
    has_crash_day = any(p <= CRASH_THRESHOLD for p in pcts[-5:])
    score = 0
    dims: dict[str, int | float] = {}
    if not has_crash_day:
        score += NO_CRASH_SAFE_BONUS
        dims[f"{prefix}_no_crash_safe"] = NO_CRASH_SAFE_BONUS
        if len(pcts) >= 2:
            recent_2_return = pcts[-2] + pcts[-1]
            if recent_2_return > RECENT_2_RETURN_THRESHOLD:
                score += RECENT_2D_BONUS
                dims[f"{prefix}_recent_2d"] = RECENT_2D_BONUS
    return has_crash_day, score, dims


def _vol_rank_combo_score(vol_ratio: float, rank_change: int) -> int:
    if vol_ratio > VOL_RANK_VOL_THRESHOLD and rank_change >= VOL_RANK_STRONG_RC:
        return VOL_RANK_STRONG_PTS
    if vol_ratio > VOL_RANK_VOL_THRESHOLD and rank_change >= VOL_RANK_MEDIUM_RC:
        return VOL_RANK_MEDIUM_PTS
    if vol_ratio > VOL_RANK_VOL_THRESHOLD and rank_change >= VOL_RANK_WEAK_RC:
        return VOL_RANK_WEAK_PTS
    return 0


def _vol_peak_ratio(volumes: list[float], lookback: int = VOL_PEAK_LOOKBACK,
                    today_vol: float | None = None) -> float:
    """末根量能 / 近 N 根量能峰值。

    today_vol 传入时优先用投影后的今日全天量能（_project_today_vol），
    消除早盘部分量能导致 vol_peak 天然偏低、恒吃 -5/-8 惩罚的偏置。
    缺省（回测/无今日 bar 场景）回退 volumes[-1]，行为不变。
    """
    window = volumes[-lookback:] if len(volumes) >= lookback else volumes
    peak = max(window)
    last = today_vol if today_vol is not None else (volumes[-1] if volumes else 0.0)
    return last / peak if peak > 0 else 1.0


def _band_score(value: float, bands: list[tuple[float, str]], W: dict,
                default_key: str | None = None) -> int:
    """升序阶梯打分：bands 为 [(上界, 权重键), ...]（上界递增）。

    返回首个满足 value < 上界 的 W[权重键]；都不满足时返回 W[default_key] 或 0。
    用于收口各策略中"按区间取权重"的重复 if/elif 阶梯。
    """
    for upper, key in bands:
        if value < upper:
            return W[key]
    return W[default_key] if default_key is not None else 0


def _score_today_pct(today_pct: float, W: dict, prefix: str) -> tuple[int, str, int]:
    # 调用方已显式处理 today_pct >= 6（走 today_pct_6_8 / today_pct_gt_8），
    # 故本函数仅覆盖 < 6 区间，不存在对未定义权重键 today_pct_6_7 / today_pct_7_12 的引用。
    score = _band_score(
        today_pct,
        [(0.5, "today_pct_lt_0_5"), (1, "today_pct_0_5_1"), (2, "today_pct_1_2")],
        W, default_key="today_pct_2_6",
    )
    return score, f"{prefix}_today_pct", score



def _compute_new_face_indicators(closes: list[float], historical_kline: list[KlineBar],
                                 W: dict, feats: dict | None = None) -> tuple[int, dict]:
    """New face specific indicator scoring (oversold reversal signals)."""
    feats = _get_features(closes, historical_kline, feats)
    rsi_val = feats["rsi6"]
    rsi14_val = feats["rsi14"]
    kdj_val = feats.get("kdj")
    macd_val = feats["macd"]
    boll = feats["boll"]
    atr = feats.get("atr")
    obv = feats.get("obv")

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
        if kdj_val["K"] < 20:
            prev_k = kdj_val.get("prev_K")
            prev_d = kdj_val.get("prev_D")
            if prev_k is not None and prev_d is not None:
                # 金叉时刻：前日 K<=D，今日 K>D（避免金叉后持续加分）
                golden_cross = prev_k <= prev_d and kdj_val["K"] > kdj_val["D"]
            else:
                golden_cross = kdj_val["K"] > kdj_val["D"]
            if golden_cross:
                bonus += W["kdj_bonus"]
        if kdj_val["J"] < 0:
            bonus += W["kdj_bonus"]
        dims["new_face_kdj"] = round(kdj_val["J"], 1)
    if macd_val is not None:
        # MACD 金叉信号：仅 histogram 由负转正那一刻加分（底部反转确认）。
        # 不对"macd>signal 持续加分"：金叉后高位票继续加分会强化追高。
        if macd_val["histogram"] > 0 and macd_val["histogram_prev"] <= 0:
            bonus += W["macd_bonus"]
        dims["new_face_macd"] = round(macd_val["histogram"], 4)

    if boll is not None and boll["b_pct"] < 0:
        bonus += W["bollinger_oversold"]
        dims["new_face_bollinger"] = round(boll["b_pct"], 2)

    # ATR/OBV 增量确认：低波动蓄势 + OBV 未转负（底背离资金吸筹）
    if atr is not None and closes:
        atr_pct = atr / closes[-1] * 100
        dims["new_face_atr_pct"] = round(atr_pct, 2)
        if atr_pct < 3:
            bonus += W["atr_contraction"]
    if obv is not None:
        dims["new_face_obv_trend"] = obv["obv_trend"]
        if obv["obv_trend"] >= 0:
            bonus += W["obv_not_negative"]

    return bonus, dims


def _compute_momentum_indicators(closes: list[float], historical_kline: list[KlineBar],
                                 W: dict, feats: dict | None = None) -> tuple[int, dict]:
    """Momentum specific indicator scoring (trend confirmation signals)."""
    feats = _get_features(closes, historical_kline, feats)
    rsi_val = feats["rsi6"]
    kdj_val = feats.get("kdj")
    macd_val = feats["macd"]
    adx_val = feats.get("adx")
    atr = feats.get("atr")
    obv = feats.get("obv")

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
        # MACD 红柱加分逻辑：
        # 仅在红柱增长时 +3（趋势加速），红柱缩短时 -3（动能衰竭）。
        # 不对 histogram>0 无条件加分，避免顶部大红柱反而加分最多。
        if macd_val["histogram"] > 0 and macd_val["histogram"] > macd_val["histogram_prev"]:
            bonus += W["macd_bonus"]
        elif macd_val["histogram"] > 0 and macd_val["histogram"] < macd_val["histogram_prev"]:
            bonus -= W["macd_bonus"]
        dims["momentum_macd"] = round(macd_val["histogram"], 4)
    if adx_val is not None:
        if adx_val["adx"] > 25:
            bonus += W["adx_bonus"]
        elif adx_val["adx"] < 20:
            bonus += W["adx_weak"]
        dims["momentum_adx"] = round(adx_val["adx"], 1)

    # ATR/OBV 增量确认：波动率适中=趋势健康，过高=过热；OBV 上行=趋势资金确认
    if atr is not None and closes:
        atr_pct = atr / closes[-1] * 100
        dims["momentum_atr_pct"] = round(atr_pct, 2)
        if 2 <= atr_pct <= 6:
            bonus += W["atr_healthy"]
        elif atr_pct > 10:
            bonus += W["atr_overheated"]
    if obv is not None and obv["obv_trend"] == 1:
        bonus += W["obv_uptrend"]
        dims["momentum_obv_trend"] = obv["obv_trend"]

    return bonus, dims


def analyze_new_face(stock: StockInfo, kline: list[KlineBar] | None,
                     today_str: str | None = None,
                     features: dict | None = None,
                     now=None) -> KlineSummary | None:
    if not kline or len(kline) < 5:
        return None

    W = NEW_FACE_WEIGHTS

    today_pct = stock.percent

    if today_pct <= 0:
        return None

    if today_pct > MAX_NEW_FACE_TODAY_PCT:
        return None

    today_str = today_str or now_beijing().date().isoformat()
    historical_kline, pcts, closes = _split_today(kline, today_str)

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
    if accumulated < -10:
        return None
    if accumulated > 20:
        return None
    accum_incl_today = _accum_incl_today(kline, today_str, today_pct)

    feats = _get_features(closes, historical_kline, features)

    vol_ratio, avg_vol = _compute_volume_metrics(kline, today_str, now)
    volumes = [k["volume"] for k in kline]
    recent_3_pcts = pcts[-3:] if len(pcts) >= 3 else pcts
    no_heavy_loss = all(p > BOTTOM_MAX_LOSS for p in recent_3_pcts)
    volume_surge = vol_ratio > BOTTOM_VOL_SURGE
    near_20d_low = ((closes[-1] - min(closes[-20:])) / max(min(closes[-20:]), 0.01)
                    < BOTTOM_NEAR_LOW_PCT if len(closes) >= 20 else False)
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
    dims["accumulated_incl_today"] = round(accum_incl_today, 2)

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

    vol_peak = _vol_peak_ratio(volumes, today_vol=_project_today_vol(kline, today_str, now))
    if vol_peak < VOL_PEAK_NEW_FACE_MIN:
        score += VOL_PEAK_NEW_FACE_PENALTY
        dims["new_face_vol_peak"] = round(vol_peak, 2)

    vol_rank = _vol_rank_combo_score(vol_ratio, stock.rank_change)
    score += vol_rank
    if vol_rank:
        dims["new_face_vol_rank"] = vol_rank
    # VOL_RANK_HIGH_ACCUM_OVERLAP 组合惩罚已删除：单一组合场景过拟合，无回测支撑。
    # new_face_gap_up 不再对 new_face 加高开加分（高开次日多为冲高回落）。
    # momentum 侧保留 gap_up 维度（小样本另行评估）。
    if stock.value >= 10000:
        score += W["value_gte_10000"]
        dims["new_face_value"] = W["value_gte_10000"]
    elif stock.value >= 5000:
        score += W["value_gte_5000"]
        dims["new_face_value"] = W["value_gte_5000"]

    ma_bull = _ma_bull_score(closes, feats)
    score += ma_bull
    if ma_bull:
        dims["new_face_ma_bull"] = ma_bull

    indicator_bonus, indicator_dims = _compute_new_face_indicators(closes, historical_kline, W, feats)
    score += indicator_bonus
    dims.update(indicator_dims)

    pattern_score, pattern_dims = detect_new_face_patterns(historical_kline)
    score += pattern_score
    dims.update(pattern_dims)

    return KlineSummary(trend=trend, accumulated_pct=round(accumulated, 2),
                        volume_ratio=round(vol_ratio, 2), bottom_confirmed=bottom_confirmed,
                        score=score, dimensions=dims, avg_volume=round(avg_vol, 2))


def analyze_momentum(stock: StockInfo, kline: list[KlineBar] | None,
                     today_str: str | None = None,
                     features: dict | None = None,
                     now=None) -> KlineSummary | None:
    if not kline or len(kline) < 5:
        return None

    W = MOMENTUM_WEIGHTS

    today_pct = stock.percent
    if today_pct <= 0:
        return None

    today_str = today_str or now_beijing().date().isoformat()
    historical_kline, pcts, closes = _split_today(kline, today_str)
    if len(closes) >= 6:
        accumulated = (closes[-1] - closes[-6]) / closes[-6] * 100
    else:
        accumulated = sum(pcts[-5:])
    accum_incl_today = _accum_incl_today(kline, today_str, today_pct)

    feats = _get_features(closes, historical_kline, features)

    vol_ratio, avg_vol = _compute_volume_metrics(kline, today_str, now)
    volumes = [k["volume"] for k in kline]

    # ── "首次启动" 子模式 ──
    # 累计涨幅尚低（0~7%，还没跑起来）+ 今日 3.5~8% 放量启动 + MA 转多头：
    # 提前 1-2 天进 momentum 池（目标区间 4-6% 为数据最佳带：cum3d +9.46%）。
    # 缩量(vol<1.5)/MA空头/有顶背离 一律不放行，交给 validator pos_dims≥2 再过滤一次。
    if MOMENTUM_LAUNCH_ACCUM_MIN <= accumulated < MOMENTUM_LAUNCH_ACCUM_MAX:
        is_launch = (
            MOMENTUM_LAUNCH_TODAY_MIN <= today_pct <= MOMENTUM_LAUNCH_TODAY_MAX
            and vol_ratio >= MOMENTUM_LAUNCH_VOL
            and _ma_bull_score(closes, feats) >= MA_BULL_2_TIER_SCORE
        )
        if is_launch:
            div_bonus, div_detail = _mo_divergence(closes, historical_kline, feats)
            if div_bonus < 0:  # 有顶背离：放弃首日启动（动能衰竭）
                return None
            score = MOMENTUM_WEIGHTS["today_pct_6_8"] + MOMENTUM_WEIGHTS["accum_10_15"]
            dims: dict[str, int | float] = {
                "momentum_today_pct": MOMENTUM_WEIGHTS["today_pct_6_8"],
                "momentum_accumulated": MOMENTUM_WEIGHTS["accum_10_15"],
                "momentum_volume": MOMENTUM_WEIGHTS["vol_healthy"],
                "momentum_first_launch": 1,
                "momentum_vol_ratio": round(vol_ratio, 2),
                "accumulated_incl_today": round(accum_incl_today, 2),
            }
            score += MOMENTUM_WEIGHTS["vol_healthy"]
            ma_boost = _ma_bull_score(closes, feats)
            score += ma_boost
            dims["momentum_ma_bull"] = ma_boost
            ind_bonus, ind_dims = _compute_momentum_indicators(closes, historical_kline, W, feats)
            score += ind_bonus
            dims.update(ind_dims)
            if stock.value >= 10000:
                score += W["value_gte_10000"]
                dims["momentum_value"] = W["value_gte_10000"]
            elif stock.value >= 5000:
                score += W["value_gte_5000"]
                dims["momentum_value"] = W["value_gte_5000"]
            return KlineSummary(
                trend=MOMENTUM_LAUNCH_WORD,
                accumulated_pct=round(accumulated, 2),
                volume_ratio=round(vol_ratio, 2),
                bottom_confirmed=True,
                score=score,
                dimensions=dims,
                avg_volume=round(avg_vol, 2),
            )

    if accumulated < MOMENTUM_LAUNCH_ACCUM_MIN:
        return None
    if accumulated < MOMENTUM_LAUNCH_ACCUM_MAX:
        # launch 区间 [0,7)：若上方的"首次启动"分支未命中（非放量/MA空头/非3.5-8%），
        # 维持原行为——刚启动但结构未确认的票不能进 momentum 池。
        return None

    score = 0
    dims: dict[str, int | float] = {}
    dims["accumulated_incl_today"] = round(accum_incl_today, 2)

    if today_pct > MAX_MOMENTUM_TODAY_PCT:
        return None

    # 今日涨幅 >= 6% 由显式分支处理（对齐 STRATEGY.md：6~8%→+5；8~10%→+3），
    # 不进入 _score_today_pct，避免其 today_pct_6_7 / today_pct_7_12 分支被覆盖却仍被读取。
    if today_pct >= 8:
        today_score = W["today_pct_8_10"]
        today_dim_key = "momentum_today_pct"
        today_dim_val = today_score
    elif today_pct >= 6:
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
    if vol_ratio < MOMENTUM_VOL_HEALTHY_MIN:
        score += W["vol_low"]
        dims["momentum_volume"] = W["vol_low"]
    elif vol_ratio < MOMENTUM_VOL_HEALTHY_MAX:
        score += W["vol_healthy"]
        dims["momentum_volume"] = W["vol_healthy"]
    else:
        score += W["vol_surge"]
        dims["momentum_volume"] = W["vol_surge"]

    vol_peak = _vol_peak_ratio(volumes, today_vol=_project_today_vol(kline, today_str, now))
    if vol_peak < VOL_PEAK_MOMENTUM_WARN:
        score += VOL_PEAK_MOMENTUM_PENALTY
        dims["momentum_vol_peak"] = round(vol_peak, 2)

    # Crash check — split: base safety + recent 2d bonus
    has_crash_day, crash_score, crash_dims = _crash_safety_block(pcts, "momentum")
    score += crash_score
    dims.update(crash_dims)

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

    ma_bull = _ma_bull_score(closes, feats)
    score += ma_bull
    if ma_bull:
        dims["momentum_ma_bull"] = ma_bull

    indicator_bonus, indicator_dims = _compute_momentum_indicators(closes, historical_kline, W, feats)
    score += indicator_bonus
    dims.update(indicator_dims)

    pattern_score, pattern_dims = detect_momentum_patterns(historical_kline)
    score += pattern_score
    dims.update(pattern_dims)

    # 末周期超买判定已统一至 validator._mo_is_overbought 单点判断 + enhancer 标记，
    # 分析侧不再做软惩罚（避免与 validator 口径不一致及双重计分）。

    return KlineSummary(trend=trend, accumulated_pct=round(accumulated, 2),
                        volume_ratio=round(vol_ratio, 2), bottom_confirmed=not has_crash_day,
                        score=score, dimensions=dims, avg_volume=round(avg_vol, 2))


def analyze_short_term(stock: StockInfo, kline: list[KlineBar] | None,
                       today_str: str | None = None,
                       features: dict | None = None,
                       now=None) -> KlineSummary | None:
    if not kline or len(kline) < 5:
        return None

    W = SHORT_TERM_WEIGHTS

    today_pct = stock.percent
    if today_pct < SHORT_TERM_MIN_TODAY_PCT or today_pct > SHORT_TERM_MAX_TODAY_PCT:
        return None

    today_str = today_str or now_beijing().date().isoformat()
    historical_kline, pcts, closes = _split_today(kline, today_str)

    feats = _get_features(closes, historical_kline, features)

    # short_term 的 accumulated 包含今日 bar（与策略语义"今日异动"一致）
    all_closes = [k["close"] for k in kline]
    if len(all_closes) >= 6:
        accumulated = (all_closes[-1] - all_closes[-6]) / all_closes[-6] * 100
    else:
        accumulated = sum(pcts[-5:]) + today_pct
    accum_incl_today = accumulated

    vol_ratio, avg_vol = _compute_volume_metrics(kline, today_str, now)

    score = 0
    dims: dict[str, int | float] = {}
    dims["accumulated_incl_today"] = round(accum_incl_today, 2)

    if today_pct >= 8:
        score += W["today_pct_8_12"]
        dims["st_today_pct"] = W["today_pct_8_12"]
    elif today_pct >= 6:
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
    else:
        # 2026-08-17 审计修复：原 [0,5) 区间（今日刚启动、前几日横盘的票）无兜底分支，
        # 既不加分也不写 st_accumulated dim，比命中 >=5 的票系统性少拿 10 分。补最低正区间。
        score += W["accum_0_5"]
        dims["st_accumulated"] = W["accum_0_5"]

    if vol_ratio >= 1.5:
        score += W["vol_surge"]
        dims["st_volume"] = W["vol_surge"]
    elif vol_ratio >= 1.0:
        score += W["vol_healthy"]
        dims["st_volume"] = W["vol_healthy"]
    else:
        score += W["vol_low"]
        dims["st_volume"] = W["vol_low"]

    _, crash_score, crash_dims = _crash_safety_block(pcts, "st")
    score += crash_score
    dims.update(crash_dims)

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

    rsi_val = feats["rsi6"]
    if rsi_val is not None:
        if 50 <= rsi_val < 70:
            score += W["rsi_bonus"]
            dims["st_rsi"] = round(rsi_val, 1)
        elif rsi_val > 80:
            score -= W["rsi_bonus"]
            dims["st_rsi"] = round(rsi_val, 1)

    kdj_val = feats.get("kdj")
    if kdj_val is not None:
        if kdj_val["K"] > kdj_val["D"] and 50 <= kdj_val["K"] <= 80 and kdj_val["J"] < 100:
            score += W["kdj_bonus"]
            dims["st_kdj"] = round(kdj_val["J"], 1)

    macd_val = feats["macd"]
    if macd_val is not None:
        if macd_val["histogram"] > 0:
            score += W["macd_bonus"]
            dims["st_macd"] = round(macd_val["histogram"], 4)

    # 末周期超买判定已统一至 validator._st_is_overbought 单点判断 + enhancer 标记，
    # 分析侧不再做软惩罚（避免与 validator 口径不一致及双重计分）。

    pattern_score, pattern_dims = detect_short_term_patterns(historical_kline)
    score += pattern_score
    dims.update(pattern_dims)

    # 昨日大分歧/烂板/炸板 + 今日在 2~8% 内转强（弱转强）
    # KlineBar 契约保证 open/high/close 均为数值，无需 .get() 防御
    yest_divergence = False
    if len(historical_kline) >= 2:
        yest = historical_kline[-1]
        yo = yest["open"]
        yh = yest["high"]
        yc = yest["close"]
        if yc > 0:
            upper_shadow = (yh - max(yo, yc)) / yc
            close_to_high = yc / yh - 1 if yh > 0 else 0
            yest_divergence = (upper_shadow > ST_DIVERGE_UPPER_SHADOW
                                and close_to_high < ST_DIVERGE_CLOSE_WEAK)
            prev_close = historical_kline[-2]["close"]
            if prev_close > 0 and (yh / prev_close - 1) >= ST_BOMB_HIGH and (yc / prev_close - 1) < ST_BOMB_CLOSE:
                yest_divergence = True  # 曾触板但收盘大回落 = 炸板/烂板
    if yest_divergence:
        score += W["st_weak_to_strong"]
        dims["st_weak_to_strong"] = W["st_weak_to_strong"]
        # st_wts_gap 已删除：弱转强 +8 已对该信号充分奖励，高开再加分属双重奖励。
        trend = "弱转强"
    else:
        trend = "放量启动" if vol_ratio > 1.3 else "温和放量" if vol_ratio > 1.0 else "缩量"

    return KlineSummary(trend=trend, accumulated_pct=round(accumulated, 2),
                        volume_ratio=round(vol_ratio, 2), bottom_confirmed=False,
                        score=score, dimensions=dims, avg_volume=round(avg_vol, 2))


def analyze_rebound(stock: StockInfo, kline: list[KlineBar] | None,
                    today_str: str | None = None,
                    features: dict | None = None,
                    off_list: bool = False,
                    now=None) -> KlineSummary | None:
    """超跌反弹策略：识别暴跌后的企稳首阳。

    与 new_face 的区别：new_face 要求 accumulated >= -10%（前期无大跌），
    rebound 专门捕获 5日累计跌幅 ≤ -10% 的超跌后企稳阳线。
    典型场景：连跌4-5日（含暴跌日）后出现温和放量阳线；
    或阴跌企稳（无单日暴跌但累计跌 10-15%，P0-1 放宽）。

    off_list=True（回马枪·反转）：掉榜票无热榜背书，收紧今日涨幅下限
    （COMEBACK_MIN_TODAY_PCT=2.0，反转确认而非抄底猜单），上限放宽到 12%
    （覆盖 8-12% 续涨，掉榜日无短线上限约束）。
    """
    if not kline or len(kline) < 6:  # 至少6根：5日历史+今日
        return None

    W = REBOUND_WEIGHTS
    today_pct = stock.percent

    # 入池硬筛：今日企稳阳线（温和涨幅）；off_list 用回马枪档位
    min_today = COMEBACK_MIN_TODAY_PCT if off_list else REBOUND_MIN_TODAY_PCT
    max_today = COMEBACK_MAX_TODAY_PCT if off_list else REBOUND_MAX_TODAY_PCT
    if today_pct < min_today or today_pct > max_today:
        return None

    today_str = today_str or now_beijing().date().isoformat()
    historical_kline, pcts, closes = _split_today(kline, today_str)

    if len(pcts) < 5:
        return None

    # 前5日累计跌幅（超跌判定）
    # P0-1: 阈值从 -15 放宽到 -10，覆盖"阴跌企稳"场景（无暴跌日但累计跌 10-15%）
    recent_5_pcts = pcts[-5:]
    drop_5d = sum(recent_5_pcts)
    if drop_5d > REBOUND_5D_DROP_THRESHOLD:  # 未超跌，不属于 rebound 场景
        return None
    # 暴跌日作为加分项（不再硬要求）：有暴跌日 = 典型超跌反弹，无 = 阴跌企稳
    has_crash_day = any(p <= REBOUND_CRASH_THRESHOLD for p in recent_5_pcts)

    # 累计涨幅（供下游 enhancer 使用，rebound 的 accumulated 反映前期跌幅）
    if len(closes) >= 6:
        accumulated = (closes[-1] - closes[-6]) / closes[-6] * 100
    else:
        accumulated = sum(pcts[-5:])
    accum_incl_today = _accum_incl_today(kline, today_str, today_pct)

    # 量比
    vol_ratio, avg_vol = _compute_volume_metrics(kline, today_str, now)

    # 距20日低点比例（确认仍在低位区）
    near_20d_low = False
    if len(closes) >= 20:
        low_20d = min(closes[-20:])
        near_20d_low = (closes[-1] - low_20d) / max(low_20d, 0.01) < REBOUND_NEAR_LOW_PCT

    dims: dict[str, int | float] = {}
    dims["accumulated_incl_today"] = round(accum_incl_today, 2)
    score = 0

    # 今日涨幅档
    if today_pct < 2:
        score += W["today_pct_0_5_2"]
        dims["rebound_today_pct"] = W["today_pct_0_5_2"]
    elif today_pct < 4:
        score += W["today_pct_2_4"]
        dims["rebound_today_pct"] = W["today_pct_2_4"]
    elif today_pct < 6:
        score += W["today_pct_4_6"]
        dims["rebound_today_pct"] = W["today_pct_4_6"]
    else:
        score += W["today_pct_6_8"]
        dims["rebound_today_pct"] = W["today_pct_6_8"]

    # 超跌深度档
    if drop_5d >= -20:
        score += W["drop_15_20"]
        dims["rebound_drop_depth"] = W["drop_15_20"]
    elif drop_5d >= -30:
        score += W["drop_20_30"]
        dims["rebound_drop_depth"] = W["drop_20_30"]
    else:
        score += W["drop_gte_30"]
        dims["rebound_drop_depth"] = W["drop_gte_30"]

    # 暴跌日加分（仅当有暴跌日时；阴跌企稳场景不加分）
    if has_crash_day:
        dims["rebound_crash_day"] = W["crash_day_bonus"]
        score += W["crash_day_bonus"]

    # 量能配合
    if vol_ratio >= 2.0:
        score += W["vol_surge"]
        dims["rebound_volume"] = W["vol_surge"]
    elif vol_ratio >= 1.0:
        score += W["vol_healthy"]
        dims["rebound_volume"] = W["vol_healthy"]
    else:
        score += W["vol_low"]
        dims["rebound_volume"] = W["vol_low"]

    # 技术面确认：RSI 超卖
    feats = _get_features(closes, historical_kline, features)
    rsi_val = feats["rsi6"]
    if rsi_val is not None:
        if rsi_val < 30:
            score += W["rsi_oversold"]
            dims["rebound_rsi"] = round(rsi_val, 1)
        elif rsi_val < 50:
            score += W["rsi_mid"]
            dims["rebound_rsi"] = round(rsi_val, 1)

    # BOLL 下轨支撑
    boll = feats["boll"]
    if boll is not None and boll["b_pct"] < 0.1:
        score += W["bollinger_lower"]
        dims["rebound_bollinger"] = round(boll["b_pct"], 2)

    # V型反转特征（前5日累计<-15% + 放量 + 今日>2%）
    v_shape = (drop_5d < -15 and vol_ratio > 1.5 and today_pct > 2)
    if v_shape:
        score += W["v_shape"]
        dims["rebound_v_shape"] = W["v_shape"]

    # 形态加分（传入完整 kline，含今日 bar，检测今日反转信号）
    pattern_score, pattern_dims = detect_rebound_patterns(kline)
    score += pattern_score
    dims.update(pattern_dims)

    # 趋势标签
    if not has_crash_day and drop_5d > REBOUND_CRASH_THRESHOLD * 1.5:
        trend = "阴跌企稳"
    elif v_shape:
        trend = "超跌V反"
    elif near_20d_low:
        trend = "低位企稳"
    elif vol_ratio >= 2.0:
        trend = "放量反弹"
    else:
        trend = "超跌企稳"

    return KlineSummary(trend=trend, accumulated_pct=round(accumulated, 2),
                        volume_ratio=round(vol_ratio, 2), bottom_confirmed=near_20d_low,
                        score=score, dimensions=dims, avg_volume=round(avg_vol, 2))
