from datetime import datetime

from scanner.analysis import (
    _compute_volume_metrics,
    _score_today_pct,
    analyze_momentum,
    analyze_new_face,
    analyze_rebound,
    analyze_short_term,
)
from scanner.config import MOMENTUM_WEIGHTS, NEW_FACE_WEIGHTS, REBOUND_MIN_SCORE, REBOUND_WEIGHTS
from scanner.models import StockInfo
from tests.helpers import _kline


def _stock(percent=5.0, rank_change=1500, value=8000, current=15.0, rank=10, market_cap=0.0):
    return StockInfo(
        symbol="300999", name="测试", code="300999",
        percent=percent, current=current,
        value=value, rank_change=rank_change, rank=rank,
        market_cap=market_cap,
    )


def _kline_with_today(hist_pcts, today_pct, today_str="2026-02-01"):
    """历史 N 根（日期 2026-01-XX）+ 一根日期为 today_str 的今日 bar。

    真实扫描时 kline 含今日 bar，_split_today 按其日期剔除——本 fixture 复刻该形态，
    使历史口径（accumulated_pct，不含今日）与含今日口径（accumulated_incl_today）可分。
    """
    base = 100.0
    closes = [base]
    for p in hist_pcts:
        closes.append(closes[-1] * (1 + p / 100))
    bars = []
    for i, c in enumerate(closes[1:], 1):
        bars.append({"date": f"2026-01-{i:02d}", "open": closes[i - 1], "close": c,
                     "high": c * 1.02, "low": c * 0.98, "volume": 1.0,
                     "percent": hist_pcts[i - 1]})
    today_close = closes[-1] * (1 + today_pct / 100)
    bars.append({"date": today_str, "open": closes[-1], "close": today_close,
                 "high": today_close * 1.02, "low": closes[-1] * 0.98,
                 "volume": 2.0, "percent": today_pct})
    return bars


