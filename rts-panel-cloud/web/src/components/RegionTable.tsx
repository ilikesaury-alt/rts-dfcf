/**
 * 五区块通用表格（验收 2）：**列头与列数只认下发的 `colSpec`**。
 *
 * ⛔ 本文件（以及 `components/**` 下任何文件）**不得出现列名字面量** ——
 *   列名只许写在 `lib/cols.ts` 的投影表里，由 grep 守卫守着。这里取值一律走
 *   `cellsFor(row, specs, profile, ctx)`；表头一律渲染 `spec.label`。
 *
 * v2 表现层（不改变任何列集合 / 取值口径 / 排序白名单）：
 * - 整行 hover 抬到 `--surface-3`、active 抬到 `--surface-pop`、右侧「查看 →」淡入；
 * - 整行 `role="link"` + `tabIndex=0`，点击 / Enter / Space 进详情（`lib/router.ts` hash）；
 * - 焦点环内缩 2px；行尾标记仍在 `colSpec` 之外的窄列；
 * - 冻结首列 sticky 与表头 sticky 不变。
 *
 * 「临时排序」仍限 FR-V2 既有白名单并由 `RegionCard` 显式标注（本轮不扩大）。
 */

import type { ColSpec, Row } from "../api/types";
import { cn } from "../lib/utils";
import { cellsFor, rowMarks, rowSymbol, SORTABLE, type Profile } from "../lib/cols";
import { openStock } from "../lib/router";
import EmptyState from "./EmptyState";

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
    return <EmptyState />;
  }

  const sortable = SORTABLE[profile] ?? [];
  const sorted = sortKey && sortable.includes(sortKey) ? sortRows(rows, sortKey, specs, profile) : rows;

  const open = (symbol: string, fromId?: string) => {
    if (!symbol) return;
    if (onNavigate) onNavigate(symbol);
    else openStock(symbol, fromId);
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
            const clickable = Boolean(symbol);
            return (
              <tr
                key={`${symbol}-${index}`}
                id={clickable ? `row-${symbol}` : undefined}
                className={cn("row", clickable && "row-clickable")}
                role={clickable ? "link" : undefined}
                aria-label={clickable ? `查看 ${symbol} 详情` : undefined}
                tabIndex={clickable ? 0 : undefined}
                onClick={() => open(symbol, clickable ? `row-${symbol}` : undefined)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    open(symbol, clickable ? `row-${symbol}` : undefined);
                  }
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
                    {cell.text === "—" ? <span className="nil">{cell.text}</span> : cell.text}
                  </td>
                ))}
                <td className="marks-cell">
                  {rowMarkList.map((mark) => (
                    <span key={mark.kind} className={`mark mark-${mark.kind}`}>
                      {mark.text}
                    </span>
                  ))}
                  {clickable ? (
                    <span className="row-cta" aria-hidden="true">
                      查看 →
                    </span>
                  ) : null}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
