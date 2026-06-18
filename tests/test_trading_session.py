import pytest
from datetime import time, date, datetime
from scanner.trading_session import is_trading_day, is_trading_time
from scanner.config import HOLIDAYS


class TestIsTradingDay:
    def test_weekday_is_trading(self):
        d = date(2026, 6, 18)  # Thursday
        assert is_trading_day(d)

    def test_saturday_not_trading(self):
        d = date(2026, 6, 20)  # Saturday (also in HOLIDAYS but caught by weekday check)
        assert not is_trading_day(d)

    def test_sunday_not_trading(self):
        d = date(2026, 6, 21)  # Sunday
        assert not is_trading_day(d)

    def test_holiday_not_trading(self):
        d = date(2026, 1, 1)  # New Year's Day
        assert not is_trading_day(d)

    def test_spring_festival_not_trading(self):
        d = date(2026, 2, 17)  # Spring Festival Eve
        assert not is_trading_day(d)


class TestIsTradingTime:
    def test_morning_session(self):
        dt = datetime(2026, 6, 18, 10, 0)  # Thursday 10:00
        assert is_trading_time(dt)

    def test_morning_close(self):
        dt = datetime(2026, 6, 18, 11, 30)  # Still within morning
        assert is_trading_time(dt)

    def test_lunch_break(self):
        dt = datetime(2026, 6, 18, 12, 0)  # Lunch break
        assert not is_trading_time(dt)

    def test_afternoon_session(self):
        dt = datetime(2026, 6, 18, 14, 0)  # Thursday 14:00
        assert is_trading_time(dt)

    def test_afternoon_close(self):
        dt = datetime(2026, 6, 18, 15, 0)  # Market just closed
        assert is_trading_time(dt)

    def test_after_hours(self):
        dt = datetime(2026, 6, 18, 15, 30)  # After market close
        assert not is_trading_time(dt)

    def test_weekend_not_trading_time(self):
        dt = datetime(2026, 6, 20, 10, 0)  # Saturday
        assert not is_trading_time(dt)

    def test_before_market_open(self):
        dt = datetime(2026, 6, 18, 9, 0)  # Before market opens
        assert not is_trading_time(dt)
