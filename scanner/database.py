import logging
import sqlite3
from datetime import date, datetime, timedelta

from scanner.config import DB_PATH, now_beijing
from scanner.trading_session import is_trading_day

logger = logging.getLogger(__name__)


def init_db() -> sqlite3.Connection:
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
            next_day_pct REAL,
            fwd_3d REAL,
            fwd_5d REAL,
            score_breakdown TEXT,
            source TEXT DEFAULT 'xueqiu'
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
    cur = conn.execute("PRAGMA table_info(recommendations)")
    cols = {row[1] for row in cur.fetchall()}
    if "source" not in cols:
        conn.execute("ALTER TABLE recommendations ADD COLUMN source TEXT DEFAULT 'xueqiu'")
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_rec_source ON recommendations(source)")
    except Exception:
        pass
    conn.commit()
    return conn


def record_appearances(conn: sqlite3.Connection, symbols: list[dict]):
    today = now_beijing().date().isoformat()
    rows = []
    for i, item in enumerate(symbols, 1):
        # rank 优先用真实榜单排名；缺失时回退到过滤后列表的下标（仅兜底，不应发生）
        rank = item.get("rank", i)
        if rank is None:
            rank = i
        rows.append((
            item["symbol"], item["name"], today, rank,
            item.get("percent", 0), item.get("value", 0),
        ))
    try:
        conn.executemany(
            "INSERT INTO appearances (symbol, name, date, rank, percent, value) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(symbol, date) DO UPDATE SET "
            "percent = excluded.percent, rank = excluded.rank, "
            "value = excluded.value, name = excluded.name",
            rows,
        )
        conn.commit()
    except Exception as e:
        print(f"  [!] 批量写入appearances失败: {e}, 逐行回退写入")
        for row in rows:
            try:
                conn.execute(
                    "INSERT INTO appearances (symbol, name, date, rank, percent, value) VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(symbol, date) DO UPDATE SET "
                    "percent = excluded.percent, rank = excluded.rank, "
                    "value = excluded.value, name = excluded.name",
                    row,
                )
            except Exception as e2:
                print(f"  [!] 逐行写入appearances失败 {row[0]}: {e2}")
        conn.commit()


def _n_trading_days_ago(n: int) -> str:
    cursor = now_beijing().date()
    trading_days = 0
    # 上限保护：避免节假日数据缺失/损坏时 is_trading_day 永远为 False 导致死循环
    max_iter = n * 3 + 30
    iters = 0
    while trading_days < n:
        cursor -= timedelta(days=1)
        iters += 1
        if iters > max_iter:
            logger.warning("_n_trading_days_ago(%d): max_iter=%d 触发, "
                           "回溯仅到达 %s (期望 ~%d 个交易日前), "
                           "节假日数据可能缺失", n, max_iter, cursor, n)
            break
        if is_trading_day(cursor):
            trading_days += 1
    return cursor.isoformat()


def get_symbol_appearances(conn: sqlite3.Connection, symbol: str, days: int) -> list[dict]:
    today = now_beijing().date().isoformat()
    lookback = _n_trading_days_ago(days)
    cur = conn.execute(
        "SELECT date, rank, percent, value FROM appearances WHERE symbol = ? AND date >= ? AND date < ? ORDER BY date",
        (symbol, lookback, today),
    )
    return [{"date": r[0], "rank": r[1], "percent": r[2], "value": r[3]} for r in cur.fetchall()]


def save_kline_to_db(conn: sqlite3.Connection, symbol: str, kline: list[dict]):
    rows = []
    for k in kline:
        rows.append((
            symbol, k["timestamp"], k["date"], k["open"], k["close"],
            k["high"], k["low"], k["volume"], k["percent"],
        ))
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO daily_kline "
            "(symbol, timestamp, date, open, close, high, low, volume, percent) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    except Exception as e:
        print(f"  [!] 批量写入kline失败: {e}, 逐行回退写入")
        for row in rows:
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO daily_kline "
                    "(symbol, timestamp, date, open, close, high, low, volume, percent) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    row,
                )
            except Exception as e2:
                print(f"  [!] 逐行写入kline失败 {row[0]}: {e2}")
        conn.commit()


def get_cached_kline(conn: sqlite3.Connection, symbol: str) -> list[dict] | None:
    lookback = (now_beijing().date() - timedelta(days=60)).isoformat()
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


def get_consecutive_appearance_days(conn: sqlite3.Connection, symbol: str, max_days: int = 10) -> int:
    """Count consecutive trading days a symbol appeared up to (not including) today."""
    today = now_beijing().date().isoformat()
    rows = conn.execute(
        "SELECT DISTINCT date FROM appearances WHERE symbol = ? AND date < ? ORDER BY date DESC LIMIT ?",
        (symbol, today, max_days),
    ).fetchall()
    if not rows:
        return 0
    dates = sorted(r[0] for r in rows)
    streak = 1
    for i in range(len(dates) - 1, 0, -1):
        d1 = date.fromisoformat(dates[i])
        d2 = date.fromisoformat(dates[i - 1])
        if _is_consecutive_trading_days(d2, d1):
            streak += 1
        else:
            break
    return streak


def _is_consecutive_trading_days(prev: date, curr: date) -> bool:
    """True if prev is the immediate previous trading day before curr (no trading days between)."""
    cursor = curr - timedelta(days=1)
    while cursor > prev:
        if is_trading_day(cursor):
            return False
        cursor -= timedelta(days=1)
    return True


def save_recommendations(conn: sqlite3.Connection, new_faces: list, momentum: list, source: str | None = None):
    import json
    today = now_beijing().date().isoformat()
    now = now_beijing().strftime("%H:%M:%S")
    for c in new_faces + momentum:
        try:
            existing = conn.execute(
                "SELECT id FROM recommendations WHERE date = ? AND symbol = ? AND category = ? LIMIT 1",
                (today, c.stock.symbol, c.category),
            ).fetchone()
            if existing:
                continue
            breakdown = json.dumps(c.kline.dimensions, ensure_ascii=False) if c.kline and c.kline.dimensions else None
            rec_source = source or getattr(c.stock, "source_tag", "unified")
            conn.execute(
                "INSERT INTO recommendations (date, time, symbol, name, category, score, percent, trend, score_breakdown, source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (today, now, c.stock.symbol, c.stock.name, c.category,
                 c.score, c.stock.percent, c.kline.trend if c.kline else None, breakdown, rec_source),
            )
        except Exception as e:
            print(f"  [!] 保存推荐记录失败 {c.stock.symbol}: {e}")
    conn.commit()