class TestAnalyzeNewFace:

    def test_golden_path_returns_scored_candidate(self):
        kline = _kline([2, 1, -1, 2, 4], volumes=[0.8, 0.9, 0.7, 1.5, 2.0])
        result = analyze_new_face(_stock(percent=4.5, rank_change=2500, value=12000), kline)
        assert result is not None
        assert result.score >= 20
        # Step 2 重平衡：today_pct_2_6 20→8（今日大涨对超卖反转是动量确认而非反转信号）
        assert result.dimensions["new_face_today_pct"] == 8
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
        # STRATEGY.md: 新面孔 >8% → -10（P2调整：原-15过重，8%是创业板正常强势区间）
        kline = _kline([1, 2, 1, 2, 3])
        result = analyze_new_face(_stock(percent=10, rank_change=2000, value=12000), kline)
        assert result is not None
        assert result.dimensions["new_face_today_pct"] == -10

    def test_today_pct_lt_0_5_scores_three(self):
        # Step 2 重平衡：today_pct_lt_0_5 5→3（超卖反转弱化今日涨幅奖励）。
        # _band_score 用严格 < 比较：percent=0.3 < 0.5 落 lt_0_5 档；percent=0.5 落 0_5_1 档。
        kline = _kline([1, 2, 1, 2, 3])
        result = analyze_new_face(_stock(percent=0.3, rank_change=2000, value=12000), kline)
        assert result is not None
        assert result.dimensions["new_face_today_pct"] == 3


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

    def test_today_pct_8_10_accepted(self):
        # P1-2: 动量 8~10% 现在接受（原 >8% 跳过已放宽到 >10%）
        kline = _kline([2, 2, 2, 2, 2])
        result = analyze_momentum(_stock(percent=9, rank_change=2000, value=12000), kline)
        assert result is not None
        assert result.dimensions["momentum_today_pct"] == 3

    def test_today_pct_gt_10_skipped(self):
        # P1-2: 动量 >10% 直接跳过（上限从 8 放宽到 10）
        kline = _kline([2, 2, 2, 2, 2])
        assert analyze_momentum(_stock(percent=11), kline) is None

    def test_today_pct_lt_0_5_scores_five(self):
        # STRATEGY.md: 动量 <1% → +5（含 <0.5%）
        kline = _kline([2, 2, 2, 2, 2])
        result = analyze_momentum(_stock(percent=0.3, rank_change=2000, value=12000), kline)
        assert result is not None
        assert result.dimensions["momentum_today_pct"] == 5

    def test_accumulated_10_15_scores_eight(self):
        # P0 IC 重平衡：accum_10_15 15→8（momentum_accumulated IC -0.08 反指，已涨多的票 3 日内均值回归）
        kline = _kline([2, 2, 2, 2, 2])
        result = analyze_momentum(_stock(percent=4, rank_change=2000, value=12000), kline)
        assert result is not None
        assert result.dimensions["momentum_accumulated"] == 8

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

    def test_momentum_overbought_no_analysis_side_penalty(self):
        # 超买防护已统一至 validator 单点：分析侧不再做软惩罚，不写入 mo_overbought_* 维度。
        # validator._mo_is_overbought 负责判断 + enhancer 负责标记。
        k = self._overbought_kline([2])
        result = analyze_momentum(_stock(percent=4, rank_change=2000, value=12000, current=k[-1]["close"]), k)
        assert result is not None
        assert "mo_overbought_penalty" not in result.dimensions, \
            f"分析侧不应再写入超买惩罚, dims={result.dimensions}"
        assert "mo_overbought_boll" not in result.dimensions, \
            f"分析侧不应再标记 BOLL 破上轨, dims={result.dimensions}"
        assert "mo_overbought_kdj" not in result.dimensions, \
            f"分析侧不应再标记 KDJ J 极端, dims={result.dimensions}"

    def test_momentum_overbought_20d_no_analysis_side_penalty(self):
        # 20日累计涨幅 > 60%（extreme）：分析侧不再做软惩罚，validator 单点判断。
        k = self._overbought_kline([4, 5, 8, 3, 6], ramp=2.4)
        result = analyze_momentum(_stock(percent=4, rank_change=2000, value=12000, current=k[-1]["close"]), k)
        assert result is not None
        assert "mo_overbought_20d" not in result.dimensions, \
            f"分析侧不应再标记 20日极值, dims={result.dimensions}"
        assert "mo_overbought_penalty" not in result.dimensions, \
            f"分析侧不应再写入超买惩罚, dims={result.dimensions}"

    def test_first_launch_hit_low_accumulated(self):
        # 首次启动：累计涨幅还低(~2%) + 今日4.5% + 放量(1.6) + MA转多头 → 命中 momentum
        pcts = [0.3] * 12 + [0.4, 0.5, 0.5]
        kline = _kline(pcts, volumes=[1.0] * 14 + [1.6])
        result = analyze_momentum(_stock(percent=4.5, rank_change=2000, value=12000), kline)
        assert result is not None
        assert result.dimensions.get("momentum_first_launch") == 1
        assert result.trend == "启动首日"
        assert result.accumulated_pct < 7.0

    def test_first_launch_reject_shrink_volume(self):
        # 首次启动硬门：缩量(vol<1.5) → 拒收（假阳）
        pcts = [0.3] * 12 + [0.4, 0.5, 0.5]
        kline = _kline(pcts, volumes=[1.0] * 15)
        assert analyze_momentum(_stock(percent=4.5, rank_change=2000, value=12000), kline) is None

    def test_first_launch_reject_low_today_pct(self):
        # 首次启动但今日涨幅跌破 3.5% 下限 → 拒收（2-4% 假启动噪音区）
        pcts = [0.3] * 12 + [0.4, 0.5, 0.5]
        kline = _kline(pcts, volumes=[1.0] * 14 + [1.6])
        assert analyze_momentum(_stock(percent=2.5, rank_change=2000, value=12000), kline) is None

    def test_momentum_moderate_no_overbought_dims(self):
        # 对照：温和主升浪，分析侧不写入任何超买维度（与超买票一致，统一由 validator 判断）。
        k = _kline([0] * 15 + [4, 4, 4, 4, 4, -2], volumes=[1.0] * 21)
        result = analyze_momentum(_stock(percent=4, rank_change=2000, value=12000), k)
        assert result is not None
        assert "mo_overbought_20d" not in result.dimensions, \
            f"分析侧不应标记 20日极值, dims={result.dimensions}"


