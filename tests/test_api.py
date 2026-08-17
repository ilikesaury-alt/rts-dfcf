from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from scanner.api import (
    _biaosheng_circuit_breaker,
    _fetch_minute_data,
    _normalize_minute_item,
    analyze_intraday,
    analyze_opening_strength,
    compute_surge_sentiment,
    estimate_live_volume,
)


class TestComputeSurgeSentiment:

    def test_boiling(self):
        items = [{"percent": 8}] * 10 + [{"percent": 5}] * 5
        r = compute_surge_sentiment(items)
        assert r["phase"] == "boiling"
        assert r["bonus"] == 5

    def test_warm(self):
        items = [{"percent": 5}] * 4 + [{"percent": 3.5}] * 11
        r = compute_surge_sentiment(items)
        assert r["phase"] == "warm"
        assert r["bonus"] == 2

    def test_frozen(self):
        items = [{"percent": -3}] * 10 + [{"percent": -1}] * 5
        r = compute_surge_sentiment(items)
        assert r["phase"] == "frozen"
        assert r["bonus"] == -5

    def test_cool(self):
        items = [{"percent": 0.5}] * 10 + [{"percent": -0.5}] * 5
        r = compute_surge_sentiment(items)
        assert r["phase"] == "cool"
        assert r["bonus"] == -2

    def test_neutral(self):
        items = [{"percent": 2}] * 10 + [{"percent": 1}] * 5
        r = compute_surge_sentiment(items)
        assert r["phase"] == "neutral"
        assert r["bonus"] == 0

    def test_empty_items(self):
        r = compute_surge_sentiment([])
        assert r["phase"] == "cool"
        assert r["bonus"] == -2

    def test_string_percent_and_rank_change_coerced(self):
        """回归：原始行情 percent/rank_change 为字符串时不抛 TypeError
        （此前 sum(str + float) 崩溃，拖垮整个扫描周期）。"""
        items = [
            {"percent": "8.5", "rank_change": "500"},
            {"percent": 7.0, "rank_change": 200},
            {"percent": "6.2", "rank_change": None},
            {"percent": None, "rank_change": "-"},
        ]
        r = compute_surge_sentiment(items)
        assert r["phase"] == "boiling"
        assert r["avg_top10_pct"] == pytest.approx(5.42)  # (8.5+7.0+6.2+0.0)/4
        assert r["pct_gt_5_ratio"] == pytest.approx(0.75)
        assert r["avg_rank_churn"] == pytest.approx(175.0)  # (500 + 200 + 0 + 0) / 4


class TestBiaoshengCircuitBreaker:

    def test_success_updates_cache(self):
        raw = [{"symbol": "300001"}]
        r = _biaosheng_circuit_breaker(raw, success=True)
        assert r == raw

    def test_success_with_empty(self):
        r = _biaosheng_circuit_breaker([], success=True)
        assert r == []

    def test_failure_returns_cached(self):
        _biaosheng_circuit_breaker([{"symbol": "300001"}], success=True)
        r = _biaosheng_circuit_breaker([], success=False)
        assert r == [{"symbol": "300001"}]

    def test_failure_no_cache_returns_empty(self):
        from scanner.api import _biaosheng_cb
        _biaosheng_cb["cached"] = []
        r = _biaosheng_circuit_breaker([], success=False)
        assert r == []

    def test_three_failures_enters_cooldown(self):
        from scanner.api import _biaosheng_cb
        _biaosheng_cb["cached"] = [{"symbol": "300001"}]
        _biaosheng_cb["failures"] = 0
        _biaosheng_cb["cooldown_until"] = 0

        for _ in range(3):
            _biaosheng_circuit_breaker([], success=False)

        assert _biaosheng_cb["cooldown_until"] > 0


