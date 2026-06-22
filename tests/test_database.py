import pytest
import sqlite3
import json
from datetime import date, timedelta
from scanner.database import (
    init_db, get_recent_symbols, record_appearances,
    get_symbol_appearances, save_kline_to_db, get_cached_kline,
    save_recommendations, get_active_weights, _n_trading_days_ago,
)
from scanner.models import StockInfo, Candidate, KlineSummary
from scanner.trading_session import is_trading_day


@pytest.fixture
def memory_db():
    conn = sqlite3.connect(":memory:")
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
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rec_date ON recommendations(date)")
    conn.commit()
    return conn


class TestRecordAppearances:
    def test_record_and_retrieve(self, memory_db):
        symbols = [
            {"symbol": "300001", "name": "Test1", "percent": 5.0, "value": 10000},
            {"symbol": "300002", "name": "Test2", "percent": 3.0, "value": 5000},
        ]
        record_appearances(memory_db, symbols)
        row = memory_db.execute(
            "SELECT COUNT(*) FROM appearances"
        ).fetchone()[0]
        assert row == 2

    def test_upsert_updates_percent(self, memory_db):
        symbols = [{"symbol": "300001", "name": "Test1", "percent": 5.0, "value": 10000}]
        record_appearances(memory_db, symbols)
        symbols2 = [{"symbol": "300001", "name": "Test1", "percent": 8.0, "value": 10000}]
        record_appearances(memory_db, symbols2)
        row = memory_db.execute(
            "SELECT percent FROM appearances WHERE symbol = ?", ("300001",)
        ).fetchone()
        assert row is not None
        assert row[0] == 8.0

    def test_get_symbol_appearances_no_data(self, memory_db):
        app = get_symbol_appearances(memory_db, "300999", 3)
        assert app == []

    def test_n_trading_days_ago_returns_trading_day(self, memory_db):
        result = _n_trading_days_ago(3)
        result_date = date.fromisoformat(result)
        assert is_trading_day(result_date), f"{result} should be a trading day"

    def test_symbol_appearances_holiday_gap(self, memory_db):
        today = date.today()
        if not is_trading_day(today):
            return
        cursor = date.today() - timedelta(days=1)
        non_trading_count = 0
        while cursor > date.today() - timedelta(days=10):
            if not is_trading_day(cursor):
                non_trading_count += 1
            cursor -= timedelta(days=1)
        if non_trading_count == 0:
            return
        holiday_date = None
        cursor = date.today() - timedelta(days=1)
        while cursor > date.today() - timedelta(days=10):
            if not is_trading_day(cursor):
                holiday_date = cursor
                break
            cursor -= timedelta(days=1)
        if holiday_date is None:
            return
        recent_trading = holiday_date - timedelta(days=1)
        while recent_trading > date.today() - timedelta(days=10) and not is_trading_day(recent_trading):
            recent_trading -= timedelta(days=1)
        if not is_trading_day(recent_trading):
            return
        memory_db.execute(
            "INSERT INTO appearances (symbol, name, date, rank, percent, value) VALUES (?, ?, ?, ?, ?, ?)",
            ("300001", "Test", recent_trading.isoformat(), 1, 5.0, 10000),
        )
        memory_db.commit()
        app = get_symbol_appearances(memory_db, "300001", 3)
        assert len(app) == 1, f"Should find appearance on {recent_trading} despite holiday gap (3 trading day lookback)"


class TestSaveKline:
    def test_save_and_get_cached(self, memory_db):
        kline = [
            {"date": "2026-06-17", "open": 100, "close": 102, "high": 103,
             "low": 99, "volume": 1_000_000, "percent": 2.0, "timestamp": 1},
            {"date": "2026-06-18", "open": 102, "close": 105, "high": 106,
             "low": 101, "volume": 1_200_000, "percent": 2.9, "timestamp": 2},
        ]
        save_kline_to_db(memory_db, "300001", kline)
        cached = get_cached_kline(memory_db, "300001")
        assert cached is not None
        assert len(cached) == 2
        assert cached[0]["date"] == "2026-06-17"

    def test_get_cached_nonexistent(self, memory_db):
        cached = get_cached_kline(memory_db, "999999")
        assert cached is None


class TestSaveRecommendations:
    def test_save_and_deduplicate(self, memory_db):
        stock = StockInfo(symbol="300001", name="Test", code="300001",
                          percent=5.0, current=10.0, value=10000,
                          rank_change=1000, rank=1)
        kline_summary = KlineSummary(trend="底部启动", accumulated_pct=2.0,
                                      volume_ratio=1.5, bottom_confirmed=True,
                                      score=20, dimensions={"new_face_today_pct": 20},
                                      avg_volume=1_000_000)
        candidate = Candidate(stock=stock, category="new_face", score=20,
                              reason="底部启动", kline=kline_summary,
                              first_seen="09:30")

        save_recommendations(memory_db, [candidate], [])
        save_recommendations(memory_db, [candidate], [])

        count = memory_db.execute(
            "SELECT COUNT(*) FROM recommendations"
        ).fetchone()[0]
        assert count == 1


class TestGetActiveWeights:
    def test_no_active_snapshot(self, memory_db):
        weights = get_active_weights(memory_db)
        assert weights == {}

    def test_active_snapshot_returns_weights(self, memory_db):
        params = json.dumps({"weights": {"new_face_today_pct": 25}})
        memory_db.execute(
            "INSERT INTO parameter_snapshots (version, params_json, created_at, active) "
            "VALUES (?, ?, ?, 1)",
            ("v1", params, "2026-06-18"),
        )
        memory_db.commit()
        weights = get_active_weights(memory_db)
        assert weights == {"new_face_today_pct": 25}
