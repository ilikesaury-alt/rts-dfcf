import sqlite3
from datetime import date

from scanner.backtest import (
    _ic,
    _nth_trading_day_after,
    _rank,
    compute_outcome,
    strategy_performance,
    dimension_ic,
)


def test_rank_monotonic():
    assert _rank([3, 1, 2]) == [3.0, 1.0, 2.0]


def test_ic_perfect_positive():
    # 完全正相关
    ic = _ic([1, 2, 3, 4, 5], [2, 4, 6, 8, 10])
    assert ic is not None and ic > 0.99


def test_ic_perfect_negative():
    ic = _ic([1, 2, 3, 4, 5], [10, 8, 6, 4, 2])
    assert ic is not None and ic < -0.99


def test_nth_trading_day_after_skips_weekend():
    # 2026-05-29 是周五，次一交易日应为 2026-06-01（周一）
    d = date.fromisoformat("2026-05-29")
    nxt = _nth_trading_day_after(d, 1)
    assert nxt.isoformat() == "2026-06-01"


def test_compute_outcome_returns_next_day_percent():
    kline_map = {
        "300999": {
            "2026-05-28": 1.68,
            "2026-05-29": -3.28,
            "2026-06-01": 2.0,
            "2026-06-02": 0.5,
            "2026-06-03": 1.1,
        }
    }
    occ = compute_outcome(kline_map, "300999", "2026-05-28", 1.68)
    assert occ.next_day == -3.28
    assert occ.fwd_3d == 0.5


def test_compute_outcome_missing_returns_none():
    occ = compute_outcome({}, "300999", "2026-05-28", 1.68)
    assert occ.next_day is None
    assert occ.fwd_3d is None


def test_strategy_performance_runs():
    # 用真实库（若存在），否则跳过——不强制依赖外部数据
    import os
    from scanner.config import DB_PATH

    if not os.path.exists(DB_PATH):
        return
    conn = sqlite3.connect(DB_PATH)
    stats = strategy_performance(conn, "next_day_pct")
    assert isinstance(stats, list)
    if stats:
        assert all(s.count > 0 for s in stats)
    conn.close()


def test_dimension_ic_runs():
    import os
    from scanner.config import DB_PATH

    if not os.path.exists(DB_PATH):
        return
    conn = sqlite3.connect(DB_PATH)
    dims = dimension_ic(conn, "next_day_pct")
    assert isinstance(dims, list)
    conn.close()
