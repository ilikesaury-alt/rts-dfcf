/**
 * 前端**唯一**网络收口（design §2.2「合并」+ D7 反模式）。
 *
 * 纪律：
 * - `web/src` 下**除本文件外 0 处裸 `fetch(`**（由 `tests/test_panel_web_hygiene.py`
 *   的 grep 断言守住）。组件只调 `apiGet`/`apiSend`。
 * - `{ok,error}` 信封由本文件统一解析；**404/400 一律抛错，绝不吞掉**。
 *   吞掉的代价是详情页静默显示空态，用户以为「该票没数据」，实则云端报错。
 * - 错误**按 `code` 分支，不解析 `message` 文案**（文案会改，码不会）。
 *
 * URL 一律**相对路径**（`/api/...`）：生产同域（`workers_dev:false` + routes，
 * design §3.2 第 6 层），开发期由 Vite 代理到 127.0.0.1:8787 —— 两端都无跨域，
 * 因此这里**不需要**任何 baseURL 配置。
 */

import type { ApiErrorCode, Envelope } from "./types";

/** 统一错误类型。`status` 保留 HTTP 状态码供展示，`code` 供逻辑分支。 */
export class ApiError extends Error {
  constructor(
    public code: ApiErrorCode | string,
    message: string,
    public status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

type RequestInitLite = {
  signal?: AbortSignal;
  /** 覆盖超时；默认 15s —— 够慢也不至于让状态条一直转。 */
  timeoutMs?: number;
};

const DEFAULT_TIMEOUT_MS = 15_000;

/**
 * 拿响应并解信封。三类失败在这里被**区分开**（调用方需要这个区别）：
 *  1. 网络层失败（压根没响应）→ `NETWORK`，供 E14「保留最后快照 + 状态条变红」
 *  2. 响应不是 JSON（典型：Access 拦截页返回 HTML）→ `BAD_RESPONSE`，供 E7
 *     「请重新登录」
 *  3. 业务错误（`ok:false` 或 HTTP 非 2xx）→ 按信封里的 `code`
 */
async function request<T>(path: string, init: RequestInitLite, method: "GET" | "POST", body?: unknown): Promise<T> {
  const { signal, timeoutMs = DEFAULT_TIMEOUT_MS } = init;

  // 组合外部 signal 与超时：AbortSignal.any 需要较新运行时，这里手写以免
  // 在老 Safari 上直接崩（AbortSignal.any 缺失会抛 TypeError）。
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeoutMs);
  const onAbort = () => ctl.abort();
  signal?.addEventListener("abort", onAbort);
  const cleanup = () => {
    clearTimeout(timer);
    signal?.removeEventListener("abort", onAbort);
  };

  let res: Response;
  try {
    res = await fetch(path, {
      method,
      headers: body === undefined ? undefined : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: ctl.signal,
    });
  } catch (e) {
    cleanup();
    // fetch 抛错 = 断网/DNS/被拒/CORS。**不重试**：SSE 之外的读由调用方决定节奏。
    throw new ApiError("NETWORK", e instanceof Error ? e.message : "network error", 0);
  }

  let payload: unknown;
  try {
    payload = await res.json();
  } catch {
    cleanup();
    // 有响应但不是 JSON：Access 拦截页（E7）或网关错误页。**不可当成功**。
    throw new ApiError("BAD_RESPONSE", `non-JSON response (status ${res.status})`, res.status);
  }
  cleanup();

  const env = payload as Envelope<T>;
  // 信封缺失/结构不对，同样按「响应不可信」处理，不要静默当成功。
  if (typeof env !== "object" || env === null || !("ok" in env)) {
    throw new ApiError("BAD_RESPONSE", "response is not an {ok,...} envelope", res.status);
  }
  if (!env.ok) {
    // 业务错误：保留云端给的 code/message，但 status 用 HTTP 的（可能更细）。
    throw new ApiError(env.error.code, env.error.message, res.status);
  }
  // 业务成功但 HTTP 非 2xx（如 Access 200 但 4xx）：按信封走，但要留痕。
  if (!res.ok) {
    throw new ApiError("INTERNAL", `unexpected status ${res.status} with ok:true`, res.status);
  }
  return env.data;
}

/** 读端点。`{ok:true,data}` 的 `data` 原样返回。 */
export function apiGet<T>(path: string, init?: RequestInitLite): Promise<T> {
  return request<T>(path, init ?? {}, "GET");
}

/**
 * 写端点。
 *
 * ⚠ 面板是**只读**的（NFR；全仓唯一写端点 `/api/ingest` 由扫描器调，前端永不调）。
 *   保留本函数是为了让「禁止裸 fetch」这条规矩有个**合规**的出口，而不是逼着
 *   将来真需要 POST 的地方去破例写 fetch。**当前无任何调用方**，
 *   也**不新增**任何端点来「给它找个用武之地」。
 */
export function apiSend<T>(path: string, body: unknown, init?: RequestInitLite): Promise<T> {
  return request<T>(path, init ?? {}, "POST", body);
}

/** 端点路径集中在此，避免字符串散落各处（改路径只改一处）。 */
export const API = {
  regions: "/api/regions",
  meta: "/api/meta",
  stream: "/api/stream",
  stockKline: (symbol: string) => `/api/stock/${encodeURIComponent(symbol)}/kline`,
  history: "/api/history",
  historyRounds: "/api/history/rounds",
  metaDates: "/api/meta/dates",
  metaConfig: "/api/meta/config",
} as const;
