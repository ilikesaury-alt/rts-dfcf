from datetime import datetime
from unittest.mock import patch

from scanner.config import (
    DISTRIBUTION_RANK_WEAK_INTRADAY,
    V_ST_RANK_LOW,
    V_ST_RANK_TOP10,
    V_ST_RANK_TOP30,
    WTS_FAIL_TAG,
)
from scanner.enhancer import (
    _apply_fund_flow_bonus,
    _apply_gap_up_bonus,
    _apply_list_momentum_bonus,
    _apply_live_vol_bonus,
    _apply_rps_bonus,
    _apply_sector_bonus,
    _apply_sentiment_bonus,
    _apply_turnover_bonus,
    _apply_zt_bonus,
    _detect_main_force_distribution,
    _record_dimensions,
    _set_risk_flags,
    accumulate_final_score,
    compute_market_env_bonus,
    compute_time_bonus,
)
from scanner.models import Candidate, KlineSummary, StockInfo


def _make_candidate(symbol="300999", name="测试", category="momentum",
                    rank=50, percent=5.0, accumulated_pct=10.0,
                    volume_ratio=1.0, avg_volume=1000.0, dimensions=None):
    stock = StockInfo(symbol=symbol, name=name, code=symbol,
                      percent=percent, current=15.0, value=5000,
                      rank_change=100, rank=rank)
    kline = KlineSummary(
        trend="test", accumulated_pct=accumulated_pct,
        volume_ratio=volume_ratio, bottom_confirmed=False,
        score=30, dimensions=dict(dimensions or {}), avg_volume=avg_volume,
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
        assert accumulate_final_score(c, {}) == 0

    def test_basic_sum(self):
        stock = StockInfo(symbol="300999", name="测试", code="300999",
                          percent=5.0, current=15.0, value=5000, rank_change=100, rank=30)
        c = Candidate(stock=stock, category="new_face", score=10, reason="test", kline=None)
        # 热度放大器（板块集群/实时量比/时间）必须被排除出排序键
        c.sector_bonus = 3
        c.live_vol_bonus = 2
        c.time_bonus = 3
        assert accumulate_final_score(c, {}) == 0
        # 质量/策略类 bonus 仍计入排序键
        c.first_today_bonus = 3
        c.gap_up_bonus = 2
        c.fund_flow_bonus = 3
        assert accumulate_final_score(c, {}) == 8


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
    """streak 语义（2026-08-07 修复）：以交易日计 = cross_days(历史连续天数) + 今日上榜1天；
    intraday_streak(扫描次数 60s/次) 仅作"今日在榜"=+1，不再 max() 当"日"（防 240 次饱和）。"""

    @patch("scanner.enhancer.rank_trajectory_score", return_value=0)
    def test_streak_2(self, mock_traj):
        c = _make_candidate()
        _apply_list_momentum_bonus(c, list_streaks={"300999": 1}, cross_days=1)
        assert c.list_momentum_bonus == 3

    @patch("scanner.enhancer.rank_trajectory_score", return_value=0)
    def test_streak_3(self, mock_traj):
        c = _make_candidate()
        _apply_list_momentum_bonus(c, list_streaks={"300999": 1}, cross_days=2)
        assert c.list_momentum_bonus == 5

    @patch("scanner.enhancer.rank_trajectory_score", return_value=0)
    def test_streak_5(self, mock_traj):
        c = _make_candidate()
        _apply_list_momentum_bonus(c, list_streaks={"300999": 1}, cross_days=4)
        assert c.list_momentum_bonus == 6

    @patch("scanner.enhancer.rank_trajectory_score", return_value=0)
    def test_streak_1_no_bonus(self, mock_traj):
        c = _make_candidate()
        _apply_list_momentum_bonus(c, list_streaks={"300999": 1})
        assert c.list_momentum_bonus == 0

    @patch("scanner.enhancer.rank_trajectory_score", return_value=0)
    def test_intraday_scan_count_not_treated_as_days(self, mock_traj):
        """扫描次数 240 只计今日 1 天，不触发疲劳饱和。"""
        c = _make_candidate(accumulated_pct=5.0, volume_ratio=0.8)
        c.category = "momentum"
        _apply_list_momentum_bonus(c, list_streaks={"300999": 240}, cross_days=2)
        assert c.list_momentum_bonus == -9

    @patch("scanner.enhancer.rank_trajectory_score", return_value=0)
    def test_fatigue_penalty(self, mock_traj):
        c = _make_candidate(accumulated_pct=5.0, volume_ratio=0.8)
        c.category = "momentum"
        _apply_list_momentum_bonus(c, list_streaks={"300999": 1}, cross_days=2)
        assert c.list_momentum_bonus == -9

    @patch("scanner.enhancer.rank_trajectory_score", return_value=0)
    def test_fatigue_penalty_cap(self, mock_traj):
        c = _make_candidate(accumulated_pct=5.0, volume_ratio=0.8)
        c.category = "momentum"
        _apply_list_momentum_bonus(c, list_streaks={"300999": 1}, cross_days=5)
        assert c.list_momentum_bonus == -15

    @patch("scanner.enhancer.rank_trajectory_score", return_value=0)
    def test_fatigue_new_face_skips_price(self, mock_traj):
        c = _make_candidate(accumulated_pct=3.0, volume_ratio=1.5,
                            percent=2.0, category="new_face")
        _apply_list_momentum_bonus(c, list_streaks={"300999": 1}, cross_days=2)
        assert c.list_momentum_bonus == 5

    @patch("scanner.enhancer.rank_trajectory_score", return_value=0)
    def test_fatigue_comeback_off_list_full_exemption(self, mock_traj):
        """回归（2026-08-14）：off_list（回马枪）整体豁免榜单动能——cross_days 是
        掉榜前残留（跟踪池最长保留 15 交易日），traj 来自掉榜前排名快照，均不构成
        真实榜单动能。负累计 + 残留 streak 不得触发疲劳罚分。"""
        c = _make_candidate(accumulated_pct=-10.0, volume_ratio=0.8,
                            percent=2.0, category="comeback")
        c.off_list = True
        c.comeback_variant = "反转"
        _apply_list_momentum_bonus(c, list_streaks={"300999": 1}, cross_days=2)
        assert c.list_momentum_bonus == 0, (
            f"off_list 应整体豁免榜单动能, got {c.list_momentum_bonus}")

    @patch("scanner.enhancer.rank_trajectory_score", return_value=-2)
    def test_fatigue_comeback_stale_volume_traj_not_counted(self, mock_traj):
        """回归（2026-08-14）：掉榜票残留的量能/轨迹信号不再计入疲劳——回踩变体
        volume_ratio 曾是占位 0.0（恒触发缩量信号）、traj 是掉榜前快照，二者叠加
        会给回马枪候选误打「疲劳」标签。"""
        c = _make_candidate(accumulated_pct=-10.0, volume_ratio=0.5,
                            percent=-1.0, category="comeback")
        c.off_list = True
        c.comeback_variant = "回踩"
        _apply_list_momentum_bonus(c, list_streaks={"300999": 1}, cross_days=5)
        assert c.list_momentum_bonus == 0, (
            f"off_list 残留数据不得触发疲劳/加分, got {c.list_momentum_bonus}")

    @patch("scanner.enhancer.rank_trajectory_score", return_value=0)
    def test_accelerating(self, mock_traj):
        c = _make_candidate(percent=5.0, volume_ratio=1.5)
        c.category = "momentum"
        _apply_list_momentum_bonus(c, list_streaks={"300999": 1}, cross_days=2)
        assert c.list_momentum_bonus == 6

    @patch("scanner.enhancer.rank_trajectory_score", return_value=4)
    def test_trajectory_bonus(self, mock_traj):
        c = _make_candidate()
        _apply_list_momentum_bonus(c, list_streaks={"300999": 1}, cross_days=1)
        assert c.list_momentum_bonus == 7

    @patch("scanner.enhancer.rank_trajectory_score", return_value=-2)
    def test_trajectory_negative(self, mock_traj):
        c = _make_candidate()
        _apply_list_momentum_bonus(c, list_streaks={"300999": 1}, cross_days=1)
        assert c.list_momentum_bonus == 1

    @patch("scanner.enhancer.rank_trajectory_score", return_value=0)
    def test_trajectory_zero_insufficient_history_not_fatigue(self, mock_traj):
        """traj=0（每日开盘 tracker 重置后历史不足）不再计入疲劳信号。"""
        c = _make_candidate(accumulated_pct=5.0, volume_ratio=0.8)
        c.category = "momentum"
        _apply_list_momentum_bonus(c, list_streaks={"300999": 1}, cross_days=2)
        assert c.list_momentum_bonus == -9

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
    def test_comeback_rank0_no_top40_bonus(self, mock_traj):
        """回归：回马枪掉榜票 rank=0（无榜单排名）不得被当作"榜上第 1 名"计
        TOP40/top20 加分——此前 off-list 候选 list_momentum_bonus 虚高 +13，
        超过真实榜上前 40 名的加分，违背"掉榜无热榜背书、比榜上更严"的设计。"""
        c = _make_candidate(rank=0, category="comeback")
        c.off_list = True
        c.comeback_variant = "反转"
        _apply_list_momentum_bonus(c, list_streaks={})
        assert c.list_momentum_bonus == 0, (
            f"off-list rank=0 不应有榜单动能加分, got {c.list_momentum_bonus}")
        # off_list 整体豁免后不写榜单动能维度（2026-08-14 口径）
        assert c.kline.dimensions.get("list_top40_bonus") is None

    @patch("scanner.enhancer.rank_trajectory_score", return_value=0)
    def test_no_kline(self, mock_traj):
        c = _make_candidate()
        c.kline = None
        _apply_list_momentum_bonus(c, list_streaks={"300999": 1}, cross_days=1)
        assert c.list_momentum_bonus == 3

    @patch("scanner.enhancer.rank_trajectory_score", return_value=0)
    def test_accelerating_writes_separate_fatigue_accelerate_key(self, mock_traj):
        """回归（2026-08-17 审查修复）：加速奖励写独立键 fatigue_accelerate（正值），
        fatigue 键保持「疲劳惩罚」语义。此前同名覆写后 dims["fatigue"] 为 +6——
        _set_risk_flags 判 <0 才打「疲劳」标签不受影响，但 backtest dimension_ic 按整列
        归因会把加速奖励解析进「疲劳」因子（正负混用，归因失真）。"""
        c = _make_candidate(percent=5.0, volume_ratio=1.5)
        c.category = "momentum"
        _apply_list_momentum_bonus(c, list_streaks={"300999": 1}, cross_days=2)
        assert c.list_momentum_bonus == 6
        assert c.kline.dimensions.get("fatigue_accelerate") == 6
        assert c.kline.dimensions.get("fatigue") is None, (
            f"fatigue 键应保持未写入（仅疲劳罚分写负值）, got {c.kline.dimensions.get('fatigue')}")

    @patch("scanner.enhancer.rank_trajectory_score", return_value=0)
    def test_fatigue_rebound_skips_price_signal(self, mock_traj):
        """回归（2026-08-17 审查修复）：rebound 加入 is_reversal 豁免——超跌反弹负累计
        是策略核心前提（与 RPS 豁免同理由），连榜≥3 天时不得按价格判疲劳。
        此前 rebound 漏在 is_reversal 外：accumulated_pct<8 恒真 + 低量比 0.8<1.0
        = 2 疲劳信号 → 误打疲劳罚分 -9 与「疲劳」风险标签。"""
        c = _make_candidate(accumulated_pct=-12.0, volume_ratio=0.8, percent=2.0)
        c.category = "rebound"
        _apply_list_momentum_bonus(c, list_streaks={"300999": 1}, cross_days=2)
        assert c.list_momentum_bonus == 5, (
            f"rebound 应豁免价格信号 → streak=3 榜单动能 5, got {c.list_momentum_bonus}")
        assert c.kline.dimensions.get("fatigue") is None


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
        # accum=20, vol_ratio=2.6 满足模式1其他条件（>2.5 阈值），仅 today_pct 变化
        c = _make_candidate(accumulated_pct=20.0, percent=0.8, volume_ratio=2.6)
        assert not _detect_main_force_distribution(c, c.kline.dimensions), \
            "today_pct=0.8% 在过渡区（0.5~1.0），不应触发滞涨"

    def test_today_pct_clearly_flat_triggers(self):
        """模式1：today_pct 明确滞涨（<0.5%）+ 明显放量（量比≥2.5）应触发。"""
        c = _make_candidate(accumulated_pct=20.0, percent=0.3, volume_ratio=2.6)
        assert _detect_main_force_distribution(c, c.kline.dimensions)

    def test_risk_flags_stable_across_flicker(self):
        """端到端：intraday 在 0 附近震荡时 risk_flags 不含"主力出货"。"""
        for intra in [-0.4, 0.0, 0.4]:
            c = self._make_dist_candidate(intraday_score=intra, today_pct=0.0)
            _set_risk_flags(c)
            assert "主力出货" not in c.risk_flags, \
                f"intraday={intra} 时不应闪烁出'主力出货'标签"


# ============================================================
# 2026-07-28 风险标签收敛回归：避免"全民告警"误伤正常强势股
# ============================================================

class TestRiskFlagTightening:
    """验证收紧后：活跃强势股不再被乱贴'超买'/'主力出货'，
    仅真正高位派发（genuine 过热换手 + 极端超买）才标'主力出货'。
    """

    def test_healthy_momentum_no_false_tags(self):
        """正常强势动量票（涨 8%、累计 12%、换手活跃但不过热、无超买旗）不应被贴标签。"""
        c = _make_candidate(category="momentum", percent=8.0,
                            accumulated_pct=12.0, volume_ratio=1.5)
        c.turnover_bonus = 3  # 换手 5~10%，活跃但非过热（>0 且非 <0）
        # 无 st/mo overbought 旗、无其它风险维度
        _set_risk_flags(c)
        assert "超买" not in c.risk_flags, f"正常强势票不应标超买, flags={c.risk_flags}"
        assert "主力出货" not in c.risk_flags, f"正常强势票不应标主力出货, flags={c.risk_flags}"

    def test_today_breakout_ma_support_no_trend_breakage(self):
        """2026-08-14 修复回归：今日放量突破站上 MA5 的票（v_st_ma=SUPPORT）

        不应被打「趋势破位」硬过滤标签。此前 validate_short_term 用昨日收盘
        （closes[-1]）判定 MA，昨日在 MA5 下方 + 今日放量突破的标准买点票被误判
        V_ST_MA_BROKEN → enhancer 打「趋势破位」→ 硬过滤移出推荐（行云科技
        2026-08-14 案例：昨收 34.67 < MA5 36.33，今日 +6.68% 站上却被移出）。
        """
        from scanner.config import V_ST_MA_BROKEN, V_ST_MA_SUPPORT
        from scanner.enhancer import _detect_trend_breakage

        # 今日已站上 MA5 → v_st_ma=SUPPORT（修复后 validate 输出）
        c = _make_candidate(category="short_term", percent=5.0,
                            accumulated_pct=5.0, volume_ratio=1.5,
                            dimensions={"v_st_ma": V_ST_MA_SUPPORT})
        assert not _detect_trend_breakage(c.kline.dimensions), \
            "今日站上 MA5（SUPPORT）不应判趋势破位"
        _set_risk_flags(c)
        assert "趋势破位" not in c.risk_flags, f"不应误打趋势破位, flags={c.risk_flags}"

        # 对照：今日仍在 MA5 下方 → v_st_ma=BROKEN → 仍应判趋势破位（真破位止损）
        c2 = _make_candidate(category="short_term", percent=5.0,
                             accumulated_pct=5.0, volume_ratio=1.5,
                             dimensions={"v_st_ma": V_ST_MA_BROKEN})
        assert _detect_trend_breakage(c2.kline.dimensions), \
            "今日仍在 MA5 下方（BROKEN）应判趋势破位（真破位止损信号）"

    def test_distribution_rule2_needs_overheated_turnover(self):
        """主力出货 Rule 2：累计≥15% + 超买旗，但换手仅'活跃'(>0 非过热) 不应触发。"""
        c = _make_candidate(category="momentum", accumulated_pct=18.0,
                            volume_ratio=1.5)
        c.turnover_bonus = 3  # 换手 5~10%：活跃但 < 20%（非过热）
        c.kline.dimensions["mo_overbought_flag"] = True  # 已收紧的极端超买旗
        assert not _detect_main_force_distribution(c, c.kline.dimensions), \
            "Rule 2 仅活跃换手(>0)不应判出货，需 turnover_bonus<0（过热）"

    def test_distribution_rule2_triggers_on_overheated_turnover(self):
        """主力出货 Rule 2：累计≥15% + 极端超买 + 真正过热换手(>20%) 应触发。"""
        c = _make_candidate(category="momentum", accumulated_pct=18.0,
                            volume_ratio=1.5)
        c.turnover_bonus = -3  # 换手 > 20%：genuine 派发级过热
        c.kline.dimensions["mo_overbought_flag"] = True
        assert _detect_main_force_distribution(c, c.kline.dimensions), \
            "Rule 2 过热换手 + 极端超买应判出货"

    def test_low_turnover_high_accum_not_distribution(self):
        """累计很高但换手低迷（无放量），不应被'高位高换手'规则误判。"""
        c = _make_candidate(category="momentum", accumulated_pct=20.0,
                            volume_ratio=1.2)
        c.turnover_bonus = 0  # 换手 ≤5%：低迷
        c.kline.dimensions["mo_overbought_flag"] = True
        assert not _detect_main_force_distribution(c, c.kline.dimensions), \
            "高累计+低迷换手不应判主力出货（无放量派发特征）"


# ============================================================
# 2026-08-14 Rule 5：后排上榜 + 盘中走弱 → 主力出货（硬过滤）
# 依据：short_term 去重 218 条中该画像 12 条，next_day -1.90%/胜率25%，
# cum_3d -4.24%/胜率12%（n=8）——全历史最强负向组合（300317 珈伟新能案例）。
# ============================================================

class TestDistributionRule5BackrowIntradayWeak:
    """验证主力出货 Rule 5：short_term 后排上榜（rank>30，v_st_rank 负值）
    且分时持续走弱（intraday <= -1.5）即判定冲高派发，无需累计涨幅背书。
    """

    @staticmethod
    def _rule5_candidate(intraday, v_st_rank=V_ST_RANK_LOW, accumulated=10.0):
        """构造 300317 型画像：累计 10%（<15%，rule 3 不触发）+ 后排 + 盘中弱。"""
        c = _make_candidate(category="short_term", rank=99,
                            accumulated_pct=accumulated, percent=3.5)
        c.kline.dimensions["v_st_rank"] = v_st_rank
        c.intraday_score = intraday
        c.kline.dimensions["intraday_score"] = intraday
        return c

    def test_backrow_intraday_weak_triggers(self):
        """后排(rank>30) + intraday<=-1.5：即使累计仅 10% 也应判主力出货。"""
        c = self._rule5_candidate(intraday=DISTRIBUTION_RANK_WEAK_INTRADAY)
        assert _detect_main_force_distribution(c, c.kline.dimensions), \
            "后排+盘中弱应触发 Rule 5（300317 型画像）"

    def test_backrow_intraday_borderline_no_trigger(self):
        """intraday 在 -1.5 之上（-1.4）不触发：阈值带宽防闪烁。"""
        c = self._rule5_candidate(intraday=DISTRIBUTION_RANK_WEAK_INTRADAY + 0.1)
        assert not _detect_main_force_distribution(c, c.kline.dimensions), \
            "intraday=-1.4 未达 -1.5 阈值不应触发"

    def test_backrow_intraday_neutral_no_trigger(self):
        """后排但盘中中性（intraday=0）不触发：分时走弱是必要条件。"""
        c = self._rule5_candidate(intraday=0.0)
        assert not _detect_main_force_distribution(c, c.kline.dimensions), \
            "盘中中性不应触发 Rule 5"

    def test_front_rank_intraday_weak_no_trigger(self):
        """前排上榜（rank<=30）即使盘中弱也不触发：无边际派发语义。"""
        for bonus in (V_ST_RANK_TOP30, V_ST_RANK_TOP10):
            c = self._rule5_candidate(intraday=-2.0, v_st_rank=bonus)
            assert not _detect_main_force_distribution(c, c.kline.dimensions), \
                f"前排(v_st_rank={bonus})不应触发 Rule 5"

    def test_no_v_st_rank_dim_no_trigger(self):
        """非 short_term 候选（无 v_st_rank 维度）即使盘中弱也不触发。"""
        c = _make_candidate(category="momentum", rank=99, accumulated_pct=10.0)
        c.intraday_score = -2.0
        c.kline.dimensions["intraday_score"] = -2.0
        assert not _detect_main_force_distribution(c, c.kline.dimensions), \
            "momentum（无 v_st_rank）不应触发 Rule 5"

    def test_end_to_end_risk_flag_hard_filter(self):
        """端到端：_set_risk_flags 应打上'主力出货'标签（RISK_FLAGS_HARD_FILTER）。"""
        c = self._rule5_candidate(intraday=-1.5)
        _set_risk_flags(c)
        assert "主力出货" in c.risk_flags, f"应打主力出货标签, flags={c.risk_flags}"


# ============================================================
# 2026-08-14 弱转强失效：弱转强(v_st_weak>0) + 分时明确走弱(intraday<=-1.0)
# → 硬过滤。依据：全期 12 样本大跌(≤-7%) 25% vs 大涨 8.3%，平均次日 -2.61%，
# 含 -16.34/-18.44 两个极端日（301230/301583，均弱转强+盘中弱）。
# ============================================================

class TestWtsFailureHardFilter:
    """验证弱转强失效标签：弱转强当日分时明确走弱即判定转强失败并硬过滤。"""

    @staticmethod
    def _wts_candidate(intraday, wts=8):
        c = _make_candidate(category="short_term", rank=20, percent=5.0)
        c.kline.dimensions["v_st_weak"] = wts  # 弱转强直通维度
        c.intraday_score = intraday
        c.kline.dimensions["intraday_score"] = intraday
        return c

    def test_wts_intraday_weak_triggers(self):
        """弱转强 + intraday=-1.0（明确走弱）→ 打弱转强失效标签。"""
        c = self._wts_candidate(intraday=-1.0)
        _set_risk_flags(c)
        assert WTS_FAIL_TAG in c.risk_flags, f"应打弱转强失效, flags={c.risk_flags}"

    def test_wts_intraday_borderline_no_trigger(self):
        """intraday=-0.9（-1.0 之上）不触发：带宽阈值防闪烁。"""
        c = self._wts_candidate(intraday=-0.9)
        _set_risk_flags(c)
        assert WTS_FAIL_TAG not in c.risk_flags, f"intraday=-0.9 不应触发, flags={c.risk_flags}"

    def test_wts_intraday_positive_no_trigger(self):
        """弱转强 + 盘中走强（intraday>0）不触发：转强成功。"""
        c = self._wts_candidate(intraday=1.0)
        _set_risk_flags(c)
        assert WTS_FAIL_TAG not in c.risk_flags, f"盘中走强不应判失效, flags={c.risk_flags}"

    def test_non_wts_intraday_weak_no_trigger(self):
        """非弱转强（v_st_weak=0）即使盘中弱也不触发：仅针对弱转强直通。"""
        c = self._wts_candidate(intraday=-2.0, wts=0)
        _set_risk_flags(c)
        assert WTS_FAIL_TAG not in c.risk_flags, f"非弱转强不应判失效, flags={c.risk_flags}"

    def test_hard_filter_membership(self):
        """弱转强失效应在 RISK_FLAGS_HARD_FILTER 中（从所有推荐列表移除）。"""
        from scanner.config import RISK_FLAGS_HARD_FILTER
        assert WTS_FAIL_TAG in RISK_FLAGS_HARD_FILTER

    def test_end_to_end_hard_filter_removal(self):
        """端到端：命中弱转强失效的候选被 orchestrator 硬过滤移出。"""
        from scanner.orchestrator import _candidate_excluded_by_risk
        c = self._wts_candidate(intraday=-1.5)
        _set_risk_flags(c)
        assert _candidate_excluded_by_risk(c), "弱转强失效候选应被硬过滤"
        c2 = self._wts_candidate(intraday=1.0)
        _set_risk_flags(c2)
        assert not _candidate_excluded_by_risk(c2), "转强成功的弱转强候选不应被硬过滤"


# ============================================================
# 行情增强 bonus：资金流 / 涨停连板（2026-08-06 新增）
# ============================================================

class TestApplyFundFlowBonus:
    @staticmethod
    def _flow(c, main_pct, main_net, super_net):
        return {c.stock.symbol: {"fund_flow": {"main_pct": main_pct,
                                                "main_net": main_net,
                                                "super_net": super_net}}}

    def test_strong_inflow(self):
        c = _make_candidate()
        _apply_fund_flow_bonus(c, self._flow(c, 8.5, 123456789.0, 6e7))
        # 2026-08-10: 强流入加分归零（回测强流入组 next_day -1.13% 反指），字段仍写入 dims
        assert c.fund_flow_bonus == 0
        assert c.kline.dimensions.get("fund_flow_main_pct") == 8.5
        assert c.kline.dimensions.get("fund_flow_main_net") == 123456789.0

    def test_weak_outflow(self):
        c = _make_candidate()
        _apply_fund_flow_bonus(c, self._flow(c, -6.0, -1e8, -2e7))
        assert c.fund_flow_bonus == -3

    def test_neutral(self):
        c = _make_candidate()
        _apply_fund_flow_bonus(c, self._flow(c, 2.0, 1e6, 0))
        assert c.fund_flow_bonus == 0

    def test_no_flow(self):
        c = _make_candidate()
        _apply_fund_flow_bonus(c, None)
        assert c.fund_flow_bonus == 0

    def test_garbage_flow_values_no_crash(self):
        """回归：market_extra 中 main_pct 为不可解析字符串/NaN/None 时
        float() 不能抛 ValueError 拖垮整轮扫描（数据入口防御）。"""
        c = _make_candidate()
        _apply_fund_flow_bonus(c, {c.stock.symbol: {"fund_flow": {
            "main_pct": "abc", "main_net": "NaN", "super_net": None}}})
        assert c.fund_flow_bonus == 0
        assert c.kline.dimensions.get("fund_flow_main_pct") == 0.0

    def test_inf_flow_coerced(self):
        """回归：main_pct=±inf（Python json 字面量）→ 0，不触发档位判断
        （inf >= 5 恒真，此前会把 inf 当作强流入展示）。"""
        import math
        c = _make_candidate()
        _apply_fund_flow_bonus(c, {c.stock.symbol: {"fund_flow": {
            "main_pct": float("inf"), "main_net": float("-inf"), "super_net": float("nan")}}})
        assert c.fund_flow_bonus == 0
        v = c.kline.dimensions.get("fund_flow_main_pct")
        assert v == 0.0 and math.isfinite(v)
        assert c.kline.dimensions.get("fund_flow_main_net") == 0.0
        assert c.kline.dimensions.get("fund_flow_super_net") == 0.0


class TestApplyZtBonus:
    @staticmethod
    def _zt(c, lianban, zhaban, industry="软件"):
        return {c.stock.symbol: {"zt": {"lianban": lianban, "zhaban": zhaban,
                                        "industry": industry}}}

    def test_3board_momentum(self):
        c = _make_candidate(category="momentum")
        _apply_zt_bonus(c, self._zt(c, 3, 0))
        assert c.zt_lianban_bonus == 8
        assert c.kline.dimensions.get("zt_lianban") == 3

    def test_2board_short_term(self):
        c = _make_candidate(category="short_term")
        _apply_zt_bonus(c, self._zt(c, 2, 1))
        assert c.zt_lianban_bonus == 5

    def test_over4_board_penalty(self):
        c = _make_candidate(category="momentum")
        _apply_zt_bonus(c, self._zt(c, 4, 0))
        assert c.zt_lianban_bonus == -5

    def test_new_face_ignored(self):
        c = _make_candidate(category="new_face")
        _apply_zt_bonus(c, self._zt(c, 3, 0))
        assert c.zt_lianban_bonus == 0

    def test_garbage_zt_values_no_crash(self):
        """回归：zt lianban/zhaban 为不可解析字符串/None 时 int() 不能抛
        ValueError 拖垮整轮扫描（数据入口防御）。"""
        c = _make_candidate(category="momentum")
        _apply_zt_bonus(c, {c.stock.symbol: {"zt": {
            "lianban": "abc", "zhaban": "x", "industry": None}}})
        assert c.zt_lianban_bonus == 0
        assert c.kline.dimensions.get("zt_lianban") == 0
        assert c.kline.dimensions.get("zt_zhaban") == 0


class TestMarketExtraRiskFlags:
    def test_fund_outflow_flag(self):
        c = _make_candidate()
        _apply_fund_flow_bonus(c, {c.stock.symbol: {"fund_flow": {
            "main_pct": -9.0, "main_net": -2e8, "super_net": -5e7}}})
        _set_risk_flags(c)
        assert "资金流出" in c.risk_flags

    def test_no_outflow_flag_on_moderate(self):
        c = _make_candidate()
        _apply_fund_flow_bonus(c, {c.stock.symbol: {"fund_flow": {
            "main_pct": -4.0, "main_net": -5e7, "super_net": -1e7}}})
        _set_risk_flags(c)
        assert "资金流出" not in c.risk_flags

    def test_zhaban_flag(self):
        c = _make_candidate()
        _apply_zt_bonus(c, {c.stock.symbol: {"zt": {
            "lianban": 1, "zhaban": 2, "industry": "软件"}}})
        _set_risk_flags(c)
        assert "炸板" in c.risk_flags
        assert "资金流出" not in c.risk_flags


class TestAccumulateWithMarketExtra:
    def test_rank_key_excludes_heat_bonuses(self):
        c = _make_candidate()
        # sector_bonus 属热度放大器(板块集群)，不计入排序键；
        # fund_flow / zt_lianban 属质量/策略类，计入。
        c.sector_bonus = 2
        c.fund_flow_bonus = 5
        c.zt_lianban_bonus = 8
        result = accumulate_final_score(c, {})
        assert result == 13


class TestApplyAllBonusesFundRisk:
    """apply_all_bonuses 全链路：fund_risk 参数 → _set_risk_flags 打财务风险标签。"""

    def _run(self, fund_risk):
        c = _make_candidate(symbol="SZ300027")
        from scanner.enhancer import apply_all_bonuses
        apply_all_bonuses(
            [c], [], {}, {}, {}, {}, {}, None, 0,
            sentiment_info=None, rps_scores=None, list_streaks=None,
            market_extra=None, fund_risk=fund_risk, conn=None,
        )
        return c

    def test_hit_appends_fund_risk_tag(self):
        from scanner.config import FUND_RISK_TAG
        c = self._run({"SZ300027": "资不抵债"})
        assert FUND_RISK_TAG in c.risk_flags

    def test_miss_no_tag(self):
        c = self._run({"SZ300999": "资不抵债"})
        assert "财务风险" not in c.risk_flags

    def test_none_default_no_tag(self):
        c = self._run(None)
        assert "财务风险" not in c.risk_flags

