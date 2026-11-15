# 桌面版「工作台动作面」· 产品 / 技术规格

> 版本：v1.0 · 2026-11-15 · **纯规格 + 原型，零生产代码改动**
> 交付：`SPEC.md`（本文）+ `prototype.html`（自包含可交互高保真原型）
> 前置：v2 视觉已定稿并 commit（`fc57a32`）；本文沿用 `docs/features/desktop-design-polish/DESIGN.md` / `preview.html` / `rts-panel-cloud/web/src/index.css`，不另起视觉体系。
> 本文件与原型**只创建新文件**；不改 `web/src/**`、`scanner/**`、`desktop/**`、`tests/**`，不动已 commit 的设计文档，不 commit、不装依赖、不跑构建。
> **本次修正（2026-11-15）只改本 `SPEC.md`**；`prototype.html` 保持原样（其「一键调研」卡片名与剪贴板等价入口文案是旧口径演示态，以本文为准），仍不改生产代码、不 commit。

> **口径说明（2026-11-15 17:05 Lichun 确认「开始吧」，按现状推进）** —— 归属权**不再作为打包前的阻塞项**：
> 1. **数据库**：桌面版与终端共用本机那份有最新数据的活库 `scanner.db`（仓库根 `D:\everything\rts-dfcf-max\scanner.db`，11-13 的 227MB 活库）；**不再**从包内冷快照（2026-10-20、146MB）种出 `%APPDATA%\rts-desktop\scanner.db` 后「各记各的库」。任务管理器 / 子进程必须与主循环指向同一份库，桌面版**不得成为第二个写者**。
> 2. **AI 分析**：主入口恒为 `python -m scanner.ai_digest`（默认 research：模型自查真实数据、出排序与判断、报告自动归档 `logs/ai_digest/YYYY-MM-DD/`、只归档不回灌、需 `MAGPIE_*` 凭据且出网）。`pick.py --clipboard` 降级为剪贴板清单的**等价 / 可选入口**，云端 `/api/ai-explain` 那套 LLM 转录**不是**本动作面口径。手工链路不再要求「按 `c` 复制 → `pick.py`」，直接跑主入口（默认读 `股票池.md` 面板原文，缺了自动回退 print → view → log）。
>
> **§2.6 第 7 条的归属权分歧按「分期已定」收口，不再阻塞打包**：P0/P1/P2 **完全不碰** `main.js` 的启动语义（§2.6 第 5 条），故打包态仍是**既有的 `takeover`**（壳拉起自己的 `unified_scanner.py` 写同一份库，与改动前逐字节一致）。`RTS_SCANNER_OWNERSHIP` 三态（`takeover` / `attach` / `refuse`）与 `/api/tasks/scanner` 原属 P3 —— **2026-11-15 17:32 Lichun 决定不实现**（实际用法是「以后多用桌面版，不会终端 + 桌面版一起用」，`attach` / `refuse` 两态没有使用场景，`takeover` 即所需）。**当前 P0 阶段「无第二写入者」判据的准确表述**：桌面壳是**该库在本机桌面路径下的唯一拉起者**（终端手动跑 `unified_scanner.py` 时由 `stop_existing_scanners` 交接），**不是**「桌面版只读」——只读约束锁的是**看板数据面**（`/api/{health,meta,regions,stock/*}`），不锁动作面（§2.1 第 3 条）。
>
> **A1（2026-11-15 17:32 Lichun 决定）—— 安装包不再携带 `scanner.db`**：① 安装包 290MB → **~63MB**；② 消灭「包内冷快照 ⇒ 首启数据陈旧」这个病根（实测旧 `userData` 副本停在 2026-10-19，落后活库 19 个交易日）；③ 降级路径改为：解析不到外部活库 ⇒ 由 Python `init_db()`（幂等建表 + 跑迁移，实测空库建 31 张表）在 `userData/scanner.db` **建空库**，桌面版自己的扫描器填起来。**代价**：全新安装且无外部活库时，首启无历史 K 线，需自行积累几轮。
>
> 保留不改：只读镜像、colSpec 单源、`SCHEMA_VERSION=2` 零契约改动、结论词 grep 守卫、Node 侧不读 SQLite（D2）、`RTS_TASKS` 缺省关、CSRF 四层防护、单槽串行、不推飞书、不改 `webPreferences`。

---

### 实现状态（2026-11-15 16:10）

P0 最小闭环已落生产代码（未 commit）：`scanner/tasks_server.py`（独立 127.0.0.1:8900 服务、
四层校验、单槽串行、白名单 catalog）、`local_panel_server.py` 对 `/api/tasks/*` 做同源反代、
`unified_scanner.py` 挂点（RTS_TASKS 缺省关）、`web/src/pages/Tasks.tsx`（AI 分析动作面，
hash 路由 `#/tasks`）、`tests/test_tasks_server.py`（10 例）。端到端实测 `--materials` → ok/exit 0、
46 行日志、坏 Origin 403。另有两条实测修正：① `--demo` 仍走 research 工具链会联网，已从 R 档改判 N 档；
② `desktop/main.js` 已改成优先解析本机最新活库（`RTS_DB_PATH`/`RTS_LIVE_DB`/`RTS_REPO_ROOT`），
不再默认从包内冷快照种独立库。

## 0. 读过的东西（本规格的事实底座）

| 来源 | 用到的结论 |
|---|---|
| `COMMANDS.md` **全文 §0–§10** | 十类几十条 python 命令 = 动作面需求真源；逐条见 §4 映射表 |
| `TECH-STACK.md` §6「真实差距清单」 | 第 7 行「命令驱动 vs 工作台驱动」标 🟡/⬜ 空白，第 8 行 a11y 只有规格未进生产；§6 结论「真正的差距在体验层，不在运行时层」 |
| `TECH-STACK.md` §5 | §5.1 保留 7 条（薄壳 / Python 数据心脏 / `127.0.0.1` 同源只读 / `userData` / 打包链 / 前端组合 / 安全基线）；§5.3 明确不做 7 条（不换壳、不引状态库/图表库/新请求、不动 `SCHEMA_VERSION`、不改关窗 kill 语义） |
| `BOUNDARIES.md` | §1 守卫清单（hygiene 6 条 / cols 6 条 / contract 9 条 / local_panel 28 条 / desktop_shell 14 条）；§2.2 **必须保留的挂载点**：`data-scroll-root="main"`、`anchor-*`、`.region-table`/`.sticky-col`/`.row-clickable`/`.cell-*` 等结构类、`#root`、`#/stock/` hash 前缀、`/api/*` + `{ok,data}` 信封 |
| `DESIGN.md` + `preview.html` | 48 / 76 / flex / 40 四段高度链；208 / flex / 386 三列；卡片 10px 圆角 + 58px head + 18px 间距；5 级字阶；hover/focus-visible/active 140ms；两处渐变白名单；`prefers-reduced-motion` 兜底；**9 个结论词禁令** |
| `rts-panel-cloud/web/src/index.css` | 实际 token：`--surface-0:#070b12` / `-1:#0c131e` / `-2:#141e2d` / `-3:#1c2a3e` / `--surface-pop:#243650`；`--brand:#4c8dff` / `--brand-soft:#22d3ee` / `--brand-wash:rgba(76,141,255,.12)`；`--text-1..4` / `--line*` / `--amber:#f5bb63` / `--coral:#ff7b8e`；`--up:#ef232a` / `--down:#14b143`；`--mono` / `--sans` / `--ease:140ms ease` / `--shadow-card` / `--shadow-pop` |
| `scanner/local_panel_server.py` | 现状 **只读 GET + 静态托管**：`/api/health` `/api/regions` `/api/meta` `/api/stock/{sym}/kline` + `_static()` SPA 回退；`BIND_HOST="127.0.0.1"` 模块常量（无 env 覆盖）；`_ok/_err` 信封；`_ensure_file_log` 的 `propagate=False`；`handle_error` 不刷终端 |
| `scanner/panel_serialize.py` / `web/src/api/types.ts` / `worker/src/serialize.ts` | `SCHEMA_VERSION=2` 三端镜像；`serialize_view` 的 7 个顶层键；`tests/test_panel_contract.py` 守字段集 |
| `desktop/main.js` / `package.json` / `docs/desktop/IMPLEMENTATION.md` §5 | `execFile` 拉 `unified_scanner.py 60`；env 净化 7 变量 + `PYTHONNOUSERSITE=1`；`userData` 可写落点 + 首启种冷快照不覆盖（**现状记录，已被 2026-11-15 口径推翻：不再种冷快照、不再分家**）；`probeMeta` 只认自己的信封；关窗 kill；**§5.3「启动会杀掉终端扫描器」** |
| `scanner/single_instance.py` / `unified_scanner.py:891–915` | `stop_existing_scanners` 按**脚本路径 + 启动时间更早**杀旧（与锁路径无关）；`--no-lock` 跳过；内核级文件锁随进程退出释放 |
| `AGENTS.md` | D2「Node 侧不读 SQLite（与主循环抢写锁）」；Fail-open 只 catch `EXTERNAL_FAILURES`、禁裸 `except Exception`；WAL 库不能 `cp`；`recommendations` 存冻结分；东财 push2 IP 限频；B 段脚本刻意不给结论；AI 归档不回灌 |
| `requirements.lock` / `requirements.txt` | 只锁 5 个直接依赖（`python-dotenv==1.0.1` / `requests==2.34.2` / `wcwidth==0.2.13` / `psutil==7.2.2` / `akshare==1.18.64`）；**自承**「numpy/pandas 等传递依赖未全量固定」；`pywencai` 被注释掉（可选） |

---

## 1. 定位与原则

> **定位一句话：** 在已定稿的 v2 只读看板旁，加一层**只负责「执行了什么、结果在哪」的本机动作面**——把 `COMMANDS.md` 里的 python 命令收进一个带任务中心的桌面操作台；看板层零改动，动作层另起。

### 原则（5 条）

1. **看板层零改动，动作层另起。** `/api/{health,meta,regions,stock/*}`、静态托管、`serialize_view`、`get_latest`、`colSpec`、区块组件、详情组件、hash 路由、四段高度链全部保持；动作面是**并列的新命名空间** `/api/tasks/*` + 独立模块 + 独立状态存储 + 独立鉴权，不塞进任何既有数据通路。
2. **不重算、不生第二份结论、不把终端逻辑搬进 Node。** 执行体恒等于 `subprocess` 复用现有 python 脚本（**不重写业务逻辑、不复制抽码/评分/排序/口径**）；Node 侧仍只做「开窗口 + 拉子进程 + 探活 + kill」，绝不读 SQLite（D2）；动作面只呈现命令原文、状态、exit code、日志与产出路径，不把报告内容解释成判断。
3. **只读是数据面的属性，不是服务的属性。** 「只读镜像」锁的是 GET 载荷与快照对象；写操作只在动作面命名空间里发生，且必须能逐条证明没碰到数据面（§2.1）。
4. **契约零漂移。** `SCHEMA_VERSION=2` 不动、三端镜像三文件不动、`web/dist` 之外的构建链不动、不新增端口、不新增前端依赖、不引状态库、不引 WebSocket/SSE 长连接（日志用增量轮询）。
5. **默认最小权限。** 动作面 `RTS_TASKS` 缺省关（与 `RTS_LOCAL_PANEL` 缺省关同纪律）；写库与外呼档必须显式确认并**全局串行**；任何任务都不推飞书、不注入 `--no-lock`、不改写 `RTS_DB_PATH`/`RTS_ENV_FILE`/`RTS_LOG_DIR`。

---

## 2. 六条架构红线（逐条正面回答，不绕）

### 2.1 「只读镜像」锁的是看板数据面，还是整个本地服务？

**结论：锁的是「看板数据面」，不是整个本地服务进程 / 端口。**

四条可验证的证据：

1. **原文作用域是端点表，不是进程。** `local_panel_server.py` 文件头写「本地**只读** HTTP 服务」，紧接着列的是那 4 个 GET 端点与「静态托管」，并特别强调「`/api/regions` 必须显式去 `stocks`」「端点语义对齐云端」「前端 `api/client.ts` / `api/types.ts` 完全复用，不引入第二套契约」。它描述的是**这 4 个 GET 的语义**，不是「本进程不得写入任何东西」。
2. **守卫锁的全是这 4 个 GET 的回放保真。** `tests/test_local_panel.py` 28 条：`/api/regions` == `serialize_view` 输出、顶层与 `regions` 都不得有 `stocks`、`cols` key 序列 == `COLS_*`、`meta` 无快照 200 `{ready:false}`、无快照 `NO_SNAPSHOT` 404、`_static` 路径穿越 404……**没有任何一条断言「本服务进程零副作用」**。
3. **同一进程早就在写库。** `local_view_cache.persist_latest()` 在主循环里 `INSERT INTO view_snapshot`（m021），`_connect()` 本来就 `get_conn()` 打开 `scanner.db`。「整个本地服务零写」从来不是事实；纪律的实际表述是 `local_view_cache.py` 文件头的「**单写多读**：只有主循环写 `cache_latest`，HTTP 线程读 `get_latest`」—— 锁的是**那份快照对象**，不是进程。
4. **`AGENTS.md` / `DESIGN.md` G5 的措辞是「面板是终端的只读镜像」**，宾语是「面板 / 看板」，约束是「不新增筛选、不新增排序、不新增结论、不改写快照」。动作面不是面板。

因此新增 `/api/tasks/*` **在以下 7 条边界内不破坏基石**（每条都可写成断言）：

| # | 边界 | 可判定守卫 |
|---|---|---|
| B1 | **命名空间隔离**：只允许 `/api/tasks/*`（含 `/api/tasks/catalog`、`/api/tasks/scanner`）；`/api/{health,meta,regions,stock/*}` 与 `_static()` 的**路由分支一字不改** | 新测 `test_task_plane_does_not_alter_readonly_routes`：对 `_route()` 源码做哈希基线比对；`test_local_panel.py` 全绿 |
| B2 | **单向依赖**：任务代码**不得** import `serialize_view` / `get_latest` / `local_view_cache` / `build_stock_payload` / `cols_payload`；不读快照、不写快照 | AST 守卫：新模块 import 图不含上述符号 |
| B3 | **GET 零副作用不变**：`do_GET` 保持原样；写操作只经 `do_POST`；`/api/tasks/*` 的 GET 只读任务元数据，不触发任何脚本 | `do_GET` 函数体哈希基线；任务 GET 用例断言无子进程被 spawn |
| B4 | **信封与 helper 复用**：动作面调用 `lps._ok()` / `lps._err()` / `_send_json()` 原函数，不另造解析器（详见 §2.2） | 新测断言 `/api/tasks` 失败响应 `== {"ok":false,"error":{"code":..,"message":..}}` |
| B5 | **回环不变**：`BIND_HOST` 常量仍是 `"127.0.0.1"`，无新 env 覆盖路径，源码仍无 `0.0.0.0` | `test_bind_host_is_loopback` / `test_desktop_binds_loopback_only` 继续绿 |
| B6 | **载荷零交集**：任何 `/api/tasks/*` 响应 `data` 中**不得**出现 `schema` / `regions` / `ctx` / `stocks` / `gate` / `cols` 任一键 | 新测 `test_task_response_has_no_board_payload_keys` |
| B7 | **不成为第二数据源**：任务产出只落 `logs/tasks/<id>/`，不进 `view_snapshot`、不进 `serialize_view`、不进飞书、不进主循环、不进任何提示词 | 新测断言 `view_snapshot` 行数在提交一个只读任务前后不变 |

