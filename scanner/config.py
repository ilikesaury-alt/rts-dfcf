import os
from datetime import datetime, time as dtime, timezone, timedelta

BEIJING_TZ = timezone(timedelta(hours=8), name="CST")


def now_beijing() -> datetime:
    """Return current datetime in Beijing timezone (UTC+8)."""
    return datetime.now(BEIJING_TZ)

REFRESH_INTERVAL = 60
REQUEST_TIMEOUT = 15
NEW_FACE_LOOKBACK_DAYS = 3

# Normal mode thresholds
NEW_FACE_MIN_SCORE = 18
MOMENTUM_MIN_SCORE = 16
PULLBACK_MIN_SCORE = 18  # 保留常量供 analyze_pullback 测试使用，orchestrator 已下线 pullback
SHORT_TERM_MIN_SCORE = 15
REBOUND_MIN_SCORE = 18

YI = 100_000_000
MAX_MARKET_CAP = 500 * YI
MAX_STOCK_PRICE = 200.0
MAX_NEW_FACE_TODAY_PCT = 12
MAX_MOMENTUM_TODAY_PCT = 10  # P1-2: 8→10，让 9-10% 加速票能进 momentum（主升浪中段）
PULLBACK_MAX_TODAY_PCT = 0.0      # 仅今日平盘/下跌才算回调，消除 today∈(0,2] 死区
SHORT_TERM_MIN_TODAY_PCT = 2.0
SHORT_TERM_MAX_TODAY_PCT = 12.0  # P1-1: 8→12，覆盖 8-12% 强势股（创业板涨停 20% 仍排除）
# 超跌反弹：今日企稳阳线（温和涨幅），前期暴跌
REBOUND_MIN_TODAY_PCT = 0.5
REBOUND_MAX_TODAY_PCT = 8.0
REBOUND_CRASH_THRESHOLD = -10.0      # 前5日内至少一日跌幅 ≤ 此值（有暴跌日额外加分）
REBOUND_5D_DROP_THRESHOLD = -10.0    # 前5日累计跌幅 ≤ 此值即进入 rebound 评估
                                      # -10~-15% 无暴跌日 = 阴跌企稳场景（P0-1 修复）
REBOUND_NEAR_LOW_PCT = 0.10          # 收盘距20日低点 ≤ 此比例

# 超短同板块数量上限：板块普涨日防止单板块淹没超短列表（P0-69 后再加一道闸）
SHORT_TERM_MAX_PER_SECTOR = 2

# 弱转强（分歧转一致）判定阈值
ST_SMALL_CAP = 100          # 流通市值 ≤100亿 视为小盘（超短偏好）
ST_MID_CAP = 300            # 100~300亿 中盘，>300亿 超短弹性差不加分
ST_DIVERGE_UPPER_SHADOW = 0.04   # 昨日上影线比例阈值
ST_DIVERGE_CLOSE_WEAK = 0.03     # 收盘/最高 - 1 < 此值 视为未封住高位
ST_BOMB_HIGH = 0.18              # 昨日最高/前收 - 1 ≥ 此值 视为曾触板（创业板≈20%）
ST_BOMB_CLOSE = 0.10             # 昨日收盘/前收 - 1 < 此值 视为收盘大回落（炸板/烂板）

# Vol-rank combo scoring thresholds
VOL_RANK_VOL_THRESHOLD = 1.5
VOL_RANK_STRONG_RC = 2000
VOL_RANK_MEDIUM_RC = 1000
VOL_RANK_WEAK_RC = 500
VOL_RANK_STRONG_PTS = 15
VOL_RANK_MEDIUM_PTS = 12
VOL_RANK_WEAK_PTS = 8

# Peak volume ratio thresholds (current / max volume in lookback window)
VOL_PEAK_LOOKBACK = 20
VOL_PEAK_MOMENTUM_WARN = 0.5  # volume < 50% of peak → momentum exhaustion
VOL_PEAK_NEW_FACE_MIN = 0.3   # volume < 30% of peak → insufficient reversal volume
VOL_PEAK_PULLBACK_CONFIRM = 0.3  # volume < 30% of peak → genuine shrinkage

