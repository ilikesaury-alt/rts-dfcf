from datetime import datetime

from scanner.config import (
    DISTRIBUTION_ACCUM_HIGH,
    DISTRIBUTION_ACCUM_MID,
    DISTRIBUTION_ACCUM_PULLBACK,
    DISTRIBUTION_INTRADAY_WEAK,
    DISTRIBUTION_OPENING_STRONG,
    DISTRIBUTION_RANK_WEAK_INTRADAY,
    DISTRIBUTION_TODAY_PCT_LOW,
    DISTRIBUTION_VOL_RATIO,
    EARLY_BONUS,
    EARLY_TRADE_CUTOFF,
    FATIGUE_ACCELERATE_BONUS_CAP,
    FATIGUE_ACCELERATE_BONUS_PER_DAY,
    FATIGUE_ACCELERATE_PCT,
    FATIGUE_PENALTY_CAP,
    FATIGUE_PENALTY_PER_DAY,
    FATIGUE_PRICE_WARN_ACCUM,
    FATIGUE_STREAK_MIN,
    FATIGUE_VOL_WARN_RATIO,
    FUND_FLOW_BONUS_WEAK,
    FUND_FLOW_MAIN_PCT_WEAK,
    FUND_OUTFLOW_NET_PCT,
    FUND_RISK_TAG,
    LATE_BONUS,
    LATE_TRADE_START,
    LIST_STREAK_BONUS_2,
    LIST_STREAK_BONUS_3,
    LIST_STREAK_BONUS_5,
    LIVE_VOL_BONUS,
    LIVE_VOL_RATIO_THRESHOLD,
    MARKET_ENV_STRONG,
    MARKET_ENV_WEAK,
    MARKET_STRONG_THRESHOLD,
    MARKET_WEAK_THRESHOLD,
    MCAP_BONUS_MID,
    MCAP_BONUS_SMALL,
    MCAP_MID_THRESHOLD,
    MCAP_SMALL_THRESHOLD,
    OVERVALUED_ACCUM_THRESHOLD,
    SECTOR_CLUSTER_BONUS_2,
    SECTOR_CLUSTER_BONUS_3,
    SECTOR_CLUSTER_BONUS_4,
    SECTOR_CLUSTER_BONUS_5,
    TOP20_EXTRA,
    TOP40_ADVANCE_PER_10,
    TOP40_BONUS,
    TOP40_THRESHOLD,
    TURNOVER_BONUS_HEALTHY,
    TURNOVER_BONUS_MODERATE,
    TURNOVER_BONUS_PENALTY,
    TURNOVER_HIGH,
    TURNOVER_LOW,
    TURNOVER_MEDIUM,
    V_MO_DIVERGENCE_BEAR,
    V_MO_MA_NONE,
    V_MO_VOL_SPIKE,
    V_ST_MA_BROKEN,
    V_ST_RANK_LOW,
    WTS_FAIL_TAG,
    ZT_LIANBAN_BONUS_2,
    ZT_LIANBAN_BONUS_3,
    ZT_LIANBAN_GT3_PENALTY,
    ZT_ZHA_BAN_MIN,
    now_beijing,
)
from scanner.database import get_consecutive_appearance_days_batch, get_prominence_map
from scanner.models import Candidate
from scanner.rank_trend import rank_trajectory_score
from scanner.sector import classify_sector
from scanner.utils import to_float, to_int


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
    market_extra: dict = None,
    fund_risk: dict[str, str] = None,
    conn=None,
):
    syms = [c.stock.symbol for c in candidates]
    # N+1 → 批量：连续上榜天数 / 辨识度各一次 SQL 查询（此前每个候选各发一条）
    cross_days_map = get_consecutive_appearance_days_batch(conn, syms) if conn else {}
    prominence_map = get_prominence_map(conn, syms) if conn else {}
    for c in candidates:
        _apply_sector_bonus(c, clusters)
        _apply_intraday_bonus(c, intraday_scores)
        _apply_live_vol_bonus(c, live_volumes)
        _apply_turnover_bonus(c, market_caps)
        _apply_sentiment_bonus(c, sentiment_info)
        _apply_rps_bonus(c, rps_scores)
        _apply_market_cap_bonus(c)
        _apply_list_momentum_bonus(c, list_streaks, cross_days=cross_days_map.get(c.stock.symbol, 0))
        c.time_bonus = time_bonus
        _apply_gap_up_bonus(c)
        _apply_fund_flow_bonus(c, market_extra)
        _apply_zt_bonus(c, market_extra)
        _record_dimensions(c, market_idx_pct, opening_scores)
        _set_risk_flags(c, fund_risk=fund_risk)
        _compute_prominence_labels(c, prominence_map)


