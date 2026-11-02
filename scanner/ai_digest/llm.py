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

from scanner.ai_digest.material import OffboardMaterials
from scanner.utils import EXTERNAL_FAILURES

__all__ = [
    "AIUpstreamError",
    "MagpieConfig",
    "SYSTEM_PROMPT_ANALYZE",
    "SYSTEM_PROMPT_TRANSCRIBE",
    "build_messages",
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


def build_messages(m: OffboardMaterials, mode: str = "analyze") -> list[dict[str, str]]:
    """素材 → 提示词消息（纯函数，便于测试断言「模型只看得到素材」）。"""
    lines = "\n".join(f"{i.id} | {i.label} | {i.value}" for i in m.items)
    user = "\n".join(
        [
            f"信号日：{m.date}",
            f"素材（每行：编号 | 项目 | 值，共 {len(m.items)} 项）：",
            lines or "（无）",
            "",
            f"缺失项：{'、'.join(m.missing) if m.missing else '无'}",
        ]
    )
    system = SYSTEM_PROMPT_ANALYZE if mode == "analyze" else SYSTEM_PROMPT_TRANSCRIBE
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def transcribe(
    cfg: MagpieConfig, materials: OffboardMaterials, *, timeout: float = TIMEOUT_S, mode: str = "analyze"
) -> str:
    """调用模型，返回文本。任何失败一律抛 `AIUpstreamError`（CLI 转退出码 2）。"""
    try:
        import requests
    except ImportError:  # pragma: no cover - 环境相关
        raise AIUpstreamError("缺少 requests 依赖，无法调用模型服务。") from None

    body = {
        "model": cfg.model,
        # temperature 0：只为减少随机性，**不保证「同一素材 → 同一文案」**（实测 2026-11-03：
        # 同一素材跑两次，措辞与分句完全不同，但抽出的数字集合逐项相同 ⇒ 事实稳定、表述不稳）。
        # 因此人工核对应该看**数字对不对**，而不是看文案两次是否相同。
        "temperature": 0,
        # ⚠ **显式关闭流式**（照抄 `fde-agent-delivery/product1/lib/llm.ts` 的踩坑结论）：
        #   若中转默认走 SSE，响应体不是单个 JSON，`resp.json()` 会解析失败并被误报成
        #   「服务不可用」—— 那是把协议协商问题伪装成故障，最难查。
        "stream": False,
        "messages": build_messages(materials, mode),
    }
    try:
        resp = requests.post(
            f"{cfg.base_url}/chat/completions",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {cfg.api_key}"},
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            timeout=timeout,
        )
    except EXTERNAL_FAILURES as e:
        # 只记异常类型：URL/响应体可能含内部信息，不进用户可见输出、也不进日志详情。
        raise AIUpstreamError(f"模型服务调用失败（{type(e).__name__}），请稍后重试。") from None

    if resp.status_code in (401, 403):
        raise AIUpstreamError("模型服务凭据无效（HTTP 401/403），请检查 MAGPIE_API_KEY。")
    if resp.status_code == 429:
        raise AIUpstreamError("模型服务限流（HTTP 429），请稍后重试。")
    if resp.status_code != 200:
        # 只回状态码：上游响应体可能含内部信息。
        raise AIUpstreamError(f"模型服务返回 HTTP {resp.status_code}，请稍后重试。")

    try:
        payload = resp.json()
        text = payload["choices"][0]["message"]["content"]
    except EXTERNAL_FAILURES:
        raise AIUpstreamError("模型服务返回体形状异常（不是预期的 chat/completions 结构）。") from None
    except (TypeError, IndexError, AttributeError):
        raise AIUpstreamError("模型服务返回体形状异常（不是预期的 chat/completions 结构）。") from None

    if not isinstance(text, str) or not text.strip():
        raise AIUpstreamError("模型服务返回空内容。")
    # 只去**行尾空白**：实测 2026-11-03 真实模型会输出 Markdown 硬换行的「行尾双空格」
    # （5 行里有 4 行）—— 直接打出来看不出来，复制出去却是脏字符。不改任何非空白字符，
    # 正文仍算「原样展示」。
    return "\n".join(line.rstrip() for line in text.strip().splitlines())
