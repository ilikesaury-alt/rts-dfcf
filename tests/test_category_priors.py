"""类别先验单一事实源守护（2026-09-14，目标函数统一为「次日≥7% hit 率」）。

**为什么需要这组测试**：2026-09-14 静态审查（docs/strategy-flow-review-2026-09-14.md §一）
发现系统里存在**三张手抄的类别先验表且口径互相矛盾**：

  nextday_prob.BASE_RATE_BY_CAT      → hit 率（终选排序用）
  config_scoring.COMPOSITE_CAT_BASE  → hit 率线性映射（但取自更早的快照）
  decision.DECISION_CATEGORY_SPECS   → **平均超额收益**（决策层准入与顺序）

后果：同一类别在系统内既是最好又是最差。core_dip 平均超额 +1.69%（旧表第一优先级）
而 hit 率仅 6.5%（低于基准）；momentum 平均超额 −0.70%（旧表「永禁」，被
`category != "momentum"` 硬编码剔除）而 hit 率 10.0%（高于基准）。系统会把 hit 率高的
票排在前面，再用硬编码整体删掉。

现在规定：**`config_scoring.CATEGORY_HIT_RATE` 是唯一手抄源**，其余两处由它派生。
本文件把这条约束钉死——任何"再长出一份手抄副本"或"派生公式被改歪"的改动都会 fail。

（数值漂移由 tests/test_nextday_calib.py 的代码常数↔快照守护负责，与本文件的
「结构/派生关系」守护互补。）
"""

from __future__ import annotations

import math

import pytest

from scanner import decision as dm
from scanner.categories import MAIN_TABLE_CATEGORIES, SCORE_DESCENDING_BY_CAT
from scanner.config import (
    CATEGORY_HIT_RATE,
    CATEGORY_HIT_RATE_DEFAULT,
    COMPOSITE_CAT_BASE,
)
from scanner.nextday_prob import BASE_RATE_BY_CAT, BASE_RATE_DEFAULT

# ── 1. 唯一手抄源：下游必须派生，不得复制 ──


def test_nextday_prob_aliases_the_single_source():
    """nextday_prob.BASE_RATE_BY_CAT 必须**是** CATEGORY_HIT_RATE 本人（同一对象）。

    若此处变成 `is not`，说明有人又写了一份字面 dict —— 那正是本次要消灭的
    「三张手抄表」问题复发。别名而非 copy 也是刻意的：保证「改一处、全系统一致」。
    """
    assert BASE_RATE_BY_CAT is CATEGORY_HIT_RATE, (
        "BASE_RATE_BY_CAT 必须别名 config_scoring.CATEGORY_HIT_RATE（单一手抄源），不得再复制一份字面表"
    )
    assert BASE_RATE_DEFAULT == CATEGORY_HIT_RATE_DEFAULT


def test_single_source_keys_cover_all_live_categories():
    """单源必须覆盖全部在产类别（含已下线 pullback 供回测），否则下游会静默取兜底值。"""
    expected = {
        "rebound",
        "known_new_face",
        "momentum",
        "new_face",
        "core_dip",
        "short_term",
        "comeback",
        "pool_pick",
        "pullback",
    }
    assert expected <= set(CATEGORY_HIT_RATE)
    assert all(0.0 < v < 1.0 for v in CATEGORY_HIT_RATE.values())
    assert 0.0 < CATEGORY_HIT_RATE_DEFAULT < 1.0


# ── 2. COMPOSITE_CAT_BASE 必须可由单源复算 ──


def test_composite_cat_base_is_derived_from_hit_rate():
    """cat_base = (hit − 基准) / (最高 hit − 基准) × 10，逐类别复算一致。

    这条断言的价值：把「换口径」变成一次可复算的数学关系，而不是又一轮手抄。
    新增类别忘记登记时 keys 断言会 fail。
    """
    spread = max(CATEGORY_HIT_RATE.values()) - CATEGORY_HIT_RATE_DEFAULT
    assert spread > 0
    assert set(COMPOSITE_CAT_BASE) == set(CATEGORY_HIT_RATE), "派生表与单源 key 集合必须一致"
    for cat, rate in CATEGORY_HIT_RATE.items():
        expected = round((rate - CATEGORY_HIT_RATE_DEFAULT) / spread * 10.0, 1)
        assert math.isclose(COMPOSITE_CAT_BASE[cat], expected, abs_tol=0.05), (
            f"{cat}: cat_base={COMPOSITE_CAT_BASE[cat]} 与应用单源复算的 {expected} 不符"
        )


