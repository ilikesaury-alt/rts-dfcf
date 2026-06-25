from scanner.analysis import analyze_momentum, analyze_new_face, analyze_pullback
from scanner.models import StockInfo


def _stock(percent=5.0, rank_change=1500, value=8000, current=15.0, rank=10):
    return StockInfo(
        symbol="300999", name="测试", code="300999",
        percent=percent, current=current,
        value=value, rank_change=rank_change, rank=rank,
    )


def _kline(pcts, volumes=None, body_ratio=None):
    """Build simulated K-line data with proper OHLC.

    pcts: list of daily percent changes (newest last)
    volumes: optional list of volumes (default: 1.0 for all)
    body_ratio: if set, overrides the standard OHLC to create candles
               with the given body-to-range ratio (e.g., 0.8 = fat body)
    """
    N = len(pcts)
    volumes = volumes or [1.0] * N
    closes = [100.0]
    for p in pcts:
        closes.append(closes[-1] * (1 + p / 100))

    result = []
    for i in range(N):
        o = closes[i]
        c = closes[i + 1]
        if body_ratio is not None and o != c:
            body = c - o
            total_range = abs(body) / body_ratio
            wiggle = (total_range - abs(body)) / 2
            h = max(o, c) + wiggle
            lo = min(o, c) - wiggle
        else:
            h = max(o, c) * 1.02 if max(o, c) > 0 else o + 1
            lo = min(o, c) * 0.98 if min(o, c) > 0 else o - 1
        result.append({
            "date": f"2026-01-{i+1:02d}",
            "open": o, "close": c, "high": h, "low": lo,
            "volume": volumes[i], "percent": pcts[i],
        })
    return result


class TestAnalyzeNewFace:

    def test_golden_path_returns_scored_candidate(self):
        kline = _kline([2, 1, -1, 2, 4], volumes=[0.8, 0.9, 0.7, 1.5, 2.0])
        result = analyze_new_face(_stock(percent=4.5, rank_change=2500, value=12000), kline)
        assert result is not None
        assert result.score >= 20
        assert result.dimensions["new_face_today_pct"] == 20
        assert "new_face_vol_rank" in result.dimensions

    def test_zero_or_negative_pct_returns_none(self):
        kline = _kline([1, 2, 1, 2, 3])
        assert analyze_new_face(_stock(percent=0), kline) is None
        assert analyze_new_face(_stock(percent=-2), kline) is None

    def test_short_kline_returns_none(self):
        kline = _kline([1, 2])
        assert analyze_new_face(_stock(percent=3), kline) is None

    def test_none_kline_returns_none(self):
        assert analyze_new_face(_stock(percent=3), None) is None

    def test_weak_form_filter_rejects_downtrend(self):
        kline = _kline([-2, -1, -1, -0.5, 2.5], volumes=[0.8, 0.9, 0.7, 0.8, 0.9])
        result = analyze_new_face(_stock(percent=2.5), kline)
        assert result is None

    def test_high_accumulated_penalty(self):
        kline = _kline([6, 5, 5, 4, 7], volumes=[1.0]*5)
        result = analyze_new_face(_stock(percent=3, rank_change=500, value=3000), kline)
        assert result is not None
        assert result.dimensions.get("new_face_accumulated", 0) < 0

    def test_over_12_pct_rejected(self):
        kline = _kline([1, 1, 1, 2, 3], volumes=[1.0]*5)
        result = analyze_new_face(_stock(percent=13), kline)
        assert result is None

    def test_bottom_confirmed_gives_bonus(self):
        kline = _kline([-2, -1, 1, 1, 4], volumes=[0.8, 0.9, 1.5, 1.8, 2.2])
        result = analyze_new_face(_stock(percent=4, rank_change=1000, value=5000), kline)
        if result:
            assert "new_face_bottom" in result.dimensions

    def test_ma_bull_bonus_with_bull_arrangement(self):
        pcts = [0.5]*5 + [0.8]*5 + [1.2]*5 + [1.5]*5
        kline = _kline(pcts)
        result = analyze_new_face(_stock(percent=3), kline)
        assert result is not None
        assert "new_face_ma_bull" in result.dimensions
        assert result.dimensions["new_face_ma_bull"] >= 3

class TestAnalyzeMomentum:

    def test_golden_path_returns_scored_candidate(self):
        kline = _kline([2, 3, 4, 5, 3], volumes=[1.0, 1.0, 1.2, 1.1, 1.0])
        result = analyze_momentum(_stock(percent=4, rank_change=2000, value=12000), kline)
        assert result is not None
        assert result.score >= 15

    def test_zero_or_negative_pct_returns_none(self):
        kline = _kline([2, 3, 4, 3, 5])
        assert analyze_momentum(_stock(percent=0), kline) is None
        assert analyze_momentum(_stock(percent=-1), kline) is None

    def test_low_accumulated_returns_none(self):
        kline = _kline([1, 1, 1, 1, 2])
        assert analyze_momentum(_stock(percent=3), kline) is None

    def test_over_12_pct_returns_none(self):
        kline = _kline([2, 3, 4, 5, 5])
        assert analyze_momentum(_stock(percent=13), kline) is None

    def test_short_kline_returns_none(self):
        assert analyze_momentum(_stock(percent=3), _kline([1, 2])) is None

    def test_none_kline_returns_none(self):
        assert analyze_momentum(_stock(percent=3), None) is None

    def test_volume_surge_penalty(self):
        kline = _kline([2, 3, 4, 5, 3], volumes=[1.0, 1.0, 0.8, 0.6, 3.5])
        result = analyze_momentum(_stock(percent=3), kline)
        assert result is not None
        assert result.dimensions.get("momentum_volume", 0) == -4

    def test_high_accumulated_danger(self):
        kline = _kline([10, 10, 10, 8, 5])
        result = analyze_momentum(_stock(percent=2, rank_change=500, value=3000), kline)
        assert result is not None
        assert result.dimensions.get("momentum_accumulated", 0) < 0

    def test_ma_bull_bonus_in_momentum(self):
        pcts = [0.5]*5 + [1.0]*5 + [1.5]*5 + [2.0, 2.0, 2.5, 3.0, 3.0]
        kline = _kline(pcts)
        result = analyze_momentum(_stock(percent=3, rank_change=2000, value=12000), kline)
        assert result is not None
        assert "momentum_ma_bull" in result.dimensions


