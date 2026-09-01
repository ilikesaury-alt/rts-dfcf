"""飞书推送测试。

覆盖：
  1. 推送决策 should_push（空池 / 冷却 / ok-change / ok-timeout / disabled）
  2. push_feishu 编排（view=None 短路、成功回写状态、失败不回写、_post_card 重试语义）
  3. FEISHU_TOP_N 门控与去重同源（_view_symbols 与 build_feishu_card 同常量）

注意：should_push 是纯函数，依赖模块级 FEISHU_WEBHOOK / FEISHU_MIN_INTERVAL；
PushState 可注入，避免原 _last_push_time/_last_push_symbols 散落 global 的 monkeypatch。
"""

from typing import Any, cast

from scanner.feishu import (
    FEISHU_MIN_INTERVAL,
    PushState,
    build_feishu_card,
    push_feishu,
    should_push,
)

# ── should_push 决策表 ──


def test_should_push_disabled_without_webhook(monkeypatch):
    """未配置 webhook → disabled，整体不推。"""
    monkeypatch.setattr("scanner.feishu.FEISHU_WEBHOOK", "")
    d = should_push(PushState(), {"SZ300001"}, 1000.0)
    assert d.push is False and d.reason == "disabled"


def test_should_push_empty_pool(monkeypatch):
    """配置 webhook 但本轮无任何可展示票 → empty，不推空卡片。"""
    monkeypatch.setattr("scanner.feishu.FEISHU_WEBHOOK", "https://example.com/hook")
    d = should_push(PushState(), set(), 1000.0)
    assert d.push is False and d.reason == "empty"


def test_should_push_cooldown_when_no_change(monkeypatch):
    """票集未变且距上次推送不足 FEISHU_MIN_INTERVAL → cooldown。"""
    monkeypatch.setattr("scanner.feishu.FEISHU_WEBHOOK", "https://example.com/hook")
    state = PushState(last_time=1000.0, last_symbols={"SZ300001"})
    # +FEISHU_MIN_INTERVAL-1 秒：仍在冷却窗内
    d = should_push(state, {"SZ300001"}, 1000.0 + FEISHU_MIN_INTERVAL - 1)
    assert d.push is False and d.reason == "cooldown"


def test_should_push_ok_when_changed(monkeypatch):
    """票集变化（新增一只）→ ok，立即推送。"""
    monkeypatch.setattr("scanner.feishu.FEISHU_WEBHOOK", "https://example.com/hook")
    state = PushState(last_time=1000.0, last_symbols={"SZ300001"})
    d = should_push(state, {"SZ300001", "SZ300002"}, 1000.0)  # 同一时刻也推
    assert d.push is True and d.reason == "ok"


def test_should_push_ok_after_timeout(monkeypatch):
    """票集未变但已超时（≥ FEISHU_MIN_INTERVAL）→ ok，重新推送。"""
    monkeypatch.setattr("scanner.feishu.FEISHU_WEBHOOK", "https://example.com/hook")
    state = PushState(last_time=1000.0, last_symbols={"SZ300001"})
    d = should_push(state, {"SZ300001"}, 1000.0 + FEISHU_MIN_INTERVAL)
    assert d.push is True and d.reason == "ok"


# ── push_feishu 编排 ──


def _fake_view(symbols):
    """构造最小 ScanView 替身，只含 _view_symbols / build_feishu_card 读取的字段。

    main_rows 项需有 .entry(dict) / .rank / .accum / .score；
    flow_pct_map / show_comeback / comeback_rows / show_core_dip / core_dip_rows /
    pool_rows / weak / warnings 为渲染所需最小集合。
    """

    class _Row:
        def __init__(self, sym):
            self.entry = {"symbol": sym, "name": "股", "score": 70, "_candidate": None}
            self.rank = 1
            self.accum = 5.0
            self.score = 70

    class _View:
        def __init__(self, syms):
            self.main_rows = [_Row(s) for s in syms]
            self.flow_pct_map = {}
            self.show_comeback = False
            self.comeback_rows = []
            self.show_core_dip = False
            self.core_dip_rows = []
            self.pool_rows = None
            self.weak = False
            self.warnings = []

    # duck-typed 替身：结构上满足 push_feishu/_view_symbols/build_feishu_card 的读取面，
    # cast 仅为通过类型检查（测试桩不继承 ScanView）。
    return cast(Any, _View(symbols))


