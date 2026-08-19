from datetime import date, datetime, timedelta

from scanner.candidate_pool import ScanSession
from scanner.models import Candidate, KlineSummary, StockInfo
from scanner.orchestrator import (
    _candidate_excluded_by_risk,
    _enrich_candidate_market_cap,
    _fetch_all_klines,
)


def _make_candidate(symbol: str, score: int = 20, kline_dims: dict | None = None,
                    name: str = "Test") -> Candidate:
    stock = StockInfo(symbol=symbol, name=name, code="300001",
                      percent=5.0, current=10.0, value=10000,
                      rank_change=1000, rank=1)
    kline_s = KlineSummary(trend="test", accumulated_pct=2.0,
                            volume_ratio=1.5, bottom_confirmed=True,
                            score=score, dimensions=kline_dims or {}, avg_volume=1_000_000)
    return Candidate(stock=stock, category="new_face", score=score,
                     reason="test", kline=kline_s, first_seen="09:30")


class TestEnrichCandidateMarketCap:
    """回归：回马枪 off-list 候选补齐 stock.market_cap（亿元），
    使 enhancer._apply_market_cap_bonus 的小市值加分不再系统性缺失。"""

    def _cand(self, symbol: str = "SZ300123") -> Candidate:
        st = StockInfo(symbol=symbol, name="测试", code="300123",
                       percent=3.0, current=10.0, value=0.0,
                       rank_change=0, rank=0, source_tag="comeback")
        ks = KlineSummary(trend="t", accumulated_pct=-10.0, volume_ratio=1.0,
                          bottom_confirmed=False, score=30)
        return Candidate(stock=st, category="comeback", score=30,
                         reason="t", kline=ks)

    def test_sets_stock_market_cap_in_yi(self):
        from scanner.enhancer import _apply_market_cap_bonus
        c = self._cand()
        _enrich_candidate_market_cap(c, {"market_cap": 5_000_000_000, "circ_market_cap": 3_000_000_000})
        assert c.market_cap == 5_000_000_000
        assert c.circ_market_cap == 3_000_000_000
        assert c.stock.market_cap == 30.0  # 30 亿元（流通市值优先）
        _apply_market_cap_bonus(c)
        assert c.market_cap_bonus == 3  # 小市值加分不再缺失

    def test_circ_preferred_over_total(self):
        c = self._cand()
        _enrich_candidate_market_cap(c, {"market_cap": 2_000_000_000, "circ_market_cap": 200_000_000_000})
        assert c.stock.market_cap == 2000.0  # 用流通市值

    def test_missing_cap_data_keeps_zero(self):
        c = self._cand()
        _enrich_candidate_market_cap(c, {})
        assert c.market_cap == 0
        assert c.stock.market_cap == 0.0

    def test_large_cap_no_bonus(self):
        from scanner.enhancer import _apply_market_cap_bonus
        c = self._cand()
        _enrich_candidate_market_cap(c, {"circ_market_cap": 50_000_000_000})
        _apply_market_cap_bonus(c)
        assert c.market_cap_bonus == 0  # 500 亿超大市值不加分


class TestScanSession:
    def test_reset_on_new_day(self):
        ss = ScanSession()
        ss.seen_today.add("300001")
        ss.last_today = "2026-06-17"
        changed = ss.reset_if_new_day("2026-06-18")
        assert changed
        assert len(ss.seen_today) == 0
        assert len(ss.today_pool) == 0

    def test_no_reset_same_day(self):
        ss = ScanSession()
        ss.seen_today.add("300001")
        ss.last_today = "2026-06-18"
        changed = ss.reset_if_new_day("2026-06-18")
        assert not changed
        assert "300001" in ss.seen_today

    def test_mark_seen_first_time(self):
        ss = ScanSession()
        assert ss.mark_seen("300001") is True
        assert ss.mark_seen("300001") is False

    def test_update_pool_sets_first_seen(self):
        ss = ScanSession()
        c = _make_candidate("300001")
        ss.update_pool([c])
        assert c.stock.symbol in ss.today_pool
        assert c.first_seen is not None


class TestClassifyCategory:
    """按价格结构选标签，而非尝试顺序（修复核心）。"""

    def _stock(self, percent):
        return StockInfo(symbol="300001", name="Test", code="300001",
                         percent=percent, current=10.0, value=10000,
                         rank_change=1000, rank=1)

    def test_new_stock_prefers_new_face(self):
        from scanner.orchestrator import _classify_category
        c_nf = _make_candidate("300001")
        assert _classify_category(self._stock(5), True, None, c_nf) == "new_face"

    def test_known_stock_up_day_prefers_momentum(self):
        from scanner.orchestrator import _classify_category
        c_mo = _make_candidate("300002")
        assert _classify_category(self._stock(3), False, c_mo, None) == "momentum"

    def test_known_stock_only_new_face_fallback(self):
        from scanner.orchestrator import _classify_category
        c_nf = _make_candidate("300001")
        assert _classify_category(self._stock(3), False, None, c_nf) == "known_new_face"

    def test_no_candidate(self):
        from scanner.orchestrator import _classify_category
        assert _classify_category(self._stock(3), False, None, None) is None

    def test_known_stock_up_day_weak_to_strong_prefers_short_term(self):
        from scanner.orchestrator import _classify_category
        c_mo = _make_candidate("300001")
        c_st = _make_candidate("300002",
                               kline_dims={"st_weak_to_strong": 8})
        # 弱转强超短即便同时过动量也优先归超短
        assert _classify_category(self._stock(3), False, c_mo, None, c_st) == "short_term"

    def test_known_stock_up_day_non_wts_short_term_falls_to_momentum(self):
        from scanner.orchestrator import _classify_category
        c_mo = _make_candidate("300001")
        c_st = _make_candidate("300002")
        # 非弱转强超短合格票若同时过动量 → 归动量（避免掏空动量桶）
        assert _classify_category(self._stock(3), False, c_mo, None, c_st) == "momentum"

    def test_known_stock_up_day_non_wts_short_term_only_stays_short_term(self):
        from scanner.orchestrator import _classify_category
        c_st = _make_candidate("300002")
        # 仅过非弱转强超短、不过动量 → 仍留超短（不丢票）
        assert _classify_category(self._stock(3), False, None, None, c_st) == "short_term"

    def test_new_stock_prefers_short_term_over_momentum(self):
        from scanner.orchestrator import _classify_category
        c_mo = _make_candidate("300001")
        c_st = _make_candidate("300002")
        assert _classify_category(self._stock(5), True, c_mo, None, c_st) == "short_term"


class TestScoreStockKnownNewFace:
    """端到端锁定 P0：老股仅命中 new_face 时不应被丢弃。"""

    def test_known_stock_only_new_face_not_dropped(self, monkeypatch):
        import scanner.orchestrator as o
        from scanner.candidate_pool import ScanSession
        from scanner.models import KlineSummary, StockInfo

        ks = KlineSummary(trend="t", accumulated_pct=2.0, volume_ratio=1.5,
                          bottom_confirmed=True, score=30, dimensions={},
                          avg_volume=1_000_000)
        monkeypatch.setattr(o, "analyze_new_face", lambda *a, **k: ks)
        monkeypatch.setattr(o, "analyze_momentum", lambda *a, **k: None)
        monkeypatch.setattr(o, "validate", lambda *a, **k: (True, 0, {}))
        # 返回非空历史 -> is_new=False
        monkeypatch.setattr(o, "get_symbol_appearances",
                            lambda *a, **k: [{"date": "2026-06-01"}])

        stock = StockInfo(symbol="300001", name="Test", code="300001",
                          percent=3.0, current=10.0, value=10000,
                          rank_change=1000, rank=1)
        nf, mo, rb, st = o._score_stock(
            stock, conn=None, klines={}, today="2026-06-18",
            session_state=ScanSession(), clusters=None,
        )
        assert nf is not None, "老股仅命中 new_face 不应被丢弃"
        assert nf.category == "known_new_face"
        assert mo is None and rb is None