**落地形态的取舍（正面给决策，不「看情况」）：**

- **决策 = 同一回环、同一端口、同一 `ThreadingHTTPServer`，但动作层「另起」**：
  - 新模块 `scanner/local_tasks.py`：`TaskSupervisor`（线程 + 队列 + 状态机 + 鉴权 + catalog 单源）+ `dispatch(handler, path, body)`。
  - `scanner/local_panel_server.py` **唯一改动** = 新增一个 `do_POST`（≤8 行）：校验路径必须以 `/api/tasks/` 开头，然后 `return local_tasks.dispatch(self, path, body)`；不进 `_route()`、不改任何 GET、不碰 `_static` / `BIND_HOST` / `_ok` / `_err` / `handle_error` / `log_message` 的函数体。
  - 理由：保留「同源免 CORS」与「一次探活」；不引入第二个端口 / 第二份 env 净化 / 第二套守卫；任务子进程与扫描器**共享同一份已净化 env 与同一个 `sys.executable`**（§2.5 的关键前提）。
- **备选（仅当上述 8 行也被判为不可接受时启用）**：独立端口 + 独立 `HTTPServer` 实例，`local_panel_server.py` 零字节改动。代价是可判定的 4 条：① 破「同源免 CORS」，渲染进程需跨源 → 鉴权与预检面变大；② `main.js` 要第二处 `probeMeta` / 第二处端口常量 / 第二组守卫；③ 任务进程要**重新做一遍** env 净化（`PYTHONNOUSERSITE=1` 等 7 变量）与 `userData` 落点推导，极易漏（§6 的实测坑会复现）；④ 动作面拿不到主循环的 `round_gate`（§2.4），写库任务与主循环写事务的协同退化为「只靠 `busy_timeout` 硬等」。**启用判据**：`test_local_panel.py` 中任何一条因 `do_POST` 的存在而变红 —— 此时才切备选，否则不用。

> 一句话红线结论：**写操作不是加进「只读服务」，是加在「只读数据面」旁边、与它物理隔离的控制面；只读服务的契约、守卫、信封、回环、静态托管一个字节都没松。**

### 2.2 为什么新增端点 ≠ 改 `SCHEMA_VERSION=2` 三端镜像载荷？信封怎么对齐？

**`SCHEMA_VERSION` 的版本对象是「上报载荷」**，不是「本机 HTTP 服务的所有响应」。它的三个镜像各司其职：

| 文件 | 角色 | 为什么**不碰** |
|---|---|---|
| `scanner/panel_serialize.py` | **生产者**：`serialize_view(view)` 把 `ScanView` 结构转换成 7 键载荷，`SCHEMA_VERSION=2` 写进 `payload["schema"]` | 动作面的响应不是 `ScanView` 的函数输出，也不是一轮扫描结果；往这里加字段 = 凭空造一份「第 8 键」，会让 `test_panel_serialize.py:279-298` 的顶层字段集精确相等断言立刻红 |
| `rts-panel-cloud/worker/src/serialize.ts` | **云端 ingest 契约**：`export const SCHEMA_VERSION = 2`，`tests/test_panel_contract.py:43-45` 用正则读它，`ingest.ts` 拿它做 400 `SCHEMA_MISMATCH` | 云端**根本不接收** `/api/tasks/*`（纯本机、无上报、无账号、无云）；给它加任务类型 = 让一个不存在的消费方去解析不存在的数据 |
| `rts-panel-cloud/web/src/api/types.ts` | **前端手抄镜像**：`Payload` / `Regions` / `Ctx` / `Stock` / `Envelope<T>` / `ApiErrorCode` | 任务视图是 `TaskView`，与 `Payload` 无任何字段交叠；`types.ts` 的注释自称「手抄镜像…改契约必须双端同改」，这里的「契约」明确指 `serialize_view` 那份。**改它反而是把动作面误登记成看板契约** |

`tests/test_panel_contract.py` 守的是「`serialize_view` 的键 ⊆ `serialize.ts` 源码文本」+ `SCHEMA_VERSION==2` + `STOCK_QUOTA`/`BODY_LIMIT`/`marks` 形态，**它不读 `local_panel_server.py` 的路由表**。新增本机专属端点在它的观测面之外，故三端不动、版本不升、云面板不发版。

**响应信封对齐方式 = 直接复用，不是「设计一套相似的」：**

```
成功： lps._ok(data)          → 200 {"ok":true,"data": <TaskView | TaskView[]>}
失败： lps._err(code, message)→ 4xx/5xx {"ok":false,"error":{"code":"...","message":"..."}}
```

- 用到的就是 `local_panel_server.py:78-84` 那两个**原函数**，以及 `_send_json()` 的 `Content-Type: application/json; charset=utf-8` + `Cache-Control: no-store` + 显式 `Content-Length`（HTTP/1.1 keep-alive 纪律不破）。
- HTTP 状态沿用现有语义：`200` 成功、`400` `BAD_REQUEST`、`403` `FORBIDDEN_ORIGIN`、`404` `NOT_FOUND`、`409` 冲突、`500` `INTERNAL`、`503` `TASKS_DISABLED`。
- `error.code` 仍是机器分支依据、`message` 仍是给人看的可变文案 —— 与 `api/client.ts` / `client.test.ts` 的「按码分支，不解析 message」纪律一致。
- **前端收口**：生产落地时只在 `rts-panel-cloud/web/src/api/client.ts` 里扩 `apiTasksGet/apiTasksPost`（复用既有 `apiSend` POST 包装，`client.test.ts` 已覆盖 `apiSend` POST 且无 `Authorization`），**不新增网络模块、不裸 `fetch`**（`test_panel_web_hygiene.py::test_no_bare_fetch_outside_client` 会扫到任何新 `.ts/.tsx`）。`types.ts` 里可以新增 `TaskView` 类型而不改任何既有类型 —— 但**规格要求连这个都不做**：P0/P1 的任务类型放在新增的 `lib/taskTypes.ts`（新文件、纯类型、无运行时依赖），`api/types.ts` 保持零 diff，让「三端镜像零改动」可以被 `git diff --stat` 一眼验掉。

### 2.3 只绑 `127.0.0.1` 不变，但「本地可写」把 CSRF 风险面变了 —— 具体防护

回环 ≠ 安全：本机任意网页（`http://evil.example` 或 `file://`）都能向 `127.0.0.1:8899` 发 POST。浏览器对**简单请求**（`Content-Type` 为 `application/x-www-form-urlencoded` / `text/plain` / `multipart/form-data`）不发预检、直接送达；`<form>` 更是天然简单请求。GET 只读时这最多泄露一份快照，POST 可写时就是「网页替用户跑 `backfill_kline.py`」。给四层**可判定**防护：

| 层 | 规则（写操作 `/api/tasks/*` 的 POST 全部强制；GET 端点不受影响） | 拦住什么 |
|---|---|---|
| **L1 Host 白名单** | `Host` 必须逐字等于 `127.0.0.1:<port>` 或 `localhost:<port>`，且 `<port>` == 实际绑定端口；否则 `403 FORBIDDEN_ORIGIN` | **DNS rebinding**（把域名解析到 127.0.0.1 后 Host 仍是攻击者域名） |
| **L2 Origin 精确匹配** | `Origin` 必须存在且 `== http://127.0.0.1:<port>`（Electron 渲染进程同源 POST 会带）；其余一律 403 | 任意网页的 fetch / XHR；`<form>` 提交的 `Origin` 也不是本机 |
| **L3 一次性 token + 自定义头** | 服务每次启动 `secrets.token_urlsafe(32)`；所有写请求必须带 `X-RTS-Task-Token: <token>`。token 由 `desktop/main.js` 生成 → 经 env `RTS_TASK_TOKEN` 传给 Python 子进程 → 由主进程 `session.webRequest.onBeforeSendHeaders`（filter 仅 `http://127.0.0.1:<port>/api/tasks/*`）**注入到请求上**；**渲染进程 JS 从不持有 token**，页面里查不到它。服务端对 `/api/tasks/*` **不返回任何 `Access-Control-Allow-*` 头** | 跨域带自定义头必触发 CORS 预检 → 无允许头 → 浏览器直接拦掉；即使拿到页面也没法伪造 token |
| **L4 Content-Type 白名单** | 只接受 `application/json`（且 body ≤ 16 KB）；`text/plain` / `x-www-form-urlencoded` / `multipart/form-data` / 缺失一律 403 | HTML `<form>` 与「伪装成简单请求的 fetch」绕过预检 |

附加硬规则：

- **不用 Cookie / Session / Basic Auth**：不引入 CSRF 的经典载体；token 每进程一次性、进程退出即失效、**不进 `.env`、不进包、不进 `web/dist` 静态文件、不进任何日志、不进任务 env 回显**。
- **缺省关**：`RTS_TASKS` 未设 / `0` / 空 ⇒ 所有 `/api/tasks/*` 返回 `503 TASKS_DISABLED`，且 `do_POST` **不启动任何子进程**；只有显式 `RTS_TASKS=1` 才启用。与 `RTS_LOCAL_PANEL` 缺省关、`RTS_TASKS` 与 `RTS_PANEL` 相互独立，同一纪律。
- **开发态旁路**：不经过 Electron 时（直接浏览器打开 `127.0.0.1:8899`），token 由 `RTS_TASK_TOKEN` env 提供，且必须 `RTS_TASKS=1`；文档写明「开发态不要把 token 贴进页面/仓库」。
- **Node 侧仍不读 SQLite**（D2）：`onBeforeSendHeaders` 只做 header 注入，不做任何数据加工；`contextIsolation:true / nodeIntegration:false / sandbox:true` 与 `shell.openExternal` 外链策略不变。
- 验收：`curl -H 'Origin: http://evil.example' -H 'Content-Type: application/json' -d '{}' http://127.0.0.1:8899/api/tasks` → **403** 且任务列表零新增；缺 token / 错 token / `Host: evil.example` / `Content-Type: text/plain` 四种各 403。

### 2.4 任务执行与扫描器主循环并发 —— 逐条规避

先摆事实：`scanner/db/schema.py::get_conn()` 已把 `timeout=10` / `PRAGMA busy_timeout=10000` / `PRAGMA journal_mode=WAL` 三件套收口（「WAL 下写者不阻塞读者」），但 **WAL 下多个写者仍然互斥**；主循环每 60s 写一批（`recommendations` / `daily_kline` / `view_snapshot` / `crosssection_signal` …）。

| 风险 | 规避方案（可判定） |
|---|---|
| **SQLite WAL 写锁** | ① 任务子进程必须走同一个 `get_conn()`（脚本本来就这么写，禁止工作台绕过去拼 `sqlite3.connect`）；② **写库档全局单槽** `exclusive_slot`（容量 1，与外呼档共用），拿到槽才 spawn，拿不到保持 `queued`；③ 任务管理器与 `run_scanner` **同进程**，用进程内 `round_gate`：主循环每轮写事务提交后 `tasks.notify_round_done()`，写库任务在 spawn 前 `wait_for_round_boundary(timeout=70s)`；超时未等到 → `cancelled` + `TASK_BUSY`，**绝不硬闯**；④ 子进程若在 `busy_timeout` 内拿不到锁就 `failed`，不重试、不无限等。WAL 备份纪律（`Online Backup API` / 不 `cp`）不因动作面改变 |
| **单实例锁** | `SingleInstanceLock` 是 `logs/scanner.lock` 上的内核文件锁，锁的是 `unified_scanner.py` 这一个主循环。任务 subprocess 跑的是**别的脚本**（`diag_health.py` / `stock_report.py` / `backfill_kline.py` …），`single_instance._runs_script()` 按 `unified_scanner.py` 路径匹配，**既不会被误杀也不会误抢锁**。任务管理器**不碰** `scanner.lock`、不读写 `.pid`。`unified_scanner.py` 本身**不进普通任务目录**，只由 P3 的唯一所有者管理（§2.6） |
| **`scanner.db` 争用（共用本机活库，不再分家）** | **新口径（2026-11-15）**：桌面版与终端共用本机那份有最新数据的活库 `scanner.db`（仓库根 `scanner.db`）；打包态**不再**从包内冷快照种出 `userData/scanner.db` 独立库，也不再「各记各的」。任务管理器 / 任务子进程必须与主循环 `run_scanner` 指向**同一份库**：继承扫描器进程那一份 `os.environ`（`RTS_DB_PATH` 解析到同一份本机活库，`RTS_LOG_DIR` / `RTS_ENV_FILE` / `PYTHONNOUSERSITE=1` 与 7 个净化变量逐项相等），`cwd = repoRoot()`，解释器用 **`sys.executable`**。**禁止**另拼 env / 用系统 Python / 把 `RTS_DB_PATH` 改指 `userData/scanner.db` 或包内冷快照。**桌面版不得成为第二个写者**：不另起会写这份库的独立主循环；写库档仍走唯一主循环的 `round_gate` + `exclusive_slot` 单槽串行，只读档对该库只读。`test_desktop_shell.py` 的 env 净化守卫继续绿 |
| **重复推飞书** | 唯一会推飞书的是主循环（`feishu.py` + `push_gate.py`）；`buy_priority_report.py` 明确「不进主循环/终端/飞书」，`ai_digest` 明确「不进库、不进飞书、不进提示词」。规避：① 工作台**不把 `unified_scanner.py` 做成一键任务**；② 任务 env **不注入也不放开** `RTS_FEISHU_WEBHOOK` / `RTS_PUSH_GATE`；③ 主循环只有一个（单实例锁 + P3 唯一所有者）；④ catalog 里每条命令硬标 `outbound: none\|eastmoney\|xueqiu\|feishu\|ai_gateway`，`feishu` 值恒为空集，新测断言 |
| **重复外呼（AI 网关 / 东财 / 雪球 IP 限频）** | ① 外呼档与写库档**共用同一把 `exclusive_slot`**（因为东财 push2 有 IP 级限频，`AGENTS.md` 记「调试频繁 ulist 也不通，别误判代码故障」）；② 幂等：提交带 `idempotencyKey`（或 `catalogId+args` 指纹），5 分钟内重复 → `200` 返回既有 `task_id` + `deduplicated:true`，不二次 spawn；③ 同一 `catalogId` 60s 提交节流；④ 超时硬上限（外呼 180s，`--ai N` / `--max-calls` 类 900s），超时 `terminate()`→5s→`kill()`；⑤ **P0/P1 默认选零网络档**（`--no-ann` / `--materials` / `--offline-demo` / `--no-fetch` / `--quick`），把出网留到 P2 且必须过确认；⑥ **AI 分析主入口 `python -m scanner.ai_digest`** 的 `exit code` 语义原样呈现（1 未配凭据 / 2 上游失败 / 3 触红线 / 5 幻觉巡检），工作台不替脚本重试 |
| **主循环被任务拖慢 / 效率前提** | `AGENTS.md`「效率是前提，慢手段只能进离线旁路，不进主循环/终端/飞书」。规避：`TaskSupervisor` 是 daemon 线程，不参与 `scan_with_raw`；只读任务可并发 3、`exclusive` 1、总 `running ≤ 4`；任务 stdout/stderr 一律写文件（`propagate=False` 同款纪律），**绝不回灌扫描器终端**（`handle_error` 的「终端是主输出面」同理）；任务不进主循环、不进终端渲染、不进飞书 |