class TestXueqiuHotCircuitBreaker:

    def test_success_updates_cache(self):
        from scanner.api import _xueqiu_hot_circuit_breaker
        raw = [{"symbol": "300001"}]
        r = _xueqiu_hot_circuit_breaker(raw, success=True)
        assert r == raw

    def test_success_with_empty(self):
        from scanner.api import _xueqiu_hot_circuit_breaker
        r = _xueqiu_hot_circuit_breaker([], success=True)
        assert r == []

    def test_failure_returns_cached(self):
        from scanner.api import _xueqiu_hot_circuit_breaker, _xueqiu_hot_cb
        _xueqiu_hot_circuit_breaker([{"symbol": "300001"}], success=True)
        r = _xueqiu_hot_circuit_breaker([], success=False)
        assert r == [{"symbol": "300001"}]

    def test_failure_no_cache_returns_empty(self):
        from scanner.api import _xueqiu_hot_circuit_breaker, _xueqiu_hot_cb
        _xueqiu_hot_cb["cached"] = []
        r = _xueqiu_hot_circuit_breaker([], success=False)
        assert r == []

    def test_three_failures_enters_cooldown(self):
        from scanner.api import _xueqiu_hot_circuit_breaker, _xueqiu_hot_cb
        _xueqiu_hot_cb["cached"] = [{"symbol": "300001"}]
        _xueqiu_hot_cb["failures"] = 0
        _xueqiu_hot_cb["cooldown_until"] = 0

        for _ in range(3):
            _xueqiu_hot_circuit_breaker([], success=False)

        assert _xueqiu_hot_cb["cooldown_until"] > 0


_minute_5_items = [
    {"current": 100.0, "volume": 500},
    {"current": 100.5, "volume": 600},
    {"current": 101.0, "volume": 700},
    {"current": 101.5, "volume": 400},
    {"current": 102.0, "volume": 300},
]


class TestAnalyzeOpeningStrength:

    def test_strong_opening(self):
        with patch("scanner.api._fetch_minute_data", return_value=_minute_5_items + [
            {"current": 102.0, "volume": 200},
        ]):
            score = analyze_opening_strength(MagicMock(), "300501")
        assert score is not None
        assert score > 0

    def test_weak_opening(self):
        items = [{"current": 100.0, "volume": 500},
                 {"current": 99.0, "volume": 600}]
        with patch("scanner.api._fetch_minute_data", return_value=items):
            score = analyze_opening_strength(MagicMock(), "300502")
        assert score is None or score < 0

    def test_insufficient_data_returns_none(self):
        items = [{"current": 100.0, "volume": 500}] * 3
        with patch("scanner.api._fetch_minute_data", return_value=items):
            score = analyze_opening_strength(MagicMock(), "300503")
        assert score is None


class TestAnalyzeIntraday:

    def test_returns_float_for_good_data(self):
        items = [{"current": 100.0 + i * 0.1, "volume": 100} for i in range(250)]
        with patch("scanner.api._fetch_minute_data", return_value=items):
            score = analyze_intraday(MagicMock(), "300999")
        assert isinstance(score, float)

    def test_insufficient_data_returns_none(self):
        items = [{"current": 100.0, "volume": 100}]
        with patch("scanner.api._fetch_minute_data", return_value=items):
            score = analyze_intraday(MagicMock(), "300111")
        assert score is None

    def test_none_data_returns_none(self):
        with patch("scanner.api._fetch_minute_data", return_value=None):
            score = analyze_intraday(MagicMock(), "300222")
        assert score is None


class TestEstimateLiveVolume:

    def test_basic_estimate(self):
        items = [{"current": 100.0, "volume": 100}] * 120
        with patch("scanner.api._fetch_minute_data", return_value=items):
            vol = estimate_live_volume(MagicMock(), "300333")
        assert vol == 24000.0

    def test_none_data_returns_none(self):
        with patch("scanner.api._fetch_minute_data", return_value=None):
            vol = estimate_live_volume(MagicMock(), "300444")
        assert vol is None


