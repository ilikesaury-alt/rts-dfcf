/**
 * 面板主体（2026-10 桌面 UI 现代化后）：状态条 / 侧边导航 / 可拖拽分栏 /
 * 四区块卡片 / 本轮计数图 / 过门区 / 页脚。
 *
 * 版面纪律（与终端同意图，与 design §7.1 的「终端 → 面板」映射一致）：
 * - 区块**顺序与终端一致**，前端**不另行排序**（FR-V2：排序已在
 *   `assemble.build_scan_view` 完成，面板只渲染）。
 * - 区块标题里写死的是**展示文案**（设计规定的那几句），不是列定义 —— 列定义一律来自
 *   下发的 `colSpec`（`components/**` 由 grep 守卫不得出现列名）。
 * - 面板**不新增**任何结论：没有短名单、没有综合判断、没有持仓建议，也没有「评分高=更
 *   可能大涨」这类暗示（G5）。
 *
 * 2026-10 版面变化（`docs/features/desktop-ui-modernize/CONTEXT.md`，纯展示层）：
 * - 左侧**可折叠侧边导航**（锚点滚动，**不是**第二个路由器 —— 路由仍只有
 *   「列表 / 个股详情」两个页面，`lib/router.ts`）；
 * - 主区与过门侧栏改为 **`react-resizable-panels` 可拖拽分栏**（窄屏仍堆叠）；
 * - 区块包进 `RegionCard`（表头 sticky + 横向滚动冻结首列，纯 CSS）；
 * - 新增 `StatsCharts`：**只消费已下发字段**的只读计数图，零新增 fetch。
 *
 * ⚠ 已知口径差异（不是 bug，但必须让人看见）：载荷的 `regions` 只有
 * `main / hist / hot / offboard` 四个键，**终端的「榜内异动」段不在契约里**
 * （加它要动 SCHEMA_VERSION，本轮冻结为 2）。故本区的标题写「分段并列」而非终端的
 * 「三段并列」—— 面板确实只有两段，标题不能替不存在的东西作保。
 */

import { useCallback, useEffect, useMemo, useState, lazy, Suspense } from "react";
import { Group as PanelGroup, Panel, Separator as PanelResizeHandle, useDefaultLayout } from "react-resizable-panels";

import type { ColSpec, RegionsResponse } from "./api/types";
import GatePanel from "./components/GatePanel";
import RegionCard from "./components/RegionCard";
import Sidebar, { NAV_ITEMS } from "./components/Sidebar";
import StatusBar from "./components/StatusBar";
import { useSnapshot } from "./lib/useSnapshot";
import { useRoute } from "./lib/router";
import Stock from "./pages/Stock";

/** 区块标题（设计规定文案；`—` 前缀的段标题见下方 build 函数，不得改用 `◆`）。 */
const TITLES = {
  pool: "◆ v1 池选 — 新票优先·类别优先·排名升序·资金流降序",
  hist: "◆ v1 回捞",
  hot: "◆ 沪深飙升 · 极有可能大涨",
} as const;

/** 段标题的括号正文：与 `scanner/view/model.py` 的 `hot_subtitle` / `offboard_subtitle` 逐字一致。 */
export const hotSubtitle = (n: number): string =>
  `（榜内创业板·当日动能+热度跃升（已涨）·排序=评分↓(排名上升35/涨幅25/价格15/量能25)·同分比排名上升·${n} 只）`;

export const offboardSubtitle = (n: number): string =>
  `（榜外创业板·非榜单来源·层级 T1=量先动·价未动 / T2=启动首日·排序=T2 启动首日→T1 量先动→量比→主力净占比·开盘 15 分内不产出(量比失真)·${n} 只·观察段·未回测）`;

const EMPTY_MARKS = { breakout: {}, beauty: {} };

function colsOf(snapshot: RegionsResponse, key: string): ColSpec[] {
  return snapshot.regions.cols?.[key] ?? [];
}