### 2.5 打包态：standalone Python 能不能跑？依赖 / 落点 / env / 日志

**依赖清单覆盖吗 —— 分档给可判定结论**（`requirements.lock` 只有 5 个直接依赖，且**自承**「numpy/pandas 等传递依赖未全量固定」，`pywencai` 在 `requirements.txt` 里被注释掉）：

| 档 | 命令示例 | 打包态能跑吗 | 门禁 |
|---|---|---|---|
| **D0 纯标准库 + 已锁 5 依赖** | `-m scanner.hot_watch --offline-demo`、`-m scanner.historical_watch --offline-demo`、`-m scanner.offboard_watch --offline-demo`、`-m scanner.crosssection --health`、`python diag_health.py`、`python diag_capture.py`、`python query_today.py`、`python query_summary.py`、`-m scanner.ai_digest --materials`、`python scripts/buy_priority_report.py --no-ann` | ✅ 能（零网络、零新增 import） | **P0 只放这一档** |
| **D1 触发 `akshare` / `numpy` / `pandas` 传递依赖** | `python today_report.py`、`python stock_report.py 300319 --quick`、`python prevday_perf.py`、`-m scanner.backtest --days 60 --metric next_day_pct` | 🟡 **未保证**：lock 未固定传递依赖，`fetch-python.mjs` 装的版本可能漂移（`requirements.txt` 自己写了「无上界时 CI 每次全新安装都会拉最新版…静默返回不同数据」） | 进 catalog 前必须在 **standalone `python.exe`** 里真跑 `python -c "import <script 的模块级依赖>"`，逐条 exit 0；把 `prepare.mjs` 现有的 `import unified_scanner` 前置扩成「catalog import 清单」，任一失败即 `prepare` 红 |
| **D2 需可选 / 未声明依赖** | `pywencai` 路径（`fundamentals.py` 兜底）、`ths_api_probe.py` | ⛔ 默认跳过（`fundamentals.py` 已「未安装自动跳过」）；工作台不把「可选依赖」做成一键任务 | catalog 标 `requiresOptional: true` 且默认隐藏 |
| **D3 需 `MAGPIE_*` 凭据 + 联网（AI 分析主入口的默认档）** | `python -m scanner.ai_digest`（**AI 分析主入口**，默认 research）、`python pick.py --clipboard`（剪贴板清单的等价 / 可选入口）、`buy_priority_report.py --ai N` | ⛔ **打包态默认缺凭据**（`.env` 不进包）⇒ 脚本 exit 1 | 工作台把「未配凭据」当**可呈现的 failed 态**（显示 exit 1、日志与 `logs/ai_digest/YYYY-MM-DD/` 归档路径），不崩、不重试、不弹内部纪律文案；P2 才开放 |
| **D4 会写库 / 会覆盖冻结值** | `backfill_kline.py`、`backfill_market_index.py --overwrite-live`、`ann_cache_sync.py --migrate-legacy` | ⚠ 能跑但**危险** | P2 才开放，且默认带 `--dry-run`，必须过确认弹层 |

> **结论（不「看情况」）**：P0 catalog **只能是 D0**；任何命令进 catalog 的准入判据 = 在 `resources/python/python.exe` 下 `python -c "import …"` 全 exit 0 **且** `outbound=none` **且** `writesDb=false`。这一条写进 `prepare.mjs` 前置，失败即打包红 —— 复用了 §6「`python-dotenv` 漏声明，打包才炸」的实测教训。

**可写落点**：任务历史 / 日志 / 产出（`--out`）一律写 `LOG_DIR`（`resolve_log_dir()` → 打包态 `%APPDATA%\rts-desktop\logs`），新增子目录：

```
<LOG_DIR>/tasks/<task_id>/
  meta.json        # 状态机、argv（脱敏）、catalogId、安全档、起止时刻、exitCode
  stdout.log       # 增量轮询的字节源
  stderr.log
  artifacts/       # --out 的报告只准落这里
```

- **绝不写 `resources/repo`**（安装态可能只读）；若任务管理器自身需要持久化，用 `<userData>/tasks.db`，**不新增第二份 `scanner.db`**（本机活库唯一）。
- **WAL 不进包 / `.env` 不进包**两条纪律保留；**「种子不覆盖 / 从包内冷快照种独立库」已被 2026-11-15 口径推翻**：打包态不再种 `userData/scanner.db`，桌面版与终端共用本机最新活库 `scanner.db`；任务、主循环、桌面版读到的都是这同一份，不存在「第二个库」，也不允许出现第二个独立写者。
- **`--out` 路径守卫**：只接受**相对文件名**，服务端拼到 `artifacts/` 下；绝对路径、`..`、盘符、`%5C` 变体一律 `400 BAD_REQUEST`（复用 `_static()` 的 `normpath + startswith` 同款思路）。
- **AI 归档路径（「结果在哪」）**：AI 分析主入口默认归档 `logs/ai_digest/YYYY-MM-DD/`。注意 `archive.DEFAULT_ARCHIVE_DIR` 是**相对 `cwd`**（`repoRoot()`），而打包态 `RTS_LOG_DIR` 指向 `userData/logs` —— 两者在打包态可能不一致，工作台「结果在哪」必须回显**实际解析到的归档目录**而非硬编码；统一方式留实施期由 Developer / Operator 定，本次不改生产代码。

**env 净化**：任务 subprocess 用 `env = os.environ.copy()`（扫描器进程已净化的那份）+ `cwd=repoRoot()` + `sys.executable` + `shell=False` 的 `list[str]` argv。**不重新拼 env、不引入 `PYTHONPATH`、不把 `RTS_DB_PATH` 改指 `userData/scanner.db` 或包内冷快照**；额外只加非 Python 变量 `RTS_TASK_ID` / `RTS_TASK_DIR`。`SCRUBBED_ENV` 7 变量与 `PYTHONNOUSERSITE=1`、`PYTHONUNBUFFERED=1`、`PYTHONIOENCODING=utf-8` 天然继承（后者解决 `label_registry` 等需要 UTF-8 控制台的脚本）。

**日志落哪 / 不刷终端**：沿用 `_ensure_file_log` 的动机 —— 扫描器 stdout 是主输出面，`socketserver` 默认把 traceback 打 stderr 会刷屏。任务 stdout/stderr **只落文件**，UI 只从 `GET /api/tasks/{id}/log?offset=` 拉；任务 logger 同样 `propagate=False`。总日志量裁剪：保留最近 `MAX_HISTORY=200` 条任务的 `meta.json`（不可删，用于孤儿清扫），单任务 `stdout.log` 超 `MAX_LOG_BYTES=2 MiB` 只截尾部并记 `truncated:true`。

### 2.6 「桌面壳启动会杀掉终端扫描器」 vs 「工作台接管启停」—— 可判定方案

**事实**（`IMPLEMENTATION.md` §5.3 + `unified_scanner.py:905-910` + `single_instance.py::stop_existing_scanners`）：`main()` 在 `not args.no_lock` 时按**脚本路径 + 启动时间更早**结束旧的 `unified_scanner.py` 进程（与锁路径无关），`main.js` 用 `execFile(python, ["unified_scanner.py","60",…])` 拉子进程 ⇒ 双击 exe 必然接管终端扫描器。这是主程序**既有行为**，不是壳引入的。

**矛盾点**：若工作台出现「启动 / 停止扫描器」按钮，而壳在**用户没点任何按钮时**已经替他做了一次「停止别人的 + 启动自己的」，那按钮语义与启动语义自相矛盾，用户会看到「我没点启动，终端扫描器却没了」。

**消解 = 单一所有者 + 所有权三态 + 时间点隔离**（全部可观测，无「看情况」）：

1. **唯一所有者 = 桌面壳拉起的那个 `child`**。工作台「扫描器状态」只反映**这个子进程**的存活，判定式固定为三元组：`child !== null` ∧ `probeMeta(port)` 命中我们自己的 `{ok,data}` 信封 ∧ `/api/meta` 的 `(date,time)` 在最近 2 轮内 ⇒ `运行中`；否则 `未运行` / `滞后`。**不扫全系统的 `unified_scanner.py`**，也不把 `scanner.lock` 的 `.pid` 当状态真源（它只用于冲突提示）。
2. **所有权三态由 `RTS_SCANNER_OWNERSHIP` 显式指定，默认 `takeover`（与现状逐字节一致）**：
   - `takeover`（默认）：保持既有「杀旧 → 独占」。壳启动成功后，动作面状态卡**立刻**显示一行「本机扫描器由本窗口管理」—— 把隐式的接管变成**可见的、一次性的**所有权声明。
   - `attach`：壳**不拉起**子进程，只 `loadURL` 到已存在的 `http://127.0.0.1:<port>/`（要求终端扫描器已用 `RTS_LOCAL_PANEL=1` 起服）。零杀、零重复推飞书；工作台启停按钮置灰并标「由终端实例提供」。
   - `refuse`：`probeMeta` 已命中自己的信封 **或** `scanner.lock` 的 `.pid` 指向活进程 ⇒ 壳**不启动、不杀**，加载错误页提示「已有实例在运行，可用 attach 模式打开」。
   - 判据只有上述可观测信号，不需要猜「用户是不是想保留终端」。
3. **工作台的启停只作用于 `child`，且只在窗口创建之后**：
   - `stop` = `proc.kill()`（内核级文件锁随进程退出释放，既有纪律不变）；
   - `start` = 重新 `startScanner()`，**前置硬判据** `child === null` ∧ `probeMeta(port) === false`（锁已释放）；否则 `409 SCANNER_BUSY`；
   - **绝不调用 `stop_existing_scanners` 去杀别人**；`--no-lock` 永不从表单透传（catalog 白名单硬拒）。
4. **时间点隔离**：接管发生在 `createWindow()` 之前的启动阶段；工作台启停发生在之后的交互阶段。两者不是同一个动作，也不重叠 ⇒ 不自相矛盾。
5. **分期**：P0/P1/P2 **完全不碰** `main.js` 的启动语义（沿用 §5.3 差异记录，工作台只显示状态、启停按钮置灰标「由桌面壳管理」）。**P3 的 `RTS_SCANNER_OWNERSHIP` 三态与 `/api/tasks/scanner` 已于 2026-11-15 决定不实现**（Lichun 实际用法：只用桌面版，不与终端并行 ⇒ `attach` / `refuse` 无使用场景；`takeover` 即所需）。若日后用法改变需重启这项，则 `tests/test_desktop_shell.py` **追加**守卫（14 条既有字面量一个不动）。
6. 保留既有开发态旁路 `RTS_DESKTOP_SCANNER_ARGS="--no-lock --no-feishu"`（`main.js` 文件头注释里的正当联调用法），规格注明「这是人工调试口，工作台表单不得复用」。
7. **原「残留待拍板」已按分期收口（2026-11-15 17:05，17:32 终局）**：本条原为「实施前需 Lichun 定：打包态默认是否改为 `attach`（壳只连终端实例、零写），或桌面版对该库只读、启停一律交终端」。**结论：按既有 `takeover` 推进** —— 依据是 §2.6 第 5 条（P0/P1/P2 完全不碰 `main.js` 的启动语义）与 §2.1 第 3 条（只读锁的是看板数据面，不锁动作面）；`takeover` 与改动前**逐字节一致**，不引入新行为。三态切换（`takeover` / `attach` / `refuse`）与 `/api/tasks/scanner` **决定不实现**（2026-11-15 17:32：实际用法是「以后多用桌面版，不会终端 + 桌面版一起用」，另两态无使用场景）。

---

## 3. 安全分级（三档 · 执行前确认 / 并发 / 超时 / 日志留痕）

> **分类（§4 的 A/B/C/D）是「交互形态」，安全档是「执行纪律」，二者正交**：任何 `W`/`N` 档无论归 A/B/C，最终「执行」都必须过确认弹层。`C 需确认` 特指「本身零参数、但因 W/N 必须确认才能跑」的一键形态。

| 档 | 定义 | 执行前确认 | 并发限制 | 超时（硬上限） | 日志留痕 |
|---|---|---|---|---|---|
| **R 只读** | 零写库、零外呼、零副作用；只读 `scanner.db` / `logs/` / 内置样本；`writesDb=false ∧ outbound=none` | **无**，一键执行 | 最多 **3** 并发（与总 `running ≤ 4` 取小）；`queued` 上限 20 | **60s**（`--offline-demo` / `--materials` / `--no-ann` 档 30s） | `task_id` / `catalogId` / 脱敏 argv / `createdAt·startedAt·finishedAt` / `exitCode` / `stdout.log` 字节数 / `artifacts[]`；日志落 `logs/tasks/<id>/`，meta 不可删 |
| **W 写库** | 会 `INSERT/UPDATE/DELETE` `scanner.db`（`backfill*` / `repair*` 真写 / `--apply` / `--backfill` / `--migrate-legacy` / `--overwrite-live`） | **必须显式确认弹层**：列出命令原文、目标表 / 影响面、是否 `--dry-run`、`RTS_DB_PATH`、预计耗时；且「确认执行」默认 disabled，须勾选一个显式 checkbox 才启用。**默认一律带 `--dry-run`**，去掉 dry-run 要二次勾选 | **全局串行 1**：`exclusive_slot` 容量 1（与 N 档共用）；且必须过 `round_gate`（等主循环本轮写事务提交，70s 等不到 → `cancelled` + `TASK_BUSY`）；排队态 `queued` | **600s**（回填类可 1800s，但必须在 catalog 里逐条声明） | R 档全部 **+**：确认时刻、确认者「本机」、`dryRun: true/false`、DB 路径、`round_gate` 等待时长、任务前后不主动 `wal_checkpoint`；日志**不可删**（供孤儿清扫与审计） |
| **N 外呼** | 联网 AI 网关（MAGPIE）/ 东财 / 雪球 / 公告 / 龙虎榜 / 分时；`outbound ∈ {ai_gateway,eastmoney,xueqiu}`；**AI 分析主入口 `python -m scanner.ai_digest` 默认档归此类** | **必须显式确认弹层**：列出会访问哪类上游、是否消耗凭据、**是否会自动归档文件**、是否推飞书（恒为「否」）、调用预算 / 预计耗时；`--ai N` / `--max-calls` 需填预算 | **全局串行 1**（与 W 共用 `exclusive_slot`，理由见 §2.4 东财 IP 限频）；幂等键 5 分钟去重；同 `catalogId` 60s 节流 | **180s**（`--ai N` / `--max-calls` 类 900s） | R 档全部 **+**：上游类别、幂等键、**不记任何凭据 / token**、`exit code` 语义原样（1/2/3/5）、产出路径与「结果在哪」（含 AI 分析主入口自动归档的 `logs/ai_digest/YYYY-MM-DD/`）；上游失败不重试 |

