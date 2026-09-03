import sqlite3
from datetime import timedelta

import pytest

from scanner.config import now_beijing
from scanner.core_themes import (
    _dip_metrics,
    _recent_10d_return,
    core_stock_symbols,
    find_core_theme_dips,
    identify_core_themes,
)

# 相对日期（2026-09-03 修复）：get_cached_klines 有「近 60 天」滚动过滤，
# 此前测试硬编码 2026-07-01 起的 K 线日期随真实时间漂移出窗口（09-03 起
# 前 4 根被滤掉只剩 22 根 < _MIN_BARS=24，find_core_theme_dips 恒空）。
# 所有日期一律相对真实今天生成，不再随时间漂移。
TODAY = now_beijing().date().isoformat()
# 主题持续上榜日：today-5 / today-3 / today-1（3 个不同日 ≥ CORE_THEME_MIN_DAYS）
REC_DATES = (
    (now_beijing().date() - timedelta(days=5)).isoformat(),
    (now_beijing().date() - timedelta(days=3)).isoformat(),
    (now_beijing().date() - timedelta(days=1)).isoformat(),
)
# 回看起点 mock：足够早，使任意相对推荐日都落入回看窗口
LOOKBACK_START = "2000-01-01"


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
        (date, sym, name, concept),
    )
    conn.commit()


def _mk_series(base, run_pct, pullback_pct, n=26, today=None):
    """升序日期价格序列：前 20 根 flat=base，随后 5 根 ramp 到 peak，最后一根今日回撤。

    这样 close[-21] 恰为 base（20 日涨幅基准精确），回撤基准为 20 日窗口内高点。
    返回 (dates, closes)，最后一根 date=today。日期相对真实今天生成（见文件头说明）。
    """
    dates = []
    closes = []
    end = now_beijing().date()
    flat_n = n - 6  # 前 flat_n 根 flat = base（n=26 → 20 根）
    peak = base * (1 + run_pct)
    for i in range(n - 1):
        dates.append((end - timedelta(days=n - 1 - i)).isoformat())
        if i < flat_n:
            closes.append(base)
        else:
            frac = (i - flat_n + 1) / 5.0
            closes.append(base + (peak - base) * frac)
    # 最后一根 = 今日，回撤
    closes.append(peak * (1 + pullback_pct))
    dates.append((today or end).isoformat())
    return dates, closes


def _write_klines(conn, sym, dates, closes):
    for d, c in zip(dates, closes, strict=True):
        conn.execute(
            "INSERT INTO daily_kline (symbol, date, open, close, high, low, volume, percent) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
            (sym, d, c, c, c, c, 1000),
        )
    conn.commit()


class TestDipMetrics:
    def test_short_series_returns_none(self):
        assert _dip_metrics([1, 2, 3], ["a", "b", "c"], "2026-08-19") is None

    def test_run_and_pullback(self):
        # 涨 20% 后回撤 5%
        dates, closes = _mk_series(10.0, 0.20, -0.05)
        m = _dip_metrics(closes, dates, dates[-1])
        assert m is not None
        assert m["overheated"] is False
        assert m["run"] == pytest.approx(0.14, abs=0.03)  # (1.20*0.95-1)
        assert m["pullback"] == pytest.approx(-0.05, abs=0.02)

    def test_overheated_flag(self):
        # 涨 80% 后回撤 → 超买死亡区
        dates, closes = _mk_series(10.0, 0.80, -0.05)
        m = _dip_metrics(closes, dates, dates[-1])
        assert m is not None
        assert m["overheated"] is True

    def test_today_pct(self):
        # 最后一根比前一根低 8% → 崩盘日
        dates, closes = _mk_series(10.0, 0.20, -0.05)
        # 手动把最后一根再压低 8%
        closes[-1] = closes[-2] * 0.92
        m = _dip_metrics(closes, dates, dates[-1])
        assert m is not None
        assert m["today_pct"] == pytest.approx(-0.08, abs=0.01)


