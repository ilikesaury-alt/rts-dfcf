import os
from datetime import time as dtime

REFRESH_INTERVAL = 60
REQUEST_TIMEOUT = 15
NEW_FACE_LOOKBACK_DAYS = 3

# Normal mode thresholds
NEW_FACE_MIN_SCORE = 18
MOMENTUM_MIN_SCORE = 15
PULLBACK_MIN_SCORE = 18

YI = 100_000_000
MAX_MARKET_CAP = 300 * YI
MAX_STOCK_PRICE = 100.0
MAX_NEW_FACE_TODAY_PCT = 8

# Vol-rank combo scoring thresholds
VOL_RANK_VOL_THRESHOLD = 1.15
VOL_RANK_STRONG_RC = 2000
VOL_RANK_MEDIUM_RC = 1000
VOL_RANK_WEAK_RC = 500
VOL_RANK_STRONG_PTS = 15
VOL_RANK_MEDIUM_PTS = 12
VOL_RANK_WEAK_PTS = 8

# List momentum scoring (consecutive surge list appearances + rank trajectory)
LIST_STREAK_BONUS_2 = 3
LIST_STREAK_BONUS_3 = 5
LIST_STREAK_BONUS_5 = 8
TOP40_THRESHOLD = 40
TOP40_BONUS = 5
TOP40_ADVANCE_PER_10 = 2
TOP20_EXTRA = 3

FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "")
FEISHU_KEYWORD = "lichun"

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
    "today_pct_lt_0_5": 2,
    "today_pct_6_8": 5,
    "accum_neg5_10": 10,
    "accum_lt_neg5": -5,
    "accum_10_15": 5,
    "accum_15_25": -5,
    "accum_gt_25": -15,
    "bottom_confirmed": 10,
    "v_shape": 10,
    "volume_surge": 15,
    "vol_rank_combo": 12,
    "gap_up_gt_2": 8,
    "gap_up_1_2": 5,
    "gap_up_0_5_1": 3,
    "value_gte_10000": 2,
    "value_gte_5000": 1,
    "ma_bull": 5,
    "ma_bear": -3,
    "rsi_bonus": 3,
    "kdj_bonus": 3,
    "macd_bonus": 3,
}

MOMENTUM_WEIGHTS: dict[str, int] = {
    "today_pct_2_6": 26,
    "today_pct_1_2": 10,
    "today_pct_0_5_1": 5,
    "today_pct_lt_0_5": 2,
    "today_pct_6_8": 5,
    "accum_10_15": 19,
    "accum_15_20": 10,
    "accum_20_30": 5,
    "accum_gte_30": -15,
    "vol_healthy": 5,
    "vol_surge": -4,
    "vol_low": -5,
    "no_crash": 13,
    "vol_rank_combo": 8,
    "gap_up_gt_2": 8,
    "gap_up_1_2": 5,
    "gap_up_0_5_1": 3,
    "value_gte_10000": 2,
    "value_gte_5000": 1,
    "ma_bull": 5,
    "ma_bear": -3,
    "rsi_bonus": 3,
    "kdj_bonus": 3,
    "macd_bonus": 3,
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
}

# Bonus constants
FIRST_TODAY_BONUS = 5
FIRST_BREAKOUT_BONUS = 8
FIRST_BREAKOUT_RANK_CHANGE = 500
FIRST_BREAKOUT_VOL_RATIO = 1.15

LIVE_VOL_BONUS = 5
LIVE_VOL_RATIO_THRESHOLD = 1.3

TURNOVER_BONUS_MODERATE = 3
TURNOVER_BONUS_HEALTHY = 5
TURNOVER_BONUS_PENALTY = -3
TURNOVER_HIGH = 8
TURNOVER_MEDIUM = 4
TURNOVER_LOW = 2

SECTOR_CLUSTER_BONUS_5 = 14
SECTOR_CLUSTER_BONUS_4 = 10
SECTOR_CLUSTER_BONUS_3 = 6
SECTOR_CLUSTER_BONUS_2 = 3