**横切硬规则**

- **写库类必须显式确认且串行化**：`POST /api/tasks` 若 `level=W|N` 而请求体没有 `confirm:{acknowledged:true, shown:[...]}` → `400 TASK_CONFIRM_REQUIRED`，不 spawn；`exclusive_slot` 被占时新任务只能 `queued`，`GET /api/tasks?status=running` 中 W+N 合计恒 ≤ 1。
- **飞书永不入档**：catalog 的 `outbound` 枚举里没有 `feishu`；任何任务都不注入 / 不放开 `RTS_FEISHU_WEBHOOK` 与 `RTS_PUSH_GATE`。
- **失败 fail-open 边界**：任务管理器自身异常（写日志失败、meta 落盘失败）按 `AGENTS.md` 只 catch `EXTERNAL_FAILURES`（`OSError`/`timeout`/`requests`/`sqlite3.Error`/`ValueError`/`KeyError`），编程错误冒泡，不裸 `except Exception`；任务失败**不阻断**扫描主循环（与 `panel_reporter` / `start_server` 的同款纪律）。
- **不重算、不第二结论源**：`--mode analyze` / `--mode transcribe` / `buy_priority_report.py` 的报告内容**只作为文件产出**，UI 不渲染成判断；`ai_digest` 档案「只归档、不回灌」纪律不变。
- **AI 分析主入口的分级（2026-11-15 口径）**：`python -m scanner.ai_digest`（默认 research）属 **N 档** —— 出网、耗 `MAGPIE_*` 凭据、**会自动归档文件**到 `logs/ai_digest/YYYY-MM-DD/`，必须显式确认 + `exclusive_slot` 全局串行 + 幂等，失败按 exit code 1/2/3/5 原样呈现，**不重试、不把报告渲染成判断**；`--materials` 属 **R 档零出网零写库**（工作台侧对 R 档任务不注入 / 不放开 `MAGPIE_*`，把「零出网」写成可判定断言），无需确认、可一键；`--demo` / `--json --demo` **只跳过读库，仍走 research 工具链会联网**，实测 2026-11-15 归 **N 档需确认**；`pick.py --clipboard` 与主入口同属 N:ai，但只是剪贴板清单的**等价 / 可选入口**。「结果在哪」只回显 `logs/ai_digest/YYYY-MM-DD/` 归档路径，不回显报告内容。

---

## 4. 命令 → 工作台映射表（`COMMANDS.md` 逐条）

> **分类**：`A` 可一键化（零参数）｜`B` 需参数表单｜`C` 需确认（本身零参数但 W/N）｜`D` 不建议搬（危险或低频/开发向）
> **安全档**：`R` 只读｜`W` 写库｜`N` 外呼（`N:ai`=AI 网关 / `N:em`=东财 / `N:xq`=雪球）
> **执行体恒为 subprocess 复用原脚本**；`argv` 由服务端 catalog 白名单拼装（`shell=False`），前端只传 `catalogId` + 白名单参数值，**永不传整条命令字符串**。

### 4.1 §0 最常用 5 条 / §1 日常扫描与开关

| 命令 | 分类 | 档 | 理由 |
|---|---|---|---|
| `python unified_scanner.py` | **D** | — | 长驻主循环，壳已独占（单实例锁 + `stop_existing_scanners`）；做成一键任务 = 第二个写者 + 重复推飞书。P3 由唯一所有者的启停接管，不进任务目录 |
| `python unified_scanner.py 120` | **D** | — | 同上；间隔参数归「扫描器状态与开关」面板（P3），不是一次性任务 |
| `python unified_scanner.py --no-feishu` | **D** | — | 同上；且工作台不提供放开飞书的开关，只在 P3 的启动参数里做只读回显 |
| `python unified_scanner.py --no-panel` | **D** | — | 会关掉看板本身（看板靠 `RTS_LOCAL_PANEL`，`--no-panel` 只关云端上报，语义易混）；不给用户这个开关 |
| `python unified_scanner.py --no-lock` | **D（禁搬）** | — | **危险**：跳过单实例锁「会和已有实例抢库」（`COMMANDS.md` §1 原文），与 §2.4 全部规避方案冲突；catalog 白名单硬拒该 token，新测断言 |
| 终端按 `c` 复制两区完整原文 | **D（收编，不模拟按键）** | — | 是主循环内键盘交互（`scanner/terminal_copy.py`），工作台不在那个进程。**新口径不再要求先按 `c`**：直接一步提交 AI 分析主入口 `python -m scanner.ai_digest`（默认读 `股票池.md` 面板原文）；剪贴板只作可选等价入口（见 §4.3），**不重写抽码 / 提示词** |
| `RTS_CROSSSECTION=0` / `RTS_HOT_WATCH=0` 等 §8 开关 | **D（映射到状态面板，不是任务）** | — | env 开关归 P3「扫描器状态与开关」；`RTS_DB_PATH` / `RTS_ENV_FILE` / `RTS_LOG_DIR` **禁止**工作台改（新口径下尤其不得把 `RTS_DB_PATH` 改指分家库 / 包内冷快照） |

### 4.2 §2 看盘 / 报表 · §2.1 买入优先级反馈报告

| 命令 | 分类 | 档 | 理由 |
|---|---|---|---|
| `python today_report.py` | **A** | R | 默认最近有数据的交易日，零参数、零写库；报告只落 stdout/产出文件 |
| `python today_report.py --date 2026-11-03 --top 10` | **B** | R | 日期 + 条数需表单（date 输入 / top 数字），仍只读 |
| `python today_report.py --json` | **A** | R | 固定参数、机器可读，最适合工作台逐行回显；一键 |
| `python prevday_perf.py` | **A** | R | 历史复盘汇总，零参数、只读 |
| `python prevday_perf.py --days 0` | **B** | R | `days` 需输入（0 = 不限天数，要在表单里说明口径） |
| `python prevday_perf.py --force` | **C** | R | 「跳过数据健康检查（交叉验证不符也强行出）」= 放宽口径，需确认并显式标注（不是写库，但产出可能带未校验数字） |
| `python query_today.py` / `python query_summary.py` | **A** | R | 查当日 / 默认昨日汇总，零参数、零网络、纯本地库；**P0 首选** |
| `python query_today.py --date 2026-11-03` | **B** | R | 指定日需表单 |
| `python stock_report.py 300319` | **B** | N:em/xq | 代码或名称必填；默认会调 API 取数 ⇒ 提交时仍需确认；表单给「代码/名称」输入 |
| `python stock_report.py 麦捷科技 --quick` | **B** | R | `--quick` = 只用本地数据不调 API ⇒ 降为 R 档；开关必须能改值并同步到命令预览 |
| `python scripts/buy_priority_report.py` | **C** | N:em | 默认档 ~8–11s，外呼东财公告 12 只 + 龙虎榜 4 只；只读库但出网 ⇒ 需确认 |
| `python scripts/buy_priority_report.py --no-ann` | **A** | R | **纯本地零网络 ~0.5s**（资金/位置/个股历史/板块排位/市场环境）；`COMMANDS.md` 明写「只读库、不写库、不改任何生产逻辑、不进主循环/终端/飞书」⇒ **P0 最安全的一键档** |
| `python scripts/buy_priority_report.py --out 今日.md` | **B** | N:em | 落盘路径需表单；服务端把它重写到 `artifacts/` 并做路径穿越守卫 |
| `python scripts/buy_priority_report.py --symbols 300638,300568` | **B** | N:em | 代码清单需输入，校验 `30/00/60/68` + `SZ` 前缀形态 |
| `python scripts/buy_priority_report.py --top 10` | **B** | N:em | 条数需输入 |
| `python scripts/buy_priority_report.py --date 2026-11-04` | **B** | N:em | 复盘交易日需日期输入（需当日有标签） |
| `python scripts/buy_priority_report.py --dtb 0` / `--ann-top 0` | **B** | N:em | 耗时档开关；`--ann-top 0` 等价 `--no-ann`，可降 R 档（表单联动） |
| `python scripts/buy_priority_report.py --minute N` | **B** | N:xq | 雪球分时，+18s；需输入 N 并提示耗时 |
| `python scripts/buy_priority_report.py --ai N` | **B** | N:ai | **≈60s/只**，AI 深度复核；需预算输入 + 确认 + 900s 硬超时；P2 才开放 |

### 4.3 §3 AI 分析（主入口 `python -m scanner.ai_digest`）

> **主入口（2026-11-15 Lichun 确认）**：`python -m scanner.ai_digest` 系列 = **AI 分析主入口**（默认 research，模型自查真实数据、出排序与判断、自动归档 `logs/ai_digest/YYYY-MM-DD/`、只归档不回灌、需 `MAGPIE_*` 且出网）。`pick.py --clipboard` 只是剪贴板清单的**等价 / 可选入口 / 次要选项**，不是默认入口、不是核心命令；云端 `/api/ai-explain` 那套 LLM 转录**不属于本动作面口径**。手工链路不再走「按 `c` → `pick.py`」。

| 命令 | 分类 | 档 | 理由 |
|---|---|---|---|
| `python -m scanner.ai_digest` | **C** | N:ai | **AI 分析主入口（默认 research）**：默认读 `股票池.md` 面板原文、模型自己查真实数据并出排序与判断；报告自动归档 `logs/ai_digest/YYYY-MM-DD/`、只归档不回灌库；**出网 + 耗 `MAGPIE_*` 凭据 + 会归档文件** ⇒ 需确认 + 全局串行 + 幂等；报告只作文件产出，UI 只回显「结果在哪」 |
| `python -m scanner.ai_digest --brief` | **C** | N:ai | 主入口的一屏报告档；同上（N 档确认 + 串行） |
| `python -m scanner.ai_digest --limit 3` | **B** | N:ai | 「每区只取前 3 只（按区截断）」需输入；口径说明原样带进表单帮助文字 |
| `python -m scanner.ai_digest --materials` | **A** | R | **R 档零出网零写库**：只打模型会收到的提示词，不出网、不耗凭据、不归档；核对输入用；**P0 首选** |
| `python -m scanner.ai_digest --source print --materials` | **A** | R | **R 档零出网**：核对输入 = 终端最后打印行（带 seq 与「数据 X 分钟前 / 非实时」）；`--source print` 是可选来源，主入口默认 `auto` 优先 `股票池.md` 面板原文 |
| `python -m scanner.ai_digest --demo` | **C** | N | **实测修正（2026-11-15）**：只跳过读库，仍走 research 工具链、会联网耗凭据；需确认 + 串行，不进 P0 |
| `python -m scanner.ai_digest --json` | **C** | N:ai | 主入口的机器可读出口；单独跑仍走默认 research ⇒ 出网 + 耗凭据，N 档需确认 |
| `python -m scanner.ai_digest --json --demo` | **C** | N | 同 `--demo`：仍走工具链会联网；机器可读输出，需确认，不进 P0 |
| `python -m scanner.ai_digest --out 报告.txt --max-calls 40` | **B** | N:ai | 落盘路径 + 调用预算双参数 |
| `python -m scanner.ai_digest --mode analyze` | **C** | N:ai | 按历史分层命中率算账，`AGENTS.md` 明写「无第二结论源纪律的**有意例外**」⇒ 必须确认；UI 只显示「结果在哪」，不渲染判断 |
| `python -m scanner.ai_digest --mode transcribe` | **C** | N:ai | 纯转述过全部红线（与云端 `/api/ai-explain` 同款纪律，但本动作面只跑本机命令、不接云端端点），触红线 exit 3；失败态要原样呈现 exit code |
| `python -m scanner.ai_digest --no-archive` / `--archive-dir D:/复盘` | **B** | N:ai | 默认归档 `logs/ai_digest/YYYY-MM-DD/`；`--no-archive` / `--archive-dir` 需表单，绝对路径必须被服务端重写到 `artifacts/`（路径守卫）；「结果在哪」回显实际归档目录 |
| `python pick.py` （交互粘贴，空行结束） | **D** | — | 交互式 stdin，桌面表单不做粘贴会话；改由主入口 `python -m scanner.ai_digest --symbols` / `--symbols-file` 给代码，`--clipboard` 仅作可选 |
| `python pick.py 300319 300666 SZ300770` | **B** | N:ai | 直接给代码（一次 ≤50 只）需清单输入与数量校验 |
| `python pick.py --clipboard` | **C** | N:ai | **等价 / 可选入口（已降级，非默认入口、非核心命令）**：仅当用户已按 `c` 把两区原文放进剪贴板时，读 Windows 剪贴板并转交同一 `ai_digest` 主入口；出网 + 耗凭据 ⇒ 确认 + 串行 + 幂等 |
| `python pick.py --symbols-file list.txt` | **B** | N:ai | 文件清单需表单；路径只准相对 `artifacts/` |
| `python pick.py --clipboard --limit 10` | **B** | N:ai | 「面板票多时只取前 10 只（按面板顺序）」需输入 |
| `python pick.py --clipboard --out 报告.txt --max-calls 50` | **B** | N:ai | 落盘 + 预算 |
| `python -m scanner.ai_digest --symbols ...` | **B** | N:ai | **主入口的自选清单档**；`pick.py` 只是它的**简写**（反向说明），目录里以 `-m scanner.ai_digest` 为主命令串，不重复做两个按钮；表单里给「等价入口」说明 |
| `scripts/` 里另写报告脚本 | **D（禁止）** | — | `COMMANDS.md` §7.1 原文「B 段 AI 分析走 `python -m scanner.ai_digest`（`pick.py` 为简写），**不要另写报告脚本**」；主入口恒为 `-m scanner.ai_digest` |
| §3.2 TradingAgents-astock | **D** | — | 已评估、已删除（2026-11-03 用户决策）；不进 catalog |
| `ls logs/ai_digest/YYYY-MM-DD/` | **D（映射到历史）** | — | shell 命令不是 python 任务；由任务中心「结果在哪 / 产出文件」呈现归档路径 `logs/ai_digest/YYYY-MM-DD/` |

