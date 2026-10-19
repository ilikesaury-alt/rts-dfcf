/**
 * 侧边导航（2026-10 桌面 UI 现代化 · Slice 2）。
 *
 * 两个设计要点：
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
 * 面板**不新增**任何结论：没有「最值得看的票」「推荐先看哪区」这类排序，
 *    导航顺序 = 区块在页面里的物理顺序，一一对应。
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

type Props = {
  /** 当前所在区块（用于高亮）。由 `App.tsx` 用 IntersectionObserver 维护。 */
  active: string | null;
  onNavigate: (key: string) => void;
};

export default function Sidebar({ active, onNavigate }: Props) {
  return (
    <nav className="flex h-full flex-col gap-1 overflow-y-auto p-2" aria-label="区块导航">
      <div className="px-2 py-1 text-[11px] tracking-wide text-text-dim uppercase">区块</div>
      {NAV_ITEMS.map((item) => {
        const isActive = active === item.key;
        return (
          <button
            key={item.key}
            type="button"
            aria-current={isActive ? "true" : undefined}
            onClick={() => onNavigate(item.key)}
            className={cn(
              "flex flex-col items-start gap-0.5 rounded-md px-2 py-1.5 text-left transition-colors outline-none focus-visible:ring-2 focus-visible:ring-ring",
              isActive ? "bg-white/8 text-text" : "text-text-dim hover:bg-white/5 hover:text-text",
            )}
          >
            <span className="text-[13px] font-medium">{item.label}</span>
            {item.hint ? <span className="text-[11px] leading-4 opacity-70">{item.hint}</span> : null}
          </button>
        );
      })}
    </nav>
  );
}
