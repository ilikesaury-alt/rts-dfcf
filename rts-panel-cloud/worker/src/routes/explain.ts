/**
 * `GET /api/ai-explain?symbol=` —— AI 解读（E2，ADR 0001）。
 *
 * 为什么是 **GET** 而不是 POST：面板的 NFR 是只读，全仓唯一写端点是 `/api/ingest`，
 * 且 `api/client.ts` 明文写着「不新增端点给 `apiSend` 找用武之地」。本端点**不写任何表**，
 * 语义上就是读 ⇒ 用 GET，并把唯一参数放 query（请求面只有 `symbol`，**没有自由文本**，
 * 「越界提问」这条攻击面在结构上不存在）。
 *
 * 四步流水线（顺序有意为之：先便宜后贵，且任何一步不过都不出网）：
 *   ① 参数校验 → ② 确定性素材组装（`explain/material.ts`）
 *   → ③ 配置检查 + 缓存命中 → ④ 模型转录 → ⑤ 红线词表扫描 → 定稿文案
 *
 * ⚠ AI 输出**绝不**进 `recommendations` / 排序 / 过门；此处连 D1 写语句都没有。
 */
import type { Env } from "../env";
import { AppError } from "../error";
import { AIUpstreamError, magpieConfig, transcribe } from "../explain/llm";
import { buildMaterials, hasAnySource, type ExplainMaterials, type MaterialItem } from "../explain/material";
import { REFUSAL, isRedlined } from "../explain/guard";
import type { Regions, Row } from "../serialize";
import { isSymbol } from "../symbol";

/** 每个响应都必须带的标签（服务端保证，前端不信任自拼 —— 验收 §3-4）。 */
export const AI_LABEL = "仅解读，非投资建议";

export { REFUSAL };

type Ctx<E> = {
  env: E;
  json: (b: unknown, s?: number) => Response;
  req: { query: (k: string) => string | undefined };
};

export type ExplainResponse = {
  symbol: string;
  label: string;
  text: string;
  /** 是否因触红线被整段替换（前端可显不同底色）。 */
  redlined: boolean;
  /** 是否命中内存缓存（同素材重复请求不出网）。 */
  cached: boolean;
  materials: MaterialItem[];
  missing: string[];
  /**
   * 素材来源（**两侧轮次分别披露**，不假装是同一时刻）：
   * - `date`/`time` = 最新快照轮次；
   * - `klineDate`   = 该票 K 线的最新交易日（**可能落后快照**，票掉出 20 票配额后即如此）。
   *
   * ⚠ 审查 A / P2-2：Spec 原文写「禁止跨表拼装不同轮数据」，实现是跨表拼装
   *   （快照按轮、K 线按票）。改为「允许跨表，但必须两侧都披露」——
   *   强制同轮会使命中率极低（票掉出配额后就没有该轮 K 线），反而让解读大面积不可用。
   */
  generatedFrom: { date: string; time: string; klineDate: string | null } | null;
};

/**
 * 进程内缓存（决策 P3）：key = 模型 + 素材内容，值是已定稿文案。
 * 重启即失（可接受）—— 省 D1 写入配额，且绝不进任何计算路径。
 * 容量上限防极端情况下无界增长（Map 保持插入序，超限删最早一条）。
 */
const CACHE = new Map<string, { text: string; redlined: boolean }>();
const CACHE_MAX = 200;

/** 同 symbol 的出网冷却（毫秒）。扫描轮间隔 60s，取 30s：够挡住连点，不妨碍正常换轮解读。 */
const THROTTLE_MS = 30_000;

/** symbol → 上次**出网**时刻（缓存命中不计入，故正常刷新不受影响）。 */
const LAST_CALL = new Map<string, number>();

function cacheKey(model: string, m: ExplainMaterials): string {
  // ⚠ 必须带上 symbol：素材 items 里**不含**代码（只含名称/数值等），两只字段值完全相同的票
  //   会得到同一个 items 串 ⇒ 不加 symbol 就会串味（实测：缓存把 A 票的解读返回给 B 票）。
  // ⚠ 必须带上 `missing`：提示词末行是「缺失来源」清单，而 items 里的「暂无数据」
  //   并不与 missing 一一对应（当前不会撞，但那是两个状态的**隐式巧合**；将来加一个
  //   「只登记 missing、不产出 item」的来源就会串味 —— 文案里会说错缺什么）。
  // 素材即键：同为纯函数产物，内容不同必然键不同。无需抗碰撞哈希（不做安全边界用）。
  return `${model}|${m.symbol}|${JSON.stringify(m.missing)}|${JSON.stringify(m.items)}`;
}

function parseRegions(payload: string | undefined): Regions | undefined {
  if (!payload) return undefined;
  try {
    const parsed = JSON.parse(payload) as { regions?: Regions };
    return parsed.regions;
  } catch (e) {
    // payload 损坏不该让解读 500：降级为「无区块素材」（K 线素材仍可用）。
    console.error("ai-explain: snapshot payload parse failed", e);
    return undefined;
  }
}

