/**
 * 区块卡片（2026-10 桌面 UI 现代化 · Slice 3）：把既有 `RegionTable` 包进
 * shadcn `Card`，并补两处**纯展示**增强 —— 表头 sticky、横向滚动冻结首列。
 *
 * 三条纪律（改这个文件前必读）：
 *
 * 1. **列头与列数仍只认下发的 `colSpec`**。本文件只写区块标题/分段标题/空态文案，
 *    **不出现任何列名**（`tests/test_panel_web_hygiene.py` grep 守卫）。
 *    取值与标记一律走 `RegionTable` → `lib/cols.ts`，不在此重算。
 *
 * 2. **空态文案逐字沿用终端**：`—（本轮无数据）`。`RegionTable` 内部已有同一句，
 *    这里只在**没有 segment 时**渲染同一句（分段区块整体为空），不得改写。
 *
 * 3. **冻结首列是纯 CSS `position: sticky`**，只作用于表格自身横向滚动容器内的
 *    单元格：与 `react-resizable-panels` 的分隔条正交，不会互相推挤
 *    （CONTEXT.md R3 风险点的应对）。冻结的是**展示顺序上的第一列**
 *    （即 `specs[0]`，终端同款为 `#`），不改变任何排序/取值口径。
 */

import type { ColSpec, Row } from "@/api/types";
import type { Profile } from "@/lib/cols";
import RegionTable from "./RegionTable";

/** 空区块占位文案 —— 与 `RegionTable` 的 `EMPTY_TEXT` 逐字一致（不得改写）。 */
const EMPTY_TEXT = "—（本轮无数据）";

type Props = {
  /** 区块标题（`App.tsx` 的 `TITLES.*`，设计规定文案）。 */
  title: string;
  /** 区块副标题（口径说明，逐字来自终端 `hot_subtitle`/`offboard_subtitle` 同源）。 */
  sub?: string;
  /** 锚点 id（侧栏导航跳转用；渲染成 `anchor-<key>`，与 `Sidebar`/`useActiveSection` 同格式）。 */
  anchorId?: string;
  specs: ColSpec[];
  rows: Row[];
  profile: Profile;
  marks: {
    breakout: Record<string, boolean | string>;
    beauty: Record<string, string>;
  };
  /** 分段区块（沪深飙升）：一段一个子表，标题由调用方给。 */
  segments?: { title: string; rows: Row[]; profile: Profile }[];
  /** 空态时的说明（区块级，非列级）。 */
  emptyNote?: string;
};

export default function RegionCard({
  title,
  sub,
  anchorId,
  specs,
  rows,
  profile,
  marks,
  segments,
  emptyNote,
}: Props) {
  return (
    <section
      id={`anchor-${anchorId}`}
      className="flex flex-col gap-1 rounded-lg border border-border bg-card px-3 py-2 shadow-sm"
      aria-label={title}
    >
      <header className="flex flex-col gap-0.5">
        <h2 className="text-[13px] leading-5 font-semibold text-text">{title}</h2>
        {sub ? <p className="text-[11px] leading-4 text-text-dim">{sub}</p> : null}
      </header>

      {segments ? (
        segments.map((seg) => (
          <div key={seg.title} className="flex flex-col gap-1">
            <h3 className="mt-1 text-[12px] leading-4 font-medium text-text-dim">{seg.title}</h3>
            {seg.rows.length === 0 ? (
              <p className="text-[12px] leading-7 text-text-dim">{EMPTY_TEXT}</p>
            ) : (
              <RegionTable specs={specs} rows={seg.rows} profile={seg.profile} marks={marks} frozen />
            )}
          </div>
        ))
      ) : rows.length === 0 ? (
        <p className="text-[12px] leading-7 text-text-dim">{emptyNote ?? EMPTY_TEXT}</p>
      ) : (
        <RegionTable specs={specs} rows={rows} profile={profile} marks={marks} frozen />
      )}
    </section>
  );
}
