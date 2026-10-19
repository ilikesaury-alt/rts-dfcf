/**
 * 快照轮询（design D4：60s 轮询，**不用 SSE**）。
 *
 * 唯一的网络入口是 `api/client.ts`（本文件只调 `apiGet`，不碰 `fetch`）。
 *
 * 行为要点（每条都对应一条验收）：
 * - **先 `meta` 后 `regions`**：首轮扫描完成前 `meta.ready=false`，此时不去打
 *   `regions`（那会 404 `NO_SNAPSHOT`），界面显示「等待首轮上报」而不是空白。
 * - **失败保留最后快照**（验收 3「不白屏」）：本地服务无响应时只置 `error`，
 *   已拿到的 `snapshot` 原样留在界面上，状态条变红。
 * - 轮询间隔取服务端下发的 `ctx.config.refreshInterval`（缺省 60s，与
 *   `scanner.config.REFRESH_INTERVAL` 同值），用 ref 读取以免重建定时器。
 */

import { useEffect, useRef, useState } from "react";

import { API, ApiError, apiGet } from "../api/client";
import type { MetaResponse, RegionsResponse } from "../api/types";

const POLL_MS = 60_000;

export type SnapshotState = {
  snapshot: RegionsResponse | null;
  meta: MetaResponse | null;
  /** 最近一次失败的 error code（成功即清空）。`NETWORK` = 本地服务没响应。 */
  error: string | null;
  /** 最后一次成功拿到快照的**本机时刻**（ms），用于「数据滞后」提示。 */
  updatedAt: number | null;
};

export function useSnapshot(): SnapshotState {
  const [state, setState] = useState<SnapshotState>({ snapshot: null, meta: null, error: null, updatedAt: null });
  const intervalRef = useRef(POLL_MS);

  const configured = state.snapshot?.ctx?.config?.refreshInterval;
  if (typeof configured === "number" && configured > 0) {
    intervalRef.current = configured * 1000;
  }

  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;

    const tick = async () => {
      try {
        const meta = await apiGet<MetaResponse>(API.meta);
        if (cancelled) return;
        if (meta.ready) {
          const snapshot = await apiGet<RegionsResponse>(API.regions);
          if (cancelled) return;
          setState({ snapshot, meta, error: null, updatedAt: Date.now() });
        } else {
          setState((prev) => ({ ...prev, meta, error: null }));
        }
      } catch (e) {
        if (cancelled) return;
        const code = e instanceof ApiError ? e.code : "INTERNAL";
        // 保留最后快照：只置错误，不动 snapshot/meta（换了就会白屏）
        setState((prev) => ({ ...prev, error: code }));
      } finally {
        if (!cancelled) timer = window.setTimeout(() => void tick(), intervalRef.current);
      }
    };

    void tick();
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, []);

  return state;
}
