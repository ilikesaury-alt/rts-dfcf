"""**先验入行 + 「先强资弱」矛盾提示**（2026-11-04）的单测。

这批改动的缘起（样本外回放结论，见 `scripts/rerank_backtest.py`）：
- 分数在**类别内**毫无区分力（类内 rank IC −0.020，正负天数接近 1:1）⇒ 不该显示在
  排序表里（原来叫「评分」列，显示它等于暗示「高分=更可能涨」）；
- 类别先验是**类内有效**的排序依据（IC +0.027）⇒ 把它放进行里（列「类别·先验」）；
- 主力净占比是类内最强的区分键（IC +0.092）⇒ 当「先验强」与「资金流出」同时成立时，
  行位靠前但证据相反 ⇒ 用确定性标记点出（「先强资弱」，零 LLM 成本）。

守卫线：
1. **口径单源**：先验值来自 `push_gate.classify`（= `CATEGORY_HIT_RATE`），三出口同源；
2. **矛盾判定**：先验档位 = A 且主力净占比 < 0（阈值全部复用现成单源，不新造）；
3. **三出口同待遇**：终端行尾 / 飞书卡片行 / 面板载荷（载荷字段 + 前端 mark kind）。
"""

from __future__ import annotations

import pytest

from scanner.view.model import category_prior_pct, prior_flow_conflict


def _entry(symbol="SZ300001", category="rebound", **over):
    base = {"symbol": symbol, "name": "样例", "category": category}
    base.update(over)
    return base


# ── 1. 先验口径单源 ────────────────────────────────────────────────────────────


def test_category_prior_matches_config_table():
    """先验取值 = `CATEGORY_HIT_RATE`（唯一手抄源），显示为百分数。"""
    from scanner.config import CATEGORY_HIT_RATE

    for cat, hit in CATEGORY_HIT_RATE.items():
        assert category_prior_pct(cat) == pytest.approx(hit * 100)


def test_category_prior_of_unknown_category_falls_back_to_baseline():
    """表外类别 → 全体基线（与 `push_gate.classify` 同一条兑底，不另写一份）。"""
    from scanner.config import CATEGORY_HIT_RATE_DEFAULT

    assert category_prior_pct("不存在的类别") == pytest.approx(CATEGORY_HIT_RATE_DEFAULT * 100)


def test_prior_is_stronger_than_score_in_pool_sorting():
    """回归守卫：**先验 > 分数**（类内 IC +0.027 vs −0.020）。
    这条测试本身不重算 IC（那是 scripts/rerank_backtest.py 的活），
    它守的是「排序表里显示的是先验、不是分数」这个决定不被悄悄改回去。"""
    from scanner.view.model import COLS_POOL

    labels = [c[0] for c in COLS_POOL]
    assert "类别·先验" in labels
    assert "评分" not in labels, "分数在类别内无区分力，不该回到排序表里"


# ── 2. 矛盾判定 ────────────────────────────────────────────────────────────────


def test_conflict_requires_strong_prior_and_outflow():
    """先验强（A 档）且主力净流出 ⇒ 冲突。"""
    flow = {"SZ300001": -3.0}
    assert prior_flow_conflict(_entry(category="rebound"), flow) is True


def test_conflict_not_triggered_when_flow_is_positive():
    assert prior_flow_conflict(_entry(category="rebound"), {"SZ300001": 5.0}) is False


def test_conflict_not_triggered_for_weak_prior():
    """先验不够强（B/C 档）⇒ 不标（否则满屏都是标记，等于没标）。"""
    flow = {"SZ300001": -3.0}
    assert prior_flow_conflict(_entry(category="short_term"), flow) is False
    assert prior_flow_conflict(_entry(category="new_face"), flow) is False


def test_conflict_needs_flow_data():
    """资金流缺失 ⇒ 不标（缺失 ≠ 流出，与全仓 fail-open 语义一致）。"""
    assert prior_flow_conflict(_entry(category="rebound"), {}) is False


