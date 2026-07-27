from datetime import datetime
from unittest.mock import patch

from scanner.enhancer import (
    _apply_gap_up_bonus,
    _apply_live_vol_bonus,
    _apply_list_momentum_bonus,
    _apply_rps_bonus,
    _apply_sector_bonus,
    _apply_sentiment_bonus,
    _apply_turnover_bonus,
    _detect_main_force_distribution,
    _set_risk_flags,
    _record_dimensions,
    compute_market_env_bonus,
    compute_time_bonus,
    accumulate_final_score,
)
from scanner.models import Candidate, KlineSummary, StockInfo


def _make_candidate(symbol="300999", name="测试", category="momentum",
                    rank=50, percent=5.0, accumulated_pct=10.0,
                    volume_ratio=1.0, avg_volume=1000.0):
    stock = StockInfo(symbol=symbol, name=name, code=symbol,
                      percent=percent, current=15.0, value=5000,
                      rank_change=100, rank=rank)
    kline = KlineSummary(
        trend="test", accumulated_pct=accumulated_pct,
        volume_ratio=volume_ratio, bottom_confirmed=False,
        score=30, dimensions={}, avg_volume=avg_volume,
    )
    return Candidate(stock=stock, category=category, score=30,
                     reason="test", kline=kline)


# ============================================================
# Original tests (preserved)
# ============================================================

class TestComputeTimeBonus:
    def test_early_session(self):
        dt = datetime(2026, 6, 18, 10, 0)
        assert compute_time_bonus(dt) == -2

    def test_mid_session(self):
        dt = datetime(2026, 6, 18, 13, 0)
        assert compute_time_bonus(dt) == 0

    def test_late_session(self):
        dt = datetime(2026, 6, 18, 14, 30)
        assert compute_time_bonus(dt) == 3


class TestComputeMarketEnvBonus:
    def test_strong_market(self):
        assert compute_market_env_bonus(1.0) == 2

    def test_weak_market(self):
        assert compute_market_env_bonus(-1.5) == -2

    def test_neutral_market(self):
        assert compute_market_env_bonus(0.0) == 0

    def test_unknown_market(self):
        assert compute_market_env_bonus(None) == 0


class TestAccumulateFinalScore:
    def test_all_zeros(self):
        stock = StockInfo(symbol="300999", name="测试", code="300999",
                          percent=0.0, current=10.0, value=1000, rank_change=0, rank=50)
        c = Candidate(stock=stock, category="new_face", score=10, reason="test", kline=None)
        assert accumulate_final_score(c, 0, {}) == 0

    def test_basic_sum(self):
        stock = StockInfo(symbol="300999", name="测试", code="300999",
                          percent=5.0, current=15.0, value=5000, rank_change=100, rank=30)
        c = Candidate(stock=stock, category="new_face", score=10, reason="test", kline=None)
        c.sector_bonus = 3
        c.live_vol_bonus = 2
        c.time_bonus = 3
        result = accumulate_final_score(c, 0, {})
        assert result == 8


# ============================================================
# _apply_sector_bonus
# ============================================================

class TestApplySectorBonus:
    def test_cluster_5(self):
        c = _make_candidate(name="某某芯片")
        clusters = {"半导体": ["A", "B", "C", "D", "E"]}
        _apply_sector_bonus(c, clusters)
        assert c.sector == "半导体"
        assert c.sector_bonus == 8

    def test_cluster_4(self):
        c = _make_candidate(name="某某芯片")
        clusters = {"半导体": ["A", "B", "C", "D"]}
        _apply_sector_bonus(c, clusters)
        assert c.sector_bonus == 6

    def test_cluster_3(self):
        c = _make_candidate(name="某某芯片")
        clusters = {"半导体": ["A", "B", "C"]}
        _apply_sector_bonus(c, clusters)
        assert c.sector_bonus == 4

    def test_cluster_2(self):
        c = _make_candidate(name="某某芯片")
        clusters = {"半导体": ["A", "B"]}
        _apply_sector_bonus(c, clusters)
        assert c.sector_bonus == 2

    def test_cluster_1(self):
        c = _make_candidate(name="某某芯片")
        clusters = {"半导体": ["A"]}
        _apply_sector_bonus(c, clusters)
        assert c.sector_bonus == 0

    def test_other_sector(self):
        c = _make_candidate(name="某某科技")
        clusters = {}
        _apply_sector_bonus(c, clusters)
        assert c.sector == "其他"
        assert c.sector_bonus == 0


