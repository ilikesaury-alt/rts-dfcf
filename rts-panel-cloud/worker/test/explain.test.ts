/**
 * `GET /api/ai-explain?symbol=`（E2）—— AI 解读端点。
 *
 * 红线纪律（ADR 0001）在这些用例里逐条落地：
 * - 请求面只有 `symbol`，**没有自由文本** ⇒ 不存在「越界提问」这条攻击面；
 * - 素材由服务端确定性组装（`explain/material.ts` 纯函数）⇒ 同输入字节级同输出；
 * - 输出过**确定性词表**扫描 ⇒ 命中建议类动词整段替换为拒答；
 * - 未配置 / 票不在快照 / symbol 非法 ⇒ **零模型调用**（用 `pendingInterceptors` 证明）。
 *
 * 跑真实 workerd + 真实 D1；外呼用 `fetchMock` 拦截（`disableNetConnect` 保证不会真出网）。
 */
import { env, applyD1Migrations, createExecutionContext, fetchMock, waitOnExecutionContext } from "cloudflare:test";
import { afterEach, beforeAll, beforeEach, describe, expect, it } from "vitest";
import worker from "../src/index";
import type { Env } from "../src/env";
import { AI_LABEL, REFUSAL } from "../src/routes/explain";
import { buildMaterials } from "../src/explain/material";
import { SCHEMA_VERSION } from "../src/serialize";
import schemaSql from "../migrations/0001_init.sql?raw";
import { TEST_SECRET } from "./secret";

const AUTH = { Authorization: `Bearer ${TEST_SECRET}`, "Content-Type": "application/json" };
const ORIGIN = "https://magpie.test";
/** magpie 的 baseUrl 自带 `/v1`（与 fde 作品集的接入口径一致），代码再拼 `/chat/completions`。 */
const BASE_URL = `${ORIGIN}/v1`;
const CHAT_PATH = "/v1/chat/completions";

/** `env` 是 worker 的绑定集合；解读需要的三个变量按用例注入（生产走 wrangler secret）。 */
function envWith(over: Partial<Env> = {}): Env {
  return { DB: env.DB, INGEST_SECRET: TEST_SECRET, WEB_ORIGINS: "", ...over } as Env;
}

const MAGPIE = {
  MAGPIE_BASE_URL: BASE_URL,
  MAGPIE_API_KEY: "k-test",
  MAGPIE_MODEL: "test-model",
};

async function call(path: string, e: Env = envWith(), init: RequestInit = {}) {
  const ctx = createExecutionContext();
  const res = await worker.fetch(new Request(`https://panel.test${path}`, init), e, ctx);
  await waitOnExecutionContext(ctx);
  return res;
}

async function ingestRound(opts: {
  date?: string;
  time?: string;
  main?: Record<string, unknown>[];
  hot?: Record<string, unknown>[];
  stocks?: Record<string, unknown>;
}) {
  const body = {
    schema: SCHEMA_VERSION,
    seq: 1,
    date: opts.date ?? "2026-11-02",
    time: opts.time ?? "10:31:00",
    durationMs: 100,
    regions: { main: opts.main ?? [], hist: [], hot: opts.hot ?? [], offboard: [] },
    ctx: { marketIdxPct: 1.2, weak: false, flowFiltered: 0, warnings: [] },
    stocks: opts.stocks ?? {},
  };
  const res = await call("/api/ingest", envWith(), {
    method: "POST",
    headers: AUTH,
    body: JSON.stringify(body),
  });
  expect(res.status).toBe(201);
}

const stockOf = (close = 12) => ({
  klineDate: "2026-11-02",
  kline: [
    ["2026-10-31", 10, 11, 9.5, 11.5, 1000],
    ["2026-11-02", 11, close, 10.8, close + 0.4, 1200],
  ],
  appearances: [{ date: "2026-11-01", time: "10:00:00", category: "momentum", score: 55, percent: 6.2 }],
});

const MAIN_ROW = (symbol: string) => ({
  symbol,
  name: "中科江南",
  category: "momentum",
  cat_label: "动量",
  pct: 3.21,
  accum: 8.4,
  current: 12.3,
  rank: 17,
  sector: "软件",
  score: 61,
  is_new_entry: true,
  score_breakdown: { momentum: 30, flow: 20, structure: 11 },
});

/** 一个正常（无建议动词）的模型回答。 */
const OK_TEXT = "该票本轮进入动量类池选，评分 61，排名第 17，板块软件。出现史 1 条。";

beforeAll(async () => {
  const queries = schemaSql.split(";").map((q) => q.trim()).filter(Boolean);
  await applyD1Migrations(env.DB, [{ name: "0001_init", queries }]);
  fetchMock.activate();
  fetchMock.disableNetConnect();
});

beforeEach(async () => {
  await env.DB.exec("DELETE FROM snapshots; DELETE FROM kline_cache;");
});

