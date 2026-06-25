from scanner.config import (
    V_MO_MA_FULL,
    V_NF_HL_CLEAR,
    V_NF_HL_FAIL,
    V_NF_SECTOR_STRONG,
    V_PB_SHRINK_NO,
    V_PB_SHRINK_YES,
)
from scanner.models import KlineSummary, StockInfo
from scanner.validator import (
    validate,
    _mo_divergence,
    _mo_ma_alignment,
    _mo_volume_uniformity,
    _nf_convergence,
    _nf_higher_low,
    _nf_sector,
    _pb_ma_trend,
    _pb_shrinkage,
    _pb_sector,
    validate_momentum,
    validate_nf,
    validate_pullback,
)


def _kline(pcts, volumes=None):
    N = len(pcts)
    volumes = volumes or [1.0] * N
    closes = [100.0]
    for p in pcts:
        closes.append(closes[-1] * (1 + p / 100))

    result = []
    for i in range(N):
        o = closes[i]
        c = closes[i + 1]
        h = max(o, c) * 1.02 if max(o, c) > 0 else o + 1
        lo = min(o, c) * 0.98 if min(o, c) > 0 else o - 1
        result.append({
            "date": f"2026-01-{i+1:02d}",
            "open": o, "close": c, "high": h, "low": lo,
            "volume": volumes[i], "percent": pcts[i],
        })
    return result


def _stock(name="测试"):
    return StockInfo(
        symbol="300999", name=name, code="300999",
        percent=4.0, current=15.0,
        value=8000, rank_change=1500, rank=10,
    )


SEMICONDUCTOR_CLUSTER = {"半导体": ["300001", "300002", "300003"]}


class TestValidateNewFaceHelpers:

    def test_convergence_returns_valid_with_34_bars(self):
        pcts = [0.3]*15 + [-3, -4, -5, -3, -2, -1, 1, 2, 3, 2, 4]*2
        k = _kline(pcts, volumes=[1.0]*37)
        closes = [c["close"] for c in k[:-1]]
        bonus, detail = _nf_convergence(closes, k[:-1])
        assert isinstance(bonus, int) and isinstance(detail, str)
        assert "data_short" not in detail

    def test_higher_low_detects_improvement(self):
        pcts = [-2]*8 + [0.5]*7  # recent zone higher than prev zone
        k = _kline(pcts, volumes=[1.0]*15)
        closes = [c["close"] for c in k[:-1]]
        bonus, detail = _nf_higher_low(closes)
        assert bonus == V_NF_HL_CLEAR, f"expected clear HL, got {bonus} ({detail})"

    def test_higher_low_continued_decline(self):
        pcts = [-0.5]*10 + [-2]*5  # recent zone lower than prev zone
        k = _kline(pcts, volumes=[1.0]*15)
        closes = [c["close"] for c in k[:-1]]
        bonus, detail = _nf_higher_low(closes)
        assert bonus == V_NF_HL_FAIL, f"expected HL fail, got {bonus} ({detail})"

    def test_sector_strong(self):
        bonus, count = _nf_sector("半导体测试", SEMICONDUCTOR_CLUSTER)
        assert bonus == V_NF_SECTOR_STRONG
        assert count == 3

    def test_sector_weak(self):
        bonus, count = _nf_sector("测试", {"医疗": ["300001"]})
        assert count <= 1

    def test_sector_none(self):
        bonus, count = _nf_sector("测试", None)
        assert count == 0


class TestValidateNewFace:

    def test_all_three_pass(self):
        pcts = [0.3]*15 + [-3, -4, -5, -2, -1, 0.5, 1.5, 2.5, 2.0, 4.0]*2
        k = _kline(pcts, volumes=[1.0]*35)
        closes = [c["close"] for c in k[:-1]]
        passed, total, dims = validate_nf(
            _stock(name="半导体测试"), None, closes, k[:-1],
            SEMICONDUCTOR_CLUSTER
        )
        assert passed, f"should pass, total={total}, dims={dims}"
        assert dims["v_nf_sector"] == V_NF_SECTOR_STRONG

    def test_short_kline(self):
        k = _kline([1, 2, 3], volumes=[1.0]*3)
        closes = [c["close"] for c in k[:-1]]
        passed, total, dims = validate_nf(_stock(), None, closes, k[:-1], None)
        assert not passed