# ============================================================
# _apply_turnover_bonus
# ============================================================

class TestApplyTurnoverBonus:
    def test_high_turnover(self):
        c = _make_candidate()
        c.market_cap = 100_000_000
        market_caps = {"300999": {"turnover_rate": 25}}
        _apply_turnover_bonus(c, market_caps)
        assert c.turnover_bonus == -3

    def test_moderate_turnover(self):
        c = _make_candidate()
        c.market_cap = 100_000_000
        market_caps = {"300999": {"turnover_rate": 15}}
        _apply_turnover_bonus(c, market_caps)
        assert c.turnover_bonus == 3

    def test_healthy_turnover(self):
        c = _make_candidate()
        c.market_cap = 100_000_000
        market_caps = {"300999": {"turnover_rate": 8}}
        _apply_turnover_bonus(c, market_caps)
        assert c.turnover_bonus == 5

    def test_low_turnover(self):
        c = _make_candidate()
        c.market_cap = 100_000_000
        market_caps = {"300999": {"turnover_rate": 3}}
        _apply_turnover_bonus(c, market_caps)
        assert c.turnover_bonus == 0

    def test_no_market_cap(self):
        c = _make_candidate()
        c.market_cap = 0
        market_caps = {"300999": {"turnover_rate": 15}}
        _apply_turnover_bonus(c, market_caps)
        assert c.turnover_bonus == 0

    def test_no_turnover_data(self):
        c = _make_candidate()
        c.market_cap = 100_000_000
        market_caps = {"300999": {}}
        _apply_turnover_bonus(c, market_caps)
        assert c.turnover_bonus == 0


# ============================================================
# _apply_live_vol_bonus
# ============================================================

class TestApplyLiveVolBonus:
    def test_above_threshold(self):
        c = _make_candidate(avg_volume=1000)
        live_volumes = {"300999": 1500.0}
        _apply_live_vol_bonus(c, live_volumes)
        assert c.live_vol_bonus == 3

    def test_below_threshold(self):
        c = _make_candidate(avg_volume=1000)
        live_volumes = {"300999": 1200.0}
        _apply_live_vol_bonus(c, live_volumes)
        assert c.live_vol_bonus == 0

    def test_no_kline(self):
        c = _make_candidate()
        c.kline = None
        live_volumes = {"300999": 1500.0}
        _apply_live_vol_bonus(c, live_volumes)
        assert c.live_vol_bonus == 0

    def test_no_live_data(self):
        c = _make_candidate()
        live_volumes = {}
        _apply_live_vol_bonus(c, live_volumes)
        assert c.live_vol_bonus == 0


# ============================================================
# _apply_sentiment_bonus / _apply_rps_bonus / _apply_gap_up_bonus
# ============================================================

class TestApplySentimentBonus:
    def test_with_sentiment(self):
        c = _make_candidate()
        _apply_sentiment_bonus(c, {"bonus": 5})
        assert c.market_sentiment_bonus == 5

    def test_no_sentiment(self):
        c = _make_candidate()
        _apply_sentiment_bonus(c, None)
        assert c.market_sentiment_bonus == 0

    def test_empty_sentiment(self):
        c = _make_candidate()
        _apply_sentiment_bonus(c, {})
        assert c.market_sentiment_bonus == 0


class TestApplyRpsBonus:
    def test_with_rps(self):
        c = _make_candidate()
        _apply_rps_bonus(c, {"300999": 4})
        assert c.rps_bonus == 4

    def test_no_rps(self):
        c = _make_candidate()
        _apply_rps_bonus(c, None)
        assert c.rps_bonus == 0

    def test_symbol_not_in_rps(self):
        c = _make_candidate()
        _apply_rps_bonus(c, {"OTHER": 4})
        assert c.rps_bonus == 0


class TestApplyGapUpBonus:
    def test_new_face_gap(self):
        c = _make_candidate(category="new_face")
        c.kline.dimensions["new_face_gap_up"] = 8
        _apply_gap_up_bonus(c)
        assert c.gap_up_bonus == 8

    def test_momentum_gap(self):
        c = _make_candidate(category="momentum")
        c.kline.dimensions["momentum_gap_up"] = 5
        _apply_gap_up_bonus(c)
        assert c.gap_up_bonus == 5

    def test_no_gap(self):
        c = _make_candidate(category="momentum")
        _apply_gap_up_bonus(c)
        assert c.gap_up_bonus == 0

    def test_no_kline(self):
        c = _make_candidate()
        c.kline = None
        _apply_gap_up_bonus(c)
        assert c.gap_up_bonus == 0