def test_cat_base_sign_follows_hit_rate_vs_baseline():
    """cat_base 符号必须与「hit 率是否高于基准」一致（方向性守护）。

    旧表 core_dip = +0.7 而 hit 率 6.5% < 基准 7.8% —— 符号反了，这是本次审查
    抓到的真实错位之一。符号断言能防它回来。
    """
    for cat, rate in CATEGORY_HIT_RATE.items():
        base = COMPOSITE_CAT_BASE[cat]
        if rate > CATEGORY_HIT_RATE_DEFAULT:
            assert base > 0, f"{cat}: hit {rate:.3f} 高于基准，cat_base 应为正，实为 {base}"
        elif rate < CATEGORY_HIT_RATE_DEFAULT:
            assert base < 0, f"{cat}: hit {rate:.3f} 低于基准，cat_base 应为负，实为 {base}"


# ── 3. 决策层准入/顺序必须由单源派生 ──


def test_decision_gate_is_hit_rate_above_baseline_over_main_table():
    """决策层准入 = 主表类别 ∩ hit 率 > 全体基准（唯一口径，2026-09-14）。"""
    expected = {cat for cat in MAIN_TABLE_CATEGORIES if CATEGORY_HIT_RATE.get(cat, 0.0) > CATEGORY_HIT_RATE_DEFAULT}
    assert frozenset(expected) == dm.DECISION_GATED_CATEGORIES
    assert expected, "准入集合不应为空（基准可能取错）"


def test_decision_caps_keys_match_gate_exactly():
    """配额表的键集合必须与准入集合**逐项相等**。

    配对失效时：多一个键 = 某个已被 hit 率淘汰的类别仍在决策层取数（口径回退）；
    少一个键 = 新达标的类别被静默忽略。两种都该在这里 fail，逼一次显式决策。
    """
    assert set(dm.DECISION_CAT_CAPS) == set(dm.DECISION_GATED_CATEGORIES)


def test_decision_specs_ordered_by_hit_rate_desc():
    """输出顺序 = hit 率降序（配额截断时先验强者优先占位）。"""
    ordered = [cat for cat, _, _ in dm.DECISION_CATEGORY_SPECS]
    rates = [CATEGORY_HIT_RATE[c] for c in ordered]
    assert rates == sorted(rates, reverse=True), f"决策层类别顺序未按 hit 率降序: {ordered}"
    assert set(ordered) == set(dm.DECISION_CAT_CAPS)


def test_decision_direction_from_registry_single_source():
    """分数排序方向必须取自 categories.SCORE_DESCENDING_BY_CAT（不得在 decision 手写）。

    kNF 是分数反指（低分档 hit 更高）→ 升序；其余降序。
    """
    for cat, _cap, direction in dm.DECISION_CATEGORY_SPECS:
        expected = "desc" if SCORE_DESCENDING_BY_CAT.get(cat, True) else "asc"
        assert direction == expected, f"{cat}: 方向 {direction} ≠ 注册表单源 {expected}"


# ── 4. 方向性：momentum 由低于基准升为高于基准这件事必须显式钉住 ──


def test_momentum_is_admitted_because_hit_rate_beats_baseline():
    """★ 回归哨兵：momentum 准入的依据是 hit 率，不是「曾经被永禁」。

    2026-09-14 前 momentum 被平均超额（−0.70%）口径无条件剔除；改为 hit 率口径后
    hit 10.0% > 基准 7.8% → 进入决策层准入。若有人重新按均值口径把它踢出去，
    本断言会 fail —— 届时必须回到口径决策，而不是单方面改代码。
    """
    assert CATEGORY_HIT_RATE["momentum"] > CATEGORY_HIT_RATE_DEFAULT
    assert "momentum" in dm.DECISION_GATED_CATEGORIES
    assert "momentum" in {cat for cat, _, _ in dm.DECISION_CATEGORY_SPECS}


def test_core_dip_excluded_by_gate_not_by_hardcode():
    """core_dip 因 hit 率低于基准而不在准入内（而非被某处硬编码删除）。

    它在终选区与观察区照常展示，只是不参与「替你下单的那 3 只」。
    """
    assert CATEGORY_HIT_RATE["core_dip"] < CATEGORY_HIT_RATE_DEFAULT
    assert "core_dip" not in dm.DECISION_GATED_CATEGORIES


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
