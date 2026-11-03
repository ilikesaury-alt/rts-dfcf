"""AI 调研循环（2026-11-03）—— 模型**自己决定**查哪些数据、查到多少，再给判断。

这是「让模型自己做市场数据调研」的实现：网关支持 tool-calling（实测 `finish_reason=
tool_calls`），我们把 `research.build_tools()` 的真实数据工具交给它，它自己发起查询、
我们执行、结果回灌，直到它给出最终判断。

三条纪律：

1. **模型决定查什么，但工具决定返回什么真实数据** —— 模型不能自己编数；它能编的只有
   「解读」，而解读里的数字会在事后被**幻觉巡检**核对（基准 = 所有工具输出的全文）。
2. **预算封顶**：最多 `max_rounds` 轮、`max_calls` 次工具调用、每次输出截断
   （`research._MAX_TOOL_CHARS`）。没有封顶的话，一个「多查点」的建议能烧掉几十次外网请求。
3. **工具失败要说人话**（「该数据本轮不可用」），不能静默返回空 —— 模型会把空数据当成
   「查过了，没有」，那是幻觉的温床。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from scanner.ai_digest.llm import MagpieConfig, chat
from scanner.ai_digest.research import ResearchContext, ToolSpec, build_tools, run_tool

__all__ = ["ResearchResult", "run_research"]

#: 调研员系统提示词：**给能力，不给结论**。
RESEARCH_SYSTEM_PROMPT = "\n".join(
    [
        "你是一名 A 股（创业板）市场调研员。你会拿到一批候选股票，需要**自己用工具查真实市场数据**，"
        "再做综合判断，筛选出**接下来可能上涨**的票。",
        "",
        "工作方式：",
        "1. 先看工具能查什么，然后**主动调用**：每只票至少查 K 线（形态/位置/量能）与资金流（资金方向），",
        "   再查它的概念板块、以及该板块当日谁在领涨（板块共振 vs 个股孤军）、当日市场概览与飙升榜（情绪与热度）。",
        "2. 查询要有目的：拿到数据后要能说清「这只票凭什么可能涨」，而不是把工具输出复述一遍。",
        "2b. 想看游资痕迹/历史上这类信号涨没涨 → 查 get_dragon_tiger（该票没上过榜会返回空）；"
        "想看日内节奏 → 查 get_intraday_minute；想看公司有无实质事件 → 查 get_announcements。",
        "3. 你可以多查几轮（例如对最看好的票再查日线细节、查它在系统里的出现史），但要有节制。",
        "",
        "输出要求：",
        "4. 给出排序（第1/第2…），每只票写清：入选依据（引用你查到的具体数据）、主要风险、证据强弱（强/中/弱）。",
        "5. 数据不支持的票就明说「证据不足」；不要为了凑数硬排。",
        "6. 只许用工具查到的数据说话。**禁止编造任何数字**（事后有机器逐个核对，查不到的数字会被标出）。",
        "7. 禁止给出目标价与止损位（本系统没有这类数据支撑，给了就是编造）。",
        "8. 结尾给一句话的整体判断：这批票里有没有真正值得动手的；如果整体证据都不够，就直说。",
        "9. 不要用 Markdown 标题/列表符号/加粗；需要分段就用单个换行。",
    ]
)


@dataclass
class ResearchResult:
    text: str
    """最终结论原文。"""

    rounds: int
    """实际轮数。"""

    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    """工具调用记录（`{name, args, ok}`），供输出侧展示「它查了什么」。"""

    tool_output_blob: str = ""
    """所有工具输出拼成的全文 —— 幻觉巡检的比对基准。"""

    def tool_summary(self) -> str:
        if not self.tool_calls:
            return "（本轮没有调用任何工具）"
        parts = []
        for c in self.tool_calls:
            args = ", ".join(f"{k}={v}" for k, v in (c.get("args") or {}).items())
            parts.append(f"{c['name']}({args})")
        return "、".join(parts)


#: 自选清单模式的候选字段（值缺失就整段不渲染 —— 随意指定清单没有层/量比等
#: 信号字段，写「层 None」会污染提示词，模型可能把 None 当成真值引用）。
_CAND_FIELDS: tuple[tuple[str, str], ...] = (
    ("tier", "层"),
    ("percent", "信号时涨幅"),
    ("vol_ratio", "量比"),
    ("main_pct", "主力净占比"),
    ("accum_5d", "5日累计"),
    ("amount", "成交额"),
    ("float_cap", "流通市值"),
)

#: 值后缀带百分号的字段（与旧提示词逐字同口径）。
_PCT_KEYS = {"percent", "main_pct", "accum_5d"}


def _candidate_lines(candidates: list[dict[str, Any]]) -> list[str]:
    """候选清单 → 提示词行：只渲染非 None 的字段。"""
    lines = []
    for c in candidates:
        parts = [f"- {c.get('symbol')} {c.get('name')}".rstrip()]
        parts += [
            f"｜{label} {c.get(key)}{'%' if key in _PCT_KEYS else ''}"
            for key, label in _CAND_FIELDS
            if c.get(key) is not None
        ]
        lines.append("".join(parts))
    return lines


def run_research(
    cfg: MagpieConfig,
    candidates: list[dict[str, Any]],
    ctx: ResearchContext,
    *,
    max_rounds: int = 8,
    max_calls: int = 30,
    tools: list[ToolSpec] | None = None,
    origin: str = "榜外异动段今日过门的票",
) -> ResearchResult:
    """跑一轮「模型自己调研」。返回最终文本 + 工具调用记录 + 工具输出全文。

    `origin`：候选来源文案（写进提示词，模型据此知道自己在分析什么池子；
    自选清单模式传「用户指定的自选清单」）。
    """
    tool_list = tools if tools is not None else build_tools()
    schemas = [t.schema() for t in tool_list]

    cand_lines = _candidate_lines(candidates)
    user = "\n".join(
        [
            f"候选股票 {len(candidates)} 只（{origin}）：",
            *cand_lines,
            "",
            "请自己调用工具查真实市场数据（K线/报价/资金流/概念板块/板块涨幅分布/市场概览/飙升榜/系统出现史），",
            "然后给出排序、入选依据、风险与整体判断。",
        ]
    )

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": RESEARCH_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]

    calls: list[dict[str, Any]] = []
    blob_parts: list[str] = []
    rounds = 0

    while rounds < max_rounds and len(calls) < max_calls:
        rounds += 1
        resp = chat(cfg, messages, tools=schemas)
        tool_calls = resp.get("tool_calls") or []
        content = resp.get("content")

        if not tool_calls:
            text = (content or "").strip()
            if not text:
                text = "（模型未给出结论）"
            return ResearchResult(text=text, rounds=rounds, tool_calls=calls, tool_output_blob="\n".join(blob_parts))

        if content:
            messages.append({"role": "assistant", "content": content})

        for tc in tool_calls:
            if len(calls) >= max_calls:
                break
            name = tc.get("name") or ""
            raw_args = tc.get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
            except json.JSONDecodeError:
                args = {}
            spec = next((t for t in tool_list if t.name == name), None)
            out = f"未知工具 {name}" if spec is None else run_tool(spec, args, ctx)
            calls.append({"name": name, "args": args, "ok": bool(out) and "失败" not in out[:40]})
            blob_parts.append(f"[{name}] {out}")
            messages.append({"role": "tool", "tool_call_id": tc.get("id") or name, "content": out})

    # 预算用尽仍未给结论 → 让模型基于已有工具输出收尾
    messages.append(
        {
            "role": "user",
            "content": "工具调用次数已达上限。请基于已经查到的数据给出最终排序与判断，不要再调用工具。",
        }
    )
    resp = chat(cfg, messages, tools=None)
    text = (resp.get("content") or "").strip() or "（模型未给出结论）"
    return ResearchResult(text=text, rounds=rounds, tool_calls=calls, tool_output_blob="\n".join(blob_parts))