**AI 分析主入口怎么把手工链路收成一步（不重写业务逻辑）：**

> 旧链路（**已被 2026-11-15 口径推翻**）：终端按 `c` → 两区完整原文进剪贴板 → `python pick.py --clipboard` → 报告。
> 新链路（**权威口径**）：**直接跑 `python -m scanner.ai_digest`**（默认 research，读 `logs/panel_md/股票池.md` 面板原文），报告自动归档 `logs/ai_digest/YYYY-MM-DD/`；面板文件缺失时脚本自己回退 print → view → log。
> 工作台一步 = 提交 **一个** subprocess 任务，命令原文取 **AI 分析主入口**，参数由服务端 catalog 固定：`python -m scanner.ai_digest`（可加 `--brief` / `--limit N` / `--source ...`）。
> - `--source auto`（默认）优先 `股票池.md` 面板原文（`v1 池选` + `沪深飙升` 三段，保留全部列与各区口径），缺失时自动回退 print → view → log（`COMMANDS.md` §3.0）；**工作台不实现回退、不拼提示词、不抽码**。`--source print` 可读 `offboard_print_snapshot`「终端最后一次打印的行，含留存行、顺序即屏幕顺序」，输出自带 seq 与「数据 X 分钟前 / 非实时」。
> - 表单里可选 `--source auto|panel|print|view|log` 与 `--limit N`；默认 `auto`（即面板原文），不再默认 `print`。
> - UI 在按钮旁显示输入事实与「结果在哪」：`输入 = 股票池.md 面板原文 · 本轮轮次 …`、归档路径 `logs/ai_digest/YYYY-MM-DD/`；**不显示任何报告结论、不把报告渲染成判断**。
> - 若用户已手动按过 `c`，表单可提供剪贴板**等价 / 可选入口**（`pick.py --clipboard` 或 `-m scanner.ai_digest --clipboard`），二选一，不并行；它不替代主入口。
> - P0/P1 默认给 `--materials`（**R 档零出网零写库**）验证链路；`--demo` / `--json --demo` 实测仍联网，归 N 档不进 P0；默认 research 真出网档留到 P2 且必须过确认弹层，失败态按 exit code 1/2/3/5 原样呈现、**不重试**。

### 4.4 §4 数据回填 / 修复 / 诊断

| 命令 | 分类 | 档 | 理由 |
|---|---|---|---|
| `python backfill_kline.py` | **C** | **W** | 收盘后回填缺失 K 线 + **重算 `next_day_pct`**（写库）；必须显式确认 + 串行 + `round_gate`；默认强制加 `--dry-run` |
| `python backfill_kline.py --dry-run --days 120` | **A** | R | 「只统计缺失，不调 API 不写库」⇒ **P0 可一键**；是 W 档命令的安全预演模板 |
| `python backfill_market_index.py` | **C** | **W** | 回填 `market_index_log` 历史（默认 200 天），写库 |
| `python backfill_market_index.py --dry-run --overwrite-live` | **C** | **W** | `--overwrite-live`「覆盖被冻结的基准值」= 破坏冻结语义（`AGENTS.md`「`recommendations` 存冻结分」同款）；即使 dry-run 也标 C 且二次勾选 |
| `python repair_kline.py --since 2026-10-01` | **C** | **W** | 修 `daily_kline` 非法 bar（默认 dry-run）；日期需表单，真写要确认 |
| `python diag_health.py` | **A** | R | 数据新鲜度体检（各表最新日期/行数 + 扫描质量日志），只读；**P0 首选** |
| `python diag_capture.py` | **A** | R | 榜单捕获率诊断，只读；P0 首选 |
| `python leaderboard_obs.py --days 7` | **B** | R | 时间序列需 `days` 输入，只读 |
| `python query_today.py --date 2026-11-03` | **B** | R | 见 §4.2 |
| K 线漂移 `logs/finalize.log` | **D（只读回显）** | — | 不是命令；任务中心提供「产出/日志」入口查看，不新增诊断逻辑 |

### 4.5 §5 验证 / 回测 · §6 独立区自检

| 命令 | 分类 | 档 | 理由 |
|---|---|---|---|
| `python -m scanner.backtest --days 60 --metric next_day_pct` | **B** | R | 次日涨幅分布回放，只读；`days` + `metric` 需表单（口径提醒「唯一口径是次日 ≥7% hit 率」原样作帮助文字） |
| `python -m scanner.backtest --ranking --backfill` | **C** | **W** | `--backfill` 回填标签 = 写库；确认 + 串行 |
| `python -m scanner.nextday_attribution --days 30 --excess` | **B** | R | 次日归因，只读；参数需表单 |
| `rule_validate` / `nextday_calib` / `portfolio_backtest` 等 | **D** | — | 已整体归档 `legacy/`，「常数没有任何生产读取方」；不进 catalog |
| `python -m scanner.hot_watch --offline-demo` | **A** | R | 12 条内置样本逐条核对、全绿 exit 0；**零库零网**，回归哨兵；**P0 最安全** |
| `python -m scanner.historical_watch --offline-demo` | **A** | R | 同上（v1 回捞区） |
| `python -m scanner.offboard_watch --offline-demo` | **A** | R | 同上（榜外异动段） |
| `python -m scanner.crosssection --health` | **A** | R | 库内轮次/信号数摘要（离线只读）；P0 首选 |
| `python -m scanner.crosssection --date 2026-11-13 --json` | **B** | R | 指定日 + 机器可读，需表单 |
| `python -m scanner.crosssection` | **A** | R | 打印当日最新一轮 p95+ 榜外信号（查询态，只读）；若后续加入刷新写入参数则重新归 C/W |

### 4.6 §7 离线研究脚本（`scripts/` 逐条）

| 命令 | 分类 | 档 | 理由 |
|---|---|---|---|
| `python scripts/offboard_nextday.py` | **A** | R | B 段次日表现追踪，**刻意不给结论**（`test_report_contains_no_verdict_wording`）；只读 |
| `python scripts/offboard_nextday.py --days 20 --tier T2 --json` | **B** | R | 三参数需表单；仍是只读统计 |
| `python scripts/offboard_nextday.py --repair-next-day` | **C** | **W** | 修历史错标签（默认 dry-run），语义是写库 ⇒ 确认；AGENTS.md「三防呆」纪律不动 |
| `python scripts/offboard_nextday.py --repair-next-day --apply` | **C** | **W** | **真写**；必须显式确认 + 串行 + 二次勾选 |
| `python scripts/shadow_feature_backtest.py --no-fetch` | **A** | R | `--no-fetch` 零联网，只读回溯；P0 首选 |
| `python scripts/ann_cache_sync.py --migrate-legacy` | **C** | **W** | 旧 12MB JSON 一次性导入（幂等但写库）；确认 + 串行 |
| `python scripts/ann_cache_sync.py` | **C** | W+N:em | 当日最后一轮榜外行批量刷新公告（写库 + 多码 1 请求出网）；确认 + 串行；「取数失败不推进水位」纪律不动 |
| `scripts/profit_ledger.py` | **B** | R | 收益账本按规则复算，需规则参数；只读 |
| `scripts/ann_event_backtest.py` | **B** | R | 公告事件样本外检验（配对 bootstrap），参数多、耗时长；只读但低频 |
| `scripts/push_gate_impact.py` | **B** | R | 飞书过滤门离线影响评估，只读；低频 |
| `scripts/prelaunch_probe.py` | **A** | R | 启动前形态探针，只读、不联网；可一键 |
| `scripts/beauty_mark_eval.py` | **B** | R | 「稳」标记分档裁决，需参数；低频 |
| `scripts/flow_mark_evidence.py` | **B** | R | 资金流图标实证复算，需参数；低频 |
| `scripts/label_audit.py` | **A** | R | 标签登记簿审计，只读、零参数 |
| `scripts/nextday_rule_scan.py` | **B** | R | H1/H2 分半验证，需参数；低频 |
| `scripts/nextday_mark_split_verify.py` | **B** | R | 分型前后命中率对比，需参数；低频 |
| `scripts/ranking_validate_day.py` | **B** | R | 单日排序 → 次日验证，需日期；低频 |
| `scripts/review_tier_replay.py` | **B** | R | 忠实回放 `entry_tier`，需参数；低频 |
| `scripts/tier_penalty_replay.py` | **B** | R | 级联否决 vs 扣分制对照，需参数；低频 |
| `scripts/tier3_reason_perf.py` | **B** | R | 档3 劣后原因归因，需参数；低频 |
| `scripts/seq_refine_replay.py` | **B** | R | 排序序列细化回放，需参数；低频 |
| `scripts/signal_combo_analysis.py` | **B** | R | 组合信号命中率，需参数；低频 |
| `scripts/pool_filter_scan.py` | **B** | R | 池内过滤器摸底，需参数；低频 |
| `scripts/replay_source_swap.py` | **D** | — | 一次性数据源替换实验，参数复杂、耗时长、离线研究性质；不进桌面动作面 |
| `python -m scanner.label_registry` | **B** | R | 需 `PYTHONIOENCODING=utf-8`（任务 env 已带，可跑），但低频审计；表单给控制台编码回显 |
| `scripts/project_audit.py` | **A** | R | 项目量化普查，只读零参数；但属开发向，P2 后再评估 |
| `scripts/ths_api_probe.py` | **D** | — | 同花顺接入前一次性探测；低频、网络探针 |
| `scripts/panel_m0_check.py` | **D** | — | M0 门「终端渲染 vs **云端读回**逐条比对」；桌面纯本地无云端，跑不了 |
| `scripts/_verify_view_split.py` | **D** | — | 视图层拆分等价性一次性证明；开发向 |
| `scripts/_config_diff.py` | **D** | — | 对比 git HEAD 版 `config.py`；依赖 git 工作树，打包态无 `.git` |
| `scripts/_run_tests_all.py` | **D** | — | 一次性跑全量 pytest；开发向、分钟级、读 `tests/`，不放桌面 |
| `scripts/buy_priority_report.py` | 见 §4.2 | — | 已在 §2.1 逐档分类 |
| §7.1b `legacy/scripts/rerank_*` | **D** | — | 已归档，复现方式见 `legacy/README.md`；不进 catalog |

### 4.7 §8 环境变量 / §9 改代码验证 / §10 常见坑

- **§8 全部 env**：不做任务；映射到 P3「扫描器状态与开关」面板（只回显，不改 `RTS_DB_PATH` / `RTS_ENV_FILE` / `RTS_LOG_DIR` / 任何凭据）。
- **§9 `ruff check .` / `ruff format .` / `mypy scanner/` / `python -m pytest tests/ -q`**：**D**（开发向、分钟级、会改文件 / 读 `tests/`；`ruff format .` 还会碰 72 个未改动文件，`IMPLEMENTATION.md` §6 明确禁止）。工作台不提供「跑测试」按钮。
- **§10 常见坑**：写进 §3 横切规则与 §7 风险表（WAL 不能 `cp`、飞书 webhook、东财限频、雪球两接口、Beijing 时区、B 段不给结论、冻结分）。

### 4.8 最该先做的一档

> **P0 = R 档「零参数 + 零写库 + 零外呼」一键化**，具体 8 条：
> `-m scanner.hot_watch --offline-demo`、`-m scanner.historical_watch --offline-demo`、`-m scanner.offboard_watch --offline-demo`、`-m scanner.crosssection --health`、`python diag_health.py`、`python diag_capture.py`、`python query_today.py`、`python query_summary.py`，外加 `-m scanner.ai_digest --materials`（**R 档零出网零写库**；`--demo` 实测仍联网归 N 档；**主入口默认 research 出网档不进 P0**）、`python scripts/buy_priority_report.py --no-ann`、`python backfill_kline.py --dry-run --days 120`、`python scripts/shadow_feature_backtest.py --no-fetch`。
> 理由：① 全部满足 §2.5 的 D0 门禁（standalone 解释器必能 import）；② 零 `scanner.db` 写、零出网 ⇒ §2.4 的 WAL 写锁 / 重复推飞书 / IP 限频三条风险**全部不存在**；③ 无需确认弹层与 `exclusive_slot`，任务管理器可以先只做「提交 / 查询 / 日志回显」最小闭环；④ 三条 `--offline-demo` 自带 exit 0 断言，是天然验收信号。
>
> **逐字门禁注**：`COMMANDS.md` §2 把 `query_today.py` / `query_summary.py` 写在**同一行合并单元格**（`| \`python query_today.py\` / \`query_summary.py\` |`），故 `python query_summary.py` 这一整串在文档里 `grep -F` 不命中。`prototype.html` 的动作目录为满足 §10「假任务名逐条 `grep -F` 命中」，该位改用逐字出现的 `python today_report.py`（`COMMANDS.md:47`）；生产 catalog 仍按 §4.2 同时收录 `query_today.py` 与 `query_summary.py`（以脚本文件名为准，不依赖文档排版）。

---

## 5. `/api/tasks/*` 契约草案

> **执行体 = `subprocess.Popen([sys.executable, <script>, ...argv], shell=False, cwd=repoRoot(), env=scanner_env)`，复用现有 python 脚本，不重写业务逻辑。**
> 所有响应沿用 `lps._ok` / `lps._err` / `_send_json`；`Content-Type: application/json; charset=utf-8`、`Cache-Control: no-store`、显式 `Content-Length`。

### 5.1 端点

| 方法 / 路径 | 作用 | 安全 | 请求 | 响应 `data` |
|---|---|---|---|---|
| `GET /api/tasks/catalog` | 命令目录（**服务端单源**，前端不硬编码命令） | R GET + L1 | — | `{items: CatalogItem[], levels:{read,write,network}, maxConcurrent:{read:3,exclusive:1}}` |
| `POST /api/tasks` | 提交任务 | L1–L4 + token；W/N 需 `confirm` | `{catalogId, args:{...}, source:"workbench", idempotencyKey?, confirm?:{acknowledged,shown[]}}` | `TaskView`（`202` 表示 `queued`；幂等命中 `200` + `deduplicated:true`） |
| `GET /api/tasks` | 列表 / 筛选 | L1 | `?status=&level=&limit=&cursor=` | `{items: TaskView[], nextCursor, counts:{queued,running,ok,failed,cancelled}}` |
| `GET /api/tasks/{id}` | 查询单个 | L1 | — | `TaskView`；不存在 `404 NOT_FOUND` |
| `POST /api/tasks/{id}/cancel` | 取消 | L1–L4 + token | `{}` | `TaskView`（`state=cancelled`）；终态 → `409 TASK_STATE_CONFLICT` |
| `GET /api/tasks/history` | 历史（终态任务，含产出路径） | L1 | `?limit=&cursor=` | `{items: TaskView[], nextCursor}` |
| `GET /api/tasks/{id}/log` | 日志流（**增量轮询**） | L1 | `?offset=<bytes>&limit=65536` | `{offset, nextOffset, eof, truncated, lines:string[]}` |
| `POST /api/tasks/cancel-all` | 关窗收尾（P2） | L1–L4 + token | `{}` | `{cancelled:[id], remaining:[id]}` |
| `POST /api/tasks/scanner` | 扫描器启停（**P3**，走同一命名空间） | L1–L4 + token；仅 `takeover` 态 | `{action:"start"\|"stop"}` | `{state:"running"\|"stopped", owner:"desktop-child"}` |

