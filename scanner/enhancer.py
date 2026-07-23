from datetime import datetime

from scanner.config import (
    now_beijing,
    CROSS_SOURCE_BONUS,
    DISTRIBUTION_ACCUM_HIGH,
    DISTRIBUTION_ACCUM_MID,
    DISTRIBUTION_ACCUM_PULLBACK,
    DISTRIBUTION_INTRADAY_WEAK,
    DISTRIBUTION_OPENING_STRONG,
    DISTRIBUTION_TODAY_PCT_LOW,
    DISTRIBUTION_VOL_RATIO,
    EARLY_BONUS,
    EARLY_TRADE_CUTOFF,
    LATE_BONUS,
    LATE_TRADE_START,
    FATIGUE_ACCELERATE_BONUS_PER_DAY,
    FATIGUE_ACCELERATE_BONUS_CAP,
    FATIGUE_ACCELERATE_PCT,
    FATIGUE_PENALTY_CAP,
    FATIGUE_PENALTY_PER_DAY,
    FATIGUE_PRICE_WARN_ACCUM,
    FATIGUE_STREAK_MIN,
    FATIGUE_VOL_WARN_RATIO,
    LIST_STREAK_BONUS_2,
    LIST_STREAK_BONUS_3,
    LIST_STREAK_BONUS_5,
    LIVE_VOL_BONUS,
    LIVE_VOL_RATIO_THRESHOLD,
    MARKET_ENV_STRONG,
    MARKET_ENV_WEAK,
    MARKET_STRONG_THRESHOLD,
    MARKET_WEAK_THRESHOLD,
    MCAP_BONUS_SMALL,
    MCAP_BONUS_MID,
    MCAP_SMALL_THRESHOLD,
    MCAP_MID_THRESHOLD,
    OVERVALUED_ACCUM_THRESHOLD,
    SECTOR_CLUSTER_BONUS_2,
    SECTOR_CLUSTER_BONUS_3,
    SECTOR_CLUSTER_BONUS_4,
    SECTOR_CLUSTER_BONUS_5,
    TOP20_EXTRA,
    TOP40_ADVANCE_PER_10,
    TOP40_BONUS,
    TOP40_THRESHOLD,
    TURNOVER_BONUS_MODERATE,
    TURNOVER_BONUS_PENALTY,
    TURNOVER_BONUS_HEALTHY,
    TURNOVER_HIGH,
    TURNOVER_LOW,
    TURNOVER_MEDIUM,
    V_MO_DIVERGENCE_BEAR,
    V_MO_MA_NONE,
    V_MO_VOL_SPIKE,
    V_PB_MA_DOWN,
    V_PB_SHRINK_NO,
    V_ST_MA_BROKEN,
)
from scanner.models import Candidate
from scanner.rank_trend import rank_trajectory_score
from scanner.sector import classify_sector


def apply_all_bonuses(
    candidates: list[Candidate],
    gem_stocks: list,
    intraday_scores: dict[str, float | None],
    opening_scores: dict[str, float | None],
    live_volumes: dict[str, float | None],
    market_caps: dict[str, dict],
    clusters: dict[str, list[str]],
    market_idx_pct: float | None,
    time_bonus: int,
    sentiment_info: dict = None,
    rps_scores: dict[str, int] = None,
    list_streaks: dict[str, int] = None,
    conn=None,
):
    for c in candidates:
        _apply_sector_bonus(c, clusters)
        _apply_intraday_bonus(c, intraday_scores)
        _apply_live_vol_bonus(c, live_volumes)
        _apply_turnover_bonus(c, market_caps)
        _apply_sentiment_bonus(c, sentiment_info)
        _apply_rps_bonus(c, rps_scores)
        _apply_market_cap_bonus(c)
        _apply_list_momentum_bonus(c, list_streaks, conn)
        c.time_bonus = time_bonus
        _apply_gap_up_bonus(c)
        _record_dimensions(c, market_idx_pct, opening_scores)
        _set_risk_flags(c)