# Peak volume ratio scoring (analysis.py hardcoded values migrated)
VOL_PEAK_NEW_FACE_PENALTY = -5   # new_face vol_peak < threshold → penalty
VOL_PEAK_MOMENTUM_PENALTY = -8   # momentum vol_peak < threshold → penalty
VOL_PEAK_PULLBACK_BONUS = 5     # pullback vol_peak < threshold → shrinkage bonus

# MA bull extra bonus (pullback)
MA_BULL_EXTRA_BONUS = 5

# Analysis.py thresholds — previously hardcoded in analysis.py, centralized for tuning
# Weak-form filter thresholds
WEAK_FORM_MIN_DOWN_DAYS = 3
WEAK_FORM_MAX_ACCUM = 5
WEAK_FORM_MIN_ACCUM = -5
WEAK_FORM_MAX_TODAY_PCT = 3
WEAK_FORM_CRASH_THRESHOLD = -10

# Gap-up thresholds
GAP_UP_STRONG = 2.0
GAP_UP_MEDIUM = 1.0
GAP_UP_WEAK = 0.5
GAP_UP_STRONG_PTS = 8
GAP_UP_MEDIUM_PTS = 5
GAP_UP_WEAK_PTS = 3

# Bottom confirmation thresholds
BOTTOM_MAX_LOSS = -3.0
BOTTOM_VOL_SURGE = 1.5
BOTTOM_NEAR_LOW_PCT = 0.08

# Crash detection thresholds
CRASH_THRESHOLD = -12.0
RECENT_2_RETURN_THRESHOLD = -3.0
NO_CRASH_SAFE_BONUS = 8      # 拆分自 no_crash：无 crash day 基础安全分
RECENT_2D_BONUS = 5           # 拆分自 no_crash：近2日不差附加分
MOMENTUM_VOL_HEALTHY_MIN = 0.7
MOMENTUM_VOL_HEALTHY_MAX = 2.0

# MA alignment scoring
MA_BULL_3_TIER_SCORE = 6
MA_BULL_2_TIER_SCORE = 3
MA_BEAR_SCORE = -3

# List momentum scoring (consecutive surge list appearances + rank trajectory)
LIST_STREAK_BONUS_2 = 3
LIST_STREAK_BONUS_3 = 5
LIST_STREAK_BONUS_5 = 6
TOP40_THRESHOLD = 40
TOP40_BONUS = 3
TOP40_ADVANCE_PER_10 = 2
TOP20_EXTRA = 2

FEISHU_WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/d0caf1dd-54b6-4b86-b83d-861e4c79afda"
FEISHU_KEYWORD = "lichun"
FEISHU_MIN_INTERVAL = 300  # 飞书最小推送间隔（秒），防止触发 Lark 限流

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://xueqiu.com/",
}

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "scanner.db")
LOG_DIR = os.path.join(BASE_DIR, "logs")

MORNING_START = dtime(9, 30)
MORNING_END = dtime(11, 30)
AFTERNOON_START = dtime(13, 0)
AFTERNOON_END = dtime(15, 0)

# Scoring weights — used by analysis.py
NEW_FACE_WEIGHTS: dict[str, int] = {
    "today_pct_2_6": 20,
    "today_pct_1_2": 10,
    "today_pct_0_5_1": 5,
    "today_pct_lt_0_5": 5,
    "today_pct_6_8": 5,
    "today_pct_gt_8": -15,
    "accum_neg5_10": 10,
    "accum_lt_neg5": 0,
    "accum_10_15": 5,
    "accum_15_20": -5,
    "bottom_confirmed": 0,
    "v_shape": 10,
    "volume_surge": 10,
    "value_gte_10000": 2,
    "value_gte_5000": 1,
    "rsi_bonus": 3,
    "macd_bonus": 3,
    "rsi14_oversold_bonus": 3,
    "bollinger_oversold": 4,
    "kdj_bonus": 1,
    "atr_contraction": 2,
    "obv_not_negative": 2,
}
# NOTE: new_face_kdj 已降权（3->1）。回测 IC=-0.184 但 n=30（小样本），
# 仅弱化不消除，待累积样本后再评估是否清零。

