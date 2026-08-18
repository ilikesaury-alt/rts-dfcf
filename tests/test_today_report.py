"""今日综合排序分析报告测试（2026-08-18 新增，today_report.py）。

核心是 _tier0_verdict 纯函数（档0 🎯 票的「位置/资金量能/风险/评级」分析，
依据已回测结论：rebound hit 28.6%、弱转强∩非超买 hit 15.8%、甜蜜带+累计≥6 hit 20%；
风险项：尾盘回吐/RSI顶背离/主力净流出≤-8%/超买/疲劳/8-10%陷阱带（非 short_term）。
资金流仅作规避信号——2026-08-10 加分已归零，当日主力流入=追涨资金次日兑现，
分析中「主力流入」不得提升评级（仅展示图标）。
"""
import json
import sqlite3

from scanner.config import now_beijing
from scanner.database import get_today_recommendations
from today_report import _build_report, _tier0_verdict


def _entry(category="momentum", score=60, percent=1.0, accum=8.0, dims=None,
           live=None, has_live=True, symbol="SZ300001", name="测试",
           concept="华为概念", trend="放量启动"):
    e = {
        "symbol": symbol, "name": name, "category": category, "score": score,
        "percent": percent, "concept": concept, "trend": trend,
        "score_breakdown": dims or {}, "_accum": accum,
    }
    if has_live:
        e["live_rank"] = 50
        e["live_percent"] = live if live is not None else percent
    return e


# ── _tier0_verdict 纯函数 ──
def test_verdict_rebound_sweet_band_strong():
    """rebound 甜蜜带低吸 + 主力流入 → ★★★ 首选（类别 hit 28.6% 最强）。
    注意：主力流入只展示图标，不参与正向加分（资金流 2026-08-10 归零规避语义）。"""
    a = _tier0_verdict(_entry(category="rebound", percent=1.5, accum=-8.0,
                              dims={"fund_flow_main_pct": 6.0}), {})
    assert a["verdict"] >= 2 and a["label"] == "首选"
    assert a["risks"] == []
    assert a["flow"] == 6.0 and a["flow_icon"] == "▲"
    assert any("低吸带" in p for p in a["pos_detail"])
    assert "超跌反弹位" in a["pos"]


def test_verdict_rebound_tail_pullback_demotes():
    """尾盘回吐（推荐后回落 ≥1.5pp，追高兑现风险）→ ★★★ 降为 ★★。"""
    a = _tier0_verdict(_entry(category="rebound", percent=1.5, accum=-8.0,
                              dims={"fund_flow_main_pct": 6.0}, live=-0.5), {})
    assert a["verdict"] == 1 and a["label"] == "可参与"
    assert any("尾盘回吐" in r for r in a["risks"])


def test_verdict_momentum_rsi_divergence_caution():
    """RSI 顶背离（v_mo_divergence<0）→ ★ 谨慎。"""
    a = _tier0_verdict(_entry(category="momentum", percent=6.0, accum=10.0,
                              dims={"v_mo_divergence": -10, "fund_flow_main_pct": 1.0}), {})
    assert a["verdict"] <= 0 and a["label"] == "谨慎"
    assert "RSI顶背离" in a["risks"]


def test_verdict_short_term_weak_to_strong_strong():
    """short_term 弱转强∩非超买 → ★★★ 首选（hit 15.8%，唯一有效子集）。
    2026-08-18 口径修正：short_term 不看涨幅带（规律在弱转强），位置详情
    不应出现 死区/陷阱 标签（那是 momentum/new_face 口径，贴上去会误导）。"""
    a = _tier0_verdict(_entry(category="short_term", percent=2.0, accum=-4.0,
                              dims={"v_st_weak": 8}), {})
    assert a["verdict"] >= 2 and a["label"] == "首选"
    assert a["pos"] == "弱转强低位"
    assert not any(("死区" in p) or ("陷阱" in p) for p in a["pos_detail"]), a["pos_detail"]


def test_verdict_short_term_non_weak_not_preferred():
    """short_term 非弱转强（甜蜜带但无弱转强）→ 基线 1，不享受首选特权。"""
    a = _tier0_verdict(_entry(category="short_term", percent=5.0, accum=8.0, dims={}), {})
    assert a["verdict"] == 1 and a["label"] == "可参与"


def test_verdict_outflow_risk():
    """主力净流出 ≤-8% → 风险 + 评级扣分（规避语义）。"""
    a = _tier0_verdict(_entry(category="momentum", percent=6.0, accum=10.0,
                              dims={"fund_flow_main_pct": -9.0}), {})
    assert any("主力净流出" in r for r in a["risks"])
    assert a["verdict"] <= 0


def test_verdict_flow_map_fallback():
    """dims 无资金流时回退 flow_pct_map（DB 快照）。"""
    a = _tier0_verdict(_entry(category="momentum", percent=6.0, accum=10.0, dims={}),
                       {"SZ300001": 6.0})
    assert a["flow"] == 6.0


def test_verdict_no_live_no_tail_pullback():
    """无实时数据（掉榜行无 appearances）→ 不误判尾盘回吐（fail-open）。"""
    a = _tier0_verdict(_entry(category="rebound", percent=1.5, accum=-8.0,
                              dims={}, has_live=False), {})
    assert a["verdict"] >= 2 and a["risks"] == []


