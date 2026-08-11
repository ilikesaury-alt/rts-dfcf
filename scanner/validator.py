from scanner.config import (
    COMEBACK_POS_DIMS,
    PULLBACK_20D_GAIN_EXTREME,
    PULLBACK_VOL_HEALTHY,
    PULLBACK_VOL_HIGH,
    PULLBACK_VOL_LOW,
    ST_OVERBOUGHT_BOLL,
    ST_OVERBOUGHT_KDJ,
    V_MO_DIVERGENCE_BEAR,
    V_MO_DIVERGENCE_NONE,
    V_MO_MA_FULL,
    V_MO_MA_NONE,
    V_MO_MA_PARTIAL,
    V_MO_VOL_SPIKE,
    V_MO_VOL_STABLE,
    V_MO_VOL_UP,
    V_NF_CONVERGE_PARTIAL,
    V_NF_CONVERGE_STRONG,
    V_NF_DIVERGENCE_BULL,
    V_NF_SECTOR_MOD,
    V_NF_SECTOR_STRONG,
    V_NF_SECTOR_WEAK,
    V_NF_VOLUME_CONFIRM,
    V_PB_BOLLINGER_MID,
    V_PB_BOLLINGER_TOUCH,
    V_PB_MA_DOWN,
    V_PB_MA_FLAT,
    V_PB_MA_UP,
    V_PB_SECTOR_DEAD,
    V_PB_SECTOR_HOT,
    V_PB_SECTOR_NEUTRAL,
    V_PB_SHRINK_MOD,
    V_PB_SHRINK_NO,
    V_PB_SHRINK_YES,
    V_RB_OVERSOLD_PARTIAL,
    V_RB_OVERSOLD_STRONG,
    V_RB_PATTERN_3BULL,
    V_RB_PATTERN_HAMMER,
    V_RB_PATTERN_STRONG,
    V_RB_SECTOR_ACTIVE,
    V_RB_SECTOR_MOD,
    V_RB_VOL_HEALTHY,
    V_RB_VOL_LOW,
    V_RB_VOL_SURGE,
    V_ST_MA_BROKEN,
    V_ST_MA_SUPPORT,
    V_ST_RANK_LOW,
    V_ST_RANK_TOP10,
    V_ST_RANK_TOP20,
    V_ST_RANK_TOP30,
    V_ST_SECTOR_COLD,
    V_ST_SECTOR_HOT,
    V_ST_SECTOR_WARM,
    V_ST_VOL_HEALTHY,
    V_ST_VOL_SURGE,
)
from scanner.features import build_features
from scanner.indicators import (
    compute_bollinger_bands,
    compute_kdj,
    compute_ma,
)
from scanner.sector import classify_sector
from scanner.models import KlineBar


def _get_features(closes: list[float], historical_kline: list[KlineBar],
                  feats: dict | None = None) -> dict:
    """构建特征（调用方已预计算则复用，否则从 historical_kline 抽取 high/low 现算）。

    与 analysis._get_features 的唯一差异：validator 各维度不需要 volumes/OBV
    （momentum 的 OBV 背离由 _mo_divergence 独立计算），故不传 volumes。
    """
    if feats is not None:
        return feats
    highs = [k["high"] for k in historical_kline]
    lows = [k["low"] for k in historical_kline]
    return build_features(closes, highs, lows)


def _nf_convergence(closes: list[float], historical_kline: list[KlineBar],
                    feats: dict | None = None) -> tuple[int, str, int]:
    if len(closes) < 10:
        return 0, "data_short", 0

    feats = _get_features(closes, historical_kline, feats)
    rsi = feats["rsi6"]
    macd = feats["macd"]
    kdj = feats.get("kdj")

    hits = 0

    if rsi is not None and rsi < 30:
        hits += 1
    if macd is not None and macd["histogram"] > 0 and macd["histogram_prev"] <= 0:
        hits += 1
    if kdj is not None and kdj["K"] < 20:
        prev_k = kdj.get("prev_K")
        prev_d = kdj.get("prev_D")
        if prev_k is not None and prev_d is not None:
            # 金叉时刻：前日 K<=D，今日 K>D（避免金叉后持续加分）
            golden_cross = prev_k <= prev_d and kdj["K"] > kdj["D"]
        else:
            # 兼容旧数据/mock：仅判 K>D
            golden_cross = kdj["K"] > kdj["D"]
        if golden_cross:
            hits += 1

    divergence_bonus = V_NF_DIVERGENCE_BULL if _has_macd_bull_divergence(closes, macd) else 0

    if hits >= 3:
        return V_NF_CONVERGE_STRONG + divergence_bonus, "converge_3of3", hits
    if hits >= 2:
        return V_NF_CONVERGE_PARTIAL + divergence_bonus, f"converge_{hits}of3", hits
    if divergence_bonus:
        return divergence_bonus, "macd_bull_divergence", hits
    return 0, "converge_weak", hits


