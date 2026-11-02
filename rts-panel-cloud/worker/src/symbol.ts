/**
 * symbol 口径的**单一来源**（2026-11-02 提取，同日补前缀一致性）。
 *
 * 之前 `SYMBOL_RE` 只写在 `routes/ingest.ts` 里且是 `/^\d{6}$/` —— 与真实键格式
 * （`SZ301171` / `SH600036`）不符，导致每轮 stocks 全被丢弃、`kline_cache` 恒空。
 * 现在写端点（ingest）与读端点（stock / explain）共用本模块，避免「两份正则各自漂移」。
 *
 * 合法形式：`(SH|SZ|BJ)?\d{6}`。**不归一化**（不去前缀、不统一大小写）：键要能被
 * web 详情链接与 regions 行原样查到；小写 / 其他市场前缀 / 长度不符一律拒绝 ——
 * 放行只会存进永远查不到的行。
 *
 * ⚠ 2026-11-02 追加：**前缀与代码段必须一致**（交叉审查复核项 3）。
 *   原正则只验形状不验一致性，`SH300001`（上交所前缀 + 深交所创业板代码）能通过并入库，
 *   之后还会被 `stock.ts` / `explain.ts` 精确匹配到、喂进模型 —— 一个永远不该存在的键。
 *
 * ⚠ **不要把本模块改回 `/^\d{6}$/` 当作「回滚」**：它被三个端点共用，
 *   改回去会同时重新引入「kline_cache 恒空」缺陷并让两个读端点对带前缀 symbol 全返 400。
 *   详见 `docs/specs/ai-explain.md` §6 已知项。
 */
export const SYMBOL_RE = /^(?:SH|SZ|BJ)?\d{6}$/;

/**
 * 交易所前缀 → 该所代码首位集合（**宽松形状校验，不是编码册**）。
 *
 * 只做到「明显不可能的组合一定拒」这一层：
 * - `SH`：6（主板/科创板）、9（B 股）、5（基金）
 * - `SZ`：0（主板）、3（创业板）、2（B 股）、1（基金）
 * - `BJ`：4（新三板/北交所）、8、9
 *
 * 刻意不做到「精确段位」（如 688 才是科创板）：段位表会随市场扩容变化，
 * 写死会把未来新增的合法段位误杀 —— 而越严的校验一旦误杀，表现是「整票数据静默消失」。
 */
const PREFIX_SEGMENT: Record<string, readonly string[]> = {
  SH: ["6", "9", "5"],
  SZ: ["0", "3", "2", "1"],
  BJ: ["4", "8", "9"],
};

export function isSymbol(v: unknown): v is string {
  if (typeof v !== "string") return false;
  if (!SYMBOL_RE.test(v)) return false;
  const m = /^(SH|SZ|BJ)(\d{6})$/.exec(v);
  // 裸 6 位：历史兼容（上报器可能不带前缀），不做交易所推断。
  if (!m) return true;
  return PREFIX_SEGMENT[m[1]].includes(m[2][0]);
}