def _set_risk_flags(c: Candidate):
    """设置复合风险标签，供 UI 显示⚠️标记。

    每个标签对应明确的交易决策含义，基于多字段组合判断而非单维 IC 反指。
    不清零加分（基础评分维度清零会破坏策略逻辑），仅加风险标签供人工判断。
    """
    dims = c.kline.dimensions if c.kline else {}

    # 超买：末周期鱼尾段（BOLL %B>1.0 或 KDJ J>105 或 20日涨幅>60%）
    if dims.get("st_overbought_flag") or dims.get("mo_overbought_flag"):
        c.risk_flags.append("超买")
    # 疲劳：连续上榜后劲不足（fatigue 惩罚已触发）
    if (dims.get("fatigue") or 0) < 0:
        c.risk_flags.append("疲劳")
    # 弱市：大盘涨幅<-1.0%
    if (dims.get("market_env_bonus") or 0) < 0:
        c.risk_flags.append("弱市")
    # 主力出货：高位派发复合判断
    if _detect_main_force_distribution(c, dims):
        c.risk_flags.append("主力出货")
    # 趋势破位：MA 破位合并标签（止损信号）
    if _detect_trend_breakage(dims):
        c.risk_flags.append("趋势破位")
    # 涨幅过大：追高风险
    if _detect_overvalued(c):
        c.risk_flags.append("涨幅过大")
    # 量价背离：量价不匹配（含顶背离）
    if _detect_volume_price_divergence(c, dims):
        c.risk_flags.append("量价背离")


def _detect_main_force_distribution(c: Candidate, dims: dict) -> bool:
    """识别主力高位派发迹象。

    四种经典出货模式（满足任一即判定）：
    1. 高位放量滞涨：累计涨幅大 + 量比高 + 今日几乎不涨（量价背离派发）
    2. 高位高换手+超买：高位 + 高换手 + 超买（借势派发）
    3. 冲高回落：开盘强势但分时走弱 + 已有累计涨幅（盘中冲高出货）
    4. 爆量+顶背离：量能爆量 + 顶背离（经典量价顶背离派发）
    """
    accum = c.kline.accumulated_pct if c.kline else 0.0
    vol_ratio = c.kline.volume_ratio if c.kline else 1.0
    today_pct = c.stock.percent
    opening = dims.get("opening_score")
    intraday = c.intraday_score
    overbought = bool(dims.get("st_overbought_flag") or dims.get("mo_overbought_flag"))

    # 1. 高位放量滞涨
    if (accum >= DISTRIBUTION_ACCUM_HIGH
            and vol_ratio >= DISTRIBUTION_VOL_RATIO
            and today_pct <= DISTRIBUTION_TODAY_PCT_LOW):
        return True
    # 2. 高位高换手+超买
    if (accum >= DISTRIBUTION_ACCUM_MID
            and c.turnover_bonus > 0
            and overbought):
        return True
    # 3. 冲高回落（opening_score 范围 -5~5，intraday_score 范围 -10~10）
    if (opening is not None
            and opening >= DISTRIBUTION_OPENING_STRONG
            and intraday < DISTRIBUTION_INTRADAY_WEAK
            and accum >= DISTRIBUTION_ACCUM_PULLBACK):
        return True
    # 4. 爆量+顶背离（validator 判定的经典出货信号）
    if (dims.get("v_mo_volume") == V_MO_VOL_SPIKE
            and dims.get("v_mo_divergence") == V_MO_DIVERGENCE_BEAR):
        return True
    return False


def _detect_trend_breakage(dims: dict) -> bool:
    """识别 MA 趋势破位（止损信号）。

    合并四种 MA 破位场景（满足任一即判定）：
    - momentum MA 空头排列（v_mo_ma == V_MO_MA_NONE）
    - pullback MA20 下行（v_pb_ma_trend == V_PB_MA_DOWN）
    - short_term 跌破 MA5（v_st_ma == V_ST_MA_BROKEN）
    - pullback MA 破位（pullback_ma_broken < 0）
    """
    if dims.get("v_mo_ma") == V_MO_MA_NONE:
        return True
    if dims.get("v_pb_ma_trend") == V_PB_MA_DOWN:
        return True
    if dims.get("v_st_ma") == V_ST_MA_BROKEN:
        return True
    if (dims.get("pullback_ma_broken") or 0) < 0:
        return True
    return False


