"""kline_drift（复权漂移指纹监控）单元测试。

覆盖：锚定初始化 / 无漂移静默 / 价格改写告警（仅一次）/ 后补历史票不污染 /
volume 变化不告警（哈希只含价格）/ 历史不足不初始化。
"""

import json
import sqlite3

import pytest

from scanner import data_health as kd


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.execute("""CREATE TABLE daily_kline (
        symbol TEXT NOT NULL, timestamp INTEGER, date TEXT NOT NULL, open REAL, close REAL,
        high REAL, low REAL, volume REAL, percent REAL, finalized INTEGER DEFAULT 1,
        PRIMARY KEY(symbol, date))""")
    # 与 schema.init_db 同构（测试夹具不触真实库，自建所需表）
    c.execute("""CREATE TABLE kline_fingerprint (
        anchor_date TEXT PRIMARY KEY, start_date TEXT, end_date TEXT,
        symbols TEXT, kline_hash TEXT, updated TEXT DEFAULT '')""")
    yield c
    c.close()


def _seed(conn, dates, symbols=("SZ300001", "SZ300002"), close=10.0, pct=1.0):
    rows = []
    for sym in symbols:
        for d in dates:
            rows.append((sym, d, close, pct))
    conn.executemany("INSERT INTO daily_kline (symbol, date, close, percent) VALUES (?,?,?,?)", rows)
    conn.commit()


def _dates(n, prefix="2026-01-"):
    return [f"{prefix}{i + 1:02d}" for i in range(n)]


def test_insufficient_history_no_init(conn):
    """历史不足最低门槛（KLINE_DRIFT_MIN_BARS）→ 不初始化（返回 None，表无行）。"""
    _seed(conn, _dates(10))
    assert kd.check_kline_fingerprint(conn) is None
    assert conn.execute("SELECT COUNT(*) FROM kline_fingerprint").fetchone()[0] == 0


def test_partial_history_init_with_smaller_window(conn, monkeypatch):
    """可用历史在 [MIN, 250) → 以实际可用长度锚定（立即生效，不空等满窗口）。"""
    monkeypatch.setattr(kd, "KLINE_DRIFT_FINGERPRINT_BARS", 250)
    _seed(conn, _dates(40))  # 40 ∈ [30, 250)
    line = kd.check_kline_fingerprint(conn)
    assert line is not None and "初始化" in line
    row = conn.execute("SELECT start_date, end_date FROM kline_fingerprint").fetchone()
    assert row[0] == _dates(40)[0] and row[1] == _dates(40)[-1]


def test_init_then_silent(conn, monkeypatch):
    """首跑初始化（返回初始化行），同窗口二跑无事件（None）。"""
    _seed(conn, _dates(kd.KLINE_DRIFT_FINGERPRINT_BARS + 5))
    line = kd.check_kline_fingerprint(conn)
    assert line is not None and "初始化" in line
    row = conn.execute("SELECT start_date, end_date, symbols, kline_hash FROM kline_fingerprint").fetchone()
    assert row[3] and len(json.loads(row[2])) == 2
    # 二跑：无变化 → 无事件
    assert kd.check_kline_fingerprint(conn) is None


def test_price_rewrite_alerts_once(conn):
    """历史价格被改写（模拟 qfq 除权重算）→ 告警一次；再跑静默（指纹已更新）。"""
    _seed(conn, _dates(kd.KLINE_DRIFT_FINGERPRINT_BARS + 5))
    kd.check_kline_fingerprint(conn)
    # 改写窗口内某根历史 close（除权重算会批量改，单根足以改变哈希）。
    # 窗口 = 最后 250 个交易日（首个 5 日在窗口外），取中部日期确保命中。
    in_window = _dates(kd.KLINE_DRIFT_FINGERPRINT_BARS + 5)[10]
    conn.execute("UPDATE daily_kline SET close = 11.5 WHERE date = ? AND symbol = 'SZ300001'", (in_window,))
    line = kd.check_kline_fingerprint(conn)
    assert line is not None and "复权漂移告警" in line
    # 同一变更不重复告警
    assert kd.check_kline_fingerprint(conn) is None


def test_backfilled_symbol_no_pollution(conn):
    """窗口内后补新票（合法的 backfill 行为）→ 不告警（symbol 集合快照隔离）。"""
    _seed(conn, _dates(kd.KLINE_DRIFT_FINGERPRINT_BARS + 5))
    kd.check_kline_fingerprint(conn)
    _seed(conn, _dates(kd.KLINE_DRIFT_FINGERPRINT_BARS + 5), symbols=("SZ300099",), close=20.0)
    assert kd.check_kline_fingerprint(conn) is None


def test_volume_change_no_alert(conn):
    """volume 变化不参与哈希（复权重算只改价格）→ 不告警。"""
    _seed(conn, _dates(kd.KLINE_DRIFT_FINGERPRINT_BARS + 5))
    kd.check_kline_fingerprint(conn)
    conn.execute("UPDATE daily_kline SET volume = 99999 WHERE symbol = 'SZ300002'")
    assert kd.check_kline_fingerprint(conn) is None


def test_percent_change_alerts(conn):
    """percent 也是复权重算的产物（随 close 重算）→ 纳入哈希。"""
    _seed(conn, _dates(kd.KLINE_DRIFT_FINGERPRINT_BARS + 5))
    kd.check_kline_fingerprint(conn)
    conn.execute("UPDATE daily_kline SET percent = 2.5 WHERE symbol = 'SZ300001'")
    line = kd.check_kline_fingerprint(conn)
    assert line is not None and "复权漂移告警" in line


def test_window_data_missing_warns_once(conn):
    """锚定窗口数据被清空 → 缺失告警一次（hash 置空哨兵），之后静默。"""
    _seed(conn, _dates(kd.KLINE_DRIFT_FINGERPRINT_BARS + 5))
    kd.check_kline_fingerprint(conn)
    conn.execute("DELETE FROM daily_kline")
    line = kd.check_kline_fingerprint(conn)
    assert line is not None and "数据缺失" in line
    assert kd.check_kline_fingerprint(conn) is None
