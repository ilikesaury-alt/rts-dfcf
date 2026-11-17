/**
 * 面板主体（v2「有温度的精致」）：标题栏 / 本轮读数带 / 侧边导航 / 可拖拽分栏 /
 * 四区块卡片 / 过门区 / 产品语言页脚 / 个股详情。
 *
 * 版面纪律（与终端同意图，纯只读镜像）：
 * - 区块**顺序与终端一致**，前端**不另行排序**（排序已在 `assemble.build_scan_view`
 *   完成，面板只渲染）。列头只吃服务端下发 `colSpec`。
 * - 面板**不新增**任何结论：没有短名单、没有综合判断、没有持仓建议。
 * - 路由只有「列表 / 个股详情」两页（`lib/router.ts` 的 hash 路由）；侧栏是锚点滚动，
 *   **不是**第二个路由器。
 * - `TITLES.*.full` 三串与 `hotSubtitle` / `offboardSubtitle` 模板逐字守卫
 *   （`test_panel_web_cols.py`），Python `scanner/view/model.py` 是逐字真源，不动。
 *
 * v2 落地的交互（全部是视图层，零新增请求、零新增业务字段、零新增依赖）：
 * - 侧栏图标化 + 折叠态 tooltip + 强选中色条，复用 `scrollMainTo` / `useActiveSection`；
 * - 卡片折叠 / 侧栏折叠 / 分段位置写 `lib/uiState.ts` 的单一 localStorage 键；
 * - 表格整行 hover + 「查看 →」+ Enter/Space 进详情；返回按钮 / Esc 返回并归还焦点；
 * - 过门区滑动分段（← → / Home / End）；状态胶囊多态 + 一句解释弹层；
 * - 空态用纯 CSS 图形 + 口径说明 + `—（本轮无数据）`；删掉右栏遗留空卡片。
 *
 * ⚠ 已知口径差异（不是 bug，但必须让人看见）：载荷的 `regions` 只有
 * `main / hist / hot / offboard` 四个键，**终端的「榜内异动」段不在契约里**
 * （加它要动 SCHEMA_VERSION，本轮冻结为 2）。故本区标题写「分段并列」而非「三段并列」。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Group as PanelGroup, Panel, Separator as PanelResizeHandle, useDefaultLayout } from "react-resizable-panels";

import type { ColSpec, RegionsResponse } from "./api/types";
import GatePanel from "./components/GatePanel";
import EmptyState from "./components/EmptyState";
import RegionCard from "./components/RegionCard";
import Sidebar, { NAV_ITEMS, type NavCounts } from "./components/Sidebar";
import StatusBar, { deriveStatus } from "./components/StatusBar";
import { useSnapshot } from "./lib/useSnapshot";
import { consumeOpener, goToList, useRoute } from "./lib/router";
import { useUiState } from "./lib/uiState";
import Stock from "./pages/Stock";
import Tasks from "./pages/Tasks";

/**
 * 区块标题（设计规定文案，逐字守卫：`test_panel_web_cols.py::test_region_titles_match_terminal_wording`）。
 * 卡片头两级化 —— `short` 作视觉锚点、`full` 逐字渲染在弱化副行（守卫 grep 完整串，
 * 必须原样在此出现，不重措辞；`RegionCard` 会剥去 `◆ ` 前缀）。
 */
const TITLES = {
  pool: { short: "v1 池选", full: "◆ v1 池选 — 新票优先·类别优先·排名升序·资金流降序" },
  hist: { short: "v1 回捞", full: "◆ v1 回捞" },
  hot: { short: "沪深飙升", full: "◆ 沪深飙升 · 极有可能大涨" },
} as const;

/** 段标题的括号正文：与 `scanner/view/model.py` 的 `hot_subtitle` / `offboard_subtitle` 逐字一致。 */
export const hotSubtitle = (n: number): string =>
  `（榜内创业板·当日动能+热度跃升（已涨）·排序=评分↓(排名上升35/涨幅25/价格15/量能25)·同分比排名上升·量比=上报原值·${n} 只）`;

export const offboardSubtitle = (n: number): string =>
  `（榜外创业板·非榜单来源·层级 T1=量先动·价未动 / T2=启动首日·排序=T2 启动首日→T1 量先动→量比→主力净占比·开盘 5 分内不产出·量比按市场前置系数去偏·${n} 只·观察段·未回测）`;

const EMPTY_MARKS = { breakout: {}, beauty: {} };

function colsOf(snapshot: RegionsResponse, key: string): ColSpec[] {
  return snapshot.regions.cols?.[key] ?? [];
}

