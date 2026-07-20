from unittest.mock import MagicMock, PropertyMock, patch

from scanner.api import (
    _biaosheng_circuit_breaker,
    _fetch_minute_data,
    _normalize_minute_item,
    analyze_intraday,
    analyze_opening_strength,
    compute_surge_sentiment,
    estimate_live_volume,
)
from scanner.ths_api import ths_normalize


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


class TestThsNormalize:

    def test_basic_fields(self):
        item = {"code": "300999", "name": "测试", "rise_and_fall": 5.2,
                "hot_rank_chg": 100, "order": 10, "market": 33,
                "tag": {"concept_tag": ["芯片"], "popularity_tag": "热"}}
        r = ths_normalize(item)
        assert r["symbol"] == "SZ300999"
        assert r["name"] == "测试"
        assert r["percent"] == 5.2
        assert r["rank_change"] == 100
        assert r["rank"] == 10
        assert r["source_tag"] == "tonghuashun"
        assert r["concept_tags"] == ["芯片"]

    def test_sh_market(self):
        item = {"code": "600000", "market": 17}
        r = ths_normalize(item)
        assert r["symbol"] == "SH600000"

    def test_missing_code_returns_empty(self):
        r = ths_normalize({"name": "no code"})
        assert r == {}

    def test_missing_tag(self):
        item = {"code": "300001", "name": "A", "market": 33}
        r = ths_normalize(item)
        assert r["symbol"] == "SZ300001"
        assert r["concept_tags"] == []


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
        raw = [1609459200000, 500, 100.5, 101.0]
        d = _normalize_minute_item(raw)
        assert isinstance(d, dict)
        assert d["volume"] == 500
        assert d["current"] == 101.0
        assert d["avg_price"] == 100.5

    def test_dict_passthrough(self):
        raw = {"current": 10.0, "volume": 1}
        assert _normalize_minute_item(raw) is raw

    def test_short_array_safe(self):
        d = _normalize_minute_item([1])
        assert d["current"] == 0.0


class TestFetchMinuteDataRawArray:

    def test_raw_array_items_yield_dicts(self):
        raw_items = [[1609459200000 + i * 60000, 100, 10.0 + i, 10.0 + i] for i in range(30)]
        fake_resp = MagicMock()
        fake_resp.json.return_value = {"data": {"items": raw_items}}
        with patch("scanner.api._request_with_retry", return_value=fake_resp):
            items = _fetch_minute_data(MagicMock(), "300001")
        assert items is not None
        assert all(isinstance(it, dict) for it in items)
        # 消费者按 dict 访问不应崩溃
        score = analyze_intraday(MagicMock(), "300001")
        assert isinstance(score, float)
        opening = analyze_opening_strength(MagicMock(), "300001")
        assert opening is not None
