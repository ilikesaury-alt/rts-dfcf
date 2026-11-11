"""`get_board_rank` 盘后降级说明的守卫（2026-11-11）。

## 问题

雪球飙升榜是**全球**市场热度的读数。A 股 15:00 收盘后 A 股停止交易，榜单自然由海外
标的占据 —— 实测 2026-11-11 18:10 收盘后实拉，前排是 NVDA / SK海力士 / CoreWeave /
AMD，A 股只占 8~9/30。

模型若不加区分，会拿「英伟达涨幅」去论证「A 股某只算力票的题材共振」—— 这是幻觉式
推理的温床（工具给的数字是真的，但**用它得出的结论是假的**）。

## 为什么用「实际 A 股占比」而不是「时钟」判断

榜单构成才是事实。节假日/周末当天 A 股根本没交易，占比同样会塌 —— 时钟判断在那些
日子会误判成「正常 A 股榜单」。用实际占比天然适配所有非交易时段。

实测基准（`leaderboard_log.symbol_snapshot`）：
- 盘中 09:30~14:59：A 股占 82%~95%（尾盘 14:50~14:59 稳定 95%）
- 收盘后 18:10：A 股占 27%
"""

from __future__ import annotations

import pytest

from scanner.ai_digest.research import (
    _BD_ASHARE_MIN_RATIO,
    _board_ashare_note,
    _tool_get_board_rank,
)


def _items(a_count: int, foreign_count: int) -> list[dict[str, object]]:
    return [{"symbol": f"SZ300{i:03d}"} for i in range(a_count)] + [
        {"symbol": f"X{i}"} for i in range(foreign_count)
    ]


def test_intraday_ratio_no_downgrade() -> None:
    """盘中（A股占 95%）⇒ 不降级，且**明说可用**。"""
    note = _board_ashare_note(_items(38, 2))
    assert "以 A 股为主" in note
    assert "不可用于论证" not in note


def test_after_close_ratio_triggers_downgrade() -> None:
    """盘后（A股占 27%）⇒ 必须降级说明，且明说不可用于 A 股题材共振。"""
    note = _board_ashare_note(_items(8, 22))
    assert "仅占 8/30" in note
    assert "不可用于论证 A 股个股的题材共振" in note
    assert "海外标的" in note


def test_empty_board_no_note() -> None:
    """空榜不产出说明（没有数据就没有时段结论）。"""
    assert _board_ashare_note([]) == ""


def test_threshold_boundary() -> None:
    """占比恰好等于阈值 ⇒ 视为 A 股为主（不降级）。"""
    items = _items(5, 5)  # 50% == _BD_ASHARE_MIN_RATIO
    assert _BD_ASHARE_MIN_RATIO == 0.5
    assert "以 A 股为主" in _board_ashare_note(items)

    items2 = _items(4, 6)  # 40% < 50%
    assert "不可用于论证" in _board_ashare_note(items2)


def test_note_does_not_leak_into_malformed_items() -> None:
    """缺 symbol/code 的行不该让判定崩掉（按非 A 股计）。"""
    items = [{"symbol": "SZ300001"}, {"name": "无代码"}, {"code": "AMD"}]
    note = _board_ashare_note(items)
    assert "仅占" in note  # 1/3 < 50% ⇒ 降级


def test_tool_output_contains_note(monkeypatch: pytest.MonkeyPatch) -> None:
    """工具输出里**必须带**这条说明（模型看到的就是这段文本）。"""
    import scanner.api as api

    monkeypatch.setattr(
        api,
        "fetch_biaosheng",
        lambda session, size: [
            {"symbol": "NVDA", "name": "英伟达", "percent": -5.47},
            {"symbol": "AMD", "name": "超威半导体", "percent": 1.30},
            {"symbol": "SZ300548", "name": "长芯博创", "percent": 7.95},
        ],
    )

    class _Ctx:
        def get_session(self):  # noqa: D102 - 测试桩
            return object()

        def cached(self, key, fn):  # noqa: D102 - 测试桩：直接执行
            return fn()

    out = _tool_get_board_rank({}, _Ctx())
    assert "不可用于论证 A 股个股的题材共振" in out
    assert "NVDA" in out  # 榜单内容照旧给，不藏数据
