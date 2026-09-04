"""决策层（scanner.decision）单元测试：市场门 / 类别先验 / 配额 / 落库闭环。"""

import sqlite3

import pytest

from scanner.decision import (
    DECISION_MAX_PICKS,
    build_decision_picks,
    decision_lines,
    market_gate,
    save_decision_picks,
)


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE recommendations ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, time TEXT,"
        " symbol TEXT, name TEXT, category TEXT, score REAL, percent REAL,"
        " excluded INTEGER DEFAULT 0)"
    )
    conn.execute("CREATE TABLE market_index_log (date TEXT PRIMARY KEY, index_pct REAL)")
    return conn


def _seed_index(conn: sqlite3.Connection, days_pct: list[tuple[str, float]]) -> None:
    conn.executemany(
        "INSERT OR REPLACE INTO market_index_log (date, index_pct) VALUES (?, ?)", days_pct
    )
    conn.commit()


def _seed_rec(conn: sqlite3.Connection, date: str, sym: str, cat: str, score: float,
              percent: float = 3.0, name: str = "", excluded: int = 0) -> None:
    conn.execute(
        "INSERT INTO recommendations (date, time, symbol, name, category, score, percent, excluded)"
        " VALUES (?, '10:00:00', ?, ?, ?, ?, ?, ?)",
        (date, sym, name, cat, score, percent, excluded),
    )
    conn.commit()


# ── 市场门 ──

def test_gate_closed_on_big_drop_day():
    """大跌日（指数 < 0）：市场门关闭，fail-closed。"""
    conn = _db()
    _seed_index(conn, [("2026-09-01", 0.5), ("2026-09-02", 0.3),
                       ("2026-09-03", -0.2), ("2026-09-04", -1.8)])
    allowed, reason = market_gate(conn)
    assert not allowed
    assert "空仓" in reason


def test_gate_closed_on_weak_5d():
    """当日反弹但 5 日累计仍弱（<= -3%）：门关闭。"""
    conn = _db()
    _seed_index(conn, [("2026-08-28", -2.0), ("2026-08-31", -1.0),
                       ("2026-09-01", -0.8), ("2026-09-03", -0.5), ("2026-09-04", 1.2)])
    allowed, reason = market_gate(conn)
    assert not allowed
    assert "5日" in reason


def test_gate_open_on_strong_day():
    """强势日（>0 且 5日累计 > -3%）：门开。"""
    conn = _db()
    _seed_index(conn, [("2026-08-28", 0.5), ("2026-08-31", -0.2),
                       ("2026-09-01", 0.3), ("2026-09-03", 0.6), ("2026-09-04", 1.0)])
    allowed, reason = market_gate(conn)
    assert allowed


def test_gate_fail_closed_without_data():
    """基准缺失：fail-closed（决策层与主流程 fail-open 语义相反）。"""
    conn = _db()
    allowed, reason = market_gate(conn)
    assert not allowed
    assert "无基准" in reason


# ── 决策层选票 ──

def _seed_strong_day(conn: sqlite3.Connection, date: str = "2026-09-04") -> None:
    _seed_index(conn, [("2026-08-28", 0.5), ("2026-08-31", -0.2),
                       ("2026-09-01", 0.3), ("2026-09-03", 0.6), (date, 1.0)])


def test_picks_category_prior_order_and_cap():
    """门开时：按先验顺序（core_dip → kNF → rebound）输出，全局 ≤3。"""
    conn = _db()
    _seed_strong_day(conn)
    _seed_rec(conn, "2026-09-04", "SZ300001", "core_dip", 80, name="低吸甲")
    _seed_rec(conn, "2026-09-04", "SZ300002", "core_dip", 70, name="低吸乙")
    _seed_rec(conn, "2026-09-04", "SZ300003", "rebound", 90, name="反弹甲")
    _seed_rec(conn, "2026-09-04", "SZ300004", "rebound", 60, name="反弹乙")
    _seed_rec(conn, "2026-09-04", "SZ300005", "known_new_face", 50, name="老面孔")
    _seed_rec(conn, "2026-09-04", "SZ300006", "momentum", 99, name="动量禁入")
    result = build_decision_picks(conn)
    assert result["allowed"]
    syms = [p["symbol"] for p in result["picks"]]
    # 先验顺序：core_dip 配额 2（甲80/乙70）→ kNF(50)，rebound 被全局配额截掉
    assert syms == ["SZ300001", "SZ300002", "SZ300005"]
    assert len(result["picks"]) <= DECISION_MAX_PICKS