class TestNormalizeMinuteItem:

    def test_raw_array_normalized_to_dict(self):
        # 列序与 fetch_kline 一致: [ts, volume, open, high, low, close, chg, percent, turnoverrate, amount]
        raw = [1609459200000, 500, 99.0, 102.0, 98.0, 101.0, 1.0, 1.0, 0.5, 50500.0]
        d = _normalize_minute_item(raw)
        assert isinstance(d, dict)
        assert d["volume"] == 500
        # current = close = raw[5]
        assert d["current"] == 101.0
        # avg_price = amount / volume = 50500 / 500
        assert d["avg_price"] == pytest.approx(101.0)

    def test_dict_passthrough(self):
        raw = {"current": 10.0, "volume": 1}
        assert _normalize_minute_item(raw) is raw

    def test_short_array_safe(self):
        d = _normalize_minute_item([1])
        assert d["current"] == 0.0

    def test_array_preserves_high_low_percent(self):
        """2026-08-17 审查修复：数组形态保留 high/low/percent——盘中分时兜底
        _build_today_bar_from_minute 依赖 high/low 构造今日 bar 振幅、percent 构造
        今日涨幅，此前被裁剪丢弃（high/low 恒=current）致振幅失真。"""
        raw = [1609459200000, 500, 99.0, 102.0, 98.0, 101.0, 1.0, 1.5, 0.5, 50500.0]
        d = _normalize_minute_item(raw)
        assert d["high"] == 102.0
        assert d["low"] == 98.0
        assert d["percent"] == 1.5

    def test_short_array_high_low_percent_zero(self):
        d = _normalize_minute_item([1])
        assert d["high"] == 0.0
        assert d["low"] == 0.0
        assert d["percent"] == 0.0

    def test_string_values_coerced(self):
        """回归：分时接口与 kline 同源，偶发返回字符串/None/NaN 数值字段。
        保持字符串会让 analyze_opening_strength / estimate_live_volume 抛 TypeError
        丢分时信号（与 fetch_kline 的 _num 同族修复）。"""
        raw = [1609459200000, "500", 99.0, 102.0, 98.0, "101.0", 1.0, 1.0, 0.5, "50500.0"]
        d = _normalize_minute_item(raw)
        assert isinstance(d["volume"], float) and d["volume"] == 500.0
        assert isinstance(d["current"], float) and d["current"] == 101.0
        assert isinstance(d["avg_price"], float) and d["avg_price"] == pytest.approx(101.0)

    def test_dict_string_values_coerced_by_fetch(self):
        """dict 直通形态的字符串字段由 _fetch_minute_data 统一兜底强转，
        三个消费者（opening_strength/live_volume/intraday）不抛 TypeError。"""
        raw_items = [
            {"timestamp": 1609459200000 + i * 60000, "volume": "100",
             "avg_price": "10.0", "current": "10.0"}
            for i in range(20)
        ]
        fake_resp = MagicMock()
        fake_resp.json.return_value = {"data": {"items": raw_items}}
        with patch("scanner.api._request_with_retry", return_value=fake_resp):
            items = _fetch_minute_data(MagicMock(), "300555")
        assert items is not None
        assert isinstance(items[0]["current"], float)
        assert isinstance(items[0]["volume"], float)
        # 消费者不再崩溃
        assert analyze_opening_strength(MagicMock(), "300555") is not None
        assert estimate_live_volume(MagicMock(), "300555") is not None
        assert analyze_intraday(MagicMock(), "300555") is not None


class TestFetchMinuteDataRawArray:

    def test_raw_array_items_yield_dicts(self):
        raw_items = [
            [1609459200000 + i * 60000, 100, 10.0 + i, 10.0 + i, 10.0 + i,
             10.0 + i, 1.0, 1.0, 0.5, 1000.0 + i * 100]
            for i in range(30)
        ]
        fake_resp = MagicMock()
        fake_resp.json.return_value = {"data": {"items": raw_items}}
        with patch("scanner.api._request_with_retry", return_value=fake_resp):
            items = _fetch_minute_data(MagicMock(), "300001")
        assert items is not None
        assert all(isinstance(it, dict) for it in items)
        assert items[0]["current"] == 10.0
        assert items[0]["avg_price"] == pytest.approx(10.0)
        # 消费者按 dict 访问不应崩溃
        score = analyze_intraday(MagicMock(), "300001")
        assert isinstance(score, float)
        opening = analyze_opening_strength(MagicMock(), "300001")
        assert opening is not None