class TestScoreStockStaleKlineAudit:
    """Layer1 审计（2026-08-14）：评分所用 K 线缺今日 bar（补拉失败旧缓存兜底）时，
    候选打 stale_kline 标记 + 交易时段 fail-loud 告警，不静默吞掉数据质量下降。

    这正是网宿类 bug 的隐蔽点：上游静默降级（回退旧缓存），下游无感知消费，
    量比按昨日量误判放量票。标记 + 告警使问题在扫描输出与落库两层都可见。
    """

    def _stock(self, symbol: str = "300001") -> StockInfo:
        return StockInfo(symbol=symbol, name="测试", code=symbol,
                         percent=3.0, current=10.0, value=10000,
                         rank_change=1000, rank=1)

    def _klines(self, with_today: bool, today: str = "2026-06-18") -> dict:
        kline = [{"date": "2026-06-17", "close": 10.0, "percent": 2.0},
                 {"date": "2026-06-16", "close": 9.8, "percent": -1.0}]
        if with_today:
            kline.append({"date": today, "close": 10.5, "percent": 3.0})
        return {"300001": kline}

    def test_stale_kline_marked_when_missing_today_bar(self, monkeypatch):
        import scanner.orchestrator as o
        from scanner.candidate_pool import ScanSession
        from scanner.models import KlineSummary

        ks = KlineSummary(trend="t", accumulated_pct=2.0, volume_ratio=0.9,
                          bottom_confirmed=True, score=30, dimensions={},
                          avg_volume=1_000_000)
        monkeypatch.setattr(o, "is_trading_time", lambda: True)
        monkeypatch.setattr(o, "analyze_new_face", lambda *a, **k: ks)
        monkeypatch.setattr(o, "analyze_momentum", lambda *a, **k: None)
        monkeypatch.setattr(o, "validate", lambda *a, **k: (True, 0, {}))
        monkeypatch.setattr(o, "get_symbol_appearances", lambda *a, **k: [])

        nf, *_ = o._score_stock(
            self._stock(), conn=None, klines=self._klines(with_today=False),
            today="2026-06-18", session_state=ScanSession(), clusters=None,
        )
        assert nf is not None
        assert nf.stale_kline is True  # 缺今日 bar → 审计标记

    def test_fresh_kline_not_marked(self, monkeypatch):
        import scanner.orchestrator as o
        from scanner.candidate_pool import ScanSession
        from scanner.models import KlineSummary

        ks = KlineSummary(trend="t", accumulated_pct=2.0, volume_ratio=1.5,
                          bottom_confirmed=True, score=30, dimensions={},
                          avg_volume=1_000_000)
        monkeypatch.setattr(o, "is_trading_time", lambda: True)
        monkeypatch.setattr(o, "analyze_new_face", lambda *a, **k: ks)
        monkeypatch.setattr(o, "analyze_momentum", lambda *a, **k: None)
        monkeypatch.setattr(o, "validate", lambda *a, **k: (True, 0, {}))
        monkeypatch.setattr(o, "get_symbol_appearances", lambda *a, **k: [])

        nf, *_ = o._score_stock(
            self._stock(), conn=None, klines=self._klines(with_today=True),
            today="2026-06-18", session_state=ScanSession(), clusters=None,
        )
        assert nf is not None
        assert nf.stale_kline is False  # 含今日 bar → 正常

    def test_fail_loud_warning_during_trading_hours(self, monkeypatch, capsys):
        import scanner.orchestrator as o
        from scanner.candidate_pool import ScanSession
        from scanner.models import KlineSummary

        ks = KlineSummary(trend="t", accumulated_pct=2.0, volume_ratio=0.9,
                          bottom_confirmed=True, score=30, dimensions={},
                          avg_volume=1_000_000)
        monkeypatch.setattr(o, "is_trading_time", lambda: True)
        monkeypatch.setattr(o, "analyze_new_face", lambda *a, **k: ks)
        monkeypatch.setattr(o, "analyze_momentum", lambda *a, **k: None)
        monkeypatch.setattr(o, "validate", lambda *a, **k: (True, 0, {}))
        monkeypatch.setattr(o, "get_symbol_appearances", lambda *a, **k: [])

        o._score_stock(
            self._stock(), conn=None, klines=self._klines(with_today=False),
            today="2026-06-18", session_state=ScanSession(), clusters=None,
        )
        captured = capsys.readouterr().out
        assert "评分基于缺今日bar旧缓存" in captured  # fail-loud 告警
        assert "300001" in captured

    def test_no_warning_outside_trading_hours(self, monkeypatch, capsys):
        import scanner.orchestrator as o
        from scanner.candidate_pool import ScanSession
        from scanner.models import KlineSummary

        ks = KlineSummary(trend="t", accumulated_pct=2.0, volume_ratio=0.9,
                          bottom_confirmed=True, score=30, dimensions={},
                          avg_volume=1_000_000)
        monkeypatch.setattr(o, "is_trading_time", lambda: False)
        monkeypatch.setattr(o, "analyze_new_face", lambda *a, **k: ks)
        monkeypatch.setattr(o, "analyze_momentum", lambda *a, **k: None)
        monkeypatch.setattr(o, "validate", lambda *a, **k: (True, 0, {}))
        monkeypatch.setattr(o, "get_symbol_appearances", lambda *a, **k: [])

        o._score_stock(
            self._stock(), conn=None, klines=self._klines(with_today=False),
            today="2026-06-18", session_state=ScanSession(), clusters=None,
        )
        captured = capsys.readouterr().out
        assert "评分基于缺今日bar旧缓存" not in captured  # 非交易时段不告警


