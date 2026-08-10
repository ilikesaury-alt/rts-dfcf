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
        "name TEXT, category TEXT, score INTEGER, percent REAL, trend TEXT, "
        "score_breakdown TEXT, source TEXT)"
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


def test_last_calendar_day_buy_is_skipped():
    """回归：买入日落在日历末尾时不得产生 T+0 假交易。

    此前 clamp 逻辑 `exit_idx >= len(calendar) -> len-1` 会让「买入日=最后一个交易日」
    的信号 exit_index == buy_index，产生当日买入当日卖出（hold_days=0）的 T+0 交易，
    违反 A 股 T+1 约束。修复后该信号应被跳过（无信号/无交易）。
    """
    import tempfile, os
    from scanner.portfolio_backtest import PBConfig, _build_calendar, _load_signals, run_backtest
    from scanner.trading_session import is_trading_day

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE daily_kline "
                     "(symbol TEXT, timestamp INTEGER, date TEXT, open REAL, close REAL, "
                     "high REAL, low REAL, volume REAL, percent REAL, PRIMARY KEY(symbol, date))")
        conn.execute("CREATE TABLE recommendations "
                     "(id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, time TEXT, symbol TEXT, "
                     "name TEXT, category TEXT, score INTEGER, percent REAL, trend TEXT, "
                     "score_breakdown TEXT, source TEXT)")
        base = date(2026, 6, 1)
        dates = []
        d = base
        while len(dates) < 12:
            if is_trading_day(d):
                dates.append(d.isoformat())
            d += timedelta(days=1)
        for i, dt in enumerate(dates):
            conn.execute("INSERT INTO daily_kline VALUES (?,?,?,?,?,?,?,?,?)",
                         ("300001", i, dt, 10.0 + i, 10.0 + i, 10.0 + i, 10.0 + i, 1e6, 1.0))
        # 推荐日=倒数第2个交易日，buy_delay=1 → 买入日=最后一个交易日
        conn.execute(
            "INSERT INTO recommendations (date,time,symbol,name,category,score,percent,trend) "
            "VALUES (?, '09:30:00', '300001', 'T', 'new_face', 50, 1.0, 'up')",
            (dates[-2],),
        )
        conn.commit()

        calendar = _build_calendar(conn, dates[0], dates[-1])
        cal_index = {d: i for i, d in enumerate(calendar)}
        cfg = PBConfig(category="new_face", buy_delay=1, hold_days=3, max_positions=10)
        sigs = _load_signals(conn, cfg, calendar, cal_index, calendar[-1])
        assert sigs == [], "买入日=最后交易日，无法持有≥1日，信号应被跳过"
        res = run_backtest(conn, cfg)
        conn.close()
        assert res.metrics["n_trades"] == 0, "不得产生 T+0 假交易"
    finally:
        os.remove(path)


def test_buy_at_close_uses_close_price():
    """--buy-at close 应在买入日收盘买入（更贵），总收益低于 --buy-at open。

    合成上涨库：每日 +1%，买入日 open=前收、close=open*1.01，故同笔交易
    收盘买比开盘买多付 ~1%，总收益应更低；且 Trade.buy_price 应等于当日收盘（> 开盘）。
    """
    import tempfile, os
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        conn = _make_rising_db(path, prefix_days=2)
        res_open = run_backtest(conn, PBConfig(days=0, hold_days=3, buy_delay=1,
                                               max_positions=10, buy_at="open"))
        res_close = run_backtest(conn, PBConfig(days=0, hold_days=3, buy_delay=1,
                                                max_positions=10, buy_at="close"))
        conn.close()
        assert res_open.metrics["n_trades"] == 1
        assert res_close.metrics["n_trades"] == 1
        # 收盘买更贵 → 总收益更低
        assert res_close.metrics["total_return"] < res_open.metrics["total_return"], \
            "买入日收盘买应比开盘买总收益更低"
        # Trade.buy_price 应等于买入日收盘价（> 开盘价）
        assert res_close.trades[0].buy_price > res_open.trades[0].buy_price
    finally:
        os.remove(path)


