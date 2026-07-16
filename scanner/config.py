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
PULLBACK_MIN_SCORE = 18
SHORT_TERM_MIN_SCORE = 15

YI = 100_000_000
MAX_MARKET_CAP = 500 * YI
MAX_STOCK_PRICE = 200.0
MAX_NEW_FACE_TODAY_PCT = 12
MAX_MOMENTUM_TODAY_PCT = 8
PULLBACK_MAX_TODAY_PCT = 2.0
SHORT_TERM_MIN_TODAY_PCT = 2.0
SHORT_TERM_MAX_TODAY_PCT = 8.0

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

# Vol-rank + accumulated combo penalty
VOL_RANK_HIGH_ACCUM_OVERLAP_MIN_RANK = 12
VOL_RANK_HIGH_ACCUM_OVERLAP_MIN_ACCUM = 20
VOL_RANK_HIGH_ACCUM_OVERLAP_PENALTY = -10

# MA bull extra bonus (pullback)
MA_BULL_EXTRA_BONUS = 5

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

THS_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://www.10jqka.com.cn/",
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
}
# NOTE: new_face_kdj 已降权（3->1）。回测 IC=-0.184 但 n=30（小样本），
# 仅弱化不消除，待累积样本后再评估是否清零。

MOMENTUM_WEIGHTS: dict[str, int] = {
    "today_pct_2_6": 20,
    "today_pct_1_2": 10,
    "today_pct_0_5_1": 5,
    "today_pct_lt_0_5": 5,
    "today_pct_6_8": 5,
    "accum_10_15": 15,
    "accum_15_20": 10,
    "accum_20_30": 5,
    "accum_gte_30": -15,
    "vol_healthy": 2,
    "vol_surge": 0,
    "vol_low": -3,
    "no_crash": 13,
    "value_gte_10000": 3,
    "value_gte_5000": 1,
    "rsi_bonus": 3,
    "kdj_bonus": 3,
    "macd_bonus": 3,
    "adx_bonus": 5,
    "adx_weak": -3,
}

PULLBACK_WEIGHTS: dict[str, int] = {
    "today_pos0_2": 5,
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
    "no_crash": 13,
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
    "accum_5_10": 10,
    "accum_10_15": 15,
    "accum_15_20": 8,
    "accum_gte_20": -5,
    "accum_lt_0": -5,
    "vol_healthy": 8,
    "vol_surge": 12,
    "vol_low": -5,
    "no_crash": 10,
    "value_small_cap": 6,
    "value_mid_cap": 2,
    "st_weak_to_strong": 8,
    "st_wts_gap": 4,
    "rsi_bonus": 3,
    "kdj_bonus": 3,
    "macd_bonus": 3,
    "rank_top10": 8,
    "rank_top20": 5,
    "rank_top30": 3,
}

# 超短末周期（鱼尾段）超买防护：与分析侧软惩罚阈值保持一致。
# 20日涨幅阈值/惩罚直接复用 PULLBACK_20D_GAIN_*（避免常量膨胀）。
ST_OVERBOUGHT_BOLL = 1.0          # BOLL %B > 此值 = 破上轨（高位）
ST_OVERBOUGHT_BOLL_PENALTY = -5
ST_OVERBOUGHT_KDJ = 105           # KDJ J > 此值 = 极端超买（健康强趋势 J 常 90~115，100 易误伤）
ST_OVERBOUGHT_KDJ_PENALTY = -5
MO_OVERBOUGHT_VALIDATION_PENALTY = -5  # 动量超买时验证分轻度压制（不硬否决）

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

# RPS bonus
RPS_BONUS_HIGH = 4
RPS_BONUS_MEDIUM = 2
RPS_BONUS_LOW = -3
RPS_PCTILE_HIGH = 80
RPS_PCTILE_MEDIUM = 60
RPS_PCTILE_LOW = 30

# K-line fetch configuration
KLINE_FETCH_DAYS = 45     # Number of days to fetch from API
KLINE_MIN_LENGTH = 34     # Minimum kline bars required for analysis

# Fatigue detection for multi-day list appearances
FATIGUE_PRICE_WARN_ACCUM = 8    # 5-day accum below this after 3+ days → price fatigue
FATIGUE_VOL_WARN_RATIO = 1.0   # vol_ratio below this → volume fatigue
FATIGUE_STREAK_MIN = 3          # minimum streak before fatigue applies
FATIGUE_PENALTY_PER_DAY = -3   # penalty per streak day when fatigued
FATIGUE_PENALTY_CAP = -15      # max fatigue penalty
FATIGUE_ACCELERATE_PCT = 3     # today pct above this + healthy vol → acceleration bonus
FATIGUE_ACCELERATE_BONUS_PER_DAY = 2  # bonus per streak day when accelerating

# Time-based bonus thresholds (minutes since midnight)
STALE_TIMEOUT_MINUTES = 30  # 掉榜后保留时长

EARLY_TRADE_CUTOFF = 10 * 60 + 30   # 10:30
LATE_TRADE_START = 14 * 60           # 14:00
EARLY_BONUS = -5
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
PULLBACK_20D_GAIN_WARN = 40       # 20-day gain > 40% → warn
PULLBACK_20D_GAIN_EXTREME = 60    # 20-day gain > 60% → extreme
PULLBACK_20D_WARN_PENALTY = -10
PULLBACK_20D_EXTREME_PENALTY = -15
TECH_POSITIVE_RETURN_BONUS = 4