class TestIdentifyCoreThemes:
    def test_picks_persistent_strong_theme(self, db, monkeypatch):
        monkeypatch.setattr("scanner.core_themes._n_trading_days_ago", lambda *a, **k: "2026-08-01")
        # 华为概念：多日持续且成员涨幅高
        _insert_rec(db, "SZ300001", "a", "华为概念", "2026-08-12")
        _insert_rec(db, "SZ300002", "b", "华为概念", "2026-08-14")
        _insert_rec(db, "SZ300003", "c", "华为概念", "2026-08-18")
        # 冷门概念：只出现一次 → 被过滤
        _insert_rec(db, "SZ300004", "d", "冷门", "2026-08-18")
        themes = identify_core_themes(db, "2026-08-19", min_days=2)
        names = [t["name"] for t in themes]
        assert "华为概念" in names
        assert "冷门" not in names


class TestFindCoreThemeDips:
    def test_none_conn_returns_empty(self):
        assert find_core_theme_dips(None, "2026-08-19") == []

    def test_returns_dip_candidates(self, db, monkeypatch):
        monkeypatch.setattr("scanner.core_themes._n_trading_days_ago", lambda *a, **k: LOOKBACK_START)
        # 核心主题：华为概念持续上榜
        for d in REC_DATES:
            _insert_rec(db, "SZ300001", "龙头", "华为概念", d)
        # 成员：涨20%后回撤5% = 低吸候选
        dates, closes = _mk_series(10.0, 0.20, -0.05)
        _write_klines(db, "SZ300001", dates, closes)
        db.execute(
            "INSERT INTO concept_cache (symbol, concepts, updated) VALUES ('SZ300001', '[\"华为概念\", \"电子\"]', ?)",
            (TODAY,),
        )
        db.execute("INSERT INTO appearances (symbol, name, date) VALUES ('SZ300001', '龙头', ?)", (TODAY,))
        db.commit()

        dips = find_core_theme_dips(db, TODAY)
        assert len(dips) == 1
        assert dips[0]["symbol"] == "SZ300001"
        assert dips[0]["concept"] == "华为概念"
        assert dips[0]["run"] > 0

    def test_crash_day_filtered(self, db, monkeypatch):
        monkeypatch.setattr("scanner.core_themes._n_trading_days_ago", lambda *a, **k: LOOKBACK_START)
        for d in REC_DATES:
            _insert_rec(db, "SZ300001", "龙头", "华为概念", d)
        # 成员今日崩盘 -8% → 被 CORE_TODAY_FLOOR 过滤
        dates, closes = _mk_series(10.0, 0.20, -0.05)
        closes[-1] = closes[-2] * 0.92
        _write_klines(db, "SZ300001", dates, closes)
        db.execute(
            "INSERT INTO concept_cache (symbol, concepts, updated) VALUES ('SZ300001', '[\"华为概念\"]', ?)",
            (TODAY,),
        )
        db.commit()
        assert find_core_theme_dips(db, TODAY) == []

    def test_theme_member_even_if_not_recently_rec(self, db, monkeypatch):
        """核心主题成员可来自 concept_cache（未被近 N 日推荐也纳入，扩大核心股来源）。"""
        monkeypatch.setattr("scanner.core_themes._n_trading_days_ago", lambda *a, **k: LOOKBACK_START)
        for d in REC_DATES:
            _insert_rec(db, "SZ300001", "龙头", "华为概念", d)
        # 成员2 只存在于 concept_cache，不在近期推荐，但属于华为概念且回调 → 应纳入
        dates2, closes2 = _mk_series(20.0, 0.30, -0.05)
        _write_klines(db, "SZ300002", dates2, closes2)
        db.execute(
            "INSERT INTO concept_cache (symbol, concepts, updated) VALUES "
            "('SZ300001', '[\"华为概念\"]', ?), "
            "('SZ300002', '[\"华为概念\"]', ?)",
            (TODAY, TODAY),
        )
        db.commit()

        dips = find_core_theme_dips(db, TODAY)
        got = {c["symbol"] for c in dips}
        assert "SZ300002" in got


