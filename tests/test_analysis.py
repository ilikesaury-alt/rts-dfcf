from scanner.analysis import analyze_momentum, analyze_new_face, analyze_pullback, analyze_short_term
from scanner.models import StockInfo

from tests.helpers import _kline


def _stock(percent=5.0, rank_change=1500, value=8000, current=15.0, rank=10, market_cap=0.0):
    return StockInfo(
        symbol="300999", name="测试", code="300999",
        percent=percent, current=current,
        value=value, rank_change=rank_change, rank=rank,
        market_cap=market_cap,
    )


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
        assert result is None

    def test_over_12_pct_rejected(self):
        kline = _kline([1, 1, 1, 2, 3], volumes=[1.0]*5)
        result = analyze_new_face(_stock(percent=13), kline)
        assert result is None

    def test_bottom_confirmed_gives_bonus(self):
        # 足够历史（>=20 根）以合法确认底部：横盘低位 + 末日放量 + 今日小涨
        pcts = [0.0] * 20 + [-0.5, 3.0]
        volumes = [1.0] * 21 + [3.0]
        kline = _kline(pcts, volumes=volumes)
        result = analyze_new_face(_stock(percent=4, rank_change=1000, value=5000), kline)
        assert result is not None, "低位横盘放量应给出候选"
        assert "new_face_bottom" in result.dimensions, "应确认底部启动"

    def test_ma_bull_bonus_with_bull_arrangement(self):
        pcts = [0.5]*5 + [0.8]*5 + [1.2]*5 + [1.5]*5
        kline = _kline(pcts)
        result = analyze_new_face(_stock(percent=3), kline)
        assert result is not None
        assert "new_face_ma_bull" in result.dimensions
        assert result.dimensions["new_face_ma_bull"] >= 3

    def test_today_pct_6_8_scores_low(self):
        # STRATEGY.md: 新面孔 6%~8% → +5（偏高但仍可接受）
        kline = _kline([1, 2, 1, 2, 3])
        result = analyze_new_face(_stock(percent=7, rank_change=2000, value=12000), kline)
        assert result is not None
        assert result.dimensions["new_face_today_pct"] == 5

    def test_today_pct_gt_8_penalized_not_rejected(self):
        # STRATEGY.md: 新面孔 >8% → -15（空间不足），但 >12% 才直接跳过
        kline = _kline([1, 2, 1, 2, 3])
        result = analyze_new_face(_stock(percent=10, rank_change=2000, value=12000), kline)
        assert result is not None
        assert result.dimensions["new_face_today_pct"] == -15

    def test_today_pct_lt_0_5_scores_five(self):
        # STRATEGY.md: 新面孔 <1% → +5（含 <0.5%）
        kline = _kline([1, 2, 1, 2, 3])
        result = analyze_new_face(_stock(percent=0.5, rank_change=2000, value=12000), kline)
        assert result is not None
        assert result.dimensions["new_face_today_pct"] == 5


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

    def test_over_15_pct_returns_none(self):
        kline = _kline([2, 3, 4, 5, 5])
        assert analyze_momentum(_stock(percent=16), kline) is None

    def test_short_kline_returns_none(self):
        assert analyze_momentum(_stock(percent=3), _kline([1, 2])) is None

    def test_none_kline_returns_none(self):
        assert analyze_momentum(_stock(percent=3), None) is None

    def test_volume_surge_neutral(self):
        # 放量突破不再惩罚（与"突破需放量"主流一致），权重改为 0
        kline = _kline([2, 3, 4, 5, 3], volumes=[1.0, 1.0, 0.8, 0.6, 3.5])
        result = analyze_momentum(_stock(percent=3), kline)
        assert result is not None
        assert result.dimensions.get("momentum_volume", 0) == 0

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

    def test_today_pct_6_8_scores_low(self):
        # STRATEGY.md: 动量 6%~8% → +5
        kline = _kline([2, 2, 2, 2, 2])
        result = analyze_momentum(_stock(percent=7, rank_change=2000, value=12000), kline)
        assert result is not None
        assert result.dimensions["momentum_today_pct"] == 5

    def test_today_pct_gt_8_skipped(self):
        # STRATEGY.md: 动量 >8% 直接跳过
        kline = _kline([2, 2, 2, 2, 2])
        assert analyze_momentum(_stock(percent=9), kline) is None

    def test_today_pct_lt_0_5_scores_five(self):
        # STRATEGY.md: 动量 <1% → +5（含 <0.5%）
        kline = _kline([2, 2, 2, 2, 2])
        result = analyze_momentum(_stock(percent=0.3, rank_change=2000, value=12000), kline)
        assert result is not None
        assert result.dimensions["momentum_today_pct"] == 5

    def test_accumulated_10_15_scores_fifteen(self):
        # STRATEGY.md: 动量 5日累计 10%~15% → +15
        kline = _kline([2, 2, 2, 2, 2])
        result = analyze_momentum(_stock(percent=4, rank_change=2000, value=12000), kline)
        assert result is not None
        assert result.dimensions["momentum_accumulated"] == 15

    def _overbought_kline(self, tail_pcts, ramp=2.4):
        # 构造末周期超买序列（鱼尾段）：长横盘 + 末段仅最后几根急拉冲刺，
        # 使 BOLL%破上轨（末根远离低波动的 20日中轨）、KDJ J>105、20日涨幅>60%。
        base = 10.0
        closes = [base] * 30  # 长横盘：低波动 → 20日带宽窄
        # 末段仅最后几根急拉（窄幅 +15%×3 冲刺使 KDJ J>105，整体破上轨使 BOLL%>1）
        burst = [15, 15, 15, 8]
        last = closes[-1]
        for p in burst:
            last *= (1 + p / 100)
            closes.append(last)
        for p in tail_pcts:
            last *= (1 + p / 100)
            closes.append(last)
        klines = []
        for i, c in enumerate(closes):
            # 全程窄幅（±2%）：KDJ J 对急拉敏感，窄幅即可爆表
            klines.append({
                "date": f"2026-03-{i+1:02d}",
                "open": c, "close": c,
                "high": c * 1.02, "low": c * 0.98,
                "volume": 1.0, "percent": 0,
            })
        return klines

    def test_momentum_overbought_boll_kdj_penalty(self):
        # 鱼尾段（破上轨 + J>105）：分析侧应施加软惩罚，标记 mo_overbought_*。
        k = self._overbought_kline([2])
        result = analyze_momentum(_stock(percent=4, rank_change=2000, value=12000, current=k[-1]["close"]), k)
        assert result is not None
        assert "mo_overbought_boll" in result.dimensions, f"应标记 BOLL 破上轨, dims={result.dimensions}"
        assert "mo_overbought_kdj" in result.dimensions, f"应标记 KDJ J 极端, dims={result.dimensions}"
        assert result.dimensions.get("mo_overbought_penalty", 0) < 0, "超买软惩罚应为负分"

    def test_momentum_overbought_20d_extreme_penalty(self):
        # 20日累计涨幅 > 60%（extreme）：应触发 mo_overbought_20d 软惩罚。
        k = self._overbought_kline([4, 5, 8, 3, 6], ramp=2.4)
        result = analyze_momentum(_stock(percent=4, rank_change=2000, value=12000, current=k[-1]["close"]), k)
        assert result is not None
        assert "mo_overbought_20d" in result.dimensions, f"应标记 20日极值, dims={result.dimensions}"
        assert result.dimensions["mo_overbought_20d"] > 60, "20日涨幅应 > 60%"

    def test_momentum_moderate_no_20d_extreme_penalty(self):
        # 对照：近期温和加速（accumulated>=10）但 20日涨幅 < 40% WARN，
        # 不触发 20日极值惩罚（BOLL% 破上轨在健康动量中属正常强势，软惩罚可接受）。
        k = _kline([0] * 15 + [4, 4, 4, 4, 4, -2], volumes=[1.0] * 21)
        result = analyze_momentum(_stock(percent=4, rank_change=2000, value=12000), k)
        assert result is not None
        assert "mo_overbought_20d" not in result.dimensions, \
            f"温和主升浪不应触发 20日极值惩罚, dims={result.dimensions}"


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
        # Rally → pullback below MA20 → slight recovery, 5-day accumulated still ≥ 5%
        pcts = [0.8]*15 + [-3.0]*4 + [1.0]*5
        kline = _kline(pcts)
        result = analyze_pullback(_stock(percent=-1), kline)
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

    def test_kdj_indicator_in_pullback(self):
        pcts = [-1]*5 + [2]*5 + [-2]*5 + [-3]*5  # downtrend after rise, J<0 likely
        kline = _kline(pcts, volumes=[1.0]*20)
        result = analyze_pullback(_stock(percent=-2), kline)
        if result:
            assert "pullback_kdj" in result.dimensions or "pullback_rsi" in result.dimensions
            assert result.score >= 10

    def test_bollinger_mid_support_in_pullback(self):
        pcts = [1.0]*15 + [4, 4, -1, -1, 0]
        kline = _kline(pcts, volumes=[1.0]*20)
        result = analyze_pullback(_stock(percent=-0.5), kline)
        assert result is not None
        assert result.score >= 0

    def test_20day_gain_under_warn_no_penalty(self):
        """20日涨幅 < 40% → 无惩罚"""
        pcts = [1.0] * 22
        kline = _kline(pcts, volumes=[1.0] * 22)
        result = analyze_pullback(_stock(percent=-1, rank_change=2500, value=12000), kline)
        assert result is not None
        assert "pullback_20d_gain" not in result.dimensions

    def test_20day_gain_warn_penalty(self):
        """20日涨幅 40-60% → -10 分"""
        pcts = [2.0] * 22
        kline = _kline(pcts, volumes=[1.0] * 22)
        result = analyze_pullback(_stock(percent=-1, rank_change=2500, value=12000), kline)
        assert result is not None
        assert result.dimensions.get("pullback_20d_gain") == -10

    def test_20day_gain_extreme_penalty(self):
        """20日涨幅 > 60% → -15 分"""
        pcts = [2.5] * 22
        kline = _kline(pcts, volumes=[1.0] * 22)
        result = analyze_pullback(_stock(percent=-1, rank_change=2500, value=12000), kline)
        assert result is not None
        assert result.dimensions.get("pullback_20d_gain") == -15