def _compute_prominence_labels(c: Candidate, prominence_map: dict):
    """辨识度标签（↻）：复用 get_prominence_map 批量结果，口径与 display 一致。"""
    if not prominence_map:
        return
    try:
        if prominence_map.get(c.stock.symbol):
            c.prominence_labels.append("\u21bb")
    except Exception:
        pass


def _set_risk_flags(c: Candidate, fund_risk: dict[str, str] = None):
    """设置复合风险标签，供 UI 显示⚠️标记。

    每个标签对应明确的交易决策含义，基于多字段组合判断。
    不清零加分（基础评分维度清零会破坏策略逻辑），仅加风险标签供人工判断。
    """
    dims = c.kline.dimensions if c.kline else {}

    # 硬过滤命中标签收集（仅 RISK_FLAGS_HARD_FILTER 成员），写 excluded_reason 落库审计。
    # 与展示型标签（超买/疲劳/弱市/资金流出/炸板）区分：后者不进 excluded_reason。
    hard_hits: list[str] = []

    # 财务风险：资不抵债等基本面硬伤（排除式硬过滤，2026-08-12 新增）。
    # fund_risk 由 orchestrator 从 pywencai 问财反向查询全市场资不抵债股获得，
    # 命中即打 FUND_RISK_TAG 标签，RISK_FLAGS_HARD_FILTER 据此移出推荐列表。
    if fund_risk and c.stock.symbol in fund_risk:
        reason = f"{FUND_RISK_TAG}:{fund_risk[c.stock.symbol]}"
        c.risk_flags.append(FUND_RISK_TAG)
        hard_hits.append(reason)

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
        hard_hits.append("主力出货")
    # 弱转强失效（2026-08-14 新增）：弱转强直通特权的前提是"今日转强成功"，
    # 分时明确走弱（intraday<=-1.0，与 Rule 3 冲高回落同阈值）即转强失败。
    # 全期 12 样本：大跌(≤-7%) 25% vs 大涨 8.3%，平均次日 -2.61%，含 -16.34/-18.44
    # 两个极端日（均弱转强+盘中弱）→ 硬过滤（RISK_FLAGS_HARD_FILTER 移出推荐列表）。
    if (
        (dims.get("v_st_weak") or 0) > 0
        and c.intraday_score is not None
        and c.intraday_score <= DISTRIBUTION_INTRADAY_WEAK
    ):
        c.risk_flags.append(WTS_FAIL_TAG)
        hard_hits.append(WTS_FAIL_TAG)
    # 趋势破位：MA 破位合并标签（止损信号）
    if _detect_trend_breakage(dims):
        c.risk_flags.append("趋势破位")
        hard_hits.append("趋势破位")
    # 涨幅过大：追高风险
    if _detect_overvalued(c):
        c.risk_flags.append("涨幅过大")
    # 量价背离：量价不匹配（含顶背离）
    if _detect_volume_price_divergence(c, dims):
        c.risk_flags.append("量价背离")
    # 资金流出：主力净流出占比超阈值（展示型警告，非硬过滤）
    if (dims.get("fund_flow_main_pct") or 0) <= FUND_OUTFLOW_NET_PCT:
        c.risk_flags.append("资金流出")
    # 炸板：今日曾涨停但盘中炸板（封板未稳，追高/筹码松动风险，展示型警告）
    if (dims.get("zt_zhaban") or 0) >= ZT_ZHA_BAN_MIN:
        c.risk_flags.append("炸板")

    # 落库审计：硬过滤命中标签串（逗号分隔）。被硬过滤砍的票从 DB 可反推原因，
    # 消除"无审计依据的误杀"盲点（08-19 复盘发现 excluded=1 但 breakdown 无任何
    # 硬过滤信号，因 risk_flags 此前从不落库）。
    if hard_hits:
        c.excluded_reason = ",".join(hard_hits)