def _has_macd_bull_divergence(closes: list[float], macd: dict | None) -> bool:
    """真正的 MACD 底背离：价格创新低，但 histogram 未创新低（动能收敛）。

    标准底背离三条件：
    1. 现低点 < 前低点（价格创新低）
    2. 现低点处 histogram > 前低点处 histogram（动能收敛）
    3. 现低点处 histogram < 0（在零轴下方，标准底背离要求）
    """
    if macd is None or len(closes) < 34:  # slow+signal-1 = 34
        return False

    # 计算完整 histogram 序列（与 compute_macd 内部 EMA 逻辑一致）
    def _ema_seq(data, period):
        m = 2 / (period + 1)
        result = [data[0]]
        for v in data[1:]:
            result.append((v - result[-1]) * m + result[-1])
        return result

    ema_f = _ema_seq(closes, 12)
    ema_s = _ema_seq(closes, 26)
    macd_line = [f - s for f, s in zip(ema_f, ema_s)]
    signal_line = _ema_seq(macd_line, 9)
    histogram = [m - s for m, s in zip(macd_line, signal_line)]

    # 寻找两个价格低点：现低（最近5日）和前低（5-20日前窗口）
    n = len(closes)
    recent_start = max(34, n - 5)
    recent_low_idx = recent_start
    for i in range(recent_start, n):
        if closes[i] < closes[recent_low_idx]:
            recent_low_idx = i

    prev_start = max(34, recent_low_idx - 20)
    prev_end = max(34, recent_low_idx - 5)
    if prev_end <= prev_start:
        return False
    prev_low_idx = prev_start
    for i in range(prev_start + 1, prev_end):
        if closes[i] < closes[prev_low_idx]:
            prev_low_idx = i

    # 条件1：价格创新低
    if closes[recent_low_idx] >= closes[prev_low_idx]:
        return False
    # 条件2：histogram 未创新低（动能收敛）
    if histogram[recent_low_idx] <= histogram[prev_low_idx]:
        return False
    # 条件3：当前 histogram 在零轴下方
    if histogram[recent_low_idx] >= 0:
        return False
    return True


def _nf_higher_low(closes: list[float]) -> tuple[int, str]:
    # Step 2 (2026-08-07): IC 归因显示「更高低结构」对 cum_3d 的 IC 为负
    # （触发组均值 -2.26% vs 未触发 -1.01%，胜率 30% vs 39%），即更高低反而是
    # 动量延续而非超卖反转，会误导 new_face 选股。故将其从「正维度」改为中性：
    # 不再加分（V_NF_HL_CLEAR 失效），也不再作为 pos_dims 的通过依据。
    # 超卖反转的真正信号由 convergence（RSI<30/MACD金叉/KDJ）承担。
    if len(closes) < 10:
        return 0, "hl_neutral_data_short"

    recent_zone = min(closes[-5:])
    prev_zone = min(closes[-10:-5])

    prev_zone = max(prev_zone, 0.001)

    if recent_zone > prev_zone * 1.01:
        return 0, f"hl_neutral_clear_{recent_zone/prev_zone:.3f}"
    if recent_zone > prev_zone * 0.98:
        return 0, f"hl_neutral_stable_{recent_zone/prev_zone:.3f}"
    return 0, f"hl_neutral_fail_{recent_zone/prev_zone:.3f}"


def _nf_sector(name: str, clusters: dict[str, list[str]] | None) -> tuple[int, int]:
    if not clusters:
        return V_NF_SECTOR_WEAK, 0
    sec = classify_sector(name)
    count = len(clusters.get(sec, []))
    if count >= 3:
        return V_NF_SECTOR_STRONG, count
    if count >= 2:
        return V_NF_SECTOR_MOD, count
    return V_NF_SECTOR_WEAK, count


def _nf_volume_surge(kline_summary) -> tuple[int, str]:
    if kline_summary is None:
        return 0, "no_data"
    vr = kline_summary.volume_ratio
    if vr > 1.3:
        return V_NF_VOLUME_CONFIRM, f"vol_surge_{vr:.1f}x"
    return 0, f"vol_{vr:.1f}x"


