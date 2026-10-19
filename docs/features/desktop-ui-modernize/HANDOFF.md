# 桌面版 UI 现代化 · HANDOFF

> 面向**未来会话**：要从哪儿接着干。实施记录（怎么做成这样的）在 `CONTEXT.md`，不重复。
> 交付文档（给测试/业务）在 `交付文档.md`，也不重复。

## 当前状态

- **改动已完成并自测通过，尚未 commit**（按 dev-task Step 7，提交需你确认）。
- 工作树：`git status --short` 见下「变更清单」。零 `.py` 改动、零 `scanner/` 改动、零契约改动。

```
 M rts-panel-cloud/web/package.json            # 依赖增删
 M rts-panel-cloud/web/pnpm-lock.yaml
 M rts-panel-cloud/web/src/App.tsx             # 侧栏 + 分栏 + 懒加载图表 + 导航/高亮
 M rts-panel-cloud/web/src/components/GatePanel.tsx
 M rts-panel-cloud/web/src/components/RegionTable.tsx   # 新增 frozen prop
 M rts-panel-cloud/web/src/components/StatusBar.tsx
 M rts-panel-cloud/web/src/main.tsx            # styles.css → index.css
 M rts-panel-cloud/web/src/pages/Stock.tsx
 D rts-panel-cloud/web/src/styles.css          # 内容已全量迁入 index.css
 M rts-panel-cloud/web/tsconfig.json           # baseUrl + paths（@/ alias）
 M rts-panel-cloud/web/vite.config.ts          # tailwindcss() 插件 + alias
?? rts-panel-cloud/web/src/index.css
?? rts-panel-cloud/web/src/lib/utils.ts        # cn()
?? rts-panel-cloud/web/src/components/ui/tabs.tsx     # 唯一保留的 shadcn 组件
?? rts-panel-cloud/web/src/components/Sidebar.tsx
?? rts-panel-cloud/web/src/components/RegionCard.tsx
?? rts-panel-cloud/web/src/components/StatsCharts.tsx
?? docs/features/desktop-ui-modernize/         # CONTEXT / 交付文档 / 截图
```

## 复现验证命令

```bash
# 前端（在 rts-panel-cloud/web 下）
pnpm install && pnpm typecheck && pnpm test && pnpm build

# 纪律守卫 + 契约（仓库根）
python -m pytest tests/test_panel_web_hygiene.py tests/test_panel_web_cols.py tests/test_panel_contract.py -q

# 端到端（真实扫描器 + 真实 Electron）
cd D:/everything/rts-dfcf-max
RTS_LOCAL_PANEL=1 RTS_LOCAL_PANEL_PORT=8899 python unified_scanner.py 60 --no-feishu --no-lock
# 另开一个 shell：cd desktop && ./node_modules/.bin/electron <probe.js>
#   probe 里 loadURL("http://127.0.0.1:8899/")，再 executeJavaScript 断言 DOM

# 打包前置门（要求 web/dist 已构建）
cd desktop && node prepare.mjs
```

**造数据**（非交易时段没有新轮次时）：`python` 里 `build_scan_view(conn)` → `serialize_view` → `local_view_cache.persist_latest`，重启扫描器即由 `load_latest` 回放。步骤见 `CONTEXT.md`「自测证据」。

## 未解决问题 / 已知限制

1. **内容少时「沪深飙升」高亮可能落在「v1 回捞」**：10 行数据 ⇒ 主区最大 scrollTop 仅 249px，`hot` 到不了阈值线。**已用 300px 高窗口复测证明算法正确**（四项导航全对）。数据量大时自然正常，**不建议**为此改算法。
2. **窄屏（≤900px）未实测**：堆叠约束有等价实现，但没在手机/小窗验过。
3. **`OFFBOARD_OPENING_SILENCE_MIN`（=15）在前端是手抄副本**（`App.tsx` 的 `offboardSubtitle` 文案里）——**存量形态、非本次引入**。若 Python 改这个常数，段标题会双端漂移，而 `test_panel_web_cols.py` 只断言字符串相等、不会预警。**建议后续**：把该常量随 `colSpec` 一并下发（要动契约，需升 `SCHEMA_VERSION`，属独立任务）。
4. **`ui/tabs.tsx` 是唯一保留的 shadcn 组件**：其余 5 个（card/button/separator/tooltip/collapsible）已作为死代码删除。若后续 Phase 3~5 需要，按 `ui/tabs.tsx` 的头注释范式重写（注意 Radix v1 是扁平命名导出）。

