"""标签登记簿（scanner/label_registry.py）的守卫测试。

四条硬规则见模块 docstring（G1~G4）。本文件另加两条**跨模块**守卫：

  · 反向依赖：评分/排序/准入链路不得 import label_registry —— 它是纯展示元数据，
    一旦被评分模块依赖，「标签科学化」就会悄悄变成「标签影响评分」。
  · 图例可达（**2026-09-28 已反向**）：原先是「每个 E0 spec 的 counter 必须出现在
    终端与飞书两端输出里」；同日用户第二次决策「去掉显示读法」把渲染调用点全部移除，
    故现在两端**都不该**出现「读法：」行 —— 反向断言在 tests/test_display.py。
    本文件继续守护 `legend_line()` 文案本身（仍是单源、仍含披露关键词），
    因为 scripts/label_audit.py 打印它，挂回渲染时文案必须已经是对的。
"""
from __future__ import annotations

import pathlib
import re

import pytest

from scanner import label_registry as lr

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

# 允许 import label_registry 的模块白名单（纯展示层 + 审计脚本 + 测试）。
# 评分/排序/准入链路（ranking / signals / config_scoring / pipeline / orchestrator…）
# 出现在这里即为违规。
# 注：render.py / feishu.py 自 2026-09-28「去掉显示读法」后已不再 import ——
# 白名单是「允许」不是「要求」，留着是为了挂回渲染时不必再改本集合。
_ALLOWED_IMPORTERS = {
    "scanner/view/render.py",
    "scanner/feishu.py",
    "scripts/label_audit.py",
}

# 匹配真正的 import 语句（from scanner.label_registry import / import scanner.label_registry），
# 而非注释里提到 "label_registry" 字样。
_IMPORT_RE = re.compile(r"^\s*(?:from\s+scanner\.label_registry\s+import|import\s+scanner\.label_registry)", re.MULTILINE)


def test_registry_not_empty():
    assert len(lr.LABEL_REGISTRY) >= 5, "登记簿空到没有守卫价值，先补条目"


def test_g1_source_and_grade_are_known():
    for spec in lr.LABEL_REGISTRY.values():
        assert spec.source_code in lr.SOURCES, f"[{spec.id}] 未知出处分级 {spec.source_code!r}"
        assert spec.grade in lr.GRADES, f"[{spec.id}] 未知证据等级 {spec.grade!r}"


def test_g2_s5_implies_e0_and_d_implies_e2():
    """S5（本仓拟合）只能作「未校准提示」；D（系统定义）必须可复现、无经验断言。"""
    for spec in lr.LABEL_REGISTRY.values():
        if spec.source_code == "S5":
            assert spec.grade == "E0", f"[{spec.id}] S5 必须落 E0，实际 {spec.grade}"
        if spec.source_code == "D":
            assert spec.grade == "E2", f"[{spec.id}] D 必须落 E2，实际 {spec.grade}"


def test_g3_e0_counter_must_disclose():
    """E0 的 counter 必须含披露关键词 —— 否则「未校准」只是内部状态，用户看不到。"""
    for spec in lr.LABEL_REGISTRY.values():
        if not spec.unverified:
            continue
        assert any(k in spec.counter for k in lr.DISCLOSURE_KEYWORDS), (
            f"[{spec.id}] E0 的 counter 未含披露关键词：{spec.counter}"
        )


def test_g4_labels_appear_in_declared_surfaces():
    """登记簿与实际渲染不得脱钩：声明的字面量必须真的出现在声明的模块里。"""
    for spec in lr.LABEL_REGISTRY.values():
        for label in spec.labels:
            assert any(lr._label_in_file(label, path) for path in spec.surfaces), (
                f"[{spec.id}] 字面量 {label!r} 未出现在 {spec.surfaces}"
            )


def test_no_scoring_module_imports_registry():
    """反向依赖守卫：评分/排序/准入链路不得依赖展示层元数据。"""
    offenders: list[str] = []
    for py in sorted((REPO_ROOT / "scanner").rglob("*.py")):
        rel = py.relative_to(REPO_ROOT).as_posix()
        if rel in _ALLOWED_IMPORTERS or rel == "scanner/label_registry.py":
            continue
        if _IMPORT_RE.search(py.read_text(encoding="utf-8")):
            offenders.append(rel)
    assert not offenders, f"以下模块 import 了 label_registry（应为纯展示层）：{offenders}"


def test_legend_line_is_single_line_and_nonempty():
    for section in lr.LEGEND_SECTIONS:
        line = lr.legend_line(section)
        assert line, f"区块 {section} 的一行图例为空"
        assert "\n" not in line, f"区块 {section} 的图例必须是一行"
        assert line.startswith("读法："), f"区块 {section} 的图例应以「读法：」开头"


def test_legend_covers_every_e0_spec_with_sections():
    """声明了 sections 的 E0 spec，其 counter 必须真的进了对应区块的图例。"""
    for section in lr.LEGEND_SECTIONS:
        line = lr.legend_line(section)
        for spec in lr.LABEL_REGISTRY.values():
            if spec.unverified and section in spec.sections:
                assert spec.counter in line, f"[{spec.id}] 未校准但未进 {section} 图例"


def test_legend_sections_are_known():
    for spec in lr.LABEL_REGISTRY.values():
        for sec in spec.sections:
            assert sec in lr.LEGEND_SECTIONS, f"[{spec.id}] 未知图例区块 {sec!r}"


def test_audit_reports_no_problems():
    """登记簿自检（scripts/label_audit.py 跑的是同一个函数）。"""
    assert lr.audit() == [], f"登记簿元数据违规：{lr.audit()}"


def test_beauty_mark_literals_match_registry():
    """生产字面量必须与登记簿一致 —— 改字面量时两处同改，否则图例在说别的票。"""
    from scanner.trend_beauty import BEAUTY_MARK, BEAUTY_MARK_STRONG

    spec = lr.LABEL_REGISTRY["trend_steady"]
    assert BEAUTY_MARK in spec.labels
    assert BEAUTY_MARK_STRONG in spec.labels
    assert BEAUTY_MARK == "稳" and BEAUTY_MARK_STRONG == "稳★"


def test_limit_rules_single_source():
    """涨停判定单源：hot_watch 与 limit_rules 必须是同一份实现。"""
    from scanner import hot_watch, limit_rules

    assert hot_watch.limit_pct_for is limit_rules.limit_pct_for
    assert hot_watch.limit_prices is limit_rules.limit_prices
    assert limit_rules.limit_pct_for("300862") == 20.0  # 创业板
    assert limit_rules.limit_pct_for("600519") == 10.0  # 主板
    # 比例制：主板 9.8（沿用原值）、创业板 19.6 —— 写死 9.8 正是被修的那个 bug
    assert limit_rules.near_limit_pct("600519", 0.98) == pytest.approx(9.8)
    assert limit_rules.near_limit_pct("300862", 0.98) == pytest.approx(19.6)
