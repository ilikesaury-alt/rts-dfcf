"""⚡ 蓄势突破观察画像 —— 类别门 dispatcher 一致性守护（2026-08-26 Phase 4）。

覆盖不变量：`_breakout_profile_key` 全类别 × 首推矩阵 ——
两变体按构造不相交 + 各类别归属符合文档语义。

> 2026-09-16：原 `TestNextdaySpecRegistry` / `TestNextdayMarkedParity` 随 🎯
> 次日大涨画像（`is_nextday_marked` / `NEXTDAY_CAT_SPECS` / `categories.NEXTDAY_CAT_PRIORITY`）
> 一并删除。本文件现只守 ⚡ 画像这一条单源约束。
"""

from scanner.ranking import _breakout_profile_key


class TestBreakoutProfileKey:
    """⚡ 类别门 dispatcher：全类别 × 首推矩阵。"""

    CATS = ("rebound", "known_new_face", "momentum", "new_face", "short_term", "core_dip")

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
            ("core_dip", False): None,
        }
        for (cat, push), want in expected.items():
            assert self._key(cat, push) == want, (cat, push)

    def test_first_push_flag_falsy_variants(self):
        """first_today_bonus 为 0/None 视同非首推。"""
        e = {"symbol": "S", "date": "D", "category": "short_term", "score": 50,
             "score_breakdown": {"first_today_bonus": 0}}
        assert _breakout_profile_key(e) == "relist"