function RegionList({ snapshot }: { snapshot: RegionsResponse }) {
  const marks = snapshot.regions.marks ?? EMPTY_MARKS;
  const { main, hist, hot, offboard } = snapshot.regions;
  const flowFiltered = snapshot.ctx?.flowFiltered ?? 0;
  const warnings = snapshot.ctx?.warnings ?? [];

  return (
    <div className="flex flex-col gap-2.5">
      {warnings.length > 0 ? (
        <ul className="m-0 list-disc pl-4 text-lag-warn">
          {warnings.map((w) => (
            <li key={w}>{w}</li>
          ))}
        </ul>
      ) : null}
      {flowFiltered > 0 ? (
        <p className="m-0 text-lag-warn">▸ 资金流出已剔除 {flowFiltered} 只（全区域统一口径）</p>
      ) : null}

      <RegionCard
        anchorId={NAV_ITEMS[0].key}
        title={TITLES.pool}
        specs={colsOf(snapshot, "pool")}
        rows={main}
        profile="pool"
        marks={marks}
      />

      <RegionCard
        anchorId={NAV_ITEMS[1].key}
        title={TITLES.hist}
        sub="（前若干交易日进过 v1 · 今日回调到位 · 与上方口径独立）"
        specs={colsOf(snapshot, "hist")}
        rows={hist}
        profile="hist"
        marks={marks}
      />

      <RegionCard
        anchorId={NAV_ITEMS[2].key}
        title={TITLES.hot}
        sub="（创业板 · 分段并列·互不排名 · 与上方主线口径独立）"
        specs={colsOf(snapshot, "hot")}
        rows={hot}
        profile="hot"
        marks={marks}
        segments={[
          { title: `— 榜内飙升${hotSubtitle(hot.length)}`, rows: hot, profile: "hot" },
          { title: `— 榜外异动${offboardSubtitle(offboard.length)}`, rows: offboard, profile: "offboard" },
        ]}
      />
    </div>
  );
}

const MAIN_SCROLL_SELECTOR = "[data-scroll-root='main']";
const SIDE_SCROLL_SELECTOR = "[data-scroll-root='side']";

/** 找到**真正装有**该锚点的滚动容器。 */
function scrollContainerFor(el: HTMLElement): HTMLElement | null {
  for (const sel of [MAIN_SCROLL_SELECTOR, SIDE_SCROLL_SELECTOR]) {
    const box = document.querySelector<HTMLElement>(sel);
    if (box && box.contains(el)) return box;
  }
  return null;
}

function scrollMainTo(key: string) {
  const el = document.getElementById(`anchor-${key}`);
  if (!el) return;
  const box = scrollContainerFor(el);
  if (!box) return;
  // ⚠ 四个坑（2026-10-19 实测，Electron 44 / Chromium）：
  // 1. **不要**用 `box.scrollTo({behavior:"smooth"})`：这个环境里平滑滚动会被静默
  //    丢弃（`scrollTop` 一动不动、无报错）。瞬移赋值才可靠。
  // 2. **不要**用 rect 差值：点击那一刻容器可能还没布局完（右栏图表懒加载会改变
  //    高度），测出来的 delta 偏小，滚到半路就停（实测 249 而非 426）。
  //    改用 `offsetTop` 沿 offsetParent 链累加到容器 —— 这是**布局坐标**，
  //    不受「当前滚到哪」影响，也不需要容器已滚动稳定。
  // 3. **不要**再叠 `scrollIntoView`：它会盖掉上面算好的 `scrollTop`。
  // 4. 8px 呼吸间距：区块标题不紧贴容器顶边。
  let top = 0;
  for (let n: HTMLElement | null = el; n && n !== box; n = n.offsetParent as HTMLElement | null) {
    top += n.offsetTop;
  }
  box.scrollTop = Math.max(0, top - 8);
}

/** 侧栏高亮：当前「正在看」的区块 = 主区里最后一个顶部已滚过容器 1/4 线的区块。
 *
 *  ⚠ 为什么**不用** IntersectionObserver（2026-10-19 实测否掉）：
 *  1. 主区与右栏是两个滚动容器、各有一套观察器，而右栏那个高 536px 的
 *     `anchor-gate` 长期处于 intersecting ⇒ 它会把主区滚到哪都盖掉
 *     （现象：点「v1 池选」却高亮「飞书过门」）。要么给两套观察器分优先级，
 *     要么像现在这样只让主区发言。
 *  2. `rootMargin` 负边距那条「带」在高容器里很难调：带落在哪个区块上纯看
 *     滚动位置与各区块高度的比例，实测「滚到沪深飙升却高亮 v1 回捞」。
 *  直接按滚动位置算「最后一个越过阈值线的区块」是确定性的，易验、易解释。
 *
 *  ⚠ 另两个必须配对的条件（少一个就静默失效，而这是纯交互退化、无测试会红）：
 *  - 首轮 `snapshot` 还是 null ⇒ 锚点**尚不存在**，用 `MutationObserver` 等它们挂上来；
 *  - 只监听**主区**容器的 scroll（区块导航的四个锚点里三个在主区；`gate` 在右栏，
 *    由点击导航时直接置高亮，见 `App` 里的 `onNavigate`）。
 */