MOMENTUM_WEIGHTS: dict[str, int] = {
    "today_pct_2_6": 20,
    "today_pct_1_2": 10,
    "today_pct_0_5_1": 5,
    "today_pct_lt_0_5": 5,
    "today_pct_6_8": 5,
    "today_pct_8_10": 3,   # P1-2: 新增 8-10% 档（加速赶顶风险，进一步降权）
    "accum_10_15": 15,
    "accum_15_20": 10,
    "accum_20_30": 5,
    "accum_gte_30": -15,
    "vol_healthy": 2,
    "vol_surge": 0,
    "vol_low": -3,
    "value_gte_10000": 3,
    "value_gte_5000": 1,
    "rsi_bonus": 3,
    "kdj_bonus": 3,
    "macd_bonus": 3,
    "adx_bonus": 5,
    "adx_weak": -3,
    "atr_healthy": 3,
    "atr_overheated": -3,
    "obv_uptrend": 3,
}

PULLBACK_WEIGHTS: dict[str, int] = {
    "today_neg1_0": 10,
    "today_neg3_neg1": 15,
    "today_neg5_neg3": 8,
    "accum_5_10": 10,
    "accum_10_20": 18,
    "accum_20_30": 8,
    "accum_gte_30": -10,
    "vol_healthy": 12,
    "vol_low": 0,
    "vol_surge": -8,
    "ma_support": 12,
    "ma_broken": -10,
    "rsi_oversold": 5,
    "rsi_mid": 3,
    "macd_bonus": 3,
    "rank_top10": 8,
    "rank_top30": 5,
    "kdj_bonus": 3,
    "bollinger_mid_support": 5,
}

