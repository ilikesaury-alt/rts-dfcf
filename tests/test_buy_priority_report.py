"""scripts/buy_priority_report.py 的守卫测试（2026-11-05）。

口径类测试：分档规则、公告无前视、标记文本、候选池范围。
**不联网**（取数全在 DB 里，公告那块用假数据喂）。
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location("bpr", ROOT / "scripts" / "buy_priority_report.py")
bpr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bpr)  # type: ignore[union-attr]


def _row(**kw):
    """一条最小可评分的行（默认各项都干净）。"""
    base = {
        "symbol": "SZ300000",
        "name": "测试",
        "category": "momentum",
        "prior_tier": "A",
        "prior_pct": 10.0,
        "price": 20.0,
        "pct": 2.0,
        "main_pct": 6.0,
        "main_net": 1e8,
        "super_net": 1e7,
        "flow_sum": 1e8,
        "flow_sum_long": 1e8,
        "flow_pos_days": 5,
        "flow_days": 5,
        "hist_n": 25,
        "hist_hit": 5,
        "hist_rate": 0.2,
        "hist_avg": 1.0,
        "above_ma5": 3.0,
        "accum_hist5": 5.0,
        "vol_vs_10d": 1.1,
        "vol_ratio": 1.2,
        "turnover": 8.0,
        "amount": 1e9,
        "float_cap": 5e9,
        "prev_close": 19.6,
        "overbought": False,
        "ann": [],
        "rank": 10,
    }
    base.update(kw)
    return base


def test_clean_strong_row_is_first_tier():
    rows = bpr.classify([_row()])
    assert rows[0]["adj_tier"] == "A", rows[0]["score_parts"]
    assert rows[0]["score"] >= bpr.SCORE_T1


def test_hard_veto_goes_to_avoid():
    rows = bpr.classify([_row(main_pct=-9.0)])
    assert rows[0]["adj_tier"] == "X"
    assert any("主力净占比" in v for v in rows[0]["veto"])


def test_chase_limit_goes_to_avoid():
    rows = bpr.classify([_row(pct=9.0)])
    assert rows[0]["adj_tier"] == "X"
    assert any("追高线" in v for v in rows[0]["veto"])


def test_verified_negative_event_demotes():
    """已验证负面公告（减持/对外担保/股权激励）必须真的压分 ⇒ 掉出第一梯队。"""
    clean = bpr.classify([_row()])[0]["score"]
    bad = bpr.classify([_row(ann=["减持"])])[0]
    assert bad["score"] < clean
    assert bad["adj_tier"] != "A"
    assert any("已验证负面" in d for d in bad["downgrade"])


def test_unverified_event_only_hints():
    """未验证事件（质押/减值/终止/解禁）只提示，不降档。"""
    bad = bpr.classify([_row(ann=["股份质押"])])[0]
    assert bad["adj_tier"] == "A"
    assert not any("已验证负面" in d for d in bad["downgrade"])


def test_c_prior_is_demoted():
    rows = bpr.classify([_row(prior_tier="C", prior_pct=6.2)])
    assert rows[0]["adj_tier"] != "A"
    assert any("基线" in d for d in rows[0]["downgrade"])


def test_announcement_lookahead_is_ignored():
    """信号日**之后**的公告不得进判定（无前视）。"""
    ann = [
        {"date": "2026-11-05", "title": "关于股东减持股份的公告"},
        {"date": "2026-11-06", "title": "关于股东减持股份的公告"},
    ]
    hits = bpr.classify_events(ann, signal_date="2026-11-05")
    assert hits == ["减持"], hits


def test_announcement_window_is_respected():
    ann = [
        {"date": "2026-11-04", "title": "关于为子公司提供担保的公告"},
        {"date": "2026-09-01", "title": "关于为子公司提供担保的公告"},
    ]
    hits = bpr.classify_events(ann, signal_date="2026-11-05")
    assert hits == ["对外担保"], hits


def test_mark_text_is_stable():
    assert bpr.mark_text(["减持", "股份质押"]) == "⚑减持/质押"
    assert bpr.mark_text([]) == ""


def test_candidate_scope_excludes_retired_categories():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE recommendations (id INTEGER PRIMARY KEY, symbol TEXT, name TEXT, category TEXT,"
        " score REAL, percent REAL, concept TEXT, accumulated_pct REAL, score_breakdown TEXT,"
        " date TEXT, excluded INTEGER)"
    )
    rows = [
        ("SZ300001", "甲", "momentum", 50, 1.0, None, None, None, "2026-11-05", 0),
        ("SZ300002", "乙", "pool_pick", 50, 1.0, None, None, None, "2026-11-05", 0),
        ("SZ300003", "丙", "core_dip", 50, 1.0, None, None, None, "2026-11-05", 0),
        ("SZ300004", "丁", "short_term", 50, 1.0, None, None, None, "2026-11-05", 1),
    ]
    conn.executemany(
        "INSERT INTO recommendations (symbol,name,category,score,percent,concept,accumulated_pct,"
        "score_breakdown,date,excluded) VALUES (?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    got = bpr.load_candidates(conn, "2026-11-05")
    assert [r["symbol"] for r in got] == ["SZ300001"], [r["symbol"] for r in got]
    conn.close()


def test_spike_followup_counts_only_spike_days():
    """「大涨之后」模式：只统计当日涨幅 ≥5% 的那些日子的次日表现。"""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE recommendations (symbol TEXT, date TEXT, percent REAL, next_day_pct REAL)")
    conn.executemany(
        "INSERT INTO recommendations VALUES (?,?,?,?)",
        [
            ("SZ300001", "2026-10-01", 6.0, -3.0),  # 大涨 → 次日 -3
            ("SZ300001", "2026-10-02", 1.0, 9.0),  # 非大涨 → 不计
            ("SZ300001", "2026-10-03", 7.0, -1.0),  # 大涨 → 次日 -1
        ],
    )
    got = bpr.load_spike_followup(conn, ["SZ300001"], "2026-11-05")
    assert got["SZ300001"]["n"] == 2, got
    assert got["SZ300001"]["avg"] == -2.0, got
    assert got["SZ300001"]["hit"] == 0
    conn.close()


def test_spike_followup_negative_demotes_score():
    """该票大涨之后历史为负 ⇒ 扣分（AI 复核里最硬的反证）。"""
    base = bpr.classify([_row()])[0]["score"]
    bad = bpr.classify([_row(spike={"n": 3, "avg": -4.0, "hit": 0, "worst": -9.0})])[0]
    assert bad["score"] < base
    assert "大涨后历史为负-6" in bad["score_parts"]


def test_minute_below_avg_demotes_score():
    base = bpr.classify([_row()])[0]["score"]
    bad = bpr.classify([_row(minute={"last": 10.0, "avg": 10.5})])[0]
    assert bad["score"] < base
    assert "收盘低于均价-6" in bad["score_parts"]


def test_dragon_tiger_precedent_demotes_score():
    """龙虎榜先例：最近一次「当日大涨上榜」后 D1 大跌 ⇒ 扣分。"""
    base = bpr.classify([_row()])[0]["score"]
    bad = bpr.classify([_row(dtb=[{"date": "2025-12-02", "change": 6.24, "d1": -7.09, "d5": -13.37}])])[0]
    assert bad["score"] < base
    assert "龙虎榜先例-8" in bad["score_parts"]


def test_dragon_tiger_without_spike_does_not_demote():
    """上榜当日没大涨 ⇒ 不是这个模式，不该扣分。"""
    base = bpr.classify([_row()])[0]["score"]
    same = bpr.classify([_row(dtb=[{"date": "2025-12-02", "change": 1.2, "d1": -7.09, "d5": -13.37}])])[0]
    assert same["score"] == base


def test_parse_minute_text():
    text = (
        "分时点数 91｜开盘 19.46｜最新 20.90｜日内最高 21.43｜最低 19.11｜均价 20.70\n"
        "收盘/均价 高于均价（20.90 vs 20.70）\n"
        "全天成交量 3411万股｜尾盘(14:30后)量占比 2.2%"
    )
    got = bpr._parse_minute_text(text)
    assert got["last"] == 20.90
    assert got["avg"] == 20.70
    assert got["tail_pct"] == 2.2


def test_market_env_classifies_broad_rally():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE daily_kline (symbol TEXT, date TEXT, percent REAL)")
    conn.executemany(
        "INSERT INTO daily_kline VALUES (?,?,?)",
        [(f"SZ3000{i:02d}", "2026-11-05", 5.0) for i in range(8)]
        + [(f"SZ3001{i:02d}", "2026-11-05", -2.0) for i in range(2)],
    )
    env = bpr.load_market_env(conn, "2026-11-05")
    assert env["up"] == 8 and env["down"] == 2
    assert "普涨" in env["kind"]
    conn.close()
