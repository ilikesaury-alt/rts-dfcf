"""tests for scanner/panel_md.py — 面板逐轮存档（整份覆盖·只留最近一次·fail-open）。"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from scanner import config, panel_md

_PANEL_1 = "◆ v1 池选\n  1  SZ300182  捷成股份  +1.85%  MOM·10.0%"
_PANEL_2 = "◆ v1 池选\n  2  SZ300770  新媒股份  +3.92%  MOM·10.0%"


@pytest.fixture(autouse=True)
def _default_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(config, "LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(config, "PANEL_MD_ENABLED", True)
    monkeypatch.setattr(config, "PANEL_MD_PATH", "")


@pytest.fixture
def fixed_clock(monkeypatch: pytest.MonkeyPatch):
    """冻结北京时间；返回可推进时间的 setter。"""
    from scanner.config import BEIJING_TZ

    current = [datetime(2026, 11, 6, 11, 37, 5, tzinfo=BEIJING_TZ)]
    monkeypatch.setattr(panel_md, "now_beijing", lambda: current[0])

    def advance(**kwargs):
        current[0] = current[0] + timedelta(**kwargs)

    return advance


def _md_path() -> Path:
    return panel_md.panel_md_path()


def _snapshots(text: str) -> list[str]:
    return re.findall(r"^## 快照 (\d{4}-\d{2}-\d{2} \d{2}:\d{2})$", text, re.MULTILINE)


def test_default_path_is_股票池_md(tmp_path: Path):
    assert _md_path() == tmp_path / "logs" / "panel_md" / "股票池.md"


def test_first_round_writes_reference_style_snapshot(fixed_clock):
    p = panel_md.save_panel_round(_PANEL_1)
    assert p == str(_md_path())
    text = _md_path().read_text(encoding="utf-8")
    assert text.startswith("# 股票池 / 盘面榜单\n")
    assert "数据来源：系统榜单输出（v1 池选 / 沪深飙升三段）" in text
    assert "当前最新快照：2026-11-06 11:37" in text
    assert "\n---\n" in text
    assert _snapshots(text) == ["2026-11-06 11:37"]
    assert f"```text\n{_PANEL_1}\n```\n" in text


def test_next_round_overwrites_and_keeps_only_latest(fixed_clock):
    panel_md.save_panel_round(_PANEL_1)
    first_size = _md_path().stat().st_size
    fixed_clock(minutes=1)
    assert panel_md.save_panel_round(_PANEL_2) == str(_md_path())
    text = _md_path().read_text(encoding="utf-8")
    # 只有一个快照、且是第二轮的时间与正文。
    assert _snapshots(text) == ["2026-11-06 11:38"]
    assert "当前最新快照：2026-11-06 11:38" in text
    assert len(re.findall(r"^当前最新快照：", text, re.MULTILINE)) == 1
    assert _PANEL_2 in text
    assert "捷成股份" not in text  # 上一轮正文被覆盖掉
    assert "## 快照 2026-11-06 11:37" not in text
    # 文件不会随轮数增长（只有一个 code block）。
    assert text.count("```text") == 1
    assert _md_path().stat().st_size <= first_size + 20


def test_overwrites_preexisting_file_including_its_header(fixed_clock, tmp_path, monkeypatch):
    external = tmp_path / "workspace" / "股票池.md"
    external.parent.mkdir()
    external.write_text(
        "# 人工标题\n\n> 人工说明\n\n## 快照 2026-11-06 11:30\n\n```text\n旧面板\n```\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "PANEL_MD_PATH", str(external))
    panel_md.save_panel_round(_PANEL_1)
    text = external.read_text(encoding="utf-8")
    assert text.startswith("# 股票池 / 盘面榜单\n")
    assert "人工说明" not in text and "旧面板" not in text
    assert _snapshots(text) == ["2026-11-06 11:37"]
    assert "捷成股份" in text
    assert text.count("# 股票池 / 盘面榜单") == 1
    assert not (tmp_path / "workspace" / "股票池.md.tmp").exists()  # tmp 已原子替换掉


def test_strips_ansi_and_carriage_returns(fixed_clock):
    panel_md.save_panel_round("\x1b[91m红字\x1b[0m  \r进度行")
    text = _md_path().read_text(encoding="utf-8")
    assert "\x1b" not in text and "\r" not in text
    assert "红字" in text


def test_empty_text_skips_without_touching_existing_file(fixed_clock):
    panel_md.save_panel_round(_PANEL_1)
    before = _md_path().read_text(encoding="utf-8")
    assert panel_md.save_panel_round("") is None
    assert panel_md.save_panel_round("   \n  ") is None
    assert _md_path().read_text(encoding="utf-8") == before  # 空本轮不覆盖掉上一份


def test_disabled_flag_skips(fixed_clock, monkeypatch):
    monkeypatch.setattr(config, "PANEL_MD_ENABLED", False)
    assert panel_md.save_panel_round(_PANEL_1) is None
    assert not _md_path().exists()


def test_default_text_comes_from_latest_panel(fixed_clock, monkeypatch):
    monkeypatch.setattr(panel_md, "latest_panel_text", lambda: "最新一轮面板")
    panel_md.save_panel_round()
    assert "最新一轮面板" in _md_path().read_text(encoding="utf-8")


def test_oserror_fail_open(tmp_path: Path, monkeypatch, fixed_clock):
    # 目标目录被同名文件占住 ⇒ mkdir 抛 FileExistsError(OSError) ⇒ 静默返回 None。
    blocker = tmp_path / "logs" / "panel_md"
    blocker.parent.mkdir(parents=True)
    blocker.write_text("", encoding="utf-8")
    assert panel_md.save_panel_round(_PANEL_1) is None
