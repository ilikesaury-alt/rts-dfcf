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

from scanner.ai_digest.features import SymbolFeatures, build_features, render_feature_block
from scanner.ai_digest.llm import (
    BRIEF_INSTRUCTION,
    BRIEF_RESEARCH_INSTRUCTION,
    MagpieConfig,
    chat,
)
from scanner.ai_digest.panel import PanelRegion, PanelSnapshot
from scanner.ai_digest.research import ResearchContext, ToolSpec, build_tools, run_tool
from scanner.utils import EXTERNAL_FAILURES

__all__ = [
    "ResearchResult",
    "build_research_prompt",
    "build_summary_messages",
    "run_research",
]

#: 调研员系统提示词：**给能力，不给结论**。
RESEARCH_SYSTEM_PROMPT = "\n".join(
    [
        "你是一名 A 股（创业板）市场调研员。你会拿到一批候选股票，需要**自己用工具查真实市场数据**，"
        "再做综合判断，筛选出**接下来可能上涨**的票。",
        "",
        "工作方式（**效率是硬要求：请在 2~3 轮内完成取证**）：",
        "1. 多数工具支持 `symbols` **数组一次查多只** —— 请把这一轮想查的票**合并成少数几次调用**，",
        "   不要一票一次地调（那是本命令最慢的做法）。一次调用的最优形态举例：",
        "   `get_kline(symbols=[全部候选])` + `get_fund_flow(symbols=[全部候选])` + `get_concepts(symbols=[全部候选])`",
        "   + `get_market_overview()` + `get_board_rank()` —— 这些**可以同一条消息里一起发出**，不要等一个回来再发下一个。",
        "2. 第一轮就把面上数据查齐（K线/资金流/概念/板块热力图/市场概览/飙升榜）；第二轮只对**最看好的 2~3 只**",
        "   补查细节（get_dragon_tiger 游资痕迹 / get_intraday_minute 日内节奏 / get_announcements 实质事件）。",
        "3. 查询要有目的：拿到数据后要能说清「这只票凭什么可能涨」，而不是把工具输出复述一遍。",
        "4. 别重复查你已经查过的东西；证据够了就直接给结论，不要为了「查得更全」多跑几轮。",
        "",
        "输出要求：",
        "5. 把候选分成**四档**，每只票只能落在一档：",
        "   · **首选** —— 证据强、且你愿意现在就动手的。**没有数量上限**：如果 3 只都够硬，",
        "     就 3 只都是首选；只有 1 只够硬就 1 只。**不要为了「显得严谨」硬压到 1 只**，",
        "     也不要为了凑数把证据不够的塞进来。",
        "   · **次选** —— 逻辑成立但差一口气（位置偏高 / 量能不足 / 缺板块共振 / 等确认）。",
        "     同样**没有数量上限**，够格的有几只就列几只。",
        "   · **观察** —— 有亮点但现在不能动手（信号未确认、需要明天看开盘）。",
        "   · **证据不足** —— 数据不支持下判断，明说为什么不足（缺哪块数据），不要硬排。",
        "6. 每只票写清：入选依据（引用你查到的具体数据）、主要风险、证据强弱（强/中/弱）。",
        "7. 只许用工具查到的数据说话。**禁止编造任何数字**（事后有机器逐个核对，查不到的数字会被标出）。",
        "8. 禁止给出目标价与止损位（本系统没有这类数据支撑，给了就是编造）。",
        "9. 结尾给一句话的整体判断：这批票里有没有真正值得动手的；如果整体证据都不够，就直说。",
        "10. **简洁是硬要求**：每只票不超过 4 行，不复述过程，不重复同一只票。",
        "11. 不要用 Markdown 标题/列表符号/加粗；需要分段就用单个换行。",
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

    summarized: bool = False
    """报告是否出自第二阶段的独立总结器（`False` = 调研阶段自己写的）。"""

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


#: 第二阶段（总结）系统提示词 —— **与调研阶段的上下文彻底分开**。
#:
#: 为什么值得分成两阶段：调研阶段的模型手里有 12 个工具 schema、几十轮「我要再查什么」
#: 的来回，最后还要在同一段上下文里写报告 —— 又长又容易把过程话当成结论写进去。
#: 总结器只看**证据摘要**（候选清单 + 工具输出全文），上下文小得多、输出也更聚焦。
#:
#: ⚠ 它不再面对工具，所以「只许用证据里的数字」这条比调研阶段更容易被模型忽略 ——
#: 提示词里再次写死，且幻觉巡检照旧跑（基准 = 同一份证据）。
SUMMARY_SYSTEM_PROMPT = "\n".join(
    [
        "你是 A 股调研报告的**撰写员**（不是分析师）。给你候选清单与已经查到的全部证据，"
        "把它们整理成一份**简洁**报告。",
        "",
        "硬性规则：",
        "1. 只许使用证据里出现过的数字，一个都不许新增、估算、换算、求平均或求和（事后有机器逐个核对）。",
        "2. 证据里没有的项目就说「证据不足」，绝不猜测、绝不用常识补齐。",
        "3. 不要复述证据原文，不要写查询过程（「我查了 K 线」这类话一律不要）。",
        "4. 按**首选 / 次选 / 观察 / 证据不足**四档归类（每只只能落一档），每只写：判断、",
        "   入选依据（引用具体数字）、主要风险。**首选与次选都没有数量上限** —— 证据强的有几只",
        "   就列几只，不要为了「显得严谨」硬压成一只，也不要凑数。",
        "5. 证据不足以区分的就直说「证据不足」并写明缺哪块数据，不要为了凑数硬排。",
        "6. 禁止给出目标价与止损位（证据里没有这类数据，给了就是编造）。",
        "7. 结尾一句话给整体判断：这批里有没有真正值得动手的。",
        "8. 不要用 Markdown 标题/列表符号/加粗；需要分段就用单个换行。",
        "9. **简洁是硬要求**：每只票不超过 4 行，整篇不要重复同一只票。",
    ]
)


def build_summary_messages(
    user_prompt: str,
    evidence: str,
    *,
    brief: bool = False,
) -> list[dict[str, Any]]:
    """第二阶段（总结）的消息列表：候选清单 + 证据全文，**不含任何工具**。

    `user_prompt`：第一阶段的候选清单（各区原文），沿用同一份以免两处口径漂移；
    `evidence`：工具输出全文（= 幻觉巡检的比对基准，两处必须同源）。
    """
    lines = [
        "候选清单：",
        user_prompt,
        "",
        "已查到的证据（每段以 [工具名] 开头）：",
        evidence or "（本轮没有调用任何工具）",
        "",
        "请基于以上内容写报告。",
    ]
    if brief:
        lines += ["", BRIEF_INSTRUCTION, BRIEF_RESEARCH_INSTRUCTION]
    return [
        {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(lines)},
    ]

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


#: 面板区域渲染时**不进提示词**的列（`#` 是行号、`代码`/`名称` 单独渲染）。
_SKIP_COLS = {"#", "代码", "名称"}


def _region_lines(region: PanelRegion) -> list[str]:
    """一个面板区 → 提示词行：**逐列原样**，不换算、不重排。"""
    cols = [c for c in region.columns if c not in _SKIP_COLS]
    lines = [
        f"【{region.title}】共 {len(region.rows)} 只；本区列：{' '.join(cols)}",
    ]
    if region.note:
        lines.append(f"本区口径（面板原文）：{region.note}")
    for i, row in enumerate(region.rows, 1):
        parts = [f"{i} {row.get('symbol')} {row.get('name')}"]
        parts += [f"{c}{row[c]}" for c in cols if row.get(c) not in (None, "")]
        marks = str(row.get("marks") or "").strip()
        if marks:
            parts.append(f"标记{marks}")
        lines.append(" ".join(parts))
    return lines


def build_research_prompt(
    candidates: list[dict[str, Any]],
    origin: str,
    *,
    panel: PanelSnapshot | None = None,
    brief: bool = False,
    features: dict[str, Any] | None = None,
) -> str:
    """构造 research 的 user 提示词（**纯函数**）。

    单一来源：`run_research` 用它发请求，CLI 用同一份做幻觉巡检的比对基准 ——
    此前 CLI 拿 `llm.build_messages()`（analyze 模板）当基准，而模型实际收到的是这里
    拼的这一份，两个 `7.77`（模型从未见过的 `ff_pct`）就被判成"没编造"。

    `panel` 有值时按**区**渲染（v1 池选 / 沪深飙升各段），并保留每区自己的口径原文；
    没有面板（自选清单模式）就退回扁平候选行。

    `features`：`features.build_features` 算好的本地特征（`{symbol: SymbolFeatures}`）。
    有值时在面板后面追加一段**结论化**的本地数据（5日/10日累计、距20日高点、量能、
    最近公告），并明确告诉模型「这些不用再调工具取」—— 实测省掉 `get_kline` /
    `get_announcements` 的往返，代价约 +1.7k tokens。**不塞原始 K 线**：30 只票 × 10 根
    是 3414 字符且模型基本只用来判断「位置/放量」，压成两个百分比即可。
    """
    if panel is not None and panel.regions:
        head = [
            f"以下是终端面板最近一次打印的原文（{panel.date} {panel.time}），共 "
            f"{panel.total} 只，分 {len(panel.regions)} 个区。",
            "**各区口径互不相同，不要跨区排名** —— 它们本就是并列的独立观察段。",
            "",
        ]
        body: list[str] = []
        for region in panel.regions:
            body += _region_lines(region)
            body.append("")
        tail = [
            "上面每个区只给了终端**已经显示的字段**；其余数据（K线形态、资金流明细、"
            "概念板块、龙虎榜、公告、板块共振…）需要你自己调用工具查。",
            "请先查各区候选，再给出排序、入选依据、风险与整体判断。",
        ]
        lines = head + body + tail
    else:
        lines = [
            f"候选股票 {len(candidates)} 只（{origin}）：",
            *_candidate_lines(candidates),
            "",
            "请自己调用工具查真实市场数据（K线/报价/资金流/概念板块/板块涨幅分布/市场概览/飙升榜/系统出现史），",
            "然后给出排序、入选依据、风险与整体判断。",
        ]
    if features:
        order: list[str] = []
        if panel is not None and panel.regions:
            for region in panel.regions:
                order += [str(r.get("symbol")) for r in region.rows]
        else:
            order += [str(c.get("symbol")) for c in candidates]
        block = render_feature_block(features, order)
        if block:
            lines += ["", *block]
    if brief:
        lines += ["", BRIEF_INSTRUCTION, BRIEF_RESEARCH_INSTRUCTION]
    return "\n".join(lines)


#: 软闸：工具调用数达到此值时，给模型注入一句「请收敛」——不硬截断，只提醒。
#: 为什么要有：硬闸（`max_calls`）一旦触发，模型是被**突然掐断**的，那一轮 20~30 秒
#: 白花（真实场景：跑满 30 次调用后还没给结论）。软闸留一轮余地让它自己收尾。
_SOFT_STOP_RATIO = 0.7

#: 并行执行工具调用的线程上限。
#: 实测一轮常见 3~6 个调用；上限设小是为了不把雪球/东财打限频 ——
#: 单次调用本身已是批量（`_MAX_SYMBOLS_PER_CALL=20`），不需要更高并发。
_PARALLEL_WORKERS = 4


def _run_calls_parallel(
    jobs: list[tuple[ToolSpec | None, dict[str, Any]]],
    ctx: ResearchContext,
) -> list[str]:
    """同一轮里模型一次发出的多个工具调用 → **并行**执行，结果与入参同序。

    ## 为什么值得并行

    实测（2026-11-11）：12 个工具**串行全跑一遍只要 15 秒**，而整次调研 47~72 秒 ——
    瓶颈是 **LLM 往返轮次**（网关后面是推理模型，一轮 20~30 秒），不是工具本身。
    模型一轮里能同时发多个调用（OpenAI `parallel_tool_calls` 默认允许），串行执行等于把
    这些调用的耗时**相加**，并行则取最大值。

    ## 线程安全

    工具通过 `ctx.conn_for_thread()` 取连接（子线程各自懒建只读连接），`ctx.cached()`
    内部加锁 ⇒ 共享状态只剩已加锁的缓存。调用方须先用 `ctx.can_parallelize()` 确认主干
    是文件库（`:memory:` 反查不到路径）—— 不满足时自动退回串行。
    """
    if len(jobs) <= 1 or not ctx.can_parallelize():
        return _run_calls_serial(jobs, ctx)

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=min(_PARALLEL_WORKERS, len(jobs))) as pool:
        return list(pool.map(lambda j: _exec_job(j, ctx), jobs))


def _exec_job(job: tuple[ToolSpec | None, dict[str, Any]], ctx: ResearchContext) -> str:
    """执行单个工具调用（`spec` 为 None = 模型编了个不存在的工具名）。"""
    spec, args = job
    if spec is None:
        return "未知工具（该工具不在可用列表里）"
    return run_tool(spec, args, ctx)


def _run_calls_serial(
    jobs: list[tuple[ToolSpec | None, dict[str, Any]]],
    ctx: ResearchContext,
) -> list[str]:
    """串行回退（单调用 / 非文件库 / 并行不可用）。"""
    return [_exec_job(j, ctx) for j in jobs]


def _prefetch_features(
    candidates: list[dict[str, Any]],
    panel: PanelSnapshot | None,
    ctx: ResearchContext,
) -> dict[str, SymbolFeatures] | None:
    """从本地库算候选票特征；任何异常都返回 None（预加载是增强，不是前置条件）。"""
    syms: list[str] = []
    if panel is not None and panel.regions:
        for region in panel.regions:
            syms += [str(r.get("symbol")) for r in region.rows if r.get("symbol")]
    else:
        syms += [str(c.get("symbol")) for c in candidates if c.get("symbol")]
    if not syms:
        return None
    try:
        return build_features(ctx.conn_for_thread(), syms)
    except EXTERNAL_FAILURES:  # 预加载失败绝不能拖垮调研
        return None


def run_research(
    cfg: MagpieConfig,
    candidates: list[dict[str, Any]],
    ctx: ResearchContext,
    *,
    max_rounds: int = 8,
    max_calls: int = 30,
    tools: list[ToolSpec] | None = None,
    origin: str = "榜外异动段今日过门的票",
    brief: bool = False,
    panel: PanelSnapshot | None = None,
    user_prompt: str | None = None,
    summarize: bool = True,
    summarize_always: bool = False,
    prefetch_features: bool = True,
) -> ResearchResult:
    """跑一轮「模型自己调研」。返回最终文本 + 工具调用记录 + 工具输出全文。

    `origin`：候选来源文案（写进提示词，模型据此知道自己在分析什么池子；
    自选清单模式传「用户指定的自选清单」）。
    `panel`：面板原文快照（`panel.parse_panel_md` 的产物）。有值时提示词按**区**渲染并
    保留各区自己的口径 —— `v1 池选` 的 `类别·先验` 与 `榜外异动` 的 T1/T2、以及三段
    各不相同的排序键不是一回事，混成一个扁平清单就丢了「它凭什么在这张表里」。
    `user_prompt`：直接给成品提示词（CLI 用同一份做幻觉巡检基准，保证
    「巡检比对的文本」== 「模型实际收到的文本」）。
    `prefetch_features`：把本地算得出的特征（5日/10日累计、距20日高点、量能、最近公告）
    直接写进提示词，省掉模型调 `get_kline`/`get_announcements` 的往返。纯本地、可失败 ——
    出错就当没有，不影响调研。
    """
    tool_list = tools if tools is not None else build_tools()
    schemas = [t.schema() for t in tool_list]

    # 本地特征预加载：只算**结论化**字段（不塞原始 K 线），失败就当没有。
    # 为什么不在 `build_research_prompt` 里做：那是纯函数，拿不到连接。
    feats: dict[str, SymbolFeatures] | None = None
    if prefetch_features and user_prompt is None:
        feats = _prefetch_features(candidates, panel, ctx)

    user = user_prompt or build_research_prompt(
        candidates, origin, panel=panel, brief=brief, features=feats
    )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": RESEARCH_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]

    calls: list[dict[str, Any]] = []
    blob_parts: list[str] = []
    rounds = 0

    #: 软闸只提醒一次，不反复念叨（反复注入会挤占上下文且让模型以为必须立刻停）。
    soft_stop_warned = False
    soft_stop_at = max(1, int(max_calls * _SOFT_STOP_RATIO))

    while rounds < max_rounds and len(calls) < max_calls:
        rounds += 1
        resp = chat(cfg, messages, tools=schemas)
        tool_calls = resp.get("tool_calls") or []
        content = resp.get("content")

        if not tool_calls:
            # 模型不再要工具 ⇒ 它已经把结论写好了。**默认直接用**（不再多花一轮调用）。
            # `summarize_always=True` 时才改由独立总结器重写一遍：质量更稳（小上下文、
            # 专写报告），代价是**多一次 LLM 往返（约 20~30 秒）** —— 默认关，因为本命令
            # 的痛点是慢，不该为了排版默认加一轮。
            blob_now = "\n".join(blob_parts)
            text = (content or "").strip()
            if summarize_always:
                resp = chat(cfg, build_summary_messages(user, blob_now, brief=brief), tools=None)
                text = (resp.get("content") or "").strip() or text
            if not text:
                text = "（模型未给出结论）"
            return ResearchResult(
                text=text,
                rounds=rounds,
                tool_calls=calls,
                tool_output_blob=blob_now,
                summarized=bool(summarize_always),
            )

        # 模型的 assistant 轮次必须回放（含它要调的工具），否则下一轮它看不到自己请求过什么。
        # ⚠ 实测（2026-11-11）：网关接受「只带 content」的 assistant 消息，但**规范做法是
        #   带上 tool_calls** —— 不带时模型可能重复发起同一批查询（白烧一轮往返）。
        assistant_msg: dict[str, Any] = {"role": "assistant", "content": content or ""}
        assistant_msg["tool_calls"] = [
            {
                "id": tc.get("id") or f"call_{i}",
                "type": "function",
                "function": {"name": tc.get("name") or "", "arguments": tc.get("arguments") or "{}"},
            }
            for i, tc in enumerate(tool_calls)
        ]
        messages.append(assistant_msg)

        # 预算耗尽：只执行装得下的那几个，其余如实告知（**不静默丢**）。
        room = max_calls - len(calls)
        runnable, skipped = tool_calls[:room], tool_calls[room:]
        jobs: list[tuple[ToolSpec | None, dict[str, Any]]] = []
        names: list[str] = []
        for tc in runnable:
            name = tc.get("name") or ""
            raw_args = tc.get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
            except json.JSONDecodeError:
                args = {}
            jobs.append((next((t for t in tool_list if t.name == name), None), args))
            names.append(name)

        # 同一轮里的多个调用**并行**执行（省 LLM 往返，见 `_run_calls_parallel`）。
        outputs = _run_calls_parallel(jobs, ctx)
        for tc, name, args, out in zip(runnable, names, [j[1] for j in jobs], outputs, strict=True):
            calls.append({"name": name, "args": args, "ok": bool(out) and "失败" not in out[:40]})
            blob_parts.append(f"[{name}] {out}")
            messages.append({"role": "tool", "tool_call_id": tc.get("id") or name, "content": out})

        # 超额未执行的调用也必须回一条 tool 消息 —— 协议要求每个 tool_call_id 都有响应，
        # 且模型必须知道「这几次没查」而不是以为查过了。
        for tc in skipped:
            nm = tc.get("name") or ""
            messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id") or nm,
                "content": f"（本次未执行 {nm}：已达工具调用上限 {max_calls} 次，请基于已有数据给结论）",
            })

        # 软闸：到阈值就提醒收敛，但**不截断** —— 让它自己用剩下的一两轮收尾。
        if not soft_stop_warned and len(calls) >= soft_stop_at:
            soft_stop_warned = True
            messages.append({
                "role": "user",
                "content": (
                    f"（已用 {len(calls)}/{max_calls} 次工具调用。请不要再扩大查询范围 —— "
                    "用现有数据给结论；若还有最关键的一两项没查，只补那一项。）"
                ),
            })

    # 预算用尽仍未给结论 → **第二阶段：独立总结**。
    #
    # ⚠ 关键：这一路**本来就有一次收尾调用**（此前是在研究上下文里再问一句「请给结论」）。
    #   现在把它换成只看证据的总结器 ⇒ **调用次数不变**，但上下文从「12 个工具 schema +
    #   全部来回」缩成「候选清单 + 证据全文」，更快也更聚焦。没有靠加调用换简洁。
    blob = "\n".join(blob_parts)
    if not summarize:
        return ResearchResult(
            text="（已达工具调用上限，本次未生成结论）",
            rounds=rounds,
            tool_calls=calls,
            tool_output_blob=blob,
        )
    resp = chat(cfg, build_summary_messages(user, blob, brief=brief), tools=None)
    text = (resp.get("content") or "").strip() or "（模型未给出结论）"
    return ResearchResult(text=text, rounds=rounds, tool_calls=calls, tool_output_blob=blob, summarized=True)
