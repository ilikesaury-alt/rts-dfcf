/**
 * 区块卡片（v2 §5.1）：浮起表面 / 独立 58px header / 可折叠（状态记 localStorage）。
 *
 * 保留的口径与挂载点（改前必看 BOUNDARIES §2）：
 * - `id={`anchor-${anchorId}`}`：`scrollMainTo` / `useActiveSection` 依赖；
 * - `region-card` / `region-head` / `region-title` / `region-fulltitle` / `region-sub` /
 *   `region-segtitle` / `region-count` / `region-sortnote` / `region-sortreset`：
 *   `index.css` 结构选择器与既有守卫依赖；
 * - `fullTitle.startsWith("◆ ") ? slice(2)`：`TITLES.*.full` 的 `◆ ` 前缀；
 * - 空态文案 `—（本轮无数据）` 由 `EmptyState` 渲染（`RegionTable` 内另有一份同文）。
 *
 * 折叠：body 用 `grid-template-rows: 1fr → 0fr` + 透明度 150ms；折叠后内容
 * `inert` + `aria-hidden`，退出 Tab 顺序（原生 inert 属性）。折叠按钮不是筛选/排序控件，
 * 只改变当前区块展开状态。
 *
 * 「临时排序」仍限 FR-V2 既有白名单且显式标注（不借重做之名扩大排序范围）。
 */

import { useState } from "react";

import type { ColSpec, Row } from "@/api/types";
import type { Profile } from "@/lib/cols";
import EmptyState from "./EmptyState";
import RegionTable from "./RegionTable";

type Props = {
  /** 区块短主标题（卡片头部的视觉锚点；完整口径串见 `fullTitle`）。 */
  title: string;
  /** 完整口径标题（`App.tsx` 的 `TITLES.*`，设计规定文案，逐字渲染在副行）。 */
  fullTitle?: string;
  /** 区块副标题（口径说明）。 */
  sub?: string;
  /** 锚点 id（侧栏导航跳转用；渲染成 `anchor-<key>`）。 */
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
  /** 头部标签（如「主线」「分段并列」），纯展示短词。 */
  tag?: string;
  /** 右侧头部行数徽章（「本轮 N 行」，纯计数事实，不评价）。 */
  count?: number;
  /** 卡片是否处于当前导航高亮区。 */
  active?: boolean;
  /** 折叠状态（由 App 持有并写 localStorage）。 */
  collapsed?: boolean;
  /** 折叠切换（写 localStorage）。 */
  onToggleCollapse?: () => void;
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
  tag,
  count,
  active = false,
  collapsed = false,
  onToggleCollapse,
}: Props) {
  // 临时排序状态由卡片持有：表头点击切换，再点同列还原；切区块/刷新不改载荷，
  // 故无需持久化（FR-V2 的「临时」语义）。段区块共用一个 sortKey（各段独立可排）。
  const [sortKey, setSortKey] = useState<string | null>(null);
  const toggle = (key: string) => setSortKey((prev) => (prev === key ? null : key));

  const bodyId = `body-${anchorId ?? title}`;
  const head = (
    <header className="region-head">
      <span className="card-accent" aria-hidden="true" />
      <div className="card-title-wrap">
        <h2 className="region-title">
          {title}
          {tag ? <span className="card-tag">{tag}</span> : null}
          {sortKey ? (
            <span className="region-sortnote" role="status">
              临时排序
            </span>
          ) : null}
        </h2>
        {fullTitle && fullTitle !== title ? (
          <p className="region-fulltitle">
            {fullTitle.startsWith("◆ ") ? fullTitle.slice(2) : fullTitle}
          </p>
        ) : null}
        {sub ? <p className="region-sub">{sub}</p> : null}
      </div>
      <div className="card-head-actions">
        {sortKey ? (
          <button type="button" className="region-sortreset" onClick={() => setSortKey(null)}>
            还原顺序
          </button>
        ) : null}
        {typeof count === "number" ? (
          <span className="region-count">
            {count} <em>本轮行</em>
          </span>
        ) : null}
        <button
          type="button"
          className="card-toggle"
          aria-expanded={!collapsed}
          aria-controls={bodyId}
          aria-label={`${collapsed ? "展开" : "折叠"} ${title}`}
          onClick={onToggleCollapse}
        >
          <span className="chevron" aria-hidden="true" />
        </button>
      </div>
    </header>
  );

  return (
    <section
      id={`anchor-${anchorId}`}
      className={`region-card${active ? " is-active" : ""}`}
      data-collapsed={collapsed ? "true" : "false"}
      aria-label={title}
    >
      {head}

      <div className="card-body" id={bodyId}>
        {/* inert：折叠后内容退出 Tab 顺序、不进无障碍树（原生属性，Chromium 支持）。 */}
        <div
          className="card-body-inner"
          inert={collapsed}
          aria-hidden={collapsed || undefined}
        >
          {segments ? (
            segments.map((seg) => (
              <div key={seg.title} className="segment-block">
                <h3 className="region-segtitle">{seg.title}</h3>
                {seg.rows.length === 0 ? (
                  <EmptyState segment />
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
            <EmptyState />
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
        </div>
      </div>
    </section>
  );
}