class TestFetchMarketCapsCoercion:
    """回归：batch quote 各字段强转 float，字符串/缺失不抛 TypeError（拖垮扫描）。"""

    def _fake_resp(self, payload):
        r = MagicMock()
        r.json.return_value = payload
        return r

    def test_market_caps_values_coerced(self):
        from scanner.api import fetch_market_caps_batch
        payload = {
            "data": {
                "SZ300001": {"quote": {
                    "symbol": "SZ300001",
                    "market_capital": "5000000000",
                    "float_market_capital": "3000000000",
                    "turnover_rate": "5.5",
                    "current": "12.5",
                    "percent": "3.2",
                }},
                "SZ300002": {"quote": {"symbol": "SZ300002"}},  # 缺数值字段 → 0.0
            }
        }
        with patch("scanner.api._request_with_retry", return_value=self._fake_resp(payload)):
            result = fetch_market_caps_batch(MagicMock(), ["SZ300001", "SZ300002"])
        q1 = result["SZ300001"]
        assert q1["market_cap"] == 5000000000.0 and isinstance(q1["market_cap"], float)
        assert q1["circ_market_cap"] == 3000000000.0
        assert q1["turnover_rate"] == 5.5
        assert q1["current"] == 12.5 and q1["percent"] == 3.2
        q2 = result["SZ300002"]
        assert q2["market_cap"] == 0.0 and q2["percent"] == 0.0

    def test_items_array_form_coerced(self):
        from scanner.api import fetch_market_caps_batch
        payload = {
            "data": {
                "items": [{"quote": {
                    "symbol": "SZ300003",
                    "market_capital": "1000000000",
                    "float_market_capital": "800000000",
                    "turnover_rate": 2.0,
                    "current": 20.0,
                    "percent": "1.5",
                }}]
            }
        }
        with patch("scanner.api._request_with_retry", return_value=self._fake_resp(payload)):
            result = fetch_market_caps_batch(MagicMock(), ["SZ300003"])
        assert result["SZ300003"]["current"] == 20.0
        assert result["SZ300003"]["percent"] == 1.5
        assert isinstance(result["SZ300003"]["percent"], float)


class TestFetchListSoftErrorCircuitBreaker:
    """软错误（HTTP 200 但 data 缺失/为空）必须按失败计，不得重置熔断计数。"""

    def _fake_resp(self, payload):
        r = MagicMock()
        r.json.return_value = payload
        return r

    def test_biaosheng_empty_data_not_success(self):
        from scanner.api import fetch_biaosheng, _biaosheng_cb
        _biaosheng_cb["failures"] = 0
        _biaosheng_cb["cooldown_until"] = 0
        _biaosheng_cb["cached"] = []
        resp = self._fake_resp({"error_code": 40016, "error_description": "too many requests"})
        with patch("scanner.api._request_with_retry", return_value=resp):
            r = fetch_biaosheng(MagicMock(), 100)
        assert r == []
        assert _biaosheng_cb["failures"] == 1, "软错误应按失败计，熔断计数递增"

    def test_biaosheng_real_data_is_success(self):
        from scanner.api import fetch_biaosheng, _biaosheng_cb
        _biaosheng_cb["failures"] = 0
        _biaosheng_cb["cooldown_until"] = 0
        _biaosheng_cb["cached"] = []
        resp = self._fake_resp({"data": {"items": [{"symbol": "SZ300001"}]}})
        with patch("scanner.api._request_with_retry", return_value=resp):
            r = fetch_biaosheng(MagicMock(), 100)
        assert r == [{"symbol": "SZ300001"}]
        assert _biaosheng_cb["failures"] == 0, "正常数据重置失败计数"

    def test_hot_list_empty_data_not_success(self):
        from scanner.api import fetch_xueqiu_hot_list, _xueqiu_hot_cb
        _xueqiu_hot_cb["failures"] = 0
        _xueqiu_hot_cb["cooldown_until"] = 0
        _xueqiu_hot_cb["cached"] = []
        resp = self._fake_resp({"error_code": 40016})
        with patch("scanner.api._request_with_retry", return_value=resp):
            r = fetch_xueqiu_hot_list(MagicMock(), 100)
        assert r == []
        assert _xueqiu_hot_cb["failures"] == 1, "软错误应按失败计，熔断计数递增"

    def test_hot_list_real_data_is_success(self):
        from scanner.api import fetch_xueqiu_hot_list, _xueqiu_hot_cb
        _xueqiu_hot_cb["failures"] = 0
        _xueqiu_hot_cb["cooldown_until"] = 0
        _xueqiu_hot_cb["cached"] = []
        resp = self._fake_resp({"data": {"items": [{"symbol": "SZ300002"}]}})
        with patch("scanner.api._request_with_retry", return_value=resp):
            r = fetch_xueqiu_hot_list(MagicMock(), 100)
        assert r == [{"symbol": "SZ300002"}]
        assert _xueqiu_hot_cb["failures"] == 0, "正常数据重置失败计数"


