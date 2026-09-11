"""规则冻结回归测试（2026-09-11）。

配套：`docs/rule-freeze-2026-09-11.md`、`scripts/check_rule_freeze.py`。

冻结期内（2026-09-11 ~ 约 2026-10-16）规则文件不应变动。本测试把校验器
接入 pytest，使 `pytest tests/` 即可发现规则漂移，无需额外记得跑脚本。

⚠️ 若某条断言失败，**不要**直接改测试期望值。正确处理顺序：
  1. 确认这次改动是否真的必要（冻结期内应尽量不改规则）；
  2. 若确需改，到 `docs/rule-freeze-log.md` 登记原因；
  3. 再更新 `docs/rule-freeze-2026-09-11.md` 基线与脚本 BASELINE。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "check_rule_freeze.py"


def _run_checker() -> subprocess.CompletedProcess[str]:
    # 参数为仓库内固定路径 + sys.executable，无外部输入。
    return subprocess.run(  # noqa: S603
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(ROOT),
        check=False,
    )


class TestRuleFreeze:
    """冻结期内规则不得漂移。"""

    def test_checker_script_exists(self):
        assert SCRIPT.exists(), f"冻结校验器缺失：{SCRIPT}"

    def test_no_rule_drift(self):
        """文件指纹 / 关键阈值 / 类别注册表三层校验必须全绿。"""
        proc = _run_checker()
        assert proc.returncode == 0, (
            "检测到规则冻结破坏——冻结期内规则文件不应变动。\n"
            "若确需改动，请到 docs/rule-freeze-log.md 登记后更新基线。\n"
            f"--- 校验器输出 ---\n{proc.stdout}\n{proc.stderr}"
        )

    def test_checker_baseline_self_consistent(self):
        """校验器内 BASELINE 与文档第 1 节表格必须一致（防「文档改了脚本没改」）。"""
        proc = _run_checker()
        assert "基线自检告警" not in proc.stdout, (
            "校验器的 BASELINE 常量与 docs/rule-freeze-2026-09-11.md 不一致。\n"
            f"--- 校验器输出 ---\n{proc.stdout}"
        )


class TestSellTagsSingleSource:
    """标签单源约束（2026-09-11 收敛）。"""

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