def test_push_feishu_none_view_returns_false(monkeypatch):
    """回归（2026-08-17 审查修复）：view 为 None（无 conn / 今日无推荐）不推。"""
    monkeypatch.setattr("scanner.feishu.FEISHU_WEBHOOK", "https://example.com/hook")
    assert push_feishu(None, gem_total=10) is False


def test_push_feishu_success_updates_state(monkeypatch):
    """推送成功 → 回写 last_time / last_symbols；返回 True。"""
    monkeypatch.setattr("scanner.feishu.FEISHU_WEBHOOK", "https://example.com/hook")
    calls = []
    monkeypatch.setattr("scanner.feishu._post_card", lambda card: calls.append(1) or (True, None))
    state = PushState(last_time=0.0, last_symbols=set())
    ok = push_feishu(_fake_view({"SZ300001"}), gem_total=10, state=state)
    assert ok is True
    assert calls == [1]
    assert state.last_symbols == {"SZ300001"}
    assert state.last_time > 0


def test_push_feishu_failure_does_not_update_state(monkeypatch):
    """推送失败（_post_card 返回非 0）→ 返回 False，且不回写状态（避免「假冷却」）。"""
    monkeypatch.setattr("scanner.feishu.FEISHU_WEBHOOK", "https://example.com/hook")
    monkeypatch.setattr("scanner.feishu._post_card", lambda card: (False, "飞书返回非 0: xxx"))
    state = PushState(last_time=0.0, last_symbols=set())
    ok = push_feishu(_fake_view({"SZ300001"}), gem_total=10, state=state)
    assert ok is False
    assert state.last_symbols == set()  # 未回写
    assert state.last_time == 0.0


# ── FEISHU_TOP_N 门控与去重同源 ──


def test_view_symbols_uses_feishu_top_n(monkeypatch):
    """回归：_view_symbols 取自 main_rows[:FEISHU_TOP_N]，与卡片展示条数同源。

    此前 cards 用 top_n=10 而去重用[:10] 双处硬编码，改一处忘另一处会
    「卡片推了但去重没算到」。现两者共用同一常量。
    """
    from scanner.feishu import FEISHU_TOP_N, _view_symbols

    # 超过 TOP_N 的票：前 TOP_N 只进 main_rows，其余仅存在候选但不在 main
    syms = [f"SZ30000{i}" for i in range(FEISHU_TOP_N + 3)]
    view = _fake_view(syms)
    view_syms = _view_symbols(view)

    # 卡片实际展示条数必须等于 TOP_N（首节「策略优选池」）
    card = build_feishu_card(view, gem_total=100)
    # 首节 div 的 text.content 以标题开头；元素序列为 header(hr+div) 交替，故按内容定位
    section_divs = [
        e
        for e in card["elements"]
        if e.get("tag") == "div" and e.get("text", {}).get("content", "").startswith("**◆ 策略优选池**")
    ]
    assert section_divs, "卡片应含「策略优选池」分节"
    first_section = section_divs[0]["text"]["content"]
    assert first_section.startswith("**◆ 策略优选池**")
    shown_lines = [ln for ln in first_section.splitlines() if ln.strip().startswith("`")]
    assert len(shown_lines) == FEISHU_TOP_N
    # 去重集合也应恰好覆盖被展示的 TOP_N 只
    assert view_syms == set(syms[:FEISHU_TOP_N])