# ============================================================
# _apply_list_momentum_bonus
# ============================================================

class TestApplyListMomentumBonus:
    @patch("scanner.enhancer.rank_trajectory_score", return_value=0)
    def test_streak_2(self, mock_traj):
        c = _make_candidate()
        _apply_list_momentum_bonus(c, list_streaks={"300999": 2})
        assert c.list_momentum_bonus == 3

    @patch("scanner.enhancer.rank_trajectory_score", return_value=0)
    def test_streak_3(self, mock_traj):
        c = _make_candidate()
        _apply_list_momentum_bonus(c, list_streaks={"300999": 3})
        assert c.list_momentum_bonus == 5

    @patch("scanner.enhancer.rank_trajectory_score", return_value=0)
    def test_streak_5(self, mock_traj):
        c = _make_candidate()
        _apply_list_momentum_bonus(c, list_streaks={"300999": 5})
        assert c.list_momentum_bonus == 6

    @patch("scanner.enhancer.rank_trajectory_score", return_value=0)
    def test_streak_1_no_bonus(self, mock_traj):
        c = _make_candidate()
        _apply_list_momentum_bonus(c, list_streaks={"300999": 1})
        assert c.list_momentum_bonus == 0

    @patch("scanner.enhancer.rank_trajectory_score", return_value=0)
    def test_fatigue_penalty(self, mock_traj):
        c = _make_candidate(accumulated_pct=5.0, volume_ratio=0.8)
        c.category = "momentum"
        _apply_list_momentum_bonus(c, list_streaks={"300999": 3})
        assert c.list_momentum_bonus == -9

    @patch("scanner.enhancer.rank_trajectory_score", return_value=0)
    def test_fatigue_penalty_cap(self, mock_traj):
        c = _make_candidate(accumulated_pct=5.0, volume_ratio=0.8)
        c.category = "momentum"
        _apply_list_momentum_bonus(c, list_streaks={"300999": 6})
        assert c.list_momentum_bonus == -15

    @patch("scanner.enhancer.rank_trajectory_score", return_value=0)
    def test_fatigue_new_face_skips_price(self, mock_traj):
        c = _make_candidate(accumulated_pct=3.0, volume_ratio=1.5,
                            percent=2.0, category="new_face")
        _apply_list_momentum_bonus(c, list_streaks={"300999": 3})
        assert c.list_momentum_bonus == 5

    @patch("scanner.enhancer.rank_trajectory_score", return_value=0)
    def test_accelerating(self, mock_traj):
        c = _make_candidate(percent=5.0, volume_ratio=1.5)
        c.category = "momentum"
        _apply_list_momentum_bonus(c, list_streaks={"300999": 3})
        assert c.list_momentum_bonus == 6

    @patch("scanner.enhancer.rank_trajectory_score", return_value=4)
    def test_trajectory_bonus(self, mock_traj):
        c = _make_candidate()
        _apply_list_momentum_bonus(c, list_streaks={"300999": 2})
        assert c.list_momentum_bonus == 7

    @patch("scanner.enhancer.rank_trajectory_score", return_value=-2)
    def test_trajectory_negative(self, mock_traj):
        c = _make_candidate()
        _apply_list_momentum_bonus(c, list_streaks={"300999": 2})
        assert c.list_momentum_bonus == 1

    @patch("scanner.enhancer.rank_trajectory_score", return_value=0)
    def test_top40_bonus(self, mock_traj):
        c = _make_candidate(rank=30)
        _apply_list_momentum_bonus(c, list_streaks={})
        assert c.list_momentum_bonus == 5

    @patch("scanner.enhancer.rank_trajectory_score", return_value=0)
    def test_top20_bonus(self, mock_traj):
        c = _make_candidate(rank=15)
        _apply_list_momentum_bonus(c, list_streaks={})
        assert c.list_momentum_bonus == 9

    @patch("scanner.enhancer.rank_trajectory_score", return_value=0)
    def test_rank_50_no_top_bonus(self, mock_traj):
        c = _make_candidate(rank=50)
        _apply_list_momentum_bonus(c, list_streaks={})
        assert c.list_momentum_bonus == 0

    @patch("scanner.enhancer.rank_trajectory_score", return_value=0)
    def test_no_kline(self, mock_traj):
        c = _make_candidate()
        c.kline = None
        _apply_list_momentum_bonus(c, list_streaks={"300999": 2})
        assert c.list_momentum_bonus == 3


