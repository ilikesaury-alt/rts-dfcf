"""回归测试：盘中残留今日 bar 的收盘定稿机制（2026-08-18 拓斯达案例）。

问题：盘中扫描把未收盘的今日 bar（盘中价+部分量能）写入 daily_kline，收盘后无
定稿覆盖则残留永久保留（拓斯达 08-18 盘中 36.27 残留、真实收盘 37.90，DB 一度
显示 -3.36% 实际 +0.99%）。

修复：
  1. unified_scanner._finalize_today_klines —— 主循环收盘后自动用最终 bar 覆盖一次
  2. backfill_kline._select_rows_to_write —— 回填时今日 bar 无条件纳入写集合
"""
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import backfill_kline
import unified_scanner as us

BEIJING = timezone(timedelta(hours=8))


def _now(h: int, m: int = 0, day: int = 18) -> datetime:
    return datetime(2026, 8, day, h, m, tzinfo=BEIJING)


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, symbols):
        self._symbols = symbols

    def execute(self, sql, params=()):
        if "SELECT DISTINCT symbol" in sql:
            return _FakeCursor([(s,) for s in self._symbols])
        return _FakeCursor([])


class _FakeAdapter:
    def __init__(self, kline):
        self._kline = kline
        self.calls = 0

    def fetch_kline(self, symbol, days):
        self.calls += 1
        return self._kline


@pytest.fixture(autouse=True)
def _reset_finalize_flag():
    us._finalize_date = None
    yield
    us._finalize_date = None