def test_accumulate_final_score_excludes_heat_bonuses():
    """排序键必须剔除热度放大器（RPS/榜单动量/板块集群/实时量比/时间/市场情绪）。

    这些项只应作展示徽章（已写入 dimensions），不能进入 c.score 排序键，
    否则综合排序沦为「买最热的票」的追涨陷阱（回测实证综合 -28% 劣于无筛选基准）。
    """
    from scanner.enhancer import accumulate_final_score, HEAT_AMPLIFIER_BONUS_ATTRS
    from scanner.models import Candidate, StockInfo, KlineSummary

    # 全部热度 bonus 拉满，质量类 bonus 归零
    c = Candidate(
        stock=StockInfo(symbol="300001", name="测试", code="300001", percent=1.0,
                        current=10.0, value=1e8, rank_change=0, rank=1, source_tag="both"),
        category="new_face", score=0, reason="",
        kline=KlineSummary(trend="up", accumulated_pct=0.0, volume_ratio=1.0,
                           bottom_confirmed=False, score=0, dimensions={}),
    )
    for attr in HEAT_AMPLIFIER_BONUS_ATTRS:
        setattr(c, attr, 50)  # 热度项全置 50
    c.time_bonus = 50
    c.market_sentiment_bonus = 50

    total = accumulate_final_score(c, opening_scores={})
    # 排序键应完全忽略上述热度项：结果为 0（质量类 bonus 全为默认 0）
    assert total == 0, f"排序键不应包含热度 bonus，期望 0，实得 {total}"

    # 反向：质量类 bonus 应被计入
    c.first_today_bonus = 3
    c.gap_up_bonus = 2
    c.fund_flow_bonus = 4
    assert accumulate_final_score(c, opening_scores={}) == 9, \
        "质量类 bonus 应计入排序键"


def test_assign_rank_scores_signal_percentile():
    """within-(date,category) 百分位：同类内 score 越高 percentile 越高；跨类各自归一。"""
    from scanner.portfolio_backtest import Signal, _assign_rank_scores

    # 类别 A：低分标尺（新面孔风格，score 17~45）
    # 类别 B：高分标尺（comeback 风格，score 114~129）
    sigs = [
        Signal(rec_date="2026-07-01", symbol="A1", name="a", category="new_face", score=17),
        Signal(rec_date="2026-07-01", symbol="A2", name="a", category="new_face", score=45),
        Signal(rec_date="2026-07-01", symbol="A3", name="a", category="new_face", score=30),
        Signal(rec_date="2026-07-01", symbol="B1", name="b", category="comeback", score=114),
        Signal(rec_date="2026-07-01", symbol="B2", name="b", category="comeback", score=129),
    ]
    _assign_rank_scores(sigs)
    by = {s.symbol: s for s in sigs}
    # 类别 A：3 只，分位 0/50/100
    assert by["A1"].rank_score == 0.0
    assert by["A3"].rank_score == 50.0
    assert by["A2"].rank_score == 100.0
    # 类别 B：2 只，分位 0/100
    assert by["B1"].rank_score == 0.0
    assert by["B2"].rank_score == 100.0
    # 跨类可比：A2（分位100）应优于 B1（分位0），即便 B1 的 raw score(114) >> A2(45)
    assert by["A2"].rank_score > by["B1"].rank_score


def test_assign_rank_scores_dict_percentile():
    """database._assign_rank_scores 对 dict 记录的百分位归一化（综合排序展示用）。"""
    from scanner.database import _assign_rank_scores

    recs = [
        {"date": "2026-07-01", "category": "new_face", "score": 20},
        {"date": "2026-07-01", "category": "new_face", "score": 45},
        {"date": "2026-07-01", "category": "comeback", "score": 122},
    ]
    _assign_rank_scores(recs)
    by = {r["category"]: r for r in recs}
    assert by["new_face"]["rank_score"] == 100.0   # 同类内最高
    assert by["comeback"]["rank_score"] == 100.0    # 同类内唯一 -> 100
    # 两者类内均居首，故综合排序并列优先，不再被 comeback 的标尺(122)压过 new_face(45)
    assert by["new_face"]["rank_score"] == by["comeback"]["rank_score"]


def test_deheat_score_unit():
    """_deheat_score 从含热度 final_score 重建去热度分（验证 Step 1 可历史回测）。"""
    import json
    from scanner.config import CROSS_SOURCE_BONUS
    from scanner.portfolio_backtest import _deheat_score

    dims = {
        "sector_bonus": 2, "live_vol_bonus": 3, "rps_bonus": 5,
        "list_momentum_bonus": 4, "time_bonus": 1,
        "market_sentiment_bonus": 2, "market_env_bonus": 3,
    }  # 热度合计 = 20
    # 含热度 raw=200，减掉 20 -> 180
    assert _deheat_score(200, json.dumps(dims), "xueqiu") == 180
    # source=='both' 额外减 CROSS_SOURCE_BONUS
    assert _deheat_score(200, json.dumps(dims), "both") == 180 - CROSS_SOURCE_BONUS
    # 无 breakdown -> 回退原始分（不报错）
    assert _deheat_score(200, None, "xueqiu") == 200
    # breakdown 仅含部分热度键 -> 只减存在的
    assert _deheat_score(200, json.dumps({"sector_bonus": 2}), "xueqiu") == 198
    # 非法 JSON -> 回退原始分
    assert _deheat_score(200, "not-json", "xueqiu") == 200


