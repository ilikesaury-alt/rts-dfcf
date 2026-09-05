"""triple_barrier（三重屏障标签生成器）单元测试。

覆盖：样本口径（去重/排除）、止盈先触、止损先触、时间到期、同日双触保守跳过、
后续行情不足跳过、幂等重建、报告不崩。
"""

import sqlite3

import pytest

from scanner import triple_barrier as tb


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE recommendations (
        id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, time TEXT, symbol TEXT, name TEXT,
        category TEXT, score REAL, percent REAL, trend TEXT, score_breakdown TEXT,
        source TEXT, concept TEXT, accumulated_pct REAL, stale_kline INTEGER DEFAULT 0,
        excluded INTEGER DEFAULT 0, excluded_reason TEXT DEFAULT '',
        next_day_pct REAL, cum_2d REAL, cum_3d REAL, fwd_3d REAL, fwd_5d REAL)""")
    c.execute("""CREATE TABLE daily_kline (
        symbol TEXT NOT NULL, timestamp INTEGER, date TEXT NOT NULL, open REAL, close REAL,
        high REAL, low REAL, volume REAL, percent REAL, finalized INTEGER DEFAULT 1,
        PRIMARY KEY(symbol, date))""")
    c.execute("""CREATE TABLE triple_barrier_labels (
        date TEXT NOT NULL, symbol TEXT NOT NULL, category TEXT NOT NULL,
        label INTEGER, touch_date TEXT, touch_pct REAL, ret_at_horizon REAL,
        buy_price REAL, updated TEXT DEFAULT '',
        PRIMARY KEY (date, symbol, category))""")
    yield c
    c.close()


def _add_rec(conn, date, sym, cat="momentum", ndp=2.0, excluded=0):
    conn.execute(
        "INSERT INTO recommendations (date, time, symbol, name, category, score, excluded, next_day_pct) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (date, "10:00", sym, "票" + sym, cat, 50.0, excluded, ndp),
    )
    conn.commit()


def _add_bars(conn, sym, bars):
    """bars: [(date, open, high, low, close), ...]"""
    conn.executemany(
        "INSERT INTO daily_kline (symbol, date, open, high, low, close) VALUES (?,?,?,?,?,?)",
        [(sym, d, o, h, lo, c) for d, o, h, lo, c in bars],
    )
    conn.commit()


def _base_bars(conn, sym="SZ300001", t="2026-03-02", buy=10.0):
    """信号日 T（close=10）+ 后续 3 根中性 bar（high/low 不触 ±7%/-5% 屏障）。"""
    _add_bars(
        conn,
        sym,
        [
            (t, 10.0, 10.3, 9.8, buy),
            ("2026-03-03", 10.0, 10.5, 9.8, 10.2),
            ("2026-03-04", 10.2, 10.6, 9.9, 10.3),
            ("2026-03-05", 10.3, 10.6, 10.0, 10.4),
            ("2026-03-06", 10.4, 10.7, 10.1, 10.5),
        ],
    )


def test_take_profit_first_touch(conn):
    """T+1 high ≥ +7% → label=+1，touch 记录首触日。"""
    _add_rec(conn, "2026-03-02", "SZ300001")
    _add_bars(
        conn,
        "SZ300001",
        [
            ("2026-03-02", 10.0, 10.3, 9.8, 10.0),
            ("2026-03-03", 10.0, 10.8, 9.8, 10.2),  # high 10.8 ≥ 10.7 止盈线
            ("2026-03-04", 10.2, 10.6, 9.9, 10.3),
            ("2026-03-05", 10.3, 10.6, 10.0, 10.4),
            ("2026-03-06", 10.4, 10.7, 10.1, 10.5),
        ],
    )
    written, skipped = tb.build_labels(conn)
    assert (written, skipped) == (1, 0)
    row = conn.execute("SELECT * FROM triple_barrier_labels").fetchone()
    assert row["label"] == 1 and row["touch_date"] == "2026-03-03"
    assert row["buy_price"] == 10.0
    assert abs(row["touch_pct"] - 8.0) < 1e-9  # (10.8/10 - 1)*100


def test_stop_loss_first_touch(conn):
    """T+1 low ≤ -5% → label=-1。"""
    _add_rec(conn, "2026-03-02", "SZ300001")
    _add_bars(
        conn,
        "SZ300001",
        [
            ("2026-03-02", 10.0, 10.3, 9.8, 10.0),
            ("2026-03-03", 10.0, 10.2, 9.4, 9.8),  # low 9.4 ≤ 9.5 止损线
            ("2026-03-04", 10.2, 10.6, 9.9, 10.3),
            ("2026-03-05", 10.3, 10.6, 10.0, 10.4),
            ("2026-03-06", 10.4, 10.7, 10.1, 10.5),
        ],
    )
    tb.build_labels(conn)
    row = conn.execute("SELECT * FROM triple_barrier_labels").fetchone()
    assert row["label"] == -1 and row["touch_date"] == "2026-03-03"
    assert abs(row["touch_pct"] - (-6.0)) < 1e-9


def test_time_horizon_expiry(conn):
    """3 日内未触屏障 → label=0，ret_at_horizon = 第3根 close 收益。"""
    _add_rec(conn, "2026-03-02", "SZ300001")
    _base_bars(conn)  # 10.0 → 第3根（03-05）close 10.4 = +4%
    tb.build_labels(conn)
    row = conn.execute("SELECT * FROM triple_barrier_labels").fetchone()
    assert row["label"] == 0 and row["touch_date"] is None
    assert abs(row["ret_at_horizon"] - 4.0) < 1e-9


def test_same_day_double_touch_skipped(conn):
    """同根 bar 双触（high≥+7% 且 low≤-5%）→ 保守跳过。"""
    _add_rec(conn, "2026-03-02", "SZ300001")
    _add_bars(
        conn,
        "SZ300001",
        [
            ("2026-03-02", 10.0, 10.3, 9.8, 10.0),
            ("2026-03-03", 10.0, 10.9, 9.3, 10.2),  # 双触
            ("2026-03-04", 10.2, 10.6, 9.9, 10.3),
            ("2026-03-05", 10.3, 10.6, 10.0, 10.4),
            ("2026-03-06", 10.4, 10.7, 10.1, 10.5),
        ],
    )
    written, skipped = tb.build_labels(conn)
    assert (written, skipped) == (0, 1)
    assert conn.execute("SELECT COUNT(*) FROM triple_barrier_labels").fetchone()[0] == 0


def test_insufficient_future_bars_skipped(conn):
    """后续行情不足 horizon → 跳过（样本太新）。"""
    _add_rec(conn, "2026-03-02", "SZ300001")
    _add_bars(
        conn,
        "SZ300001",
        [
            ("2026-03-02", 10.0, 10.3, 9.8, 10.0),
            ("2026-03-03", 10.0, 10.5, 9.8, 10.2),  # 只有 2 根 < 3
            ("2026-03-04", 10.2, 10.6, 9.9, 10.3),
        ],
    )
    written, skipped = tb.build_labels(conn)
    assert (written, skipped) == (0, 1)


def test_dedup_last_round_and_excluded(conn):
    """同票同日多轮取最后一轮（跨类别）；excluded=1 不进样本。"""
    _add_rec(conn, "2026-03-02", "SZ300001", cat="momentum", ndp=2.0)
    _add_rec(conn, "2026-03-02", "SZ300001", cat="rebound", ndp=2.0)  # 同票同日第二轮
    _add_rec(conn, "2026-03-02", "SZ300002", excluded=1)
    _base_bars(conn)
    _base_bars(conn, sym="SZ300002")
    written, _ = tb.build_labels(conn)
    assert written == 1
    row = conn.execute("SELECT symbol, category FROM triple_barrier_labels").fetchone()
    assert (row["symbol"], row["category"]) == ("SZ300001", "rebound")  # 最后一轮的类别


def test_idempotent_rebuild(conn):
    """全量重建幂等：重复跑不产生重复行。"""
    _add_rec(conn, "2026-03-02", "SZ300001")
    _base_bars(conn)
    tb.build_labels(conn)
    tb.build_labels(conn)
    assert conn.execute("SELECT COUNT(*) FROM triple_barrier_labels").fetchone()[0] == 1


def test_report_no_crash(conn):
    """一致性报告在空表/有数据时均不崩。"""
    tb.print_report(conn)  # 空表 → 提示行
    _add_rec(conn, "2026-03-02", "SZ300001", ndp=8.0)
    _add_bars(
        conn,
        "SZ300001",
        [
            ("2026-03-02", 10.0, 10.3, 9.8, 10.0),
            ("2026-03-03", 10.0, 10.8, 9.8, 10.2),
            ("2026-03-04", 10.2, 10.6, 9.9, 10.3),
            ("2026-03-05", 10.3, 10.6, 10.0, 10.4),
            ("2026-03-06", 10.4, 10.7, 10.1, 10.5),
        ],
    )
    tb.build_labels(conn)
    tb.print_report(conn)  # 有数据 → 打表
