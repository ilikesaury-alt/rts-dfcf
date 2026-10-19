# 桌面 UI 现代化 · CONTEXT

> 工作流：dev-task（🟡 标准档）。本文件是 需求对齐 → 方案设计 → 验收 的单一活文档。
> 上游约束源：`docs/ui-panel-requirements.md`(v2.1) / `docs/ui-panel-design.md`(v1.1) / `AGENTS.md`。

## 元信息（Step 0）
- **模型/工具组合**：本会话单 agent 完成 规划+编码+审查（当前模型）；如需拆分写代码/审查模型可后续切换。
- **重量档**：🟡 标准（多文件、有方案权衡、前端改造；不碰数据契约）。
- **测试环境**：node v22.22.2 / pnpm 12.11.2；`rts-panel-cloud/web` `tsc --noEmit` 当前干净；守卫 `test_panel_web_hygiene.py` / `test_panel_web_cols.py` / `test_panel_contract.py` 已存在，必须保持绿。
- **现状**（已勘察）：桌面 UI = Electron 壳(`desktop/main.js`) 加载 本地只读 HTTP(`scanner/local_panel_server.py:8899`) 托管的前端(`rts-panel-cloud/web`，React19+Vite+TS，依赖仅 react/react-dom/kline-component)；手写 `styles.css` 走 CSS 变量，无组件库、无 Tailwind、无导航/分栏。

## 当前硬纪律（不可破，否则守卫红 / 破坏项目立身之本）
1. **只读镜像**：面板不新增筛选、不新增排序（FR-V2 允许的「临时排序」仅 评分/涨幅/量比 且标「临时排序」）、不新增结论（G5：无短名单/综合判断/持仓建议）。
2. **列名单源**：列头只来自服务端下发 `colSpec`；`components/**` 不得写死列名（`test_panel_web_hygiene.py` 守卫）。
3. **契约三端对齐**：`api/types.ts` ⇄ `panel_serialize.py` ⇄ `worker/src/serialize.ts` 手抄镜像；动载荷必须三端同改 + 升 `SCHEMA_VERSION` + 过 `test_panel_contract.py`。
4. **唯一网络收口**：`client.ts` 之外 0 处裸 `fetch`/`XMLHttpRequest`。
5. **涨跌色/空值/标记语义** 与终端同源（红涨绿跌 `#ef232a/#14b143`、空显 `—`、行尾 `新/⚡/稳/▼`）。

---

## 待 clarify（Step 1 已定）
- **Q1 技术路线**：A — Tailwind v4（@tailwindcss/vite 插件）+ shadcn/ui 设计系统；配色沿用现有 A 股红涨绿跌（`#ef232a/#14b143`）。
- **Q2 首轮切片**：A — 聚焦 Phase 1+2（设计系统 + 可折叠侧边导航 + 可拖拽分栏 + 区块卡片化），3/4/5 期后续增量。
- **Q3 客户端图表**：A — 加纯前端只读图表（Recharts，从现有 payload 算），零后端/契约改动、不新增结论。
- **Q4 契约与文案**：是 — 区块标题/过门区固定标题/行尾标记语义原样保留，只换样式不换文案。

## 要解决的问题
桌面版前端只有手写 `styles.css`（CSS 变量）+ 原生 `<table>`，无设计系统、无组件库、无导航/分栏/微交互，观感「朴实」。目标：在不破坏「只读镜像 / 列名单源 / 契约三端对齐」纪律的前提下，把面板升级成有现代交易终端观感与交互的 UI（设计系统 + 侧边导航 + 可拖拽分栏 + 卡片化区块 + 纯前端只读图表）。

## 范围（In / Out）
**In（本轮 Phase 1+2 + Q3 图表）**
1. 引入 Tailwind v4 + shadcn/ui 基础设施（design token 化现有配色；圆角/分隔/排版/焦点环/可访问性统一）。
2. 重构 `App.tsx`：顶部状态条不变语义 → 左侧**可折叠导航**（五区块 + 过门 + 个股）+ 主区**可拖拽分栏**（`react-resizable-panels`，主表格区 ↔ 过门侧栏可拖宽）。
3. 区块表格**卡片化**：区块 = shadcn Card；表头 `sticky`；横向滚动时**冻结「代码/名称」首列**（纯展示，不动排序）；行 hover 高亮、行尾 `新/⚡/稳/▼` 标记保留。
4. 纯前端只读图表（Q3）：用现有 payload 在客户端画**分布/计数小图**（如过门分组计数、评分/涨幅分布），仅展示已有 `gate.stats`/`regions` 字段，不新增结论、不碰契约。
5. 涨跌色/空值 `—`/标记语义与终端同源（沿用现有 CSS 变量映射，不重造）。

