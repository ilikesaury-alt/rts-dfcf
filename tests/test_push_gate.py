"""tests/test_push_gate.py — 飞书严格过滤门（scanner/push_gate.py）。

门的三条纪律各有守卫：
  1. 主键是类别先验、**不是 score** → test_score_never_enters_the_gate
  2. 否决阈值全部复用上游单源 → test_veto_thresholds_are_shared_sources
  3. fail-open + 不静默少票 → test_missing_fields_do_not_veto / test_note_reports_every_drop
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from scanner.config import (
    CATEGORY_HIT_RATE,
    CATEGORY_HIT_RATE_DEFAULT,
    FUND_OUTFLOW_NET_PCT,
    OVERHEAT_ACCUM_MAX,
    PUSH_FALLBACK_MAX_RANK,
    PUSH_TIER_A_MIN,
    PUSH_TIER_B_MIN,
)
from scanner.push_gate import (
    TIER_A,
    TIER_B,
    TIER_C,
    GateResult,
    GateStats,
    apply_push_gate,
    classify,
)


# ── 轻量替身：只带门读得到的字段 ──
@dataclass
class FakeMainRow:
    entry: dict
    rank: int | None = None
    accum: float | None = None

    @property
    def symbol(self) -> str:
        return self.entry["symbol"]


@dataclass
class FakeView:
    main_rows: list = field(default_factory=list)
    flow_pct_map: dict = field(default_factory=dict)
    hist_rows: list | None = None
    hot_rows: list | None = None
    offboard_rows: list | None = None


@dataclass
class FakeHot:
    symbol: str
    streak: int = 1
    rank_change: int = 0
    accum_5d: float | None = None
    ff_pct: float | None = None


def _row(cat: str, *, rank=None, accum=None, sym="SZ300000", name="测试") -> FakeMainRow:
    return FakeMainRow(entry={"symbol": sym, "name": name, "category": cat}, rank=rank, accum=accum)


# ── 纪律 1：主键是类别先验，不是 score ──


def test_classify_splits_on_priors_only():
    assert classify("rebound")[0] == TIER_A  # 17.9%
    assert classify("known_new_face")[0] == TIER_A  # 12.7%
    assert classify("new_face")[0] == TIER_B  # 9.7%
    assert classify("short_term")[0] == TIER_C  # 6.2%
    assert classify("pool_pick")[0] == TIER_C  # 2.1%


def test_tier_boundaries_derive_from_priors_not_magic_numbers():
    """B 档边界必须**就是** CATEGORY_HIT_RATE_DEFAULT，先验表一动门跟着动。"""
    assert PUSH_TIER_B_MIN == CATEGORY_HIT_RATE_DEFAULT
    # A 档边界落在 momentum(10.0%) 之上、new_face(9.7%) 之下
    assert CATEGORY_HIT_RATE["momentum"] >= PUSH_TIER_A_MIN > CATEGORY_HIT_RATE["new_face"]


def test_out_of_table_categories_are_C_not_B():
    """⚠ 回归守卫：`comeback` 被从先验表删掉了（实测 hit 2.8%，全场最差）。

    若按「未知 → CATEGORY_HIT_RATE_DEFAULT」放行，它会整批白拿 B 档。
    实测该 bug 让 2026-09-14~16 的 64 只 comeback 全部通过（hit 2.8%）。
    """
    assert "comeback" not in CATEGORY_HIT_RATE  # 前提：它确实不在表里
    for cat in ("comeback", "pullback", "old_face", "early_momentum", None, "", "不存在的桶"):
        tier, _hit = classify(cat)
        assert tier == TIER_C, f"{cat} 不该拿到放行档"


def test_score_never_enters_the_gate():
    """纪律 1 的行为守卫：score 再高也不改变判定（门只看 category/rank/否决项）。"""
    a = _row("pool_pick", rank=5)
    b = _row("pool_pick", rank=5)
    a.entry["score"] = 0
    b.entry["score"] = 999
    view = FakeView(main_rows=[a, b])
    res = apply_push_gate(view)
    assert len(res.main) == 2  # 两者同进同出


# ── 纪律 2：否决阈值复用上游单源 ──


def test_veto_thresholds_are_shared_sources():
    assert FUND_OUTFLOW_NET_PCT == -8.0
    assert OVERHEAT_ACCUM_MAX == 50.0


@pytest.mark.parametrize(
    ("ff", "accum", "expect_pass"),
    [
        (None, None, True),  # fail-open：全缺数据不否决
        (0.0, 0.0, True),
        (-7.9, 10.0, True),  # 未到 -8
        (-8.0, 10.0, False),  # 边界命中
        (-20.0, 0.0, False),
        (0.0, 49.9, True),  # 未到过热
        (0.0, 50.0, False),  # 边界命中
    ],
)
def test_veto_boundaries(ff, accum, expect_pass):
    v = FakeView(main_rows=[_row("rebound", accum=accum)], flow_pct_map={"SZ300000": ff})
    res = apply_push_gate(v)
    assert bool(res.main) is expect_pass


def test_risk_hard_flags_veto():
    class C:
        risk_flags = ["超买"]  # ∈ RISK_FLAGS_DISPLAY_HARD

    r = _row("rebound")
    r.entry["_candidate"] = C()
    assert not apply_push_gate(FakeView(main_rows=[r])).main
    # 软信号不否决
    class C2:
        risk_flags = ["小板块共振"]  # ∉ RISK_FLAGS_DISPLAY_HARD

    r2 = _row("rebound")
    r2.entry["_candidate"] = C2()
    assert apply_push_gate(FakeView(main_rows=[r2])).main


# ── 纪律 3：fail-open + 不静默少票 ──


def test_missing_rank_does_not_veto_but_blocks_fallback():
    """rank 缺失 → C 档拿不到兜底 → 被剔（但**不是**否决，账上记 dropped_no_fallback）。"""
    res = apply_push_gate(FakeView(main_rows=[_row("pool_pick", rank=None)]))
    assert not res.main
    assert res.stats.vetoed == 0
    assert res.stats.dropped_no_fallback == 1


def test_fallback_uses_rank_threshold():
    inside = apply_push_gate(FakeView(main_rows=[_row("pool_pick", rank=PUSH_FALLBACK_MAX_RANK)]))
    outside = apply_push_gate(FakeView(main_rows=[_row("pool_pick", rank=PUSH_FALLBACK_MAX_RANK + 1)]))
    assert len(inside.main) == 1
    assert not outside.main


def test_note_reports_every_drop():
    """卡片必须能说清「10 只里过了几只、为什么」——静默少票=与终端分叉。"""
    res = apply_push_gate(
        FakeView(
            main_rows=[
                _row("rebound", sym="SZ300001"),  # A 档
                _row("pool_pick", rank=5, sym="SZ300002"),  # C 档兜底
                _row("pool_pick", rank=99, sym="SZ300003"),  # C 档无兜底
                _row("rebound", accum=99.0, sym="SZ300004"),  # 过热否决
            ]
        )
    )
    note = res.note()
    assert "2/4" in note
    assert "兜底档 1" in note
    assert "类别先验不足" in note
    assert "否决" in note


def test_note_empty_when_nothing_filtered():
    res = apply_push_gate(FakeView(main_rows=[_row("rebound")]))
    assert res.note() == ""


def test_stats_totals_add_up():
    res = apply_push_gate(FakeView(main_rows=[_row("rebound"), _row("pool_pick", rank=99)]))
    s = res.stats
    assert s.total == 2
    assert s.passed == 1
    assert s.filtered == 1
    assert s.passed == s.tier_a + s.tier_b + s.tier_c_fallback


# ── 三区各自兜底条件 ──


def test_hot_rows_need_streak_or_rank_rise():
    """hot 区无 category → 恒 C 档，只能靠热度证据。"""
    assert apply_push_gate(FakeView(hot_rows=[FakeHot("SZ1", streak=3)])).hot
    assert apply_push_gate(FakeView(hot_rows=[FakeHot("SZ2", rank_change=30)])).hot
    assert not apply_push_gate(FakeView(hot_rows=[FakeHot("SZ3", streak=1, rank_change=5)])).hot


def test_offboard_rows_need_volume_ratio():
    @dataclass
    class B:
        symbol: str
        volume_ratio: float = 0.0
        accum_5d: float | None = None
        ff_pct: float | None = None

    assert apply_push_gate(FakeView(offboard_rows=[B("SZ1", volume_ratio=2.0)])).offboard
    assert not apply_push_gate(FakeView(offboard_rows=[B("SZ2", volume_ratio=1.0)])).offboard


def test_hist_rows_classified_by_rec_category():
    @dataclass
    class H:
        symbol: str
        rec_category: str | None = None
        vol_ratio: float = 0.0
        accum_5d: float | None = None
        ff_pct: float | None = None

    # rec_category=rebound(A 档) → 无需量比
    assert apply_push_gate(FakeView(hist_rows=[H("SZ1", "rebound", vol_ratio=0.0)])).hist
    # rec_category=pool_pick(C 档) → 必须量比
    assert not apply_push_gate(FakeView(hist_rows=[H("SZ2", "pool_pick", vol_ratio=0.0)])).hist
    assert apply_push_gate(FakeView(hist_rows=[H("SZ3", "pool_pick", vol_ratio=2.0)])).hist


# ── 与 view_has_content / 去重键的契约 ──


def test_dedup_key_is_passed_set_not_all_rows():
    """去重键必须是**通过集** —— 旧实现取 main_rows[:10]，那 10 行每轮都在换，
    击穿冷却（2026-09-28 全天推了 ~50 张）。"""
    from scanner.feishu import _view_symbols

    view = FakeView(
        main_rows=[_row("rebound", sym="SZ_A", rank=1), _row("pool_pick", rank=99, sym="SZ_B")]
    )
    assert _view_symbols(view) == {"SZ_A"}


def test_gate_has_content_false_when_all_filtered():
    from scanner.feishu import gate_has_content, view_has_content

    view = FakeView(main_rows=[_row("pool_pick", rank=99)])
    assert view_has_content(view) is True  # 视��里有内容
    assert gate_has_content(view) is False  # 但过滤后一张都推不出 → 不推空卡


def test_gate_result_symbols_reads_all_four_sections():
    res = GateResult(main=[_row("rebound", sym="SZ_A")], hot=[FakeHot("SZ_B")])
    assert res.symbols() == {"SZ_A", "SZ_B"}


def test_max_rows_cap_keeps_high_tier_first():
    """超上限时按 A→B→C 截断，不能让兜底票挤掉高先验票。"""
    from scanner.config import PUSH_MAX_ROWS

    rows = [_row("pool_pick", rank=1, sym=f"SZ_C{i}") for i in range(PUSH_MAX_ROWS + 3)]
    rows.append(_row("rebound", sym="SZ_A"))
    res = apply_push_gate(FakeView(main_rows=rows))
    assert len(res.main) == PUSH_MAX_ROWS
    assert res.main[0].entry["symbol"] == "SZ_A"  # A 档必须在最前


def test_gate_disabled_passes_everything_through(monkeypatch):
    monkeypatch.setattr("scanner.push_gate.PUSH_GATE_ENABLED", False)
    view = FakeView(main_rows=[_row("pool_pick", rank=99)])
    res = apply_push_gate(view)
    assert len(res.main) == 1
    assert res.note() == ""


def test_empty_view_is_safe():
    res = apply_push_gate(FakeView())
    assert res.passed_total == 0
    assert res.symbols() == set()
    assert isinstance(res.stats, GateStats)
