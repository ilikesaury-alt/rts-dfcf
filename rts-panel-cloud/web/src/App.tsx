/**
 * 面板主体（验收 1~4、6）：状态条 / 四区块表格 / 过门区 / 页脚。
 *
 * 版面纪律（与终端同意图，与 7.1 的「终端 → 面板」映射一致）：
 * - 区块**顺序与终端一致**，前端**不另行排序**（FR-V2：排序已在 `assemble.build_scan_view`
 *   完成，面板只渲染）。
 * - 区块标题里写死的是**展示文案**（设计规定的那几句），不是列定义 —— 列定义一律来自
 *   下发的 `colSpec`（`components/**` 由 grep 守卫不得出现列名）。
 * - 面板**不新增**任何结论：没有短名单、没有综合判断、没有持仓建议，也没有「评分高=更
 *   可能大涨」这类暗示。
 *
 * ⚠ 已知口径差异（不是 bug，但必须让人看见）：载荷的 `regions` 只有
 * `main / hist / hot / offboard` 四个键，**终端的「榜内异动」段不在契约里**
 * （加它要动 SCHEMA_VERSION，本轮冻结为 2）。故本区的标题写「分段并列」而非终端的
 * 「三段并列」—— 面板确实只有两段，标题不能替不存在的东西作保。
 */

import { useEffect, useState } from "react";

import type { ColSpec, RegionsResponse } from "./api/types";
import GatePanel from "./components/GatePanel";
import RegionTable from "./components/RegionTable";
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
    <div className="regions">
      {warnings.length > 0 ? (
        <ul className="warnings">
          {warnings.map((w) => (
            <li key={w}>{w}</li>
          ))}
        </ul>
      ) : null}
      {flowFiltered > 0 ? <p className="flow-filtered">▸ 资金流出已剔除 {flowFiltered} 只（全区域统一口径）</p> : null}

      <section className="region">
        <h2 className="region-title">{TITLES.pool}</h2>
        <RegionTable specs={colsOf(snapshot, "pool")} rows={main} profile="pool" marks={marks} />
      </section>

      <section className="region">
        <h2 className="region-title">{TITLES.hist}</h2>
        <p className="region-sub">（前若干交易日进过 v1 · 今日回调到位 · 与上方口径独立）</p>
        <RegionTable specs={colsOf(snapshot, "hist")} rows={hist} profile="hist" marks={marks} />
      </section>

      <section className="region">
        <h2 className="region-title">{TITLES.hot}</h2>
        <p className="region-sub">（创业板 · 分段并列·互不排名 · 与上方主线口径独立）</p>

        <h3 className="segment-title">— 榜内飙升{hotSubtitle(hot.length)}</h3>
        <RegionTable specs={colsOf(snapshot, "hot")} rows={hot} profile="hot" marks={marks} />

        <h3 className="segment-title">— 榜外异动{offboardSubtitle(offboard.length)}</h3>
        <RegionTable specs={colsOf(snapshot, "hot")} rows={offboard} profile="offboard" marks={marks} />
      </section>
    </div>
  );
}

export default function App() {
  const route = useRoute();
  const { snapshot, meta, error } = useSnapshot();
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 30_000);
    return () => window.clearInterval(timer);
  }, []);

  const configured = snapshot?.ctx?.config?.refreshInterval;
  const intervalMs = (typeof configured === "number" && configured > 0 ? configured : 60) * 1000;

  return (
    <div className="app">
      <StatusBar meta={meta} snapshot={snapshot} error={error} intervalMs={intervalMs} now={now} />

      {route.name === "stock" ? (
        <main className="main single">
          <Stock symbol={route.symbol} />
        </main>
      ) : (
        <main className="main">
          {snapshot ? (
            <>
              <RegionList snapshot={snapshot} />
              <GatePanel gate={snapshot.regions.gate} regions={{ main: snapshot.regions.main, hist: snapshot.regions.hist, hot: snapshot.regions.hot, offboard: snapshot.regions.offboard }} />
            </>
          ) : (
            <div className="bootstrap">
              {error ? `本地服务无响应（${error}）` : "等待扫描器首轮上报…"}
              <span className="bootstrap-hint">扫描器在本机运行；首轮（约 60s 内）完成后本页自动刷新。</span>
            </div>
          )}
        </main>
      )}

      <footer className="footer">
        只读视图：不新增筛选、不新增排序、不新增判断。数据来自本机扫描器的最后一轮，页面每 {Math.round(intervalMs / 1000)}s 轮询一次。
      </footer>
    </div>
  );
}