## 猜测 vs 已验证（明确分开）

**已验证（有命令/截图/实测数据）**
- 构建/类型/单测/守卫全绿（见上方命令）。
- Electron 实测：表格 10 行、表头 10 列来自 colSpec、11 个 sticky 首列单元格、`.tone-up` = `rgb(239,35,42)`、空态 `—（本轮无数据）`、过门标题逐字正确 + 7 项 stats、详情页 K 线 Canvas + 评分分解 + 出现史、**零 console error**。
- 导航/高亮：300px 窗口下四项导航全对；`gate` 锚点滚右栏（`gateRel=-28`）。
- 契约审查员结论：零契约改动、口径与终端一致、无第二结论源。
- 6 个 pytest 失败在干净树上同样失败（`git stash` 验证）——预存在，与本次无关。

**猜测（未验证，别当事实用）**
- 真实交易时段（10 行以外的数据量、榜内飙升/榜外异动非空时）的版面表现——实测时这两段恒空。
- 分栏拖拽后的布局持久化（`useDefaultLayout` 写 localStorage）只验证了「不报错」，没验证「刷新后宽度真的恢复」。
- 打包态（`app.isPackaged`）行为未跑——只跑了 `prepare.mjs` 前置检查。

## 下一步从哪干（Phase 3~5 候选，按性价比排序）

1. **表格微交互**（纯展示）：行刷新微闪、单元格条形背景、键盘快捷键（`j/k` 移动、`Enter` 进详情）。不动契约。
2. **Electron 壳打磨**：自定义标题栏、最小化到托盘、`⌘K` 命令面板跳股票。⚠ 会动 `desktop/main.js`，注意其「不做任何数据加工」纪律。
3. **更丰富的只读可视化**：板块分布、跨日对比。仍只消费已下发字段。
4. **窄屏适配实测**。
5. **（契约相关，需独立任务）**把 `OFFBOARD_OPENING_SILENCE_MIN` 等常量随 `colSpec` 下发，消除前端手抄副本。

## 关键文件路径

| 内容 | 路径 |
|---|---|
| 设计 token + 表格 CSS | `rts-panel-cloud/web/src/index.css` |
| 布局 / 导航 / 高亮 | `rts-panel-cloud/web/src/App.tsx` |
| 侧边导航 | `rts-panel-cloud/web/src/components/Sidebar.tsx` |
| 区块卡片（冻结首列） | `rts-panel-cloud/web/src/components/RegionCard.tsx` |
| 只读图表 | `rts-panel-cloud/web/src/components/StatsCharts.tsx` |
| 列投影 / 取值口径（**未改**） | `rts-panel-cloud/web/src/lib/cols.ts` |
| 网络收口（**未改**） | `rts-panel-cloud/web/src/api/client.ts` |
| 契约（**未改**） | `rts-panel-cloud/web/src/api/types.ts` ⇄ `scanner/panel_serialize.py` ⇄ `rts-panel-cloud/worker/src/serialize.ts` |
| 需求/方案/实施记录 | `docs/features/desktop-ui-modernize/CONTEXT.md` |
| 交付文档 | `docs/features/desktop-ui-modernize/交付文档.md` |
| 截图 | `docs/features/desktop-ui-modernize/panel-final.png`、`panel-final-nav.png`、`panel-detail-e2e.png` |
