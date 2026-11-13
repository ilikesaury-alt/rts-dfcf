"""AI 分析报告归档（人工复盘）的守卫，2026-11-13。

守的是三条边界：① 每次出报告自动留一份、同秒不覆盖；② 档案不含次日绩效、
不回灌下一次分析（前视 / 第二结论源）；③ 幻觉巡检未通过注记置首行、写盘失败
不影响报告与退出码。顺带守 `--out` 不带 `--brief` 的历史 NameError。
"""

from __future__ import annotations

import dataclasses
from datetime import datetime
from pathlib import Path

import pytest

from scanner.ai_digest.archive import (
    DISCLAIMER,
    ArchivePayload,
    render_archive,
    write_archive,
)

_AT = datetime(2026, 11, 13, 14, 55, 3)


def _payload(**kw: object) -> ArchivePayload:
    base: dict[str, object] = {
        "title": "榜外异动 · AI 综合分析",
        "label": "AI 综合分析 · 含模型主观判断 · 非系统结论，不构成投资建议",
        "mode": "analyze",
        "source": "log",
        "source_desc": "offboard_launch_log 2026-11-02（整日过门账）",
        "model": "m-test",
        "total": 17,
        "shown": 12,
        "text": "整体观察：量价配合一般。",
        "generated_at": _AT,
    }
    base.update(kw)
    return ArchivePayload(**base)  # type: ignore[arg-type]


# ── 1. 结构：档案不能带「事后结果」字段 ────────────────────────────────────────


def test_payload_has_no_outcome_fields():
    """档案只记「当时说了什么」；出现 next_day_pct / 收益之类字段就是前视入口。"""
    names = [f.name for f in dataclasses.fields(ArchivePayload)]
    assert not [n for n in names if any(k in n for k in ("next_day", "pct", "return", "perf", "outcome"))]
    assert "generated_at" in names and "unverified" in names


# ── 2. 渲染：元数据 / 首行告警 / 免责声明 ──────────────────────────────────────


def test_render_archive_has_metadata_and_disclaimer():
    text = render_archive(_payload())
    assert "归档时刻：2026-11-13 14:55:03（北京时间）" in text
    assert "模式：analyze" in text and "模型：m-test" in text
    assert "共 17 只 / 本次 12 只" in text
    assert "整体观察：量价配合一般。" in text
    assert "幻觉巡检：通过" in text and "红线：未触发" in text
    assert text.rstrip().endswith(DISCLAIMER)
    assert "不回灌下一次分析" in text


def test_render_archive_puts_unverified_note_on_first_line():
    text = render_archive(_payload(unverified=("18.5", "9.99"), redlined=True))
    first = text.splitlines()[0]
    assert first.startswith("[!] 幻觉巡检未通过")
    assert "18.5" in first and "9.99" in first
    assert "幻觉巡检：未通过" in text and "已触发" in text


# ── 3. 写盘：按日分目录、同秒不覆盖、失败 fail-open ──────────────────────────


def test_write_archive_dated_dir_and_no_overwrite(tmp_path: Path):
    p1 = write_archive(_payload(), root=tmp_path)
    p2 = write_archive(_payload(), root=tmp_path)
    assert p1 is not None and p2 is not None and p1 != p2
    assert p1.parent == tmp_path / "2026-11-13"
    assert p1.name == "145503_analyze_log.md"
    assert p2.name == "145503_analyze_log_2.md"
    assert p1.read_text(encoding="utf-8") == render_archive(_payload())
    assert p2.read_text(encoding="utf-8") == p1.read_text(encoding="utf-8")


def test_write_archive_fail_open_on_unwritable_root(tmp_path: Path):
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x", encoding="utf-8")
    assert write_archive(_payload(), root=blocker) is None


# ── 4. CLI 集成：每次出报告自动留档 ──────────────────────────────────────────


def _fake_info(cli: object) -> dict[str, object]:
    return {
        "date": "2026-11-02",
        "rows": [dict(r) for r in cli.DEMO_ROWS],  # type: ignore[attr-defined]
        "source": "log",
        "time": "",
        "market": cli.DEMO_MARKET,  # type: ignore[attr-defined]
        "baseline": "",
    }


