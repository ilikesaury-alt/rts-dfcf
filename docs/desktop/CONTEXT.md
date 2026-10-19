# 本次需求 CONTEXT —— 扫描器桌面版（Electron 本地直连）

> 元信息：模型组合 = 规划/编码/审查均当前模型（pi）；测试环境 = 本机（Windows，已装 node 22 / pnpm 12 / Python 3.12）；重量档 = 🟡 标准。
> 流程：dev-task 标准档（clarify → design 对抗 → 授权闸 → implement → 自测 → review → 交付文档 → 提交 → 交接）。当前处于**授权闸**，下方方案定稿后需用户明确授权才进实现。

---

## 一、要解决的问题

现有扫描器只有两个出口：**ANSI 终端**（`scanner/view/render.py`）与**飞书卡片**（`scanner/feishu.py`），均为快照式纯文本，无法做表格交互、K 线下钻、跨区对照。用户要在**扫描机本地**有一个桌面 GUI 看板，复用已存在的 `ScanView` 数据层与已写一半的 React 前端骨架（`rts-panel-cloud/web`），**不经公网/Cloudflare**，且由桌面应用**一键拉起 Python 扫描器进程**。

### 现状盘点（已读代码确认，避免重复造轮子）

| 层 | 现状 | 桌面版能否复用 |
|---|---|---|
| Python 数据层 | `scanner/panel_serialize.serialize_view` 纯函数，把 `ScanView` → 含 `regions`/`ctx`/`stocks` 的 dict；`panel_reporter.report` 推云端 | ✅ **直接复用**契约与序列化，零漂移 |
| 契约 | `panel_serialize` ⇄ `worker/src/serialize.ts` ⇄ `web/src/api/types.ts` 三处手抄镜像，`SCHEMA_VERSION=2`，`tests/test_panel_contract.py` 兜底 | ✅ **零契约改动**，前端 `api/types.ts` 已是真类型 |
| 云端后端 | `rts-panel-cloud/worker` Cloudflare Worker+D1（ingest/regions/meta/stock） | ❌ 桌面版不用，但**其端点语义是本地 serve 的参照** |
| 前端 React | `rts-panel-cloud/web` 骨架已立（`App.tsx` 五个区块全是 `T1.5/T5.3 实现` 占位符），`client.ts` 唯一网络收口、相对路径 `/api` | ⚠️ **须填肉**：RegionTable/StatusBar/GatePanel/StockDetail + 代理改端口 |
| Electron | 无 | ❌ 全新 |

**核心结论**：桌面版不是从零建，是「复用已验证的数据契约 + 把半成品前端做完 + 用 Electron 把本地 Python 包起来」。最大工程量在**前端填肉**与**Electron 生命周期/打包**，而非数据通路。

---

## 二、范围（In / Out）

### In（本期必做）
- **本地直连架构**：Electron（Node 主进程）一键 `spawn` Python 跑 `unified_scanner.py`（带本地面板开关）；Python 主循环每轮算好 `ScanView` 后，把 `serialize_view(view)` 结果**缓存到进程内单例 + 落 `view_snapshot` 表**（新增 m021 迁移）。
- **本地只读 HTTP 线程**：绑定 `127.0.0.1`（**严禁 `0.0.0.0`**），暴露：
  - `GET /api/regions` → `{date,time,seq,regions,ctx}`（**去 stocks**，与云端 regions 端点一致；`regions` 含 main/hist/hot/offboard/gate/cols/marks）；
  - `GET /api/meta` → `{ready,date,time,seq}`（从缓存 view 取）；
  - `GET /api/stock/{symbol}/kline` → K 线（读 `daily_kline`）+ 评分分解 + 出现史（读 `recommendations`），db 现算。
