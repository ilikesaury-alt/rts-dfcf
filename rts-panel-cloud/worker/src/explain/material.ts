/**
 * 解读素材的**确定性组装**（ADR 0001 第一道闸）。
 *
 * 本模块是整个 AI 解读层的事实源边界：
 * - **零 I/O、零时钟、零随机**：同输入 → 字节级同输出（可测、可复现、可缓存）；
 * - 字段名不是自拟的，全部来自 `web/src/lib/cols.ts` 的取值投影（该文件是「中文列名 ↔ 英文字段」
 *   的唯一对应表），先验文案另见 `PRIORS`；
 * - 拿不到的字段写 `暂无数据`，**绝不**让模型去补 —— 这是「不得编造」的落地点。
 *
 * 换句话说：模型看到的只有这里产出的 items，它没有别的输入。
 */
import type { Regions, Row } from "../serialize";
import { REGION_ORDER, findRegionRow, rowSymbol, type RegionKey } from "../region-row";

export type MaterialItem = { id: string; label: string; value: string };

export type ExplainMaterials = {
  symbol: string;
  items: MaterialItem[];
  /** 缺失的来源，供前端/提示词明确「哪些是没有的」。 */
  missing: string[];
};

export type BuildInput = {
  symbol: string;
  regions?: Regions;
  kline: { klineDate: string; kline: number[][]; appearances: Row[] } | null;
};

/** 空值统一文案（与终端/面板的 `—` 同语义，但这里要能被模型读懂）。 */
export const NA = "暂无数据";

/**
 * 类别先验（**手工维护**，决策 P2：不扩 Python 上报链路）。
 *
 * ⚠ 只写「这个桶是什么、**先验往哪个方向读**」，**不写任何命中率数字** ——
 *   本项目唯一的统计产物在本地（`rule_validate` / `prevday_perf`），未随快照上报，
 *   在云端凭空写一个百分比就是编造证据。
 *   来源：`scanner/categories.py::CATEGORIES` 的 `CategoryInfo` + `AGENTS.md`「目标函数」。
 *   改动口径请同步 `scanner/categories.py`。
 *
 * ⚠ 2026-11-04 口径修正（样本外实测，n=70 天 walkforward）：**「评分越高越靠前」是错的**。
 *   分数在**类别内**毫无区分力（类内 rank IC −0.020，正负天数接近 1:1）⇒
 *   排序的主轴是**类别先验**，类别内靠主力净占比/榜位区分。
 *   ⇒ 下列文案不再提「评分」，也不提「低分档命中更高」（那是 kNF 的旧口径，
 *     已被本次回放证伪为「类内无区分力」，保留会变成另一个误导）。
 */
export const PRIORS: Record<string, string> = {
  rebound: "反弹类（代号 RBD）：前期回调后出现反弹形态。**类别先验高**（该类历史次日≥7%命中率高）。",
  known_new_face: "已知新面孔（代号 kNF）：反复上榜的熟悉票重新出现。类别先验次高。",
  momentum: "动量类（代号 MOM）：榜上动能加速的票。类别先验居中。",
  new_face: "新面孔（代号 NEW）：首次/近阶段首次进入候选池的票。类别先验低于基线。",
  short_term: "短线类（代号 ST）：短线博弈形态。**类别先验最低**（低于全体基线），属参考口径。",
  core_dip: "核心票回调低吸（代号 core_dip）：核心方向标的回调到位。",
};

/** 评分在**类别内**无区分力的口径提示（2026-11-04 样本外实测）。
 *  云端解读**不得**把评分讲成「越强越可能涨」。 */
export const SCORE_CAVEAT =
  "⚠ 评分**不参与排序**（实测：类别内与次日涨幅无单调关系）；排序主轴是类别先验，类别内靠主力净占比/榜位区分。";

/** 行字段白名单：`[字段名, 中文标签, 格式化]`（字段名同 `cols.ts`，顺序决定素材顺序）。 */
type Fmt = "text" | "pct" | "price" | "score1" | "dec1" | "dec2" | "int" | "plusInt" | "cap" | "days" | "bool";
type Field = readonly [string, string, Fmt];