def _detect_main_force_distribution(c: Candidate, dims: dict) -> bool:
    """识别主力高位派发迹象。

    五种经典出货模式（满足任一即判定）：
    1. 高位放量滞涨：累计涨幅大 + 量比高 + 今日几乎不涨（量价背离派发）
    2. 高位高换手+超买：高位 + 高换手 + 超买（借势派发）
    3. 冲高回落：开盘强势但分时走弱 + 已有累计涨幅（盘中冲高出货）
    4. 爆量+顶背离：量能爆量 + 顶背离（经典量价顶背离派发）
    5. 后排+盘中走弱：short_term 后排上榜（rank>30）且分时持续走弱——
       边际放量票开盘强盘中弱 = 冲高派发，累计涨幅不足 15% 也判定（2026-08-14 新增）。
    """
    accum = c.kline.accumulated_pct if c.kline else 0.0
    vol_ratio = c.kline.volume_ratio if c.kline else 1.0
    today_pct = c.stock.percent
    opening = dims.get("opening_score")
    intraday = c.intraday_score
    overbought = bool(dims.get("st_overbought_flag") or dims.get("mo_overbought_flag"))

    # 1. 高位放量滞涨：累计高位 + 明显放量（量比≥2.5）+ 今日几乎不涨（量价背离派发）
    if (
        accum >= DISTRIBUTION_ACCUM_HIGH
        and vol_ratio >= DISTRIBUTION_VOL_RATIO
        and today_pct <= DISTRIBUTION_TODAY_PCT_LOW
    ):
        return True
    # 2. 高位高换手+超买：累计≥15% + 真正过热换手（turnover>20% → turnover_bonus<0）
    #    + 已收紧的"极端超买"。原逻辑仅要求换手>5%（活跃常态）+ 宽松超买，
    #    把任何活跃强势股都误判为出货，2026-07-28 收紧为 genuine 派发级条件。
    if accum >= DISTRIBUTION_ACCUM_MID and c.turnover_bonus < 0 and overbought:
        return True
    # 3. 冲高回落（opening_score 范围 -5~5，intraday_score 范围 -10~10）
    #    intraday None 守卫与 Rule 5 对齐（2026-08-24 审查：当前默认 0.0 不可达，
    #    但字段类型语义上可空，缺守卫会在 apply_all_bonuses 循环内抛 TypeError
    #    中断整轮 bonus）。
    if (
        opening is not None
        and opening >= DISTRIBUTION_OPENING_STRONG
        and intraday is not None
        and intraday < DISTRIBUTION_INTRADAY_WEAK
        and accum >= DISTRIBUTION_ACCUM_PULLBACK
    ):
        return True
    # 4. 爆量+顶背离（validator 判定的经典出货信号）
    if dims.get("v_mo_volume") == V_MO_VOL_SPIKE and dims.get("v_mo_divergence") == V_MO_DIVERGENCE_BEAR:
        return True
    # 5. 后排+盘中走弱（short_term 专属，2026-08-14 新增）
    #    dims["v_st_rank"]==V_ST_RANK_LOW 自带 short_term 语义（仅 validate_short_term
    #    写该字段）且 rank>30；叠加分时持续走弱 → 冲高派发。
    #    历史校准：12 样本 next_day -1.90%/胜率25%、cum_3d -4.24%/胜率12%（n=8），
    #    11 股/7 交易日分布（07-20~08-13），非单票集中；弱转强直通亦不可豁免。
    if dims.get("v_st_rank") == V_ST_RANK_LOW and intraday is not None and intraday <= DISTRIBUTION_RANK_WEAK_INTRADAY:
        return True
    return False


def _detect_trend_breakage(dims: dict) -> bool:
    """识别 MA 趋势破位（止损信号）。

    合并 MA 破位场景（满足任一即判定）：
    - momentum MA 空头排列（v_mo_ma == V_MO_MA_NONE）
    - short_term 跌破 MA5（v_st_ma == V_ST_MA_BROKEN）
    """
    if dims.get("v_mo_ma") == V_MO_MA_NONE:
        return True
    if dims.get("v_st_ma") == V_ST_MA_BROKEN:
        return True
    return False


def _detect_overvalued(c: Candidate) -> bool:
    """识别涨幅过大（追高风险）。

    满足任一即判定：
    - 累计涨幅 >= OVERVALUED_ACCUM_THRESHOLD
    - momentum 累计>=30% 惩罚已触发（momentum_accumulated <= -15）
    """
    accum = c.kline.accumulated_pct if c.kline else 0.0
    if accum >= OVERVALUED_ACCUM_THRESHOLD:
        return True
    dims = c.kline.dimensions if c.kline else {}
    if (dims.get("momentum_accumulated") or 0) <= -15:
        return True
    return False


def _detect_volume_price_divergence(c: Candidate, dims: dict) -> bool:
    """识别量价背离（量价不匹配）。

    满足任一即判定：
    - 顶背离（v_mo_divergence == V_MO_DIVERGENCE_BEAR）：价格创新高但指标不创新高
    - momentum 缩量（momentum_volume < 0）：动量延续却缩量，上涨动能不足
    """
    if dims.get("v_mo_divergence") == V_MO_DIVERGENCE_BEAR:
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


