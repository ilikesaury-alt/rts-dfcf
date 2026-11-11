"""`agent.py` 的三项工程纪律守卫（2026-11-11）。

守的都是「省 LLM 往返」这件事的**行为**，不是实现细节：
① **并行执行**同一轮的多个工具调用（往返次数才是 <60 秒预算里的瓶颈）；
② **软闸**在预算将尽时提醒收敛，而不是让硬闸突然掐断（白花一轮 20~30 秒）；
③ **两阶段**：预算用尽后的收尾走独立总结器（上下文小、报告聚焦），
且**不额外增加调用次数**。
"""

from __future__ import annotations

import sqlite3

import pytest

from scanner.ai_digest import agent as agent_mod
from scanner.ai_digest.agent import (
    SUMMARY_SYSTEM_PROMPT,
    build_summary_messages,
    run_research,
)
from scanner.ai_digest.llm import MagpieConfig
from scanner.ai_digest.research import ResearchContext, ToolSpec

CFG = MagpieConfig("https://x/v1", "k", "m")


def _ctx(path=None):
    conn = sqlite3.connect(":memory:")
    ctx = ResearchContext(conn=conn)
    return conn, ctx


# ── ① 并行执行 ────────────────────────────────────────────────────────────────


def _slow_tool(name: str, sleep: float, calls: list[str]) -> ToolSpec:
    def handler(args, ctx):
        import time

        time.sleep(sleep)
        calls.append(name)
        return f"{name} ok"

    return ToolSpec(name=name, description=name, parameters={"type": "object", "properties": {}}, handler=handler)


def test_parallel_execution_overlaps_slow_tools():
    """3 个各睡 0.4s 的工具并行跑应远快于串行 1.2s。"""
    import time

    calls: list[str] = []
    tools = [_slow_tool(f"t{i}", 0.4, calls) for i in range(3)]
    # ⚠ 并行只在**文件库**上启用（`:memory:` 反查不到路径）。这里用临时文件库。
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "t.db"
        f.write_bytes(b"")
        c2 = sqlite3.connect(f"file:{f}?mode=ro", uri=True)
        ctx = ResearchContext(conn=c2)
        assert ctx.can_parallelize(), "文件库应支持并行"
        t0 = time.time()
        out = agent_mod._run_calls_parallel([(t, {}) for t in tools], ctx)
        dt = time.time() - t0
        c2.close()
    assert len(out) == 3 and all("ok" in o for o in out)
    assert dt < 1.0, f"并行应明显快于串行 1.2s，实测 {dt:.2f}s"


def test_parallel_falls_back_to_serial_for_memory_db():
    """:memory: 拿不到路径 ⇒ 不硬闯，退回串行且结果不变。"""
    conn, ctx = _ctx()
    try:
        assert not ctx.can_parallelize()
        calls: list[str] = []
        tools = [_slow_tool(f"m{i}", 0.0, calls) for i in range(3)]
        out = agent_mod._run_calls_parallel([(t, {}) for t in tools], ctx)
        assert out == ["m0 ok", "m1 ok", "m2 ok"]
        assert ctx.conn_for_thread() is conn, "非文件库必须返回原连接"
    finally:
        conn.close()


def test_run_research_executes_all_calls_from_one_round():
    """一轮里模型发出多个调用 ⇒ 全部执行、全部回灌（不能只跑第一个）。"""
    seen: list[list[dict]] = []

    def fake_chat(cfg, messages, tools=None, timeout=None):
        if len(seen) == 0:
            seen.append(list(messages))
            return {
                "content": None,
                "tool_calls": [
                    {"id": "a", "name": "get_kline", "arguments": '{"symbols":["SZ300001"]}'},
                    {"id": "b", "name": "get_fund_flow", "arguments": '{"symbols":["SZ300001"]}'},
                ],
            }
        seen.append(list(messages))
        return {"content": "结论", "tool_calls": []}

    conn, ctx = _ctx()
    tools = [_slow_tool("get_kline", 0.0, []), _slow_tool("get_fund_flow", 0.0, [])]
    orig = agent_mod.chat
    agent_mod.chat = fake_chat
    try:
        res = run_research(CFG, [{"symbol": "SZ300001", "name": "x"}], ctx, tools=tools, summarize=False)
    finally:
        agent_mod.chat = orig
        conn.close()
    assert len(res.tool_calls) == 2, res.tool_calls
    tool_msgs = [m for m in seen[1] if m.get("role") == "tool"]
    assert len(tool_msgs) == 2, "两个调用都要回 tool 消息"
    assert {m["tool_call_id"] for m in tool_msgs} == {"a", "b"}


