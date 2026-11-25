/**
 * 上报载荷的 TS 类型 + 契约常量（design §5.1）。
 *
 * ⚠ `SCHEMA_VERSION` **必须硬编码在这里**，不能放 wrangler `vars`
 *   （复核修 1）：Cloudflare 的 vars 一律是字符串，比较会变成 `2 !== "2"`
 *   → 每一轮都被云端 400 拒绝。改契约时**两端同改**并提版本
 *   （本地 `scanner/panel_serialize.py` 的同名常量）。
 */
// v3（2026-11-17，与 scanner/panel_serialize.py 同批）：regions 新增
// `onboard`（榜内异动段）与 `crosssection`（≡ 横截面层）。ingest 的 payload 是
// `{regions, ctx}` 原样 JSON，故云端**无需**改表结构 —— 只是版本号必须双端同值。
export const SCHEMA_VERSION = 3;

export type GateStats = {
  total: number;
  passed: number;
  tier_a: number;
  tier_b: number;
  tier_c_fallback: number;
  vetoed: number;
  dropped_no_fallback: number;
};

/** 扁平标量行（主线/回捞/飙升/榜外同构）。 */
export type Row = Record<string, unknown>;

export type ColSpec = { key: string; label: string; align: string; width: number };

export type Gate = {
  main: Row[];
  hist: Row[];
  hot: Row[];
  offboard: Row[];
  stats: GateStats;
};

export type Regions = {
  main: Row[];
  hist: Row[];
  hot: Row[];
  /** 榜内异动段（v3）：与 `hot` 同表头（`cols.onboard`）。 */
  onboard?: Row[];
  offboard: Row[];
  /** 横截面观察层（v3）；`undefined` / `enabled:false` = 未启用/非交易时段。 */
  crosssection?: CrossSection;
  gate: Gate;
  cols?: Record<string, ColSpec[]>;
  /**
   * 行尾标记 map。键是 `"sym|cat"` 字符串（本地把 tuple 键转好了，
   * 见 `panel_serialize._marks`）—— 云端**不要**试图再拼 `"sym|cat"`，原样用。
   */
  marks?: { breakout: Record<string, boolean | string>; beauty: Record<string, string> };
};

/** 横截面观察层（v3 新增；`crosssection.snapshot_payload` 原样透传，云端不重算）。 */
export type CrossSection = {
  enabled: boolean;
  state?: string | null;
  time?: string;
  valid_rows?: number;
  buffer_depth?: number;
  buffer_rounds?: number;
  p95?: number;
  signals?: number;
  divergence?: number | null;
  divergence_ok?: boolean | null;
  sealed_skipped?: number;
  warmup?: boolean;
  stale_rounds?: number;
  persist_min?: number;
  top_persist?: number;
  top_new?: number;
  display_flow_min?: number;
  display_pct_max?: number;
  hidden_mid?: number;
  hidden_flow?: number;
  hidden_pct?: number;
  /** Python `crosssection._flow_gate_note` 生成的人读说明；前端原样渲染，不重拼。 */
  display_flow_note?: string;
  display_pct_note?: string;
  groups?: { persist: Row[]; new: Row[] };
};

export type Ctx = {
  marketIdxPct: number | null;
  weak: boolean | null;
  flowFiltered: number;
  warnings: string[];
  ruleResult?: unknown;
  config?: Record<string, unknown>;
};

export type Stock = { klineDate: string; kline: number[][]; appearances: Row[] };

export type Payload = {
  schema: number;
  /** 仅供展示。任何「取最新一轮」的查询一律 ORDER BY date DESC, time DESC。 */
  seq: number;
  date: string;
  time: string;
  durationMs: number;
  regions: Regions;
  ctx: Ctx;
  stocks: Record<string, Stock>;
};

/** `/api/regions` 的响应 data（不含 stocks，design §4.3 / D4）。 */
export type RegionsResponse = {
  date: string;
  time: string;
  seq: number;
  ingestedAt?: string;
  regions: Regions;
  ctx: Ctx;
};