def _safe_float(v, default: float = 0.0) -> float:
    """安全转 float：None/NaN/±inf/不可解析字符串 → default（统一走 utils.to_float）。

    数据入口防御，防整轮扫描异常丢失。
    """
    return to_float(v, default)


def _safe_int(v, default: int = 0) -> int:
    """安全转 int：None/不可解析字符串/浮点 → 就近取整（统一走 utils.to_int）。"""
    return to_int(v, default)


def _apply_fund_flow_bonus(c: Candidate, market_extra: dict):
    """主力资金流评分：主力净流入占比 ≥阈值加分，净流出明显扣分。

    原始数据写入 dimensions（fund_flow_*），供展示与 backtest dimension_ic 归因。
    无数据（market_extra 缺失/该票无记录）时零影响。
    """
    entry = ((market_extra or {}).get(c.stock.symbol, {}) or {}).get("fund_flow")
    if not entry or not c.kline:
        return
    main_pct = _safe_float(entry.get("main_pct"))
    c.kline.dimensions["fund_flow_main_pct"] = round(main_pct, 2)
    c.kline.dimensions["fund_flow_main_net"] = _safe_float(entry.get("main_net"))
    c.kline.dimensions["fund_flow_super_net"] = _safe_float(entry.get("super_net"))
    if main_pct <= FUND_FLOW_MAIN_PCT_WEAK:
        c.fund_flow_bonus = FUND_FLOW_BONUS_WEAK


def _apply_zt_bonus(c: Candidate, market_extra: dict):
    """涨停池评分：连板数加分（动量/超短），≥4 板追高降权。

    连板信息也写入 dimensions（zt_*），供展示与风险标签使用。
    """
    entry = ((market_extra or {}).get(c.stock.symbol, {}) or {}).get("zt")
    if not entry or not c.kline:
        return
    lianban = _safe_int(entry.get("lianban"))
    c.kline.dimensions["zt_lianban"] = lianban
    c.kline.dimensions["zt_zhaban"] = _safe_int(entry.get("zhaban"))
    c.kline.dimensions["zt_industry"] = str(entry.get("industry") or "")
    if c.category in ("momentum", "short_term"):
        if lianban >= 4:
            c.zt_lianban_bonus = ZT_LIANBAN_GT3_PENALTY
        elif lianban == 3:
            c.zt_lianban_bonus = ZT_LIANBAN_BONUS_3
        elif lianban == 2:
            c.zt_lianban_bonus = ZT_LIANBAN_BONUS_2


