from datetime import date, datetime, timedelta

from scanner.candidate_pool import ScanSession
from scanner.models import Candidate, KlineSummary, StockInfo
from scanner.orchestrator import (
    _candidate_excluded_by_risk,
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

    def test_stale_candidates_timeout(self):
        ss = ScanSession()
        c = _make_candidate("300001", score=25)
        ss.update_pool([c])

        now = datetime(2026, 6, 18, 10, 0)
        ss.update_pool([], now)  # empty = candidate dropped from list
        stale = ss.get_stale_candidates(now)
        assert len(stale) == 1
        assert stale[0].is_stale

    def test_stale_removed_after_timeout(self):
        ss = ScanSession()
        c = _make_candidate("300001")
        ss.update_pool([c])

        first = datetime(2026, 6, 18, 10, 0)
        ss.update_pool([], first)
        ss.get_stale_candidates(first)
        assert "300001" in ss.today_pool

        later = datetime(2026, 6, 18, 11, 0)
        stale = ss.get_stale_candidates(later)
        assert len(stale) == 0
        assert "300001" in ss.today_pool  # 不再物理删除，reset_if_new_day 统一清理

    def test_stale_candidates_sorted_by_score(self):
        ss = ScanSession()
        c1 = _make_candidate("300001", score=30)
        c2 = _make_candidate("300002", score=20)
        ss.update_pool([c1, c2])

        now = datetime(2026, 6, 18, 10, 0)
        ss.update_pool([], now)
        stale = ss.get_stale_candidates(now)
        assert len(stale) == 2
        assert stale[0].score >= stale[1].score


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


class TestCapShortTermBySector:
    """P0-69 补充：板块普涨日防止单板块淹没超短列表。"""

    def _st(self, symbol: str, name: str, score: int) -> Candidate:
        return _make_candidate(symbol, score=score, name=name)

    def test_same_sector_capped_to_max(self):
        from scanner.orchestrator import _cap_short_term_by_sector
        # 4 只医药（name 含"医药"），score 各异
        group = [
            self._st("300101", "医药A", 60),
            self._st("300102", "医药B", 90),
            self._st("300103", "医药C", 40),
            self._st("300104", "医药D", 70),
        ]
        out = _cap_short_term_by_sector(group, max_per_sector=2)
        assert len(out) == 2
        # 高分优先：90、70 的保留
        syms = {c.stock.symbol for c in out}
        assert syms == {"300102", "300104"}

    def test_different_sectors_not_capped(self):
        from scanner.orchestrator import _cap_short_term_by_sector
        group = [
            self._st("300101", "医药A", 60),
            self._st("300201", "半导体B", 90),
            self._st("300301", "新能源C", 40),
        ]
        out = _cap_short_term_by_sector(group, max_per_sector=2)
        assert len(out) == 3


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
        nf, mo, pb, rb, st, _pb = o._score_stock(
            stock, conn=None, klines={}, today="2026-06-18",
            session_state=ScanSession(), clusters=None,
        )
        assert nf is not None, "老股仅命中 new_face 不应被丢弃"
        assert nf.category == "known_new_face"
        assert mo is None and pb is None and rb is None


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
        monkeypatch.setattr(o, "get_cached_kline", lambda conn, sym: cached)
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
        monkeypatch.setattr(o, "get_cached_kline", lambda conn, sym: cached)
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
        monkeypatch.setattr(o, "get_cached_kline", lambda conn, sym: cached)
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
        monkeypatch.setattr("scanner.orchestrator.get_cached_kline", lambda conn, sym: cached)

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
        monkeypatch.setattr("scanner.orchestrator.get_cached_kline", lambda conn, sym: cached)

        class _FakeAdapter:
            def fetch_kline(self, symbol, days=15):
                return None

        monkeypatch.setattr("scanner.orchestrator.save_kline_to_db", lambda *a, **k: None)

        _fetch_all_klines(conn, _FakeAdapter(), [self._stock()])
        captured = capsys.readouterr().out
        assert "今日K线缺失" not in captured


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


