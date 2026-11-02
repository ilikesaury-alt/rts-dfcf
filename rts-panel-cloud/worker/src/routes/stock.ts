/**
 * `GET /api/stock/{symbol}/kline` —— 个股详情（design §4.4 / T4.1）。
 *
 * 这个端点是 `web/src/pages/Stock.tsx` **早已依赖**的（`API.stockKline()`），但 worker
 * 此前只实现了 ingest/meta/regions 三个路由 ⇒ 云端详情页点开即 404。本次补齐（E1）。
 *
 * 两条纪律：
 * 1. **只读**：D1 两查（kline_cache 1 次 + 最新快照 1 次），零写入，不进 Access 之外的鉴权分支
 *    （读端点走 Cloudflare Access + `workers_dev:false`，与 regions/meta 同一层）。
 * 2. **评分分解只回显、不重算**：取自最新快照 regions 行里的 `score_breakdown` 原值；
 *    不在快照里就回 `null`（前端显「无评分分解」），**不猜、不补**（G5 零第二结论源）。
 *
 * ⚠ 取最新 K 线一律 `ORDER BY kline_date DESC`（**不是** `updated_at`）：
 *   `updated_at` 是写入时刻，同一交易日重传会把旧一根刷成"更新"，但日期没前进。
 */
import type { Env } from "../env";
import { AppError } from "../error";
import { asBreakdown, findRegionRow } from "../region-row";
import type { Regions, Row } from "../serialize";
import { isSymbol } from "../symbol";

type Ctx<E> = {
  env: E;
  json: (b: unknown, s?: number) => Response;
  req: { param: (k: string) => string | undefined };
};

/** 契约与 `web/src/api/types.ts::KlineResponse` 逐字段对齐（改这里必须同改前端）。 */
export type KlineResponse = {
  symbol: string;
  klineDate: string;
  kline: number[][];
  appearances: Row[];
  /** 该票在本轮的评分分解；非 v1 票没有 → `null`（详情页显「无评分分解」）。 */
  scoreBreakdown: Record<string, number> | null;
};

function parseJsonArray<T>(raw: string, what: string): T[] {
  try {
    const v = JSON.parse(raw);
    return Array.isArray(v) ? (v as T[]) : [];
  } catch (e) {
    // 行是本仓写的，理论上永远合法；但 D1 行可能被手工改过/半写。
    // 带上原始异常进 Workers Logs，响应体只给通用文案（不外泄内部行内容）。
    console.error(`stock: ${what} parse failed`, e);
    throw new AppError("CORRUPT_SNAPSHOT", `${what} 数据损坏`, 500);
  }
}

export async function stockKline<E extends Env>(c: Ctx<E>): Promise<Response> {
  const symbol = c.req.param("symbol") ?? "";
  if (!isSymbol(symbol)) {
    throw new AppError("BAD_REQUEST", "代码格式非法：应为 6 位数字或带 SH/SZ/BJ 前缀", 400);
  }

  const row = await c.env.DB.prepare(
    `SELECT symbol, kline_date, kline, appearances
       FROM kline_cache
      WHERE symbol = ?
      ORDER BY kline_date DESC
      LIMIT 1`,
  )
    .bind(symbol)
    .first<{ symbol: string; kline_date: string; kline: string; appearances: string }>();

  // 不在上报集里不是系统错误（`collect_stocks` 无 K 线时有意跳过）——前端按码显"暂无数据"。
  if (!row) {
    throw new AppError("NOT_IN_REPORT", `该股票（${symbol}）暂无可用的 K 线数据`, 404);
  }

  const body: KlineResponse = {
    symbol: row.symbol,
    klineDate: row.kline_date,
    kline: parseJsonArray<number[]>(row.kline, "kline"),
    appearances: parseJsonArray<Row>(row.appearances, "appearances"),
    scoreBreakdown: null,
  };

  // 评分分解来自**最新快照**的 regions 行（1 次查询）。解析失败/无快照都不该让详情页 500：
  // 分解只是附加展示，降级为 null 即可（与「不在快照里」同一表现）。
  const snap = await c.env.DB.prepare(
    `SELECT payload FROM snapshots ORDER BY date DESC, time DESC LIMIT 1`,
  ).first<{ payload: string }>();
  if (snap) {
    try {
      const parsed = JSON.parse(snap.payload) as { regions?: Regions };
      body.scoreBreakdown = asBreakdown(findRegionRow(parsed.regions, symbol)?.row.score_breakdown);
    } catch (e) {
      console.error("stock: snapshot payload parse failed", e);
    }
  }

  return c.json({ ok: true, data: body });
}