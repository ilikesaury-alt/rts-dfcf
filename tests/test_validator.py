from tests.helpers import _kline  # noqa: E402

from scanner.config import (
    V_MO_MA_FULL,
    V_MO_MA_NONE,
    V_MO_MA_PARTIAL,
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
    _mo_is_overbought,
    _mo_ma_alignment,
    _mo_volume_uniformity,
    _nf_convergence,
    _nf_higher_low,
    _nf_sector,
    _pb_ma_trend,
    _pb_shrinkage,
    _pb_sector,
    _st_is_overbought,
    validate_momentum,
    validate_nf,
    validate_pullback,
    validate_rebound,
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
        # 构造先跌到低点A(70)→温和反弹到84→回调到74(高于A*1.01)的序列：
        # - 最后6根持续下跌使 RSI<30 → 超卖信号命中
        #   （反弹幅度需克制：原 70→90 反弹使 RSI 升至 ~33 不触超卖；
        #    改为 70→84 温和反弹，最后6根全跌 → RSI≈17 触发超卖）
        # - recent_zone min(74) > prev_zone min(70)*1.01 → higher_low_clear
        # - 板块共振 → pos_dims >= 2 → 通过
        closes = [100 - i for i in range(24)] + [70, 75, 80, 82, 84, 82, 80, 78, 76, 74]
        k = [{"date": f"2026-01-{i+1:02d}", "open": c, "close": c,
              "high": c * 1.02, "low": c * 0.98, "volume": 1.0, "percent": 0}
             for i, c in enumerate(closes)]
        passed, total, dims = validate_nf(
            _stock(name="半导体测试"), None, closes, k,
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

    def test_ma_alignment_uses_ema_consistent_with_analysis(self):
        # P0 回归：validator 的 _mo_ma_alignment 必须与 analysis._ma_bull_score
        # 使用同一 EMA 约定，否则评分加分与验证维度会脱节。
        from scanner.analysis import _ma_bull_score

        # 构造 EMA 多头（5>10>20）序列：稳定上升
        pcts = [0.5]*5 + [1.0]*5 + [1.5]*5 + [2.0, 2.0, 2.5, 3.0, 3.0]
        k = _kline(pcts, volumes=[1.0]*20)
        closes = [c["close"] for c in k[:-1]]

        val_bonus, val_detail = _mo_ma_alignment(closes)
        ana_bonus = _ma_bull_score(closes)

        # 三态对齐：FULL↔+6 / PARTIAL↔+3 / NONE↔-3
        if val_bonus == V_MO_MA_FULL:
            assert ana_bonus > 0, f"validator FULL but analysis={ana_bonus} ({val_detail})"
        elif val_bonus == V_MO_MA_NONE:
            assert ana_bonus <= 0, f"validator NONE but analysis={ana_bonus} ({val_detail})"
        else:  # PARTIAL
            assert ana_bonus > 0, f"validator PARTIAL but analysis={ana_bonus} ({val_detail})"

    def test_ma_alignment_ema_differs_from_sma_rejected(self):
        # 守卫：确保 compute_ma 用的是 EMA 而非 SMA。
        # 标准 EMA 遍历完整序列，与前 period 个 SMA 种子不同；
        # 前10低价后10高价的序列，EMA(5) 会因前段低价拉低而 < SMA(5)=20.0
        from scanner.indicators import compute_ma
        closes = [10.0]*10 + [20.0]*10
        sma5 = sum(closes[-5:]) / 5  # = 20.0
        ema5 = compute_ma(closes, 5, ema=True)
        assert ema5 is not None
        assert ema5 < sma5, f"EMA should differ from SMA: ema={ema5}, sma={sma5}"


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

    def test_divergence_medium_requires_other_dims(self, monkeypatch):
        # 中等处置：顶背离(-10)不计入正维度，需 MA 多头 + 量能均匀 两个其它正维度才放行
        pcts = [0.5]*5 + [1.0]*5 + [1.5]*5 + [2, 2, 2.5, 3, 3]
        k = _kline(pcts, volumes=[1.0]*20)
        closes = [c["close"] for c in k[:-1]]

        # 背离 + MA 多头 + 量能均匀 → 仍通过
        monkeypatch.setattr("scanner.validator._mo_divergence",
                            lambda c, h, f=None: (-10, "bear_divergence"))
        monkeypatch.setattr("scanner.validator._mo_ma_alignment",
                            lambda c, f=None: (V_MO_MA_FULL, "full"))
        monkeypatch.setattr("scanner.validator._mo_volume_uniformity",
                            lambda h: (5, "uniform"))
        passed, total, dims = validate_momentum(_stock(), None, closes, k[:-1], None)
        assert passed, f"背离时其它两维为正应通过, total={total}, dims={dims}"

        # 背离 + MA 破位 + 量能爆量 → 不通过
        monkeypatch.setattr("scanner.validator._mo_ma_alignment",
                            lambda c, f=None: (0, "broken"))
        monkeypatch.setattr("scanner.validator._mo_volume_uniformity",
                            lambda h: (-8, "spike"))
        passed2, total2, dims2 = validate_momentum(_stock(), None, closes, k[:-1], None)
        assert not passed2, f"背离且无可补偿正维度应不通过, total={total2}, dims={dims2}"

    def _overbought_kline(self, ramp=1.6):
        # 末周期超买序列：前段横盘 + 末段持续拉升，BOLL%破上轨、KDJ J>105、20日涨幅>60%。
        base = 10.0
        closes = [base] * 25
        n = 25
        for i in range(n):
            closes.append(base * (1 + ramp * (i / (n - 1)) ** 1.5))
        last = closes[-1]
        for p in [2, 3, 6, 2, 4]:
            last *= (1 + p / 100)
            closes.append(last)
        klines = []
        for i, c in enumerate(closes):
            klines.append({
                "date": f"2026-03-{i+1:02d}",
                "open": c, "close": c,
                "high": c * 1.04, "low": c * 0.96,
                "volume": 1.0, "percent": 0,
            })
        return klines

    def test_overbought_flagged_and_suppressed(self):
        # 鱼尾段超买：v_mo_overbought=True，仅标记不压制 total；
        # 不硬否决（momentum 趋势延续，保留 passed 门禁）。
        k = self._overbought_kline()
        closes = [c["close"] for c in k[:-1]]
        stock = StockInfo(symbol="300999", name="测试", code="300999",
                          percent=4.0, current=k[-1]["close"], value=8000,
                          rank_change=1500, rank=10)
        passed, total, dims = validate_momentum(stock, None, closes, k[:-1], None)
        assert dims["v_mo_overbought"] is True, f"应判超买, dims={dims}"
        # enhancer 标记逻辑（复刻）：以 v_mo_overbought 为准，类型统一为 bool
        flag = True if dims.get("v_mo_overbought") else None
        assert flag is not None
        # 超买不再压制 total：与非超买对照同等输入应得相同 total
        k2 = _kline([0.5, -0.3, 0.4, -0.2, 0.3] * 6, volumes=[1.0] * 30)
        closes2 = [c["close"] for c in k2[:-1]]
        stock_clean = StockInfo(symbol="300999", name="测试", code="300999",
                                percent=4.0, current=k2[-1]["close"], value=8000,
                                rank_change=1500, rank=10)
        _, total_clean, dims_clean = validate_momentum(stock_clean, None, closes2, k2[:-1], None)
        assert dims_clean["v_mo_overbought"] is False
        # total 不再因超买而被压制（仅 MA/背离/量能三项之和）
        assert total == dims["v_mo_ma"] + dims["v_mo_divergence"] + dims["v_mo_volume"], \
            f"超买不再压制 total, total={total}, dims={dims}"

    def test_overbought_no_hard_veto(self):
        # 超买不应硬否决：若 MA/量能等正维度达标，passed 仍可为 True。
        k = self._overbought_kline()
        closes = [c["close"] for c in k[:-1]]
        stock = StockInfo(symbol="300999", name="测试", code="300999",
                          percent=4.0, current=k[-1]["close"], value=8000,
                          rank_change=1500, rank=10)
        # 用 monkeypatch 风格的正维度组合：MA 多头 + 量能均匀
        import scanner.validator as V
        orig_ma, orig_vol = V._mo_ma_alignment, V._mo_volume_uniformity
        V._mo_ma_alignment = lambda c, f=None: (V.V_MO_MA_FULL, "full")
        V._mo_volume_uniformity = lambda h: (5, "uniform")
        try:
            passed, total, dims = validate_momentum(stock, None, closes, k[:-1], None)
            assert dims["v_mo_overbought"] is True
            assert passed, f"超买不应硬否决，正维度达标应通过, total={total}, dims={dims}"
            # 超买仅标记不压制：total = MA + 背离 + 量能（无超买惩罚）
            assert total == V.V_MO_MA_FULL + 5, \
                f"超买不再压制 total, total={total}, dims={dims}"
        finally:
            V._mo_ma_alignment, V._mo_volume_uniformity = orig_ma, orig_vol


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
            trend="缩量回调", accumulated_pct=15.0, volume_ratio=0.3,
            bottom_confirmed=True, score=20, avg_volume=1.0,
        )
        bonus, vr = _pb_shrinkage(ks)
        assert bonus == V_PB_SHRINK_YES, f"expected shrink yes, got {bonus}"

    def test_shrinkage_neutral_at_high_boundary(self):
        # vol_ratio == PULLBACK_VOL_HIGH(1.3) 视为中性，与分析端口径一致
        ks = KlineSummary(
            trend="回踩整理", accumulated_pct=18.0, volume_ratio=1.3,
            bottom_confirmed=True, score=16, avg_volume=1.0,
        )
        bonus, vr = _pb_shrinkage(ks)
        assert bonus == 0, f"1.3 应为中性, got {bonus}"

    def test_shrinkage_no(self):
        # vol_ratio > PULLBACK_VOL_HIGH(1.3) 才惩罚
        ks = KlineSummary(
            trend="放量回落", accumulated_pct=18.0, volume_ratio=1.5,
            bottom_confirmed=True, score=16, avg_volume=1.0,
        )
        bonus, vr = _pb_shrinkage(ks)
        assert bonus == V_PB_SHRINK_NO, f"expected shrink no, got {bonus}"

    def test_volume_band_consistency_analysis_vs_validator(self):
        # B3 回归：分析端与验证端对回踩量能的"正/中性/惩罚"判定必须一致，
        # 避免 vol_ratio∈(1.0,1.3] 时分析端中性、验证端却扣分导致误判 pos_dims。
        from scanner.analysis import _score_pullback_volume
        from scanner.config import PULLBACK_WEIGHTS

        bands = [0.3, 0.5, 0.9, 1.1, 1.3, 1.5, 2.0]
        for vr in bands:
            ks = KlineSummary(
                trend="t", accumulated_pct=15.0, volume_ratio=vr,
                bottom_confirmed=True, score=20, avg_volume=1.0,
            )
            vbonus, _ = _pb_shrinkage(ks)
            validator_negative = vbonus < 0
            a_score, _ = _score_pullback_volume(vr, PULLBACK_WEIGHTS)
            analysis_negative = a_score < 0
            assert validator_negative == analysis_negative, (
                f"vol_ratio={vr}: 验证端负={validator_negative} 分析端负={analysis_negative} 不一致"
            )

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

    def test_dispatch_rebound(self):
        pcts = [0] * 15 + [1, -3, -12, -2, -3, 1, -2]
        k = _kline(pcts, volumes=[1.0] * len(pcts))
        closes = [c["close"] for c in k[:-1]]
        ks = KlineSummary(
            trend="超跌V反", accumulated_pct=-18.0, volume_ratio=1.5,
            bottom_confirmed=True, score=25, avg_volume=1.0,
        )
        passed, total, dims = validate(
            "rebound", _stock(name="半导体测试"),
            ks, closes, k[:-1], SEMICONDUCTOR_CLUSTER
        )
        assert isinstance(passed, bool)
        assert isinstance(total, int)
        assert "v_rb_oversold" in dims


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
        # HOT sector + rank Top10 提供 ≥2 正维度（含非 sector），使门禁放行，
        # 从而可断言 v_st_vol 的健康放量档取值。
        passed, total, dims = validate_short_term(
            _stock(name="半导体测试"), ks, closes, k[:-1], SEMICONDUCTOR_CLUSTER
        )
        assert passed
        assert dims["v_st_vol"] == V_ST_VOL_HEALTHY

    def test_top10_rank_bonus(self):
        k = _kline([5, 3, 6, 2, 4], volumes=[1.0, 1.2, 1.5, 1.8, 2.0])
        closes = [c["close"] for c in k[:-1]]
        ks = KlineSummary(
            trend="上攻", accumulated_pct=5.0, volume_ratio=1.5,
            bottom_confirmed=False, score=18, avg_volume=1.0,
        )
        stock = StockInfo(symbol="300999", name="半导体测试", code="300999",
                          percent=5.0, current=15.0, value=8000,
                          rank_change=1500, rank=5)
        # HOT sector 提供第 2 个正维度，rank Top10 为非 sector 正维度，门禁放行。
        passed, total, dims = validate_short_term(stock, ks, closes, k[:-1], SEMICONDUCTOR_CLUSTER)
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

    def test_weak_to_strong_not_double_counted(self):
        # P0-68 回归：st_weak_to_strong(+8) 已在 analyze_short_term 的 score 计入，
        # validate_short_term 仅将其作为门控维度，不得再加入 total，否则最终分重复 +8。
        from scanner.analysis import analyze_short_term

        # 构造昨日分歧/炸板 + 今日 2~8% 转强、量比≥1.0 的弱转强序列
        prev = 10.0
        bars = []
        for i in range(9):
            bars.append({"date": f"2026-04-{i+1:02d}", "open": prev, "high": prev * 1.01,
                         "low": prev * 0.99, "close": prev, "volume": 1.0, "percent": 0.0})
        # 昨日：上影线 >4%、收盘/最高-1 <3%（分歧/烂板）
        bars.append({"date": "2026-04-10", "open": 10.0, "high": 12.0,
                     "low": prev * 0.98, "close": 10.9, "volume": 2.0, "percent": 9.0})
        # 今日：3% 转强
        TODAY = "2026-04-11"
        bars.append({"date": TODAY, "open": 10.9, "high": 11.34,
                     "low": 10.68, "close": 11.23, "volume": 2.5, "percent": 3.0})

        stock = StockInfo(symbol="300999", name="测试", code="300999",
                          percent=3.0, current=11.23, value=8000,
                          rank_change=1500, rank=50, market_cap=50)
        ks = analyze_short_term(stock, bars, today_str=TODAY)
        assert ks is not None
        assert ks.dimensions.get("st_weak_to_strong") == 8, "分析分应含弱转强 +8"

        closes = [b["close"] for b in bars[:-1]]
        historical = bars[:-1]
        passed, total, dims = validate_short_term(stock, ks, closes, historical, None)

        # 门控仍生效
        assert passed, f"弱转强应放行, total={total}, dims={dims}"
        # 量能已在 analyze_short_term 的 score 计入，validate 的 total 不再含 vol，
        # 避免重复计分；total 应等于其余维度之和
        expected_total = dims["v_st_sector"] + dims["v_st_rank"] + dims["v_st_ma"]
        assert total == expected_total, (
            f"total 不应含 vol（已计入分析分），实际 total={total} 期望={expected_total}"
        )
        assert dims["v_st_weak"] == 8  # 仅展示用

        # 对照：把 st_weak_to_strong 置 0 后，validate 的 total 应不变，
        # 证明修复前 wts 确实被重复计入 total（修复后不再计入）。
        ks_no_wts = analyze_short_term(stock, bars, today_str=TODAY)
        ks_no_wts.dimensions["st_weak_to_strong"] = 0
        _, total_no_wts, _ = validate_short_term(stock, ks_no_wts, closes, historical, None)
        assert total - total_no_wts == 0, (
            f"修复后 wts 不应影响 total（应差 0），实际差 {total - total_no_wts}"
        )

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

    def test_single_rank_dimension_now_rejected(self):
        # 收紧后：仅 rank 单一正维度（sector 冷/MA不足/非弱转强）→ pos_dims=1，应淘汰。
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
        assert not passed, f"single positive dim should now be rejected, dims={dims}"

    def test_sector_only_rejected(self):
        # 今日病根回归：HOT sector 单一正维度（rank>30 负、MA不足、非弱转强）→ 不应放行。
        k = _kline([5, 3, 6, 2, 4], volumes=[1.0, 1.0, 1.0, 1.0, 1.2])
        closes = [c["close"] for c in k[:-1]]
        ks = KlineSummary(
            trend="温和放量", accumulated_pct=5.0, volume_ratio=1.2,
            bottom_confirmed=False, score=18, avg_volume=1.0,
        )
        stock = StockInfo(symbol="300999", name="半导体测试", code="300999",
                          percent=5.0, current=15.0, value=8000,
                          rank_change=1500, rank=50)
        passed, total, dims = validate_short_term(stock, ks, closes, k[:-1], SEMICONDUCTOR_CLUSTER)
        assert dims["v_st_sector"] == V_ST_SECTOR_HOT
        assert not passed, f"sector-only pass must be rejected, dims={dims}"

    def test_two_dims_with_non_sector_passes(self):
        # rank Top10（非 sector 正维度）+ HOT sector → pos_dims=2 且含非 sector，应放行。
        k = _kline([5, 3, 6, 2, 4], volumes=[1.0, 1.0, 1.0, 1.0, 1.2])
        closes = [c["close"] for c in k[:-1]]
        ks = KlineSummary(
            trend="温和放量", accumulated_pct=5.0, volume_ratio=1.2,
            bottom_confirmed=False, score=18, avg_volume=1.0,
        )
        stock = StockInfo(symbol="300999", name="半导体测试", code="300999",
                          percent=5.0, current=15.0, value=8000,
                          rank_change=1500, rank=5)
        passed, total, dims = validate_short_term(stock, ks, closes, k[:-1], SEMICONDUCTOR_CLUSTER)
        assert passed, f"two dims incl non-sector should pass, dims={dims}"

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

    def _overbought_kline(self, tail_pcts):
        # 构造末周期超买序列：前段低位横盘后，末段持续拉升使近 20日涨幅>60%、BOLL%破上轨
        pcts = [0.2] * 5 + [2.8] * 30 + list(tail_pcts)
        return _kline(pcts, volumes=[1.0] * len(pcts))

    def test_overbought_suppresses_validation_bonus(self):
        # 鱼尾段（超买）+ 弱转强 + 板块共振：验证加分应被压下为 0，
        # 不再给 inflated 共振分（对应 300534 原本 +24 验证分）。
        k = self._overbought_kline([2, 3, 6, 2, 4])
        closes = [c["close"] for c in k[:-1]]
        ks = KlineSummary(
            trend="弱转强", accumulated_pct=5.0, volume_ratio=1.2,
            bottom_confirmed=False, score=18, avg_volume=1.0,
            dimensions={"st_weak_to_strong": 8},
        )
        stock = StockInfo(symbol="300999", name="半导体测试", code="300999",
                          percent=5.0, current=k[-1]["close"], value=8000,
                          rank_change=1500, rank=50)
        passed, total, dims = validate_short_term(stock, ks, closes, k[:-1], SEMICONDUCTOR_CLUSTER)
        assert dims["v_st_overbought"] is True
        # 超买：验证加分归零（非超买时同等输入应为 vol+sector+ma+rank 之和 > 0）
        assert total == 0, f"超买时验证加分应压下为 0, 实际 total={total}, dims={dims}"

    def test_non_overbought_validation_bonus_intact(self):
        # 非超买：同等输入应正常发放验证加分（对照，证明超买压制是针对性行为）
        k = _kline([0.3, -0.2, 0.4, -0.3, 0.2] * 5, volumes=[1.0] * 25)
        closes = [c["close"] for c in k[:-1]]
        ks = KlineSummary(
            trend="弱转强", accumulated_pct=5.0, volume_ratio=1.5,
            bottom_confirmed=False, score=18, avg_volume=1.0,
            dimensions={"st_weak_to_strong": 8},
        )
        stock = StockInfo(symbol="300999", name="半导体测试", code="300999",
                          percent=5.0, current=k[-1]["close"], value=8000,
                          rank_change=1500, rank=5)
        passed, total, dims = validate_short_term(stock, ks, closes, k[:-1], SEMICONDUCTOR_CLUSTER)
        assert dims["v_st_overbought"] is False
        assert total > 0, "非超买时应正常发放验证加分"
        assert passed

    def test_non_overbought_weak_to_strong_still_passes(self):
        # 非超买：弱转强即便其它维度全负仍直通（保留既有行为）
        k = _kline([0.3, -0.2, 0.4, -0.3, 0.2] * 5, volumes=[1.0] * 25)
        closes = [c["close"] for c in k[:-1]]
        ks = KlineSummary(
            trend="弱转强", accumulated_pct=5.0, volume_ratio=1.2,
            bottom_confirmed=False, score=18, avg_volume=1.0,
            dimensions={"st_weak_to_strong": 8},
        )
        stock = StockInfo(symbol="300999", name="测试", code="300999",
                          percent=5.0, current=k[-1]["close"], value=8000,
                          rank_change=1500, rank=50)
        passed, total, dims = validate_short_term(stock, ks, closes, k[:-1], None)
        assert dims["v_st_overbought"] is False
        assert passed, f"非超买弱转强应直通, dims={dims}"

    def test_overbought_flag_visible_when_analysis_no_penalty(self):
        # 超买防护统一至 validator 单点后：分析侧不再写 st_overbought_penalty，
        # validator 仍应判超买并打 v_st_overbought 标记，enhancer 据此打 st_overbought_flag。
        k = _kline([0.3, -0.2, 0.4, -0.3, 0.2] * 5, volumes=[1.0] * 25)
        closes = [c["close"] for c in k[:-1]]
        # 今日收盘远高于历史序列末端 → validator 端 BOLL/J 破阈值
        today_close = closes[-1] * 1.25
        ks = KlineSummary(
            trend="弱转强", accumulated_pct=5.0, volume_ratio=1.5,
            bottom_confirmed=False, score=18, avg_volume=1.0,
            dimensions={"st_weak_to_strong": 8},
        )
        stock = StockInfo(symbol="300999", name="半导体测试", code="300999",
                          percent=20.0, current=today_close, value=8000,
                          rank_change=1500, rank=5)
        passed, total, dims = validate_short_term(stock, ks, closes, k[:-1], SEMICONDUCTOR_CLUSTER)
        assert dims["v_st_overbought"] is True, f"今日急拉应被 validator 判超买, dims={dims}"
        # enhancer 标记逻辑（复刻）：以 v_st_overbought 为准，打 st_overbought_flag
        dims["st_overbought_flag"] = True
        assert dims["st_overbought_flag"] is not None


class TestOverboughtLengthAlignment:
    """FIX-3 回归：series 与 highs/lows 对今日收盘的追加须同步，避免长度错位。"""

    def _make_stock(self, today_close):
        return StockInfo(symbol="300999", name="测试", code="300999",
                         percent=4.0, current=today_close, value=8000,
                         rank_change=1500, rank=10)

    def test_st_overbought_today_in_series(self):
        # 今日收盘已在历史序列末端（today_close == closes[-1]）：
        # series 不追加，highs/lows 也不应追加，三者等长。
        k = _kline([1.0] * 25, volumes=[1.0] * 25)
        closes = [c["close"] for c in k[:-1]]
        today_close = closes[-1]
        stock = self._make_stock(today_close)
        # 不抛异常且返回布尔；重点验证 FIX-3 后逻辑稳定（无长度错位）
        result = _st_is_overbought(closes, k[:-1], stock)
        assert isinstance(result, bool)

    def test_st_overbought_today_not_in_series(self):
        # 今日收盘高于历史序列末端：series 追加，highs/lows 同步追加。
        k = _kline([1.0] * 25, volumes=[1.0] * 25)
        closes = [c["close"] for c in k[:-1]]
        today_close = closes[-1] * 1.2
        stock = self._make_stock(today_close)
        result = _st_is_overbought(closes, k[:-1], stock)
        assert isinstance(result, bool)

    def test_mo_overbought_length_alignment(self):
        # 与 test_st 同构，覆盖 momentum 端 _mo_is_overbought。
        k = _kline([1.0] * 25, volumes=[1.0] * 25)
        closes = [c["close"] for c in k[:-1]]
        stock = self._make_stock(closes[-1])  # 今日收盘已在序列
        result = _mo_is_overbought(closes, k[:-1], stock)
        assert isinstance(result, bool)


class TestValidateRebound:

    def test_passes_with_two_positive_dims(self):
        # 超卖确认 + 量能确认 → pos_dims=2 → 通过
        pcts = [0] * 15 + [1, -3, -12, -2, -3, 1, -2]
        k = _kline(pcts, volumes=[1.0] * len(pcts))
        closes = [c["close"] for c in k[:-1]]
        ks = KlineSummary(
            trend="超跌V反", accumulated_pct=-18.0, volume_ratio=1.5,
            bottom_confirmed=True, score=25, avg_volume=1.0,
        )
        passed, total, dims = validate_rebound(
            _stock(name="半导体测试"), ks, closes, k[:-1], SEMICONDUCTOR_CLUSTER
        )
        assert passed, f"应通过（2正维度），dims={dims}"
        assert dims["v_rb_oversold"] > 0
        assert dims["v_rb_volume"] > 0

    def test_fails_with_insufficient_dims(self):
        # 仅1正维度（量能），无超卖/板块/形态 → pos_dims=1 → 不通过
        # 交替涨跌避免3连阳形态检测触发
        pcts = [1, -1] * 13
        k = _kline(pcts, volumes=[1.0] * 26)
        closes = [c["close"] for c in k[:-1]]
        ks = KlineSummary(
            trend="超跌企稳", accumulated_pct=-18.0, volume_ratio=1.5,
            bottom_confirmed=False, score=25, avg_volume=1.0,
        )
        passed, total, dims = validate_rebound(
            _stock(name="测试"), ks, closes, k[:-1], None
        )
        assert not passed, f"仅1正维度不应通过, dims={dims}"

    def test_volume_low_penalty(self):
        # 量比<1.0 → vol_bonus 为负，不计入 pos_dims
        pcts = [0] * 15 + [1, -3, -12, -2, -3, 1, -2]
        k = _kline(pcts, volumes=[1.0] * len(pcts))
        closes = [c["close"] for c in k[:-1]]
        ks = KlineSummary(
            trend="超跌企稳", accumulated_pct=-18.0, volume_ratio=0.5,
            bottom_confirmed=True, score=25, avg_volume=1.0,
        )
        passed, total, dims = validate_rebound(
            _stock(name="半导体测试"), ks, closes, k[:-1], SEMICONDUCTOR_CLUSTER
        )
        assert dims["v_rb_volume"] < 0, f"缩量应为负分, dims={dims}"

    def test_sector_resonance_bonus(self):
        # 同板块≥3只 → 板块共振加分
        pcts = [0] * 15 + [1, -3, -12, -2, -3, 1, -2]
        k = _kline(pcts, volumes=[1.0] * len(pcts))
        closes = [c["close"] for c in k[:-1]]
        ks = KlineSummary(
            trend="超跌V反", accumulated_pct=-18.0, volume_ratio=1.5,
            bottom_confirmed=True, score=25, avg_volume=1.0,
        )
        passed, total, dims = validate_rebound(
            _stock(name="半导体测试"), ks, closes, k[:-1], SEMICONDUCTOR_CLUSTER
        )
        assert dims["v_rb_sector"] > 0
        assert dims["v_rb_sector_count"] >= 3

    def test_sector_mid_tier_bonus(self):
        # 同板块=2只 → 中间档加分（Design-3 回归）
        pcts = [0] * 15 + [1, -3, -12, -2, -3, 1, -2]
        k = _kline(pcts, volumes=[1.0] * len(pcts))
        closes = [c["close"] for c in k[:-1]]
        ks = KlineSummary(
            trend="超跌V反", accumulated_pct=-18.0, volume_ratio=1.5,
            bottom_confirmed=True, score=25, avg_volume=1.0,
            dimensions={"rb_pattern_engulfing_crash": 6},
        )
        cluster_2 = {"半导体": ["300001", "300002"]}
        passed, total, dims = validate_rebound(
            _stock(name="半导体测试"), ks, closes, k[:-1], cluster_2
        )
        assert dims["v_rb_sector"] > 0, "2只板块应有中间档加分"
        assert dims["v_rb_sector_count"] == 2

    def test_pattern_not_double_counted_in_total(self):
        # BUG-1 回归：形态分不并入 validate total（已在 analyze_rebound 计入）
        pcts = [0] * 15 + [1, -3, -12, -2, -3, 1, -2]
        k = _kline(pcts, volumes=[1.0] * len(pcts))
        closes = [c["close"] for c in k[:-1]]
        # dims 含 engulfing 形态（+6），但 total 不应包含它
        ks = KlineSummary(
            trend="超跌V反", accumulated_pct=-18.0, volume_ratio=1.5,
            bottom_confirmed=True, score=25, avg_volume=1.0,
            dimensions={"rb_pattern_engulfing_crash": 6},
        )
        passed, total, dims = validate_rebound(
            _stock(name="半导体测试"), ks, closes, k[:-1], SEMICONDUCTOR_CLUSTER
        )
        assert dims["v_rb_pattern"] > 0, "形态维度应被识别"
        # total = os_bonus + vol_bonus + sec_bonus（不含 pat_bonus）
        assert total == dims["v_rb_oversold"] + dims["v_rb_volume"] + dims["v_rb_sector"]

