import sqlite3
from datetime import date, timedelta

import pytest

from scanner.database import (
    _n_trading_days_ago,
    get_cached_kline,
    get_symbol_appearances,
    get_today_recommendations,
    record_appearances,
    save_kline_to_db,
    save_recommendations,
)
from scanner.models import Candidate, KlineSummary, StockInfo
from scanner.config import now_beijing
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
            score_breakdown TEXT,
            source TEXT DEFAULT 'xueqiu',
            concept TEXT,
            accumulated_pct REAL,
            excluded INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS watch_pool (
            symbol TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            added_date TEXT NOT NULL,
            last_list_date TEXT NOT NULL,
            last_eval_date TEXT,
            over_limit INTEGER DEFAULT 0
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
        """Holiday gap should not prevent finding records within N-trading-day lookback."""
        today = now_beijing().date()
        if not is_trading_day(today):
            pytest.skip("Not a trading day")
        cursor = today - timedelta(days=1)
        non_trading_count = 0
        while cursor > today - timedelta(days=20):
            if not is_trading_day(cursor):
                non_trading_count += 1
            cursor -= timedelta(days=1)
        if non_trading_count == 0:
            pytest.skip("No holidays found in last 20 days")
        holiday_date = None
        cursor = today - timedelta(days=1)
        while cursor > today - timedelta(days=20):
            if not is_trading_day(cursor):
                holiday_date = cursor
                break
            cursor -= timedelta(days=1)
        if holiday_date is None:
            pytest.skip("Could not find holiday date")
        recent_trading = holiday_date - timedelta(days=1)
        while recent_trading > today - timedelta(days=20) and not is_trading_day(recent_trading):
            recent_trading -= timedelta(days=1)
        if not is_trading_day(recent_trading):
            pytest.skip("Could not find trading day before holiday")
        memory_db.execute(
            "INSERT INTO appearances (symbol, name, date, rank, percent, value) VALUES (?, ?, ?, ?, ?, ?)",
            ("300001", "Test", recent_trading.isoformat(), 1, 5.0, 10000),
        )
        memory_db.commit()
        lookback = 3
        while lookback <= 10:
            app = get_symbol_appearances(memory_db, "300001", lookback)
            if len(app) == 1:
                break
            lookback += 1
        assert len(app) == 1, (
            f"Should find appearance on {recent_trading} within reasonable lookback, "
            f"needed {lookback} days"
        )


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

    def test_get_cached_filters_bad_close_bars(self, memory_db):
        """回归：历史脏数据 close 为 None/0 的行必须被剔除，不能漏进下游
        closes 算术（analyze_* 减法抛 TypeError）。正常 bar 保留。"""
        kline = [
            {"date": "2026-06-17", "open": 100, "close": 102, "high": 103,
             "low": 99, "volume": 1_000_000, "percent": 2.0, "timestamp": 1},
            {"date": "2026-06-18", "open": 102, "close": None, "high": 106,
             "low": 101, "volume": 1_200_000, "percent": 2.9, "timestamp": 2},
            {"date": "2026-06-19", "open": 106, "close": 0, "high": 107,
             "low": 104, "volume": 1_100_000, "percent": 1.0, "timestamp": 3},
            {"date": "2026-06-22", "open": 107, "close": 110, "high": 111,
             "low": 106, "volume": 1_300_000, "percent": 3.0, "timestamp": 4},
        ]
        save_kline_to_db(memory_db, "300001", kline)
        cached = get_cached_kline(memory_db, "300001")
        assert cached is not None
        dates = [k["date"] for k in cached]
        assert dates == ["2026-06-17", "2026-06-22"]
        assert all(k["close"] > 0 for k in cached)

    def test_get_cached_all_bad_returns_none(self, memory_db):
        """全部 bar 均为脏数据时返回 None（与无数据语义一致，不返回空列表）。"""
        kline = [
            {"date": "2026-06-18", "open": 102, "close": None, "high": 106,
             "low": 101, "volume": 1_200_000, "percent": 2.9, "timestamp": 2},
        ]
        save_kline_to_db(memory_db, "300001", kline)
        cached = get_cached_kline(memory_db, "300001")
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

    def test_save_persists_driving_concept(self, memory_db):
        stock = StockInfo(symbol="300001", name="Test", code="300001",
                          percent=5.0, current=10.0, value=10000,
                          rank_change=1000, rank=1)
        kline_summary = KlineSummary(trend="底部启动", accumulated_pct=2.0,
                                      volume_ratio=1.5, bottom_confirmed=True,
                                      score=20, dimensions={"new_face_today_pct": 20},
                                      avg_volume=1_000_000)
        candidate = Candidate(stock=stock, category="new_face", score=20,
                              reason="底部启动", kline=kline_summary,
                              first_seen="09:30", driving_concept="华为概念")

        save_recommendations(memory_db, [candidate], [])

        row = memory_db.execute(
            "SELECT concept FROM recommendations WHERE symbol = '300001'"
        ).fetchone()
        assert row is not None
        assert row[0] == "华为概念"


class TestTodayRecommendationsExcluded:
    """P1-7 (2026-08-10): 当日被硬过滤（excluded=1）的推荐不再出现在综合排序。"""

    def test_excluded_flag_filtered_out(self, memory_db):
        today = now_beijing().date().isoformat()
        rows = [
            (today, "300001", "通过", "momentum", 60, 0),
            (today, "300002", "被过滤", "momentum", 80, 1),
        ]
        memory_db.executemany(
            "INSERT INTO recommendations (date, time, symbol, name, category, score, percent, excluded) "
            "VALUES (?, '13:00', ?, ?, ?, ?, 2.0, ?)",
            rows,
        )
        memory_db.commit()
        recs = get_today_recommendations(memory_db)
        syms = {r["symbol"] for r in recs}
        assert "300001" in syms
        assert "300002" not in syms, "excluded=1 的硬过滤票不应出现在综合排序"


class TestWatchPoolEviction:
    """WATCH_POOL_MAX 容量上限：超限时淘汰 last_list_date 最旧条目。"""

    def _seed(self, memory_db, symbols_dates):
        # symbols_dates: [(symbol, last_list_date), ...]
        today = now_beijing().date().isoformat()
        for sym, lst in symbols_dates:
            memory_db.execute(
                "INSERT INTO watch_pool (symbol, name, added_date, last_list_date, over_limit) "
                "VALUES (?, ?, ?, ?, 0)",
                (sym, "T", today, lst),
            )
        memory_db.commit()

    def test_evicts_oldest_beyond_max(self, memory_db, monkeypatch):
        from scanner.database import upsert_watch_symbols
        monkeypatch.setattr("scanner.database.WATCH_POOL_MAX", 3)
        today = now_beijing().date().isoformat()
        self._seed(memory_db, [
            ("300001", "2026-01-01"),  # 最旧
            ("300002", "2026-01-02"),
            ("300003", "2026-01-03"),
        ])
        # 加入第 4 条 → 池超 3 条 → 淘汰最旧的 300001
        upsert_watch_symbols(memory_db, [{"symbol": "300004", "name": "T",
                                          "last_list_date": today}])
        remaining = {r[0] for r in memory_db.execute(
            "SELECT symbol FROM watch_pool").fetchall()}
        assert "300001" not in remaining, "超限应淘汰 last_list_date 最旧"
        assert remaining == {"300002", "300003", "300004"}

    def test_eviction_keeps_newest_when_upserting(self, memory_db, monkeypatch):
        from scanner.database import upsert_watch_symbols
        monkeypatch.setattr("scanner.database.WATCH_POOL_MAX", 2)
        today = now_beijing().date().isoformat()
        self._seed(memory_db, [
            ("300001", "2026-01-01"),
            ("300002", "2026-01-02"),
        ])
        upsert_watch_symbols(memory_db, [{"symbol": "300003", "name": "T",
                                          "last_list_date": today}])
        remaining = {r[0] for r in memory_db.execute(
            "SELECT symbol FROM watch_pool").fetchall()}
        assert remaining == {"300002", "300003"}

    def test_no_eviction_within_limit(self, memory_db, monkeypatch):
        from scanner.database import upsert_watch_symbols
        monkeypatch.setattr("scanner.database.WATCH_POOL_MAX", 10)
        self._seed(memory_db, [
            ("300001", "2026-01-01"),
            ("300002", "2026-01-02"),
        ])
        upsert_watch_symbols(memory_db, [{"symbol": "300003", "name": "T"}])
        remaining = {r[0] for r in memory_db.execute(
            "SELECT symbol FROM watch_pool").fetchall()}
        assert remaining == {"300001", "300002", "300003"}


class TestProminenceWindow:
    """回归：辨识度排名窗口与计数窗口必须一致（原先差 1 天）。"""

    def _seed_appearances(self, memory_db, symbol, days_rank):
        # days_rank: [(date_str, rank), ...]
        for d, r in days_rank:
            memory_db.execute(
                "INSERT INTO appearances (symbol, name, date, rank, percent, value) "
                "VALUES (?, 'T', ?, ?, 0, 0)",
                (symbol, d, r),
            )
        memory_db.commit()

    def test_prominence_rank_window_matches_count_window(self, memory_db):
        from scanner.config import PROMINENCE_LOOKBACK_DAYS, PROMINENCE_REPEAT_THRESHOLD
        from scanner.database import get_prominence_map
        # 构造：计数窗口内恰好重复阈值天，排名窗口若多算一天（旧 bug）会把一天
        # 极差排名的历史日拉低平均，导致误判。这里验证两个窗口取同一 lookback。
        today = now_beijing().date()
        # 计算 lookback 日期（与实现同口径）
        lookback = _n_trading_days_ago(PROMINENCE_LOOKBACK_DAYS - 1)
        # 在 [lookback, today] 内放 PROMINENCE_REPEAT_THRESHOLD 天的记录，
        # 全部 rank=50（优良），且其中有一天恰好是 lookback 前一天（应被排除在外）
        # 用较差排名 999 验证"排名窗口不比计数窗口多一天"。
        import datetime
        one_before = (datetime.date.fromisoformat(lookback)
                      - timedelta(days=1)).isoformat()
        memory_db.execute(
            "INSERT INTO appearances (symbol, name, date, rank, percent, value) "
            "VALUES ('300111', 'T', ?, 999, 0, 0)", (one_before,))
        # 窗口内一天：凑满重复阈值（其余用 today 同日期覆盖会去重，改为多天）
        day_dates = []
        cursor = datetime.date.fromisoformat(lookback)
        while len(day_dates) < PROMINENCE_REPEAT_THRESHOLD:
            if is_trading_day(cursor):
                day_dates.append(cursor.isoformat())
            cursor += timedelta(days=1)
        for d in day_dates[:PROMINENCE_REPEAT_THRESHOLD]:
            memory_db.execute(
                "INSERT INTO appearances (symbol, name, date, rank, percent, value) "
                "VALUES ('300111', 'T', ?, 50, 0, 0)", (d,))
        memory_db.commit()
        result = get_prominence_map(memory_db, ["300111"])
        # 若排名窗口错误多算 one_before（rank=999），平均会被拉低；正确实现则忽略它。
        assert result.get("300111") is True, (
            f"窗口外 bad-rank 日期不得计入排名平均，got {result}")
