/**
 * 侧边导航（2026-10 UI 重构：行数徽章 + 更紧凑的项高）。
 *
 * 两个设计要点（不变）：
 *
 * 1. **导航项文案一律用区块/段标题**（「v1 池选」「沪深飙升」…），**不用列名**。
 *    `tests/test_panel_web_hygiene.py::test_table_components_have_no_hardcoded_column_labels`
 *    对 `components/**` 下任何文件 grep 列名（`涨幅`/`评分`/`板块`/…）。该守卫**刻意**
 *    只扫 `components/`，所以本文件写在 `components/` 下就必须守规矩 —— 写成
 *    「涨幅最高的票」这类文案会直接让它红。
 *
 * 2. 导航是**锚点滚动**（`#anchor-<key>`），不是第二个路由器：本项目路由只有
 *    「列表 / 个股详情」两个页面（`lib/router.ts`），区块顺序是**设计规定的固定顺序**
 *    （`assemble.build_scan_view` 已排好，前端不另行排序）。加一个「页面级」路由
 *    会制造「面板自行决定区块集合」的口径，违反只读镜像纪律（G5）。
 *
 * 2026-10 新增：**行数徽章**。每项右侧显示该区块本轮行数（`v1 池选 10`），
 * 数字是**纯计数事实**（来自载荷行集合 / `gate.stats.passed`），零重算、零评价 ——
 * 盘中最常回答的问题是「这一轮哪里有东西」，徽章让它不用滚动就可见。
 */

import { cn } from "@/lib/utils";

/** 导航项：`key` 同时是锚点 id 与视觉标识来源。 */
export type NavItem = { key: string; label: string; hint?: string };

/**
 * 区块清单。顺序 = 页面物理顺序（与终端一致，前端不重排）。
 *
 * ⚠ 文案与 `App.tsx` 的 `TITLES` / 段标题同源；改这里要同批改那边（反之亦然），
 *   否则导航点的锚点与区块标题对不上。
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
};

export default function Sidebar({ active, onNavigate, counts }: Props) {
  return (
    <nav className="flex h-full flex-col gap-0.5 overflow-y-auto p-2" aria-label="区块导航">
      {NAV_ITEMS.map((item) => {
        const isActive = active === item.key;
        const n = counts ? counts[item.key as keyof NavCounts] : undefined;
        return (
          <button
            key={item.key}
            type="button"
            aria-current={isActive ? "true" : undefined}
            onClick={() => onNavigate(item.key)}
            className={cn(
              "flex items-center justify-between gap-2 rounded-md px-2 py-1.5 text-left transition-colors outline-none focus-visible:ring-2 focus-visible:ring-ring",
              isActive ? "bg-white/8 text-text" : "text-text-dim hover:bg-white/5 hover:text-text",
            )}
          >
            <span className="min-w-0">
              <span className="block truncate text-[13px] font-medium leading-5">{item.label}</span>
              {item.hint ? <span className="block truncate text-[11px] leading-4 opacity-60">{item.hint}</span> : null}
            </span>
            {typeof n === "number" && n > 0 ? (
              <span
                className="shrink-0 rounded-full bg-white/8 px-1.5 py-px text-[11px] tabular-nums leading-4 text-text-dim"
                aria-label={`${n} 只`}
              >
                {n}
              </span>
            ) : null}
          </button>
        );
      })}
    </nav>
  );
}
