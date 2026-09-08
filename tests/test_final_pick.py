"""终选参考区（scanner.final_pick）单元测试：双挂归一 / 过滤门 / momentum 先验 /
概率排序 / 周期标签 / 去相关 / 渲染。

终选区 = v1+v2+回马/低吸 合池 → 次日大涨概率终选（nextday_prob 单源）→ ≤N 只
+ 落选理由。与决策层互补：决策层答「该不该买」，终选区答「必须持仓时买谁」。
2026-09-05 升级：排序键由「verdict→🎯→score」改为「概率→verdict→score」，
FINAL_PICK_MAX 3→2，新增驱动概念去相关与周期标签。
"""

import sqlite3

import pytest

from scanner.final_pick import (
    build_final_picks,
    dedup_candidates,
    horizon_label,
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


def test_dedup_keeps_comeback_core_dip_low_priority():
    """2026-09-05 扩池：comeback/core_dip 纳入终选池（低优桶，不遮蔽主表类别）。"""
    rows = dedup_candidates(
        [
            _entry(symbol="SZ300001", category="comeback"),
            _entry(symbol="SZ300002", category="core_dip"),
        ]
    )
    assert {r["category"] for r in rows} == {"comeback", "core_dip"}


# ── 过滤门与排序 ──


def test_momentum_excluded_and_marked_in_rejects():
    """momentum 负先验（唯一负超额类别）：永不入选，落选理由标注先验。"""
    mom = _entry(
        symbol="SZ300009",
        name="动量票",
        category="momentum",
        score=99,
        percent=6.0,
        accum=10.0,
        concept="算力",  # 与入选票不同板块，避免同板块理由抢先
    )
    pool = _entry(symbol="SZ300001", name="池选票", category="pool_pick", score=10, percent=3.0, accum=5.0)
    result = _build(_conn(), [mom, pool])
    assert [p["symbol"] for p in result["picks"]] == ["SZ300001"]
    assert any(r["symbol"] == "SZ300009" for r in result["rejects"])
    lines = render_final_pick_lines(result)
    assert any("momentum负先验" in ln for ln in lines)


def test_chase_gate_filters_overcap():
    """追涨门（>8%）：超帽票不进终选也不进落选（与 v1/v2 展示门同源）。"""
    hot = _entry(symbol="SZ300008", name="追高票", percent=9.9)
    pool = _entry(symbol="SZ300001", name="池选票", percent=3.0)
    result = _build(_conn(), [hot, pool])
    syms = {p["symbol"] for p in result["picks"]} | {r["symbol"] for r in result["rejects"]}
    assert "SZ300008" not in syms and "SZ300001" in syms


def test_picks_sorted_by_probability():
    """主排序：次日大涨概率降序。rebound 🎯（甜蜜带+非超买，base 17.9%×OR2.6）
    概率显著高于 unmarked 池选（2-4% 死区，base 2.8%×OR0.70）——分数仅平局末键。"""
    rbd = _entry(
        symbol="SZ300002",
        name="反弹票",
        category="rebound",
        score=10,  # 低分但概率高
        percent=1.5,  # 低吸带 + 非超买 → 🎯
        accum=-8.0,
    )
    pool_hi = _entry(symbol="SZ300001", name="池选高分", score=99)  # percent 3.0 死区
    pool_lo = _entry(symbol="SZ300003", name="池选低分", score=5)
    result = _build(_conn(), [rbd, pool_hi, pool_lo])
    syms = [p["symbol"] for p in result["picks"]]
    assert syms[0] == "SZ300002"  # 概率排序压过跨桶分数
    assert result["picks"][1]["symbol"] == "SZ300001"  # 平级概率按分数


def test_nextday_mark_map_feeds_probability():
    """display 预计算 map 优先：map 标 🎯 的行概率提升并入选首位（同类别对比）。"""
    marked_lo = _entry(symbol="SZ300001", name="池选🎯", category="pool_pick", score=13, percent=1.5)
    unmarked_hi = _entry(symbol="SZ300002", name="池选高分", category="pool_pick", score=93, percent=3.0)
    result = build_final_picks(
        _conn(),
        [unmarked_hi, marked_lo],
        {},
        {},
        nextday_mark={
            ("SZ300001", "pool_pick"): True,
            ("SZ300002", "pool_pick"): False,
        },
    )
    assert result["picks"][0]["symbol"] == "SZ300001"


def test_max_picks_quota():
    """终选配额 ≤ FINAL_PICK_MAX。"""
    from scanner.config import FINAL_PICK_MAX

    entries = [_entry(symbol=f"SZ30000{i}", name=f"票{i}") for i in range(1, 7)]
    result = _build(_conn(), entries)
    assert 0 < len(result["picks"]) <= FINAL_PICK_MAX


# ── 周期标签 ──


def test_horizon_labels_cum3d_vs_nextday():
    """周期标签单源 HOLD_DAYS_BY_CATEGORY：comeback/core_dip = 3日修复，其余次日靶点。"""
    assert horizon_label("comeback") == "3日修复"
    assert horizon_label("core_dip") == "3日修复"
    assert horizon_label("rebound") == "次日靶点"
    assert horizon_label("pool_pick") == "次日靶点"


def test_comeback_pick_carries_horizon_tag():
    cb = _entry(symbol="SZ300004", name="回马票", category="comeback", score=45, percent=1.0)
    result = _build(_conn(), [cb])
    assert result["picks"][0]["_horizon"] == "3日修复"


# ── 去相关 ──


def test_second_pick_prefers_different_theme():
    """买满配额时同驱动概念的第 2 只跳过（同板块齐涨齐跌，覆盖度≈买 1 只）。"""
    from scanner.config import FINAL_PICK_MAX

    a = _entry(symbol="SZ300001", name="A票", category="rebound", score=50, percent=1.5, concept="AI")
    b = _entry(symbol="SZ300002", name="B票", category="rebound", score=40, percent=1.5, concept="AI")
    c = _entry(symbol="SZ300003", name="C票", category="rebound", score=30, percent=1.5, concept="机器人")
    result = _build(_conn(), [a, b, c])
    syms = [p["symbol"] for p in result["picks"]]
    # A 入选，B 同板块被延后，C 不同板块优先入选
    assert syms[0] == "SZ300001"  # A 概率最高首选
    assert syms[1] == "SZ300003"  # C 不同板块优先于 B
    # 名额未满时 B 回填进 picks；名额满时 B 落选
    if FINAL_PICK_MAX >= 3:
        assert syms == ["SZ300001", "SZ300003", "SZ300002"]
    else:
        assert syms == ["SZ300001", "SZ300003"]
        b_reject = next(r for r in result["rejects"] if r["symbol"] == "SZ300002")
        assert _reject_reason_text(result, b_reject).startswith("同板块")


def test_backfill_when_all_same_theme():
    """全池同板块：去相关跳过后名额不满，按概率回填（不因去相关放弃名额）。"""
    a = _entry(symbol="SZ300001", name="A票", category="rebound", score=50, percent=1.5, concept="AI")
    b = _entry(symbol="SZ300002", name="B票", category="rebound", score=40, percent=1.5, concept="AI")
    result = _build(_conn(), [a, b])
    assert [p["symbol"] for p in result["picks"]] == ["SZ300001", "SZ300002"]


def _reject_reason_text(result, v):
    from scanner.final_pick import _reject_reason

    return _reject_reason(v, result["picks"])


# ── 渲染 ──


def test_render_pick_and_reject_lines():
    mom = _entry(
        symbol="SZ300009",
        name="动量票",
        category="momentum",
        score=99,
        percent=6.0,
        accum=10.0,
        concept="算力",
    )
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
    assert any("合格池" in ln and "排序估计非保证" in ln for ln in lines)  # 基准率诚实提示
    assert any("SZ300001" in ln and "池选票" in ln and "P=" in ln and "次日靶点" in ln for ln in lines)
    assert any("落选" in ln and "动量票" in ln for ln in lines)


def test_render_theme_notes_for_second_pick():
    a = _entry(symbol="SZ300001", name="A票", category="rebound", score=50, percent=1.5, concept="AI")
    b = _entry(symbol="SZ300002", name="B票", category="rebound", score=40, percent=1.5, concept="机器人")
    lines = render_final_pick_lines(_build(_conn(), [a, b]))
    pick2 = next(ln for ln in lines if ln.strip().startswith("2."))
    assert "与#1分散" in pick2


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
