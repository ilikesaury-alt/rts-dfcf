/**
 * `GET /api/stock/{symbol}/kline`（E1）—— 补齐 `web/src/pages/Stock.tsx` 已依赖、
 * 但 worker 一直**未实现**的读端点（design §4.4 / T4.1）。
 *
 * 契约来源不是本文件自拟，而是已存在的前端类型 `web/src/api/types.ts::KlineResponse`
 * 与 `API.stockKline()`；错误码 `NOT_IN_REPORT` 亦来自前端的 `ApiErrorCode`。
 *
 * 跑真实 workerd + 真实 D1；数据经 `POST /api/ingest` 真实写入（顺手覆盖
 * 「ingest 写入 → 读端点能读到」这条端到端链路，而不是直接往表里塞行）。
 */
import { env, applyD1Migrations, createExecutionContext, waitOnExecutionContext } from "cloudflare:test";
import { beforeAll, beforeEach, describe, expect, it } from "vitest";
import worker from "../src/index";
import { SCHEMA_VERSION } from "../src/serialize";
import schemaSql from "../migrations/0001_init.sql?raw";
import { TEST_SECRET } from "./secret";

const AUTH = { Authorization: `Bearer ${TEST_SECRET}`, "Content-Type": "application/json" };

async function call(path: string, init: RequestInit = {}) {
  const ctx = createExecutionContext();
  const res = await worker.fetch(new Request(`https://panel.test${path}`, init), env, ctx);
  await waitOnExecutionContext(ctx);
  return res;
}

/** 一轮上报：`main` 行 + `stocks` 由调用方给。 */
async function ingestRound(opts: {
  date?: string;
  time?: string;
  main?: Record<string, unknown>[];
  stocks?: Record<string, unknown>;
}) {
  const body = {
    schema: SCHEMA_VERSION,
    seq: 1,
    date: opts.date ?? "2026-11-02",
    time: opts.time ?? "10:31:00",
    durationMs: 100,
    regions: { main: opts.main ?? [], hist: [], hot: [], offboard: [] },
    ctx: { marketIdxPct: 1.2, weak: false, flowFiltered: 0, warnings: [] },
    stocks: opts.stocks ?? {},
  };
  const res = await call("/api/ingest", {
    method: "POST",
    headers: AUTH,
    body: JSON.stringify(body),
  });
  expect(res.status).toBe(201);
  return res;
}

const klineOf = (sym: string, close: number) => [
  ["2026-10-31", 10, close, 9.5, close + 1, 1000],
  ["2026-11-02", close, close + 0.2, close - 0.1, close + 0.3, 1200],
];

const stockOf = (sym: string, close = 12) => ({
  klineDate: "2026-11-02",
  kline: klineOf(sym, close),
  appearances: [{ date: "2026-11-01", time: "10:00:00", category: "momentum", score: 55, percent: 6.2 }],
});

beforeAll(async () => {
  const queries = schemaSql.split(";").map((q) => q.trim()).filter(Boolean);
  await applyD1Migrations(env.DB, [{ name: "0001_init", queries }]);
});

beforeEach(async () => {
  await env.DB.exec("DELETE FROM snapshots; DELETE FROM kline_cache;");
});

describe("GET /api/stock/{symbol}/kline", () => {
  it("命中：返回 klineDate/kline/appearances，scoreBreakdown 取自同轮 regions 行", async () => {
    await ingestRound({
      main: [
        {
          symbol: "SZ301171",
          name: "中科江南",
          score: 61,
          cat_label: "动量",
          score_breakdown: { momentum: 30, flow: 20, structure: 11 },
        },
      ],
      stocks: { SZ301171: stockOf("SZ301171") },
    });

    const res = await call("/api/stock/SZ301171/kline");
    expect(res.status).toBe(200);
    const body = await res.json<any>();
    expect(body.ok).toBe(true);
    const d = body.data;
    expect(d.symbol).toBe("SZ301171");
    expect(d.klineDate).toBe("2026-11-02");
    // 元组顺序契约：[date, open, close, low, high, volume]（close 在 low/high 之前）
    expect(d.kline[0]).toHaveLength(6);
    expect(d.kline[1][2]).toBe(12.2);
    expect(d.appearances[0].category).toBe("momentum");
    expect(d.scoreBreakdown).toEqual({ momentum: 30, flow: 20, structure: 11 });
  });

  it("不在 kline_cache → 404 NOT_IN_REPORT（不是 500，也不是空 200）", async () => {
    await ingestRound({ main: [{ symbol: "SZ300319", name: "麦捷科技" }] });
    const res = await call("/api/stock/SZ300319/kline");
    expect(res.status).toBe(404);
    const body = await res.json<any>();
    expect(body.ok).toBe(false);
    expect(body.error.code).toBe("NOT_IN_REPORT");
  });

  it("同一 symbol 多日 K 线 → 取 kline_date 最新一根", async () => {
    await ingestRound({
      date: "2026-11-01",
      stocks: { SZ301171: { ...stockOf("SZ301171", 10), klineDate: "2026-11-01" } },
    });
    await ingestRound({
      date: "2026-11-02",
      stocks: { SZ301171: { ...stockOf("SZ301171", 20), klineDate: "2026-11-02" } },
    });

    const body = await (await call("/api/stock/SZ301171/kline")).json<any>();
    expect(body.data.klineDate).toBe("2026-11-02");
    expect(body.data.kline[1][2]).toBe(20.2);
  });

  it("非法 symbol → 400 BAD_REQUEST（不查库）", async () => {
    for (const bad of ["abc", "SZ30117", "hk00700", "%20"]) {
      const res = await call(`/api/stock/${bad}/kline`);
      expect(res.status).toBe(400);
      expect((await res.json<any>()).error.code).toBe("BAD_REQUEST");
    }
  });

  it("该票不在本轮 regions → scoreBreakdown 为 null（非 v1 票，不猜）", async () => {
    await ingestRound({
      main: [{ symbol: "SZ300319", name: "麦捷科技" }],
      stocks: { SZ301171: stockOf("SZ301171") },
    });
    const body = await (await call("/api/stock/SZ301171/kline")).json<any>();
    expect(body.data.scoreBreakdown).toBeNull();
  });

  it("hot/offboard 区行用 code 字段 → 也能取到评分分解", async () => {
    const body = {
      schema: SCHEMA_VERSION,
      seq: 1,
      date: "2026-11-02",
      time: "10:31:00",
      durationMs: 100,
      regions: {
        main: [],
        hist: [],
        hot: [{ code: "SZ301171", name: "中科江南", tier: "T1", score_breakdown: { tier: 1 } }],
        offboard: [],
      },
      ctx: { marketIdxPct: null, weak: null, flowFiltered: 0, warnings: [] },
      stocks: { SZ301171: stockOf("SZ301171") },
    };
    await call("/api/ingest", { method: "POST", headers: AUTH, body: JSON.stringify(body) });

    const res = await (await call("/api/stock/SZ301171/kline")).json<any>();
    expect(res.data.scoreBreakdown).toEqual({ tier: 1 });
  });
});