/**
 * 列投影（D7 的另一半：**列头**单源在云端 `colSpec`，**取值**投影在本文件）。
 *
 * 为什么前端必须有这样一份表：`col_spec()`（`scanner/panel_serialize.py`）下发的
 * `{key,label,align,width}` 里 `key === label === 中文列名`，而**行数据用的是英文字段名**
 * （`symbol` / `pct` / `accum` / `rank_change` …）。两者的对应关系只能由消费方持有 ——
 * 载荷里没有可推导的线索。
 *
 * 三条纪律：
 *
 * 1. **列名只许出现在本文件**。`components/**` 由
 *    `tests/test_panel_web_hygiene.py::test_table_components_have_no_hardcoded_column_labels`
 *    守着不得出现列名字面量。
 * 2. 本表的键集合必须与 `COLS_POOL/HOT/HIST` 的列名**逐项同序完全相同**
 *    （不少一列、不多一列）—— 由 `tests/test_panel_web_cols.py` 比对 Python 侧真源，
 *    漂移即红。少一列的后果不是报错而是**静默空白格**，故必须有守卫。
 * 3. 取值口径与终端 `scanner/view/render.py`（`_hot_row_cells` / `_emit_pool_table_row` /
 *    回捞区渲染）同源：同一字段、同一格式化、同一「空显 `—` 不显 `0`」规则。
 *    **唯一有意的分歧**是涨跌配色 —— 终端用风险温度分级色，面板按验收标准一律
 *    「红涨 #ef232a / 绿跌 #14b143」（按符号着色，不引入分级阈值）。
 */

import type { ColSpec, Row } from "../api/types";

/** 区块口径。列集相同而语义不同处靠它区分（与终端 `_hot_row_cells` 的 `board_segment` 同源）。 */
export type Profile = "pool" | "hot" | "offboard" | "hist";

/** 涨跌色（红涨绿跌，A 股口径）。 */
export type Tone = "up" | "down";

/** 行尾标记：与终端行尾图标同源（新 / ⚡ / 稳 / ▼）。 */
export type MarkKind = "new" | "bolt" | "steady" | "outflow";

export type Mark = { text: string; kind: MarkKind };

export type Cell = {
  text: string;
  tone?: Tone;
  /** 强调（核心股加粗；榜内飙升段的连击 ★ 另由 `strong` 走同一路径）。 */
  strong?: boolean;
};

export type RowContext = {
  /** 从 1 开始的行号（终端 `#` 列同理）。 */
  index: number;
  /**
   * 榜内飙升段？（同 `COLS_HOT` 的三段靠它区分：榜内飙升段带榜单专属量）
   * 由 `cellsFor` 按 profile 填 —— 调用方**不必也不该**自己传。
   */
  board?: boolean;
  /** `marks.breakout` / `marks.beauty`（键为 `"sym|cat"`）。 */
  marks: {
    breakout: Record<string, boolean | string>;
    beauty: Record<string, string>;
  };
};

/** 空值一律显 `—`（**不是 `0`**）—— 与终端「不伪造 0.00」同规则。 */
const DASH = "—";

/** 连击 ≥ 该值标 ★（单源 `config_hot_watch.HOT_HIGHLIGHT_STREAK`）。 */
export const HOT_HIGHLIGHT_STREAK = 3;
/** 资金流出标记阈值（单源 `config.FUND_FLOW_MAIN_PCT_WEAK` / `_EXTREME`）。 */
const FUND_OUT_PCT = -5;
const FUND_STRONG_OUT_PCT = -8;