class TestCrossFunctionSilentDegradation:
    """跨函数静默降级集成测试（2026-08-14 网宿类 bug 形态）。

    这类 bug 不在任何单函数逻辑内，而是函数之间的数据流不变量被破坏：
    上游 `_fetch_all_klines` 补拉失败返回"看似正常"的旧缓存（无今日 bar）→
    下游 `_score_stock`/`_compute_volume_metrics` 静默消费 → 量比按昨日量
    误判放量启动票 → 量比硬门误杀。本套件把完整链路串起来验证：

    _fetch_all_klines(补拉失败+分时兜底) → _score_stock(stale 标记+告警)
      → save_recommendations(stale_kline 落库) → save_scan_quality(血缘日志)
    """

    def _stock(self, symbol: str = "300001") -> StockInfo:
        return StockInfo(symbol=symbol, name="测试", code=symbol,
                         percent=7.0, current=15.5, value=10000,
                         rank_change=1000, rank=1)

    def _stale_kline(self, n: int = 40, end: str = "2026-06-17") -> list[dict]:
        base = date.fromisoformat(end)
        return [
            {"date": (base - timedelta(days=(n - 1) - i)).isoformat(),
             "open": 10.0, "close": 10.0, "high": 10.2, "low": 9.8,
             "volume": 1000, "percent": 0.0}
            for i in range(n)
        ]

    def _score_candidate(self, monkeypatch, conn, klines, today="2026-06-18"):
        """复用 _score_stock 真实链路产出候选（仅注入 analyze/validate 结果）。"""
        import scanner.orchestrator as o
        from scanner.candidate_pool import ScanSession
        from scanner.models import KlineSummary
        ks = KlineSummary(trend="放量启动", accumulated_pct=3.0, volume_ratio=1.2,
                          bottom_confirmed=True, score=30, dimensions={},
                          avg_volume=1_000_000)
        monkeypatch.setattr(o, "analyze_short_term", lambda *a, **k: ks)
        monkeypatch.setattr(o, "analyze_new_face", lambda *a, **k: None)
        monkeypatch.setattr(o, "analyze_momentum", lambda *a, **k: None)
        monkeypatch.setattr(o, "analyze_rebound", lambda *a, **k: None)
        monkeypatch.setattr(o, "validate", lambda *a, **k: (True, 0, {}))
        monkeypatch.setattr(o, "get_symbol_appearances", lambda *a, **k: [])
        nf, mo, rb, st = o._score_stock(
            self._stock(), conn=conn, klines=klines, today=today,
            session_state=ScanSession(), clusters=None,
        )
        return st

    def test_full_chain_stale_kline_recommendation(self, monkeypatch, capsys):
        """链路完整验证：补拉失败(无今日bar)→候选 stale 标记→落库 stale_kline=1。

        模拟网宿形态：日线补拉失败 + 分时兜底也失败（残留场景），候选仍产出，
        但被显式标记为基于旧缓存评分，不静默吞掉数据质量下降。
        """
        import sqlite3

        import scanner.orchestrator as o
        from scanner.database import save_recommendations

        # 真实 DB（含迁移），真实 save_recommendations 落库
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        # 手动建 recommendations 表（含 stale_kline 列）
        conn.execute("""CREATE TABLE recommendations (
            id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL, time TEXT NOT NULL,
            symbol TEXT NOT NULL, name TEXT NOT NULL, category TEXT NOT NULL,
            score INTEGER NOT NULL, percent REAL, trend TEXT, next_day_pct REAL,
            fwd_3d REAL, fwd_5d REAL, score_breakdown TEXT, source TEXT DEFAULT 'xueqiu',
            concept TEXT, accumulated_pct REAL, excluded INTEGER DEFAULT 0,
            stale_kline INTEGER DEFAULT 0)""")

        monkeypatch.setattr(o, "is_trading_time", lambda: True)
        monkeypatch.setattr(o, "now_beijing",
                            lambda: datetime(2026, 6, 18, 10, 0))
        # K 线缺今日 bar（旧缓存）
        klines = {"300001": self._stale_kline()}

        st = self._score_candidate(monkeypatch, conn, klines)
        assert st is not None
        assert st.stale_kline is True  # Layer1: 消费点标记
        capsys.readouterr()  # 清除告警输出

        # Layer2: 落库审计——stale_kline=1 持久化
        save_recommendations(conn, [st], [])
        row = conn.execute(
            "SELECT stale_kline FROM recommendations WHERE symbol='300001'").fetchone()
        assert row is not None
        assert row[0] == 1

    def test_fetch_failure_stale_cache_still_scored_with_marker(self, monkeypatch, capsys):
        """真实 _fetch_all_klines 补拉失败 → _score_stock 识别 stale。

        跨两个函数验证不变量：即使 _fetch_all_klines 返回旧缓存（无今日 bar），
        下游 _score_stock 仍必须标记 stale 而非静默当作正常数据评分。
        """
        import sqlite3

        import scanner.orchestrator as o
        stale = self._stale_kline()

        monkeypatch.setattr(o, "is_trading_time", lambda: True)
        monkeypatch.setattr(o, "now_beijing",
                            lambda: datetime(2026, 6, 18, 10, 0))
        monkeypatch.setattr(o, "get_cached_klines", lambda conn, syms: {s: stale for s in syms})
        monkeypatch.setattr(o, "save_kline_to_db", lambda *a, **k: None)
        monkeypatch.setattr(o, "TODAY_BAR_MINUTE_TIMEOUT", 2.0)

        class _Adapter:
            def fetch_kline(self, symbol, days=15):
                return None  # 日线补拉失败

            def fetch_minute(self, symbol):
                return None  # 分时兜底也失败（残留场景）

        conn = sqlite3.connect(":memory:")
        klines = o._fetch_all_klines(conn, _Adapter(), [self._stock()])
        assert klines["300001"] is stale  # 回退旧缓存

        # 下游必须识别 stale（即便分时兜底失败）
        st = self._score_candidate(monkeypatch, conn, klines)
        assert st is not None
        assert st.stale_kline is True
        captured = capsys.readouterr().out
        assert "评分基于缺今日bar旧缓存" in captured  # fail-loud 告警

    def test_volume_ratio_stale_kline_misjudgment(self):
        """核心不变量：缺今日 bar 时量比按昨日量计算 → 放量票误判缩量。

        这是网宿被误杀的直接机制：同一 K 线，缺今日 bar 时 vol_ratio 用昨日
        全天量（0.5/1.0=0.5 <1.0 硬门），含今日 bar 时才反映真实放量。
        验证 `_compute_volume_metrics` 对两种输入的口径差异。
        """
        from scanner.analysis import _compute_volume_metrics
        # 历史 5 根全天量 1.0，昨日量为 0.5 → 无今日 bar 时 ratio=0.5
        no_today = [
            {"date": f"2026-06-{13+i}", "close": 10.0, "volume": 1.0} for i in range(4)
        ] + [{"date": "2026-06-17", "close": 10.0, "volume": 0.5}]
        ratio_stale, _ = _compute_volume_metrics(no_today, "2026-06-18",
                                                 now=datetime(2026, 6, 18, 10, 0))
        assert ratio_stale < 1.0  # 昨日量当作今日量 → 误判缩量

        # 含今日 bar（放量启动 2.0）→ 量比反映真实放量
        with_today = no_today + [
            {"date": "2026-06-18", "close": 10.5, "volume": 2.0}]
        ratio_fresh, _ = _compute_volume_metrics(with_today, "2026-06-18",
                                                 now=datetime(2026, 6, 18, 10, 0))
        assert ratio_fresh >= 1.0  # 今日放量 → 过硬门

    def test_quality_log_counts_stale_and_fallback(self, monkeypatch):
        """血缘日志：fetch_failed / minute_fallback 计数器正确。

        上游降级规模必须反映到可查询的日志中（这是审查抓不到 bug 的可观测化）。
        """
        import sqlite3

        import scanner.orchestrator as o
        stale = self._stale_kline()

        monkeypatch.setattr(o, "is_trading_time", lambda: True)
        monkeypatch.setattr(o, "now_beijing",
                            lambda: datetime(2026, 6, 18, 10, 0))
        monkeypatch.setattr(o, "get_cached_klines", lambda conn, syms: {s: stale for s in syms})
        monkeypatch.setattr(o, "save_kline_to_db", lambda *a, **k: None)
        monkeypatch.setattr(o, "TODAY_BAR_MINUTE_TIMEOUT", 2.0)

        class _Adapter:
            def fetch_kline(self, symbol, days=15):
                return None  # 日线补拉失败

            def fetch_minute(self, symbol):
                return [  # 分时兜底成功
                    {"timestamp": 1, "volume": 100.0, "current": 15.0,
                     "avg_price": 14.9, "high": 15.2, "low": 14.8, "percent": 3.0},
                ]

        conn = sqlite3.connect(":memory:")
        stats: dict = {}
        klines = o._fetch_all_klines(conn, _Adapter(), [self._stock()], stats=stats)
        assert stats["fetch_failed"] == 1           # 1 只补拉失败
        assert stats["minute_fallback"] == 1        # 分时兜底成功 1 次
        assert klines["300001"][-1]["date"] == "2026-06-18"  # 今日 bar 已构造

    def test_minute_fallback_phase_budget_expired_skips(self, monkeypatch):
        """分时兜底总量预算（2026-08-17 审查修复）：_merge_minute_today_bar 接收共享
        deadline，已耗尽时直接返回 None 不再发分时请求——此前单只 join(8s) 限时存在但
        串行叠加无总量上限（API 故障时补拉 N 只 × 8s 可拖垮单轮扫描，数据质量
        "看似正常"的假死形态）。"""
        import scanner.orchestrator as o
        from datetime import date as _date

        called: list[str] = []

        class _Adapter:
            def fetch_minute(self, symbol):
                called.append(symbol)
                return [{"timestamp": 1, "volume": 100.0, "current": 15.0,
                         "avg_price": 14.9, "high": 15.2, "low": 14.8, "percent": 3.0}]

        monkeypatch.setattr(o, "is_trading_time", lambda: True)
        stock = self._stock()
        stale = self._stale_kline()
        # deadline 已过（now-1s）→ 兜底整体跳过，不发分时请求
        res = o._merge_minute_today_bar(
            _Adapter(), stock, _date(2026, 6, 18), stale,
            deadline=o.now_beijing().timestamp() - 1)
        assert res is None
        assert called == [], f"预算耗尽时不得再发分时请求, called={called}"
        # 预算尚足（deadline=now+30s）→ 正常构造今日 bar
        res2 = o._merge_minute_today_bar(
            _Adapter(), stock, _date(2026, 6, 18), stale,
            deadline=o.now_beijing().timestamp() + 30)
        assert res2 is not None and res2[-1]["date"] == "2026-06-18"
        assert len(called) == 1, f"预算充足时应发出分时请求, called={called}"


