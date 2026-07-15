from tests.helpers import _kline  # noqa: E402

from scanner.config import (
    V_MO_MA_FULL,
    V_NF_HL_CLEAR,
    V_NF_HL_FAIL,
    V_NF_SECTOR_STRONG,
    V_PB_SHRINK_NO,
    V_PB_SHRINK_YES,
    V_ST_VOL_SURGE,
    V_ST_VOL_HEALTHY,
    V_ST_RANK_TOP10,
    V_ST_SECTOR_HOT,
    V_ST_MA_SUPPORT,
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
    validate_short_term,
)


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
        bonus, detail, hits = _nf_convergence(closes, k[:-1])
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

    def test_uptrend_without_oversold_rejected(self):
        # 上升中继股：higher_low + 板块共振都满足，但无超卖共振，
        # 按新规则不应冒充新面孔
        pcts = [1.0] * 20
        k = _kline(pcts, volumes=[1.0] * 20)
        closes = [c["close"] for c in k[:-1]]
        passed, total, dims = validate_nf(
            _stock(name="半导体测试"), None, closes, k[:-1],
            SEMICONDUCTOR_CLUSTER
        )
        assert not passed, f"无超卖信号不应通过 new_face，dims={dims}"
        assert dims["v_nf_convergence_hits"] == 0


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
        assert bonus == 0, f"expected neutral (no divergence => 0), got {bonus} ({detail})"

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
        assert bonus == 0


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

    def test_dispatch_short_term(self):
        k = _kline([5, 3, 6, 2, 4], volumes=[1.0, 1.2, 1.5, 1.8, 2.0])
        closes = [c["close"] for c in k[:-1]]
        ks = KlineSummary(
            trend="放量上攻", accumulated_pct=10.0, volume_ratio=1.5,
            bottom_confirmed=False, score=18, avg_volume=1.0,
        )
        passed, total, dims = validate(
            "short_term", _stock(name="半导体测试"),
            ks, closes, k[:-1], SEMICONDUCTOR_CLUSTER
        )
        assert isinstance(passed, bool)


