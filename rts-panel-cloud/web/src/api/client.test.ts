/**
 * `api/client.ts` 行为测试（T1.2 验收：**404/400 统一抛错不吞**）。
 *
 * 为什么必须有：仓库纪律禁「空断言」。若只靠 grep 断言「没有裸 fetch」，
 * 那只证明**结构**对，不证明**行为**对 —— 而「吞掉错误」正是本任务最要防的
 * 失败模式：详情页静默显示空态，用户以为「该票没数据」，实则云端在报错。
 *
 * 测的是信封解析与失败分类，**不打真网络**：fetch 被替换为可编排的桩。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { API, ApiError, apiGet, apiSend } from "./client";

/** 可编排的 fetch 桩。 */
function stubFetch(impl: (url: string, init?: RequestInit) => Promise<Response> | Response) {
  const spy = vi.fn(impl);
  vi.stubGlobal("fetch", spy);
  return spy;
}

const jsonResponse = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

const okEnvelope = (data: unknown) => ({ ok: true, data });
const errEnvelope = (code: string, message: string) => ({ ok: false, error: { code, message } });

beforeEach(() => {
  vi.unstubAllGlobals();
});
afterEach(() => {
  vi.restoreAllMocks();
});

describe("apiGet —— 成功路径", () => {
  it("解出 {ok:true} 的 data", async () => {
    stubFetch(() => jsonResponse(okEnvelope({ date: "2026-09-29", time: "10:31:00" })));
    await expect(apiGet(API.regions)).resolves.toEqual({ date: "2026-09-29", time: "10:31:00" });
  });

  it("用相对路径（生产同域、开发走 Vite 代理，两端都无跨域）", async () => {
    const spy = stubFetch(() => jsonResponse(okEnvelope(null)));
    await apiGet(API.regions);
    expect(spy.mock.calls[0][0]).toBe("/api/regions");
  });
});

describe("apiGet —— 失败必须抛，不得吞", () => {
  it("404 NO_SNAPSHOT → 抛 ApiError，带 code 与 status", async () => {
    stubFetch(() => jsonResponse(errEnvelope("NO_SNAPSHOT", "尚无任何上报"), 404));
    await expect(apiGet(API.regions)).rejects.toThrow(ApiError);
    await apiGet(API.regions).catch((e: ApiError) => {
      expect(e.code).toBe("NO_SNAPSHOT"); // 按 code 分支，不解析 message
      expect(e.status).toBe(404);
    });
  });

  it("400 BAD_REQUEST → 抛，不返回 null/空对象", async () => {
    stubFetch(() => jsonResponse(errEnvelope("BAD_REQUEST", "bad date"), 400));
    await expect(apiGet(`${API.history}?date=x`)).rejects.toMatchObject({ code: "BAD_REQUEST", status: 400 });
  });

  it("E10 NOT_IN_REPORT（票未上报）→ 抛，且文案为设计指定那句", async () => {
    stubFetch(() => jsonResponse(errEnvelope("NOT_IN_REPORT", "本轮上报未包含该票"), 404));
    await apiGet(API.stockKline("300319")).catch((e: ApiError) => {
      expect(e.code).toBe("NOT_IN_REPORT");
      expect(e.message).toBe("本轮上报未包含该票");
    });
  });

  it("E7 非 JSON 响应（Access 拦截页）→ BAD_RESPONSE，不当成功", async () => {
    stubFetch(() => new Response("<html>Access login</html>", { status: 200 }));
    await expect(apiGet(API.regions)).rejects.toMatchObject({ code: "BAD_RESPONSE" });
  });

  it("缺 ok 字段的信封 → BAD_RESPONSE（不把畸形响应当成功）", async () => {
    stubFetch(() => jsonResponse({ data: { something: 1 } }));
    await expect(apiGet(API.regions)).rejects.toMatchObject({ code: "BAD_RESPONSE" });
  });

  it("E14 网络不可达 → NETWORK（调用方据此保留最后快照）", async () => {
    stubFetch(() => {
      throw new TypeError("Failed to fetch");
    });
    await expect(apiGet(API.regions)).rejects.toMatchObject({ code: "NETWORK", status: 0 });
  });
});

describe("apiSend", () => {
  it("POST + JSON body，且不带 Authorization（前端不碰 ingest 密钥）", async () => {
    const spy = stubFetch(() => jsonResponse(okEnvelope({ done: true }), 201));
    await apiSend("/api/none", { a: 1 });
    const [, init] = spy.mock.calls[0] as unknown as [string, RequestInit];
    expect(init.method).toBe("POST");
    expect(init.body).toBe(JSON.stringify({ a: 1 }));
    expect((init.headers as Record<string, string>).Authorization).toBeUndefined();
  });

  it("错误同样抛，不吞", async () => {
    stubFetch(() => jsonResponse(errEnvelope("INTERNAL", "服务内部错误"), 500));
    await expect(apiSend("/api/none", {})).rejects.toThrow(ApiError);
  });
});

describe("超时与取消", () => {
  it("超时 → NETWORK，不无限挂起", async () => {
    vi.useFakeTimers();
    stubFetch(
      (_u, init) =>
        new Promise((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () => reject(new Error("aborted")));
        }),
    );
    // ⚠ 先挂上拒绝处理再推进时间：否则 abort 触发的 rejection 会在 handler
    //   挂上之前就逃逸成 unhandled rejection（vitest 会记为 Error 并让 CI 红）。
    const settled = expect(apiGet(API.regions, { timeoutMs: 50 })).rejects.toMatchObject({ code: "NETWORK" });
    await vi.advanceTimersByTimeAsync(60);
    await settled;
    vi.useRealTimers();
  });

  it("外部 AbortSignal 可取消", async () => {
    const ctl = new AbortController();
    stubFetch(
      (_u, init) =>
        new Promise((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () => reject(new Error("aborted")));
        }),
    );
    const settled = expect(apiGet(API.regions, { signal: ctl.signal })).rejects.toMatchObject({ code: "NETWORK" });
    ctl.abort();
    await settled;
  });
});
