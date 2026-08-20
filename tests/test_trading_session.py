from datetime import date, datetime

from scanner.trading_session import (
    _nth_trading_day_after,
    is_trading_day,
    is_trading_time,
    trading_minutes_elapsed,
)


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


class TestTradingMinutesElapsed:
    def test_before_open_zero(self):
        assert trading_minutes_elapsed(datetime(2026, 6, 18, 9, 0)) == 0

    def test_morning_quarter(self):
        assert trading_minutes_elapsed(datetime(2026, 6, 18, 9, 45)) == 15

    def test_morning_open_moment(self):
        assert trading_minutes_elapsed(datetime(2026, 6, 18, 9, 30)) == 1

    def test_morning_first_minute(self):
        assert trading_minutes_elapsed(datetime(2026, 6, 18, 9, 30, 59)) == 1

    def test_morning_close_120(self):
        assert trading_minutes_elapsed(datetime(2026, 6, 18, 11, 30)) == 120

    def test_lunch_break_120(self):
        assert trading_minutes_elapsed(datetime(2026, 6, 18, 12, 30)) == 120

    def test_afternoon_mid(self):
        assert trading_minutes_elapsed(datetime(2026, 6, 18, 14, 0)) == 180

    def test_afternoon_close_240(self):
        assert trading_minutes_elapsed(datetime(2026, 6, 18, 15, 0)) == 240

    def test_after_hours_240(self):
        assert trading_minutes_elapsed(datetime(2026, 6, 18, 15, 30)) == 240

    def test_non_trading_day_zero(self):
        assert trading_minutes_elapsed(datetime(2026, 6, 20, 10, 0)) == 0


class TestNthTradingDayAfter:
    """P0-3 收敛单源回归：backtest / portfolio_backtest / historical_rescan 此前各抄一份，
    backtest 版在 max_iter 耗尽时静默返回非交易日（holidays.json 损坏会算错 next_day 收益）。
    统一到本模块后，耗尽必须返回 None。
    """

    def test_skips_weekend(self):
        # 2026-05-29 周五 → 次一交易日 2026-06-01 周一
        d = date.fromisoformat("2026-05-29")
        assert _nth_trading_day_after(d, 1).isoformat() == "2026-06-01"

    def test_skips_holiday(self):
        # 2026-02-17 春节前（假期）→ 次一交易日应跳过整段假期到 2026-02-18 之后
        d = date.fromisoformat("2026-02-17")
        nxt = _nth_trading_day_after(d, 1)
        assert nxt is not None and is_trading_day(nxt)

    def test_returns_none_on_exhaustion(self, monkeypatch):
        # 节假日数据异常（is_trading_day 永远 False）→ 耗尽安全上限后返回 None，
        # 而非静默返回非交易日（旧 backtest 版 bug）。
        monkeypatch.setattr("scanner.trading_session.is_trading_day", lambda _d: False)
        assert _nth_trading_day_after(date.fromisoformat("2026-06-18"), 1) is None

    def test_is_single_source_for_consumers(self):
        # 三处消费方（backtest / portfolio_backtest / historical_rescan）应复用同一函数对象，
        # 杜绝再次分叉出带 bug 的本地拷贝。
        import scanner.backtest as backtest
        import scanner.historical_rescan as historical_rescan
        import scanner.portfolio_backtest as portfolio_backtest

        assert backtest._nth_trading_day_after is _nth_trading_day_after
        assert historical_rescan._nth_trading_day_after is _nth_trading_day_after
        assert portfolio_backtest._nth_trading_day_after is _nth_trading_day_after