/** 个股名称：从本轮快照的行集合里读展示名（纯展示读，不新增请求/不重算口径）。 */
function nameFor(snapshot: RegionsResponse | null, symbol: string): string {
  if (!snapshot) return "";
  const r = snapshot.regions;
  const all = [
    ...r.main,
    ...r.hist,
    ...r.hot,
    ...r.offboard,
    ...r.gate.main,
    ...r.gate.hist,
    ...r.gate.hot,
    ...r.gate.offboard,
  ];
  for (const row of all) {
    const sym = (typeof row.symbol === "string" ? row.symbol : typeof row.code === "string" ? row.code : "") as string;
    if (sym === symbol && typeof row.name === "string") return row.name;
  }
  return "";
}

type RegionListProps = {
  snapshot: RegionsResponse;
  activeKey: string | null;
  collapsed: Record<string, boolean>;
  onToggle: (key: string) => void;
};

function RegionList({ snapshot, activeKey, collapsed, onToggle }: RegionListProps) {
  const marks = snapshot.regions.marks ?? EMPTY_MARKS;
  const { main, hist, hot, offboard } = snapshot.regions;
  const flowFiltered = snapshot.ctx?.flowFiltered ?? 0;
  const warnings = snapshot.ctx?.warnings ?? [];

  return (
    <div className="flex flex-col gap-[22px]">
      {warnings.length > 0 ? (
        <div className="ctx-note" role="status">
          {warnings.map((w) => (
            <div key={w}>{w}</div>
          ))}
        </div>
      ) : null}
      {flowFiltered > 0 ? (
        <p className="ctx-note">资金流出已剔除 {flowFiltered} 只 · 全区域统一口径</p>
      ) : null}

      <RegionCard
        anchorId={NAV_ITEMS[0].key}
        title={TITLES.pool.short}
        fullTitle={TITLES.pool.full}
        tag="主线"
        specs={colsOf(snapshot, "pool")}
        rows={main}
        profile="pool"
        marks={marks}
        count={main.length}
        active={activeKey === NAV_ITEMS[0].key}
        collapsed={Boolean(collapsed[NAV_ITEMS[0].key])}
        onToggleCollapse={() => onToggle(NAV_ITEMS[0].key)}
      />

      <RegionCard
        anchorId={NAV_ITEMS[1].key}
        title={TITLES.hist.short}
        fullTitle={TITLES.hist.full}
        sub="（前若干交易日进过 v1 · 今日回调到位 · 与上方口径独立）"
        specs={colsOf(snapshot, "hist")}
        rows={hist}
        profile="hist"
        marks={marks}
        count={hist.length}
        active={activeKey === NAV_ITEMS[1].key}
        collapsed={Boolean(collapsed[NAV_ITEMS[1].key])}
        onToggleCollapse={() => onToggle(NAV_ITEMS[1].key)}
      />

      <RegionCard
        anchorId={NAV_ITEMS[2].key}
        title={TITLES.hot.short}
        fullTitle={TITLES.hot.full}
        sub="（创业板 · 分段并列·互不排名 · 与上方主线口径独立）"
        tag="分段并列"
        specs={colsOf(snapshot, "hot")}
        rows={hot}
        profile="hot"
        marks={marks}
        count={hot.length + offboard.length}
        segments={[
          { title: `— 榜内飙升${hotSubtitle(hot.length)}`, rows: hot, profile: "hot" },
          { title: `— 榜外异动${offboardSubtitle(offboard.length)}`, rows: offboard, profile: "offboard" },
        ]}
        active={activeKey === NAV_ITEMS[2].key}
        collapsed={Boolean(collapsed[NAV_ITEMS[2].key])}
        onToggleCollapse={() => onToggle(NAV_ITEMS[2].key)}
      />
    </div>
  );
}

const MAIN_SCROLL_SELECTOR = "[data-scroll-root='main']";
const SIDE_SCROLL_SELECTOR = "[data-scroll-root='side']";

