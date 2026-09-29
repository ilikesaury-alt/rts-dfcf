from datetime import date, datetime

from scanner.trading_session import (
    is_trading_day,
    is_trading_time,
    nth_trading_day_after,
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

    def test_mid_autumn_2026_not_trading(self):
        """2026 中秋（2026-09-29 补登记）。

        漏登记造成双重故障：is_trading_day 对休市日返回 True，且 backfill_kline
        把它算进 expected 而 API 永不返回该日 K 线 → missing 永不收敛。
        实证：2026-09-25 全表 368 只票零 K 线，而 09-24/09-28 均正常。
        """
        d = date(2026, 9, 25)  # 中秋节（周五）
        assert not is_trading_day(d)


class TestHolidayCalendarConsistency:
    """日历自洽性：全表零 K 线的日子要么是休市日，要么是真实数据缺口。

    这条守卫防的是**下一个**被漏登记的法定假日：只要某个工作日在 daily_kline 里
    一行都没有，它就不该被 is_trading_day 判成交易日。数据库缺失时自动跳过。
    """

    def test_no_trading_day_with_zero_bars(self):
        """枚举日历时逐日核：**不能**用 `GROUP BY date` 取零行（那只返回存在的日期）。"""
        import os
        import sqlite3
        import sys
        from datetime import timedelta
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from scanner.config import DB_PATH

        if not os.path.exists(DB_PATH):
            return  # 无真实库（CI/干净树）则不校验

        try:
            conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
            lo, hi = conn.execute("SELECT MIN(date), MAX(date) FROM daily_kline").fetchone()
            present = {r[0] for r in conn.execute("SELECT DISTINCT date FROM daily_kline")}
            conn.close()
        except sqlite3.Error:
            return

        if not lo or not hi:
            return

        cur = date.fromisoformat(lo)
        end = date.fromisoformat(hi)
        offenders: list[str] = []
        while cur <= end:
            # 不变量：被判为交易日的工作日必须有 K 线。仅容忍末尾 2 天
            # （当日/昨日盘中数据本就可能尚未落库）；再往前都是已收市的完整交易日，
            # 零 K 线只能是节假日漏登记。is_trading_day 已含周末与假日判定。
            if is_trading_day(cur) and cur.isoformat() not in present and (end - cur).days > 2:
                offenders.append(cur.isoformat())
            cur += timedelta(days=1)

        assert not offenders, (
            f"这些工作日 daily_kline 零 K 线却被判为交易日：{offenders} —— "
            f"多半是节假日漏登记（见 holidays.py docstring 的双重故障说明）"
        )


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
        assert nth_trading_day_after(d, 1).isoformat() == "2026-06-01"

    def test_skips_holiday(self):
        # 2026-02-17 春节前（假期）→ 次一交易日应跳过整段假期到 2026-02-18 之后
        d = date.fromisoformat("2026-02-17")
        nxt = nth_trading_day_after(d, 1)
        assert nxt is not None and is_trading_day(nxt)

    def test_returns_none_on_exhaustion(self, monkeypatch):
        # 节假日数据异常（is_trading_day 永远 False）→ 耗尽安全上限后返回 None，
        # 而非静默返回非交易日（旧 backtest 版 bug）。
        monkeypatch.setattr("scanner.trading_session.is_trading_day", lambda _d: False)
        assert nth_trading_day_after(date.fromisoformat("2026-06-18"), 1) is None

    def test_is_single_source_for_consumers(self):
        # 三处消费方（backtest / portfolio_backtest / historical_rescan）应复用同一函数对象，
        # 杜绝再次分叉出带 bug 的本地拷贝。
        import scanner.backtest as backtest
        import scanner.historical_rescan as historical_rescan
        import scanner.portfolio_backtest as portfolio_backtest

        assert backtest.nth_trading_day_after is nth_trading_day_after
        assert historical_rescan.nth_trading_day_after is nth_trading_day_after
        assert portfolio_backtest.nth_trading_day_after is nth_trading_day_after