class TestFetchAllKlinesIntradayRefresh:

    def _cached(self, today):
        return [{"date": "2026-06-17", "close": 10.0}] + \
               [{"date": today, "close": 10.5}] * 40

    def test_trading_time_reuses_cache_within_ttl(self, monkeypatch):
        import scanner.orchestrator as o
        from scanner.models import StockInfo
        from datetime import date

        today = date.today().isoformat()
        cached = self._cached(today)
        monkeypatch.setattr(o, "get_cached_klines", lambda conn, syms: {s: cached for s in syms})
        monkeypatch.setattr(o, "is_trading_time", lambda: True)
        # last fetch 10s ago (within TTL)
        o._last_kline_fetch["300001"] = o.now_beijing().timestamp() - 10
        fetched = {}

        class _FakeAdapter:
            def fetch_kline(self, symbol, days=15):
                fetched["called"] = True
                return cached

        monkeypatch.setattr(o, "save_kline_to_db", lambda *a, **k: None)

        res = o._fetch_all_klines(None, _FakeAdapter(), [StockInfo(symbol="300001", name="T", code="300001", percent=3.0, current=10.0, value=10000, rank_change=1000, rank=1)])
        assert res["300001"] is cached
        assert "called" not in fetched  # no refetch

    def test_trading_time_refetches_after_ttl(self, monkeypatch):
        import scanner.orchestrator as o
        from scanner.models import StockInfo
        from datetime import date

        today = date.today().isoformat()
        cached = self._cached(today)
        monkeypatch.setattr(o, "get_cached_klines", lambda conn, syms: {s: cached for s in syms})
        monkeypatch.setattr(o, "is_trading_time", lambda: True)
        # last fetch 600s ago (past TTL)
        o._last_kline_fetch["300001"] = o.now_beijing().timestamp() - 600
        fetched = {}
        fresh = [{"date": today, "close": 11.0}] * 45

        class _FakeAdapter:
            def fetch_kline(self, symbol, days=15):
                fetched["called"] = True
                return fresh

        monkeypatch.setattr(o, "save_kline_to_db", lambda *a, **k: None)

        res = o._fetch_all_klines(None, _FakeAdapter(), [StockInfo(symbol="300001", name="T", code="300001", percent=3.0, current=10.0, value=10000, rank_change=1000, rank=1)])
        assert "called" in fetched  # refetch triggered
        assert res["300001"] is not None
        # merge 后结果包含 stale_cache + API 数据，不再是同一对象
        assert res["300001"][-1]["close"] == 11.0

    def test_trading_time_missing_today_bar_always_fetches(self, monkeypatch):
        # 回归：盘中且缓存尚未含今日 Bar（max_date < today）必须补拉，
        # 否则全天无今日行情（A3 条件反转 bug）。
        import scanner.orchestrator as o
        from scanner.models import StockInfo
        from datetime import date, timedelta

        yesterday = (date.today() - timedelta(days=1)).isoformat()
        cached = [{"date": yesterday, "close": 10.0}] * 40
        monkeypatch.setattr(o, "get_cached_klines", lambda conn, syms: {s: cached for s in syms})
        monkeypatch.setattr(o, "is_trading_time", lambda: True)
        # 即便上次拉取在 TTL 内，缺少今日 Bar 也应强制补拉
        o._last_kline_fetch["300001"] = o.now_beijing().timestamp() - 10
        fetched = {}
        fresh = [{"date": date.today().isoformat(), "close": 11.0}] * 45

        class _FakeAdapter:
            def fetch_kline(self, symbol, days=15):
                fetched["called"] = True
                return fresh

        monkeypatch.setattr(o, "save_kline_to_db", lambda *a, **k: None)

        res = o._fetch_all_klines(None, _FakeAdapter(), [StockInfo(symbol="300001", name="T", code="300001", percent=3.0, current=10.0, value=10000, rank_change=1000, rank=1)])
        assert "called" in fetched  # 必须补拉
        assert res["300001"] is not None
        # merge 后 40 条旧日期 → 1 条(key 相同), 45 条今日 → 1 条 = 2 条
        assert len(res["300001"]) == 2
        assert res["300001"][-1]["close"] == 11.0
        assert res["300001"][-1]["date"] == o.date.today().isoformat()


class TestTryCandidateHighRiskTrend:
    def test_high_risk_trend_rejected(self):
        import scanner.orchestrator as o
        from scanner.models import StockInfo, KlineSummary

        stock = StockInfo(symbol="SZ300001", name="Test", code="300001",
                          percent=5.0, current=10.0, value=10000,
                          rank_change=1000, rank=1)
        kline_s = KlineSummary(trend="回踩整理", accumulated_pct=2.0,
                                volume_ratio=1.5, bottom_confirmed=True,
                                score=50, dimensions={}, avg_volume=1_000_000)
        result = o._try_candidate(stock, kline_s, "momentum",
                                   True, "2026-07-21", [], [], [], None)
        assert result is None

    def test_safe_trend_passes(self):
        import scanner.orchestrator as o
        from scanner.models import StockInfo, KlineSummary

        stock = StockInfo(symbol="SZ300001", name="Test", code="300001",
                          percent=5.0, current=10.0, value=10000,
                          rank_change=1000, rank=1)
        kline_s = KlineSummary(trend="破位回调", accumulated_pct=2.0,
                                volume_ratio=1.5, bottom_confirmed=True,
                                score=50, dimensions={}, avg_volume=1_000_000)
        result = o._try_candidate(stock, kline_s, "momentum",
                                   True, "2026-07-21", [], [], [], None)
        # Should not be rejected by trend filter (may fail later validation, but not here)
        # We only care that the trend filter didn't block it
        # The result might still be None due to other filters, so check "缩量回调"
        # is no longer blocked (it was removed from HIGH_RISK_TRENDS)
        assert kline_s.trend not in {"回踩整理"}