class TestValidateShortTerm:

    def test_vol_ratio_below_1_rejected(self):
        k = _kline([5, 3, 6, 2, 4], volumes=[1.0, 1.0, 1.0, 1.0, 0.5])
        closes = [c["close"] for c in k[:-1]]
        ks = KlineSummary(
            trend="缩量", accumulated_pct=5.0, volume_ratio=0.5,
            bottom_confirmed=False, score=18, avg_volume=1.0,
        )
        passed, total, dims = validate_short_term(_stock(), ks, closes, k[:-1], None)
        assert not passed
        assert dims["v_st_vol_gate"] == "fail"

    def test_vol_surge_gives_bonus(self):
        k = _kline([5, 3, 6, 2, 4], volumes=[0.5, 0.6, 0.7, 0.8, 3.0])
        closes = [c["close"] for c in k[:-1]]
        ks = KlineSummary(
            trend="放量", accumulated_pct=5.0, volume_ratio=4.6,
            bottom_confirmed=False, score=18, avg_volume=1.0,
        )
        passed, total, dims = validate_short_term(
            _stock(name="半导体测试"), ks, closes, k[:-1],
            SEMICONDUCTOR_CLUSTER
        )
        assert passed
        assert dims["v_st_vol"] == V_ST_VOL_SURGE

    def test_healthy_volume_gives_bonus(self):
        k = _kline([5, 3, 6, 2, 4], volumes=[1.0, 1.0, 1.0, 1.0, 1.2])
        closes = [c["close"] for c in k[:-1]]
        ks = KlineSummary(
            trend="温和放量", accumulated_pct=5.0, volume_ratio=1.2,
            bottom_confirmed=False, score=18, avg_volume=1.0,
        )
        passed, total, dims = validate_short_term(_stock(), ks, closes, k[:-1], None)
        assert passed
        assert dims["v_st_vol"] == V_ST_VOL_HEALTHY

    def test_top10_rank_bonus(self):
        k = _kline([5, 3, 6, 2, 4], volumes=[1.0, 1.2, 1.5, 1.8, 2.0])
        closes = [c["close"] for c in k[:-1]]
        ks = KlineSummary(
            trend="上攻", accumulated_pct=5.0, volume_ratio=1.5,
            bottom_confirmed=False, score=18, avg_volume=1.0,
        )
        stock = StockInfo(symbol="300999", name="测试", code="300999",
                          percent=5.0, current=15.0, value=8000,
                          rank_change=1500, rank=5)
        passed, total, dims = validate_short_term(stock, ks, closes, k[:-1], None)
        assert passed
        assert dims["v_st_rank"] == V_ST_RANK_TOP10

    def test_weak_to_strong_passes_when_other_dims_negative(self):
        # 板块<3、排名>30、MA破位 全不达标，但弱转强应作为第4软维度放行
        k = _kline([5, 3, 6, 2, 4], volumes=[1.0, 1.0, 1.0, 1.0, 1.2])
        closes = [c["close"] for c in k[:-1]]
        ks = KlineSummary(
            trend="弱转强", accumulated_pct=5.0, volume_ratio=1.2,
            bottom_confirmed=False, score=18, avg_volume=1.0,
            dimensions={"st_weak_to_strong": 8},
        )
        stock = StockInfo(symbol="300999", name="测试", code="300999",
                          percent=5.0, current=15.0, value=8000,
                          rank_change=1500, rank=50)
        passed, total, dims = validate_short_term(stock, ks, closes, k[:-1], None)
        assert passed
        assert dims["v_st_weak"] == 8

    def test_hot_sector_bonus(self):
        k = _kline([5, 3, 6, 2, 4], volumes=[1.0, 1.2, 1.5, 1.8, 2.0])
        closes = [c["close"] for c in k[:-1]]
        ks = KlineSummary(
            trend="上攻", accumulated_pct=5.0, volume_ratio=1.5,
            bottom_confirmed=False, score=18, avg_volume=1.0,
        )
        passed, total, dims = validate_short_term(
            _stock(name="半导体测试"), ks, closes, k[:-1],
            SEMICONDUCTOR_CLUSTER
        )
        assert passed
        assert dims["v_st_sector"] == V_ST_SECTOR_HOT

    def test_ma_support_bonus(self):
        # Uptrend: rising closes so ma5 > ma10 and last close > ma5
        pcts = [0.5] * 25
        k = _kline(pcts, volumes=[1.0] * 25)
        closes = [c["close"] for c in k[:-1]]
        ks = KlineSummary(
            trend="上攻", accumulated_pct=5.0, volume_ratio=1.5,
            bottom_confirmed=False, score=18, avg_volume=1.0,
        )
        passed, total, dims = validate_short_term(_stock(), ks, closes, k[:-1], None)
        assert passed
        assert dims["v_st_ma"] == V_ST_MA_SUPPORT

    def test_single_positive_dimension_passes(self):
        # Only rank is positive, others neutral/cold — should still pass (pos_dims >= 1)
        k = _kline([5, 3, 6, 2, 4], volumes=[1.0, 1.0, 1.0, 1.0, 1.2])
        closes = [c["close"] for c in k[:-1]]
        ks = KlineSummary(
            trend="温和放量", accumulated_pct=5.0, volume_ratio=1.2,
            bottom_confirmed=False, score=18, avg_volume=1.0,
        )
        stock = StockInfo(symbol="300999", name="测试", code="300999",
                          percent=5.0, current=15.0, value=8000,
                          rank_change=1500, rank=5)
        passed, total, dims = validate_short_term(stock, ks, closes, k[:-1], None)
        assert passed, f"single positive dim should pass, total={total}"

    def test_all_dims_zero_or_negative_fails(self):
        # 板块冷(None) + 排名>30(负) + MA不足(<20根) + 非弱转强 → pos_dims=0 应淘汰
        k = _kline([5, 3, 6, 2, 4], volumes=[1.0, 1.0, 1.0, 1.0, 1.2])
        closes = [c["close"] for c in k[:-1]]
        ks = KlineSummary(
            trend="温和放量", accumulated_pct=5.0, volume_ratio=1.2,
            bottom_confirmed=False, score=18, avg_volume=1.0,
        )
        stock = StockInfo(symbol="300999", name="测试", code="300999",
                          percent=5.0, current=15.0, value=8000,
                          rank_change=1500, rank=50)
        passed, total, dims = validate_short_term(stock, ks, closes, k[:-1], None)
        assert not passed
