/**
 * 面板骨架（T1.1 / design §7.1 布局）。
 *
 * 本任务只交付**工程骨架**：把 §7.1 的版面结构（状态条 / 五区块 / 右侧过门栏 /
 * 页脚）立起来，各槽位先放占位。下列内容由后续任务填入，**不在 T1.1 范围内**：
 *
 * | 槽位 | 由谁实现 | 关键约束 |
 * |---|---|---|
 * | 状态条数据 | T1.5 `StatusBar` | 滞后 >1 变黄 `#f5a623` + 文案「数据滞后 N 轮」 |
 * | 五区块表格 | T1.4 `RegionTable` | 列数/列头只认 `colSpec`，**组件内不得硬编码列名**（D7） |
 * | 过门面板 | T5.3 `GatePanel` | 标题固定串，含「下一张卡会推什么（非已推内容）」 |
 * | 数据获取 | T1.2 `api/client.ts` | 唯一网络收口，组件内**禁止裸 `fetch(`** |
 * | 实时化 | T3.2 `subscribeSSE` | 断线重连后先全量对齐再等增量 |
 * | 日历/历史 | T5.2 `History.tsx` | 无快照日不可点 |
 *
 * 现在**不引入**任何网络请求：T1.1 的验收是「`pnpm dev` 起 5173 且 `/api`
 * 能代理到 8787」，骨架阶段发请求只会让还没写好的 client 层返工。
 */

/** §7.1 的五区块顺序。顺序与终端一致，**不得**在前端另行排序（FR-V2）。 */
const REGION_TITLES = [
  "◆ v1 池选 — 新票优先·类别优先·排名升序·资金流降序",
  "◆ v1 回捞",
  "◆ 沪深飙升·极有可能大涨",
  "  A 段（榜内飙升）",
  "  B 段（榜外异动）",
] as const;

export default function App() {
  return (
    <div className="app">
      {/* 状态条：固定顶部 h=36px（§7.2）。T1.5 填数据与滞后提示。 */}
      <header className="statusbar" role="banner">
        <span className="statusbar__slot">状态条 · T1.5 实现</span>
      </header>

      <div className="layout">
        <main className="regions">
          {REGION_TITLES.map((t) => (
            <section key={t} className="card">
              <h2 className="card__title">{t}</h2>
              {/* 空态文案按 §7.2：空区块显「—（本轮无数据）」，不留空白表。 */}
              <p className="card__empty">—（本轮无数据）</p>
            </section>
          ))}
        </main>

        {/* 右侧固定侧栏，高度跟随内容（§7.1）。T5.3 填 GatePanel。 */}
        <aside className="side">
          <section className="card">
            <h2 className="card__title">◆ 飞书过门</h2>
            <p className="card__empty">—（T5.3 实现）</p>
          </section>
        </aside>
      </div>

      {/* 页脚：G5 明示「只读 · 不构成投资建议」与数据来源。 */}
      <footer className="footer">数据源 = 本地扫描器上报 · 只读 · 不构成投资建议</footer>
    </div>
  );
}