class TestIndicatorIntegration:

    def test_new_face_bollinger_oversold(self):
        pcts = [-2, -3, -4, -5, -3, -2, 3]
        kline = _kline(pcts, volumes=[1.0]*7)
        result = analyze_new_face(_stock(percent=3, rank_change=1500, value=8000), kline)
        if result:
            assert "new_face_bollinger" in result.dimensions or "new_face_rsi14" in result.dimensions

    def test_momentum_adx_bonus(self):
        pcts = [2]*35
        kline = _kline(pcts, volumes=[1.0]*35)
        result = analyze_momentum(_stock(percent=4, rank_change=2000, value=12000), kline)
        assert result is not None
        assert "momentum_adx" in result.dimensions
        assert result.dimensions["momentum_adx"] == 5

    def test_momentum_kdj_scoring(self):
        pcts = [1, 2, 3, 4, 5, 6, 5, 7, 6, 8]
        kline = _kline(pcts, volumes=[1.0]*10)
        result = analyze_momentum(_stock(percent=3, rank_change=1500, value=8000), kline)
        assert result is not None
        assert "momentum_kdj" in result.dimensions


class TestAccumulatedCalculation:

    def test_close_based_with_volatile_pattern(self):
        pcts = [5, -4, 5, -4, 5, -4, 5, -4, 5, -4]
        kline = _kline(pcts)
        cb = [b["close"] for b in kline]
        accumulated = (cb[-1] - cb[-6]) / cb[-6] * 100
        # sum(pcts[-5:]) = -4+5-4+5-4 = -2% (wrong), close-based = -2.46% (correct)
        assert round(accumulated, 2) == -2.46


