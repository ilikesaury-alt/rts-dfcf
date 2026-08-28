"""各策略权重表（从 config.py 拆出，集中管理打分权重）。

config.py 仅 re-export 这些名字，保持既有 `from scanner.config import NEW_FACE_WEIGHTS`
导入不破。权重是回测校准（scanner.backtest / scanner.ic_attribution）的主要调参面。
"""

# Scoring weights — used by analysis.py
# Step 2 (2026-08-07, 9826399, merge 24443ff 中丢失后于 2026-08-10 恢复):
# IC 归因重平衡——new_face 是「超卖反转」策略，原权重过度奖励动量确认信号
# （今日大涨 / 放量 / 累计涨幅，cum_3d IC 均为负），真正有预测力的反转触发信号
# （KDJ K<20金叉/J<0、RSI<30、MACD金叉）权重过小。重平衡后 reconstruct_score
# rank-IC：new_face +0.045→+0.109、Combined +0.041→+0.099（ic_attribution.py）。
# 2026-08-10 独立复核：KDJ超卖金叉触发组 cum_3d +0.54 vs 未触发 -0.52（IC +0.42）、
# 放量 >1.3 触发组 -2.83 vs 未触发 -0.51（IC ≈0），方向一致。
NEW_FACE_WEIGHTS: dict[str, int] = {
    "today_pct_2_6": 8,
    "today_pct_1_2": 6,
    "today_pct_0_5_1": 4,
    "today_pct_lt_0_5": 3,
    "today_pct_6_8": 5,
    "today_pct_gt_8": -10,  # P2: -15→-10，8%是创业板正常强势区间，过重惩罚会误杀强势反转
    "accum_neg5_10": 6,
    "accum_lt_neg5": 0,
    "accum_10_15": 3,
    "accum_15_20": -5,
    "bottom_confirmed": 0,
    "v_shape": 8,
    "volume_surge": 0,
    "value_gte_10000": 2,
    "value_gte_5000": 1,
    "rsi_bonus": 5,
    "macd_bonus": 6,
    "rsi14_oversold_bonus": 4,
    "bollinger_oversold": 5,
    "kdj_bonus": 6,
    "atr_contraction": 2,
    "obv_not_negative": 3,
}

# P0 IC 重平衡 (2026-08-08, e3ee10e4, merge 24443ff 中丢失后于 2026-08-10 恢复)。
# 依据 cum_3d 口径 dimension_ic（当前库复核一致）：
#   momentum_volume -0.32 强反指 → 健康量能清零；momentum_value +0.22 正指 → 提权；
#   momentum_macd -0.21 / momentum_rsi -0.06 / momentum_accumulated -0.09 反指 → 清零/降权；
#   momentum_adx +0.22 强正指 → 提权；momentum_kdj ≈0 → 中性略提。
MOMENTUM_WEIGHTS: dict[str, int] = {
    "today_pct_2_6": 20,
    "today_pct_1_2": 10,
    "today_pct_0_5_1": 5,
    "today_pct_lt_0_5": 5,
    "today_pct_6_8": 5,
    "today_pct_8_10": 3,   # P1-2: 新增 8-10% 档（加速赶顶风险，进一步降权）
    "accum_10_15": 8,
    "accum_15_20": 5,
    "accum_20_30": 3,
    "accum_gte_30": -15,
    "vol_healthy": 0,
    "vol_surge": 0,
    "vol_low": -3,
    "value_gte_10000": 5,
    "value_gte_5000": 2,
    "rsi_bonus": 0,
    "kdj_bonus": 4,
    "macd_bonus": 0,
    "adx_bonus": 7,
    "adx_weak": -3,
    "atr_healthy": 0,
    "atr_overheated": -3,
    "obv_uptrend": 3,
    # 首次启动子模式专用权重（accumulated ∈ [0,7), today_pct ∈ [3.5,8]）
    # 与常规 momentum 的 today_pct_6_8/accum_10_15 语义不同，单独定义避免键名与值域错位
    "launch_today_pct": 5,  # 首次启动今日涨幅档（3.5-8%，中性偏正）
    "launch_accum": 8,      # 首次启动累计涨幅档（0-7%，刚启动低位）
}

SHORT_TERM_WEIGHTS: dict[str, int] = {
    "today_pct_2_4": 15,
    # 2026-08-10: 4-6% 20→8（分桶最差档：41 条 cum_3d -1.41%，权重却是最高）、
    # 8-12% 8→15（分桶最好档：21 条 +3.84%，接近涨停梯队次日惯性）——按数据反向修正，
    # 替换原"涨幅偏大降权"的拍脑袋设定。2-4% / 6-8% 保持（+0.43% / +0.45% 中性）。
    "today_pct_4_6": 8,
    "today_pct_6_8": 12,
    "today_pct_8_12": 15,   # P1-1: 8-12% 档（2026-08-10 由 8 上调，数据支持）
    "accum_0_5": 5,      # 2026-08-17 审计修复：原 [0,5) 区间无分支，静默漏 accumulation 分
    "accum_5_10": 10,
    "accum_10_15": 15,
    "accum_15_20": 8,
    "accum_gte_20": -5,
    "accum_lt_0": -5,
    "vol_healthy": 8,
    "vol_surge": 12,
    "vol_low": -5,
    "value_small_cap": 6,
    "value_mid_cap": 2,
    "st_weak_to_strong": 8,
    "rsi_bonus": 3,
    "kdj_bonus": 3,
    "macd_bonus": 3,
    "rank_top10": 8,
    "rank_top20": 5,
    "rank_top30": 3,
}

REBOUND_WEIGHTS: dict[str, int] = {
    # 今日企稳阳线涨幅档（温和涨幅为主，避免追高）
    "today_pct_0_5_2": 15,      # 0.5~2%：温和企稳
    "today_pct_2_4": 18,        # 2~4%：明显企稳
    "today_pct_4_6": 12,        # 4~6%：较强企稳
    "today_pct_6_8": 5,         # 6~8%：涨幅偏大降权
    # 超跌深度档（越深反弹空间越大）
    "drop_15_20": 10,           # 前5日累计跌15~20%
    "drop_20_30": 15,           # 跌20~30%
    "drop_gte_30": 20,          # 跌≥30%
    "crash_day_bonus": 5,       # 前5日有单日暴跌(≤-10%)额外加分
    # 量能配合
    "vol_healthy": 8,           # 量比1.0~2.0：正常企稳量能
    "vol_surge": 12,            # 量比≥2.0：放量企稳（主力介入）
    "vol_low": -3,              # 量比<0.8：缩量企稳不可信
    # 技术面确认
    "rsi_oversold": 8,          # RSI<30：超卖反弹
    "rsi_mid": 3,               # RSI 30~50：低位企稳
    "bollinger_lower": 5,       # 触及BOLL下轨
    "v_shape": 8,               # V型反转特征（缩量低点+放量阳线）
    # 板块/市值
    "sector_active": 5,         # 同板块≥3只（板块共振）
    "value_small_cap": 4,       # 小盘弹性大
    "value_mid_cap": 2,
}