def test_conflict_uses_the_same_flow_chain_as_the_icon():
    """资金流取值必须与行尾 ▼ 图标同一条链（行内 dims → flow_map），
    否则会出现「图标说流出、标记说不冲突」。"""
    entry = _entry(category="rebound", score_breakdown={"fund_flow_main_pct": -2.0})
    assert prior_flow_conflict(entry, {}) is True


# ── 3. 三出口同待遇 ────────────────────────────────────────────────────────────


def _fake_view():
    from scanner.view.model import MainRow, ScanView

    rows = []
    for i, (cat, _flow) in enumerate((("rebound", -4.0), ("short_term", -4.0))):
        entry = _entry(symbol=f"SZ30000{i}", category=cat)
        entry["score"] = 80
        rows.append(
            MainRow(
                entry=entry,
                rank=None,
                accum=0.0,
                score=80.0,
                composite_score=0.0,
                core=False,
                cat_label="RBD" if cat == "rebound" else "ST",
                pct=1.0,
                current=10.0,
                sector="人工智能",
                is_new_entry=False,
                prior_pct=category_prior_pct(cat),
                prior_flow_conflict=cat == "rebound",
            )
        )
    return ScanView(
        main_rows=rows,
        breakout_mark={},
        flow_pct_map={"SZ300000": -4.0, "SZ300001": -4.0},
        last_ranks={},
        weak=False,
        warnings=[],
        rule_result=None,
        beauty_mark={},
        guxing_mark={},
        hot_rows=[],
        offboard_rows=[],
        onboard_rows=[],
        hist_rows=[],
        flow_filtered=0,
        market_idx_pct=0.0,
    )


def test_terminal_shows_prior_column_and_conflict_marker(capsys):
    from scanner.view.render import render_terminal

    render_terminal(_fake_view())
    out = capsys.readouterr().out
    assert "类别·先验" in out
    assert "RBD·17.9%" in out
    assert "先强资弱" in out
    # 只有先验强的那一行带矛盾标记（short_term 先验 6.2% ⇒ B/C 档 ⇒ 不标）
    assert out.count("先强资弱") == 1


def test_feishu_card_shows_prior_and_conflict_marker():
    from scanner.feishu import build_feishu_card

    card = build_feishu_card(_fake_view(), gem_total=100)
    section = next(
        e["text"]["content"]
        for e in card["elements"]
        if e.get("tag") == "div" and e.get("text", {}).get("content", "").startswith("**◆ v1 池选**")
    )
    assert "17.9%" in section
    assert section.count("先强资弱") == 1


def test_panel_payload_carries_prior_and_conflict_flag():
    """面板只渲染不重算 ⇒ 载荷必须带 prior_pct 与 prior_flow_conflict。"""
    from scanner.panel_serialize import serialize_view

    payload = serialize_view(
        _fake_view(),
        extra={"date": "2026-11-04", "time": "10:00:00", "seq": 1, "stocks": {}},
    )
    row = payload["regions"]["main"][0]
    assert row["prior_pct"] == pytest.approx(17.9)
    assert row["prior_flow_conflict"] is True
    assert payload["regions"]["main"][1]["prior_flow_conflict"] is False


def test_panel_web_projection_matches_python_columns():
    """云端前端的列投影表必须与 Python 侧 COLS_POOL **逐项同序同名**（守卫文件比对）。"""
    from pathlib import Path

    from scanner.view.model import COLS_POOL

    ts = Path("rts-panel-cloud/web/src/lib/cols.ts").read_text(encoding="utf-8")
    pool_block = ts.split("const POOL_CELLS")[1].split("};")[0]
    for label, _w, _a in COLS_POOL:
        assert f'"{label}":' in pool_block, f"前端列投影缺 {label}"
    assert '"评分":' not in pool_block, "前端列投影仍留着已删的「评分」列"
