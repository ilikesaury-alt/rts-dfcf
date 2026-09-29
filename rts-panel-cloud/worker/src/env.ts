/** 绑定的唯一真源（design §4.1）。改 wrangler 绑定必须同步这里。 */
import type { Context } from "hono";

export type Env = {
  /** D1 数据库绑定 */
  DB: D1Database;
  /** 上报密钥（`wrangler secret put INGEST_SECRET`，**绝不能**放 vars） */
  INGEST_SECRET: string;
  /** CORS 白名单，逗号分隔。生产同域部署后留空，仅本地 dev 用。 */
  WEB_ORIGINS: string;
};

/** 各路由处理器共用的上下文类型（避免在每个路由里重写一遍 & 到处 `as any`）。 */
export type Ctx = Context<{ Bindings: Env }>;

/** 解析后的 CORS 白名单（空串 → 同域，不加任何 CORS 头）。 */
export function webOrigins(env: Env): string[] {
  return (env.WEB_ORIGINS || "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}
