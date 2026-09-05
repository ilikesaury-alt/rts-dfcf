"""scanner.nextday_prob 单元测试：base rate / 因子方向 / 防重复计费 / 截断。

模型是朴素贝叶斯式 odds 乘积（log-odds 可加 + 收缩），所有常数校准于
2026-09-05 当期 1786 去重样本。校准漂移时重算口径（与 nextday_attribution 一致）：

    python -m scanner.nextday_attribution
    # 因子条件命中率补算：load_attribution_rows(conn, "next_day_pct",
    #   cols="name, percent, score_breakdown, accumulated_pct")
    # 按 scanner/nextday_prob.py docstring 的因子定义重算，复核本模块常数。

纪律：样本 <20 的因子不纳入（见模块 docstring 的排除清单）。
"""

import math

from scanner.nextday_prob import (
    BASE_RATE_BY_CAT,
    BASE_RATE_DEFAULT,
    OR_BAND_DEAD,
    OR_BAND_MID,
    OR_BAND_SWEET_LOW,
    OR_BAND_TRAP,
    OR_MARKED,
    OR_OVERBOUGHT,
    OR_SMALL_SECTOR,
    P_MAX,
    P_MIN,
    _band_or,
    next_day_hit_probability,
)


def _entry(cat="pool_pick", percent=3.0, dims=None):
    """最小综合排序行：band/超买/板块共振经 ranking 助手从 score_breakdown 读。"""
    return {"category": cat, "percent": percent, "score_breakdown": dims or {}}


# ── base rate ──


def test_base_rates_cover_known_categories():
    """活跃类别 base rate 全覆盖且为合法概率。"""
    for cat in (
        "rebound",
        "known_new_face",
        "momentum",
        "new_face",
        "core_dip",
        "short_term",
        "pool_pick",
        "comeback",
    ):
        assert cat in BASE_RATE_BY_CAT
        assert 0.0 < BASE_RATE_BY_CAT[cat] < 1.0
    assert math.isclose(BASE_RATE_DEFAULT, 0.078)


def test_unknown_category_uses_default_base():
    """未知类别兜底全体基准 7.8%——介于最强（rebound）与最弱（pool_pick）之间。"""
    p_rebound = next_day_hit_probability(_entry("rebound"), marked=False)
    p_unknown = next_day_hit_probability(_entry("不存在的类别"), marked=False)
    p_pool = next_day_hit_probability(_entry("pool_pick"), marked=False)
    assert p_rebound > p_unknown > p_pool


# ── 单因子方向 ──


def test_marked_raises_probability():
    """🎯 复合画像（OR 2.6）提升概率。"""
    e = _entry("new_face", percent=5.0)  # 甜蜜中段
    assert next_day_hit_probability(e, marked=True) > next_day_hit_probability(e, marked=False)


def test_overbought_lowers_probability():
    """超买（OR 0.68）降低概率（unmarked 行）。"""
    ob = next_day_hit_probability(_entry("short_term", dims={"v_st_overbought": 1}), marked=False)
    clean = next_day_hit_probability(_entry("short_term"), marked=False)
    assert ob < clean


def test_band_dead_below_sweet_mid():
    """unmarked 非 short_term：2-4% 死区概率低于 4-8% 甜蜜中段。"""
    dead = next_day_hit_probability(_entry("new_face", percent=3.0), marked=False)
    mid = next_day_hit_probability(_entry("new_face", percent=5.0), marked=False)
    assert mid > dead


def test_short_term_exempt_from_band():
    """short_term 豁免涨幅带（与 ranking._entry_tier 同口径）：带不同概率相同。"""
    a = next_day_hit_probability(_entry("short_term", percent=3.0), marked=False)
    b = next_day_hit_probability(_entry("short_term", percent=5.0), marked=False)
    assert a == b


def test_outflow_lowers_probability():
    """主力净流出 ≤-8%（OR 0.32）显著降低概率。"""
    base_p = next_day_hit_probability(_entry("new_face", percent=5.0), marked=False)
    out = next_day_hit_probability(_entry("new_face", percent=5.0), marked=False, flow=-9.0)
    assert out < base_p


def test_small_sector_resonance_lowers_probability():
    """小板块共振 cnt<15（OR 0.58）降低概率。"""
    base_p = next_day_hit_probability(_entry("new_face", percent=5.0), marked=False)
    sect = next_day_hit_probability(
        _entry("new_face", percent=5.0, dims={"v_st_sector": 1, "v_st_sector_count": 3}),
        marked=False,
    )
    assert sect < base_p


def test_prominence_raises_probability():
    """辨识度（OR 2.0）提升概率；False（查过但非辨识度）不提升。"""
    e = _entry("rebound", percent=1.5)
    p_prom = next_day_hit_probability(e, marked=False, prominence=True)
    p_plain = next_day_hit_probability(e, marked=False, prominence=False)
    assert p_prom > p_plain


# ── 防重复计费 ──


def test_marked_skips_band_and_overbought_factors():
    """🎯 复合画像已含甜蜜带+非超买效应：marked 行的带/超买差异不影响概率。"""
    dead_marked = next_day_hit_probability(_entry("new_face", percent=3.0), marked=True)
    mid_marked = next_day_hit_probability(_entry("new_face", percent=5.0), marked=True)
    assert dead_marked == mid_marked  # band 因子不叠加

    ob_marked = next_day_hit_probability(_entry("new_face", percent=5.0, dims={"v_mo_overbought": 1}), marked=True)
    clean_marked = next_day_hit_probability(_entry("new_face", percent=5.0), marked=True)
    assert ob_marked == clean_marked  # 超买因子不叠加


# ── 截断与数值稳定 ──


def test_probability_bounded_even_with_all_positive_factors():
    """多正因子叠加（🎯+辨识度）不越过 P_MAX 上限；无因子不跌破 P_MIN。"""
    hot = _entry("rebound", percent=5.0)
    p_hot = next_day_hit_probability(hot, marked=True, prominence=True)
    assert P_MIN <= p_hot <= P_MAX

    cold = _entry("pool_pick", percent=3.0, dims={"v_st_overbought": 1})
    p_cold = next_day_hit_probability(cold, marked=False)
    assert P_MIN <= p_cold <= P_MAX


def test_band_or_mapping():
    """涨幅带 → OR 映射（与 ranking._entry_band 带边界同源）。"""
    assert _band_or(-2.0) == OR_BAND_SWEET_LOW  # 下跌并入低吸带（最近可得分桶）
    assert _band_or(1.0) == OR_BAND_SWEET_LOW  # 0-2%
    assert _band_or(3.0) == OR_BAND_DEAD  # 2-4% 死区
    assert _band_or(5.0) == OR_BAND_MID  # 4-8% 甜蜜中段
    assert _band_or(9.0) == OR_BAND_TRAP  # ≥8% 陷阱带
    assert OR_MARKED > 1.0  # 🎯 复合画像正向
    assert OR_BAND_DEAD < 1.0  # 死区负向
    assert 0.0 < OR_OVERBOUGHT < 1.0  # 超买负向
    assert 0.0 < OR_SMALL_SECTOR < 1.0  # 小板块共振负向


if __name__ == "__main__":  # pragma: no cover
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