def test_picks_known_new_face_score_ascending():
    """kNF 是分数反指（IC -0.167）：同类内按分数升序取头部。"""
    conn = _db()
    _seed_strong_day(conn)
    _seed_rec(conn, "2026-09-04", "SZ300001", "known_new_face", 90)
    _seed_rec(conn, "2026-09-04", "SZ300002", "known_new_face", 40)
    result = build_decision_picks(conn)
    assert [p["symbol"] for p in result["picks"]] == ["SZ300002"]


def test_picks_excluded_and_chase_filtered():
    """excluded=1 与超帽（>8% 不追涨）的票不进决策层。"""
    conn = _db()
    _seed_strong_day(conn)
    _seed_rec(conn, "2026-09-04", "SZ300001", "core_dip", 90, excluded=1)
    _seed_rec(conn, "2026-09-04", "SZ300002", "core_dip", 80, percent=9.9)
    _seed_rec(conn, "2026-09-04", "SZ300003", "core_dip", 70, percent=5.0)
    result = build_decision_picks(conn)
    assert [p["symbol"] for p in result["picks"]] == ["SZ300003"]


def test_picks_empty_when_gate_closed():
    """门关时 picks 为空且给出空仓原因——空仓是合法输出。"""
    conn = _db()
    _seed_index(conn, [("2026-09-03", 0.5), ("2026-09-04", -2.0)])
    _seed_rec(conn, "2026-09-04", "SZ300001", "core_dip", 90)
    result = build_decision_picks(conn)
    assert not result["allowed"] and not result["picks"]
    assert "空仓" in result["gate_reason"]


# ── 落库闭环 ──

def test_save_decision_picks_persists_gate_and_rows():
    """落库含 __gate__ 状态行 + 入选行，幂等（重扫覆盖不重复）。"""
    conn = _db()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS decision_picks ("
        " date TEXT NOT NULL, symbol TEXT NOT NULL, name TEXT, category TEXT,"
        " score REAL, percent REAL, reason TEXT, created TEXT,"
        " PRIMARY KEY (date, symbol))"
    )
    _seed_strong_day(conn)
    _seed_rec(conn, "2026-09-04", "SZ300001", "core_dip", 80, name="低吸甲")
    result = build_decision_picks(conn)
    save_decision_picks(conn, result)
    save_decision_picks(conn, result)  # 幂等
    rows = conn.execute(
        "SELECT symbol, category FROM decision_picks WHERE date='2026-09-04' ORDER BY symbol"
    ).fetchall()
    syms = [r[0] for r in rows]
    assert "__gate__" in syms and "SZ300001" in syms
    assert len(syms) == len(set(syms)), "重复落库不应产生重复行"


# ── 渲染行 ──

def test_decision_lines_empty_state():
    """空仓态渲染：标题 + ✗ 空仓 + 原因，不出现个股行。"""
    conn = _db()
    _seed_index(conn, [("2026-09-03", 0.5), ("2026-09-04", -2.0)])
    lines = decision_lines(conn)
    assert any("决策层" in ln for ln in lines)
    assert any("空仓" in ln for ln in lines)
    assert not any("SZ" in ln for ln in lines)


def test_decision_lines_with_picks():
    """入选态渲染：标题 + 带序号个股行。"""
    conn = _db()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS decision_picks ("
        " date TEXT NOT NULL, symbol TEXT NOT NULL, name TEXT, category TEXT,"
        " score REAL, percent REAL, reason TEXT, created TEXT,"
        " PRIMARY KEY (date, symbol))"
    )
    _seed_strong_day(conn)
    _seed_rec(conn, "2026-09-04", "SZ300001", "core_dip", 80, name="低吸甲")
    lines = decision_lines(conn)
    assert any("1. SZ300001" in ln for ln in lines)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
