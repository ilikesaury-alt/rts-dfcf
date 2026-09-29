/**
 * `GET /api/regions` —— 面板首屏唯一读端点（design §4.3）。
 *
 * 三条硬规则（复核修 2 / D4）：
 * 1. 取最新轮一律 `ORDER BY date DESC, time DESC` —— **不是** `seq`。
 *    `seq` 是本地进程内轮号，重启后重排，按它排序会读到旧轮。
 * 2. **1 次查询**（design §3.4 预算：每请求 ≤10）。
 * 3. **不返回 stocks**（列表页不碰 K 线，K 线走 `/api/stock/{symbol}/kline`）。
 */
import type { Env } from "../env";
import { AppError } from "../error";
import type { RegionsResponse } from "../serialize";

type Ctx<E> = {
  env: E;
  json: (b: unknown, s?: number) => Response;
};

export async function regions<E extends Env>(c: Ctx<E>): Promise<Response> {
  const row = await c.env.DB.prepare(
    `SELECT date, time, seq, payload, ingested_at
       FROM snapshots
      ORDER BY date DESC, time DESC
      LIMIT 1`,
  ).first<{ date: string; time: string; seq: number; payload: string; ingested_at: string }>();

  if (!row) throw new AppError("NO_SNAPSHOT", "尚无任何上报", 404);

  // payload 是本仓写入的，理论上永远合法；但 D1 行可能被手工改过/半写。
  // 裸 JSON.parse 抛 SyntaxError 会变成 500 且丢掉错误信封 —— 包起来给 500 信封。
  let parsed: { regions: RegionsResponse["regions"]; ctx: RegionsResponse["ctx"] };
  try {
    parsed = JSON.parse(row.payload);
  } catch (e) {
    // 带上原始异常便于排查（Workers Logs 里直接可见），但**不回给客户端**：
    // 响应体是用户可见的，不该泄露内部行内容。
    console.error("regions: payload parse failed", row.date, row.time, e);
    throw new AppError("CORRUPT_SNAPSHOT", `snapshot ${row.date} ${row.time} payload 损坏`, 500);
  }
  const { regions, ctx } = parsed;
  const body: RegionsResponse = {
    date: row.date,
    time: row.time,
    seq: row.seq,
    ingestedAt: row.ingested_at,
    regions,
    ctx,
  };
  return c.json({ ok: true, data: body });
}