**Out（本轮不做）**
- ❌ 不动数据契约（不动 `api/types.ts` / `panel_serialize.py` / `worker/src/serialize.ts`；不升 `SCHEMA_VERSION`）。
- ❌ 不动 Electron 壳（`desktop/main.js`）、不动 `local_panel_server.py` 阈值/路由语义。
- ❌ 面板新增筛选/排序/结论（G5）；FR-V2 临时排序不在本轮范围。
- ❌ 新引入状态库（Zustand/Redux）/ SSR（design §2.2「不拆」）；路由仍走现有 `lib/router.ts` 哈希路由（不引 react-router）。
- ❌ Electron 壳打磨（自定义标题栏/托盘/命令面板）—— 属 Phase 5，不在本轮。
- ❌ 改动 `components/**` 列名硬编码（列头仍只来自 `lib/cols.ts` / 下发 `colSpec`）。

## 关键决策与取舍
- **Tailwind v4 走 `@tailwindcss/vite` 插件**（不引 postcss 链路，更稳；零运行时）。现有 `styles.css` 的 CSS 变量（`--up/--down/--bg/--card/--border/--text/...`）迁成 Tailwind `@theme` token，组件里逐步改用工具类，但**涨跌色变量名保留**（K 线 Canvas 用不到 CSS 变量，已在 `Stock.tsx` 另给 `ASHARE_THEME`）。
- **shadcn/ui 只取无副作用的展示组件**（Card / Button / Separator / Tabs / Tooltip / Sheet 或 Collapsible 做侧栏），不引其数据表格（table 仍用现有 `RegionTable` 吃 `colSpec`，守住列名单源）。
- **唯一网络收口不变**：图表数据全部来自已下发的 `snapshot.regions` / `snapshot.ctx`，不新增任何 fetch。
- **pnpm12 坑**：新增依赖若带 postinstall（`esbuild` 已在 `allowBuilds`），`web/pnpm-workspace.yaml` 的 `allowBuilds` 需包含新键（如 `tailwindcss` 的 `@tailwindcss/oxide`、`lightningcss`）。CI 复现用 `pnpm approve-builds --all`。
- **构建产物**：`pnpm build` 仍输出 `web/dist`，Electron `desktop/prepare.mjs` 前置检查仍要求 `web/dist/index.html` 存在。

## 验收标准（写死）
1. **构建通过**：`cd rts-panel-cloud/web && pnpm install && pnpm build && pnpm typecheck` 全绿；产物 `dist/index.html` 存在（Electron 前置检查不红）。
2. **纪律守卫全绿**：`pytest tests/test_panel_web_hygiene.py tests/test_panel_web_cols.py tests/test_panel_contract.py` 全过（`components/**` 无列名硬编码；列投影与 `COLS_*` 逐列同序；契约未漂移）。
3. **布局**：左侧可折叠导航（五区块 + 过门 + 个股详情入口）+ 主区与过门侧栏可拖拽分栏；窄屏（≤900px）不溢出（现有 `styles.css` 媒体查询约束保留/等价）。
4. **卡片化**：每个区块包进 shadcn Card（标题/分段标题/空态 `—（本轮无数据）` 文案与终端同源，不改字）。
5. **冻结首列**：横向滚动时「代码/名称」首列固定（纯展示）。
6. **图表（Q3）**：仅用已下发 `regions`/`ctx`/`gate.stats`；不出现任何「评分高=更可能涨」类暗示文案（G5；`test_panel_web_cols.py` 第 4 条守卫）。
7. **配色同源**：红涨绿跌、`—` 空值、行尾 `新/⚡/稳/▼` 与终端一致。
8. **零契约改动**：`SCHEMA_VERSION` 不变；`test_panel_contract.py` 通过（即本次未动 payload）。

