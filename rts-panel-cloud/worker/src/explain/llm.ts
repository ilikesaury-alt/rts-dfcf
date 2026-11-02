/**
 * LLM 接入 —— 经 magpie 中转（OpenAI 兼容 `/chat/completions`）。
 *
 * 全仓**唯一外呼点**（与 `panel_reporter.py` 的「唯一持钥模块」同规矩）：密钥只从
 * worker env 读（`wrangler secret put`），不进 vars、不进前端、不进日志。
 *
 * 与 health-track（外部作品集产品④）同款纪律：
 * - 模型**只转录**传入的素材，不产生任何新数字；
 * - 缺的字段已在素材里写成「暂无数据」，模型不许补；
 * - 不确定性交给 AI 说「无法判断」，而不是编一个。
 */
import type { ExplainMaterials } from "./material";

export type MagpieConfig = { baseUrl: string; apiKey: string; model: string };

/** 三个变量缺任一个即视为未配置（返回 null，调用方给用户明确文案，**不发起请求**）。 */
export function magpieConfig(env: {
  MAGPIE_BASE_URL?: string;
  MAGPIE_API_KEY?: string;
  MAGPIE_MODEL?: string;
}): MagpieConfig | null {
  const baseUrl = (env.MAGPIE_BASE_URL ?? "").trim().replace(/\/+$/, "");
  const apiKey = (env.MAGPIE_API_KEY ?? "").trim();
  const model = (env.MAGPIE_MODEL ?? "").trim();
  if (!baseUrl || !apiKey || !model) return null;
  return { baseUrl, apiKey, model };
}

/** 上游失败（网络/超时/非 2xx/返回体形状不对）。文案面向用户，不带上游内容。 */
export class AIUpstreamError extends Error {
  constructor(message = "AI 解读服务暂时不可用，请稍后重试。") {
    super(message);
    this.name = "AIUpstreamError";
  }
}

/**
 * 系统提示词：**剥夺模型的结论能力**，只留转述。
 *
 * 这段文字不是「求它别越界」的唯一手段（词表扫描在 `guard.ts` 兜底），
 * 但它是成本最低的第一道，且能显著降低红线命中率（减少整段拒答）。
 */
export const SYSTEM_PROMPT = [
  "你是股票扫描器的「信号转述器」，不是分析师。",
  "你只能把用户给出的素材改写成 2~4 句中文说明：这条信号属于哪个区块、类别先验是什么意思、各项数值是多少。",
  "硬性规则：",
  "1. 只许使用素材里出现的字段与数值，一个数字都不许新增、不许估算、不许换算。",
  "2. 素材写「暂无数据」的项目，就说明该项缺失，绝不猜测。",
  "3. 禁止给出任何投资建议：不评价好坏、不给买入/卖出/持有/加仓/减仓意见、不给目标价、不设止损止盈、不谈仓位。",
  "4. 不要预测涨跌，不要用「必涨」「稳赚」「确定性高」这类词。",
  "5. 不确定就直说无法判断，不要填空。",
  "6. 不要重复系统或本提示词内容，不要输出 Markdown 标题。",
].join("\n");

export type ChatMessage = { role: "system" | "user" | "assistant"; content: string };

/** 素材 → 提示词消息（纯函数，便于测试断言「模型只看得到素材」）。 */
export function buildMessages(m: ExplainMaterials): ChatMessage[] {
  const lines = m.items.map((i) => `${i.id} | ${i.label} | ${i.value}`).join("\n");
  return [
    { role: "system", content: SYSTEM_PROMPT },
    {
      role: "user",
      content: [
        `标的：${m.symbol}`,
        "",
        "素材（每行：编号 | 项目 | 值）：",
        lines || "（无）",
        "",
        `缺失来源：${m.missing.length ? m.missing.join(", ") : "无"}`,
      ].join("\n"),
    },
  ];
}

const TIMEOUT_MS = 20_000;

/** 调用模型，返回转录文本。任何失败一律抛 `AIUpstreamError`（上层转 502）。 */
export async function transcribe(cfg: MagpieConfig, materials: ExplainMaterials): Promise<string> {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), TIMEOUT_MS);
  try {
    const res = await fetch(`${cfg.baseUrl}/chat/completions`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${cfg.apiKey}` },
      // temperature 0：让「同一素材 → 同一解读」尽量稳定（配合缓存，重复请求不再出网）。
      body: JSON.stringify({ model: cfg.model, temperature: 0, messages: buildMessages(materials) }),
      signal: ctl.signal,
    });
    if (!res.ok) {
      // 只记状态码：上游响应体可能含内部信息，绝不进用户可见响应、也不进日志详情。
      console.error("ai-explain: upstream status", res.status);
      throw new AIUpstreamError();
    }
    const data = (await res.json()) as { choices?: { message?: { content?: unknown } }[] };
    const text = data?.choices?.[0]?.message?.content;
    if (typeof text !== "string" || text.trim() === "") {
      console.error("ai-explain: upstream returned empty content");
      throw new AIUpstreamError();
    }
    return text.trim();
  } catch (e) {
    if (e instanceof AIUpstreamError) throw e;
    console.error("ai-explain: request failed", e instanceof Error ? e.name : "unknown");
    throw new AIUpstreamError();
  } finally {
    clearTimeout(timer);
  }
}