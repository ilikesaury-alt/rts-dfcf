/**
 * Worker 入口（design §2.3 / §4.1）。
 *
 * 路由清单（**无任何写端点除 /api/ingest**，NFR 只读）：
 *   POST /api/ingest              上报（Bearer 密钥，**不在** Access 覆盖内）
 *   GET  /api/regions             首屏五区块（Access 之后，1 查询，不含 stocks）
 *   GET  /api/meta                状态条（1 查询）
 *   GET  /api/health              存活探针（不碰 D1）
 */
import { Hono } from "hono";
import type { Env } from "./env";
import { webOrigins } from "./env";
import { AppError, err } from "./error";
import { ingest } from "./routes/ingest";
import { meta } from "./routes/meta";
import { regions } from "./routes/regions";

const app = new Hono<{ Bindings: Env }>();

app.onError((e, c) => {
  if (e instanceof AppError) return c.json(err(e.code, e.message), e.status as 400);
  // 非 AppError = 编程错误/未预期异常：日志留全量，响应体不带内部细节。
  console.error("unhandled", e);
  return c.json(err("INTERNAL", "服务内部错误"), 500);
});

// 同域部署后生产 WEB_ORIGINS 留空 → 本中间件不注入任何 CORS 头。
// 保留它只为本地 dev（vite 5173 → worker 8787）跨域。
app.use("*", async (c, next) => {
  const origin = c.req.header("Origin");
  const allowed = origin && webOrigins(c.env).includes(origin);
  if (allowed) {
    c.header("Access-Control-Allow-Origin", origin);
    c.header("Vary", "Origin");
    c.header("Access-Control-Allow-Headers", "Authorization,Content-Type");
    c.header("Access-Control-Allow-Methods", "GET,POST,OPTIONS");
  }
  if (c.req.method === "OPTIONS") return new Response(null, { status: 204, headers: c.res.headers });
  await next();
  return c.res;
});

app.get("/api/health", (c) => c.json({ ok: true, data: { alive: true } }));

app.post("/api/ingest", (c) => ingest(c as any));
app.get("/api/regions", (c) => regions(c as any));
app.get("/api/meta", (c) => meta(c as any));

app.notFound((c) => c.json(err("NOT_FOUND", "无此端点"), 404));

export default app;