def _apply_list_momentum_bonus(c: Candidate, list_streaks: dict[str, int] = None, cross_days: int = 0):
    if c.off_list:
        # 掉榜跟踪票（回马枪）整体豁免榜单动能：cross_days/盘中 streak 是掉榜前残留
        # （跟踪池最长保留 WATCH_OFFLIST_KEEP_DAYS=15 交易日，连榜早已结束）；traj 来自
        # 掉榜前排名快照；回踩变体的 volume_ratio 还是合成占位值 0.0——三者都不构成
        # 真实榜单动能。若不豁免：0.0 < FATIGUE_VOL_WARN_RATIO 恒真 + 掉榜票排名走低的
        # traj<0，两个"疲劳信号"叠加即误触疲劳惩罚与「疲劳」风险标签（2026-08-14 修复，
        # 与 rank=0 豁免 TOP40 路径同族）。
        c.list_momentum_bonus = 0
        return
    intraday_streak = (list_streaks or {}).get(c.stock.symbol, 0)
    # streak 以"交易日"计：cross_days 是历史连续上榜天数（不含今日，由调用方批量查询），
    # intraday_streak 是本次盘中连续扫描次数（60s/次），仅作为"今日上榜"=+1 天。
    # 绝不能把扫描次数当天数：盘中可达 240，max() 取大后疲劳/加速评分恒饱和 ±15。
    streak = cross_days + (1 if intraday_streak >= 1 else 0)
    traj = rank_trajectory_score(c.stock.symbol)
    rank = c.stock.rank
    streak_bonus = 0

    if streak >= FATIGUE_STREAK_MIN:
        # 底部反转类本就期望低 accumulated，跳过价格疲劳信号以免误罚。
        # new_face/known_new_face：新面孔底部突破；comeback 反转变体：掉榜 5 日跌≤-8% 后企稳；
        # rebound：超跌反弹（5日跌≤-10% 后企稳），负累计是策略核心前提——按价格判疲劳等于
        # 惩罚策略本身（与 RPS 豁免同理由，candidates.compute_rps 对 rebound 返回 0）。
        # 2026-08-17 审查修复：此前漏掉 rebound，连榜≥3 天时 accumulated_pct<8 恒真，
        # 叠加低量比易凑 2 信号 → 误打「疲劳」风险标签。
        is_reversal = c.category in ("new_face", "known_new_face", "comeback", "rebound")
        fatigue_signals = 0
        if c.kline and c.kline.accumulated_pct < FATIGUE_PRICE_WARN_ACCUM and not is_reversal:
            fatigue_signals += 1
        if c.kline and c.kline.volume_ratio < FATIGUE_VOL_WARN_RATIO:
            fatigue_signals += 1
        # 仅真实下行轨迹（trajectory_score < 0）算疲劳信号；
        # 0=历史不足 2 个快照（每日开盘 tracker.reset 后）不算，避免全量误判疲劳。
        if traj < 0:
            fatigue_signals += 1

        today_pct = c.stock.percent
        accelerating = (
            (today_pct >= FATIGUE_ACCELERATE_PCT and c.kline and c.kline.volume_ratio > 1.0) if c.kline else False
        )

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
            streak_bonus = min(streak * FATIGUE_ACCELERATE_BONUS_PER_DAY, FATIGUE_ACCELERATE_BONUS_CAP)
            if c.kline:
                # 2026-08-17 审查修复：加速分支此前把正值写进 dims["fatigue"]——
                # 该键语义是「疲劳惩罚」（_set_risk_flags 判 <0、backtest dimension_ic
                # 按整列归因），正值混入会污染"疲劳"维度 IC（加速奖励被解析进疲劳因子）。
                # 改写入独立键 fatigue_accelerate，与惩罚键分离，正负语义不再混用。
                c.kline.dimensions["fatigue_accelerate"] = streak_bonus
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
    # rank>0 前提：回马枪掉榜票（comeback）rank=0，无榜单排名，不能把它当作"榜上第 1 名"
    # 计 TOP40/top20 加分（此前掉榜票拿的榜单动能加分反而超过真正的榜上前 40，分失真）。
    if 0 < rank <= TOP40_THRESHOLD:
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
    if c.fund_flow_bonus:
        c.kline.dimensions["fund_flow_bonus"] = c.fund_flow_bonus
    if c.zt_lianban_bonus:
        c.kline.dimensions["zt_lianban_bonus"] = c.zt_lianban_bonus
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


# 热度放大器 bonus：仅作「展示徽章」，不再进入排序键。
# 理由（2026-08-07 code review）：RPS/板块集群/榜单动量(top40+轨迹)/实时量比/双榜/市场情绪/
#       时间/市场环境 均与「已经涨起来的热票」高度相关，叠加后综合排序≈系统性追涨，
#       回测显示综合(评分筛选) -28.5% 反而劣于无筛选基准 -14.3%。
#       这些项仍通过 _record_dimensions 写入 c.kline.dimensions（即 recommendations.score_breakdown JSON），
#       展示层继续可见，只是不再参与 c.score 排序键。
HEAT_AMPLIFIER_BONUS_ATTRS = (
    "sector_bonus",  # 板块集群
    "live_vol_bonus",  # 实时量比
    "rps_bonus",  # RPS（近期涨幅百分位）
    "list_momentum_bonus",  # 榜单动量（连板+轨迹+top40）
    "time_bonus",  # 盘中时段
    "market_sentiment_bonus",  # 市场情绪（全市场，非个股）
    # cross_source / market_env_bonus 在下方累加时显式排除（非 c 属性）
)


def accumulate_final_score(c: Candidate, opening_scores: dict[str, float | None]) -> int:
    """返回**排序键**应累加的 bonus 之和。

    排序键只保留「个股质量 / 策略信号」类 bonus，确保：
      1. 类内排序由策略信号驱动，而非「谁更热」；
      2. 跨类别综合排序时，各类别标尺差异不会由热度放大器进一步放大。
    热度放大器（见 HEAT_AMPLIFIER_BONUS_ATTRS）已写入 dimensions 供展示，不在此累加。
    market_env_bonus（市场环境，全市场非个股）属热度放大器，仅作展示维度，不入排序键。
    """
    opening = opening_scores.get(c.stock.symbol)
    opening_bonus = int(round(opening)) if opening is not None else 0
    total = (
        c.first_today_bonus
        + c.first_breakout_bonus
        + c.turnover_bonus
        + c.market_cap_bonus
        + c.gap_up_bonus
        + c.fund_flow_bonus
        + c.zt_lianban_bonus
        + opening_bonus
    )
    return total
