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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS parameter_snapshots (
            version TEXT PRIMARY KEY,
            params_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            metrics_json TEXT,
            notes TEXT,
            active INTEGER DEFAULT 0
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
    except Exception:
        for row in rows:
            try:
                conn.execute(
                    "INSERT INTO appearances (symbol, name, date, rank, percent, value) VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(symbol, date) DO UPDATE SET "
                    "percent = MAX(percent, excluded.percent), rank = excluded.rank, "
                    "value = excluded.value, name = excluded.name",
                    row,
                )
            except Exception:
                continue
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
    except Exception:
        for row in rows:
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO daily_kline "
                    "(symbol, timestamp, date, open, close, high, low, volume, percent) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    row,
                )
            except Exception:
                continue
        conn.commit()


def get_cached_kline(conn: sqlite3.Connection, symbol: str) -> list[dict] | None:
    today = date.today().isoformat()
    lookback = (date.today() - timedelta(days=15)).isoformat()
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
        except Exception:
            continue
    conn.commit()


def _last_trading_day(conn: sqlite3.Connection) -> str:
    today = date.today()
    cur = conn.execute(
        "SELECT MAX(date) FROM appearances WHERE date < ?",
        (today.isoformat(),),
    )
    row = cur.fetchone()
    if row and row[0]:
        return row[0]
    return (date.today() - timedelta(days=1)).isoformat()


def update_recommendation_results(conn: sqlite3.Connection, session=None):
    from scanner.evolution.tracker import backfill_outcomes
    result = backfill_outcomes(conn, session)
    if result["filled"] > 0:
        print(f"  [进化] 填补 {result['filled']}/{result['total']} 条推荐 outcome")


def get_active_weights(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        "SELECT params_json FROM parameter_snapshots WHERE active = 1 ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    if not row:
        return {}
    import json
    try:
        params = json.loads(row[0])
        return params.get("weights", {})
    except (json.JSONDecodeError, TypeError):
        return {}


def get_tracking_summary(conn: sqlite3.Connection) -> str:
    from scanner.evolution.tracker import tracking_stats

    stats = tracking_stats(conn)
    all_stats = stats.get("all")
    if not all_stats:
        return ""

    def fmt_wr(wins, total):
        return f"{wins}/{total} ({wins*100//max(total,1)}%)" if total else "N/A"

    lines = ["", "▎胜率统计 (累计)"]
    for cat in ("new_face", "known_new_face", "momentum", "all"):
        s = stats.get(cat)
        if not s or s["total"] == 0:
            continue
        label = {"new_face": "新面孔", "known_new_face": "已知△", "momentum": "动量", "all": "合计"}.get(cat, cat)
        w1 = fmt_wr(s["wins_1d"], s["total"])
        w3 = fmt_wr(s["wins_3d"], s["total"])
        w5 = fmt_wr(s["wins_5d"], s["total"])
        a1 = f"{s['avg_1d']:+.2f}%" if s["avg_1d"] is not None else "N/A"
        lines.append(f"  {label}: {s['total']}次  +1d{w1}  +3d{w3}  +5d{w5}  均收益{a1}")

    yesterday = _last_trading_day(conn)
    cur = conn.execute(
        "SELECT name, category, score, percent, trend, next_day_pct, fwd_3d, fwd_5d "
        "FROM recommendations WHERE date = ? ORDER BY score DESC LIMIT 8",
        (yesterday,),
    )
    rows = cur.fetchall()
    if rows:
        lines.append("")
        lines.append("▎昨日推荐明细")
        for name, cat, score, pct, trend, nd_pct, f3, f5 in rows:
            tag = {"new_face": "新", "known_new_face": "△", "momentum": "动量"}.get(cat, "?")
            parts = [f"  {tag} {name} 评分{score} 入{pct:+.1f}%"]
            if nd_pct is not None:
                parts.append(f"+1d{nd_pct:+.1f}%{'✅' if nd_pct > 0 else '❌'}")
            else:
                parts.append("+1d待更新")
            if f3 is not None:
                parts.append(f"+3d{f3:+.1f}%")
            if f5 is not None:
                parts.append(f"+5d{f5:+.1f}%")
            lines.append(" ".join(parts))

    return "\n".join(lines)