SHORT_TERM_WEIGHTS: dict[str, int] = {
    "today_pct_2_4": 15,
    "today_pct_4_6": 20,
    "today_pct_6_8": 12,
    "today_pct_8_12": 8,   # P1-1: 新增 8-12% 档（涨幅偏大降权，但仍可入选）
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

# 超短末周期（鱼尾段）超买防护：validator 单点判断阈值。
# 20日涨幅阈值直接复用 PULLBACK_20D_GAIN_*（避免常量膨胀）。
# 惩罚已移除：超买时仅靠 validator passed 门禁否决 + enhancer 标记，不再做 score 压制。
# 2026-07-28 收紧：原 BOLL=1.0 / KDJ=105 在强势股主升浪中几乎必中（健康强趋势 J 常 90~115，
# 单日大涨即破上轨），导致"超买"标签沦为废话、短炒票全民告警。改为仅"极端超买"才触发：
#   - BOLL %B > 1.10：明显脱离上轨（而非刚触碰）
#   - KDJ J > 115：超过健康强趋势上限（90~115）
# 20日涨幅>60% 维持（ genuinely 过热），三项仍任一即判（否决用，宁可漏放不可误杀主升浪）。
ST_OVERBOUGHT_BOLL = 1.10         # BOLL %B > 此值 = 明显破上轨（极端高位）
ST_OVERBOUGHT_KDJ = 115           # KDJ J > 此值 = 极端超买（健康强趋势 J 常 90~115，旧 105 误伤）

# ── rebound 交叉验证常量 ──
V_RB_OVERSOLD_STRONG = 8      # RSI<30 + KDJ J<0 / MACD翻红 ≥2 命中
V_RB_OVERSOLD_PARTIAL = 4     # 仅1命中
V_RB_VOL_SURGE = 6            # 量比≥2.0 放量企稳
V_RB_VOL_HEALTHY = 4          # 量比1.0~2.0 正常企稳
V_RB_VOL_LOW = -3             # 量比<1.0 缩量企稳不可信
V_RB_SECTOR_ACTIVE = 4        # 同板块≥3只 板块共振
V_RB_SECTOR_MOD = 2           # 同板块=2只 板块温和共振
V_RB_PATTERN_STRONG = 6       # 暴跌后阳包阴（强反转信号）
V_RB_PATTERN_HAMMER = 4       # 锤子线（低位承接）
V_RB_PATTERN_3BULL = 3        # 3连阳企稳

# Bonus constants
CROSS_SOURCE_BONUS = 5
FIRST_TODAY_BONUS = 3
FIRST_BREAKOUT_BONUS = 8
FIRST_BREAKOUT_RANK_CHANGE = 500
FIRST_BREAKOUT_VOL_RATIO = 1.15

LIVE_VOL_BONUS = 3
LIVE_VOL_RATIO_THRESHOLD = 1.3

TURNOVER_BONUS_MODERATE = 3
TURNOVER_BONUS_HEALTHY = 5
TURNOVER_BONUS_PENALTY = -3
TURNOVER_HIGH = 20
TURNOVER_MEDIUM = 10
TURNOVER_LOW = 5

SECTOR_CLUSTER_BONUS_5 = 8
SECTOR_CLUSTER_BONUS_4 = 6
SECTOR_CLUSTER_BONUS_3 = 4
SECTOR_CLUSTER_BONUS_2 = 2

MARKET_ENV_STRONG = 2
MARKET_ENV_WEAK = -2
MARKET_STRONG_THRESHOLD = 0.5
MARKET_WEAK_THRESHOLD = -1.0

# Sentiment cycle thresholds
SENTIMENT_BOILING = 5
SENTIMENT_WARM = 2
SENTIMENT_COOL = -2
SENTIMENT_FROZEN = -5
SENTIMENT_AVG_TOP10_BOILING = 6.5
SENTIMENT_PCT_GT5_BOILING = 0.30
SENTIMENT_AVG_TOP10_WARM = 4.0
SENTIMENT_PCT_GT5_WARM = 0.15
SENTIMENT_AVG_TOP10_COOL = 1.0
SENTIMENT_PCT_GT5_COOL = 0.05

# Market cap bonus (enhancer)
MCAP_BONUS_SMALL = 3
MCAP_BONUS_MID = 1
MCAP_SMALL_THRESHOLD = 100  # 亿，≤此值 → 小市值加分
MCAP_MID_THRESHOLD = 300    # 亿，≤此值且 > 小市值 → 中等市值加分

# RPS bonus
RPS_BONUS_HIGH = 4
RPS_BONUS_MEDIUM = 2
RPS_BONUS_LOW = -3
RPS_PCTILE_HIGH = 80
RPS_PCTILE_MEDIUM = 60
RPS_PCTILE_LOW = 30

# K-line fetch configuration
KLINE_FETCH_DAYS = 45     # Number of days to fetch from API
KLINE_MIN_LENGTH = 32     # Minimum kline bars required for analysis

# Fatigue detection for multi-day list appearances
FATIGUE_PRICE_WARN_ACCUM = 8    # 5-day accum below this after 3+ days → price fatigue
FATIGUE_VOL_WARN_RATIO = 1.0   # vol_ratio below this → volume fatigue
FATIGUE_STREAK_MIN = 3          # minimum streak before fatigue applies
FATIGUE_PENALTY_PER_DAY = -3   # penalty per streak day when fatigued
FATIGUE_PENALTY_CAP = -15      # max fatigue penalty
FATIGUE_ACCELERATE_PCT = 3     # today pct above this + healthy vol → acceleration bonus
FATIGUE_ACCELERATE_BONUS_PER_DAY = 2  # bonus per streak day when accelerating
FATIGUE_ACCELERATE_BONUS_CAP = 15     # max acceleration bonus（与 FATIGUE_PENALTY_CAP 对称）

# ── 主力出货风险标签阈值 ──
# 满足任一复合条件即标记"主力出货"，用于识别高位派发迹象。
# 2026-07-28 收紧：原 Rule 2（高位高换手+超买）仅要求换手率>5% + 宽松超买，
# 几乎把所有活跃强势股都打成"主力出货"。改为高确信条件，且依赖已收紧的极端超买。
DISTRIBUTION_ACCUM_HIGH = 20.0      # 累计涨幅高位阈值（放量滞涨场景）
DISTRIBUTION_ACCUM_MID = 15.0       # 累计涨幅中高位阈值（高换手超买场景，需配合 genuine 过热换手）
DISTRIBUTION_ACCUM_PULLBACK = 15.0  # 冲高回落场景的累计涨幅下限（原 10 → 15，提升确信度）
DISTRIBUTION_VOL_RATIO = 2.5        # 量比阈值（放量滞涨场景，原 2.0 → 2.5，需更明确放量）
# 滞涨判定用带宽阈值避免闪烁：today_pct 在 1.0% 附近震荡时不应反复触发/消失。
# 0.5% 以下才算明确滞涨（1.0%~0.5% 为过渡区，不触发）。
DISTRIBUTION_TODAY_PCT_LOW = 0.5
DISTRIBUTION_OPENING_STRONG = 4.0   # 开盘强度阈值（冲高回落场景，opening_score 范围 -5~5）
# 分时走弱判定用负带宽避免闪烁：intraday_score 在 0 附近震荡时不应反复触发/消失。
# intraday_score 范围 -10~10，0 只是中性，<-1.0 才算明确分时转弱。
DISTRIBUTION_INTRADAY_WEAK = -1.0
# 主力出货 Rule 2 的换手率门槛：要求"真正过热"而非单纯活跃。
# enhancer 中以 c.turnover_bonus < 0 判定（turnover_rate > TURNOVER_HIGH=20%，即派发级过热）。

# ── 涨幅过大风险标签阈值 ──
# 累计涨幅超过此值时标记"涨幅过大"，提示追高风险
OVERVALUED_ACCUM_THRESHOLD = 25.0

# Trend-label hard filter: exclude trends with avg next-day return < -2%
# Based on 2729 historical recommendations analysis
# Only includes labels actually produced by current analysis.py
HIGH_RISK_TRENDS: set[str] = {
    # "缩量回调" 已移除：avg -2.09% win 39.2% 在候选池场景可接受，
    # 保留 MA 支撑 + 小幅回调的合理 pullback 候选。
    "回踩整理",   # pullback: avg -3.89%, win 21.6%
}

# ── 风险标签硬排除集合 ──
# 命中即直接从所有推荐列表移除（推荐输出只保留可买票）。
# 仅纳入"卖出/止损"级信号：
#   - 主力出货：高位派发，明确的卖出信号
#   - 趋势破位：MA 破位，止损信号
# 其余标签保留为展示型警告（不在此过滤）：
#   - 超买：上下文语义（仅 short_term 条件性否决，其余策略展示）
#   - 涨幅过大 / 疲劳 / 弱市：追高/后劲不足/大盘环境提示
#   - 量价背离：含轻度负面（回踩却不缩量），不足以单独排除
RISK_FLAGS_HARD_FILTER: set[str] = {
    "主力出货",
    "趋势破位",
}

# Time-based bonus thresholds (minutes since midnight)
STALE_TIMEOUT_MINUTES = 30  # 掉榜后保留时长
TRACK_RECOMMENDATION_DAYS = 5  # 历史推荐跟踪窗口（交易日）

# ── 历史推荐跟踪 — 买点信号识别阈值 ──
# 跟踪场景核心：找"推荐后回调到买点"的票，不是简单展示全部历史推荐
# 硬过滤：排除不能买的（大涨追高/已错过/暴跌失效）
TRACK_FILTER_TODAY_HIGH = 5.0    # 今日涨幅≥此值 → 过滤（不追高）
TRACK_FILTER_TODAY_LOW = -5.0    # 今日跌幅≥此值 → 过滤（可能破位）
TRACK_FILTER_CUM_HIGH = 10.0     # 累计收益≥此值 → 过滤（已错过）
TRACK_FILTER_CUM_LOW = -10.0     # 累计收益≤此值 → 过滤（信号失效）
# 买点信号阈值（满足条件计 1 分，信号数决定状态分类）
TRACK_MA20_SUPPORT_PCT = 3.0     # |close-MA20|/MA20 < 此值 且 MA20 上行 → MA20 支撑
TRACK_VOL_SHRINK_RATIO = 0.8     # vol_ratio < 此值 → 缩量回调
TRACK_RSI_LOW = 30               # RSI 合理区下限
TRACK_RSI_HIGH = 50              # RSI 合理区上限（回落但不超卖）
TRACK_BOLL_MID_PCT = 3.0         # 距 BOLL 中轨±此值内 → 位置合理
TRACK_MA20_SLOPE_MIN = 0.5       # MA20 日涨幅>此值 → 上行（百分比）
# 状态分类阈值
TRACK_STATUS_BUY = 4             # 信号数≥此值 → "到买点"
# 观察中门槛由 2 提到 3：原 2 个信号极易由同源指标（MA20支撑/未破位/BOLL中轨
# 三者本质都是"价格在均线附近"）一次凑齐，导致大量横盘票涌入观察列表、噪声过大。
# 提到 3 后"观察中"需更实质的回调结构才出现。
TRACK_STATUS_WATCH = 3           # 信号数≥此值 → "观察中"，否则 "未到买点"（过滤）
TRACK_KLINE_REFRESH_LOOPS = 5    # 每 N 轮给跟踪票拉一次 K 线（节流）
# 历史推荐跟踪显示上限（避免列表过长）：只显示高确信"到买点"，
# "观察中"默认不显示（TRACK_DISPLAY_WATCH_MAX=0）。若想恢复观察中补充尾部，
# 把该值改回 >0（如 5）即可，display 会自动追加并封顶。
TRACK_DISPLAY_BUY_MAX = 10       # "到买点"最多显示条数
TRACK_DISPLAY_WATCH_MAX = 0      # "观察中"补充最多显示条数（0 = 不显示，只看到买点）

# 辨识度标签 — 反复上榜
PROMINENCE_LOOKBACK_DAYS = 5     # 回溯 N 个交易日
PROMINENCE_REPEAT_THRESHOLD = 3  # 出现 ≥ N 天 → "↻"
PROMINENCE_MAX_AVG_RANK = 70    # 近 N 日平均排名 ≤ 此值

EARLY_TRADE_CUTOFF = 10 * 60 + 30   # 10:30
LATE_TRADE_START = 14 * 60           # 14:00
# -5→-2：原 -5 会把刚过 MIN_SCORE 的早盘票压到门槛下，导致 9:30-10:30 票迟迟不推。
# -2 保留早盘噪音轻度抑制，但不再系统性压杀早盘异动。
EARLY_BONUS = -2
LATE_BONUS = 3

HOLIDAYS_FILE = os.path.join(BASE_DIR, "holidays.json")

_HOLIDAYS_FALLBACK: set[str] = {
    "2025-01-01",                              # 元旦
    "2025-01-28", "2025-01-29", "2025-01-30", "2025-01-31",  # 春节
    "2025-02-01", "2025-02-02", "2025-02-03", "2025-02-04",  # 春节
    "2025-04-04", "2025-04-05", "2025-04-06",                # 清明
    "2025-05-01", "2025-05-02", "2025-05-03", "2025-05-04", "2025-05-05",  # 劳动节
    "2025-05-31", "2025-06-01", "2025-06-02",                # 端午
    "2025-10-01", "2025-10-02", "2025-10-03",                # 国庆
    "2025-10-04", "2025-10-05", "2025-10-06", "2025-10-07", "2025-10-08",  # 国庆
    "2026-01-01",                              # 元旦
    "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20",  # 春节
    "2026-02-21", "2026-02-22", "2026-02-23", "2026-02-24",  # 春节
    "2026-04-05", "2026-04-06",                               # 清明
    "2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04", "2026-05-05",  # 劳动节
    "2026-06-19", "2026-06-20", "2026-06-21",                # 端午
    "2026-09-30",                          # 国庆前 (调休)
    "2026-10-01", "2026-10-02", "2026-10-03",                # 国庆
    "2026-10-04", "2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08",  # 国庆
}


def _load_holidays_from_file(path: str) -> set[str] | None:
    try:
        import json
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return set(data)
        return None
    except (FileNotFoundError, json.JSONDecodeError, TypeError):
        return None


HOLIDAYS: set[str] = _load_holidays_from_file(HOLIDAYS_FILE) or _HOLIDAYS_FALLBACK

# == Cross-validation weights ==
# New face
V_NF_CONVERGE_STRONG = 13
V_NF_CONVERGE_PARTIAL = 8
V_NF_HL_CLEAR = 5
V_NF_HL_STABLE = 2
V_NF_HL_FAIL = -5
V_NF_SECTOR_STRONG = 8
V_NF_SECTOR_MOD = 5
V_NF_SECTOR_WEAK = 0

# Momentum
V_MO_MA_FULL = 6
V_MO_MA_PARTIAL = 3
V_MO_MA_NONE = -5
V_MO_DIVERGENCE_NONE = 0
V_MO_DIVERGENCE_BEAR = -10
V_MO_VOL_UP = 8
V_MO_VOL_STABLE = 5
V_MO_VOL_SPIKE = -5

# Pullback
V_PB_MA_UP = 10
V_PB_MA_FLAT = 0
V_PB_MA_DOWN = -10
V_PB_SHRINK_YES = 10
V_PB_SHRINK_MOD = 5
V_PB_SHRINK_NO = -5

# 回踩量能比阈值（分析端与验证端共用，避免口径矛盾）
#   vol_ratio < PULLBACK_VOL_LOW        -> 极度缩量（确认）
#   vol_ratio <= PULLBACK_VOL_HEALTHY   -> 健康缩量
#   vol_ratio > PULLBACK_VOL_HIGH       -> 放量（非缩量，惩罚）
# 中间带 (HEALTHY, HIGH] 视为中性，两端一致。
PULLBACK_VOL_LOW = 0.4
PULLBACK_VOL_HEALTHY = 0.9
PULLBACK_VOL_HIGH = 1.3
V_PB_SECTOR_HOT = 8
V_PB_SECTOR_DEAD = -5
V_PB_SECTOR_NEUTRAL = 0

# New face — added in P0
V_NF_DIVERGENCE_BULL = 8
V_NF_VOLUME_CONFIRM = 5

# Pullback — added in P0
V_PB_BOLLINGER_TOUCH = 5
V_PB_BOLLINGER_MID = 2

# Short term
V_ST_VOL_HEALTHY = 8
V_ST_VOL_SURGE = 12
V_ST_SECTOR_HOT = 10
V_ST_SECTOR_WARM = 5
V_ST_SECTOR_COLD = 0
V_ST_RANK_TOP10 = 8
V_ST_RANK_TOP20 = 5
V_ST_RANK_TOP30 = 2
V_ST_RANK_LOW = -3
V_ST_MA_SUPPORT = 5
V_ST_MA_BROKEN = -5

# Pullback 20-day gain penalty (late-stage lifecycle protection)
# PULLBACK_20D_GAIN_WARN/EXTREME 同时用于 validator 超买判定阈值。
PULLBACK_20D_GAIN_WARN = 40       # 20-day gain > 40% → warn
PULLBACK_20D_GAIN_EXTREME = 60    # 20-day gain > 60% → extreme
PULLBACK_20D_WARN_PENALTY = -10
PULLBACK_20D_EXTREME_PENALTY = -15