# ── _build_report 集成（内存库）──
def _report_db():
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
    conn.execute("""CREATE TABLE market_extra_cache (
        symbol TEXT NOT NULL, date TEXT NOT NULL, data_type TEXT NOT NULL,
        payload_json TEXT NOT NULL, updated TEXT NOT NULL, PRIMARY KEY(symbol, data_type))""")
    conn.execute("""CREATE TABLE scan_quality_log (
        date TEXT, time TEXT, gem_count INTEGER, fetch_failed INTEGER,
        today_bar_missing INTEGER, minute_fallback INTEGER, stale_recs INTEGER, updated TEXT)""")
    return conn


def _insert_rec(conn, symbol, name, category, score, percent, trend="",
                concept="", dims=None, accum=None, excluded=0):
    today = now_beijing().date().isoformat()
    conn.execute(
        "INSERT INTO recommendations (date, time, symbol, name, category, score, percent, trend, "
        "concept, score_breakdown, accumulated_pct, excluded) "
        "VALUES (?, '14:00', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (today, symbol, name, category, score, percent, trend, concept,
         json.dumps(dims or {}, ensure_ascii=False), accum, excluded),
    )
    conn.commit()


def test_build_report_tiers_and_analysis():
    """集成：甜蜜带 rebound → 档0 首选；8-10% momentum → 档3；comeback → 回马枪资金质量。"""
    conn = _report_db()
    _insert_rec(conn, "SZ300001", "反弹票", "rebound", 47, 1.5, trend="阴跌企稳",
                concept="华为概念", dims={"fund_flow_main_pct": 6.0,
                                        "v_rb_volume_detail": "vol_healthy"}, accum=-8.0)
    _insert_rec(conn, "SZ300002", "陷阱票", "momentum", 70, 9.0, trend="放量启动",
                dims={"v_mo_divergence": -10, "fund_flow_main_pct": 1.0}, accum=12.0)
    _insert_rec(conn, "SZ300003", "回踩票", "comeback", 101, 2.4, trend="回踩·到买点",
                dims={"comeback_variant": "回踩", "fund_flow_main_pct": 8.0,
                      "comeback_signals": "缩量/未破位"}, accum=3.0)
    conn.commit()
    rep = _build_report(conn, now_beijing().date().isoformat(), None)
    assert not rep["empty"]
    assert len(rep["tier0"]) == 1, "甜蜜带 rebound 应进档0"
    assert rep["tier0"][0]["label"] == "首选"
    assert rep["tier0"][0]["symbol"] == "SZ300001"
    assert len(rep["tier3"]) == 1, "8-10% 陷阱带 momentum 应进档3"
    assert rep["tier3"][0]["symbol"] == "SZ300002"
    assert len(rep["comeback_flow"]) == 1
    assert rep["comeback_flow"][0]["flow"] == 8.0
    assert rep["main"] == 2 and rep["comeback"] == 1


def test_build_report_excluded_and_quality():
    """被移出票与数据质量快照进报告；excluded 行不参与档位（get_today_recommendations 已过滤）。"""
    conn = _report_db()
    _insert_rec(conn, "SZ300001", "正常票", "momentum", 60, 1.0, dims={}, accum=8.0)
    _insert_rec(conn, "SZ300003", "过热票", "short_term", 59, 1.0, dims={}, accum=112.0)
    _insert_rec(conn, "SZ300002", "妖股", "short_term", 59, 6.0, dims={}, accum=112.0, excluded=1)
    conn.execute(
        "INSERT INTO scan_quality_log (date, time, gem_count, fetch_failed, today_bar_missing, "
        "minute_fallback, stale_recs, updated) VALUES (?, '15:00', 15, 0, 0, 0, 0, '15:00')",
        (now_beijing().date().isoformat(),),
    )
    conn.commit()
    rep = _build_report(conn, now_beijing().date().isoformat(), None)
    assert len(rep["excluded"]) == 1 and rep["excluded"][0]["symbol"] == "SZ300002"
    assert rep["quality"]["gem_count"] == 15 and rep["quality"]["fetch_failed"] == 0
    # 盘中新鲜度：质量快照时间 + 最近推荐时间（2026-08-18 新增）
    assert rep["quality_time"] == "15:00"
    assert rep["last_rec_time"] == "14:00"
    # 非 excluded 的过热票（累计112 ≥50）→ 档3（累计过热优先于 🎯）
    assert rep["tier3"][0]["symbol"] == "SZ300003"
    assert [a["symbol"] for a in rep["tier0"]] == ["SZ300001"], "过热票不应进档0（累计≥50 优先劣后）"


def test_build_report_empty_date():
    conn = _report_db()
    rep = _build_report(conn, "2026-08-01", None)
    assert rep["empty"] is True


def test_build_report_historical_as_of():
    """历史日期回放：get_today_recommendations(as_of=...) 与档位判定兼容。"""
    conn = _report_db()
    conn.execute(
        "INSERT INTO recommendations (date, time, symbol, name, category, score, percent, trend, "
        "concept, score_breakdown, accumulated_pct, excluded) "
        "VALUES ('2026-08-17', '14:00', 'SZ300001', '反弹票', 'rebound', 47, 1.5, '阴跌企稳', "
        "'华为概念', ?, -8.0, 0)",
        (json.dumps({"fund_flow_main_pct": 6.0}),),
    )
    conn.commit()
    recs = get_today_recommendations(conn, as_of="2026-08-17")
    assert len(recs) == 1 and recs[0]["symbol"] == "SZ300001"
    rep = _build_report(conn, "2026-08-17", None)
    assert not rep["empty"] and rep["date"] == "2026-08-17"
    assert len(rep["tier0"]) == 1