/** 宽接口扣值：非数值/NaN/空串 → null（null 表示「无此量」，显示 `—`）。 */
export function num(v: unknown): number | null {
  if (typeof v === "number") return Number.isFinite(v) ? v : null;
  if (typeof v === "string" && v.trim() !== "") {
    const parsed = Number(v);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

/** 文本取值（非字符串 → 空串）。 */
export function text(v: unknown): string {
  return typeof v === "string" ? v : "";
}

/** `[+/-]x.xx%`。 */
export function fmtPct(v: number | null): string {
  if (v === null) return DASH;
  return `${v >= 0 ? "+" : ""}${v.toFixed(2)}%`;
}

/** 成交量：股 → 手（1 手 = 100 股），带中文单位（终端 `_fmt_hot_volume_hand` 同源）。 */
export function fmtVolumeHand(v: number | null): string {
  if (v === null || v <= 0) return DASH;
  const hands = v / 100;
  if (hands >= 1e8) return `${(hands / 1e8).toFixed(2)}亿手`;
  if (hands >= 1e4) return `${(hands / 1e4).toFixed(2)}万手`;
  return `${hands.toFixed(0)}手`;
}

/** 成交额：元 → 亿/万（终端 `_fmt_hot_amount` 同源）。 */
export function fmtAmount(v: number | null): string {
  if (v === null || v <= 0) return DASH;
  if (v >= 1e8) return `${(v / 1e8).toFixed(2)}亿`;
  if (v >= 1e4) return `${(v / 1e4).toFixed(1)}万`;
  return v.toFixed(0);
}

type CellFn = (row: Row, ctx: RowContext) => Cell;

/** 涨跌格：按符号着色（红涨绿跌，验收 2）。 */
function pctCell(v: number | null, asText?: string): Cell {
  if (v === null) return { text: DASH };
  return { text: asText ?? fmtPct(v), tone: v > 0 ? "up" : v < 0 ? "down" : undefined };
}

function accumCell(v: number | null): Cell {
  return v === null ? { text: DASH } : { text: fmtPct(v) };
}

function priceCell(v: number | null): Cell {
  return v === null || v === 0 ? { text: DASH } : { text: v.toFixed(2) };
}

/** 评分格：pool 取整（终端 `{row.score:.0f}`）、hot 一位小数（`{c.score:.1f}`）、hist 取整。 */
function scoreCell(v: number | null, digits: number): Cell {
  if (v === null || v === 0) return { text: DASH };
  return { text: v.toFixed(digits) };
}

/** 榜内飙升段专属量（其余段结构上没有 → `—`，不是 0）。 */
const boardOnly = (wanted: boolean, fn: CellFn): CellFn => (row, ctx) => (ctx.board === wanted ? fn(row, ctx) : { text: DASH });

/** `#` 列：行号。 */
const idxCell: CellFn = (_row, ctx) => ({ text: String(ctx.index + 1) });

/** 板块列：空值显 `—`（取数失败）而非「其他」（确实没匹配上）。 */
const sectorCell: CellFn = (row) => {
  const sector = text(row.sector);
  return { text: sector || DASH };
};

// ── v1 池选（COLS_POOL）────────────────────────────────────────────────────
// #region COLS:pool
const POOL_CELLS: Record<string, CellFn> = {
  "#": idxCell,
  "代码": (row) => ({ text: text(row.symbol) }),
  "名称": (row) => ({ text: text(row.name), strong: row.core === true }),
  "涨幅": (row) => pctCell(num(row.pct)),
  "5日累计": (row) => accumCell(num(row.accum)),
  "现价": (row) => priceCell(num(row.current)),
  "排名": (row) => ({ text: num(row.rank) === null ? DASH : String(num(row.rank)) }),
  "板块": sectorCell,
  "评分": (row) => scoreCell(num(row.score), 0),
  "策略": (row) => ({ text: text(row.cat_label) }),
};
// #endregion

// ── 沪深飙升三段（COLS_HOT；`board=false` 供榜内异动/榜外异动段复用）───────
// #region COLS:hot
const HOT_CELLS: Record<string, CellFn> = {
  "#": idxCell,
  "代码": (row) => ({ text: text(row.code) }),
  "名称": (row) => ({ text: text(row.name) }),
  "涨幅": (row) => pctCell(num(row.percent)),
  "5日累计": (row) => accumCell(num(row.accum_5d)),
  "现价": (row) => priceCell(num(row.current)),
  "排名上升": boardOnly(true, (row) => {
    const rc = num(row.rank_change);
    return { text: rc === null ? DASH : `+${rc}` };
  }),
  "成交量": (row) => ({ text: fmtVolumeHand(num(row.volume)) }),
  "成交额": (row) => ({ text: fmtAmount(num(row.amount)) }),
  "量比": (row) => {
    const vr = num(row.volume_ratio);
    return { text: vr === null || vr <= 0 ? DASH : vr.toFixed(2) };
  },
  "换手%": (row) => {
    const tr = num(row.turnover_rate);
    return { text: tr === null || tr <= 0 ? DASH : tr.toFixed(1) };
  },
  "市值(亿)": (row) => {
    const cap = num(row.market_capital);
    return { text: cap === null || cap <= 0 ? DASH : (cap / 1e8).toFixed(0) };
  },
  "板块": sectorCell,
  "评分": boardOnly(true, (row) => scoreCell(num(row.score), 1)),
  "层": boardOnly(false, (row) => ({ text: text(row.tier) || DASH })),
  "连击": boardOnly(true, (row) => {
    const streak = num(row.streak);
    if (streak === null) return { text: DASH };
    return { text: streak >= HOT_HIGHLIGHT_STREAK ? `★${streak}` : String(streak), strong: streak >= HOT_HIGHLIGHT_STREAK };
  }),
};
// #endregion

// ── v1 回捞（COLS_HIST）────────────────────────────────────────────────────
// #region COLS:hist
const HIST_CELLS: Record<string, CellFn> = {
  "#": idxCell,
  "代码": (row) => ({ text: text(row.code) }),
  "名称": (row) => ({ text: text(row.name) }),
  "涨幅": (row) => pctCell(num(row.percent)),
  "5日累计": (row) => accumCell(num(row.accum_5d)),
  "现价": (row) => priceCell(num(row.current)),
  "量比": (row) => {
    const vr = num(row.vol_ratio);
    return { text: vr === null || vr <= 0 ? DASH : vr.toFixed(2) };
  },
  "距v1": (row) => {
    const days = num(row.rec_days_ago);
    return { text: days === null ? DASH : `${days}日` };
  },
  "评分": (row) => scoreCell(num(row.score), 0),
  "上次v1桶": (row) => ({ text: text(row.rec_category) }),
};
// #endregion

/** 榜外异动段与榜内飙升段**列集相同、语义不同**（同 `COLS_HOT`，`board=false`）。 */
const OFFBOARD_CELLS = HOT_CELLS;

const TABLES: Record<Profile, Record<string, CellFn>> = {
  pool: POOL_CELLS,
  hot: HOT_CELLS,
  offboard: OFFBOARD_CELLS,
  hist: HIST_CELLS,
};

/**
 * 榜内飙升段（`board=true`）？榜内异动段与榜外异动段同门同层（`board=false`）。
 *
 * ⚠ 面板当前只有两段：`regions.hot` = 榜内飙升、`regions.offboard` = 榜外异动。
 * 终端的**榜内异动段不在契约里**（`regions` 只有 main/hist/hot/offboard 四个键），
 * 加它要动契约版本（SCHEMA_VERSION），不在本轮范围。
 */
export function isBoardSegment(profile: Profile): boolean {
  return profile === "hot";
}

/** 按 `colSpec` 逐列取值（`spec.key` 即中文列名；未知列名 → 空串，由守卫测试兜住）。 */
export function cellsFor(row: Row, specs: ColSpec[], profile: Profile, ctx: RowContext): Cell[] {
  const table = TABLES[profile];
  const scoped: RowContext = { ...ctx, board: isBoardSegment(profile) };
  return specs.map((spec) => table[spec.key]?.(row, scoped) ?? { text: "" });
}

/** 行尾标记的 `"sym|cat"` 键（与 `panel_serialize._marks` 的键格式一致，**不要重拼**）。 */
function markKey(row: Row): string {
  return `${text(row.symbol) || text(row.code)}|${text(row.category)}`;
}

/**
 * 行尾标记（与终端 `_entry_row_suffix` / `_watch_tail_terminal` 同源）。
 *
 * · v1 池选：`新`（本轮新进池）→ `⚡`（动量加速画像）→ `稳`（日线美感）
 * · 飙升/回捞三区：`▼`/`▼▼`（主力净流出）+ `稳`
 *   ⚠ 正流入的 `▲`/`▲▲` **不画**（2026-09-28 用户决策，终端同款）
 * 风险 `⚠` 不在此列：它挂在候选活对象上，**不进载荷**（`panel_serialize` 剥掉了
 * `_candidate`），面板无从判断 —— 不猜、不补。
 */
export function rowMarks(row: Row, profile: Profile, ctx: RowContext): Mark[] {
  const marks: Mark[] = [];
  const key = markKey(row);
  if (profile === "pool" && row.is_new_entry === true) {
    marks.push({ text: "新", kind: "new" });
  }
  if (profile !== "pool") {
    const ff = num(row.ff_pct);
    if (ff !== null && ff <= FUND_STRONG_OUT_PCT) marks.push({ text: "▼▼", kind: "outflow" });
    else if (ff !== null && ff <= FUND_OUT_PCT) marks.push({ text: "▼", kind: "outflow" });
  }
  if (profile === "pool" && ctx.marks.breakout[key]) {
    marks.push({ text: "⚡", kind: "bolt" });
  }
  const beauty = ctx.marks.beauty[key];
  if (beauty) {
    marks.push({ text: beauty, kind: "steady" });
  }
  return marks;
}

/** 详情页路由用的 symbol（hot/hist 行同名不同列：`symbol` 恒在，`code` 为展示码）。 */
export function rowSymbol(row: Row): string {
  return text(row.symbol) || text(row.code);
}
