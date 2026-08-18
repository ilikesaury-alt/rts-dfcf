"""数据真实性前置检查回归测试（2026-08-18 拓斯达脏数据事故）。

覆盖：
  - check_kline_health 交叉验证：不符检测 / 阻断阈值 / 源不可达 fail-open
  - health_banner 渲染：阻断 / 低比例警告 / 无异常空串
  - count_unfinalized_today：finalized=0 计数 / 旧库无列容错
  - save_kline_to_db 的 finalized 标记（盘中今日=0，收盘后/历史=1）
"""
import sqlite3
from datetime import datetime, timezone, timedelta

import pytest

from scanner import data_health as dh
from scanner.database import get_cached_kline, save_kline_to_db

BEIJING = timezone(timedelta(hours=8))


def _now(h: int, m: int = 0) -> datetime:
    return datetime(2026, 8, 18, h, m, tzinfo=BEIJING)


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.execute("""CREATE TABLE daily_kline (
        symbol TEXT NOT NULL, timestamp INTEGER, date TEXT NOT NULL, open REAL, close REAL,
        high REAL, low REAL, volume REAL, percent REAL, finalized INTEGER DEFAULT 1,
        PRIMARY KEY(symbol, date))""")
    yield c
    c.close()


def _seed(conn, rows):
    conn.executemany(
        "INSERT INTO daily_kline (symbol, date, close, percent, finalized) VALUES (?,?,?,?,?)",
        rows,
    )
    conn.commit()


class TestCheckKlineHealth:
    def test_no_mismatch(self, conn, monkeypatch):
        _seed(conn, [("SZ300607", "2026-08-18", 37.90, 0.99, 1),
                     ("SZ300012", "2026-08-18", 15.22, 0.0, 1)])
        monkeypatch.setattr(dh, "_sina_close", lambda sym, d: 37.90 if sym == "SZ300607" else 15.22)
        r = dh.check_kline_health(conn, dates=["2026-08-18"], sample_n=10)
        assert r.checked == 2 and r.mismatched == 0 and r.source_ok
        assert not r.blocked

    def test_mismatch_blocked(self, conn, monkeypatch):
        """不符比例 ≥30% 且样本足够 → blocked（数据疑似污染）。"""
        rows = []
        for i in range(10):
            rows.append((f"SZ3000{i:02d}", "2026-08-18", 10.0 + i, 0.0, 1))
        _seed(conn, rows)
        # 6/10 不符（60% ≥ 30%）
        monkeypatch.setattr(dh, "_sina_close",
                            lambda sym, d: 99.0 if int(sym[-2:]) < 6 else float(sym[-2:]) + 10)
        r = dh.check_kline_health(conn, dates=["2026-08-18"], sample_n=10)
        assert r.checked == 10 and r.mismatched == 6 and r.blocked
        assert len(r.samples) == 6

    def test_low_mismatch_not_blocked(self, conn, monkeypatch):
        _seed(conn, [("SZ300607", "2026-08-18", 37.90, 0.99, 1)] * 1)
        monkeypatch.setattr(dh, "_sina_close", lambda sym, d: 37.80)  # 差 0.1 元 = 不符
        r = dh.check_kline_health(conn, dates=["2026-08-18"], sample_n=10)
        assert r.checked == 1 and r.mismatched == 1
        assert not r.blocked  # 样本 < MIN_CHECKED，只警告不阻断

    def test_source_unavailable_fail_open(self, conn, monkeypatch):
        _seed(conn, [("SZ300607", "2026-08-18", 37.90, 0.99, 1)])
        monkeypatch.setattr(dh, "_sina_close", lambda sym, d: None)  # 源不可达
        r = dh.check_kline_health(conn, dates=["2026-08-18"])
        assert r.checked == 0 and not r.source_ok and not r.blocked

    def test_empty_db(self, conn, monkeypatch):
        r = dh.check_kline_health(conn, dates=["2026-08-18"])
        assert r.checked == 0 and not r.blocked


class TestHealthBanner:
    def test_ok_empty(self):
        assert dh.health_banner(dh.HealthReport(checked=5, mismatched=0)) == ""

    def test_blocked_banner(self):
        r = dh.HealthReport(checked=10, mismatched=7,
                            samples=[("SZ300607", "2026-08-18", 36.27, 37.90)])
        b = dh.health_banner(r)
        assert "疑似污染" in b and "repair_kline" in b and "36.27" in b

    def test_low_ratio_warning(self):
        r = dh.HealthReport(checked=10, mismatched=1)
        b = dh.health_banner(r)
        assert "低于阈值" in b

    def test_source_unavailable_banner(self):
        b = dh.health_banner(dh.HealthReport(checked=0, source_ok=False))
        assert "不可达" in b


class TestCountUnfinalizedToday:
    def test_count(self, conn):
        _seed(conn, [("SZ300001", "2026-08-18", 10.0, 0.0, 0),
                     ("SZ300002", "2026-08-18", 11.0, 0.0, 1),
                     ("SZ300003", "2026-08-18", 12.0, 0.0, 0)])
        assert dh.count_unfinalized_today(conn, "2026-08-18") == 2

    def test_missing_column_graceful(self):
        """旧库无 finalized 列 → 返回 0 不崩溃。"""
        c = sqlite3.connect(":memory:")
        c.execute("""CREATE TABLE daily_kline (
            symbol TEXT NOT NULL, date TEXT NOT NULL, close REAL,
            PRIMARY KEY(symbol, date))""")
        assert dh.count_unfinalized_today(c, "2026-08-18") == 0
        c.close()


class TestSaveKlineFinalizedMark:
    def _bar(self, d: str, close: float) -> dict:
        return {"date": d, "open": close, "close": close, "high": close,
                "low": close, "volume": 1, "percent": 0.0, "timestamp": 1}

    def test_trading_hours_today_bar_marked_unfinalized(self, conn, monkeypatch):
        """盘中写入今日 bar → finalized=0（未定稿快照）。"""
        monkeypatch.setattr("scanner.database.now_beijing", lambda: _now(10, 30))
        monkeypatch.setattr("scanner.database.is_trading_time", lambda: True)
        save_kline_to_db(conn, "SZ300607", [self._bar("2026-08-18", 36.27)])
        bars = get_cached_kline(conn, "SZ300607")
        assert bars and bars[-1]["finalized"] is False

    def test_after_close_today_bar_finalized(self, conn, monkeypatch):
        """收盘后（定稿/backfill）写入今日 bar → finalized=1。"""
        monkeypatch.setattr("scanner.database.now_beijing", lambda: _now(18, 0))
        monkeypatch.setattr("scanner.database.is_trading_time", lambda: False)
        save_kline_to_db(conn, "SZ300607", [self._bar("2026-08-18", 37.90)])
        bars = get_cached_kline(conn, "SZ300607")
        assert bars and bars[-1]["finalized"] is True

    def test_historical_bar_always_finalized(self, conn, monkeypatch):
        """历史 bar（非今日）盘中写入也置 finalized=1。"""
        monkeypatch.setattr("scanner.database.now_beijing", lambda: _now(10, 30))
        monkeypatch.setattr("scanner.database.is_trading_time", lambda: True)
        save_kline_to_db(conn, "SZ300607", [self._bar("2026-08-17", 37.53)])
        bars = get_cached_kline(conn, "SZ300607")
        assert bars and bars[-1]["finalized"] is True
