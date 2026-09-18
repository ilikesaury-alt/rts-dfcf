"""飞书推送测试。

覆盖：
  1. 推送决策 should_push（空池 / 冷却 / ok-change / ok-timeout / disabled / has_content）
  2. push_feishu 编排（view=None 短路、成功回写状态、失败不回写、_post_card 重试语义）
  3. FEISHU_TOP_N 门控与去重同源（_view_symbols 与 build_feishu_card 同常量）
  4. 飙升区（hot_rows）：去重键**不含**飙升票、但参与「有内容」判据（2026-09-15 P1）
  5. 飙升行定宽：_COLS_HOT_FEISHU 每列宽度 ≥ 格式化输出上界，行宽不随数据参差（2026-09-15 P2）
  6. 失败/成功都写日志、且写进**被隔离的**日志目录（2026-09-15 P3）
  7. _post_card 失败信息带全现场（code / http / body / hook 指纹）
  8. v1 回捞区（hist_rows，2026-09-16）：与飙升区同款 —— 进去重键会击穿节流、
     参与「有内容」判据、行定宽、列数与终端 COLS_HIST 一致、分节在飙升区之前

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


def _fake_view(symbols, *, hot_rows=None, hist_rows=None, final_pick_lines=None):
    """构造最小 ScanView 替身，只含 _view_symbols / view_has_content / build_feishu_card 读取的字段。

    main_rows 项需有 .entry(dict) / .rank / .accum / .score；
    flow_pct_map / weak / warnings 为渲染所需最小集合。

    2026-09-14：`show_core_dip` / `core_dip_rows` / `pool_rows` / `pool_total` 四个桩字段
    已移除 —— 卡片不再画 v2 池选与核心低吸两节，头部也不再读池选计数。
    2026-09-15：新增 `hot_rows` / `final_pick_lines`（默认 None = 该区块为空），
    供飙升区门控与「有内容」判据的用例使用。
    2026-09-16：新增 `hist_rows`（默认 None = 回捞区为空）—— 该字段是 build_feishu_card /
    view_has_content 用 getattr 读的，桩必须显式持有，否则「回捞区有内容」的用例会静默退化成
    「该区为空」而假绿。
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
            self.hist_rows = hist_rows
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


def _fake_hist(**over):
    """构造一条 HistCandidate（v1 回捞区行），默认值为「2 日前进过 v1、今日回调到位」。"""
    from scanner.historical_watch import HistCandidate

    fields = {
        "symbol": "SZ300750",
        "code": "300750",
        "name": "宁德时代",
        "current": 180.55,
        "percent": -4.2,
        "vol_ratio": 1.35,
        "rec_date": "2026-09-14",
        "rec_days_ago": 2,
        "rec_category": "momentum",
        "rec_score": 72,
        "cum_pct": 3.5,
        "market_cap": 8.0e11,
        "accum_5d": -2.5,
        "score": 66.4,
        "reasons": ["回调到位", "量能未缩"],
    }
    fields.update(over)
    return HistCandidate(**fields)


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


# ── v1 回捞区（hist_rows，2026-09-16）：与飙升区同款门控、分节在飙升之前 ──


def test_hist_only_view_is_pushable_but_hist_is_not_a_dedup_key(monkeypatch):
    """回捞区有行、主线空池 → 卡片有内容可推；且回捞票**不进**去重键。

    与飙升区同构（理由同 hot）：回捞的今日涨幅/量比是分钟级刷新，计入去重键会让
    has_change 几乎每轮为真，把 FEISHU_MIN_INTERVAL 的节流打回 60s 一张卡。
    同时钉死卡片确实画得出这一节 —— 该区 2026-09-16 前只进终端（本用例的回归点）。
    """
    from scanner.feishu import _view_symbols, view_has_content

    monkeypatch.setattr("scanner.feishu.FEISHU_WEBHOOK", "https://example.com/hook")
    view = _fake_view([], hist_rows=[_fake_hist()])

    assert _view_symbols(view) == set(), "回捞票不是去重键"
    assert view_has_content(view) is True, "仅回捞区有内容时也必须可推"

    card = build_feishu_card(view, gem_total=100)
    assert any("v1 回捞" in t for t in _section_titles(card)), "卡片应画出回捞区"
    ok = should_push(PushState(), _view_symbols(view), 1000.0, has_content=view_has_content(view))
    assert ok.push is True and ok.reason == "ok"