def _detect_overvalued(c: Candidate) -> bool:
    """识别涨幅过大（追高风险）。

    满足任一即判定：
    - 累计涨幅 >= OVERVALUED_ACCUM_THRESHOLD
    - pullback 20日涨幅过大惩罚已触发（pullback_20d_gain < 0）
    - momentum 累计>=30% 惩罚已触发（momentum_accumulated <= -15）
    """
    accum = c.kline.accumulated_pct if c.kline else 0.0
    if accum >= OVERVALUED_ACCUM_THRESHOLD:
        return True
    dims = c.kline.dimensions if c.kline else {}
    if (dims.get("pullback_20d_gain") or 0) < 0:
        return True
    if (dims.get("momentum_accumulated") or 0) <= -15:
        return True
    return False


def _detect_volume_price_divergence(c: Candidate, dims: dict) -> bool:
    """识别量价背离（量价不匹配）。

    满足任一即判定：
    - 顶背离（v_mo_divergence == V_MO_DIVERGENCE_BEAR）：价格创新高但指标不创新高
    - pullback 非缩量（v_pb_shrinkage == V_PB_SHRINK_NO）：回踩却不缩量，量价不匹配
    - momentum 缩量（momentum_volume < 0）：动量延续却缩量，上涨动能不足
    """
    if dims.get("v_mo_divergence") == V_MO_DIVERGENCE_BEAR:
        return True
    if dims.get("v_pb_shrinkage") == V_PB_SHRINK_NO:
        return True
    if (dims.get("momentum_volume") or 0) < 0:
        return True
    return False


def _apply_sector_bonus(c: Candidate, clusters: dict[str, list[str]]):
    sec = classify_sector(c.stock.name)
    c.sector = sec
    if sec != "其他":
        cluster_count = len(clusters.get(sec, []))
        if cluster_count >= 5:
            c.sector_bonus = SECTOR_CLUSTER_BONUS_5
        elif cluster_count >= 4:
            c.sector_bonus = SECTOR_CLUSTER_BONUS_4
        elif cluster_count >= 3:
            c.sector_bonus = SECTOR_CLUSTER_BONUS_3
        elif cluster_count >= 2:
            c.sector_bonus = SECTOR_CLUSTER_BONUS_2


def _apply_intraday_bonus(c: Candidate, intraday_scores: dict[str, float | None]):
    intra = intraday_scores.get(c.stock.symbol)
    if intra is not None:
        c.intraday_score = intra


def _apply_live_vol_bonus(c: Candidate, live_volumes: dict[str, float | None]):
    live_vol = live_volumes.get(c.stock.symbol)
    if live_vol is not None and c.kline and c.kline.avg_volume > 0:
        live_vol_ratio = live_vol / c.kline.avg_volume  # 实时量比 = 今日成交量 / 日均量
        if live_vol_ratio > LIVE_VOL_RATIO_THRESHOLD:
            c.live_vol_bonus = LIVE_VOL_BONUS


def _apply_turnover_bonus(c: Candidate, market_caps: dict[str, dict]):
    if c.market_cap > 0:
        tr = market_caps.get(c.stock.symbol, {}).get("turnover_rate")
        if tr is not None:
            if tr > TURNOVER_HIGH:
                c.turnover_bonus = TURNOVER_BONUS_PENALTY
            elif tr > TURNOVER_MEDIUM:
                c.turnover_bonus = TURNOVER_BONUS_MODERATE
            elif tr > TURNOVER_LOW:
                c.turnover_bonus = TURNOVER_BONUS_HEALTHY


def _apply_sentiment_bonus(c: Candidate, sentiment_info: dict):
    if sentiment_info:
        c.market_sentiment_bonus = sentiment_info.get("bonus", 0)


def _apply_rps_bonus(c: Candidate, rps_scores: dict[str, int]):
    if rps_scores:
        c.rps_bonus = rps_scores.get(c.stock.symbol, 0)


