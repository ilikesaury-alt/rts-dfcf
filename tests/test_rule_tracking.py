"""规则追踪与标签单源测试。

配套：`docs/rule-baseline-2026-09-11.md`、`scripts/rule_change_tracker.py`。

⚠️ 冻结已于 2026-09-11 12:20 解除。因此本测试**不再断言"规则不得变动"**——
那样会在每次合法改动后误报。改为断言工具本身可用、且基线自洽。

真正要守住的是 `TestSellTagsSingleSource`：标签字面量必须单源，
这条与冻结无关，是长期约束。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "rule_change_tracker.py"
DOC = ROOT / "docs" / "rule-baseline-2026-09-11.md"


def _run_tracker(*extra: str) -> subprocess.CompletedProcess[str]:
    # 参数为仓库内固定路径 + sys.executable，无外部输入。
    return subprocess.run(  # noqa: S603
        [sys.executable, str(SCRIPT), *extra],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(ROOT),
        check=False,
    )


class TestRuleTracker:
    """变更追踪器可用性（解冻后为只读报告工具，从不阻断）。"""

    def test_script_exists(self):
        assert SCRIPT.exists(), f"规则追踪器缺失：{SCRIPT}"

    def test_doc_exists(self):
        assert DOC.exists(), f"规则基线文档缺失：{DOC}"

    def test_never_blocks(self):
        """工具必须始终返回 0——解冻后规则变更不应阻断任何流程。"""
        proc = _run_tracker()
        assert proc.returncode == 0, (
            "规则追踪器不应阻断（解冻后规则改动是合法的）。\n"
            f"--- 输出 ---\n{proc.stdout}\n{proc.stderr}"
        )

    def test_self_consistent(self):
        """脚本内 BASELINE 与文档第 1 节表格须一致（防「文档改了脚本没改」）。"""
        proc = _run_tracker()
        assert "基线自检告警" not in proc.stdout, (
            "追踪器的 BASELINE 常量与 docs/rule-baseline-2026-09-11.md 不一致。\n"
            f"--- 输出 ---\n{proc.stdout}"
        )

    def test_since_mode_works(self):
        """--since 必须能成功对比历史版本（只读）。"""
        proc = _run_tracker("--since", "HEAD")
        assert proc.returncode == 0
        assert "规则变更报告" in proc.stdout


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