> **日志流为什么用增量轮询而不是 SSE/WebSocket**：`http.server` 的 `HTTP/1.1` 纪律要求显式 `Content-Length`（`local_panel_server.py` 文件头「一律带 Content-Length，故 keep-alive 安全」）；SSE 要改成长连接 + chunked，WebSocket 要新依赖。用 `offset` 字节偏移轮询（前端 500ms 一次、`eof` 后停）零依赖、零长连接、断点可续，且与 `api/client.ts` 的 `apiGet` 完全同构。P2 再评估 SSE，不作为本期范围。

### 5.2 `CatalogItem` / `TaskView` 字段

```jsonc
// CatalogItem（服务端单源，前端只渲染）
{
  "id": "hot_watch_offline_demo",
  "cmd": "python -m scanner.hot_watch --offline-demo",   // 展示原文；不传给服务端
  "argv": ["-m", "scanner.hot_watch", "--offline-demo"], // 服务端固定，前端不可覆盖
  "label": "沪深飙升区自检",
  "group": "独立区自检",          // 与 COMMANDS.md 章节同名
  "level": "read",               // read | write | network
  "outbound": "none",            // none | ai_gateway | eastmoney | xueqiu（无 feishu）
  "writesDb": false,
  "timeoutSec": 30,
  "params": [],                  // [{key,type,default,help,enum?,min?,max?,pattern?}]
  "requiresOptional": false,
  "dryRunDefault": false
}
```

```jsonc
// TaskView（**不含** schema / regions / ctx / stocks / gate / cols 任一键，B6）
{
  "id": "t_20261115_104233_a1b2",
  "catalogId": "buy_priority_no_ann",
  "command": "python scripts/buy_priority_report.py --no-ann",  // 脱敏后的展示原文
  "level": "read",
  "state": "running",            // queued | running | ok | failed | cancelled
  "progress": 0.55,              // 0..1；无进度能力时 null（不编造百分比）
  "createdAt": "...", "startedAt": "...", "finishedAt": null,
  "exitCode": null,              // 终态才有；原样呈现脚本退出码
  "logOffset": 4096, "logBytes": 4096,
  "artifacts": [],               // [{name, path(相对 artifacts/), bytes}]
  "error": null,                 // {code, message} | null
  "timeoutSec": 30,
  "deduplicated": false,
  "owner": "scanner-child",     // 谁派生的；P3 才有 desktop-child 扫描器态
  "confirm": null                // W/N 档记 {acknowledgedAt, shown[], dryRun}
}
```

### 5.3 状态机

```
                 ┌──────────── cancel / cancel-all ────────────┐
                 ▼                                              │
  POST ──► queued ──(拿到槽 + round_gate)──► running ──┬──► ok      │
                 │                                      ├──► failed  │
                 │                                      └──► cancelled ◄┘
                 └──────── cancel ─────────────────────────► cancelled
```

- 合法迁移只有 `queued→running`、`queued→cancelled`、`running→{ok,failed,cancelled}`；`ok/failed/cancelled` 为**终态**。
- 非法迁移（终态再 cancel、不存在的 id、状态回退）→ `409 TASK_STATE_CONFLICT`。
- `running` 超过 `2 × timeoutSec` 仍无退出 → 强制 `failed`（`error.code=TASK_TIMEOUT`），且不留 `running` 残留。
- 取消：`queued` 直接出队；`running` 先 `proc.terminate()`，5s 后 `kill()`，再标 `cancelled`（不假装成功）。
- 任务 meta **在 spawn 之前落盘**（write-ahead），保证崩溃后孤儿清扫一定能扫到（§6）。

### 5.4 错误码（沿用 `error.{code,message}`，新增 `TASK_*` 前缀）

| code | HTTP | 含义 |
|---|---|---|
| `BAD_REQUEST` | 400 | 未知 `catalogId` / 参数非法 / body >16KB / `--out` 路径穿越 / 含 `--no-lock` |
| `FORBIDDEN_ORIGIN` | 403 | L1–L4 任一不过（Host / Origin / token / Content-Type） |
| `TASKS_DISABLED` | 503 | `RTS_TASKS` 缺省关 |
| `TASK_CONFIRM_REQUIRED` | 400 | W/N 档缺 `confirm.acknowledged` |
| `NOT_FOUND` | 404 | task id 不存在 |
| `TASK_BUSY` | 409 | `exclusive_slot` 被占 / `round_gate` 70s 未等到主循环轮边界 |
| `TASK_QUEUE_FULL` | 429 | `queued` > 20 |
| `TASK_STATE_CONFLICT` | 409 | 非法状态迁移 |
| `SCANNER_BUSY` | 409 | P3 `start` 时 `child !== null` 或 `probeMeta` 仍命中 |
| `ORPHAN_REAPED` | 200 | 启动时清扫到的孤儿任务（写进该任务的 `error`） |
| `INTERNAL` | 500 | 服务内部错误（信封与既有 `/api/*` 一致） |

> 幂等命中不报错：`200` + 既有 `TaskView` + `deduplicated:true`。脚本自身的退出码语义（`ai_digest` 1/2/3/5、`--offline-demo` 全绿 0）**原样进 `exitCode`**，不翻译成新码。

### 5.5 超时与回收

- 每任务 `timeoutSec` 由 catalog 逐条声明（R 60s / 自检 30s / W 600s / N 180s / `--ai` 900s）。
- 超时：`terminate()` → 5s → `kill()`；`2×timeout` 兜底标 `failed`。
- `queued` 任务在队列中最多等 `QUEUE_TTL=300s`，超时 `cancelled` + `TASK_BUSY`。
- 回收：`meta.json` 保留最近 `MAX_HISTORY=200` 条**不可删**；`stdout.log` 超 `2 MiB` 截尾并记 `truncated:true`；`artifacts/` 随任务保留，不做自动删除（报告是产出，用户要能找到）。

---

## 6. 生命周期与并发

**任务管理器放哪：`scanner/local_tasks.py` 的 `TaskSupervisor`，跑在扫描器 Python 进程内的一条 daemon 线程。**

选它的 4 条可判定理由：① 与扫描器共享**同一份已净化 env / `sys.executable` / `RTS_DB_PATH` / `LOG_DIR`**（§2.5 的硬前提，独立进程会全部丢掉；新口径下 `RTS_DB_PATH` 恒指向同一份本机最新活库 `scanner.db`）；② 不需要第二端口 / 第二探活 / 第二套 env 净化守卫；③ 与 `run_scanner` **同进程**才能做 `round_gate`（§2.4），把写库任务排到主循环写事务之后；④ 零新增进程类型、零新增依赖。
**否决「独立进程管理器」**：独立进程拿不到主循环的轮边界，写库并发只能退化为 `busy_timeout` 硬等；且要重做 env 净化与 `userData` 推导，`IMPLEMENTATION.md` §6 的三个实测坑会复现。

**与主循环的关系**

```
unified_scanner.py (主进程)
 ├─ 主循环 run_scanner()  ── 每 60s 写 scanner.db ── notify_round_done() ─┐
 ├─ local_view_cache     ── 单写多读（原样不动）                          │
 ├─ local_panel_server   ── GET 只读线程（原样不动）+ do_POST 转发          │
 └─ local_tasks.TaskSupervisor (daemon 线程)  ◄── round_gate ────────────┘
      ├─ read 槽(3)      → Popen 只读脚本，可与主循环并发（WAL 读不阻塞写）
      ├─ exclusive 槽(1) → W/N 脚本，须等 round_gate 才 spawn
      └─ 孤儿清扫 / cancel-all / meta write-ahead
```

- `Supervisor` **不参与** `scan_with_raw`、不碰 `cache_latest` / `get_latest`、不碰终端渲染、不碰飞书。
- 总 `running ≤ 4`（`read 3` + `exclusive 1`），`queued ≤ 20`。
- 只读任务失败不阻断扫描（fail-open）；编程错误冒泡，不裸 `except Exception`（`AGENTS.md`）。

**关窗时怎么收任务 / 孤儿防护（把 `IMPLEMENTATION.md` §8「记录不修」升级为必修）**

> 现状：`main.js::before-quit` 直接 `proc.kill()`（Windows `TerminateProcess`，不走 `atexit`）⇒ 扫描器子进程被强杀时，它派生的任务 subprocess 会成孤儿。这是动作面引入的新风险，必须处理。

1. **收尾顺序改一条**：`before-quit` 先 `POST /api/tasks/cancel-all`（带 token），最多等 3s 让 Python 侧把所有 `running` 任务 `terminate→kill` 干净，**再** `stopScanner()`。3s 超时则强杀并写一条 `orphan_risk` 日志（可观测，不静默）。`stopScanner()` 函数体与 `test_closing_window_kills_python_child` 的既有字面量不变。
2. **write-ahead meta**：`meta.json` 在 spawn 之前写；`state=running` 但 pid 不存在 / pid 存在却 cmdline 不在 catalog argv 白名单 ⇒ 启动时标 `cancelled` + `ORPHAN_REAPED`；仍活着且匹配白名单的 pid ⇒ kill 后同样标记。判据全是 pid + cmdline，不「看情况」。
3. **进程组**：Windows 下 `Popen(..., creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)`，取消时整组收；POSIX 用 `start_new_session=True` + `os.killpg`（打包目标仍是 Windows，这条只作对称说明）。
4. **token 生命周期**：token 随进程启动生成、进程退出即失效；不落盘、不进日志。
5. **验收**：关窗后 `meta.json` 中无 `state=running`；`tasklist | findstr python` 无 catalog 白名单脚本残留；重启后 `/api/tasks/history` 能读到被 `ORPHAN_REAPED` 的记录。

---

## 7. 前端动作面形态（沿用 v2 视觉，1600 × 900 逐屏尺寸）

> **外壳完全沿用 v2**：48px Title → 76px Readout → `flex:1;min-height:0` Workspace → 40px Footer；暗色唯一形态；四级表面 / 5 级字阶 / 10px 卡片 / 58px card-head / 18px 间距 / 28px 行高 / tabular-nums / 140ms 三态 / 两处渐变白名单 / `prefers-reduced-motion` 兜底。token 值逐字取自 `rts-panel-cloud/web/src/index.css`（§0 表）。
> **切换**：v2 侧栏顶部加 **36px 分段控件**「看板 ｜ 动作面」（`role=tablist` + 150ms 滑块，复用第二处白名单渐变）。切到动作面时 **Title / Readout / Footer / 侧栏 208px / 右栏 386px 全部保留**，只换工作区内容 —— 是同一个产品的第二个视图，不是第二个界面。

### 7.1 看板视图（默认，与 `preview.html` 同一产品）

`208px 侧栏（区块导航 + 顶部分段控件）` → `flex 主区（padding 18 / gap 18，10 行 v1 池选表 + 两块 136px 空态 + 分段空态）` → `386px 右栏（过门账目 7 项 + 门内/门外滑块）`。列头仍取服务端 `colSpec`，行尾 `新/⚡/稳/▼` 不并入数据列，红涨绿跌只用 `--up/--down`。**本视图零改动**，只是多了一个分段控件。

### 7.2 动作面视图 · 区域划分与尺寸

| 区域 | 位置 / 尺寸 | 内容 |
|---|---|---|
| **Title** | 全宽 48px，`--surface-0` + 品牌径向光晕 | 左：logo 30×30 + 产品名 15.5px + 副名 10px `--brand-soft`；右：`本机 · 动作面` chip（26px 高）+ 238×34 状态胶囊（沿用 v2 五态，`aria-live`） |
| **Readout** | 全宽 76px，`--surface-1` | 只读任务事实：`运行中 1` / `排队 2` / `最近一次 <command>` / `成功 12 · 失败 1`（22px tabular-nums）；**不派生指标、不放 KPI 墙** |
| **动作目录** | 侧栏 208px（含顶部 36px 分段），每项 44px | 按安全档分组「只读 / 写库 / 外呼」，组标签 10px `micro`；每项 = 档位色点（6px，**不用涨跌色**）+ 13px 命令短名 + 右对齐档位徽章；选中态 `--surface-3` + 左 3px `--brand` 色条（与 v2 `.nav-item` 同款） |
| **卡 A · 参数表单** | 主区上部，card-head 58px + body ≈ 200px（`--surface-2` / 10px 圆角 / 顶部内高光 / 单层浅阴影） | 两列字段，每行 32px：下拉（主入口档位 / `--source` / `--mode`）、输入（`--date` / `--top` / `--days` / `--symbols` / `--limit` / `--out`）、开关（`--quick` / `--no-ann` / `--json` / `--dry-run` / `--materials`）；标签 11px `--text-3`，控件 30px、`--surface-0` 底 + 1px `--line`；**所有控件能改值**，底部一条 11px mono「将执行：`python …`」实时预览（服务端下发 argv，前端只拼展示串） |
| **卡 A 内 · AI 分析主入口**（原型卡片名仍叫「一键调研」，本次不改 `prototype.html`，以本文口径为准） | 主区，紧接表单，高 72px（card 内独立分区，1px `--line-soft` 分隔） | 左：13px 主句「AI 分析 · 主入口 `python -m scanner.ai_digest`」+ 11px/1.65 口径句「输入 = `股票池.md` 面板原文 · 数据 X 分钟前 / 非实时 · 结果在哪：`logs/ai_digest/YYYY-MM-DD/`」（事实，不推断）；右：主按钮「执行 AI 分析」（34px 高，`--brand` 底，三态齐全）。提交 `python -m scanner.ai_digest`（可加 `--brief` / `--limit N` / `--source`）；P0 原型默认 `--materials` 零出网档（`--demo` 实测仍联网归 N 档），默认 research 真出网档留到 P2 且必须过确认弹层；剪贴板链路（`pick.py --clipboard`）只作可选等价入口 |
| **卡 B · 任务中心** | 主区下部，head 58px + 列表 ≈ 240px + 日志区 200px | 列表每行 44px：状态徽章（`queued`/`running`/`ok`/`failed`/`cancelled`，配文字，色用 `--brand-soft`/`--brand`/`--amber`/`--coral`/`--text-4`，**不占涨跌色语义位**）+ 命令原文 mono 11.5px + 4px 进度条 + 耗时 + 「日志 / 取消 / 产出」按钮；日志区 `--surface-0` 底、mono 11px / 18px 行高、逐行追加、`overflow:auto` |
| **卡 C · 扫描器状态与开关** | 右栏顶部 ≈ 168px | 状态点 + `seq` / 轮次时间（沿用 v2 五态语义，只显示不解释）+ 「启动 / 停止」34px 按钮；**P0–P2 置灰**并标「由桌面壳管理」（P3 才启用，见 §2.6）；**不出现 `--no-lock`** |
| **卡 D · 历史记录** | 右栏 `flex:1`（`side-scroll`，padding 18） | 终态任务每行 36px：`state` / 命令短名 / `finishedAt` / `exitCode` / 产出路径（含 AI 分析主入口归档 `logs/ai_digest/YYYY-MM-DD/`）；点击把该任务的日志回填到卡 B 日志区；空态沿用 v2 语法（纯 CSS 图形 + 「本轮无数据」+ `—（本轮无数据）`） |
| **Footer** | 全宽 40px，`--surface-0`，10.5px | `动作在本机执行 · 结果落 logs/tasks/<id>/ · AI 归档 logs/ai_digest/YYYY-MM-DD/ · 命令来自本机脚本 · 数据版本 v2`；**不写内部纪律宣言、不写结论词** |

