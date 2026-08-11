"""长跑健壮性相关测试：缓存淘汰、K线拉取 deadline、supervisor 退出码判定。"""
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from unified_scanner import _build_child_cmd, _should_restart
from scanner.api import _cache_put
from scanner.concept import _cache_put as _concept_cache_put
from scanner.models import StockInfo

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
        orig = mod.CACHE_MAX_ENTRIES
        mod._concept_ttl_cache.clear()
        mod.CACHE_MAX_ENTRIES = 3
        try:
            for i in range(5):
                _concept_cache_put(mod._concept_ttl_cache, f"s{i}", ([], 0.0))
            assert set(mod._concept_ttl_cache.keys()) == {"s2", "s3", "s4"}
        finally:
            mod.CACHE_MAX_ENTRIES = orig
            mod._concept_ttl_cache.clear()


class TestSuperviseDecision:

    def test_clean_exit_no_restart(self):
        assert _should_restart(0) is False

    def test_crash_exit_restarts(self):
        assert _should_restart(1) is True
        assert _should_restart(-1) is True
        assert _should_restart(2) is True

    def test_build_child_cmd_forwards_args(self):
        cmd = _build_child_cmd(120, no_feishu=True)
        assert cmd[0] == sys.executable
        assert cmd[1].endswith("unified_scanner.py")
        assert str(120) in cmd
        assert cmd[-1] == "--no-feishu"

    def test_build_child_cmd_without_no_feishu(self):
        cmd = _build_child_cmd(60, no_feishu=False)
        assert "--no-feishu" not in cmd
        assert str(60) in cmd


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


class TestSuperviseLoop:

    def test_restarts_after_crash_then_stops_on_clean_exit(self):
        from unittest.mock import MagicMock

        from unified_scanner import _supervise

        procs = [MagicMock(), MagicMock()]  # 崩溃(1) → 重启 → 干净退出(0)
        with patch("subprocess.Popen", side_effect=procs), \
             patch("unified_scanner._wait_or_kill", side_effect=[1, 0]), \
             patch("unified_scanner.time.sleep") as mock_sleep, \
             patch("unified_scanner._supervise_log"):
            code = _supervise(60, no_feishu=True)
        assert code == 0
        # 崩溃(1)后 sleep 退避一次再重启，干净退出(0)时不再 sleep
        assert mock_sleep.call_count == 1

    def test_clean_exit_no_restart(self):
        from unittest.mock import MagicMock

        from unified_scanner import _supervise

        procs = [MagicMock()]
        with patch("subprocess.Popen", side_effect=procs), \
             patch("unified_scanner._wait_or_kill", return_value=0), \
             patch("unified_scanner.time.sleep") as mock_sleep, \
             patch("unified_scanner._supervise_log"):
            code = _supervise(60, no_feishu=False)
        assert code == 0
        assert mock_sleep.call_count == 0

    def test_frozen_child_killed_via_heartbeat(self):
        from unittest.mock import MagicMock

        from unified_scanner import _wait_or_kill

        proc = MagicMock()
        proc.poll.return_value = None  # 永不退出
        with patch("unified_scanner.SUPERVISE_CHILD_TIMEOUT", 1), \
             patch.object(proc, "kill") as mock_kill, \
             patch("unified_scanner._heartbeat_age", return_value=999), \
             patch("unified_scanner.time.sleep") as mock_sleep, \
             patch("unified_scanner._supervise_log"):
            # grace=0：跳过启动宽限期（默认 SUPERVISE_CHILD_GRACE=60 会让本测试
            # 真实等待 60s 才进心跳判定，纯测试成本）。SUPERVISE_CHILD_TIMEOUT 是
            # 函数内读模块属性、patch 生效；grace 是默认参数、定义时已绑定，须显式传。
            code = _wait_or_kill(proc, grace=0)
        assert code == -9
        mock_kill.assert_called_once()


def _stock(symbol: str, rank: int = 1) -> StockInfo:
    return StockInfo(
        symbol=symbol, name="测试", code=symbol[2:],
        percent=1.0, current=10.0, value=1.0,
        rank_change=0, rank=rank,
    )


class TestKlineFetchDeadline:

    def test_breaks_at_deadline_falls_back_to_stale(self):
        import scanner.orchestrator as orch

        stale_kline = [
            {"date": "2026-08-04", "open": 9.0, "close": 10.0,
             "high": 11.0, "low": 8.0, "volume": 100.0, "percent": 1.0}
        ] * 40  # len >= KLINE_MIN_LENGTH

        conn = MagicMock()
        adapter = MagicMock()
        stocks = [_stock("SZ300001", 1), _stock("SZ300002", 2)]

        with patch.object(orch, "now_beijing", return_value=_BASE), \
             patch.object(orch, "is_trading_time", return_value=True), \
             patch.object(orch, "get_cached_kline", return_value=stale_kline), \
             patch.object(orch, "KLINE_FETCH_DEADLINE", 0):
            result = orch._fetch_all_klines(conn, adapter, stocks)

        # deadline=0 → 首轮即超时，不发起任何拉取
        adapter.fetch_kline.assert_not_called()
        # 两只票都回退旧缓存，不丢数据
        assert set(result.keys()) == {"SZ300001", "SZ300002"}
        assert result["SZ300001"] is stale_kline
        assert result["SZ300002"] is stale_kline

    def test_fetches_within_deadline(self):
        import scanner.orchestrator as orch

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
             patch.object(orch, "get_cached_kline", return_value=stale_kline), \
             patch.object(orch, "KLINE_FETCH_DEADLINE", 3600):
            result = orch._fetch_all_klines(conn, adapter, stocks)

        adapter.fetch_kline.assert_called_once_with("SZ300001", orch.KLINE_FETCH_DAYS)
        # 新旧合并，含今日 bar
        assert result["SZ300001"][-1]["date"] == "2026-08-05"