- **前端填肉**：五个区块表格（`RegionTable`，吃 `colSpec` 投影，不硬编码列名）、状态条（`StatusBar`，轮次时间+滞后 N 轮提示）、过门区（`GatePanel`，标题固定含「下一张卡会推什么（非已推内容）」）、个股详情页（`Stock.tsx`：kline-component Canvas K 线+成交量 + 评分分解 + 出现史）；Vite `/api` 代理目标从 `:8787` 改为本地端口。
- **60s 轮询** `/api/regions`（保持奥卡姆，SSE 不在本期）。
- **Electron 壳**：开发态 = 主进程 `spawn` 本机 Python + 加载 dev server URL（5173）；窗口关闭时 `child.kill()` 且 Python `finally` 释放 `scanner.lock`。
- **终端继续照常**（stdout ANSI 不杀、不强制隐藏），桌面版是「第二个消费端」而非替代。
- **零第二结论源**（G5）：桌面版只渲染已有证据，不新增评分/筛选/建议。

### Out（本期不做，标注后续）
- **SSE 推送**（云端 M3）：本期用 60s 轮询，前端 `client.ts` 已有 `apiGet` 轮询骨架。
- **历史日历回看**（云端 M5 T5.1/5.2）：本期只显示「当前轮快照」，不做跨日回看 UI。
- **Cloudflare / 公网 / Access / WAF**：桌面版纯本地，这些全部不需要。
- **飞书推送配置 UI / 权重调参面板**：不在本期。
- ~~**打包成可双击 exe**~~：已纳入本期（见 Issue D，技术栈 = `python-build-standalone` + `electron-builder.extraResources`，不再列为 Out）；原拟的 PyInstaller/conda-pack 方案作废。

---

## 三、关键决策与取舍

| # | 决策 | 取舍理由 |
|---|---|---|
| D1 | **复用主循环内存 view 缓存，而非从 db 现算整份 `ScanView`** | 反例：主循环 `build_scan_view` 依赖本轮 `live_quotes`/`today_pool`/`rank_map`/本轮候选集，这些**不落库**（hot/offboard 只落各自 log 表且不含 live 行情）。从 db 现算会丢失实时涨幅/排名/新票标记，且复制第二份构建链路（违背零漂移）。→ 主循环算好后把 `serialize_view(view)` 结果缓存，本地 serve 直接回放 == 与云端版收到的是同一份（云端版就是 `serialize_view(view)` 推上去的）。 |
| D2 | **Python 侧起本地 HTTP 线程，而非 Node 侧读 SQLite** | Node 用 better-sqlite3 读 `scanner.db` 会与 Python 主循环每 60s 大量写入抢锁（WAL 下 better-sqlite3 默认阻塞式）。Python 自己 serve 只读内存缓存，零 db 写竞争，且前端 `/api` 相对路径**零改动**。 |
| D3 | **独立开关 `RTS_LOCAL_PANEL`，与 `RTS_PANEL`（云端）分离** | 两者可并存（既推云端又本地 serve），也可互斥；桌面版场景下默认开本地、不影响既有云端面板用户。 |
| D4 | **60s 轮询而非 SSE** | 需求 G2 允许 ≤70s；SSE 是云端 M3 未做项，桌面版首版无需引入复杂度。滞后提示靠 `regions` 响应的轮次时间算 N 轮。 |
| D5 | **本地 serve 端点语义对齐云端 regions/meta/stock**，但数据来源 = 内存缓存 + db 现算 | 让前端 `types.ts` / `client.ts` 完全复用，不引入第二套契约。 |

---

## 四、验收标准（写死）