afterEach(() => {
  // 每个用例自身负责消费/断言它注册的拦截器；残留即泄漏（会污染后续用例）。
  fetchMock.assertNoPendingInterceptors();
});

describe("GET /api/ai-explain", () => {
  // 零调用证明方式：**不注册任何拦截器**。`fetchMock.disableNetConnect()` 下，一旦真的
  // 发起外呼就抛错 → `llm.ts` 会表面成 502 AI_UPSTREAM；因此这些用例断言 503/400/404
  // 就是在同时断言「没出网」（出了就会变成 502）。
  it("未配置模型服务 → 503 AI_UNAVAILABLE，零模型调用", async () => {
    await ingestRound({ main: [MAIN_ROW("SZ301171")] });

    const res = await call("/api/ai-explain?symbol=SZ301171", envWith());
    expect(res.status).toBe(503);
    const body = await res.json<any>();
    expect(body.error.code).toBe("AI_UNAVAILABLE");
    expect(body.error.message).not.toMatch(/stack|Error:|at /);
  });

  it("symbol 缺失或非法 → 400 BAD_REQUEST，零模型调用", async () => {
    for (const q of ["", "?symbol=", "?symbol=abc", "?symbol=SZ30117"]) {
      const res = await call(`/api/ai-explain${q}`, envWith(MAGPIE));
      expect(res.status).toBe(400);
      expect((await res.json<any>()).error.code).toBe("BAD_REQUEST");
    }
  });

  it("票不在任何区块也不在 K 线缓存 → 404 NOT_IN_REPORT，零模型调用", async () => {
    await ingestRound({ main: [MAIN_ROW("SZ300319")] });
    const res = await call("/api/ai-explain?symbol=SZ301999", envWith(MAGPIE));
    expect(res.status).toBe(404);
    expect((await res.json<any>()).error.code).toBe("NOT_IN_REPORT");
  });

  it("正常路径 → 200：label 恒在、text 原样、素材非空", async () => {
    fetchMock
      .get(ORIGIN)
      .intercept({ path: CHAT_PATH, method: "POST" })
      .reply(200, { choices: [{ message: { role: "assistant", content: OK_TEXT } }] });
    await ingestRound({ main: [MAIN_ROW("SZ301171")], stocks: { SZ301171: stockOf() } });

    const res = await call("/api/ai-explain?symbol=SZ301171", envWith(MAGPIE));
    expect(res.status).toBe(200);
    const d = (await res.json<any>()).data;
    expect(d.label).toBe(AI_LABEL);
    expect(d.label).toBe("仅解读，非投资建议");
    expect(d.text).toBe(OK_TEXT);
    expect(d.redlined).toBe(false);
    expect(d.cached).toBe(false);
    expect(Array.isArray(d.materials)).toBe(true);
    // 素材必须含：区块行字段、出现史、K 线、类别先验（手工文案）
    const ids = d.materials.map((m: any) => m.id);
    expect(ids.some((i: string) => i.startsWith("row.main."))).toBe(true);
    expect(ids.some((i: string) => i.startsWith("appearance."))).toBe(true);
    expect(ids.some((i: string) => i.startsWith("kline."))).toBe(true);
    expect(ids.some((i: string) => i.startsWith("prior.momentum"))).toBe(true);
    // 素材值必须与快照一致（不是模型编的）—— 验收 7：多点抽验，覆盖数值/文本/布尔格式化
    const score = d.materials.find((m: any) => m.id === "row.main.score");
    expect(score.value).toBe("61.0");
    const pct = d.materials.find((m: any) => m.id === "row.main.pct");
    expect(pct.value).toBe("+3.21%");
    const rank = d.materials.find((m: any) => m.id === "row.main.rank");
    expect(rank.value).toBe("17");
    const isNew = d.materials.find((m: any) => m.id === "row.main.is_new_entry");
    expect(isNew.value).toBe("是");
    const breakdown = d.materials.find((m: any) => m.id === "row.main.score_breakdown");
    expect(breakdown.value).toBe("flow=20.0, momentum=30.0, structure=11.0"); // 键排序，确定性
    const app0 = d.materials.find((m: any) => m.id === "appearance.0");
    expect(app0.value).toBe("2026-11-01 10:00:00 momentum score=55.0 pct=+6.20%");
    const klineLatest = d.materials.find((m: any) => m.id === "kline.latest");
    expect(klineLatest.value).toBe("2026-11-02 开11.00 收12.00 低10.80 高12.40 量1200");
    expect(d.generatedFrom.date).toBe("2026-11-02");
    expect(d.generatedFrom.klineDate).toBe("2026-11-02"); // 双侧轮次披露（审查 A / P2-2）
  });

  it("验收 8 零写入：解读调用后 snapshots / kline_cache 行数不变", async () => {
    fetchMock
      .get(ORIGIN)
      .intercept({ path: CHAT_PATH, method: "POST" })
      .reply(200, { choices: [{ message: { content: OK_TEXT } }] });
    await ingestRound({ main: [MAIN_ROW("SZ301178")], stocks: { SZ301178: stockOf() } });
    const before = {
      snapshots: Number((await env.DB.prepare("SELECT count(*) c FROM snapshots").first<{ c: number }>())?.c),
      kline: Number((await env.DB.prepare("SELECT count(*) c FROM kline_cache").first<{ c: number }>())?.c),
    };

    await call("/api/ai-explain?symbol=SZ301178", envWith(MAGPIE));
    await call("/api/ai-explain?symbol=SZ301178", envWith(MAGPIE)); // 两次（含缓存命中路径）

    const afterSnap = Number((await env.DB.prepare("SELECT count(*) c FROM snapshots").first<{ c: number }>())?.c);
    const afterKline = Number((await env.DB.prepare("SELECT count(*) c FROM kline_cache").first<{ c: number }>())?.c);
    expect(afterSnap).toBe(before.snapshots);
    expect(afterKline).toBe(before.kline);
  });

  it("红线词表：模型返回「建议买入」→ 整段替换为拒答，label 仍在", async () => {
    fetchMock
      .get(ORIGIN)
      .intercept({ path: CHAT_PATH, method: "POST" })
      .reply(200, { choices: [{ message: { content: "该票评分高，建议买入，目标价 18 元。" } }] });
    await ingestRound({ main: [MAIN_ROW("SZ301172")], stocks: { SZ301172: stockOf() } });

    const d = (await (await call("/api/ai-explain?symbol=SZ301172", envWith(MAGPIE))).json<any>()).data;
    expect(d.redlined).toBe(true);
    expect(d.text).toBe(REFUSAL);
    expect(d.text).not.toContain("建议买入");
    expect(d.label).toBe(AI_LABEL);
  });

  it("同素材第二次请求命中缓存 → 零模型调用（只消费 1 个拦截器）", async () => {
    fetchMock
      .get(ORIGIN)
      .intercept({ path: CHAT_PATH, method: "POST" })
      .reply(200, { choices: [{ message: { content: OK_TEXT } }] });
    await ingestRound({ main: [MAIN_ROW("SZ301173")], stocks: { SZ301173: stockOf() } });

    const a = (await (await call("/api/ai-explain?symbol=SZ301173", envWith(MAGPIE))).json<any>()).data;
    const b = (await (await call("/api/ai-explain?symbol=SZ301173", envWith(MAGPIE))).json<any>()).data;
    expect(a.cached).toBe(false);
    expect(b.cached).toBe(true);
    expect(b.text).toBe(a.text);
    // 拦截器只被消费一次 ⇒ 第二次没出网。afterEach 的 assertNoPendingInterceptors 也会兜住。
  });

  it("上游 5xx → 502 AI_UPSTREAM，响应不外泄上游 body", async () => {
    fetchMock
      .get(ORIGIN)
      .intercept({ path: CHAT_PATH, method: "POST" })
      .reply(500, "internal-upstream-secret-detail");
    await ingestRound({ main: [MAIN_ROW("SZ301174")], stocks: { SZ301174: stockOf() } });

    const res = await call("/api/ai-explain?symbol=SZ301174", envWith(MAGPIE));
    expect(res.status).toBe(502);
    const body = await res.json<any>();
    expect(body.error.code).toBe("AI_UPSTREAM");
    expect(JSON.stringify(body)).not.toContain("internal-upstream-secret-detail");
  });

  it("上游失败后释放冷却：同一 symbol 可立即重试（不被 429 锁死）", async () => {
    // 交叉审查复核项 1-3：频控在 transcribe **之前**武装（同步临界区，防并发双打），
    // 若不释放，一次上游抖动会锁死 30s —— 那不是限流而是可用性 bug。
    fetchMock
      .get(ORIGIN)
      .intercept({ path: CHAT_PATH, method: "POST" })
      .reply(500, "boom")
      .times(1);
    fetchMock
      .get(ORIGIN)
      .intercept({ path: CHAT_PATH, method: "POST" })
      .reply(200, { choices: [{ message: { content: OK_TEXT } }] });
    await ingestRound({ main: [MAIN_ROW("SZ301176")], stocks: { SZ301176: stockOf() } });

    const first = await call("/api/ai-explain?symbol=SZ301176", envWith(MAGPIE));
    expect(first.status).toBe(502);

    // 立刻重试：素材未变（缓存未写入、但冷却已释放）⇒ 应重新出网并成功
    const second = await call("/api/ai-explain?symbol=SZ301176", envWith(MAGPIE));
    expect(second.status).toBe(200);
    expect((await second.json<any>()).data.text).toBe(OK_TEXT);
  });

  it("频控：素材变化后同 symbol 再请求 → 429 AI_RATE_LIMITED，零模型调用", async () => {
    // 审查 A / P2-5：缓存拦不住「素材变了」的重复点击（每轮涨幅/排名都变）。
    // 第一次请求（消费拦截器 1）；第二次素材已变（改了涨幅）⇒ 缓存 miss ⇒ 靠频控拦。
    fetchMock
      .get(ORIGIN)
      .intercept({ path: CHAT_PATH, method: "POST" })
      .reply(200, { choices: [{ message: { content: OK_TEXT } }] });
    await ingestRound({ main: [MAIN_ROW("SZ301175")], stocks: { SZ301175: stockOf() } });
    const first = await call("/api/ai-explain?symbol=SZ301175", envWith(MAGPIE));
    expect(first.status).toBe(200);

    // 新一轮：涨幅变化 ⇒ 素材不同 ⇒ 缓存必 miss
    await ingestRound({
      date: "2026-11-03",
      main: [{ ...MAIN_ROW("SZ301175"), pct: 9.9 }],
      stocks: { SZ301175: stockOf(15) },
    });
    const second = await call("/api/ai-explain?symbol=SZ301175", envWith(MAGPIE));
    expect(second.status).toBe(429);
    expect((await second.json<any>()).error.code).toBe("AI_RATE_LIMITED");
    // 零出网：只注册过 1 个拦截器，若第二次真出网会报 “no interceptor matched”
  });
});

