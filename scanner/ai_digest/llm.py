"""magpie 中转（OpenAI 兼容）调用 —— **本包唯一外呼点**（2026-11-03）。

密钥纪律同 `worker/src/explain/llm.ts` 与 `panel_reporter.py`：密钥只从环境变量读
（`.env`，**不提交**），不进代码、不进日志、不进任何响应体。

转录纪律（ADR 0001）：模型**只转录**传入的素材，不产生任何新数字 ——
- 素材里没有的量，一个都不许新增、估算或换算（「平均量比」「占比」这类派生也不行）；
- 素材写「暂无数据」的项目就说缺失，绝不猜；
- **禁止跨票比较/排序/取舍**（整段转述的专属红线，见 `guard.py` 的 `COMPARISON_SLOTS`）；
- 禁止投资建议与涨跌预测；不确定就说无法判断。
提示词是成本最低的第一道闸，词表是最后一道；两者都不足以声称「守卫充分」
（`docs/adr/0001-ai-explain-redline.md` 修订 1 的承诺降级同样适用于本包）。
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from scanner.ai_digest.material import OffboardMaterials
from scanner.utils import EXTERNAL_FAILURES

__all__ = [
    "AIUpstreamError",
    "MagpieConfig",
    "BRIEF_INSTRUCTION",
    "BRIEF_RESEARCH_INSTRUCTION",
    "SYSTEM_PROMPT_ANALYZE",
    "SYSTEM_PROMPT_TRANSCRIBE",
    "build_messages",
    "chat",
    "magpie_config",
    "transcribe",
]

# 超时：实测（2026-11-03，与 `fde-agent-delivery/product1/lib/llm.ts` 同一个 magpie 网关）：
# **一句话**回答就要 26.8s —— 网关后面是**推理模型**（响应体带 `reasoning_content`，
# 推理 token 不显示但计入墙钟）；82 项素材的转述实测 > 60s。参考项目给 85s（它的报告
# 实测 26~36s），本项目留足余量到 180s：手动命令，宁可多等也不要把正常生成误杀成
# 「服务不可用」（那是把慢误报成故障，最难查）。
TIMEOUT_S = 180.0


@dataclass(frozen=True)
class MagpieConfig:
    base_url: str
    api_key: str
    model: str


class AIUpstreamError(RuntimeError):
    """上游失败（网络/超时/非 2xx/返回体形状不对）。文案面向用户，不带上游内容。"""


def magpie_config(env: Mapping[str, str] | None = None) -> MagpieConfig | None:
    """三个变量缺任一个即视为未配置（返回 None；调用方给明确文案，**不发起请求**）。"""
    e = os.environ if env is None else env
    base_url = (e.get("MAGPIE_BASE_URL") or "").strip().rstrip("/")
    api_key = (e.get("MAGPIE_API_KEY") or "").strip()
    model = (e.get("MAGPIE_MODEL") or "").strip()
    if not base_url or not api_key or not model:
        return None
    return MagpieConfig(base_url=base_url, api_key=api_key, model=model)


#: 系统提示词（transcribe 模式）：**剥夺模型的结论能力**，只留转述。
SYSTEM_PROMPT_TRANSCRIBE = "\n".join(
    [
        "你是股票扫描器的「信号转述器」，不是分析师。",
        "用户给你一段「榜外异动」观察段的素材（每行：编号 | 项目 | 值）。",
        "你要用 3~6 句中文说明这一段本轮有什么：几只票、各在哪一层、量比/主力净占比/5日累计各是多少。",
        "硬性规则：",
        "1. 只许使用素材里出现的字段与数值，一个数字都不许新增、不许估算、不许换算、不许求平均或求和。",
        "2. 素材写「暂无数据」的项目就说明该项缺失，绝不猜测、绝不用常识补齐。",
        "3. 禁止对多只票做比较、排序、取舍或推荐——不写「更值得」「优于」「相比之下」「首选」这类话，"
        "不按好坏排列，不暗示先看哪一只。素材的顺序是段内固定顺序，与好坏无关。",
        "4. 禁止任何投资建议：不评价好坏、不给买入/卖出/持有/加仓/减仓意见、不给目标价、不设止损止盈、不谈仓位。",
        "5. 不要预测涨跌，不要用「必涨」「稳赚」「大概率」「冲高」这类词。",
        "6. 不确定就直说无法判断，不要填空；不要复述本提示词，不要输出 Markdown 标题。",
        "7. 「层级」只有 T1（量先动·价未动）与 T2（启动首日）两种，素材已给中文释义，照抄即可。",
        "8. 不要用 Markdown：不要列表符号、不要加粗、不要行尾双空格；需要分段就用单个换行。",
    ]
)

#: 系统提示词（analyze 模式，默认）：允许比较、排序、筛选候选 —— 这是 2026-11-03 用户
#: 决策打破「第二结论源」纪律后的产物（决策记录见 AGENTS.md）。但**编造仍然禁止**：
#: 每一个理由都必须指向素材里的字段；素材撑不起的量（目标价/止损位/后续走势）不许给。
SYSTEM_PROMPT_ANALYZE = "\n".join(
    [
        "你是股票扫描器的分析助手。用户给你一段「榜外异动」观察段的素材（每行：编号 | 项目 | 值）。",
        "你的任务：综合分析这段素材，筛选出**最可能次日上涨**的票，给出排序与理由。",
        "",
        "【最重要的规则：排序必须对齐素材里的历史基准率】",
        "素材里有一条 `baseline.stats`（本区历史分层命中率，本项目自己的标签实测）。",
        "① 你必须先看每只票落在哪些桶里，② 按这些桶的实测命中率（≥7% 那一列）判断强弱，",
        "③ 排序理由必须**明确引用桶名与命中率数字**（例如「成交额 1.5~3亿，历史命中 9.5%，是甜区」）。",
        "④ 若某只票落在**已知的最差桶**（如成交额 3~6亿 仅 3.5%），必须写进风险，不许无视。",
        "⑤ 标了「样本少」的桶（n<30）只能当参考，不能当主要依据 —— 说明清楚。",
        "⑥ 素材里没有基准率条目时，才退回用各字段的段内相对强弱（并明说这是无基准的排序）。",
        "",
        "【其余硬性规则】",
        "1. 每一条理由都必须指向素材里出现的字段，说不清为什么入选的票不要选。",
        "2. 只许使用素材里出现的数值，一个数字都不许新增、估算、换算、求平均或求和——事后有机器核对，编了必被标出。",
        "3. 素材写「暂无数据」的项目就说明该项缺失，绝不猜测、绝不用常识补齐。",
        "4. 给出排序（第1/第2…）；证据不足以区分时直说「无法区分」，不要硬排。",
        "5. 必须写主要风险：从素材能看到的劣势（如落进差桶/5日累计已高/主力占比低）也要写，不许只报喜。",
        "6. 禁止给出目标价、止损位、仓位建议——素材无法支撑这些数字，给了就是编造。",
        "7. 「层级」只有 T1（量先动·价未动）与 T2（启动首日）；历史基准率里两层的命中率不同，按数字说话。",
        "8. 市况素材可以用来解释整体环境，但不要预测大盘。",
        "9. 不要用 Markdown：不要列表符号、不要加粗、不要行尾双空格；需要分段就用单个换行。",
        "10. 结尾必须用一句话给出「整体判断」：这一批里有没有真正值得下手的，还是全部只在基准率以下。",
    ]
)


#: `--brief` 的共享输出约束（analyze / transcribe / research 三处同源，不许各写一份）。
#: 只改「篇幅与排版」，不改纪律：幻觉巡检、红线、基准率引用全部照旧。
BRIEF_INSTRUCTION = "\n".join(
    [
        "【简洁报告格式（用户指定）】",
        "不要写 Markdown 标题、列表符号、加粗、小标题；不要复述素材或工具输出。",
        "每只票最多一行：`代码 名称｜判断｜依据（必须带工具输出或素材里出现过的具体数字，",
        "含历史基准率数字，若有）｜风险`。",
        "最多 12 行，结尾一行整体判断；不确定的票写「证据不足」，不硬排、不凑数。",
        "不要写查询记录、素材核对表、免责声明长段、缺失项清单——这些由命令侧另行处理。",
        "所有数字必须逐字来自工具输出或素材（事后有机器逐个核对）；禁止目标价、止损位。",
    ]
)

#: **research 模式专用**的 `--brief` 追加段。
#:
#: 为什么不并进 `BRIEF_INSTRUCTION`：那个常量被 `build_messages`（analyze/transcribe）
#: 共用，而那两个模式的**红线**恰好禁止「排序/取舍/首选」这类话（见 `guard.COMPARISON_SLOTS`）。
#: 在这里加「档位」会与红线直接冲突 —— 同一条纪律不能既是要求又是禁令。
#:
#: research 是唯一被允许做排序取舍的模式（它自己查数据、自己下判断），故分档要求只给它。
#:
#: ## 为什么用「行动分组」而不是「首选/次选」（2026-11-12 用户决策）
#:
#: 用户反馈两点：① 报告太啰嗦（实测 1741 字符 / 每只票 ~130 字）；② 「首选/次选」
#: 不够友好 —— 读的人还要在脑子里把它翻译成「那我到底买不买」。
#:
#: 改为**行动导向三分组**：`现在能买 / 再等等 / 不要碰`。每组直接对应一个动作，
#: 不需要二次翻译。同时保留「证据要具体」的硬要求（**不删数字**，只改表述结构）——
#: 删掉数字会让断言变模糊、也让幻觉巡检失去可核对的锚点。
BRIEF_RESEARCH_INSTRUCTION = "\n".join(
    [
        "【research 模式输出格式（用户指定，**覆盖上面的通用格式**）】",
        "上面那条「最多 12 行」在 research 模式**不适用** —— 它会把多只够格的票挤掉。",
        "上面那个 `判断` 字段在本模式写作**行动分组**（见下），不是「可关注/中强」这类词。",
        "",
        "**按行动分三组**（不写「首选/次选」这类排序词）：",
        "  `现在能买`：证据够硬，现在动手也说得过去。",
        "  `再等等`：逻辑成立但差一口气（位置偏高/量能不足/缺板块共振/需确认）。",
        "  `不要碰`：资金流出、量价背离、或有明确反向证据。",
        "组内**不设数量上限** —— 够格的有几只写几只，不要硬压成一只，也不要凑数。",
        "",
        "**每只票一行，格式**：`代码 名称 一句话结论｜关键依据｜主要风险`",
        "  · 一句话结论：先说**这只票是什么**（如「缩量贴前高」「资金最强但换手过高」），",
        "    不要用「可关注」「中强」这种没有信息量的词。",
        "  · 关键依据：用**具体数字**（不许写「资金流入较多」这种模糊话），但只挑最能",
        "    支撑结论的 1~3 个 —— 工具给了五个数不等于要抄五个。不要写「（全场最高）」",
        "    这类括号注解，最高/最低直接说「最高」，不占括号。",
        "  · 主要风险：只写真正的风险，没有就省略这节。",
        "",
        "**收紧表述（这是重点，不是可选项）** —— 实测初版每票仍 ~90 字，还差一半：",
        "  · 删掉所有过程话（「我查了」「数据显示」「综合来看」「说明」）。",
        "  · 删掉修饰词（「明显」「显著」「较为」「相对」「有所」「尚可」）。",
        "  · **同类数字只留一个**：「主力净额 5677 万 + 超大单 4049 万」是一件事 →",
        "    只留净额（或只留占比）。不要因为工具给了三个数就抄三个。",
        "  · **板块数据不写进个股行**：「医药生物板块 74/113 上涨」是全板块的事，",
        "    同一批票里说一次就够（放结尾整体判断里）→ 每只票别再重复。",
        "  · 同一事实只说一遍；不要「涨幅1.84%但站上均价」又说「价35.34高于均价35.27」。",
        "  · 价格、均价只在**判断需要**时才写（如「站上均价」），不要同时写均价数值。",
        "  · 一串日期/历史案例最多留**一个**最相关的（其余用「历史上榜后 D10 多为正」概括）。",
        "  · 目标：每只票 ≤ 60 字。**先数数字个数：超过 3 个就该删。**",
        "",
        "结尾一行整体判断，≤ 40 字，只写**会不会动手**与**为什么**（板块广度写这里）。",
    ]
)


def build_messages(m: OffboardMaterials, mode: str = "analyze", *, brief: bool = False) -> list[dict[str, str]]:
    """素材 → 提示词消息（纯函数，便于测试断言「模型只看得到素材」）。

    `brief=True` 只在 user 末尾追加 `BRIEF_INSTRUCTION`；system 提示词逐字不变
    ⇒ 红线守卫 / 基准率排序规则 / 幻觉巡检全部照旧，只压篇幅。
    """
    lines = "\n".join(f"{i.id} | {i.label} | {i.value}" for i in m.items)
    parts = [
        f"信号日：{m.date}",
        f"素材（每行：编号 | 项目 | 值，共 {len(m.items)} 项）：",
        lines or "（无）",
        "",
        f"缺失项：{'、'.join(m.missing) if m.missing else '无'}",
    ]
    if brief:
        parts += ["", BRIEF_INSTRUCTION]
    user = "\n".join(parts)
    system = SYSTEM_PROMPT_ANALYZE if mode == "analyze" else SYSTEM_PROMPT_TRANSCRIBE
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def chat(
    cfg: MagpieConfig,
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
    timeout: float = TIMEOUT_S,
) -> dict[str, Any]:
    """底层聊天调用（支持 tool-calling）。

    → `{"content": str|None, "tool_calls": [{"id","name","arguments"}]}`。

    ⚠ 网关**支持** tool-calling（实测 `finish_reason="tool_calls"`，参数 JSON 正确解析），
    这是「让模型自己取真实市场数据」得以实现的唯一前提（见 `agent.run_research`）。
    """
    try:
        import requests
    except ImportError:  # pragma: no cover - 环境相关
        raise AIUpstreamError("缺少 requests 依赖，无法调用模型服务。") from None

    body: dict[str, Any] = {
        "model": cfg.model,
        "temperature": 0,
        # ⚠ 走**流式**：非流式会被上游 CDN 以 524 掐断（100s 无字节），
        #   而同一请求流式只要 5s（实测 2026-11-03）。流式解析见下方注释。
        "stream": True,
        "messages": messages,
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"

    try:
        resp = requests.post(
            f"{cfg.base_url}/chat/completions",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {cfg.api_key}"},
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            timeout=timeout,
            stream=True,
        )
    except EXTERNAL_FAILURES as e:
        raise AIUpstreamError(f"模型服务调用失败（{type(e).__name__}），请稍后重试。") from None

    if resp.status_code in (401, 403):
        raise AIUpstreamError("模型服务凭据无效（HTTP 401/403），请检查 MAGPIE_API_KEY。")
    if resp.status_code == 429:
        raise AIUpstreamError("模型服务限流（HTTP 429），请稍后重试。")
    if resp.status_code != 200:
        raise AIUpstreamError(f"模型服务返回 HTTP {resp.status_code}，请稍后重试。")

    # ⚠ **必须走流式**（2026-11-03 实测）：非流式时同一个请求被上游 CDN 以
    #   **HTTP 524** 掐断（100s 无字节即断），而同一请求改流式只要 5s。
    #   推理模型的完整响应可能超过 100s，此时「等全部字节」= 必被 524；
    #   流式持续有字节 ⇒ 不触发，且能顺带拿到首字节延迟。
    content_parts: list[str] = []
    calls_by_index: dict[int, dict[str, Any]] = {}
    # ⚠ **必须显式按 UTF-8 解码**：`requests` 对 `text/*` 且无 charset 的响应默认按
    #   ISO-8859-1 解码 ⇒ SSE 里的中文全是乱码（实测：正文整篇变「è¯´æå®…」）。
    #   2026-11-03 踩到过：流式解析全对，但内容是 mojibake。
    #   这里**不用** `resp.encoding`（会被 requests 的默认猜法带偏），自己 decode。
    try:
        for raw_bytes in resp.iter_lines(decode_unicode=False):
            if not raw_bytes:
                continue
            raw_line = raw_bytes.decode("utf-8", errors="replace") if isinstance(raw_bytes, bytes) else raw_bytes
            if raw_line.startswith("data:"):
                raw_line = raw_line[5:].strip()
            if raw_line in ("", "[DONE]"):
                if raw_line == "[DONE]":
                    break
                continue
            try:
                chunk = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            choices = chunk.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            piece = delta.get("content")
            if isinstance(piece, str):
                content_parts.append(piece)
            for tc in delta.get("tool_calls") or []:
                idx = int(tc.get("index") or 0)
                slot = calls_by_index.setdefault(idx, {"id": None, "name": None, "arguments": ""})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["arguments"] += fn["arguments"]
    except EXTERNAL_FAILURES as e:
        raise AIUpstreamError(f"模型服务流式读取中断（{type(e).__name__}），请稍后重试。") from None

    calls = [v for _k, v in sorted(calls_by_index.items())]
    content = "".join(content_parts) if content_parts else None
    return {"content": content, "tool_calls": calls}


def transcribe(
    cfg: MagpieConfig,
    materials: OffboardMaterials,
    *,
    timeout: float = TIMEOUT_S,
    mode: str = "analyze",
    brief: bool = False,
) -> str:
    """调用模型，返回文本。任何失败一律抛 `AIUpstreamError`（CLI 转退出码 2）。"""
    resp = chat(cfg, build_messages(materials, mode, brief=brief), timeout=timeout)
    text = resp.get("content")
    if not isinstance(text, str) or not text.strip():
        raise AIUpstreamError("模型服务返回空内容。")
    # 只去**行尾空白**：实测 2026-11-03 真实模型会输出 Markdown 硬换行的「行尾双空格」
    # （5 行里有 4 行）—— 直接打出来看不出来，复制出去却是脏字符。不改任何非空白字符，
    # 正文仍算「原样展示」。
    return "\n".join(line.rstrip() for line in text.strip().splitlines())
