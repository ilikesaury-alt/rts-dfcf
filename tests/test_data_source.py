"""数据源适配层测试。

覆盖：
- 符号格式转换（_xq_to_ak / _ak_to_xq）
- XueqiuAdapter 委托 api.py
- AkshareAdapter 格式转换（K线/市值/指数）
- FallbackAdapter 自动降级
- get_adapter 工厂 + 单例
"""
import sys
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from scanner.data_source import (
    AkshareAdapter,
    FallbackAdapter,
    XueqiuAdapter,
    _ak_to_xq,
    _xq_to_ak,
    get_adapter,
    reset_adapter,
)


class TestSymbolConversion:
    def test_xq_to_ak(self):
        assert _xq_to_ak("SZ300001") == "300001"
        assert _xq_to_ak("SH600000") == "600000"
        assert _xq_to_ak("BJ430047") == "430047"
        assert _xq_to_ak("300001") == "300001"  # 无前缀不变

    def test_ak_to_xq(self):
        assert _ak_to_xq("300001") == "SZ300001"
        assert _ak_to_xq("600000") == "SH600000"
        assert _ak_to_xq("399006") == "SZ399006"
        assert _ak_to_xq("430047") == "BJ430047"


class TestXueqiuAdapter:
    def test_is_available_success(self):
        adapter = XueqiuAdapter()
        with patch("scanner.data_source.api.make_session") as mock:
            mock.return_value = MagicMock()
            assert adapter.is_available() is True

    def test_is_available_failure(self):
        adapter = XueqiuAdapter()
        with patch("scanner.data_source.api.make_session") as mock:
            mock.side_effect = Exception("network error")
            assert adapter.is_available() is False

    def test_fetch_kline_delegates(self):
        adapter = XueqiuAdapter()
        with patch("scanner.data_source.api") as mock_api:
            mock_api.make_session.return_value = MagicMock()
            mock_api.fetch_kline.return_value = [{"date": "2026-07-31"}]
            result = adapter.fetch_kline("SZ300001", 15)
            mock_api.fetch_kline.assert_called_once()
            assert result == [{"date": "2026-07-31"}]

    def test_fetch_biaosheng_delegates(self):
        adapter = XueqiuAdapter()
        with patch("scanner.data_source.api") as mock_api:
            mock_api.make_session.return_value = MagicMock()
            mock_api.fetch_biaosheng.return_value = [{"symbol": "SZ300001"}]
            result = adapter.fetch_biaosheng(100)
            assert result == [{"symbol": "SZ300001"}]

    def test_fetch_market_index_delegates(self):
        adapter = XueqiuAdapter()
        with patch("scanner.data_source.api") as mock_api:
            mock_api.make_session.return_value = MagicMock()
            mock_api.fetch_market_index.return_value = 1.5
            result = adapter.fetch_market_index()
            assert result == 1.5


