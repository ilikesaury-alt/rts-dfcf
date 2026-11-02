/**
 * `/api/ingest` + `/api/regions` + `/api/meta` 集成测试（T0.7 / T0.8 验收）。
 *
 * 跑在**真实 workerd + 真实 D1** 上（vitest.config.ts）—— `ON CONFLICT` 幂等性与
 * `ORDER BY date DESC, time DESC` 的取轮语义只有真 SQLite 验得住。
 */
import { env, applyD1Migrations, createExecutionContext, waitOnExecutionContext } from "cloudflare:test";
import { beforeAll, beforeEach, describe, expect, it } from "vitest";
import worker from "../src/index";
import { STOCK_QUOTA, pickStocks } from "../src/routes/ingest";
import { SCHEMA_VERSION } from "../src/serialize";
import schemaSql from "../migrations/0001_init.sql?raw";
import { TEST_SECRET } from "./secret";

const AUTH = { Authorization: `Bearer ${TEST_SECRET}`, "Content-Type": "application/json" };

type Body = Record<string, unknown>;

function payload(over: Body = {}): Body {
  return {
    schema: SCHEMA_VERSION,
    seq: 1,
    date: "2026-09-29",
    time: "10:31:00",
    durationMs: 1234,
    regions: { main: [{ symbol: "SZ300319", name: "麦捷科技" }], hist: [], hot: [], offboard: [] },
    ctx: { marketIdxPct: 1.2, weak: false, flowFiltered: 3, warnings: [] },
    stocks: {},
    ...over,
  };
}

async function call(path: string, init: RequestInit = {}) {
  const ctx = createExecutionContext();
  const res = await worker.fetch(new Request(`https://panel.test${path}`, init), env, ctx);
  await waitOnExecutionContext(ctx);
  return res;
}

const post = (body: unknown, headers: Record<string, string> = AUTH) =>
  call("/api/ingest", { method: "POST", headers, body: JSON.stringify(body) });

async function snapshotRows(): Promise<{ count: number; seq: number }> {
  const r = await env.DB.prepare("SELECT count(*) AS count, max(seq) AS seq FROM snapshots").first<{
    count: number;
    seq: number;
  }>();
  return { count: Number(r?.count ?? 0), seq: Number(r?.seq ?? 0) };
}

beforeAll(async () => {
  // 本版本 pool 没有 config 级 migrations 钩子，得在测试里把 SQL 读进来再应用。
  // 用 Vite 的 `?raw` 读**真实**迁移文件（而不是把建表语句抄一份进测试）——
  // 抄一份就多一处会漂移的副本，而本仓库的多处断言正是在防这类漂移。
  const queries = schemaSql
    .split(";")
    .map((q) => q.trim())
    .filter(Boolean);
  expect(queries.length).toBeGreaterThan(0);
  await applyD1Migrations(env.DB, [{ name: "0001_init", queries }]);
});

beforeEach(async () => {
  await env.DB.exec("DELETE FROM snapshots; DELETE FROM kline_cache;");
});