class TestIndicatorIntegration:

    def test_new_face_bollinger_oversold(self):
        pcts = [-2, -3, -4, -5, -3, -2, 3]
        kline = _kline(pcts, volumes=[1.0]*7)
        result = analyze_new_face(_stock(percent=3, rank_change=1500, value=8000), kline)
        if result:
            assert "new_face_bollinger" in result.dimensions or "new_face_rsi14" in result.dimensions

    def test_new_face_atr_obv_dims_present(self):
        # 低波动蓄势序列（>=15 根供 ATR 计算）→ ATR 维度存在 + OBV 非负确认
        pcts = [-1, -1, 0, 0, 1, 1, 1, 2, -1, 0, 1, 0, -1, 1, 1] + [3.0]
        kline = _kline(pcts, volumes=[1.0] * 16)
        result = analyze_new_face(_stock(percent=3, rank_change=1500, value=8000), kline)
        assert result is not None
        assert "new_face_atr_pct" in result.dimensions
        assert "new_face_obv_trend" in result.dimensions
        # 低波动下行后企稳 → OBV 不应转负（资金未撤离）
        assert result.dimensions["new_face_obv_trend"] >= 0
        assert result.dimensions["new_face_atr_pct"] > 0

    def test_momentum_adx_bonus(self):
        pcts = [2]*35
        kline = _kline(pcts, volumes=[1.0]*35)
        result = analyze_momentum(_stock(percent=4, rank_change=2000, value=12000), kline)
        assert result is not None
        assert "momentum_adx" in result.dimensions
        # 维度现在存储实际 ADX 值（非权重常量）
        assert isinstance(result.dimensions["momentum_adx"], float)
        assert result.dimensions["momentum_adx"] > 0

    def test_momentum_kdj_scoring(self):
        pcts = [1, 2, 3, 4, 5, 6, 5, 7, 6, 8]
        kline = _kline(pcts, volumes=[1.0]*10)
        result = analyze_momentum(_stock(percent=3, rank_change=1500, value=8000), kline)
        assert result is not None
        assert "momentum_kdj" in result.dimensions

    def test_momentum_atr_obv_dims_present(self):
        # 16 根温和上行 K 线（6日累计>10% 通过动量门槛，且 >=15 根供 ATR 计算）
        # → ATR% 落入健康区间、OBV 上行趋势
        pcts = [2.0] * 15 + [3.0]
        kline = _kline(pcts, volumes=[1.0] * 16)
        result = analyze_momentum(_stock(percent=3, rank_change=2000, value=12000), kline)
        assert result is not None
        assert "momentum_atr_pct" in result.dimensions
        assert "momentum_obv_trend" in result.dimensions
        assert result.dimensions["momentum_obv_trend"] == 1
        # 波动率适中（ATR% 在 2~6 健康区间）拿到 atr_healthy 加分
        assert 2 <= result.dimensions["momentum_atr_pct"] <= 6