class TestCoreStockSymbols:
    """核心股判定（2026-08-19）：核心主题成员 + 20日累计≥CORE_RUN_MIN（走强龙头）。

    **与核心低吸区（core_dip）的边界**：core_dip 候选必须满足「主题成员 + 走强 + 低吸
    回撤窗口」；高亮用本集合不含低吸窗口——低吸区只捕获「回调中的核心股」，会漏掉创新高
    走强中的主线龙头（2026-08-19 江天化学：央国企改革成员、20日+22.3%，回撤0%落不进
    低吸窗口，但它是核心股应高亮）。{core_dip} ⊆ 本集合（低吸区候选必然同时满足两条）。
    """

    @staticmethod
    def _seed_theme(db, monkeypatch, member_syms):
        """造一个核心主题（华为概念，3 个推荐日）+ concept_cache 成员。"""
        monkeypatch.setattr("scanner.core_themes._n_trading_days_ago", lambda *a, **k: LOOKBACK_START)
        for d in REC_DATES:
            _insert_rec(db, "SZ300099", "主题日", "华为概念", d)
        for sym, concepts in member_syms:
            db.execute("INSERT INTO concept_cache (symbol, concepts, updated) VALUES (?, ?, ?)", (sym, concepts, TODAY))
        db.commit()

    def test_none_conn_returns_empty(self):
        assert core_stock_symbols(None, "2026-08-19") == set()

    def test_running_leader_at_high_included(self, db, monkeypatch):
        """江天化学式：核心主题成员、20日+25%（走强）但创新高（回撤0）——不在低吸窗口，
        但按用户口径只要「核心股」就高亮，应被本集合捕获。"""
        self._seed_theme(db, monkeypatch, [("SZ300001", '["华为概念"]')])
        dates, closes = _mk_series(10.0, 0.25, 0.0)  # 走强 + 创新高（无回撤）
        _write_klines(db, "SZ300001", dates, closes)
        got = core_stock_symbols(db, TODAY)
        assert "SZ300001" in got

    def test_weak_member_excluded(self, db, monkeypatch):
        """核心主题成员但 20 日累计 < CORE_RUN_MIN（未走强）→ 非核心股不高亮。"""
        self._seed_theme(db, monkeypatch, [("SZ300001", '["华为概念"]')])
        dates, closes = _mk_series(10.0, 0.05, 0.0)  # 20日仅 +5% < 12%
        _write_klines(db, "SZ300001", dates, closes)
        got = core_stock_symbols(db, TODAY)
        assert "SZ300001" not in got

    def test_non_member_excluded(self, db, monkeypatch):
        """非核心主题成员（concept_cache 无核心概念）→ 非核心股。"""
        self._seed_theme(db, monkeypatch, [("SZ300001", '["冷门概念"]')])
        dates, closes = _mk_series(10.0, 0.30, 0.0)
        _write_klines(db, "SZ300001", dates, closes)
        got = core_stock_symbols(db, TODAY)
        assert "SZ300001" not in got

    def test_dip_candidate_is_core_stock_subset(self, db, monkeypatch):
        """一致性：回调中的走强核心股既进 core_dip（低吸候选）也进 core_stock_symbols——
        「低吸区里的票都是核心股」原语义保留。"""
        self._seed_theme(db, monkeypatch, [("SZ300001", '["华为概念"]')])
        dates, closes = _mk_series(10.0, 0.20, -0.05)  # 涨20%回撤5% = 低吸候选
        _write_klines(db, "SZ300001", dates, closes)
        dips = {c["symbol"] for c in find_core_theme_dips(db, TODAY)}
        assert "SZ300001" in dips
        assert "SZ300001" in core_stock_symbols(db, TODAY)