def test_hist_section_precedes_hot():
    """分节顺序与终端一致（v1 池选 → v1 回捞 → 沪深飙升）。

    顺序不是装饰：两区都自称"与上方口径独立"，若卡片里回捞排在飙升之后，读者会把它
    当成飙升区的子表。
    """
    view = _fake_view(["SZ300001"], hist_rows=[_fake_hist()], hot_rows=[_fake_hot()])
    card = build_feishu_card(view, gem_total=100)
    titles = _section_titles(card)

    ordered = [t for t in titles if ("回捞" in t or "飙升" in t)]
    assert ordered == ["**◆ v1 回捞**", "**◆ 沪深飙升 · 极有可能大涨**"], f"分节顺序与终端不一致：{titles}"


def test_hist_row_width_is_uniform():
    """回捞行可见宽度恒定（含双宽「—」与各列上界值）。

    与 test_hot_row_width_is_uniform 同一套判据：任何一列宽度定小了都会红，
    「数值列宁可错列也不截断」的下界由末段两条断言钉住。
    """
    from scanner.display import _vis_len
    from scanner.feishu import _COLS_HIST_FEISHU, _fmt_hist_row_feishu

    expected = sum(w for w, _ in _COLS_HIST_FEISHU) + (len(_COLS_HIST_FEISHU) - 1) + 2  # 分隔空格 + 反引号
    cases = [
        {},  # 常规
        {"current": 0, "vol_ratio": 0, "cum_pct": 0},  # 字段缺失 → 全部「—」（最易触发字符数/可见宽度混用）
        {  # 各列上界（创业板涨跌幅上限 ±20%，累计涨幅量级按 ±999.99% 给）
            "current": 9999.99,
            "percent": -20.0,
            "cum_pct": -999.99,
            "vol_ratio": 99.99,
            "rec_days_ago": 9,
            "score": 100.0,
        },
        {"name": "超长名字啊"},  # 自由文本列超宽 → 必须截断而不是撑宽
        {"rec_category": "known_new_face"},  # 最长真实桶名（14 列）→ 恰好占满、不溢出
    ]
    widths = {_vis_len(_fmt_hist_row_feishu(_fake_hist(**c), i)) for i, c in enumerate(cases, 1)}
    assert widths == {expected}, f"行宽参差：{sorted(widths)}（应恒为 {expected}）"

    # 数值列宽度不足会走截断 —— 比错列更糟（显示错值），故单独钉住上界不被截断
    extreme = _fmt_hist_row_feishu(_fake_hist(current=9999.99, accum_5d=-99.9, vol_ratio=99.99), 1)
    assert "9999.99" in extreme and "-99.9%" in extreme and "99.99" in extreme


def test_hist_row_columns_match_terminal():
    """列数必须与终端 COLS_HIST 一致（列序/含义由 _COLS_HIST_FEISHU 的逐列注释对齐）。"""
    from scanner.display import COLS_HIST
    from scanner.feishu import _COLS_HIST_FEISHU

    assert len(_COLS_HIST_FEISHU) == len(COLS_HIST), "终端加/删了列，卡片压缩列规格没跟着改"