# ============================================================
# _record_dimensions
# ============================================================

class TestRecordDimensions:
    def test_records_sector_and_vol(self):
        c = _make_candidate()
        c.sector_bonus = 4
        c.live_vol_bonus = 5
        c.intraday_score = 0.75
        opening_scores = {"300999": 0.6}
        _record_dimensions(c, None, opening_scores)
        assert c.kline.dimensions["sector_bonus"] == 4
        assert c.kline.dimensions["live_vol_bonus"] == 5
        assert c.kline.dimensions["intraday_score"] == 0.8
        assert c.kline.dimensions["opening_score"] == 0.6

    def test_market_env_strong(self):
        c = _make_candidate()
        _record_dimensions(c, 1.0, {})
        assert c.kline.dimensions["market_env_bonus"] == 2

    def test_market_env_weak(self):
        c = _make_candidate()
        _record_dimensions(c, -2.0, {})
        assert c.kline.dimensions["market_env_bonus"] == -2

    def test_market_env_neutral(self):
        c = _make_candidate()
        _record_dimensions(c, 0.0, {})
        assert "market_env_bonus" not in c.kline.dimensions

    def test_no_kline(self):
        c = _make_candidate()
        c.kline = None
        _record_dimensions(c, 1.0, {})


# ============================================================
# 主力出货标签闪烁修复（intraday_score / today_pct 带宽阈值）
# ============================================================

class TestMainForceDistributionFlicker:
    """验证"主力出货"标签在阈值附近不反复触发/消失。

    场景：久之洋类票 —— 累计涨幅够、开盘强势，intraday_score 在 0 附近震荡。
    修复前 intraday < 0.0 触发，-0.3 触发 / +0.5 消失 / -0.2 再触发。
    修复后 intraday < -1.0 才触发，0 附近的噪声不再导致闪烁。
    """

    def _make_dist_candidate(self, intraday_score=None, today_pct=0.0,
                             accumulated=15.0, opening_score=5.0):
        c = _make_candidate(accumulated_pct=accumulated, percent=today_pct)
        c.kline.dimensions["opening_score"] = opening_score
        if intraday_score is not None:
            c.intraday_score = intraday_score
            c.kline.dimensions["intraday_score"] = intraday_score
        return c

    def test_intraday_near_zero_no_flicker(self):
        """intraday_score 在 0 附近震荡（-0.5 ~ +0.5）不应触发主力出货。"""
        for intra in [-0.5, -0.3, 0.0, 0.3, 0.5]:
            c = self._make_dist_candidate(intraday_score=intra, today_pct=0.0)
            assert not _detect_main_force_distribution(c, c.kline.dimensions), \
                f"intraday={intra} 不应触发主力出货（0 附近为中性，非走弱）"

    def test_intraday_clearly_weak_triggers(self):
        """intraday_score 明确走弱（<-1.0）应触发主力出货。"""
        c = self._make_dist_candidate(intraday_score=-1.5, today_pct=0.0)
        assert _detect_main_force_distribution(c, c.kline.dimensions)

    def test_today_pct_near_threshold_no_flicker(self):
        """模式1（放量滞涨）：today_pct 在 0.5%~1.0% 过渡区不应反复触发。"""
        # accum=20, vol_ratio=2.0 满足模式1其他条件，仅 today_pct 变化
        c = _make_candidate(accumulated_pct=20.0, percent=0.8, volume_ratio=2.0)
        assert not _detect_main_force_distribution(c, c.kline.dimensions), \
            "today_pct=0.8% 在过渡区（0.5~1.0），不应触发滞涨"

    def test_today_pct_clearly_flat_triggers(self):
        """模式1：today_pct 明确滞涨（<0.5%）应触发。"""
        c = _make_candidate(accumulated_pct=20.0, percent=0.3, volume_ratio=2.0)
        assert _detect_main_force_distribution(c, c.kline.dimensions)

    def test_risk_flags_stable_across_flicker(self):
        """端到端：intraday 在 0 附近震荡时 risk_flags 不含"主力出货"。"""
        for intra in [-0.4, 0.0, 0.4]:
            c = self._make_dist_candidate(intraday_score=intra, today_pct=0.0)
            _set_risk_flags(c)
            assert "主力出货" not in c.risk_flags, \
                f"intraday={intra} 时不应闪烁出'主力出货'标签"
