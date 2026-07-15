from datetime import datetime

from scanner.candidate_pool import ScanSession
from scanner.models import Candidate, KlineSummary, StockInfo


def _make_candidate(symbol: str, score: int = 20) -> Candidate:
    stock = StockInfo(symbol=symbol, name="Test", code="300001",
                      percent=5.0, current=10.0, value=10000,
                      rank_change=1000, rank=1)
    kline_s = KlineSummary(trend="test", accumulated_pct=2.0,
                            volume_ratio=1.5, bottom_confirmed=True,
                            score=score, dimensions={}, avg_volume=1_000_000)
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
        assert "300001" not in ss.today_pool

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
        assert _classify_category(self._stock(5), True, None, None, c_nf) == "new_face"

    def test_known_stock_down_day_is_pullback(self):
        from scanner.orchestrator import _classify_category
        c_pb = _make_candidate("300001", score=20)
        assert _classify_category(self._stock(-2), False, c_pb, None, None) == "pullback"

    def test_known_stock_up_day_prefers_momentum(self):
        from scanner.orchestrator import _classify_category
        c_pb = _make_candidate("300001")
        c_mo = _make_candidate("300002")
        assert _classify_category(self._stock(3), False, c_pb, c_mo, None) == "momentum"

    def test_known_stock_only_new_face_fallback(self):
        from scanner.orchestrator import _classify_category
        c_nf = _make_candidate("300001")
        assert _classify_category(self._stock(3), False, None, None, c_nf) == "known_new_face"

    def test_no_candidate(self):
        from scanner.orchestrator import _classify_category
        assert _classify_category(self._stock(3), False, None, None, None) is None

    def test_known_stock_up_day_prefers_short_term(self):
        from scanner.orchestrator import _classify_category
        c_mo = _make_candidate("300001")
        c_st = _make_candidate("300002")
        assert _classify_category(self._stock(3), False, None, c_mo, c_st, c_st) == "short_term"

    def test_known_stock_down_day_ignores_short_term(self):
        from scanner.orchestrator import _classify_category
        c_pb = _make_candidate("300001", score=20)
        c_st = _make_candidate("300002")
        assert _classify_category(self._stock(-2), False, c_pb, None, None, c_st) == "pullback"

    def test_new_stock_prefers_short_term_over_momentum(self):
        from scanner.orchestrator import _classify_category
        c_mo = _make_candidate("300001")
        c_st = _make_candidate("300002")
        assert _classify_category(self._stock(5), True, None, c_mo, None, c_st) == "short_term"


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
        monkeypatch.setattr(o, "analyze_pullback", lambda *a, **k: None)
        monkeypatch.setattr(o, "validate", lambda *a, **k: (True, 0, {}))
        # 返回非空历史 -> is_new=False
        monkeypatch.setattr(o, "get_symbol_appearances",
                            lambda *a, **k: [{"date": "2026-06-01"}])

        stock = StockInfo(symbol="300001", name="Test", code="300001",
                          percent=3.0, current=10.0, value=10000,
                          rank_change=1000, rank=1)
        nf, mo, pb, st = o._score_stock(
            stock, conn=None, klines={}, today="2026-06-18",
            session_state=ScanSession(), clusters=None,
        )
        assert nf is not None, "老股仅命中 new_face 不应被丢弃"
        assert nf.category == "known_new_face"
        assert mo is None and pb is None