class TestFetchKlineCoercion:
    """回归：K 线 OHLCV/percent 必须强转数值，字符串/None/NaN 不再漏进下游
    closes 算术（此前 close=None 落库后 analyze_short_term 减法抛 TypeError）。"""

    def _fake_resp(self, payload):
        r = MagicMock()
        r.json.return_value = payload
        return r

    def test_ohlcv_values_coerced(self):
        from scanner.api import fetch_kline
        # 列序: [ts, volume, open, high, low, close, chg, percent, ...]
        payload = {"data": {"item": [
            [1609459200000, "500", "10.5", 11.0, "10.0", 10.8, 1.0, "2.3"],
        ]}}
        with patch("scanner.api._request_with_retry", return_value=self._fake_resp(payload)):
            kline = fetch_kline(MagicMock(), "SZ300001", days=15)
        assert kline is not None and len(kline) == 1
        bar = kline[0]
        assert isinstance(bar["open"], float) and bar["open"] == 10.5
        assert isinstance(bar["close"], float) and bar["close"] == 10.8
        assert isinstance(bar["volume"], float) and bar["volume"] == 500.0
        assert isinstance(bar["percent"], float) and bar["percent"] == 2.3

    def test_none_and_nan_ohlcv_do_not_crash(self):
        from scanner.api import fetch_kline
        payload = {"data": {"item": [
            [1609459200000, None, None, None, None, None, None, None],
            [1609459200000, float("nan"), "10.5", 11.0, "10.0", 10.8, 1.0, "2.3"],
        ]}}
        with patch("scanner.api._request_with_retry", return_value=self._fake_resp(payload)):
            kline = fetch_kline(MagicMock(), "SZ300001", days=15)
        assert kline is not None
        # 2026-08-11 契约收紧（重构 P0-1）：make_kline_bar 统一剔除 close<=0 脏 bar
        # （与 get_cached_kline 口径一致，此前 fetch_kline 保留 close=0 靠 analyze_* 兜底，
        # 两个生产端行为不一致）。None bar 的 close 归 0 → 被剔除；NaN/字符串 bar 正常强转保留。
        assert len(kline) == 1
        assert kline[0]["open"] == 10.5 and kline[0]["close"] == 10.8

    def test_short_item_skipped(self):
        from scanner.api import fetch_kline
        # 缺列 item（长度 <8）应跳过该根，不抛 IndexError
        payload = {"data": {"item": [
            [1609459200000, "500"],
            [1609459200000, "500", "10.5", 11.0, "10.0", 10.8, 1.0, "2.3"],
        ]}}
        with patch("scanner.api._request_with_retry", return_value=self._fake_resp(payload)):
            kline = fetch_kline(MagicMock(), "SZ300001", days=15)
        assert kline is not None and len(kline) == 1
        assert kline[0]["close"] == 10.8

    def test_malformed_timestamp_bar_skipped_not_abort(self):
        """回归：时间戳为 None/字符串等脏值时应跳过该根 bar，而不是抛异常
        拖垮整只票的 K 线解析（datetime.fromtimestamp 对 None/str 抛 TypeError）。"""
        from scanner.api import fetch_kline
        good_ts = 1609459200000
        payload = {"data": {"item": [
            [None, "500", "10.5", 11.0, "10.0", 10.8, 1.0, "2.3"],
            ["not-a-number", "500", "10.5", 11.0, "10.0", 10.8, 1.0, "2.3"],
            [good_ts, "500", "10.5", 11.0, "10.0", 10.8, 1.0, "2.3"],
        ]}}
        with patch("scanner.api._request_with_retry", return_value=self._fake_resp(payload)):
            kline = fetch_kline(MagicMock(), "SZ300001", days=15)
        assert kline is not None and len(kline) == 1
        assert kline[0]["close"] == 10.8
        assert kline[0]["timestamp"] == good_ts

    def test_negative_or_zero_timestamp_bar_skipped(self):
        """回归：时间戳为 0/负值（无法映射为有效交易日）的 bar 同样跳过，
        避免产出 1970 年脏日期。"""
        from scanner.api import fetch_kline
        good_ts = 1609459200000
        payload = {"data": {"item": [
            [0, "500", "10.5", 11.0, "10.0", 10.8, 1.0, "2.3"],
            [-1, "500", "10.5", 11.0, "10.0", 10.8, 1.0, "2.3"],
            [good_ts, "500", "10.5", 11.0, "10.0", 10.8, 1.0, "2.3"],
        ]}}
        with patch("scanner.api._request_with_retry", return_value=self._fake_resp(payload)):
            kline = fetch_kline(MagicMock(), "SZ300001", days=15)
        assert kline is not None and len(kline) == 1
        assert kline[0]["close"] == 10.8