def test_hist_tail_marks_stay_outside_the_fixed_width_block():
    """回捞行的行尾标记必须追加在反引号**之外**，定宽部分宽度不受影响。

    塞进定宽块会撑破列对齐 —— 本区所有行共用一套列宽，一行变宽会让其后每列错位
    （`_pad` 对超宽单元格只会 pad 0 个空格，不会报错）。
    同时钉死卡片用 **emoji** 而不是终端那套 ANSI 三角：卡片是 lark_md，
    ANSI 色码会原样显示成乱码。

    2026-09-16：成形函数由 `_hist_tail_feishu` 改名为 `_marks_tail_card`（接收
    裸 ff_pct/beauty 而非候选对象），因为**飙升区也要用同一份** —— 标记是跨展示区
    通用的，不该只有回捞区画。
    """
    from scanner.display import _vis_len
    from scanner.feishu import _fmt_hist_row_feishu, _marks_tail_card

    c = _fake_hist(ff_pct=6.0, beauty="美")
    base = _fmt_hist_row_feishu(c, 1)
    tail = _marks_tail_card(c.ff_pct, c.beauty)

    assert tail == " 🟢 美"
    assert _vis_len(base) == 77, "定宽块宽度不应受行尾标记影响"
    assert "🟢" not in base, "标记必须在块外"

    assert _marks_tail_card(None, "") == ""  # 无数据 → 不标
    assert _marks_tail_card(0.0, "") == ""  # 中性档不显示（与主线同一精简口径）
    assert _marks_tail_card(-6.0, "") == " 🔴"
    assert _marks_tail_card(9.0, "") == " 🟢🟢"
    assert _marks_tail_card(6.0, "") == " 🟢"


def test_marks_tail_card_is_shared_by_both_watch_regions():
    """飙升区与回捞区必须走**同一个**行尾标记成形函数（标记跨区通用）。

    此前只有回捞区画标记、飙升区不画 —— 同一条判定（`fund_flow_signal` /
    `display_gates.beauty_marks_daily`）在两个出口两种待遇。本用例从**渲染结果**
    反查：两个区的卡片行都必须带上标记，说明两边都调了它。
    """
    from scanner.feishu import _marks_tail_card, build_feishu_card

    view = _fake_view(
        [],
        hot_rows=[_fake_hot(ff_pct=6.2, beauty="美")],
        hist_rows=[_fake_hist(ff_pct=6.2, beauty="美")],
    )
    text = str(build_feishu_card(view, gem_total=100))
    assert text.count("🟢 美") >= 2, f"两个区都应带行尾标记：{text}"
    # 成形口径本身（emoji 而非 ANSI 三角、中性档留空）由上面那条用例钉住
    assert _marks_tail_card(6.2, "美") == " 🟢 美"
    assert "▲" not in text, "卡片是 lark_md，不能出现终端那套 ANSI 三角"


def test_hist_category_column_fits_longest_bucket():
    """终端与卡片的「上次v1桶」列都必须容得下最长真实桶名（14 个 ASCII 列）。

    2026-09-16 修：终端原宽 12，而 `known_new_face` / `early_momentum` 都是 14 列 ——
    `_pad` 对超宽单元格不补位（pad=max(0,…)），于是同一区里这些行比别的行宽、后续列整体错开。
    两出口同宽是本用例的重点：卡片压缩列规格唯独**不**收窄这一列。
    """
    from scanner.categories import CATEGORY_REGISTRY
    from scanner.display import COLS_HIST, _vis_len
    from scanner.feishu import _COLS_HIST_FEISHU

    longest = max(_vis_len(name) for name in CATEGORY_REGISTRY)
    assert COLS_HIST[9][0] == "上次v1桶", "列位置变了，本用例的索引与断言需同步"
    assert COLS_HIST[9][1] >= longest, f"终端列宽 {COLS_HIST[9][1]} < 最长桶名 {longest}"
    assert _COLS_HIST_FEISHU[9][0] >= longest, f"卡片列宽 {_COLS_HIST_FEISHU[9][0]} < 最长桶名 {longest}"


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
    """只有四个来源（终选参考 / v1 池选 / v1 回捞 / 飙升区）全空才算空卡片 → empty。"""
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
