# 桌面版技术选型趋势调研与结论（2026-11-15）

> **性质**：纯研究 + 结论文档。本次**只创建这一个文件**；不改任何生产代码，不改 `DESIGN.md` / `preview.html` / `BOUNDARIES.md`，不安装依赖、不跑构建、不跑测试。
> **时间锚**：权威日期 **2026-11-15**。所有「当下」结论均以该日期联网查证为准；2023/2024 的信息只作为**历史沿革**引用，不当作现状。
> **查证方式**：WebFetch / WebSearch 访问官方文档、官方博客、GitHub Releases API、npm registry、Devographics 调查站；凡具体版本号、发布日期、星数、体积数字均给出处链接。**未能核实**的结论一律显式标注；未找到可信 2026 桌面端「趋势综述」文章的部分也如实写明。
> **本地依据**：`desktop/package.json`、`desktop/main.js`、`desktop/prepare.mjs`、`desktop/fetch-python.mjs`、`rts-panel-cloud/web/package.json`、`docs/desktop/IMPLEMENTATION.md`、`docs/features/desktop-design-polish/BOUNDARIES.md`、`DESIGN.md`、`pyproject.toml`、`requirements.lock`、`tests/test_desktop_shell.py`（均为本次实际读取）。

---

## 0. 一句话结论（先给）

**保留 Electron 44.x 薄壳 + Python `unified_scanner.py` 数据心脏 + `127.0.0.1` 同源只读 HTTP + React 19 / Tailwind v4 前端的整套架构；把「更新」投到「壳的体验细节」与「前端工具链的安全小版本升级」，不追着换 Tauri / Electrobun / Wails 等新运行时。**

理由浓缩成一句：**2026 年真正在流行的是「薄 WebView 壳 + 现代前端 + local-first 本地同源数据」这个架构形态，而不是某一个特定壳的品牌；本项目已经在形态上「踩中点」，落后的是产品完成度（图标/菜单/标题栏/空态/分发/自动更新），不是技术栈的代际。**

---

## 1. 项目现状（先读代码，不凭空比较）

### 1.1 架构事实（来自本次实际读取）

```
┌──────────────────────────────────────────────────────────────┐
│ Electron 44.x 主进程（desktop/main.js，纯 Node，零数据加工）      │
│   · 开 BrowserWindow（1440×900，contextIsolation/nodeIntegration │
│     false/sandbox true）                                       │
│   · execFile 拉起 Python 子进程 unified_scanner.py 60           │
│   · env 净化：删 PYTHONHOME/PYTHONPATH/…/PYTHONNOUSERSITE=1      │
│   · 探活 127.0.0.1:<port>/api/meta（只认自己的 {ok,data} 信封）  │
│   · loadURL http://127.0.0.1:<port>/  ← Python 同源托管 web/dist │
│   · 关窗 → before-quit → kill 子进程                             │
└───────────────────────────┬──────────────────────────────────┘
                            │ 只绑 127.0.0.1，纯本地只读
┌───────────────────────────▼──────────────────────────────────┐
│ Python 3.12（数据心脏）                                         │
│   unified_scanner.py ── build_scan_view → serialize_view        │
│   scanner.db ≈ 226 MB，SQLite WAL，单实例内核级文件锁             │
│   scanner/local_panel_server.py：/api/* + 静态托管 web/dist       │
│   契约 SCHEMA_VERSION = 2，三端镜像（py / worker ts / web ts）    │
└───────────────────────────┬──────────────────────────────────┘
                            │ 同源 JSON（免 CORS）
┌───────────────────────────▼──────────────────────────────────┐
│ rts-panel-cloud/web（React 19 + Vite 6 + TS 5.7 + Tailwind v4   │
│ @tailwindcss/vite + vitest；依赖极少，无状态库、无路由库）         │
└──────────────────────────────────────────────────────────────┘
```

### 1.2 必须纳入决策的硬约束

| 事实 | 出处 / 证据 | 对选型的含义 |
|---|---|---|
| `unified_scanner.py` 是数据心脏；终端输出一个字节没变，桌面版是同一份载荷的**第二个消费者** | `docs/desktop/IMPLEMENTATION.md` §0–§1 | 任何「壳」都**不能**成为第二份数据处理逻辑；否则零契约纪律破 |
| `scanner.db` ≈ **226 MB**（本次 `ls` 实测 227,803,136 B ≈ 217 MiB） | 仓库根 `scanner.db`；`IMPLEMENTATION.md` §4.1 记录包内冷快照 | 包体大小的主要矛盾**不是** Chromium，是 Python 运行时 + 库；换壳省不了大头 |
| 全量 `pytest` ≈ **1954 passed**（另有 13 skipped / 3 个既有失败） | `docs/desktop/HANDOFF.md:76`；`CONTEXT.md:114`；`.workbuddy/memory/2026-10-19.md:6` | 1954 条是纪律资产；换运行时会引入一套全新的、与契约无关的失败面 |
| 契约 `SCHEMA_VERSION = 2`，三端镜像 | `scanner/panel_serialize.py:32`、`rts-panel-cloud/worker/src/serialize.ts`、`web/src/api/types.ts`；`IMPLEMENTATION.md` §4 验收 10 | 加「榜内异动」段都要升 v3 且三端同批发版 ⇒ 架构变更成本极高 |
| Windows 单机优先；打包目标只做 Windows | `IMPLEMENTATION.md` §7.2；`package.json` 只有 `win`；`findPython()` 的 `win32` 分支 | 跨平台框架的卖点（iOS/Android/macOS）对本项目**零价值** |
| 纯本地只读，只绑 `127.0.0.1`；前端产物由 Python 同源托管 | `main.js` 的 `HOST` 常量；`test_desktop_shell.py::test_desktop_binds_loopback_only`；`IMPLEMENTATION.md` §3 | 这正是 2026 年 local-first 形态；换壳必须重现「回环 + 同源 + 免 CORS」 |
| Electron 只是「开窗口 + 拉子进程 + 探活 + 关窗收尸」 | `main.js` 文件头注释；`IMPLEMENTATION.md` §0 | 壳的代码量极小 ⇒ **换壳的收益上限也极小** |
| 打包链：python-build-standalone 3.12 → `resources/python` + `requirements.lock` 精确版 + `prepare.mjs` 前置检查 | `desktop/fetch-python.mjs`、`desktop/prepare.mjs` | 这条链是实测踩坑换来的（用户 site-packages 串味、`dotenv` 漏声明）；换壳要整条重做 |
| 打包态实测 **839.7 MB**（`resources/python` 329 MB + `resources/repo` 142 MB） | `IMPLEMENTATION.md` §4.1 | 见 §3 的反直觉发现：换壳省不下多少 |
| 三处已知差异 | `IMPLEMENTATION.md` §5：①「榜内异动」段未进载荷（刻意，保 v2）；②打包态库与终端分家（`userData` 首启种冷快照）；③双击 exe 会接管/杀掉正在跑的终端扫描器（主程序既有单实例语义） | 都是产品/数据语义问题，与壳无关 |
| 壳有 14 条结构守卫 | `tests/test_desktop_shell.py` | 换壳 ⇒ 这些守卫全部作废或重写 |

### 1.3 版本快照（本次实际读取）

| 位置 | 当前声明 |
|---|---|
| `desktop/package.json` | `electron ^44.9.0`、`electron-builder ^26.15.3` |
| `rts-panel-cloud/web/package.json` | `react ^19.0.0` / `react-dom ^19.0.0`、`vite ^6.0.7`、`typescript ^5.7.3`、`tailwindcss ^4.3.3` + `@tailwindcss/vite ^4.3.3`、`vitest ^5.0.2`、`@vitejs/plugin-react ^4.3.4`、`@radix-ui/react-tabs ^1.2.1`、`react-resizable-panels ^4.14.5`、`clsx`/`tailwind-merge` |
| `pyproject.toml` / `requirements.lock` | `requires-python >=3.12`；lock 精确版 `python-dotenv==1.0.1` / `requests==2.34.2` / `wcwidth==0.2.13` / `psutil==7.2.2` / `akshare==1.18.64` |

---

## 2. 联网查证：2026-11-15 的「当下」到底是什么

### 2.1 Electron：仍是主线，且节奏比 2024 更快