class TestLowBuyQualitySort:
    def test_flow_positive_before_negative(self):
        from scanner.core_themes import _low_buy_quality

        pos = {"flow_pct": 3.0, "pullback": -0.05, "run": 0.2, "today_pct": 0.0}
        neg = {"flow_pct": -3.0, "pullback": -0.05, "run": 0.2, "today_pct": 0.0}
        # 升序键：pos 更小 → 排前
        assert _low_buy_quality(pos) < _low_buy_quality(neg)

    def test_strong_inflow_before_mild(self):
        from scanner.core_themes import _low_buy_quality

        strong = {"flow_pct": 6.0, "pullback": -0.05, "run": 0.2, "today_pct": 0.0}
        mild = {"flow_pct": 2.0, "pullback": -0.05, "run": 0.2, "today_pct": 0.0}
        assert _low_buy_quality(strong) < _low_buy_quality(mild)

    def test_deeper_pullback_first_within_tier(self):
        from scanner.core_themes import _low_buy_quality

        deep = {"flow_pct": None, "pullback": -0.10, "run": 0.2, "today_pct": None}
        shallow = {"flow_pct": None, "pullback": -0.04, "run": 0.2, "today_pct": None}
        assert _low_buy_quality(deep) < _low_buy_quality(shallow)

    def test_stronger_leader_first_within_tier(self):
        from scanner.core_themes import _low_buy_quality

        strong = {"flow_pct": None, "pullback": -0.05, "run": 0.5, "today_pct": None}
        weak = {"flow_pct": None, "pullback": -0.05, "run": 0.15, "today_pct": None}
        assert _low_buy_quality(strong) < _low_buy_quality(weak)

    def test_extreme_today_first_within_tier(self):
        from scanner.core_themes import _low_buy_quality

        # 2026-08-29：今日波动剧烈（涨多/跌狠）优先排前，|today| 越大越靠前；
        # 小涨(0.02)与小跌(-0.03)幅度相近 → 各自都排在大涨(0.09)/(-0.09)之后。
        big_up = {"flow_pct": None, "pullback": -0.05, "run": 0.2, "today_pct": 0.09}
        big_down = {"flow_pct": None, "pullback": -0.05, "run": 0.2, "today_pct": -0.09}
        flat = {"flow_pct": None, "pullback": -0.05, "run": 0.2, "today_pct": 0.0}
        assert _low_buy_quality(big_up) < _low_buy_quality(flat)
        assert _low_buy_quality(big_down) < _low_buy_quality(flat)
        # 涨多(+)与跌狠(-)同幅并列，互不影响主排序（都按 |today| 排前）
        assert _low_buy_quality(big_up) == _low_buy_quality(big_down)


