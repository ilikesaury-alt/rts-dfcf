"""scanner.ranking 单源不变量（设计审查 P0 #1）。

防止档位/🎯 排序逻辑再次散落为 display 内的副本：
- ranking 必须定义全部排序纯函数；
- display 必须 re-export **同一个对象**（不是重新实现一份）；
  一旦有人在 display 里又写 `def _entry_tier`，`display._entry_tier is
  ranking._entry_tier` 即变 False，本测试报警，挡住口径漂移。
"""
import scanner.display as D
import scanner.ranking as R

RANKING_FUNCS = [
    "_entry_band",
    "_entry_dims",
    "_entry_fund_flow_pct",
    "_entry_overbought",
    "_entry_sector_resonance",
    "_entry_tier",
    "_entry_weak_to_strong",
    "_in_nextday_sweet_band",
    "_is_nextday_marked",
    "_nextday_entry_accum",
    "_nextday_entry_percent",
]


def test_ranking_defines_all_functions():
    missing = [n for n in RANKING_FUNCS if not hasattr(R, n)]
    assert not missing, f"scanner.ranking 缺少函数: {missing}"


def test_display_reexports_same_objects():
    """display 必须指向与 ranking 完全相同的函数对象（单源，非副本）。"""
    for n in RANKING_FUNCS:
        assert hasattr(D, n), f"display 未 re-export {n}"
        assert getattr(D, n) is getattr(R, n), (
            f"display.{n} 不是 scanner.ranking.{n} 的同一对象——"
            f"疑似在 display 内重写了排序逻辑（口径漂移风险）"
        )


def test_sweet_band_pure_logic():
    """甜蜜带纯函数行为不变量（<2% 或 4~8% 命中，2~4% 死区不命中）。"""
    assert R._in_nextday_sweet_band(1.0) is True
    assert R._in_nextday_sweet_band(5.0) is True
    assert R._in_nextday_sweet_band(3.0) is False
    assert R._in_nextday_sweet_band(9.0) is False