function scrollMainTo(key: string) {
  const el = document.getElementById(`anchor-${key}`);
  if (!el) return;
  const box = document.querySelector<HTMLElement>(MAIN_SCROLL_SELECTOR);
  if (!box) return;
  // ⚠ 四个坑（2026-10-19 实测，Electron 44 / Chromium；沿革见 git）：
  // 1. **不要**用 `box.scrollTo({behavior:"smooth"})`：这个环境里平滑滚动会被静默
  //    丢弃（`scrollTop` 一动不动、无报错）。瞬移赋值才可靠。
  // 2. **不要**用 rect 差值：点击那一刻容器可能还没布局完（右栏懒加载会改变
  //    高度），测出来的 delta 偏小，滚到半路就停。
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
 *  1. 主区与右栏是两个滚动容器、各有一套观察器，而右栏那个高容器里的
 *     `anchor-gate` 长期处于 intersecting ⇒ 它会把主区滚到哪都盖掉。
 *  2. `rootMargin` 负边距那条「带」在高容器里很难调。
 *  直接按滚动位置算「最后一个越过阈值线的区块」是确定性的，易验、易解释。
 *
 *  ⚠ 另两个必须配对的条件（少一个就静默失效，而这是纯交互退化、无测试会红）：
 *  - 首轮 `snapshot` 还是 null ⇒ 锚点**尚不存在**，用 `MutationObserver` 等它们挂上来；
 *  - 只监听**主区**容器的 scroll（`gate` 在右栏，由点击导航时直接置高亮）。
 */
function useActiveSection(keys: string[], viewKey: string): string | null {
  const [active, setActive] = useState<string | null>(null);
  const keysKey = keys.join(",");

  // ⚠ viewKey（列表 / 详情）必须进依赖：切到详情时主区滚动容器会**整体卸载**，
  //   切回列表时又是一个全新的 DOM 节点。若不在切换时重挂监听，滚动高亮会在
  //   第一次「详情 → 返回」之后永久停在旧值（纯交互退化，无测试会红）。
  useEffect(() => {
    // 详情视图里锚点会整体卸载：此时不挂 MutationObserver，否则它会随着 K 线
    // Canvas / 解读区的每次 DOM 变化反复唤醒，却永远等不到主区锚点出现。
    if (keys.length === 0 || viewKey === "stock") return;
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
  }, [keys, keysKey, viewKey]);

  return active;
}

/** 主区分栏的 id：`useDefaultLayout` 用它把「用户拖出来的宽度」存进 localStorage。 */
const PANEL_GROUP_ID = "rts-panel-main";

/**
 * 右侧「过门」侧栏。
 * **删掉了 `StatsCharts`**（Recharts 懒加载那块）：它把 `gate.stats` 的同一组七个
 * 数字用「条形图 + 饼图 + 明细表」画了三遍，右栏本来就窄，三重冗余只是消耗纵向
 * 空间；且它为了占位饼图把 bundle 拖大 330KB。同一组数字在 `GatePanel` 的紧凑账目区
 * 一处展示，不再重复；右栏顶部的空占位容器也一并移除。
 */
export default function App() {
  const route = useRoute();
  const { snapshot, error } = useSnapshot();
  const [now, setNow] = useState(() => Date.now());
  const { ui, update, toggleCard } = useUiState();
  const [statusOpen, setStatusOpen] = useState(false);
  const pillRef = useRef<HTMLButtonElement | null>(null);

  // 分栏宽度持久化（react-resizable-panels v4：`defaultLayout` + `onLayoutChanged`）。
  // 只存 layout，不存任何业务状态。
  const { defaultLayout, onLayoutChanged } = useDefaultLayout({ id: PANEL_GROUP_ID });

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 30_000);
    return () => window.clearInterval(timer);
  }, []);

  const configured = snapshot?.ctx?.config?.refreshInterval;
  const intervalMs = (typeof configured === "number" && configured > 0 ? configured : 60) * 1000;
  const status = deriveStatus(snapshot, error, intervalMs, now);

  const navKeys = useMemo(() => NAV_ITEMS.map((i) => i.key), []);
  const active = useActiveSection(navKeys, route.name);
  // `gate` 锚点在右栏（不在主滚动容器里），主区的 scroll 监听算不到它 ⇒ 点导航
  // 进 gate 时直接置高亮，避免「滚到了却不高亮」。
  const [gateActive, setGateActive] = useState(false);

  // 导航计数徽章 / 读数带区块计数：四个区块的行数（纯计数事实，来自载荷本身，零重算）。
  const counts = useMemo<NavCounts>(() => {
    const r = snapshot?.regions;
    return {
      pool: r?.main.length ?? 0,
      hist: r?.hist.length ?? 0,
      hot: (r?.hot.length ?? 0) + (r?.offboard.length ?? 0),
      gate: r?.gate.stats.passed ?? 0,
    };
  }, [snapshot]);

  const scrollToSection = useCallback((key: string) => {
    if (key === "gate") {
      setGateActive(true);
      // `飞书过门` 对应右栏卡片，滚右栏到顶（不在主区滚动容器里）。
      const side = document.querySelector<HTMLElement>(SIDE_SCROLL_SELECTOR);
      if (side) side.scrollTop = 0;
      return;
    }
    setGateActive(false);
    scrollMainTo(key);
  }, []);
  const activeKey = gateActive ? "gate" : active;

  // `gate` 的高亮是「点击后的短暂覆盖」（设计 §5.6：短暂锁定后高亮跟随滚动）。
  // 用户一旦重新滚主区，就交还给 useActiveSection 的滚动高亮；否则 gate 会
  // 永久占住高亮，回看上面三张卡片时侧栏仍亮着「飞书过门」。
  // 只监听主区容器：gate 导航滚的是右栏，不会触发这里的清除。
  useEffect(() => {
    const box = document.querySelector<HTMLElement>(MAIN_SCROLL_SELECTOR);
    if (!box) return;
    const clear = () => setGateActive(false);
    box.addEventListener("scroll", clear, { passive: true });
    return () => box.removeEventListener("scroll", clear);
  }, [snapshot, route.name]);

  // Esc：优先关状态弹层并归还焦点，其次从详情返回列表。统一入口，避免双监听抢事件。
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      if (statusOpen) {
        e.preventDefault();
        setStatusOpen(false);
        pillRef.current?.focus();
      } else if (route.name === "stock" || route.name === "tasks") {
        e.preventDefault();
        goToList();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [statusOpen, route.name]);

  // 回到列表时把焦点还给来源行 / 门内按钮（详情 → 列表这一次）。
  const prevRoute = useRef(route.name);
  useEffect(() => {
    const wasStock = prevRoute.current === "stock";
    prevRoute.current = route.name;
    if (route.name !== "list" || !wasStock) return;
    const id = consumeOpener();
    if (!id) return;
    const raf = requestAnimationFrame(() => {
      document.getElementById(id)?.focus({ preventScroll: true });
    });
    return () => cancelAnimationFrame(raf);
  }, [route.name]);

  const stockName = route.name === "stock" ? nameFor(snapshot, route.symbol) : "";

  return (
    <div className="app-shell">
      <StatusBar
        snapshot={snapshot}
        error={error}
        intervalMs={intervalMs}
        now={now}
        statusOpen={statusOpen}
        onStatusToggle={() => setStatusOpen((v) => !v)}
        onStatusClose={() => setStatusOpen(false)}
        pillRef={pillRef}
      />

      {/* ── 本轮读数带：哪一轮 / 是否新鲜 / 本轮各区块多少只（只用已有快照字段） ── */}
      <section className="readout" aria-label="本轮读数">
        {route.name === "stock" ? (
          <>
            <div className="readout-metrics">
              <div className="metric">
                <span className="metric-label">当前视图</span>
                <span className="metric-value" style={{ fontSize: 16 }}>个股</span>
              </div>
            </div>
            <div className="readout-divider" />
            <div className="round-copy">
              <div className="round-line">
                个股 <span className="mono">{route.symbol}</span>
                {stockName ? <span> {stockName}</span> : null}
              </div>
              {/* v4：说明句收进页脚，读数带只留读数本身（见下方 footer 注释）。 */}
            </div>
            <div className="readout-end">
              <span className="scope-chip"><i />本机回环 127.0.0.1</span>
              <span className="round-pill">{snapshot ? `seq ${snapshot.seq}` : "seq —"}</span>
            </div>
          </>
        ) : (
          <>
            <div className="readout-metrics">
              <div className="metric">
                <span className="metric-label">池选</span>
                <span className="metric-value">{counts.pool}<small>行</small></span>
              </div>
              <div className="metric">
                <span className="metric-label">过门通过</span>
                <span className="metric-value is-brand">{counts.gate}<small>只</small></span>
              </div>
            </div>
            <div className="readout-divider" />
            <div className="round-copy">
              <div className="round-line">
                本轮轮次 <span className="mono">{snapshot ? `${snapshot.date} ${snapshot.time}` : "—"}</span>
                {" · "}<span className="mono">seq {snapshot ? snapshot.seq : "—"}</span>
                {snapshot ? ` · ${status.session}` : ""}
              </div>
              {/* v4：这行 11px 灰字原本横贯读数带中部，是全页最长的一句，
                却只重复页脚已有的口径说明（且把读数挤到右边）。已移入页脚。 */}
            </div>
            {/* ⚠ v4：删掉原有的四颗 `r-chip`。
                它们与侧栏导航的计数徽章是**同一组数字**（同一 `counts` 来源），
                同屏出现两次、相隔不到 300px，还各自带一套样式 ⇒ 读者要判断
                「这两个 13 是不是同一件事」。计数归侧栏徽章唯一持有（它是常驻导航，
                滚动时一直可见）；读数带只留「本轮几个数」这一件事。 */}
            {/* ⚠ v4：右端原先挤了三个 pill（AI 分析 / 本机回环 / 状态·seq），
                与顶栏状态胶囊、页脚又名三处重复。现在只留「AI 分析」入口；
                新鲜度与 seq 由顶栏那颗状态胶囊唯一承担（`freshLabel` 随之删除）。 */}
            <div className="readout-end">
              <a className="tasks-entry" href="#/tasks">AI 分析</a>
            </div>
          </>
        )}
      </section>

      {route.name === "tasks" ? (
        <Tasks />
      ) : route.name === "stock" ? (
        /* 详情视图沿用同一个壳：隐藏侧栏与右栏，避免「单只股票」与「本轮卡片集合」混屏。 */
        <div className="workspace detail-workspace">
          <Stock symbol={route.symbol} name={stockName || undefined} />
        </div>
      ) : (
        <div className="workspace">
          {/* ── 侧边导航（可折叠，图标 + tooltip + 选中品牌色条）────────────── */}
          <Sidebar
            active={activeKey}
            onNavigate={scrollToSection}
            counts={counts}
            collapsed={ui.sidebarCollapsed}
            onToggle={() => update({ sidebarCollapsed: !ui.sidebarCollapsed })}
          />

          {/* ── 主区：区块列表 + 右侧「过门」侧栏，可拖拽分栏 ── */}
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
              <Panel id="main-table" minSize={45} defaultSize={74} className="overflow-hidden">
                <main className="main-scroll h-full min-h-0" data-scroll-root="main">
                  <RegionList
                    snapshot={snapshot}
                    activeKey={activeKey}
                    collapsed={ui.collapsed}
                    onToggle={toggleCard}
                  />
                </main>
              </Panel>
              <PanelResizeHandle className="panel-resize" />
              <Panel id="side-stats" minSize={18} defaultSize={26} className="overflow-hidden">
                <aside className="side-scroll h-full min-h-0" data-scroll-root="side">
                  <GatePanel
                    gate={snapshot.regions.gate}
                    regions={{
                      main: snapshot.regions.main,
                      hist: snapshot.regions.hist,
                      hot: snapshot.regions.hot,
                      offboard: snapshot.regions.offboard,
                    }}
                    tab={ui.gateTab}
                    onTabChange={(tab) => update({ gateTab: tab })}
                    active={activeKey === "gate"}
                    collapsed={Boolean(ui.collapsed.gate)}
                    onToggleCollapse={() => toggleCard("gate")}
                  />
                </aside>
              </Panel>
            </PanelGroup>
          ) : (
            /* 首轮未上报 / 服务无响应：沿用「不白屏」语义，换成完整版面空态。 */
            <main className="workspace-empty min-w-0 flex-1">
              <div className="region-card" style={{ maxWidth: 560 }}>
                <div className="region-head">
                  <span className="card-accent" aria-hidden="true" />
                  <div className="card-title-wrap">
                    <h2 className="region-title">
                      {error ? `本地服务无响应（${error}）` : "等待扫描器首轮上报"}
                    </h2>
                    <p className="region-fulltitle">
                      {error ? "本机服务暂时未响应；尚无已读取到的快照可呈现。" : "扫描器在本机运行"}
                    </p>
                  </div>
                </div>
                <EmptyState note="扫描器在本机运行；首轮快照到达后本页自动呈现，页面每 60s 读取一次。" />
              </div>
            </main>
          )}
        </div>
      )}

      {/* ⚠ v4：页脚是「数据说明」不是「轮次第三次播报」。
          原先右侧又写一遍 `date time · seq`，与顶栏 + 读数带三处重复，
          且让页脚只剩重复内容。现在页脚只留「来源 / 读取节奏 / 口径边界 / 契约版本」，
          轮次与 seq 归读数带与状态胶囊唯一持有。 */}
      <footer className="footer">
        <span>数据来源：本机扫描器最后一轮快照 · 本机回环 127.0.0.1</span>
        <span className="sep" />
        <span>每 {Math.round(intervalMs / 1000)}s 读取一次</span>
        <span className="sep" />
        <span>轮次、时段与区块口径随快照呈现</span>
        <span className="right">数据契约 v2 · 本机只读镜像</span>
      </footer>
    </div>
  );
}
