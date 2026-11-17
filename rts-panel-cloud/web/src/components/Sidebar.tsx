/**
 * 侧边导航（v2 §5.6）：图标 + 文字、选中态左侧品牌色条、折叠态 tooltip、
 * 点击滚动到区块、高亮跟随仍复用 `App.tsx` 的 `scrollMainTo` / `useActiveSection`。
 *
 * 两个设计要点（不变）：
 * 1. **导航项文案一律用区块/段标题**，不用列名 —— `components/**` 由
 *    `test_panel_web_hygiene.py` grep 列名，写成「涨幅最高的票」会直接红。
 * 2. 导航是**锚点滚动**（`#anchor-<key>`），不是第二个路由器；区块顺序是设计规定的
 *    固定顺序，前端不重排（G5 只读镜像）。
 *
 * v2：折叠态只留图标（不再是两字缩写），所有入口带 `title` + 自绘 tooltip；
 * 折叠状态与卡片/分段共享 `lib/uiState.ts` 的单一 localStorage 键。
 */

import { NAV_ICONS } from "./icons";

/** 导航项：`key` 同时是锚点 id 与视觉标识来源。 */
export type NavItem = { key: string; label: string; hint?: string };

/**
 * 区块清单。顺序 = 页面物理顺序（与终端一致，前端不重排）。
 * ⚠ 文案与 `App.tsx` 的 `TITLES` 同源；改这里要同批改那边。
 */
export const NAV_ITEMS: NavItem[] = [
  { key: "pool", label: "v1 池选", hint: "新票优先·类别优先·排名升序·资金流降序" },
  { key: "hist", label: "v1 回捞", hint: "前 N 日进过 v1 · 今日回调到位" },
  { key: "hot", label: "沪深飙升", hint: "榜内飙升 / 榜外异动 分段并列" },
  { key: "gate", label: "飞书过门", hint: "下一张卡会推什么（非已推内容）" },
];

/** 各区块的行数徽章值（`App.tsx` 从载荷行集合算好传入；纯计数，不评价）。 */
export type NavCounts = { pool: number; hist: number; hot: number; gate: number };

type Props = {
  /** 当前所在区块（用于高亮）。由 `App.tsx` 按滚动位置维护。 */
  active: string | null;
  onNavigate: (key: string) => void;
  counts?: NavCounts;
  /** 折叠状态（v2：由 App 持有并持久化）。 */
  collapsed: boolean;
  onToggle: () => void;
};

export default function Sidebar({ active, onNavigate, counts, collapsed, onToggle }: Props) {
  const tipFor = (item: NavItem): string => {
    const n = counts ? counts[item.key as keyof NavCounts] : undefined;
    return typeof n === "number" && n > 0 ? `${item.label} · ${n} 行` : `${item.label} · 本轮无数据`;
  };

  return (
    <aside
      className={`sidebar${collapsed ? " is-collapsed" : ""}`}
      aria-label="区块导航"
    >
      <div className="sidebar-label">
        <span className="label-text">区块 / SECTIONS</span>
        <span className="label-mark" />
      </div>

      <nav className="nav" aria-label="区块">
        {NAV_ITEMS.map((item) => {
          const isActive = active === item.key;
          const n = counts ? counts[item.key as keyof NavCounts] : undefined;
          const Icon = NAV_ICONS[item.key];
          const hasRows = typeof n === "number" && n > 0;
          return (
            <button
              key={item.key}
              type="button"
              aria-current={isActive ? "location" : undefined}
              data-tip={tipFor(item)}
              title={tipFor(item)}
              onClick={() => onNavigate(item.key)}
              className={`nav-item${isActive ? " is-active" : ""}`}
            >
              {Icon ? <Icon /> : null}
              <span className="nav-text">{item.label}</span>
              <span className="nav-count" aria-label={hasRows ? `${n} 只` : "本轮无数据"}>
                {hasRows ? n : "—"}
              </span>
            </button>
          );
        })}
      </nav>

      <div className="sidebar-foot">
        {/* ⚠ v4：删掉这里的「本机 · 只读」徽章。
            顶栏右侧已有同文案徽章，页脚也写了「本机只读镜像」——同一句话三处出现，
            而这句是**应用级属性**（只读镜像），不是区块级信息，
            放在区块导航的页脚里既错位又重复。只读属性归顶栏那一处唯一持有。 */}
        <button
          type="button"
          className="sidebar-toggle"
          onClick={onToggle}
          aria-expanded={!collapsed}
          aria-label={collapsed ? "展开导航" : "收起导航"}
          data-tip={collapsed ? "展开导航" : "收起导航"}
          title={collapsed ? "展开导航" : "收起导航"}
        >
          <span className="toggle-icon" aria-hidden="true" />
        </button>
      </div>
    </aside>
  );
}
