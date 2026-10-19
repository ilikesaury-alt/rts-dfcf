# 桌面版实施记录（Issue A → D）

> 规格冻结在 [`CONTEXT.md`](CONTEXT.md)（决策 D1–D5 + 10 条验收 + 4 个顺序 Issue）。
> 本文只记**做成了什么、怎么跑、怎么验、哪里与规格不同**。

## 0. 一句话

终端输出**一个字节没变**。桌面版是同一份载荷的**第二个消费者**：

```
主循环 build_scan_view → serialize_view(view) ─┬─→ 云端面板（RTS_PANEL，本期不动）
                                              ├─→ 进程内缓存 + view_snapshot（重启回放）
                                              └─→ 终端 ANSI 渲染（原样）
                                                        ↑
                        http://127.0.0.1:8899/ ← Python 只读线程回放缓存
                                                        ↑
                        Electron 窗口 / 浏览器 / 任意 HTTP 客户端
```

**零契约**：Electron 侧没有第二份数据处理逻辑，它只是把 `127.0.0.1:8899` 装进窗口
（design D1/D2/D5）。前端 `api/client.ts`、`api/types.ts` **一字未改**，
`SCHEMA_VERSION` 仍 == 2。

## 1. 交付物

| 文件 | 作用 | Issue |
|---|---|---|
| `scanner/local_panel_server.py` | 只读 HTTP 服务：`/api/{health,meta,regions,stock/{sym}/kline}` + 静态托管 `web/dist`（同源 ⇒ 免 CORS） | A |
| `scanner/local_view_cache.py` | 进程内视图缓存 + `view_snapshot` 落库/回放 | A |
| `scanner/config_core.py` | `LOCAL_PANEL_PORT`；`resolve_log_dir()` + `RTS_LOG_DIR`（打包态可写落点） | A/D |
| `scanner/db/migrations.py` | `m021_view_snapshot` | A |
| `unified_scanner.py` | `_local_panel_enabled()`；启动起服 + 回放；每轮 `cache_latest` ÷ `persist_latest` | A |
| `rts-panel-cloud/web/src/lib/cols.ts` | `COLS_*` 标签 → 行字段投影（前端**唯一**持有列名的地方） | B |
| `rts-panel-cloud/web/src/lib/router.ts` | 零依赖 hash 路由 `#/stock/{symbol}` | B |
| `rts-panel-cloud/web/src/lib/useSnapshot.ts` | meta→regions 轮询（出错保留上一份，不白屏） | B |
| `rts-panel-cloud/web/src/components/{StatusBar,RegionTable,GatePanel}.tsx` | 状态条 / 表格 / 过门区（**不含任何列名字面量**） | B |
| `rts-panel-cloud/web/src/pages/Stock.tsx` | 详情页：K 线（`kline-component`）+ 评分分解 + 出现史 | B |
| `rts-panel-cloud/web/src/{App.tsx,styles.css}` | 区块装配 + 主题（红涨绿跌） | B |
| `desktop/main.js` | Electron 主进程：`findPython` / env 净化 / 拉起子进程 / 探活 / 关窗收子进程 | C/D |
| `desktop/fetch-python.mjs` | 取 `python-build-standalone` 3.12 → `resources/python/` 并装 `requirements.lock` | D |
| `desktop/prepare.mjs` | 打包前置检查（缺 `dist`/`python` 就红，不产出「能装但打不开」的包） | D |
| `desktop/package.json` | `start` / `dist` / `fetch-python`；`extraResources` = `python` + `repo` | C/D |
| `tests/test_local_panel.py` | 服务端/缓存/迁移守卫（35 条，含两条终端防刷屏） | A |
| `tests/test_panel_web_cols.py` | 前端列投影与 `COLS_*` 同源（Python 侧断言标签逐字相等） | B |
| `tests/test_panel_web_hygiene.py` | 前端代码卫生（**沿用**，非本期新增，2026-09-30 T1.2）：组件里不得有列名字面量、不得裸 `fetch(` —— 新组件一写就得过它 | 既有 |
| `tests/test_desktop_shell.py` | Electron 壳的结构守卫（回环、env 净化、`userData` 落点、依赖声明） | C/D |
| `requirements.lock` · `requirements.txt` | 补上漏声明的 `python-dotenv`（入口模块级 `import dotenv`，见 §6） | D |