describe("POST /api/ingest", () => {
  it("同 date+time 重放幂等：行数仍为 1，seq 取新值", async () => {
    const a = await post(payload({ seq: 1 }));
    expect(a.status).toBe(201);
    const b = await post(payload({ seq: 99, durationMs: 4321 }));
    expect(b.status).toBe(201);

    const rows = await snapshotRows();
    expect(rows.count).toBe(1);
    expect(rows.seq).toBe(99); // seq 仅展示，但覆盖要生效（幂等覆盖列）
  });

  it("错密钥 → 401 UNAUTHORIZED", async () => {
    const res = await post(payload(), { Authorization: "Bearer wrong", "Content-Type": "application/json" });
    expect(res.status).toBe(401);
    expect((await res.json<any>()).error.code).toBe("UNAUTHORIZED");
    expect((await snapshotRows()).count).toBe(0); // 数据未被污染
  });

  it("无 Authorization 头 → 401", async () => {
    const res = await call("/api/ingest", { method: "POST", body: JSON.stringify(payload()) });
    expect(res.status).toBe(401);
    expect((await res.json<any>()).error.code).toBe("UNAUTHORIZED");
  });

  it("schema=1 → 400 SCHEMA_MISMATCH", async () => {
    const res = await post(payload({ schema: 1 }));
    expect(res.status).toBe(400);
    expect((await res.json<any>()).error.code).toBe("SCHEMA_MISMATCH");
    expect((await snapshotRows()).count).toBe(0);
  });

  it("25 票 → 201 且 stocksStored=20 / stocksDropped=5，**整轮不丢**", async () => {
    const stocks: Record<string, unknown> = {};
    for (let i = 0; i < 25; i++) {
      stocks[String(300000 + i)] = { klineDate: "2026-09-29", kline: [[1, 2, 3, 4, 5, 6]], appearances: [] };
    }
    const res = await post(payload({ stocks }));
    expect(res.status).toBe(201);
    const body = await res.json<any>();
    expect(body.data.stocksStored).toBe(STOCK_QUOTA);
    expect(body.data.stocksStored).toBe(20);
    expect(body.data.stocksDropped).toBe(5);
    // 关键：超配额只截 stocks，regions 仍完整入库
    expect((await snapshotRows()).count).toBe(1);
    const n = await env.DB.prepare("SELECT count(*) AS c FROM kline_cache").first<{ c: number }>();
    expect(Number(n?.c)).toBe(20);
  });

  it("脏 symbol 键被白名单丢弃，不写库", async () => {
    // ⚠ 合法键刻意用 **7 位**（“3011711”）：自 2026-11-02 放宽 SYMBOL_RE 后，
    //   “300001” 已是**合法**键，若继续用它，这个用例就变成「验证合法键被收」——
    //   测试名与断言内容矛盾，未来正则再收紧/放宽它都拦不住（审查 A / P2-4）。
    const res = await post(
      payload({ stocks: { "not-a-symbol": { kline: [] }, "3011711": { kline: [] } } }),
    );
    const body = await res.json<any>();
    expect(body.data.stocksStored).toBe(0); // 脏键不进计数
    const n = await env.DB.prepare("SELECT count(*) AS c FROM kline_cache").first<{ c: number }>();
    expect(Number(n?.c)).toBe(0);
  });

  // ── 2026-11-02 缺陷守卫：真实 symbol 带交易所前缀 ─────────────────────────
  // 此前 `SYMBOL_RE = /^\d{6}$/` 只收 6 位纯数字，而实测 scanner.db 的真实键是
  // `SZ301171` / `SH600036` ⇒ 生产环境**全部 stocks 被当脏键丢弃**、kline_cache 恒空。
  // 既有用例一律用 `300000+i` 这类 6 位键，与生产格式不符 ⇒ 缺陷长期假绿（AGENTS.md 同型）。
  it("真实格式 symbol（SZ301171 / SH600036）被接受并写库", async () => {
    const stocks = {
      SZ301171: { klineDate: "2026-09-29", kline: [["2026-09-29", 1, 2, 0.5, 3, 100]], appearances: [] },
      SH600036: { klineDate: "2026-09-29", kline: [["2026-09-29", 2, 3, 1, 4, 200]], appearances: [] },
      300001: { klineDate: "2026-09-29", kline: [["2026-09-29", 3, 4, 2, 5, 300]], appearances: [] },
    };
    const res = await post(payload({ stocks }));
    expect(res.status).toBe(201);
    const body = await res.json<any>();
    expect(body.data.stocksStored).toBe(3);
    expect(body.data.stocksDropped).toBe(0);
    const n = await env.DB.prepare("SELECT count(*) AS c FROM kline_cache").first<{ c: number }>();
    expect(Number(n?.c)).toBe(3);
    // 键**原样**存（不归一化）—— web 详情链接与 regions 行都用带前缀 symbol。
    const one = await env.DB.prepare("SELECT symbol FROM kline_cache WHERE symbol = 'SZ301171'").first();
    expect(one).not.toBeNull();
  });

  it("pickStocks 边界：前缀大小写/长度/非 A 股前缀/前缀-代码段错配都不放行", async () => {
    const ok = ["SZ301171", "SH600036", "BJ430047", "300001"];
    const bad = [
      "sz301171",
      "3011711",
      "30031",
      "SZ30117",
      "HK00700",
      "SZ301171x",
      "",
      "SZ301171 ",
      // 2026-11-02 追加：形状合法但前缀与代码段矛盾（交叉审查复核项 3）
      "SH300001", // 上交所前缀 + 创业板代码
      "SZ600036", // 深交所前缀 + 沪市主板代码
      "BJ300001", // 北交所前缀 + 创业板代码
    ];
    for (const s of ok) expect(pickStocks({ [s]: {} }).accepted.map(([k]) => k)).toEqual([s]);
    for (const s of bad) expect(pickStocks({ [s]: {} }).accepted).toEqual([]);
  });

  it("缺字段 → 400 且列出全部缺失项", async () => {
    const p = payload();
    delete (p as any).durationMs;
    delete (p as any).ctx;
    const res = await post(p);
    expect(res.status).toBe(400);
    const body = await res.json<any>();
    expect(body.error.code).toBe("BAD_REQUEST");
    expect(body.error.message).toContain("durationMs");
    expect(body.error.message).toContain("ctx");
  });

  it("date/time 格式非法 → 400", async () => {
    expect((await post(payload({ date: "2026/09/29" }))).status).toBe(400);
    expect((await post(payload({ time: "10:31" }))).status).toBe(400);
    const res = await post(payload({ date: "2026/09/29" }));
    expect((await res.json<any>()).error.code).toBe("BAD_REQUEST");
  });

  it("body 不是 JSON → 400", async () => {
    const res = await call("/api/ingest", { method: "POST", headers: AUTH, body: "<<not json>>" });
    expect(res.status).toBe(400);
  });
});

