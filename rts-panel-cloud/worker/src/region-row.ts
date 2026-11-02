/**
 * 在 `regions` 里按 symbol 找行（读端点共用）。
 *
 * 两个必须知道的事实（都来自 `web/src/lib/cols.ts` 的取值投影，不是猜的）：
 * - **symbol 取值是 `row.symbol ?? row.code`**，与前端 `cols.ts` 的
 *   `text(row.symbol) || text(row.code)` 同序（2026-11-02 实测四区行都带 `symbol`，
 *   形如 `SZ301171`；回退 `code` 是为裸 6 位形态留口）。
 * - 三区行还有 `tier` / `streak` 等专属量，pool 区有 `cat_label` / `is_new_entry`
 *   （⚠ 但**榜内飙升**行结构上没有 `tier`，见 `explain/material.ts` 的字段表分拆）。
 *
 * 只做精确匹配：web 详情链接用的就是行里的原值（`stockHref(row.symbol || row.code)`），
 * 所以这里若做「带前缀/裸 6 位等价」的模糊匹配，反而可能撞到另一只票。
 */
import type { Regions, Row } from "./serialize";

export type RegionKey = "main" | "hist" | "hot" | "offboard";

/** 区块顺序与面板阅读顺序一致（池选 / 回捞 / 榜内飙升 / 榜外异动）。 */
export const REGION_ORDER: readonly RegionKey[] = ["main", "hist", "hot", "offboard"];

export type FoundRow = { region: RegionKey; index: number; row: Row };

/** 行内 symbol：`symbol ?? code`（与 `web/src/lib/cols.ts` 的 `text(row.symbol) || text(row.code)` 同序）。 */
export function rowSymbol(row: Row): string {
  const v = row.symbol ?? row.code;
  return typeof v === "string" ? v : "";
}

/** 按 symbol 找行；找不到返回 null（**不抛错** —— 非 v1 票本来就可以不在任何区块）。 */
export function findRegionRow(regions: Regions | undefined, symbol: string): FoundRow | null {
  if (!regions) return null;
  for (const region of REGION_ORDER) {
    const rows = (regions[region] ?? []) as Row[];
    for (let i = 0; i < rows.length; i++) {
      if (rowSymbol(rows[i]) === symbol) return { region, index: i, row: rows[i] };
    }
  }
  return null;
}

/** `score_breakdown` 统一成 `Record<string, number> | null`（字符串/数组/缺失 → null）。 */
export function asBreakdown(v: unknown): Record<string, number> | null {
  if (!v || typeof v !== "object" || Array.isArray(v)) return null;
  const out: Record<string, number> = {};
  let n = 0;
  for (const [k, val] of Object.entries(v as Record<string, unknown>)) {
    if (typeof val === "number" && Number.isFinite(val)) {
      out[k] = val;
      n++;
    }
  }
  return n ? out : null;
}