## 2. 三种跑法

```powershell
# ① 只开本地看板（终端扫描器照旧，只是多一行「🖥 本地看板: …」）
$env:RTS_LOCAL_PANEL=1; python unified_scanner.py        # → http://127.0.0.1:8899/
$env:RTS_LOCAL_PANEL_PORT="9000"                        # 换端口（非法值回退 8899）

# ② 开发态 Electron（自带窗口，用你系统装的 Python）
cd desktop; pnpm install; pnpm start

# ③ 打包 exe
cd rts-panel-cloud/web; pnpm build        # 先出 web/dist（Python 同源托管它）
cd ../../desktop; pnpm run fetch-python   # 一次性：取自带 Python + 装 requirements.lock
pnpm run dist                             # → desktop/dist/win-unpacked/RTSPanel.exe
```

调试用 env（不改生产语义）：`RTS_PYTHON`（指定解释器）、
`RTS_DESKTOP_SCANNER_ARGS`（追加扫描器参数；本机联调用 `--no-lock --no-feishu`
就能**不干扰**已在跑的终端实例）。

## 3. 不变式（破了就是 bug，都有守卫）

| 不变式 | 守卫 |
|---|---|
| 只绑 `127.0.0.1`，无 env 覆盖路径 | `test_bind_host_is_loopback` / `test_desktop_binds_loopback_only` |
| 契约零漂移：`/api/regions` == `serialize_view` 输出（去 `stocks`） | `test_regions_replays_same_payload_as_serialize_view`、`test_regions_response_has_no_stocks_key` |
| `regions.cols` 的 key 序列 == `COLS_*` 列名序列 | `test_regions_cols_keys_match_col_specs`、`test_col_projection_covers_every_terminal_column` |
| `RTS_LOCAL_PANEL` 缺省即关（连模块都不导入） | `test_local_panel_disabled_by_default`、`test_local_panel_module_not_imported_when_disabled` |
| 与云端开关 `RTS_PANEL` 独立 | `test_local_switch_is_independent_of_cloud_switch` |
| Node 侧不读 SQLite（D2） | `test_node_side_never_reads_sqlite_directly` |
| 可写落点 = `userData`，种子**不覆盖** | `test_writable_state_goes_to_user_data_without_overwriting` |
| 子进程 env 净化（含禁用户 site） | `test_python_env_is_scrubbed_of_system_interpreter_vars` |
| 连接级异常不刷终端 | `test_handle_error_swallows_connection_reset_without_traceback` |
| 组件里没有列名字面量、没有裸 `fetch(` | `tests/test_panel_web_hygiene.py` |

## 4. 验收对照（10 条）

| # | 验收 | 结果 / 证据 |
|---|---|---|
| 1 | 数据一致性 | 载荷就是 `serialize_view(view)` 的输出（同一函数），`cols` 逐列同源 |
| 2 | 四区块按 colSpec 渲染 | 实机截图：池选 10 列 / 飙升 16 列 / 榜外同表；空区显「—（本轮无数据）」；涨幅实测 `rgb(239,35,42)`、跌幅 `rgb(20,177,67)`；点行进 `#/stock/{sym}` |
| 3 | 状态条 | 「轮次 … · seq … · 早盘」；快照过期时橙字「数据滞后 N 轮」；无数据时不白屏（显「等待扫描器首轮上报…」） |
| 4 | 过门区 | 标题含「下一张卡会推什么（非已推内容）」；「门内/门外」切换只换数据源、标题不变（实测：池选 25→19 行） |
| 5 | 详情页 | K 线（K + 量）+ 评分分解 + 出现史；无分解显「无评分分解」；不在本轮上报显「本轮上报未包含该票」 |
| 6 | 零第二结论源 | 无短名单/综合判断/持仓建议；`test_report_contains_no_verdict_wording` |
| 7 | 生命周期 | 关窗 → 子进程被 kill（实测 electron 0 / python 0 / 端口释放）；锁是**内核级文件锁**，进程死即释放（`test_lock_auto_released_when_holder_killed`），重启正常 |
| 8 | 纯本地 | 不依赖公网；`RTS_LOCAL_PANEL=0` 行为与改前一致；仅 `127.0.0.1` |
| 9 | 终端并存 | 本期对 `unified_scanner.py` 的改动**全是加法**；关开关时连模块都不导入 |
| 10 | 契约守门 | `SCHEMA_VERSION == 2`；`tests/test_panel_contract.py` 仍绿 |

