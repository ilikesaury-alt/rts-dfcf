"""scanner.ranking 单源不变量（设计审查 P0 #1）。

防止档位/🎯 排序逻辑再次散落为 display 内的副本：
- ranking 必须定义全部排序纯函数；
- display 必须 re-export **同一个对象**（不是重新实现一份）；
  一旦有人在 display 里又写 `def _entry_tier`，`display._entry_tier is
  ranking._entry_tier` 即变 False，本测试报警，挡住口径漂移。
"""
import pytest

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


import sqlite3
from scanner.ranking import build_accum_map, _nextday_entry_accum


def _mk_db():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE daily_kline ("
        " symbol TEXT NOT NULL, date TEXT NOT NULL, open REAL,"
        " close REAL, high REAL, low REAL, volume REAL, percent REAL,"
        " PRIMARY KEY(symbol, date))"
    )
    return conn


def _insert_kline(conn, sym, rows):
    # rows: list of (date, close, percent)
    for dt, close, pct in rows:
        conn.execute(
            "INSERT OR REPLACE INTO daily_kline"
            " (symbol, date, open, close, high, low, volume, percent)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (sym, dt, close, close, close, close, 0, pct),
        )


def _entry(sym, rec_date, accumulated_pct=None):
    e = {"symbol": sym, "date": rec_date, "category": "momentum", "score": 50}
    if accumulated_pct is not None:
        e["accumulated_pct"] = accumulated_pct
    return e


class TestBuildAccumMap:
    """P1-9：build_accum_map 单批次回放 ≡ 逐行 _nextday_entry_accum，且保留 DB 落库兜底。"""

    def test_matches_per_row_replay(self):
        conn = _mk_db()
        closes = [10.0, 10.5, 11.0, 11.5, 12.0, 12.6]
        pcts = [0.0, 5.0, 5.0, 5.0, 5.0, 5.0]
        dates = [f"2026-08-1{i}" for i in range(1, 7)]
        _insert_kline(conn, "SZ300001", list(zip(dates, closes, pcts)))
        entries = [_entry("SZ300001", "2026-08-16")]
        batch = build_accum_map(conn, entries)
        per_row = _nextday_entry_accum(entries[0], conn)
        assert batch["SZ300001"] == per_row
        assert batch["SZ300001"] == pytest.approx(26.0)  # (12.6-10.0)/10.0*100

    def test_db_fallback_when_no_kline(self):
        conn = _mk_db()  # 无 daily_kline 行
        entries = [_entry("SZ300002", "2026-08-16", accumulated_pct=12.0)]
        batch = build_accum_map(conn, entries)
        # 回放无数据 → 兜底 DB 落库 accumulated_pct
        assert batch["SZ300002"] == 12.0

    def test_candidate_row_uses_dimensions(self):
        conn = _mk_db()
        cand = type("C", (), {})()
        kline = type("K", (), {})()
        kline.dimensions = {"accumulated_incl_today": 8.5}
        kline.accumulated_pct = 99.0
        cand.kline = kline
        e = {"symbol": "SZ300003", "date": "2026-08-16", "category": "momentum",
              "score": 50, "_candidate": cand}
        batch = build_accum_map(conn, [e])
        assert batch["SZ300003"] == 8.5  # 维度优先，不查 DB
