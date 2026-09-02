from datetime import datetime

from scanner.config import (
    EARLY_BONUS,
    LATE_BONUS,
    MARKET_ENV_STRONG,
    MARKET_ENV_WEAK,
    MAX_MARKET_CAP,
    MAX_STOCK_PRICE,
    MOMENTUM_MIN_SCORE,
    NEW_FACE_MIN_SCORE,
    REFRESH_INTERVAL,
    now_beijing,
)


class TestNowBeijing:
    def test_returns_datetime(self):
        assert isinstance(now_beijing(), datetime)


class TestConstants:
    def test_interval(self):
        assert isinstance(REFRESH_INTERVAL, int) and REFRESH_INTERVAL > 0

    def test_min_scores(self):
        assert all(isinstance(s, int) for s in [NEW_FACE_MIN_SCORE, MOMENTUM_MIN_SCORE])
        assert all(s > 0 for s in [NEW_FACE_MIN_SCORE, MOMENTUM_MIN_SCORE])

    def test_market_cap(self):
        assert MAX_MARKET_CAP > 0

    def test_stock_price(self):
        assert MAX_STOCK_PRICE > 0

    def test_time_bonus(self):
        assert isinstance(EARLY_BONUS, int)
        assert isinstance(LATE_BONUS, int)

    def test_market_env(self):
        assert MARKET_ENV_STRONG > 0
        assert MARKET_ENV_WEAK < 0
