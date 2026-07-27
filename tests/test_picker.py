"""今日首选选择器测试。"""

from scanner.models import Candidate, KlineSummary, StockInfo
from scanner.picker import pick_top_candidates, BLOCKING_RISK_TAGS, PickResult


def _make_candidate(symbol="300001", name="测试票", category="momentum",
                    score=30, sector="半导体", risk_flags=None,
                    market_env_bonus=0, first_breakout_bonus=0,
                    sector_bonus=0, live_vol_bonus=0, gap_up_bonus=0):
    stock = StockInfo(symbol=symbol, name=name, code=symbol,
                      percent=5.0, current=15.0, value=5000,
                      rank_change=100, rank=50)
    kline = KlineSummary(
        trend="test", accumulated_pct=10.0,
        volume_ratio=1.5, bottom_confirmed=False,
        score=score, dimensions={}, avg_volume=1000.0,
    )
    c = Candidate(stock=stock, category=category, score=score,
                  reason="test", kline=kline)
    c.sector = sector
    c.risk_flags = list(risk_flags) if risk_flags else []
    if market_env_bonus:
        kline.dimensions["market_env_bonus"] = market_env_bonus
    c.first_breakout_bonus = first_breakout_bonus
    c.sector_bonus = sector_bonus
    c.live_vol_bonus = live_vol_bonus
    c.gap_up_bonus = gap_up_bonus
    return c


class TestPickTopCandidates:
    """核心选择逻辑测试。"""

    def test_empty_input_returns_empty(self):
        assert pick_top_candidates([]) == []

    def test_single_candidate_returns_one_pick(self):
        c = _make_candidate(symbol="300001", score=30, category="momentum")
        picks = pick_top_candidates([c], top_n=2)
        assert len(picks) == 1
        assert picks[0].candidate.stock.symbol == "300001"
        assert "momentum 30分" in picks[0].reason

    def test_strategy_priority_momentum_over_new_face(self):
        """确定性相同时，评分高的胜出（不再按策略优先级排序）。"""
        mo = _make_candidate(symbol="300001", score=35, category="momentum")
        nf = _make_candidate(symbol="300002", score=30, category="new_face")
        picks = pick_top_candidates([nf, mo], top_n=1)
        assert picks[0].candidate.stock.symbol == "300001"  # momentum 分更高

    def test_strategy_priority_new_face_over_pullback(self):
        """确定性相同时，评分高的胜出。"""
        mo = _make_candidate(symbol="300001", score=35, category="new_face")
        pb = _make_candidate(symbol="300002", score=30, category="pullback")
        picks = pick_top_candidates([pb, mo], top_n=1)
        assert picks[0].candidate.stock.symbol == "300001"

    def test_same_strategy_higher_score_wins(self):
        c1 = _make_candidate(symbol="300001", score=25, category="momentum")
        c2 = _make_candidate(symbol="300002", score=35, category="momentum")
        picks = pick_top_candidates([c1, c2], top_n=1)
        assert picks[0].candidate.stock.symbol == "300002"

    def test_known_new_face_normalized_as_new_face(self):
        """known_new_face 应归入 new_face 桶参与比较（确定性相同按评分排序）。"""
        knf = _make_candidate(symbol="300001", score=35, category="known_new_face")
        pb = _make_candidate(symbol="300002", score=30, category="pullback")
        picks = pick_top_candidates([pb, knf], top_n=1)
        assert picks[0].candidate.stock.symbol == "300001"  # 评分高胜出

    def test_top_n_two_returns_two_picks(self):
        candidates = [
            _make_candidate(symbol=f"30000{i}", score=30 - i, category="momentum")
            for i in range(5)
        ]
        picks = pick_top_candidates(candidates, top_n=2)
        assert len(picks) == 2
        # 评分最高的两只
        assert picks[0].candidate.score == 30
        assert picks[1].candidate.score == 29