def _stub_cli(monkeypatch: pytest.MonkeyPatch, cli: object, llm: object, text: str = "整体观察：量价配合一般。") -> None:
    monkeypatch.setattr(cli, "_load_env", lambda: None)  # type: ignore[attr-defined]
    monkeypatch.setenv("MAGPIE_BASE_URL", "https://magpie.test/v1")
    monkeypatch.setenv("MAGPIE_API_KEY", "k-test")
    monkeypatch.setenv("MAGPIE_MODEL", "m-test")
    monkeypatch.setattr(cli, "_resolve_rows", lambda args: _fake_info(cli))  # type: ignore[attr-defined]
    monkeypatch.setattr(
        llm,  # type: ignore[attr-defined]
        "transcribe",
        lambda cfg, mats, mode="analyze", timeout=None, brief=False: text,
    )


def test_main_archives_one_report_per_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import scanner.ai_digest.__main__ as cli
    import scanner.ai_digest.llm as llm

    _stub_cli(monkeypatch, cli, llm)
    code = cli.main(["--mode", "analyze", "--archive-dir", str(tmp_path)])
    files = sorted(tmp_path.rglob("*.md"))
    assert code == 0
    assert len(files) == 1
    body = files[0].read_text(encoding="utf-8")
    assert "榜外异动 · AI 综合分析" in body
    assert "整体观察：量价配合一般。" in body
    assert "不回灌下一次分析" in body
    assert "2026-11-02" in body  # 信号日来自数据源，不是归档日
    # 再跑一次：累计两份，绝不覆盖
    assert cli.main(["--mode", "analyze", "--archive-dir", str(tmp_path)]) == 0
    assert len(sorted(tmp_path.rglob("*.md"))) == 2


def test_main_no_archive_demo_and_materials_write_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import scanner.ai_digest.__main__ as cli
    import scanner.ai_digest.llm as llm

    _stub_cli(monkeypatch, cli, llm)
    assert cli.main(["--mode", "analyze", "--no-archive", "--archive-dir", str(tmp_path)]) == 0
    assert list(tmp_path.rglob("*.md")) == []
    # --demo 是自检：`_resolve_rows` 被桩替换了也要按开关跳过，不能污染归档。
    assert cli.main(["--demo", "--mode", "analyze", "--archive-dir", str(tmp_path)]) == 0
    assert list(tmp_path.rglob("*.md")) == []
    assert cli.main(["--materials", "--mode", "analyze", "--archive-dir", str(tmp_path)]) == 0
    assert list(tmp_path.rglob("*.md")) == []


def test_main_out_without_brief_no_longer_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """回归：长报告的 --out 曾引用只在 brief 分支定义的 `report` ⇒ NameError。"""
    import scanner.ai_digest.__main__ as cli
    import scanner.ai_digest.llm as llm

    _stub_cli(monkeypatch, cli, llm)
    out = tmp_path / "report.txt"
    code = cli.main(["--mode", "analyze", "--no-archive", "--out", str(out)])
    assert code == 0
    body = out.read_text(encoding="utf-8")
    assert "整体观察：量价配合一般。" in body


def test_pick_passes_archive_dir_value_through():
    """`--archive-dir` 是带值标志；漏进 `_VALUE_FLAGS` 会把路径当成股票代码。"""
    import pick

    assert pick._build_argv(["--clipboard", "--archive-dir", "D:/复盘"]) == [
        "--clipboard",
        "--archive-dir",
        "D:/复盘",
    ]
    assert pick._build_argv(["300319", "--no-archive"]) == ["--symbols", "300319", "--no-archive"]
    assert pick._build_argv(["--archive-dir=D:/复盘"]) == ["--archive-dir=D:/复盘"]


def test_main_json_output_stays_clean_and_still_archives(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    import json

    import scanner.ai_digest.__main__ as cli
    import scanner.ai_digest.llm as llm

    _stub_cli(monkeypatch, cli, llm)
    code = cli.main(["--mode", "analyze", "--json", "--archive-dir", str(tmp_path)])
    assert code == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True  # [归档] 走 stderr，不污染 JSON
    assert len(list(tmp_path.rglob("*.md"))) == 1
