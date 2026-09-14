"""决策层（scanner.decision）单元测试：市场门 / 类别先验 / 配额 / 落库闭环。"""

import sqlite3

import pytest

from scanner.decision import (
    DECISION_MAX_PICKS,
    build_and_persist_decision,
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
    """门开时：按 hit 率降序先验（rebound → kNF → momentum → new_face）输出，全局 ≤3。

    2026-09-14 口径统一：本表由 config_scoring.CATEGORY_HIT_RATE 派生。此前按
    **平均超额**排（core_dip 第一、momentum 永禁）。本用例同时锁住三件事：
    ① 顺序 = hit 率降序；② 每类配额生效；③ core_dip（hit 6.5% < 基准 7.8%）不在准入内——
    即便它被落库也不会进入决策层。
    """
    conn = _db()
    _seed_strong_day(conn)
    _seed_rec(conn, "2026-09-04", "SZ300001", "rebound", 80, name="反弹甲")
    _seed_rec(conn, "2026-09-04", "SZ300002", "rebound", 70, name="反弹乙")
    _seed_rec(conn, "2026-09-04", "SZ300003", "known_new_face", 50, name="老面孔")
    _seed_rec(conn, "2026-09-04", "SZ300004", "momentum", 99, name="动量票")
    _seed_rec(conn, "2026-09-04", "SZ300005", "new_face", 88, name="新面孔")
    _seed_rec(conn, "2026-09-04", "SZ300006", "core_dip", 77, name="低吸票")
    result = build_decision_picks(conn, today=TODAY)
    assert result["allowed"]
    syms = [p["symbol"] for p in result["picks"]]
    # rebound 配额 2（80/70）→ kNF 配额 1（50）占满全局配额 3；
    # momentum / new_face 在准入内但被配额截断（非被禁）；core_dip 不在准入集合。
    assert syms == ["SZ300001", "SZ300002", "SZ300003"]
    assert len(result["picks"]) <= DECISION_MAX_PICKS


def test_picks_momentum_admissible_not_banned():
    """★ momentum 不再「永禁」（2026-09-14 口径统一）：它 hit 10.0% > 基准 7.8%，
    单类别在场时应照常入选。此前用平均超额（-0.70%）把它整体剔除。"""
    conn = _db()
    _seed_strong_day(conn)
    _seed_rec(conn, "2026-09-04", "SZ300001", "momentum", 90, name="动量票")
    result = build_decision_picks(conn, today=TODAY)
    assert [p["symbol"] for p in result["picks"]] == ["SZ300001"]


def test_picks_known_new_face_score_ascending():
    """kNF 是分数反指（IC -0.167）：同类内按分数升序取头部（方向取
    categories.SCORE_DESCENDING_BY_CAT 单源，不再在此手写 "asc"）。"""
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
    _seed_rec(conn, "2026-09-04", "SZ300001", "rebound", 90, excluded=1)
    _seed_rec(conn, "2026-09-04", "SZ300002", "rebound", 80, percent=9.9)
    _seed_rec(conn, "2026-09-04", "SZ300003", "rebound", 70, percent=5.0)
    result = build_decision_picks(conn, today=TODAY)
    assert [p["symbol"] for p in result["picks"]] == ["SZ300003"]


def test_picks_empty_when_gate_closed():
    """门关时 picks 为空且给出空仓原因——空仓是合法输出。"""
    conn = _db()
    _seed_index(conn, [("2026-09-03", 0.5), ("2026-09-04", -2.0)])
    _seed_rec(conn, "2026-09-04", "SZ300001", "rebound", 90)
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
    _seed_rec(conn, "2026-09-04", "SZ300001", "rebound", 80, name="反弹甲")
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
    _seed_rec(conn, "2026-09-04", "SZ300001", "rebound", 80, name="反弹甲")
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
    _seed_rec_sb(conn, "SZ300001", "rebound", 90, -3.0)  # 高分但分时走弱 → 拦
    _seed_rec_sb(conn, "SZ300002", "rebound", 70, 5.0)  # 分时漂亮 → 入选
    result = build_decision_picks(conn, today=TODAY)
    assert [p["symbol"] for p in result["picks"]] == ["SZ300002"]
    assert result["beauty_blocked"] == 1


def test_decision_intraday_gate_zero_score_fail_open():
    """intraday_score=0.0（未评分默认值歧义）→ 按缺失 fail-open 不拦。"""
    conn = _db_sb()
    _seed_strong_day(conn)
    _seed_rec_sb(conn, "SZ300001", "rebound", 80, 0.0)
    result = build_decision_picks(conn, today=TODAY)
    assert [p["symbol"] for p in result["picks"]] == ["SZ300001"]
    assert result["beauty_blocked"] == 0


def test_decision_intraday_gate_blocks_all_reports_in_reason(monkeypatch):
    """门开：全部分时走弱 → 空仓且 gate_reason 标注分时门拦截数。"""
    import scanner.decision as dm

    monkeypatch.setattr(dm, "DECISION_INTRADAY_BEAUTY_ENABLED", True)
    conn = _db_sb()
    _seed_strong_day(conn)
    _seed_rec_sb(conn, "SZ300001", "rebound", 90, -2.0)
    _seed_rec_sb(conn, "SZ300002", "rebound", 80, -1.5)
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
    _seed_rec_sb(conn, "SZ300001", "rebound", 90, -3.0)
    result = build_decision_picks(conn, today=TODAY)
    assert [p["symbol"] for p in result["picks"]] == ["SZ300001"]
    assert result["beauty_blocked"] == 0


def test_decision_intraday_gate_no_sb_column_fail_open():
    """旧库无 score_breakdown 列：分时门整体跳过，决策行为不变（fail-open）。"""
    conn = _db()  # 无 score_breakdown 列
    _seed_strong_day(conn)
    _seed_rec(conn, TODAY, "SZ300001", "rebound", 80, name="反弹甲")
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
    _seed_rec_sb(conn, "SZ300001", "rebound", 90, -3.0)  # 拦
    _seed_rec_sb(conn, "SZ300002", "rebound", 70, 5.0)  # 入选
    lines = decision_lines(conn, today=TODAY)
    assert any("分时门拦1只" in ln for ln in lines)
    assert any("1. SZ300002" in ln for ln in lines)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))


