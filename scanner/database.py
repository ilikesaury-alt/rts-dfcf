import sqlite3
from datetime import date, datetime, timedelta

from scanner.config import DB_PATH
from scanner.trading_session import is_trading_day


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
            score_breakdown TEXT
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
    today = date.today().isoformat()
    lookback = (date.today() - timedelta(days=days)).isoformat()
    cur = conn.execute(
        "SELECT DISTINCT symbol FROM appearances WHERE date >= ? AND date < ?",
        (lookback, today),
    )
    return {row[0] for row in cur.fetchall()}


def record_appearances(conn: sqlite3.Connection, symbols: list[dict]):
    today = date.today().isoformat()
    rows = []
    for i, item in enumerate(symbols, 1):
        rows.append((
            item["symbol"], item["name"], today, i,
            item.get("percent", 0), item.get("value", 0),
        ))
    try:
        conn.executemany(
            "INSERT INTO appearances (symbol, name, date, rank, percent, value) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(symbol, date) DO UPDATE SET "
            "percent = MAX(percent, excluded.percent), rank = excluded.rank, "
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
                    "percent = MAX(percent, excluded.percent), rank = excluded.rank, "
                    "value = excluded.value, name = excluded.name",
                    row,
                )
            except Exception as e2:
                print(f"  [!] 逐行写入appearances失败 {row[0]}: {e2}")
        conn.commit()


def _n_trading_days_ago(n: int) -> str:
    cursor = date.today()
    trading_days = 0
    while trading_days < n:
        cursor -= timedelta(days=1)
        if is_trading_day(cursor):
            trading_days += 1
    return cursor.isoformat()


def get_symbol_appearances(conn: sqlite3.Connection, symbol: str, days: int) -> list[dict]:
    today = date.today().isoformat()
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
    lookback = (date.today() - timedelta(days=60)).isoformat()
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


def save_recommendations(conn: sqlite3.Connection, new_faces: list, momentum: list):
    import json
    today = date.today().isoformat()
    now = datetime.now().strftime("%H:%M:%S")
    for c in new_faces + momentum:
        try:
            existing = conn.execute(
                "SELECT id FROM recommendations WHERE date = ? AND symbol = ? AND category = ? LIMIT 1",
                (today, c.stock.symbol, c.category),
            ).fetchone()
            if existing:
                continue
            breakdown = json.dumps(c.kline.dimensions, ensure_ascii=False) if c.kline and c.kline.dimensions else None
            conn.execute(
                "INSERT INTO recommendations (date, time, symbol, name, category, score, percent, trend, score_breakdown) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (today, now, c.stock.symbol, c.stock.name, c.category,
                 c.score, c.stock.percent, c.kline.trend if c.kline else None, breakdown),
            )
        except Exception as e:
            print(f"  [!] 保存推荐记录失败 {c.stock.symbol}: {e}")
    conn.commit()
