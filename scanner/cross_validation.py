"""第三层：交叉验证层。

只读现有系统 (recommendations) 和新系统 (chokepoint_recommendations)
的输出，标记交叉验证级别，写入独立表 cross_validated_signals。

不修改也不依赖两套系统的任何文件。
"""

import json
import sqlite3
from datetime import datetime, timedelta

from scanner.config import now_beijing
from scanner.database import DB_PATH, init_cross_validation_tables


def _load_existing_recommendations(conn: sqlite3.Connection, days: int = 1) -> dict[str, dict]:
    today = now_beijing().date().isoformat()
    rows = conn.execute(
        "SELECT symbol, name, category, score, trend FROM recommendations "
        "WHERE date = ? ORDER BY score DESC",
        (today,),
    ).fetchall()
    return {
        r[0]: {"name": r[1], "category": r[2], "score": r[3], "trend": r[4]}
        for r in rows
    }


def _load_chain_recommendations(conn: sqlite3.Connection, days: int = 1) -> dict[str, dict]:
    today = now_beijing().date().isoformat()
    rows = conn.execute(
        "SELECT symbol, name, chain_name, node_name, is_bottleneck, chain_phase, score "
        "FROM chokepoint_recommendations WHERE date = ? ORDER BY score DESC",
        (today,),
    ).fetchall()
    return {
        r[0]: {
            "name": r[1], "chain_name": r[2], "node_name": r[3],
            "is_bottleneck": r[4], "chain_phase": r[5], "score": r[6],
        }
        for r in rows
    }


_CROSS_T1_BONUS = 15
_CROSS_T2_BONUS = 8


def cross_validate() -> list[dict]:
    conn = init_cross_validation_tables()
    try:
        existing = _load_existing_recommendations(conn)
        chain_recs = _load_chain_recommendations(conn)
        today = now_beijing().date().isoformat()
        now = datetime.now().strftime("%H:%M:%S")

        results: list[dict] = []

        if not existing or not chain_recs:
            return results

        for sym, chain_info in chain_recs.items():
            if sym in existing:
                exist_info = existing[sym]
                level = "T1"
                bonus = _CROSS_T1_BONUS
                results.append({
                    "date": today,
                    "symbol": sym,
                    "name": chain_info["name"],
                    "level": level,
                    "bonus": bonus,
                    "existing_category": exist_info["category"],
                    "chain_name": chain_info["chain_name"],
                    "chain_phase": chain_info["chain_phase"],
                })
            else:
                sym_in_existing_last_3d = _check_appearance_history(conn, sym)
                if sym_in_existing_last_3d:
                    level = "T2"
                    bonus = _CROSS_T2_BONUS
                else:
                    level = "T3"
                    bonus = 0

                latest_cat = _get_latest_existing_category(conn, sym) if sym_in_existing_last_3d else None
                results.append({
                    "date": today,
                    "symbol": sym,
                    "name": chain_info["name"],
                    "level": level,
                    "bonus": bonus,
                    "existing_category": latest_cat,
                    "chain_name": chain_info["chain_name"],
                    "chain_phase": chain_info["chain_phase"],
                })

        _save_results(conn, results, now)
        return results
    finally:
        conn.close()


def _check_appearance_history(conn: sqlite3.Connection, symbol: str) -> bool:
    try:
        lookback = (now_beijing().date() - timedelta(days=3)).isoformat()
        row = conn.execute(
            "SELECT 1 FROM appearances WHERE symbol = ? AND date >= ? LIMIT 1",
            (symbol, lookback),
        ).fetchone()
        return row is not None
    except Exception:
        return False


def _get_latest_existing_category(conn: sqlite3.Connection, symbol: str) -> str | None:
    try:
        row = conn.execute(
            "SELECT category FROM recommendations WHERE symbol = ? ORDER BY date DESC LIMIT 1",
            (symbol,),
        ).fetchone()
        return row[0] if row else None
    except Exception:
        return None


def _save_results(conn: sqlite3.Connection, results: list[dict], now: str):
    today = now_beijing().date().isoformat()
    conn.execute("DELETE FROM cross_validated_signals WHERE date = ?", (today,))
    for r in results:
        try:
            conn.execute(
                "INSERT INTO cross_validated_signals "
                "(date, symbol, name, level, bonus, existing_category, chain_name, chain_phase, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (r["date"], r["symbol"], r["name"], r["level"], r["bonus"],
                 r["existing_category"], r["chain_name"], r["chain_phase"], now),
            )
        except Exception as e:
            print(f"  [!] 交叉验证保存失败 {r['symbol']}: {e}")
    conn.commit()


