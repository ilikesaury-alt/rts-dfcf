"""画像注册表一致性守护测试（2026-08-26 Phase 4）。

覆盖两类单源不变量：
1. NEXTDAY_CAT_SPECS 键集合 ≡ categories.NEXTDAY_CAT_PRIORITY——🎯 规格表
   从 _is_nextday_marked 的 if/elif 收口而来，两处类别宇宙漂移即报警；
2. ⚡ 类别门 dispatcher（_breakout_profile_key）全类别×首推矩阵：
   两变体按构造不相交 + 各类别归属符合文档语义。
"""

from scanner.categories import NEXTDAY_CAT_PRIORITY
from scanner.ranking import (
    NEXTDAY_CAT_SPECS,
    _breakout_profile_key,
    _is_nextday_marked,
)


def _entry(cat, percent=5.0, breakdown=None):
    e = {"symbol": "SZ300099", "date": "2026-08-26", "category": cat,
         "score": 50, "percent": percent}
    if breakdown is not None:
        e["score_breakdown"] = breakdown
    return e


class TestNextdaySpecRegistry:

    def test_spec_keys_match_category_registry(self):
        """规格表键集合必须与类别注册表派生的 NEXTDAY_CAT_PRIORITY 完全一致。"""
        assert set(NEXTDAY_CAT_SPECS) == set(NEXTDAY_CAT_PRIORITY)

    def test_accum_exempts_match_doc(self):
        """累计门槛豁免 = rebound/short_term（校准结论：负累计天然/规律在弱转强）。"""
        assert NEXTDAY_CAT_SPECS["rebound"][1] is False
        assert NEXTDAY_CAT_SPECS["short_term"][1] is False
        for cat in ("known_new_face", "momentum", "new_face"):
            assert NEXTDAY_CAT_SPECS[cat][1] is True, cat

    def test_short_term_shape_is_weak_to_strong(self):
        """short_term 分型 = 弱转强（甜蜜带对其负效，2026-08-17 校准）。"""
        assert NEXTDAY_CAT_SPECS["short_term"][0] == "weak_to_strong"


class TestNextdayMarkedParity:
    """🎯 判定行为矩阵（收口前后语义不变的回归保护）。"""

    def test_momentum_accum_gate_blocks_low(self):
        e = _entry("momentum", percent=5.0)
        assert _is_nextday_marked(e, accum=3.0) is False
        assert _is_nextday_marked(e, accum=7.0) is True

    def test_momentum_accum_missing_fail_open(self):
        """累计缺失 fail-open 放行（不误杀）。"""
        assert _is_nextday_marked(_entry("momentum"), accum=None) is True

    def test_rebound_exempt_from_accum(self):
        """rebound 负累计天然豁免：低累计甜蜜带仍标。"""
        assert _is_nextday_marked(_entry("rebound", percent=1.0), accum=-12.0) is True

    def test_short_term_requires_weak_to_strong_not_band(self):
        """short_term：甜蜜带但无弱转强 → 不标；弱转强（涨幅带外）→ 标。"""
        sweet_no_w2s = _entry("short_term", percent=5.0)
        assert _is_nextday_marked(sweet_no_w2s) is False
        w2s = _entry("short_term", percent=9.0,
                     breakdown={"st_weak_to_strong": 1})
        assert _is_nextday_marked(w2s) is True

    def test_overbought_vetoes_all_categories(self):
        """超买死亡信号对全部类别一票否决。"""
        e = _entry("momentum", breakdown={"v_mo_overbought": True})
        assert _is_nextday_marked(e, accum=7.0) is False
        e_rb = _entry("rebound", percent=1.0,
                      breakdown={"v_st_overbought": True})
        assert _is_nextday_marked(e_rb) is False

    def test_non_markable_categories_never_marked(self):
        for cat in ("comeback", "core_dip", "pullback"):
            assert _is_nextday_marked(_entry(cat)) is False


class TestBreakoutProfileKey:
    """⚡ 类别门 dispatcher：全类别 × 首推矩阵。"""

    CATS = ("rebound", "known_new_face", "momentum", "new_face",
            "short_term", "comeback", "core_dip")

    @staticmethod
    def _key(cat, first_push):
        sb = {"first_today_bonus": 10} if first_push else {}
        return _breakout_profile_key({"symbol": "S", "date": "D",
                                      "category": cat, "score": 50,
                                      "score_breakdown": sb})

    def test_variants_disjoint_everywhere(self):
        """不变量：任意 (category, first_push) 组合下两变体互斥（至多命中一个）。"""
        for cat in self.CATS:
            for push in (False, True):
                hits = [k for k in ("breakout", "relist")
                        if self._key(cat, push) == k]
                assert len(hits) <= 1, (cat, push, hits)

    def test_gate_matrix(self):
        expected = {
            ("new_face", False): "breakout",
            ("known_new_face", False): "breakout",
            ("momentum", True): "breakout",      # 首推条款不限类别
            ("momentum", False): None,
            ("short_term", True): "breakout",     # 首推 short_term 归 ⚡
            ("short_term", False): "relist",      # 非首推 short_term 归 ⚡R
            ("rebound", False): None,
            ("comeback", False): None,
            ("core_dip", False): None,
        }
        for (cat, push), want in expected.items():
            assert self._key(cat, push) == want, (cat, push)

    def test_first_push_flag_falsy_variants(self):
        """first_today_bonus 为 0/None 视同非首推。"""
        e = {"symbol": "S", "date": "D", "category": "short_term", "score": 50,
             "score_breakdown": {"first_today_bonus": 0}}
        assert _breakout_profile_key(e) == "relist"