def _apply_market_cap_bonus(c: Candidate):
    mc = c.stock.market_cap
    if mc <= 0:
        return
    if mc <= MCAP_SMALL_THRESHOLD:
        c.market_cap_bonus = MCAP_BONUS_SMALL
    elif mc <= MCAP_MID_THRESHOLD:
        c.market_cap_bonus = MCAP_BONUS_MID


def _apply_gap_up_bonus(c: Candidate):
    if c.kline and c.kline.dimensions:
        gap_key = "new_face_gap_up" if c.category in ("new_face", "known_new_face") else "momentum_gap_up"
        c.gap_up_bonus = c.kline.dimensions.get(gap_key, 0)


def _apply_list_momentum_bonus(c: Candidate, list_streaks: dict[str, int] = None, conn=None):
    intraday_streak = (list_streaks or {}).get(c.stock.symbol, 0)
    if conn:
        from scanner.database import get_consecutive_appearance_days
        cross_days = get_consecutive_appearance_days(conn, c.stock.symbol)
    else:
        cross_days = 0
    streak = max(cross_days, intraday_streak)
    traj = rank_trajectory_score(c.stock.symbol)
    rank = c.stock.rank
    streak_bonus = 0

    if streak >= FATIGUE_STREAK_MIN:
        # 底部反转（new_face）本就期望低 accumulated，跳过价格疲劳信号以免误罚
        is_reversal = c.category in ("new_face", "known_new_face")
        fatigue_signals = 0
        if c.kline and c.kline.accumulated_pct < FATIGUE_PRICE_WARN_ACCUM and not is_reversal:
            fatigue_signals += 1
        if c.kline and c.kline.volume_ratio < FATIGUE_VOL_WARN_RATIO:
            fatigue_signals += 1
        if traj < 2:
            fatigue_signals += 1

        today_pct = c.stock.percent
        accelerating = (
            today_pct >= FATIGUE_ACCELERATE_PCT
            and c.kline and c.kline.volume_ratio > 1.0
        ) if c.kline else False

        if fatigue_signals >= 2:
            penalty = max(streak * FATIGUE_PENALTY_PER_DAY, FATIGUE_PENALTY_CAP)
            streak_bonus = penalty
            if c.kline:
                c.kline.dimensions["fatigue"] = penalty
                c.kline.dimensions["fatigue_detail"] = f"signals_{fatigue_signals}/3_streak_{streak}"
        elif accelerating:
            # 封顶：intraday_streak 是扫描次数（60s/次），cross_days 是交易日数，
            # max 取大值后 streak 可能被 intraday_streak 主导（盘中累计可达 240）。
            # 与 FATIGUE_PENALTY_CAP=-15 对称，加速奖励也设上限，避免分数膨胀。
            streak_bonus = min(streak * FATIGUE_ACCELERATE_BONUS_PER_DAY,
                               FATIGUE_ACCELERATE_BONUS_CAP)
            if c.kline:
                c.kline.dimensions["fatigue"] = streak_bonus
                c.kline.dimensions["fatigue_detail"] = "accelerating"
        else:
            if streak >= 5:
                streak_bonus = LIST_STREAK_BONUS_5
            elif streak >= 3:
                streak_bonus = LIST_STREAK_BONUS_3
            elif streak >= 2:
                streak_bonus = LIST_STREAK_BONUS_2
    else:
        if streak >= 5:
            streak_bonus = LIST_STREAK_BONUS_5
        elif streak >= 3:
            streak_bonus = LIST_STREAK_BONUS_3
        elif streak >= 2:
            streak_bonus = LIST_STREAK_BONUS_2

    traj_bonus = traj
    top40_bonus = 0
    if rank <= TOP40_THRESHOLD:
        top40_bonus = TOP40_BONUS
        advance = (TOP40_THRESHOLD - rank) // 10
        top40_bonus += advance * TOP40_ADVANCE_PER_10
        if rank <= 20:
            top40_bonus += TOP20_EXTRA
    c.list_momentum_bonus = streak_bonus + traj_bonus + top40_bonus
    if c.kline:
        c.kline.dimensions["list_streak_bonus"] = streak_bonus
        c.kline.dimensions["list_traj_bonus"] = traj_bonus
        c.kline.dimensions["list_top40_bonus"] = top40_bonus