class TestValidateMomentumHelpers:

    def test_ma_full_alignment(self):
        pcts = [0.5]*21
        k = _kline(pcts, volumes=[1.0]*21)
        closes = [c["close"] for c in k[:-1]]
        bonus, detail = _mo_ma_alignment(closes)
        assert bonus == V_MO_MA_FULL, f"expected full alignment, got {bonus} ({detail})"

    def test_divergence_none(self):
        pcts = [0.5]*5 + [1.0]*5 + [1.5]*5 + [2.0, 2.0, 2.5, 3.0, 3.0]
        k = _kline(pcts, volumes=[1.0]*20)
        closes = [c["close"] for c in k[:-1]]
        bonus, detail = _mo_divergence(closes, k[:-1])
        assert bonus > 0, f"expected no divergence (bonus>0), got {bonus} ({detail})"

    def test_volume_uniformity_good(self):
        k = _kline([0.5]*15, volumes=[1.0, 1.2, 1.4, 1.5, 1.6]*3)
        bonus, detail = _mo_volume_uniformity(k[:-1])
        assert bonus > 0, f"expected positive bonus, got {bonus} ({detail})"


class TestValidateMomentum:

    def test_full_bull_passes(self):
        pcts = [0.5]*5 + [1.0]*5 + [1.5]*5 + [2.0, 2.0, 2.5, 3.0, 3.0]
        k = _kline(pcts, volumes=[1.0]*20)
        closes = [c["close"] for c in k[:-1]]
        passed, total, dims = validate_momentum(_stock(), None, closes, k[:-1], None)
        assert passed, f"should pass, total={total}, dims={dims}"

    def test_short_kline(self):
        k = _kline([1, 2, 3], volumes=[1.0]*3)
        closes = [c["close"] for c in k[:-1]]
        passed, total, dims = validate_momentum(_stock(), None, closes, k[:-1], None)
        assert not passed


class TestValidatePullbackHelpers:

    def test_ma_trend_up(self):
        pcts = [1.0]*26
        k = _kline(pcts, volumes=[1.0]*26)
        closes = [c["close"] for c in k[:-1]]
        bonus, detail = _pb_ma_trend(closes)
        assert bonus > 0, f"expected ma up, got {bonus} ({detail})"

    def test_ma_trend_down(self):
        pcts = [-1.0]*26
        k = _kline(pcts, volumes=[1.0]*26)
        closes = [c["close"] for c in k[:-1]]
        bonus, detail = _pb_ma_trend(closes)
        assert bonus < 0, f"expected ma down, got {bonus} ({detail})"

    def test_shrinkage_yes(self):
        ks = KlineSummary(
            trend="缩量回调", accumulated_pct=15.0, volume_ratio=0.5,
            bottom_confirmed=True, score=20, avg_volume=1.0,
        )
        bonus, vr = _pb_shrinkage(ks)
        assert bonus == V_PB_SHRINK_YES, f"expected shrink yes, got {bonus}"

    def test_shrinkage_no(self):
        ks = KlineSummary(
            trend="回踩整理", accumulated_pct=18.0, volume_ratio=1.3,
            bottom_confirmed=True, score=16, avg_volume=1.0,
        )
        bonus, vr = _pb_shrinkage(ks)
        assert bonus == V_PB_SHRINK_NO, f"expected shrink no, got {bonus}"

    def test_sector_hot(self):
        bonus, count = _pb_sector("半导体测试", SEMICONDUCTOR_CLUSTER)
        assert bonus > 0
        assert count == 3

    def test_sector_dead(self):
        bonus, count = _pb_sector("测试", None)
        assert count == 0

    def test_sector_cold(self):
        bonus, count = _pb_sector("半导体测试", {"医疗": ["300001"]})
        assert count == 0
        assert bonus < 0


