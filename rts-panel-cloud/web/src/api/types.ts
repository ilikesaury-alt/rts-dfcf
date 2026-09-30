/**
 * 上报载荷的前端类型 —— 与 `worker/src/serialize.ts` **逐字段对齐**（design §5.1）。
 *
 * ⚠ 这是**手抄镜像**，不是自动生成。改契约必须双端同改：
 *   本地 `scanner/panel_serialize.py` ⇄ 云端 `worker/src/serialize.ts` ⇄ 本文件。
 *   漂移由 `tests/test_panel_contract.py` 守着（它直接读云端源码做对照）。
 *
 * ⚠ 行数据 `Row` 刻意保持 `Record<string, unknown>`：行的字段来自
 *   `RecommendationRow` / 各区候选的**异构**结构（design §5.3），前端不逐字段声明。
 *   取值一律经 `lib/cols.ts` 的 colSpec 驱动，而不是在这里枚举。
 */

export type GateStats = {
  total: number;
  passed: number;
  tier_a: number;
  tier_b: number;
  tier_c_fallback: number;
  vetoed: number;
  dropped_no_fallback: number;
};

/** 扁平标量行（五区块同构）。 */
export type Row = Record<string, unknown>;

/** 列定义投影，由云端随 `/api/regions` 下发（D7 列头单源化）。 */
export type ColSpec = {
  key: string;
  label: string;
  align: string;
  width: number;
};

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
  offboard: Row[];
  gate: Gate;
  cols?: Record<string, ColSpec[]>;
  /**
   * 行尾标记。键是 `"sym|cat"` 字符串 —— 本地已把 tuple 键转好
   * （`panel_serialize._marks`），前端**不要**再自己拼。
   */
  marks?: {
    breakout: Record<string, boolean | string>;
    beauty: Record<string, string>;
  };
};

export type Ctx = {
  marketIdxPct: number | null;
  weak: boolean | null;
  flowFiltered: number;
  warnings: string[];
  ruleResult?: unknown;
  config?: Record<string, unknown>;
};

export type Stock = {
  klineDate: string;
  /** `[date, open, close, low, high, volume][]` —— **注意 close 在 low/high 之前**（ECharts 约定）。 */
  kline: number[][];
  appearances: Row[];
};

/** 完整上报载荷（`/api/regions` 不返回 stocks，故其响应另有类型）。 */
export type Payload = {
  schema: number;
  /** 仅供展示；「取最新一轮」是云端的事，前端不据此排序。 */
  seq: number;
  date: string;
  time: string;
  durationMs: number;
  regions: Regions;
  ctx: Ctx;
  stocks: Record<string, Stock>;
};

/** `/api/regions` 响应。**不含 stocks** —— 列表页不碰 K 线（D4）。 */
export type RegionsResponse = {
  date: string;
  time: string;
  seq: number;
  ingestedAt?: string;
  regions: Regions;
  ctx: Ctx;
};

/** `/api/meta` 响应。无快照时 `ready=false` 且为 **200**（不是 404）。 */
export type MetaResponse = {
  ready: boolean;
  date: string | null;
  time: string | null;
  seq: number | null;
  ingested_at?: string | null;
};

/** `/api/stock/{symbol}/kline` 响应（T4.1 实现，此处先定型）。 */
export type KlineResponse = {
  symbol: string;
  klineDate: string;
  kline: number[][];
  appearances: Row[];
  /** 该票在本轮的评分分解；非 v1 票没有 → 详情页显「无评分分解」。 */
  scoreBreakdown?: Record<string, number> | null;
};

/** 统一响应信封（`error.ts`）：成功 `{ok:true,data}`，失败 `{ok:false,error}`。 */
export type Envelope<T> = { ok: true; data: T } | { ok: false; error: { code: string; message: string } };

/**
 * 云端错误码（design §8 错误表）。**前端按码分支，不解析 message 文案** ——
 * 文案会改，码不会。
 */
export type ApiErrorCode =
  | "UNAUTHORIZED" // E3  密钥错
  | "SCHEMA_MISMATCH" // E4  上报端 schema 不符
  | "BAD_REQUEST" // E9  参数非法
  | "RANGE_EXCEEDED" // E9  days 越界
  | "NOT_IN_REPORT" // E10 票不在 kline_cache
  | "NO_SNAPSHOT" // E11 date 无快照
  | "CORRUPT_SNAPSHOT" //     payload 损坏
  | "NOT_FOUND" //  无此端点
  | "INTERNAL"
  | "NETWORK" //     自造：请求根本没拿到响应（E14 云端整体不可用）
  | "BAD_RESPONSE"; //     自造：拿到了响应但不是 JSON（典型是 Access 拦截页返回 HTML）