class TestRiskFiltering:
    """风险标签过滤测试。"""

    def test_blocking_risk_tag_excludes_candidate(self):
        for tag in BLOCKING_RISK_TAGS:
            c = _make_candidate(symbol="300001", score=50, category="momentum",
                                risk_flags=[tag])
            picks = pick_top_candidates([c], top_n=2)
            assert picks == [], f"带 {tag} 标签的票不应入选"

    def test_non_blocking_risk_tag_kept(self):
        """疲劳/弱市/涨幅过大/量价背离 不在阻断列表，可入选。"""
        c = _make_candidate(symbol="300001", score=30, category="momentum",
                            risk_flags=["疲劳", "量价背离"])
        picks = pick_top_candidates([c], top_n=2)
        assert len(picks) == 1

    def test_high_score_with_risk_tag_still_excluded(self):
        """即使评分最高，带阻断标签也剔除。"""
        risky = _make_candidate(symbol="300001", score=50, category="momentum",
                                risk_flags=["主力出货"])
        safe = _make_candidate(symbol="300002", score=20, category="momentum")
        picks = pick_top_candidates([risky, safe], top_n=1)
        assert picks[0].candidate.stock.symbol == "300002"


class TestWeakMarket:
    """弱市环境下仅 momentum 可见。"""

    def test_weak_market_excludes_non_momentum(self):
        market_bonus = -2  # MARKET_ENV_WEAK
        nf = _make_candidate(symbol="300001", score=40, category="new_face",
                             market_env_bonus=market_bonus)
        rb = _make_candidate(symbol="300002", score=40, category="rebound",
                             market_env_bonus=market_bonus)
        st = _make_candidate(symbol="300003", score=40, category="short_term",
                             market_env_bonus=market_bonus)
        mo = _make_candidate(symbol="300004", score=20, category="momentum",
                             market_env_bonus=market_bonus)
        picks = pick_top_candidates([nf, rb, st, mo], top_n=2)
        # 弱市仅 momentum 可见
        assert len(picks) == 1
        assert picks[0].candidate.stock.symbol == "300004"
        assert "弱市仅取动量" in picks[0].reason

    def test_strong_market_all_strategies_visible(self):
        market_bonus = 2  # MARKET_ENV_STRONG
        nf = _make_candidate(symbol="300001", score=40, category="new_face",
                             market_env_bonus=market_bonus)
        mo = _make_candidate(symbol="300002", score=30, category="momentum",
                             market_env_bonus=market_bonus)
        picks = pick_top_candidates([nf, mo], top_n=2)
        assert len(picks) == 2
        # 确定性相同（同参数），按评分降序：new_face 40 > momentum 30
        assert picks[0].candidate.stock.symbol == "300001"
        assert picks[1].candidate.stock.symbol == "300002"


class TestSectorDiversification:
    """第二只避免与第一只同板块。"""

    def test_second_pick_avoids_same_sector(self):
        mo_a = _make_candidate(symbol="300001", score=35, category="momentum",
                               sector="半导体")
        mo_b = _make_candidate(symbol="300002", score=30, category="momentum",
                               sector="半导体")
        nf_c = _make_candidate(symbol="300003", score=25, category="new_face",
                               sector="新能源")
        picks = pick_top_candidates([mo_a, mo_b, nf_c], top_n=2)
        assert len(picks) == 2
        assert picks[0].candidate.stock.symbol == "300001"  # momentum 35 分
        assert picks[1].candidate.stock.symbol == "300003"  # 跳过同板块的 300002
        assert picks[1].candidate.sector == "新能源"

    def test_fallback_when_only_same_sector_available(self):
        """只有同板块候选时，兜底放宽约束补齐。"""
        mo_a = _make_candidate(symbol="300001", score=35, category="momentum",
                               sector="半导体")
        mo_b = _make_candidate(symbol="300002", score=30, category="momentum",
                               sector="半导体")
        picks = pick_top_candidates([mo_a, mo_b], top_n=2)
        # 没有其他板块，兜底选第二只同板块
        assert len(picks) == 2
        assert picks[0].candidate.stock.symbol == "300001"
        assert picks[1].candidate.stock.symbol == "300002"

    def test_first_pick_no_sector_constraint(self):
        """第一只无板块约束，直接取最高分。"""
        c1 = _make_candidate(symbol="300001", score=40, category="momentum",
                             sector="半导体")
        c2 = _make_candidate(symbol="300002", score=30, category="momentum",
                             sector="新能源")
        picks = pick_top_candidates([c1, c2], top_n=2)
        assert picks[0].candidate.stock.symbol == "300001"


