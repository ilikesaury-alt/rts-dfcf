from scanner.patterns import (
    detect_new_face_patterns,
    detect_momentum_patterns,
    detect_pullback_patterns,
    detect_short_term_patterns,
)


def _kline_manual(data: list[dict]) -> list[dict]:
    return data


class TestDetectNewFacePatterns:

    def test_engulfing(self):
        k = [
            {"open": 51, "close": 49, "high": 52, "low": 48},
            {"open": 48, "close": 53, "high": 54, "low": 47.5},
        ]
        score, dims = detect_new_face_patterns(k)
        assert score == 5
        assert dims["nf_pattern_engulfing"] == 5

    def test_engulfing_strict_cover(self):
        k = [
            {"open": 51, "close": 49, "high": 52, "low": 48},
            {"open": 48, "close": 51.5, "high": 52.0, "low": 47.5},
        ]
        score, dims = detect_new_face_patterns(k)
        assert score == 0, "close must > prev_high=52"
        assert dims == {}

    def test_hammer(self):
        k = [
            {"open": 48, "close": 47, "high": 48.5, "low": 46.5},
            {"open": 50, "close": 51, "high": 51.3, "low": 47.5},
        ]
        score, dims = detect_new_face_patterns(k)
        assert score == 4
        assert dims["nf_pattern_hammer"] == 4

    def test_hammer_not_when_engulfing_hits(self):
        k = [
            {"open": 50, "close": 48, "high": 51, "low": 47},
            {"open": 47, "close": 52, "high": 53, "low": 46},
        ]
        score, dims = detect_new_face_patterns(k)
        assert dims.get("nf_pattern_engulfing") == 5
        assert "nf_pattern_hammer" not in dims

    def test_three_bullish(self):
        k = [
            {"open": 48, "close": 47, "high": 49, "low": 46.5},
            {"open": 47, "close": 49, "high": 50, "low": 46},
            {"open": 49, "close": 51, "high": 52, "low": 48},
            {"open": 51, "close": 53, "high": 54, "low": 50},
        ]
        score, dims = detect_new_face_patterns(k)
        assert dims["nf_pattern_3bull"] == 3, f"got {dims}"
        assert score == 3

    def test_three_bullish_closes_not_increasing(self):
        k = [
            {"open": 47, "close": 49, "high": 50, "low": 46},
            {"open": 50, "close": 49, "high": 51, "low": 48},
            {"open": 49, "close": 53, "high": 54, "low": 48},
        ]
        score, dims = detect_new_face_patterns(k)
        assert score == 0

    def test_too_short_kline(self):
        k = [{"open": 50, "close": 51, "high": 52, "low": 49}]
        score, dims = detect_new_face_patterns(k)
        assert score == 0
        assert dims == {}

    def test_empty_kline(self):
        score, dims = detect_new_face_patterns([])
        assert score == 0
        assert dims == {}


class TestDetectMomentumPatterns:

    def test_breakout_3day_high(self):
        k = [
            {"open": 49, "close": 50, "high": 51},
            {"open": 50, "close": 49, "high": 50.5},
            {"open": 49, "close": 50.5, "high": 51.5},
            {"open": 51, "close": 52, "high": 52.5},
        ]
        score, dims = detect_momentum_patterns(k)
        assert dims["mo_pattern_breakout"] == 5
        assert score == 5

    def test_breakout_not_met(self):
        k = [
            {"open": 49, "close": 50, "high": 52},
            {"open": 50, "close": 51, "high": 51.5},
            {"open": 51, "close": 50, "high": 51},
            {"open": 50, "close": 51, "high": 52},
        ]
        score, dims = detect_momentum_patterns(k)
        assert score == 0

    def test_three_bullish(self):
        k = [
            {"open": 48, "close": 47, "high": 53, "low": 46.5},
            {"open": 47, "close": 49, "high": 52, "low": 46},
            {"open": 49, "close": 51, "high": 51, "low": 48},
            {"open": 51, "close": 53, "high": 53, "low": 50},
        ]
        score, dims = detect_momentum_patterns(k)
        assert dims["mo_pattern_3bull"] == 3

    def test_too_short_kline(self):
        k = [
            {"open": 50, "close": 51, "high": 52},
            {"open": 51, "close": 52, "high": 53},
            {"open": 52, "close": 53, "high": 54},
        ]
        score, dims = detect_momentum_patterns(k)
        assert score == 0

    def test_empty_kline(self):
        score, dims = detect_momentum_patterns([])
        assert score == 0
        assert dims == {}


class TestDetectPullbackPatterns:

    def test_engulfing(self):
        k = [
            {"open": 51, "close": 49, "high": 52, "low": 48},
            {"open": 48, "close": 53, "high": 54, "low": 47.5},
        ]
        score, dims = detect_pullback_patterns(k, vol_ratio=1.0)
        assert dims["pb_pattern_engulfing"] == 5

    def test_doji(self):
        k = [
            {"open": 51, "close": 49, "high": 52, "low": 48},
            {"open": 50, "close": 50.05, "high": 50.5, "low": 49.5},
        ]
        score, dims = detect_pullback_patterns(k, vol_ratio=0.5)
        assert dims["pb_pattern_doji"] == 4

    def test_doji_vol_too_high(self):
        k = [
            {"open": 51, "close": 49, "high": 52, "low": 48},
            {"open": 50, "close": 50.05, "high": 50.5, "low": 49.5},
        ]
        score, dims = detect_pullback_patterns(k, vol_ratio=0.9)
        assert score == 0, "vol_ratio must < 0.8"
        assert dims == {}

    def test_too_short_kline(self):
        k = [{"open": 50, "close": 51, "high": 52, "low": 49}]
        score, dims = detect_pullback_patterns(k, vol_ratio=1.0)
        assert score == 0

    def test_empty_kline(self):
        score, dims = detect_pullback_patterns([], vol_ratio=1.0)
        assert score == 0
        assert dims == {}


class TestDetectShortTermPatterns:

    def test_breakout_3day_high(self):
        k = [
            {"open": 49, "close": 50, "high": 51},
            {"open": 50, "close": 49, "high": 50.5},
            {"open": 49, "close": 50.5, "high": 51.5},
            {"open": 51, "close": 52, "high": 52.5},
        ]
        score, dims = detect_short_term_patterns(k)
        assert dims["st_pattern_breakout"] == 5

    def test_breakout_not_met(self):
        k = [
            {"open": 49, "close": 50, "high": 52},
            {"open": 50, "close": 51, "high": 51.5},
            {"open": 51, "close": 50, "high": 51},
            {"open": 50, "close": 51, "high": 52},
        ]
        score, dims = detect_short_term_patterns(k)
        assert score == 0

    def test_too_short_kline(self):
        k = [
            {"open": 50, "close": 51, "high": 52},
            {"open": 51, "close": 52, "high": 53},
            {"open": 52, "close": 53, "high": 54},
        ]
        score, dims = detect_short_term_patterns(k)
        assert score == 0

    def test_empty_kline(self):
        score, dims = detect_short_term_patterns([])
        assert score == 0
        assert dims == {}
