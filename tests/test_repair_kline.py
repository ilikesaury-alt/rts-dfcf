"""repair_kline.py 回归测试（2026-08-19 修复）。

覆盖：历史脏行 close 为字符串/NULL 时 abs() 不再抛 TypeError 中止整批修复——
此前 for bar 循环在 try 外，异常会跳过 conn.commit()、整批零写入。
"""
import sqlite3
import sys

import pytest

import repair_kline


@pytest.fixture
def tmp_db(tmp_path):
    path = tmp_path / "scanner.db"
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE daily_kline (
        symbol TEXT NOT NULL, timestamp INTEGER, date TEXT NOT NULL, open REAL, close REAL,
        high REAL, low REAL, volume REAL, percent REAL, finalized INTEGER DEFAULT 1,
        PRIMARY KEY(symbol, date))""")
    # 08-18 脏行：close 为字符串（契约重构前遗留）；08-17 为干净行（权威值一致）
    conn.execute("INSERT INTO daily_kline (symbol, date, open, close, high, low, volume, percent) "
                 "VALUES ('SZ300607', '2026-08-18', 36.27, '36.27', 36.27, 36.27, 100, 1.0)")
    conn.execute("INSERT INTO daily_kline (symbol, date, open, close, high, low, volume, percent) "
                 "VALUES ('SZ300607', '2026-08-17', 37.53, 37.53, 37.53, 37.53, 100, 5.0)")
    conn.commit()
    conn.close()
    return path


class _FakeSession:
    def close(self):
        pass


def _clean_kline():
    return [
        {"date": "2026-08-17", "open": 37.53, "close": 37.53, "high": 37.53,
         "low": 37.53, "volume": 50, "percent": 5.0, "timestamp": 1},
        {"date": "2026-08-18", "open": 37.21, "close": 37.90, "high": 37.90,
         "low": 36.01, "volume": 55, "percent": 0.99, "timestamp": 1},
    ]


def test_dirty_close_row_treated_as_repair_target(tmp_db, monkeypatch, capsys):
    """2026-08-19 修复回归：脏行 close 字符串/None 不再崩溃——清洗后无效值视为
    必须修复（权威值覆盖），dry-run 统计不抛异常且正确计数。"""
    monkeypatch.setattr(repair_kline, "DB_PATH", str(tmp_db))
    monkeypatch.setattr(repair_kline, "make_session", lambda: _FakeSession())
    monkeypatch.setattr(repair_kline, "fetch_kline",
                        lambda session, symbol, days=15: _clean_kline())
    monkeypatch.setattr(sys, "argv", ["repair_kline.py", "--dry-run", "--limit", "1"])

    repair_kline.main()  # 修复前：此处抛 TypeError；修复后：正常统计

    out = capsys.readouterr().out
    assert "差异行: 1" in out  # 08-18 脏行（str '36.27' vs 权威 37.90）识别为必须修复
    assert "SZ300607" in out


def test_null_close_row_no_crash(tmp_db, monkeypatch, capsys):
    """NULL close 脏行同样不崩溃（None 进 abs() 前被 to_float 清洗）。"""
    conn = sqlite3.connect(tmp_db)
    conn.execute("INSERT INTO daily_kline (symbol, date, open, close, high, low, volume, percent) "
                 "VALUES ('SZ300608', '2026-08-18', 10.0, NULL, 10.0, 10.0, 10, 0.0)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(repair_kline, "DB_PATH", str(tmp_db))
    monkeypatch.setattr(repair_kline, "make_session", lambda: _FakeSession())
    monkeypatch.setattr(repair_kline, "fetch_kline",
                        lambda session, symbol, days=15: _clean_kline())
    monkeypatch.setattr(sys, "argv", ["repair_kline.py", "--dry-run", "--limit", "2"])

    repair_kline.main()

    out = capsys.readouterr().out
    assert "差异行: 2" in out  # SZ300607 08-18 脏行 + SZ300608 08-18 NULL 行