class TestSaveCoreDips:
    def test_saves_core_dip_category(self, db, monkeypatch):
        from scanner.core_themes import save_core_dips

        dips = [
            {
                "symbol": "SZ300001",
                "name": "龙头",
                "concept": "华为概念",
                "run": 0.2,
                "pullback": -0.08,
                "today_pct": -0.01,
                "below_ma20_ratio": 0.0,
                "flow_pct": 3.0,
            }
        ]
        monkeypatch.setattr(
            "scanner.core_themes.now_beijing", lambda: __import__("datetime").datetime(2026, 8, 19, 10, 0, 0)
        )
        save_core_dips(db, dips, "2026-08-19")
        row = db.execute(
            "SELECT symbol, category, concept, percent, trend, score_breakdown FROM recommendations"
        ).fetchone()
        assert row[0] == "SZ300001"
        assert row[1] == "core_dip"
        assert row[2] == "华为概念"
        assert row[3] == -1.0  # today_pct -0.01 → -1%
        assert row[4] == "主线回调"
        import json

        sb = json.loads(row[5])
        assert sb["pullback"] == -0.08

    def test_saves_accumulated_pct(self, db, monkeypatch):
        """核心低吸行必须落库 accumulated_pct（与主表「5日累计」列同口径）。

        此前 save_core_dips 漏写该列 → 展示层回退链取到 None → 核心低吸区
        「5日累计」整列恒显 —（见 display._print_priority_row）。
        """
        from scanner.core_themes import save_core_dips

        dips = [
            {
                "symbol": "SZ300001",
                "name": "龙头",
                "concept": "华为概念",
                "run": 0.2,
                "pullback": -0.08,
                "today_pct": -0.01,
                "below_ma20_ratio": 0.0,
                "flow_pct": 3.0,
                "accum_5": 6.18,
            }
        ]
        monkeypatch.setattr(
            "scanner.core_themes.now_beijing", lambda: __import__("datetime").datetime(2026, 8, 19, 10, 0, 0)
        )
        save_core_dips(db, dips, "2026-08-19")
        acc = db.execute("SELECT accumulated_pct FROM recommendations").fetchone()[0]
        assert acc == 6.18
        # 数据缺失（无 accum_5）应落 NULL 而非报错
        save_core_dips(
            db,
            [
                {
                    "symbol": "SZ300002",
                    "name": "x",
                    "concept": "c",
                    "run": 0.2,
                    "pullback": -0.08,
                    "today_pct": 0.0,
                    "below_ma20_ratio": 0.0,
                    "flow_pct": 1.0,
                }
            ],
            "2026-08-19",
        )
        acc2 = db.execute("SELECT accumulated_pct FROM recommendations WHERE symbol='SZ300002'").fetchone()[0]
        assert acc2 is None

    def test_dedup_keeps_highest_score(self, db, monkeypatch):
        from scanner.core_themes import save_core_dips

        monkeypatch.setattr(
            "scanner.core_themes.now_beijing", lambda: __import__("datetime").datetime(2026, 8, 19, 10, 0, 0)
        )
        low = {
            "symbol": "SZ300001",
            "name": "龙头",
            "concept": "华为概念",
            "run": 0.1,
            "pullback": -0.04,
            "today_pct": 0.0,
            "below_ma20_ratio": 0.0,
            "flow_pct": -3.0,
        }
        high = {
            "symbol": "SZ300001",
            "name": "龙头",
            "concept": "华为概念",
            "run": 0.4,
            "pullback": -0.15,
            "today_pct": -0.02,
            "below_ma20_ratio": 0.0,
            "flow_pct": 6.0,
        }
        save_core_dips(db, [low], "2026-08-19")
        save_core_dips(db, [high], "2026-08-19")
        rows = db.execute("SELECT COUNT(*), MAX(score) FROM recommendations").fetchone()
        assert rows[0] == 1  # 同票只保留一条
        # 高分（深回撤+强流入）应胜过低分（浅回撤+流出）
        assert rows[1] > 50

    def test_update_writes_score_column(self, db, monkeypatch):
        """UPDATE 必须同步写 score 列（2026-08-24 第二轮审查）。

        原实现只更新 time/percent/trend/breakdown——breakdown 已是更优低吸时刻，
        score 却留首次插入旧值，两字段互相矛盾且 ORDER BY score DESC 失真。
        """
        import json

        from scanner.core_themes import save_core_dips

        monkeypatch.setattr(
            "scanner.core_themes.now_beijing", lambda: __import__("datetime").datetime(2026, 8, 19, 10, 0, 0)
        )
        low = {
            "symbol": "SZ300001",
            "name": "龙头",
            "concept": "华为概念",
            "run": 0.1,
            "pullback": -0.04,
            "today_pct": 0.0,
            "below_ma20_ratio": 0.0,
            "flow_pct": -3.0,
        }
        high = {
            "symbol": "SZ300001",
            "name": "龙头",
            "concept": "华为概念",
            "run": 0.4,
            "pullback": -0.15,
            "today_pct": -0.02,
            "below_ma20_ratio": 0.0,
            "flow_pct": 6.0,
        }
        save_core_dips(db, [low], "2026-08-19")
        first_score = db.execute("SELECT score FROM recommendations").fetchone()[0]
        save_core_dips(db, [high], "2026-08-19")
        score, sb = db.execute("SELECT score, score_breakdown FROM recommendations").fetchone()
        assert score > first_score, "score 列必须随更优时刻更新"
        assert json.loads(sb)["pullback"] == -0.15

    def test_empty_or_none_is_fail_open(self, db):
        from scanner.core_themes import save_core_dips

        save_core_dips(None, [{"symbol": "x"}], "2026-08-19")  # None conn → no-op
        save_core_dips(db, [], "2026-08-19")  # 空列表 → no-op
        assert db.execute("SELECT COUNT(*) FROM recommendations").fetchone()[0] == 0


class TestRecent10DReturn:
    def test_basic(self):
        # 近 10 个交易日涨 5%：base = closes[-11] = 11，最后一根 11.55
        closes = [10.0] * 9 + [11.0] * 10 + [11.55]
        assert _recent_10d_return(closes) == pytest.approx(0.05, abs=0.001)