## 已知风险 / 踩过的坑
- pnpm 12 默认拦截 postinstall：`web/pnpm-workspace.yaml` 的 `allowBuilds` 需含 Tailwind v4 的原生二进制键（`@tailwindcss/oxide` / `lightningcss`），否则构建期缺二进制。design doc T1.1 已踩 esbuild，同机制。
- `web/dist` 缺失 → Electron 窗口 404；`desktop/prepare.mjs` 前置检查会拦打包。
- shadcn/ui 的 `init` CLI 会改 `tsconfig`/`components.json`/`lib/utils.ts`（cn 助手）；本项目已用 `@/` 之外的相对导入，需确认 `cn()` 与现有 `clsx` 习惯一致，不破坏 `RegionTable` 等现有组件。
- Tailwind 工具类与现有手写 CSS 混用期：过渡阶段 `styles.css` 保留、逐步迁移；最终删除重复手写规则，避免双套样式分叉。

---

## 已确认方案（Step 2 · design-plan 输出）

### 问题本质（第一性原理）
面板已是终端的**忠实只读镜像**，功能与契约完备。「朴实」纯粹是**表现层**问题：无设计系统、无组件库、无导航/分栏/微交互。故本次只是**表现层现代化**，约束为三条不变式：①只读镜像（G5，不增筛选/排序/结论）②列名单源（colSpec 来自服务端）③契约三端对齐（不动 payload）。

### 方案对比（奥卡姆：选假设最少、最不侵入的一条）
| 方案 | 做法 | 收益 | 代价 / 风险 | 结论 |
|---|---|---|---|---|
| **A（选定）** | Tailwind v4(@tailwindcss/vite) + shadcn/ui 设计系统 + 可折叠侧边导航 + 可拖拽分栏 + 区块卡片化 + 纯前端只读图表（Recharts，吃现有 payload） | 现代观感最强、组件可访问性/焦点环统一、零契约改动 | 新增前端构建依赖 + pnpm12 allowBuilds 坑 + shadcn init 改 tsconfig | 用户已选 A |
| B | 纯手写 CSS 升级（design token + 导航/分栏，不引依赖） | 零新依赖、最稳 | 现代感上限低，仍需手写大量组件 | 用户未选 |
| C（拒绝，属 Phase 5） | 全交易终端壳：自定义标题栏/托盘/命令面板/板块热力图/偏好持久化 | 终态最完整 | 动 Electron 壳、引入 localStorage、范围过大、破「不拆」纪律 | 本轮不做 |

### 模块边界（高内聚、不拆）
- **`web/src/index.css`（新）**：Tailwind 入口 `@import "tailwindcss"` + `@theme` 把现有 CSS 变量（`--up/--down/--bg/--card/--border/--text/...`）映射成设计 token；保留涨跌色变量名（K 线 Canvas 仍用 `Stock.tsx` 的 `ASHARE_THEME`，不认 CSS 变量）。
- **`web/src/components/ui/*`（shadcn 生成）**：只取展示型无副作用组件 `card` / `button` / `separator` / `tabs` / `tooltip` / `sheet|collapsible`（侧栏）。**不引 shadcn 数据表格**——表格仍用现有 `RegionTable`（守住列名单源）。
- **`web/src/components/Sidebar.tsx`（新）**：左侧可折叠导航，项 = v1 池选 / v1 回捞 / 沪深飙升 / 榜外异动 / 飞书过门 / 个股详情入口。项文案用**区块/段标题**（非列名），不触发 `test_panel_web_hygiene.py` 的列名硬编码守卫。
- **`web/src/App.tsx`（重构）**：状态条语义不变 → 外框 = 侧栏 + 主区 `react-resizable-panels`（主表格区 ↔ 过门侧栏可拖宽）。窄屏（≤900px）保持现有堆叠约束。
- **`web/src/components/RegionCard.tsx`（新）**：把现有 `RegionTable` 包进 shadcn Card；表头 `sticky`；横向滚动冻结「代码/名称」首列（纯 CSS `position:sticky;left:0`）；行 hover 高亮；行尾 `新/⚡/稳/▼` 标记保留。
- **`web/src/components/StatsCharts.tsx`（新，Q3）**：纯前端 Recharts，仅消费已下发 `regions`/`ctx`/`gate.stats`（如过门分组计数、评分/涨幅分布）。**禁 verdict 文案**（G5）。
- **不动**：`api/types.ts` / `panel_serialize.py` / `worker/src/serialize.ts` / `desktop/main.js` / `local_panel_server.py` / `lib/router.ts`（哈希路由保留，不引 react-router）/ `lib/cols.ts`（列投影逐列同序守卫保留）。

