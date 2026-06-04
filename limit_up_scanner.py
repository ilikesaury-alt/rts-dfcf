"""
雪球飙升榜 → 创业板智能扫描
策略: 只盯300xxx，区分"新面孔(底部异动)"和"旧面孔(盘整二波)"

用法:
    python limit_up_scanner.py              # 每5分钟
    python limit_up_scanner.py 120          # 每2分钟
"""

import sys
import time
import json
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import requests
import wcwidth
from datetime import datetime, date, timedelta, time as dtime
from dataclasses import dataclass, field
from typing import Any

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

# ── Config ──
REFRESH_INTERVAL = 180
REQUEST_TIMEOUT = 15
TOP_N = 40                     # 只看前40名
NEW_FACE_LOOKBACK_DAYS = 3         # 几天内没出现过算新面孔
OLD_FACE_STRONG_PREV_LOOKBACK = 5  # 旧面孔前置涨幅回查窗口(天)

MOMENTUM_MIN_SCORE = 15    # 动量延续最低门槛

# ── 小而美策略 ──
# 过滤大市值高股价，聚焦小盘低价股
YI = 100_000_000                                 # 1亿
MAX_MARKET_CAP = 500 * YI                        # 最大总市值（超过则过滤）
MAX_STOCK_PRICE = 100.0                           # 最高股价（超过则过滤）


# 飞书机器人推送（环境变量或直接填入）
FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK",
    "https://open.feishu.cn/open-apis/bot/v2/hook/d0caf1dd-54b6-4b86-b83d-861e4c79afda")
FEISHU_KEYWORD = "lichun"      # 自定义关键词校验

# ── 请求限流 ──
_last_api_call: float = 0


def _throttle(min_interval: float = 0.15):
    """确保两次API调用之间至少间隔 min_interval 秒"""
    global _last_api_call
    elapsed = time.time() - _last_api_call
    if elapsed < min_interval:
        time.sleep(min_interval - elapsed)
    _last_api_call = time.time()


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

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "scanner.db")
LOG_DIR = os.path.join(BASE_DIR, "logs")

# ── Trading Session Config ──

# A股交易时段
MORNING_START = dtime(9, 30)
MORNING_END = dtime(11, 45)
AFTERNOON_START = dtime(13, 0)
AFTERNOON_END = dtime(15, 0)

# A股法定节假日（仅含今年已知的，每年需要更新）
HOLIDAYS: set[str] = {
    # 2025年
    "2025-01-01",                     # 元旦
    "2025-01-28", "2025-01-29", "2025-01-30", "2025-01-31",  # 春节
    "2025-02-01", "2025-02-02", "2025-02-03", "2025-02-04",
    "2025-04-04", "2025-04-05", "2025-04-06",                # 清明
    "2025-05-01", "2025-05-02", "2025-05-03", "2025-05-04", "2025-05-05",  # 劳动节
    "2025-05-31", "2025-06-01", "2025-06-02",                # 端午
    "2025-10-01", "2025-10-02", "2025-10-03",                # 国庆/中秋
    "2025-10-04", "2025-10-05", "2025-10-06", "2025-10-07", "2025-10-08",
    # 2026年
    "2026-01-01",                     # 元旦
    "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20",  # 春节
    "2026-02-21", "2026-02-22", "2026-02-23", "2026-02-24",
    "2026-04-05", "2026-04-06",                               # 清明（4/4周六）
    "2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04", "2026-05-05",  # 劳动节
    "2026-06-19", "2026-06-20", "2026-06-21",                # 端午
    "2026-09-30",                                             # 中秋
    "2026-10-01", "2026-10-02", "2026-10-03",                # 国庆
    "2026-10-04", "2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08",
}


def is_trading_day(d: date) -> bool:
    """是否交易日（非周末、非节假日）"""
    if d.weekday() >= 5:  # 周六日
        return False
    return d.isoformat() not in HOLIDAYS


def is_trading_time(now: datetime | None = None) -> bool:
    """当前是否在交易时段内（9:30-11:30 或 13:00-15:00）"""
    now = now or datetime.now()
    if not is_trading_day(now.date()):
        return False
    t = now.time()
    return (MORNING_START <= t <= MORNING_END) or (AFTERNOON_START <= t <= AFTERNOON_END)


def seconds_until_next_session(now: datetime | None = None) -> int:
    """
    距离下一个交易时段开始的秒数（用于非交易时段休眠）。
    若当前在交易时段内返回 0。
    """
    now = now or datetime.now()
    today = now.date()
    t = now.time()

    # 在同一交易日内：判断是上午盘前、午间休市、还是收盘后
    if is_trading_day(today):
        if t < MORNING_START:
            # 上午盘前 → 等到 9:30
            return int((datetime.combine(today, MORNING_START) - now).total_seconds())
        if MORNING_END < t < AFTERNOON_START:
            # 午间休市 → 等到 13:00
            return int((datetime.combine(today, AFTERNOON_START) - now).total_seconds())
        if t > AFTERNOON_END:
            # 今日收盘 → 等到下一个交易日 9:30
            return _seconds_until_next_trading_day(now)
        # 交易时段内
        return 0

    # 非交易日 → 下一个交易日 9:30
    return _seconds_until_next_trading_day(now)


def _seconds_until_next_trading_day(now: datetime) -> int:
    """从 now 到下一个交易日 9:30 的秒数"""
    cursor = now.date() + timedelta(days=1)
    while not is_trading_day(cursor):
        cursor += timedelta(days=1)
    return int((datetime.combine(cursor, MORNING_START) - now).total_seconds())


def next_session_label(now: datetime | None = None) -> str:
    """返回下一个交易时段的描述文字"""
    now = now or datetime.now()
    t = now.time()
    today = now.date()

    if not is_trading_day(today):
        return _next_trading_day_label(now)

    if t < MORNING_START:
        return "今日开盘 09:30"
    if MORNING_END < t < AFTERNOON_START:
        return "下午开盘 13:00"
    if t > AFTERNOON_END:
        return _next_trading_day_label(now)
    return ""


def _next_trading_day_label(now: datetime) -> str:
    cursor = now.date() + timedelta(days=1)
    while not is_trading_day(cursor):
        cursor += timedelta(days=1)
    return f"下次交易 {cursor.isoformat()} 09:30"


