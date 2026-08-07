"""组合级回测引擎测试。

覆盖：
- 真实库冒烟测试（集成）：在 scanner.db 上跑短窗口，检查 NAV / 指标结构合理。
- 合成场景：上涨行情下，开启成本的总收益应低于零成本（验证佣金/印花税/滑点已计入）。
"""

import math
import sqlite3
from datetime import date, timedelta

from scanner.config import DB_PATH
from scanner.portfolio_backtest import PBConfig, run_backtest


def _make_rising_db(path: str, prefix_days: int = 0) -> sqlite3.Connection:
    """构造一只上涨标的的迷你库：收盘价每日 +1%。

    prefix_days: 在首个推荐信号之前预留的"空仓交易日"数量（用于验证活跃窗口跳过空仓期）。
    推荐信号放在第 prefix_days 个交易日的开盘后（买入日 = 第 prefix_days+1 个交易日）。
    """
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE daily_kline "
        "(symbol TEXT, timestamp INTEGER, date TEXT, open REAL, close REAL, "
        "high REAL, low REAL, volume REAL, percent REAL, PRIMARY KEY(symbol, date))"
    )
    conn.execute(
        "CREATE TABLE recommendations "
        "(id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, time TEXT, symbol TEXT, "
        "name TEXT, category TEXT, score INTEGER, percent REAL, trend TEXT)"
    )
    from scanner.trading_session import is_trading_day
    base = date(2026, 6, 1)
    dates = []
    d = base
    while len(dates) < prefix_days + 12:   # 足够覆盖 prefix + 买入 + 持有 + 缓冲
        if is_trading_day(d):
            dates.append(d.isoformat())
        d += timedelta(days=1)
    prev_close = 100.0
    for i, dt in enumerate(dates):
        if i == 0:
            open_p = 100.0
            close_p = 100.0
            prev_close = 100.0
        else:
            open_p = prev_close
            close_p = round(prev_close * 1.01, 3)
            prev_close = close_p
        conn.execute(
            "INSERT INTO daily_kline VALUES (?,?,?,?,?,?,?,?,?)",
            ("300001", i, dt, open_p, close_p, close_p, open_p, 1e6, 1.0),
        )
    rec_idx = prefix_days
    conn.execute(
        "INSERT INTO recommendations (date, time, symbol, name, category, score, percent, trend) "
        "VALUES (?, '09:30:00', '300001', '测试股', 'new_face', 50, 1.0, 'up')",
        (dates[rec_idx],),
    )
    conn.commit()
    return conn


def test_synthetic_costs_reduce_return():
    import tempfile, os
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        conn = _make_rising_db(path)
        # 零成本
        cfg_free = PBConfig(days=0, hold_days=3, buy_delay=1, max_positions=10,
                            commission=0.0, stamp_duty=0.0, slippage=0.0)
        res_free = run_backtest(conn, cfg_free)
        # 默认成本（万2.5 / 0.05% / 0.1%）
        cfg_cost = PBConfig(days=0, hold_days=3, buy_delay=1, max_positions=10)
        res_cost = run_backtest(conn, cfg_cost)
        conn.close()

        assert res_free.metrics["n_trades"] == 1, "合成场景应恰好 1 笔交易"
        assert res_cost.metrics["n_trades"] == 1
        # 上涨 +1%/日，持有 3 日，零成本应为正收益；有成本应更低（仍可能为正）
        assert res_free.metrics["total_return"] > 0
        assert res_cost.metrics["total_return"] < res_free.metrics["total_return"], \
            "成本应使总收益下降"
        # 指标有限
        for key in ("total_return", "sharpe", "max_drawdown"):
            assert math.isfinite(res_cost.metrics[key])
    finally:
        os.remove(path)


def test_real_db_smoke():
    conn = sqlite3.connect(DB_PATH)
    cfg = PBConfig(days=20, hold_days=3, buy_delay=1, max_positions=10)
    res = run_backtest(conn, cfg)
    conn.close()
    assert len(res.nav) > 5, "NAV 序列应非空"
    assert "total_return" in res.metrics, "指标应包含总收益"
    assert math.isfinite(res.metrics.get("total_return", float("nan")))
    assert math.isfinite(res.metrics.get("sharpe", float("nan")))
    assert res.metrics.get("n_trades", 0) >= 0


def test_metrics_use_active_window():
    """指标必须在活跃窗口(首个买入日→末个卖出日)上计算，剔除空仓期初/期末平值。

    构造 10 个交易日空仓前缀：推荐放在第 10 个交易日(idx10)，买入日 = idx11，
    故 active_start 应等于 11，且 total_return 必须等于 nav[active_end]/nav[active_start]-1。
    """
    import tempfile, os
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        conn = _make_rising_db(path, prefix_days=10)
        res = run_backtest(conn, PBConfig(days=0, hold_days=3, buy_delay=1, max_positions=10))
        conn.close()
        s, e = res.active_start, res.active_end
        assert s == 11, f"活跃起点应跳过 10 日空仓前缀, got {s}"
        expected = res.nav[e][1] / res.nav[s][1] - 1.0
        assert abs(res.metrics["total_return"] - expected) < 1e-9, "total_return 必须基于活跃窗口"
        assert res.metrics["total_return"] > 0
    finally:
        os.remove(path)


def test_benchmark_no_skill_runs():
    """基准(无筛选)模式应在真实库上正常产出指标。"""
    conn = sqlite3.connect(DB_PATH)
    res = run_backtest(conn, PBConfig(days=20, no_skill=True, category=None))
    conn.close()
    assert "total_return" in res.metrics
    assert math.isfinite(res.metrics["total_return"])
    assert math.isfinite(res.metrics["sharpe"])


def test_no_same_day_round_trip():
    """T+1 约束：任何一笔交易的卖出日必须晚于买入日（不可当日买入当日卖出）。"""
    import tempfile, os
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        conn = _make_rising_db(path)
        res = run_backtest(conn, PBConfig(days=0, hold_days=1, buy_delay=1, max_positions=10))
        conn.close()
        assert res.metrics["n_trades"] == 1, "合成场景应恰好 1 笔交易"
        for t in res.trades:
            assert t.sell_date > t.buy_date, "T+1：卖出日必须晚于买入日"
    finally:
        os.remove(path)