### 7.3 危险操作确认弹层

- `--surface-pop` 表面 + 10px 圆角 + `--shadow-pop` + 顶部内高光；140ms 淡入 + 4px 下沉（不是 toast、不自动消失）。
- 内容（**只列执行事实，不替用户判断**）：命令原文（mono）、安全档、`会写 scanner.db 的目标表` 或 `会访问的上游类别`、`是否 dry-run`、`RTS_DB_PATH`（本机最新活库 `scanner.db`，与终端同一份）、预计耗时、产出目录（任务 `artifacts/` 与 AI 分析主入口归档 `logs/ai_digest/YYYY-MM-DD/`）。
- 两个按钮「取消」「确认执行」；**W 档「确认执行」默认 disabled**，须勾选一个显式 checkbox（文案为「已核对命令与 dry-run 状态」，不是结论文案）才启用；N 档须勾「已知会出网并消耗调用预算」。
- `Esc` / 外部点击关闭并把焦点还给触发按钮；`role=dialog aria-modal=true`，焦点进入时落在取消按钮；reduced-motion 时去掉淡入位移。
- 点「确认执行」后请求体带 `confirm:{acknowledged:true, shown:[...]}`；服务端独立校验（前端勾了不算数）。

### 7.4 交互三态与可访问性（沿用 `DESIGN.md` §7）

所有可点元素 hover（表面抬到 `--surface-3` / 边框微青蓝）、focus-visible（`2px solid var(--brand)` / offset 2px，表格行内缩 -2px）、active（`--surface-pop` + 1px 下沉），过渡 140ms ease；无卡片位移、无发光、无缩放。键盘：Tab / Enter / Space / Esc 全可达，分段控件 `← → / Home / End`，`aria-selected` / `aria-expanded` / `aria-live` / `aria-modal` 齐备。`@media (prefers-reduced-motion: reduce)` 把 animation/transition 降到 `.01ms`、取消 active 位移，但折叠 / 切换 / 状态 / 内容完全可用。

### 7.5 文案纪律

- **动作面只呈现「执行了什么、结果在哪」**：命令原文、状态、进度、exit code、日志行、产出路径（含 AI 分析主入口自动归档的 `logs/ai_digest/YYYY-MM-DD/` 目录回显，只回显路径、不回显报告内容）、耗时。
- 不出现 `推荐 / 建议 / 应该买 / 综合判断 / 更可能涨`（以及 `DESIGN.md` §8 / `test_panel_web_cols.py` 的 9 词表：`短名单/买入/卖出/持仓/目标价/止盈/止损`）。命令路径里的 `buy_priority_report.py` 是**真实文件名**，照抄；中文说明写「盘中反馈报告 · 纯本地档」，不写「买入优先级」。
- 不把内部纪律写成 UI 文案（G5 / colSpec 单源 / 三端镜像 / `SCHEMA_VERSION` / D2 留在测试与注释里）。
- 空态不推断原因（`COMMANDS.md` §10「B 段脚本刻意不给结论」同款）。

---

## 8. 明确「不做什么」

1. **不动看板组件**：`StatusBar` / `RegionCard` / `RegionTable` / `GatePanel` / `Sidebar`（只加分段控件挂载点）/ `pages/Stock.tsx` 的既有渲染不改；`data-scroll-root="main"`、`anchor-*`、`.region-table` / `.sticky-col` / `.row-clickable` / `.cell-*` 结构类一个不改名（BOUNDARIES §2.2）。
2. **不动 colSpec**：不改 `COLS_*`、不改 `lib/cols.ts` 的 `// #region COLS:*` 块、不改 `POOL_CELLS`、不改 `HOT_HIGHLIGHT_STREAK`、不改 `hotSubtitle/offboardSubtitle` 模板。
3. **不动 `SCHEMA_VERSION=2`**：`panel_serialize.py` / `worker/src/serialize.ts` / `web/src/api/types.ts` **零 diff**（任务类型放新增纯类型文件，见 §2.2）；不加「榜内异动」段。
4. **不新增筛选 / 排序 / 结论**：动作目录的分组是**命令分类**，不是数据筛选；任务列表按 `createdAt` 是**执行时序**，不是行情排序；不生成短名单 / 综合判断 / 持仓或买卖建议。
5. **不引状态库**：沿用 `useState` + `lib/router.ts` hash 路由 + `useSnapshot`；不引 Zustand / Redux / react-router。
6. **不引新前端依赖 / 新网络模块**：唯一网络收口仍是 `api/client.ts`（扩 `apiTasks*`，复用 `apiSend`）；不裸 `fetch`、不新增 `XMLHttpRequest`、不引图表库、不引 WebSocket / SSE 长连接。
7. **不把内部纪律写成 UI 文案**：页脚 / 空态 / 弹层只写执行事实与口径。
8. **不把终端逻辑搬进 Node**：Node 侧不读 SQLite、不算分、不拼提示词、不抽码、不解析报告；`main.js` 只多一个 token 生成 + `onBeforeSendHeaders` 注入（P2）与所有权三态（P3）。
9. **不重写业务脚本**：所有命令都是 `subprocess` 复用现有 python 文件；不改 `unified_scanner.py` 主循环、不改 `scanner/**` 既有模块的函数体（只新增 `local_tasks.py`）。
10. **不新增端口 / 不改回环 / 不改静态托管**：`BIND_HOST`、`_static()` SPA 回退、`DEFAULT_STATIC_DIR`、`hash 路由` 全部保持。
11. **不透传 `--no-lock`、不推飞书、不改写 `RTS_DB_PATH`/`RTS_ENV_FILE`/`RTS_LOG_DIR`**。
12. **本次不改任何生产代码**：只写 `docs/features/desktop-workbench/SPEC.md` 与 `prototype.html`。

---

## 9. 分期（每期独立构建、独立回滚；列文件 / 风险 / 验证命令）

### P0 · 只读类一键化（零写库，最安全）

| 项 | 内容 |
|---|---|
| **范围** | §4.8 的 R 档一键命令；动作面视图骨架、分段切换、动作目录、命令预览、任务中心最小闭环（提交 / 查询 / 日志增量轮询）、原型四态 |
| **文件** | 新增 `scanner/local_tasks.py`（catalog + Supervisor + `dispatch`）；`scanner/local_panel_server.py` **仅加 `do_POST` ≤8 行**；`unified_scanner.py` 启动段加一行 `local_tasks.install(...)`（不改主循环体）；新增 `web/src/lib/taskTypes.ts`（纯类型）、`web/src/components/workbench/{Workbench,TaskCenter,ParamForm,ConfirmDialog}.tsx`（纯渲染）、`web/src/index.css` 追加 `.wb-*` 结构类（**既有 token 值零改动**）、`web/src/api/client.ts` 加 `apiTasksGet/apiTasksPost`；新增 `tests/test_local_tasks.py`；`desktop/prepare.mjs` 的 CHECKS 追加「P0 catalog import 清单」 |
| **风险** | ① 新增 `do_POST` 是否让 `test_local_panel.py` 红（不会，全是 GET 断言；加哈希基线守卫）；② standalone Python 缺传递依赖（用 D0 门禁 + `prepare.mjs` 真 import 拦）；③ 新 `.tsx` 触发 hygiene 守卫（不写 11 个列名 / 9 个结论词 / 裸 `fetch`）；④ `RTS_TASKS` 缺省关漏实现 → 端口一开就可写 |
| **验证命令** | `python -m pytest tests/test_local_panel.py tests/test_local_tasks.py tests/test_desktop_shell.py tests/test_panel_contract.py -q`<br>`python -m scanner.hot_watch --offline-demo`（exit 0）<br>`python -m scanner.historical_watch --offline-demo` / `python -m scanner.offboard_watch --offline-demo`<br>`python -m scanner.crosssection --health`<br>`python diag_health.py`、`python query_today.py`、`python query_summary.py`<br>`python -m scanner.ai_digest --materials`<br>`python scripts/buy_priority_report.py --no-ann`<br>`python backfill_kline.py --dry-run --days 120`<br>打包态：`resources\python\python.exe -c "import scanner.hot_watch, scanner.historical_watch, scanner.offboard_watch, scanner.crosssection, diag_health, query_today, query_summary"`（exit 0）<br>`pnpm typecheck && pnpm build` |

### P1 · 报表参数化 + 任务中心

| 项 | 内容 |
|---|---|
| **范围** | B 档命令（§4.2/§4.5/§4.6 表单）、参数校验与白名单 argv 拼装、任务列表四态 + 进度 + 逐行日志、历史记录、**AI 分析主入口 `python -m scanner.ai_digest` 的 R 档可呈现闭环**（提交 `-m scanner.ai_digest --materials` → `logs/tasks/<id>/` 日志与 `logs/ai_digest/YYYY-MM-DD/` 归档路径回显 → exit code 原样；零出网零写库、**不重试、不把报告渲染成判断**）、`/api/tasks/catalog` / `history` / `{id}/log?offset=`、产出文件清单 |
| **文件** | `local_tasks.py` 扩 catalog 参数 schema + 路径守卫；`web/src/components/workbench/ParamForm.tsx` 成型；`client.ts` 扩 GET；`tests/test_local_tasks.py` 加注入 / 穿越 / 幂等用例；`index.css` 追加表单与进度条结构类 |
| **风险** | ① **参数注入**：只收 `catalogId` + 白名单参数值，服务端拼 `list[str]` + `shell=False`，绝不接整条命令；② **`--out` / `--archive-dir` 路径穿越**（复用 `_static()` 的 normpath+startswith，绝对路径 / `..` / 盘符 / `%5C` 全 400）；③ 进度编造（无进度能力的任务 `progress=null`，不填假百分比）；④ 日志轮询把 `Content-Length` 纪律破掉（每响应显式长度） |
| **验证命令** | `python -m pytest tests/test_local_tasks.py tests/test_panel_web_hygiene.py tests/test_panel_web_cols.py -q`<br>`python -m scanner.ai_digest --materials`（零出网，验素材链路）<br>`python scripts/buy_priority_report.py --no-ann --top 10`<br>`python -m scanner.crosssection --date 2026-11-13 --json`<br>注入用例：`POST {"catalogId":"..","args":{"__argv":"; calc"}}` → 400 `BAD_REQUEST`<br>`ruff check scanner/local_tasks.py && mypy scanner/local_tasks.py` |

### P2 · 写库与外呼类（带确认与串行）

| 项 | 内容 |
|---|---|
| **范围** | W/N 档、确认弹层、`exclusive_slot`、`round_gate`、幂等键、超时与回收、`cancel-all` 收尾、孤儿清扫、L1–L4 鉴权 + token 注入、`RTS_TASKS=1` 显式开关、`userData/logs/tasks/`；**AI 分析主入口 `python -m scanner.ai_digest` 默认 research 的可呈现闭环**（提交 `-m scanner.ai_digest` → 日志 / `logs/ai_digest/YYYY-MM-DD/` 归档路径回显 → 失败态按 exit code 1/2/3/5 原样呈现；**不重试、不把报告渲染成判断**） |
| **文件** | `local_tasks.py` 加槽 / gate / token 校验 / write-ahead meta；`main.js` 加 token 生成 + `onBeforeSendHeaders` + `cancel-all` 收尾（**14 条既有守卫字面量不动**）；`web/src/components/workbench/ConfirmDialog.tsx`；`tests/test_local_tasks.py` 加鉴权 / 并发 / 超时 / 孤儿用例；`desktop/prepare.mjs` 加 `RTS_TASKS` 默认关检查 |
| **风险** | **本期最大**：① SQLite WAL 写锁与主循环争用（`round_gate` + 单槽）；② 重复外呼 / 东财 IP 限频 / 误推飞书（幂等 + 节流 + `outbound` 枚举无 feishu）；③ **孤儿进程**（write-ahead + cancel-all + 清扫）；④ `requirements.lock` 覆盖不到 D3 脚本凭据与 D1 传递依赖（缺凭据当可呈现 failed，不崩）；⑤ 确认弹层只做前端勾选会被绕过（服务端独立校验 `confirm`）；⑥ **AI 分析主入口的报告是主观判断**（只归档不回灌）：UI 只回显命令 / 日志 / `logs/ai_digest/YYYY-MM-DD/` 归档路径，不解析、不渲染成判断、不替脚本重试 |
| **验证命令** | `python -m pytest tests/test_local_tasks.py tests/test_migrations.py tests/test_desktop_shell.py -q`<br>`python backfill_kline.py --dry-run --days 120` / `python backfill_market_index.py --dry-run`（零写库验模板）<br>`python scripts/offboard_nextday.py --repair-next-day`（默认 dry-run）<br>并发：连提两个 W 档 → 第一个 `running`、第二个 `queued`，`GET /api/tasks?status=running&level=write` 计数 ≤1<br>鉴权四连（见 §2.3）全部 403<br>关窗：`tasklist \| findstr python` 无 catalog 残留；重启后 history 有 `ORPHAN_REAPED` 记录<br>AI 分析主入口闭环：提交 `python -m scanner.ai_digest`（未配 `MAGPIE_*` ⇒ `exitCode=1`）⇒ 失败态原样呈现、零重试、`logs/ai_digest/YYYY-MM-DD/` 归档路径回显、报告内容不渲染 |

### P3 · 扫描器生命周期接管

