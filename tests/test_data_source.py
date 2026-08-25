"""数据源适配层测试。

覆盖：
- 符号格式转换（_xq_to_ak / _ak_to_xq）
- XueqiuAdapter 委托 api.py
- ThsAdapter（2026-08-23 替代 AkshareAdapter：K线走 THS 官方源，
  市值保留东财 push2delay 直连，指数保留 akshare 可选路径）
- FallbackAdapter 自动降级
- get_adapter 工厂 + 单例
"""
import sys
from datetime import datetime
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from scanner.config import BEIJING_TZ
from scanner.data_source import (
    FallbackAdapter,
    ThsAdapter,
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

    def test_get_market_index_meta_delegates(self):
        """指数血缘元数据委托 api.get_market_index_meta（bar 日期 = 读到哪一天的证据）。"""
        adapter = XueqiuAdapter()
        with patch("scanner.data_source.api") as mock_api:
            mock_api.make_session.return_value = MagicMock()
            mock_api.get_market_index_meta.return_value = (-6.26, "2026-08-19")
            result = adapter.get_market_index_meta()
            assert result == (-6.26, "2026-08-19", "xueqiu")


class TestThsAdapter:
    """THS 官方 API 兜底适配器（2026-08-23 替代 AKShare）。"""

    @pytest.fixture(autouse=True)
    def _with_key(self, monkeypatch):
        # 默认视为已配置 Key（is_available 不打网络）；个别用例自行覆盖
        monkeypatch.setattr("scanner.ths_api.get_api_key", lambda: "test-key")

    def test_is_available_with_key(self):
        assert ThsAdapter().is_available() is True

    def test_is_available_without_key(self, monkeypatch):
        monkeypatch.setattr("scanner.ths_api.get_api_key", lambda: "")
        assert ThsAdapter().is_available() is False

    def _ths_bar(self, d_ms, o, h, lo, c, v):
        return {"date_ms": d_ms, "open_price": o, "high_price": h,
                "low_price": lo, "close_price": c, "volume": v}

    def test_fetch_kline_format(self):
        """验证 THS K线返回格式与雪球一致；percent 由收盘价推算。"""
        adapter = ThsAdapter()
        bars = [
            self._ths_bar(1785000000000, 10.0, 10.8, 9.8, 10.5, 100000),
            self._ths_bar(1785086400000, 11.0, 11.8, 10.8, 11.5, 120000),
        ]
        with patch("scanner.ths_api._call") as mock_call:
            mock_call.return_value = {"code": 0, "data": {"item": bars}}
            result = adapter.fetch_kline("SZ300001", 15)
        assert result is not None
        assert len(result) == 2
        k = result[0]
        # 字段名与雪球格式 1:1 对齐
        assert set(k.keys()) >= {"timestamp", "date", "open", "high", "low",
                                 "close", "volume", "percent"}
        assert k["close"] == 10.5
        assert k["percent"] == 0.0  # 首根无前收盘 → 0
        assert result[1]["percent"] == pytest.approx(9.5238, abs=0.01)
        assert isinstance(k["timestamp"], int)

    def test_fetch_kline_empty(self):
        adapter = ThsAdapter()
        with patch("scanner.ths_api._call") as mock_call:
            mock_call.return_value = {"code": 0, "data": {"item": []}}
            assert adapter.fetch_kline("SZ300001") is None

    def test_fetch_kline_exception(self):
        """接口失败 → None（不抛错，上层 FallbackAdapter 干净降级）。"""
        adapter = ThsAdapter()
        with patch("scanner.ths_api._call", side_effect=RuntimeError("net")):
            assert adapter.fetch_kline("SZ300001") is None

    def test_fetch_market_caps_batch(self):
        """2026-08-19 重构保留：直连 push2delay ulist.np/get 按 secids 精确查，
        THS 无市值字段故沿用东财源。仅返回请求的票。"""
        adapter = ThsAdapter()
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
        adapter = ThsAdapter()
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
        adapter = ThsAdapter()
        with patch("scanner.data_source.requests.get",
                   side_effect=requests.ConnectionError("x")):
            result = adapter.fetch_market_caps_batch(["SZ300001"])
        assert result == {}

    def test_fetch_market_caps_batch_empty_request(self):
        adapter = ThsAdapter()
        assert adapter.fetch_market_caps_batch([]) == {}

    def _fake_ak_module(self, df=None, error=None):
        fake = MagicMock()
        if error:
            fake.stock_zh_index_spot_em.side_effect = error
        else:
            fake.stock_zh_index_spot_em.return_value = df
        return fake

    def test_fetch_market_index_nan_coerced(self):
        adapter = ThsAdapter()
        mock_df = pd.DataFrame({"代码": ["399006"], "涨跌幅": [float("nan")]})
        fake = self._fake_ak_module(mock_df)
        with patch.dict(sys.modules, {"akshare": fake}):
            assert adapter.fetch_market_index() == 0.0  # NaN → 0（中性）

    def test_fetch_market_index(self):
        adapter = ThsAdapter()
        mock_df = pd.DataFrame({
            "代码": ["000001", "399001", "399006"],
            "名称": ["上证指数", "深证成指", "创业板指"],
            "涨跌幅": [0.5, 1.0, -1.5],
        })
        fake = self._fake_ak_module(mock_df)
        with patch.dict(sys.modules, {"akshare": fake}):
            result = adapter.fetch_market_index()
        assert result == -1.5  # 创业板指

    def test_fetch_market_index_not_found(self):
        adapter = ThsAdapter()
        mock_df = pd.DataFrame({"代码": ["000001", "399001"],
                                "涨跌幅": [0.5, 1.0]})
        fake = self._fake_ak_module(mock_df)
        with patch.dict(sys.modules, {"akshare": fake}):
            assert adapter.fetch_market_index() is None

    def test_fetch_market_index_akshare_missing_degrades(self):
        """akshare 未安装 → 指数兜底干净降级 None（不抛错）。"""
        adapter = ThsAdapter()
        with patch.dict(sys.modules, {"akshare": None}):
            assert adapter.fetch_market_index() is None

    def test_get_market_index_meta_same_day(self):
        """东财 spot 恒为当日实况 → bar 日期 = 今日（same-day 语义，区别于雪球旧 bar）。"""
        adapter = ThsAdapter()
        mock_df = pd.DataFrame({"代码": ["399006"], "涨跌幅": [-6.26]})
        fake = self._fake_ak_module(mock_df)
        with patch.dict(sys.modules, {"akshare": fake}):
            adapter.fetch_market_index()
        pct, bar, source = adapter.get_market_index_meta()
        assert pct == -6.26 and source == "akshare"
        assert bar == datetime.now(BEIJING_TZ).date().isoformat()

    def test_get_market_index_meta_failure_resets(self):
        adapter = ThsAdapter()
        fake = self._fake_ak_module(error=Exception("net"))
        with patch.dict(sys.modules, {"akshare": fake}):
            assert adapter.fetch_market_index() is None
        assert adapter.get_market_index_meta() == (None, None, "akshare")

    def test_fetch_biaosheng_returns_empty(self):
        adapter = ThsAdapter()
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

    def test_fetch_market_index_none_falls_back(self):
        """回归（2026-08-19）：api.fetch_market_index 内部吞异常返回 None（不抛给 _call），
        原"仅异常降级"对指数形同死代码——雪球指数失败时大盘标签退化为中性且无兜底。
        primary 返回 None 应视为失败并降级到 secondary。"""
        primary = MagicMock()
        primary.is_available.return_value = True
        primary.name = "xueqiu"
        primary.fetch_market_index.return_value = None   # 雪球失败返回 None
        secondary = MagicMock()
        secondary.name = "akshare"
        secondary.fetch_market_index.return_value = -6.26

        adapter = FallbackAdapter(primary, secondary)
        adapter.is_available()
        result = adapter.fetch_market_index()
        assert result == -6.26
        secondary.fetch_market_index.assert_called_once()

    def test_get_market_index_meta_delegates_used_source(self):
        """指数血缘元数据委托实际生效的数据源。"""
        primary = MagicMock()
        primary.is_available.return_value = True
        primary.name = "xueqiu"
        primary.get_market_index_meta.return_value = (-6.26, "2026-08-19", "xueqiu")
        secondary = MagicMock()
        secondary.name = "akshare"
        secondary.get_market_index_meta.return_value = (-6.26, "2026-08-19", "akshare")

        adapter = FallbackAdapter(primary, secondary)
        adapter.is_available()
        assert adapter.get_market_index_meta() == (-6.26, "2026-08-19", "xueqiu")
        primary.get_market_index_meta.assert_called_once()
        secondary.get_market_index_meta.assert_not_called()


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
        """auto 模式：雪球可用 + THS Key 已配置 → FallbackAdapter"""
        monkeypatch.setattr("scanner.data_source.DATA_SOURCE", "auto")
        monkeypatch.setattr("scanner.ths_api.get_api_key", lambda: "k")
        with patch("scanner.data_source.api.make_session") as mock:
            mock.return_value = MagicMock()
            adapter = get_adapter()
        assert isinstance(adapter, FallbackAdapter)
        assert adapter._secondary.name == "ths"

    def test_auto_mode_xueqiu_only(self, monkeypatch):
        """auto 模式：雪球可用 + THS Key 未配置 → 仅 XueqiuAdapter"""
        monkeypatch.setattr("scanner.data_source.DATA_SOURCE", "auto")
        monkeypatch.setattr("scanner.ths_api.get_api_key", lambda: "")
        with patch("scanner.data_source.api.make_session") as mock_xq:
            mock_xq.return_value = MagicMock()
            adapter = get_adapter()
        assert isinstance(adapter, XueqiuAdapter)

    def test_auto_mode_xueqiu_down_still_fallback(self, monkeypatch):
        """auto 模式：雪球启动探测失败 + THS Key 已配置 → 仍恒构造 FallbackAdapter。

        2026-08-24 审查修复：旧实现此处返回裸 ThsAdapter 且单例永不复探——启动
        瞬间网络抖动一次即把长跑进程整天锁死为 THS-only（飙升榜返空=全天零候选）。
        现双源配置时恒构造 FallbackAdapter，由 _call 逐请求降级承担故障切换。
        """
        monkeypatch.setattr("scanner.data_source.DATA_SOURCE", "auto")
        monkeypatch.setattr("scanner.ths_api.get_api_key", lambda: "k")
        with patch("scanner.data_source.api.make_session") as mock_xq:
            mock_xq.side_effect = Exception("xueqiu down")
            adapter = get_adapter()
        assert isinstance(adapter, FallbackAdapter)
        assert adapter._secondary.name == "ths"

    def test_auto_mode_both_unavailable_raises(self, monkeypatch):
        """auto 模式：雪球不可用且无 THS Key → RuntimeError"""
        monkeypatch.setattr("scanner.data_source.DATA_SOURCE", "auto")
        monkeypatch.setattr("scanner.ths_api.get_api_key", lambda: "")
        with patch("scanner.data_source.api.make_session") as mock_xq:
            mock_xq.side_effect = Exception("xueqiu down")
            with pytest.raises(RuntimeError, match="无可用数据源"):
                get_adapter()