const HOT_COMMON_FIELDS: readonly Field[] = [
  ["name", "名称", "text"],
  ["percent", "涨幅", "pct"],
  ["accum_5d", "5日累计", "pct"],
  ["current", "现价", "price"],
  ["rank_change", "排名上升", "plusInt"],
  ["volume_ratio", "量比", "dec2"],
  ["turnover_rate", "换手%", "dec1"],
  ["market_capital", "市值(亿)", "cap"],
  ["sector", "板块", "text"],
  ["score", "评分", "score1"],
];

const STREAK_FIELD: Field = ["streak", "连击", "int"];

/**
 * 榜内飙升段（`hot_watch.HotCandidate`）字段：**无 `tier`**。
 *
 * ⚠ 层级列只有「榜内异动 / 榜外异动」两段填（AGENTS.md 2026-10-13 第 3 条：各段只用
 *   自己那一列，另一列显 `—`）。而 `HotCandidate` 结构上**没有** `tier` 属性 ⇒
 *   把它列进本段字段表只会给每份素材塞一条「层 = 暂无数据」—— 那不是「缺数据」，
 *   是噪声（模型会认真解释一个本就不存在的列）。
 */
const HOT_BOARD_FIELDS: readonly Field[] = [...HOT_COMMON_FIELDS, STREAK_FIELD];

/** 榜外异动段（`offboard_watch.OffboardCandidate`；榜内异动 `onboard_anomaly` 同形同字段）：
 *  同序 + 在「评分」后插入「层」（T1/T2）。 */
const OFFBOARD_FIELDS: readonly Field[] = [...HOT_COMMON_FIELDS, ["tier", "层", "text"], STREAK_FIELD];

const ROW_FIELDS: Record<RegionKey, readonly Field[]> = {
  main: [
    ["name", "名称", "text"],
    ["pct", "涨幅", "pct"],
    ["accum", "5日累计", "pct"],
    ["current", "现价", "price"],
    ["rank", "排名", "int"],
    ["sector", "板块", "text"],
    // ⚠ 2026-11-04：主表**不给评分**（类别内无区分力）⇒ 改给**类别先验命中率**
    //   （`prior_pct` 由 Python 侧随载荷下发，见 scanner/view/model.py::MainRow.prior_pct）。
    ["cat_label", "类别", "text"],
    ["prior_pct", "先验命中率(%)", "dec1"],
    ["is_new_entry", "本轮新进池", "bool"],
  ],
  hist: [
    ["name", "名称", "text"],
    ["percent", "涨幅", "pct"],
    ["accum_5d", "5日累计", "pct"],
    ["current", "现价", "price"],
    ["vol_ratio", "量比", "dec2"],
    ["rec_days_ago", "距v1", "days"],
    ["rec_category", "上次v1桶", "text"],
    ["score", "评分", "score1"],
  ],
  hot: HOT_BOARD_FIELDS,
  offboard: OFFBOARD_FIELDS,
};

const REGION_LABEL: Record<RegionKey, string> = {
  main: "池选",
  hist: "回捞",
  hot: "榜内飙升",
  offboard: "榜外异动",
};

function num(v: unknown): number | null {
  if (typeof v === "number") return Number.isFinite(v) ? v : null;
  if (typeof v === "string" && v.trim() !== "") {
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
  }
  return null;
}

/** 数值格式化：**固定小数位**（不依赖 locale/时区），保证确定性。 */
function fmt(kind: Fmt, v: unknown): string {
  if (kind === "bool") {
    if (v === true) return "是";
    if (v === false) return "否";
    return NA;
  }
  if (kind === "text") {
    if (typeof v === "string" && v.trim() !== "") return v;
    if (typeof v === "number" && Number.isFinite(v)) return String(v);
    return NA;
  }
  const n = num(v);
  if (n === null) return NA;
  switch (kind) {
    case "pct":
      return `${n >= 0 ? "+" : ""}${n.toFixed(2)}%`;
    case "price":
      return n.toFixed(2);
    case "score1":
      return n.toFixed(1);
    case "dec1":
      return n.toFixed(1);
    case "dec2":
      return n.toFixed(2);
    case "int":
      return String(Math.round(n));
    case "plusInt":
      return `${n >= 0 ? "+" : ""}${Math.round(n)}`;
    case "cap":
      return (n / 1e8).toFixed(2);
    case "days":
      return `${Math.round(n)}日`;
    default:
      return String(n);
  }
}

