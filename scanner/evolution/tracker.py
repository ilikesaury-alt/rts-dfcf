import sqlite3
from datetime import date


def backfill_outcomes(conn: sqlite3.Connection, session=None) -> dict:
    """Backfill missing forward returns using next-day open as entry.

    For ultra-short trading, entry is assumed at the next trading day's
    OPEN price (not the recommendation day's close).  Returns are computed
    from that open to future closes: next_day_pct = (day2_close - day2_open) / day2_open.

    Safe to call every scan cycle – already-filled rows are skipped.
    """
    today_str = date.today().isoformat()

    # First pass: find recs with next-day K-line data, use next-day open as entry
    missing = conn.execute("""
        SELECT r.id, r.symbol, r.date,
               (SELECT d2.open FROM daily_kline d2
                WHERE d2.symbol = r.symbol AND d2.date > r.date
                ORDER BY d2.date LIMIT 1) AS entry_open
        FROM recommendations r
        WHERE r.next_day_pct IS NULL
          AND EXISTS (
            SELECT 1 FROM daily_kline d2
            WHERE d2.symbol = r.symbol AND d2.date > r.date
          )
    """).fetchall()

    # Second pass: use API fallback for rows without cached data
    if session is not None:
        fallback_missing = conn.execute("""
            SELECT r.id, r.symbol, r.date
            FROM recommendations r
            WHERE r.next_day_pct IS NULL
              AND NOT EXISTS (
                SELECT 1 FROM daily_kline d2
                WHERE d2.symbol = r.symbol AND d2.date > r.date
              )
        """).fetchall()

        for rec_id, symbol, rec_date in fallback_missing:
            from scanner.api import fetch_kline
            from scanner.database import save_kline_to_db

            try:
                kline = fetch_kline(session, symbol)
                if kline:
                    save_kline_to_db(conn, symbol, kline)
                    next_day = None
                    for k in kline:
                        if k["date"] > rec_date:
                            next_day = k["open"]
                            break
                    if next_day is not None:
                        missing.append((rec_id, symbol, rec_date, next_day))
            except Exception:
                continue

    if not missing:
        return {"total": 0, "filled": 0, "skipped": 0}

    filled = 0
    skipped = 0

    for rec_id, symbol, rec_date, entry_open in missing:
        if entry_open is None or entry_open <= 0:
            skipped += 1
            continue

        future = conn.execute("""
            SELECT date, close FROM daily_kline
            WHERE symbol = ? AND date > ? AND date <= ?
            ORDER BY date LIMIT 5
        """, (symbol, rec_date, today_str)).fetchall()

        if not future and session is not None:
            try:
                from scanner.api import fetch_kline
                from scanner.database import save_kline_to_db
                kline = fetch_kline(session, symbol)
                if kline:
                    save_kline_to_db(conn, symbol, kline)
                    future = conn.execute("""
                        SELECT date, close FROM daily_kline
                        WHERE symbol = ? AND date > ? AND date <= ?
                        ORDER BY date LIMIT 5
                    """, (symbol, rec_date, today_str)).fetchall()
            except Exception:
                pass

        if not future:
            skipped += 1
            continue

        closes = [r[1] for r in future]
        fwd_1d = (closes[0] - entry_open) / entry_open
        fwd_3d = (closes[min(2, len(closes) - 1)] - entry_open) / entry_open if len(closes) >= 3 else None
        fwd_5d = (closes[min(4, len(closes) - 1)] - entry_open) / entry_open if len(closes) >= 5 else None

        conn.execute("""
            UPDATE recommendations SET
                next_day_pct = ?, fwd_3d = ?, fwd_5d = ?
            WHERE id = ? AND next_day_pct IS NULL
        """, (
            round(fwd_1d * 100, 2),
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
