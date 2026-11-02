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
    latest_signal_date,
    load_latest_view_offboard,
    load_offboard_rows,
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
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


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
    assert body["stream"] is False
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
    assert unverified_numbers("量比 2.31、5日累计 6.2%，第1只。", prompt) == []
    assert unverified_numbers("目标价 18.5，涨幅 2.3%", prompt) == ["2.3", "18.5"]
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