### 4.1 打包态实测（Issue D 收尾，2026-10-19）

`desktop/dist/win-unpacked/RTSPanel.exe`，整目录 839.7 MB
（`resources/python` 329 MB + `resources/repo` 142 MB）：

| 检查项 | 实测 |
|---|---|
| 子进程解释器 | `resources\python\python.exe`（**不是**系统 Python） |
| 可写落点 | `%APPDATA%\rts-desktop\scanner.db`（首次种入包内冷快照；`m021` 由子进程自己补上）+ `logs\` |
| `/api/meta` · `/api/regions` | 200 · main 29 / hot 8 / offboard 6；过门区 43/39 |
| 静态资源 | `index-*.js` 264 438 B `text/javascript` · `index-*.css` 4 599 B `text/css`，均 200 |
| 渲染进程 | `title` = 面板标题；`body` 背景 `rgb(15,17,21)`（CSS 生效）；`styleSheets` 1；`#root` 有子节点；四条 `resource` 全 200；`readyState=complete`；控制台**零报错** |
| 生命周期 | 关窗 → `RTSPanel` 0 / 子进程 0 / 8899 与调试端口均释放 |
| 包内清单 | 含 `scanner.db` + `web/dist`；**不含** `.env`、`desktop/`、`logs/`、`scanner.db-wal` |

> ⚠ **验证方法上的坑（差点误判成「打包版白屏」）**：别用**屏幕区域截图**验证窗口 ——
> 本机用户正在用电脑，截图会把别人的窗口内容一起截进来（实测截到用户浏览器的白页，
> 一度以为打包版渲染挂了）。改用 DevTools 协议：以
> `RTSPanel.exe --remote-debugging-port=9333` 启动，`Page.captureScreenshot` /
> `Runtime.evaluate` 只反映**本应用渲染进程**。（9222 常被用户的 Chrome 占着。）

## 5. 与规格的三处差异

1. **「榜内异动」段没有进载荷。** 该段（2026-09-30 上线，`ScanView.onboard_rows`）
   不在契约里 —— 加它就要 `SCHEMA_VERSION` 3 且两端同批发版，本期按验收 10 的
   「不得引入契约漂移」**刻意不加**（2026-10-19 用户拍板：**暂不加**，见 §7）。
   桌面版因此比终端少一段。
   对应地，飙升区标题写的是「（创业板 · 分段并列 · 互不排名 …）」而不是终端的
   「三段并列」（段数确实不同，写「三段并列」会是假话）。