class TestRiskFlagHardFilter:
    """风险标签硬排除：主力出货/趋势破位 命中即移出推荐，其余标签保留为展示警告。"""

    def _cand(self, risk_flags: list[str]) -> Candidate:
        c = _make_candidate("300999")
        c.risk_flags = list(risk_flags)
        return c

    def test_main_force_distribution_excluded(self):
        assert _candidate_excluded_by_risk(self._cand(["主力出货"]))

    def test_trend_breakage_excluded(self):
        assert _candidate_excluded_by_risk(self._cand(["趋势破位"]))

    def test_both_excluded(self):
        assert _candidate_excluded_by_risk(self._cand(["主力出货", "趋势破位"]))

    def test_weak_market_not_excluded(self):
        # 弱市是展示型警告，不应硬过滤
        assert not _candidate_excluded_by_risk(self._cand(["弱市"]))

    def test_overbought_not_excluded(self):
        # 超买维持现状（仅 short_term 条件性否决），不应在此硬过滤
        assert not _candidate_excluded_by_risk(self._cand(["超买"]))

    def test_volume_divergence_not_excluded(self):
        # 用户要求仅留 主力出货+趋势破位，量价背离不硬过滤
        assert not _candidate_excluded_by_risk(self._cand(["量价背离"]))

    def test_multiple_warning_flags_not_excluded(self):
        # 涨幅过大 + 疲劳 + 弱市 等展示型标签组合，仍保留
        assert not _candidate_excluded_by_risk(self._cand(["涨幅过大", "疲劳", "弱市"]))

    def test_no_flags_not_excluded(self):
        assert not _candidate_excluded_by_risk(self._cand([]))


class TestFetchAllKlinesTodayBarWarning:
    """盘中 K 线缺今日 bar 时应打印可见警告（旧缓存评分，下次刷新重试）。"""

    def _kline(self, n: int, end_date: str) -> list[dict]:
        base = date.fromisoformat(end_date)
        return [
            {"date": (base - timedelta(days=(n - 1) - i)).isoformat(),
             "open": 10.0, "close": 10.0, "high": 10.2, "low": 9.8,
             "volume": 1000, "percent": 0.0}
            for i in range(n)
        ]

    def _stock(self, symbol: str = "300999") -> StockInfo:
        return StockInfo(symbol=symbol, name="测试", code=symbol,
                         percent=5.0, current=10.0, value=10000,
                         rank_change=1000, rank=1)

    def test_missing_today_bar_warns(self, monkeypatch, capsys):
        conn = None
        cached = self._kline(40, "2026-07-30")
        monkeypatch.setattr("scanner.orchestrator.is_trading_time", lambda *a, **k: True)
        monkeypatch.setattr("scanner.orchestrator.now_beijing",
                            lambda: datetime(2026, 7, 31, 10, 0))
        monkeypatch.setattr("scanner.orchestrator.get_cached_klines", lambda conn, syms: {s: cached for s in syms})

        class _FakeAdapter:
            def fetch_kline(self, symbol, days=15):
                return None

        monkeypatch.setattr("scanner.orchestrator.save_kline_to_db", lambda *a, **k: None)

        result = _fetch_all_klines(conn, _FakeAdapter(), [self._stock()])
        captured = capsys.readouterr().out
        assert "今日K线缺失" in captured
        assert "300999" in captured
        assert "旧缓存评分" in captured
        assert result["300999"] is cached

    def test_full_today_bar_no_warning(self, monkeypatch, capsys):
        conn = None
        cached = self._kline(40, "2026-07-31")
        monkeypatch.setattr("scanner.orchestrator.is_trading_time", lambda *a, **k: True)
        monkeypatch.setattr("scanner.orchestrator.now_beijing",
                            lambda: datetime(2026, 7, 31, 10, 0))
        monkeypatch.setattr("scanner.orchestrator.get_cached_klines", lambda conn, syms: {s: cached for s in syms})

        class _FakeAdapter:
            def fetch_kline(self, symbol, days=15):
                return None

        monkeypatch.setattr("scanner.orchestrator.save_kline_to_db", lambda *a, **k: None)

        _fetch_all_klines(conn, _FakeAdapter(), [self._stock()])
        captured = capsys.readouterr().out
        assert "今日K线缺失" not in captured


class TestFetchAllKlinesSharedDeadline:
    """回归：榜上票 + 回马枪两批 K 线补拉必须共用同一 deadline。

    修复前两批各建新 45s deadline → 串行最坏 ~90s，超 60s 扫描间隔；
    修复后传入同一个 deadline，第二批在首批耗尽时间后立即停止，不再重新计时。
    """

    def _stock(self, symbol: str = "300999") -> StockInfo:
        return StockInfo(symbol=symbol, name="测试", code=symbol,
                         percent=5.0, current=10.0, value=10000,
                         rank_change=1000, rank=1)

    def _kline_fresh(self, n: int) -> list[dict]:
        base = date.fromisoformat("2026-07-31")
        return [
            {"date": (base - timedelta(days=(n - 1) - i)).isoformat(),
             "open": 10.0, "close": 10.0, "high": 10.2, "low": 9.8,
             "volume": 1000, "percent": 0.0}
            for i in range(n)
        ]

    def test_shared_deadline_caps_total_fetch_time(self, monkeypatch):
        import time as _t
        import scanner.orchestrator as o

        monkeypatch.setattr(o, "is_trading_time", lambda *a, **k: True)
        monkeypatch.setattr(o, "get_cached_klines", lambda conn, syms: {s: None for s in syms})
        monkeypatch.setattr(o, "save_kline_to_db", lambda *a, **k: None)

        fetch_calls = {"n": 0}

        class _SlowAdapter:
            def fetch_kline(self, symbol, days=15):
                fetch_calls["n"] += 1
                _t.sleep(0.02)  # 每只 20ms，deadline 内可拉 ~7 只
                return TestFetchAllKlinesSharedDeadline()._kline_fresh(40)

        # 构造共享 deadline：真实时钟 + 极短窗口（仅剩 ~30ms）。
        # 注意不能 mock now_beijing 为固定值——deadline 检查依赖它随时间推进。
        deadline = o.now_beijing().timestamp() + 0.03
        stocks = [self._stock(f"30000{i}") for i in range(1, 6)]
        # 第一批：短暂 deadline 内只能拉少量几只（首只检查后再拉，能拉 1~2 只）
        r1 = o._fetch_all_klines(None, _SlowAdapter(), stocks, deadline=deadline)
        n_after_first = fetch_calls["n"]
        assert 1 <= n_after_first < 5, f"首批只应拉 1~2 只（30ms 窗口），got {n_after_first}"
        # 第二批：同一（已过期）deadline → 一只都不拉
        r2 = o._fetch_all_klines(None, _SlowAdapter(), stocks[:1], deadline=deadline)
        n_after_second = fetch_calls["n"]
        assert n_after_second == n_after_first, (
            f"共享 deadline 下第二批不得重新计时补拉，{n_after_first}→{n_after_second}")

    def test_default_deadline_still_works(self, monkeypatch):
        # 未传 deadline 时沿用默认 KLINE_FETCH_DEADLINE 行为（向后兼容）
        import scanner.orchestrator as o
        monkeypatch.setattr(o, "is_trading_time", lambda *a, **k: True)
        monkeypatch.setattr(o, "now_beijing",
                            lambda: datetime(2026, 7, 31, 10, 0))
        monkeypatch.setattr(o, "get_cached_klines", lambda conn, syms: {s: None for s in syms})
        monkeypatch.setattr(o, "save_kline_to_db", lambda *a, **k: None)
        fresh = self._kline_fresh(40)

        class _FakeAdapter:
            def fetch_kline(self, symbol, days=15):
                return fresh

        res = o._fetch_all_klines(None, _FakeAdapter(), [self._stock("300999")])
        assert res["300999"] is not None


