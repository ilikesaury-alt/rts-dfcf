"""v2 池选区排序与展示截断单测（2026-09-03）。

覆盖 _v2_pool_sort_key 三级排序键（实时优先 → 涨幅降序 → 榜上排名升序）
与 ScanView.pool_total 全量计数字段。纯函数测试，不依赖 DB/网络。
"""

import pytest

from scanner.config import V2_POOL_DISPLAY_TOP
from scanner.display import ScanView, _v2_pool_sort_key


class TestV2PoolSortKey:
    def test_live_ranks_before_stale(self):
        """有实时行情的票排在 stale 票前面，即使涨幅更低（涨幅口径同源，杜绝错位）。"""
        live_low = _v2_pool_sort_key(pct=1.0, is_live=True, rank=5)
        stale_high = _v2_pool_sort_key(pct=9.9, is_live=False, rank=1)
        assert live_low < stale_high

    def test_pct_descending_within_same_liveness(self):
        """同为实时（或同为 stale）时按涨幅降序。"""
        assert _v2_pool_sort_key(9.0, True, 5) < _v2_pool_sort_key(5.0, True, 1)
        assert _v2_pool_sort_key(3.0, False, 9) < _v2_pool_sort_key(-2.0, False, 2)

    def test_rank_ascending_tiebreak(self):
        """涨幅相同时按榜上排名升序（消除平局洗牌）。"""
        assert _v2_pool_sort_key(7.0, True, 3) < _v2_pool_sort_key(7.0, True, 10)

    def test_missing_rank_sorts_last(self):
        """rank 缺失排在同涨幅有 rank 票之后。"""
        with_rank = _v2_pool_sort_key(7.0, True, 999)
        no_rank = _v2_pool_sort_key(7.0, True, None)
        assert with_rank < no_rank

    def test_zero_pct_rank_none_is_stable_key(self):
        """极端组合不抛错且可比较（float | None / 边界值），且符合键定义次序。"""
        k_stale_zero = _v2_pool_sort_key(0.0, False, None)
        k_live_zero = _v2_pool_sort_key(0.0, True, None)
        k_live_down = _v2_pool_sort_key(-20.0, True, 1)
        assert k_live_zero < k_live_down < k_stale_zero

    def test_pool_display_top_configured(self):
        """展示截断常量存在且为正（config 单一阈值源）。"""
        assert isinstance(V2_POOL_DISPLAY_TOP, int)
        assert V2_POOL_DISPLAY_TOP > 0

    def test_scanview_pool_total_default(self):
        """ScanView.pool_total 默认 0，pool_rows 默认 None（向后兼容旧构造方）。"""
        view = ScanView(
            main_rows=[],
            comeback_rows=[],
            core_dip_rows=[],
            nextday_mark={},
            breakout_mark={},
            flow_pct_map={},
            last_ranks={},
            adj_picks=None,
            weak=False,
            show_comeback=False,
            show_core_dip=False,
            warnings=[],
        )
        assert view.pool_total == 0
        assert view.pool_rows is None


if __name__ == "__main__":
    pytest.main([__file__])