| 结论 | 证据 |
|---|---|
| **当前最新稳定 = Electron 45.3.2**，发布于 **2026-11-13** | [GitHub Releases API](https://api.github.com/repos/electron/electron/releases?per_page=12)；[npm dist-tags `latest=45.3.2`](https://registry.npmjs.org/electron/latest) |
| **本项目用的 44.x 仍在官方支持窗口内**：44.0.0 stable **2026-08-24**，EOL **2027-03-02†**；44.11.3 发布于 2026-11-12 | [Electron Release Schedule](https://releases.electronjs.org/schedule)；[Release API](https://api.github.com/repos/electron/electron/releases?per_page=12) |
| 45.0.0 stable **2026-10-20**（Chromium M156 / Node v24.22.0 / V8 15.6）；44.0.0 是 Chromium M152 / Node v24.18.1 | [Electron 45 发布博客（2026-10-21）](https://www.electronjs.org/blog/electron-45-0)、[Electron 44 发布博客（2026-08-25）](https://www.electronjs.org/blog/electron-44-0)、[Release Schedule](https://releases.electronjs.org/schedule) |
| **发布节奏：每 8 周一个 major**（4 周 alpha + 4 周 beta），对齐 Chromium 4 周节奏；**官方只支持最新 3 个稳定 major** | [Electron Releases / Timelines 官方文档](https://www.electronjs.org/docs/latest/tutorial/electron-timelines)（原文：*"Electron's cadence between major version releases is 8 weeks long"*、*"The latest three stable major versions are supported"*） |
| 2026 年内已发 40/41/42/43/44/45 六个 major | [Electron 官方博客索引](https://www.electronjs.org/blog) |
| 生态规模（本次调研取数）：`electron/electron` **≈123,900 stars**；官方安全文档点名 *"the most popular Electron apps (Atom, Slack, Visual Studio Code, etc)"* | [GitHub repo API](https://api.github.com/repos/electron/electron)；[Electron Security 文档](https://www.electronjs.org/docs/latest/tutorial/security) |
| 本项目的 `contextIsolation: true` / `nodeIntegration: false` / `sandbox: true` **正是官方 2026 年推荐基线** | [Electron Security Checklist](https://www.electronjs.org/docs/latest/tutorial/security)（第 2/3/4 条；原文：*"Even when nodeIntegration: false is used, to truly enforce strong isolation … contextIsolation must also be used"*、*"You should enable the sandbox in all renderers"*） |

> ⚠ **真实差距（不是「Electron 过时」，是「版本落后一个 major」）**：44.x 仍在支持期，但 45.x 已 stable；官方同时明确建议 *"Keep your application up-to-date with the latest Electron framework release"*（[Security 文档](https://www.electronjs.org/docs/latest/tutorial/security)）。这是应补的**维护项**，不是迁移理由。

### 2.2 Tauri 2.x：已成熟、活跃，但对本项目是「净负担」

| 结论 | 证据 |
|---|---|
| **Tauri 2 最新稳定线 = 2.12.5（2026-10-21）**；2.12 官宣博客 **2026-09-26**；2.0 stable 是历史（2024-10-02） | [Tauri Releases API](https://api.github.com/repos/tauri-apps/tauri/releases?per_page=10)、[Announcing Tauri 2.12（2026-09-26）](https://v2.tauri.app/blog/tauri-2.12/)、[Tauri 博客索引](https://v2.tauri.app/blog/) |
| **v3 已在 alpha**：`tauri-v3.0.0-alpha.6` 发布于 **2026-11-06** | [Tauri Releases API](https://api.github.com/repos/tauri-apps/tauri/releases?per_page=10)（⚠ 该 API 把 alpha 标记的 `prerelease` 置为 `false`，但 tag 名明确是 alpha；**不要把它当 stable**） |
| **Windows 端依赖 WebView2 + MSVC C++ Build Tools + Rust 工具链** | [Tauri Prerequisites 官方文档](https://v2.tauri.app/start/prerequisites/)（原文：*"Tauri uses the Microsoft C++ Build Tools for development as well as Microsoft Edge WebView2. These are both required for development on Windows."*、*"Tauri is built with Rust and requires it for development."*）。同页也说明 WebView2 在 Win10 1803+ 已预装，属**开发环境**门槛而非终端用户必然门槛 |
| **包体优势来自「不打包 Chromium，用系统 WebView」** | [Tauri What is Tauri](https://v2.tauri.app/start/)（*"Create small, fast, secure…"*）、[Tauri App Size](https://v2.tauri.app/concept/size/)（*"While Tauri by default provides very small binaries…"*）；**Electrobun 的第三方对比页**给出可复核的 hello-world 数字：Electron 45 = **125 MiB**、Electrobun Zig = 1.7 MiB、Tauri 栏写「Not measured yet」——[Electrobun vs. Electron & Tauri（Last reviewed October 2026）](https://electrobun.dev/electrobun/vs/)。⚠ 该页是**竞品自测**，只作数量级参考，不作权威市占/性能结论 |
| **Rust 后端 + 前端无关**（可接 Vite/任意前端） | [Tauri 官网](https://tauri.app/)（*"Frontend Independent … Tauri supports any frontend framework"*）、[Prerequisites](https://v2.tauri.app/start/prerequisites/) |
| **与「Python 子进程 + standalone 解释器 + SQLite WAL」工作负载的适配难点（有一手文档佐证）** | ① Tauri 官方把这种外部可执行文件叫 `sidecar`，并明确 *"Common use cases are Python CLI applications or API servers bundled using pyinstaller"*、*"prevent users from installing additional dependencies (e.g., Node.js or Python)"* —— [Embedding External Binaries](https://v2.tauri.app/develop/sidecar/)；② `externalBin` **要求每个目标架构一个带 `-$TARGET_TRIPLE` 后缀的二进制**（原文：*"a binary with the same name and a -$TARGET_TRIPLE suffix must exist"*）——本项目 `fetch-python.mjs` 是按 `x86_64-pc-windows-msvc` 单一资产取 `install_only.tar.gz`，整个 `resources/python/` 目录（含 site-packages）不是「一个 sidecar 二进制」，要改成 `resources` 打包 + Rust 侧拼路径 + 目标三元组命名，**整条打包链重写**；③ Tauri 官方 sidecar 教程明确列出 IPC 选项 *"a localhost server, stdin/stdout or local sockets. Note that each has their own advantages, drawbacks and security concerns."* —— [Node.js as a sidecar](https://v2.tauri.app/learn/sidecar-nodejs/)；④ Tauri 还有专门的 [localhost 插件](https://v2.tauri.app/plugin/localhost/)（*"Expose your app's assets through a localhost server instead of the default custom protocol"*）与 [Brownfield Pattern](https://v2.tauri.app/concept/inter-process-communication/brownfield/) 文档页 ⇒ **说明「本地 HTTP 服务 + WebView」是被官方承认的一等架构，不是 Electron 的土办法**；⑤ 权限/Capabilities/CSP 是一套新的声明式安全模型（[Tauri Security 章节](https://v2.tauri.app/security/)），本项目已有 `HOST="127.0.0.1"` 的结构守卫要整套重做 |
| 生态规模（本次调研取数）：`tauri-apps/tauri` **≈112,904 stars** | [GitHub repo API](https://api.github.com/repos/tauri-apps/tauri) |

> ⚠ **未能核实**：Tauri 2.x 的**市占率 / 生产应用数量 / 与 Electron 的份额对比**——本次未找到可信的 2026 年第三方统计（WebSearch 对中英文「2026 桌面框架对比/份额」查询均返回无关结果）。上文 stars 只是社区热度代理，**不等于市占**。请勿把「Tauri 流行」当成可量化事实。

### 2.3 后起 / 相邻方案：都在动，但都不适合「Python 数据心脏」

| 方案 | 2026-11-15 现状（均有出处） | 与本项目适配判断 |
|---|---|---|
| **Electrobun** | 2.0 发布博客 **2026-08-21**（[launch post](https://electrobun.dev/blog/electrobun-2-0/)）；最新稳定 `v2.0.6`（**2026-11-04**），同期 `v2.0.7-beta.21`（2026-11-14）仍是 prerelease（[Releases API](https://api.github.com/repos/blackboardsh/electrobun/releases?per_page=30)）。系统 WebView 为主、可选打包 CEF；主进程可用 TypeScript(Bun/Cottontail) / Zig / Rust / Go / Odin；自家 CLI「Hutch」+ 内置差分更新与签名（[官网](https://electrobun.dev/electrobun/)、[README](https://github.com/blackboardsh/electrobun)、[对比页](https://electrobun.dev/electrobun/vs/)）。≈13,055 stars（[repo API](https://api.github.com/repos/blackboardsh/electrobun)） | **最值得观察，但最不该现在上**：① 2.0 才 3 个月、2.0.7 仍在 beta；② 官方 README 自称 Cottontail 为 *experimental*、Dash 为 *private beta*；③ 它的「~1 MB/4 KB 差分更新」优势建立在**不携带 Python 运行时**的前提上，本项目仍需同样的 329 MB `resources/python` + 226 MB 库；④ 自带一整套新工具链（Hutch / Cottontail / Warren），与 1954 测试和三端契约毫无协同；⑤ 官方对比页自己写明：*"Pick Electron — If you depend on its large ecosystem of modules and examples, or on Electron-specific APIs"* |
| **Wails** | 官方 README 明确 **v2 Stable + v3.0.0 Stable**，且 *"Uses native rendering engines - no embedded browser!"*、*"Build desktop applications using Go & Web Technologies"*（[README](https://github.com/wailsapp/wails)）；`v3.0.0` **2026-10-26**、`v3.1.0` **2026-11-01** 均为非 prerelease，另有 `v3.2.0-nightly.*` 持续滚动（[Releases API](https://api.github.com/repos/wailsapp/wails/releases?per_page=30)）。≈37,107 stars（[repo API](https://api.github.com/repos/wailsapp/wails)）。⚠ `wails.io` / `v3.wails.io` 本次 WebFetch 均 403，**未能核实**其 Windows/WebView2 具体说明页 | Go 后端，与 Python 数据心脏**语言不匹配**；引入 Go 只为「开窗口 + spawn Python」是纯增一层。v3 刚 stable 一个月、仍有 nightly 并行，成熟度不足以承载 1954 条测试纪律 |
| **Neutralinojs** | 最新稳定 `v6.10.0`（**2026-10-06**），另有 nightly（2026-11-05）（[Releases API](https://api.github.com/repos/neutralinojs/neutralinojs/releases?per_page=30)）。官方：*"doesn't bundle Chromium and uses the existing web browser library in the operating system … implements a WebSocket connection for native operations and embeds a static web server to serve the web content"*（[官方文档 Introduction](https://neutralino.js.org/docs/)）。≈8,693 stars（[repo API](https://api.github.com/repos/neutralinojs/neutralinojs)） | 与本项目「本地静态服务器 + WebView」形态**最接近**，但它的原生能力走 WebSocket/扩展 IPC，仍要重写 `main.js` 的 14 条守卫；生态最小、Windows 桌面完成度（签名/更新/图标/菜单）需另验。**未能核实**：v6 在 Windows 的 WebView2 具体行为与 2026 年生产案例 |
| **Flutter Desktop** | 官方 [Desktop support for Flutter](https://docs.flutter.dev/platform-integration/desktop) 页面 `dateModified` = **2026-07-31**（页面仍在维护）；`flutter/flutter` ≈181,263 stars（[repo API](https://api.github.com/repos/flutter/flutter)）。⚠ 本次 **未能核实** Flutter 2026 最新稳定桌面版本号（GitHub Releases API 对该仓库只返回到 2024 的 3.19.0-0.1.pre，Flutter 正式版主要通过 SDK 渠道分发，不在 GitHub Releases） | **自绘引擎（非 WebView）**，意味着 React 19 + 整个 `web/` 前端要**全部重写为 Dart Widget**，`serialize_view` 的消费端从 TS 变 Dart，三端契约纪律直接崩。**最不适配** |
| **纯 Web / PWA 本地化** | 见 §4 表 | 数据层仍是本地 Python 服务；去掉壳只能得到「用户手动开浏览器输 `127.0.0.1:8899`」，失去桌面身份、窗口生命周期、图标/任务栏/自动更新 |

### 2.4 「local-first + WebView 薄壳」是架构潮流本身（不是某个框架）

可核实的 2026 年信号：

- **Tauri 官方自己把「已有本地服务 + WebView」列为正式架构**：[Brownfield Pattern](https://v2.tauri.app/concept/inter-process-communication/brownfield/) 与 [localhost 插件](https://v2.tauri.app/plugin/localhost/)（*"Expose your app's assets through a localhost server instead of the default custom protocol"*）；[Node.js as a sidecar](https://v2.tauri.app/learn/sidecar-nodejs/) 明说 *"package a Node.js application to a self contained binary … without requiring the end user to have a Node.js installation"*，并列出 *"a localhost server, stdin/stdout or local sockets"* 三种 IPC。⇒ **「自带运行时 + 本地 HTTP + WebView 薄壳」是连 Tauri 都承认的一等公民模式，不是 2020 年的过渡方案。**
- **Neutralinojs 官方**同样是「系统 WebView + 内嵌静态服务器 + WebSocket 原生 API」（[Introduction](https://neutralino.js.org/docs/)）；**Electrobun** 默认引擎也是系统 WebView，可选才打包 CEF（[对比页](https://electrobun.dev/electrobun/vs/)）。⇒ 三个不同阵营的壳，收敛到**同一个形态**。
- **local-first 社区 2026 仍在活跃**：[localfirst.fm](https://www.localfirst.fm/) 有 2026-03-31 的「Special: Local-First Conf 2026」；[Local-First Conf 官网](https://localfirstconf.com/) 已挂出「HIGHLIGHTS FROM 2026」并开放 **2027 年 5 月 25–26 日柏林**的 waitlist；经典纲领见 [Ink & Switch, *Local-First Software*](https://www.inkandswitch.com/local-first/)（⚠ 该页需 JS 渲染，本次 WebFetch 未取到正文，**7 条原则原文未能核实**，仅作为概念出处引用，不引具体条文）。
- **Electron 官方文档**亦明确点名主流 Electron 应用「display primarily local content」（[Security](https://www.electronjs.org/docs/latest/tutorial/security)），即「本地内容 + 受信渲染」是官方预期用法。

> **本项目的位置**：`只绑 127.0.0.1` + `Python 同源托管 web/dist` + `零 CORS` + `userData 落库` + `关窗收子进程` = **教科书式的 local-first 单机形态**。它甚至比多数「local-first」项目更纯粹（无云同步、无账号、无远端）。**架构层面没有代际差距。**

> ⚠ **2026 年桌面端趋势综述/对比文章**：按要求尝试了多组中英文检索（WebSearch），返回大量无关结果；**未能核实**到 1~2 篇可信、可引用、有明确发布日期的 2026 年综述长文。可引用的「近似综述」只有两篇**一手/半一手**材料，均已在上面标注性质：① [Electrobun vs. Electron & Tauri（Last reviewed October 2026，厂商自测对比）](https://electrobun.dev/electrobun/vs/)；② [Elanis/web-to-desktop-framework-comparison（GitHub 长期维护的多框架对比表，被 Neutralinojs 官方 README 引用为对比来源）](https://github.com/Elanis/web-to-desktop-framework-comparison)——⚠ 本次 WebFetch 只拿到 GitHub 页面外壳，**其表格内容与最后更新日期未能核实**。这两篇都**不能**当作独立第三方趋势综述；本报告的趋势判断改以官方文档 + 版本/发布事实 + 社区热度数据为准。

### 2.5 前端侧：「React 19 / Vite / Tailwind v4 / 无状态库 / 组件原语」是否仍属主流

**结论：组合本身仍是主流；真正落后的是「具体小版本」，而且是 2026 年才发生的大版本跳跃。**

| 项 | 2026-11-15 现状 | 本项目 | 差距 |
|---|---|---|---|
| **React** | `latest = 19.3.1`（**2026-10-14** 发布）；19.0.0 是 2024-12-05 | `^19.0.0` | **同代，minor 落后**。React 19 仍是当前主线（`dist-tags.latest` 落在 19.x），无「该换 Vue/Svelte/Solid」的信号。⚠ 本次**未能核实** 2026 年前端框架市占百分比（Devographics 2025 结果页需登录，State of JS 2026 仍在开放填写、无结果） |
| **Vite** | `latest = 8.3.8`（**2026-11-11**）；[Vite 8.0 官宣 **2026-03-12**](https://vite.dev/blog/announcing-vite8)——换成 Rolldown 单一 Rust bundler，*"up to 10-30x faster builds"*，*"Vite is now being downloaded 65 million times a week"*；`dist-tags.previous = 6.4.6`（2026-11-09，说明 6.x 仍有维护但已是 previous 线）；Vite 7.0.0 是 2025-06-24、8.0.0 是 2026-03-12 | `^6.0.7` | **落后两个 major**（6 → 8）。这是全表最实质的版本差距；但 `@vitejs/plugin-react` v6 才配 Vite 8，本项目还是 `^4.3.4`，升级是一个**含迁移指南的独立小任务**，不与壳耦合 |
| **TypeScript** | `latest = 7.0.2`（**2026-07-08**）；[官方公告：*"TypeScript 7, a 10x faster native port"*](https://devblogs.microsoft.com/typescript/announcing-typescript-7-0/)；5.7.3 是 2025-01-08 | `^5.7.3` | **落后两个 major**（5.7 → 7.0）。⚠ TS 7 是原生移植，官方公告中的 **5.x 兼容性说明本次未能完整核实**；本项目用 `tsc --noEmit` 做 typecheck，升级须单独验证，别顺手带上 |
| **Tailwind CSS v4** | `latest = 4.3.5`（**2026-11-06**）；`@tailwindcss/vite` 同步 `4.3.5`；4.0.0 是 2025-01-21；`dist-tags.v3-lts = 3.4.19` | `^4.3.3` | **几乎无差距**（一个 patch）。`@tailwindcss/vite` 正是 v4 官方推荐的 Vite 接入方式；`BOUNDARIES.md` §4 记录「Tailwind v4 原生二进制不需要补 `allowBuilds`」，链路干净 |
| **Vitest** | `latest = 5.1.0`（**2026-11-02**）；5.0.0 是 2026-09-03 | `^5.0.2` | 同代，一个 patch |
| **无状态库 / 组件原语** | [State of JS 2025 官方结论](https://survey.devographics.com/en-US/survey/state-of-js/2025/results/)原文：*"TypeScript has won"*、*"…and the day of Vite has come. This year, we saw Vite downloads surpass webpack's. And alongside it, Vitest usage has surged upwards"*、*"2026 promises to be the year that happens – with the stable release of Rolldown"*；[Devographics 调查站](https://survey.devographics.com/) 显示 **State of JavaScript 2026 仍为 Open Survey**（无结果），2025 为已关闭的最新完整版 | `useState` + 零依赖 hash 路由 `lib/router.ts` + `@radix-ui/react-tabs`（组件原语）+ `clsx`/`tailwind-merge` | **方向仍主流**：Vite/Vitest/TS 恰是 2025 调查点名的上升组合；`DESIGN.md` §8 明确「不引 Zustand/Redux/react-router」，对一个**只读、单一快照、轮询刷新**的看板是正确的极简选择。⚠ 「无状态库是否 2026 主流」**未能核实**到量化数据，此处是**基于已知信息的判断**：本项目状态面只有 `useSnapshot` 一份快照 + 几个 UI 偏好键，引入状态库是净增依赖 |

> **有没有明显更该换的方向？** 没有。Svelte 5.57.3 / Vue 3.5.45 / Solid 1.9.19 都在活跃（[npm latest](https://registry.npmjs.org/svelte/latest) 等），但**换 UI 框架 = 重写整个 `web/src` 并重新过 `test_panel_web_hygiene.py` / `test_panel_web_cols.py` / `test_panel_contract.py`**，而收益（更小的 bundle、更快的渲染）对一个 264 KB JS + 4.6 KB CSS 的本地只读页面几乎为零（`IMPLEMENTATION.md` §4.1 实测）。**主流 ≠ 必须换。**

---

## 3. 分两层看选型：主流 / 本项目 / 差距

### 3.1 第一层：应用运行时 / 壳

| | **当下主流（2026-11-15）** | **本项目现在** | **差距** |
|---|---|---|---|
| 形态 | **薄 WebView 壳 + 自带运行时/本地服务 + local-first 同源数据**；三条路线并存且都活跃：① 打包 Chromium（Electron 45.x，8 周节奏）② 系统 WebView + Rust（Tauri 2.12.x，v3 alpha 中）③ 系统 WebView + 新工具链（Electrobun 2.0.6 / Wails 3.1 / Neutralino 6.10） | Electron 44.9.x，只做 4 件事（开窗口 / 拉 Python / 探活 / 收尸） | **形态上无差距**；Electron 是三条主流路线之一，且 star 数最高、发布最密集 |
| 版本 | Electron 45.3.2（2026-11-13）；44.x EOL 2027-03-02† | 44.x（44.11.3 仍在发） | **落后 1 个 major，但仍在支持窗口**；属维护债，不属代际差 |
| 安全基线 | `contextIsolation` + `nodeIntegration:false` + `sandbox:true` + 只加载本地/受信内容 | 全部已具备，且 `HOST` 写死 `127.0.0.1`、外链走 `shell.openExternal` | **无差距，且优于多数示例工程** |
| 包体 | 系统 WebView 派宣称 hello-world 1.7 MiB ~ 十几 MiB，Electron 45 约 125 MiB | 打包实测 **839.7 MB** | **数字差距巨大，但见下「反直觉发现」：成因不是 Chromium** |
| 桌面完成度 | 图标 / 自定义标题栏 / 菜单 / 窗口状态记忆 / 签名 / 自动更新 / 差分更新，是 2026 年「像样产品」的默认项 | 无图标、原生标题栏 + 内部 StatusBar「双头」、无窗口状态记忆、无自动更新、无签名配置 | **这是真正的大差距，且与「换壳」无关**（详见 §6） |

#### 🔍 最反直觉的一条发现

> **换 Tauri/Electrobun 几乎省不下这个 839.7 MB 包，因为体积的大头根本不是 Chromium。**

`IMPLEMENTATION.md` §4.1 的实测构成：`resources/python` **329 MB**（python-build-standalone 3.12 + `requirements.lock` 里 akshare / numpy / pandas 等传递依赖）+ `resources/repo` **142 MB**（其中含 `scanner.db` 冷快照 ≈ 217 MiB 量级的一部分，以及 `web/dist`、docs、源码）+ Electron 运行时本身。Electrobun 竞品自测里「Electron 45 hello-world = 125 MiB」（[对比页](https://electrobun.dev/electrobun/vs/)）是**不含任何应用资产**的裸壳数字。

也就是说：**换壳最多省下 Chromium 那一两百 MB，而 `resources/python` 329 MB 与 226 MB 的 `scanner.db` 一分不少，且 Tauri/Electrobun 还得照样把它们塞进 `resources`/`externalBin`。** 本项目「包体大」的根因是**自带 Python 数据心脏 + 冷快照库**，不是壳。用「包体」当迁移 Tauri 的第一理由，在这个项目上是**算错了账**。

（顺带：`IMPLEMENTATION.md` §5 已记录「打包态库与终端分家、`userData` 首启种冷快照」——**冷快照是否必须随包分发**本身就是一个比「换壳」性价比高得多的体积优化问题，但那属于数据/分发决策，本次不改代码、只标为可研究项。）

### 3.2 第二层：前端 UI

| | **当下主流（2026-11-15）** | **本项目现在** | **差距** |
|---|---|---|---|
| 框架 | React 19.x（`latest 19.3.1`）仍是主线 | React 19.0.0 | 同代，minor 级 |
| 构建 | Vite 8.3.8 / Rolldown；Vite 6 已是 `previous` 线 | Vite 6.0.7 | **落后 2 major**，是唯一实质版本差 |
| 类型 | TypeScript 7.0.2（原生移植） | TS 5.7.3 | **落后 2 major**，需单独验兼容 |
| 样式 | Tailwind v4 + `@tailwindcss/vite`（`4.3.5`） | `4.3.3` | 几乎为零 |
| 测试 | Vitest 5.1.0 | `5.0.2` | 一个 patch |
| 状态 / 组件 | 「够用就好」：原生 `useState` + 组件原语（Radix 等）+ 少量 class 工具；Vite/Vitest/TS 是 2025 调查点名的上升组合 | 完全符合：`useState` + 零依赖 hash 路由 + Radix Tabs + clsx/tailwind-merge，`DESIGN.md` §8 明确不引状态库 | **方向无差距** |
| 设计系统 / 可访问性 | token 化表面层级、5 级字阶、tabular-nums、focus-visible、`aria-*`、reduced-motion、多态空态 | `DESIGN.md` + `preview.html` **已有完整规格**（§3/§5/§7/§10），但**生产代码尚未落地** | **规格有、实现无**——这是「文档领先、代码落后」，不是技术栈落后 |
| 依赖卫生 | 少依赖、可复现构建 | 7 个 runtime 依赖 + `prepare.mjs` 前置检查 | 优，无需「现代化」 |

> **结论**：前端层没有「该换方向」的证据；只有一次**工具链版本升级**（Vite 6→8、TS 5.7→7、Electron 44→45、Tailwind/vitest patch），而且必须与视觉重做**解耦、分批**做，因为 `BOUNDARIES.md` §4 的守卫只保证「不新增依赖/不改契约」时零 Python 改动成立。

---

## 4. 逐方案对比表

> 工期/风险量级为**基于已知信息的估计**（未执行任何迁移，未跑构建），标注为「推测量级」，不是承诺。

| 维度 | **A. Electron 维持现状（+小版本升级）** | **B. Tauri 2 迁移** | **C. 新壳（Electrobun / Wails / Neutralino）** | **D. 纯 Web / PWA 本地化（去壳）** | **E. Flutter Desktop** |
|---|---|---|---|---|---|
| **对本项目的适配度** | **★★★★★** 现有 4 件事全保留；45.x 仍在 8 周节奏内；`test_desktop_shell.py` 14 守卫继续有效 | ★★☆ 官方承认 sidecar/localhost 形态，但 Rust + WebView2 + C++ Build Tools + 权限模型是全新面 | ★☆ Electrobun 2.0 才 3 个月且 2.0.7 在 beta；Wails 是 Go；Neutralino 生态最小 | ★★☆ 数据层不变，但失去窗口/图标/任务栏/生命周期/更新 | ★ 自绘引擎，React 前端全废 |
| **需要重写什么** | 仅 `package.json` 版本号（+ 可选 `BrowserWindow` 选项、图标、preload） | **`main.js` 整份重写为 Rust**（findPython / env 净化 / execFile / 探活 / kill / 单实例 / userData 种子拷贝 / errorPage）；新增 `src-tauri/`、`tauri.conf.json`、Capabilities、CSP、sidecar 目标三元组命名；**`tests/test_desktop_shell.py` 14 条守卫全部作废或重写**；`prepare.mjs` / `electron-builder` 配置整条替换 | 同 B，另加各自新工具链（Hutch / Cottontail、Go 模块、neu CLI） | `main.js` 删除；改为「用户自己起 Python + 开浏览器」或加一个启动脚本；无窗口体验可写 | **`web/src/**` 全部重写为 Dart**；`api/types.ts` 换成 Dart 模型，三端镜像变四端 |
| **对 1954 测试与契约纪律的影响** | **零**：`SCHEMA_VERSION=2`、三端镜像、`serialize_view` 单源、`tests/**` 全不动 | 契约本身可不动（Python 服务照跑），但**壳的 14 条结构守卫失去对象**；新增 Rust 侧无 pytest 覆盖；`RTS_LOCAL_PANEL` / `PYTHONNOUSERSITE` / 回环 / userData 种子「不覆盖」等纪律要在 Rust 里重新实现并重新证明 | 同 B，且新壳没有对应的成熟测试范式可复用 | 契约零影响；但「关窗收子进程」验收 7 变成「用户自己关终端」，生命周期纪律消失 | **最大**：契约消费端语言都变了，`test_panel_contract.py` / `test_panel_web_cols.py` 的前提被掏空 |
| **打包链改动** | `fetch-python.mjs` / `prepare.mjs` / `requirements.lock` **一字不改**；`electron-builder` 仍是 `extraResources` 两条 | **整条重写**：`externalBin` + `-$TARGET_TRIPLE` 命名（[sidecar 文档](https://v2.tauri.app/develop/sidecar/)）与「整个 `resources/python/` 目录 + 226 MB 冷快照 + `web/dist`」的 `resources` 打包冲突；`fetch-python.mjs` 取单一 Windows 资产的假设要改；Tauri 的 [Windows Installer / Sign](https://v2.tauri.app/distribute/windows-installer/) 另起一套 | 同 B，且 Electrobun 的 Hutch / Wails 的 Go 构建 / Neutralino 的 neu CLI 各自要新 CI | 打包链消失（好事），但也失去分发物 | 全新 Dart/Flutter 工具链 |
| **Windows 签名 / 分发** | `electron-builder` 现成（`dist:installer` 已有）；⚠ 本仓**未见代码签名配置**，`IMPLEMENTATION.md` §7.2 明确只做 Windows `dir` 目标 → **这是空白，不是缺陷** | Tauri 有 [Windows 签名文档](https://v2.tauri.app/distribute/sign/windows/) 与 [Updater 插件](https://v2.tauri.app/plugin/updater/)，能力具备但要重新接入 | Electrobun 宣称「签名内建 + 4 KB 差分更新」（[官网](https://electrobun.dev/electrobun/)），**但均为厂商自述，未在本仓/第三方核实** | 无安装器、无签名、无自动更新 | 另一套 Windows 打包 |
| **包体变化** | 839.7 MB 不变；45.x 与 44.x 同量级 | **推测**：只省 Chromium 部分（竞品自测裸壳 125 MiB 量级），329 MB Python + 226 MB 库照带 ⇒ 仍数百 MB | 同 B | 分发物最小（无壳） | 引擎 + Dart 运行时 + 仍需 Python |
| **工期（推测量级）** | **天级**（版本 bump + 回归） | **周级~月级**（Rust 重写 + 打包链 + 守卫重建 + 真机验 WAL/userData/单实例） | 月级，且踩坑无 prior art | 天级（但产品形态退化） | 月级以上（前端全重写） |
| **风险量级** | **低**：唯一风险是 45.x 的 Chromium/Node 升级对渲染的影响；本项目只用 `loadURL` + 静态页，面很窄 | **高**：`IMPLEMENTATION.md` §6 记录的坑（用户 site-packages 串味、`dotenv` 漏声明、`PYTHONNOUSERSITE`、WAL 冷快照、`app.isPackaged` 分支）要在 Rust 侧**重新踩一遍**；Tauri v3 alpha 还带来未来二次迁移的不确定性 | **很高**：框架本身在快速变动（Electrobun 2.0.7 beta、Wails nightly），无 LTS 承诺 | 中（产品退化风险 > 技术风险） | 极高 |

**表读法**：A 在「适配度 / 测试纪律 / 打包链 / 工期 / 风险」五项全胜；B/C/E 唯一能拿分的是「裸壳体积」，而 §3.1 已论证这个分数在本项目上几乎不落地。D 是**产品降级**而非技术升级。

---

## 5. 结论：保留什么 / 更新什么 / 明确不做什么

### 5.1 ✅ 保留（不动架构）

1. **Electron 作为薄壳**，及其「四件事」职责边界：开窗口、`execFile` 拉 Python、探活 `127.0.0.1:<port>/api/meta`、关窗 `kill`。
2. **Python `unified_scanner.py` 作为唯一数据心脏**；`serialize_view` 单源、`SCHEMA_VERSION=2` 三端镜像、`tests/**` 与 1954 条基线全不动。
3. **`127.0.0.1` 回环 + Python 同源托管 `web/dist` + 免 CORS + 纯只读**——这是 local-first 主形态，不是历史包袱。
4. **`userData` 可写落点 + 首启种冷快照「绝不覆盖」+ WAL 不进包 + `.env` 不进包**的全套纪律（`test_desktop_shell.py` 锁死）。
5. **`fetch-python.mjs` / `prepare.mjs` / `requirements.lock` 打包链**（含 `PYTHONNOUSERSITE=1` 与「自带解释器真跑 `import unified_scanner`」两道前置）。
6. **前端组合**：React 19 + Vite + TS + Tailwind v4 `@tailwindcss/vite` + Vitest + 无状态库 + Radix 组件原语 + 零依赖 hash 路由。
7. **`contextIsolation/nodeIntegration:false/sandbox:true` 安全基线**与 `shell.openExternal` 外链策略。

### 5.2 🔄 更新（低风险、可分批、与视觉重做解耦）

| 优先 | 动作 | 出处/理由 | 前提 |
|---|---|---|---|
| P0 | **Electron 44 → 45**（`^44.9.0` → 45.x 稳定线），`electron-builder 26.15.3 → 26.18.0` | [Release Schedule](https://releases.electronjs.org/schedule)（44 EOL 2027-03-02†、45 stable 2026-10-20）+ [官方安全建议](https://www.electronjs.org/docs/latest/tutorial/security) | 单独一次回归：`pnpm start` + 打包态 DevTools 冒烟（`IMPLEMENTATION.md` §4.1 那套），别和 UI 改动混一个 diff |
| P0 | **补桌面完成度**（见 §6）：应用图标 `.ico` + `build.win.icon` / `BrowserWindow.icon`、`autoHideMenuBar:true` 或 `Menu.setApplicationMenu(null)`、窗口尺寸/位置记忆到 `userData`、`backgroundColor` 三处同色 | `BOUNDARIES.md` §3.2 / §7 已判定「可独立做、低风险、无守卫冲突」 | 新资源进 `build.files`，并把新资源加进 `prepare.mjs` 的 `CHECKS`（`BOUNDARIES.md` §3.2 建议，不冲突守卫） |
| P1 | **Vite 6 → 8**、`@vitejs/plugin-react` → v6、TS 5.7 → 7（**分开验**）、Tailwind/vitest patch | [Vite 8 官宣](https://vite.dev/blog/announcing-vite8)、[TS 7 官宣](https://devblogs.microsoft.com/typescript/announcing-typescript-7-0/)、npm registry | ⚠ TS 7 是原生移植，**5.x 兼容性本次未核实**；Vite 8 要求 Node 20.19+/22.12+；`@vitejs/plugin-react` v5 仍兼容 Vite 8（官方博客），可分步。**这是独立任务，不是「桌面版技术选型」的一部分** |
| P1 | 把 `DESIGN.md` / `preview.html` 的规格落地到 `web/src/**`（P0 身份层级 → P1 空态/状态 → P2 窗口壳） | `BOUNDARIES.md` §5 / `DESIGN.md` §9 已有分期与门禁 | 严守 §7 的「零 Python 改动」边界 |
| P2 | 自定义标题栏 `frame:false` + 受限 `preload` 三白名单 IPC（**最后做、原型先行**） | `DESIGN.md` §4.1 / §9 P2、`BOUNDARIES.md` §3.2 P2b | 与 `StatusBar` 双头、关窗 kill 语义隔离；`sandbox:true` 下 preload 行为 **BOUNDARIES §6 标注为未验证** |

### 5.3 ⛔ 明确不做

1. **不迁移 Tauri 2 / Tauri 3 alpha / Electrobun / Wails / Neutralino / Flutter Desktop。** 理由见 §4 表 + §5.4。
2. **不换 UI 框架**（Vue / Svelte / Solid / Dart）。1954 测试与三端契约的语法前提全是 `web/src/*.ts(x)`；换语言 = 重写纪律。
3. **不引状态库 / react-router / 图表库 / 新请求**。`DESIGN.md` §8 已列硬纪律；`IMPLEMENTATION.md` §8 记录 `StatsCharts`（330 KB 图表依赖）已删且不回归。
4. **不动 `SCHEMA_VERSION=2`、不加「榜内异动」段**（那要升 v3 且云面板 + 桌面 + 前端列投影同批发版，`IMPLEMENTATION.md` §5/§7.1 已拍板暂不加）。
5. **不把打包目标扩到 macOS**（`IMPLEMENTATION.md` §7.2：需另取 aarch64 standalone Python 并过签名/公证，另立任务）。
6. **不改「关窗 = 退出 + kill Python」语义、不做「最小化到托盘 + 后台常驻」**（`BOUNDARIES.md` §3.2：与 `test_desktop_shell.py` 及验收 7 冲突）。
7. **不为「追流行」而推倒重来，也不因保守而隐瞒真实差距**（§6 就是差距清单）。

### 5.4 重点论证：为什么「追着换运行时」对这套 Python 数据心脏是净负担

**(a) 壳的价值已经被压缩到极限。** `main.js` 里没有任何数据加工，Node 侧连 SQLite 都不读（`test_node_side_never_reads_sqlite_directly`）。一个只做「开窗口 + spawn + 探活 + kill」的组件，无论用 Electron、Rust 还是 Zig 写，代码量都是几百行。**换壳能优化的上限 = 这几百行 + Chromium 体积**，而它要触动的是下面整条链：

```
fetch-python.mjs（单 Windows 资产 / pip 禁用户 site）
  → resources/python（329 MB，含 akshare/numpy/pandas 传递依赖）
  → prepare.mjs（真跑 import unified_scanner 的静默缺失前置）
  → main.js（env 净化 7 变量 + PYTHONNOUSERSITE + RTS_DB_PATH/RTS_LOG_DIR/RTS_ENV_FILE）
  → userData 种子不覆盖 + WAL 不进包
  → test_desktop_shell.py（14 条结构守卫）
  → electron-builder extraResources 两条 + 4 条被锁 filter
```

`IMPLEMENTATION.md` §6 记录的坑（用户 site-packages 排到自带 site-packages **之前**、`python-dotenv` 漏声明导致「打包成功但双击炸」、WAL 冷拷贝不一致）**全部是实测踩出来的**，且全部位于「壳 ↔ Python」的接缝上。换壳 = 把这条接缝在另一门语言里重新踩一遍，而 Python 侧一个字节都不用改。**这是典型的「改动面最大、收益面最小」**。

**(b) 体积与跨平台卖点在本项目上失效。** 见 §3.1 反直觉发现：329 MB Python + 226 MB 库是硬成本；Tauri 的移动端、Electrobun 的 4 KB 差分更新、Wails 的 Go 单二进制，对「Windows 单机、无云、无同步」的场景都不是痛点。`IMPLEMENTATION.md` §7.2 已把 macOS 明确排除。

**(c) 「薄 WebView 壳 + 现代前端 + local-first 同源」本身就是当下主流。** §2.4 的证据链：Tauri 官方有 [Brownfield Pattern](https://v2.tauri.app/concept/inter-process-communication/brownfield/) 和 [localhost 插件](https://v2.tauri.app/plugin/localhost/)；Neutralino 官方是「系统 WebView + 内嵌静态服务器」；Electrobun 默认系统 WebView；Electron 安全文档预期「本地内容」。**三个互相竞争的阵营收敛到同一形态**——那么「形态」就是主流，具体用哪家的壳只是实现选择。本项目 2026-10 交付的正是这个形态，且比多数方案更彻底（无账号、无云、无远端、`127.0.0.1` 硬编码有守卫）。

**(d) 流行度数据不支持「Electron 已过时」。** 8 周一个 major、2026 年发了六个 major、45.3.2 在 2026-11-13 还在出补丁、≈123,900 stars、44.x 支持到 2027-03（[Release Schedule](https://releases.electronjs.org/schedule) / [Blog](https://www.electronjs.org/blog)）。反过来，Tauri v3 还在 alpha（2026-11-06）、Electrobun 2.0.7 还在 beta（2026-11-14）、Wails nightly 并行——**现在迁过去，等于用一套稳定的纪律资产去换一套正在动的资产**。

**(e) 真正的差距在体验层，不在运行时层。** 见 §6：图标、菜单、双头、空态、字阶、可访问性、键盘、签名、自动更新。这些 100% 可以在**零 Python 改动**下补完（§7），而换壳一项都补不了。

---

## 6. 真实差距清单：相较 2026 年一个「像样产品」，到底落后在哪

> 标注：`✅已有方案` = `DESIGN.md` / `preview.html` / `BOUNDARIES.md` 已覆盖设计与可行性；`🟡部分` = 有结论但缺实现细节或未验证；`⬜空白` = 三份文档均未覆盖。
> **注意：这些差距全部与「换壳」无关**——换到 Tauri 一样要补。

| # | 差距 | 现状证据 | 覆盖状态 |
|---|---|---|---|
| 1 | **应用图标与品牌身份** | `BOUNDARIES.md` §3.1：全仓无产品图标（只有 Python 自带 IDLE/Tk 演示图与界面截图）；`package.json` 无 `build.win.icon`，`BrowserWindow` 无 `icon` | ✅ `BOUNDARIES.md` §3.2 / §7 第 1 项给了完整落地路径（造多尺寸 `.ico` → `build.files` → 两处 icon 配置 → `prepare.mjs` 补 `CHECKS`）；`DESIGN.md` §3/§5.6 有品牌色与侧栏标识规格 |
| 2 | **原生标题栏 + 内部 StatusBar「双头」；无自定义标题栏/窗口控件** | `main.js` 未设 `frame`/`titleBarStyle`；`DESIGN.md` §0 现状表第 1 条 | ✅ `DESIGN.md` §4.1 / §9 P2 定了 frameless + 三白名单 IPC + `no-drag`；`BOUNDARIES.md` §3.2 判定「需配套改动、P2b 原型先行」；🟡 `sandbox:true` 下 preload / `-webkit-app-region` 点击吞没**未验证**（`BOUNDARIES.md` §6） |
| 3 | **应用菜单 / 托盘** | `autoHideMenuBar:false` 显式、无 `Menu`/`Tray`/`ipcMain`/`preload` | ✅ `BOUNDARIES.md` §3.2：菜单收口「可独立做、低风险」；托盘「仅显示/退出且保持关窗=退出」可做，「最小化到托盘 + 常驻」明确别碰 |
| 4 | **窗口尺寸/位置记忆** | `main.js` 只有固定 1440×900 / min 900×600；`App.tsx` 另有分栏 localStorage | ✅ `BOUNDARIES.md` §3.2 / §7 第 2 项：`userData/window-state.json`，与分栏持久化互补、不碰被锁字面量 |
| 5 | **设计系统 / 字阶 / 表面层级 / 动效** | 现状「近全灰、字号集中 11~12px、卡片与底色几乎无对比」（`DESIGN.md` §0 用户反馈） | ✅ `DESIGN.md` §3（4 级 surface + 5 级字阶 + tabular-nums + 10px 圆角 + 两处白名单渐变 + 120~160ms）+ §10 验收清单；`preview.html` 已高保真实现 |
| 6 | **空态 / 多态状态表达** | 空区只有 `—（本轮无数据）` 一行灰字；下半屏约 250px 纯黑（`DESIGN.md` §0） | ✅ `DESIGN.md` §5.3 / §5.4 / §7（136px 区块空态、90px 段空态、5 态胶囊 + `aria-live` 解释弹层）；`BOUNDARIES.md` §5 P1 列了全部待改文件与「文案不能写 11 个列名 / 9 个结论词」的雷区 |
| 7 | **命令驱动 vs 工作台驱动** | 当前是「四个区块 + 侧栏 + 详情」的只读镜像；无命令面板、无筛选/排序（后者是**刻意**纪律） | 🟡 `DESIGN.md` §5.6 / §7 把侧栏升级为「图标 + 强选中 + 行数徽章 + 键盘可达」的工作台导航；但**命令面板 / 全局搜索 / 快捷跳转**三份文档均未提 → ⬜ **空白**（且与「不加筛选/排序」纪律需划清边界）。本项目是只读看板，命令驱动是否必要本身也值得产品判断 |
| 8 | **可访问性与键盘** | 生产代码现状未见完整 `aria-*`/focus-visible/reduced-motion | ✅ `DESIGN.md` §7 有逐控件键盘表（Tab / Enter / Space / Esc / ←→ / Home / End、`aria-expanded`/`aria-controls`/`aria-current`/`aria-pressed`、focus 归还、`prefers-reduced-motion`）+ §10 验收。⚠ 仅**规格**，生产代码未落地 |
| 9 | **分发 / 代码签名 / 自动更新** | `package.json` 有 `dist:installer` 但 `win.target:["dir"]`、无签名配置；`IMPLEMENTATION.md` §7.2 只做 Windows；无任何 updater | ⬜ **空白**：`DESIGN.md` / `preview.html` / `BOUNDARIES.md` **三份文档都没覆盖**签名证书、MSIX/NSIS 选择、`electron-updater` / Squirrel、更新通道与回滚。作为对照，[Electrobun 官网](https://electrobun.dev/electrobun/) 与 [Tauri Updater 插件](https://v2.tauri.app/plugin/updater/) 都把「签名 + 自动更新」做成开箱项 ⇒ **2026 年产品默认项，本项目缺** |
| 10 | **崩溃/异常退出的子进程孤儿** | `IMPLEMENTATION.md` §8「记录不修」：Electron 崩溃 / `taskkill /F` 时 Python 成孤儿 | 🟡 已知、有记录、明确不修（正常关窗路径已覆盖，单实例「杀旧」语义下次启动可接管） |
| 11 | **打包体积与冷快照策略** | 839.7 MB；包内含 `scanner.db` 冷快照；`IMPLEMENTATION.md` §5 第 2 条记录「打包态库与终端分家」 | 🟡 有记录无优化方案 → 冷快照是否必须随包、能否改为首启建空库 + 增量补齐，是**未被三份文档讨论**的分发策略空白 |
| 12 | **Electron 版本与前端工具链版本** | 44.x / Vite 6 / TS 5.7（见 §2.1、§2.5） | 🟡 本次报告 §5.2 首次列出；`BOUNDARIES.md` §4 只讨论「新增依赖才动 `pnpm-workspace.yaml`」，**未讨论跨 major 升级** |

**最该补的 2 个空白**（按「2026 产品默认项 + 现有文档完全没覆盖」排序）：
1. **分发 / 代码签名 / 自动更新**（第 9 行）：唯一一个三份文档全部没提、且属于「装到别人电脑上」的门槛项。
2. **命令驱动 / 工作台导航与键盘效率的产品定位**（第 7 行 + 第 8 行落地）：规格写得很满，但「这个看板到底该是命令驱动还是工作台驱动」没有产品结论，且 §8 的 a11y 规格尚未进生产代码。

（若按「性价比最高」而非「最空白」排，则是 `BOUNDARIES.md` §7 已点名的两项：**图标 + 菜单收口**、**窗口尺寸记忆**——它们零守卫冲突、当天可做完。）

---

## 7. 迁移触发条件（可判定，不看情况）

> 只有**满足下列任一硬条件**才值得立项真迁移到 Tauri / 其它壳。每条都写成可观测、可勾选的判据；**「感觉 Electron 过时」「别人都在用 Tauri」一律不算**。

| # | 触发条件（可判定） | 判据 / 怎么验 |
|---|---|---|
| **T1** | **包体成为可量化的分发阻断** | 有明确渠道/用户反馈证明「下载/安装失败率」与包体相关，且经拆解确认 **Chromium 部分占比 > 30%**；同时 329 MB `resources/python` 与冷快照已先通过剥离/压缩降不下来。若拆解后大头仍是 Python + `scanner.db`，**换壳不解决问题，不触发** |
| **T2** | **壳本身成为性能/稳定性瓶颈** | 有可复现的实测数据（冷启动 P95、内存常驻、崩溃率）证明瓶颈在 Electron 运行时而非 Python 扫描首轮或 `scanner.db`；且维持现状的优化（45.x 升级、预加载、窗口复用）已穷尽。本项目 `IMPLEMENTATION.md` §4.1 打包态实测「控制台零报错、四条 resource 全 200」，当前**不满足** |
| **T3** | **Electron 主线终止或 44.x/45.x 全部 EOL 且无可用升级路径** | 以 [releases.electronjs.org/schedule](https://releases.electronjs.org/schedule) 为准：当在用的 major 及其后继均进入 End of Life、且官方 3-major 支持窗口内无稳定版本时触发。2026-11-15 时 44.x EOL 2027-03-02†、45.x 2027-04-27†、46 已排期，**不满足** |
| **T4** | **必须交付 macOS / Linux / 移动端，且愿意为每平台重做 standalone Python 资产** | 出现明确的多平台交付需求（`IMPLEMENTATION.md` §7.2 目前明确「只做 Windows」），且已为 `aarch64-apple-darwin` 等目标重新验证 `fetch-python.mjs` 的 `pickAsset` 正则、签名/公证链。**注意：这条触发的是「跨平台工程」，不是「换壳」**——Electron 本身跨平台，先评估直接扩 target 再谈换运行时 |
| **T5** | **Python 数据心脏被替换掉**（架构前提消失） | `unified_scanner.py` / `scanner.db` / `serialize_view` 三端镜像中≥2 项被下线或改成 Rust/Go 实现，`SCHEMA_VERSION` 三端契约解散，1954 条 pytest 的相关守卫不再成立。**此时壳与数据层可一起重选；在此之前换壳是单腿走路** |
| **T6** | **新壳达到可验证的成熟度门槛，且迁移成本有预算** | 同时满足：① 目标壳有**非 alpha/beta 的 stable + 明确 LTS/支持承诺**（对标 Electron 官方「最新 3 个 stable major」政策，[Timelines](https://www.electronjs.org/docs/latest/tutorial/electron-timelines)）；② 有 ≥1 个可核实的「Python sidecar + 本地 SQLite WAL + Windows 签名分发」生产案例；③ 已为 `test_desktop_shell.py` 的 14 条守卫写好等价替代并通过；④ 有独立回归窗口（打包态 `/api/meta`、DOM/样式、关窗 0 残留、端口释放）与回滚方案。**2026-11-15 时 Tauri v3 在 alpha、Electrobun 2.0.7 在 beta，不满足①** |
| **T7** | **安全/合规要求明确禁止打包 Chromium** | 有书面合规条款或客户安全基线要求「不得随应用分发浏览器引擎」。此时才轮到系统 WebView 派（Tauri/Neutralino/Electrobun）作为**合规驱动**选项，且仍须先过 T1 的体积账 |

**否决项（即使上面某条成立也不该立刻动）**：`SCHEMA_VERSION` 正处于升版窗口、或 1954 条 pytest 存在未清零的既有失败、或 `docs/desktop/IMPLEMENTATION.md` §5 的三处已知差异尚未验收——**先收口既有纪律，再谈架构**。

---

## 8. 「前端落地零 Python 改动」是否仍成立？与 BOUNDARIES.md 是否一致

### 8.1 结论：**一致，且本报告不改变其边界**

`BOUNDARIES.md` §4 的结论原文是 **「成立，且有条件」**，其精确定义为：

> `scanner/**`、`unified_scanner.py`、`tests/**`、`requirements*.txt|lock` **全部保持原样**；只改 `rts-panel-cloud/web/src/**`（+ 可选 `web/index.html`、`web/pnpm-workspace.yaml` **当且仅当新增依赖**）+ P2 的 `desktop/**`（+ 可选新图标/preload 资源）。

本次调研**逐条复核了它的依据**，没有发现冲突：

| BOUNDARIES 的连带项 | 本次复核 | 一致性 |
|---|---|---|
| `scanner/local_panel_server.py` 静态托管 / SPA 回退不需要改 | `IMPLEMENTATION.md` §1 记录 `_static()` 从 `web/dist` 回放并对未知非 `/api` 路径回退 `index.html`；`lib/router.ts` 是 hash 路由，不需回退；前端 `api/client.ts` 走 `/api/*` 相对路径 | ✅ 一致 |
| `prepare.mjs` 的 `web/dist/index.html` 前置检查**不需改、但必须跑** | 本次读 `prepare.mjs`：`CHECKS[1]` 正是该路径；`test_desktop_shell.py::test_prepare_rejects_python_that_cannot_import_entry` 锁 3 个字符串 | ✅ 一致 |
| `pnpm-workspace.yaml` 的 `allowBuilds`：不新增依赖 ⇒ 不动 | 本次读 `web/package.json`：7 个 runtime 依赖全是纯 JS/TS（Radix Tabs、clsx、kline-component、react-resizable-panels、tailwind-merge）；Tailwind v4 走 `@tailwindcss/vite` 原生二进制，`BOUNDARIES.md` §4 已记录「实测不需要补键」 | ✅ 一致 |
| `requirements.lock` / `requirements.txt` 不动 | 本次读两个文件 + `test_entry_module_level_imports_are_declared_in_requirements`：视觉重做不新增 Python import | ✅ 一致 |
| `panel_serialize.py` / `worker/src/serialize.ts` / `web/src/api/types.ts` 三端镜像不动 | 本次 grep 确认 `SCHEMA_VERSION=2` 在 `scanner/panel_serialize.py:32` 且 `worker/src/routes/ingest.ts:42` 校验它；`DESIGN.md` §8 第 1 条也写「不动契约」 | ✅ 一致 |
| `web/dist` 必须重新生成（**是构建产物，不是源码改动**） | `IMPLEMENTATION.md` §2 三种跑法第 ③ 条：`cd rts-panel-cloud/web; pnpm build` | ✅ 一致 |
| `desktop/package.json` / `main.js` **P2 才动** | `BOUNDARIES.md` §5 P2a/P2b；`DESIGN.md` §9 明确「P0/P1 不碰 `desktop/**`」 | ✅ 一致 |

**破零 Python 的反例**（`BOUNDARIES.md` §4 末尾）本次也复核成立：改 API 路径或 `{ok,data}` 信封、改 `regions` 字段集 / 加「榜内异动」段、改端口常量三处同步（`scanner.config.LOCAL_PANEL_PORT` ↔ `main.js` ↔ `vite.config.ts`）、hash 路由换 history 且删/改 `_static` 回退、给前端新增本地只读端点。

### 8.2 需要说清的**三点差异**（不是矛盾，是范围/口径不同）

1. **范围差异：本报告多谈了「跨 major 版本升级」，BOUNDARIES 的「零 Python」不包含它。**
   `BOUNDARIES.md` §4 讨论的是「新增依赖 / 改契约 / 改路由形态」，**没有讨论** Electron 44→45、Vite 6→8、TS 5.7→7 这类工具链跨代升级。本报告 §5.2 把它们列为独立的 P0/P1 维护任务。这些升级**仍然零 Python 改动**（`scanner/**`、`unified_scanner.py`、`tests/**`、`requirements*` 都不碰），但它会动 `web/package.json` / `pnpm-lock.yaml` / `desktop/package.json`，且可能触发 `pnpm-workspace.yaml` 的 `allowBuilds` 复核（Vite 8 → Rolldown 原生二进制、`@vitejs/plugin-react` v6）。⇒ **「零 Python」成立，「零 lockfile / 零构建链改动」不成立**。这一点必须写清，否则容易被当成「升级也是零成本」。

2. **口径差异：本报告把「壳的体验打磨」也算在「零 Python」里，但明确了它是 `desktop/**` 侧改动。**
   `BOUNDARIES.md` 的定义允许 P2 动 `desktop/**`（含新增 `preload.js` / 图标），且 §3.2 逐项判定 `frame:false` 需要 preload + `ipcMain` + `build.files` + `prepare.mjs` 补 `CHECKS`。本报告 §6 的图标/菜单/窗口记忆/frameless 全部落在这一侧，**不越界**；但 `prepare.mjs` 一旦被改，`test_desktop_shell.py::test_prepare_rejects_python_that_cannot_import_entry` 的 3 个字符串必须仍在（`BOUNDARIES.md` §3.2 已说明「新增检查不冲突」）。

3. **证据差异：本报告补上了 BOUNDARIES 未做的「联网时效核对」。**
   `BOUNDARIES.md` 是**纯静态代码审计**（其 §6 第 1 条自承「没有跑任何 pytest / pnpm / node / Electron / 打包命令」），它不回答「44.x 是否还主流 / Vite 6 是否过时 / Tauri 到哪一代了」。本报告 §2 补的正是这一层，结论是：**「零 Python 改动」的工程结论不因 2026 年的版本演进而失效，反而因为前端组合仍在主流而更稳。**

> ⚠ **本次调研自身也未跑任何构建/测试/打包**（遵守交付约束）。因此上述「一致」是**静态复核 + 联网核对**，不是实测绿灯。真落地时仍按 `BOUNDARIES.md` §5「分期共用门禁」跑：`pnpm typecheck && pnpm test && pnpm build` → 六组 pytest 纪律守卫 → `node prepare.mjs` → P2 追加 Electron 真机冒烟。

---

## 9. 来源索引（全部为本次实际访问）

**官方 / 一手**
- [Electron Releases & Timelines（8 周节奏 / 最新 3 major 支持政策）](https://www.electronjs.org/docs/latest/tutorial/electron-timelines)
- [Electron Release Schedule（43/44/45/46 稳定与 EOL 日期）](https://releases.electronjs.org/schedule)
- [Electron 官方博客索引（2026 年 40–45 发布）](https://www.electronjs.org/blog) · [Electron 45（2026-10-21）](https://www.electronjs.org/blog/electron-45-0) · [Electron 44（2026-08-25）](https://www.electronjs.org/blog/electron-44-0)
- [Electron Security Checklist（安全基线 / 本地内容建议）](https://www.electronjs.org/docs/latest/tutorial/security) · [Process Model](https://www.electronjs.org/docs/latest/tutorial/process-model)
- [Electron GitHub Releases API](https://api.github.com/repos/electron/electron/releases?per_page=12) · [npm electron dist-tags](https://registry.npmjs.org/electron)
- [Tauri Prerequisites（Windows WebView2 / C++ Build Tools / Rust）](https://v2.tauri.app/start/prerequisites/) · [What is Tauri](https://v2.tauri.app/start/) · [App Size](https://v2.tauri.app/concept/size/)
- [Tauri Embedding External Binaries / sidecar（Python CLI、TARGET_TRIPLE 命名）](https://v2.tauri.app/develop/sidecar/) · [Node.js as a sidecar（localhost server / stdin-stdout / sockets）](https://v2.tauri.app/learn/sidecar-nodejs/) · [localhost 插件](https://v2.tauri.app/plugin/localhost/) · [Brownfield Pattern](https://v2.tauri.app/concept/inter-process-communication/brownfield/)
- [Tauri 官网](https://tauri.app/) · [Blog 索引](https://v2.tauri.app/blog/) · [Announcing Tauri 2.12（2026-09-26）](https://v2.tauri.app/blog/tauri-2.12/) · [Tauri Releases API（2.12.5 / 3.0.0-alpha.6）](https://api.github.com/repos/tauri-apps/tauri/releases?per_page=10) · [Windows Installer](https://v2.tauri.app/distribute/windows-installer/) · [Windows 签名](https://v2.tauri.app/distribute/sign/windows/) · [Updater 插件](https://v2.tauri.app/plugin/updater/)
- [Electrobun 官网](https://electrobun.dev/electrobun/) · [2.0 发布博客（2026-08-21）](https://electrobun.dev/blog/electrobun-2-0/) · [vs. Electron & Tauri（Last reviewed October 2026，厂商自测）](https://electrobun.dev/electrobun/vs/) · [README](https://github.com/blackboardsh/electrobun) · [Releases API](https://api.github.com/repos/blackboardsh/electrobun/releases?per_page=30)
- [Wails README（v2/v3 均 Stable，Go + 原生渲染）](https://github.com/wailsapp/wails) · [Releases API](https://api.github.com/repos/wailsapp/wails/releases?per_page=30)
- [Neutralinojs Introduction（系统 WebView + 内嵌静态服务器）](https://neutralino.js.org/docs/) · [Releases API](https://api.github.com/repos/neutralinojs/neutralinojs/releases?per_page=30)
- [Flutter Desktop support（页面 dateModified 2026-07-31）](https://docs.flutter.dev/platform-integration/desktop)
- [Vite 8.0 is out!（2026-03-12，Rolldown / 6500 万周下载）](https://vite.dev/blog/announcing-vite8) · [npm vite / react / tailwindcss / typescript / vitest latest & time](https://registry.npmjs.org/vite)
- [Announcing TypeScript 7.0（2026-07-08，原生移植）](https://devblogs.microsoft.com/typescript/announcing-typescript-7-0/)
- [Devographics 调查站（State of JS 2026 开放中 / 2025 已关闭）](https://survey.devographics.com/) · [State of JS 2025 官方结论（"TypeScript has won" / "the day of Vite has come"）](https://survey.devographics.com/en-US/survey/state-of-js/2025/results/)
- [localfirst.fm（2026-03-31 Local-First Conf 2026 特辑）](https://www.localfirst.fm/) · [Local-First Conf（HIGHLIGHTS FROM 2026 / 2027 柏林）](https://localfirstconf.com/) · [Ink & Switch, Local-First Software（概念出处）](https://www.inkandswitch.com/local-first/)
- [GitHub repo API 星数（本次调研取数）](https://api.github.com/repos/electron/electron)：Electron ≈123,900 / Tauri ≈112,904 / Electrobun ≈13,055 / Wails ≈37,107 / Neutralino ≈8,693 / Flutter ≈181,263

**本地文件**
`desktop/package.json`、`desktop/main.js`、`desktop/prepare.mjs`、`desktop/fetch-python.mjs`、`rts-panel-cloud/web/package.json`、`pyproject.toml`、`requirements.lock`、`tests/test_desktop_shell.py`、`docs/desktop/IMPLEMENTATION.md`、`docs/desktop/HANDOFF.md:76`、`CONTEXT.md:114`、`scanner/panel_serialize.py:32`、`rts-panel-cloud/worker/src/routes/ingest.ts:42`、`docs/features/desktop-design-polish/BOUNDARIES.md`（§0/§3/§4/§5/§6/§7）、`DESIGN.md`（§0/§3/§4.1/§5/§7/§8/§9/§10）、`preview.html`（只读，未评价）。

---

## 10. 明确「未能核实 / 基于已知信息推测」汇总

| 项 | 状态 |
|---|---|
| Electron / Tauri / 各壳的**市占率、生产应用数、量化流行度对比** | **未能核实**（无可信 2026 第三方统计；GitHub stars 仅作社区热度代理） |
| 2026 年**独立第三方的桌面端趋势综述长文 1~2 篇** | **未能核实**（多组中英文 WebSearch 均返回无关结果；仅引用厂商自测对比页与 GitHub 对比表，且已标注其性质） |
| `Elanis/web-to-desktop-framework-comparison` 的表格内容与最后更新日期 | **未能核实**（WebFetch 只取到 GitHub 页面外壳） |
| Ink & Switch《Local-First Software》7 条理想的具体原文、发布日期 | **未能核实**（页面需 JS 渲染，WebFetch 未取到正文；仅作概念出处） |
| Wails 官方站对 Windows / WebView2 的具体说明 | **未能核实**（`wails.io` / `v3.wails.io` 均 403） |
| Neutralinojs v6 在 Windows 的 WebView2 具体行为与生产案例 | **未能核实** |
| Flutter 2026 最新稳定桌面版本号 | **未能核实**（GitHub Releases API 只到 2024 的 3.19.0-0.1.pre；Flutter 正式版走 SDK 渠道） |
| TypeScript 7 对 5.x 的完整兼容说明 | **未能核实**（只核实到「10x faster native port」与发布日期） |
| 「无状态库 / 组件原语」在 2026 的量化使用率 | **未能核实**（State of JS 2026 无结果、2025 结果页需登录）；本报告相关判断标注为**基于已知信息的推测** |
| Electrobun「4 KB 差分更新」「签名内建」、Tauri「small binaries」的具体 MB 数字 | **厂商自述 / 定性表述**，未在本仓或独立第三方复现；对比页的 125 MiB / 1.7 MiB 数字标注为**竞品自测** |
| Tauri v3.0.0-alpha.6 的 `prerelease` 标志 | GitHub API 返回 `false`，但 tag 名明确为 alpha → 本报告按 **alpha（非 stable）** 处理，并注明这一 API 口径异常 |
| §4 表的工期与风险量级 | **基于已知信息的推测**（未执行任何迁移、构建、测试） |
| §8 的「一致性」结论 | **静态复核 + 联网核对**，非实测绿灯；本次未跑任何 pytest / pnpm / node / Electron / 打包命令 |
| 冷快照是否必须随包分发 / 能否改为首启建空库 | **未评估**，仅标为比「换壳」性价比更高的可研究项（§3.1、§6 第 11 行） |

---

> **给发起人的收口**：本次调研的直接动作不是「换技术栈」，而是「按 `BOUNDARIES.md` §5 / `DESIGN.md` §9 的分期把已有规格落地，并单独排一次 44→45 / Vite 6→8 / TS 5.7→7 的版本维护」。技术选型这一题，**2026-11-15 的答案是：不用换**。