class TestAnalyzeShortTerm:

    def test_golden_path_returns_scored_candidate(self):
        kline = _kline([5, 3, 6, 2, 4], volumes=[1.0, 1.2, 1.5, 1.8, 2.0])
        result = analyze_short_term(_stock(percent=5.0, rank=5), kline)
        assert result is not None
        assert result.score >= 15

    def test_pct_below_2_returns_none(self):
        kline = _kline([5, 3, 6, 2, 4])
        assert analyze_short_term(_stock(percent=1.0), kline) is None
        assert analyze_short_term(_stock(percent=0), kline) is None

    def test_pct_above_8_returns_none(self):
        kline = _kline([5, 3, 6, 2, 4])
        assert analyze_short_term(_stock(percent=9.0), kline) is None

    def test_short_kline_returns_none(self):
        kline = _kline([5, 3, 6])
        assert analyze_short_term(_stock(percent=5.0), kline) is None

    def test_none_kline_returns_none(self):
        assert analyze_short_term(_stock(percent=5.0), None) is None

    def test_vol_ratio_surge_gives_bonus(self):
        kline = _kline([5, 3, 6, 2, 4], volumes=[0.5, 0.6, 0.7, 0.8, 3.0])
        result = analyze_short_term(_stock(percent=5.0, rank=5), kline)
        assert result is not None
        assert "st_volume" in result.dimensions
        assert result.dimensions["st_volume"] == 12  # vol_surge

    def test_high_rank_penalty(self):
        kline = _kline([5, 3, 6, 2, 4], volumes=[1.0, 1.2, 1.5, 1.8, 2.0])
        result = analyze_short_term(_stock(percent=5.0, rank=50), kline)
        assert result is not None
        # rank>40: st_rank not recorded (no bonus/penalty)

    def _wts_kline(self, yesterday_high, yesterday_close, prev_close, yesterday_pct):
        # 6 根 K 线：末根为昨日、倒数第二根为前日收盘基准
        bars = [
            {"date": "2026-01-01", "open": 9.8, "high": 10.1, "low": 9.7, "close": 10.0, "volume": 1.0, "percent": 2.0},
            {"date": "2026-01-02", "open": 10.0, "high": 10.2, "low": 9.9, "close": 10.0, "volume": 1.0, "percent": 0.0},
            {"date": "2026-01-03", "open": 10.0, "high": 10.3, "low": 9.8, "close": 10.0, "volume": 1.0, "percent": 0.0},
            {"date": "2026-01-04", "open": 10.0, "high": 10.2, "low": 9.9, "close": 10.0, "volume": 1.0, "percent": 0.0},
            {"date": "2026-01-05", "open": 10.0, "high": 10.2, "low": 9.9, "close": prev_close, "volume": 1.0, "percent": 0.0},
            {"date": "2026-01-06", "open": 10.0, "high": yesterday_high, "low": 9.8,
             "close": yesterday_close, "volume": 2.0, "percent": yesterday_pct},
        ]
        return bars

    def test_weak_to_strong_bomb_detected(self):
        # 昨日曾触板(高/前收-1=20%)但收盘仅+5% → 炸板/烂板；今日高开转强
        kline = self._wts_kline(yesterday_high=12.0, yesterday_close=10.5, prev_close=10.0, yesterday_pct=5.0)
        result = analyze_short_term(_stock(percent=5.0, rank=5, current=11.5), kline)
        assert result is not None
        assert result.dimensions.get("st_weak_to_strong") == 8
        assert result.dimensions.get("st_wts_gap") == 4   # 今日高开(gap_pts>0)
        assert result.trend == "弱转强"

    def test_no_weak_to_strong_when_strong_close(self):
        # 昨日小上影且收盘强势 → 非分歧，不触发弱转强
        kline = self._wts_kline(yesterday_high=10.3, yesterday_close=10.25, prev_close=10.0, yesterday_pct=2.5)
        result = analyze_short_term(_stock(percent=5.0, rank=5, current=10.26), kline)
        assert result is not None
        assert "st_weak_to_strong" not in result.dimensions
        assert result.trend != "弱转强"

    def test_small_cap_preferred(self):
        kline = _kline([5, 3, 6, 2, 4])
        result = analyze_short_term(_stock(percent=5.0, rank=5, market_cap=80), kline)
        assert result is not None
        assert result.dimensions.get("st_value_small") == 6

    def test_mid_cap_small_bonus(self):
        kline = _kline([5, 3, 6, 2, 4])
        result = analyze_short_term(_stock(percent=5.0, rank=5, market_cap=150), kline)
        assert result is not None
        assert result.dimensions.get("st_value_mid") == 2

    def test_large_cap_no_value_bonus(self):
        kline = _kline([5, 3, 6, 2, 4])
        result = analyze_short_term(_stock(percent=5.0, rank=5, market_cap=400), kline)
        assert result is not None
        assert "st_value_small" not in result.dimensions
        assert "st_value_mid" not in result.dimensions

    def test_accumulated_negative_penalty(self):
        # 末段连续下跌使累计涨幅 < 0 → 应计 accum_lt_0 (-5)，而非漏记 0 分
        pcts = [5, 5, 5, -2, -2, -2, -2, -2, -2]
        kline = _kline(pcts)
        result = analyze_short_term(_stock(percent=3.0, rank=5), kline)
        assert result is not None
        assert result.dimensions["st_accumulated"] == -5

    def test_accumulated_positive_bucket(self):
        pcts = [1, 1, 1, 1, 1, 1, 1]
        kline = _kline(pcts)
        result = analyze_short_term(_stock(percent=3.0, rank=5), kline)
        assert result is not None
        assert result.dimensions["st_accumulated"] == 10  # accum_5_10

    def test_rsi_uses_period6_and_5070_window(self):
        from unittest.mock import patch

        kline = _kline([2, 3, 4, 3, 5, 2, 3])
        with patch("scanner.analysis.compute_rsi", return_value=60.0) as m:
            r = analyze_short_term(_stock(percent=3.0, rank=5), kline)
            assert m.call_args.kwargs.get("period") == 6
            assert "st_rsi" in r.dimensions
        with patch("scanner.analysis.compute_rsi", return_value=85.0):
            r2 = analyze_short_term(_stock(percent=3.0, rank=5), kline)
            assert "st_rsi" not in r2.dimensions  # >80 不应给加分

    def test_kdj_range_5080(self):
        from unittest.mock import patch

        kline = _kline([2, 3, 4, 3, 5, 2, 3])
        with patch("scanner.analysis.compute_kdj", return_value={"K": 60.0, "D": 50.0, "J": 55.0}):
            r = analyze_short_term(_stock(percent=3.0, rank=5), kline)
            assert "st_kdj" in r.dimensions
        with patch("scanner.analysis.compute_kdj", return_value={"K": 90.0, "D": 50.0, "J": 55.0}):
            r2 = analyze_short_term(_stock(percent=3.0, rank=5), kline)
            assert "st_kdj" not in r2.dimensions  # K>80 不加分
        with patch("scanner.analysis.compute_kdj", return_value={"K": 40.0, "D": 50.0, "J": 55.0}):
            r3 = analyze_short_term(_stock(percent=3.0, rank=5), kline)
            assert "st_kdj" not in r3.dimensions  # K<D 不加分

    def test_volume_ratio_low_consistency(self):
        from scanner.validator import validate_short_term

        # 量比 0.99：分析阶段判 vol_low，验证门禁同样判失败（同一量比值，口径一致）
        pcts = [3, 3, 3, 3, 3, 3, 3]
        volumes = [1.0] * 6 + [0.99]
        kline = _kline(pcts, volumes=volumes)
        result = analyze_short_term(_stock(percent=3.0, rank=5), kline)
        assert result is not None
        assert result.dimensions.get("st_volume") == -5
        closes = [k["close"] for k in kline]
        passed, _, _ = validate_short_term(_stock(percent=3.0, rank=5), result, closes, kline, None)
        assert passed is False

    def test_overbought_20d_gain_warn_penalty(self):
        # 末周期：连续温和上涨使 20日累计涨幅落入 40%~60% 预警区间 → 软惩罚
        pcts = [2.0] * 25
        kline = _kline(pcts, volumes=[1.0] * 25)
        result = analyze_short_term(_stock(percent=3.0, rank=5), kline)
        assert result is not None
        assert "st_overbought_20d" in result.dimensions
        # 40%~60% → PULLBACK_20D_GAIN_WARN_PENALTY = -10（与 pullback 对齐）
        assert result.dimensions["st_overbought_penalty"] < 0
        # 仍远高于入选门槛，不会被软惩罚单独淘汰
        assert result.score >= 15

    def test_overbought_20d_gain_extreme_penalty(self):
        # 20日累计 > 60% → 更重惩罚（-15）
        pcts = [2.8] * 25
        kline = _kline(pcts, volumes=[1.0] * 25)
        result = analyze_short_term(_stock(percent=3.0, rank=5), kline)
        assert result is not None
        assert "st_overbought_20d" in result.dimensions

    def test_overbought_boll_kdj_penalty(self):
        # 抛物线式拉升：破 BOLL 上轨(%B>1) + 20日巨幅获利盘 → 应触发末周期超买扣分
        pcts = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 5.0, 7.0, 8.0, 9.0] * 3
        kline = _kline(pcts, volumes=[1.0] * 30)
        result = analyze_short_term(_stock(percent=6.0, rank=5, current=kline[-1]["close"]), kline)
        assert result is not None
        assert result.dimensions.get("st_overbought_boll") is not None
        assert result.dimensions["st_overbought_penalty"] < 0

    def test_low_position_no_overbought_penalty(self):
        # 低位横盘小幅波动：不触发任何末周期超买惩罚
        pcts = [0.3, -0.2, 0.4, -0.3, 0.2] * 5
        kline = _kline(pcts, volumes=[1.0] * 25)
        result = analyze_short_term(_stock(percent=3.0, rank=5), kline)
        assert result is not None
        assert "st_overbought_penalty" not in result.dimensions