# ── 落库与渲染的职责分离（2026-09-13，测评 A2）──


def _decision_db() -> sqlite3.Connection:
    conn = _db()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS decision_picks ("
        " date TEXT NOT NULL, symbol TEXT NOT NULL, name TEXT, category TEXT,"
        " score REAL, percent REAL, reason TEXT, created TEXT,"
        " PRIMARY KEY (date, symbol))"
    )
    return conn


def _picks_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM decision_picks").fetchone()[0]


def test_decision_lines_does_not_persist():
    """★ 视图层零副作用：`decision_lines` 只算不写。

    原实现内部调 save_decision_picks，而唯一生产调用方是 display.build_scan_view
    ——一个自称"只算不画"的视图函数。后果：看一屏终端就写一次库，将来出 HTML
    报告也会顺带落库。落库现由主循环 build_and_persist_decision 显式负责。
    """
    from scanner.decision import decision_lines as _pure_lines

    conn = _decision_db()
    _seed_strong_day(conn)
    _seed_rec(conn, TODAY, "SZ300001", "rebound", 80, name="反弹甲")
    before = _picks_count(conn)

    lines = _pure_lines(conn, today=TODAY)

    assert _picks_count(conn) == before, "decision_lines 不得写库"
    assert any("SZ300001" in ln for ln in lines), "不落库也要正常渲染"


def test_decision_lines_works_without_picks_table():
    """纯渲染路径不应依赖 decision_picks 表存在（建表前调用也不炸）。"""
    conn = _db()  # 无 decision_picks 表
    _seed_strong_day(conn)
    _seed_rec(conn, TODAY, "SZ300001", "rebound", 80)
    lines = decision_lines(conn, today=TODAY)
    assert any("SZ300001" in ln for ln in lines)


def test_build_and_persist_decision_writes_db():
    """主循环用的那个：构建 + 落库 + 渲染，三者都要发生。"""
    conn = _decision_db()
    _seed_strong_day(conn)
    _seed_rec(conn, TODAY, "SZ300001", "rebound", 80, name="反弹甲")

    lines = build_and_persist_decision(conn, today=TODAY)

    assert _picks_count(conn) > 0, "build_and_persist_decision 必须落库"
    syms = {r[0] for r in conn.execute("SELECT symbol FROM decision_picks")}
    assert "__gate__" in syms and "SZ300001" in syms
    assert any("SZ300001" in ln for ln in lines)


def test_build_and_persist_decision_is_idempotent():
    """连调两次不产生重复行（INSERT OR REPLACE 语义）。"""
    conn = _decision_db()
    _seed_strong_day(conn)
    _seed_rec(conn, TODAY, "SZ300001", "rebound", 80)
    build_and_persist_decision(conn, today=TODAY)
    n1 = _picks_count(conn)
    build_and_persist_decision(conn, today=TODAY)
    assert _picks_count(conn) == n1


def test_persist_failure_still_renders():
    """落库失败不影响展示（fail-open）：save_decision_picks 捕获 EXTERNAL_FAILURES 仅告警。

    构造真实失败而非打桩替换函数——不建 decision_picks 表，executemany 抛
    `no such table`（sqlite3.OperationalError ∈ EXTERNAL_FAILURES）。
    打桩替换整个 save_decision_picks 会绕过它内部的 try，测不到真实路径。
    """
    conn = _db()  # 故意没有 decision_picks 表
    _seed_strong_day(conn)
    _seed_rec(conn, TODAY, "SZ300001", "rebound", 80)
    lines = build_and_persist_decision(conn, today=TODAY)
    assert any("SZ300001" in ln for ln in lines), "落库失败也要正常渲染"


def test_render_decision_lines_is_pure():
    """渲染是纯函数：输入 dict → 输出行，不碰 conn（可单测、可复用）。"""
    from scanner.decision import render_decision_lines

    lines = render_decision_lines(
        {"allowed": True, "gate_reason": "大盘门开", "beauty_blocked": 2,
         "picks": [{"symbol": "SZ300001", "name": "甲", "category": "rebound",
                    "score": 80.0, "percent": 3.0}]}
    )
    assert any("分时门拦2只" in ln for ln in lines)
    assert any("1. SZ300001" in ln and "+3.0%" in ln for ln in lines)

    empty = render_decision_lines(
        {"allowed": False, "gate_reason": "大盘门未开", "beauty_blocked": 0, "picks": []}
    )
    assert any("空仓" in ln for ln in empty)
    assert not any("SZ" in ln for ln in empty)


def test_render_handles_missing_percent():
    """percent 为 None（缺行情）渲染成 —，不是崩溃或 0.0%。"""
    from scanner.decision import render_decision_lines

    lines = render_decision_lines(
        {"allowed": True, "gate_reason": "", "beauty_blocked": 0,
         "picks": [{"symbol": "SZ300001", "name": "甲", "category": "rebound",
                    "score": 80.0, "percent": None}]}
    )
    assert any("—" in ln for ln in lines)
