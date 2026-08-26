"""档3 劣后原因单源 + 排序组合层回归测试（2026-08-26 重构）。

覆盖三件事：
1. entry_tier_reasons 与 _entry_tier 判定严格同源：原因非空 ⇔ 档3（对会评估
   警示因子的票），防两套逻辑再次漂移；
2. sort_main_entries 复合键 (symbol, category)：nf∩st 双挂票不再按 symbol 键控；
3. score_sort_key 分数方向由类别注册表驱动（kNF 升序、其余降序）。
"""
import pytest

from scanner import ranking as R


def _entry(sym, cat, score=50, breakdown=None, percent=5.0, date="2026-08-26"):
    e = {"symbol": sym, "date": date, "category": cat, "score": score,
         "percent": percent}
    if breakdown is not None:
        e["score_breakdown"] = breakdown
    return e


class TestEntryTierReasons:
    """entry_tier_reasons 单源不变量。"""

    def test_overheat_reason_and_tier(self):
        """过热（≥OVERHEAT_ACCUM_MAX）→ 唯一原因 + 档3，优先于 🎯。"""
        e = _entry("SZ300001", "momentum", percent=5.0)
        rs = R.entry_tier_reasons(e, accum=60.0)
        assert rs == [R.TIER_REASON_OVERHEAT]
        assert R._entry_tier(e, accum=60.0, marked=True) == 3

    def test_marked_entry_has_no_reasons(self):
        """🎯 档0 票不评估警示因子（与级联短路一致）→ 空原因。"""
        e = _entry("SZ300002", "momentum", breakdown={"v_st_overbought": True})
        # percent=5.0 在甜蜜带；显式 marked=True 模拟画像命中
        assert R.entry_tier_reasons(e, accum=10.0, marked=True) == []
        assert R._entry_tier(e, accum=10.0, marked=True) == 0

    def test_rebound_comeback_exempt_from_warnings(self):
        """rebound（档1）/comeback（豁免）不看警示因子 → 空原因。"""
        rb = _entry("SZ300003", "rebound", breakdown={"v_st_overbought": True})
        cb = _entry("SZ300004", "comeback", breakdown={"v_st_overbought": True})
        assert R.entry_tier_reasons(rb, accum=10.0) == []
        assert R.entry_tier_reasons(cb, accum=10.0) == []
        assert R._entry_tier(rb, accum=10.0) == 1
        assert R._entry_tier(cb, accum=10.0) == 2

    def test_warning_reason_matches_tier3(self):
        """超买命中 → 原因非空且档3；无警示 → 空原因且档2。同源不变量。

        percent=-1.0（down 带）避开 🎯 甜蜜带短路，确保走到警示因子评估。
        """
        overbought = _entry("SZ300005", "momentum", percent=-1.0,
                            breakdown={"v_mo_overbought": True})
        clean = _entry("SZ300006", "momentum", percent=-1.0, breakdown={})
        for e, expect_tier in ((overbought, 3), (clean, 2)):
            rs = R.entry_tier_reasons(e, accum=10.0)
            tier = R._entry_tier(e, accum=10.0)
            assert tier == expect_tier
            assert bool(rs) == (tier == 3)

    def test_band_dead_zone_reason(self):
        """2-4% 死区 → 涨幅带死区/陷阱原因（momentum 不豁免）。"""
        e = _entry("SZ300007", "momentum", percent=3.0)
        rs = R.entry_tier_reasons(e, accum=10.0)
        assert R.TIER_REASON_BAND in rs
        assert R._entry_tier(e, accum=10.0) == 3

    def test_short_term_band_exempt_but_weak_to_strong_markable(self):
        """short_term 豁免涨幅带：死区涨幅无弱转强不标 🎯 → 落档2 无原因。"""
        e = _entry("SZ300008", "short_term", percent=3.0)
        assert R.entry_tier_reasons(e, accum=10.0) == []
        assert R._entry_tier(e, accum=10.0) == 2

    def test_flow_param_overrides_dims(self):
        """显式 flow 覆盖 dims 缺失（掉榜行 market_extra_cache 补值路径）。"""
        e = _entry("SZ300009", "momentum", percent=-1.0, breakdown={})
        rs = R.entry_tier_reasons(e, accum=10.0, flow=-9.0)
        assert R.TIER_REASON_FUND_OUTFLOW in rs

    def test_multiple_reasons_stackable(self):
        """多因子叠加：超买+资金流出同时命中，全部返回。"""
        e = _entry("SZ300010", "momentum",
                   breakdown={"v_mo_overbought": True, "fund_flow_main_pct": -10.0})
        rs = R.entry_tier_reasons(e, accum=10.0)
        assert R.TIER_REASON_OVERBOUGHT in rs
        assert R.TIER_REASON_FUND_OUTFLOW in rs


class TestSortMainEntriesCompositeKey:
    """tier_map 复合键 (symbol, category)。"""

    def test_dual_listed_same_symbol_different_tiers(self):
        """nf∩st 双挂票：同 symbol 两行各用自己类别的档位（st 豁免死区带）。"""
        nf = _entry("SZ300020", "new_face", score=70, percent=3.0)   # 死区 → 档3
        st = _entry("SZ300020", "short_term", score=70, percent=3.0)  # 豁免 → 档2
        tier_map = {
            ("SZ300020", "new_face"): R._entry_tier(nf, accum=10.0),
            ("SZ300020", "short_term"): R._entry_tier(st, accum=10.0),
        }
        assert tier_map[("SZ300020", "new_face")] == 3
        assert tier_map[("SZ300020", "short_term")] == 2
        out = R.sort_main_entries([nf, st], tier_map)
        assert [e["category"] for e in out] == ["short_term", "new_face"]

    def test_missing_key_defaults_tier2(self):
        """未知键兜底档2（向后兼容）。"""
        e = _entry("SZ300021", "momentum")
        out = R.sort_main_entries([e], {})
        assert out == [e]


class TestScoreSortKeyRegistry:
    """分数方向由 categories.SCORE_DESCENDING_BY_CAT 驱动。"""

    def test_knf_ascending(self):
        lo = _entry("SZ300030", "known_new_face", score=20)
        hi = _entry("SZ300031", "known_new_face", score=90)
        out = R.sort_main_entries([hi, lo], {})
        assert [e["score"] for e in out] == [20, 90]

    def test_others_descending(self):
        lo = _entry("SZ300032", "momentum", score=20)
        hi = _entry("SZ300033", "momentum", score=90)
        out = R.sort_main_entries([lo, hi], {})
        assert [e["score"] for e in out] == [90, 20]

    def test_registry_has_no_hardcoded_knf_in_ranking(self):
        """方向语义只在注册表：SCORE_DESCENDING_BY_CAT['known_new_face'] is False。"""
        from scanner.categories import SCORE_DESCENDING_BY_CAT
        assert SCORE_DESCENDING_BY_CAT["known_new_face"] is False
        assert all(v for k, v in SCORE_DESCENDING_BY_CAT.items() if k != "known_new_face")