class TestAkshareAdapter:
    def test_is_available_with_akshare(self):
        adapter = AkshareAdapter()
        # akshare 已安装（测试环境确认）
        assert adapter.is_available() is True

    def test_is_available_without_akshare(self):
        adapter = AkshareAdapter()
        # 模拟 akshare 未安装
        with patch.dict(sys.modules, {"akshare": None}):
            adapter._ak = None  # 重置缓存
            assert adapter.is_available() is False

    def test_fetch_kline_format(self):
        """验证 AKShare K线返回格式与雪球一致。"""
        adapter = AkshareAdapter()
        mock_df = pd.DataFrame({
            "日期": ["2026-07-30", "2026-07-31"],
            "开盘": [10.0, 11.0],
            "收盘": [10.5, 11.5],
            "最高": [10.8, 11.8],
            "最低": [9.8, 10.8],
            "成交量": [100000, 120000],
            "成交额": [1050000.0, 1380000.0],
            "振幅": [9.62, 9.09],
            "涨跌幅": [5.0, 9.52],
            "涨跌额": [0.5, 1.0],
            "换手率": [1.2, 1.5],
        })
        mock_ak = MagicMock()
        mock_ak.stock_zh_a_hist.return_value = mock_df
        adapter._ak = mock_ak

        result = adapter.fetch_kline("SZ300001", 15)

        assert result is not None
        assert len(result) == 2
        k = result[0]
        # 字段名与雪球格式 1:1 对齐
        assert set(k.keys()) == {"timestamp", "date", "open", "high", "low",
                                 "close", "volume", "percent"}
        assert k["date"] == "2026-07-30"
        assert k["open"] == 10.0
        assert k["close"] == 10.5
        assert k["high"] == 10.8
        assert k["low"] == 9.8
        assert k["volume"] == 100000
        assert k["percent"] == 5.0
        assert isinstance(k["timestamp"], int)

    def test_fetch_kline_empty(self):
        """东财空 → 降级新浪；新浪也空 → None。"""
        adapter = AkshareAdapter()
        mock_ak = MagicMock()
        mock_ak.stock_zh_a_hist.return_value = pd.DataFrame()
        mock_ak.stock_zh_a_daily.return_value = pd.DataFrame()
        adapter._ak = mock_ak
        assert adapter.fetch_kline("SZ300001") is None
        mock_ak.stock_zh_a_daily.assert_called_once()

    def test_fetch_kline_em_fails_falls_back_to_sina(self):
        """东财异常 → 降级新浪成功返回（percent 由收盘价推算）。"""
        adapter = AkshareAdapter()
        mock_ak = MagicMock()
        mock_ak.stock_zh_a_hist.side_effect = Exception("network error")
        mock_ak.stock_zh_a_daily.return_value = pd.DataFrame({
            "date": ["2026-07-30", "2026-07-31"],
            "open": [10.0, 11.0],
            "high": [10.8, 11.8],
            "low": [9.8, 10.8],
            "close": [10.5, 11.5],
            "volume": [100000, 120000],
            "amount": [1050000.0, 1380000.0],
            "outstanding_share": [1e9, 1e9],
            "turnover": [0.01, 0.01],
        })
        adapter._ak = mock_ak

        result = adapter.fetch_kline("SZ300001", 15)

        assert result is not None
        assert len(result) == 2
        k = result[0]
        assert set(k.keys()) == {"timestamp", "date", "open", "high", "low",
                                 "close", "volume", "percent"}
        assert k["date"] == "2026-07-30"
        assert k["close"] == 10.5
        # percent 为推算值：首根无前收盘 → 0；次根 (11.5/10.5-1)*100
        assert k["percent"] == 0.0
        assert result[1]["percent"] == pytest.approx(9.5238, abs=0.01)
        assert isinstance(k["timestamp"], int)

    def test_fetch_kline_exception(self):
        """东财 + 新浪都失败 → None（不抛错）。"""
        adapter = AkshareAdapter()
        mock_ak = MagicMock()
        mock_ak.stock_zh_a_hist.side_effect = Exception("network error")
        mock_ak.stock_zh_a_daily.side_effect = Exception("sina network error")
        adapter._ak = mock_ak
        assert adapter.fetch_kline("SZ300001") is None

    def test_fetch_market_caps_batch(self):
        """2026-08-19 重构：直连 push2delay ulist.np/get 按 secids 精确查，
        不再走 akshare stock_zh_a_spot_em（其内部硬编码 push2.eastmoney.com
        在本机不可达）。仅返回请求的票。"""
        import requests
        adapter = AkshareAdapter()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"data": {"diff": [
            {"f12": "300001", "f14": "股票A", "f2": 10.5, "f3": 5.0,
             "f8": 1.5, "f20": 1e9, "f21": 8e8},
            {"f12": "300002", "f14": "股票B", "f2": 20.0, "f3": -2.0,
             "f8": 2.0, "f20": 2e9, "f21": 1.5e9},
            {"f12": "600000", "f14": "股票C", "f2": 5.0, "f3": 0.0,
             "f8": 0.5, "f20": 3e9, "f21": 2.5e9},
        ]}}
        with patch("scanner.data_source.requests.get", return_value=mock_resp) as m:
            result = adapter.fetch_market_caps_batch(["SZ300001", "SZ300002"])
        assert "SZ300001" in result
        assert "SZ300002" in result
        assert "SH600000" not in result  # 未请求的不返回
        assert result["SZ300001"]["market_cap"] == 1e9
        assert result["SZ300001"]["circ_market_cap"] == 8e8
        assert result["SZ300001"]["turnover_rate"] == 1.5
        assert result["SZ300001"]["current"] == 10.5
        assert result["SZ300001"]["percent"] == 5.0
        # 只查请求的票（secids 不含 600000）
        secids = m.call_args.kwargs["params"]["secids"]
        assert "600000" not in secids
        assert "0.300001" in secids and "0.300002" in secids

    def test_fetch_market_caps_batch_nan_inf_coerced(self):
        """回归：接口脏值（None/NaN/inf 字符串）→ 0，不产出 NaN 市值/现价。"""
        import math
        import requests
        adapter = AkshareAdapter()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"data": {"diff": [
            {"f12": "300001", "f14": "股票A", "f2": "nan", "f3": "inf",
             "f20": None, "f21": "-inf", "f8": None},
        ]}}
        with patch("scanner.data_source.requests.get", return_value=mock_resp):
            result = adapter.fetch_market_caps_batch(["SZ300001"])
        e = result["SZ300001"]
        assert e["current"] == 0.0 and e["percent"] == 0.0
        assert e["market_cap"] == 0.0 and e["circ_market_cap"] == 0.0
        assert e["turnover_rate"] == 0.0
        for v in e.values():
            assert math.isfinite(v)

    def test_fetch_market_caps_batch_network_error(self):
        """push2delay 请求异常 → 返回 {} 不抛（上层 FallbackAdapter 干净降级）。"""
        import requests
        adapter = AkshareAdapter()
        with patch("scanner.data_source.requests.get", side_effect=requests.ConnectionError("x")):
            result = adapter.fetch_market_caps_batch(["SZ300001"])
        assert result == {}

    def test_fetch_market_index_nan_coerced(self):
        adapter = AkshareAdapter()
        mock_df = pd.DataFrame({
            "代码": ["399006"],
            "涨跌幅": [float("nan")],
        })
        mock_ak = MagicMock()
        mock_ak.stock_zh_index_spot_em.return_value = mock_df
        adapter._ak = mock_ak
        assert adapter.fetch_market_index() == 0.0  # NaN → 0（中性）

    def test_fetch_market_caps_batch_empty_request(self):
        adapter = AkshareAdapter()
        assert adapter.fetch_market_caps_batch([]) == {}

    def test_fetch_market_index(self):
        adapter = AkshareAdapter()
        mock_df = pd.DataFrame({
            "代码": ["000001", "399001", "399006"],
            "名称": ["上证指数", "深证成指", "创业板指"],
            "涨跌幅": [0.5, 1.0, -1.5],
        })
        mock_ak = MagicMock()
        mock_ak.stock_zh_index_spot_em.return_value = mock_df
        adapter._ak = mock_ak

        result = adapter.fetch_market_index()
        assert result == -1.5  # 创业板指

    def test_fetch_market_index_not_found(self):
        adapter = AkshareAdapter()
        mock_df = pd.DataFrame({
            "代码": ["000001", "399001"],
            "涨跌幅": [0.5, 1.0],
        })
        mock_ak = MagicMock()
        mock_ak.stock_zh_index_spot_em.return_value = mock_df
        adapter._ak = mock_ak
        assert adapter.fetch_market_index() is None

    def test_fetch_biaosheng_returns_empty(self):
        adapter = AkshareAdapter()
        assert adapter.fetch_biaosheng() == []
        assert adapter.fetch_hot_list() == []


