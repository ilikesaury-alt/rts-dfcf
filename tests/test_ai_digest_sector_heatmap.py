"""`get_sector_heatmap` 成分股样本完整性的守卫（2026-11-12）。

## 问题：样本残缺被当成板块真相

原实现只读 `daily_kline` 的 `MAX(date)` 行。实测（11-12 盘中）：

| 概念 | 成分股 | 当日有本地数据 | 覆盖率 |
|---|---|---|---|
| 人工智能 | 208 | 10 | **4.8%** |
| 传媒 | 41 | 7 | 17% |
| 游戏概念 | 7 | 0 | **0%** |

输出却写「概念『人工智能』**当日** 7 只有本地数据」，模型读到的是「这个板块今天
只有 7 只涨跌」。

**后果不是"少了点信息"，而是结论反向**：

- 修复前那 7 只恰好含涨幅榜前列（`SZ301171 +5.77%`、`SZ300030 +5.32%`），
  模型会判「AI 板块强势」；
- 补齐全 208 只后真实情况是 **上涨 66 只 = 32%**，AI 板块整体偏弱。

「游戏概念」更糟：7 只全部陈旧 ⇒ 直接返回「本地无成分股当日数据」，
模型会认为该概念没有成分股。

## 修法

成分股名单来自 `concept_cache`（不随时间变），涨跌分两部分：
本地当日有就用，缺的走**雪球批量报价**（50 只/请求，实测 208 只用 1.6 秒）。
输出里给出「上涨 X 只（Y%）」—— 板块共振的可比读数，且由工具算好（不让模型自己数，
模型计数会被巡检判编造）。
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

import scanner.api as api_mod
from scanner.ai_digest.research import _tool_get_sector_heatmap


@pytest.fixture()
def conn() -> sqlite3.Connection:
    """3 只成分股：1 只有今日本地数据，2 只没有（须联网补）。"""
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE daily_kline (symbol TEXT, date TEXT, percent REAL, close REAL)")
    c.execute("CREATE TABLE concept_cache (symbol TEXT, concepts TEXT, updated TEXT)")
    c.execute(
        "INSERT INTO concept_cache VALUES (?,?,?)",
        ("SZ300001", '["人工智能","算力"]', "2026-11-12T09:00:00+08:00"),
    )
    c.execute(
        "INSERT INTO concept_cache VALUES (?,?,?)",
        ("SZ300002", '["人工智能"]', "2026-11-12T09:00:00+08:00"),
    )
    c.execute(
        "INSERT INTO concept_cache VALUES (?,?,?)",
        ("SZ300003", '["人工智能"]', "2026-11-12T09:00:00+08:00"),
    )
    # 只有 SZ300001 有当日数据
    c.execute("INSERT INTO daily_kline VALUES (?,?,?,?)", ("SZ300001", "2026-11-12", 5.0, 10.0))
    c.execute("INSERT INTO daily_kline VALUES (?,?,?,?)", ("SZ300002", "2026-10-01", 99.0, 10.0))
    c.commit()
    return c


class _Ctx:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def conn_for_thread(self) -> sqlite3.Connection:
        return self._conn

    def cached(self, key: str, fn: Any) -> Any:
        return fn()

    def get_session(self) -> Any:
        return object()


def test_missing_members_are_backfilled(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    """本地缺的成分股必须联网补齐，不能只报「有本地数据的那些」。"""
    calls: list[list[str]] = []

    def fake_batch(session: Any, syms: list[str], batch_size: int = 50) -> dict[str, dict]:
        calls.append(list(syms))
        return {s: {"percent": -2.0} for s in syms}

    monkeypatch.setattr(api_mod, "fetch_hot_quotes_batch", fake_batch)

    out = _tool_get_sector_heatmap({"concept": "人工智能"}, _Ctx(conn))

    assert calls, "本地缺成分股时应联网"
    assert len(calls[0]) == 2, f"应只补缺的 2 只，实际 {calls[0]}"
    # 3 只全都在（1 本地 + 2 补充）
    assert "成分股 3 只" in out
    assert "取到当日涨跌 3 只" in out


def test_up_count_is_computed_by_tool(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    """上涨家数与占比由**工具**算好 —— 不让模型自己数（它会算错，且巡检判编造）。"""
    monkeypatch.setattr(
        api_mod,
        "fetch_hot_quotes_batch",
        lambda session, syms, batch_size=50: {s: {"percent": -2.0} for s in syms},
    )
    out = _tool_get_sector_heatmap({"concept": "人工智能"}, _Ctx(conn))
    # 1 涨（+5.0）+ 2 跌（-2.0）⇒ 上涨 1 只 / 33%
    assert "上涨 1 只（33%）" in out


def test_stale_local_row_not_used_as_today(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    """本地陈旧行（10-01）**不能**被当成当日涨幅用。

    这是这个 bug 的核心：`daily_kline` 里躺着的旧行看起来"有数据"，
    但它不是今天的涨跌。
    """
    monkeypatch.setattr(
        api_mod,
        "fetch_hot_quotes_batch",
        lambda session, syms, batch_size=50: {s: {"percent": 1.0} for s in syms},
    )
    out = _tool_get_sector_heatmap({"concept": "人工智能"}, _Ctx(conn))
    # SZ300002 的旧值 +99.0 不该出现（那会是"大涨"的假信号）
    assert "+99.00%" not in out
    assert "SZ300002 +1.00%" in out


def test_network_failure_degrades_loudly(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    """联网补失败 ⇒ 用本地能给的部分，但**明说补齐失败**（不能让模型以为样本完整）。"""

    def boom(session: Any, syms: list[str], batch_size: int = 50) -> dict[str, dict]:
        raise OSError("网络挂了")

    monkeypatch.setattr(api_mod, "fetch_hot_quotes_batch", boom)
    out = _tool_get_sector_heatmap({"concept": "人工智能"}, _Ctx(conn))

    assert "联网补齐失败" in out
    assert "成分股 3 只" in out
    assert "取到当日涨跌 1 只" in out  # 只有本地那 1 只


def test_unknown_concept_says_so(conn: sqlite3.Connection) -> None:
    """概念名不匹配 ⇒ 明说「无成分股名单」（区别于「有名单但无数据」）。"""
    out = _tool_get_sector_heatmap({"concept": "不存在的概念"}, _Ctx(conn))
    assert "无该概念的成分股名单" in out


def test_all_members_fail_returns_plain_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    """本地无数据 + 联网也空 ⇒ 给一句人话，不返回空串。"""
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE daily_kline (symbol TEXT, date TEXT, percent REAL, close REAL)")
    c.execute("CREATE TABLE concept_cache (symbol TEXT, concepts TEXT, updated TEXT)")
    c.execute("INSERT INTO concept_cache VALUES (?,?,?)", ("SZ300001", '["人工智能"]', "x"))
    c.execute("INSERT INTO daily_kline VALUES (?,?,?,?)", ("SZ300001", "2026-10-01", 1.0, 10.0))
    c.commit()
    monkeypatch.setattr(
        api_mod, "fetch_hot_quotes_batch", lambda session, syms, batch_size=50: {}
    )
    out = _tool_get_sector_heatmap({"concept": "人工智能"}, _Ctx(c))
    assert out.strip()
    assert "无当日涨跌数据" in out


def test_partial_backfill_is_flagged_incomplete(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """只补到一部分也要**明说样本仍不完整**。

    「本地缺 2 只，补齐 0 只」如果写成普通语气，模型会读成「已经补过了」，
    进而把 1 只的样本当成整个板块 —— 这正是本模块要根治的失效方式。
    """
    monkeypatch.setattr(
        api_mod, "fetch_hot_quotes_batch", lambda session, syms, batch_size=50: {}
    )
    out = _tool_get_sector_heatmap({"concept": "人工智能"}, _Ctx(conn))
    assert "样本仍不完整" in out
    assert "取到当日涨跌 1 只" in out


def test_missing_concept_param() -> None:
    """缺参数 ⇒ 参数错误提示。"""
    out = _tool_get_sector_heatmap({}, _Ctx(sqlite3.connect(":memory:")))
    assert "参数错误" in out
