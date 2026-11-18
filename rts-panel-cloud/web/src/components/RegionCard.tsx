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
  /** 区块副标题（**一句短文**：口径差异 / 观察面说明）。长口径串走 `mouth`。 */
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
  /** 长口径正文（v7）：默认收成「口径 ▾」一行，点开才铺原文。
   *
   *  ⚠ 为什么要收起来：段副标题是**逐字契约串**（`view.model.hot_subtitle` 等，
   *  含排序键与层级词表），以前直接铺在标题下，一段 4 行公式墙 —— 屏幕上像日志。
   *  收起不删字：原文仍逐字渲染，只是先不占版面（守卫仍查源码里的原串）。 */
  mouth?: string;
  /** 分段区块（沪深飙升）：一段一个子表，标题由调用方给；`note` 同 `mouth`（段级）。 */
  segments?: { title: string; note?: string; rows: Row[]; profile: Profile }[];
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
  /** 本轮 0 行时**默认收起**（v7）：空区块只留一行头，点「展开」才看空态说明。
   *
   *  为什么：以前 `0 行` 的空态占掉整块版面（标题 + 一行说明 + 大留白），
   *  而它恰恰是最不需要注意的那一块。收起后仍可展开 —— 空态说明不是删掉，是让位。 */
  foldWhenEmpty?: boolean;
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
  mouth,
  foldWhenEmpty = false,
}: Props) {
  // 临时排序状态由卡片持有：表头点击切换，再点同列还原；切区块/刷新不改载荷，
  // 故无需持久化（FR-V2 的「临时」语义）。段区块共用一个 sortKey（各段独立可排）。
  const [sortKey, setSortKey] = useState<string | null>(null);
  const toggle = (key: string) => setSortKey((prev) => (prev === key ? null : key));

  const isEmpty = !segments && rows.length === 0;
  const foldable = foldWhenEmpty && isEmpty;
  // 空区块的展开是**卡片内部**状态：它的默认收起不写 localStorage —— 否则
  // 「今天回捞恰好有票」时会被上一轮存下的折叠态挡住，看着像没数据。
  const [emptyOpen, setEmptyOpen] = useState(false);
  const showBody = foldable ? emptyOpen : !collapsed;

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
        {mouth ? (
          <details className="region-mouth">
            <summary>口径</summary>
            <p>{mouth}</p>
          </details>
        ) : null}
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
          aria-expanded={showBody}
          aria-controls={bodyId}
          aria-label={`${showBody ? "折叠" : "展开"} ${title}`}
          onClick={() => (foldable ? setEmptyOpen((v) => !v) : onToggleCollapse?.())}
        >
          <span className="chevron" aria-hidden="true" />
        </button>
      </div>
    </header>
  );

  return (
    <section
      id={`anchor-${anchorId}`}
      className={`region-card${active ? " is-active" : ""}${foldable && !emptyOpen ? " is-empty-folded" : ""}`}
      data-collapsed={showBody ? "false" : "true"}
      aria-label={title}
    >
      {head}

      <div className="card-body" id={bodyId}>
        {/* inert：折叠后内容退出 Tab 顺序、不进无障碍树（原生属性，Chromium 支持）。 */}
        <div
          className="card-body-inner"
          inert={!showBody}
          aria-hidden={!showBody || undefined}
        >
          {segments ? (
            segments.map((seg) => (
              <div key={seg.title} className="segment-block">
                <h3 className="region-segtitle">{seg.title}</h3>
                {seg.note ? (
                  <details className="region-mouth seg-mouth">
                    <summary>口径</summary>
                    <p>{seg.note}</p>
                  </details>
                ) : null}
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
