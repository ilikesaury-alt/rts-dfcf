"""标签单源约束测试。

2026-11-04：原 `TestRuleTracker` 守的 `scripts/rule_change_tracker.py`
（退出码恒 0、只报不改的规则变更追踪器）已随离线验证子系统归档到
`legacy/scripts/`（见 legacy/README.md），对应断言一并移除。

保留 `TestSellTagsSingleSource`：标签字面量必须单源，
这条与冻结/归档无关，是长期约束。
"""

from __future__ import annotations


class TestSellTagsSingleSource:
    """标签单源约束（2026-09-11 收敛）——与冻结无关的长期约束。"""

    def test_config_has_sell_tags(self):
        from scanner.config import TACTICS_SELL_TAGS

        assert set(TACTICS_SELL_TAGS) == {"⬇减仓", "⬇减半", "🔻勿接", "💰落袋"}

    def test_sell_tags_immutable(self):
        """frozenset 防运行期误改。"""
        from scanner.config import TACTICS_SELL_TAGS

        assert isinstance(TACTICS_SELL_TAGS, frozenset)

    def test_add_tag_not_in_sell_set(self):
        """⬆加仓 是买点信号，不得混入卖出集合（方向相反）。"""
        from scanner.config import TACTICS_SELL_TAGS, TACTICS_TAG_ADD

        assert TACTICS_TAG_ADD not in TACTICS_SELL_TAGS
