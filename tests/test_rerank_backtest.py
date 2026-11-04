"""`scripts/rerank_backtest.py` 的单测（池内排序依据的样本外检验）。

三条守卫线：
1. **口径**：先验只用 train 窗标签重算（walkforward），样本只含 v1 池选五类；
2. **方法学**：零值日不进符号检验（否则主依据被系统性判负）；类内 IC 能剥掉类别构成效应；
3. **诚实性**：配对天数 < 3 时**不出区间**（n=1 的 CI 是宽度 0 的假显著）。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "rerank_backtest.py"
_spec = importlib.util.spec_from_file_location("rerank_backtest", _SCRIPT)
assert _spec and _spec.loader
rb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rb)


def _row(date="2026-10-01", symbol="SZ300001", category="rebound", ndp=1.0, **over):
    base = {
        "date": date,
        "symbol": symbol,
        "category": category,
        "ndp": ndp,
        "system_p": 0.2,
        "score": 70.0,
        "flow": 1.0,
        "prior": None,
    }
    base.update(over)
    return base


# ── 1. 口径 ────────────────────────────────────────────────────────────────────


def test_v1_pool_categories_exclude_core_dip():
    """rerank 排的是 v1 池选（不含 core_dip）⇒ 回放样本必须同池，否则口径错配。"""
    assert "core_dip" not in rb.V1_POOL_CATEGORIES
    assert "pool_pick" not in rb.V1_POOL_CATEGORIES
    assert "pullback" not in rb.V1_POOL_CATEGORIES
    assert set(rb.V1_POOL_CATEGORIES) == {"rebound", "known_new_face", "momentum", "new_face", "short_term"}


def test_train_prior_uses_only_train_rows():
    """先验必须只用 train 窗的标签算 —— 这是 walkforward 样本外的关键。"""
    # n ≥ 10（低于此数回退到全局基线，不拿 n=2 的读数当先验）
    train = [_row(category="rebound", ndp=10.0) for _ in range(5)]
    train += [_row(symbol=f"SZ30000{i}", category="rebound", ndp=0.0) for i in range(5)]
    prior = rb.train_prior(train)
    assert prior["rebound"] == pytest.approx(0.5)


def test_train_prior_small_category_falls_back_to_baseline():
    train = [_row(category="rebound", ndp=10.0)] + [_row(symbol="SZ300009", category="new_face", ndp=0.0)]
    prior = rb.train_prior(train)
    assert prior["new_face"] == rb.CATEGORY_HIT_RATE_DEFAULT


def test_train_prior_ignores_rows_outside_given_list():
    """只统计传入的 train 行：多传一行测试标签就会改变先验 ⇒ 泄漏。"""
    base = [_row(category="rebound", ndp=10.0) for _ in range(6)]
    base += [_row(symbol=f"SZ30001{i}", category="rebound", ndp=0.0) for i in range(4)]
    p1 = rb.train_prior(base)["rebound"]
    with_extra = base + [_row(symbol="SZ300999", category="rebound", ndp=0.0)]
    p2 = rb.train_prior(with_extra)["rebound"]
    assert p1 != p2  # 传入的标签确实参与计算（函数是纯的，没有隐式全局）


# ── 2. 方法学 ──────────────────────────────────────────────────────────────────


def test_sign_test_excludes_zero_days():
    """零差日是「无信息」不是「负」—— 算进负侧会让主依据被系统性判负。"""
    res = rb.sign_test([0.0, 0.0, 0.0, 5.0, -3.0])
    assert res["pos"] == 1
    assert res["neg"] == 1
    assert res["zero"] == 3
    # 分母只算非零（1+1=2 < MIN_PAIRED_DAYS）⇒ 不做检验
    assert res["p"] is None


def test_sign_test_gives_p_for_enough_nonzero_days():
    res = rb.sign_test([5.0, 4.0, 3.0, 2.0, -1.0, -2.0, 0.0, 0.0])
    assert res["pos"] == 4
    assert res["neg"] == 2
    assert res["zero"] == 2
    # 双侧二项检验：pos=4/neg=6 → p = 2·P(X≤2|n=6) = 2·22/64 = 0.6875
    assert res["p"] == pytest.approx(0.6875)


def test_paired_ci_refuses_interval_when_too_few_days():
    """配对天数 < 3 ⇒ 不出区间（n=1 的 CI 退化成宽度 0 的假显著）。"""
    res = rb.paired_ci([3.0, -1.0])
    assert res["days"] == 2
    assert res["lo"] is None and res["hi"] is None
    assert "不出区间" in res["note"]


def test_within_category_ic_removes_category_composition_effect():
    """类内 IC 能剥掉「类别构成效应」：分数在类别内无区分力，但全池 IC 会被类别带偏。"""
    rows = [
        # rebound 三只：分数高但次日差
        _row(symbol="A", category="rebound", score=90.0, ndp=-5.0, system_p=0.9),
        _row(symbol="B", category="rebound", score=70.0, ndp=-1.0, system_p=0.5),
        _row(symbol="B2", category="rebound", score=50.0, ndp=2.0, system_p=0.1),
        # short_term 三只：分数低但次日好（分数与次日**正**相关）
        _row(symbol="C", category="short_term", score=10.0, ndp=3.0, system_p=0.05),
        _row(symbol="D", category="short_term", score=15.0, ndp=5.0, system_p=0.03),
        _row(symbol="D2", category="short_term", score=20.0, ndp=8.0, system_p=0.02),
    ]
    # 类别内：rebound 组分数与次日**反相关**（-1）；short_term 组正相关（+1）
    assert rb.within_category_rank_ic(rows, "score") == pytest.approx(0.0)
    # 全池：分数与次日几乎无关（构成效应互相抵消）
    assert rb.daily_rank_ic(rows, "score") is not None


def test_within_category_ic_skips_groups_below_three():
    rows = [
        _row(symbol="A", category="rebound", score=90.0),
        _row(symbol="B", category="rebound", score=50.0),
        _row(symbol="C", category="momentum", score=70.0),
    ]
    # 只有 rebound 组够 3 只 ⇒ momentum 组被跳过 ⇒ 返回 None（无有效组）
    assert rb.within_category_rank_ic(rows, "score") is None


def test_topn_hit_rate_returns_none_when_pool_too_small():
    rows = [_row(), _row(symbol="SZ300002")]
    assert rb.topn_hit_rate(rows, "score", 3) is None


# ── 3. 脚本纪律 ────────────────────────────────────────────────────────────────


def test_script_has_no_write_sql():
    """回放脚本只读库：不许有写语句（否则会把「检验」变成「改数据」）。"""
    import re

    src = _SCRIPT.read_text(encoding="utf-8")
    want = re.compile(r"\b(INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|CREATE\s+TABLE|DROP\s+TABLE)\b", re.I)
    assert not want.search(src)


def test_script_has_no_bare_except_exception():
    src = _SCRIPT.read_text(encoding="utf-8")
    import re

    assert not re.search(r"except\s+Exception\b", src)


def test_script_gives_no_recommendation_wording():
    """不给建议、不产短名单（与 `scripts/offboard_nextday.py` 同纪律）。"""
    src = _SCRIPT.read_text(encoding="utf-8")
    for banned in ("建议买入", "建议卖出", "首选", "推荐买入"):
        assert banned not in src