describe("素材组装（纯函数，确定性）", () => {
  const regions = {
    main: [MAIN_ROW("SZ301171")],
    hist: [],
    hot: [],
    offboard: [],
    gate: { main: [], hist: [], hot: [], offboard: [], stats: {} as any },
  };

  it("同输入 → 深相等；缺字段写「暂无数据」而非造值", () => {
    const input = {
      symbol: "SZ301171",
      regions,
      kline: { klineDate: "2026-11-02", kline: [], appearances: [] },
      date: "2026-11-02",
      time: "10:31:00",
    } as any;
    const a = buildMaterials(input);
    const b = buildMaterials(input);
    expect(a).toEqual(b);
    expect(JSON.stringify(a)).toBe(JSON.stringify(b));

    // 无 K 线 / 无出现史 → 明确写「暂无数据」，不出现编造数值
    const noKline = buildMaterials({ ...input, kline: null } as any);
    const items = noKline.items.map((i: any) => i.id);
    expect(items).toContain("kline.summary");
    expect(noKline.items.find((i: any) => i.id === "kline.summary")?.value).toBe("暂无数据");
    expect(items.some((i: string) => i.startsWith("appearance."))).toBe(false);
    expect(noKline.missing).toContain("appearances");
  });

  it("不在任何区块的票 → materials 里没有 row.* 项，missing 明确登记", () => {
    const m = buildMaterials({
      symbol: "SZ301999",
      regions,
      kline: { klineDate: "2026-11-02", kline: [["2026-11-02", 1, 2, 0.9, 2.1, 10]], appearances: [] },
      date: "2026-11-02",
      time: "10:31:00",
    } as any);
    expect(m.items.some((i: any) => i.id.startsWith("row."))).toBe(false);
    expect(m.missing).toContain("regionRow");
  });

  // 2026-11-02（复核后补）：层级列只有榜外/榜内异动两段填，榜内飙升行（`HotCandidate`）
  // **结构上没有 `tier`** —— 字段表若共用，每份榜内素材都会多一条「层 = 暂无数据」的噪声。
  it("榜内飙升段不列「层」（结构上无 tier），榜外异动段列「层」", () => {
    const mix = {
      main: [],
      hist: [],
      hot: [{ symbol: "SZ300758", code: "300758", name: "七彩化学", percent: 5.5, score: 66.5, streak: 3 }],
      offboard: [{ symbol: "SZ300999", code: "300999", name: "某票", tier: "T2", percent: 4.1, streak: 1 }],
      gate: { main: [], hist: [], hot: [], offboard: [], stats: {} as any },
    } as any;

    const hot = buildMaterials({ symbol: "SZ300758", regions: mix, kline: null } as any);
    expect(hot.items.some((i: any) => i.id === "row.hot.tier")).toBe(false);
    expect(hot.items.find((i: any) => i.id === "row.hot.streak")?.value).toBe("3");
    expect(hot.items.find((i: any) => i.id === "row.hot.score")?.value).toBe("66.5");

    const off = buildMaterials({ symbol: "SZ300999", regions: mix, kline: null } as any);
    expect(off.items.find((i: any) => i.id === "row.offboard.tier")?.value).toBe("T2");
    expect(off.items.find((i: any) => i.id === "row.offboard.percent")?.value).toBe("+4.10%");
  });
});