def validate_nf(stock, kline_summary, closes: list[float],
                historical_kline: list[KlineBar], clusters: dict[str, list[str]] | None,
                feats: dict | None = None
                ) -> tuple[bool, int, dict]:
    feats = _get_features(closes, historical_kline, feats)
    conv_bonus, conv_detail, conv_hits = _nf_convergence(closes, historical_kline, feats)
    hl_bonus, hl_detail = _nf_higher_low(closes)
    sec_bonus, sec_count = _nf_sector(stock.name, clusters)
    vol_bonus, vol_detail = _nf_volume_surge(kline_summary)

    details: dict[str, int | float | str] = {
        "v_nf_convergence": conv_bonus,
        "v_nf_convergence_detail": conv_detail,
        "v_nf_convergence_hits": conv_hits,
        "v_nf_higher_low": hl_bonus,
        "v_nf_higher_low_detail": hl_detail,
        "v_nf_sector": sec_bonus,
        "v_nf_sector_count": sec_count,
        "v_nf_volume": vol_bonus,
        "v_nf_volume_detail": vol_detail,
    }

    total = conv_bonus + hl_bonus + sec_bonus + vol_bonus

    pos_dims = sum(1 for b in (conv_bonus, hl_bonus, sec_bonus, vol_bonus) if b > 0)
    # 硬前提：必须至少 1 项超卖共振（convergence 命中 或 MACD 底背离），
    # 否则仅凭 higher_low + 板块无法证明是"超卖反转"，杜绝上升中继股冒充新面孔。
    oversold_signal = conv_hits >= 1 or conv_detail == "macd_bull_divergence"
    passed = oversold_signal and pos_dims >= 2

    details["_pos_dims"] = pos_dims
    details["_max_dims"] = 4
    return passed, total, details


def _mo_ma_alignment(closes: list[float], feats: dict | None = None) -> tuple[int, str]:
    # 与 analysis._ma_bull_score 统一使用 EMA 约定，消除「分析加分 / 验证剔除」脱节。
    # 数据不足（data_short）返回 0 而非 V_MO_MA_NONE：
    #   - 避免对无 20 日历史的新股误判「趋势破位」硬过滤（enhancer._detect_trend_breakage）
    #   - 避免无谓 -5 惩罚（无足够样本时无法判定空头，应中性处理而非当作空头证据）
    # 真正的空头排列是数据充足时 ma5<=ma10 的 "ma_none"，两者必须区分。
    if feats is None:
        if len(closes) < 10:
            return 0, "data_short"
        ma5 = compute_ma(closes, 5, ema=True)
        ma10 = compute_ma(closes, 10, ema=True)
        ma20 = compute_ma(closes, 20, ema=True) if len(closes) >= 20 else None
    else:
        ma5 = feats.get("ma5_ema")
        ma10 = feats.get("ma10_ema")
        ma20 = feats.get("ma20_ema")
    if ma5 is None or ma10 is None:
        return 0, "data_short"

    if ma20 is not None and ma5 > ma10 > ma20:
        return V_MO_MA_FULL, "ma_full_5gt10gt20"
    if ma5 > ma10:
        return V_MO_MA_PARTIAL, "ma_partial_5gt10"
    return V_MO_MA_NONE, "ma_none"