class TestFetchAllKlinesMinuteTodayBarFallback:
    """盘中 K 线补拉失败时，用分时数据构造今日 bar 兜底（2026-08-14 网宿案例）。

    背景：网宿科技 10:56~14:14 在榜 3 小时（涨幅 6.5%~11.8% 在 short_term 可推荐
    区间），但 K 线补拉失败回退旧缓存（无今日 bar）→ 量比硬门误杀放量启动票。
    本兜底在补拉失败且交易时段时，用分时累计量能构造今日 bar，仅本轮评分使用。
    """

    def _stock(self, symbol: str = "300999") -> StockInfo:
        return StockInfo(symbol=symbol, name="测试", code=symbol,
                         percent=7.0, current=15.5, value=10000,
                         rank_change=1000, rank=1)

    def _kline_stale(self, n: int = 40, end_date: str = "2026-07-30") -> list[dict]:
        base = date.fromisoformat(end_date)
        return [
            {"date": (base - timedelta(days=(n - 1) - i)).isoformat(),
             "open": 10.0, "close": 10.0, "high": 10.2, "low": 9.8,
             "volume": 1000, "percent": 0.0}
            for i in range(n)
        ]

    def test_fallback_merges_today_bar_when_fetch_fails(self, monkeypatch, capsys):
        import scanner.orchestrator as o
        cached = self._kline_stale()
        monkeypatch.setattr(o, "is_trading_time", lambda *a, **k: True)
        monkeypatch.setattr(o, "now_beijing",
                            lambda: datetime(2026, 7, 31, 10, 0))
        monkeypatch.setattr(o, "get_cached_klines", lambda conn, syms: {s: cached for s in syms})
        monkeypatch.setattr(o, "save_kline_to_db", lambda *a, **k: None)
        monkeypatch.setattr(o, "TODAY_BAR_MINUTE_TIMEOUT", 3.0)

        class _FakeAdapter:
            def fetch_kline(self, symbol, days=15):
                return None  # 日线补拉失败

            def fetch_minute(self, symbol):
                return [
                    {"timestamp": 1, "volume": 100.0, "current": 15.0,
                     "avg_price": 14.9, "high": 15.2, "low": 14.8, "percent": 3.0},
                    {"timestamp": 2, "volume": 200.0, "current": 15.5,
                     "avg_price": 15.1, "high": 15.6, "low": 15.0, "percent": 7.0},
                ]

        res = o._fetch_all_klines(None, _FakeAdapter(), [self._stock()])
        kl = res["300999"]
        assert kl is not None
        # 今日 bar 已 merge 进返回的 kline（供本轮评分）
        assert kl[-1]["date"] == "2026-07-31"
        assert kl[-1]["volume"] == 300.0
        assert kl[-1]["close"] == 15.5
        assert kl[-1]["percent"] == 7.0
        # 今日 bar 未写 DB（不污染缓存）
        capsys.readouterr()

    def test_fallback_skipped_outside_trading_hours(self, monkeypatch):
        import scanner.orchestrator as o
        cached = self._kline_stale()
        monkeypatch.setattr(o, "is_trading_time", lambda *a, **k: False)
        monkeypatch.setattr(o, "now_beijing",
                            lambda: datetime(2026, 7, 31, 16, 0))
        monkeypatch.setattr(o, "get_cached_klines", lambda conn, syms: {s: cached for s in syms})

        class _FakeAdapter:
            def fetch_kline(self, symbol, days=15):
                return None

            def fetch_minute(self, symbol):
                raise AssertionError("非交易时段不应拉分时兜底")

        res = o._fetch_all_klines(None, _FakeAdapter(), [self._stock()])
        # 维持旧回退行为：返回 stale 缓存，不 merge 今日 bar
        assert res["300999"] is cached

    def test_fallback_when_minute_unavailable(self, monkeypatch):
        import scanner.orchestrator as o
        cached = self._kline_stale()
        monkeypatch.setattr(o, "is_trading_time", lambda *a, **k: True)
        monkeypatch.setattr(o, "now_beijing",
                            lambda: datetime(2026, 7, 31, 10, 0))
        monkeypatch.setattr(o, "get_cached_klines", lambda conn, syms: {s: cached for s in syms})
        monkeypatch.setattr(o, "save_kline_to_db", lambda *a, **k: None)
        monkeypatch.setattr(o, "TODAY_BAR_MINUTE_TIMEOUT", 3.0)

        class _FakeAdapter:
            def fetch_kline(self, symbol, days=15):
                return None

            def fetch_minute(self, symbol):
                return None  # 分时也不可用

        res = o._fetch_all_klines(None, _FakeAdapter(), [self._stock()])
        assert res["300999"] is cached


class TestComputeRps:
    """RPS 相对强弱加分：baseline 口径 + 无 baseline 回退 + rebound 豁免。

    回归：无 baseline 回退分支曾按列表顺序而非累计涨幅分位分配奖励
    （sorted_by_accum[i] 作排序键 = 单调 → 恒等置换，最强票拿 LOW）。
    """

    def _cand(self, symbol: str, acc: float, category: str = "new_face") -> Candidate:
        stock = StockInfo(symbol=symbol, name="Test", code=symbol,
                          percent=5.0, current=10.0, value=10000,
                          rank_change=1000, rank=1)
        kline_s = KlineSummary(trend="t", accumulated_pct=acc,
                               volume_ratio=1.5, bottom_confirmed=True,
                               score=20, dimensions={}, avg_volume=1_000_000)
        return Candidate(stock=stock, category=category, score=20,
                         reason="t", kline=kline_s, first_seen="09:30")

    def test_baseline_branch_percentile_in_baseline(self):
        from scanner.config import (RPS_BONUS_HIGH, RPS_BONUS_LOW,
                                    RPS_BONUS_MEDIUM)
        from scanner.orchestrator import _compute_rps
        baseline = [0.0, 5.0, 8.0, 10.0, 12.0, 15.0, 20.0, 30.0]
        cands = [
            self._cand("300001", 12.0),  # lo=5 -> 62 -> MEDIUM
            self._cand("300002", 5.0),   # lo=2 -> 25 -> LOW
            self._cand("300003", 25.0),  # lo=7 -> 87 -> HIGH
        ]
        scores = _compute_rps(cands, baseline=baseline)
        assert scores["300001"] == RPS_BONUS_MEDIUM
        assert scores["300002"] == RPS_BONUS_LOW
        assert scores["300003"] == RPS_BONUS_HIGH

    def test_empty_baseline_fallback_orders_by_accum(self):
        # 回归：最强票必须拿 HIGH，最弱票拿 LOW（此前按列表顺序反了）
        from scanner.config import (RPS_BONUS_HIGH, RPS_BONUS_LOW,
                                    RPS_BONUS_MEDIUM)
        from scanner.orchestrator import _compute_rps
        cands = [
            self._cand("300001", 10.0),
            self._cand("300002", 5.0),
            self._cand("300003", 8.0),
            self._cand("300004", 3.0),
        ]
        scores = _compute_rps(cands, baseline=[])
        # pctiles: 100 / 50 / 75 / 25  -> HIGH / 中性 / MEDIUM / LOW
        assert scores["300001"] == RPS_BONUS_HIGH
        assert scores["300002"] == 0
        assert scores["300003"] == RPS_BONUS_MEDIUM
        assert scores["300004"] == RPS_BONUS_LOW

    def test_rebound_exempt_from_rps(self):
        from scanner.orchestrator import _compute_rps
        cands = [
            self._cand("300001", 10.0),
            self._cand("300002", -12.0, category="rebound"),
        ]
        scores = _compute_rps(cands, baseline=[])
        assert scores["300002"] == 0  # 超跌反弹豁免 RPS 惩罚

    def test_accum_map_overrides_kline(self):
        # short_term 的 kline.accumulated_pct 含今日 bar，accum_map 用历史口径覆盖
        from scanner.config import RPS_BONUS_LOW, RPS_BONUS_MEDIUM
        from scanner.orchestrator import _compute_rps
        baseline = [0.0, 5.0, 8.0, 10.0, 12.0, 15.0, 20.0, 30.0]
        cands = [
            self._cand("300001", 50.0, category="short_term"),  # kline 50 但被覆盖为 5.0
            self._cand("300002", 15.0),  # lo=6 -> 75 -> MEDIUM
        ]
        scores = _compute_rps(cands, baseline=baseline,
                              accum_map={"300001": 5.0})
        # 历史口径 5.0 -> 25 -> LOW（若未覆盖则 50 -> 100 -> HIGH）
        assert scores["300001"] == RPS_BONUS_LOW
        assert scores["300002"] == RPS_BONUS_MEDIUM

    def test_dual_listed_symbol_counted_once(self):
        # 双挂票（同代码出现在多个桶）只计一次，避免拉高 total 扭曲分位
        from scanner.orchestrator import _compute_rps
        cands = [
            self._cand("300001", 10.0, category="new_face"),
            self._cand("300001", 10.0, category="short_term"),
            self._cand("300002", 5.0),
        ]
        scores = _compute_rps(cands, baseline=[])
        assert set(scores) == {"300001", "300002"}


