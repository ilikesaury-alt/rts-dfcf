"""市场择时门（scanner.decision.market_gate）单元测试。

历史：本文件原为「决策层」全套测试（市场门 / 类别先验 / 配额 / 落库闭环，
约 450 行）。2026-09-14 决策层整体删除后，只保留择时门这一部分 —— 它是唯一
被留下的部件，且仍被终选参考区（scanner/final_pick）消费，故必须继续有守护。
被删除的测试覆盖的是 build_decision_picks / save_decision_picks /
render_decision_lines / decision_lines / build_and_persist_decision —— 这些函数
已不存在。需复原见 git 历史。
"""

import sqlite3

from scanner.decision import market_gate


def _db() -> sqlite3.Connection:
    """空库（含 market_index_log 表但无行）——「无基准数据」与「表缺失」是两条不同分支。"""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE market_index_log (date TEXT PRIMARY KEY, index_pct REAL)")
    return conn


def _seed_index(conn: sqlite3.Connection, days_pct: list[tuple[str, float]]) -> None:
    conn.executemany("INSERT OR REPLACE INTO market_index_log (date, index_pct) VALUES (?, ?)", days_pct)
    conn.commit()


def test_gate_closed_on_big_drop_day():
    """大跌日（指数 < 0）：市场门关闭，fail-closed。"""
    conn = _db()
    _seed_index(conn, [("2026-09-01", 0.5), ("2026-09-02", 0.3), ("2026-09-03", -0.2), ("2026-09-04", -1.8)])
    allowed, reason = market_gate(conn)
    assert not allowed
    assert "空仓" in reason


def test_gate_closed_on_weak_5d():
    """当日反弹但 5 日累计仍弱（<= -3%）：门关闭。"""
    conn = _db()
    _seed_index(
        conn,
        [("2026-08-28", -2.0), ("2026-08-31", -1.0), ("2026-09-01", -0.8), ("2026-09-03", -0.5), ("2026-09-04", 1.2)],
    )
    allowed, reason = market_gate(conn)
    assert not allowed
    assert "5日" in reason


def test_gate_open_on_strong_day():
    """强势日（>0 且 5日累计 > -3%）：门开。"""
    conn = _db()
    _seed_index(
        conn, [("2026-08-28", 0.5), ("2026-08-31", -0.2), ("2026-09-01", 0.3), ("2026-09-03", 0.6), ("2026-09-04", 1.0)]
    )
    allowed, reason = market_gate(conn)
    assert allowed


def test_gate_fail_closed_without_data():
    """基准缺失：fail-closed（与主流程 fail-open 语义相反——这里在保守侧）。"""
    conn = _db()
    allowed, reason = market_gate(conn)
    assert not allowed
    assert "无基准" in reason