2. **打包态的库与终端分家。** 安装态 `resources/` 可能只读，且 WAL 要写权限，
   故打包版把 `scanner.db` 与 `logs/` 指到 `%APPDATA%\rts-desktop\`，并在**首次**
   启动时把包里那份冷快照种过去（之后绝不覆盖）。⇒ 桌面版是**另一个写入者**，
   与仓库根 `scanner.db` 从此各记各的；想共用就在启动前设 `RTS_DB_PATH`。
3. **桌面版启动会接管扫描器。** `unified_scanner.py` 的单实例语义是
   「杀掉更旧的实例」（按脚本路径匹配，与锁路径无关）—— 所以双击 exe 会把
   正在跑的终端扫描器结束掉，这是主程序**既有**行为，不是本壳引入的。

## 6. 实测踩到的坑

- **终端被 traceback 刷屏**：`socketserver` 默认 `traceback.print_exc()` 直写 stderr，
  而浏览器每关一次 keep-alive 连接就抛 `ConnectionResetError` ⇒ 用户每点一下面板，
  终端被 25 行 traceback 冲刷（终端是主输出面，这条不能忍）。改法：`handle_error`
  连接级异常降 debug、其余落 `logs/local_panel.log`，且 logger **无条件**
  `propagate=False`（建日志文件失败也不许回落 stderr）。
- **用户 site-packages 串味**（实测 `sys.path` 里
  `%APPDATA%\Python\Python312\site-packages` **排在自带 site-packages 之前**）：
  删 `PYTHONPATH` 不够，必须 `PYTHONNOUSERSITE=1`。症状是打包时 pip 报一堆
  `sqlfluff … is not installed` 的无关冲突 —— 它看见了用户那份依赖，
  也就是说**某些依赖会根本不装进包里**，换台机器就缺。装依赖与跑子进程两处都加了。
- **Electron 44 没有 `postinstall`**（`npm view electron@44.9.0 scripts` = 空）：
  二进制改成**首次 require 时懒下载**，`node node_modules/electron/install.js`
  也可以显式拉。`pnpm install` 完发现没有 `dist/electron.exe` 时别怀疑网络。
- **pnpm 12 的构建白名单**：键名是 `pnpm-workspace.yaml` 里的 `allowBuilds`（map），
  不是 pnpm 10 的 `onlyBuiltDependencies`（数组），也不在 `package.json` 的 `pnpm` 字段。
- **`ruff format` 基线不干净**：ruff 0.16.2 判定 72 个**未改动**文件需要重排
  （行情敏感的是空行与引号风格）。⇒ **只格式化自己碰过的文件**，不要 `ruff format .`，
  否则混进几十个无关文件的 diff。
- **入口的模块级 import 没进依赖清单（只有打包才暴露）**：`unified_scanner.py:21`
  直接 `import dotenv`，而 `python-dotenv` 两处依赖清单都没有 ⇒ 自带的独立 Python
  一跑就是 `ModuleNotFoundError`（开发态系统 Python 装着它，**本地永远复现不了**）。
  改法：补进 `requirements.lock` / `requirements.txt`，并加两道前置 —— 单测
  `test_entry_module_level_imports_are_declared_in_requirements`（逐条比对入口的
  模块级 import 与清单）+ `prepare.mjs` 里以 `PYTHONNOUSERSITE=1` 真跑一次
  `import unified_scanner`（已验证它能把缺依赖的包拦下来）。
- **`Failed to open persistent cache files … DawnGraphiteCache … (0x20)`**：Electron
  一起来就报，且删掉那些缓存目录后依旧 —— 成因未确认（同 `userData` 的其它 Electron
  实例 / 上次异常退出的残留句柄），但实测**与窗口渲染无关**（同一实例里 DOM、样式、
  资源全部正常）。别把它当白屏的根因。
- **WAL 不进包**：`scanner.db-wal/-shm` 是活文件，冷拷贝=不一致快照；包里只放
  checkpoint 过的 `scanner.db`，缺的近期数据由桌面版自己的扫描轮补齐。

## 7. 规格未决项的结论（2026-10-19 用户拍板）

1. **「榜内异动」段暂不进契约**（要进就得 `SCHEMA_VERSION` 3，且云面板 + 桌面 +
   前端列投影同批发版）。⇒ 「桌面版比终端少一段」是**已知且接受**的取舍，不是遗漏；
   `SCHEMA_VERSION` 保持 2，验收 10 的「零契约漂移」继续成立。要补时按 §5 第 1 条一起改。
2. **打包目标只做 Windows**：`desktop/` 只按 Windows 写（`python.exe` / `resourcesPath`
   / `win-unpacked`）。macOS 要另取 `aarch64-apple-darwin` 的 standalone Python 并过
   签名/公证 —— 需要时另立一项，本期不做。
