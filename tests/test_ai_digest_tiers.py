"""research 模式「四档分档」的守卫（2026-11-11）。

## 背景

用户要求：「首选、次选不限制只有一个，可以多个」。

排查发现**根本问题不是数量限制，而是「首选/次选」这两个档位在 research 提示词里
根本没有定义** —— 它只出现在 CLI 帮助文案（"出首选/次选"）和 transcribe 的红线词表
（`guard.COMPARISON_SLOTS`，那里是**禁止**排序）里。于是模型自己发明了「可关注」
「谨慎追高」这类标签，而且往往只给 1~2 只。

本次加明确的四档定义（首选 / 次选 / 观察 / 证据不足），且**首选与次选都不设数量上限**。

## 一个必须守住的边界

`BRIEF_INSTRUCTION` 是 analyze / transcribe / research **三处共用**的，而 analyze /
transcribe 的红线恰好**禁止**「首选/优于/排序」这类话。故分档要求不能并进那个常量
—— 否则同一条纪律既是要求又是禁令。这里锁住「research 专用段独立存在」。
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


@pytest.fixture()
def snap() -> PanelSnapshot:
    return PanelSnapshot(
        date="2026-11-11",
        time="14:59",
        regions=[
            PanelRegion(
                title="v1 池选",
                columns=("#", "代码", "名称"),
                note="",
                rows=[{"symbol": "SZ300001", "name": "甲公司", "marks": ""}],
            )
        ],
    )


def test_research_prompt_defines_four_tiers() -> None:
    """调研系统提示词必须**定义**四档，而不是只说「给排序」。"""
    for tier in ("首选", "次选", "观察", "证据不足"):
        assert tier in RESEARCH_SYSTEM_PROMPT, f"缺档位定义：{tier}"


def test_research_prompt_allows_multiple_primary() -> None:
    """必须明说首选没有数量上限 —— 这是用户的核心要求。"""
    assert "没有数量上限" in RESEARCH_SYSTEM_PROMPT
    # 且要挡住「为了显得严谨硬压成一只」这个实际发生过的行为
    assert "硬压" in RESEARCH_SYSTEM_PROMPT


def test_summary_prompt_matches_research_tiers() -> None:
    """两阶段总结器的档位口径必须与调研阶段一致（否则总结器会改写档位）。"""
    for tier in ("首选", "次选", "观察", "证据不足"):
        assert tier in SUMMARY_SYSTEM_PROMPT, f"总结器缺档位：{tier}"
    assert "没有数量上限" in SUMMARY_SYSTEM_PROMPT


def test_brief_overrides_twelve_line_cap(snap: PanelSnapshot) -> None:
    """`--brief` 的「最多 12 行」必须被 research 段**显式覆盖**。

    否则 12 行装不下「多只首选 + 多只次选 + 观察 + 证据不足」的分档结论，
    用户要的多首选会被行数上限挤掉 —— 这是最隐蔽的失效方式。
    """
    text = build_research_prompt([], "面板", panel=snap, brief=True)
    assert "最多 12 行" in text  # 通用段仍在
    assert BRIEF_RESEARCH_INSTRUCTION in text
    assert "不适用" in text  # 明确覆盖
    assert "20 行" in text  # 给了新的上限


def test_brief_summary_also_carries_tier_rules(snap: PanelSnapshot) -> None:
    """总结器路径同样要带分档要求（`--summarize-always` 走这条）。"""
    msgs = build_summary_messages("候选清单", "证据", brief=True)
    user = msgs[-1]["content"]
    assert BRIEF_RESEARCH_INSTRUCTION in user
    assert "首选" in user


def test_non_brief_still_has_tiers(snap: PanelSnapshot) -> None:
    """不加 `--brief` 时档位定义来自 system 提示词，依然生效。"""
    text = build_research_prompt([], "面板", panel=snap, brief=False)
    assert BRIEF_RESEARCH_INSTRUCTION not in text  # brief 专属段不出现
    assert "首选" in RESEARCH_SYSTEM_PROMPT  # 但 system 里有


def test_shared_brief_has_no_tier_language() -> None:
    """`BRIEF_INSTRUCTION`（analyze/transcribe 共用）**不许**出现档位词。

    为什么：analyze/transcribe 的红线（`guard.COMPARISON_SLOTS`）禁止「首选/优于/
    排序」这类话 —— 若把档位要求写进共用常量，同一条纪律既要求又禁止，模型会精神分裂。
    """
    for banned in ("首选", "次选", "档位"):
        assert banned not in BRIEF_INSTRUCTION, f"共用 brief 段不该含「{banned}」"


def test_tier_order_specified() -> None:
    """档位顺序要写明（模型按这个顺序排版，用户按这个顺序读）。"""
    assert "首选 → 次选 → 观察 → 证据不足" in BRIEF_RESEARCH_INSTRUCTION
