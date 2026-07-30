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


def test_rank_tie_averaging():
    # 相等值取平均秩：两个 2.0 并列，秩应为 (2+3)/2=2.5
    assert _rank([2.0, 1.0, 2.0, 4.0]) == [2.5, 1.0, 2.5, 4.0]


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
    # kline_map 新格式：{date: {"close": float, "percent": float}}
    # close 用于累计收益（cum_2d/cum_3d），percent 用于单日涨幅（next_day/fwd_3d/fwd_5d）
    kline_map = {
        "300999": {
            "2026-05-28": {"close": 10.0, "percent": 1.68},
            "2026-05-29": {"close": 9.67, "percent": -3.28},
            "2026-06-01": {"close": 9.86, "percent": 2.0},
            "2026-06-02": {"close": 9.91, "percent": 0.5},
            "2026-06-03": {"close": 10.02, "percent": 1.1},
        }
    }
    occ = compute_outcome(kline_map, "300999", "2026-05-28", 1.68)
    assert occ.next_day == -3.28
    # fwd_3d 是第 3 个交易日(2026-06-02)的当日涨幅(0.5)，并非累计收益
    assert occ.fwd_3d == 0.5
    # cum_2d: (close[T+2] - close[T]) / close[T] * 100
    # T+2 = 2026-06-01, close=9.86; rec_close=10.0
    assert occ.cum_2d is not None
    assert abs(occ.cum_2d - ((9.86 - 10.0) / 10.0 * 100)) < 1e-9


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


def test_dimension_ic_keeps_live_momentum_kdj():
    import os
    from scanner.config import DB_PATH

    if not os.path.exists(DB_PATH):
        return
    conn = sqlite3.connect(DB_PATH)
    dims = {d.dimension for d in dimension_ic(conn, "next_day_pct")}
    assert "momentum_kdj" in dims
    conn.close()