def test_assistant_turn_replays_tool_calls():
    """assistant 轮次必须带 tool_calls 回放 —— 不带时模型会重复发起同一批查询。"""
    seen: list[list[dict]] = []

    def fake_chat(cfg, messages, tools=None, timeout=None):
        seen.append(list(messages))
        if len(seen) == 1:
            return {
                "content": None,
                "tool_calls": [{"id": "a", "name": "get_kline", "arguments": '{"symbols":["SZ300001"]}'}],
            }
        return {"content": "结论", "tool_calls": []}

    conn, ctx = _ctx()
    tools = [_slow_tool("get_kline", 0.0, [])]
    orig = agent_mod.chat
    agent_mod.chat = fake_chat
    try:
        run_research(CFG, [{"symbol": "SZ300001"}], ctx, tools=tools, summarize=False)
    finally:
        agent_mod.chat = orig
        conn.close()
    assistant = [m for m in seen[1] if m.get("role") == "assistant"]
    assert assistant and assistant[0].get("tool_calls"), "assistant 消息必须回放 tool_calls"
    assert assistant[0]["tool_calls"][0]["function"]["name"] == "get_kline"


# ── ② 软闸 ───────────────────────────────────────────────────────────────────


def test_soft_stop_warns_before_hard_cap():
    """到软闸阈值就注入「请收敛」，且硬闸未触发。"""
    seen: list[list[dict]] = []

    def fake_chat(cfg, messages, tools=None, timeout=None):
        seen.append(list(messages))
        if len(seen) <= 2:  # 前两轮：各要一个工具，把调用数推到软闸之上
            return {
                "content": None,
                "tool_calls": [{"id": f"c{len(seen)}", "name": "get_kline", "arguments": '{"symbols":["SZ300001"]}'}],
            }
        return {"content": "结论", "tool_calls": []}

    conn, ctx = _ctx()
    tools = [_slow_tool("get_kline", 0.0, [])]
    orig = agent_mod.chat
    agent_mod.chat = fake_chat
    try:
        # max_calls=2 ⇒ 软闸阈值 = max(1, int(2*0.7)) = 1 ⇒ 第一次调用后应提醒
        res = run_research(
            CFG, [{"symbol": "SZ300001"}], ctx, tools=tools, max_calls=2, summarize=False, max_rounds=5
        )
    finally:
        agent_mod.chat = orig
        conn.close()
    all_text = "\n".join(str(m.get("content") or "") for msgs in seen for m in msgs)
    assert "请不要再扩大查询范围" in all_text, "软闸提醒未注入"
    assert len(res.tool_calls) <= 2


def test_soft_stop_warns_only_once():
    """软闸只说一次 —— 反复念叨会挤占上下文。"""
    seen: list[list[dict]] = []
    n = {"i": 0}

    def fake_chat(cfg, messages, tools=None, timeout=None):
        seen.append(list(messages))
        n["i"] += 1
        if n["i"] <= 4:
            return {
                "content": None,
                "tool_calls": [{"id": f"c{n['i']}", "name": "get_kline", "arguments": '{"symbols":["SZ300001"]}'}],
            }
        return {"content": "结论", "tool_calls": []}

    conn, ctx = _ctx()
    tools = [_slow_tool("get_kline", 0.0, [])]
    orig = agent_mod.chat
    agent_mod.chat = fake_chat
    try:
        # max_calls=4 ⇒ 软闸阈值 = max(1, int(4*0.7)) = 2 ⇒ 第 2 次调用后提醒；
        # 之后第 3、4 次调用仍会过这个判断，但 warned 标志保证只注入一次。
        run_research(CFG, [{"symbol": "SZ300001"}], ctx, tools=tools, max_calls=4, summarize=False, max_rounds=6)
    finally:
        agent_mod.chat = orig
        conn.close()
    # ⚠ 每一轮 `seen` 存的是**当时累积的** messages，同一条提醒会在后续每轮快照里各出现一次。
    #    故只在**最后一轮**的快照里计数（那才是「最终注入了几条」）。
    hits = sum(1 for m in seen[-1] if "请不要再扩大查询范围" in str(m.get("content") or ""))
    assert hits == 1, f"软闸应只提醒一次，实际 {hits} 次"


# ── ③ 两阶段总结 ─────────────────────────────────────────────────────────────


def test_summary_prompt_forbids_new_numbers_and_repetition():
    """总结器的纪律（数字只能来自证据 / 简洁 / 不给目标价）是硬要求，别被改没。"""
    for token in ("一个都不许新增", "证据不足", "目标价", "简洁是硬要求", "不要复述"):
        assert token in SUMMARY_SYSTEM_PROMPT, token


def test_summary_messages_carry_candidates_and_evidence():
    msgs = build_summary_messages("候选清单原文", "[get_kline] xxx", brief=False)
    assert msgs[0]["role"] == "system"
    user = msgs[1]["content"]
    assert "候选清单原文" in user and "[get_kline] xxx" in user
    assert msgs[1]["role"] == "user"