class TestValidatePullback:

    def test_healthy_pullback_passes(self):
        pcts = [1.0]*20 + [-1, -1.5, -2, -0.5, 0.0]
        vols = [1.0]*20 + [0.4, 0.5, 0.6, 0.7, 0.8]
        k = _kline(pcts, volumes=vols)
        closes = [c["close"] for c in k[:-1]]
        ks = KlineSummary(
            trend="缩量回调", accumulated_pct=15.0, volume_ratio=0.5,
            bottom_confirmed=True, score=20, avg_volume=1.0,
        )
        passed, total, dims = validate_pullback(
            _stock(name="半导体测试"), ks, closes, k[:-1],
            SEMICONDUCTOR_CLUSTER
        )
        assert passed, f"should pass, total={total}, dims={dims}"

    def test_short_kline(self):
        k = _kline([1, 2, 3], volumes=[1.0]*3)
        closes = [c["close"] for c in k[:-1]]
        ks = KlineSummary(
            trend="缩量回调", accumulated_pct=10.0, volume_ratio=0.5,
            bottom_confirmed=True, score=18, avg_volume=1.0,
        )
        passed, total, dims = validate_pullback(_stock(), ks, closes, k[:-1], None)
        assert not passed


class TestValidateDispatch:

    def test_dispatch_new_face(self):
        pcts = [0.3]*15 + [-3, -4, -5, -2, -1, 0.5, 1.5, 2.5, 2.0, 4.0]*2
        k = _kline(pcts, volumes=[1.0]*35)
        closes = [c["close"] for c in k[:-1]]
        ks = KlineSummary(
            trend="底部启动", accumulated_pct=-2.0, volume_ratio=1.8,
            bottom_confirmed=True, score=22, avg_volume=1.0,
        )
        passed, total, dims = validate(
            "new_face", _stock(name="半导体测试"),
            ks, closes, k[:-1], SEMICONDUCTOR_CLUSTER
        )
        assert isinstance(passed, bool)
        assert isinstance(total, int)

    def test_dispatch_momentum(self):
        pcts = [0.5]*5 + [1.0]*5 + [1.5]*5 + [2.0, 2.0, 2.5, 3.0, 3.0]
        k = _kline(pcts, volumes=[1.0]*20)
        closes = [c["close"] for c in k[:-1]]
        ks = KlineSummary(
            trend="动量延续", accumulated_pct=15.0, volume_ratio=1.2,
            bottom_confirmed=True, score=18, avg_volume=1.0,
        )
        passed, total, dims = validate(
            "momentum", _stock(), ks, closes, k[:-1], None
        )
        assert isinstance(passed, bool)

    def test_dispatch_pullback(self):
        pcts = [1.0]*20 + [-1, -1.5, -2, -0.5, 0.0]
        vols = [1.0]*20 + [0.4, 0.5, 0.6, 0.7, 0.8]
        k = _kline(pcts, volumes=vols)
        closes = [c["close"] for c in k[:-1]]
        ks = KlineSummary(
            trend="缩量回调", accumulated_pct=15.0, volume_ratio=0.5,
            bottom_confirmed=True, score=20, avg_volume=1.0,
        )
        passed, total, dims = validate(
            "pullback", _stock(name="半导体测试"),
            ks, closes, k[:-1], SEMICONDUCTOR_CLUSTER
        )
        assert isinstance(passed, bool)

    def test_unknown_category(self):
        passed, total, dims = validate("unknown", _stock(), None, [], [], None)
        assert not passed
        assert total == 0
        assert dims == {}
