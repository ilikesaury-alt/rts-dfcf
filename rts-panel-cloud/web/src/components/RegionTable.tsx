/**
 * 五区块通用表格（验收 2）：**列头与列数只认下发的 `colSpec`**。
 *
 * ⛔ 本文件（以及 `components/**` 下任何文件）**不得出现列名字面量**——
 *   列名只许写在 `lib/cols.ts` 的投影表里，由
 *   `tests/test_panel_web_hygiene.py::test_table_components_have_no_hardcoded_column_labels`
 *   用 grep 守着。这里取值一律走 `cellsFor(row, specs, profile, ctx)`。
 *
 * 2026-10 UI 重构新增（纯展示层，均属 FR-V2 / 验收 2 既有授权范围）：
 * - **临时排序**：可排序列的表头渲染成按钮，点击按该列原始数值降序（再点还原）。
 *   可排集合 = `SORTABLE[profile]`（FR-V2 白名单：评分/涨幅/量比），排序状态由
 *   调用方（`RegionCard` → `App`）持有并**在区块标题旁显式标「临时排序」**——
 *   排序一经触达立刻可见，绝无静默重排。还原后恢复服务端下发的原始顺序。
 * - **键盘**：行 Tab 可达、Enter 打开详情（原有）；表头按钮原生焦点即可。
 * - **两级强调**：涨跌色只在 ≥5% 的行加粗（`cols.ts` `pctCell`），长列表不逐格喊。
 *
 * 两处刻意与终端一致（不变）：
 * - **行尾标记在定宽列之外**（终端 `_table_row(...) + _suffix` 同款）：另起一个
 *   窄单元格承载 `新 / ⚡ / 稳 / ▼`，不混进任何一列的语义里。
 * - **空区块显 `—（本轮无数据）`**，不留一张只有列头的空表 —— 与终端「无结果整段跳过」
 *   同意图，但面板是固定版面，故留一句占位而不是整块消失。
 */

import type { ColSpec, Row } from "../api/types";
import { cn } from "../lib/utils";
import { cellsFor, rowMarks, rowSymbol, SORTABLE, type Profile } from "../lib/cols";
import { stockHref } from "../lib/router";

const EMPTY_TEXT = "—（本轮无数据）";

type Props = {
  /** 列定义投影（来自 `regions.cols.*`，随载荷下发）。 */
  specs: ColSpec[];
  rows: Row[];
  profile: Profile;
  marks: {
    breakout: Record<string, boolean | string>;
    beauty: Record<string, string>;
  };
  onNavigate?: (symbol: string) => void;
  /**
   * 临时排序：`null` = 服务端原始顺序（默认）。列 key 必须在 `SORTABLE[profile]` 内。
   * 排序只重排**展示顺序**，不改任何取值口径；谁持有谁负责标注（`RegionCard`）。
   */
  sortKey?: string | null;
  /** 点击可排列表头（仅在 `SORTABLE[profile]` 内的列触发）。归属 `RegionCard` 持有并标注。 */
  onSortToggle?: (key: string) => void;
  /** 冻结首列 + sticky 表头（纯展示，2026-10 现代化保留）。 */
  frozen?: boolean;
};

const alignClass = (align: string) => (align === "r" ? "cell-r" : align === "c" ? "cell-c" : "cell-l");

/** 排序比较：有值的在前按降序，无值（`undefined`）恒排后（不伪造 0）。 */
function sortRows(rows: Row[], key: string, specs: ColSpec[], profile: Profile): Row[] {
  const idx = specs.findIndex((s) => s.key === key);
  if (idx < 0) return rows;
  const withVal: { row: Row; v: number }[] = [];
  const without: Row[] = [];
  for (const row of rows) {
    const cell = cellsFor(row, specs, profile, { index: 0, marks: { breakout: {}, beauty: {} } })[idx];
    if (cell && typeof cell.val === "number" && Number.isFinite(cell.val)) withVal.push({ row, v: cell.val });
    else without.push(row);
  }
  withVal.sort((a, b) => b.v - a.v);
  return [...withVal.map((x) => x.row), ...without];
}

export default function RegionTable({ specs, rows, profile, marks, onNavigate, sortKey, onSortToggle, frozen }: Props) {
  if (rows.length === 0) {
    return <div className="region-empty">{EMPTY_TEXT}</div>;
  }

  const sortable = SORTABLE[profile] ?? [];
  const sorted = sortKey && sortable.includes(sortKey) ? sortRows(rows, sortKey, specs, profile) : rows;

  const open = (symbol: string) => {
    if (!symbol) return;
    if (onNavigate) onNavigate(symbol);
    else window.location.hash = stockHref(symbol);
  };

  return (
    <div className="table-scroll">
      <table className="region-table">
        <thead>
          <tr>
            {specs.map((spec, i) => {
              const canSort = sortable.includes(spec.key);
              const isActive = canSort && sortKey === spec.key;
              return (
                <th
                  key={spec.key}
                  className={cn(alignClass(spec.align), frozen && i === 0 && "sticky-col")}
                  style={{ minWidth: `${spec.width}ch` }}
                  aria-sort={isActive ? ("descending" as const) : undefined}
                >
                  {canSort ? (
                    <button
                      type="button"
                      onClick={() => onSortToggle?.(spec.key)}
                      title="临时排序（点击切换 / 还原）"
                      className={cn("sort-head", isActive && "sort-active")}
                    >
                      {spec.label}
                      <span className="sort-arrow" aria-hidden>
                        {isActive ? "↓" : "↕"}
                      </span>
                    </button>
                  ) : (
                    spec.label
                  )}
                </th>
              );
            })}
            {/* 行尾标记列：**不在 colSpec 之内**（终端同款：标记追加在定宽列之外）。 */}
            <th className="marks-head" aria-label="标记" />
          </tr>
        </thead>
        <tbody>
          {sorted.map((row, index) => {
            const symbol = rowSymbol(row);
            const cells = cellsFor(row, specs, profile, { index, marks });
            const rowMarkList = rowMarks(row, profile, { index, marks });
            return (
              <tr
                key={`${symbol}-${index}`}
                className={cn("row", symbol && "row-clickable")}
                tabIndex={symbol ? 0 : undefined}
                onClick={() => open(symbol)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") open(symbol);
                }}
              >
                {cells.map((cell, i) => (
                  <td
                    key={specs[i].key}
                    className={cn(
                      alignClass(specs[i].align),
                      frozen && i === 0 && "sticky-col",
                      cell.tone && `tone-${cell.tone}`,
                      cell.strong && "cell-strong",
                    )}
                  >
                    {cell.text}
                  </td>
                ))}
                <td className="marks-cell">
                  {rowMarkList.map((mark) => (
                    <span key={mark.kind} className={`mark mark-${mark.kind}`}>
                      {mark.text}
                    </span>
                  ))}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