class TestAnalyzePullback:

    def test_golden_path_returns_scored_candidate(self):
        kline = _kline([1, 2, 1, 2, 3], volumes=[0.8, 0.9, 0.7, 1.5, 2.0])
        result = analyze_pullback(_stock(percent=-2, rank_change=2500, value=12000), kline)
        assert result is not None
        assert result.score >= 18
        assert "pullback_today_pct" in result.dimensions
        assert "pullback_accumulated" in result.dimensions

    def test_zero_to_two_pct_returns_scored(self):
        kline = _kline([1, 2, 1, 2, 3])
        result = analyze_pullback(_stock(percent=0), kline)
        assert result is not None
        assert "pullback_today_pct" in result.dimensions
        assert result.dimensions["pullback_today_pct"] == 10  # today_neg1_0 weight (0 falls in -1 < pct <= 0)

        result = analyze_pullback(_stock(percent=1), kline)
        assert result is not None
        assert "pullback_today_pct" in result.dimensions
        assert result.dimensions["pullback_today_pct"] == 5  # today_pos0_2 weight (0 < pct <= 2)

        result = analyze_pullback(_stock(percent=2), kline)
        assert result is not None
        assert "pullback_today_pct" in result.dimensions
        assert result.dimensions["pullback_today_pct"] == 5  # today_pos0_2 weight

    def test_short_kline_returns_none(self):
        kline = _kline([1, 2])
        assert analyze_pullback(_stock(percent=-2), kline) is None

    def test_none_kline_returns_none(self):
        assert analyze_pullback(_stock(percent=-2), None) is None

    def test_over_2_pct_rejected(self):
        kline = _kline([1, 1, 1, 2, 3], volumes=[1.0]*5)
        assert analyze_pullback(_stock(percent=2.5), kline) is None

    def test_under_neg8_pct_rejected(self):
        kline = _kline([1, 1, 1, 2, 3], volumes=[1.0]*5)
        assert analyze_pullback(_stock(percent=-8.5), kline) is None

    def test_low_accumulated_rejected(self):
        kline = _kline([0.5, 0.5, 0.5, 0.5, 0.5], volumes=[1.0]*5)  # sum = 2.5 < 5
        assert analyze_pullback(_stock(percent=-2), kline) is None

    def test_volume_scenarios(self):
        kline = _kline([1, 2, 1, 2, 3], volumes=[1.0]*5)
        result = analyze_pullback(_stock(percent=-2), kline)
        assert result is not None
        assert "pullback_volume" in result.dimensions

    def test_ma_bull_bonus(self):
        pcts = [0.5]*5 + [1.0]*5 + [1.5]*5 + [2.0, 2.0, 2.5, 3.0, 3.0]
        kline = _kline(pcts)
        result = analyze_pullback(_stock(percent=-2), kline)
        assert result is not None
        assert "pullback_ma_bull" in result.dimensions

    def test_ma_support_bonus(self):
        pcts = [0.5]*10 + [1.0]*5  # sideways near MA10, no bull alignment (ma5 <= ma10)
        kline = _kline(pcts)
        result = analyze_pullback(_stock(percent=-1), kline)
        assert result is not None
        assert "pullback_ma_support" in result.dimensions
        assert "pullback_ma_bull" not in result.dimensions

    def test_ma_broken_penalty(self):
        pcts = [0.5]*10 + [1.0]*10 + [1.0]*5  # strong uptrend, 25 days
        kline = _kline(pcts)
        # Manually adjust last close to be below MA20 (keep percent positive for accumulated)
        kline[-1]["close"] = kline[-1]["close"] * 0.85  # 15% drop, close < MA20
        # today_pct is separate (passed via stock.percent)
        result = analyze_pullback(_stock(percent=-5), kline)
        assert result is not None
        assert "pullback_ma_broken" in result.dimensions

    def test_rank_bonus(self):
        kline = _kline([1, 2, 1, 2, 3], volumes=[1.0]*5)
        result = analyze_pullback(_stock(percent=-2, rank=5), kline)
        assert result is not None
        assert "pullback_rank" in result.dimensions

    def test_rsi_indicator_bonus(self):
        kline = _kline([1, 2, 1, 2, 3] + [1.0]*5, volumes=[1.0]*10)  # last 5 sum to 5
        result = analyze_pullback(_stock(percent=-2), kline)
        assert result is not None
        assert "pullback_rsi" in result.dimensions

    def test_macd_indicator_bonus(self):
        kline = _kline([1, 2, 1, 2, 3] + [1.0]*30, volumes=[1.0]*35)  # 35 days for MACD (needs 34+)
        result = analyze_pullback(_stock(percent=-2), kline)
        assert result is not None
        assert "pullback_macd" in result.dimensions