class TestNumInf:
    """回归：_num 对 ±inf 必须按 0 处理（此前仅判 `f != f`，inf 会漏进下游）。

    Python json 默认可解析 JSON 字面量 NaN/Infinity，inf 与数值比较恒为真/假，
    会绕过 s.current > MAX_STOCK_PRICE 等越界判断，与 NaN 同族。
    """

    def test_inf_coerced_to_default(self):
        from scanner.api import _num
        assert _num(float("inf")) == 0.0
        assert _num(float("-inf")) == 0.0

    def test_inf_string_coerced(self):
        from scanner.api import _num
        assert _num("Infinity") == 0.0
        assert _num("-Infinity") == 0.0

    def test_finite_unchanged(self):
        from scanner.api import _num
        assert _num("5.23") == 5.23
        assert _num(None) == 0.0
        assert _num(float("nan")) == 0.0


class TestFetchMarketIndexCoercion:
    """回归：大盘指数涨幅列偶发字符串/NaN 时强转，否则 enhancer 比较抛 TypeError
    拖垮整轮扫描（enhancer._record_dimensions: market_idx_pct > MARKET_STRONG_THRESHOLD）。"""

    def _fake_resp(self, item):
        r = MagicMock()
        r.json.return_value = {"data": {"item": [item]}}
        return r

    def test_string_pct_coerced(self):
        import scanner.api as api
        # 列序: [ts, volume, open, high, low, close, chg, percent, ...]
        with patch("scanner.api._request_with_retry",
                   return_value=self._fake_resp([1700000000000, 1, 10, 11, 9, 10, 0.1, "-0.12", 1.2])):
            api._market_index_cache = (None, 0)  # 清缓存防跨测试污染
            pct = api.fetch_market_index(MagicMock())
        assert isinstance(pct, float) and pct == pytest.approx(-0.12)
        # 下游比较不再崩溃
        assert (pct > 0.5) is False

    def test_nan_pct_coerced(self):
        import scanner.api as api
        with patch("scanner.api._request_with_retry",
                   return_value=self._fake_resp([1700000000000, 1, 10, 11, 9, 10, 0.1, float("nan"), 1.2])):
            api._market_index_cache = (None, 0)
            pct = api.fetch_market_index(MagicMock())
        assert pct == 0.0  # NaN → 0（中性，不触发大盘强弱标签）

    def test_none_pct_returns_none(self):
        import scanner.api as api
        with patch("scanner.api._request_with_retry",
                   return_value=self._fake_resp([1700000000000, 1, 10, 11, 9, 10, 0.1, None, 1.2])):
            api._market_index_cache = (None, 0)
            pct = api.fetch_market_index(MagicMock())
        assert pct is None


