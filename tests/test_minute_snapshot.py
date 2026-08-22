"""minute_snapshot 分时快照落库测试（2026-08-21）。

每轮扫描把最终候选 {现价, 涨幅} 采样进时间序列，历史分时形态可回放。
锁定：批量写入/同刻覆盖/脏值剔除/fail-open/剪枝（2026-08-22）。
"""
import sqlite3

from scanner.database import prune_minute_snapshots, save_minute_snapshots


def _db():
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE minute_snapshot (
            date TEXT,
            time TEXT,
            symbol TEXT,
            price REAL,
            pct REAL,
            updated TEXT DEFAULT '',
            PRIMARY KEY (date, time, symbol)
        )
    """)
    return conn


class TestSaveMinuteSnapshots:
    def test_batch_write_and_readback(self):
        conn = _db()
        n = save_minute_snapshots(conn, [
            {"symbol": "SZ300001", "price": 10.5, "pct": 3.2},
            {"symbol": "SZ300002", "price": 20.0, "pct": -1.1},
        ])
        assert n == 2
        rows = conn.execute("SELECT symbol, price, pct FROM minute_snapshot ORDER BY symbol").fetchall()
        assert rows[0][0] == "SZ300001" and rows[0][1] == 10.5 and rows[0][2] == 3.2
        assert rows[1][0] == "SZ300002" and rows[1][2] == -1.1

    def test_same_slot_overwrite(self):
        """同 (date,time,symbol) 覆盖（同轮重复调用不产生重复行）。"""
        conn = _db()
        save_minute_snapshots(conn, [{"symbol": "SZ300001", "price": 10.0, "pct": 1.0}])
        save_minute_snapshots(conn, [{"symbol": "SZ300001", "price": 11.0, "pct": 2.0}])
        rows = conn.execute("SELECT price FROM minute_snapshot").fetchall()
        assert len(rows) == 1 and rows[0][0] == 11.0

    def test_dirty_values_dropped(self):
        """脏值行剔除（price<=0 / 非有限 / None），合法行不受影响。"""
        conn = _db()
        n = save_minute_snapshots(conn, [
            {"symbol": "SZ300001", "price": 0, "pct": 1.0},          # 停牌降级价 0 → 剔除
            {"symbol": "SZ300002", "price": None, "pct": 1.0},       # 缺失 → 剔除
            {"symbol": "SZ300003", "price": float("nan"), "pct": 1.0},
            {"symbol": "SZ300004", "price": 12.0, "pct": float("inf")},
            {"symbol": "", "price": 10.0, "pct": 1.0},               # 无 symbol → 剔除
            {"symbol": "SZ300006", "price": 15.0, "pct": 2.5},       # 合法
        ])
        assert n == 1
        rows = conn.execute("SELECT symbol FROM minute_snapshot").fetchall()
        assert rows == [("SZ300006",)]

    def test_empty_and_fail_open(self):
        conn = _db()
        assert save_minute_snapshots(conn, []) == 0
        # 表不存在 → fail-open 返回 0 不抛异常
        broken = sqlite3.connect(":memory:")
        assert save_minute_snapshots(broken, [{"symbol": "S", "price": 1, "pct": 1}]) == 0

    def test_date_is_today(self):
        conn = _db()
        save_minute_snapshots(conn, [{"symbol": "SZ300001", "price": 10.0, "pct": 1.0}])
        row = conn.execute("SELECT date FROM minute_snapshot").fetchone()
        from scanner.config import now_beijing
        assert row[0] == now_beijing().date().isoformat()


class TestPruneMinuteSnapshots:
    def test_prunes_old_keeps_recent(self):
        """只删 keep_trading_days 个交易日之前的行，窗口内（含当日）保留。"""
        from scanner.db import _n_trading_days_ago

        cutoff = _n_trading_days_ago(60)
        recent = _n_trading_days_ago(30)
        conn = _db()
        for d in (cutoff, "2020-01-01", recent, "2099-01-01"):
            conn.execute(
                "INSERT INTO minute_snapshot (date, time, symbol, price, pct)"
                " VALUES (?, '10:00', 'SZ300001', 10.0, 1.0)",
                (d,))
        removed = prune_minute_snapshots(conn, 60)
        assert removed == 1  # 仅严格早于 cutoff 的删除；cutoff 当天/recent/future 保留
        dates = {r[0] for r in conn.execute("SELECT date FROM minute_snapshot").fetchall()}
        assert dates == {cutoff, recent, "2099-01-01"}

    def test_fail_open_on_broken_conn(self):
        """表不存在 → fail-open 返回 0 不抛异常。"""
        broken = sqlite3.connect(":memory:")
        assert prune_minute_snapshots(broken, 60) == 0

    def test_empty_table_returns_zero(self):
        conn = _db()
        assert prune_minute_snapshots(conn, 60) == 0