1. **数据一致性**：Electron 启动、Python 首轮扫描完成后，`GET /api/regions` 返回的 `regions.main` 行数 / 首行 symbol / 顺序与同轮终端 `v1 池选` **逐条一致**；`regions.cols` 的 key 序列 == `[c[0] for c in COLS_POOL/HOT/HIST]`。
2. **五区块表格**：`main`/`hist`/`hot`/`offboard` 四区按 `colSpec` 渲染（列数==colSpec 长度、列头与 `COLS_*` 逐列一致）；空区块显 `—（本轮无数据）`；红涨 `#ef232a` / 绿跌 `#14b143`；点击行进详情路由。
3. **状态条**：显示轮次时间/seq/时段；云端（此处=本地）无响应或滞后 >1 轮时变黄 `#f5a623` 显「数据滞后 N 轮」，且不白屏。
4. **过门区**：`regions.gate` 正确渲染（main/hist/hot/offboard 各区通过行 + `stats` 七字段）；标题固定含「下一张卡会推什么（非已推内容）」；切「门内/门外」只换数据源不改标题。
5. **详情页**：点任意行 → ≤1.5s 渲染 K 线（kline-component：K 线+成交量）+ 评分分解（各项之和==总分）+ 出现史；非 v1 票显「无评分分解」；`daily_kline` 缺票显「本轮上报未包含该票」。
6. **零第二结论源**：桌面版无短名单 / 综合判断 / 持仓建议文案（G5）。
7. **生命周期**：关闭 Electron 窗口 → Python 子进程被 kill，`scanner.lock` 释放（无残留锁导致下次启动 `⛔ 已有实例`）；重启可正常起。
8. **纯本地**：不依赖公网/Cloudflare；`RTS_LOCAL_PANEL=0` 时本地 HTTP 线程不启动、行为与改前一致；`127.0.0.1` 绑定，不监听 `0.0.0.0`。
9. **终端并存**：桌面版运行时，原 ANSI 终端输出不受影响（同进程 stdout 照常）。
10. **契约守门**：`SCHEMA_VERSION` 仍 == 2；`tests/test_panel_contract.py` 仍绿（不引入契约漂移）。

---

## 五、已知风险 / 踩过的坑

- `serialize_view` 的 `stocks` 在 payload **顶层**（regions 不含），本地 `/api/regions` 须**显式去 stocks**（与云端 regions 端点一致，前端 `RegionsResponse` 无 stocks），否则详情页首屏会拉 K 线（D4 违反「列表页不碰 K 线」）。
- 本地 HTTP 线程必须用 `http.server` 绑定 `127.0.0.1`；**误绑 `0.0.0.0` = 同网段可读数据**，安全硬约束。
- 端口冲突：本地端口（默认 `8899`）可能被占；占用时主循环告警 + 桌面版显「连接失败」，不致命，端口可配置（`RTS_LOCAL_PANEL_PORT`）。
- 非交易时段主循环 `sleep` 不重算 view → 缓存是「上次交易时段最后一轮」，桌面版应显「非交易时段」而非空白（与终端一致）。
- Electron 拉起 `unified_scanner.py` 须带 `RTS_LOCAL_PANEL=1`、**不带** `--no-panel`（那是关云端）；`--no-feishu` 可选。
- 新增 `view_snapshot` 表走 `MIGRATIONS` 末尾追加（`m021`），**不得**改 `init_db` 探测块（AGENTS.md 明文）。
- Node 侧**不要**直接读 `scanner.db`（锁竞争，见 D2）。

---

## 六、已确认方案（design-plan 对抗审查结论）

### 6.1 问题本质（一句话）
在扫描机本地，用 Electron 把「已验证的 `ScanView` JSON 契约 + 半成品 React 前端」包成一个**不依赖公网、一键拉起 Python** 的桌面看板。

### 6.2 方案对比（奥卡姆）

| 方案 | 做法 | 收益 | 代价 | 风险 |
|---|---|---|---|---|
| **P1（采纳）本地只读 HTTP + 内存缓存 view** | Python 主循环每轮缓存 `serialize_view(view)` → 起 `127.0.0.1` HTTP 线程 serve；Electron 前端 `/api` 代理到该端口，60s 轮询 | 前端**零契约/零网络层改动**；**零漂移**（==云端收到那份）；不碰 `panel_serialize`/`build_scan_view`；不动 `panel_reporter` | 需新增本地 serve 模块 + view 缓存 + m021 迁移；Electron 拉起/生命周期 | 端口冲突、误绑 0.0.0.0、子进程锁未释放 |
| P2 复用 panel_reporter 改指本地 ingest | 把 `report()` endpoint 改 `127.0.0.1:<port>`，起一个只收 POST 的本地 ingest | 复用已验证上报链 | 改 `panel_reporter`（M0 已验证）有回归风险；还要起 ingest 服务；仍要从 db 重算或缓存 | 回归风险 + 多一层服务 |
| P3 stdio/IPC 直喂 JSON | 主进程 fork Python，每轮 stdout 推 JSON，Electron 解析 | 无端口 | 前端 `client.ts` 要改 IPC 而非 fetch；stdout 混日志脆弱；子进程生命周期复杂 | 解析脆弱、前端大改 |

