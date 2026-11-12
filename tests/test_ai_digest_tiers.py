"""research 模式「行动分组」输出格式的守卫（2026-11-12 改版）。

## 沿革

- 2026-11-11 首版：四档 `首选 / 次选 / 观察 / 证据不足`，不设数量上限。
- 2026-11-12 改版（用户反馈两条）：
  ① 报告太啰嗦（实测 **1741 字符 / 每只 ~130 字**）；
  ② 「首选/次选」不够友好 —— 读的人还要在脑子里翻译成「那我到底买不买」。

改为**行动导向三分组** `现在能买 / 再等等 / 不要碰`，每组直接对应一个动作。
证据**不删数字**（删了断言变模糊、幻觉巡检也失去可核对锚点），只改表述结构：
先说结论、紧凑排数字、删修饰词与过程话，目标每票 ≤ 60 字。

## 一个必须守住的边界

`BRIEF_INSTRUCTION` 是 analyze / transcribe / research **三处共用**的，而 analyze /
transcribe 的红线恰好**禁止**「排序/取舍」这类话（`guard.COMPARISON_SLOTS`）。
故行动分组的要求不能并进那个常量 —— 否则同一条纪律既是要求又是禁令。
"""

from __future__ import annotations

import pytest

from scanner.ai_digest.agent import (
    RESEARCH_SYSTEM_PROMPT,
    SUMMARY_SYSTEM_PROMPT,
    build_research_prompt,
    build_summary_messages,
)
from scanner.ai_digest.llm import BRIEF_INSTRUCTION, BRIEF_RESEARCH_INSTRUCTION
from scanner.ai_digest.panel import PanelRegion, PanelSnapshot

#: 三个行动组（顺序即输出顺序：先给能做的，再给要等的，最后给别碰的）。
_GROUPS = ("现在能买", "再等等", "不要碰")


@pytest.fixture()
def snap() -> PanelSnapshot:
    return PanelSnapshot(
        date="2026-11-12",
        time="11:29",
        regions=[
            PanelRegion(
                title="v1 池选",
                columns=("#", "代码", "名称"),
                note="",
                rows=[{"symbol": "SZ300001", "name": "甲公司", "marks": ""}],
            )
        ],
    )


def test_research_prompt_defines_action_groups() -> None:
    """调研系统提示词必须**定义**三个行动组，而不是只要求排序。"""
    for group in _GROUPS:
        assert group in RESEARCH_SYSTEM_PROMPT, f"缺行动组定义：{group}"


def test_research_prompt_forbids_ranking_words() -> None:
    """必须明确**禁止**「首选/次选」这类排序词 —— 这是本次改版的核心。"""
    assert "不写「首选/次选」" in RESEARCH_SYSTEM_PROMPT


def test_research_prompt_allows_multiple_in_each_group() -> None:
    """每组都不设数量上限（用户明确要求过：不要硬压成一只）。"""
    assert "没有数量上限" in RESEARCH_SYSTEM_PROMPT
    assert "硬压" in RESEARCH_SYSTEM_PROMPT


def test_research_prompt_requires_brevity() -> None:
    """每票 ≤ 60 字是硬要求（实测原为 ~130 字）。"""
    assert "60 字" in RESEARCH_SYSTEM_PROMPT
    # 必须点名要删的修饰词与过程话 —— 否则模型不知道该删什么
    assert "修饰词" in RESEARCH_SYSTEM_PROMPT
    assert "过程话" in RESEARCH_SYSTEM_PROMPT


def test_summary_prompt_matches_research_groups() -> None:
    """两阶段总结器的分组口径必须与调研阶段一致（否则 `--summarize-always` 会改写）。"""
    for group in _GROUPS:
        assert group in SUMMARY_SYSTEM_PROMPT, f"总结器缺行动组：{group}"
    assert "60 字" in SUMMARY_SYSTEM_PROMPT


def test_summary_prompt_drops_old_tiers() -> None:
    """总结器里不该再有旧的**档位用法**（避免两套口径并存）。

    ⚠ 只查「档位用法」而不是裸词：`证据不足` 在提示词里还有正当用途 ——
    「证据里没有的项目就说『证据不足』」是在讲**怎么处理缺失数据**，
    不是档位名。把两者混为一谈会让测试误报。
    """
    for old in ("首选", "次选"):
        assert old not in SUMMARY_SYSTEM_PROMPT, f"总结器仍残留旧档位词：{old}"
    # 「观察」作为通用动词也可能出现，只在档位语境下不允许
    assert "｜观察｜" not in SUMMARY_SYSTEM_PROMPT


def test_brief_overrides_twelve_line_cap(snap: PanelSnapshot) -> None:
    """`--brief` 的「最多 12 行」必须被 research 段**显式覆盖**。

    否则 12 行装不下「多只现在能买 + 多只再等等 + 不要碰」的分组结论 ——
    这是最隐蔽的失效方式（用户要的多只票被行数上限悄悄挤掉）。
    """
    text = build_research_prompt([], "面板", panel=snap, brief=True)
    assert "最多 12 行" in text  # 通用段仍在
    assert BRIEF_RESEARCH_INSTRUCTION in text
    assert "不适用" in text  # 明确覆盖


def test_brief_overrides_judgment_field(snap: PanelSnapshot) -> None:
    """通用段的 `判断` 字段在 research 里是行动分组，必须说清（否则模型会写「可关注」）。"""
    text = build_research_prompt([], "面板", panel=snap, brief=True)
    assert "行动分组" in text


def test_brief_summary_also_carries_group_rules(snap: PanelSnapshot) -> None:
    """总结器路径同样要带分组要求（`--summarize-always` 走这条）。"""
    msgs = build_summary_messages("候选清单", "证据", brief=True)
    user = msgs[-1]["content"]
    assert BRIEF_RESEARCH_INSTRUCTION in user
    assert "现在能买" in user


def test_non_brief_still_has_groups(snap: PanelSnapshot) -> None:
    """不加 `--brief` 时分组定义来自 system 提示词，依然生效。"""
    text = build_research_prompt([], "面板", panel=snap, brief=False)
    assert BRIEF_RESEARCH_INSTRUCTION not in text  # brief 专属段不出现
    assert "现在能买" in RESEARCH_SYSTEM_PROMPT


def test_shared_brief_has_no_group_language() -> None:
    """`BRIEF_INSTRUCTION`（analyze/transcribe 共用）**不许**出现行动分组词。

    为什么：analyze/transcribe 的红线（`guard.COMPARISON_SLOTS`）禁止「首选/优于/
    排序」这类话 —— 若把分组要求写进共用常量，同一条纪律既要求又禁止。
    """
    for banned in ("首选", "次选", "现在能买", "再等等", "不要碰"):
        assert banned not in BRIEF_INSTRUCTION, f"共用 brief 段不该含「{banned}」"


def test_group_order_specified() -> None:
    """三个组的顺序要写明（先给能做的，再给要等的，最后给别碰的）。"""
    idx = [BRIEF_RESEARCH_INSTRUCTION.find(g) for g in _GROUPS]
    assert all(i >= 0 for i in idx), "三个组都必须出现"
    assert idx == sorted(idx), f"组顺序应为 {_GROUPS}，实际位置 {idx}"