export async function aiExplain<E extends Env>(c: Ctx<E>): Promise<Response> {
  const symbol = (c.req.query("symbol") ?? "").trim();
  if (!isSymbol(symbol)) {
    throw new AppError("BAD_REQUEST", "代码格式非法：应为 6 位数字或带 SH/SZ/BJ 前缀", 400);
  }

  // ① 取素材来源：最新快照（1 查询）+ 该票 K 线（1 查询）。
  //    两者都是「最新可用」，不假装同一轮 —— `generatedFrom` 如实回告快照轮次。
  const snap = await c.env.DB.prepare(
    `SELECT date, time, payload FROM snapshots ORDER BY date DESC, time DESC LIMIT 1`,
  ).first<{ date: string; time: string; payload: string }>();

  const kl = await c.env.DB.prepare(
    `SELECT kline_date, kline, appearances FROM kline_cache WHERE symbol = ? ORDER BY kline_date DESC LIMIT 1`,
  )
    .bind(symbol)
    .first<{ kline_date: string; kline: string; appearances: string }>();

  const materials = buildMaterials({
    symbol,
    regions: parseRegions(snap?.payload),
    kline: kl ? { klineDate: kl.kline_date, kline: safeArray<number[]>(kl.kline), appearances: safeArray<Row>(kl.appearances) } : null,
  });

  if (!hasAnySource(materials)) {
    throw new AppError("NOT_IN_REPORT", `该股票（${symbol}）暂无上报数据，无法解读`, 404);
  }

  // ② 配置检查：未配置就明确告知，**不发起请求**（验收 §3-2/§3-3）。
  const cfg = magpieConfig(c.env);
  if (!cfg) {
    throw new AppError("AI_UNAVAILABLE", "AI 解读暂不可用：未配置模型服务凭据，请检查配置后重试。", 503);
  }

  // ③ 缓存：同素材 + 同模型直接复用，杜绝重复计费。
  const key = cacheKey(cfg.model, materials);
  const hit = CACHE.get(key);
  if (hit) {
    return c.json({
      ok: true,
      data: answer(symbol, materials, hit.text, hit.redlined, true, snap ?? undefined, kl?.kline_date ?? null),
    });
  }

  // ③' 频控：同一 symbol 在 **THROTTLE_MS** 内只允许一次**真实出网**。
  //    缓存只能拦「素材完全相同」的重复请求；素材一变（每轮涨幅/排名都变）缓存就失效，
  //    理论上可被无限点击刷爆付费调用（审查 A / P2-5）。
  //    这里用最便宜的节流：同 symbol 冷却期内直接拒（429），不排队、不预取。
  const last = LAST_CALL.get(symbol);
  const now = Date.now();
  if (last !== undefined && now - last < THROTTLE_MS) {
    throw new AppError("AI_RATE_LIMITED", `该标的刚解读过，请 ${Math.ceil((THROTTLE_MS - (now - last)) / 1000)} 秒后再试`, 429);
  }
  LAST_CALL.set(symbol, now);

  // ④ 转录 → ⑤ 红线扫描（fail-closed：命中即整段替换，不留半句建议）。
  let text: string;
  try {
    text = await transcribe(cfg, materials);
  } catch (e) {
    // ⚠ 失败必须**释放冷却**（交叉审查复核项 1-3）：`LAST_CALL.set` 在 await 之前执行，
    //   目的是用「同步临界区」挡住并发双打（同 isolate 内 TOCTOU 防护）。
    //   但若上游抖动/超时后不释放，用户会因一次 502 被锁死 30s 无法重试 —— 那不是限流，是可用性 bug。
    LAST_CALL.delete(symbol);
    if (e instanceof AIUpstreamError) throw new AppError("AI_UPSTREAM", e.message, 502);
    throw e;
  }
  const redlined = isRedlined(text);
  if (redlined) {
    // 日志只记「发生了一次拒答」，不记模型原文（避免红线内容进日志）。
    console.error("ai-explain: output hit redline, replaced by refusal", symbol);
    text = REFUSAL;
  }

  if (CACHE.size >= CACHE_MAX) {
    const oldest = CACHE.keys().next();
    if (!oldest.done) CACHE.delete(oldest.value);
  }
  CACHE.set(key, { text, redlined });

  return c.json({
    ok: true,
    data: answer(symbol, materials, text, redlined, false, snap ?? undefined, kl?.kline_date ?? null),
  });
}

function answer(
  symbol: string,
  m: ExplainMaterials,
  text: string,
  redlined: boolean,
  cached: boolean,
  snap: { date: string; time: string } | undefined,
  klineDate: string | null,
): ExplainResponse {
  return {
    symbol,
    label: AI_LABEL,
    text,
    redlined,
    cached,
    materials: m.items,
    missing: m.missing,
    generatedFrom: snap ? { date: snap.date, time: snap.time, klineDate } : null,
  };
}

/** JSON 列解析的降级版：坏行不抛错（解读不该因一行坏数据全灭）。 */
function safeArray<T>(raw: string): T[] {
  try {
    const v = JSON.parse(raw);
    return Array.isArray(v) ? (v as T[]) : [];
  } catch (e) {
    console.error("ai-explain: kline column parse failed", e);
    return [];
  }
}