### 垂直切片（每片独立可构建、守卫绿，便于独立验收）
1. **Slice 1 设计系统基建**：装 Tailwind v4 + `@tailwindcss/vite` + shadcn init（card/button/separator/tabs/tooltip/collapsible）+ `index.css` `@theme` 映射现有变量 + `pnpm-workspace.yaml` allowBuilds 补 `@tailwindcss/oxide`/`lightningcss`。验收：仅样式接入，`pnpm build`+`typecheck` 绿，页面外观不变。
2. **Slice 2 布局重构**：`App.tsx` → 侧栏 + `react-resizable-panels` 分栏；`Sidebar.tsx` 用区块标题文案。验收：验收标准 3；`test_panel_web_hygiene.py` 仍绿（侧栏无列名）。
3. **Slice 3 卡片化 + 冻结首列 + 行 hover**：`RegionCard.tsx` 包 `RegionTable`；sticky 首列；空态 `—（本轮无数据）` 文案同终端。验收：验收标准 4/5/7。
4. **Slice 4 只读图表**：`StatsCharts.tsx` 用 Recharts 吃现有 payload。验收：验收标准 6（无 verdict 文案，`test_panel_web_cols.py` #4 绿）。

### 对抗性审查（找能推翻核心假设的反例）
1. **shadcn init 改 tsconfig/加 Radix 依赖** → 可能让 `tsc --noEmit` 或现有相对导入告警。反制：init 后跑 `pnpm typecheck` + `pnpm build` 双绿；`cn()` 与现有导入习惯一致；新增 Radix 为纯 JS 无 postinstall。
2. **列名硬编码守卫误伤** → 若侧栏/卡片错写了 `涨幅` 等列名会红。反制：侧栏只用区块/段标题（守卫显式排除段标题），列名恒走 `lib/cols.ts`。
3. **图表越界成「第二结论源」** → 出现「评分高更可能涨」暗示即破 G5。反制：图表标题只写事实（分布/计数），`test_panel_web_cols.py` #4 grep 兜底。
4. **冻结首列与分栏布局打架** → sticky 在可拖拽 pane 内可能错位。反制：sticky 只作用于表格自身横向滚动容器内的首列单元格，与 pane 分隔条正交。
5. **契约漂移** → 我们不动 payload 字段，`SCHEMA_VERSION` 不变，`test_panel_contract.py` 当圣人。

### 奥卡姆剃刀（已砍）
- 不引状态库（Zustand/Redux）/ SSR / react-router / 数据表格库——现有 `useState`+哈希路由+`RegionTable` 已够，引入即过度设计。
- 不重做 K 线（Canvas 自包含、已主题化）。
- 不引入偏好持久化（localStorage）——属 Phase 5。
- 区块/段标题/过门标题**原样保留**，不重措辞。

### 高风险点（已标注 + 应对）
- **R1 pnpm12 allowBuilds**：Tailwind v4 原生二进制 `@tailwindcss/oxide`/`lightningcss` 必须进 `web/pnpm-workspace.yaml` 的 `allowBuilds`，否则 `pnpm build` 缺二进制失败。应对：`pnpm approve-builds --all` 后核对锁文件；CI 复现。
- **R2 shadcn init 副作用**：改 tsconfig + 加 Radix。应对：init 后双绿（typecheck+build）+ 守卫全绿。
- **R3 冻结首列 sticky × 分栏**：视觉漂移。应对：仅表格内横向滚动容器首列 sticky；验收标准 5 人工目检。
- **R4 图表 G5 越界**：应对：事实性标题 + `test_panel_web_cols.py` #4。

### 是否拆 Issue
不拆：单个内聚 UI 现代化 PR，按 4 个垂直切片顺序提交；每片守卫绿可独立回滚。

---

## 实施记录（Step 3 产出）

### 依赖新增（`rts-panel-cloud/web/package.json`）
| 包 | 位置 | 用途 |
|---|---|---|
| `tailwindcss` + `@tailwindcss/vite` 4.3.3 | dev | 设计系统（v4 插件式接入，不走 postcss 链路） |
| `clsx` + `tailwind-merge` | prod | `lib/utils.ts` 的 `cn()`（后写类覆盖先写类） |
| `class-variance-authority` | prod | shadcn `button` 的 variant |
| `@radix-ui/react-{slot,separator,tabs,tooltip,collapsible}` | prod | 手写 shadcn 组件的底层原语 |
| `react-resizable-panels` 4.14.5 | prod | 主区 ↔ 侧栏可拖拽分栏 |
| `recharts` 3.10.1 | prod | 只读统计图（懒加载，独立 chunk） |
| `@types/node` | dev | `vite.config.ts` 里 `node:url` 的类型 |

