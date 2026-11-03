"""AGENTS.md 维护纪律守卫（2026-11-03）。

AGENTS.md 是**给 agent 读的入口文档**，不是设计文档。历史上它被塞进实现细节、
证据数字、沿革史 ⇒ 越写越长、越写越没人看。本守卫把「不许再堆」变成机器可查的规则：

1. 文件总行数 ≤ 330（现状 305）；
2. 单条 bullet ≤ 20 行（现状最长 19 行，是历史沿革条目，新条目不得更长）；
3. 必须保留「AGENTS.md 维护纪律」小节及其四条规则 —— 删掉纪律等于关掉守卫。

⚠ 上限刻意留了余量而不是卡死在现状：目的是**阻止继续膨胀**，不是逼人删历史条目。
"""

from __future__ import annotations

import re

import pytest

AGENTS_MD = "AGENTS.md"
MAX_TOTAL_LINES = 330
MAX_BULLET_LINES = 20
DISCIPLINE_HEADING = "## AGENTS.md 维护纪律"

_BULLET_RE = re.compile(r"^\s*[-*] ")


def _read() -> str:
    with open(AGENTS_MD, encoding="utf-8") as f:
        return f.read()


def _bullets(lines: list[str]) -> list[list[str]]:
    """按 bullet 切块（续行 = 缩进非空行）。"""
    out: list[list[str]] = []
    cur: list[str] | None = None
    for line in lines:
        if _BULLET_RE.match(line):
            if cur:
                out.append(cur)
            cur = [line]
        elif cur is not None and line.startswith("  ") and line.strip():
            cur.append(line)
        else:
            if cur:
                out.append(cur)
            cur = None
    if cur:
        out.append(cur)
    return out


def test_agents_md_exists_and_is_not_empty():
    assert _read().strip(), "AGENTS.md 不能为空"


def test_agents_md_total_lines_bounded():
    lines = _read().splitlines()
    assert len(lines) <= MAX_TOTAL_LINES, f"AGENTS.md 已 {len(lines)} 行（上限 {MAX_TOTAL_LINES}）—— 别往这儿堆细节"


def test_agents_md_bullets_not_too_long():
    for block in _bullets(_read().splitlines()):
        assert len(block) <= MAX_BULLET_LINES, (
            f"AGENTS.md 单条 bullet {len(block)} 行（上限 {MAX_BULLET_LINES}）: {block[0][:60]}"
        )


def test_discipline_section_present():
    text = _read()
    assert DISCIPLINE_HEADING in text, "AGENTS.md 必须保留「维护纪律」小节"


@pytest.mark.parametrize(
    "keyword",
    ["默认不改本文件", "只有三类东西配进本文件", "行", "自问"],
)
def test_discipline_section_states_the_rules(keyword: str):
    text = _read()
    assert DISCIPLINE_HEADING in text
    section = text.split(DISCIPLINE_HEADING, 1)[1].split("\n## ", 1)[0]
    assert keyword in section, f"纪律小节缺少要点: {keyword}"