def test_budget_exhausted_uses_summarizer_not_study_context():
    """预算用尽 → 收尾走**独立总结器**：消息里只有 system(总结) + user(清单+证据)，无工具。"""
    seen: list[list[dict]] = []
    n = {"i": 0}

    def fake_chat(cfg, messages, tools=None, timeout=None):
        seen.append((list(messages), tools))
        n["i"] += 1
        if n["i"] <= 2:  # 只有前 2 轮要工具；第 3 次就是收尾的总结调用
            return {
                "content": None,
                "tool_calls": [{"id": f"c{n['i']}", "name": "get_kline", "arguments": '{"symbols":["SZ300001"]}'}],
            }
        return {"content": "总结报告", "tool_calls": []}

    conn, ctx = _ctx()
    tools = [_slow_tool("get_kline", 0.0, [])]
    orig = agent_mod.chat
    agent_mod.chat = fake_chat
    try:
        res = run_research(
            CFG, [{"symbol": "SZ300001"}], ctx, tools=tools, max_calls=2, max_rounds=8, summarize=True
        )
    finally:
        agent_mod.chat = orig
        conn.close()

    final_msgs, final_tools = seen[-1]
    assert final_tools is None, "总结阶段不得再给工具"
    assert final_msgs[0]["content"] == SUMMARY_SYSTEM_PROMPT, "收尾必须用总结器提示词，而非调研上下文"
    assert res.summarized is True
    assert res.text == "总结报告"
    # 工具输出全文仍是幻觉巡检基准
    assert "get_kline" in res.tool_output_blob


def test_summarize_off_returns_without_extra_call():
    """`--summarize-always` 默认关：模型自己写了结论就直接用，不额外多花一轮。"""
    calls = {"n": 0}

    def fake_chat(cfg, messages, tools=None, timeout=None):
        calls["n"] += 1
        return {"content": "模型自己的结论", "tool_calls": []}

    conn, ctx = _ctx()
    orig = agent_mod.chat
    agent_mod.chat = fake_chat
    try:
        res = run_research(CFG, [{"symbol": "SZ300001"}], ctx, tools=[], summarize_always=False)
    finally:
        agent_mod.chat = orig
        conn.close()
    assert calls["n"] == 1, "默认不得为了排版多花一轮调用"
    assert res.summarized is False
    assert res.text == "模型自己的结论"


def test_summarize_always_adds_exactly_one_call():
    """开了开关才多一次 —— 且**只多一次**，不是每轮都总结。"""
    calls = {"n": 0}

    def fake_chat(cfg, messages, tools=None, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return {
                "content": None,
                "tool_calls": [{"id": "a", "name": "get_kline", "arguments": '{"symbols":["SZ300001"]}'}],
            }
        return {"content": "结论", "tool_calls": []}

    conn, ctx = _ctx()
    tools = [_slow_tool("get_kline", 0.0, [])]
    orig = agent_mod.chat
    agent_mod.chat = fake_chat
    try:
        res = run_research(CFG, [{"symbol": "SZ300001"}], ctx, tools=tools, summarize_always=True)
    finally:
        agent_mod.chat = orig
        conn.close()
    # 轮1: 要工具 / 轮2: 给结论 / 轮3: 总结器
    assert calls["n"] == 3, f"应是「2 轮调研 + 1 次总结」，实际 {calls['n']}"
    assert res.summarized is True


# ── 提示词：批量优先 ─────────────────────────────────────────────────────────


def test_system_prompt_tells_model_to_batch_and_be_fast():
    """提示词必须明确要求合并调用、限制轮次 —— 否则模型会一票一次地调（最慢的做法）。"""
    p = agent_mod.RESEARCH_SYSTEM_PROMPT
    assert "symbols" in p and "数组" in p, "必须告诉模型可以用数组批量查"
    assert "2~3 轮" in p or "2-3 轮" in p, "必须给出轮次期望"
    assert "一起发出" in p, "必须说明同一轮可并发发出多个调用"
    assert "不要一票一次" in p


@pytest.mark.parametrize("tool_name", ["get_kline", "get_concepts", "get_announcements"])
def test_batchable_tools_put_symbols_first_in_schema(tool_name):
    """可批量工具的 schema 里 `symbols` 必须排在 `symbol` 前面。

    模型按 schema 顺序写参数 —— `symbol` 排前面就会习惯性只给单票，批量改造白做。
    """
    from scanner.ai_digest.research import build_tools

    spec = next(t for t in build_tools() if t.name == tool_name)
    props = list(spec.parameters.get("properties", {}))
    assert "symbols" in props, f"{tool_name} 缺 symbols 参数"
    assert props.index("symbols") < props.index("symbol"), f"{tool_name} 的 symbols 应排在 symbol 前"