| 项 | 内容 |
|---|---|
| **范围** | `RTS_SCANNER_OWNERSHIP=takeover\|attach\|refuse`、`/api/tasks/scanner` 启停、单一所有者状态机、开发态 `attach` 联调 |
| **文件** | `desktop/main.js`（所有权分支 + `child` 状态判定，保留 `execFile` / env 净化 / `stopScanner` 形状）、`web/src/components/workbench/ScannerCard.tsx`、`tests/test_desktop_shell.py` **追加**守卫（14 条既有断言全绿）、`docs/desktop/IMPLEMENTATION.md` §5.3 补一句结论（**仅在实施期，本次交付不动**） |
| **风险** | ① 与既有「杀旧」语义冲突（§2.6 三态 + 时间点隔离消解）；② `probeMeta` 被别的程序占用时误判（只认自己的 `{ok,data}` 信封，既有逻辑不动）；③ 与 `DESIGN.md` §9 P2 的 frameless / preload 未验证项叠加（两者分期解耦，不混一个 diff）；④ 改 `main.js` 触发 14 条文本守卫（追加而非修改） |
| **验证命令** | `python -m pytest tests/test_desktop_shell.py tests/test_local_tasks.py -q`<br>`RTS_SCANNER_OWNERSHIP=attach pnpm start`（终端扫描器仍在、零杀、状态卡标「由终端实例提供」）<br>`RTS_SCANNER_OWNERSHIP=refuse pnpm start`（已有实例时弹错误页、不杀）<br>默认 `takeover`：关窗后 0 个 python / 端口释放 / 重启正常（沿用验收 7）<br>打包态 DevTools 冒烟（`IMPLEMENTATION.md` §4.1 那套） |

---

## 10. 验收判据（全部可判定，不写「体验良好」）

**契约与只读面**

- [ ] `SCHEMA_VERSION == 2`；`git diff --stat` 中 `scanner/panel_serialize.py`、`rts-panel-cloud/worker/src/serialize.ts`、`rts-panel-cloud/web/src/api/types.ts`、`scanner/view/model.py::COLS_*`、`web/src/lib/cols.ts` **全部零 diff**。
- [ ] `tests/test_panel_contract.py`、`test_panel_serialize.py`、`test_panel_web_hygiene.py`、`test_panel_web_cols.py`、`test_local_panel.py`（35 条）、`test_desktop_shell.py`（14 条）全绿；全量 `pytest tests/ -q` 基线不掉。
- [ ] 同一 `seq` 快照下 `GET /api/regions` 响应**逐字节**等于改造前；`GET /api/meta` 无快照仍 `200 {ready:false,...}`；`GET /api/stock/{sym}/kline` 与 `_static()` 路径穿越守卫不变。
- [ ] `local_panel_server.BIND_HOST == "127.0.0.1"`，源码无 `0.0.0.0`，无新增端口常量 / 无新 env 覆盖路径；`_route()` 与 `do_GET` 函数体哈希与基线一致。
- [ ] 任一 `/api/tasks/*` 响应的 `data` 中不含 `schema` / `regions` / `ctx` / `stocks` / `gate` / `cols`（B6 守卫）。
- [ ] 所有 `/api/tasks/*` 响应恒为 `{ok:true,data}` 或 `{ok:false,error:{code,message}}`；`Content-Type` / `Cache-Control: no-store` / 显式 `Content-Length` 与既有端点一致。
- [ ] 提交一个 R 档任务前后，`view_snapshot` 行数不变（B7）。

**鉴权（§2.3 四连 + 缺省关）**

- [ ] `Origin: http://evil.example` / `Host: evil.example` / 缺 `X-RTS-Task-Token` / 错 token / `Content-Type: text/plain` —— 五种 POST 各返回 **403 `FORBIDDEN_ORIGIN`**，且 `GET /api/tasks` 零新增。
- [ ] `RTS_TASKS` 未设 / `0` / 空 ⇒ 所有 `/api/tasks/*` **503 `TASKS_DISABLED`**，`do_POST` 未 spawn 任何进程。
- [ ] token 不出现在 `web/dist/**`、`.env`、包内文件、任何日志、任务 env 回显里；进程重启后旧 token 立即失效。

**任务语义**

- [ ] 命令目录由 `GET /api/tasks/catalog` 单源下发；前端源码里 `grep -E 'python (unified_scanner|stock_report|today_report|backfill|pick|-m scanner)'` 命中为 0（不硬编码命令）。
- [ ] `POST /api/tasks` 只接受 `catalogId` + 白名单 `args`；传整条命令字符串 / `--no-lock` / 绝对路径 `--out` / `..` 变体 ⇒ **400 `BAD_REQUEST`**，`shell=False` 的 argv 由服务端拼装。
- [ ] 状态迁移只沿 `queued→running→{ok,failed,cancelled}`；终态再 cancel ⇒ **409 `TASK_STATE_CONFLICT`**；不存在 id ⇒ **404 `NOT_FOUND`**。
- [ ] W/N 档缺 `confirm.acknowledged` ⇒ **400 `TASK_CONFIRM_REQUIRED`** 且零 spawn；前端勾选被绕过时服务端仍拦。
- [ ] 两个 W 档并发：一个 `running`、一个 `queued`；`GET /api/tasks?status=running` 中 W+N 合计恒 **≤ 1**；`exclusive_slot` 释放后 `queued` 自动升 `running`。
- [ ] `round_gate` 70s 等不到主循环轮边界 ⇒ `cancelled` + `TASK_BUSY`，不硬闯；主循环 `scan_with_raw` 耗时无回归。
- [ ] 幂等键 5 分钟内重复提交 ⇒ `200` + `deduplicated:true` + 同一 `task_id`，无第二次 spawn；同 `catalogId` 60s 节流生效。
- [ ] 超时任务有 `exitCode` 与可读末行日志，无 `running` 残留超过 `2×timeoutSec`；`queued` 超过 `QUEUE_TTL=300s` 自动 `cancelled`。
- [ ] `ai_digest` 退出码 1/2/3/5 与 `--offline-demo` 的 0 **原样进 `exitCode`**，工作台不重试、不翻译、不渲染报告结论。
- [ ] **AI 分析主入口**：`GET /api/tasks/catalog` 中 `python -m scanner.ai_digest` 系列标为 AI 分析主入口（默认 research），`pick.py --clipboard` 标为等价 / 可选入口且非默认入口；全文 `grep` 复核旧表述「`pick.py --clipboard` = 核心 / 默认入口」命中 0，无「默认入口 = `pick.py --clipboard`」或把云端 `/api/ai-explain` 当主入口的表述。
- [ ] **AI 分析安全档**：默认 `python -m scanner.ai_digest` = N 档（出网 + 耗 `MAGPIE_*` 凭据 + 会归档文件）⇒ 需确认 + 全局串行 + 幂等；`--materials` / `--demo` / `--json --demo` = R 档零出网零写库（工作台侧不注入 `MAGPIE_*`，无需确认）。
- [ ] **AI 分析可呈现闭环**：提交 `-m scanner.ai_digest` 后任务中心回显 `logs/tasks/<id>/` 日志与 `logs/ai_digest/YYYY-MM-DD/` 归档路径（「结果在哪」）；失败态按 exit code 1/2/3/5 原样呈现，**不重试、不把报告内容渲染成判断**。

**生命周期 / 打包态**

- [ ] 任务 stdout/stderr 只落 `<LOG_DIR>/tasks/<id>/stdout.log`；扫描器终端 stdout 与 `desktop_child.log` 中**不含任何任务日志行**（不刷屏，`propagate=False`）。
- [ ] 关窗后所有 `meta.json` 中无 `state=running`；`tasklist | findstr python` 无 catalog 白名单脚本孤儿；重启后 `/api/tasks/history` 能读到 `ORPHAN_REAPED`。
- [ ] 打包态 `resources\python\python.exe` 逐条 import P0 catalog 的脚本全 exit 0；任务产出落 `%APPDATA%\rts-desktop\logs\tasks\`，`resources\repo` 无新写文件；`scanner.db-wal/-shm` 不进包、`.env` 不进包。
- [ ] **桌面版读到的库 = 本机最新 `scanner.db`**：以仓库根 `scanner.db` 的 mtime 或 `/api/meta` 最新一轮 `(date,time)` / 轮次时间比对，桌面版与任务读库结果一致；不是包内 2026-10-20 的 146MB 冷快照，也不是 `%APPDATA%\rts-desktop\scanner.db` 那份旧分家库。
- [ ] **无第二写入者**：打包态不另起会写本机活库的独立主循环；所有写库任务经唯一主循环的 `round_gate` + `exclusive_slot` 单槽串行；任务 / 主循环 / 桌面版的 `RTS_DB_PATH` 解析到同一份本机活库。
- [ ] **不再从包内冷快照种库**：`userData/scanner.db` 不作为数据源，不存在「种子不覆盖后各记各的」；打包链不复制冷快照种独立库，桌面版只读或共用本机活库。
- [ ] 任务 subprocess 的 `RTS_DB_PATH` / `RTS_LOG_DIR` / `RTS_ENV_FILE` / `PYTHONNOUSERSITE=1` 与扫描器子进程**逐项相等**（同一份 env），且 `RTS_DB_PATH` 指向同一份本机最新活库 `scanner.db`（不再是 `userData/scanner.db`）；Node 侧仍不读 SQLite。
- [ ] `pnpm typecheck && pnpm build` 通过；`node prepare.mjs` 通过；打包态 DevTools 冒烟（`IMPLEMENTATION.md` §4.1）零 console error。
- [ ] P3 三态：`attach` 时终端扫描器进程仍在（零杀）；`refuse` 时不启动不杀；默认 `takeover` 关窗后 0 残留、端口释放、重启正常。

**原型（`prototype.html`）**

- [ ] 单文件双击 / Preview 可开；1600×900 桌面观感；无 CDN、无外链图片/字体、无 `import`、无 `fetch` / `XMLHttpRequest`；不真发请求（JS 定时器模拟）。
- [ ] 外观与 `preview.html` 同一产品：`--surface-0..3` / `--surface-pop` / `--brand` / `--brand-soft` / `--text-1..4` / `--line*` 逐值相同；四级表面、5 级字阶、10px 圆角、58px card-head、18px 间距、两处渐变白名单可辨。
- [ ] 左侧「看板 / 动作面」切换可用；任务列表同时可见 **运行中 / 成功 / 失败 / 排队** 四种状态；参数表单的下拉 / 输入 / 开关**能改值**且命令预览同步变化。
- [ ] 点「执行」后出现进度条 + **逐行**日志回显；历史记录随任务终态增长；写库类命令点执行**必弹确认层**，取消则不产生任务；扫描器状态与开关可点（仅演示态）；hover / focus-visible / active 三态齐全；`prefers-reduced-motion` 下动画退场但功能全在。
- [ ] 全文 `grep -E '推荐|建议|应该买|综合判断|更可能涨|短名单|买入|卖出|持仓|目标价|止盈|止损'` 命中为 0；所有假任务名都能在 `COMMANDS.md` 里 `grep -F` 到（逐条真命令，不编命令）。
- [ ] 交付物只有两个新文件；`git status --short` 中 `web/src/**`、`scanner/**`、`desktop/**`、`tests/**`、已 commit 的设计文档零改动；无 commit、无依赖安装、无构建产物。

---

## 11. 落地最大的一个风险

> **打包态 standalone Python 的依赖覆盖 + 写库任务与主循环的 WAL 写者互斥，这两件事叠在 P2。**
>
> `requirements.lock` 只精确锁了 5 个直接依赖，且文件头**自承**「numpy/pandas 等传递依赖未全量固定」；`pywencai` 是注释掉的可选项，`MAGPIE_*` 凭据在打包态默认不存在（`.env` 不进包）。开发态「本机碰巧装过」的脚本，一到 `resources/python/python.exe` 就可能 `ModuleNotFoundError` 或 exit 1 —— 这正是 `IMPLEMENTATION.md` §6 里 `python-dotenv` 漏声明「打包成功但双击炸」的同款坑，只是这次一口气铺到几十条命令上。
> 同时 P2 才第一次让动作面在**同一份本机活库**上执行写库任务（新口径下**不得成为第二个独立写者**：打包态不另起主循环，所有写仍经唯一主循环的 `round_gate` + `exclusive_slot` 单槽串行）：WAL 只保证「写不阻塞读」，不保证「两个写者不互斥」；主循环每 60s 写一批，`round_gate` / `exclusive_slot` 任一处漏掉，就会表现为「任务卡住、标签错冻结、或把主循环轮内事务提前提交」这类**静默数据错误**（`IMPLEMENTATION.md` §8 已经修过一次 `persist_latest` 在共享连接上 `commit()` 的同类问题）。
>
> **缓解是可判定的**：P0 只放 §4.8 的 D0 档（零写库、零出网、零新增 import），把依赖门禁做成 `prepare.mjs` 里「standalone 解释器逐条真 import」，把写库并发做成「单槽 + `round_gate` + 默认 `--dry-run` + 确认留痕」四件套，且 P2 单独验收、不与 P0/P1 混一个 diff。若这两条门禁任一无法写成断言，**就不放 W/N 档进 catalog** —— 这是本规格的硬止损线。

---

## 12. 责任人交接（handoff）

| 交给 | 交接物 | 需要他回答 / 做 |
|---|---|---|
| **Researcher** | §0 事实底座、§2.5 依赖分档 D0–D4 | 在 `resources/python/python.exe` 下逐条实测 P0 catalog 的 `import` 与 `--offline-demo` exit code；核实 `akshare/numpy/pandas` 传递依赖在打包链里的实际版本，给「D1 哪些能进 catalog」的清单 |
| **Designer** | §7 逐屏尺寸、`prototype.html`、v2 `DESIGN.md` / `preview.html` | 复核动作面与看板「同一个产品」的观感一致性、确认弹层的措辞与焦点归还、四态任务徽章不占涨跌色语义位；确认「不把内部纪律写成 UI 文案」 |
| **Developer** | §2（六条红线）、§5 契约、§6 生命周期、§9 分期、§10 验收 | P0 先落 `local_tasks.py` + `do_POST` 8 行 + 哈希基线守卫；`shell=False` / 白名单 argv / 路径穿越 / 信封复用逐条写成测试；不碰三端镜像与看板组件 |
| **Operator** | §2.3 token 与 `RTS_TASKS`、§2.5 `LOG_DIR` 落点与**共用本机活库口径**、§2.6 `RTS_SCANNER_OWNERSHIP`、§9 P2/P3 | 管 `RTS_TASKS` 默认关、`RTS_TASK_TOKEN` 注入链、`logs/tasks/` 权限与日志保留策略、`prepare.mjs` 的打包前置；**核实 `RTS_DB_PATH` 指向本机最新活库 `scanner.db`、不再从包内冷快照种独立库、无第二写入者**；`MAGPIE_*` 凭据只在 N 档出网任务注入；P3 三态在真机各跑一遍并留冒烟证据 |
| **Evaluator**（验收期） | §10 全部 checkbox | 按 §10 逐条判绿/红；特别验「四连 403」「两 W 并发 ≤1」「关窗无孤儿」「全文无结论词」「两个新文件零生产 diff」 |
