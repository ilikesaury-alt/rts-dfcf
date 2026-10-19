/**
 * 五区块通用表格（验收 2）：**列头与列数只认下发的 `colSpec`**。
 *
 * ⛔ 本文件（以及 `components/**` 下任何文件）**不得出现列名字面量**——
 * 列名只许写在 `lib/cols.ts` 的投影表里，由
 * `tests/test_panel_web_hygiene.py::test_table_components_have_no_hardcoded_column_labels`
 * 用 grep 守着。这里取值一律走 `cellsFor(row, specs, profile, ctx)`。
 *
 * 两处刻意与终端一致：
 * - **行尾标记在定宽列之外**（终端 `_table_row(...) + _suffix` 同款）：另起一个
 *   窄单元格承载 `新 / ⚡ / 稳 / ▼`，不混进任何一列的语义里。
 * - **空区块显 `—（本轮无数据）`**，不留一张只有列头的空表 —— 与终端「无结果整段跳过」
 *   同意图，但面板是固定版面，故留一句占位而不是整块消失。
 */

import type { ColSpec, Row } from "../api/types";
import { cn } from "../lib/utils";
import { cellsFor, rowMarks, rowSymbol, type Profile } from "../lib/cols";
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
   * 冻结首列 + sticky 表头（2026-10 桌面 UI 现代化 · Slice 3，**纯展示**）。
   *
   * sticky 只作用于**表格自身横向滚动容器内**的单元格，与 `react-resizable-panels`
   * 的分隔条正交（CONTEXT.md R3）。冻结的是 `specs[0]`（终端同款为 `#` 列）——
   * 只改视觉，不改排序、不改取值口径。
   */
  frozen?: boolean;
};

const alignClass = (align: string) => (align === "r" ? "cell-r" : align === "c" ? "cell-c" : "cell-l");

export default function RegionTable({ specs, rows, profile, marks, onNavigate, frozen }: Props) {
  if (rows.length === 0) {
    return <div className="region-empty">{EMPTY_TEXT}</div>;
  }

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
            {specs.map((spec, i) => (
              <th
                key={spec.key}
                className={cn(alignClass(spec.align), frozen && i === 0 && "sticky-col")}
                style={{ minWidth: `${spec.width}ch` }}
              >
                {spec.label}
              </th>
            ))}
            {/* 行尾标记列：**不在 colSpec 之内**（终端同款：标记追加在定宽列之外）。 */}
            <th className="marks-head" aria-label="标记" />
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => {
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
