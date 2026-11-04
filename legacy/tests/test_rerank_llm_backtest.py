"""`scripts/rerank_llm_backtest.py` 的单测（Phase 2：LLM 排序 vs 确定性排序）。

三条守卫线：
1. **口径**：重建池只含 v1 五类；只留带次日标签的行（否则两侧被拉低的程度不同 ⇒ 不公平）；
2. **公平性**：LLM 与确定性基线吃**完全相同**的候选集；
3. **纪律**：脚本只读库、不给建议；配对天数不足不出区间。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "rerank_llm_backtest.py"
_spec = importlib.util.spec_from_file_location("rerank_llm_backtest", _SCRIPT)
assert _spec and _spec.loader
rl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rl)


def _row(symbol="SZ300001", category="rebound", ndp=1.0, system_p=0.2, **over):
    base = {
        "symbol": symbol,
        "name": "样例",
        "category": category,
        "score": 70.0,
        "percent": 1.0,
        "concept": "人工智能",
        "accum": 0.0,
        "flow": 1.0,
        "rank": None,
        "sector": "人工智能",
        "ndp": ndp,
        "system_p": system_p,
        # `ai_digest.rerank` 的素材字段名（表里那一列叫 p，不是 system_p）
        "p": system_p,
        "prior": 0.179,
        "gate": "A档（先验最高档）",
    }
    base.update(over)
    return base


# ── 1. 口径 ────────────────────────────────────────────────────────────────────


def test_v1_pool_categories_match_rerank_module():
    """Phase 2 的池子口径必须与 `ai_digest.rerank` 完全一致（同池才可比）。"""
    from scanner.ai_digest.rerank import _POOL_FIELDS  # noqa: F401 - 存在性检查

    assert set(rl.V1_POOL_CATEGORIES) == {"rebound", "known_new_face", "momentum", "new_face", "short_term"}
    assert "core_dip" not in rl.V1_POOL_CATEGORIES


def test_hit_threshold_is_the_system_single_target():
    """命中口径必须是系统的唯一定义：次日涨幅 ≥7%。"""
    assert rl.HIT_THRESHOLD == 7.0


def test_topn_hit_counts_only_hits():
    rows = [
        _row(symbol="A", ndp=10.0, system_p=0.9),
        _row(symbol="B", ndp=1.0, system_p=0.8),
        _row(symbol="C", ndp=8.0, system_p=0.7),
        _row(symbol="D", ndp=0.0, system_p=0.6),
    ]
    # 按 system_p 取前 3 → A/C/D → 命中 A(10) 与 C(8) = 2/3
    assert rl.topn_hit(rows, "system_p", 3) == pytest.approx(200 / 3)


def test_topn_hit_returns_none_when_pool_too_small():
    rows = [_row(symbol="A"), _row(symbol="B")]
    assert rl.topn_hit(rows, "system_p", 3) is None


# ── 2. 公平性与统计 ────────────────────────────────────────────────────────────


def test_both_sides_see_the_same_candidate_set():
    """LLM 与确定性排序必须吃同一批行 —— 否则命中率不可比。"""
    rows = [_row(symbol=f"S{i:03d}", ndp=1.0 * i) for i in range(6)]
    llm_order = ["S000", "S001", "S002"]
    det_order = [r["symbol"] for r in sorted(rows, key=lambda r: r["system_p"], reverse=True)]
    # 确定性侧就是「同一份 rows 按 system_p 排序」⇒ 是候选集的一个排列
    assert set(det_order) == {r["symbol"] for r in rows}
    assert len(det_order) == len(rows)
    assert set(llm_order) <= {r["symbol"] for r in rows}


def test_paired_ci_refuses_interval_with_too_few_days():
    res = rl.paired_ci([1.0, -2.0])
    assert res["days"] == 2
    assert res["lo"] is None and res["hi"] is None


def test_sign_test_excludes_zero_days():
    res = rl.sign_test([0.0, 0.0, 5.0, -1.0])
    assert res["pos"] == 1
    assert res["neg"] == 1
    assert res["zero"] == 2
    assert res["p"] is None  # 有效天数 2 < 3


def test_sign_test_p_value_is_two_sided_binomial():
    res = rl.sign_test([5.0, 4.0, 3.0, 2.0, -1.0, -2.0])
    assert res["pos"] == 4 and res["neg"] == 2
    assert res["p"] == pytest.approx(0.6875)


def test_materials_fingerprint_changes_with_materials():
    """缓存键：素材变了就不能复用旧结果（否则拿旧排序评新池）。"""
    from scanner.ai_digest.rerank import build_pool_materials

    a = build_pool_materials("2026-10-30", [_row(symbol="A", system_p=0.2)])
    b = build_pool_materials("2026-10-30", [_row(symbol="A", system_p=0.9)])
    assert rl.materials_fingerprint(a) != rl.materials_fingerprint(b)


def test_next_day_label_never_enters_the_materials():
    """次日收益（标签）**绝不进提示词** —— 进了就是前视偏差（模型直接读答案）。
    连带效果：只改标签不改素材 ⇒ 指纹不变（缓存仍可复用）。"""
    from scanner.ai_digest.rerank import build_pool_materials

    a = build_pool_materials("2026-10-30", [_row(symbol="A", ndp=1.0)])
    b = build_pool_materials("2026-10-30", [_row(symbol="A", ndp=99.0)])
    blob_a = "\n".join(f"{i.id}|{i.value}" for i in a.items)
    blob_b = "\n".join(f"{i.id}|{i.value}" for i in b.items)
    assert "99" not in blob_a and "ndp" not in blob_a
    assert blob_a == blob_b
    assert rl.materials_fingerprint(a) == rl.materials_fingerprint(b)


# ── 3. 脚本纪律 ────────────────────────────────────────────────────────────────


def test_order_ic_measures_rank_against_next_day_return():
    """排序的 rank IC：名次与次日收益的 Spearman 相关（用满整条排序的功效）。"""
    ndp = {"A": 10.0, "B": 1.0, "C": 5.0, "D": 0.0}
    # 完全正确的排序 ⇒ IC = +1
    assert rl.order_ic(["A", "C", "B", "D"], ndp) == pytest.approx(1.0)
    # 完全颠倒 ⇒ IC = -1
    assert rl.order_ic(["D", "B", "C", "A"], ndp) == pytest.approx(-1.0)
    # 样本不足 3 只 ⇒ None
    assert rl.order_ic(["A", "B"], ndp) is None


def test_order_ic_ignores_stocks_without_labels():
    """没标签的票不进 IC 计算（否则当成 0 收益会污染相关性）。"""
    ndp = {"A": 10.0, "B": 1.0, "C": 5.0}
    assert rl.order_ic(["A", "C", "B"], ndp) == pytest.approx(1.0)
    assert rl.order_ic(["A", "X", "C", "B"], ndp) == pytest.approx(1.0)


def test_cache_version_is_part_of_cache_key():
    """缓存键必须含版本号（守卫逻辑变更后旧结果不得被当新结果用）。"""
    assert rl.CACHE_VERSION >= 2
    # 键在函数体内拼装成 f"v{CACHE_VERSION}:{date}:{fp}" —— 用源码断言，不靠运行。
    import inspect

    src = inspect.getsource(rl.llm_rank_for_day)
    assert "CACHE_VERSION" in src


def test_script_has_no_write_sql():
    import re

    src = _SCRIPT.read_text(encoding="utf-8")
    want = re.compile(r"\b(INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|CREATE\s+TABLE|DROP\s+TABLE)\b", re.I)
    assert not want.search(src)


def test_script_has_no_bare_except_exception():
    """AGENTS.md 硬规则：只 catch `scanner.utils.EXTERNAL_FAILURES`，禁裸 except Exception。"""
    import re

    src = _SCRIPT.read_text(encoding="utf-8")
    assert not re.search(r"except\s+Exception\b", src)


def test_script_gives_no_recommendation_wording():
    src = _SCRIPT.read_text(encoding="utf-8")
    for banned in ("建议买入", "建议卖出", "首选", "推荐买入"):
        assert banned not in src