class TestFallbackAdapter:
    def test_primary_available_uses_primary(self):
        primary = MagicMock()
        primary.is_available.return_value = True
        primary.fetch_kline.return_value = [{"date": "primary"}]
        secondary = MagicMock()

        adapter = FallbackAdapter(primary, secondary)
        assert adapter.is_available() is True
        result = adapter.fetch_kline("SZ300001")
        assert result == [{"date": "primary"}]
        secondary.fetch_kline.assert_not_called()

    def test_primary_unavailable_falls_back(self):
        primary = MagicMock()
        primary.is_available.return_value = False
        primary.name = "xueqiu"
        secondary = MagicMock()
        secondary.is_available.return_value = True
        secondary.name = "akshare"
        secondary.fetch_kline.return_value = [{"date": "secondary"}]

        adapter = FallbackAdapter(primary, secondary)
        assert adapter.is_available() is True
        result = adapter.fetch_kline("SZ300001")
        assert result == [{"date": "secondary"}]

    def test_primary_exception_falls_back(self):
        primary = MagicMock()
        primary.is_available.return_value = True
        primary.name = "xueqiu"
        primary.fetch_kline.side_effect = Exception("connection error")
        secondary = MagicMock()
        secondary.name = "akshare"
        secondary.fetch_kline.return_value = [{"date": "secondary"}]

        adapter = FallbackAdapter(primary, secondary)
        adapter.is_available()
        result = adapter.fetch_kline("SZ300001")
        assert result == [{"date": "secondary"}]

    def test_both_unavailable(self):
        primary = MagicMock()
        primary.is_available.return_value = False
        secondary = MagicMock()
        secondary.is_available.return_value = False

        adapter = FallbackAdapter(primary, secondary)
        assert adapter.is_available() is False

    def test_market_caps_empty_falls_back_to_secondary(self):
        """回归（2026-08-19）：api.fetch_market_caps_batch 在内部 catch 异常后返回 {}
        （不抛给 _call），原 FallbackAdapter 的"仅异常降级"对市值形同死代码。
        雪球临时失败时 {} 静默传播 → 小而美规则整体失效。
        primary 返回空 dict 应视为失败并降级到 secondary 补拉。"""
        primary = MagicMock()
        primary.is_available.return_value = True
        primary.name = "xueqiu"
        primary.fetch_market_caps_batch.return_value = {}   # 雪球失败返回空
        secondary = MagicMock()
        secondary.name = "akshare"
        secondary.fetch_market_caps_batch.return_value = {"SZ300001": {"market_cap": 1e9}}

        adapter = FallbackAdapter(primary, secondary)
        adapter.is_available()
        result = adapter.fetch_market_caps_batch(["SZ300001"])
        assert result == {"SZ300001": {"market_cap": 1e9}}
        secondary.fetch_market_caps_batch.assert_called_once_with(["SZ300001"])

    def test_market_caps_exception_falls_back_to_secondary(self):
        """雪球市值批量查询抛异常 → 降级到 secondary 补拉。"""
        primary = MagicMock()
        primary.is_available.return_value = True
        primary.name = "xueqiu"
        primary.fetch_market_caps_batch.side_effect = Exception("403")
        secondary = MagicMock()
        secondary.name = "akshare"
        secondary.fetch_market_caps_batch.return_value = {"SZ300002": {"market_cap": 2e9}}

        adapter = FallbackAdapter(primary, secondary)
        adapter.is_available()
        result = adapter.fetch_market_caps_batch(["SZ300002"])
        assert result == {"SZ300002": {"market_cap": 2e9}}

    def test_market_caps_empty_no_secondary_returns_empty(self):
        """primary 返回空且无 secondary → 返回空 dict（不抛错，调用方干净降级）。"""
        primary = MagicMock()
        primary.is_available.return_value = True
        primary.name = "xueqiu"
        primary.fetch_market_caps_batch.return_value = {}

        adapter = FallbackAdapter(primary, None)
        adapter.is_available()
        result = adapter.fetch_market_caps_batch(["SZ300001"])
        assert result == {}