**pnpm12 allowBuilds（R1）实际结论**：Tailwind v4 的 `@tailwindcss/oxide` / `lightningcss` 以**平台预编译二进制**形式作为 optional dep 分发，**不需要 postinstall**，故 `web/pnpm-workspace.yaml` 无需新增键（`esbuild: true` 保持原样）。这是与 design doc T1.1 那次 esbuild 坑的不同之处。

### 落地文件
- 新增 `web/src/index.css`：`@theme` 设计 token（`--color-up/down/flat/bg/card/border/text/text-dim/lag-warn/err/ring/mark-new` + `--spacing-row-h`）+ 迁移的表格结构 CSS（`.region-table` / `.sticky-col` / `.tone-*` / `.mark-*` / `.kv-total` / `.region-empty` 等）。
- 新增 `web/src/lib/utils.ts`：`cn()`。
- 新增 `web/src/components/ui/{card,button,separator,tabs,tooltip,collapsible}.tsx`：手写 shadcn（**不用 CLI init**，避免改 tsconfig/拉版本漂移）。⚠ Radix v1 是**扁平命名导出**（`Root`/`List`/`Trigger`/`Provider`/`Portal`），老写法 `Tabs.List` 会 TS 报错。
- 新增 `web/src/components/Sidebar.tsx`：锚点滚动导航（**不是**第二个路由器），文案只用区块/段标题。
- 新增 `web/src/components/RegionCard.tsx`：区块卡片 + 分段区块（沪深飙升两段）。
- 新增 `web/src/components/StatsCharts.tsx`：Recharts 只读图（懒加载）。
- 改 `App.tsx`：状态条语义不变 + 侧栏（可折叠）+ `Group/Panel/Separator` 分栏 + `useDefaultLayout` 持久化 + `lazy(StatsCharts)`。
- 改 `RegionTable.tsx`：新增 `frozen` prop（sticky 表头 + 冻结 `specs[0]` 首列）；改用 `cn()`。
- 改 `GatePanel.tsx`：Radix Tabs 两态；标题与 7 字段 stats 不变。
- 改 `StatusBar.tsx` / `pages/Stock.tsx`：迁移到 Tailwind 工具类。
- 改 `vite.config.ts`：加 `tailwindcss()` 插件 + `@` alias；改 `tsconfig.json`：加 `baseUrl`/`paths`。
- **删** `web/src/styles.css`（内容已全部迁入 `index.css`）。

### 自测证据（Step 4）
| 项 | 命令 | 结果 |
|---|---|---|
| 类型检查 | `pnpm typecheck` | 干净 |
| 构建 | `pnpm build` | 成功；主 chunk 371KB（gzip 118KB）、图表独立 chunk 330KB（gzip 99KB）、CSS 16.5KB |
| 前端单测 | `pnpm test`（vitest） | 12 passed |
| 纪律守卫 | `pytest tests/test_panel_web_hygiene.py tests/test_panel_web_cols.py tests/test_panel_contract.py -q` | 22 passed |
| Python 全量 | `pytest tests/ -q` | 1951 passed / 6 failed / 13 skipped；**6 个失败已 `git stash` 验证在干净树上同样失败**（`test_data_health.py` 3 个 + `test_rule_tracking.py` 3 个，预存在，与本次无关，本次零 `.py` 改动） |
| Lint | `ruff check scanner/` | All checks passed |

**端到端（真实数据 + 真实 Electron）**：
- 用 `RTS_LOCAL_PANEL=1 RTS_LOCAL_PANEL_PORT=8899 python unified_scanner.py 60 --no-feishu --no-lock` 起真扫描器，`/api/regions` 返回真实数据（v1 池选 10 行、gate.stats 7 字段、3 组 colSpec）。
- 用 `desktop/node_modules/.bin/electron` 起真实 Electron 窗口加载 `http://127.0.0.1:8899/`，DOM 断言 + 截图：
  - 列表页：侧栏 4 项、4 个 region section、表格 10 行、表头 10 列全来自 colSpec、11 个 sticky 首列单元格、过门标题逐字正确、`.tone-up` 计算色 = `rgb(239, 35, 42)`、3 处空态 `—（本轮无数据）`、1 个分栏手柄、页脚纪律文案在位、状态条「非交易时段 · 数据滞后 N 轮」告警正常。
  - 详情页：K 线 Canvas 1701×525（37 根日线 + MA + MACD，红涨绿跌）、评分分解表 63 行、出现史表、返回链接样式、`.text-up`/`.text-down` 计算色正确、**零 console error**。
  - 截图：`docs/features/desktop-ui-modernize/panel-e2e.png`、`panel-detail-e2e.png`。