**奥卡姆优选 P1**：假设最少（复用已验证契约 + 主循环内存 view）、最简单（前端零改动、不动已验证模块）、风险最低（不碰 M0 链路）。

### 6.3 模块边界

| 新增/改动 | 路径 | 职责 |
|---|---|---|
| 新增 | `scanner/local_view_cache.py` | 进程内单例缓存最近一轮 `serialize_view(view)` 结果（可选落 `view_snapshot` 表）；`cache_latest(payload)` / `get_latest()` |
| 新增 | `scanner/local_panel_server.py` | `http.server` 线程（绑定 `127.0.0.1`），`/api/regions`（去 stocks）、`/api/meta`、`/api/stock/{sym}/kline`（db 现算，复用 `panel_reporter._klines_for`/`_appearances_for` + score_breakdown 解析） |
| 改动 | `unified_scanner.py` | 主循环 `display()` 后：`cache_latest(serialize_view(view))` + 若 `RTS_LOCAL_PANEL=1` 起本地 serve 线程（一次）；新增 `RTS_LOCAL_PANEL` / `RTS_LOCAL_PANEL_PORT` 开关与 CLI 无关 |
| 改动 | `scanner/db/migrations.py` | 末尾追加 `m021_view_snapshot`（建 `view_snapshot` 表） |
| 改动 | `rts-panel-cloud/web/src/components/RegionTable.tsx`（新） | 五区块表格，吃 `colSpec` |
| 改动 | `rts-panel-cloud/web/src/components/StatusBar.tsx`（新） | 轮次/滞后提示 |
| 改动 | `rts-panel-cloud/web/src/components/GatePanel.tsx`（新） | 过门区两态 |
| 改动 | `rts-panel-cloud/web/src/pages/Stock.tsx` + `RowDetail.tsx`（新） | 详情页 kline-component（Canvas，零依赖） |
| 改动 | `rts-panel-cloud/web/src/App.tsx` | 用真实组件替换占位符 |
| 改动 | `rts-panel-cloud/web/vite.config.ts` | `/api` 代理目标 → 本地端口（开发态）；生产态由 Python `local_panel_server` 同源托管 `dist`，Electron 加载 `http://127.0.0.1:8899/`，免 CORS/免主进程转发 |
| 新增 | `desktop/`（Electron 工程，独立 package.json） | `main.js`（spawn Python + 加载前端 + 生命周期）、`package.json`（electron/electron-builder deps）、`build` 脚本 |

### 6.4 高风险点（权限/状态/并发/一致性/迁移回滚）

1. **本地 HTTP 绑定安全**（权限）：必须 `127.0.0.1`，误绑 `0.0.0.0` 即同网段可读。→ 代码写死 host，单测断言 `server_address[0]=='127.0.0.1'`。
2. **子进程生命周期**（状态）：Electron 关窗须 `child.kill()`；Python `SingleInstanceLock` 须 `finally: lock.release()` 释放，否则残留锁阻断下次启动。→ 端到端验证「关窗→重启可起」。
3. **进程内缓存一致性**（并发）：HTTP 线程读内存缓存，主循环写缓存；Python GIL 下单写多读安全，但须保证 `get_latest()` 在首轮完成前返回 `ready:false`（不崩溃）。
4. **迁移回滚**（迁移）：m021 建表，`check` = `_has_table('view_snapshot')`，`up` = `CREATE TABLE IF NOT EXISTS`；失败回滚不写账本（沿用 `run_migrations` 语义）。
5. **端口占用**（状态）：默认 `8899` 可配；占用时主循环打告警、serve 不启动，不阻断扫描（fail-open，与 `panel_reporter` 同纪律）。
6. **生产态渲染进程 /api 路由**（一致性）：打包后渲染进程从 `file://`/自定义协议加载，**相对路径 `/api` 无法解析到 `127.0.0.1:8899`**。最简稳妥做法 = 让 Python `local_panel_server` **同时托管构建后的 `web/dist` 静态文件**，Electron 生产态直接加载 `http://127.0.0.1:8899/`，与 API 同源 → 相对 `/api` 免 CORS、免主进程转发；开发态仍由 vite(5173) 代理到 8899（见 6.3）。

