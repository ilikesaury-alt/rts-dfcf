import sqlite3
from datetime import date

import pytest

from scanner.backtest import (
    ALLOWED_METRICS,
    DAY_EXCESS_MIN_CAT,
    DAY_EXCESS_MIN_POOL,
    RankCategoryStat,
    _check_metric,
    _rank,
    compute_outcome,
    day_excess_stats,
    dimension_ic,
    nth_trading_day_after,
    print_ranking_report,
    rank_category_stats,
    spearman,
    strategy_performance,
    suggest_priority,
)


def test_rank_monotonic():
    assert _rank([3, 1, 2]) == [3.0, 1.0, 2.0]


def test_rank_tie_averaging():
    # 相等值取平均秩：两个 2.0 并列，秩应为 (2+3)/2=2.5
    assert _rank([2.0, 1.0, 2.0, 4.0]) == [2.5, 1.0, 2.5, 4.0]


def test_rank_float_tie_averaging():
    # 浮点近邻（差 < 1e-9）也应视为并列取平均秩，避免浮点累加误差让本应并列的分数分到不同秩
    assert _rank([1.0, 1.0 + 1e-10, 3.0]) == [1.5, 1.5, 3.0]


def test_ic_single_source():
    # 单源守护：nextday_attribution 必须复用 backtest.spearman 对象，
    # 杜绝再次分叉出 tie 处理规则不同的等价实现（浮点近邻算出不同 IC）。
    import scanner.nextday_attribution as nextday_attribution

    assert nextday_attribution.spearman is spearman


def test_ic_perfect_positive():
    # 完全正相关
    ic = spearman([1, 2, 3, 4, 5], [2, 4, 6, 8, 10])
    assert ic is not None and ic > 0.99


def test_ic_perfect_negative():
    ic = spearman([1, 2, 3, 4, 5], [10, 8, 6, 4, 2])
    assert ic is not None and ic < -0.99


def test_nth_trading_day_after_skips_weekend():
    # 2026-05-29 是周五，次一交易日应为 2026-06-01（周一）
    d = date.fromisoformat("2026-05-29")
    nxt = nth_trading_day_after(d, 1)
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


@pytest.mark.smoke
def test_strategy_performance_runs():
    # 真实库集成测试：依赖 scanner.db（默认跳过，--run-smoke 运行）
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


@pytest.mark.smoke
def test_dimension_ic_runs():
    import os

    from scanner.config import DB_PATH

    if not os.path.exists(DB_PATH):
        return
    conn = sqlite3.connect(DB_PATH)
    dims = dimension_ic(conn, "next_day_pct")
    assert isinstance(dims, list)
    conn.close()


@pytest.mark.smoke
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
        "(date TEXT, category TEXT, score REAL, next_day_pct REAL, cum_3d REAL, "
        "score_breakdown TEXT, symbol TEXT, excluded INTEGER DEFAULT 0)"
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


def test_rank_category_stats_metric_is_next_day_pct():
    # 2026-08-18 统一口径：默认口径为 next_day_pct（次日大涨），与排序决策一致
    conn = _ranking_db()
    rows = [
        ("2026-07-01", "momentum", 50, 2.5),
        ("2026-07-01", "momentum", 40, -0.5),
    ]
    conn.executemany(
        "INSERT INTO recommendations (date, category, score, next_day_pct) VALUES (?,?,?,?)",
        rows,
    )
    conn.commit()
    stats = rank_category_stats(conn)
    assert len(stats) == 1
    assert abs(stats[0].avg_return - 1.0) < 1e-9


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


def test_print_report_default_metric_is_nextday_and_all_baseline(capsys):
    # 2026-08-18 统一口径：默认口径为 next_day_pct（次日大涨），报告仍含
    # "ALL(全推荐基准)" 汇总行（不挑选买入全部推荐的无选择基准）。
    import scanner.backtest as bt

    assert bt.build_parser().get_default("metric") == "next_day_pct"
    conn = _ranking_db()
    rows = [
        ("2026-07-01", "momentum", 50, 2.0),
        ("2026-07-01", "momentum", 40, -1.0),
        ("2026-07-01", "new_face", 30, -2.0),
    ]
    conn.executemany(
        "INSERT INTO recommendations (date, category, score, next_day_pct) VALUES (?,?,?,?)",
        rows,
    )
    conn.commit()
    bt.print_report(conn, metric="next_day_pct", days=0)
    out = capsys.readouterr().out
    assert "ALL(全推荐基准)" in out
    assert "next_day_pct" in out  # 报告头标注当前口径


