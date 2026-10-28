/**
 * 区块卡片（2026-10 UI 重构）：把 `RegionTable` 包进卡片，并负责「临时排序」状态的
 * 持有与**显式标注**（FR-V2：临时排序必须标出来，不得静默重排）。
 *
 * 2026-10 重构的两条版面纪律：
 *
 * 1. **标题两级化**：卡片头部只显示「短主标题」（如 `v1 池选` / `沪深飙升`），
 *   完整口径长标题（设计规定文案，含排序键说明）收进副行弱化展示 —— 长标题不再
 *   与数据争视觉权重。区块标题字符串**逐字保留**（`tests/test_panel_web_cols.py`
 *   的 `test_region_titles_match_terminal_wording` 从 `App.tsx` 读 `TITLES`，
 *   故完整串必须在源码里原样出现：副行渲染的正是 `TITLES.*` 去掉 `◆` 前缀后的
 *   全串，不重措辞）。
 * 2. **空态文案逐字沿用终端**：`—（本轮无数据）`。`RegionTable` 内部已有同一句，
 *   这里只在**没有 segment 时**渲染同一句（分段区块整体为空），不得改写。
 *
 * 冻结首列仍是纯 CSS `position: sticky`，只作用于表格自身横向滚动容器内。
 */

import { useState } from "react";

import type { ColSpec, Row } from "@/api/types";
import type { Profile } from "@/lib/cols";
import RegionTable from "./RegionTable";

/** 空区块占位文案 —— 与 `RegionTable` 的 `EMPTY_TEXT` 逐字一致（不得改写）。 */
const EMPTY_TEXT = "—（本轮无数据）";

type Props = {
  /** 区块短主标题（卡片头部的视觉锚点；完整口径串见 `fullTitle`）。 */
  title: string;
  /** 完整口径标题（`App.tsx` 的 `TITLES.*`，设计规定文案，逐字渲染在副行）。 */
  fullTitle?: string;
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
  /** 右侧头部行数徽章（「本轮 N 只」，纯计数事实，不评价）。 */
  count?: number;
};

export default function RegionCard({
  title,
  fullTitle,
  sub,
  anchorId,
  specs,
  rows,
  profile,
  marks,
  segments,
  emptyNote,
  count,
}: Props) {
  // 临时排序状态由卡片持有：表头点击切换，再点同列还原；切区块/刷新不改载荷，
  // 故无需持久化（FR-V2 的「临时」语义）。段区块共用一个 sortKey（各段独立可排）。
  const [sortKey, setSortKey] = useState<string | null>(null);
  const toggle = (key: string) => setSortKey((prev) => (prev === key ? null : key));

  const head = (
    <header className="region-head">
      <div className="flex min-w-0 flex-wrap items-baseline gap-x-2 gap-y-0.5">
        <h2 className="region-title">{title}</h2>
        {typeof count === "number" ? <span className="region-count">{count}</span> : null}
        {sortKey ? (
          <span className="region-sortnote" role="status">
            临时排序
          </span>
        ) : null}
      </div>
      {fullTitle && fullTitle !== title ? (
        <p className="region-fulltitle">
          {fullTitle.startsWith("◆ ") ? fullTitle.slice(2) : fullTitle}
        </p>
      ) : null}
      {sub ? <p className="region-sub">{sub}</p> : null}
    </header>
  );

  return (
    <section
      id={`anchor-${anchorId}`}
      className="region-card"
      aria-label={title}
    >
      {head}

      {sortKey ? (
        <button type="button" className="region-sortreset" onClick={() => setSortKey(null)}>
          还原顺序
        </button>
      ) : null}

      {segments ? (
        segments.map((seg) => (
          <div key={seg.title} className="flex flex-col gap-1">
            <h3 className="region-segtitle">{seg.title}</h3>
            {seg.rows.length === 0 ? (
              <p className="text-[12px] leading-7 text-text-dim">{EMPTY_TEXT}</p>
            ) : (
              <RegionTable
                specs={specs}
                rows={seg.rows}
                profile={seg.profile}
                marks={marks}
                frozen
                sortKey={sortKey}
                onSortToggle={toggle}
              />
            )}
          </div>
        ))
      ) : rows.length === 0 ? (
        <p className="text-[12px] leading-7 text-text-dim">{emptyNote ?? EMPTY_TEXT}</p>
      ) : (
        <RegionTable
          specs={specs}
          rows={rows}
          profile={profile}
          marks={marks}
          frozen
          sortKey={sortKey}
          onSortToggle={toggle}
        />
      )}
    </section>
  );
}
