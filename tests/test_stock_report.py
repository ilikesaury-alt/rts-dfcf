import sqlite3
import tempfile
import os

from stock_report import find_stock


def _make_conn():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    return conn, path


def test_find_stock_digit_match_both_tables_not_truncated():
    # C1 回归：修复前 UNION 后整体 LIMIT 1 会把 recommendations 中的命中截断，
    # 仅返回 1 条；修复后子查询分别 LIMIT，两表各命中均保留。
    conn, path = _make_conn()
    try:
        conn.execute("CREATE TABLE appearances (symbol TEXT, name TEXT)")
        conn.execute("CREATE TABLE recommendations (symbol TEXT, name TEXT)")
        conn.execute("INSERT INTO appearances VALUES ('300320', '测试A')")
        conn.execute("INSERT INTO recommendations VALUES ('300320', '测试A')")
        conn.commit()
        res = find_stock(conn, "300320")
        symbols = {r["symbol"] for r in res}
        # 两表各 LIMIT 1，UNION 去重后仍应返回该代码
        assert symbols == {"300320"}, symbols
    finally:
        conn.close()
        os.remove(path)


def test_find_stock_name_match_both_tables():
    conn, path = _make_conn()
    try:
        conn.execute("CREATE TABLE appearances (symbol TEXT, name TEXT)")
        conn.execute("CREATE TABLE recommendations (symbol TEXT, name TEXT)")
        conn.execute("INSERT INTO appearances VALUES ('300111', '半导体甲')")
        conn.execute("INSERT INTO recommendations VALUES ('300222', '半导体乙')")
        conn.commit()
        res = find_stock(conn, "半导体")
        symbols = {r["symbol"] for r in res}
        assert symbols == {"300111", "300222"}, symbols
    finally:
        conn.close()
        os.remove(path)