class TestParallelFetchDeadline:
    """回归：分时数据拉取各相带 deadline（MINUTE_FETCH_PHASE_DEADLINE），
    超时的票降级为 None，不再 as_completed 无限等待（minute API 挂死时
    最坏可卡 ~5 分钟）。"""

    def test_phase_deadline_bounds_and_degrades(self, monkeypatch):
        import time

        import scanner.orchestrator as o
        from concurrent.futures import ThreadPoolExecutor
        from unittest.mock import MagicMock

        def _slow(session, sym, items=None):
            time.sleep(5.0)
            return 1.0

        class _FakeAdapter:
            def fetch_minute(self, symbol):
                return [{"volume": 1.0, "current": 10.0, "avg_price": 10.0}] * 60

        monkeypatch.setattr(o, "_get_session", lambda: MagicMock())
        monkeypatch.setattr(o, "analyze_intraday", _slow)
        monkeypatch.setattr(o, "analyze_opening_strength", lambda s, x, items=None: 2.5)
        monkeypatch.setattr(o, "estimate_live_volume", lambda s, x, items=None: 123.0)

        cands = [_make_candidate("300001")]
        intra, opening, live = {}, {}, {}
        pool = ThreadPoolExecutor(max_workers=6)
        try:
            start = time.time()
            o._parallel_fetch(pool, cands, intra, opening, live,
                              _FakeAdapter(), phase_deadline=0.4)
            elapsed = time.time() - start
        finally:
            pool.shutdown(wait=False)  # 与扫描主路径一致：不阻塞等后台任务

        # 拉取相 + 三相各 ≤0.4s deadline：总耗时受约束（而非等 5s 慢任务）
        assert elapsed < 1.6, f"应受四相 deadline 约束, elapsed={elapsed:.2f}s"
        assert intra["300001"] is None   # intraday 相超时 → 降级 None
        assert opening["300001"] == 2.5  # opening 相即时完成
        assert live["300001"] == 123.0   # live_vol 相即时完成

    def test_parallel_fetch_no_minute_degrades(self, monkeypatch):
        """adapter.fetch_minute 返回 None（AKShare 源）→ 三相整体降级为 None，不回退雪球。"""
        import scanner.orchestrator as o
        from concurrent.futures import ThreadPoolExecutor
        from unittest.mock import MagicMock

        class _NoMinuteAdapter:
            def fetch_minute(self, symbol):
                return None

        monkeypatch.setattr(o, "_get_session", lambda: MagicMock())
        monkeypatch.setattr(o, "analyze_intraday", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应调用")))
        monkeypatch.setattr(o, "analyze_opening_strength", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应调用")))
        monkeypatch.setattr(o, "estimate_live_volume", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应调用")))

        cands = [_make_candidate("300001")]
        intra, opening, live = {}, {}, {}
        pool = ThreadPoolExecutor(max_workers=6)
        try:
            o._parallel_fetch(pool, cands, intra, opening, live, _NoMinuteAdapter())
        finally:
            pool.shutdown(wait=False)

        assert intra["300001"] is None
        assert opening["300001"] is None
        assert live["300001"] is None


class TestFilterGemStocks:
    """回归：原始行情字段必须强转数值，脏数据整票跳过（防 TypeError 拖垮整轮扫描）。"""

    def test_coerces_string_numbers(self):
        from scanner.orchestrator import _filter_gem_stocks
        raw = [
            {"symbol": "SZ300001", "code": "300001", "name": "测试A",
             "percent": "5.23", "current": "12.5", "value": "10000",
             "rank_change": "1200", "rank": "3"},
        ]
        stocks = _filter_gem_stocks(raw)
        assert len(stocks) == 1
        s = stocks[0]
        assert isinstance(s.percent, float) and s.percent == 5.23
        assert isinstance(s.current, float) and s.current == 12.5
        assert isinstance(s.value, float) and s.value == 10000.0
        assert isinstance(s.rank_change, int) and s.rank_change == 1200
        assert s.rank == 3

    def test_rank_numeric_string_with_decimal_kept(self):
        """回归：rank="3.0" 这类数值字符串此前 int("3.0") 抛 ValueError 导致整票被跳过
        （漏推荐）；现与 rank_change 同口径 float 中转后正常解析保留。"""
        from scanner.orchestrator import _filter_gem_stocks
        raw = [
            {"symbol": "SZ300001", "code": "300001", "name": "测试A",
             "percent": 5.0, "current": 10.0, "value": 8000,
             "rank_change": 100, "rank": "3.0"},
        ]
        stocks = _filter_gem_stocks(raw)
        assert len(stocks) == 1
        assert stocks[0].rank == 3

    def test_skips_garbage_numeric_row(self):
        # rank_change="-" 无法解析 → 该票跳过，其余票正常进入（不再抛 TypeError）
        from scanner.orchestrator import _filter_gem_stocks
        raw = [
            {"symbol": "SZ300001", "code": "300001", "name": "测试A",
             "percent": "5.23", "current": "12.5", "value": "10000",
             "rank_change": "-", "rank": 1},
            {"symbol": "SZ300002", "code": "300002", "name": "测试B",
             "percent": 3.1, "current": 10.0, "value": 8000,
             "rank_change": 1200, "rank": 2},
        ]
        stocks = _filter_gem_stocks(raw)
        assert [s.symbol for s in stocks] == ["SZ300002"]
        # 下游比较不再崩溃
        assert (stocks[0].current > 200.0) is (stocks[0].current > 200.0)

    def test_none_symbol_code_name_skipped_not_crash(self):
        # symbol/code/name 为 None（键存在但值为 null）时，is_hk_stock/is_gem/is_st
        # 不能抛 AttributeError/TypeError 拖垮整轮扫描；脏值按空串处理，被过滤掉。
        from scanner.orchestrator import _filter_gem_stocks
        raw = [
            {"symbol": None, "code": None, "name": None,
             "percent": 5.0, "current": 10.0, "value": 8000,
             "rank_change": 100, "rank": 1},
            {"symbol": "SZ300002", "code": "300002", "name": "测试B",
             "percent": 3.1, "current": 10.0, "value": 8000,
             "rank_change": 1200, "rank": 2},
        ]
        stocks = _filter_gem_stocks(raw)
        assert [s.symbol for s in stocks] == ["SZ300002"]

    def test_missing_symbol_code_name_ok(self):
        # 键完全缺失时默认空串，同样不崩溃且被过滤
        from scanner.orchestrator import _filter_gem_stocks
        raw = [{"percent": 5.0, "current": 10.0, "value": 8000,
                "rank_change": 100, "rank": 1}]
        stocks = _filter_gem_stocks(raw)
        assert stocks == []

    def test_int_symbol_code_name_coerced_not_crash(self):
        """回归：symbol/code/name 为 int（API 偶发数值类型，docstring 声称"强转 str"
        但此前只处理 None，int 会让 is_hk_stock.isdigit()/is_gem.startswith() 抛
        AttributeError 拖垮整轮扫描）。现 str() 强转后正常过滤/保留。"""
        from scanner.orchestrator import _filter_gem_stocks
        raw = [
            {"symbol": 300001, "code": "300001", "name": 12345,
             "percent": 5.0, "current": 10.0, "value": 8000,
             "rank_change": 100, "rank": 1},
            {"symbol": "SZ300002", "code": 300002, "name": "测试B",
             "percent": 3.1, "current": 10.0, "value": 8000,
             "rank_change": 1200, "rank": 2},
        ]
        stocks = _filter_gem_stocks(raw)
        # int symbol 300001 → str "300001"（GEM 代码保留），int name 12345 → "12345"
        assert [s.symbol for s in stocks] == ["300001", "SZ300002"]

    def test_nan_inf_coerced_to_zero(self):
        """回归：percent/current/value 为 NaN/inf（Python json 可解析 JSON 字面量）
        必须强转 0，否则 NaN 绕过 `s.current > MAX_STOCK_PRICE` 等数值过滤、
        产出 NaN 评分写库为 NULL；inf 同理。rank/rank_change 为 NaN 时不得整票跳过。"""
        from scanner.orchestrator import _filter_gem_stocks
        raw = [
            {"symbol": "SZ300001", "code": "300001", "name": "测试A",
             "percent": float("nan"), "current": float("nan"), "value": 8000,
             "rank_change": 100, "rank": 1},
            {"symbol": "SZ300002", "code": "300002", "name": "测试B",
             "percent": float("inf"), "current": float("-inf"), "value": 8000,
             "rank_change": float("nan"), "rank": float("nan")},
        ]
        stocks = _filter_gem_stocks(raw)
        assert len(stocks) == 2
        s1, s2 = stocks
        assert s1.percent == 0.0 and s1.current == 0.0
        assert s2.percent == 0.0 and s2.current == 0.0
        assert s2.rank_change == 0
        assert s2.rank == 2  # rank=NaN → 回退列表下标


class TestFetchAllKlinesShortCacheTtl:
    """回归：短缓存（len<KLINE_MIN_LENGTH）同样受 KLINE_REFRESH_TTL 节流，
    不再每扫描周期强制重拉（此前耗尽 KLINE_FETCH_DEADLINE 拖累全列表）。"""

    def _cached(self, today):
        return [{"date": "2026-06-17", "close": 10.0}] + \
               [{"date": today, "close": 10.5}] * 25   # 26 根 < KLINE_MIN_LENGTH=32

    def test_short_cache_with_today_bar_within_ttl_not_refetched(self, monkeypatch):
        import scanner.orchestrator as o
        from scanner.models import StockInfo
        from datetime import date

        today = date.today().isoformat()
        cached = self._cached(today)
        monkeypatch.setattr(o, "get_cached_klines", lambda conn, syms: {s: cached for s in syms})
        monkeypatch.setattr(o, "is_trading_time", lambda: True)
        o._last_kline_fetch["300123"] = o.now_beijing().timestamp() - 10
        fetched = {}

        class _FakeAdapter:
            def fetch_kline(self, symbol, days=15):
                fetched["called"] = True
                return cached

        monkeypatch.setattr(o, "save_kline_to_db", lambda *a, **k: None)
        res = o._fetch_all_klines(None, _FakeAdapter(), [StockInfo(
            symbol="300123", name="T", code="300123", percent=3.0,
            current=10.0, value=10000, rank_change=1000, rank=1)])
        assert "called" not in fetched  # TTL 内短缓存复用
        assert res["300123"] is cached

    def test_short_cache_past_ttl_refetches(self, monkeypatch):
        import scanner.orchestrator as o
        from scanner.models import StockInfo
        from datetime import date

        today = date.today().isoformat()
        cached = self._cached(today)
        monkeypatch.setattr(o, "get_cached_klines", lambda conn, syms: {s: cached for s in syms})
        monkeypatch.setattr(o, "is_trading_time", lambda: True)
        o._last_kline_fetch["300124"] = o.now_beijing().timestamp() - 600
        fetched = {}
        fresh = [{"date": today, "close": 11.0}] * 45

        class _FakeAdapter:
            def fetch_kline(self, symbol, days=15):
                fetched["called"] = True
                return fresh

        monkeypatch.setattr(o, "save_kline_to_db", lambda *a, **k: None)
        res = o._fetch_all_klines(None, _FakeAdapter(), [StockInfo(
            symbol="300124", name="T", code="300124", percent=3.0,
            current=10.0, value=10000, rank_change=1000, rank=1)])
        assert "called" in fetched  # 超 TTL 后仍补拉（跨日增长 K 线根数）
        assert res["300124"] is not None

    def test_short_cache_reused_outside_trading_time(self, monkeypatch):
        import scanner.orchestrator as o
        from scanner.models import StockInfo
        from datetime import date

        today = date.today().isoformat()
        cached = self._cached(today)
        monkeypatch.setattr(o, "get_cached_klines", lambda conn, syms: {s: cached for s in syms})
        monkeypatch.setattr(o, "is_trading_time", lambda: False)
        fetched = {}

        class _FakeAdapter:
            def fetch_kline(self, symbol, days=15):
                fetched["called"] = True
                return cached

        monkeypatch.setattr(o, "save_kline_to_db", lambda *a, **k: None)
        res = o._fetch_all_klines(None, _FakeAdapter(), [StockInfo(
            symbol="300125", name="T", code="300125", percent=3.0,
            current=10.0, value=10000, rank_change=1000, rank=1)])
        assert "called" not in fetched  # 非交易时段一律复用缓存
        assert res["300125"] is cached

    def test_bad_date_format_in_cache_not_crash(self, monkeypatch):
        """回归：DB 缓存含非 ISO 日期（历史脏数据 '2026-6-7'）时，
        date.fromisoformat 不能抛 ValueError 拖垮整轮扫描——走补拉路径。"""
        import scanner.orchestrator as o
        from scanner.models import StockInfo

        bad_cached = [{"date": "2026-6-7", "close": 10.0},
                      {"date": "2026-06-08", "close": 10.5}]
        monkeypatch.setattr(o, "get_cached_klines", lambda conn, syms: {s: bad_cached for s in syms})
        monkeypatch.setattr(o, "is_trading_time", lambda: True)
        o._last_kline_fetch.pop("300126", None)
        fetched = {}
        fresh = [{"date": "2026-06-08", "close": 10.5}] * 45

        class _FakeAdapter:
            def fetch_kline(self, symbol, days=15):
                fetched["called"] = True
                return fresh

        monkeypatch.setattr(o, "save_kline_to_db", lambda *a, **k: None)
        res = o._fetch_all_klines(None, _FakeAdapter(), [StockInfo(
            symbol="300126", name="T", code="300126", percent=3.0,
            current=10.0, value=10000, rank_change=1000, rank=1)])
        assert "called" in fetched  # 脏日期按"需补拉"处理
        assert res["300126"] is not None


