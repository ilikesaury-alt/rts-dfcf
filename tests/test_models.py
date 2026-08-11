"""KlineBar 数据契约测试（重构 P0-1，2026-08-11）。

make_kline_bar 是所有 K 线生产端（api.fetch_kline / database.get_cached_kline /
historical_rescan._load_all_klines / ic_attribution.load_kline_by_symbol /
data_source AKShare adapter）的**唯一入口**，其校验行为必须稳定——
任何生产端输出改变都会先在这里暴露，防止「同一缺陷多处爆」复发。
"""
import math

from scanner.models import make_kline_bar


class TestMakeKlineBar:

    def test_valid_bar_all_numeric(self):
        bar = make_kline_bar({
            "date": "2026-08-11", "open": 10.5, "high": 11.0,
            "low": 10.0, "close": 10.8, "volume": 500.0, "percent": 2.3,
        })
        assert bar is not None
        assert bar["date"] == "2026-08-11"
        assert bar["close"] == 10.8
        # 全字段 float 强转
        for k in ("open", "high", "low", "close", "volume", "percent"):
            assert isinstance(bar[k], float), f"{k} 应为 float，实际 {type(bar[k])}"

    def test_string_numbers_coerced(self):
        """字符串数字（雪球/AKShare 偶发）应强转为 float 而非保留字符串。"""
        bar = make_kline_bar({
            "date": "2026-08-11", "open": "10.5", "high": "11.0",
            "low": "10.0", "close": "10.8", "volume": "500", "percent": "2.3",
        })
        assert bar is not None
        assert isinstance(bar["close"], float) and bar["close"] == 10.8
        assert isinstance(bar["volume"], float) and bar["volume"] == 500.0

    def test_close_none_rejected(self):
        """close 缺失（None）→ bar 剔除（下游 closes 算术不允许非正）。"""
        assert make_kline_bar({"date": "2026-08-11", "close": None}) is None

    def test_close_zero_rejected(self):
        assert make_kline_bar({"date": "2026-08-11", "close": 0}) is None
        assert make_kline_bar({"date": "2026-08-11", "close": -1.5}) is None

    def test_close_nan_rejected(self):
        assert make_kline_bar({"date": "2026-08-11", "close": float("nan")}) is None

    def test_close_inf_rejected(self):
        """close=inf 必须剔除：inf 进 closes 后 MA/RSI 等指标全变 nan，契约宣称值域收敛到合法正数。"""
        assert make_kline_bar({"date": "2026-08-11", "close": float("inf")}) is None
        assert make_kline_bar({"date": "2026-08-11", "close": float("-inf")}) is None

    def test_aux_inf_default_zero(self):
        """open/high/low/volume/percent 为 inf → 0（与 NaN 同族，一律不引入非法数值）。"""
        bar = make_kline_bar({
            "date": "2026-08-11",
            "open": float("inf"), "high": float("inf"),
            "low": float("-inf"), "close": 10.8,
            "volume": float("inf"), "percent": float("-inf"),
        })
        assert bar is not None
        for k in ("open", "high", "low", "volume", "percent"):
            assert bar[k] == 0.0, f"{k} 应为 0.0，实际 {bar[k]}"
        assert bar["close"] == 10.8

    def test_close_unparsable_rejected(self):
        assert make_kline_bar({"date": "2026-08-11", "close": "abc"}) is None

    def test_invalid_date_rejected(self):
        """date 缺失/非字符串 → 剔除（下游 date.fromisoformat 抛 ValueError 拖垮整轮）。"""
        assert make_kline_bar({"date": None, "close": 10.0}) is None
        assert make_kline_bar({"date": "", "close": 10.0}) is None
        assert make_kline_bar({"date": 20260811, "close": 10.0}) is None
        assert make_kline_bar({"close": 10.0}) is None

    def test_aux_dirty_values_default_zero(self):
        """open/high/low/volume/percent 脏值（None/NaN/非法串）→ 0（与旧 _num 行为一致，保留 bar）。"""
        bar = make_kline_bar({
            "date": "2026-08-11",
            "open": None, "high": float("nan"), "low": "abc",
            "close": 10.8, "volume": None, "percent": float("nan"),
        })
        assert bar is not None
        assert bar["open"] == 0.0
        assert bar["high"] == 0.0
        assert bar["low"] == 0.0
        assert bar["volume"] == 0.0
        assert bar["percent"] == 0.0
        assert bar["close"] == 10.8  # close 合法，整 bar 保留

    def test_non_dict_rejected(self):
        assert make_kline_bar(None) is None
        assert make_kline_bar([1, 2, 3]) is None
        assert make_kline_bar("bar") is None

    def test_extra_keys_attached_by_caller(self):
        """工厂返回严格 7 键 KlineBar；调用方显式附加键（如 timestamp）互不冲突。

        对应 api.fetch_kline 的 `bar["timestamp"] = ts` 用法。
        """
        bar = make_kline_bar({"date": "2026-08-11", "close": 10.8})
        assert bar is not None
        assert set(bar.keys()) == {"date", "open", "high", "low", "close", "volume", "percent"}
        bar["timestamp"] = 1609459200000  # 调用方附加
        assert bar["timestamp"] == 1609459200000
        assert bar["close"] == 10.8  # 附加不破坏既有字段

    def test_batch_skips_invalid_bars(self):
        """批量场景：脏 bar 剔除、合法 bar 保留，且互不影响。"""
        bars = [make_kline_bar(b) for b in [
            {"date": "2026-08-10", "close": 0},          # 停牌脏值 → 剔除
            {"date": "2026-08-11", "close": 10.8},       # 合法 → 保留
            {"date": None, "close": 10.0},               # 非法日期 → 剔除
        ]]
        kept = [b for b in bars if b is not None]
        assert len(kept) == 1
        assert kept[0]["date"] == "2026-08-11"
        assert kept[0]["close"] == 10.8