### 6.5 是否拆 Issue
**拆 4 个顺序 Issue**：
- **A（后端）**：`local_view_cache` + `local_panel_server` + m021 + `unified_scanner` 挂点 + 开关。验收：本地 curl `/api/regions` 得到与终端同轮一致 JSON；`/api/stock/{sym}/kline` 返回 K 线；`127.0.0.1` 绑定单测。
- **B（前端填肉）**：RegionTable/StatusBar/GatePanel/StockDetail（详情页 K线改 `kline-component`）+ App 替换占位符 + vite 代理改端口（`:8899`）。验收：dev server 加载后五区块/状态条/过门区/详情页渲染正确（用本地 serve 联调）。
- **C（Electron 壳·开发态）**：`findPython()`（开发态指向用户 Python）+ `spawn unified_scanner.py`（注入 `RTS_LOCAL_PANEL=1`）+ 加载本地 `/api` 或 dev server + 生命周期 + 关窗 `child.kill()` + Python `finally: lock.release()`。验收：启动后窗口出现、数据流入、关窗后 `scanner.lock` 释放、可重启。
- **D（打包 exe）**：`python-build-standalone` 3.12 进 `desktop/resources/python/`；`electron-builder.extraResources` 复制 Python + 仓库（含 `scanner.db`）；`findPython()` 打包态走 `resourcesPath`；`spawn` 用 `execFile` 绝对路径 + env 净化（去 `PYTHONHOME/PYTHONPATH/...`）；`scanner.db` 落 `userData` 保 WAL 写权限。验收：打包产物 `win-unpacked` 双击可起、Python 子进程拉起、`/api/regions` 可得、关窗锁释放。

---

---

## 七、GitHub 调研结论（技术架构 + UI + K线选型，2026-10 补充）

> 用户要求调研后再定方案。调研对原方案产生**实质性修正**：打包成 exe 反向约束架构；K线组件选型优于原计划 ECharts 双 grid。

### 7.1 技术架构参考（已采纳）
| 参考 | 结论 | 对架构的约束 |
|---|---|---|
| **Datasette Desktop**（Simon Willison）用 `python-build-standalone` 把完整 Python 打进 app | ⭐ 最权威范式 | 打包时把 standalone Python 放 `resources/python`；`findPython()` 用 `process.resourcesPath` 定位；`electron-builder.extraResources` 复制进包 |
| **Electron Express+Python 子进程打包后 ENOENT** 的坑 | 开发态可用、打包后 `spawn python` 找不到 | 必须用 `execFile` 指向 `resources/python/python.exe` 绝对路径；**净化 `PYTHONHOME/PYTHONPATH/PYTHONUSERBASE` 等 env**（避免指向用户系统 Python 串味） |
| **electron-vite / electron-react-template** | 三构建（main/preload/renderer 各自 vite）、React 19 + TS | 已有 `web/`（Vite+React19）作渲染进程；Electron 主进程用独立 vite 入口 |
| **windows-electron-starter-kit** | Windows 企业级套件（IPC 校验、日志脱敏、React19+TS） | 只在 Windows 跑，参考其日志规范（我们数据走 HTTP，IPC 需求低） |

**打包约束（从开发态就为打包准备）**：
- Python 不依赖系统装的环境，用 **`python-build-standalone` 的 portable 3.12** 放进 `desktop/resources/python/`；`electron-builder.extraResources` 复制。
- 主进程 `findPython()`：`app.isPackaged ? join(resourcesPath,'python','python.exe') : 开发态 Python`（用户已装）。
- `spawn` 用 `execFile(pythonExe, ['unified_scanner.py', ...])`，`cwd` = 仓库根，`env` = 净化 + 注入 `RTS_LOCAL_PANEL=1`。
- `scanner.db` 置于 `app.getPath('userData')`（可读写，保 WAL 写权限；`resources/` 只读不可放可写库）。

