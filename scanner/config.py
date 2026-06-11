import os
from datetime import time as dtime

REFRESH_INTERVAL = 120
REQUEST_TIMEOUT = 15
TOP_N = 40
NEW_FACE_LOOKBACK_DAYS = 3

# Normal mode thresholds
NEW_FACE_MIN_SCORE = 20
MOMENTUM_MIN_SCORE = 15
MIN_INTRADAY_SCORE = 1
MA_BULL_BONUS = 6

YI = 100_000_000
MAX_MARKET_CAP = 300 * YI
MAX_STOCK_PRICE = 100.0

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

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "scanner.db")
LOG_DIR = os.path.join(BASE_DIR, "logs")

MORNING_START = dtime(9, 30)
MORNING_END = dtime(11, 45)
AFTERNOON_START = dtime(13, 0)
AFTERNOON_END = dtime(15, 0)

HOLIDAYS: set[str] = {
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
