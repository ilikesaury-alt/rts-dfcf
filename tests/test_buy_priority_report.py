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


def _cand_conn() -> sqlite3.Connection:
    """带 `get_today_recommendations` 所需列（含 trend/time）的内存库。"""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE recommendations (id INTEGER PRIMARY KEY, symbol TEXT, name TEXT, category TEXT,"
        " score REAL, percent REAL, concept TEXT, accumulated_pct REAL, score_breakdown TEXT,"
        " date TEXT, excluded INTEGER, trend TEXT, time TEXT)"
    )
    return conn


def _ins(conn: sqlite3.Connection, rows: list[tuple]) -> None:
    conn.executemany(
        "INSERT INTO recommendations (symbol,name,category,score,percent,concept,accumulated_pct,"
        "score_breakdown,date,excluded,trend,time) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [(s, n, c, sc, 1.0, None, None, None, d, ex, None, "09:35:00") for s, n, c, sc, d, ex in rows],
    )


def test_candidate_scope_excludes_retired_categories():
    conn = _cand_conn()
    _ins(
        conn,
        [
            ("SZ300001", "甲", "momentum", 50, "2026-11-05", 0),
            ("SZ300002", "乙", "pool_pick", 50, "2026-11-05", 0),
            ("SZ300003", "丙", "core_dip", 50, "2026-11-05", 0),
            ("SZ300004", "丁", "short_term", 50, "2026-11-05", 1),
        ],
    )
    got = bpr.load_candidates(conn, "2026-11-05")
    assert [r["symbol"] for r in got] == ["SZ300001"], [r["symbol"] for r in got]
    conn.close()


def test_candidate_dedupe_keeps_leaderboard_category():
    """同一票当日同时有 core_dip 与 short_term 两行 ⇒ **只出现一次**，且保留榜上类别。

    （2026-11-05 实测 8 只如此；不去重会在报告里把同一只票列两遍。）
    """
    conn = _cand_conn()
    _ins(
        conn,
        [
            ("SZ300007", "汉威科技", "core_dip", 90, "2026-11-05", 0),  # 低优桶但高分
            ("SZ300007", "汉威科技", "short_term", 40, "2026-11-05", 0),  # 榜上类别但低分
        ],
    )
    got = bpr.load_candidates(conn, "2026-11-05")
    assert len(got) == 1, got
    assert got[0]["category"] == "short_term", got[0]  # 榜上类别优先，不被 core_dip 遮蔽
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


# ── 2026-11-11 关键词扩充守卫（行为变更：只扩未验证表，已验证集合不动）──


def test_expanded_keywords_recognize_previously_missed_events():
    """本次「10 日窗口 0 命中」的四类漏检：回购 / 要约收购 / 诉讼（窗口外）/ 再融资。"""
    ann = [
        {"date": "2026-11-10", "title": "智信精密:深圳市智信精密仪器股份有限公司回购股份方案"},
        {"date": "2026-11-06", "title": "宇邦新材:关于要约收购提示性公告期满60日的说明"},
        {"date": "2026-10-08", "title": "波长光电:关于公司收到起诉书的公告"},
        {"date": "2026-09-24", "title": "川环科技:关于向特定对象发行股票申请获得同意注册批复的公告"},
        {"date": "2026-11-10", "title": "概念动态:川环科技新增人形机器人概念"},  # 非公告事件
    ]
    hits10 = bpr.classify_events(ann, signal_date="2026-11-11")
    assert "回购" in hits10
    assert "要约收购" in hits10
    assert "诉讼仲裁" not in hits10, "10-08 在 10 日窗口外，不得命中"
    assert "再融资" not in hits10, "09-24 在窗口外"
    hits60 = bpr.classify_events(ann, signal_date="2026-11-11", lookback=60)
    assert "诉讼仲裁" in hits60 and "再融资" in hits60
    assert all("人形机器人" not in h for h in hits60), "概念动态不是公告事件"


def test_expanded_keywords_keep_verified_negative_set():
    """扩充只动 ANN_UNVERIFIED；已验证负面集合逐字不变（否则会悄悄多降档）。"""
    assert bpr.ANN_NEGATIVE_VERIFIED == ("减持", "对外担保", "股权激励")
    assert "回购" in bpr.ANN_UNVERIFIED and "要约收购" in bpr.ANN_UNVERIFIED
    repurchase = bpr.classify([_row(ann=["回购"])])[0]
    assert not any("已验证负面" in d for d in repurchase["downgrade"])
    incentive = bpr.classify([_row(ann=["股权激励"])])[0]
    assert any("已验证负面" in d for d in incentive["downgrade"])


def test_repurchase_for_equity_incentive_hits_both_and_still_demotes():
    """智信精密式标题：回购（未验证）+ 股权激励（已验证）⇒ 仍按已验证降档。"""
    ann = [{"date": "2026-11-10", "title": "智信精密:回购股份方案（用于股权激励或员工持股）"}]
    hits = bpr.classify_events(ann, signal_date="2026-11-11")
    assert "回购" in hits and "股权激励" in hits, hits
    row = bpr.classify([_row(ann=hits)])[0]
    assert any("已验证负面" in d for d in row["downgrade"])


def test_bond_holder_change_is_not_refinancing():
    """金杨精密式「债券持有人可转债持有比例变动」≠ 公司再融资，不得误标。"""
    ann = [{"date": "2026-11-07", "title": "金杨精密:关于债券持有人可转债持有比例变动达10%的公告"}]
    assert "再融资" not in bpr.classify_events(ann, signal_date="2026-11-11")
    genuine = [{"date": "2026-09-24", "title": "川环科技:关于向特定对象发行股票申请获得同意注册批复的公告"}]
    assert "再融资" in bpr.classify_events(genuine, signal_date="2026-11-11", lookback=60)


def test_mark_text_covers_new_categories():
    got = bpr.mark_text(["回购", "要约收购", "监管问询", "诉讼仲裁", "中标订单", "业绩预告", "再融资"])
    assert got == "⚑回购/要约/问询/诉讼/中标/预告/再融资", got
