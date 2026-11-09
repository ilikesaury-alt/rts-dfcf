"""榜外异动 AI 转述（`scanner/ai_digest/`）的单测。

三条守卫线：
1. **红线**：worker 侧 15 例对抗用例在 Python 侧必须同样拦住，另加**整段转述专属**的比较/
   排序句式；同时「合法转述」不得误杀，且**素材自身**（段口径、每一条 item 值）必须过闸
   —— 否则模型照抄素材就会被整段拒答，该票永远无法被转述。
2. **素材确定性**：同输入 → 字节级同输出；缺字段写「暂无数据」并登记；段内顺序与终端同源；
   `limit` 在排序**之后**截断。
3. **零写 + 纪律**：包内不得出现写 SQL、不得裸 `except Exception`；TS/Python 两份词表不得漂移。
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pytest

from scanner.ai_digest.guard import REFUSAL, is_redlined, normalize
from scanner.ai_digest.material import (
    NA,
    SEGMENT_NOTE,
    TIER_LABEL,
    build_materials,
    render_material_items,
    render_materials_block,
)
from scanner.ai_digest.source import (
    OffboardSourceError,
    connect_readonly,
    latest_print_snapshot,
    latest_signal_date,
    load_baseline_stats,
    load_latest_view_offboard,
    load_offboard_rows,
    render_baseline_text,
)

PKG_DIR = Path(__file__).resolve().parents[1] / "scanner" / "ai_digest"
TS_GUARD = Path(__file__).resolve().parents[1] / "rts-panel-cloud" / "worker" / "src" / "explain" / "guard.ts"


def _row(**over):
    base = {
        "symbol": "SZ300758",
        "name": "七彩化学",
        "tier": "T2",
        "percent": 4.1,
        "accum_5d": 8.42,
        "vol_ratio": 2.31,
        "main_pct": 3.2,
        "amount": 3.2e8,
        "float_cap": 2.53e9,
        "price": 15.2,
        "first_time": "2026-11-02T09:47:12+08:00",
    }
    base.update(over)
    return base


# ── 1. 红线守卫 ────────────────────────────────────────────────────────────────

BLOCK_CASES = [
    # worker 侧 10 例平凡变体（全角冒号 / 词内空格 / 全英文 / 同义表达 / 预测）
    "建议：逢低分批买入，冲高卖出。",
    "建 议买入",
    "建议:立即 买 入",
    "短期看多，第一目标位 18.5 元，18 元是合理价。",
    "当前可轻仓参与，半仓滚动操作。",
    "明天大概率冲高，回调就是买点。",
    "Intraday: buy zone 17.8-18.2, sell above 20, stop at 16.9.",
    "buy at 17.5",
    "推荐 300319 目标价 18 元",
    "止盈位 20.5 元",
    # 整段转述专属：比较 / 取优 / 排序（不带任何买卖动词，但已是结论）
    "其中更值得关注的是量比最高的那只。",
    "相比之下，第二只的量能更强。",
    "若只能看一只，首选成交额最大的。",
    "按量比排序后，前三只是……",
    "第一只优于其余几只。",
    "重点关注主力净占比为正的票。",
    "这几只里确定性最高的是第一只。",
]

PASS_CASES = [
    # 合法转述：只报字段与数值
    "本段共 8 只票，全部为 T2 启动首日。",
    "第一只七彩化学量比 2.31，主力净占比 +3.20%，5日累计 +8.42%。",
    "第二只的 5日累计为暂无数据，素材未提供该项。",
    "层级只有 T1 量先动·价未动 与 T2 启动首日 两种。",
    "信号时涨幅（首次推出时冻结）为 +4.10%，价格 15.20。",
    "成交额 3.20 亿，流通市值 25.30 亿。",
]


@pytest.mark.parametrize("text", BLOCK_CASES)
def test_guard_blocks_known_variants(text):
    assert is_redlined(text) is True


@pytest.mark.parametrize("text", PASS_CASES)
def test_guard_passes_legit_transcription(text):
    assert is_redlined(text) is False


def test_normalize_collapses_fullwidth_and_whitespace():
    assert normalize("ＢＵＹ") == "buy"
    assert normalize("建 议买 入") == "建议买入"
    assert normalize("目标价：18") == "目标价18"


def test_segment_note_and_every_material_value_pass_guard():
    """素材自身必须过闸：模型照抄素材是合法的，若素材含红线词它就会被整段拒答。

    这条与 worker 侧「PRIORS 与守卫互斥」同款 —— 那次是把类别先验文案当素材；
    本次是把**段口径说明**当素材（模型几乎一定会复述它）。
    """
    assert is_redlined(SEGMENT_NOTE) is False
    for label in TIER_LABEL.values():
        assert is_redlined(label) is False
    m = build_materials("2026-11-02", [_row(), _row(symbol="SZ301171", tier="T1")])
    offenders = [(i.id, i.value) for i in m.items if is_redlined(i.value)]
    assert not offenders, f"素材项触红线（模型照抄即被拒答）：{offenders}"


def test_refusal_contains_no_imperative_phrasing():
    assert "投资建议" in REFUSAL
    assert re.search(r"请(买入|卖出|加仓|减仓)|建议(买入|卖出|加仓|减仓)|可(买入|卖出)", REFUSAL) is None


@pytest.mark.skipif(not TS_GUARD.exists(), reason="worker 侧 guard.ts 未检出")
def test_ts_slot_vocabulary_is_covered_by_python_guard():
    """TS / Python 两份词表的漂移守卫：TS 侧每条槽位必须在 Python 侧存在。

    两份实现是刻意的（技术栈不同），但**词表漂移不能是**：只改一侧即红。
    Python 侧允许是**超集**（本包追加了比较/排序槽位），故只做单向包含断言。
    """
    ts_src = TS_GUARD.read_text(encoding="utf-8")
    # 只收槽位形状的字面量（纯字母/汉字/连字符）：同文件里的拒答文案是整句中文，
    # 含空格与标点，不可能是槽位 —— 不排除它们就会把 REFUSAL 当成「Python 侧缺失的槽位」。
    ts_slots = {s for s in re.findall(r'"([^"\\\n]{2,})"', ts_src) if re.fullmatch(r"[A-Za-z\u4e00-\u9fff-]+", s)}
    # 防空转：解析不出任何槽位时（文件被改名/改成单引号）必须先红，而不是静默通过。
    assert len(ts_slots) >= 30, f"从 guard.ts 只解析出 {len(ts_slots)} 条槽位，解析规则可能已失效"

    py_src = "\n".join(p.read_text(encoding="utf-8") for p in sorted(PKG_DIR.glob("*.py")))
    missing = sorted(s for s in ts_slots if f'"{s}"' not in py_src)
    assert not missing, f"这些 TS 槽位在 Python 守卫里不存在（两份词表已漂移）：{missing}"


# ── 2. 素材：确定性 / 降级 / 顺序 ──────────────────────────────────────────────


def test_materials_are_deterministic():
    rows = [_row(), _row(symbol="SZ301171", tier="T1", vol_ratio=1.85)]
    a = build_materials("2026-11-02", rows)
    b = build_materials("2026-11-02", rows)
    assert a == b
    assert json.dumps([list(i.__dict__.values()) for i in a.items], ensure_ascii=False) == json.dumps(
        [list(i.__dict__.values()) for i in b.items], ensure_ascii=False
    )


def test_missing_field_becomes_na_and_is_registered():
    m = build_materials("2026-11-02", [_row(accum_5d=None)], mode="transcribe")
    item = next(i for i in m.items if i.id == "sb.SZ300758.accum_5d")
    assert item.value == NA
    assert "SZ300758.accum_5d" in m.missing
    # 未缺的项不得进 missing（否则「缺失披露」本身失真）
    assert not [x for x in m.missing if not x.endswith(".accum_5d")]


def test_analyze_mode_adds_view_only_fields_and_market_ctx():
    """analyze（默认）多给板块/资金流/换手/形态/入选理由 + 当轮市况；transcribe 不给。"""
    rows = [_row(sector="化工", ff_pct=5.6, reasons="启动首日；美感:暴跌日")]
    a = build_materials("2026-11-02", rows, market={"market_idx_pct": 1.24, "weak": False})
    ids = [i.id for i in a.items]
    assert "sb.SZ300758.reasons" in ids and "ctx.market" in ids
    reasons = next(i for i in a.items if i.id == "sb.SZ300758.reasons")
    assert "美感" in reasons.value
    assert "创业板指 +1.24%" in next(i for i in a.items if i.id == "ctx.market").value

    t = build_materials("2026-11-02", rows, mode="transcribe")
    assert not [i for i in t.items if i.id == "ctx.market" or ".reasons" in i.id]


def test_dirty_numeric_strings_degrade_to_na_not_zero():
    """脏值必须显「暂无数据」而不是 0 —— 0 会被模型读成「量比为零」这个真实结论。"""
    m = build_materials("2026-11-02", [_row(vol_ratio="—"), _row(symbol="SZ300001", main_pct="n/a")])
    vol = next(i for i in m.items if i.id == "sb.SZ300758.vol_ratio")
    main = next(i for i in m.items if i.id == "sb.SZ300001.main_pct")
    assert vol.value == NA
    assert main.value == NA


def test_tier_carries_vocabulary_and_unknown_tier_is_flagged():
    m = build_materials("2026-11-02", [_row(tier="T1"), _row(symbol="SZ300999", tier="T9")])
    t1 = next(i for i in m.items if i.id == "sb.SZ300758.tier").value
    t9 = next(i for i in m.items if i.id == "sb.SZ300999.tier").value
    assert "量先动·价未动" in t1
    assert "未登记层" in t9  # 不猜它是哪一层


def test_first_time_is_time_only():
    m = build_materials("2026-11-02", [_row()])
    assert next(i for i in m.items if i.id == "sb.SZ300758.first_time").value == "09:47:12"


def test_order_matches_terminal_sort_key_then_limit_applies_after_sort():
    """段内顺序 = 终端顺序（T2 在前 → 量比降序）；`limit` 必须在排序**之后**截断。"""
    rows = [
        _row(symbol="SZ000001", tier="T1", vol_ratio=9.9),  # T1 量比最高，仍应排最后
        _row(symbol="SZ000002", tier="T2", vol_ratio=1.0),
        _row(symbol="SZ000003", tier="T2", vol_ratio=5.0),
    ]
    m = build_materials("2026-11-02", rows)
    assert m.symbols == ("SZ000003", "SZ000002", "SZ000001")

    limited = build_materials("2026-11-02", rows, limit=2)
    assert limited.symbols == ("SZ000003", "SZ000002")  # 不是数据库前两行（SZ000001, SZ000002）


def test_next_day_pct_never_enters_materials():
    """次日收益刻意不进素材：它是绩效读数，模型一转述就成了「信号有效/无效」的判断。"""
    m = build_materials("2026-11-02", [_row(next_day_pct=9.9)])
    assert not [i for i in m.items if "next_day" in i.id or "次日" in i.label]


def test_renderers_are_pure_strings_and_cover_every_symbol():
    m = build_materials("2026-11-02", [_row(), _row(symbol="SZ301171", tier="T1")])
    block = render_materials_block(m)
    items = render_material_items(m)
    assert block.count("\n") == 1  # 一只票一行
    for sym in m.symbols:
        assert sym in block
        assert f"sb.{sym}.name" in items


# ── 3. LLM 调用形状（照抄 fde-agent-delivery/product1/lib/llm.ts 的踩坑结论）───────────


class _FakeResp:
    """假响应（流式）：把 payload 拆成 SSE chunk 行。

    2026-11-03 起 `llm.chat` 走**流式**（非流式会被上游 CDN 以 524 掐断），
    所以桩必须支持 `iter_lines()`。
    """

    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload

    def iter_lines(self, decode_unicode=False):
        if not self._payload:
            yield "data: [DONE]"
            return
        message = self._payload["choices"][0]["message"]
        # content 分片
        content = message.get("content")
        if content is not None:
            yield f"data: {json.dumps({'choices': [{'delta': {'content': content}}]}, ensure_ascii=False)}"
        # tool_calls 分片（模拟增量：id/name 一片，arguments 一片）
        for tc in message.get("tool_calls") or []:
            fn = tc.get("function") or {}
            yield "data: " + json.dumps(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": tc.get("id"),
                                        "function": {"name": fn.get("name"), "arguments": ""},
                                    }
                                ]
                            }
                        }
                    ]
                },
                ensure_ascii=False,
            )
            yield "data: " + json.dumps(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [{"index": 0, "function": {"arguments": fn.get("arguments") or "{}"}}]
                            }
                        }
                    ]
                },
                ensure_ascii=False,
            )
        yield "data: [DONE]"


def _patch_post(monkeypatch, resp, sink):
    import requests

    def fake_post(url, **kwargs):
        sink.append({"url": url, **kwargs})
        return resp

    monkeypatch.setattr(requests, "post", fake_post)


def _materials():
    return build_materials("2026-11-02", [_row(), _row(symbol="SZ301171", tier="T1")])


def test_transcribe_request_shape(monkeypatch):
    """`stream: false` 必须显式发。

    参考项目实测：若中转默认走 SSE，响应体不是单个 JSON ⇒ `resp.json()` 解析失败，
    被误报成「服务不可用」—— 协议协商问题伪装成故障，最难查。
    """
    sink: list[dict] = []
    _patch_post(monkeypatch, _FakeResp(200, {"choices": [{"message": {"content": "本段共 2 只票。"}}]}), sink)

    from scanner.ai_digest.llm import MagpieConfig, transcribe

    out = transcribe(MagpieConfig("https://magpie.test/v1", "k-test", "m-test"), _materials())
    assert out == "本段共 2 只票。"

    assert len(sink) == 1
    call = sink[0]
    assert call["url"] == "https://magpie.test/v1/chat/completions"
    assert call["headers"]["Authorization"] == "Bearer k-test"
    assert call["timeout"] <= 180  # 推理模型实测 25~90s；但仍不该无限挂着
    body = json.loads(call["data"].decode("utf-8") if isinstance(call["data"], bytes) else call["data"])
    assert body["stream"] is True  # 非流式会被上游 CDN 以 524 掐断（实测）
    assert body["temperature"] == 0
    assert body["model"] == "m-test"
    # 模型只看得到素材：prompt 里必须带素材 id 与段口径，且带缺失项披露
    user = body["messages"][1]["content"]
    assert "sb.SZ300758.vol_ratio" in user and "榜外异动段" in user and "缺失项" in user


@pytest.mark.parametrize(
    ("status", "expect"),
    [(401, "凭据无效"), (403, "凭据无效"), (429, "限流"), (500, "HTTP 500")],
)
def test_transcribe_maps_http_status_to_user_facing_error(monkeypatch, status, expect):
    sink: list[dict] = []
    _patch_post(monkeypatch, _FakeResp(status), sink)

    from scanner.ai_digest.llm import AIUpstreamError, MagpieConfig, transcribe

    with pytest.raises(AIUpstreamError) as ei:
        transcribe(MagpieConfig("https://magpie.test/v1", "k-test", "m-test"), _materials())
    assert expect in str(ei.value)
    assert "k-test" not in str(ei.value)  # 密钥永不进用户可见文案


def test_transcribe_rejects_empty_content(monkeypatch):
    sink: list[dict] = []
    _patch_post(monkeypatch, _FakeResp(200, {"choices": [{"message": {"content": "   "}}]}), sink)

    from scanner.ai_digest.llm import AIUpstreamError, MagpieConfig, transcribe

    with pytest.raises(AIUpstreamError):
        transcribe(MagpieConfig("https://magpie.test/v1", "k-test", "m-test"), _materials())


def test_transcribe_strips_trailing_whitespace_only(monkeypatch):
    """行尾空白必须去掉（Markdown 硬换行产物），但**不得改任何非空白字符**。"""
    sink: list[dict] = []
    _patch_post(
        monkeypatch,
        _FakeResp(200, {"choices": [{"message": {"content": "第一句。  \n第二句。\t"}}]}),
        sink,
    )

    from scanner.ai_digest.llm import SYSTEM_PROMPT_TRANSCRIBE, MagpieConfig, transcribe

    out = transcribe(MagpieConfig("https://magpie.test/v1", "k-test", "m-test"), _materials(), mode="transcribe")
    assert out == "第一句。\n第二句。"
    assert "不要行尾双空格" in SYSTEM_PROMPT_TRANSCRIBE


def test_magpie_config_requires_all_three():
    from scanner.ai_digest.llm import magpie_config

    full = {"MAGPIE_BASE_URL": "https://x/v1", "MAGPIE_API_KEY": "k", "MAGPIE_MODEL": "m"}
    assert magpie_config(full) is not None
    for drop in full:
        partial = {k: v for k, v in full.items() if k != drop}
        assert magpie_config(partial) is None, f"缺 {drop} 时不该被当成已配置"
    # 末尾斜线归一化（否则会拼出 //chat/completions）
    assert magpie_config({**full, "MAGPIE_BASE_URL": "https://x/v1///"}).base_url == "https://x/v1"


# ── 4. 读侧：只读、最新日、无副作用 ────────────────────────────────────────────


@pytest.fixture(scope="module")
def digest_db(tmp_path_factory):
    """真库 schema（走生产 `init_db` 迁移）+ 手插的 `offboard_launch_log` 行。

    刻意不用手写 DDL：schema 是生产迁移建的，漂移不会出现在测试里却在生产里炸。
    """
    import scanner.config as cfgmod
    from scanner.database import init_db

    p = tmp_path_factory.mktemp("ai_digest") / "digest.db"
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(cfgmod, "DB_PATH", str(p))
        conn = init_db()
        conn.executemany(
            "INSERT INTO offboard_launch_log (date, symbol, name, tier, percent, accum_5d, vol_ratio,"
            " main_pct, amount, float_cap, price, first_time) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                ("2026-11-02", "SZ300758", "七彩化学", "T2", 4.1, 8.42, 2.31, 3.2, 3.2e8, 2.53e9, 15.2, "09:47:12"),
                ("2026-11-02", "SZ301171", "易点天下", "T1", 1.9, None, 1.85, 1.1, 1.1e8, 6.8e9, 21.4, "10:06:55"),
                ("2026-11-01", "SZ300001", "旧日票", "T2", 5.0, 1.0, 3.3, 2.0, 2e8, 1e9, 9.9, "09:40:00"),
            ],
        )
        conn.commit()
        conn.close()
    return p


def test_readonly_connect_refuses_missing_db_without_creating_it(tmp_path):
    missing = tmp_path / "nope.db"
    with pytest.raises(OffboardSourceError):
        connect_readonly(str(missing))
    assert not missing.exists(), "只读工具不得顺手创建一个空库"


def test_readonly_connection_rejects_writes(digest_db):
    conn = connect_readonly(str(digest_db))
    try:
        with pytest.raises(sqlite3.Error):
            conn.execute("DELETE FROM offboard_launch_log")
    finally:
        conn.close()


def test_source_reads_latest_signal_date_and_only_that_day(digest_db):
    conn = connect_readonly(str(digest_db))
    try:
        date = latest_signal_date(conn)
        assert date == "2026-11-02"
        rows = load_offboard_rows(conn, date)
    finally:
        conn.close()
    assert {r["symbol"] for r in rows} == {"SZ300758", "SZ301171"}
    assert all(set(r) == set(rows[0]) for r in rows)  # 列集合一致（白名单读）


def test_cli_demo_materials_offline(capsys):
    from scanner.ai_digest.__main__ import main

    code = main(["--demo", "--materials"])
    out = capsys.readouterr().out
    assert code == 0
    assert "segment.note" in out and "sb.SZ300758.tier" in out


def test_cli_without_credentials_exits_1_and_still_prints_materials(capsys, monkeypatch):
    """未配置凭据：不出网、明说原因，但素材照打（用户能看到「AI 会看到什么」）。"""
    import scanner.ai_digest.__main__ as cli

    monkeypatch.setattr(cli, "_load_env", lambda: None)
    for k in ("MAGPIE_BASE_URL", "MAGPIE_API_KEY", "MAGPIE_MODEL"):
        monkeypatch.delenv(k, raising=False)

    code = cli.main(["--demo"])
    out = capsys.readouterr().out
    assert code == 1
    assert "未配置模型服务凭据" in out
    assert "[素材]" in out and "SZ300758" in out  # 素材照打


# ── 5. view 源 / 数据源选择 / analyze 模式 ───────────────────────────────────


def test_unverified_numbers_detection():
    """幻觉巡检：正文数字必须能在提示词里找到；按浮点值比对（6.2 ≡ 6.20）。"""
    from scanner.ai_digest.material import unverified_numbers

    prompt = "信号日：2026-11-02\nsb.X.vol_ratio | 量比 | 2.31\nsb.X.accum_5d | 5日 | +6.20%\n第1只"
    # 完全对得上
    assert unverified_numbers("量比 2.31、5日累计 6.2%，第1只。", prompt) == []
    # 四舍五入（33.4 ≙ 33.35 这类）不算编造 —— 1% 容差
    assert unverified_numbers("量比 2.3（2.31 四舍五入）", prompt) == []
    # 真的编的：18.5 与 2.5 都不在素材里
    assert unverified_numbers("目标价 18.5，涨幅 2.5%", prompt) == ["2.5", "18.5"]
    assert unverified_numbers("", prompt) == []


def test_view_source_normalizes_keys(digest_db):
    """视图行键名（volume_ratio/float_market_capital/current）必须收口成素材口径。"""
    payload = {
        "date": "2026-11-03",
        "time": "10:31:00",
        "seq": 1,
        "regions": {
            "offboard": [
                {
                    "symbol": "SZ300758",
                    "code": "300758",
                    "name": "七彩化学",
                    "tier": "T2",
                    "percent": 4.1,
                    "volume_ratio": 2.31,
                    "main_pct": 3.2,
                    "amount": 3.2e8,
                    "float_market_capital": 2.53e9,
                    "current": 15.2,
                    "sector": "化工",
                    "ff_pct": 5.6,
                    "turnover_rate": 8.3,
                    "beauty": "深回调",
                    "reasons": ["启动首日", "美感:深回调"],
                }
            ],
            "main": [],
        },
        "ctx": {"marketIdxPct": 1.24, "weak": False},
    }
    conn_w = sqlite3.connect(str(digest_db))
    try:
        conn_w.execute(
            "INSERT INTO view_snapshot(date, time, seq, payload, updated_at) VALUES(?,?,?,?,?)",
            ("2026-11-03", "10:31:00", 1, json.dumps(payload, ensure_ascii=False), "now"),
        )
        conn_w.commit()
    finally:
        conn_w.close()

    conn = connect_readonly(str(digest_db))
    try:
        snap = load_latest_view_offboard(conn)
    finally:
        conn.close()
    assert snap is not None
    date, tm, rows, market = snap
    assert (date, tm) == ("2026-11-03", "10:31:00")
    row = rows[0]
    assert row["vol_ratio"] == 2.31 and row["float_cap"] == 2.53e9 and row["price"] == 15.2
    assert row["reasons"] == "启动首日；美感:深回调"
    assert market == {"market_idx_pct": 1.24, "weak": False}

    m = build_materials(date, rows, market=market)
    assert next(i for i in m.items if i.id == "sb.SZ300758.vol_ratio").value == "2.31"
    assert "第1只·入选理由" in [i.label for i in m.items]


def test_auto_source_prefers_newer_terminal_snapshot(digest_db, monkeypatch):
    """auto：view 快照更新（= 最近一次终端打印）就用它；log 更新则回退 log。"""
    import scanner.ai_digest.__main__ as cli

    monkeypatch.setattr(cli, "resolve_db_path", lambda: str(digest_db))
    args = cli._parse_args(["--demo"])
    args.demo = False
    args.source = "auto"
    args.date = None
    info = cli._resolve_rows(args)
    # digest_db 里 view 快照（2026-11-03，上一用例插入）比 launch_log（2026-11-02）新
    assert info["source"] == "view" and info["date"] == "2026-11-03"


def test_cli_analyze_mode_accepts_advice_but_transcribe_refuses_it(capsys, monkeypatch):
    """同一个含建议的输出：analyze（默认）放行并打「非系统结论」标签；transcribe 整段拦下。"""
    import scanner.ai_digest.__main__ as cli
    import scanner.ai_digest.llm as llm

    monkeypatch.setattr(cli, "_load_env", lambda: None)
    monkeypatch.setenv("MAGPIE_BASE_URL", "https://magpie.test/v1")
    monkeypatch.setenv("MAGPIE_API_KEY", "k-test")
    monkeypatch.setenv("MAGPIE_MODEL", "m-test")
    advice = "建议重点关注第1只，量比最高。"
    monkeypatch.setattr(llm, "transcribe", lambda cfg, mats, mode="analyze", timeout=None: advice)

    code = cli.main(["--demo", "--mode", "analyze"])
    out = capsys.readouterr().out
    assert code == 0 and "建议重点关注第1只" in out
    assert "非系统结论" in out  # analyze 的标签必须如实

    code = cli.main(["--demo", "--mode", "transcribe"])
    out = capsys.readouterr().out
    assert code == 3 and advice not in out and "触及红线" in out


def test_cli_flags_fabricated_number_and_exits_5(capsys, monkeypatch):
    """analyze 模式唯一的确定性闸：编造数字必须被标出，且退出码 5 可被脚本拦截。"""
    import scanner.ai_digest.__main__ as cli
    import scanner.ai_digest.llm as llm

    monkeypatch.setattr(cli, "_load_env", lambda: None)
    monkeypatch.setenv("MAGPIE_BASE_URL", "https://magpie.test/v1")
    monkeypatch.setenv("MAGPIE_API_KEY", "k-test")
    monkeypatch.setenv("MAGPIE_MODEL", "m-test")
    monkeypatch.setattr(
        llm,
        "transcribe",
        lambda cfg, mats, mode="analyze", timeout=None: "第1只量比 2.31，目标价 18.5 元。",
    )

    code = cli.main(["--demo", "--mode", "analyze"])
    out = capsys.readouterr().out
    assert code == 5
    assert "幻觉巡检" in out and "18.5" in out


# ── 6. 终端打印快照源 + 历史基准率（2026-11-03 用户决策）─────────────────────────


def test_print_snapshot_is_the_terminal_rows_not_daily_accumulation(digest_db):
    """`print` 源 = 终端最后一轮打印的那几行，**不是**当日过门累加（launch_log）。

    用户口径：「今日最后一次终端打印只有 3 只」—— 而 launch_log 那天有 17 行。
    两者必须能分辨，否则分析的是「当日过门过谁」而不是「屏幕上显示谁」。
    """
    import dataclasses

    from scanner.db.dal import save_offboard_print_snapshot

    @dataclasses.dataclass
    class _C:
        symbol: str
        name: str = "票"
        tier: str = "T2"
        percent: float = 4.0
        accum_5d: float = 5.0
        volume_ratio: float = 2.0
        main_pct: float = 1.0
        amount: float = 1e8
        float_market_capital: float = 1e9
        current: float = 10.0
        sector: str = ""
        turnover_rate: float = 3.0
        ff_pct: float | None = None
        beauty: str = ""
        reasons: list = dataclasses.field(default_factory=list)

    conn = sqlite3.connect(str(digest_db))
    try:
        # 第 1 轮打印 5 行，第 2 轮（更晚）只打印 3 行
        save_offboard_print_snapshot(conn, [_C(f"SZ30000{i}") for i in range(5)], today="2026-11-03", print_seq=100000)
        save_offboard_print_snapshot(
            conn, [_C("SZ300758"), _C("SZ301171"), _C("SZ300999")], today="2026-11-03", print_seq=140000
        )
    finally:
        conn.close()

    conn = connect_readonly(str(digest_db))
    try:
        snap = latest_print_snapshot(conn)
    finally:
        conn.close()
    assert snap is not None
    date, _tm, rows, seq = snap
    assert date == "2026-11-03" and seq == 140000
    assert [r["symbol"] for r in rows] == ["SZ300758", "SZ301171", "SZ300999"]  # 3 只，不是 5 只
    assert rows[0]["tier"] == "T2" and rows[0]["vol_ratio"] == 2.0


def test_baseline_stats_are_own_labels_and_never_empty_string(digest_db):
    """基准率素材：来自本项目自己的标签（不是市场先验），且桶样本量标注「样本少」。"""
    conn = sqlite3.connect(str(digest_db))
    try:
        conn.executemany(
            "INSERT INTO offboard_launch_log (date, symbol, tier, vol_ratio, main_pct, accum_5d, amount, next_day_pct)"
            " VALUES (?,?,?,?,?,?,?,?)",
            [
                ("2026-10-01", "SZ000001", "T2", 12.0, 12.0, 9.0, 2e8, 8.5),  # 命中
                ("2026-10-01", "SZ000002", "T2", 2.5, 1.0, 1.0, 0.5e8, -2.0),
                ("2026-10-02", "SZ000003", "T1", 3.5, 6.0, 5.0, 1e8, 3.0),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    conn = connect_readonly(str(digest_db))
    try:
        stats = load_baseline_stats(conn)
    finally:
        conn.close()
    assert stats["n"] == 3
    assert "T2" in stats["tiers"]
    text = render_baseline_text(
        stats, label_map={"vol_ratio": "量比", "main_pct": "主力净占比", "accum_5d": "5日累计", "amount": "成交额"}
    )
    assert "本区历史 3 条" in text
    assert "样本少" in text  # 所有桶 n<30，必须标注，不能当结论用
    # 空库时不得给出空串（模型会把它当「没有基准」而编造）
    empty = render_baseline_text({"n": 0, "tiers": {}, "buckets": {}}, label_map={})
    assert empty == NA or "暂无数据" in empty


def test_analyze_prompt_requires_baseline_alignment():
    """提示词必须**强制**排序对齐基准率桶，而不是让模型自由发挥。"""
    from scanner.ai_digest.llm import SYSTEM_PROMPT_ANALYZE

    assert "baseline.stats" in SYSTEM_PROMPT_ANALYZE
    assert "桶" in SYSTEM_PROMPT_ANALYZE and "命中率" in SYSTEM_PROMPT_ANALYZE
    assert "样本少" in SYSTEM_PROMPT_ANALYZE  # n<30 的桶只能当参考
    assert "最差桶" in SYSTEM_PROMPT_ANALYZE  # 落进差桶必须写进风险


def test_baseline_item_only_in_analyze_mode():
    rows = [{"symbol": "SZ300758", "tier": "T2", "vol_ratio": 2.31}]
    a = build_materials("2026-11-02", rows, mode="analyze", baseline="基准率文本")
    assert next(i for i in a.items if i.id == "baseline.stats").value == "基准率文本"
    t = build_materials("2026-11-02", rows, mode="transcribe", baseline="基准率文本")
    assert not [i for i in t.items if i.id == "baseline.stats"]


# ── 6. 调研工具箱（research，2026-11-03）──────────────────────────────────────


def test_research_tools_exist_and_are_wired():
    """8 个工具必须齐全 —— 少一个，模型就少一条调研路径（且是静默少）。"""
    from scanner.ai_digest.research import build_tools

    names = {t.name for t in build_tools()}
    assert names == {
        "get_kline",
        "get_realtime_quote",
        "get_fund_flow",
        "get_concepts",
        "get_sector_heatmap",
        "get_market_overview",
        "get_board_rank",
        "get_stock_record",
        # 2026-11-03 新增三类硬数据（消息面 / 日内节奏 / 多日资金趋势）
        "get_announcements",
        "get_intraday_minute",
        "get_fund_flow_trend",
        "get_dragon_tiger",
    }
    for spec in build_tools():
        sch = spec.schema()
        assert sch["type"] == "function" and sch["function"]["name"] == spec.name
        assert sch["function"]["parameters"]["type"] == "object"


def test_research_tools_use_real_local_data(digest_db, monkeypatch):
    """工具必须能用**本地真实数据**跑通（列名写错会 OperationalError ⇒ 工具静默失效）。

    ⚠ `get_kline` 本地不足 3 根会去雪球补实时 K 线并**整段替换**本地行；不桩掉
    外网路径，本用例就会随本机 cookie/网络状态忽绿忽红（2026-11-06 实测：
    联网成功时输出变成 4 根实时 bar，断言里的 2026-11-02 消失）。这里把四个
    工具会碰到的取数函数全桩成空/None，SQL 仍查真库 ⇒ 零联网，列名守卫的意图不变。
    """
    import scanner.api as api_mod
    import scanner.market_extra as market_extra_mod
    from scanner.ai_digest.research import ResearchContext, build_tools, run_tool

    calls: list[str] = []

    def _no_remote_bars(session, symbol, days=None):
        calls.append("kline")
        return []

    def _no_index(session):
        calls.append("index")

    def _no_zt():
        calls.append("zt")
        return []

    monkeypatch.setattr(api_mod, "fetch_kline", _no_remote_bars)
    monkeypatch.setattr(api_mod, "fetch_market_index", _no_index)
    monkeypatch.setattr(market_extra_mod, "fetch_zt_pool", _no_zt)
    monkeypatch.setattr(ResearchContext, "get_session", lambda self: object())

    conn = sqlite3.connect(str(digest_db))
    try:
        conn.execute(
            "INSERT INTO daily_kline (symbol, date, open, close, high, low, volume, percent) "
            "VALUES ('SZ300770','2026-11-02',38.15,40.33,40.46,37.50,16304308,6.44)"
        )
        conn.execute(
            "INSERT INTO concept_cache (symbol, concepts, updated) VALUES ('SZ300770','[\"传媒\",\"影视概念\"]','2026-11-02')"
        )
        conn.execute(
            "INSERT INTO recommendations (date, time, symbol, name, category, score, percent, excluded) "
            "VALUES ('2026-11-02','10:00:00','SZ300770','新媒股份','short_term',72,6.44,1)"
        )
        conn.commit()
    finally:
        conn.close()

    conn = connect_readonly(str(digest_db))
    try:
        ctx = ResearchContext(conn=conn)
        tools = {t.name: t for t in build_tools()}

        k = run_tool(tools["get_kline"], {"symbol": "SZ300770", "n": 5}, ctx)
        assert calls == ["kline"], "本地不足 3 根时必须走过「实时补」分支，桩才有效"
        assert "2026-11-02" in k and "40.33" in k
        assert "1630万股" in k, "成交量必须给人读单位（模型照抄，不自己换算）"

        heat = run_tool(tools["get_sector_heatmap"], {"concept": "传媒"}, ctx)
        assert "SZ300770" in heat and "+6.44%" in heat

        rec = run_tool(tools["get_stock_record"], {"symbol": "SZ300770"}, ctx)
        assert "short_term" in rec and "score=72" in rec

        ov = run_tool(tools["get_market_overview"], {}, ctx)
        assert {"index", "zt"} <= set(calls), "概览的两条外网取数也必须走桩（不许真联网）"
        assert "本地 K 线样本" in ov  # 即使指数/涨停池取不到，本地那部分也必须出得来
    finally:
        conn.close()


class _FakeEmResp:
    """东财接口桩（与上面 worker 流式桩同名会互相覆盖，故独立命名）。"""

    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def json(self):
        return self._payload


def _fake_requests(payload):
    class _Mod:
        @staticmethod
        def get(url, params=None, timeout=None):
            return _FakeEmResp(payload)

    return _Mod


def test_announcement_columns_are_string_list_not_dict(monkeypatch):
    """东财公告的 `columns` 实测是**字符串列表**（['公司公告']）—— 当 dict 列表解析会 AttributeError。"""
    from scanner.ai_digest import em_data

    payload = {
        "data": {
            "list": [
                {"notice_date": "2026-10-30 00:00:00", "title": "2026年三季度报告", "columns": ["公司公告"]},
                {
                    "notice_date": "2026-09-22 00:00:00",
                    "title": "权益分派实施",
                    "columns": [{"column_name": "分配方案"}],
                },
            ]
        }
    }
    monkeypatch.setattr(em_data, "_requests", lambda: _fake_requests(payload))
    items = em_data.fetch_announcements("SZ300770", 5)
    assert [i["date"] for i in items] == ["2026-10-30", "2026-09-22"]
    assert items[0]["type"] == "公司公告"  # 字符串形态
    assert items[1]["type"] == "分配方案"  # dict 形态也要兼容


def test_fund_flow_series_is_daily_not_intraday(monkeypatch):
    """东财 fflow `klt=101` 是**日频**（121 个交易日）—— 不能当分时曲线用。"""
    from scanner.ai_digest import em_data

    payload = {
        "data": {
            "klines": [
                "2026-11-02,22384640.0,-33582973.0,11198334.0,6268338.0,16116302.0",
                "2026-10-30,16245600.0,-1000.0,2000.0,3000.0,4000.0",
            ]
        }
    }
    monkeypatch.setattr(em_data, "_requests", lambda: _fake_requests(payload))
    pts = em_data.fetch_intraday_fund_flow("SZ300770")
    assert len(pts) == 2
    assert pts[0]["date"] == "2026-11-02"  # 日期不是时间
    assert pts[0]["main"] == 22384640.0
    assert pts[0]["xl"] == 16116302.0


def test_dragon_tiger_returns_subsequent_returns(monkeypatch):
    """龙虎榜必须带上 D1/D2/D5/D10 后续涨幅 —— 这是「上榜后到底涨没涨」的硬证据。"""
    from scanner.ai_digest import em_data

    payload = {
        "result": {
            "data": [
                {
                    "TRADE_DATE": "2020-07-01 00:00:00",
                    "EXPLAIN": "2家机构卖出",
                    "BILLBOARD_NET_AMT": -87477909,
                    "ACCUM_AMOUNT": 797676534,
                    "DEAL_AMOUNT_RATIO": 39.29,
                    "TURNOVERRATE": 4.8,
                    "CLOSE_PRICE": 202,
                    "CHANGE_RATE": -9.68,
                    "D1_CLOSE_ADJCHRATE": 2.22,
                    "D2_CLOSE_ADJCHRATE": 2.37,
                    "D5_CLOSE_ADJCHRATE": 7.25,
                    "D10_CLOSE_ADJCHRATE": 17.66,
                }
            ]
        }
    }
    monkeypatch.setattr(em_data, "_requests", lambda: _fake_requests(payload))
    rows = em_data.fetch_dragon_tiger("SZ300770")
    assert rows[0]["date"] == "2020-07-01"  # 带时间戳的日期要截断
    assert rows[0]["d1"] == 2.22 and rows[0]["d10"] == 17.66
    assert rows[0]["reason"] == "2家机构卖出"


def test_tool_failure_returns_human_text_not_exception(digest_db):
    """工具失败必须返回一句人话（模型要能区分「查不到」与「查了没有」）。"""
    from scanner.ai_digest.research import ResearchContext, ToolSpec, run_tool

    def boom(args, ctx):
        raise ValueError("坏了")

    spec = ToolSpec(name="t", description="", parameters={"type": "object"}, handler=boom)
    out = run_tool(spec, {}, ResearchContext(conn=connect_readonly(str(digest_db))))
    assert "失败" in out and "ValueError" in out


def test_money_helper_gives_readable_units():
    """金额必须换算成人读单位 —— 模型自己换算会被幻觉巡检判成编造。"""
    from scanner.ai_digest.research import _money

    assert _money(641890989.56) == "6.42亿"
    assert _money(22384640.0) == "2238.46万"
    assert _money(1234.5) == "1234.50"
    assert _money(None) == "—"


def test_research_agent_loop_calls_tools_then_answers(monkeypatch):
    """调研循环：模型先要工具 → 我们执行回灌 → 再问才拿到最终结论。"""
    import scanner.ai_digest.agent as agent_mod
    from scanner.ai_digest.llm import MagpieConfig
    from scanner.ai_digest.research import ResearchContext, build_tools

    calls = {"n": 0}

    def fake_chat(cfg, messages, tools=None, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return {
                "content": None,
                "tool_calls": [{"id": "c1", "name": "get_kline", "arguments": '{"symbol":"SZ300770","n":5}'}],
            }
        return {"content": "最终结论：量价配合最干净的是第一只。", "tool_calls": []}

    monkeypatch.setattr(agent_mod, "chat", fake_chat)
    from scanner.db.schema import get_conn

    prod = get_conn()
    try:
        res = agent_mod.run_research(
            MagpieConfig("https://x/v1", "k", "m"),
            [{"symbol": "SZ300770", "name": "新媒股份", "tier": "T2", "percent": 6.44}],
            ResearchContext(conn=prod),
            tools=build_tools(),
        )
    finally:
        prod.close()
    assert "最终结论" in res.text
    assert len(res.tool_calls) == 1 and res.tool_calls[0]["name"] == "get_kline"
    assert res.tool_output_blob  # 工具输出全文 = 幻觉巡检的比对基准


# ── 6. 包级纪律（零写 / 不用裸 except）─────────────────────────────────────────


def _py_sources() -> list[Path]:
    files = sorted(PKG_DIR.glob("*.py"))
    assert files, f"{PKG_DIR} 下没有 .py 文件，本组守卫会空转"
    return files


def test_digest_package_contains_no_write_sql():
    """AI 输出零落库的另一半：本包根本不许有写语句（读侧用 `mode=ro` 再兜一层）。"""
    want = re.compile(r"\b(INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|CREATE\s+TABLE|DROP\s+TABLE)\b", re.I)
    offenders = [
        f"{p.name}: {m.group(0)}" for p in _py_sources() for m in [want.search(p.read_text(encoding="utf-8"))] if m
    ]
    assert not offenders, f"AI 转述包内不得出现写 SQL：{offenders}"


def test_digest_package_has_no_bare_except_exception():
    """fail-open 纪律：只捕 `EXTERNAL_FAILURES`；裸 except Exception 会把编程错误吞掉。"""
    offenders = [p.name for p in _py_sources() if re.search(r"except\s+Exception\b", p.read_text(encoding="utf-8"))]
    assert not offenders, f"禁止裸 except Exception：{offenders}"