class TestAccumulatedCalculation:

    def test_close_based_with_volatile_pattern(self):
        pcts = [5, -4, 5, -4, 5, -4, 5, -4, 5, -4]
        kline = _kline(pcts)
        cb = [b["close"] for b in kline]
        accumulated = (cb[-1] - cb[-6]) / cb[-6] * 100
        # sum(pcts[-5:]) = -4+5-4+5-4 = -2% (wrong), close-based = -2.46% (correct)
        assert round(accumulated, 2) == -2.46

    def test_incl_today_dim_momentum_includes_today_bar(self):
        # 2026-08-17 修复回归：momentum 的 accumulated_pct 为历史口径（不含今日，
        # RPS/评分用），accumulated_incl_today 维度须含今日 bar——🎯 门槛即用后者。
        # 6 根历史各 +1.7%（5 日复利 ≈ +8.79%）+ 今日 +6%；含今日窗口前移一根 → ≈ +13.4%。
        kline = _kline_with_today([1.7] * 6, 6.0)
        result = analyze_momentum(_stock(percent=6.0, rank_change=2000, value=12000),
                                  kline, today_str="2026-02-01")
        assert result is not None
        assert 8.0 < result.accumulated_pct < 9.5, f"历史口径应≈8.79%: {result.accumulated_pct}"
        assert result.dimensions["accumulated_incl_today"] > 12.0, \
            f"含今日维度应≈13.4%: {result.dimensions['accumulated_incl_today']}"

    def test_incl_today_dim_new_face_includes_today_bar(self):
        kline = _kline_with_today([1.7] * 6, 6.0)
        result = analyze_new_face(_stock(percent=6.0, rank_change=2500, value=12000),
                                  kline, today_str="2026-02-01")
        assert result is not None
        assert result.dimensions["accumulated_incl_today"] > result.accumulated_pct + 3.0

    def test_incl_today_dim_short_term_equals_accumulated(self):
        # short_term 的 accumulated_pct 本身含今日（策略语义），维度值应与之一致。
        kline = _kline_with_today([1.7] * 6, 6.0)
        result = analyze_short_term(_stock(percent=6.0), kline, today_str="2026-02-01")
        assert result is not None
        assert result.dimensions["accumulated_incl_today"] == result.accumulated_pct

    def test_incl_today_dim_rebound_negative_with_today_gain(self):
        # rebound 超跌场景：历史连跌（含今日前已 -8% 复利），今日企稳 +3%，
        # accumulated_pct（不含今日）仍为负，含今日维度应抬高但方向不变。
        hist = [-2.0] * 6
        kline = _kline_with_today(hist, 3.0)
        result = analyze_rebound(_stock(percent=3.0), kline, today_str="2026-02-01")
        assert result is not None, "超跌企稳场景应命中 rebound"
        assert result.accumulated_pct < -5.0
        assert result.dimensions["accumulated_incl_today"] > result.accumulated_pct


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

    def test_pct_above_12_returns_none(self):
        # P1-1: 上限从 8 放宽到 12，9% 现在接受，>12% 才拒绝
        kline = _kline([5, 3, 6, 2, 4])
        assert analyze_short_term(_stock(percent=13.0), kline) is None

    def test_pct_8_12_accepted(self):
        # P1-1: 8~12% 档位验证（P1-8 权重 8→15，数据支持：8-12% 档分桶 cum_3d +3.84% 最好）
        kline = _kline([5, 3, 6, 2, 4])
        result = analyze_short_term(_stock(percent=9.0, rank_change=2000, value=12000), kline)
        assert result is not None
        assert result.dimensions["st_today_pct"] == 15

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
        # st_wts_gap 已删除：弱转强 +8 已充分奖励，高开不再额外加分
        assert "st_wts_gap" not in result.dimensions
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
        # 末段连续暴跌使累计涨幅 < 0（含今日 bar） → 应计 accum_lt_0 (-5)
        # 原测试未含今日 bar 导致 accumulated 偏高，修正后用更深跌幅确保含今日仍为负
        pcts = [5, 5, 5, -3, -3, -3, -3, -3, -3]
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
        with patch("scanner.features.compute_rsi", return_value=60.0) as m:
            r = analyze_short_term(_stock(percent=3.0, rank=5), kline)
            # build_features 会同时算 RSI(6)/RSI(14)，确认其中存在 period=6 的调用
            assert any(len(c.args) > 1 and c.args[1] == 6 for c in m.call_args_list)
            assert "st_rsi" in r.dimensions
        with patch("scanner.features.compute_rsi", return_value=85.0):
            r2 = analyze_short_term(_stock(percent=3.0, rank=5), kline)
            # RSI>80 时仍记录维度值（惩罚分项可见）
            assert "st_rsi" in r2.dimensions
            assert r2.dimensions["st_rsi"] == 85.0

    def test_kdj_range_5080(self):
        from unittest.mock import patch

        kline = _kline([2, 3, 4, 3, 5, 2, 3])
        with patch("scanner.features.compute_kdj", return_value={"K": 60.0, "D": 50.0, "J": 55.0}):
            r = analyze_short_term(_stock(percent=3.0, rank=5), kline)
            assert "st_kdj" in r.dimensions
        with patch("scanner.features.compute_kdj", return_value={"K": 90.0, "D": 50.0, "J": 55.0}):
            r2 = analyze_short_term(_stock(percent=3.0, rank=5), kline)
            assert "st_kdj" not in r2.dimensions  # K>80 不加分
        with patch("scanner.features.compute_kdj", return_value={"K": 40.0, "D": 50.0, "J": 55.0}):
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

    def test_overbought_20d_gain_no_analysis_side_penalty(self):
        # 超买防护已统一至 validator 单点：分析侧不再做软惩罚，不写入 st_overbought_* 维度。
        # 连续温和上涨使 20日累计涨幅落入 40%~60% 预警区间，validator 侧判断超买。
        pcts = [2.0] * 25
        kline = _kline(pcts, volumes=[1.0] * 25)
        result = analyze_short_term(_stock(percent=3.0, rank=5), kline)
        assert result is not None
        assert "st_overbought_20d" not in result.dimensions, \
            f"分析侧不应再标记 20日极值, dims={result.dimensions}"
        assert "st_overbought_penalty" not in result.dimensions, \
            f"分析侧不应再写入超买惩罚, dims={result.dimensions}"

    def test_overbought_20d_extreme_no_analysis_side_penalty(self):
        # 20日累计 > 60%：分析侧不再做软惩罚，validator 单点判断。
        pcts = [2.8] * 25
        kline = _kline(pcts, volumes=[1.0] * 25)
        result = analyze_short_term(_stock(percent=3.0, rank=5), kline)
        assert result is not None
        assert "st_overbought_20d" not in result.dimensions, \
            f"分析侧不应再标记 20日极值, dims={result.dimensions}"
        assert "st_overbought_penalty" not in result.dimensions, \
            f"分析侧不应再写入超买惩罚, dims={result.dimensions}"

    def test_overbought_boll_kdj_no_analysis_side_penalty(self):
        # 抛物线式拉升：破 BOLL 上轨(%B>1) + 20日巨幅获利盘 → 分析侧不再做超买扣分
        pcts = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 5.0, 7.0, 8.0, 9.0] * 3
        kline = _kline(pcts, volumes=[1.0] * 30)
        result = analyze_short_term(_stock(percent=6.0, rank=5, current=kline[-1]["close"]), kline)
        assert result is not None
        assert "st_overbought_boll" not in result.dimensions, \
            f"分析侧不应再标记 BOLL 破上轨, dims={result.dimensions}"
        assert "st_overbought_penalty" not in result.dimensions, \
            f"分析侧不应再写入超买惩罚, dims={result.dimensions}"

    def test_low_position_no_overbought_dims(self):
        # 低位横盘小幅波动：分析侧不写入任何超买维度（统一由 validator 判断）
        pcts = [0.3, -0.2, 0.4, -0.3, 0.2] * 5
        kline = _kline(pcts, volumes=[1.0] * 25)
        result = analyze_short_term(_stock(percent=3.0, rank=5), kline)
        assert result is not None
        assert "st_overbought_penalty" not in result.dimensions