class TestDeduplication:
    """双挂候选按 symbol 去重。"""

    def test_dual_listed_candidate_deduplicated(self):
        """同一 symbol 在多个桶出现时只算一次。"""
        # 同一只票在 new_face 和 short_term 都出现（双挂）
        nf = _make_candidate(symbol="300001", score=30, category="new_face")
        st = _make_candidate(symbol="300001", score=28, category="short_term")
        picks = pick_top_candidates([nf, st], top_n=2)
        assert len(picks) == 1  # 去重后只剩一只
        # new_face 优先级高于 short_term，取 new_face 的 30 分
        assert picks[0].candidate.score == 30


class TestReasonBuilder:
    """选择理由生成测试。"""

    def test_reason_includes_category_and_score(self):
        c = _make_candidate(symbol="300001", score=32, category="momentum")
        picks = pick_top_candidates([c], top_n=1)
        assert "momentum 32分" in picks[0].reason

    def test_reason_includes_first_breakout(self):
        c = _make_candidate(symbol="300001", score=32, category="new_face",
                            first_breakout_bonus=8)
        picks = pick_top_candidates([c], top_n=1)
        assert "首次突破" in picks[0].reason

    def test_reason_includes_sector_bonus(self):
        c = _make_candidate(symbol="300001", score=32, category="momentum",
                            sector_bonus=4)
        picks = pick_top_candidates([c], top_n=1)
        assert "板块共振+4" in picks[0].reason

    def test_reason_includes_no_risk_tag_note(self):
        c = _make_candidate(symbol="300001", score=32, category="momentum")
        picks = pick_top_candidates([c], top_n=1)
        assert "无风险标签" in picks[0].reason

    def test_reason_omits_no_risk_tag_when_risky(self):
        """有非阻断风险标签时不写"无风险标签"。"""
        c = _make_candidate(symbol="300001", score=32, category="momentum",
                            risk_flags=["疲劳"])
        picks = pick_top_candidates([c], top_n=1)
        assert "无风险标签" not in picks[0].reason


class TestConvictionSorting:
    """确定性优先排序测试。"""

    def test_returns_pick_result(self):
        """返回 PickResult 对象。"""
        c = _make_candidate(symbol="300001", score=30)
        picks = pick_top_candidates([c], top_n=1)
        assert len(picks) == 1
        assert isinstance(picks[0], PickResult)
        assert picks[0].candidate.stock.symbol == "300001"
        assert isinstance(picks[0].conviction, int)
        assert 1 <= picks[0].conviction <= 5

    def test_higher_sector_bonus_means_higher_conviction(self):
        """有板块共振的候选确定性应更高。"""
        c_no_sec = _make_candidate(symbol="300001", score=30, sector_bonus=0)
        c_with_sec = _make_candidate(symbol="300002", score=30, sector_bonus=6)
        picks = pick_top_candidates([c_no_sec, c_with_sec], top_n=2)
        # 有板块共振的确定性更高，应排第一
        assert picks[0].candidate.stock.symbol == "300002"
        assert picks[0].conviction >= picks[1].conviction

    def test_no_risk_flags_beats_risk_flags(self):
        """无风险标签的候选确定性应高于有风险标签的。"""
        c_safe = _make_candidate(symbol="300001", score=30, risk_flags=[])
        c_risky = _make_candidate(symbol="300002", score=30, risk_flags=["疲劳"])
        picks = pick_top_candidates([c_risky, c_safe], top_n=2)
        assert picks[0].candidate.stock.symbol == "300001"

    def test_signals_not_empty(self):
        """候选应有至少一个驱动信号。"""
        c = _make_candidate(symbol="300001", score=30, sector_bonus=4)
        picks = pick_top_candidates([c], top_n=1)
        assert len(picks[0].signals) > 0

    def test_vs_text_generated_for_two_picks(self):
        """两只候选时应生成 vs 对比文字。"""
        c1 = _make_candidate(symbol="300001", score=35, sector_bonus=6)
        c2 = _make_candidate(symbol="300002", score=28, sector_bonus=0)
        picks = pick_top_candidates([c1, c2], top_n=2)
        assert picks[0].vs_text != ""
        assert picks[1].vs_text != ""
