"""飞书推送测试。

覆盖：
  1. 推送决策 should_push（空池 / 冷却 / ok-change / ok-timeout / disabled / has_content）
  2. push_feishu 编排（view=None 短路、成功回写状态、失败不回写、_post_card 重试语义）
  3. FEISHU_TOP_N 门控与去重同源（_view_symbols 与 build_feishu_card 同常量）
  4. 飙升区（hot_rows）：去重键**不含**飙升票、但参与「有内容」判据（2026-09-15 P1）
  5. 飙升行定宽：_COLS_HOT_FEISHU 每列宽度 ≥ 格式化输出上界，行宽不随数据参差（2026-09-15 P2）
  6. 失败/成功都写日志、且写进**被隔离的**日志目录（2026-09-15 P3）
  7. _post_card 失败信息带全现场（code / http / body / hook 指纹）

注意：should_push 是纯函数，依赖模块级 FEISHU_WEBHOOK / FEISHU_MIN_INTERVAL；
PushState 可注入，避免原 _last_push_time/_last_push_symbols 散落 global 的 monkeypatch。
日志目录由 conftest 的 autouse fixture `_isolate_log_dir` 重定向到 tmp，
故本文件任何用例都不可能写到生产 logs/。
"""

from pathlib import Path
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


def _fake_view(symbols, *, hot_rows=None, final_pick_lines=None):
    """构造最小 ScanView 替身，只含 _view_symbols / view_has_content / build_feishu_card 读取的字段。

    main_rows 项需有 .entry(dict) / .rank / .accum / .score；
    flow_pct_map / weak / warnings 为渲染所需最小集合。

    2026-09-14：`show_core_dip` / `core_dip_rows` / `pool_rows` / `pool_total` 四个桩字段
    已移除 —— 卡片不再画 v2 池选与核心低吸两节，头部也不再读池选计数。
    2026-09-15：新增 `hot_rows` / `final_pick_lines`（默认 None = 该区块为空），
    供飙升区门控与「有内容」判据的用例使用。
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
            self.weak = False
            self.warnings = []
            self.hot_rows = hot_rows
            self.final_pick_lines = final_pick_lines

    # duck-typed 替身：结构上满足 push_feishu/_view_symbols/build_feishu_card 的读取面，
    # cast 仅为通过类型检查（测试桩不继承 ScanView）。
    return cast(Any, _View(symbols))


def _fake_hot(**over):
    """构造一条 HotCandidate（飙升区行），默认值为「正常交易中的创业板票」。"""
    from scanner.hot_watch import HotCandidate

    fields = {
        "symbol": "SZ300001",
        "code": "300001",
        "name": "特锐德",
        "exchange": "SZ",
        "current": 23.45,
        "percent": 5.6,
        "rank_change": 1234,
        "rank": 8,
        "volume": 1.2e7,
        "amount": 2.9e8,
        "market_capital": 1.2e10,
        "turnover_rate": 3.4,
        "volume_ratio": 1.23,
        "score": 78.5,
        "streak": 3,
    }
    fields.update(over)
    return HotCandidate(**fields)


def _section_titles(card) -> list[str]:
    """卡片中所有分节标题（**◆ xxx** 开头的 div）——用于「画了哪些节」的断言。"""
    return [
        e["text"]["content"].splitlines()[0]
        for e in card["elements"]
        if e.get("tag") == "div" and e.get("text", {}).get("content", "").startswith("**◆")
    ]


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


# ── 日志落盘：隔离 + 成功也记一行（2026-09-15 P3）──


def test_push_feishu_logs_to_isolated_dir(monkeypatch):
    """回归：失败只写进被隔离的 tmp 日志目录，绝不落到生产 logs/feishu_push.log。

    修复前本文件用 `(False, "飞书返回非 0: xxx")` 这个**测试桩假串**模拟失败，却走真实的
    `log_event` → 每跑一次单测就往生产 logs/feishu_push.log 追加一行假失败。那串 `xxx`
    在飞书的真实错误码里根本不存在（真实是 19024 Key Words Not Found / 19001 token invalid …），
    排查「推送为什么没到」时被这堆假记录带偏过。用户可见现象：日志里「今天 18 次推送失败」，
    实际那 18 条全是 pytest 生成的。
    """
    import scanner.log_utils as log_utils

    monkeypatch.setattr("scanner.feishu.FEISHU_WEBHOOK", "https://example.com/hook")
    monkeypatch.setattr("scanner.feishu._post_card", lambda card: (False, "飞书返回非 0: 测试桩"))
    assert push_feishu(_fake_view({"SZ300001"}), gem_total=10, state=PushState()) is False

    monkeypatch.setattr("scanner.feishu._post_card", lambda card: (True, None))
    assert push_feishu(_fake_view({"SZ300002"}), gem_total=10, state=PushState()) is True

    text = (Path(log_utils.LOG_DIR) / "feishu_push.log").read_text(encoding="utf-8")
    assert "push failed: 飞书返回非 0: 测试桩" in text
    # 成功也记一行：否则「最后一条是 push failed」会被误读成「一直坏着」
    assert "push ok:" in text
    # 日志里不得出现 token 本身，只有指纹
    assert "example.com/hook" not in text


def test_post_card_failure_reports_full_context(monkeypatch):
    """_post_card 的失败信息必须带 code / http / body / hook 指纹。

    原实现只有 `飞书返回非 0: {msg}` 一句 —— 分不清「飞书明确拒绝」与「根本不是飞书在回话」
    （代理/网关返回的 JSON 也长这样）。2026-09-15 排查推送时正是卡在这里。
    """
    import scanner.feishu as fh

    class _Resp:
        status_code = 200
        text = '{"code":19024,"data":{},"msg":"Key Words Not Found"}'

        def json(self):
            return {"code": 19024, "data": {}, "msg": "Key Words Not Found"}

    secret = "https://open.feishu.cn/open-apis/bot/v2/hook/2c288ae5-4468-47d0"
    monkeypatch.setattr(fh, "FEISHU_WEBHOOK", secret)
    monkeypatch.setattr(fh.requests, "post", lambda *a, **k: _Resp())

    ok, err = fh._post_card({"elements": []})
    assert ok is False
    assert err is not None
    for want in ("19024", "Key Words Not Found", "http=200", "hook="):
        assert want in err, f"失败信息缺少 {want}：{err}"
    assert "2c288ae5" not in err, "日志不得回显 token 本身"


def test_post_card_non_json_response_keeps_retry_and_body(monkeypatch):
    """响应非 JSON（代理/网关错误页）时仍重试 1 次，并保留状态码与正文片段。"""
    import scanner.feishu as fh

    class _Resp:
        status_code = 502
        text = "<html>502 Bad Gateway</html>"

        def json(self):
            raise ValueError("not json")

    calls: list = []

    def _fake_post(*a, **k):
        calls.append(1)
        return _Resp()

    monkeypatch.setattr(fh, "FEISHU_WEBHOOK", "https://example.com/hook")
    monkeypatch.setattr(fh.requests, "post", _fake_post)
    monkeypatch.setattr(fh.time, "sleep", lambda s: None)

    ok, err = fh._post_card({"elements": []})
    assert ok is False and len(calls) == 2, "非 JSON 仍应退避重试 1 次（与外部故障同口径）"
    assert err is not None and "502" in err and "Bad Gateway" in err


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

    # 卡片实际展示条数必须等于 TOP_N（首节「v1 池选」）
    card = build_feishu_card(view, gem_total=100)
    # 首节 div 的 text.content 以标题开头；元素序列为 header(hr+div) 交替，故按内容定位
    section_divs = [
        e
        for e in card["elements"]
        if e.get("tag") == "div" and e.get("text", {}).get("content", "").startswith("**◆ v1 池选**")
    ]
    assert section_divs, "卡片应含「v1 池选」分节"
    first_section = section_divs[0]["text"]["content"]
    assert first_section.startswith("**◆ v1 池选**")
    shown_lines = [ln for ln in first_section.splitlines() if ln.strip().startswith("`")]
    assert len(shown_lines) == FEISHU_TOP_N
    # 去重集合也应恰好覆盖被展示的 TOP_N 只
    assert view_syms == set(syms[:FEISHU_TOP_N])


# ── 飙升区（hot_rows）：参与「有内容」判据、不参与去重键（2026-09-15 P1）──


def test_hot_only_view_is_pushable_but_hot_is_not_a_dedup_key(monkeypatch):
    """P1 回归：主线空池、飙升区有行 → 卡片仍有区块可画，必须能推。

    修复前 should_push 只按 symbols 判空 ⇒ 整卡不推，而终端 render_terminal 照画飙升区，
    与 build_feishu_card 声明的「与终端分节一一对应」直接矛盾。
    同时钉死：**飙升票不进去重键**（分钟级变动，计入会让 has_change 每轮为真、击穿节流）。
    """
    from scanner.feishu import _view_symbols, view_has_content

    monkeypatch.setattr("scanner.feishu.FEISHU_WEBHOOK", "https://example.com/hook")
    view = _fake_view([], hot_rows=[_fake_hot()])

    assert _view_symbols(view) == set(), "飙升票不是去重键"
    assert view_has_content(view) is True, "仅飙升区有内容时也必须可推"

    card = build_feishu_card(view, gem_total=100)
    assert any("沪深飙升" in t for t in _section_titles(card)), "卡片应画出飙升区"

    ok = should_push(PushState(), _view_symbols(view), 1000.0, has_content=view_has_content(view))
    assert ok.push is True and ok.reason == "ok"
    # 缺省 has_content（= 旧口径「按票集判空」）会退化成 empty —— 写死以防回退
    assert should_push(PushState(), _view_symbols(view), 1000.0).reason == "empty"


def test_should_push_empty_only_when_card_has_no_section(monkeypatch):
    """只有三个来源（终选参考 / v1 池选 / 飙升区）全空才算空卡片 → empty。"""
    from scanner.feishu import view_has_content

    monkeypatch.setattr("scanner.feishu.FEISHU_WEBHOOK", "https://example.com/hook")
    empty = _fake_view([])
    assert view_has_content(empty) is False
    assert should_push(PushState(), set(), 1000.0, has_content=view_has_content(empty)).reason == "empty"

    # 仅终选参考有内容（main_rows 仍为空）→ 也必须推
    fp_only = _fake_view([], final_pick_lines=["终选参考 # 1 只", "  300001 股1 70分"])
    assert view_has_content(fp_only) is True
    assert should_push(PushState(), set(), 1000.0, has_content=view_has_content(fp_only)).push is True


def test_hot_only_push_keeps_min_interval(monkeypatch):
    """仅飙升区有内容时按 FEISHU_MIN_INTERVAL 节流，而不是每轮（60s）一张卡。

    这是「不并入去重键」的量化理由：symbols 恒空 ⇒ has_change 恒 False ⇒ 走冷却分支；
    若把飙升票并进去，has_change 几乎每轮为真，should_push 会绕过冷却直接推。
    """
    from scanner.feishu import _view_symbols, view_has_content

    monkeypatch.setattr("scanner.feishu.FEISHU_WEBHOOK", "https://example.com/hook")
    view = _fake_view([], hot_rows=[_fake_hot()])
    syms, content = _view_symbols(view), view_has_content(view)
    now = 1000.0

    inner = should_push(PushState(last_time=now - 1, last_symbols=set()), syms, now, has_content=content)
    assert inner.reason == "cooldown"
    timed_out = should_push(
        PushState(last_time=now - FEISHU_MIN_INTERVAL, last_symbols=set()), syms, now, has_content=content
    )
    assert timed_out.push is True


def test_push_feishu_posts_hot_only_card(monkeypatch):
    """端到端：仅飙升区的日子确实会 POST 出卡片（修复前 push_feishu 直接 return False）。"""
    monkeypatch.setattr("scanner.feishu.FEISHU_WEBHOOK", "https://example.com/hook")
    posted: list = []
    monkeypatch.setattr("scanner.feishu._post_card", lambda card: posted.append(card) or (True, None))

    state = PushState()
    assert push_feishu(_fake_view([], hot_rows=[_fake_hot()]), gem_total=10, state=state) is True
    assert posted, "应有卡片被推送"
    assert any("沪深飙升" in t for t in _section_titles(posted[0]))
    assert state.last_symbols == set()  # 去重键仍为空（飙升区不参与）


# ── 飙升行定宽（2026-09-15 P2）──


def test_hot_row_width_is_uniform():
    """P2 回归：飙升行可见宽度必须恒定（含双宽「万手/亿手/★」与各列上界值）。

    修复前用 f-string 的 `{x:>10}` 按**字符数**补位，双宽单元把行撑宽 —— 实测同批 93/97/97。
    用例刻意取到 _COLS_HOT_FEISHU 各列的宽度上界（见其逐列注释），
    任何一列宽度定小了，本用例都会红。
    """
    from scanner.display import _vis_len
    from scanner.feishu import _COLS_HOT_FEISHU, _fmt_hot_row_feishu

    expected = sum(w for w, _ in _COLS_HOT_FEISHU) + (len(_COLS_HOT_FEISHU) - 1) + 2  # 分隔空格 + 反引号
    cases = [
        {},  # 常规
        # 全字段缺失 → 全部渲染为「—」（宽字符，最易触发字符数/可见宽度混用）
        {"volume": 0, "amount": 0, "market_capital": 0, "turnover_rate": 0, "volume_ratio": 0},
        # 各列上界
        {
            "volume": 9.99999e9,
            "amount": 9.99999e11,
            "market_capital": 9.999e11,
            "current": 9999.99,
            "percent": -9.9,
            "rank_change": 9999,
            "volume_ratio": 99.99,
            "turnover_rate": 99.9,
            "streak": 999,
        },
        {"name": "超长名字啊"},  # 自由文本列超宽 → 必须截断而不是撑宽
    ]
    widths = {_vis_len(_fmt_hot_row_feishu(_fake_hot(**c), i)) for i, c in enumerate(cases, 1)}
    assert widths == {expected}, f"行宽参差：{sorted(widths)}（应恒为 {expected}）"

    # 数值列宽度不足会走「截断」——比错列更糟（显示错值），故单独钉住上界不被截断
    extreme = _fmt_hot_row_feishu(_fake_hot(volume=9.99999e9, amount=9.99999e11), 1)
    assert "9999.99万手" in extreme and "9999.99亿" in extreme


def test_hot_row_columns_match_terminal():
    """列数必须与终端 COLS_HOT 一致（列序/含义由 _COLS_HOT_FEISHU 的逐列注释对齐）。"""
    from scanner.display import COLS_HOT
    from scanner.feishu import _COLS_HOT_FEISHU

    assert len(_COLS_HOT_FEISHU) == len(COLS_HOT), "终端加/删了列，卡片压缩列规格没跟着改"