describe("GET /api/regions", () => {
  it("按 (date,time) 取最新轮，**不按 seq**", async () => {
    // 复核修 2 的核心回归：seq 更小但 date/time 更新的行**必须**被读到。
    await post(payload({ seq: 500, date: "2026-09-28", time: "14:59:00" }));
    await post(payload({ seq: 1, date: "2026-09-29", time: "10:31:00" }));

    const res = await call("/api/regions");
    const body = await res.json<any>();
    expect(body.ok).toBe(true);
    expect(body.data.date).toBe("2026-09-29");
    expect(body.data.time).toBe("10:31:00");
    expect(body.data.seq).toBe(1); // 展示用的 seq 照实返回，但**不参与**取轮
  });

  it("响应不含 stocks（D4：列表页不碰 K 线）", async () => {
    await post(payload({ stocks: { "300001": { kline: [[1, 2, 3, 4, 5, 6]], appearances: [] } } }));
    const body = await (await call("/api/regions")).json<any>();
    expect(body.data.stocks).toBeUndefined();
    expect(JSON.stringify(body)).not.toContain("kline");
  });

  it("无快照 → 404 NO_SNAPSHOT", async () => {
    const res = await call("/api/regions");
    expect(res.status).toBe(404);
    expect((await res.json<any>()).error.code).toBe("NO_SNAPSHOT");
  });

  it("payload 损坏 → 500 CORRUPT_SNAPSHOT（不是裸 SyntaxError）", async () => {
    await env.DB.exec(
      "INSERT INTO snapshots(date,time,seq,duration_ms,schema,payload) VALUES('2026-09-29','09:30:00',1,0,2,'<<broken>>')",
    );
    const res = await call("/api/regions");
    expect(res.status).toBe(500);
    expect((await res.json<any>()).error.code).toBe("CORRUPT_SNAPSHOT");
  });
});

describe("GET /api/meta", () => {
  it("有快照 → ready=true 且字段齐全（状态条要显示入库时间）", async () => {
    await post(payload());
    const body = await (await call("/api/meta")).json<any>();
    expect(body.data.ready).toBe(true);
    expect(body.data.date).toBe("2026-09-29");
    expect(body.data.time).toBe("10:31:00");
    expect(typeof body.data.seq).toBe("number");
    expect(typeof body.data.ingested_at).toBe("string");
  });

  it("无快照 → ready=false **且 200**（面板首开不能因此报红）", async () => {
    const res = await call("/api/meta");
    expect(res.status).toBe(200);
    const body = await res.json<any>();
    expect(body.data.ready).toBe(false);
    expect(body.data.date).toBeNull();
  });
});

describe("契约", () => {
  it("SCHEMA_VERSION = 2（与 scanner/panel_serialize.py 同值）", () => {
    expect(SCHEMA_VERSION).toBe(2);
  });

  it("regions 把 cols 原样下发（前端据此渲染表头，不硬编码列名）", async () => {
    await post(
      payload({
        regions: {
          main: [],
          hist: [],
          hot: [],
          offboard: [],
          cols: { pool: [{ key: "代码", label: "代码", align: "l", width: 12 }] },
        },
      }),
    );
    const body = await (await call("/api/regions")).json<any>();
    expect(body.data.regions.cols.pool[0].key).toBe("代码");
  });
});

describe("路由面", () => {
  it("未定义的端点 → 404 NOT_FOUND（不静默 200）", async () => {
    const res = await call("/api/unknown");
    expect(res.status).toBe(404);
    expect((await res.json<any>()).error.code).toBe("NOT_FOUND");
  });

  it("GET /api/ingest 不是写入口（方法不符 → 404，不进 handler）", async () => {
    // WAF 之前的一层：写接口只认 POST。
    expect((await call("/api/ingest")).status).toBe(404);
  });
});
