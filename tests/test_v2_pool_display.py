"""v2 池选区排序与展示截断单测（2026-09-03）。

覆盖 _v2_pool_sort_key 三级排序键（低吸标签优先 → 榜上排名升序 → 涨幅降序，
2026-09-03 方案B 用户确认）、_entry_dip_labels 单源回退链与 ScanView.pool_total
全量计数字段。纯函数测试，不依赖 DB/网络。
"""

import pytest

from scanner.config import V2_POOL_DISPLAY_TOP
from scanner.display import ScanView, _entry_dip_labels, _v2_pool_sort_key


class TestV2PoolSortKey:
    def test_label_tier_is_primary(self):
        """主键：命中低吸标签的票排前段，与排名/涨幅无关。"""
        labeled = _v2_pool_sort_key(True, 1.0, 50)
        unlabeled_top = _v2_pool_sort_key(False, 9.9, 1)
        assert labeled < unlabeled_top

    def test_rank_ascending_within_segment(self):
        """段内：榜上排名升序（与涨幅无关）。"""
        assert _v2_pool_sort_key(False, 1.0, 5) < _v2_pool_sort_key(False, 9.9, 8)
        assert _v2_pool_sort_key(True, 1.0, 3) < _v2_pool_sort_key(True, 9.9, 10)

    def test_pct_descending_tiebreak(self):
        """末键：同排名按涨幅降序（消除平局洗牌）。"""
        assert _v2_pool_sort_key(False, 9.0, 5) < _v2_pool_sort_key(False, 5.0, 5)
        assert _v2_pool_sort_key(True, 3.0, 12) < _v2_pool_sort_key(True, -2.0, 12)

    def test_missing_rank_sinks_within_segment(self):
        """rank 缺失（掉榜/无排名）段内沉底，仍按涨幅降序。"""
        assert _v2_pool_sort_key(False, -5.0, 999) < _v2_pool_sort_key(False, 9.9, None)
        assert _v2_pool_sort_key(True, 1.0, None) < _v2_pool_sort_key(True, -1.0, None)

    def test_labeled_dropped_still_beats_unlabeled_top(self):
        """极端组合：有标签掉榜票仍排无标签榜首前（标签为最高优先级）。"""
        assert _v2_pool_sort_key(True, -20.0, None) < _v2_pool_sort_key(False, 20.0, 1)

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


class TestEntryDipLabels:
    """_entry_dip_labels 单源回退链：实时候选 dims → DB score_breakdown → 空。"""

    @staticmethod
    def _cand(labels):
        from scanner.models import Candidate, KlineSummary, StockInfo

        k = KlineSummary(
            trend="",
            accumulated_pct=0.0,
            volume_ratio=1.0,
            bottom_confirmed=False,
            score=0,
            dimensions={"dip_labels": labels},
        )
        return Candidate(
            stock=StockInfo(
                symbol="SZ300001",
                name="测试",
                code="300001",
                percent=2.0,
                current=10.0,
                value=1e8,
                rank_change=0,
                rank=1,
            ),
            category="pool_pick",
            score=0,
            reason="",
            kline=k,
        )

    def test_fresh_candidate_dims_first(self):
        """实时候选 dims 优先于 DB 快照（最新数据）。"""
        entry = {
            "symbol": "SZ300001",
            "category": "pool_pick",
            "_candidate": self._cand(["弱转强"]),
            "score_breakdown": {"dip_labels": ["超跌反转"]},
        }
        assert _entry_dip_labels(entry) == ["弱转强"]

    def test_db_score_breakdown_fallback(self):
        """无候选时回退 DB score_breakdown（掉榜/重启行）。"""
        entry = {
            "symbol": "SZ300002",
            "category": "pool_pick",
            "_candidate": None,
            "score_breakdown": {"dip_labels": ["缩量回调"]},
        }
        assert _entry_dip_labels(entry) == ["缩量回调"]

    def test_missing_everywhere_returns_empty(self):
        """两路均无标签返回空表（排序视为无标签段）。"""
        entry = {"symbol": "SZ300003", "category": "pool_pick", "_candidate": None}
        assert _entry_dip_labels(entry) == []

    def test_empty_labels_treated_as_unlabeled(self):
        """空列表与缺失等价——不进标签前段。"""
        entry = {"symbol": "SZ300004", "category": "pool_pick", "_candidate": self._cand([]), "score_breakdown": {}}
        assert _entry_dip_labels(entry) == []


if __name__ == "__main__":
    pytest.main([__file__])