function useActiveSection(keys: string[]): string | null {
  const [active, setActive] = useState<string | null>(null);
  const keysKey = keys.join(",");

  useEffect(() => {
    if (keys.length === 0) return;
    let mo: MutationObserver | null = null;
    let detach: (() => void) | null = null;

    const attach = (): boolean => {
      const box = document.querySelector<HTMLElement>(MAIN_SCROLL_SELECTOR);
      if (!box) return false;
      const anchors = keys
        .map((k) => ({ key: k, el: document.getElementById(`anchor-${k}`) }))
        .filter((a): a is { key: string; el: HTMLElement } => a.el !== null && box.contains(a.el));
      if (anchors.length === 0) return false;

      const recompute = () => {
        // 阈值线：容器顶部往下 1/4 处。区块顶部越过它即「已开始看这个区块」。
        const line = box.getBoundingClientRect().top + box.clientHeight * 0.25;
        let current: string | null = anchors[0].key;
        for (const a of anchors) {
          if (a.el.getBoundingClientRect().top <= line) current = a.key;
        }
        setActive(current);
      };

      recompute();
      box.addEventListener("scroll", recompute, { passive: true });
      window.addEventListener("resize", recompute);
      detach = () => {
        box.removeEventListener("scroll", recompute);
        window.removeEventListener("resize", recompute);
      };
      return true;
    };

    if (!attach()) {
      // 锚点还没渲染出来（首轮快照未到）：等 DOM 长出它们再挂监听，挂上即断开。
      mo = new MutationObserver(() => {
        if (attach()) {
          mo?.disconnect();
          mo = null;
        }
      });
      mo.observe(document.body, { childList: true, subtree: true });
    }

    return () => {
      detach?.();
      mo?.disconnect();
    };
  }, [keys, keysKey]);

  return active;
}

/** 主区分栏的 id：`useDefaultLayout` 用它把「用户拖出来的宽度」存进 localStorage。 */
const PANEL_GROUP_ID = "rts-panel-main";

/**
 * 只读统计图（Slice 4）**懒加载**。
 *
 * 为什么拆出去：Recharts 连带 d3 打点库，压缩后仍占主 bundle 的一半以上，而面板的
 * 核心价值是四区块表格（首屏就要看到）。懒加载后表格先行、图表随后，首屏 JS 从
 * ~700KB 降到 ~400KB。它消费的仍是已下发字段（零新增 fetch，G5 不变）。
 */
const StatsCharts = lazy(() => import("./components/StatsCharts"));