def _record_dimensions(
    c: Candidate,
    market_idx_pct: float | None,
    opening_scores: dict[str, float | None],
):
    if not c.kline or c.kline.dimensions is None:
        return
    c.kline.dimensions["sector_bonus"] = c.sector_bonus
    c.kline.dimensions["live_vol_bonus"] = c.live_vol_bonus
    c.kline.dimensions["intraday_score"] = round(c.intraday_score, 1)
    if c.market_cap_bonus != 0:
        c.kline.dimensions["market_cap_bonus"] = c.market_cap_bonus
    if c.market_sentiment_bonus != 0:
        c.kline.dimensions["market_sentiment_bonus"] = c.market_sentiment_bonus
    if c.rps_bonus != 0:
        c.kline.dimensions["rps_bonus"] = c.rps_bonus
    opening = opening_scores.get(c.stock.symbol)
    if opening is not None:
        c.kline.dimensions["opening_score"] = round(opening, 1)
    if c.first_today_bonus:
        c.kline.dimensions["first_today_bonus"] = c.first_today_bonus
    if c.first_breakout_bonus:
        c.kline.dimensions["first_breakout_bonus"] = c.first_breakout_bonus
    if market_idx_pct is not None:
        if market_idx_pct > MARKET_STRONG_THRESHOLD:
            c.kline.dimensions["market_env_bonus"] = MARKET_ENV_STRONG
        elif market_idx_pct < MARKET_WEAK_THRESHOLD:
            c.kline.dimensions["market_env_bonus"] = MARKET_ENV_WEAK
    if c.turnover_bonus:
        c.kline.dimensions["turnover_bonus"] = c.turnover_bonus
    if c.category == "short_term" and c.kline.dimensions.get("v_st_overbought"):
        # 以 validator 决策为准（含今日急拉导致的超买），确保否决在报告中可见；
        # 分析侧不再做超买判定，统一由 validator 单点判断 + enhancer 标记。
        c.kline.dimensions["st_overbought_flag"] = True
    if c.category == "momentum" and c.kline.dimensions.get("v_mo_overbought"):
        # 同 short_term 逻辑：以 validator 决策为准，确保超买标记在报告中可见。
        c.kline.dimensions["mo_overbought_flag"] = True
    if c.time_bonus:
        c.kline.dimensions["time_bonus"] = c.time_bonus
    if c.list_momentum_bonus:
        c.kline.dimensions["list_momentum_bonus"] = c.list_momentum_bonus


def compute_time_bonus(now: datetime | None = None) -> int:
    now = now or now_beijing()
    now_minutes = now.hour * 60 + now.minute
    if now_minutes < EARLY_TRADE_CUTOFF:
        return EARLY_BONUS
    if now_minutes >= LATE_TRADE_START:
        return LATE_BONUS
    return 0


def compute_market_env_bonus(market_idx_pct: float | None) -> int:
    if market_idx_pct is None:
        return 0
    if market_idx_pct > MARKET_STRONG_THRESHOLD:
        return MARKET_ENV_STRONG
    if market_idx_pct < MARKET_WEAK_THRESHOLD:
        return MARKET_ENV_WEAK
    return 0


def accumulate_final_score(c: Candidate, market_env_bonus: int, opening_scores: dict[str, float | None]) -> int:
    opening = opening_scores.get(c.stock.symbol)
    opening_bonus = int(round(opening)) if opening is not None else 0
    cross_source = CROSS_SOURCE_BONUS if c.stock.source_tag == "both" else 0
    total = (c.sector_bonus + c.live_vol_bonus
             + c.first_today_bonus + c.first_breakout_bonus
             + market_env_bonus + c.turnover_bonus + c.time_bonus
             + c.market_sentiment_bonus + c.rps_bonus + c.market_cap_bonus
             + c.list_momentum_bonus + opening_bonus
             + cross_source + c.gap_up_bonus)
    return total