# ── Database ──

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS appearances (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            name TEXT NOT NULL,
            date TEXT NOT NULL,
            rank INTEGER,
            percent REAL,
            value REAL,
            UNIQUE(symbol, date)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_kline (
            symbol TEXT NOT NULL,
            timestamp INTEGER,
            date TEXT NOT NULL,
            open REAL,
            close REAL,
            high REAL,
            low REAL,
            volume REAL,
            percent REAL,
            PRIMARY KEY(symbol, date)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS recommendations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            time TEXT NOT NULL,
            symbol TEXT NOT NULL,
            name TEXT NOT NULL,
            category TEXT NOT NULL,
            score INTEGER NOT NULL,
            percent REAL,
            trend TEXT,
            next_day_pct REAL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sector_cache (
            symbol TEXT PRIMARY KEY,
            sector TEXT NOT NULL,
            updated TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_app_date ON appearances(date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_app_sym ON appearances(symbol)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rec_date ON recommendations(date)")
    conn.commit()
    return conn


def get_recent_symbols(conn: sqlite3.Connection, days: int) -> set[str]:
    """获取最近N天出现在前40的所有symbol（不包括今天）"""
    today = date.today().isoformat()
    lookback = (date.today() - timedelta(days=days)).isoformat()
    cur = conn.execute(
        "SELECT DISTINCT symbol FROM appearances WHERE date >= ? AND date < ?",
        (lookback, today),
    )
    return {row[0] for row in cur.fetchall()}


def record_appearances(conn: sqlite3.Connection, symbols: list[dict]):
    """记录本次前N名到数据库"""
    today = date.today().isoformat()
    for i, item in enumerate(symbols, 1):
        try:
            conn.execute(
                "INSERT OR REPLACE INTO appearances (symbol, name, date, rank, percent, value) VALUES (?, ?, ?, ?, ?, ?)",
                (item["symbol"], item["name"], today, i, item.get("percent", 0), item.get("value", 0)),
            )
        except Exception:
            continue
    conn.commit()


def get_symbol_appearances(conn: sqlite3.Connection, symbol: str, days: int) -> list[dict]:
    """获取某symbol最近N天的出现记录"""
    lookback = (date.today() - timedelta(days=days)).isoformat()
    cur = conn.execute(
        "SELECT date, rank, percent, value FROM appearances WHERE symbol = ? AND date >= ? ORDER BY date",
        (symbol, lookback),
    )
    return [{"date": r[0], "rank": r[1], "percent": r[2], "value": r[3]} for r in cur.fetchall()]


# ── K-line data (Xueqiu API) ──

def make_session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False  # 避开系统代理
    s.headers.update(HEADERS)
    s.get("https://xueqiu.com/hq", timeout=REQUEST_TIMEOUT)
    return s


def fetch_kline(session: requests.Session, symbol: str, days: int = 25) -> list[dict] | None:
    """从雪球获取日K线数据"""
    _throttle()
    now_ms = int(time.time() * 1000)
    begin_ms = now_ms - days * 86400 * 1000
    url = (
        f"https://stock.xueqiu.com/v5/stock/chart/kline.json"
        f"?symbol={symbol}&begin={begin_ms}&period=day&count={days}&_={now_ms}"
    )
    resp = session.get(url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.json().get("data")
    if not data:
        return None
    raw_items = data.get("item", [])
    if not raw_items:
        return None
    # column: timestamp, volume, open, high, low, close, chg, percent, turnoverrate, amount
    result = []
    for item in raw_items:
        ts = item[0]
        result.append({
            "timestamp": ts,
            "date": datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d"),
            "open": item[2],
            "high": item[3],
            "low": item[4],
            "close": item[5],
            "volume": item[1],
            "percent": item[7],
        })
    return result


def save_kline_to_db(conn: sqlite3.Connection, symbol: str, kline: list[dict]):
    for k in kline:
        try:
            conn.execute(
                "INSERT OR REPLACE INTO daily_kline (symbol, timestamp, date, open, close, high, low, volume, percent) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (symbol, k["timestamp"], k["date"], k["open"], k["close"], k["high"], k["low"], k["volume"], k["percent"]),
            )
        except Exception:
            continue
    conn.commit()


def get_cached_kline(conn: sqlite3.Connection, symbol: str) -> list[dict] | None:
    today = date.today().isoformat()
    lookback = (date.today() - timedelta(days=25)).isoformat()
    cur = conn.execute(
        "SELECT date, open, close, high, low, volume, percent FROM daily_kline WHERE symbol = ? AND date >= ? ORDER BY date",
        (symbol, lookback),
    )
    rows = cur.fetchall()
    if rows:
        return [
            {"date": r[0], "open": r[1], "close": r[2], "high": r[3], "low": r[4], "volume": r[5], "percent": r[6]}
            for r in rows
        ]
    return None


def ensure_kline(conn: sqlite3.Connection, session: requests.Session, symbol: str) -> list[dict] | None:
    """获取K线（先查缓存，没有再拉取）"""
    cached = get_cached_kline(conn, symbol)
    if cached:
        max_date_str = max(k["date"] for k in cached)
        max_date = date.fromisoformat(max_date_str)
        today = date.today()
        cursor = max_date + timedelta(days=1)
        trading_days_missing = 0
        while cursor < today:
            if is_trading_day(cursor):
                trading_days_missing += 1
            cursor += timedelta(days=1)
        if trading_days_missing <= 2:
            return cached
        try:
            kline = fetch_kline(session, symbol)
            if kline:
                save_kline_to_db(conn, symbol, kline)
                return kline
        except Exception:
            pass
        return cached
    try:
        kline = fetch_kline(session, symbol)
        if kline:
            save_kline_to_db(conn, symbol, kline)
            return kline
    except Exception:
        pass
    return None


# ── Stock helpers ──

def is_st(name: str) -> bool:
    """是否ST或退市股"""
    return name.startswith("*ST") or name.startswith("ST") or "退" in name


def _strip_exchange(code: str) -> str:
    return code[2:] if code[:2] in ("SH", "SZ", "BJ") and len(code) > 2 else code


def is_gem(code: str) -> bool:
    return _strip_exchange(code).startswith("30")


def is_hk_stock(symbol: str) -> bool:
    return symbol.isdigit()


def detect_board(symbol: str, code: str) -> str:
    if is_hk_stock(symbol):
        return "港股"
    if is_gem(code):
        return "创业板"
    raw = _strip_exchange(code)
    if raw.startswith("688"):
        return "科创板"
    return "主板"


# ── Data models ──

@dataclass
class StockInfo:
    symbol: str
    name: str
    code: str
    percent: float
    current: float
    value: float
    rank_change: int
    rank: int


@dataclass
class KlineSummary:
    trend: str
    accumulated_pct: float
    volume_ratio: float
    bottom_confirmed: bool
    score: int


@dataclass
class Candidate:
    stock: StockInfo
    category: str
    score: int
    reason: str
    kline: KlineSummary | None
    sector: str = ""
    rank_trend_bonus: int = 0
    sector_bonus: int = 0
    intraday_score: float = 0.0
    first_seen: str = ""
    last_seen: str = ""
    history_pct: list[float] = field(default_factory=list)
    market_cap: float = 0.0       # 总市值（元）
    circ_market_cap: float = 0.0  # 流通市值（元）



# ── Analysis ──

def analyze_new_face(stock: StockInfo, kline: list[dict] | None) -> KlineSummary | None:
    """新面孔K线分析：底部刚启动？"""
    if not kline or len(kline) < 5:
        return None

    # ⚡ 今日涨跌幅用飙升榜实时数据，K线的今日数据不准
    today_pct = stock.percent

    # 核心过滤：今日下跌的不算底部异动（恐慌上榜）
    if today_pct <= 0:
        return None

    pcts = [k["percent"] for k in kline]

    # 日线质量：近5日多数下跌+累计偏弱+今日涨幅偏弱 → 下降通道反弹，不推荐
    recent_5_pcts = pcts[-5:] if len(pcts) >= 5 else pcts
    down_days = sum(1 for p in recent_5_pcts if p < 0)
    if down_days >= 3 and sum(recent_5_pcts) < 5 and today_pct < 5:
        return None

    # 近5日累计涨幅（不含今日更能看清启动前状态）
    recent_5 = pcts[-6:-1] if len(pcts) >= 6 else pcts[:-1]
    recent_5 = recent_5 if recent_5 else [0]
    accumulated = sum(recent_5)

    # 成交量分析
    volumes = [k["volume"] for k in kline]
    vol_window = volumes[-11:-1] if len(volumes) >= 11 else volumes[:-1]
    avg_vol = sum(vol_window) / max(len(vol_window), 1)
    today_vol = volumes[-1] if volumes else 0
    vol_ratio = today_vol / avg_vol if avg_vol > 0 else 1.0

    # 底部判断：近3日无大跌 + 放量 + 接近20日低点
    closes = [k["close"] for k in kline]
    recent_3_pcts = pcts[-3:] if len(pcts) >= 3 else pcts
    no_heavy_loss = all(p > -3 for p in recent_3_pcts)
    volume_surge = vol_ratio > 1.3
    near_20d_low = (closes[-1] - min(closes[-20:])) / max(min(closes[-20:]), 0.01) < 0.05 if len(closes) >= 20 else True
    bottom_confirmed = no_heavy_loss and volume_surge and near_20d_low

    if bottom_confirmed:
        trend = "⚡底部启动"
    elif no_heavy_loss:
        trend = "企稳回升"
    elif accumulated < -8:
        trend = "仍在探底"
    else:
        trend = "震荡整理"

    score = 0
    # --- 涨幅评分 ---
    if 2 <= today_pct <= 6:
        score += 20  # 黄金区间
    elif today_pct < 2:
        score += 5   # 刚启动
    elif today_pct > 8:
        score -= 15  # 今日涨幅已大
    elif today_pct > 6:
        score += 5   # 偏高但可接受

    # --- 累计涨幅评分 ---
    if accumulated < 15 and accumulated > -5:
        score += 15
    elif accumulated >= 15:
        score -= 10
    if accumulated >= 25:
        score -= 10  # 累计涨幅过高，追高风险大

    # --- K线形态 ---
    if bottom_confirmed:
        score += 15
    if volume_surge:
        score += 10

    # --- 飙升榜信号（创业板阈值降低） ---
    if stock.rank_change >= 2000:
        score += 12
    elif stock.rank_change >= 1000:
        score += 6
    if stock.value >= 10000:
        score += 5
    elif stock.value >= 5000:
        score += 2

    # --- 加分组合 ---
    if today_pct <= 5 and accumulated < 8:
        score += 8

    return KlineSummary(trend=trend, accumulated_pct=round(accumulated, 2),
                        volume_ratio=round(vol_ratio, 2), bottom_confirmed=bottom_confirmed, score=score)


def analyze_old_face(stock: StockInfo, kline: list[dict] | None) -> KlineSummary | None:
    """旧面孔K线分析：盘整/回调可低吸？"""
    if not kline or len(kline) < 5:
        return None

    # ⚡ 今日涨跌幅用飙升榜实时数据
    today_pct = stock.percent

    # 涨停/大涨的票不推荐低吸，大跌也不推荐（可能有利空）
    if today_pct > 8 or today_pct < -8:
        return None

    pcts = [k["percent"] for k in kline]
    closes = [k["close"] for k in kline]

    # 日线质量：近5日多数下跌+累计偏弱+今日涨幅偏弱 → 下降通道反弹，不推荐
    recent_5 = pcts[-5:] if len(pcts) >= 5 else pcts
    if sum(1 for p in recent_5 if p < 0) >= 3 and sum(recent_5) < 5 and today_pct < 5:
        return None

    # 近5日累计涨幅
    accumulated = sum(recent_5)

    is_pullback = today_pct < 2

    # 是否破位（10日支撑位）
    recent_10_closes = closes[-10:] if len(closes) >= 10 else closes
    not_broken = recent_10_closes[-1] >= min(recent_10_closes)

    # 成交量
    volumes = [k["volume"] for k in kline]
    vol_window = volumes[-11:-1] if len(volumes) >= 11 else volumes[:-1]
    avg_vol = sum(vol_window) / max(len(vol_window), 1)
    today_vol = volumes[-1] if volumes else 0
    vol_ratio = today_vol / avg_vol if avg_vol > 0 else 1.0
    shrinking_volume = vol_ratio < 1.1

    # ── 趋势判断（使用实时 today_pct） ──
    if today_pct < -3:
        trend = "大幅回调⚠️"
    elif today_pct < 0:
        trend = "缩量回调" if shrinking_volume else "放量回调⚠️"
    elif today_pct < 2:
        trend = "横盘整理"
    else:
        trend = "再次拉升"

    score = 0
    if is_pullback:
        score += 20
    if not_broken:
        score += 15
    if shrinking_volume:
        score += 12
    if stock.value >= 10000:
        score += 10  # 高热度
    elif stock.value >= 5000:
        score += 5
    if today_pct < 0 and today_pct >= -3:
        score += 8   # 小幅回调，买点
    elif today_pct < -3:
        score -= 10  # 跌幅过大，可能破位
    # 飙升榜信号
    if stock.rank_change >= 2000:
        score += 8
    elif stock.rank_change >= 1000:
        score += 4
    # 极度缩量 → 可能是流动性枯竭而非洗盘
    if vol_ratio < 0.4:
        score -= 8

    return KlineSummary(trend=trend, accumulated_pct=round(accumulated, 2),
                        volume_ratio=round(vol_ratio, 2), bottom_confirmed=not_broken and is_pullback, score=score)


def analyze_momentum(stock: StockInfo, kline: list[dict] | None) -> KlineSummary | None:
    """动量延续：已启动的票今日温和上攻，仍有空间"""
    if not kline or len(kline) < 5:
        return None

    today_pct = stock.percent
    if today_pct <= 0:
        return None

    pcts = [k["percent"] for k in kline]
    recent_5 = pcts[-6:-1] if len(pcts) >= 6 else pcts[:-1]
    recent_5 = recent_5 if recent_5 else [0]
    accumulated = sum(recent_5)

    # 核心：必须有足够的累计涨幅才叫动量
    if accumulated < 10:
        return None

    volumes = [k["volume"] for k in kline]
    vol_window = volumes[-11:-1] if len(volumes) >= 11 else volumes[:-1]
    avg_vol = sum(vol_window) / max(len(vol_window), 1)
    today_vol = volumes[-1] if volumes else 0
    vol_ratio = today_vol / avg_vol if avg_vol > 0 else 1.0

    score = 0

    # --- 涨幅评分 ---
    if 2 <= today_pct <= 8:
        score += 20
    elif today_pct < 2:
        score += 5   # 涨幅偏弱，但不算差
    elif today_pct > 8:
        return None  # 大涨日不追动量

    # --- 累计涨幅：只惩罚极端值 ---
    if accumulated >= 30:
        score -= 15
        trend = "累计过高⚠️"
    elif accumulated >= 20:
        score += 5
        trend = "动量延续"
    elif accumulated >= 15:
        score += 10
        trend = "动量启动"
    else:
        score += 15
        trend = "加速启动"

    # --- 成交量：温和放量最好 ---
    if 0.7 < vol_ratio < 2.0:
        score += 10
    elif vol_ratio >= 2.0:
        score -= 8   # 爆量可能出货
    elif vol_ratio < 0.7:
        score -= 5   # 缩量动能不足

    # --- 近2日未修复的大跌检查 ---
    if len(pcts) >= 2:
        recent_2_return = pcts[-2] + pcts[-1]
        no_crash = recent_2_return > -3
    else:
        no_crash = True
    if no_crash:
        score += 15

    # --- 飙升榜信号 ---
    if stock.rank_change >= 2000:
        score += 12
    elif stock.rank_change >= 1000:
        score += 6
    if stock.value >= 10000:
        score += 5
    elif stock.value >= 5000:
        score += 2

    return KlineSummary(trend=trend, accumulated_pct=round(accumulated, 2),
                        volume_ratio=round(vol_ratio, 2), bottom_confirmed=no_crash, score=score)


# ── Sector classifier ──

# 行业关键词映射（从股票名称识别行业）
SECTOR_KEYWORDS: dict[str, list[str]] = {
    "半导体": ["半导体", "芯片", "集成电路", "封测"],
    "新能源": ["新能源", "新能", "光伏", "风电", "锂电", "电池", "氢能", "储能", "阳光"],
    "医药": ["医药", "医疗", "生物", "制药", "药", "医"],
    "电子": ["电子", "元器件"],
    "计算机": ["计算机", "软件", "信息", "数字", "数据", "智能", "AI"],
    "通信": ["通信", "通讯", "5G"],
    "消费": ["消费", "食品", "饮料", "家电", "白酒", "乳业"],
    "军工": ["军工", "航天", "航空", "国防"],
    "汽车": ["汽车", "新能源车", "特斯拉"],
    "机械": ["机械", "装备", "精密"],
    "有色": ["有色", "金属", "钢铁", "黄金", "铜", "铝", "铜箔"],
    "化工": ["化工", "化学", "石化"],
    "地产": ["地产", "房地产", "置业"],
    "金融": ["银行", "证券", "保险", "金融"],
    "传媒": ["传媒", "影视", "游戏", "动漫"],
    "电力": ["电力", "电网", "电气"],
    "交运": ["运输", "物流", "航空", "港口", "航运"],
    "环保": ["环保", "水务", "节能"],
    "农业": ["农业", "种业", "养殖", "牧原"],
}


def classify_sector(name: str) -> str:
    """根据股票名称判断所属行业"""
    for sector, keywords in SECTOR_KEYWORDS.items():
        for kw in keywords:
            if kw in name:
                return sector
    return "其他"


def get_sector_clusters(stocks: list) -> dict[str, list[str]]:
    """统计前N名中各行业分布"""
    clusters: dict[str, list[str]] = {}
    for s in stocks:
        sec = classify_sector(s.name)
        if sec not in clusters:
            clusters[sec] = []
        clusters[sec].append(s.symbol)
    return clusters


# ── Rank trend tracking ──

# 多轮扫描排名跟踪：记录过去N轮各symbol的排名
_scan_rank_history: list[dict[str, int]] = []


def update_rank_history(current_ranks: dict[str, int]):
    """维护最近5轮排名历史"""
    _scan_rank_history.append(current_ranks)
    if len(_scan_rank_history) > 5:
        _scan_rank_history.pop(0)


def rank_streak_score(symbol: str) -> int:
    """连续上榜 + 排名上升 = 加分"""
    if len(_scan_rank_history) < 2:
        return 0
    ranks = []
    for snap in _scan_rank_history:
        if symbol in snap:
            ranks.append(snap[symbol])
    if len(ranks) < 2:
        return 0
    # 最近两次排名趋势：排名数字变小 = 上升
    recent = ranks[-1]
    prev = ranks[-2]
    diff = prev - recent  # 正数表示排名上升
    score = 0
    if diff >= 5:
        score += 6
    elif diff >= 2:
        score += 3
    elif diff < -3:
        score -= 4  # 排名明显下滑
    # 连续3轮以上都在榜单
    if len(ranks) >= 3:
        score += 4
    return score


# ── Intraday strength ──

_INTRADAY_CACHE: dict[str, tuple[float | None, float]] = {}
_INTRADAY_CACHE_TTL = 300   # 正常缓存5分钟
_INTRADAY_CACHE_FAIL_TTL = 60  # 失败后1分钟重试


def analyze_intraday(session: requests.Session, symbol: str) -> float | None:
    """获取分时数据，计算盘中强度（进攻性），返回 -3~+3 的分数

    评分维度：
      - 早盘贡献过半 +1.5 / 尾盘偷袭 -1.0
      - 特大单净流入>5% +1.5 / 净流出>5% -1.5
      - 多波攻击(≥4段上涨) +1.5 / 温和攻击(2-3段) +0.5
      - 价格高位运行(上30%) +0.5 / 低位运行(下30%) -0.5
      - 后半段持续新高 +0.5 / 冲高回落 -0.5
    """
    now = time.time()
    if symbol in _INTRADAY_CACHE:
        val, ts = _INTRADAY_CACHE[symbol]
        if val is not None and now - ts < _INTRADAY_CACHE_TTL:
            return val
        if val is None and now - ts < _INTRADAY_CACHE_FAIL_TTL:
            return None

    try:
        _throttle()
        ts_ms = int(time.time() * 1000)
        url = f"https://stock.xueqiu.com/v5/stock/chart/minute.json?symbol={symbol}&period=1d&_={ts_ms}"
        resp = session.get(url, timeout=15)
        d = resp.json()
        items = d.get("data", {}).get("items", [])
        if not items or len(items) < 10:
            _INTRADAY_CACHE[symbol] = (None, now)
            return None

        first_px = items[0]["current"]
        last_px = items[-1]["current"]
        total_chg = (last_px - first_px) / first_px * 100

        # 早盘（前1/3时段）vs 当前走势
        split = len(items) // 3
        morning_end = items[split]["current"]
        morning_chg = (morning_end - first_px) / first_px * 100

        # 资金流向
        capital = items[-1].get("capital", {})
        xlarge = capital.get("xlarge", 0) if capital else 0

        score = 0.0
        # 1. 早盘拉升后横盘（强势） vs 尾盘拉升（弱势）
        if total_chg > 0 and morning_chg > total_chg * 0.5:
            score += 1.5
        elif total_chg > 0 and morning_chg < total_chg * 0.3:
            score -= 1.0

        # 2. 资金面：大单主力流入
        if xlarge > 5:
            score += 1.5
        elif xlarge < -5:
            score -= 1.5

        # 3. 攻击波检测：将分时切N段，统计上涨段数
        segments = 10
        seg_size = len(items) // segments
        if seg_size > 0:
            seg_prices = [items[min(i * seg_size, len(items) - 1)]["current"] for i in range(segments + 1)]
            seg_changes = [(seg_prices[i + 1] - seg_prices[i]) / seg_prices[i] * 100 for i in range(segments)]
            attack_waves = sum(1 for c in seg_changes if c > 0.2)
            if attack_waves >= 4:
                score += 1.5
            elif attack_waves >= 2:
                score += 0.5

        # 4. 价格运行区间：当前价在日内高低点的位置
        high = max(item["current"] for item in items)
        low = min(item["current"] for item in items)
        if high > low:
            position = (last_px - low) / (high - low)
            if position > 0.7:
                score += 0.5
            elif position < 0.3 and total_chg < 3:
                score -= 0.5

        # 5. 走势一致性：后半段 vs 前半段
        mid = len(items) // 2
        mid_px = items[mid]["current"]
        first_half_chg = (mid_px - first_px) / first_px * 100
        second_half_chg = (last_px - mid_px) / mid_px * 100 if last_px != mid_px else 0
        if first_half_chg > 0 and second_half_chg > first_half_chg * 0.3:
            score += 0.5
        elif first_half_chg > 0 and second_half_chg < -first_half_chg * 0.3:
            score -= 0.5

        score = max(-3.0, min(3.0, score))
        _INTRADAY_CACHE[symbol] = (score, now)
        return score
    except Exception:
        _INTRADAY_CACHE[symbol] = (None, now)
        return None


# ── Recommendation tracking ──

def save_recommendations(conn: sqlite3.Connection, new_faces: list, old_faces: list, momentum: list):
    """保存本次推荐记录到DB"""
    today = date.today().isoformat()
    now = datetime.now().strftime("%H:%M:%S")
    for c in new_faces + old_faces + momentum:
        try:
            conn.execute(
                "INSERT INTO recommendations (date, time, symbol, name, category, score, percent, trend) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (today, now, c.stock.symbol, c.stock.name, c.category,
                 c.score, c.stock.percent, c.kline.trend if c.kline else None),
            )
        except Exception:
            continue
    conn.commit()


def _last_trading_day() -> date:
    """往前推到最近一个交易日"""
    d = date.today() - timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def update_recommendation_results(conn: sqlite3.Connection):
    """更新昨日（最近交易日）推荐股票今日表现"""
    yesterday = _last_trading_day().isoformat()
    # 只更新尚未填写 next_day_pct 的记录
    cur = conn.execute(
        "SELECT DISTINCT symbol FROM recommendations WHERE date = ? AND next_day_pct IS NULL",
        (yesterday,),
    )
    symbols = [row[0] for row in cur.fetchall()]
    if not symbols:
        return

    # 从 daily_kline 获取今日K线涨跌幅
    today_str = date.today().isoformat()
    for sym in symbols:
        cur = conn.execute(
            "SELECT percent FROM daily_kline WHERE symbol = ? AND date = ?",
            (sym, today_str),
        )
        row = cur.fetchone()
        if row:
            conn.execute(
                "UPDATE recommendations SET next_day_pct = ? WHERE symbol = ? AND date = ? AND next_day_pct IS NULL",
                (row[0], sym, yesterday),
            )
    conn.commit()


def get_tracking_summary(conn: sqlite3.Connection) -> str:
    """生成昨日推荐跟踪摘要"""
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    cur = conn.execute(
        "SELECT name, category, score, percent, trend, next_day_pct "
        "FROM recommendations WHERE date = ? ORDER BY score DESC LIMIT 10",
        (yesterday,),
    )
    rows = cur.fetchall()
    if not rows:
        return ""

    lines = ["", "▎昨日回顾"]
    wins, losses = 0, 0
    for name, cat, score, pct, trend, nd_pct in rows:
        tag = {"new_face": "新", "momentum": "动量", "old_face": "旧"}.get(cat, "?")
        pct_str = f"{pct:+.2f}%" if pct else "N/A"
        if nd_pct is not None:
            nd = f"{nd_pct:+.2f}%"
            if nd_pct > 0:
                wins += 1
                nd += " ✅"
            else:
                losses += 1
                nd += " ❌"
        else:
            nd = "待更新"
        lines.append(f"  {tag} {name} {pct_str} → {nd}")

    total = wins + losses
    if total > 0:
        lines.append(f"  胜率: {wins}/{total} ({wins*100//total}%)")
    return "\n".join(lines)


# ── Fetch 飙升榜 ──

def fetch_biaosheng(session: requests.Session, size: int = 100) -> list[dict]:
    ts = int(time.time() * 1000)
    url = (
        f"https://stock.xueqiu.com/v5/stock/hot_stock/new_list.json"
        f"?page=1&size={size}&order=desc&order_by=rank_change&type=10&_={ts}"
    )
    resp = session.get(url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json().get("data", {}).get("items", [])


# ── 小而美: 批量获取市值 ──

# 市值数据缓存（避免重复请求）
_market_cap_cache: dict[str, dict] = {}
_market_cap_cache_time: float = 0


def fetch_market_caps_batch(session: requests.Session, symbols: list[str]) -> dict[str, dict]:
    """批量获取股票市值数据（先试批量API，失败则逐只回退）"""
    global _market_cap_cache, _market_cap_cache_time

    now = time.time()
    if _market_cap_cache and now - _market_cap_cache_time < 30:
        return _market_cap_cache

    if not symbols:
        return {}

    result: dict[str, dict] = {}

    # 方案1: 批量quote API
    try:
        for i in range(0, len(symbols), 50):
            batch = symbols[i:i + 50]
            sym_str = ",".join(batch)
            url = (f"https://stock.xueqiu.com/v5/stock/batch/quote.json"
                   f"?symbol={sym_str}&extend=market_cap")
            resp = session.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            items = resp.json().get("data", {}).get("items", [])
            for item in items:
                q = item.get("quote") if isinstance(item, dict) else {}
                if not q or not q.get("symbol"):
                    q = item if isinstance(item, dict) else {}
                sym = q.get("symbol", "")
                if sym:
                    mc = q.get("market_capital") or q.get("total_market_capital") or 0
                    cmc = q.get("circ_market_capital") or 0
                    result[sym] = {"market_cap": mc, "circ_market_cap": cmc}
    except Exception:
        pass

    if result:
        _market_cap_cache = result
        _market_cap_cache_time = now
        return result

    # 方案2: 逐只quote API（回退）
    try:
        for sym in symbols:
            url = f"https://stock.xueqiu.com/v5/stock/quote.json?symbol={sym}&extend=market_cap"
            resp = session.get(url, timeout=REQUEST_TIMEOUT)
            q = resp.json().get("data", {}).get("quote", {})
            if q.get("symbol"):
                result[sym] = {
                    "market_cap": q.get("market_capital", 0) or 0,
                    "circ_market_cap": q.get("circ_market_capital", 0) or 0,
                }
    except Exception:
        pass

    if result:
        _market_cap_cache = result
        _market_cap_cache_time = now
    elif not _market_cap_cache:
        print(f"\n  [!] 警告: 市值数据获取失败，小而美规则暂不生效")

    return result or _market_cap_cache

# ── Main scan ──

def scan(conn: sqlite3.Connection, session: requests.Session):
    today = date.today().isoformat()
    now = datetime.now().strftime("%H:%M:%S")

    # 1. 获取飙升榜
    raw = fetch_biaosheng(session)

    # 2. 只保留创业板，过滤ST/退市
    gem_stocks: list[StockInfo] = []
    for i, item in enumerate(raw, 1):
        symbol = item.get("symbol", "")
        code = item.get("code", "")
        name = item.get("name", "")
        if is_hk_stock(symbol) or not is_gem(code) or is_st(name):
            continue
        gem_stocks.append(StockInfo(
            symbol=symbol,
            name=item.get("name", ""),
            code=code,
            percent=item.get("percent") or 0.0,
            current=item.get("current") or 0.0,
            value=item.get("value") or 0.0,
            rank_change=item.get("rank_change") or 0,
            rank=i,
        ))

    gem_top = gem_stocks

    # 3. 先查历史记录（今天之前的），再记录今天的
    record_appearances(conn, [
        {"symbol": s.symbol, "name": s.name, "percent": s.percent, "value": s.value}
        for s in gem_top
    ])

    # 4. 先出候选（不含市值过滤），后续再对候选股拉市值
    raw_new_faces: list[Candidate] = []
    raw_old_faces: list[Candidate] = []
    raw_momentum: list[Candidate] = []

    for stock in gem_top:
        # 股价硬过滤
        if stock.current > 0 and stock.current > MAX_STOCK_PRICE:
            continue
        # 涨幅过高跳过（封板或已无介入空间）
        if stock.percent > 8:
            continue

        app_history = get_symbol_appearances(conn, stock.symbol, NEW_FACE_LOOKBACK_DAYS)
        previous_dates = [a["date"] for a in app_history if a["date"] < today]
        is_new = len(previous_dates) == 0
        first_date = previous_dates[0] if previous_dates else today

        # 旧面孔必须有前置涨幅（近N天至少有一天涨幅≥5%），否则不算热点股
        if not is_new:
            strong_history = get_symbol_appearances(conn, stock.symbol, OLD_FACE_STRONG_PREV_LOOKBACK)
            has_strong_prev = any(a["percent"] >= 5 for a in strong_history if a["date"] < today)
            if not has_strong_prev:
                continue

        kline = ensure_kline(conn, session, stock.symbol)
        kline_summary = None

        if is_new:
            kline_summary = analyze_new_face(stock, kline)
            if kline_summary and kline_summary.score >= 20:
                raw_new_faces.append(Candidate(
                    stock=stock, category="new_face", score=kline_summary.score,
                    reason=kline_summary.trend, kline=kline_summary,
                    first_seen=first_date,
                    history_pct=[k["percent"] for k in kline] if kline else [],
                ))
            else:
                # 新面孔失败 → 尝试动量延续（已有累计涨幅的票）
                momentum = analyze_momentum(stock, kline)
                if momentum and momentum.score >= MOMENTUM_MIN_SCORE:
                    raw_momentum.append(Candidate(
                        stock=stock, category="momentum", score=momentum.score,
                        reason=momentum.trend, kline=momentum,
                        first_seen=first_date,
                        history_pct=[k["percent"] for k in kline] if kline else [],
                    ))
        else:
            kline_summary = analyze_old_face(stock, kline)
            if kline_summary and kline_summary.score >= 10:
                raw_old_faces.append(Candidate(
                    stock=stock, category="old_face", score=kline_summary.score,
                    reason=kline_summary.trend, kline=kline_summary,
                    first_seen=first_date,
                    history_pct=[k["percent"] for k in kline] if kline else [],
                ))

    # 5. 对候选股拉市值，做小而美过滤 + 加分
    all_raw = raw_new_faces + raw_old_faces + raw_momentum
    if all_raw:
        cand_symbols = list(set(c.stock.symbol for c in all_raw))
        market_caps = fetch_market_caps_batch(session, cand_symbols)
    else:
        market_caps = {}

    new_faces: list[Candidate] = []
    old_faces: list[Candidate] = []
    momentum: list[Candidate] = []
    filtered_large_cap = 0

    for c in all_raw:
        cap_data = market_caps.get(c.stock.symbol, {})
        market_cap = cap_data.get("market_cap", 0)

        # 市值过滤
        if market_cap > 0 and market_cap > MAX_MARKET_CAP:
            filtered_large_cap += 1
            continue

        c.market_cap = market_cap
        c.circ_market_cap = cap_data.get("circ_market_cap", 0)

        if c.category == "new_face":
            new_faces.append(c)
        elif c.category == "momentum":
            momentum.append(c)
        else:
            old_faces.append(c)

    # 6. 行业集群 + 排名趋势 + 分时强度 额外加分
    clusters = get_sector_clusters(gem_top)

    for c in new_faces + old_faces + momentum:
        # 排名趋势加分（连续上榜/排名上升）
        c.rank_trend_bonus = rank_streak_score(c.stock.symbol)
        # 行业集群加分（同行业多只上榜说明板块效应，仅限真实行业）
        sec = classify_sector(c.stock.name)
        c.sector = sec
        if sec != "其他":
            cluster_count = len(clusters.get(sec, []))
            if cluster_count >= 3:
                c.sector_bonus = 8
            elif cluster_count >= 2:
                c.sector_bonus = 4
        # 分时强度（资金流向+早盘强度）
        intra = analyze_intraday(session, c.stock.symbol)
        if intra is not None:
            c.intraday_score = intra
        # 加到总分
        c.score += c.rank_trend_bonus + c.sector_bonus + int(c.intraday_score)

    # 分时强度过滤：弱势分时走势(-1以下)直接剔除，不参与排名
    new_faces = [c for c in new_faces if c.intraday_score > -1]
    old_faces = [c for c in old_faces if c.intraday_score > -1]
    momentum = [c for c in momentum if c.intraday_score > -1]

    # 更新排名历史（用于趋势跟踪）
    all_cats = new_faces + old_faces + momentum
    update_rank_history({c.stock.symbol: c.stock.rank for c in all_cats})

    new_faces.sort(key=lambda c: c.score, reverse=True)
    old_faces.sort(key=lambda c: c.score, reverse=True)
    momentum.sort(key=lambda c: c.score, reverse=True)
    new_faces.sort(key=lambda c: c.stock.rank)
    old_faces.sort(key=lambda c: c.stock.rank)
    momentum.sort(key=lambda c: c.stock.rank)
    return new_faces, old_faces, momentum, gem_stocks, filtered_large_cap


# ── Display ──

ANSI = {
    "RED": "\033[91m", "YELLOW": "\033[93m", "GREEN": "\033[92m",
    "CYAN": "\033[96m", "BOLD": "\033[1m", "RESET": "\033[0m",
}

# 记录上一次扫描的排名，用于展示排名变化
_last_ranks: dict[str, int] = {}


def _rank_delta_str(symbol: str, current_rank: int) -> tuple[str, str]:
    """返回 (显示的delta文本, ANSI颜色前缀) — 空颜色前缀表示无特殊颜色"""
    prev = _last_ranks.get(symbol)
    if prev is None:
        return "  —", ""
    diff = prev - current_rank
    if diff > 0:
        return f"↑{diff}", ANSI["RED"] if diff >= 5 else ""
    if diff < 0:
        return f"↓{-diff}", ANSI["GREEN"] if -diff >= 5 else ""
    return "  —", ""


def _vis_len(s: str) -> int:
    """终端视觉宽度（CJK双宽）"""
    return sum(wcwidth.wcwidth(c) or 1 for c in s)


def _pad(s: str, width: int, align: str = "l") -> str:
    """按视觉宽度填充字符串"""
    pad = max(0, width - _vis_len(s))
    return f"{' ' * pad}{s}" if align == "r" else f"{s}{' ' * pad}"


def clear_screen():
    print("\033[2J\033[H", end="")


def fmt_time():
    return datetime.now().strftime("%H:%M:%S")


def pct_colored(pct: float, width: int = 8) -> str:
    s = f"{pct:+.2f}%"
    if pct >= 9:
        c = ANSI["RED"]
    elif pct >= 5:
        c = ANSI["GREEN"]
    elif pct < 0:
        c = ANSI["YELLOW"]
    else:
        c = ""
    return f"{c}{s:>{width}}{ANSI['RESET']}" if c else f"{s:>{width}}"


def _bonus_tag(c: Candidate) -> str:
    """生成加分项标签文本"""
    parts = []
    if c.rank_trend_bonus:
        parts.append(f"T{c.rank_trend_bonus:+d}")
    if c.sector_bonus:
        parts.append(f"S{c.sector_bonus:+d}")
    if c.intraday_score:
        parts.append(f"D{int(c.intraday_score):+d}")
    return " ".join(parts) if parts else ""


def _fmt_market_cap(cap: float) -> str:
    """格式化市值显示（亿）"""
    if cap <= 0:
        return ""
    cap_yi = cap / YI
    if cap_yi < 10:
        return f"{cap_yi:.1f}亿"
    return f"{cap_yi:.0f}亿"


def display(new_faces: list[Candidate], old_faces: list[Candidate], momentum: list[Candidate],
            gem_total: int, interval: int, filtered_large_cap: int = 0):
    clear_screen()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    print(f"{'='*96}")
    print(f"  创业板飙升榜监控  ({now})")

    # 行业集群提示 + 小而美过滤
    all_c = new_faces + old_faces + momentum
    sec_counts: dict[str, int] = {}
    for c_ in all_c:
        if c_.sector:
            sec_counts[c_.sector] = sec_counts.get(c_.sector, 0) + 1
    hot_secs = [f"{s}{c}" for s, c in sorted(sec_counts.items(), key=lambda x: -x[1])[:3]]
    sec_line = f"  {' '.join(hot_secs)}" if hot_secs else ""
    filter_info = f" | 过滤{filtered_large_cap}只" if filtered_large_cap else ""
    # 检查是否有市值数据
    cap_count = sum(1 for c in all_c if c.market_cap > 0)
    cap_status = f"市值数据{cap_count}/{len(all_c)}" if all_c else "暂无候选"
    print(f"  创业板共 {gem_total} 只 | 新{len(new_faces)}动{len(momentum)}旧{len(old_faces)}{filter_info} | {sec_line} | {cap_status} | 每{interval}s刷新")
    print(f"  小而美: 市值≤{int(MAX_MARKET_CAP/YI)}亿 股价≤{MAX_STOCK_PRICE}元")
    print(f"{'='*96}")

    def _print_row(c: Candidate, show_val: bool = False):
        s = c.stock
        k = c.kline
        cur = f"{s.current:.2f}" if s.current else "N/A"
        acc = f"{k.accumulated_pct:+.2f}%" if k else "N/A"
        vr = f"{k.volume_ratio:.1f}x" if k else "N/A"
        score_visible = str(c.score)
        score_tag = f"{ANSI['BOLD']}{_pad(score_visible,4,'r')}{ANSI['RESET']}" if c.score >= 15 else _pad(score_visible,4,'r')
        trend_tag = k.trend if k else "N/A"
        delta_text, delta_color = _rank_delta_str(s.symbol, s.rank)
        delta_display = f"{delta_color}{_pad(delta_text,6,'r')}{ANSI['RESET']}" if delta_color else _pad(delta_text,6,'r')
        bonus_str = _bonus_tag(c)
        cap_str = _fmt_market_cap(c.market_cap)
        val_str = f"{s.value:.0f}" if s.value else "N/A"
        if show_val:
            print(f"  {s.rank:>4} {delta_display} {_pad(s.name,10)} {s.symbol:<12} {cur:>7} {pct_colored(s.percent)} {_pad(trend_tag,14)} {acc:>8} {vr:>6} {score_tag} {_pad(bonus_str,16)} {cap_str:>8} {val_str:>6}")
        else:
            print(f"  {s.rank:>4} {delta_display} {_pad(s.name,10)} {s.symbol:<12} {cur:>7} {pct_colored(s.percent)} {_pad(trend_tag,14)} {acc:>8} {vr:>6} {score_tag} {_pad(bonus_str,16)} {cap_str:>8}")

    hdr = f"  {_pad('排名',4,'r')} {_pad('变化',6,'r')} {_pad('名称',10)} {_pad('代码',12)} {_pad('现价',7,'r')} {_pad('涨幅',8,'r')} {_pad('趋势',14)} {_pad('5日累计',8,'r')} {_pad('量比',6,'r')} {_pad('评分',4,'r')} {_pad('增强',16)} {_pad('市值',8,'r')}"

    # ── 新面孔 ──
    print(f"\n{ANSI['GREEN']}◆ 新面孔 — 底部异动 / 刚启动{ANSI['RESET']}  (找: 今日小涨+日线底部放量)")
    print(hdr)
    print(f"  {'-'*108}")
    if new_faces:
        for c in new_faces:
            _print_row(c)
    else:
        print(f"  {ANSI['YELLOW']}暂无新面孔{ANSI['RESET']}")

    # ── 动量延续 ──
    if momentum:
        print(f"\n{ANSI['YELLOW']}◆ 动量延续 — 已启动 / 温和上攻{ANSI['RESET']}  (找: 累计涨幅已起+今日温和放量)")
        print(hdr)
        print(f"  {'-'*108}")
        for c in momentum:
            _print_row(c)

    # ── 旧面孔 ──
    hdr_val = f"{hdr} {_pad('热度',6,'r')}"
    print(f"\n{ANSI['CYAN']}◆ 旧面孔 — 盘整 / 回调低吸{ANSI['RESET']}  (找: 前期热点+今日回调)")
    print(hdr_val)
    print(f"  {'-'*116}")
    if old_faces:
        for c in old_faces:
            _print_row(c, show_val=True)
    else:
        print(f"  {ANSI['YELLOW']}暂无旧面孔{ANSI['RESET']}")

    # ── 策略 ──
    print(f"\n{'-'*96}")
    print(f"  {ANSI['GREEN']}新面孔{ANSI['RESET']}: 底部放量启动+涨幅2-6%")
    print(f"  {ANSI['YELLOW']}动量延续{ANSI['RESET']}: 累计涨幅10%+今日温和上攻")
    print(f"  {ANSI['CYAN']}旧面孔{ANSI['RESET']}: 缩量回调+未破位+高热度")
    print()


# ── Feishu Push ──

def push_feishu(new_faces: list[Candidate], old_faces: list[Candidate], momentum: list[Candidate], gem_total: int, conn: sqlite3.Connection | None = None):
    """推送极简版扫描结果到飞书"""
    if not FEISHU_WEBHOOK:
        return

    now = datetime.now().strftime("%H:%M")
    all_c = new_faces + old_faces + momentum

    # 行业热度（最多2个）
    sec_cnt: dict[str, int] = {}
    for c in all_c:
        if c.sector:
            sec_cnt[c.sector] = sec_cnt.get(c.sector, 0) + 1
    sec_hot = " ".join(f"{s}{n}" for s, n in sorted(sec_cnt.items(), key=lambda x: -x[1])[:2])

    lines = [f"{FEISHU_KEYWORD}",
             f"{now} 新{len(new_faces)}动{len(momentum)}旧{len(old_faces)}" + (f" | {sec_hot}" if sec_hot else "")]

    if new_faces:
        lines.append(f"▎新")
        for c in new_faces:
            s = c.stock
            lines.append(f" {s.rank} {s.name} {s.percent:+.1f}% {c.score}分")

    if momentum:
        lines.append(f"▎动量")
        for c in momentum:
            s = c.stock
            lines.append(f" {s.rank} {s.name} {s.percent:+.1f}% {c.score}分")

    if old_faces:
        lines.append(f"▎旧")
        for c in old_faces:
            s = c.stock
            lines.append(f" {s.rank} {s.name} {s.percent:+.1f}% {c.score}分")

    text = "\n".join(lines)

    try:
        resp = requests.post(FEISHU_WEBHOOK, json={
            "msg_type": "text",
            "content": {"text": text},
        }, timeout=10)
        result = resp.json()
        if result.get("code") != 0:
            print(f"\n  [!] 推送失败: {result.get('msg')}")
    except Exception as e:
        print(f"\n  [!] 推送异常: {e}")


def log_results(new_faces: list[Candidate], old_faces: list[Candidate], momentum: list[Candidate]):
    os.makedirs(LOG_DIR, exist_ok=True)
    today = date.today().isoformat()
    log_file = os.path.join(LOG_DIR, f"scan_{today}.csv")
    is_new = not os.path.exists(log_file)
    with open(log_file, "a", encoding="utf-8") as f:
        if is_new:
            f.write("时间,分类,名称,代码,现价,涨幅,趋势,5日累计,量比,评分\n")
        now = datetime.now().strftime("%H:%M:%S")
        for c in (new_faces[:5] + momentum[:5] + old_faces[:5]):
            k = c.kline
            tag = {"new_face": "新", "momentum": "动量", "old_face": "旧"}.get(c.category, "?")
            f.write(f"{now},{tag},{c.stock.name},{c.stock.symbol},{c.stock.current:.2f},{c.stock.percent:+.2f}%,{k.trend if k else ''},{k.accumulated_pct if k else ''},{k.volume_ratio if k else ''},{c.score}\n")


# ── Main Loop ──

def main():
    interval = REFRESH_INTERVAL
    if len(sys.argv) > 1:
        try:
            interval = max(60, int(sys.argv[1]))
        except ValueError:
            pass

    conn = init_db()
    session = make_session()

    print(f"  创业板飙升扫描器  |  每{interval}s刷新  |  DB: {DB_PATH}")
    print(f"  新面孔: 过去{NEW_FACE_LOOKBACK_DAYS}天未出现 = 新 | 旧面孔: 出现过 = 旧")
    print(f"  交易时段: 09:30-11:45 / 13:00-15:00  |  非交易时段自动休眠")
    print(f"  {'='*60}\n")

    while True:
        # ── 交易时段检查 ──
        now = datetime.now()
        if not is_trading_time(now):
            wait = seconds_until_next_session(now)
            label = next_session_label(now)
            print(f"\r  🌙 非交易时段 | {label} ({wait // 60}分后)  ", end="", flush=True)
            # 最长每60秒醒一次（防止进程无法退出）
            for _ in range(min(wait, interval), 0, -60):
                time.sleep(60)
                # 如果进入了交易时段，提前结束等待
                if is_trading_time():
                    break
            continue

        try:
            # 更新昨日推荐跟踪（每天一次，数据存在才非空）
            update_recommendation_results(conn)

            new_faces, old_faces, momentum, all_gem, filtered_large_cap = scan(conn, session)

            # ── 展示 ──
            display(new_faces, old_faces, momentum, len(all_gem), interval,
                    filtered_large_cap=filtered_large_cap)
            log_results(new_faces, old_faces, momentum)

            # 更新排名记录，用于下次对比
            _last_ranks.clear()
            for s in all_gem:
                _last_ranks[s.symbol] = s.rank

            # 底部摘要 + 昨日回顾
            # track_msg = get_tracking_summary(conn)
            if new_faces:
                top = new_faces[0]
                print(f"  ▶ 新面孔首选: {top.stock.name}({top.stock.symbol}) "
                      f"{top.stock.percent:+.2f}% | {top.kline.trend if top.kline else ''}")
                if top.score >= 20:
                    print(f"  ⚠️  底部异动信号! {top.stock.name} 评分{top.score}")
            if momentum:
                top_m = momentum[0]
                print(f"  ▶ 动量延续首选: {top_m.stock.name}({top_m.stock.symbol}) "
                      f"{top_m.stock.percent:+.2f}% | {top_m.kline.trend if top_m.kline else ''}")
            if old_faces:
                top_o = old_faces[0]
                print(f"  ▶ 旧面孔首选: {top_o.stock.name}({top_o.stock.symbol}) "
                      f"{top_o.stock.percent:+.2f}% | {top_o.kline.trend if top_o.kline else ''}")
            # if track_msg:
            #     print(track_msg)

            # 保存推荐 & 推送飞书
            save_recommendations(conn, new_faces, old_faces, momentum)
            # push_feishu(new_faces, old_faces, momentum, len(all_gem), conn)

        except requests.RequestException as e:
            print(f"\n  [!] 网络错误: {e}")
        except Exception as e:
            print(f"\n  [!] 错误: {type(e).__name__}: {e}")

        # 倒计时（每秒刷新，实时检查交易时间）
        for remaining in range(interval, 0, -1):
            if not is_trading_time():
                break
            if remaining % 10 == 0 or remaining <= 10:
                print(f"\r  ⏳ 下次刷新还有 {remaining}s ...", end="", flush=True)
            time.sleep(1)
        print()


if __name__ == "__main__":
    main()
