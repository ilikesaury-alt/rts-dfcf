import sqlite3
from datetime import date

from scanner.backtest import (
    _ic,
    _nth_trading_day_after,
    _rank,
    RankCategoryStat,
    compute_outcome,
    print_ranking_report,
    rank_category_stats,
    suggest_priority,
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


def _ranking_db():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE recommendations "
        "(date TEXT, category TEXT, score REAL, cum_3d REAL, score_breakdown TEXT, "
        "sector_capped INTEGER DEFAULT 0)"
    )
    return conn


def test_rank_category_stats_sorts_by_avg_and_filters_unknown():
    conn = _ranking_db()
    rows = [
        ("2026-07-01", "momentum", 50, 2.0),
        ("2026-07-01", "momentum", 40, -1.0),
        ("2026-07-01", "momentum", 45, 3.0),  # momentum avg = 4/3
        ("2026-07-01", "new_face", 30, -2.0),  # new_face avg = -2.0
        ("2026-07-01", "pullback", 20, -9.0),  # 离线类别仍参与归因
        ("2026-07-01", "foo", 99, 9.0),  # 非 ACTIVE_CATEGORIES，应被过滤
    ]
    conn.executemany(
        "INSERT INTO recommendations (date, category, score, cum_3d) VALUES (?,?,?,?)",
        rows,
    )
    conn.commit()
    stats = rank_category_stats(conn, "cum_3d", days=0)
    cats = [s.category for s in stats]
    assert "foo" not in cats
    assert "pullback" in cats  # 保留离线类别数据用于校准
    assert cats == ["momentum", "new_face", "pullback"]
    m = stats[0]
    assert m.count == 3
    assert abs(m.avg_return - 4.0 / 3.0) < 1e-9
    assert m.win_rate == 2.0 / 3.0


def test_rank_category_stats_recent_window_filters_by_date():
    from datetime import timedelta

    from scanner.config import now_beijing

    today = now_beijing().date()
    in_window_1 = (today - timedelta(days=1)).isoformat()
    in_window_2 = (today - timedelta(days=3)).isoformat()
    outside = (today - timedelta(days=30)).isoformat()
    conn = _ranking_db()
    rows = [
        (in_window_1, "momentum", 50, 2.0),
        (in_window_2, "momentum", 40, -1.0),
        (outside, "momentum", 45, 8.0),  # 在窗口外
    ]
    conn.executemany(
        "INSERT INTO recommendations (date, category, score, cum_3d) VALUES (?,?,?,?)",
        rows,
    )
    conn.commit()
    stats = rank_category_stats(conn, "cum_3d", days=5)
    assert len(stats) == 1
    assert stats[0].count == 2
    assert abs(stats[0].avg_return - 0.5) < 1e-9


def test_rank_category_stats_metric_is_cum_3d():
    # 默认口径为 cum_3d（3日持有收益），与排序决策一致
    conn = _ranking_db()
    rows = [
        ("2026-07-01", "momentum", 50, 2.5),
        ("2026-07-01", "momentum", 40, -0.5),
    ]
    conn.executemany(
        "INSERT INTO recommendations (date, category, score, cum_3d) VALUES (?,?,?,?)",
        rows,
    )
    conn.commit()
    stats = rank_category_stats(conn)
    assert len(stats) == 1
    assert abs(stats[0].avg_return - 1.0) < 1e-9


def test_rank_category_stats_excludes_sector_capped_by_default():
    """2026-08-12: 被板块上限（sector_capped=1）的票默认排除（校准用户实际看到的集合），
    include_capped=True 时恢复全样本。"""
    conn = _ranking_db()
    rows = [
        ("2026-07-01", "momentum", 50, 2.0, None, 0),
        ("2026-07-01", "momentum", 40, -3.0, None, 1),
        ("2026-07-01", "momentum", 45, 5.0, None, 0),
    ]
    conn.executemany(
        "INSERT INTO recommendations (date, category, score, cum_3d, score_breakdown, sector_capped) "
        "VALUES (?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    default = rank_category_stats(conn, "cum_3d", days=0)
    assert default[0].count == 2
    assert abs(default[0].avg_return - 3.5) < 1e-9  # (2+5)/2
    full = rank_category_stats(conn, "cum_3d", days=0, include_capped=True)
    assert full[0].count == 3
    assert abs(full[0].avg_return - (2.0 - 3.0 + 5.0) / 3) < 1e-9


def test_rank_category_stats_missing_sector_capped_column_compatible():
    """列缺失（旧库/最小 schema）时不过滤，保持兼容。"""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE recommendations "
        "(date TEXT, category TEXT, score REAL, cum_3d REAL)"
    )
    conn.executemany(
        "INSERT INTO recommendations (date, category, score, cum_3d) VALUES (?,?,?,?)",
        [("2026-07-01", "momentum", 50, 2.0), ("2026-07-01", "momentum", 40, -1.0)],
    )
    conn.commit()
    stats = rank_category_stats(conn, "cum_3d", days=0)
    assert len(stats) == 1
    assert stats[0].count == 2


def test_suggest_priority_sorts_desc_by_avg():
    stats = [
        RankCategoryStat("new_face", 100, -1.0, 0.3, 0.0),
        RankCategoryStat("rebound", 15, 4.0, 0.6, 0.1),
        RankCategoryStat("momentum", 50, 1.0, 0.4, 0.1),
    ]
    assert suggest_priority(stats) == ["rebound", "momentum", "new_face"]


def test_ranking_report_runs_without_gbk_crash(capsys):
    conn = _ranking_db()
    rows = [
        ("2026-07-01", "momentum", 50, 2.0),
        ("2026-07-01", "momentum", 40, -1.0),
        ("2026-07-01", "new_face", 30, -2.0),
    ]
    conn.executemany(
        "INSERT INTO recommendations (date, category, score, cum_3d) VALUES (?,?,?,?)",
        rows,
    )
    conn.commit()
    print_ranking_report(conn, metric="cum_3d", recent_days=0)
    out = capsys.readouterr().out
    assert "momentum" in out
    assert "new_face" in out


def test_print_report_default_metric_is_cum3d_and_all_baseline(capsys):
    # P1-5 (2026-08-10): 默认口径为 cum_3d（匹配持有 2-3 天操作），且报告含
    # "ALL(全推荐基准)" 汇总行（不挑选买入全部推荐的无选择基准）。
    import scanner.backtest as bt
    assert bt.build_parser().get_default("metric") == "cum_3d"
    conn = _ranking_db()
    rows = [
        ("2026-07-01", "momentum", 50, 2.0),
        ("2026-07-01", "momentum", 40, -1.0),
        ("2026-07-01", "new_face", 30, -2.0),
    ]
    conn.executemany(
        "INSERT INTO recommendations (date, category, score, cum_3d) VALUES (?,?,?,?)",
        rows,
    )
    conn.commit()
    bt.print_report(conn, metric="cum_3d", days=0)
    out = capsys.readouterr().out
    assert "ALL(全推荐基准)" in out
    assert "推荐后到收盘涨幅" in out  # P1-6 高估方向标注
