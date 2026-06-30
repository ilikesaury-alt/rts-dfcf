from datetime import datetime

from scanner.enhancer import compute_time_bonus, compute_market_env_bonus, accumulate_final_score
from scanner.models import Candidate, StockInfo


class TestComputeTimeBonus:
    def test_early_session(self):
        dt = datetime(2026, 6, 18, 10, 0)
        assert compute_time_bonus(dt) == -5

    def test_mid_session(self):
        dt = datetime(2026, 6, 18, 13, 0)
        assert compute_time_bonus(dt) == 0

    def test_late_session(self):
        dt = datetime(2026, 6, 18, 14, 30)
        assert compute_time_bonus(dt) == 3


class TestComputeMarketEnvBonus:
    def test_strong_market(self):
        assert compute_market_env_bonus(1.0) == 3

    def test_weak_market(self):
        assert compute_market_env_bonus(-1.5) == -3

    def test_neutral_market(self):
        assert compute_market_env_bonus(0.0) == 0

    def test_unknown_market(self):
        assert compute_market_env_bonus(None) == 0


class TestAccumulateFinalScore:
    def test_all_zeros(self):
        stock = StockInfo(symbol="300999", name="测试", code="300999",
                          percent=0.0, current=10.0, value=1000, rank_change=0, rank=50)
        c = Candidate(stock=stock, category="new_face", score=10, reason="test", kline=None)
        assert accumulate_final_score(c, 0, {}) == 0

    def test_basic_sum(self):
        stock = StockInfo(symbol="300999", name="测试", code="300999",
                          percent=5.0, current=15.0, value=5000, rank_change=100, rank=30)
        c = Candidate(stock=stock, category="new_face", score=10, reason="test", kline=None)
        c.sector_bonus = 3
        c.live_vol_bonus = 2
        c.time_bonus = 3
        result = accumulate_final_score(c, 0, {})
        assert result == 8