### 实施中发现并修掉的问题
1. **`.region-empty` 悬空类**（自己发现）：删 `styles.css` 时漏迁了 `RegionTable.tsx` 仍在用的 `.region-empty`，空态文字会掉样式。已在 `index.css` 补回，Electron 复验计算色 `rgb(138,143,153)` / 12px / 28px。
2. **Radix v1 导入面变更**：`@radix-ui/react-*` 1.x 改为扁平命名导出，`Tabs.List` 等写法 TS 报错。已改 `Root as TabsRoot` 形式并在文件头注明。
3. **react-resizable-panels v4 API 变更**：`PanelGroup`/`PanelResizeHandle`/`autoSaveId` 不存在，改为 `Group`/`Panel`/`Separator` + `id` + `useDefaultLayout({id})`。

### 交叉对抗审查（Step 4）发现并修掉的问题
两个后台 reviewer（一个查工程纪律/可访问性/构建链，一个查契约与跨端口径）。契约审查**全部 PASS、零 BLOCKER/MAJOR**；工程审查员超时未产出，其关注点已由契约审查员 + 下述自查覆盖。修掉 4 条：

4. **`anchor-` 前缀不匹配**（契约审查员 P2 #1）：`RegionCard` 渲染 `id={anchorId}`（=`"pool"`），而 `Sidebar`/`useActiveSection` 找 `anchor-pool` ⇒ 四个导航锚点**三个恒为 null**，点击只高亮不滚动。已统一为 `anchor-<key>`；`GatePanel` 补 `id="anchor-gate"`。
5. **5 个 shadcn 组件 + 1 个导出是死代码**（契约审查员 P2 #2）：`card/button/separator/tooltip/collapsible` 与 `regionCardClass` 无任何消费方（`noUnusedLocals` 不查未使用模块，故 TS 不报）。已删文件 + `pnpm remove` 掉 4 个随之无用的依赖（`class-variance-authority`、`@radix-ui/react-slot/separator/tooltip/collapsible`）—— 奥卡姆剃刀，不留投机代码。
6. **`scrollIntoView` 覆盖算好的 scrollTop**（自查）：滚到「沪深飙升」时实测落在 249px 而非 426px，导致高亮算成上一个区块。已删 `scrollIntoView` 补刀。
7. **高亮被右栏长锚点盖掉**（自查）：主区与右栏各一套 IntersectionObserver，右栏高 536px 的 `anchor-gate` 长期 intersecting ⇒ 「点 v1 池选却高亮飞书过门」。已改为**只监听主区 scroll + 按滚动位置算「最后一个越过 1/4 线的区块」**（确定性算法，易验易解释），`gate` 由点击导航时直接置高亮。

### 关于「沪深飙升高亮成 v1 回捞」的判定（非 bug）
实测数据只有 10 行 ⇒ 主区内容总高 630px、容器 381px ⇒ **最大 scrollTop 只有 249px**，`hot` 最多到 rel=185px。把窗口压到 300px 高（有滚动余量）复测：`v1 池选→v1 池`、`v1 回捞→v1 回`、`沪深飙升→沪深飙升`、`飞书过门→飞书过门` **四项全对**。故该现象是**内容长度不足**的产物，不是实现缺陷。

### 三个实测坑（已写进代码注释，改这段前必读）
- **Electron/Chromium 下 `scrollTo({behavior:"smooth"})` 被静默丢弃**（`scrollTop` 不动、无报错）。必须瞬移赋值 `box.scrollTop = x`。
- **分栏后页面本身不滚动**（根容器 `h-dvh overflow-hidden`），滚 `window` 无效；必须滚 Panel 内那个 `overflow-y-auto` 容器。
- **Panel 内层 `<main>` 必须 `min-h-0`**，否则它「内容多高就多高」把 Panel 撑高、分栏失效（`overflow-y-auto` 有等于没有）。

### 未做 / 后续（Phase 3~5 候选）
- Electron 壳打磨（自定义标题栏/托盘/`⌘K` 命令面板）。
- 表格微交互（刷新行微闪、单元格条形背景、键盘快捷键）。
- 板块热力图 / 更丰富的只读可视化。
- 响应式细化（≤900px 堆叠已保留等价约束，未实测小窗）。