class TestSessionExpirySelfHeal:
    """回归（2026-08-12）：长驻进程雪球 cookie 失效后，所有 API 请求返回 401/403/
    登录页 HTML，K 线补拉静默失败导致列表饿死。现于 _request_with_retry 统一自愈：
    检测失效信号 → 清 cookie 重建 → 重试一次。"""

    class _FakeCookies:
        def __init__(self):
            self.cleared = False

        def clear(self):
            self.cleared = True

    class _FakeResp:
        def __init__(self, status_code, content_type="application/json",
                     url="https://stock.xueqiu.com/v5/stock/chart/kline.json"):
            self.status_code = status_code
            self.headers = {"Content-Type": content_type}
            self.url = url

        def raise_for_status(self):
            if self.status_code >= 400:
                raise Exception(f"HTTP {self.status_code}")

    class _FakeSession:
        def __init__(self, responses):
            self.responses = list(responses)
            self.cookies = TestSessionExpirySelfHeal._FakeCookies()
            self.calls = []

        def get(self, url, timeout=None):
            self.calls.append(url)
            if "xueqiu.com/hq" in url:
                # 重建握手请求单独返回（不消耗 API 响应队列）
                return TestSessionExpirySelfHeal._FakeResp(200)
            return self.responses.pop(0)

    def test_401_triggers_rebuild_and_retries(self):
        from scanner.api import _request_with_retry
        sess = self._FakeSession([self._FakeResp(401), self._FakeResp(200)])
        resp = _request_with_retry(sess, "https://stock.xueqiu.com/v5/x", max_retries=2)
        assert resp.status_code == 200
        assert sess.cookies.cleared
        # 重建握手请求发给 /hq
        assert any("xueqiu.com/hq" in u for u in sess.calls)

    def test_html_login_page_triggers_rebuild(self):
        from scanner.api import _request_with_retry
        sess = self._FakeSession([
            self._FakeResp(200, content_type="text/html; charset=utf-8",
                           url="https://passport.xueqiu.com/"),
            self._FakeResp(200),
        ])
        resp = _request_with_retry(sess, "https://stock.xueqiu.com/v5/x", max_retries=2)
        assert resp.status_code == 200
        assert sess.cookies.cleared

    def test_normal_json_no_rebuild(self):
        from scanner.api import _request_with_retry
        sess = self._FakeSession([self._FakeResp(200)])
        resp = _request_with_retry(sess, "https://stock.xueqiu.com/v5/x", max_retries=2)
        assert resp.status_code == 200
        assert not sess.cookies.cleared

    def test_server_error_does_not_rebuild(self):
        # 5xx 是服务端问题，非 cookie 失效，走既有重试而非重建 session
        from scanner.api import _request_with_retry
        sess = self._FakeSession([self._FakeResp(500), self._FakeResp(200)])
        resp = _request_with_retry(sess, "https://stock.xueqiu.com/v5/x", max_retries=2)
        assert resp.status_code == 200
        assert not sess.cookies.cleared

    def test_rebuild_only_once(self):
        # 重建后仍 401 → 不再重复重建，按原逻辑抛错（防重建风暴）
        import pytest
        from scanner.api import _request_with_retry
        sess = self._FakeSession([self._FakeResp(401), self._FakeResp(401), self._FakeResp(401)])
        with pytest.raises(Exception):
            _request_with_retry(sess, "https://stock.xueqiu.com/v5/x", max_retries=3)
        # 只重建过一次（重建握手 + 3 次 API 请求 = 4 次 get）
        assert sess.calls.count("https://xueqiu.com/hq") == 1
