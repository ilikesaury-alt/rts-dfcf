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


# 测试锚定日（2026-09-06 修复）：种子日期硬编码在此日，所有 build_decision_picks/
# decision_lines/save_decision_picks 调用必须显式传 today=TODAY——这些函数默认取
# 真实今日，时钟一走种子就失效（2026-09-05 实测 5 个测试随日期漂移失败，
# 项目纪律「禁止依赖天真本地时钟」的测试版）。
TODAY = "2026-09-04"


def _seed_index(conn: sqlite3.Connection, days_pct: list[tuple[str, float]]) -> None:
    conn.executemany("INSERT OR REPLACE INTO market_index_log (date, index_pct) VALUES (?, ?)", days_pct)
    conn.commit()


def _seed_rec(
    conn: sqlite3.Connection,
    date: str,
    sym: str,
    cat: str,
    score: float,
    percent: float = 3.0,
    name: str = "",
    excluded: int = 0,
) -> None:
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
    """基准缺失：fail-closed（决策层与主流程 fail-open 语义相反）。"""
    conn = _db()
    allowed, reason = market_gate(conn)
    assert not allowed
    assert "无基准" in reason


# ── 决策层选票 ──


def _seed_strong_day(conn: sqlite3.Connection, date: str = "2026-09-04") -> None:
    _seed_index(
        conn, [("2026-08-28", 0.5), ("2026-08-31", -0.2), ("2026-09-01", 0.3), ("2026-09-03", 0.6), (date, 1.0)]
    )


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
    result = build_decision_picks(conn, today=TODAY)
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
    result = build_decision_picks(conn, today=TODAY)
    assert [p["symbol"] for p in result["picks"]] == ["SZ300002"]


def test_picks_excluded_and_chase_filtered():
    """excluded=1 与超帽（>8% 不追涨）的票不进决策层。"""
    conn = _db()
    _seed_strong_day(conn)
    _seed_rec(conn, "2026-09-04", "SZ300001", "core_dip", 90, excluded=1)
    _seed_rec(conn, "2026-09-04", "SZ300002", "core_dip", 80, percent=9.9)
    _seed_rec(conn, "2026-09-04", "SZ300003", "core_dip", 70, percent=5.0)
    result = build_decision_picks(conn, today=TODAY)
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
    result = build_decision_picks(conn, today=TODAY)
    save_decision_picks(conn, result, today=TODAY)
    save_decision_picks(conn, result, today=TODAY)  # 幂等
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
    lines = decision_lines(conn, today=TODAY)
    assert any("1. SZ300001" in ln for ln in lines)


# ── 分时门（2026-09-09 扩展：低吸不接正在回落的刀）──


def _db_sb() -> sqlite3.Connection:
    """带 score_breakdown 列的库（分时门数据源；旧 _db 无该列 → 门 fail-open 跳过）。"""
    conn = _db()
    conn.execute("ALTER TABLE recommendations ADD COLUMN score_breakdown TEXT")
    return conn


def _seed_rec_sb(conn, sym, cat, score, intraday):
    """种推荐行 + 落库 score_breakdown（含 intraday_score）。"""
    import json

    _seed_rec(conn, TODAY, sym, cat, score)
    conn.execute(
        "UPDATE recommendations SET score_breakdown=? WHERE symbol=?",
        (json.dumps({"intraday_score": intraday}), sym),
    )
    conn.commit()


def test_decision_intraday_gate_blocks_weak_intraday(monkeypatch):
    """门开（默认已关，2026-09-09 数据裁决）：分时走弱（负分）的票不进决策推荐。"""
    import scanner.decision as dm

    monkeypatch.setattr(dm, "DECISION_INTRADAY_BEAUTY_ENABLED", True)
    conn = _db_sb()
    _seed_strong_day(conn)
    _seed_rec_sb(conn, "SZ300001", "core_dip", 90, -3.0)  # 高分但分时走弱 → 拦
    _seed_rec_sb(conn, "SZ300002", "core_dip", 70, 5.0)  # 分时漂亮 → 入选
    result = build_decision_picks(conn, today=TODAY)
    assert [p["symbol"] for p in result["picks"]] == ["SZ300002"]
    assert result["beauty_blocked"] == 1


def test_decision_intraday_gate_zero_score_fail_open():
    """intraday_score=0.0（未评分默认值歧义）→ 按缺失 fail-open 不拦。"""
    conn = _db_sb()
    _seed_strong_day(conn)
    _seed_rec_sb(conn, "SZ300001", "core_dip", 80, 0.0)
    result = build_decision_picks(conn, today=TODAY)
    assert [p["symbol"] for p in result["picks"]] == ["SZ300001"]
    assert result["beauty_blocked"] == 0


def test_decision_intraday_gate_blocks_all_reports_in_reason(monkeypatch):
    """门开：全部分时走弱 → 空仓且 gate_reason 标注分时门拦截数。"""
    import scanner.decision as dm

    monkeypatch.setattr(dm, "DECISION_INTRADAY_BEAUTY_ENABLED", True)
    conn = _db_sb()
    _seed_strong_day(conn)
    _seed_rec_sb(conn, "SZ300001", "core_dip", 90, -2.0)
    _seed_rec_sb(conn, "SZ300002", "core_dip", 80, -1.5)
    result = build_decision_picks(conn, today=TODAY)
    assert result["picks"] == []
    assert result["beauty_blocked"] == 2
    assert "分时门拦2只" in result["gate_reason"]


def test_decision_intraday_gate_kill_switch():
    """2026-09-09 数据裁决后分时门默认关：走弱票不拦不评（beauty_blocked=0）。

    实测分时≤-3 桶 hit 全场最高（11.1%/11.2%），「不接回落刀」对次日大涨口径被
    证伪。重开：RTS_DECISION_BEAUTY_INTRADAY=1。
    """
    conn = _db_sb()
    _seed_strong_day(conn)
    _seed_rec_sb(conn, "SZ300001", "core_dip", 90, -3.0)
    result = build_decision_picks(conn, today=TODAY)
    assert [p["symbol"] for p in result["picks"]] == ["SZ300001"]
    assert result["beauty_blocked"] == 0


def test_decision_intraday_gate_no_sb_column_fail_open():
    """旧库无 score_breakdown 列：分时门整体跳过，决策行为不变（fail-open）。"""
    conn = _db()  # 无 score_breakdown 列
    _seed_strong_day(conn)
    _seed_rec(conn, TODAY, "SZ300001", "core_dip", 80, name="低吸甲")
    result = build_decision_picks(conn, today=TODAY)
    assert [p["symbol"] for p in result["picks"]] == ["SZ300001"]
    assert result["beauty_blocked"] == 0


def test_decision_lines_mention_intraday_block(monkeypatch):
    """门开：渲染层输出分时门拦截提示行。"""
    import scanner.decision as dm

    monkeypatch.setattr(dm, "DECISION_INTRADAY_BEAUTY_ENABLED", True)
    conn = _db_sb()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS decision_picks ("
        " date TEXT NOT NULL, symbol TEXT NOT NULL, name TEXT, category TEXT,"
        " score REAL, percent REAL, reason TEXT, created TEXT,"
        " PRIMARY KEY (date, symbol))"
    )
    _seed_strong_day(conn)
    _seed_rec_sb(conn, "SZ300001", "core_dip", 90, -3.0)  # 拦
    _seed_rec_sb(conn, "SZ300002", "core_dip", 70, 5.0)  # 入选
    lines = decision_lines(conn, today=TODAY)
    assert any("分时门拦1只" in ln for ln in lines)
    assert any("1. SZ300002" in ln for ln in lines)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