class TestScoreTodayPctDeadBranches:
    """FIX-1 回归：_score_today_pct 不得引用未定义权重键 today_pct_6_7 / today_pct_7_12。

    调用方已显式处理 today_pct >= 6，故本函数定义域收敛到 < 6。直接调用各边界值，
    断言不抛 KeyError 且返回的评分值确实来自对应权重字典（即查不到未定义键）。
    """

    def test_new_face_full_range_no_keyerror(self):
        for pct in (0.0, 0.49, 0.5, 0.99, 1.0, 1.99, 2.0, 3.0, 5.99):
            score, key, val = _score_today_pct(pct, NEW_FACE_WEIGHTS, "new_face")
            assert val in NEW_FACE_WEIGHTS.values(), f"返回分 {val} 不在 NEW_FACE_WEIGHTS (pct={pct})"
            assert val == score

    def test_momentum_full_range_no_keyerror(self):
        for pct in (0.0, 0.49, 0.5, 0.99, 1.0, 1.99, 2.0, 3.0, 5.99):
            score, key, val = _score_today_pct(pct, MOMENTUM_WEIGHTS, "momentum")
            assert val in MOMENTUM_WEIGHTS.values(), f"返回分 {val} 不在 MOMENTUM_WEIGHTS (pct={pct})"
            assert val == score

    def test_boundary_covers_lt_6_only(self):
        # 紧邻调用方拦截点（>= 6）的上界，确保 2~6 区间被 today_pct_2_6 覆盖
        score, key, val = _score_today_pct(5.999, NEW_FACE_WEIGHTS, "new_face")
        assert key == "new_face_today_pct"
        assert val == NEW_FACE_WEIGHTS["today_pct_2_6"]