class TestFinalizeTodayKlines:
    def test_refreshes_today_bars_after_close(self, monkeypatch):
        """收盘后（18:00）：查询有今日 bar 的 symbol，用最终收盘 bar 覆盖。"""
        monkeypatch.setattr(us, "now_beijing", lambda: _now(18, 0))
        monkeypatch.setattr(us, "is_trading_day", lambda d: True)
        monkeypatch.setattr(us, "is_trading_time", lambda n=None: False)
        today_bar = {"date": "2026-08-18", "open": 37.21, "close": 37.90, "high": 37.90,
                     "low": 36.01, "volume": 55_293_901, "percent": 0.99, "timestamp": 1}
        yesterday_bar = {"date": "2026-08-17", "open": 36.19, "close": 37.53, "high": 38.18,
                         "low": 35.12, "volume": 50_164_729, "percent": 5.13, "timestamp": 1}
        adapter = _FakeAdapter([yesterday_bar, today_bar])
        written: list = []
        monkeypatch.setattr(us, "save_kline_to_db",
                            lambda conn, sym, bars: written.append((sym, bars)))

        us._finalize_today_klines(_FakeConn(["SZ300607"]), adapter)

        assert written == [("SZ300607", [today_bar])]
        assert us._finalize_date == "2026-08-18"

    def test_once_per_day(self, monkeypatch):
        """同一交易日只执行一次：第二次调用不再拉取。"""
        monkeypatch.setattr(us, "now_beijing", lambda: _now(18, 0))
        monkeypatch.setattr(us, "is_trading_day", lambda d: True)
        monkeypatch.setattr(us, "is_trading_time", lambda n=None: False)
        bar = {"date": "2026-08-18", "open": 37.21, "close": 37.90, "high": 37.90,
               "low": 36.01, "volume": 55_293_901, "percent": 0.99, "timestamp": 1}
        adapter = _FakeAdapter([bar])
        monkeypatch.setattr(us, "save_kline_to_db", lambda *a, **k: None)

        us._finalize_today_klines(_FakeConn(["SZ300607"]), adapter)
        first_calls = adapter.calls
        us._finalize_today_klines(_FakeConn(["SZ300607"]), adapter)

        assert adapter.calls == first_calls == 1

    def test_skips_pre_open(self, monkeypatch):
        """开盘前（08:30）不触发：t < AFTERNOON_END 直接跳过。"""
        monkeypatch.setattr(us, "now_beijing", lambda: _now(8, 30))
        monkeypatch.setattr(us, "is_trading_day", lambda d: True)
        monkeypatch.setattr(us, "is_trading_time", lambda n=None: False)
        adapter = _FakeAdapter([])
        monkeypatch.setattr(us, "save_kline_to_db", lambda *a, **k: None)

        us._finalize_today_klines(_FakeConn(["SZ300607"]), adapter)

        assert adapter.calls == 0
        assert us._finalize_date is None  # 未置标记，下次收盘后仍可执行

    def test_skips_non_trading_day(self, monkeypatch):
        """非交易日不触发。"""
        monkeypatch.setattr(us, "now_beijing", lambda: _now(18, 0, day=16))  # 周日
        monkeypatch.setattr(us, "is_trading_day", lambda d: False)
        monkeypatch.setattr(us, "is_trading_time", lambda n=None: False)
        adapter = _FakeAdapter([])
        monkeypatch.setattr(us, "save_kline_to_db", lambda *a, **k: None)

        us._finalize_today_klines(_FakeConn(["SZ300607"]), adapter)

        assert adapter.calls == 0

    def test_no_today_bar_symbols_noop(self, monkeypatch):
        """库里无今日 bar 的 symbol 时直接返回，不拉取。"""
        monkeypatch.setattr(us, "now_beijing", lambda: _now(18, 0))
        monkeypatch.setattr(us, "is_trading_day", lambda d: True)
        monkeypatch.setattr(us, "is_trading_time", lambda n=None: False)
        adapter = _FakeAdapter([])
        monkeypatch.setattr(us, "save_kline_to_db", lambda *a, **k: None)

        us._finalize_today_klines(_FakeConn([]), adapter)

        assert adapter.calls == 0

    def test_fetch_failure_fail_open(self, monkeypatch):
        """单只拉取异常 fail-open：不中断，其余继续。"""
        monkeypatch.setattr(us, "now_beijing", lambda: _now(18, 0))
        monkeypatch.setattr(us, "is_trading_day", lambda d: True)
        monkeypatch.setattr(us, "is_trading_time", lambda n=None: False)
        bar = {"date": "2026-08-18", "open": 37.21, "close": 37.90, "high": 37.90,
               "low": 36.01, "volume": 55_293_901, "percent": 0.99, "timestamp": 1}

        class _FlakyAdapter:
            def __init__(self):
                self.calls = 0

            def fetch_kline(self, symbol, days):
                self.calls += 1
                if symbol == "SZ300607":
                    raise RuntimeError("network down")
                return [bar]

        adapter = _FlakyAdapter()
        written: list = []
        monkeypatch.setattr(us, "save_kline_to_db",
                            lambda conn, sym, bars: written.append(sym))

        us._finalize_today_klines(_FakeConn(["SZ300607", "SZ300608"]), adapter)

        assert adapter.calls == 2
        assert written == ["SZ300608"]  # 失败的跳过，成功的继续

    def test_flag_not_set_when_work_throws(self, monkeypatch):
        """2026-08-19 修复：_finalize_date 在工作全部完成后才置位——中途异常
        （如 DB 锁）不把当日误标为已定稿，下次循环可重试（此前在 sleep 前置位，
        异常后当日永不重试、盘中残留 bar 永久留在库里）。"""
        monkeypatch.setattr(us, "now_beijing", lambda: _now(18, 0))
        monkeypatch.setattr(us, "is_trading_day", lambda d: True)
        monkeypatch.setattr(us, "is_trading_time", lambda n=None: False)
        monkeypatch.setattr(us, "save_kline_to_db", lambda *a, **k: None)

        class _BoomConn:
            def execute(self, sql, params=()):
                raise sqlite3.OperationalError("database is locked")

        with pytest.raises(sqlite3.OperationalError):
            us._finalize_today_klines(_BoomConn(), _FakeAdapter([]))

        assert us._finalize_date is None  # 未置位 → 收盘后仍可重试


class TestBackfillSelectRowsToWrite:
    def _bar(self, d: str, close: float) -> dict:
        return {"date": d, "open": close, "close": close, "high": close,
                "low": close, "volume": 1, "percent": 0.0, "timestamp": 1}

    def test_today_bar_included_even_not_missing(self):
        """今日 bar 即使不在缺口集合也必须写入（收盘定稿覆盖盘中残留）。"""
        today = datetime(2026, 8, 18, tzinfo=BEIJING).date()
        kline = [self._bar("2026-08-14", 35.70), self._bar("2026-08-17", 37.53),
                 self._bar("2026-08-18", 37.90)]  # 08-18 在库中（盘中残留），非缺口
        missing = {"2026-08-14", "2026-08-17"}

        rows = backfill_kline._select_rows_to_write(kline, missing, today)

        assert [r["date"] for r in rows] == ["2026-08-14", "2026-08-17", "2026-08-18"]

    def test_other_dates_still_missing_only(self):
        """非今日的非缺口日期仍不写入（避免无谓覆盖）。"""
        today = datetime(2026, 8, 18, tzinfo=BEIJING).date()
        kline = [self._bar("2026-08-13", 34.39), self._bar("2026-08-14", 35.70)]
        missing = {"2026-08-14"}

        rows = backfill_kline._select_rows_to_write(kline, missing, today)

        assert [r["date"] for r in rows] == ["2026-08-14"]