def test_load_signals_deheat_toggles_score():
    """_load_signals 的 deheat 开关应改变 Signal.score（去热度 vs 原始）。"""
    import tempfile, os, json
    from scanner.config import CROSS_SOURCE_BONUS
    from scanner.portfolio_backtest import (
        PBConfig, _build_calendar, _load_signals,
    )
    from scanner.trading_session import is_trading_day

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE daily_kline "
                     "(symbol TEXT, timestamp INTEGER, date TEXT, open REAL, close REAL, "
                     "high REAL, low REAL, volume REAL, percent REAL, PRIMARY KEY(symbol, date))")
        conn.execute("CREATE TABLE recommendations "
                     "(id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, time TEXT, symbol TEXT, "
                     "name TEXT, category TEXT, score INTEGER, percent REAL, trend TEXT, "
                     "score_breakdown TEXT, source TEXT)")
        base = date(2026, 6, 1)
        dates = []
        d = base
        while len(dates) < 8:
            if is_trading_day(d):
                dates.append(d.isoformat())
            d += timedelta(days=1)
        for sym in ("300001", "300002"):
            prev = 100.0
            for i, dt in enumerate(dates):
                o = prev
                c = 100.0 if i == 0 else round(prev * 1.005, 3)
                prev = c
                conn.execute("INSERT INTO daily_kline VALUES (?,?,?,?,?,?,?,?,?)",
                             (sym, i, dt, o, c, c, o, 1e6, 0.5))
        heat = {"sector_bonus": 10, "live_vol_bonus": 5, "rps_bonus": 8,
                "list_momentum_bonus": 7, "time_bonus": 2,
                "market_sentiment_bonus": 5, "market_env_bonus": 3}  # 合计 40
        conn.execute(
            "INSERT INTO recommendations (date,time,symbol,name,category,score,percent,trend,score_breakdown,source) "
            "VALUES (?, '09:30:00','300001','A','new_face',100,0.5,'up',?, 'xueqiu')",
            (dates[0], json.dumps(heat)))
        conn.execute(
            "INSERT INTO recommendations (date,time,symbol,name,category,score,percent,trend,score_breakdown,source) "
            "VALUES (?, '09:30:00','300002','B','new_face',100,0.5,'up',?, 'both')",
            (dates[0], json.dumps(heat)))
        conn.commit()

        calendar = _build_calendar(conn, dates[0], dates[-1])
        cal_index = {d: i for i, d in enumerate(calendar)}
        cal_end = calendar[-1]

        sig_raw = _load_signals(conn, PBConfig(category="new_face", deheat=False),
                                calendar, cal_index, cal_end)
        sig_de = _load_signals(conn, PBConfig(category="new_face", deheat=True),
                               calendar, cal_index, cal_end)
        conn.close()

        assert len(sig_raw) == 2 and len(sig_de) == 2
        # deheat=False: 原始分
        assert all(s.score == 100 for s in sig_raw)
        # deheat=True: 100 - 40 = 60（xueqiu）；both 再减 CROSS_SOURCE_BONUS
        by_de = {s.symbol: s for s in sig_de}
        assert by_de["300001"].score == 60
        assert by_de["300002"].score == 60 - CROSS_SOURCE_BONUS
    finally:
        os.remove(path)


def test_load_signals_empty_recommendations_returns_empty():
    """回归：recommendations 无记录时 _load_signals 应返回空列表，
    不得因 max([]) 抛 ValueError（此前在空库回测时崩溃）。"""
    import tempfile, os
    from scanner.portfolio_backtest import (
        PBConfig, _build_calendar, _load_signals,
    )

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE daily_kline "
                     "(symbol TEXT, timestamp INTEGER, date TEXT, open REAL, close REAL, "
                     "high REAL, low REAL, volume REAL, percent REAL, PRIMARY KEY(symbol, date))")
        conn.execute("CREATE TABLE recommendations "
                     "(id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, time TEXT, symbol TEXT, "
                     "name TEXT, category TEXT, score INTEGER, percent REAL, trend TEXT, "
                     "score_breakdown TEXT, source TEXT)")
        conn.execute("INSERT INTO daily_kline VALUES ('300001',0,'2026-06-02',100,100.5,100.5,99.5,1e6,0.5)")
        conn.commit()

        calendar = _build_calendar(conn, "2026-06-01", "2026-06-30")
        cal_index = {d: i for i, d in enumerate(calendar)}
        cal_end = calendar[-1]

        signals = _load_signals(conn, PBConfig(), calendar, cal_index, cal_end)
        conn.close()
        assert signals == []
    finally:
        os.remove(path)


