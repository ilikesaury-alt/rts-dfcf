"""ranking_snapshot 快照落库测试（2026-08-26）。

覆盖：round-trip（写→读一致）、幂等覆盖、主表序号与排序组合层一致、
独立区行 rank 为 NULL、无表/无数据回退空 dict、unified_scanner 写入器 fail-open。
"""
import json
import sqlite3

import pytest

import scanner.ranking as R
from scanner.ranking_snapshot import load_ranking_snapshot, persist_ranking_snapshot


def _mk_db(with_snapshot_table=True):
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE recommendations (
            date TEXT NOT NULL, time TEXT NOT NULL, symbol TEXT NOT NULL,
            name TEXT NOT NULL, category TEXT NOT NULL, score INTEGER NOT NULL,
            percent REAL, trend TEXT, next_day_pct REAL, fwd_3d REAL, fwd_5d REAL,
            score_breakdown TEXT, concept TEXT, accumulated_pct REAL,
            excluded INTEGER DEFAULT 0)
    """)
    conn.execute("""
        CREATE TABLE daily_kline (
            symbol TEXT NOT NULL, date TEXT NOT NULL, open REAL, close REAL,
            high REAL, low REAL, volume REAL, percent REAL,
            PRIMARY KEY(symbol, date))
    """)
    if with_snapshot_table:
        conn.execute("""
            CREATE TABLE ranking_snapshot (
                date TEXT NOT NULL, symbol TEXT NOT NULL, category TEXT NOT NULL,
                tier INTEGER NOT NULL, marked INTEGER NOT NULL, reasons_json TEXT,
                rank_in_table INTEGER, created TEXT NOT NULL,
                PRIMARY KEY(date, symbol, category))
        """)
    return conn


def _ins_rec(conn, sym, cat, percent, score=60, sb=None):
    conn.execute(
        "INSERT INTO recommendations (date, time, symbol, name, category, score, "
        "percent, score_breakdown) VALUES (?,?,?,?,?,?,?,?)",
        ("2026-08-25", "10:00", sym, "票" + sym[-3:], cat, score, percent,
         json.dumps(sb or {})),
    )


def _ins_kline(conn, sym):
    """6 根 K 线 → 5 日累计（含推荐日）≈ +26%（≥ NEXTDAY_ACCUM_MIN，可过 🎯 累计门槛）。"""
    closes = [10.0, 10.5, 11.0, 11.5, 12.0, 12.6]
    dates = [f"2026-08-{d}" for d in ("19", "20", "21", "22", "23", "24")]
    # 推荐日 2026-08-25，K 线到 08-24（T-1 及更早），accum 回放取 <= 推荐日窗口
    for dt, cl in zip(dates + ["2026-08-25"], closes + [13.2], strict=True):
        conn.execute(
            "INSERT INTO daily_kline (symbol, date, close, percent) VALUES (?,?,?,?)",
            (sym, dt, cl, 5.0),
        )


@pytest.fixture()
def db():
    conn = _mk_db()
    # momentum：甜蜜带 + 累计达标 → 🎯 档0
    _ins_rec(conn, "SZ300001", "momentum", 5.0)
    _ins_kline(conn, "SZ300001")
    # new_face：死区带 → 档3（涨幅带死区/陷阱）
    _ins_rec(conn, "SZ300002", "new_face", 3.0)
    # rebound：档1 强信号
    _ins_rec(conn, "SZ300003", "rebound", -1.0)
    # comeback：独立区（不入主表）
    _ins_rec(conn, "SZ300004", "comeback", 2.0, score=55)
    conn.commit()
    return conn


class TestPersistRoundTrip:

    def test_round_trip_matches_live_computation(self, db):
        n = persist_ranking_snapshot(db, "2026-08-25")
        assert n == 4
        snap = load_ranking_snapshot(db, "2026-08-25")
        assert ("SZ300001", "momentum") in snap
        m = snap[("SZ300001", "momentum")]
        assert m["tier"] == 0
        assert m["marked"] is True
        assert m["reasons"] == []

    def test_tier3_reasons_persisted(self, db):
        persist_ranking_snapshot(db, "2026-08-25")
        snap = load_ranking_snapshot(db, "2026-08-25")
        nf = snap[("SZ300002", "new_face")]
        assert nf["tier"] == 3
        assert nf["marked"] is False
        assert R.TIER_REASON_BAND in nf["reasons"]

    def test_comeback_rank_null_main_ranks_ordered(self, db):
        persist_ranking_snapshot(db, "2026-08-25")
        snap = load_ranking_snapshot(db, "2026-08-25")
        cb = snap[("SZ300004", "comeback")]
        assert cb["rank_in_table"] is None
        ranks = {k: v["rank_in_table"] for k, v in snap.items()
                 if v["rank_in_table"] is not None}
        assert sorted(ranks.values()) == [1, 2, 3]
        # 档0 momentum 应排主表第 1
        assert snap[("SZ300001", "momentum")]["rank_in_table"] == 1

    def test_idempotent_overwrite(self, db):
        n1 = persist_ranking_snapshot(db, "2026-08-25")
        n2 = persist_ranking_snapshot(db, "2026-08-25")
        assert n1 == n2 == 4
        cnt = db.execute("SELECT COUNT(*) FROM ranking_snapshot").fetchone()[0]
        assert cnt == 4

    def test_other_date_isolated(self, db):
        persist_ranking_snapshot(db, "2026-08-25")
        assert load_ranking_snapshot(db, "2026-08-24") == {}


class TestLoadFallbacks:

    def test_missing_table_returns_empty(self):
        conn = _mk_db(with_snapshot_table=False)
        assert load_ranking_snapshot(conn, "2026-08-25") == {}

    def test_corrupt_reasons_json_tolerated(self, db):
        persist_ranking_snapshot(db, "2026-08-25")
        db.execute("UPDATE ranking_snapshot SET reasons_json='{bad json' "
                   "WHERE symbol='SZ300002'")
        snap = load_ranking_snapshot(db, "2026-08-25")
        assert snap[("SZ300002", "new_face")]["reasons"] == []


class TestUnifiedScannerFailOpen:
    """_persist_ranking_snapshot_once 异常不外泄（快照缺失由消费端回退兜底）。"""

    def test_no_raise_on_broken_db(self, monkeypatch):
        import unified_scanner as us

        monkeypatch.setattr(us, "_snapshot_done_date", None)
        monkeypatch.setattr(us, "is_trading_day", lambda d: True)
        monkeypatch.setattr(us, "is_trading_time", lambda now=None: False)

        class BoomConn:
            def execute(self, *a, **k):
                raise RuntimeError("boom")

        us._persist_ranking_snapshot_once(BoomConn())  # 不应抛出
