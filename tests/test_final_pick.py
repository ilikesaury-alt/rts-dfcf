"""终选参考区（scanner.final_pick）单元测试：双挂归一 / 过滤门 / momentum 先验 / 渲染。

终选区 = v1+v2 合池 → 档0画像评级（today_report._tier0_verdict 单源）→ ≤N 只终选
+ 落选理由。与决策层互补：决策层答「该不该买」，终选区答「必须持仓时买谁」。
"""

import sqlite3

import pytest

from scanner.final_pick import (
    build_final_picks,
    dedup_candidates,
    render_final_pick_lines,
)


def _entry(
    symbol="SZ300001",
    name="测试",
    category="pool_pick",
    score=10,
    percent=3.0,
    accum=5.0,
    dims=None,
    live_percent=None,
    concept="数据中心",
):
    e = {
        "symbol": symbol,
        "name": name,
        "category": category,
        "score": score,
        "percent": percent,
        "concept": concept,
        "trend": "整理",
        "score_breakdown": dims or {},
        "_accum": accum,
    }
    if live_percent is not None:
        e["live_percent"] = live_percent
    return e


def _conn(index_pct: float = 1.0) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE market_index_log (date TEXT PRIMARY KEY, index_pct REAL)")
    conn.executemany(
        "INSERT INTO market_index_log VALUES (?, ?)",
        [(f"2026-09-0{i}", index_pct) for i in range(1, 3)],
    )
    return conn


def _build(conn, entries):
    return build_final_picks(conn, entries, {}, {})


# ── 双挂归一 ──


def test_dedup_prefers_short_term_row():
    """同 symbol 双挂（short_term + pool_pick）：按类别优先级取 short_term 行。"""
    st = _entry(category="short_term", score=90, name="双挂票")
    pp = _entry(category="pool_pick", score=10, name="双挂票")
    rows = dedup_candidates([pp, st])
    assert len(rows) == 1 and rows[0]["category"] == "short_term"


def test_dedup_keeps_all_distinct_symbols():
    rows = dedup_candidates([_entry(symbol="SZ300001"), _entry(symbol="SZ300002")])
    assert len(rows) == 2


# ── 过滤门与排序 ──


def test_momentum_excluded_and_marked_in_rejects():
    """momentum 负先验（唯一负超额类别）：永不入选，落选理由标注先验。"""
    mom = _entry(symbol="SZ300009", name="动量票", category="momentum", score=99, percent=6.0, accum=10.0)
    pool = _entry(symbol="SZ300001", name="池选票", category="pool_pick", score=10, percent=3.0, accum=5.0)
    result = _build(_conn(), [mom, pool])
    assert [p["symbol"] for p in result["picks"]] == ["SZ300001"]
    assert any(r["symbol"] == "SZ300009" for r in result["rejects"])


def test_chase_gate_filters_overcap():
    """追涨门（>8%）：超帽票不进终选也不进落选（与 v1/v2 展示门同源）。"""
    hot = _entry(symbol="SZ300008", name="追高票", percent=9.9)
    pool = _entry(symbol="SZ300001", name="池选票", percent=3.0)
    result = _build(_conn(), [hot, pool])
    syms = {p["symbol"] for p in result["picks"]} | {r["symbol"] for r in result["rejects"]}
    assert "SZ300008" not in syms and "SZ300001" in syms


def test_picks_sorted_by_verdict_then_score():
    """主排序：档0画像评级（verdict）降序，次键评分降序。rebound 基线 2 > 池选基线 1。"""
    rbd = _entry(
        symbol="SZ300002",
        name="反弹票",
        category="rebound",
        score=60,
        percent=1.5,
        accum=-8.0,
        dims={"fund_flow_main_pct": 6.0},
    )
    pool_hi = _entry(symbol="SZ300001", name="池选高分", score=99)
    pool_lo = _entry(symbol="SZ300003", name="池选低分", score=5)
    result = _build(_conn(), [rbd, pool_hi, pool_lo])
    syms = [p["symbol"] for p in result["picks"]]
    assert syms.index("SZ300002") < syms.index("SZ300001")  # verdict 优先于分数
    assert syms.index("SZ300001") < syms.index("SZ300003")  # 平级按分数


def test_max_picks_quota():
    entries = [_entry(symbol=f"SZ30000{i}", name=f"票{i}") for i in range(1, 7)]
    result = _build(_conn(), entries)
    assert 0 < len(result["picks"]) <= 3


def test_marked_beats_higher_score_same_verdict():
    """同评级平局：🎯（次日大涨画像）优先于跨桶不可比分数（score 仅作平局末键）。"""
    marked_lo = _entry(symbol="SZ300001", name="池选🎯", category="pool_pick", score=13)
    unmarked_hi = _entry(symbol="SZ300002", name="ST高分", category="short_term", score=93)
    result = build_final_picks(
        _conn(), [unmarked_hi, marked_lo], {}, {}, nextday_mark={("SZ300001", "pool_pick"): True}
    )
    assert result["picks"][0]["symbol"] == "SZ300001"


# ── 渲染 ──


def test_render_pick_and_reject_lines():
    mom = _entry(symbol="SZ300009", name="动量票", category="momentum", score=99, percent=6.0, accum=10.0)
    pool = _entry(
        symbol="SZ300001",
        name="池选票",
        score=13,
        percent=3.0,
        accum=5.0,
        dims={"fund_flow_main_pct": 8.5},
        live_percent=4.8,
    )
    result = _build(_conn(), [mom, pool])
    lines = render_final_pick_lines(result)
    assert any("终选参考" in ln for ln in lines)
    assert any("SZ300001" in ln and "池选票" in ln for ln in lines)
    assert any("落选" in ln and "动量票" in ln and "momentum负先验" in ln for ln in lines)


def test_render_gate_closed_hint():
    """大盘门关（指数 < 0）：标题带「仅观察参考」提示——终选区门关也出结论。"""
    pool = _entry(name="池选票")
    result = _build(_conn(index_pct=-1.5), [pool])
    lines = render_final_pick_lines(result)
    assert any("大盘门关" in ln for ln in lines)
    assert any("池选票" in ln for ln in lines)


def test_render_empty_pool():
    entries = [_entry(symbol="SZ300008", percent=9.9)]  # 全部被追涨门过滤
    lines = render_final_pick_lines(_build(_conn(), entries))
    assert any("无合格标的" in ln for ln in lines)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