class TestAnalyzeRebound:

    def test_golden_path_returns_scored_candidate(self):
        # 前5日累计跌-18%，含-12%暴跌日；今日企稳+3%
        kline = _kline([1, -3, -12, -2, -3, 1, -2], volumes=[1.0] * 7)
        result = analyze_rebound(_stock(percent=3.0), kline)
        assert result is not None
        assert result.score >= REBOUND_MIN_SCORE
        assert "rebound_drop_depth" in result.dimensions
        assert "rebound_crash_day" in result.dimensions

    def test_pct_below_threshold_returns_none(self):
        kline = _kline([1, -3, -12, -2, -3, 1, -2], volumes=[1.0] * 7)
        assert analyze_rebound(_stock(percent=0.3), kline) is None

    def test_pct_above_threshold_returns_none(self):
        kline = _kline([1, -3, -12, -2, -3, 1, -2], volumes=[1.0] * 7)
        assert analyze_rebound(_stock(percent=9.0), kline) is None

    def test_short_kline_returns_none(self):
        kline = _kline([-3, -12, -2])
        assert analyze_rebound(_stock(percent=3.0), kline) is None

    def test_none_kline_returns_none(self):
        assert analyze_rebound(_stock(percent=3.0), None) is None

    def test_insufficient_drop_returns_none(self):
        # 前5日累计仅-8%，未达-10%超跌门槛（P0-1: 阈值从-15放宽到-10）
        kline = _kline([-1, -2, -3, -1, -1, 1, -2], volumes=[1.0] * 7)
        assert analyze_rebound(_stock(percent=3.0), kline) is None

    def test_no_crash_day_yields_yin_die_stabilize(self):
        # P0-1: 前5日累计-16%但无单日暴跌(≤-10%)，属"阴跌企稳"场景，现在接受
        kline = _kline([-4, -3, -3, -3, -3, 1, -2], volumes=[1.0] * 7)
        result = analyze_rebound(_stock(percent=3.0), kline)
        assert result is not None, "阴跌企稳场景（无暴跌日但累计跌>10%）应进入 rebound"
        assert result.trend == "阴跌企稳"
        assert "rebound_crash_day" not in result.dimensions, "无暴跌日不应加 crash_day_bonus"

    def test_deep_drop_scores_higher(self):
        # 前5日累计-25%（含-12%暴跌日），应比-18%得分更高
        kline = _kline([1, -5, -12, -4, -5, 1, -2], volumes=[1.0] * 7)
        deep = analyze_rebound(_stock(percent=3.0), kline)
        shallow_kline = _kline([1, -3, -12, -2, -3, 1, -2], volumes=[1.0] * 7)
        shallow = analyze_rebound(_stock(percent=3.0), shallow_kline)
        assert deep is not None and shallow is not None
        assert deep.dimensions["rebound_drop_depth"] > shallow.dimensions["rebound_drop_depth"]

    def test_volume_surge_gives_bonus(self):
        # 放量企稳（量比≥2.0）应比缩量得分更高
        kline_surge = _kline([1, -3, -12, -2, -3, 1, -2], volumes=[1.0] * 6 + [3.0])
        kline_normal = _kline([1, -3, -12, -2, -3, 1, -2], volumes=[1.0] * 7)
        surge = analyze_rebound(_stock(percent=3.0), kline_surge)
        normal = analyze_rebound(_stock(percent=3.0), kline_normal)
        assert surge is not None and normal is not None
        assert surge.dimensions["rebound_volume"] == REBOUND_WEIGHTS["vol_surge"]
        assert normal.dimensions["rebound_volume"] == REBOUND_WEIGHTS["vol_healthy"]

    def test_v_shape_reversal_detected(self):
        # V型反转：5日跌<-15% + 放量>1.5x + 今日>2%
        kline = _kline([1, -3, -12, -2, -3, 1, -2], volumes=[1.0] * 6 + [2.5])
        result = analyze_rebound(_stock(percent=3.0), kline)
        assert result is not None
        assert "rebound_v_shape" in result.dimensions
        assert result.trend == "超跌V反"

    def test_low_position_trend_label(self):
        # 低位企稳标签（无V型，近20日低点）
        pcts = [0] * 18 + [-3, -12, -2, -3, 1, -2]
        volumes = [1.0] * len(pcts)
        kline = _kline(pcts, volumes=volumes)
        result = analyze_rebound(_stock(percent=1.0), kline)
        assert result is not None
        assert result.trend in ("低位企稳", "放量反弹", "超跌企稳", "超跌V反")

    def test_rsi_oversold_bonus(self):
        # 大幅下跌应产生 RSI<30 超卖加分
        kline = _kline([1, -3, -12, -2, -3, 1, -2], volumes=[1.0] * 7)
        result = analyze_rebound(_stock(percent=3.0), kline)
        assert result is not None
        assert "rebound_rsi" in result.dimensions
        assert result.dimensions["rebound_rsi"] < 30


