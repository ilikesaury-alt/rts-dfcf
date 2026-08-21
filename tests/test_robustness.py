"""长跑健壮性相关测试：缓存淘汰、K线拉取 deadline、stdout 降级。"""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from scanner.models import StockInfo
from scanner.utils import cache_put as _cache_put

_BASE = datetime(2026, 8, 5, 10, 0, tzinfo=timezone(timedelta(hours=8)))


class TestBoundedCache:

    def test_evicts_oldest_beyond_cap(self):
        c = {}
        for i in range(5):
            _cache_put(c, f"k{i}", i, max_entries=3)
        assert set(c.keys()) == {"k2", "k3", "k4"}
        assert c["k4"] == 4

    def test_update_moves_to_tail(self):
        c = {"a": 1, "b": 2}
        _cache_put(c, "a", 10, max_entries=3)
        assert list(c.keys()) == ["b", "a"]
        assert c["a"] == 10

    def test_no_eviction_within_cap(self):
        c = {}
        for i in range(3):
            _cache_put(c, f"k{i}", i, max_entries=5)
        assert set(c.keys()) == {"k0", "k1", "k2"}

    def test_concept_cache_evicts(self):
        import scanner.concept as mod
        mod._concept_ttl_cache.clear()
        for i in range(5):
            _cache_put(mod._concept_ttl_cache, f"s{i}", ([], 0.0), max_entries=3)
        assert set(mod._concept_ttl_cache.keys()) == {"s2", "s3", "s4"}
        mod._concept_ttl_cache.clear()


class TestSilenceStdout:

    def test_silenced_stdout_is_writable(self, monkeypatch):
        """降级后的 stdout 必须可写（曾因双重 TextIOWrapper 包装抛 TypeError）。"""
        import sys

        import unified_scanner as mod

        monkeypatch.setattr(sys, "stdout", sys.stdout)
        monkeypatch.setattr(mod, "_STDOUT_SILENCED", False)
        mod._silence_stdout()
        sys.stdout.write("x")
        sys.stdout.flush()
        assert mod._STDOUT_SILENCED is True


def _stock(symbol: str, rank: int = 1) -> StockInfo:
    return StockInfo(
        symbol=symbol, name="测试", code=symbol[2:],
        percent=1.0, current=10.0, value=1.0,
        rank_change=0, rank=rank,
    )


class TestKlineFetchDeadline:

    def test_breaks_at_deadline_falls_back_to_stale(self):
        import scanner.kline_fetch as orch

        stale_kline = [
            {"date": "2026-08-04", "open": 9.0, "close": 10.0,
             "high": 11.0, "low": 8.0, "volume": 100.0, "percent": 1.0}
        ] * 40  # len >= KLINE_MIN_LENGTH

        conn = MagicMock()
        adapter = MagicMock()
        stocks = [_stock("SZ300001", 1), _stock("SZ300002", 2)]

        with patch.object(orch, "now_beijing", return_value=_BASE), \
             patch.object(orch, "is_trading_time", return_value=True), \
             patch.object(orch, "get_cached_klines", return_value={s.symbol: stale_kline for s in stocks}), \
             patch.object(orch, "KLINE_FETCH_DEADLINE", 0):
            result = orch.fetch_all_klines(conn, adapter, stocks)

        # deadline=0 → 首轮即超时，不发起任何拉取
        adapter.fetch_kline.assert_not_called()
        # 两只票都回退旧缓存，不丢数据
        assert set(result.keys()) == {"SZ300001", "SZ300002"}
        assert result["SZ300001"] is stale_kline
        assert result["SZ300002"] is stale_kline

    def test_fetches_within_deadline(self):
        import scanner.kline_fetch as orch

        stale_kline = [
            {"date": "2026-08-04", "open": 9.0, "close": 10.0,
             "high": 11.0, "low": 8.0, "volume": 100.0, "percent": 1.0}
        ] * 40
        fresh_kline = stale_kline + [
            {"date": "2026-08-05", "open": 10.0, "close": 10.5,
             "high": 11.0, "low": 9.8, "volume": 120.0, "percent": 5.0}
        ]

        conn = MagicMock()
        adapter = MagicMock()
        adapter.fetch_kline.return_value = fresh_kline
        stocks = [_stock("SZ300001", 1)]

        with patch.object(orch, "now_beijing", return_value=_BASE), \
             patch.object(orch, "is_trading_time", return_value=True), \
             patch.object(orch, "get_cached_klines", return_value={s.symbol: stale_kline for s in stocks}), \
             patch.object(orch, "KLINE_FETCH_DEADLINE", 3600):
            result = orch.fetch_all_klines(conn, adapter, stocks)

        adapter.fetch_kline.assert_called_once_with("SZ300001", orch.KLINE_FETCH_DAYS)
        # 新旧合并，含今日 bar
        assert result["SZ300001"][-1]["date"] == "2026-08-05"