class TestGetAdapter:
    def setup_method(self):
        reset_adapter()

    def teardown_method(self):
        reset_adapter()

    def test_xueqiu_mode(self, monkeypatch):
        monkeypatch.setattr("scanner.data_source.DATA_SOURCE", "xueqiu")
        with patch("scanner.data_source.api.make_session") as mock:
            mock.return_value = MagicMock()
            adapter = get_adapter()
        assert isinstance(adapter, XueqiuAdapter)

    def test_singleton(self, monkeypatch):
        monkeypatch.setattr("scanner.data_source.DATA_SOURCE", "xueqiu")
        with patch("scanner.data_source.api.make_session") as mock:
            mock.return_value = MagicMock()
            a1 = get_adapter()
            a2 = get_adapter()
        assert a1 is a2

    def test_auto_mode_fallback(self, monkeypatch):
        """auto 模式：雪球可用 + AKShare 可用 → FallbackAdapter"""
        monkeypatch.setattr("scanner.data_source.DATA_SOURCE", "auto")
        with patch("scanner.data_source.api.make_session") as mock:
            mock.return_value = MagicMock()
            adapter = get_adapter()
        assert isinstance(adapter, FallbackAdapter)

    def test_auto_mode_xueqiu_only(self, monkeypatch):
        """auto 模式：雪球可用 + AKShare 不可用 → 仅 XueqiuAdapter"""
        monkeypatch.setattr("scanner.data_source.DATA_SOURCE", "auto")
        with patch("scanner.data_source.api.make_session") as mock_xq, \
             patch.dict(sys.modules, {"akshare": None}):
            mock_xq.return_value = MagicMock()
            # 重置 AkshareAdapter 缓存
            from scanner.data_source import AkshareAdapter as AA
            adapter = get_adapter()
        assert isinstance(adapter, XueqiuAdapter)

    def test_auto_mode_akshare_only(self, monkeypatch):
        """auto 模式：雪球不可用 + AKShare 可用 → AkshareAdapter"""
        monkeypatch.setattr("scanner.data_source.DATA_SOURCE", "auto")
        with patch("scanner.data_source.api.make_session") as mock_xq:
            mock_xq.side_effect = Exception("xueqiu down")
            adapter = get_adapter()
        assert isinstance(adapter, AkshareAdapter)

    def test_auto_mode_both_unavailable_raises(self, monkeypatch):
        """auto 模式：两者都不可用 → RuntimeError"""
        monkeypatch.setattr("scanner.data_source.DATA_SOURCE", "auto")
        with patch("scanner.data_source.api.make_session") as mock_xq, \
             patch.dict(sys.modules, {"akshare": None}):
            mock_xq.side_effect = Exception("xueqiu down")
            with pytest.raises(RuntimeError, match="无可用数据源"):
                get_adapter()
