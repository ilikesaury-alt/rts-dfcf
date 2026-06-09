import sqlite3
from datetime import date


def backfill_outcomes(conn: sqlite3.Connection, session=None) -> dict:
    """Backfill all missing next_day_pct, fwd_3d, fwd_5d from daily_kline.

    Finds every recommendation whose entry close is known (via daily_kline on
    the rec date) and forward returns are not yet filled.  Queries up to 5
    future trading days from daily_kline and computes 1d/3d/5d returns.

    If entry close is not cached (JOIN failure), optionally fetches K-line
    from the API via session to fill the gap.

    Safe to call every scan cycle – already-filled rows are skipped.
    """
    today_str = date.today().isoformat()

    # First pass: try JOIN (most common case)
    missing = conn.execute("""
        SELECT r.id, r.symbol, r.date, d.close AS entry_close
        FROM recommendations r
        JOIN daily_kline d ON r.symbol = d.symbol AND d.date = r.date
        WHERE r.next_day_pct IS NULL
    """).fetchall()

    # Second pass: use API fallback for rows without cached entry close
    if session is not None:
        fallback_missing = conn.execute("""
            SELECT r.id, r.symbol, r.date
            FROM recommendations r
            WHERE r.next_day_pct IS NULL
              AND NOT EXISTS (
                SELECT 1 FROM daily_kline d
                WHERE d.symbol = r.symbol AND d.date = r.date
              )
        """).fetchall()

        for rec_id, symbol, rec_date in fallback_missing:
            from scanner.api import fetch_kline
            from scanner.database import save_kline_to_db

            try:
                kline = fetch_kline(session, symbol)
                if kline:
                    save_kline_to_db(conn, symbol, kline)
                    entry_close = None
                    for k in kline:
                        if k["date"] == rec_date:
                            entry_close = k["close"]
                            break
                    if entry_close is None and len(kline) > 0:
                        entry_close = kline[-1]["close"]
                    if entry_close:
                        missing.append((rec_id, symbol, rec_date, entry_close))
            except Exception:
                continue

    if not missing:
        return {"total": 0, "filled": 0, "skipped": 0}

    filled = 0
    skipped = 0

    for rec_id, symbol, rec_date, entry_close in missing:
        future = conn.execute("""
            SELECT date, close FROM daily_kline
            WHERE symbol = ? AND date > ? AND date <= ?
            ORDER BY date LIMIT 5
        """, (symbol, rec_date, today_str)).fetchall()

        if not future:
            skipped += 1
            continue

        closes = [r[1] for r in future]
        fwd_1d = (closes[0] - entry_close) / entry_close if entry_close else None
        fwd_3d = (closes[min(2, len(closes) - 1)] - entry_close) / entry_close if len(closes) >= 3 else None
        fwd_5d = (closes[min(4, len(closes) - 1)] - entry_close) / entry_close if len(closes) >= 5 else None

        conn.execute("""
            UPDATE recommendations SET
                next_day_pct = ?, fwd_3d = ?, fwd_5d = ?
            WHERE id = ? AND next_day_pct IS NULL
        """, (
            round(fwd_1d * 100, 2) if fwd_1d is not None else None,
            round(fwd_3d * 100, 2) if fwd_3d is not None else None,
            round(fwd_5d * 100, 2) if fwd_5d is not None else None,
            rec_id,
        ))
        filled += 1

    conn.commit()
    return {"total": len(missing), "filled": filled, "skipped": skipped}


def tracking_stats(conn: sqlite3.Connection) -> dict:
    """Return 1d/3d/5d win-rate and average return per category."""
    rows = conn.execute("""
        SELECT category,
               COUNT(*) AS total,
               SUM(CASE WHEN next_day_pct > 0 THEN 1 ELSE 0 END) AS wins_1d,
               AVG(next_day_pct) AS avg_1d,
               SUM(CASE WHEN fwd_3d > 0 THEN 1 ELSE 0 END) AS wins_3d,
               AVG(fwd_3d) AS avg_3d,
               SUM(CASE WHEN fwd_5d > 0 THEN 1 ELSE 0 END) AS wins_5d,
               AVG(fwd_5d) AS avg_5d
        FROM recommendations
        WHERE next_day_pct IS NOT NULL
        GROUP BY category
    """).fetchall()

    stats = {}
    for r in rows:
        cat = r[0]
        total = r[1]
        stats[cat] = {
            "total": total,
            "wins_1d": r[2] or 0, "avg_1d": r[3],
            "wins_3d": r[4] or 0, "avg_3d": r[5],
            "wins_5d": r[6] or 0, "avg_5d": r[7],
        }

    # Aggregate across categories
    totals_row = conn.execute("""
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN next_day_pct > 0 THEN 1 ELSE 0 END) AS w1,
               AVG(next_day_pct) AS a1,
               SUM(CASE WHEN fwd_3d > 0 THEN 1 ELSE 0 END) AS w3,
               AVG(fwd_3d) AS a3,
               SUM(CASE WHEN fwd_5d > 0 THEN 1 ELSE 0 END) AS w5,
               AVG(fwd_5d) AS a5
        FROM recommendations
        WHERE next_day_pct IS NOT NULL
    """).fetchone()

    if totals_row and totals_row[0]:
        stats["all"] = {
            "total": totals_row[0],
            "wins_1d": totals_row[1] or 0, "avg_1d": totals_row[2],
            "wins_3d": totals_row[3] or 0, "avg_3d": totals_row[4],
            "wins_5d": totals_row[5] or 0, "avg_5d": totals_row[6],
        }

    return stats
