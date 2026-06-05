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
    today = date.today().isoformat()
    lookback = (date.today() - timedelta(days=days)).isoformat()
    cur = conn.execute(
        "SELECT DISTINCT symbol FROM appearances WHERE date >= ? AND date < ?",
        (lookback, today),
    )
    return {row[0] for row in cur.fetchall()}


def record_appearances(conn: sqlite3.Connection, symbols: list[dict]):
    today = date.today().isoformat()
    for i, item in enumerate(symbols, 1):
        try:
            conn.execute(
                "INSERT INTO appearances (symbol, name, date, rank, percent, value) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(symbol, date) DO UPDATE SET "
                "percent = MAX(percent, excluded.percent), rank = excluded.rank, value = excluded.value, name = excluded.name",
                (item["symbol"], item["name"], today, i, item.get("percent", 0), item.get("value", 0)),
            )
        except Exception:
            continue
    conn.commit()


def get_symbol_appearances(conn: sqlite3.Connection, symbol: str, days: int) -> list[dict]:
    lookback = (date.today() - timedelta(days=days)).isoformat()
    cur = conn.execute(
        "SELECT date, rank, percent, value FROM appearances WHERE symbol = ? AND date >= ? ORDER BY date",
        (symbol, lookback),
    )
    return [{"date": r[0], "rank": r[1], "percent": r[2], "value": r[3]} for r in cur.fetchall()]


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


def ensure_kline(conn: sqlite3.Connection, session, symbol: str) -> list[dict] | None:
    from scanner.api import fetch_kline

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


def save_recommendations(conn: sqlite3.Connection, new_faces: list, old_faces: list, momentum: list):
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
    d = date.today() - timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def update_recommendation_results(conn: sqlite3.Connection):
    yesterday = _last_trading_day().isoformat()
    cur = conn.execute(
        "SELECT DISTINCT symbol FROM recommendations WHERE date = ? AND next_day_pct IS NULL",
        (yesterday,),
    )
    symbols = [row[0] for row in cur.fetchall()]
    if not symbols:
        return

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