### 7.2 UI 参考（视觉范式）
| 参考 | 可取要素 |
|---|---|
| **Arc UI Market Terminal** | 三栏专业交易终端：左实时 watchlist（sparkline+闪烁报价）/ 中 Canvas K线+成交量+均线+十字光标+OHLC 浮窗 / 右板块面板；**暗色默认**。→ 映射：左=v1 池选/回捞列表，中=个股详情 K线，右=过门区 |
| **ssych ui / Candlestick Chart** | 暗色 shadcn 蜡烛图（vol 直方图+磁吸十字光标+实时 OHLC 图例），可直接取组件 |
| **OpenCharts** | `lightweight-charts` 完整终端 UI 开源实现，可作整体布局反例/参考 |
| **tradeboard / FinTrader Pro** | 高密度数据网格 + 板块热力图 + 多 pane，验证「暗色+高密度表格」是行业默认 |

### 7.3 K线组件选型（修正原计划）
原计划写「ECharts 双 grid」→ 调研后改 **`kline-component`**（纯 Canvas、零依赖、中文文档、MA/EMA/BOLL/MACD/RSI/KDJ/VOL、暗/亮双主题、滚轮缩放/平移/十字光标）。
- 零依赖 = 不引入 ECharts 重包，体积友好（契合桌面内嵌）；
- 已带 VOL 直方图 + 指标副图，比手搭 ECharts 省工且更专业；
- **评分分解 + 出现史** 仍用普通 React 组件（非图表）。

### 7.4 对下方方案的修正
- 拆 Issue 改为 **A 后端 → B 前端填肉（含 K线切 kline-component）→ C Electron 壳（开发态）→ D 打包 exe（resources/python + extraResources + findPython + env 净化）**。
- 高风险点新增：打包后 Python 路径解析、WAL 写权限（userData）、env 净化避免系统 Python 串味。

---

## 八、文档复核 + 实施授权闸（本步硬停止）

### 复核结论（逐项）
1. ✅ 验收标准已写死（第 四 节 1~10 条，均可量化/可执行断言）。
2. ✅ 范围 In/Out 无歧义，与已确认方案（6.3 模块边界）一致。
3. ✅ 方案与验收自洽：`cache_latest(view)` 直接回放主循环 view == 验收 1 的「逐条一致」；`/api/stock` db 现算 == 验收 5 详情页；`/api/regions` 去 stocks == 验收 2 列表页不碰 K 线。
4. ✅ 高风险点已标注且有应对（6.4）。
5. ✅ 上下游口径一致：复用 `serialize_view` 单一契约，前端 `types.ts` 已对齐，无下游推翻上游。

### 本轮复核修正（发现并回改的漂移）
1. ✅ 验收 5 / 二-In / 六-6.3 仍残留「ECharts 双 grid」，与 七-7.3 / 六-6.5 已定的 `kline-component` 冲突 → **全量统一为 `kline-component`**。
2. ✅ 二-Out 仍写「打包为 Phase B 不阻塞」，与用户确认「打包exe」及 Issue D 矛盾 → **划线标注已转入 Issue D**，PyInstaller/conda-pack 方案作废。
3. ✅ 八-待确认项三项（8899 / 打包 / 云端推送）用户已在确认回复中拍定，原「待确认」已失真 → **改为「已拍定项」**，仅留「打包平台覆盖」一开放问题。
4. ✅ 七-7.1 `scanner.db` 落点含糊 → 统一为 `userData`。
5. ✅ 生产态 `/api` 路由缺具体方案 → 补为 高风险点 #6（Python 同源托管 dist）。

### ⛔ 暂停待授权
方案已定稿、漂移已回改，通过对抗审查与文档复核。**按授权闸纪律，不自动进入实现**。请明确以下之一才继续：
- 「开始实现」/「已切换写代码模型」/「继续」→ 进 Step 3（按 Issue A→B→C→D 顺序，先红后绿）；
- 或针对上方任一决策（D1~D5 / 生产态 /api 路由 / K线选型）提出修改。

> **已拍定项**（用户确认回复）：① 本地端口默认 `8899` ✓；② 打包 exe **纳入本期**（Issue D，替代原 Out 的 Phase B）✓；③ 云端推送（`RTS_PANEL`）**本期先不管** ✓。
> **唯一遗留开放问题**：打包目标平台是否仅 Windows？当前按 Windows 单平台设计（`python-build-standalone` 与 `electron-builder` 需对应平台二进制；若需 macOS/Linux 须另行取对应 standalone Python 与签名）。
