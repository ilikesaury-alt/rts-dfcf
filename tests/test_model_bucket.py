"""model_bucket（v3 离线可行性）单元测试。

覆盖：_auc 边界（单类返回 None / 完美判别 1.0 / 反向 0.0 / 并列平均秩）+
load_dataset 去重口径（取最后一轮 breakdown，防特征泄漏）。
"""

import sqlite3

import numpy as np
import pytest

from scanner import model_bucket as mb


def test_auc_perfect_and_reverse():
    y = np.array([0, 0, 1, 1])
    assert mb._auc(y, np.array([0.1, 0.2, 0.8, 0.9])) == 1.0
    assert mb._auc(y, np.array([0.9, 0.8, 0.2, 0.1])) == 0.0


def test_auc_single_class_returns_none():
    y = np.array([1, 1])
    assert mb._auc(y, np.array([0.5, 0.6])) is None


def test_auc_ties_average_rank():
    # 全同分 → AUC 恒 0.5（平均秩）
    y = np.array([0, 1, 0, 1])
    assert mb._auc(y, np.array([0.5, 0.5, 0.5, 0.5])) == 0.5


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute(
        "CREATE TABLE recommendations ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, time TEXT, symbol TEXT,"
        " name TEXT, category TEXT, score REAL, percent REAL, score_breakdown TEXT,"
        " excluded INTEGER DEFAULT 0)"
    )
    c.execute(
        "CREATE TABLE triple_barrier_labels ("
        " date TEXT NOT NULL, symbol TEXT NOT NULL, category TEXT NOT NULL,"
        " label INTEGER, touch_date TEXT, touch_pct REAL, ret_at_horizon REAL,"
        " buy_price REAL, updated TEXT DEFAULT '',"
        " PRIMARY KEY (date, symbol, category))"
    )
    yield c
    c.close()


def _add_rec(conn, date, sym, cat, breakdown, excluded=0):
    conn.execute(
        "INSERT INTO recommendations (date, time, symbol, name, category, score, percent,"
        " score_breakdown, excluded) VALUES (?,?,?,?,?,?,?,?,?)",
        (date, "10:00", sym, "票", cat, 50.0, 3.0, breakdown, excluded),
    )


def _add_label(conn, date, sym, cat, label):
    conn.execute(
        "INSERT INTO triple_barrier_labels (date, symbol, category, label, ret_at_horizon,"
        " buy_price) VALUES (?,?,?,?,?,?)",
        (date, sym, cat, label, 1.0, 10.0),
    )


def test_load_dataset_last_round_no_leak(conn):
    """同票同日多轮 → 特征取最后一轮 breakdown（取错轮 = 泄漏）。

    夹具键用 "a"（load_dataset 会加 d_ 前缀 → 列名 d_a；此前误写 "d_a"
    导致列名变 d_d_a、断言 KeyError）。
    """
    _add_rec(conn, "2026-03-02", "SZ300001", "momentum",
             '{"a": 1.0, "trend": "动量延续"}')  # 第一轮
    _add_rec(conn, "2026-03-02", "SZ300001", "momentum",
             '{"a": 9.0}')  # 第二轮（rowid 更大 → 应取此轮）
    _add_label(conn, "2026-03-02", "SZ300001", "momentum", 1)
    df = mb.load_dataset(conn)
    assert len(df) == 1
    row = df.iloc[0]
    assert row["d_a"] == 9.0  # 最后一轮的值
    assert row["y"] == 1
    # detail/文本键不入模
    assert "d_trend" not in df.columns


def test_load_dataset_excluded_and_touch_skipped(conn):
    """excluded=1 的推荐行不进样本；无 ret/touch 的标签行跳过。"""
    _add_rec(conn, "2026-03-02", "SZ300001", "momentum", '{"a": 1.0}', excluded=1)
    _add_label(conn, "2026-03-02", "SZ300001", "momentum", 1)
    _add_rec(conn, "2026-03-02", "SZ300002", "momentum", '{"a": 2.0}')
    _add_label(conn, "2026-03-02", "SZ300002", "momentum", 1)
    conn.execute(
        "INSERT INTO triple_barrier_labels (date, symbol, category, label, buy_price)"
        " VALUES ('2026-03-02','SZ300003','momentum',1,10.0)"
    )  # 无 touch_date 也无 ret_at_horizon（同日双触跳过类）
    df = mb.load_dataset(conn)
    assert len(df) == 1 and df.iloc[0]["symbol"] == "SZ300002"
