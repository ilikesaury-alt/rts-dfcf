import sqlite3
from datetime import date, timedelta

import pytest

from scanner.config import now_beijing
from scanner.database import (
    _n_trading_days_ago,
    get_cached_kline,
    get_consecutive_appearance_days,
    get_market_index_log,
    get_symbol_appearances,
    get_today_recommendations,
    mark_reversed_recommendations,
    record_appearances,
    save_kline_to_db,
    save_market_index_log,
    save_recommendations,
    save_scan_quality,
)
from scanner.models import Candidate, KlineSummary, StockInfo
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
            finalized INTEGER DEFAULT 1,
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
            excluded INTEGER DEFAULT 0,
            stale_kline INTEGER DEFAULT 0,
            excluded_reason TEXT
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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scan_quality_log (
            date TEXT PRIMARY KEY,
            time TEXT,
            gem_count INTEGER DEFAULT 0,
            fetch_failed INTEGER DEFAULT 0,
            today_bar_missing INTEGER DEFAULT 0,
            minute_fallback INTEGER DEFAULT 0,
            stale_recs INTEGER DEFAULT 0,
            updated TEXT DEFAULT ''
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS leaderboard_log (
            date TEXT,
            time TEXT,
            source TEXT,
            total INTEGER DEFAULT 0,
            gem_listed INTEGER DEFAULT 0,
            up_count INTEGER DEFAULT 0,
            down_count INTEGER DEFAULT 0,
            flat_count INTEGER DEFAULT 0,
            median_pct REAL,
            mean_pct REAL,
            top10_mean_pct REAL,
            max_pct REAL,
            overlap_prev REAL,
            median_rank_change REAL,
            symbol_snapshot TEXT,
            updated TEXT DEFAULT '',
            PRIMARY KEY (date, time, source)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS market_index_log (
            date TEXT PRIMARY KEY,
            time TEXT,
            index_pct REAL,
            bar_date TEXT,
            source TEXT,
            updated TEXT DEFAULT ''
        )
    """)
    conn.commit()
    return conn


class TestRecordLeaderboardLog:
    def _items(self, syms):
        out = []
        for i, s in enumerate(syms, 1):
            out.append({'symbol': s, 'name': f'n{i}', 'code': s[2:],
                        'percent': float(i), 'rank': i, 'rank_change': i * 5})
        return out

    def test_basic_stats(self, memory_db):
        from scanner.database import record_leaderboard_log
        items = self._items(['SZ300607', 'SZ300438', 'SH600000'])
        syms = record_leaderboard_log(memory_db, 'biaosheng', items, set())
        assert syms == {'SZ300607', 'SZ300438', 'SH600000'}
        row = memory_db.execute("SELECT * FROM leaderboard_log").fetchone()
        assert row[3] == 3          # total
        assert row[4] == 2          # gem_listed
        assert row[5] == 3          # up_count (全部正涨幅)
        assert row[6] == 0          # down_count
        assert row[12] == 0.0       # 首轮 overlap
        assert row[9] == 2.0        # mean_pct

    def test_median_and_dirty_guard(self, memory_db):
        from scanner.database import record_leaderboard_log
        items = [
            {'symbol': 'SZ300001', 'name': 'a', 'code': '300001', 'percent': 5.0, 'rank': 1, 'rank_change': 10},
            {'symbol': 'SZ300002', 'name': 'b', 'code': '300002', 'percent': -2.2, 'rank': 2, 'rank_change': '-'},
            {'symbol': 'SZ300003', 'name': 'c', 'code': '300003', 'percent': 3.1, 'rank': 3, 'rank_change': None},
            {'symbol': 'SZ300004', 'name': 'd', 'code': '300004', 'percent': 'bad', 'rank': 4, 'rank_change': 7},
        ]
        record_leaderboard_log(memory_db, 'biaosheng', items, set())
        row = memory_db.execute("SELECT * FROM leaderboard_log").fetchone()
        # percent 有效值 [5.0, -2.2, 3.1] → 中位数 3.1、涨2跌1
        assert row[8] == 3.1        # median_pct
        assert row[5] == 2          # up
        assert row[6] == 1          # down
        # rank_change 有效值 [10, 7] → 中位数 8.5（脏值 '-'/None 被过滤）
        assert abs(row[13] - 8.5) < 1e-6

    def test_overlap_second_round(self, memory_db, monkeypatch):
        import datetime

        from scanner import database as db_mod
        from scanner.database import record_leaderboard_log
        clock = [datetime.datetime(2026, 8, 19, 10, 0, 0)]
        monkeypatch.setattr(db_mod, 'now_beijing', lambda: clock[0])

        items = self._items(['SZ300607', 'SZ300438', 'SH600000'])
        syms = record_leaderboard_log(memory_db, 'biaosheng', items, set())
        clock[0] = clock[0] + datetime.timedelta(seconds=60)  # 下一轮不同秒
        syms2 = record_leaderboard_log(memory_db, 'biaosheng', items[:2], syms)
        assert syms2 == {'SZ300607', 'SZ300438'}
        rows = memory_db.execute("SELECT overlap_prev FROM leaderboard_log ORDER BY time").fetchall()
        assert rows[0][0] == 0.0
        assert rows[1][0] == 1.0

    def test_fail_open_returns_prev(self, memory_db):
        from scanner.database import record_leaderboard_log
        memory_db.execute("DROP TABLE leaderboard_log")
        # 表不存在 → 函数不抛，返回 prev_symbols（fail-open，不污染扫描主流程）
        assert record_leaderboard_log(memory_db, 'biaosheng', self._items(['SZ300001']), {'SZ300001'}) == {'SZ300001'}


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

    def test_consecutive_appearance_days_dirty_date_no_crash(self, memory_db):
        """回归：脏日期（非 ISO）存在时 get_consecutive_appearance_days 不抛 ValueError。

        该函数被 enhancer._apply_list_momentum_bonus 批量查询驱动，脏数据若不兜底
        会让整轮扫描崩溃（此前 date.fromisoformat("2026-08-01bad") 直接炸）。
        用近期日期保证落在批量查询的日历窗口内（批量口径含 date>=cutoff 下界）。
        """
        dirty = (now_beijing().date() - timedelta(days=5)).isoformat() + "bad"
        valid = (now_beijing().date() - timedelta(days=5)).isoformat()
        memory_db.execute(
            "INSERT INTO appearances (symbol, name, date, rank, percent, value) VALUES (?,?,?,?,?,?)",
            ("SZ300001", "Test", dirty, 5, 1.0, 100.0),
        )
        memory_db.execute(
            "INSERT INTO appearances (symbol, name, date, rank, percent, value) VALUES (?,?,?,?,?,?)",
            ("SZ300001", "Test", valid, 5, 1.0, 100.0),
        )
        memory_db.commit()
        # 脏日期应打断连续计数而非崩溃（sorted 后脏串在前，遍历到即 break）
        n = get_consecutive_appearance_days(memory_db, "SZ300001")
        assert isinstance(n, int)
        assert n >= 1

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
        # 日期用相对今天（近 60 天窗口内）：get_cached_klines 有 `date >= now-60d`
        # 滚动过滤，硬编码旧日期会随窗口前移而失效（2026-08-17 实测 06-17 被滤掉）。
        today = now_beijing().date()
        d1, d2 = (today - timedelta(days=2)).isoformat(), (today - timedelta(days=1)).isoformat()
        kline = [
            {"date": d1, "open": 100, "close": 102, "high": 103,
             "low": 99, "volume": 1_000_000, "percent": 2.0, "timestamp": 1},
            {"date": d2, "open": 102, "close": 105, "high": 106,
             "low": 101, "volume": 1_200_000, "percent": 2.9, "timestamp": 2},
        ]
        save_kline_to_db(memory_db, "300001", kline)
        cached = get_cached_kline(memory_db, "300001")
        assert cached is not None
        assert len(cached) == 2
        assert cached[0]["date"] == d1

    def test_get_cached_nonexistent(self, memory_db):
        cached = get_cached_kline(memory_db, "999999")
        assert cached is None

    def test_get_cached_filters_bad_close_bars(self, memory_db):
        """回归：历史脏数据 close 为 None/0 的行必须被剔除，不能漏进下游
        closes 算术（analyze_* 减法抛 TypeError）。正常 bar 保留。"""
        today = now_beijing().date()
        d1 = (today - timedelta(days=3)).isoformat()
        d2 = (today - timedelta(days=2)).isoformat()  # close=None → 剔除
        d3 = (today - timedelta(days=1)).isoformat()  # close=0 → 剔除
        d4 = today.isoformat()
        kline = [
            {"date": d1, "open": 100, "close": 102, "high": 103,
             "low": 99, "volume": 1_000_000, "percent": 2.0, "timestamp": 1},
            {"date": d2, "open": 102, "close": None, "high": 106,
             "low": 101, "volume": 1_200_000, "percent": 2.9, "timestamp": 2},
            {"date": d3, "open": 106, "close": 0, "high": 107,
             "low": 104, "volume": 1_100_000, "percent": 1.0, "timestamp": 3},
            {"date": d4, "open": 107, "close": 110, "high": 111,
             "low": 106, "volume": 1_300_000, "percent": 3.0, "timestamp": 4},
        ]
        save_kline_to_db(memory_db, "300001", kline)
        cached = get_cached_kline(memory_db, "300001")
        assert cached is not None
        dates = [k["date"] for k in cached]
        assert dates == [d1, d4]
        assert all(k["close"] > 0 for k in cached)

    def test_get_cached_all_bad_returns_none(self, memory_db):
        """全部 bar 均为脏数据时返回 None（与无数据语义一致，不返回空列表）。"""
        today = now_beijing().date()
        d = (today - timedelta(days=1)).isoformat()
        kline = [
            {"date": d, "open": 102, "close": None, "high": 106,
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

    def test_save_persists_stale_kline_flag(self, memory_db):
        """Layer2 审计（2026-08-14）：缺今日 bar 旧缓存评分的候选落库时打 stale_kline=1，
        供事后审计"该推荐基于什么数据评分"（网宿类 bug 的隐蔽点：静默降级无感知）。"""
        stock = StockInfo(symbol="300002", name="Test", code="300002",
                          percent=5.0, current=10.0, value=10000,
                          rank_change=1000, rank=1)
        kline_summary = KlineSummary(trend="底部启动", accumulated_pct=2.0,
                                      volume_ratio=0.9, bottom_confirmed=True,
                                      score=20, dimensions={}, avg_volume=1_000_000)
        fresh = Candidate(stock=stock, category="new_face", score=20,
                          reason="底部启动", kline=kline_summary,
                          first_seen="09:30", stale_kline=False)
        stale = Candidate(stock=stock, category="new_face", score=20,
                          reason="底部启动", kline=kline_summary,
                          first_seen="09:30", stale_kline=True)

        save_recommendations(memory_db, [fresh], [])
        save_recommendations(memory_db, [stale], [])  # 同分不覆盖，但新分更高才更新

        rows = memory_db.execute(
            "SELECT stale_kline FROM recommendations WHERE symbol = '300002' ORDER BY id"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == 0  # 首条（fresh）保留

        # 用更高分触发覆盖，确认 stale_kline 随更新写入
        stale_hi = Candidate(stock=stock, category="new_face", score=30,
                             reason="底部启动", kline=kline_summary,
                             first_seen="09:30", stale_kline=True)
        save_recommendations(memory_db, [stale_hi], [])
        rows = memory_db.execute(
            "SELECT score, stale_kline FROM recommendations WHERE symbol = '300002'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == 30
        assert rows[0][1] == 1  # 更新时写入 stale_kline=1


class TestSaveScanQuality:
    """数据血缘日志（2026-08-14）：每轮扫描的数据质量快照落库。

    跨函数静默降级（补拉失败→旧缓存、缺今日bar→昨日量）是本项目最难发现的
    bug 类别，单函数审查看不出。此日志把降级规模变成可查询的常态计数器：
    某日 fetch_failed/today_bar_missing 异常升高 + 推荐数骤降 → 关联即定位。
    """

    def test_save_and_overwrite_same_day(self, memory_db):
        save_scan_quality(memory_db, {
            "gem_count": 77, "fetch_failed": 3, "today_bar_missing": 5,
            "minute_fallback": 2, "stale_recs": 1,
        })
        save_scan_quality(memory_db, {
            "gem_count": 79, "fetch_failed": 1, "today_bar_missing": 2,
            "minute_fallback": 0, "stale_recs": 0,
        })
        rows = memory_db.execute("SELECT * FROM scan_quality_log").fetchall()
        assert len(rows) == 1  # 同日覆盖，只留最新快照
        today = now_beijing().date().isoformat()
        assert rows[0][0] == today
        assert rows[0][2] == 79   # gem_count
        assert rows[0][3] == 1    # fetch_failed
        assert rows[0][4] == 2    # today_bar_missing
        assert rows[0][5] == 0    # minute_fallback

    def test_missing_keys_default_zero(self, memory_db):
        save_scan_quality(memory_db, {"gem_count": 10})
        rows = memory_db.execute("SELECT * FROM scan_quality_log").fetchall()
        assert len(rows) == 1
        assert rows[0][3] == 0  # fetch_failed 缺省 → 0
        assert rows[0][4] == 0  # today_bar_missing 缺省 → 0


class TestSaveMarketIndexLog:
    """大盘指数血缘日志（2026-08-19）：每轮扫描使用的大盘涨幅 + bar 日期落库。

    大盘标签曾把当日 -6.26% 崩盘读成昨日 -0.93%（展示"大盘中性"）而无痕——涨幅是
    瞬时值、不进 daily_kline，不落库就无法审计"当时读到了什么"。bar 日期是「读到
    哪一天的数据」的权威证据，供 data_health 对账。
    """

    def test_save_and_overwrite_same_day(self, memory_db):
        save_market_index_log(memory_db, -0.93, "2026-08-18", "xueqiu")
        save_market_index_log(memory_db, -6.26, "2026-08-19", "xueqiu")
        rows = memory_db.execute("SELECT * FROM market_index_log").fetchall()
        assert len(rows) == 1  # 同日覆盖，只留最新快照
        today = now_beijing().date().isoformat()
        assert rows[0][0] == today
        assert rows[0][2] == pytest.approx(-6.26)
        assert rows[0][3] == "2026-08-19"

    def test_get_roundtrip(self, memory_db):
        save_market_index_log(memory_db, -6.26, "2026-08-19", "akshare")
        rec = get_market_index_log(memory_db)
        assert rec is not None
        assert rec["index_pct"] == pytest.approx(-6.26)
        assert rec["bar_date"] == "2026-08-19"
        assert rec["source"] == "akshare"

    def test_get_none_no_record(self, memory_db):
        assert get_market_index_log(memory_db) is None

    def test_get_old_library_no_table(self):
        conn = sqlite3.connect(":memory:")  # 无 market_index_log 表（未迁移）
        assert get_market_index_log(conn) is None


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

    def test_as_of_historical_date(self, memory_db):
        """2026-08-18 新增（配合 today_report.py 历史回放）：as_of 指定日期时按
        该日推荐/上榜快照查询，去重口径与今日一致；默认行为不受影响。
        2026-08-19 修复：原硬编码 2026-08-18 为「今日」，真实日期推移后默认查询
        落空——改用动态今日/昨日，任何日期运行均成立。"""
        today = now_beijing().date()
        yesterday = (today - timedelta(days=1)).isoformat()
        memory_db.executemany(
            "INSERT INTO recommendations (date, time, symbol, name, category, score, percent, excluded) "
            "VALUES (?, '13:00', ?, '测试', ?, ?, 2.0, ?)",
            [
                (yesterday, "300001", "momentum", 60, 0),
                (yesterday, "300002", "comeback", 90, 0),
                (today.isoformat(), "300003", "short_term", 70, 0),
            ],
        )
        memory_db.commit()
        recs_hist = get_today_recommendations(memory_db, as_of=yesterday)
        syms_hist = {r["symbol"] for r in recs_hist}
        assert syms_hist == {"300001", "300002"}, "as_of 应只返回该日记录"
        # 字符串与 date 对象均可
        recs_hist2 = get_today_recommendations(memory_db, as_of=date.fromisoformat(yesterday))
        assert {r["symbol"] for r in recs_hist2} == {"300001", "300002"}
        recs_today = get_today_recommendations(memory_db)
        assert {r["symbol"] for r in recs_today} == {"300003"}, "默认仍查今日"


class TestTodayRecommendationsComebackShadowing:
    """同票同日既有榜上类别（如 short_term）又有 comeback（掉榜跟踪）时，
    去重须保留榜上类别记录——否则 comeback 基线分更高（40+15×信号数）会遮蔽
    榜上推荐，且回马枪区在主区条数达标时整体隐藏，导致该票完全不可见。"""

    def _insert(self, memory_db, symbol, category, score, percent):
        today = now_beijing().date().isoformat()
        memory_db.execute(
            "INSERT INTO recommendations (date, time, symbol, name, category, score, percent, excluded) "
            "VALUES (?, '10:00', ?, '测试', ?, ?, ?, 0)",
            (today, symbol, category, score, percent),
        )
        memory_db.commit()

    def test_main_category_preferred_over_higher_score_comeback(self, memory_db):
        # comeback 104 分 > short_term 86 分：旧逻辑保留 comeback → 票从主表消失
        self._insert(memory_db, "301188", "comeback", 104, 1.23)
        self._insert(memory_db, "301188", "short_term", 86, 10.49)
        recs = get_today_recommendations(memory_db)
        matches = [r for r in recs if r["symbol"] == "301188"]
        assert len(matches) == 1, "同票去重后应只有一条"
        assert matches[0]["category"] == "short_term", "榜上类别应优先于 comeback 去重保留"

    def test_comeback_only_symbol_kept_as_comeback(self, memory_db):
        # 纯掉榜票（无榜上类别）仍以 comeback 保留，回马枪区逻辑不受影响
        self._insert(memory_db, "300383", "comeback", 104, 1.0)
        recs = get_today_recommendations(memory_db)
        matches = [r for r in recs if r["symbol"] == "300383"]
        assert len(matches) == 1
        assert matches[0]["category"] == "comeback"


class TestMarkReversedRecommendations:
    """2026-08-13 反转盲区：今日已推荐（榜上主类别）但当前不在候选池的票，回落幅度（优先按
    「当日最高涨幅 high_pct−现价」，缺失回退推荐时刻）命中任一条件即标 excluded=1 移出综合
    排序（保留落库记录）：
      ① 已转负且回落 ≥ REVERSAL_TURNED_RED_DROP（5.0，滤高位小幅回落就微幅翻绿噪音）；
      ② 回落 ≥ REVERSAL_OVERSHOOT_DROP（10.0），无论红绿——从最高点大回吐未转负也"不敢买"。
    回马枪跟踪池不参与；当前候选/行情缺失不受影响。"""

    def _insert(self, memory_db, symbol, score=80, percent=3.0, category="short_term"):
        today = now_beijing().date().isoformat()
        memory_db.execute(
            "INSERT INTO recommendations (date, time, symbol, name, category, score, percent, excluded) "
            "VALUES (?, '10:00', ?, '测试', ?, ?, ?, 0)",
            (today, symbol, category, score, percent),
        )
        memory_db.commit()

    def _recs(self, memory_db):
        return get_today_recommendations(memory_db)

    def test_reversed_non_candidate_excluded(self, memory_db):
        # 行云科技：最高 +12.33% → 现 -3.15%，从最高回落 15.48 ≥ 10（路②）
        self._insert(memory_db, "300209", percent=3.86)
        marked = mark_reversed_recommendations(
            memory_db, self._recs(memory_db), active_syms=set(),
            live_quotes={"300209": {"percent": -3.15, "high_pct": 12.33}})
        assert "300209" in marked
        assert "300209" not in {r["symbol"] for r in self._recs(memory_db)}, \
            "从最高点大幅回落的旧推荐应从综合排序消失"

    def test_turned_red_fallback_to_rec_pct(self, memory_db):
        # high_pct 缺失时回退推荐时刻涨幅：+3.86% 推荐 → -3.12%（转负 + 回落 6.98 ≥ 5，路①）
        self._insert(memory_db, "300209", percent=3.86)
        marked = mark_reversed_recommendations(
            memory_db, self._recs(memory_db), active_syms=set(),
            live_quotes={"300209": {"percent": -3.12}})
        assert "300209" in marked

    def test_big_overshoot_still_positive_excluded(self, memory_db):
        # 路②：从最高 +12% 回落到 +2%（回落 10 ≥ 10），未转负但动量已破 → 移出
        self._insert(memory_db, "300149", percent=8.0)
        marked = mark_reversed_recommendations(
            memory_db, self._recs(memory_db), active_syms=set(),
            live_quotes={"300149": {"percent": 2.0, "high_pct": 12.0}})
        assert "300149" in marked, "从最高点大幅回吐即使未转负也应移出"

    def test_normal_settle_not_excluded(self, memory_db):
        # 正常回吐：最高 +15% 现 +8%（回落 7 < 10，未转负）→ 保留
        self._insert(memory_db, "300149", percent=8.0)
        marked = mark_reversed_recommendations(
            memory_db, self._recs(memory_db), active_syms=set(),
            live_quotes={"300149": {"percent": 8.0, "high_pct": 15.0}})
        assert marked == [], "正常回吐且未转负不应移出"

    def test_turned_red_small_high_drop_not_excluded(self, memory_db):
        # 高位仅小幅回落就微幅翻绿：最高 +2% 现 -1%（回落 3 < 5）→ 噪音不移出
        self._insert(memory_db, "300209", percent=0.5)
        marked = mark_reversed_recommendations(
            memory_db, self._recs(memory_db), active_syms=set(),
            live_quotes={"300209": {"percent": -1.0, "high_pct": 2.0}})
        assert marked == [], "高位小幅回落的微幅翻绿不应移出"

    def test_comeback_not_excluded(self, memory_db):
        # 回马枪跟踪池：推荐时刻=企稳点，转负是常态，不参与自动移出
        self._insert(memory_db, "300383", percent=2.68, category="comeback")
        marked = mark_reversed_recommendations(
            memory_db, self._recs(memory_db), active_syms=set(),
            live_quotes={"300383": {"percent": -4.0, "high_pct": 12.0}})
        assert marked == [], "回马枪跟踪池不自动移出"
        assert "300383" in {r["symbol"] for r in self._recs(memory_db)}

    def test_custom_thresholds(self, memory_db):
        # 自定义：路①转负+回落≥6 → 最高+8 现-1（回落 9 ≥ 6）应移出
        self._insert(memory_db, "300209", percent=2.0)
        marked = mark_reversed_recommendations(
            memory_db, self._recs(memory_db), active_syms=set(),
            live_quotes={"300209": {"percent": -1.0, "high_pct": 8.0}}, turned_red_drop=6.0)
        assert "300209" in marked
        # 重置后再验 路②：回落≥8 → 最高+8 现+1（回落 7 < 8）不移出
        memory_db.execute("UPDATE recommendations SET excluded=0 WHERE date=?", (now_beijing().date().isoformat(),))
        memory_db.commit()
        marked = mark_reversed_recommendations(
            memory_db, self._recs(memory_db), active_syms=set(),
            live_quotes={"300209": {"percent": 1.0, "high_pct": 8.0}}, overshoot_drop=8.0)
        assert marked == [], "回落不足自定义阈值不应移出"

    def test_current_candidate_not_excluded(self, memory_db):
        self._insert(memory_db, "300209", percent=3.0)
        marked = mark_reversed_recommendations(
            memory_db, self._recs(memory_db), active_syms={"300209"},
            live_quotes={"300209": {"percent": -3.0, "high_pct": 12.0}})
        assert marked == [], "当前候选即使大幅回落也不应被移出（orchestrator 每轮重评）"
        assert "300209" in {r["symbol"] for r in self._recs(memory_db)}

    def test_missing_quote_not_excluded(self, memory_db):
        self._insert(memory_db, "300209", percent=3.0)
        # 行情缺失 / percent 缺失 fail-open：无法度量回落 → 不移出
        marked = mark_reversed_recommendations(
            memory_db, self._recs(memory_db), active_syms=set(), live_quotes={})
        assert marked == []
        marked = mark_reversed_recommendations(
            memory_db, self._recs(memory_db), active_syms=set(),
            live_quotes={"300209": {"current": 10.0}})
        assert marked == []
        assert "300209" in {r["symbol"] for r in self._recs(memory_db)}

    def test_degraded_quote_current_zero_not_excluded(self, memory_db):
        """回归（2026-08-14）：行情降级条目（current<=0，percent 被生产端强转 0.0）
        不得被当"已转负 0.00%"误移出——此前 percent=None 的 fail-open 检查因生产端
        强转而不可达，停牌/字段缺失票会被误标 excluded=1。"""
        self._insert(memory_db, "300209", percent=6.0)
        # 降级行情：current=0、percent=0.0（强转产物）、high_pct 缺失回退 rec_pct=6.0
        # 若无守卫：live_pct=0<0（转负）+ drop=6.0-0.0=6.0 ≥ 5 → 路①误移出
        marked = mark_reversed_recommendations(
            memory_db, self._recs(memory_db), active_syms=set(),
            live_quotes={"300209": {"current": 0.0, "percent": 0.0, "high_pct": None}})
        assert marked == [], "降级行情（current<=0）应 fail-open 不移出"
        assert "300209" in {r["symbol"] for r in self._recs(memory_db)}

    def test_mixed_main_and_comeback_only_main_excluded(self, memory_db):
        """回归（2026-08-17 审查修复）：同 symbol 当日既有榜上主类别行（触发反转移出）
        又有早先落库的 comeback 行时，UPDATE 必须带类别守卫（COALESCE(category,'')!='comeback'）
        ——此前无守卫会把两行全标 excluded=1，综合排序里回马枪候选凭空消失。"""
        self._insert(memory_db, "300209", percent=3.86, category="short_term")
        self._insert(memory_db, "300209", percent=3.0, category="comeback")
        marked = mark_reversed_recommendations(
            memory_db, self._recs(memory_db), active_syms=set(),
            live_quotes={"300209": {"percent": -3.15, "high_pct": 12.33}})
        assert "300209" in marked
        rows = memory_db.execute(
            "SELECT category, excluded FROM recommendations WHERE date=? AND symbol=?",
            (now_beijing().date().isoformat(), "300209")).fetchall()
        by_cat = {r[0]: r[1] for r in rows}
        assert by_cat.get("short_term") == 1, "榜上主类别行应被移出"
        assert by_cat.get("comeback") == 0, "comeback 行不得连带移出"

    def test_first_time_skips_excluded_earlier_rec(self, memory_db):
        """回归（2026-08-17 审查修复）：首推时间 MIN(time) 必须过滤 excluded 行——
        早盘已推荐的票盘中反转移出（excluded=1）后，再推荐行的时间不被早先已移出
        记录的 time 污染（此前 first_time 取到更早的 10:00）。"""
        today = now_beijing().date().isoformat()
        memory_db.execute(
            "INSERT INTO recommendations (date, time, symbol, name, category, score, percent, excluded) "
            "VALUES (?, '10:00', '300209', '已移出', 'short_term', 80, 3.0, 1)", (today,))
        memory_db.execute(
            "INSERT INTO recommendations (date, time, symbol, name, category, score, percent, excluded) "
            "VALUES (?, '14:00', '300209', '再推荐', 'short_term', 80, 3.0, 0)", (today,))
        memory_db.commit()
        recs = self._recs(memory_db)
        assert len(recs) == 1
        assert recs[0]["first_time"] == "14:00", (
            f"首推时间应取未移出记录, got {recs[0]['first_time']}")


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