def _rsi_seq(closes: list[float], period: int = 6) -> list[float]:
    """计算 RSI 完整序列（Wilder 平滑），rsi_list[i] 对应 closes[period+i]。"""
    if len(closes) < period + 1:
        return []
    gains, losses = 0.0, 0.0
    for i in range(1, period + 1):
        diff = closes[i] - closes[i - 1]
        if diff > 0:
            gains += diff
        else:
            losses -= diff
    avg_gain = gains / period
    avg_loss = losses / period
    rsi_list: list[float] = []
    if avg_loss == 0:
        rsi_list.append(100.0)
    else:
        rs = avg_gain / avg_loss
        rsi_list.append(100.0 - 100.0 / (1.0 + rs))
    for i in range(period + 1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gain = diff if diff > 0 else 0
        loss = -diff if diff < 0 else 0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        if avg_loss == 0:
            rsi_list.append(100.0)
        else:
            rs = avg_gain / avg_loss
            rsi_list.append(100.0 - 100.0 / (1.0 + rs))
    return rsi_list


def _mo_divergence(closes: list[float], historical_kline: list[KlineBar],
                   feats: dict | None = None) -> tuple[int, str]:
    """RSI 顶背离：价格创新高，但 RSI 未创新高（动能衰竭）。

    标准顶背离两条件：
    1. 现高点 > 前高点（价格创新高）
    2. 现高点处 RSI < 前高点处 RSI（动能衰竭）
    """
    if len(closes) < 15:
        return V_MO_DIVERGENCE_NONE, "data_short"

    rsi_period = 6
    rsi_list = _rsi_seq(closes, rsi_period)
    if len(rsi_list) < 10:
        return V_MO_DIVERGENCE_NONE, "rsi_na"

    n = len(closes)
    # 现高：最近5日最高点
    recent_start = max(rsi_period + 1, n - 5)
    recent_high_idx = recent_start
    for i in range(recent_start, n):
        if closes[i] > closes[recent_high_idx]:
            recent_high_idx = i

    # 前高：5-20日前窗口最高点
    prev_start = max(rsi_period + 1, recent_high_idx - 20)
    prev_end = max(rsi_period + 1, recent_high_idx - 5)
    if prev_end <= prev_start:
        return V_MO_DIVERGENCE_NONE, "no_prev_high"
    prev_high_idx = prev_start
    for i in range(prev_start + 1, prev_end):
        if closes[i] > closes[prev_high_idx]:
            prev_high_idx = i

    # 条件1：价格创新高
    if closes[recent_high_idx] <= closes[prev_high_idx]:
        return V_MO_DIVERGENCE_NONE, "no_new_high"

    # 条件2：RSI 未创新高（rsi_list[i-period] 对应 closes[i]）
    rsi_recent = rsi_list[recent_high_idx - rsi_period]
    rsi_prev = rsi_list[prev_high_idx - rsi_period]

    if rsi_recent < rsi_prev:
        return V_MO_DIVERGENCE_BEAR, "rsi_bear_divergence"

    # 条件3：OBV 顶背离（价升量减=资金流出）
    # OBV 序列与 closes 对齐，比较两个价格高点处的 OBV 值
    if historical_kline and len(historical_kline) >= len(closes):
        volumes = [k.get("volume", 0) for k in historical_kline[:len(closes)]]
        obv_seq = [0]
        for i in range(1, len(closes)):
            if closes[i] > closes[i - 1]:
                obv_seq.append(obv_seq[-1] + volumes[i])
            elif closes[i] < closes[i - 1]:
                obv_seq.append(obv_seq[-1] - volumes[i])
            else:
                obv_seq.append(obv_seq[-1])
        if obv_seq[recent_high_idx] < obv_seq[prev_high_idx]:
            return V_MO_DIVERGENCE_BEAR, "obv_bear_divergence"

    return V_MO_DIVERGENCE_NONE, "no_divergence"


def _mo_volume_uniformity(historical_kline: list[KlineBar]) -> tuple[int, str]:
    volumes = [k["volume"] for k in historical_kline[-7:]]
    if len(volumes) < 5:
        return 0, "data_short"

    recent_5 = volumes[-5:]
    inc = all(recent_5[i] <= recent_5[i + 1] for i in range(len(recent_5) - 1))
    ratio = max(recent_5) / max(min(recent_5), 0.01) if min(recent_5) > 0 else 99

    # 放宽：去掉"严格非递减"硬要求；仅当 5 日内量能爆量（ratio>=3.0）才判异常
    if inc and ratio < 3.0:
        return V_MO_VOL_UP, f"vol_up_r{ratio:.1f}"
    if ratio < 3.0:
        return V_MO_VOL_STABLE, f"vol_stable_r{ratio:.1f}"
    return V_MO_VOL_SPIKE, f"vol_spike_r{ratio:.1f}"


def validate_momentum(stock, kline_summary, closes: list[float],
                      historical_kline: list[KlineBar], clusters: dict[str, list[str]] | None,
                      feats: dict | None = None
                      ) -> tuple[bool, int, dict]:
    feats = _get_features(closes, historical_kline, feats)
    ma_bonus, ma_detail = _mo_ma_alignment(closes, feats)
    div_bonus, div_detail = _mo_divergence(closes, historical_kline, feats)
    vol_bonus, vol_detail = _mo_volume_uniformity(historical_kline)

    details: dict[str, int | float | str] = {
        "v_mo_ma": ma_bonus,
        "v_mo_ma_detail": ma_detail,
        "v_mo_divergence": div_bonus,
        "v_mo_divergence_detail": div_detail,
        "v_mo_volume": vol_bonus,
        "v_mo_volume_detail": vol_detail,
    }

    total = ma_bonus + div_bonus + vol_bonus

    # 末周期超买（鱼尾段）：仅标记，不压制 total——硬否决交给标准 passed 门禁判定。
    # momentum 是趋势延续策略，高 BOLL%/J 部分属正常强势特征，硬否决会误杀主升浪。
    overbought = _mo_is_overbought(closes, historical_kline, stock)
    details["v_mo_overbought"] = overbought

    # 中等处置：_mo_divergence 仅返回 0（无背离）或 -10（顶背离），从不计入正维度。
    # 因此出现顶背离时，候选必须通过「MA 多头 + 量能均匀」两个其它正维度（pos_dims>=2）才放行，
    # 背离本身不会单独否决候选，但会强制其它维度补偿（STRATEGY.md 动量「中等」策略）。
    pos_dims = sum(1 for b in (ma_bonus, div_bonus, vol_bonus) if b > 0)
    passed = pos_dims >= 2

    details["_pos_dims"] = pos_dims
    details["_max_dims"] = 3
    return passed, total, details


def _pb_ma_trend(closes: list[float], feats: dict | None = None) -> tuple[int, str]:
    if len(closes) < 25:
        return 0, "data_short"

    if feats is not None and feats.get("ma20_sma") is not None and feats.get("ma20_sma_prev") is not None:
        ma20_now = feats["ma20_sma"]
        ma20_prev = feats["ma20_sma_prev"]
    else:
        ma20_now = sum(closes[-20:]) / 20
        ma20_prev = sum(closes[-25:-5]) / 20
    change_pct = (ma20_now - ma20_prev) / max(ma20_prev, 0.01) * 100

    if change_pct > 0.5:
        return V_PB_MA_UP, f"ma20_up_{change_pct:+.1f}%"
    if change_pct > -0.5:
        return V_PB_MA_FLAT, f"ma20_flat_{change_pct:+.1f}%"
    return V_PB_MA_DOWN, f"ma20_down_{change_pct:+.1f}%"


def _pb_shrinkage(kline_summary) -> tuple[int, float]:
    vr = kline_summary.volume_ratio
    # 与分析端 _score_pullback_volume 共用同一组阈值，避免口径矛盾：
    #   < PULLBACK_VOL_LOW      -> 极度缩量（确认 +）
    #   <= PULLBACK_VOL_HEALTHY -> 健康缩量（+）
    #   <= PULLBACK_VOL_HIGH    -> 中性（0）
    #   >  PULLBACK_VOL_HIGH    -> 放量（非缩量，惩罚 -）
    if vr < PULLBACK_VOL_LOW:
        return V_PB_SHRINK_YES, vr
    if vr <= PULLBACK_VOL_HEALTHY:
        return V_PB_SHRINK_MOD, vr
    if vr <= PULLBACK_VOL_HIGH:
        return 0, vr
    return V_PB_SHRINK_NO, vr


def _pb_sector(name: str, clusters: dict[str, list[str]] | None) -> tuple[int, int]:
    if not clusters:
        return V_PB_SECTOR_DEAD, 0
    sec = classify_sector(name)
    count = len(clusters.get(sec, []))
    if count >= 3:
        return V_PB_SECTOR_HOT, count
    return V_PB_SECTOR_NEUTRAL, count


def _pb_bollinger_touch(closes: list[float], feats: dict | None = None) -> tuple[int, str]:
    if len(closes) < 20:
        return 0, "data_short"
    boll = feats["boll"] if (feats is not None and feats.get("boll") is not None) else compute_bollinger_bands(closes)
    if boll is None:
        return 0, "bb_na"
    current = closes[-1]
    touch_lower = current <= boll["lower"] * 1.02
    near_mid = abs(current - boll["middle"]) / max(boll["middle"], 0.01) * 100 < 1.0
    if touch_lower:
        return V_PB_BOLLINGER_TOUCH, f"bb_lower_touch_b{boll['b_pct']:.2f}"
    if near_mid:
        return V_PB_BOLLINGER_MID, f"bb_mid_return_b{boll['b_pct']:.2f}"
    return 0, f"bb_mid_b{boll['b_pct']:.2f}"


def validate_pullback(stock, kline_summary, closes: list[float],
                      historical_kline: list[KlineBar], clusters: dict[str, list[str]] | None,
                      feats: dict | None = None
                      ) -> tuple[bool, int, dict]:
    if feats is None:
        feats = build_features(closes)
    ma_bonus, ma_detail = _pb_ma_trend(closes, feats)
    shr_bonus, shr_vr = _pb_shrinkage(kline_summary)
    sec_bonus, sec_count = _pb_sector(stock.name, clusters)
    boll_bonus, boll_detail = _pb_bollinger_touch(closes, feats)

    details: dict[str, int | float | str] = {
        "v_pb_ma_trend": ma_bonus,
        "v_pb_ma_trend_detail": ma_detail,
        "v_pb_shrinkage": shr_bonus,
        "v_pb_shrinkage_vr": round(shr_vr, 2),
        "v_pb_sector": sec_bonus,
        "v_pb_sector_count": sec_count,
        "v_pb_bollinger": boll_bonus,
        "v_pb_bollinger_detail": boll_detail,
    }

    total = ma_bonus + shr_bonus + sec_bonus + boll_bonus

    pos_dims = sum(1 for b in (ma_bonus, shr_bonus, sec_bonus, boll_bonus) if b > 0)
    passed = pos_dims >= 2

    details["_pos_dims"] = pos_dims
    details["_max_dims"] = 4
    return passed, total, details


def _rb_oversold(closes: list[float], historical_kline: list[KlineBar],
                feats: dict | None = None) -> tuple[int, str]:
    """超卖确认：RSI<30 或 KDJ J<0 或 MACD 柱翻红。"""
    if len(closes) < 10:
        return 0, "data_short"
    feats = _get_features(closes, historical_kline, feats)
    rsi = feats["rsi6"]
    kdj = feats.get("kdj")
    macd = feats["macd"]
    hits = 0
    if rsi is not None and rsi < 30:
        hits += 1
    if kdj is not None and kdj["J"] < 0:
        hits += 1
    if macd is not None and macd["histogram"] > 0 and macd["histogram_prev"] <= 0:
        hits += 1
    if hits >= 2:
        return V_RB_OVERSOLD_STRONG, "oversold_strong"
    if hits >= 1:
        return V_RB_OVERSOLD_PARTIAL, "oversold_partial"
    return 0, "oversold_weak"


def _rb_volume(kline_summary) -> tuple[int, str]:
    """量能确认：放量企稳优于缩量。"""
    vr = kline_summary.volume_ratio
    if vr >= 2.0:
        return V_RB_VOL_SURGE, "vol_surge"
    if vr >= 1.0:
        return V_RB_VOL_HEALTHY, "vol_healthy"
    return V_RB_VOL_LOW, "vol_low"


def _rb_sector(name: str, clusters: dict[str, list[str]] | None) -> tuple[int, int]:
    """板块共振确认。"""
    if not clusters:
        return 0, 0
    sec = classify_sector(name)
    count = len(clusters.get(sec, []))
    if count >= 3:
        return V_RB_SECTOR_ACTIVE, count
    if count >= 2:
        return V_RB_SECTOR_MOD, count
    return 0, count


def _rb_pattern(kline_summary) -> tuple[int, str]:
    """形态确认：从 analyze_rebound 预计算的 dims 读取，避免重复检测。

    形态分已在 analyze_rebound 的 score 中计入（detect_rebound_patterns），
    此处仅作为 pos_dims 维度判定，不并入 validate total。
    """
    dims = kline_summary.dimensions if kline_summary else {}
    if dims.get("rb_pattern_engulfing_crash"):
        return V_RB_PATTERN_STRONG, "engulfing_crash"
    if dims.get("rb_pattern_hammer"):
        return V_RB_PATTERN_HAMMER, "hammer"
    if dims.get("rb_pattern_3bull_stabilize"):
        return V_RB_PATTERN_3BULL, "3bull_stabilize"
    return 0, "no_pattern"


def validate_rebound(stock, kline_summary, closes: list[float],
                     historical_kline: list[KlineBar], clusters: dict[str, list[str]] | None,
                     feats: dict | None = None
                     ) -> tuple[bool, int, dict]:
    """超跌反弹交叉验证：4 维独立判断，pos_dims >= 2 通过。

    维度：超卖确认 / 量能确认 / 板块共振 / 形态确认。
    不设超买否决（rebound 场景本身在低位，超买概率低）。
    """
    feats = _get_features(closes, historical_kline, feats)
    os_bonus, os_detail = _rb_oversold(closes, historical_kline, feats)
    vol_bonus, vol_detail = _rb_volume(kline_summary)
    sec_bonus, sec_count = _rb_sector(stock.name, clusters)
    pat_bonus, pat_detail = _rb_pattern(kline_summary)

    details: dict[str, int | float | str] = {
        "v_rb_oversold": os_bonus,
        "v_rb_oversold_detail": os_detail,
        "v_rb_volume": vol_bonus,
        "v_rb_volume_detail": vol_detail,
        "v_rb_sector": sec_bonus,
        "v_rb_sector_count": sec_count,
        "v_rb_pattern": pat_bonus,
        "v_rb_pattern_detail": pat_detail,
    }

    # 形态分已在 analyze_rebound 的 score 中计入（detect_rebound_patterns），
    # 此处仅作为 pos_dims 维度判定，不再并入 total，避免重复计分。
    total = os_bonus + vol_bonus + sec_bonus
    pos_dims = sum(1 for b in (os_bonus, vol_bonus, sec_bonus, pat_bonus) if b > 0)
    passed = pos_dims >= 2

    details["_pos_dims"] = pos_dims
    details["_max_dims"] = 4
    return passed, total, details


def validate_short_term(stock, kline_summary, closes: list[float],
                        historical_kline: list[KlineBar], clusters: dict[str, list[str]] | None,
                        feats: dict | None = None
                        ) -> tuple[bool, int, dict]:
    # 硬门禁：量比 < 1.0 直接淘汰（超短必须放量）。软维度为下方 4 项。
    # 放行条件（P0-sector 单维度刷屏修复）：弱转强直接放行；否则要求 ≥2 正维度
    # 且至少 1 项非 sector —— 杜绝板块普涨日仅靠 sector 单维度批量放行。
    vol_ratio = kline_summary.volume_ratio
    if vol_ratio < 1.0:
        return False, 0, {"v_st_vol_gate": "fail", "v_st_vol_ratio": round(vol_ratio, 2)}

    if vol_ratio >= 1.5:
        vol_bonus = V_ST_VOL_SURGE
        vol_detail = f"vol_surge_{vol_ratio:.1f}x"
    else:
        # 硬门 vol_ratio < 1.0 已提前 return，此处 vol_ratio ∈ [1.0, 1.5)
        vol_bonus = V_ST_VOL_HEALTHY
        vol_detail = f"vol_healthy_{vol_ratio:.1f}x"

    sec = classify_sector(stock.name)
    cluster_count = len(clusters.get(sec, [])) if clusters else 0
    if cluster_count >= 3:
        sec_bonus = V_ST_SECTOR_HOT
    elif cluster_count >= 2:
        sec_bonus = V_ST_SECTOR_WARM
    else:
        sec_bonus = V_ST_SECTOR_COLD

    rank = stock.rank
    if rank <= 10:
        rank_bonus = V_ST_RANK_TOP10
    elif rank <= 20:
        rank_bonus = V_ST_RANK_TOP20
    elif rank <= 30:
        rank_bonus = V_ST_RANK_TOP30
    else:
        rank_bonus = V_ST_RANK_LOW

    ma_bonus = 0
    if len(closes) >= 20:
        ma5 = sum(closes[-5:]) / 5
        ma10 = sum(closes[-10:]) / 10
        if closes[-1] > ma5 > ma10:
            ma_bonus = V_ST_MA_SUPPORT
        elif closes[-1] < ma5:
            ma_bonus = V_ST_MA_BROKEN

    # 弱转强作为第 4 个软维度：真弱转强即便板块/排名/MA 全不达标也应放行。
    # 注意：st_weak_to_strong 已在 analyze_short_term 的 score 中计入（+8），
    # 此处仅用作门控维度（计入 pos_dims），不再加入 total，避免重复计分（P0-68）。
    wts_bonus = kline_summary.dimensions.get("st_weak_to_strong", 0)

    # 量能奖励已在 analyze_short_term 的 score 中计入（W["vol_surge"]/W["vol_healthy"]），
    # 此处仅作为 pos_dims 维度判定，不再并入 total，避免重复计分。
    total = sec_bonus + rank_bonus + ma_bonus

    details: dict[str, int | float | str] = {
        "v_st_vol": vol_bonus,
        "v_st_vol_detail": vol_detail,
        "v_st_sector": sec_bonus,
        "v_st_sector_count": cluster_count,
        "v_st_rank": rank_bonus,
        "v_st_ma": ma_bonus,
        "v_st_weak": wts_bonus,
    }

    pos_dims = sum(1 for b in (sec_bonus, rank_bonus, ma_bonus, wts_bonus) if b > 0)
    non_sector_pos = sum(1 for b in (rank_bonus, ma_bonus, wts_bonus) if b > 0)

    # 末周期超买判定（鱼尾段）：用 closes（历史）+ 今日收盘，复用与分析侧一致阈值。
    overbought = _st_is_overbought(closes, historical_kline, stock)
    details["v_st_overbought"] = overbought

    # 超买时：弱转强不再享受"无条件直通"特权，且放量/板块/MA 共振这些"确认信号"
    # 实为末周期风险而非优势，故压下验证加分（total 归零），仅按标准门禁判定，
    # 避免高位破轨股仅凭弱转强 + 共振拿下 inflated 验证分（如 300534 案例 +24）。
    if overbought:
        # 末周期超买：共振验证加分归零，弱转强不再直通，按标准门禁判定。
        # 注意：pos_dims 仍含 wts_bonus，与"弱转强失效"注释矛盾——超买时
        # wts_bonus 不应再作为通过维度，故从 pos_dims / non_sector_pos 中剔除。
        pos_dims_no_wts = sum(1 for b in (sec_bonus, rank_bonus, ma_bonus) if b > 0)
        non_sector_pos_no_wts = sum(1 for b in (rank_bonus, ma_bonus) if b > 0)
        details["_pos_dims"] = pos_dims_no_wts
        details["_max_dims"] = 3
        return pos_dims_no_wts >= 2 and non_sector_pos_no_wts >= 1, 0, details

    passed = wts_bonus > 0 or (pos_dims >= 2 and non_sector_pos >= 1)
    details["_pos_dims"] = pos_dims
    details["_max_dims"] = 4
    return passed, total, details


def _is_overbought(closes: list[float], historical_kline: list[KlineBar],
                   stock: object) -> bool:
    """判定候选是否处于末周期超买（鱼尾段）。

    BOLL %B 用 closes + 今日收盘（series，反映最新价）；
    KDJ J 只算纯历史 bar（今日急拉会污染 J，见下方注释）；
    20日涨幅用历史 closes（不含今日）。
    short_term 硬否决，momentum 仅标记不硬否决（由各自 validate 函数决定）。
    """
    series = list(closes)
    today_close = getattr(stock, "current", 0) or 0
    append_today = today_close > 0 and (not series or today_close != series[-1])
    if append_today:
        series.append(today_close)

    if len(series) >= 20:
        boll = compute_bollinger_bands(series)
        if boll is not None and boll["b_pct"] > ST_OVERBOUGHT_BOLL:
            return True
    # KDJ 只算历史 bar：原逻辑把 today_close 同时塞进 high/low，产生 (high==low)
    # 的人造 bar，污染 J 值（今日急拉时易误判超买）。改用纯历史序列，
    # 长度天然与 closes / historical_kline 对齐。
    # 防御：highs/lows 与 closes 长度不一致（异常数据/历史重放时）先截断对齐，
    # 避免 compute_kdj 内 max() 取到空切片抛 ValueError（数据入口边界）。
    if len(closes) >= 9:
        n = len(closes)
        highs = [k["high"] for k in historical_kline][:n]
        lows = [k["low"] for k in historical_kline][:n]
        # 不足 n 根的补齐（只读 KDJ 不产生交易语义），防止 max() 空切片
        if len(highs) < n:
            highs = highs + [closes[-1]] * (n - len(highs))
            lows = lows + [closes[-1]] * (n - len(lows))
        kdj = compute_kdj(highs, lows, closes)
        if kdj is not None and kdj["J"] > ST_OVERBOUGHT_KDJ:
            return True
    # 20日涨幅口径与分析端一致：用历史 closes（不含今日）的 [-1] 与 [-21]
    if len(closes) >= 21:
        gain_20d = (closes[-1] - closes[-21]) / closes[-21] * 100
        if gain_20d > PULLBACK_20D_GAIN_EXTREME:
            return True
    return False


_st_is_overbought = _is_overbought
_mo_is_overbought = _is_overbought


def validate(cat: str, stock, kline_summary, closes: list[float],
             historical_kline: list[KlineBar], clusters: dict[str, list[str]] | None = None,
             feats: dict | None = None,
             off_list: bool = False,
             ) -> tuple[bool, int, dict]:
    if cat in ("new_face", "known_new_face"):
        return validate_nf(stock, kline_summary, closes, historical_kline, clusters, feats)
    if cat == "momentum":
        return validate_momentum(stock, kline_summary, closes, historical_kline, clusters, feats)
    if cat == "pullback":
        return validate_pullback(stock, kline_summary, closes, historical_kline, clusters, feats)
    if cat == "rebound":
        passed, bonus, details = validate_rebound(stock, kline_summary, closes, historical_kline, clusters, feats)
        if off_list and details.get("_pos_dims", 0) < COMEBACK_POS_DIMS:
            # 回马枪·反转：掉榜票无热榜背书，交叉验证维度比榜上更严
            return False, 0, details
        return passed, bonus, details
    if cat == "short_term":
        return validate_short_term(stock, kline_summary, closes, historical_kline, clusters, feats)
    return False, 0, {}
