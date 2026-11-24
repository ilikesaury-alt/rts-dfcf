"""横截面异动区**不进入 AI 分析**的反向守卫（2026-11-24 用户决策）。

历史：该区曾作为第三个并列区并入 research 提示词与素材管线。现要求 AI 分析
只看 `v1 池选` + `沪深飙升` 三段；终端/面板/看板仍照常展示该区，只是不喂给模型。

守护三条：
1. AI 读面板入口 `load_panel_md()` 必须剔除 `≡` 区（纯函数解析能力保留）。
2. `build_research_prompt` / `build_messages` 不再有横截面专用说明与提示词。
3. CLI 不再接受 `--source crosssection|all` 与 `--exclude`。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scanner.ai_digest.panel import PanelRegion, PanelSnapshot


def _snap() -> PanelSnapshot:
    return PanelSnapshot(
        date="2026-11-24",
        time="09:45",
        regions=(
            PanelRegion(title="v1 池选 — 新票优先", columns=("代码",), rows=({"symbol": "SZ300001"},)),
            PanelRegion(title="沪深飙升 · 极有可能大涨", columns=("代码",), rows=({"symbol": "SZ300002"},)),
            PanelRegion(title="横截面异动 · 09:45", columns=("代码",), rows=({"symbol": "SZ300003"},)),
        ),
    )


def test_load_panel_md_drops_crosssection(monkeypatch, tmp_path: Path) -> None:
    """唯一 AI 读入口必须剔除 ≡ 区，否则提示词/素材/特征预取都会带上它。"""
    import scanner.ai_digest.panel as panel_mod

    monkeypatch.setattr(panel_mod, "parse_panel_md", lambda path: _snap())
    got, _ = panel_mod.load_panel_md()
    assert got is not None
    assert [r.title for r in got.regions] == ["v1 池选 — 新票优先", "沪深飙升 · 极有可能大涨"]
    assert got.total == 2


def test_research_prompt_has_no_crosssection_wording(monkeypatch) -> None:
    """过滤后的面板进提示词，不得再出现「横截面异动区怎么读」这类专属说明。"""
    import scanner.ai_digest.panel as panel_mod
    from scanner.ai_digest.agent import build_research_prompt

    monkeypatch.setattr(panel_mod, "parse_panel_md", lambda path: _snap())
    panel, _ = panel_mod.load_panel_md()
    text = build_research_prompt([], "面板", panel=panel)
    assert "横截面" not in text


def test_llm_has_no_crosssection_system_prompt() -> None:
    """横截面专用 system prompt 与 cs. 前缀识别均已移除。"""
    from scanner.ai_digest import llm
    from scanner.ai_digest.material import OffboardMaterials

    assert not hasattr(llm, "SYSTEM_PROMPT_CROSSSECTION")
    assert not hasattr(llm, "SYSTEM_PROMPT_MERGED")
    mats = OffboardMaterials(date="2026-11-24", items=(), missing=(), symbols=())
    assert llm.build_messages(mats, "analyze")[0]["content"] == llm.SYSTEM_PROMPT_ANALYZE


def test_cli_rejects_crosssection_switches() -> None:
    import scanner.ai_digest.__main__ as cli

    for argv in (
        ["--source", "crosssection"],
        ["--source", "all"],
        ["--exclude", "crosssection"],
    ):
        with pytest.raises(SystemExit):
            cli._parse_args(argv)
