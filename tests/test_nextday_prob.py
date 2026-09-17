"""scanner.nextday_prob 单元测试：base rate / 因子方向 / 截断。

本文件**只断言因子的方向与大小关系，不断言具体数值**——常数的数值正确性由
`tests/test_nextday_calib.py` + `scanner/nextday_calib.json` 快照守护（2026-09-13 起）。
原因：2026-09-13 复核发现当时的 OR_MARKED 被高估 67%（拟合口径漏了线上
is_nextday_marked 的 5 日累计门槛），而当时本文件全绿 —— 只测方向的测试发现不了
常数漂移。

2026-09-16：🎯 降为纯展示标记 —— 🎯 不再是本模型因子（`OR_MARKED` 常数已删、
`marked` 形参已删），原先「marked 行跳过 band/超买」的防重复计费特例随之消失。

重算 / 巡检 / 同步常数：
    python -m scanner.nextday_calib            # 逐因子口径 + 实测 + 漂移（退出码 1 = 有漂移）
    python -m scanner.nextday_calib --write    # 重算并重写快照
    python -m pytest tests/test_nextday_calib.py -q

纪律：样本 <20 的因子不纳入（见 nextday_calib.FACTOR_SPECS 与模块 docstring 的排除清单）。
"""

from scanner.nextday_prob import (
    BASE_RATE_BY_CAT,
    BASE_RATE_DEFAULT,
    OR_BAND_DEAD,
    OR_BAND_MID,
    OR_BAND_SWEET_LOW,
    OR_BAND_TRAP,
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
    """活跃类别 base rate 全覆盖且为合法概率。

    数值本身不在此断言——由 tests/test_nextday_calib.py 对照快照守护（单一真源），
    避免同一数字散落在两处、改一处漏一处。
    """
    for cat in (
        "rebound",
        "known_new_face",
        "momentum",
        "new_face",
        "core_dip",
        "short_term",
        "pool_pick",
    ):
        assert cat in BASE_RATE_BY_CAT
        assert 0.0 < BASE_RATE_BY_CAT[cat] < 1.0
    assert 0.0 < BASE_RATE_DEFAULT < 1.0


def test_unknown_category_uses_default_base():
    """未知类别兜底全体基准——介于最强（rebound）与最弱（pool_pick）之间。"""
    p_rebound = next_day_hit_probability(_entry("rebound"))
    p_unknown = next_day_hit_probability(_entry("不存在的类别"))
    p_pool = next_day_hit_probability(_entry("pool_pick"))
    assert p_rebound > p_unknown > p_pool


# ── 单因子方向 ──


def test_overbought_lowers_probability():
    """超买（OR 0.84）降低概率（short_term 不吃 band，隔离出超买单因子）。"""
    ob = next_day_hit_probability(_entry("short_term", dims={"v_st_overbought": 1}))
    clean = next_day_hit_probability(_entry("short_term"))
    assert ob < clean


def test_band_dead_below_sweet_mid():
    """非 short_term：2-4% 死区概率低于 4-8% 甜蜜中段。"""
    dead = next_day_hit_probability(_entry("new_face", percent=3.0))
    mid = next_day_hit_probability(_entry("new_face", percent=5.0))
    assert mid > dead


def test_short_term_exempt_from_band():
    """short_term 豁免涨幅带（与 ranking.entry_tier 同口径）：带不同概率相同。"""
    a = next_day_hit_probability(_entry("short_term", percent=3.0))
    b = next_day_hit_probability(_entry("short_term", percent=5.0))
    assert a == b


def test_outflow_lowers_probability():
    """主力净流出 ≤-8%（OR 0.32）显著降低概率。"""
    base_p = next_day_hit_probability(_entry("new_face", percent=5.0))
    out = next_day_hit_probability(_entry("new_face", percent=5.0), flow=-9.0)
    assert out < base_p


def test_small_sector_resonance_lowers_probability():
    """小板块共振 cnt<15（OR 0.58）降低概率。"""
    base_p = next_day_hit_probability(_entry("new_face", percent=5.0))
    sect = next_day_hit_probability(
        _entry("new_face", percent=5.0, dims={"v_st_sector": 1, "v_st_sector_count": 3}),
    )
    assert sect < base_p


def test_prominence_raises_probability():
    """辨识度（OR 2.0）提升概率；False（查过但非辨识度）不提升。"""
    e = _entry("rebound", percent=1.5)
    p_prom = next_day_hit_probability(e, prominence=True)
    p_plain = next_day_hit_probability(e, prominence=False)
    assert p_prom > p_plain


# ── 🎯 已退出概率模型（2026-09-16）──


def test_marked_param_removed():
    """🎯 降为纯展示标记：next_day_hit_probability 不再接受 marked 形参（防回退）。

    这是「不喂 OR 因子」的守卫——若有人把 marked 参数加回来，本测试即失败。
    """
    try:
        next_day_hit_probability(_entry("new_face", percent=5.0), marked=True)
    except TypeError:
        return
    raise AssertionError("next_day_hit_probability 不应再接受 marked 形参（🎯 已降为纯展示标记）")


# ── 截断与数值稳定 ──


def test_probability_bounded_even_with_all_positive_factors():
    """多正因子叠加（甜蜜带+辨识度）不越过 P_MAX 上限；无因子不跌破 P_MIN。"""
    hot = _entry("rebound", percent=5.0)
    p_hot = next_day_hit_probability(hot, prominence=True)
    assert P_MIN <= p_hot <= P_MAX

    cold = _entry("pool_pick", percent=3.0, dims={"v_st_overbought": 1})
    p_cold = next_day_hit_probability(cold)
    assert P_MIN <= p_cold <= P_MAX


def test_band_or_mapping():
    """涨幅带 → OR 映射（与 ranking._entry_band 带边界同源）。"""
    assert _band_or(-2.0) == OR_BAND_SWEET_LOW  # 下跌并入低吸带（最近可得分桶）
    assert _band_or(1.0) == OR_BAND_SWEET_LOW  # 0-2%
    assert _band_or(3.0) == OR_BAND_DEAD  # 2-4% 死区
    assert _band_or(5.0) == OR_BAND_MID  # 4-8% 甜蜜中段
    assert _band_or(9.0) == OR_BAND_TRAP  # ≥8% 陷阱带
    assert OR_BAND_DEAD < 1.0  # 死区负向
    assert 0.0 < OR_OVERBOUGHT < 1.0  # 超买负向
    assert 0.0 < OR_SMALL_SECTOR < 1.0  # 小板块共振负向


if __name__ == "__main__":  # pragma: no cover
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