MARKET_ENV_STRONG = 3
MARKET_ENV_WEAK = -3
MARKET_STRONG_THRESHOLD = 0.5
MARKET_WEAK_THRESHOLD = -1.0

# Sentiment cycle thresholds
SENTIMENT_BOILING = 5
SENTIMENT_WARM = 2
SENTIMENT_COOL = -2
SENTIMENT_FROZEN = -5
SENTIMENT_AVG_TOP10_BOILING = 8.0
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

# Time-based bonus thresholds (minutes since midnight)
STALE_TIMEOUT_MINUTES = 30  # 掉榜后保留时长

EARLY_TRADE_CUTOFF = 10 * 60 + 30   # 10:30
LATE_TRADE_START = 14 * 60           # 14:00
EARLY_BONUS = -5
LATE_BONUS = 3

HOLIDAYS_FILE = os.path.join(BASE_DIR, "holidays.json")

_HOLIDAYS_FALLBACK: set[str] = {
    "2025-01-01",
    "2025-01-28", "2025-01-29", "2025-01-30", "2025-01-31",
    "2025-02-01", "2025-02-02", "2025-02-03", "2025-02-04",
    "2025-04-04", "2025-04-05", "2025-04-06",
    "2025-05-01", "2025-05-02", "2025-05-03", "2025-05-04", "2025-05-05",
    "2025-05-31", "2025-06-01", "2025-06-02",
    "2025-10-01", "2025-10-02", "2025-10-03",
    "2025-10-04", "2025-10-05", "2025-10-06", "2025-10-07", "2025-10-08",
    "2026-01-01",
    "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20",
    "2026-02-21", "2026-02-22", "2026-02-23", "2026-02-24",
    "2026-04-05", "2026-04-06",
    "2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04", "2026-05-05",
    "2026-06-19", "2026-06-20", "2026-06-21",
    "2026-09-30",
    "2026-10-01", "2026-10-02", "2026-10-03",
    "2026-10-04", "2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08",
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
V_NF_HL_CLEAR = 8
V_NF_HL_STABLE = 3
V_NF_HL_FAIL = -5
V_NF_SECTOR_STRONG = 8
V_NF_SECTOR_MOD = 5
V_NF_SECTOR_WEAK = 3

# Momentum
V_MO_MA_FULL = 10
V_MO_MA_PARTIAL = 5
V_MO_MA_NONE = -8
V_MO_DIVERGENCE_NONE = 8
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
V_PB_SECTOR_COLD = 3
V_PB_SECTOR_DEAD = -5

# ============================================================
# Industry chain trend & chokepoint scoring (industry_chain/)
# ============================================================
# Chain trend phase bonuses (applied by chokepoint_scorer.py)
CHAIN_ERUPTING_BONUS = 15
CHAIN_GROWING_BONUS = 8
CHAIN_FORMING_BONUS = 3
CHAIN_FADING_PENALTY = -5

# Chokepoint / bottleneck bonus
BOTTLENECK_BONUS = 10

# Trend judgment thresholds (used by trend_judge.py)
CHAIN_CONCENTRATION_HOT = 5
CHAIN_CONCENTRATION_WARM = 3
CHAIN_CONCENTRATION_LOW = 2
CHAIN_PERSISTENCE_MIN = 3
CHAIN_PERSISTENCE_FADING = 5

# Technical scoring weights (chokepoint_scorer.py)
TECH_MA_BULL = 8
TECH_MA_BEAR = -5
TECH_VOL_CONFIRM = 5
TECH_RSI_STRONG = 5
TECH_RSI_OVERSOLD = 3
TECH_MACD_GOLDEN = 5
TECH_ACCUM_HEALTHY = 5
TECH_ACCUM_OVERHEAT = -8
TECH_NEW_FACE_BONUS = 8
