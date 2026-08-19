import sqlite3
from datetime import date, timedelta

import pytest

from scanner.core_themes import (
    _dip_metrics,
    _recent_10d_return,
    find_core_theme_dips,
    identify_core_themes,
)


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE recommendations (
        id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL,
        time TEXT NOT NULL, symbol TEXT NOT NULL, name TEXT NOT NULL,
        category TEXT NOT NULL, score INTEGER NOT NULL, percent REAL,
        trend TEXT, next_day_pct REAL, fwd_3d REAL, fwd_5d REAL,
        score_breakdown TEXT, source TEXT DEFAULT 'xueqiu',
        concept TEXT, accumulated_pct REAL, excluded INTEGER DEFAULT 0,
        stale_kline INTEGER DEFAULT 0)""")
    conn.execute("""CREATE TABLE concept_cache (
        symbol TEXT PRIMARY KEY, concepts TEXT NOT NULL, updated TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE daily_kline (
        symbol TEXT NOT NULL, timestamp INTEGER, date TEXT NOT NULL,
        open REAL, close REAL, high REAL, low REAL, volume REAL,
        percent REAL, finalized INTEGER DEFAULT 1,
        PRIMARY KEY(symbol, date))""")
    conn.execute("""CREATE TABLE appearances (
        id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL,
        name TEXT NOT NULL, date TEXT NOT NULL, rank INTEGER,
        percent REAL, value REAL, UNIQUE(symbol, date))""")
    conn.execute("""CREATE TABLE market_extra_cache (
        symbol TEXT NOT NULL, date TEXT NOT NULL, data_type TEXT NOT NULL,
        payload_json TEXT NOT NULL, updated TEXT NOT NULL,
        PRIMARY KEY(symbol, data_type))""")
    conn.commit()
    return conn


def _insert_rec(conn, sym, name, concept, date):
    conn.execute(
        "INSERT INTO recommendations (date, time, symbol, name, category, score, "
        "percent, concept) VALUES (?, '10:00', ?, ?, 'short_term', 60, 5.0, ?)",
        (date, sym, name, concept))
    conn.commit()


def _mk_series(base, run_pct, pullback_pct, n=26, today=None):
    """升序日期价格序列：前 20 根 flat=base，随后 5 根 ramp 到 peak，最后一根今日回撤。

    这样 close[-21] 恰为 base（20 日涨幅基准精确），回撤基准为 20 日窗口内高点。
    返回 (dates, closes)，最后一根 date=today。
    """
    dates = []
    closes = []
    start = date(2026, 7, 1)
    flat_n = n - 6            # 前 flat_n 根 flat = base（n=26 → 20 根）
    peak = base * (1 + run_pct)
    for i in range(n - 1):
        dates.append((start + timedelta(days=i)).isoformat())
        if i < flat_n:
            closes.append(base)
        else:
            frac = (i - flat_n + 1) / 5.0
            closes.append(base + (peak - base) * frac)
    # 最后一根 = 今日，回撤
    closes.append(peak * (1 + pullback_pct))
    dates.append((today or date(2026, 8, 19)).isoformat())
    return dates, closes


def _write_klines(conn, sym, dates, closes):
    for d, c in zip(dates, closes):
        conn.execute(
            "INSERT INTO daily_kline (symbol, date, open, close, high, low, volume, percent) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
            (sym, d, c, c, c, c, 1000))
    conn.commit()


class TestDipMetrics:
    def test_short_series_returns_none(self):
        assert _dip_metrics([1, 2, 3], ['a', 'b', 'c'], '2026-08-19') is None

    def test_run_and_pullback(self):
        # 涨 20% 后回撤 5%
        dates, closes = _mk_series(10.0, 0.20, -0.05)
        m = _dip_metrics(closes, dates, dates[-1])
        assert m is not None
        assert m['overheated'] is False
        assert m['run'] == pytest.approx(0.14, abs=0.03)   # (1.20*0.95-1)
        assert m['pullback'] == pytest.approx(-0.05, abs=0.02)

    def test_overheated_flag(self):
        # 涨 80% 后回撤 → 超买死亡区
        dates, closes = _mk_series(10.0, 0.80, -0.05)
        m = _dip_metrics(closes, dates, dates[-1])
        assert m is not None
        assert m['overheated'] is True

    def test_today_pct(self):
        # 最后一根比前一根低 8% → 崩盘日
        dates, closes = _mk_series(10.0, 0.20, -0.05)
        # 手动把最后一根再压低 8%
        closes[-1] = closes[-2] * 0.92
        m = _dip_metrics(closes, dates, dates[-1])
        assert m is not None
        assert m['today_pct'] == pytest.approx(-0.08, abs=0.01)


class TestIdentifyCoreThemes:
    def test_picks_persistent_strong_theme(self, db, monkeypatch):
        monkeypatch.setattr(
            'scanner.core_themes._n_trading_days_ago', lambda *a, **k: '2026-08-01')
        # 华为概念：多日持续且成员涨幅高
        _insert_rec(db, 'SZ300001', 'a', '华为概念', '2026-08-12')
        _insert_rec(db, 'SZ300002', 'b', '华为概念', '2026-08-14')
        _insert_rec(db, 'SZ300003', 'c', '华为概念', '2026-08-18')
        # 冷门概念：只出现一次 → 被过滤
        _insert_rec(db, 'SZ300004', 'd', '冷门', '2026-08-18')
        themes = identify_core_themes(db, '2026-08-19', min_days=2)
        names = [t['name'] for t in themes]
        assert '华为概念' in names
        assert '冷门' not in names


class TestFindCoreThemeDips:
    def test_none_conn_returns_empty(self):
        assert find_core_theme_dips(None, '2026-08-19') == []

    def test_returns_dip_candidates(self, db, monkeypatch):
        monkeypatch.setattr(
            'scanner.core_themes._n_trading_days_ago', lambda *a, **k: '2026-08-01')
        # 核心主题：华为概念持续上榜
        for d in ('2026-08-12', '2026-08-14', '2026-08-18'):
            _insert_rec(db, 'SZ300001', '龙头', '华为概念', d)
        # 成员：涨20%后回撤5% = 低吸候选
        dates, closes = _mk_series(10.0, 0.20, -0.05)
        _write_klines(db, 'SZ300001', dates, closes)
        db.execute("INSERT INTO concept_cache (symbol, concepts, updated) "
                     "VALUES ('SZ300001', '[\"华为概念\", \"电子\"]', '2026-08-19')")
        db.execute("INSERT INTO appearances (symbol, name, date) "
                     "VALUES ('SZ300001', '龙头', '2026-08-19')")
        db.commit()

        dips = find_core_theme_dips(db, '2026-08-19')
        assert len(dips) == 1
        assert dips[0]['symbol'] == 'SZ300001'
        assert dips[0]['concept'] == '华为概念'
        assert dips[0]['run'] > 0

    def test_crash_day_filtered(self, db, monkeypatch):
        monkeypatch.setattr(
            'scanner.core_themes._n_trading_days_ago', lambda *a, **k: '2026-08-01')
        for d in ('2026-08-12', '2026-08-14', '2026-08-18'):
            _insert_rec(db, 'SZ300001', '龙头', '华为概念', d)
        # 成员今日崩盘 -8% → 被 CORE_TODAY_FLOOR 过滤
        dates, closes = _mk_series(10.0, 0.20, -0.05)
        closes[-1] = closes[-2] * 0.92
        _write_klines(db, 'SZ300001', dates, closes)
        db.execute("INSERT INTO concept_cache (symbol, concepts, updated) "
                     "VALUES ('SZ300001', '[\"华为概念\"]', '2026-08-19')")
        db.commit()
        assert find_core_theme_dips(db, '2026-08-19') == []

    def test_theme_member_even_if_not_recently_rec(self, db, monkeypatch):
        """核心主题成员可来自 concept_cache（未被近 N 日推荐也纳入，扩大核心股来源）。"""
        monkeypatch.setattr(
            'scanner.core_themes._n_trading_days_ago', lambda *a, **k: '2026-08-01')
        for d in ('2026-08-12', '2026-08-14', '2026-08-18'):
            _insert_rec(db, 'SZ300001', '龙头', '华为概念', d)
        # 成员2 只存在于 concept_cache，不在近期推荐，但属于华为概念且回调 → 应纳入
        dates2, closes2 = _mk_series(20.0, 0.30, -0.05)
        _write_klines(db, 'SZ300002', dates2, closes2)
        db.execute("INSERT INTO concept_cache (symbol, concepts, updated) VALUES "
                     "('SZ300001', '[\"华为概念\"]', '2026-08-19'), "
                     "('SZ300002', '[\"华为概念\"]', '2026-08-19')")
        db.commit()

        dips = find_core_theme_dips(db, '2026-08-19')
        got = {c['symbol'] for c in dips}
        assert 'SZ300002' in got


class TestLowBuyQualitySort:
    def test_flow_positive_before_negative(self):
        from scanner.core_themes import _low_buy_quality
        pos = {'flow_pct': 3.0, 'pullback': -0.05, 'run': 0.2, 'today_pct': 0.0}
        neg = {'flow_pct': -3.0, 'pullback': -0.05, 'run': 0.2, 'today_pct': 0.0}
        # 升序键：pos 更小 → 排前
        assert _low_buy_quality(pos) < _low_buy_quality(neg)

    def test_strong_inflow_before_mild(self):
        from scanner.core_themes import _low_buy_quality
        strong = {'flow_pct': 6.0, 'pullback': -0.05, 'run': 0.2, 'today_pct': 0.0}
        mild = {'flow_pct': 2.0, 'pullback': -0.05, 'run': 0.2, 'today_pct': 0.0}
        assert _low_buy_quality(strong) < _low_buy_quality(mild)

    def test_deeper_pullback_first_within_tier(self):
        from scanner.core_themes import _low_buy_quality
        deep = {'flow_pct': None, 'pullback': -0.10, 'run': 0.2, 'today_pct': None}
        shallow = {'flow_pct': None, 'pullback': -0.04, 'run': 0.2, 'today_pct': None}
        assert _low_buy_quality(deep) < _low_buy_quality(shallow)

    def test_stronger_leader_first_within_tier(self):
        from scanner.core_themes import _low_buy_quality
        strong = {'flow_pct': None, 'pullback': -0.05, 'run': 0.5, 'today_pct': None}
        weak = {'flow_pct': None, 'pullback': -0.05, 'run': 0.15, 'today_pct': None}
        assert _low_buy_quality(strong) < _low_buy_quality(weak)

    def test_stabilizing_today_first_within_tier(self):
        from scanner.core_themes import _low_buy_quality
        up = {'flow_pct': None, 'pullback': -0.05, 'run': 0.2, 'today_pct': 0.02}
        down = {'flow_pct': None, 'pullback': -0.05, 'run': 0.2, 'today_pct': -0.03}
        assert _low_buy_quality(up) < _low_buy_quality(down)


class TestSaveCoreDips:
    def test_saves_core_dip_category(self, db, monkeypatch):
        from scanner.core_themes import save_core_dips
        dips = [{"symbol": "SZ300001", "name": "龙头", "concept": "华为概念",
                 "run": 0.2, "pullback": -0.08, "today_pct": -0.01,
                 "below_ma20_ratio": 0.0, "flow_pct": 3.0}]
        monkeypatch.setattr('scanner.core_themes.now_beijing',
                            lambda: __import__('datetime').datetime(2026, 8, 19, 10, 0, 0))
        save_core_dips(db, dips, '2026-08-19')
        row = db.execute("SELECT symbol, category, concept, percent, trend, score_breakdown "
                         "FROM recommendations").fetchone()
        assert row[0] == 'SZ300001'
        assert row[1] == 'core_dip'
        assert row[2] == '华为概念'
        assert row[3] == -1.0          # today_pct -0.01 → -1%
        assert row[4] == '主线回调'
        import json
        sb = json.loads(row[5])
        assert sb['pullback'] == -0.08

    def test_dedup_keeps_highest_score(self, db, monkeypatch):
        from scanner.core_themes import save_core_dips
        monkeypatch.setattr('scanner.core_themes.now_beijing',
                            lambda: __import__('datetime').datetime(2026, 8, 19, 10, 0, 0))
        low = {"symbol": "SZ300001", "name": "龙头", "concept": "华为概念",
               "run": 0.1, "pullback": -0.04, "today_pct": 0.0,
               "below_ma20_ratio": 0.0, "flow_pct": -3.0}
        high = {"symbol": "SZ300001", "name": "龙头", "concept": "华为概念",
                "run": 0.4, "pullback": -0.15, "today_pct": -0.02,
                "below_ma20_ratio": 0.0, "flow_pct": 6.0}
        save_core_dips(db, [low], '2026-08-19')
        save_core_dips(db, [high], '2026-08-19')
        rows = db.execute("SELECT COUNT(*), MAX(score) FROM recommendations").fetchone()
        assert rows[0] == 1            # 同票只保留一条
        # 高分（深回撤+强流入）应胜过低分（浅回撤+流出）
        assert rows[1] > 50

    def test_empty_or_none_is_fail_open(self, db):
        from scanner.core_themes import save_core_dips
        save_core_dips(None, [{"symbol": "x"}], '2026-08-19')   # None conn → no-op
        save_core_dips(db, [], '2026-08-19')                      # 空列表 → no-op
        assert db.execute("SELECT COUNT(*) FROM recommendations").fetchone()[0] == 0


class TestRecent10DReturn:
    def test_basic(self):
        # 近 10 个交易日涨 5%：base = closes[-11] = 11，最后一根 11.55
        closes = [10.0] * 9 + [11.0] * 10 + [11.55]
        assert _recent_10d_return(closes) == pytest.approx(0.05, abs=0.001)