/** `score_breakdown` → `key=val, ...`（键排序，保证同输入同输出）。 */
function breakdownText(v: unknown): string {
  if (!v || typeof v !== "object" || Array.isArray(v)) return NA;
  const entries = Object.entries(v as Record<string, unknown>)
    .filter(([, val]) => typeof val === "number" && Number.isFinite(val as number))
    .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
  if (!entries.length) return NA;
  return entries.map(([k, val]) => `${k}=${(val as number).toFixed(1)}`).join(", ");
}

function appearancesText(a: Row): string {
  return [
    typeof a.date === "string" ? a.date : NA,
    typeof a.time === "string" ? a.time : "",
    typeof a.category === "string" ? a.category : NA,
    `score=${fmt("score1", a.score)}`,
    `pct=${fmt("pct", a.percent)}`,
  ]
    .filter(Boolean)
    .join(" ");
}

export function buildMaterials(input: BuildInput): ExplainMaterials {
  const items: MaterialItem[] = [];
  const missing: string[] = [];
  const { symbol, regions, kline } = input;

  // ── 区块行（顺序 = 区块阅读顺序，且只取**首个**命中区块，避免同一票双挂时重复计数）───
  const hit = findRegionRow(regions, symbol);
  if (!hit) {
    missing.push("regionRow");
  } else {
    const prefix = `row.${hit.region}`;
    for (const [key, label, kind] of ROW_FIELDS[hit.region]) {
      items.push({ id: `${prefix}.${key}`, label: `${REGION_LABEL[hit.region]}·${label}`, value: fmt(kind, hit.row[key]) });
    }
    items.push({
      id: `${prefix}.score_breakdown`,
      label: `${REGION_LABEL[hit.region]}·评分分解`,
      value: breakdownText(hit.row.score_breakdown),
    });
  }

  // ── 出现史 ────────────────────────────────────────────────────────────────
  const apps = kline?.appearances ?? [];
  if (!apps.length) {
    missing.push("appearances");
  } else {
    apps.forEach((a, i) => {
      items.push({ id: `appearance.${i}`, label: `出现史·第${i + 1}条`, value: appearancesText(a) });
    });
  }

  // ── K 线（只给「有几根 + 最近一根」，不给整段原始数组：模型读不懂也别拿它算）───
  if (!kline || !kline.kline.length) {
    missing.push("kline");
    items.push({ id: "kline.summary", label: "K线", value: NA });
  } else {
    const first = kline.kline[0];
    const last = kline.kline[kline.kline.length - 1];
    items.push({
      id: "kline.summary",
      label: "K线",
      value: `共 ${kline.kline.length} 根（${String(first?.[0] ?? NA)} ~ ${String(last?.[0] ?? NA)}），最新交易日 ${kline.klineDate}`,
    });
    // 元组顺序：[date, open, close, low, high, volume]（close 在 low/high 之前 —— ECharts 约定）
    items.push({
      id: "kline.latest",
      label: "K线·最近一根",
      value: `${String(last?.[0] ?? NA)} 开${fmt("price", last?.[1])} 收${fmt("price", last?.[2])} 低${fmt("price", last?.[3])} 高${fmt("price", last?.[4])} 量${fmt("int", last?.[5])}`,
    });
  }

  // ── 类别先验（手工文案；取不到类别就明说）────────────────────────────────
  const cat =
    (typeof hit?.row.category === "string" && hit.row.category) ||
    (typeof hit?.row.rec_category === "string" && hit.row.rec_category) ||
    (typeof apps[0]?.category === "string" && apps[0].category) ||
    "";
  if (cat && PRIORS[cat]) {
    items.push({ id: `prior.${cat}`, label: `类别先验·${cat}`, value: PRIORS[cat] });
  } else if (cat) {
    missing.push("prior");
    items.push({ id: `prior.${cat}`, label: `类别先验·${cat}`, value: `${NA}（该桶无手工先验文案）` });
  }
  // 评分口径提示（2026-11-04）：主表不再给评分，模型不得自己把旧分数读成强弱信号。
  items.push({ id: "caveat.score", label: "评分口径", value: SCORE_CAVEAT });

  return { symbol, items, missing };
}

/** 该票是否真的有任何素材（决定 404 还是继续解读）。 */
export function hasAnySource(m: ExplainMaterials): boolean {
  return !m.missing.includes("regionRow") || !m.missing.includes("kline");
}

export { rowSymbol };