export default function App() {
  const route = useRoute();
  const { snapshot, meta, error } = useSnapshot();
  const [now, setNow] = useState(() => Date.now());
  const [collapsed, setCollapsed] = useState(false);

  // 分栏宽度持久化（react-resizable-panels v4：`defaultLayout` + `onLayoutChanged`，
  // 取代 v2 的 `autoSaveId`）。只存 layout，不存任何业务状态。
  const { defaultLayout, onLayoutChanged } = useDefaultLayout({ id: PANEL_GROUP_ID });

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 30_000);
    return () => window.clearInterval(timer);
  }, []);

  const configured = snapshot?.ctx?.config?.refreshInterval;
  const intervalMs = (typeof configured === "number" && configured > 0 ? configured : 60) * 1000;

  const navKeys = useMemo(() => NAV_ITEMS.map((i) => i.key), []);
  const active = useActiveSection(navKeys);
  // `gate` 锚点在右栏（不在主滚动容器里），主区的 scroll 监听算不到它 ⇒ 点导航
  // 进 gate 时直接置高亮，避免「滚到了却不高亮」。
  const [gateActive, setGateActive] = useState(false);

  const scrollToSection = useCallback(
    (key: string) => {
      if (key === "gate") setGateActive(true);
      else setGateActive(false);
      scrollMainTo(key);
    },
    [],
  );
  const activeKey = gateActive ? "gate" : active;

  return (
    <div className="flex h-dvh flex-col overflow-hidden bg-bg text-text">
      <StatusBar meta={meta} snapshot={snapshot} error={error} intervalMs={intervalMs} now={now} />

      {route.name === "stock" ? (
        <main className="min-w-0 flex-1 overflow-y-auto p-3">
          <Stock symbol={route.symbol} />
        </main>
      ) : (
        <div className="flex min-h-0 flex-1">
          {/* ── 侧边导航（可折叠）────────────────────────────────────────── */}
          <aside
            className={`flex shrink-0 flex-col border-r border-border ${collapsed ? "w-11" : "w-52"}`}
            aria-label="侧边导航"
          >
            <div className="flex h-8 items-center justify-end border-b border-border px-1">
              <button
                type="button"
                onClick={() => setCollapsed((v) => !v)}
                className="rounded px-1.5 py-0.5 text-[12px] text-text-dim outline-none hover:bg-white/5 hover:text-text focus-visible:ring-2 focus-visible:ring-ring"
                aria-expanded={!collapsed}
                aria-label={collapsed ? "展开导航" : "收起导航"}
              >
                {collapsed ? "»" : "«"}
              </button>
            </div>
            {collapsed ? (
              <nav className="flex flex-col gap-1 p-1.5" aria-label="区块导航">
                {NAV_ITEMS.map((item) => (
                  <button
                    key={item.key}
                    type="button"
                    onClick={() => {
                      setCollapsed(false);
                      scrollToSection(item.key);
                    }}
                    title={item.label}
                    className={`rounded px-1 py-1.5 text-[12px] outline-none hover:bg-white/5 hover:text-text focus-visible:ring-2 focus-visible:ring-ring ${
                      activeKey === item.key ? "bg-white/8 text-text" : "text-text-dim"
                    }`}
                  >
                    {item.label.slice(0, 2)}
                  </button>
                ))}
              </nav>
            ) : (
              <Sidebar active={activeKey} onNavigate={scrollToSection} />
            )}
          </aside>

          {/* ── 主区：区块列表 + 右侧「本轮计数 / 过门」侧栏，可拖拽分栏 ── */}
          {snapshot ? (
            <PanelGroup
              id={PANEL_GROUP_ID}
              orientation="horizontal"
              defaultLayout={defaultLayout}
              onLayoutChanged={onLayoutChanged}
              className="min-w-0 flex-1"
            >
              {/* Panel 自身是 flex 容器：内层 `<main>` 必须 `min-h-0` 才会被高度约束，
                  否则它会「内容多高就多高」地把 Panel 撑高、页面整体滚（分栏就废了）。 */}
              <Panel id="main-table" minSize={45} defaultSize={72} className="overflow-hidden">
                <main className="h-full min-h-0 overflow-y-auto p-3" data-scroll-root="main">
                  <RegionList snapshot={snapshot} />
                </main>
              </Panel>
              <PanelResizeHandle className="w-1 shrink-0 cursor-col-resize bg-border transition-colors hover:bg-text-dim" />
              <Panel id="side-stats" minSize={20} defaultSize={28} className="overflow-hidden">
                <aside
                  className="flex h-full min-h-0 flex-col gap-2.5 overflow-y-auto p-3"
                  data-scroll-root="side"
                >
                  <Suspense fallback={<div className="h-40 rounded-lg border border-border bg-card" aria-hidden />}>
                    <StatsCharts
                      stats={snapshot.regions.gate.stats}
                      counts={{
                        main: snapshot.regions.main.length,
                        hist: snapshot.regions.hist.length,
                        hot: snapshot.regions.hot.length,
                        offboard: snapshot.regions.offboard.length,
                      }}
                    />
                  </Suspense>
                  <GatePanel
                    gate={snapshot.regions.gate}
                    regions={{
                      main: snapshot.regions.main,
                      hist: snapshot.regions.hist,
                      hot: snapshot.regions.hot,
                      offboard: snapshot.regions.offboard,
                    }}
                  />
                </aside>
              </Panel>
            </PanelGroup>
          ) : (
            <main className="min-w-0 flex-1 overflow-y-auto p-3">
              <div className="flex flex-col gap-1.5 text-text-dim">
                {error ? `本地服务无响应（${error}）` : "等待扫描器首轮上报…"}
                <span className="text-[12px]">扫描器在本机运行；首轮（约 60s 内）完成后本页自动刷新。</span>
              </div>
            </main>
          )}
        </div>
      )}

      <footer className="shrink-0 border-t border-border px-3 py-2 text-text-dim">
        只读视图：不新增筛选、不新增排序、不新增判断。数据来自本机扫描器的最后一轮，页面每 {Math.round(intervalMs / 1000)}s 轮询一次。
      </footer>
    </div>
  );
}