class TestComputeVolumeMetrics:
    """早盘量比投影：盘中部分量能按已交易分钟数放大为全天量能。"""

    def _kline_with_today(self, today_vol=0.5):
        # 末根日期 2026-01-06 视为"今日"，前5根为历史全天量
        return _kline([2, 1, -1, 2, 4, 3], volumes=[1.0, 1.0, 1.0, 1.0, 1.0, today_vol])

    def test_no_projection_when_elapsed_zero(self):
        kline = self._kline_with_today(today_vol=0.5)
        ratio, avg = _compute_volume_metrics(kline, "2026-01-06",
                                             now=datetime(2026, 6, 18, 9, 29))
        assert ratio == 0.5
        assert avg == 1.0

    def test_projects_morning_volume(self):
        # 09:55 → elapsed=25，投影倍数 = min(240/25, 10) = 9.6
        kline = self._kline_with_today(today_vol=0.5)
        ratio, avg = _compute_volume_metrics(kline, "2026-01-06",
                                             now=datetime(2026, 6, 18, 9, 55))
        assert ratio == round(0.5 * 9.6, 2)
        assert avg == 1.0

    def test_projection_capped_at_10x(self):
        # 09:31 → elapsed=1，原始倍数 240/1=240 被上限 10 截断
        kline = self._kline_with_today(today_vol=0.5)
        ratio, avg = _compute_volume_metrics(kline, "2026-01-06",
                                             now=datetime(2026, 6, 18, 9, 31))
        assert ratio == 5.0

    def test_first_minute_projects_at_same_factor_as_second(self):
        # 09:30:00（首分钟 elapsed=1）与 09:31:00（elapsed=1）投影倍数一致,无跳变
        kline = self._kline_with_today(today_vol=0.5)
        r_open, _ = _compute_volume_metrics(kline, "2026-01-06",
                                            now=datetime(2026, 6, 18, 9, 30))
        r_next, _ = _compute_volume_metrics(kline, "2026-01-06",
                                            now=datetime(2026, 6, 18, 9, 31))
        assert r_open == r_next == 5.0

    def test_no_projection_after_close(self):
        # 收盘后 elapsed=240 → 不投影
        kline = self._kline_with_today(today_vol=0.5)
        ratio, avg = _compute_volume_metrics(kline, "2026-01-06",
                                             now=datetime(2026, 6, 18, 15, 1))
        assert ratio == 0.5

    def test_no_projection_when_no_today_bar(self):
        # 末根日期非今日 → 视为昨日全天量，不投影
        kline = _kline([2, 1, -1, 2, 4], volumes=[1.0, 1.0, 1.0, 1.0, 0.5])
        ratio, avg = _compute_volume_metrics(kline, "2026-01-06",
                                             now=datetime(2026, 6, 18, 9, 55))
        assert ratio == 0.5

    def test_afternoon_projection(self):
        # 14:00 → elapsed=180，投影倍数 = 240/180
        kline = self._kline_with_today(today_vol=1.5)
        ratio, avg = _compute_volume_metrics(kline, "2026-01-06",
                                             now=datetime(2026, 6, 18, 14, 0))
        assert ratio == round(1.5 * 240 / 180, 2)

    def test_zero_baseline_fails_closed(self):
        # 基准窗口全 0（脏数据残留：NaN→0 等）→ avg_vol==0，量比 fail-closed 返回 0.0
        # 而非旧 1.0 白过 short_term 量比硬门（validator: vol_ratio < 1.0 → fail）。
        # 今日（末根）即便真实放量(3.0)也无法抵消不可信基准 → 仍判 0.0。
        kline = [
            {"date": "2026-01-02", "close": 10.0, "volume": 0.0},
            {"date": "2026-01-03", "close": 10.0, "volume": 0.0},
            {"date": "2026-01-04", "close": 10.0, "volume": 0.0},
            {"date": "2026-01-05", "close": 10.0, "volume": 0.0},
            {"date": "2026-01-06", "close": 10.5, "volume": 3.0},
        ]
        ratio, avg = _compute_volume_metrics(kline, "2026-01-06",
                                             now=datetime(2026, 1, 6, 10, 0))
        assert avg == 0.0
        assert ratio == 0.0