def _stat_db():
    """带 symbol / excluded 的推荐表（strategy_performance 需要过滤 excluded）。"""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE recommendations "
        "(date TEXT, category TEXT, score REAL, next_day_pct REAL, cum_3d REAL, "
        "score_breakdown TEXT, symbol TEXT, excluded INTEGER DEFAULT 0)"
    )
    return conn


# ── 主口径 = hit≥7%（2026-09-21 口径修正） ────────────────────────────────
# 动机：旧主口径是 win_rate（收益 > 0），它测的是「不跌」，与全项目唯一目标函数
# 「次日≥7%」不相干。实测 core_dip 胜率 52.8% 但 hit≥7% 仅 3.44% —— 两个数字
# 描述完全不同的东西，而排序/档位/🎯画像全按 hit≥7% 校准。


def test_strategy_performance_hit_rate_counts_ge_threshold_only():
    """hit_rate 只数 `>= NEXTDAY_HIT_THRESHOLD`，恰好等于门槛必须计入。

    边界是 `>=` 不是 `>`：NEXTDAY_HIT_THRESHOLD 本身是「命中」的定义（次日涨 7%
    就算是），写成 `>` 会静默丢掉正好 +7.00 的样本 —— 而涨停/接近涨停的票恰恰
    密集落在门槛附近，错一边就是系统性低估。
    """
    from scanner.config import NEXTDAY_HIT_THRESHOLD

    conn = _stat_db()
    t = NEXTDAY_HIT_THRESHOLD
    rows = [
        # 5 条：2 条 >= 门槛（含恰好等于门槛的那条），3 条 < 门槛
        ("2026-07-01", "momentum", 50, t, "SZ300001", 0),  # 恰好 == 门槛 → 计入
        ("2026-07-01", "momentum", 50, t + 0.01, "SZ300002", 0),  # 高于 → 计入
        ("2026-07-01", "momentum", 50, t - 0.01, "SZ300003", 0),  # 差 0.01 → 不计
        ("2026-07-01", "momentum", 50, 0.0, "SZ300004", 0),
        ("2026-07-01", "momentum", 50, -3.0, "SZ300005", 0),
    ]
    conn.executemany(
        "INSERT INTO recommendations (date, category, score, next_day_pct, symbol, excluded) VALUES (?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    try:
        s = strategy_performance(conn, "next_day_pct")[0]
        assert s.category == "momentum"
        assert s.count == 5
        assert abs(s.hit_rate - 2.0 / 5.0) < 1e-9, f"hit_rate 应为 0.4（含 == 门槛那条），实得 {s.hit_rate}"
        # 对照：win_rate 用 `> 0`，三条正收益（t / t+0.01 / t-0.01）算赢，0.0 与 -3.0 算输
        assert abs(s.win_rate - 3.0 / 5.0) < 1e-9
    finally:
        conn.close()


def test_strategy_performance_hit_rate_differs_from_win_rate():
    """口径分离的实证：同批数据里 win_rate 高但 hit_rate 低 —— 正是本轮修正的理由。

    构造「微涨多但不涨」：4 条 +0.5 的小阳线 + 1 条 +9.0 的大涨。
    旧口径（>0）给出 100% 的漂亮胜率，新口径（>=7%）只有 20%。
    """
    from scanner.config import NEXTDAY_HIT_THRESHOLD

    conn = _stat_db()
    rows = [
        ("2026-07-01", "core_dip", 50, 9.0, "SZ300001", 0),  # 唯一命中
        ("2026-07-01", "core_dip", 50, 0.5, "SZ300002", 0),
        ("2026-07-01", "core_dip", 50, 0.5, "SZ300003", 0),
        ("2026-07-01", "core_dip", 50, 0.5, "SZ300004", 0),
        ("2026-07-01", "core_dip", 50, 0.5, "SZ300005", 0),
    ]
    conn.executemany(
        "INSERT INTO recommendations (date, category, score, next_day_pct, symbol, excluded) VALUES (?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    try:
        s = strategy_performance(conn, "next_day_pct")[0]
        assert abs(s.win_rate - 1.0) < 1e-9, "旧口径：全为正"
        assert abs(s.hit_rate - 0.2) < 1e-9, "新口径：只有 1 条算大涨"
        assert NEXTDAY_HIT_THRESHOLD > 0.5, "前提：微涨不算 hit"
    finally:
        conn.close()


def test_strategy_performance_sorts_by_hit_rate_not_win_rate():
    """排序键必须是 hit_rate。若退回 win_rate 排序，本用例的两个类别顺序会翻转。

    ⚠ 必须用 ACTIVE_CATEGORIES 里的真实类别名 —— strategy_performance 内部经
    load_attribution_rows 过滤 `category IN ACTIVE_CATEGORIES`，自造类别名（如
    "smooth"）会被静默丢弃，得到一个空列表并把断言变成假绿。

    构造：`rebound` 全是不涨不跌的小阳线（win 100%、hit 0%）；
          `momentum` 少数大涨、多数小跌（win 40%、hit 40%）。
    按 win_rate 排序 rebound 在前；按 hit_rate 排序 momentum 在前 —— 后者才是系统目标。
    """
    from scanner.config import NEXTDAY_HIT_THRESHOLD

    t = NEXTDAY_HIT_THRESHOLD
    conn = _stat_db()
    rows = []
    for i in range(10):
        rows.append(("2026-07-01", "rebound", 50, 0.6, f"SZ4000{i:02d}", 0))  # win 100%, hit 0%
    for i in range(4):
        rows.append(("2026-07-01", "momentum", 50, t + 1.0, f"SZ5000{i:02d}", 0))  # hit
    for i in range(6):
        rows.append(("2026-07-01", "momentum", 50, -2.0, f"SZ6000{i:02d}", 0))  # lose
    conn.executemany(
        "INSERT INTO recommendations (date, category, score, next_day_pct, symbol, excluded) VALUES (?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    try:
        stats = strategy_performance(conn, "next_day_pct")
        assert [s.category for s in stats] == ["momentum", "rebound"], "应按 hit_rate 降序"
        hit_first = next(s for s in stats if s.category == "momentum")
        win_first = next(s for s in stats if s.category == "rebound")
        assert hit_first.hit_rate > win_first.hit_rate
        # 反向证明：一旦按 old 口径排序，顺序确实会反过来
        assert win_first.win_rate > hit_first.win_rate
    finally:
        conn.close()


def test_strategy_performance_ignores_excluded_rows():
    """excluded=1 的样本不得进入 hit_rate/win_rate 分母（口径与决策层一致）。"""
    from scanner.config import NEXTDAY_HIT_THRESHOLD

    conn = _stat_db()
    rows = [
        ("2026-07-01", "momentum", 50, NEXTDAY_HIT_THRESHOLD + 2.0, "SZ300001", 0),
        ("2026-07-01", "momentum", 50, 0.1, "SZ300002", 0),
        # 这两条若被计入会把 hit_rate 拉到 0.5
        ("2026-07-01", "momentum", 50, -9.0, "SZ300003", 1),
        ("2026-07-01", "momentum", 50, -9.0, "SZ300004", 1),
    ]
    conn.executemany(
        "INSERT INTO recommendations (date, category, score, next_day_pct, symbol, excluded) VALUES (?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    try:
        stats = strategy_performance(conn, "next_day_pct")
        assert len(stats) == 1
        assert stats[0].count == 2
        assert abs(stats[0].hit_rate - 0.5) < 1e-9
    finally:
        conn.close()


def test_print_report_shows_both_metrics_and_all_hit_baseline(capsys):
    """报告同时展示新主口径与降级后的旧口径，且 ALL 行汇总 hit（可对基准对比）。"""
    import scanner.backtest as bt
    from scanner.config import NEXTDAY_HIT_THRESHOLD

    t = NEXTDAY_HIT_THRESHOLD
    conn = _stat_db()
    rows = [
        ("2026-07-01", "momentum", 50, t + 1.0, "SZ300001", 0),
        ("2026-07-01", "momentum", 50, -1.0, "SZ300002", 0),
        ("2026-07-01", "new_face", 30, -2.0, "SZ300003", 0),
        ("2026-07-01", "new_face", 30, 0.5, "SZ300004", 0),
    ]
    conn.executemany(
        "INSERT INTO recommendations (date, category, score, next_day_pct, symbol, excluded) VALUES (?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    try:
        bt.print_report(conn, metric="next_day_pct", days=0)
        out = capsys.readouterr().out
        assert "hit≥7%" in out, "主口径列必须出现在分策略表现表"
        assert "胜率>0" in out, "旧口径降级为对照列，仍需可见"
        assert "ALL(全推荐基准)" in out
        # 1/4 命中 → ALL 行应打 25.0%
        assert "25.0%" in out
    finally:
        conn.close()


@pytest.mark.parametrize("metric", sorted(ALLOWED_METRICS))
def test_check_metric_allows_known_columns(metric):
    """白名单内的收益列名原样放行。"""
    assert _check_metric(metric) == metric


@pytest.mark.parametrize(
    "metric",
    ["", "next_day_pct; DROP TABLE recommendations--", "score", "1", "next_day_pct "],
)
def test_check_metric_rejects_injection(metric):
    """回归（2026-08-29）：metric 是列名无法参数化，只能拼进 SQL。

    此前三个聚合函数直接 f-string 内插，仅靠 argparse choices 在函数外部挡；
    函数自身边界毫无防护——新增绕过 argparse 的调用方即成注入点。
    """
    with pytest.raises(ValueError, match="非法 metric"):
        _check_metric(metric)


@pytest.mark.parametrize("func", [strategy_performance, dimension_ic, rank_category_stats])
def test_aggregate_functions_reject_bad_metric(func):
    """三个拼接 metric 的聚合入口都必须先过白名单（防漏改其中某一个）。

    拒绝必须发生在执行 SQL 之前，故用干净的内存库验证表结构完好无损。
    """
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute(
            "CREATE TABLE recommendations (date TEXT, category TEXT, score REAL,"
            " next_day_pct REAL, cum_3d REAL, score_breakdown TEXT)"
        )
        with pytest.raises(ValueError, match="非法 metric"):
            func(conn, metric="cum_3d; DROP TABLE recommendations--", days=0)
        assert (
            conn.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='recommendations'").fetchone()[
                0
            ]
            == 1
        )
    finally:
        conn.close()


# ── 日超额（配对口径）：2026-09-21 归因审计 ─────────────────────────────────
# 动机：绝对胜率/均收益无法区分 alpha 与 beta。实测日 hit 极差 38.9pp
# （0.0%~38.9%），且各类别样本期几乎不重叠（old_face 仅 05-28~06-09、
# core_dip 08-19 才上线、momentum 只有 5 个有效交易日）——拿不同月份的两个类别
# 比胜率，比的是两个不同市场。day_excess 用「每日减当日全池均值 + 对日等权」剥离 beta。


def test_day_excess_subtracts_same_day_market_mean():
    """核心语义：每只票先减「当日全池均值」，再对日等权。

    构造：day1 全池普涨（均值 +10），其中 momentum 比均值高 +2；
          day2 全池普跌（均值 -10），其中 momentum 比均值高 +2。
    期望：momentum 日超额 = +2（两天都 +2），而不是被市场均值带成 0 附近。
    """
    rows = []
    # day1: 全池 20 只均值 +10 —— 19 只 +10.0，1 只 momentum +12.0
    for _ in range(19):
        rows.append({"date": "2026-07-01", "category": "agg", "next_day_pct": 10.0})
    rows.append({"date": "2026-07-01", "category": "momentum", "next_day_pct": 12.0})
    # 补足 momentum 当日 >=3 只的门槛（DAY_EXCESS_MIN_CAT=3），均值仍 +12
    rows.append({"date": "2026-07-01", "category": "momentum", "next_day_pct": 12.0})
    rows.append({"date": "2026-07-01", "category": "momentum", "next_day_pct": 12.0})
    # day2: 全池 20 只均值 -10，momentum 三只 -8.0
    for _ in range(17):
        rows.append({"date": "2026-07-02", "category": "agg", "next_day_pct": -10.0})
    for _ in range(3):
        rows.append({"date": "2026-07-02", "category": "momentum", "next_day_pct": -8.0})

    res = day_excess_stats(rows, "next_day_pct")
    ex, days = res["momentum"]
    assert days == 2
    # day1: 全池均值 = (19*10 + 3*12)/22 = 10.2727; momentum 12 - 10.2727 = +1.7273
    # day2: 全池均值 = (17*-10 + 3*-8)/20 = -9.7;      momentum -8 - (-9.7)  = +1.7
    # 等权均值 ≈ +1.7136 —— 符号与量级都该是「稳定跑赢当日基准」
    assert ex > 1.5, f"应测得稳定正超额，实得 {ex}"


def test_day_excess_rejects_thin_days():
    """当日全池样本不足 / 类别当日票数不足 → 该日不计入，避免基准本身是噪声。"""
    # 池子太小（< DAY_EXCESS_MIN_POOL）：注意要用 MIN_POOL - 1，恰好等于门槛是放行的
    thin = [{"date": "2026-07-01", "category": "momentum", "next_day_pct": 9.0}] * (DAY_EXCESS_MIN_POOL - 1)
    assert "momentum" not in day_excess_stats(thin, "next_day_pct")

    # 池子刚好达标，但类别当日只有 1 只（< DAY_EXCESS_MIN_CAT）
    rows = [{"date": "2026-07-01", "category": "agg", "next_day_pct": 1.0} for _ in range(DAY_EXCESS_MIN_POOL)]
    rows.append({"date": "2026-07-01", "category": "momentum", "next_day_pct": 99.0})
    assert DAY_EXCESS_MIN_CAT > 1
    assert "momentum" not in day_excess_stats(rows, "next_day_pct")


def test_day_excess_is_day_equal_weighted_not_sample_weighted():
    """对**交易日**等权，而非对样本条数加权。

    反例构造：day A 有 100 条 momentum（超额 0），day B 有 3 条（超额 +10）。
    按条数加权会得到 ≈ +0.29；按日等权应得到 +5.0。后者才不会被「某天票多」带偏。
    """
    rows = []
    # day A: 100 条 momentum，与全池同值 → 超额 0
    for _ in range(100):
        rows.append({"date": "2026-07-01", "category": "momentum", "next_day_pct": 5.0})
    # day B: 30 条 agg（0.0）+ 3 条 momentum（10.0）→ 超额 +10
    for _ in range(30):
        rows.append({"date": "2026-07-02", "category": "agg", "next_day_pct": 0.0})
    for _ in range(3):
        rows.append({"date": "2026-07-02", "category": "momentum", "next_day_pct": 10.0})

    ex, days = day_excess_stats(rows, "next_day_pct")["momentum"]
    assert days == 2
    # dayA 全池=5.0（33 行全是 momentum）→ 超额 0
    # dayB 全池=(30*0 + 3*10)/33 = 0.9091 → momentum 超额 = 10 - 0.9091 = 9.0909
    # 日等权均值 = (0 + 9.0909)/2 = 4.5455
    assert abs(ex - (10.0 - 30.0 / 33.0) / 2.0) < 1e-9, f"应为日等权 4.5455，实得 {ex}"
    # 关键对照：若按**条数**加权（错误做法）会得到 100/103*0 + 3/103*9.0909 ≈ 0.265
    assert ex > 4.0, "日等权结果应远高于按条数加权的 ≈0.27"


def test_rank_category_stats_carries_day_excess_and_eff_days():
    """rank_category_stats 必须把日超额/有效日带到 RankCategoryStat 上（并排展示）。"""
    conn = _ranking_db()
    rows = []
    for d in ("2026-07-01", "2026-07-02"):
        for _ in range(20):
            rows.append((d, "new_face", 30, 0.0))
        for _ in range(3):
            rows.append((d, "momentum", 50, 8.0))  # 每日稳定 +8 超额
    conn.executemany(
        "INSERT INTO recommendations (date, category, score, next_day_pct) VALUES (?,?,?,?)",
        rows,
    )
    conn.commit()
    try:
        stats = rank_category_stats(conn, "next_day_pct", days=0)
        m = next(s for s in stats if s.category == "momentum")
        assert m.eff_days == 2
        # 每日全池 = (20*0 + 3*8)/23 = 1.0435 → momentum 超额 = 8 - 1.0435 = 6.9565
        assert abs(m.day_excess - (8.0 - 24.0 / 23.0)) < 1e-9
    finally:
        conn.close()


def test_ranking_report_marks_thin_day_excess_as_na(capsys):
    """有效交易日不足时显示 n/a 而非数字 —— 不给「看起来可信」的伪数。

    这是本改动的核心契约：宁可显示 n/a，也不要再产出一个会被误信的绝对数字。
    """
    conn = _ranking_db()
    # 只有 1 个交易日 → eff_days=1 < DAY_EXCESS_MIN_DAYS
    conn.executemany(
        "INSERT INTO recommendations (date, category, score, next_day_pct) VALUES (?,?,?,?)",
        [("2026-07-01", "momentum", 50, 8.0)] * 30,
    )
    conn.commit()
    try:
        print_ranking_report(conn, recent_days=30)
        out = capsys.readouterr().out
        assert "日超额" in out
        assert "n/a" in out
        assert "[日超额样本不足]" in out
    finally:
        conn.close()
