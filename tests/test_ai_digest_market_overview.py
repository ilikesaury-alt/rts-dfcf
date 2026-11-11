"""`get_market_overview` 输出口径的守卫（2026-11-11）。

## 背景：一次真实事故

实测（2026-11-11 22:00）模型在报告结尾写：

> 「其余多为跟随 **287 只样本**普涨（上涨 77/下跌 13、涨停 84 家、创业板指+3.11%）」

而工具当时给的是 `共 92（覆盖 92 只）`。287 **既不在工具输出里、也不在提示词里**
—— 是模型自己补的，被幻觉巡检捕获（`unverified_numbers: ['287']`）。

### 根因不是模型想编，是原表述给了「这数不完整」的暗示

`共 92（覆盖 92 只）` 里「覆盖」一词容易被读成「样本覆盖不全」；而 A 股有 5000+ 只，
92 看着就像个残缺抽样 ⇒ 模型顺手「补全」成一个更像全市场的数。

### 修法：把「这是什么样本」写清楚，并把它需要的派生值算好

1. 明说 `样本=本地库当日有 K 线的票，非全市场家数`；
2. **补上平盘数** —— 否则模型自己算 `77+13=90≠92` 会以为数据有矛盾（进而"修正"它）；
3. **直接给上涨率** —— 别让模型自己除，它算出来的数会被巡检判编造
   （与「721 万」那类误报同源）。

## 顺带修的另一个坑

原表述 `涨停池：{n} 家（连板数据见下）` —— **下面并没有任何连板数据**，
`fetch_zt_pool` 的 `lianban`/`zt_stat` 从未被输出。模型会去找一段不存在的内容。
现在直接把连板分布算出来。
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from scanner.ai_digest.research import _tool_get_market_overview


@pytest.fixture()
def conn() -> sqlite3.Connection:
    """4 涨 / 1 跌 / 1 平 = 6 只（含平盘，用来验证加总自洽）。"""
    c = sqlite3.connect(":memory:")
    c.execute(
        "CREATE TABLE daily_kline (symbol TEXT, date TEXT, open REAL, close REAL, "
        "high REAL, low REAL, volume REAL, percent REAL, finalized INT)"
    )
    rows = [
        ("SZ300001", 5.0),
        ("SZ300002", 3.0),
        ("SZ300003", 2.0),
        ("SZ300004", 1.0),
        ("SZ300005", -2.0),
        ("SZ300006", 0.0),
    ]
    for sym, pct in rows:
        c.execute(
            "INSERT INTO daily_kline VALUES (?,?,?,?,?,?,?,?,1)",
            (sym, "2026-11-11", 10.0, 10.0, 10.0, 10.0, 1000.0, pct),
        )
    c.commit()
    return c


class _Ctx:
    """最小 ResearchContext 桩：只提供 conn 与 cached（直接执行）。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def conn_for_thread(self) -> sqlite3.Connection:
        return self._conn

    def cached(self, key: str, fn: Any) -> Any:
        return fn()

    def get_session(self) -> Any:
        raise OSError("测试桩：不联网")


def test_includes_flat_count(conn: sqlite3.Connection) -> None:
    """必须给出**平盘**数 —— 否则模型自己算 4+1=5≠6，会以为数据矛盾进而"修正"它。"""
    out = _tool_get_market_overview({}, _Ctx(conn))
    assert "平盘 1" in out
    assert "上涨 4" in out
    assert "下跌 1" in out
    assert "合计 6" in out


def test_includes_up_ratio(conn: sqlite3.Connection) -> None:
    """上涨率由工具算好（4/6 = 66.7%），不让模型自己除。"""
    out = _tool_get_market_overview({}, _Ctx(conn))
    assert "上涨率 66.7%" in out


def test_states_sample_scope(conn: sqlite3.Connection) -> None:
    """必须说明这是**本地样本**、不是全市场家数。

    这是 287 事故的直接防线：模型看到「92 只」会以为是残缺抽样而自行补全，
    写明「非全市场家数」后它没有"补全"的动机。
    """
    out = _tool_get_market_overview({}, _Ctx(conn))
    assert "非全市场家数" in out
    assert "本地库" in out


def test_zt_pool_has_no_dangling_reference(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    """涨停池**不许**再写「连板数据见下」—— 下面没有任何连板数据。

    这条是回归守卫：旧文案让模型去找一段不存在的内容，它会转而自己编。
    """
    import scanner.market_extra as me

    monkeypatch.setattr(
        me,
        "fetch_zt_pool",
        lambda: {
            "600001": {"lianban": 1, "zt_stat": "首板"},
            "600002": {"lianban": 1, "zt_stat": "首板"},
            "600003": {"lianban": 3, "zt_stat": "3连板"},
        },
    )
    out = _tool_get_market_overview({}, _Ctx(conn))
    assert "见下" not in out
    # 连板分布要**真的给出来**（情绪温度看的是几进几，不是总家数）
    assert "3板1家" in out
    assert "首板2家" in out


def test_zt_pool_empty_does_not_crash(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    """涨停池取不到时给一句人话，不抛。"""
    import scanner.market_extra as me

    monkeypatch.setattr(me, "fetch_zt_pool", lambda: {})
    out = _tool_get_market_overview({}, _Ctx(conn))
    assert "涨停池：取不到" in out


def test_empty_kline_still_returns_text() -> None:
    """库是空的也要返回可读文本（不能抛、不能空串）。"""
    c = sqlite3.connect(":memory:")
    c.execute(
        "CREATE TABLE daily_kline (symbol TEXT, date TEXT, open REAL, close REAL, "
        "high REAL, low REAL, volume REAL, percent REAL, finalized INT)"
    )
    out = _tool_get_market_overview({}, _Ctx(c))
    assert out.strip()
    assert "市场概览" in out
