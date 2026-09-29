/**
 * `POST /api/ingest` —— 唯一的写端点（design §4.2）。
 *
 * 纪律：
 * - 鉴权走 `bearerGuard`（D2 第 2 层），**不在** Access 覆盖范围内。
 * - 契约校验用**白名单 + 显式规则**，不引 zod：8 个字段不值得为它把
 *   CPU 推到 10ms 硬墙边（D4）。
 * - 超配额**截断而非拒绝**：拒绝会让上报器一个配额 bug 丢掉整轮 regions。
 * - 幂等靠 `ON CONFLICT(date,time) DO UPDATE`（DR-4）。
 */
import { bearerGuard } from "../auth";
import type { Ctx } from "../env";
import { AppError } from "../error";
import { SCHEMA_VERSION } from "../serialize";
import type { Ctx as PayloadCtx, Regions, Stock } from "../serialize";

/** 校验通过后的上报体类型（`validate` 的断言目标）。 */
export type IngestBody = {
  schema: number;
  seq: number;
  date: string;
  time: string;
  durationMs: number;
  regions: Regions;
  ctx: PayloadCtx;
  stocks?: Record<string, Stock>;
};

/** FR-C2 规则 5：每轮最多 20 只票（≈160KB）。与本地 `STOCK_QUOTA` 同语义同数值。 */
export const STOCK_QUOTA = 20;
const SYMBOL_RE = /^\d{6}$/;
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
const TIME_RE = /^\d{2}:\d{2}:\d{2}$/;

const REQUIRED = ["date", "time", "seq", "durationMs", "regions", "ctx"] as const;

/** 载荷校验：只声明「必须有什么」，多余字段直接忽略（白名单丢弃，防塞脏数据）。 */
export function validate(p: unknown): asserts p is IngestBody {
  if (typeof p !== "object" || p === null) throw new AppError("BAD_REQUEST", "body must be an object", 400);
  const o = p as Record<string, unknown>;
  if (Number(o.schema) !== SCHEMA_VERSION) {
    throw new AppError("SCHEMA_MISMATCH", `expect ${SCHEMA_VERSION} got ${o.schema}`, 400);
  }
  const bad: string[] = [];
  for (const k of REQUIRED) if (o[k] === undefined) bad.push(k);
  if (bad.length) throw new AppError("BAD_REQUEST", `missing: ${bad.join(",")}`, 400);
  if (!DATE_RE.test(String(o.date))) throw new AppError("BAD_REQUEST", "bad date", 400);
  if (!TIME_RE.test(String(o.time))) throw new AppError("BAD_REQUEST", "bad time", 400);
  const regions = o.regions as { main?: unknown } | undefined;
  if (!Array.isArray(regions?.main)) throw new AppError("BAD_REQUEST", "regions.main must be an array", 400);
}

/** 取合法 symbol 的 stocks 项，**先取全量再截断**（复核修 6：截断而非拒绝）。 */
export function pickStocks(
  stocks: unknown,
  quota = STOCK_QUOTA,
): { accepted: [string, Stock][]; total: number } {
  const src = (stocks ?? {}) as Record<string, Stock>;
  const all = Object.entries(src).filter(([sym]) => SYMBOL_RE.test(sym)) as [string, Stock][];
  return { accepted: all.slice(0, quota), total: all.length };
}

export async function ingest(c: Ctx): Promise<Response> {
  const guard = bearerGuard(c);
  if (guard) return guard;

  let p: unknown;
  try {
    p = await c.req.json();
  } catch {
    throw new AppError("BAD_REQUEST", "body is not valid JSON", 400);
  }
  validate(p);

  const { accepted, total } = pickStocks(p.stocks);

  const stmts = [
    c.env.DB.prepare(
      `INSERT INTO snapshots(date,time,seq,duration_ms,schema,payload)
       VALUES(?,?,?,?,?,?)
       ON CONFLICT(date,time) DO UPDATE SET
         seq=excluded.seq, duration_ms=excluded.duration_ms,
         schema=excluded.schema, payload=excluded.payload,
         ingested_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')`,
    ).bind(
      p.date,
      p.time,
      p.seq,
      p.durationMs,
      p.schema,
      // 只存 regions+ctx：K 线走 kline_cache，独立端点读（D4 / FR-D1）
      JSON.stringify({ regions: p.regions, ctx: p.ctx }),
    ),
  ];
  for (const [sym, s] of accepted) {
    stmts.push(
      c.env.DB.prepare(
        `INSERT INTO kline_cache(symbol,kline_date,kline,appearances)
         VALUES(?,?,?,?)
         ON CONFLICT(symbol,kline_date) DO UPDATE SET
           kline=excluded.kline, appearances=excluded.appearances,
           updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')`,
      ).bind(sym, s?.klineDate ?? p.date, JSON.stringify(s?.kline ?? []), JSON.stringify(s?.appearances ?? [])),
    );
  }
  // batch 原子：要么全写要么全不写（design §3.4 A.1）
  await c.env.DB.batch(stmts);

  return c.json(
    {
      ok: true,
      data: {
        seq: p.seq,
        date: p.date,
        time: p.time,
        stocksStored: accepted.length,
        stocksDropped: total - accepted.length,
      },
    },
    201,
  );
}
