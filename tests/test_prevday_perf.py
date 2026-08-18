"""综合排序历史复盘测试（2026-08-18 新增，prevday_perf.py）。

核心验证：逐日重建档位（与 today_report 同源）→ 各组次日表现汇总统计正确；
档0 内部类别/弱转强子集、市场环境分层、案例样本收集。
"""
import json
import sqlite3

from prevday_perf import (
    _build_history,
    _market_bucket,
    _next_day_map,
    _stats,
)
from scanner.config import now_beijing


# ── 纯函数 ──
def test_stats_empty():
    assert _stats([]) == (0, None, None, None, None)
    assert _stats([None, None])[0] == 0


def test_stats_hit_win_median():
    n, avg, hit, win, med = _stats([3.0, 9.0, -2.0])
    assert n == 3
    assert round(avg, 2) == 3.33
    assert hit == 33.33333333333333  # 9.0 ≥ 7 → 1/3
    assert win == 66.66666666666666  # 3.0, 9.0 > 0 → 2/3
    assert med == 3.0


def test_market_bucket():
    assert _market_bucket(None) == "未知"
    assert _market_bucket(1.0) == "普涨日(≥+1%)"
    assert _market_bucket(0.5) == "震荡日"
    assert _market_bucket(-1.0) == "普跌日(≤-1%)"
    assert _market_bucket(-2.0) == "普跌日(≤-1%)"


# ── 内存库集成 ──
def _perf_db():
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE appearances (
        id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, name TEXT NOT NULL,
        date TEXT NOT NULL, rank INTEGER, percent REAL, value REAL, UNIQUE(symbol, date))""")
    conn.execute("""CREATE TABLE recommendations (
        id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL, time TEXT NOT NULL,
        symbol TEXT NOT NULL, name TEXT NOT NULL, category TEXT NOT NULL, score INTEGER NOT NULL,
        percent REAL, trend TEXT, next_day_pct REAL, fwd_3d REAL, fwd_5d REAL,
        score_breakdown TEXT, source TEXT DEFAULT 'xueqiu', concept TEXT, accumulated_pct REAL,
        excluded INTEGER DEFAULT 0)""")
    conn.execute("""CREATE TABLE daily_kline (
        symbol TEXT NOT NULL, timestamp INTEGER, date TEXT NOT NULL,
        open REAL, close REAL, high REAL, low REAL, volume REAL, percent REAL,
        PRIMARY KEY(symbol, date))""")
    conn.execute("""CREATE TABLE market_extra_cache (
        symbol TEXT NOT NULL, date TEXT NOT NULL, data_type TEXT NOT NULL,
        payload_json TEXT NOT NULL, updated TEXT NOT NULL, PRIMARY KEY(symbol, data_type))""")
    return conn


def _insert(conn, date, symbol, name, category, score, percent, dims=None,
            next_day=None, trend="", excluded=0):
    conn.execute(
        "INSERT INTO recommendations (date, time, symbol, name, category, score, percent, "
        "trend, next_day_pct, score_breakdown, accumulated_pct, excluded) "
        "VALUES (?, '14:00', ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
        (date, symbol, name, category, score, percent, trend, next_day,
         json.dumps(dims or {}, ensure_ascii=False), excluded),
    )
    conn.commit()


def _build():
    conn = _perf_db()
    # D1: momentum 甜蜜带 → 🎯 档0（+3.0）；short_term 陷阱带 → 档3（-2.0）
    _insert(conn, "2026-08-01", "SZ300001", "动量票", "momentum", 70, 1.5,
            dims={"accumulated_incl_today": 8.0}, next_day=3.0)
    _insert(conn, "2026-08-01", "SZ300002", "陷阱票", "short_term", 60, 9.0,
            dims={"v_st_overbought": True}, next_day=-2.0)
    # D2: rebound 甜蜜带 → 🎯 档0（+9.0 hit）；comeback → 回马枪（+1.0）
    _insert(conn, "2026-08-04", "SZ300003", "反弹票", "rebound", 50, 1.0,
            dims={"accumulated_incl_today": -5.0}, next_day=9.0)
    _insert(conn, "2026-08-04", "SZ300004", "回踩票", "comeback", 101, 2.0,
            dims={"comeback_variant": "回踩"}, next_day=1.0)
    return conn


def test_build_history_groups_and_stats():
    conn = _build()
    hist = _build_history(conn, ["2026-08-01", "2026-08-04"])
    assert len(hist) == 2
    d1, d2 = hist
    # 档位归属
    assert d1["tier0"] == [3.0], f"momentum 甜蜜带应进档0: {d1['tier0']}"
    assert d1["tier3"] == [-2.0], f"陷阱带 short_term 应进档3: {d1['tier3']}"
    assert d2["tier0"] == [9.0] and d2["comeback"] == [1.0]
    # 档0 内部类别
    assert d1["tier0_cats"]["momentum"] == [3.0]
    assert d2["tier0_cats"]["rebound"] == [9.0]
    # 汇总
    all_t0 = d1["tier0"] + d2["tier0"]
    n, avg, hit, win, med = _stats(all_t0)
    assert n == 2 and round(avg, 2) == 6.0 and hit == 50.0
    # 案例样本含日期/名称
    case = d2["cases"]["tier0"][0]
    assert case["date"] == "2026-08-04" and case["name"] == "反弹票" and case["next"] == 9.0
    conn.close()


def test_build_history_market_proxy():
    conn = _build()
    # 补 daily_kline 使市场代理非空
    for d, pct in (("2026-08-01", 2.0), ("2026-08-04", -0.5)):
        conn.execute(
            "INSERT INTO daily_kline (symbol, date, close, percent) VALUES (?, ?, 10.0, ?)",
            ("SZ300999", d, pct),
        )
    conn.commit()
    hist = _build_history(conn, ["2026-08-01", "2026-08-04"])
    assert hist[0]["market"] == 2.0
    assert hist[1]["market"] == -0.5
    conn.close()


def test_next_day_map_filters_null():
    conn = _build()
    m = _next_day_map(conn, "2026-08-01")
    assert m == {"SZ300001": 3.0, "SZ300002": -2.0}
    # 无 next_day 的日期返回空（最新一天自动排除的依据）
    conn.execute("INSERT INTO recommendations (date, time, symbol, name, category, score, percent) "
                 "VALUES ('2026-08-05', '14:00', 'SZ300005', '今日票', 'momentum', 60, 1.0)")
    conn.commit()
    assert _next_day_map(conn, "2026-08-05") == {}
    conn.close()


def test_render_smoke():
    """渲染冒烟：全流程（含案例/结论）不报错，输出含核心区块。"""
    import io
    import contextlib
    from prevday_perf import _render
    conn = _build()
    hist = _build_history(conn, ["2026-08-01", "2026-08-04"])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        pass
    out = _render(hist, 30)
    assert "综合排序历史复盘" in out
    assert "各组次日表现" in out
    assert "档0 🎯 次日大涨画像" in out
    assert "案例" in out
    conn.close()
