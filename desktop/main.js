/**
 * 桌面版 Electron 主进程（Issue C 开发态 / Issue D 打包态，2026-10-19）。
 *
 * **本进程不做任何数据加工**：它只做三件事 ——
 *   1. 定位并拉起本机 Python 扫描器（`unified_scanner.py`，注入 `RTS_LOCAL_PANEL=1`）；
 *   2. 等 `127.0.0.1:<port>/api/meta` 可用后加载 `http://127.0.0.1:<port>/`；
 *   3. 关窗时把子进程收掉（锁由内核随进程退出释放）。
 *
 * 数据面（区块/K 线/过门）**全部**走 Python 的本地只读 HTTP：
 * 渲染进程与生产态终端看到的是**同一份** `serialize_view` 输出 ⇒ 零契约、零漂移
 * （design D1/D2/D5）。Node 侧**不读** `scanner.db`（D2：与主循环抢 SQLite 写锁）。
 *
 * 三条纪律（都有守卫，见 `tests/test_desktop_shell.py`）：
 * - 端口/主机写死回环：`127.0.0.1`（误绑 `0.0.0.0` = 同网段可读本地数据）；
 * - `env` 净化：删掉 `PYTHONHOME/PYTHONPATH/...`，避免指向用户系统 Python 串味
 *   （打包态的 standalone Python 被系统 env 一冲就废）；
 * - 打包态的**可写落点**只有 `userData`：`scanner.db` 与 `logs/` 都指到那里
 *   （`resources/` 只读；WAL 需要写权限）。**安装包已不带 `scanner.db`**（2026-11-15 A1），
 *   故无「种子拷贝」这一步：无外部活库时由 Python `init_db()` 在 userData 建空库。
 *
 * 调试用的两个 env（不改生产语义）：
 * - `RTS_PYTHON`：指定解释器，跳过 `findPython()` 的推导；
 * - `RTS_DESKTOP_SCANNER_ARGS`：追加扫描器参数（空白分隔）。本机的正当用法是
 *   `--no-lock --no-feishu` —— 手边已有一个终端扫描器时，把桌面壳**单独**拉起做联调，
 *   既不去杀那个实例（`stop_existing_scanners` 按脚本路径匹配，与锁路径无关），
 *   也不重复推飞书。
 */

"use strict";

const { app, BrowserWindow, shell, Menu, screen } = require("electron");
const { execFile } = require("node:child_process");
const fs = require("node:fs");
const http = require("node:http");
const net = require("node:net");
const path = require("node:path");

/** 与 `scanner.config.LOCAL_PANEL_PORT` 同值（改一处要同步另一处）。
 * 脏值（非整数/越界，如 `RTS_LOCAL_PANEL_PORT=abc` → NaN）回退 8899 —— 与 Python 侧
 * `_local_panel_enabled` 的回退同口径。不校验的话探活会打在 NaN 上永远失败，
 * 而子进程已被 Python 侧兜底拉到 8899，两端分叉、窗口死等无提示。 */
const _rawPort = Number(process.env.RTS_LOCAL_PANEL_PORT || 8899);
/** 看板端口。**运行时可变**：启动前探测，被占用则让位到空闲端口（见 `findFreePort`）。 */
let LOCAL_PORT =
  Number.isInteger(_rawPort) && _rawPort >= 1 && _rawPort <= 65535 ? _rawPort : 8899;
/** 动作面端口。**运行时可变**：同上，与看板端口错开，避免两个实例互相抢。 */
let TASKS_PORT = TASKS_PORT_DEFAULT;
const HOST = "127.0.0.1";
/** 动作面端口：默认 8900，与 `scanner.tasks_server.DEFAULT_PORT` 同值。 */
const TASKS_PORT_DEFAULT = 8900;
/** 等 Python 起服的上限：首轮扫描要联网，起服本身只 DU 到几百毫秒，给足 40s。 */
const SERVER_WAIT_MS = 40_000;
const POLL_MS = 500;

/** 窗口默认尺寸（`index.html` 的 `viewport width=1440` 与之软耦合）。 */
const DEFAULT_WIDTH = 1440;
const DEFAULT_HEIGHT = 900;
const MIN_WIDTH = 900;
const MIN_HEIGHT = 600;
/** 窗口记忆落点：打包态/开发态都在 `userData`（与 `scanner.db`/`logs/` 同命运，
 * 升级不覆盖）。只有 `close` 时写一次，不在每次 resize/move 落盘。 */
const WINDOW_STATE_FILE = "window-state.json";
/** 产品图标（`build.win.icon` 与 `build.files` 同款，打包态经 asar 取 `__dirname`）。 */
const APP_ICON = path.join(__dirname, "icon.ico");

/** 子进程 env 里必须删掉的变量（指向用户系统 Python，会污染 standalone 解释器）。 */
const SCRUBBED_ENV = [
  "PYTHONHOME",
  "PYTHONPATH",
  "PYTHONUSERBASE",
  "PYTHONSTARTUP",
  "VIRTUAL_ENV",
  "CONDA_PREFIX",
  "PYTHONEXECUTABLE",
];
let child = null;
let win = null;
let childLog = null;
/** 所有会影响窗口位置的事件（move/resize/maximize/close…）都收敛到这里，最后一次读覆盖。 */
let windowStateListenerBound = false;

// ── 路径解析（开发态 / 打包态两条）────────────────────────────────────────────

/** 仓库根：开发态 = `desktop/` 的上一级；打包态 = `resources/repo`（extraResources）。 */
function repoRoot() {
  return app.isPackaged ? path.join(process.resourcesPath, "repo") : path.join(__dirname, "..");
}

// ── 窗口尺寸/位置记忆 ──────────────────────────────────────────────────────────
//
// 只认「可见屏幕」：拔掉外接显示器后，老坐标可能落在已不存在的显示器上，
// 此时必须回退默认，否则窗口会开在屏幕外看不见。判据用**所有**显示器的 `workArea`
// （`screen.getAllDisplays()`，可见区，去掉任务栏），把持久化矩形逐一比对：任意一块
// 屏幕能「可见地」容纳即恢复（副屏仍插着时记住副屏位置），一块都不命中 = 老屏幕已被
// 拔掉 → 回退默认。比只看主屏更贴合「窗口是否还在可见屏幕内」的口径。
//
// 校验分两层：
//   - 纯函数层（`validateWindowState`）：不依赖 Electron，可离线单测脏值/越界/极小值；
//     `display` 可传单块显示器对象或其数组（离线单测给纯数据对象即可）。
//   - 环境层（`loadWindowState`）：拿真实显示器几何再过一遍可见性。

/** 干净整数？脏值（NaN/Infinity/浮点/字符串）一律拒（不含符号判断）。 */
function isCleanInt(v) {
  return typeof v === "number" && Number.isInteger(v);
}

/**
 * 校验一份持久化的窗口状态；不合法一律回退默认 1440×900（x/y 省略、非最大化）。
 * `display` 可传单块显示器 `{ workArea: { x, y, width, height } }` 或**显示器数组**
 * （纯数据对象即可，无需真 Electron）；多屏时任意一块能容纳即恢复。
 * 规则：
 *   - 持久化的 x/y 必须是整数且 >= 0（简化：多显示器负坐标一律回退，避免半屏在外）；
 *     脏 x/y 与非整数、越界 w/h 一样**整份回退默认**（不半信半疑地只保留尺寸）；
 *   - width/height 必须是整数且不小于 `MIN_WIDTH/MIN_HEIGHT`（也挡 <=0、非整数、字符串）；
 *   - 窗口左上 + 尺寸必须能「可见地」落进某块工作区：右上角至少留 100px、下边至少留 100px
 *     （超过可见区 → 拔显示器/改分辨率的典型症状，回退默认）；
 *   - `maximized` 只接受严格布尔，其余当成未最大化。
 * 返回规范化后的 `{ x, y, width, height, maximized }`（x/y 可能省略）。
 */
function validateWindowState(raw, display) {
  const fallback = () => ({
    width: DEFAULT_WIDTH,
    height: DEFAULT_HEIGHT,
    maximized: false,
  });
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return fallback();
  // 统一成数组：离线单测可只给一块，环境层给全部显示器
  const displays = Array.isArray(display) ? display : [display];
  // 只保留几何干净的显示器（workArea 的 x/y 可为负 —— 副屏在左/上，是 Electron 给的
  // 可信几何，不拒；宽高必须为正整数）。一块干净几何都没有 → 回退默认。
  const areas = displays
    .map((d) => d && d.workArea)
    .filter((wa) =>
      wa &&
      isCleanInt(wa.x) && isCleanInt(wa.y) &&
      isCleanInt(wa.width) && isCleanInt(wa.height) &&
      wa.width > 0 && wa.height > 0,
    );
  if (areas.length === 0) return fallback();

  const w = raw.width;
  const h = raw.height;
  if (!Number.isInteger(w) || !Number.isInteger(h)) return fallback();
  if (w < MIN_WIDTH || h < MIN_HEIGHT) return fallback();
  // 尺寸至少能塞进某一块屏幕的工作区（否则是全屏都放不下的脏值）
  if (!areas.some((wa) => w <= wa.width && h <= wa.height)) return fallback();

  const maximized = raw.maximized === true; // 只认严格布尔
  // 最大化状态交给系统恢复，不强求 x/y；但要确保尺寸仍合法
  if (maximized) return { width: w, height: h, maximized: true };

  const x = raw.x;
  const y = raw.y;
  // x/y 为负（副屏在左侧/上方）、非整数或缺省 → 整份回退默认（与任务口径一致：
  // 脏坐标不值得信任，连尺寸一并丢弃；否则会留下「默认窗口大小却没位置」的半吊子状态）
  if (!Number.isInteger(x) || !Number.isInteger(y) || x < 0 || y < 0) {
    return fallback();
  }
  const VISIBLE_MARGIN = 100;
  const visibleOn = areas.some((wa) =>
    x >= wa.x &&
    y >= wa.y &&
    x + VISIBLE_MARGIN <= wa.x + wa.width &&
    y + VISIBLE_MARGIN <= wa.y + wa.height &&
    x + w <= wa.x + wa.width + VISIBLE_MARGIN &&
    y + h <= wa.y + wa.height + VISIBLE_MARGIN,
  );
  // 一块屏幕都容不下（拔掉外接显示器、改分辨率）→ 回退默认
  if (!visibleOn) return fallback();
  return { x, y, width: w, height: h, maximized: false };
}

/** 读持久化状态：文件缺失/坏 JSON/字段脏/窗口已不在可见屏幕 → 一律默认。
 * 用 `screen.getAllDisplays()`（全部显示器）判可见性：副屏仍插着时记住副屏位置，
 * 全部屏幕都容纳不下（外接显示器被拔掉）才回退默认。 */
function loadWindowState() {
  try {
    const file = path.join(app.getPath("userData"), WINDOW_STATE_FILE);
    const raw = JSON.parse(fs.readFileSync(file, "utf-8"));
    const displays = screen.getAllDisplays();
    return validateWindowState(raw, displays);
  } catch {
    // 缺文件 / 坏 JSON / screen 未就绪：都走默认，绝不因记忆文件把启动带崩（fail-open）
    return validateWindowState(null, null);
  }
}

/** 从当前窗口读回状态（最大化时保留 `restoreBounds`，否则会用掉最大化后的整屏尺寸）。 */
function captureWindowState(w) {
  if (!w || (typeof w.isDestroyed === "function" && w.isDestroyed())) return null;
  const maximized = w.isMaximized();
  const b = maximized ? w.getNormalBounds() : w.getBounds();
  return {
    x: b.x,
    y: b.y,
    width: b.width,
    height: b.height,
    maximized,
  };
}

/** 只在关闭路径写一次磁盘（见 `close` / `before-quit`），不逐次 resize 落盘。
 * `target` 默认为当前窗口；显式传窗口对象便于离线单测（不依赖模块内 `win`）。 */
function saveWindowState(target = win) {
  const state = captureWindowState(target);
  if (!state) return;
  try {
    const dir = app.getPath("userData");
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(path.join(dir, WINDOW_STATE_FILE), JSON.stringify(state));
  } catch (e) {
    console.error("[desktop] 窗口状态写入失败:", e.message);
  }
}

/** Python 解释器：`RTS_PYTHON` 覆盖 > 打包态 standalone > 开发态 PATH 上的 python。 */
function findPython() {
  if (process.env.RTS_PYTHON) return process.env.RTS_PYTHON;
  if (app.isPackaged) {
    return path.join(process.resourcesPath, "python", process.platform === "win32" ? "python.exe" : "python3");
  }
  return process.platform === "win32" ? "python" : "python3";
}

/**
 * 本机「有最新数据的」活库路径（Lichun 2026-11-15 口径）。
 *
 * 开发态用不到：`preparePackagedState()` 直接返回 null，Python 侧
 * `resolve_db_path()` 默认就落在仓库根 `scanner.db` —— 与终端同一份活库。
 * 打包态**安装包已不带 `scanner.db`**（2026-11-15 A1 决策：瘦身 + 消灭冷快照病根），
 * 故不存在「把包内旧库种进 userData」这条路。打包态优先解析外部活库；
 * 解析不到就在 `userData/scanner.db` **建空库**，由桌面版自己的扫描器填起来。
 * 降级要在日志里可见，不能静默。
 */
function resolveLiveDb() {
  // 1) 既有覆盖口 RTS_DB_PATH（终端/用户启动时已给出）
  // 2) 新增显式指针 RTS_LIVE_DB
  // 3) RTS_REPO_ROOT（安装器/快捷方式知道仓库位置时）
  const candidates = [
    process.env.RTS_DB_PATH,
    process.env.RTS_LIVE_DB,
    process.env.RTS_REPO_ROOT ? path.join(process.env.RTS_REPO_ROOT, "scanner.db") : null,
  ];
  for (const p of candidates) {
    try {
      if (p && fs.statSync(p).isFile()) return p;
    } catch {
      // 路径不存在 / 无权限：跳过，继续下一个候选
    }
  }
  return null;
}

/**
 * 打包态落点准备（开发态直接返回 null：用仓库根的原生布局）。
 *
 * `scanner.db` 优先**共用本机最新活库**，绝不拷贝（WAL 是活文件，冷拷贝必不一致），
 * 也不在 userData 另起一份 ⇒ 桌面版与主循环读同一份库。
 * `logs/` 与 `.env` 仍在 userData：运行时状态不写进可能只读的 `resources/`。
 *
 * ⚠ 降级（2026-11-15 A1）：安装包**不再携带** `scanner.db`（227MB ⇒ 安装包瘦到 ~63MB，
 * 也消灭「包内冷快照导致首启数据陈旧」这个病根）。故解析不到外部活库时：
 * 直接让 Python 在 `userData/scanner.db` **建空库**（`init_db()` 幂等建表 + 跑迁移），
 * 桌面版自己的扫描器会把它填起来。不再有「包内快照可种」这条路径。
 * 设 RTS_LIVE_DB / RTS_DB_PATH / RTS_REPO_ROOT 即走正路（读本机最新活库）。
 */
function preparePackagedState() {
  if (!app.isPackaged) return null;
  const userData = app.getPath("userData");
  const live = resolveLiveDb();
  const dbPath = live || path.join(userData, "scanner.db");
  const logDir = path.join(userData, "logs");
  fs.mkdirSync(logDir, { recursive: true });
  // ⚠ 不存在**不报错**：Python 侧 `init_db()` 会建表 + 跑迁移，空库照样能用。
  // （包内已无 scanner.db，这里刻意不再拷贝任何东西。）
  return { dbPath, liveDb: Boolean(live), logDir, envFile: path.join(userData, ".env") };
}

// ── 子进程 ────────────────────────────────────────────────────────────────────

/** 追加给扫描器的参数（**仅调试**，见文件头 `RTS_DESKTOP_SCANNER_ARGS` 说明）。 */
function extraArgs() {
  const raw = (process.env.RTS_DESKTOP_SCANNER_ARGS || "").trim();
  return raw ? raw.split(/\s+/) : [];
}

function buildChildEnv(state) {
  const env = { ...process.env };
  for (const key of SCRUBBED_ENV) delete env[key];
  env.RTS_LOCAL_PANEL = "1"; // ⛔ 不带 --no-panel：那只关云端上报，本看板靠这个开关
  env.RTS_LOCAL_PANEL_PORT = String(LOCAL_PORT);
  // 动作面端口：与看板端口错开，避免与终端实例抢同一个 8900（双绑 ⇒ 请求随机落到别人那）。
  env.RTS_TASKS_PORT = String(TASKS_PORT);
  env.PYTHONUNBUFFERED = "1";
  env.PYTHONIOENCODING = "utf-8";
  // ⚠ 实测（2026-10-19）：删掉 PYTHONPATH 还不够 —— CPython 的 `site` 模块**默认**会把
  // `%APPDATA%\Python\Python312\site-packages`（用户 site，同 minor 版本即命中）插进
  // `sys.path`，且**排在自带 site-packages 之前** ⇒ 打包态可能 import 到用户那份依赖
  // （版本不同即静默漂移），也正是 `fetch-python.mjs` 里 pip 报 sqlfluff 依赖冲突的原因。
  env.PYTHONNOUSERSITE = "1";
  // 桌面壳是「本机任务控制面」的**唯一显式开启者**：终端直接跑 unified_scanner.py
  // 不带这个 env ⇒ local_panel_server 不注册 /api/tasks/*（缺省关，与 RTS_LOCAL_PANEL
  // 同纪律）。放顶层而非 `if (state)`：开发态桌面壳同样要有 AI 分析入口。
  env.RTS_TASKS = "1";
  if (state) {
    // 用户显式设过就尊重用户（`RTS_DB_PATH` / `RTS_LOG_DIR` 是既有的覆盖入口）
    env.RTS_DB_PATH = env.RTS_DB_PATH || state.dbPath;
    env.RTS_LOG_DIR = env.RTS_LOG_DIR || state.logDir;
    // `.env` **不进包**（密钥不躺安装目录、升级不覆盖）⇒ 指向 userData 下那份。
    // 用户把仓库根的 `.env` 拷过去一次即可；没有该文件时 python-dotenv 静默跳过。
    env.RTS_ENV_FILE = env.RTS_ENV_FILE || state.envFile;
  }
  return env;
}

function startScanner() {
  const python = findPython();
  const cwd = repoRoot();
  const script = path.join(cwd, "unified_scanner.py");
  if (!fs.existsSync(script)) {
    return { error: `找不到扫描器脚本：${script}` };
  }
  const state = preparePackagedState();
  // 子进程日志是**运行时产物**，与 `logs/` 同命运：打包态去 userData，
  // 开发态默认仓库 `logs/`，但显式设了 `RTS_LOG_DIR` 就跟着走
  // （否则联调时子进程日志会落在仓库里，而它读写的库在别处 —— 排查时对不上）。
  const logDir = state ? state.logDir : process.env.RTS_LOG_DIR || path.join(cwd, "logs");
  fs.mkdirSync(logDir, { recursive: true });
  const logPath = path.join(logDir, "desktop_child.log");
  childLog = fs.createWriteStream(logPath, { flags: "a" });
  // 写日志失败不该把主进程带走（磁盘满/权限）：与扫描器同纪律，fail-open
  childLog.on("error", (e) => console.error("[desktop] 子进程日志写入失败:", e.message));

  // `execFile`（不是 `spawn`）：绝对路径可执行文件 + 参数数组，不经 shell
  // （路径/引号注入问题与 ENOENT 都归零）。
  const proc = execFile(
    python,
    [script, "60", ...extraArgs()],
    { cwd, env: buildChildEnv(state), windowsHide: true, maxBuffer: 8 * 1024 * 1024 },
    (err) => {
      // 子进程退出：关窗时是我们主动 kill（正常路径，不弹框）；否则是异常退出
      if (err && !app.isQuiting) {
        console.error("[desktop] 扫描器退出:", err.message);
      }
      child = null;
    },
  );
  proc.stdout.on("data", (buf) => {
    childLog.write(buf);
    if (!app.isPackaged) process.stdout.write(buf);
  });
  proc.stderr.on("data", (buf) => {
    childLog.write(buf);
    if (!app.isPackaged) process.stderr.write(buf);
  });
  child = proc;
  return { proc, python, cwd, logPath };
}

/** 主动结束子进程。Windows 上 `kill()` = TerminateProcess（无优雅关停），
 * 但**单实例锁是内核级文件锁**，进程一死即释放（`scanner/single_instance.py`），
 * 故不影响「关窗后能再起」。 */
function stopScanner() {
  if (!child) return;
  const proc = child;
  child = null;
  try {
    proc.kill();
  } catch (e) {
    console.error("[desktop] kill 子进程失败:", e.message);
  }
}

// ── 探活 ──────────────────────────────────────────────────────────────────────

/**
 * 端口是否已被占用。
 *
 * ⚠ 实现要点（2026-11-15 实测踩坑）：**不能用 `net.createServer().listen()` 探测**。
 * Windows 上默认 `allow_reuse_address`，探测 socket 能绑上**已被别人占用**的端口，
 * 于是 `findFreePort(8900)` 在 8900 被占时仍返回 8900 ⇒ 分配出两个相同端口。
 * ⇒ 改用**主动 connect**：连得上 ⇒ 有人在监听 ⇒ 被占；连不上（ECONNREFUSED）⇒ 空闲。
 */
function isPortFree(port) {
  return new Promise((resolve) => {
    const sock = net.connect({ host: HOST, port });
    const done = (free) => {
      sock.removeAllListeners();
      sock.destroy();
      resolve(free);
    };
    sock.setTimeout(1200);
    sock.once("connect", () => done(false)); // 连上了 ⇒ 被占
    sock.once("timeout", () => done(true)); // 连不上且超时 ⇒ 空闲
    sock.once("error", () => done(true)); // ECONNREFUSED ⇒ 空闲
  });
}

/** 从 `start` 起找第一个空闲端口（最多试 20 个连续端口）。 */
async function findFreePort(start, attempts = 20, exclude = null) {
  for (let i = 0; i < attempts; i += 1) {
    const port = start + i;
    if (port > 65535) break;
    if (port === exclude) continue; // 已被看板端口占用 ⇒ 跳过
    // eslint-disable-next-line no-await-in-loop
    if (await isPortFree(port)) return port;
  }
  return null;
}

/**
 * 分配看板 + 动作面两个端口。
 *
 * 规则：**各自**找空闲端口，且两者**互不相同**（避免自己跟自己撞）。
 * 若首选端口空闲就用首选（与旧行为逐字节一致）；被占才让位。
 * 返回 null = 连续 20 个端口全被占 ⇒ 无法启动。
 */
async function allocatePorts() {
  const panel = await findFreePort(LOCAL_PORT);
  if (panel === null) return null;
  LOCAL_PORT = panel;
  // 动作面从默认端口找；**必须排除看板端口**（两者会同时起服，抢同一个端口 = 双绑）。
  let tasks = await findFreePort(TASKS_PORT_DEFAULT, 20, LOCAL_PORT);
  if (tasks === null) {
    // 默认段用尽 ⇒ 从看板端口之后另起一段
    tasks = await findFreePort(LOCAL_PORT + 1, 20, LOCAL_PORT);
  }
  if (tasks === null || tasks === LOCAL_PORT) return null;
  TASKS_PORT = tasks;
  return { panel: LOCAL_PORT, tasks: TASKS_PORT };
}

function probeMeta(port) {
  return new Promise((resolve) => {
    const req = http.get({ host: HOST, port, path: "/api/meta", timeout: 1500 }, (res) => {
      let body = "";
      res.on("data", (chunk) => (body += chunk));
      res.on("end", () => {
        try {
          const parsed = JSON.parse(body);
          // 只认**我们自己**的信封：端口被别的程序占着时不能把它的页面当看板加载
          resolve(res.statusCode === 200 && parsed && parsed.ok === true && "ready" in (parsed.data || {}));
        } catch {
          resolve(false);
        }
      });
    });
    req.on("timeout", () => req.destroy());
    req.on("error", () => resolve(false));
  });
}

/**
 * 探活；`aborted` 让调用方能在**子进程已死**时立刻收手（否则白等 40s 才弹错）。
 */
async function waitForServer(port, aborted) {
  const deadline = Date.now() + SERVER_WAIT_MS;
  while (Date.now() < deadline) {
    if (await probeMeta(port)) return true;
    if (aborted && aborted()) return false;
    await new Promise((r) => setTimeout(r, POLL_MS));
  }
  return false;
}

// ── 窗口 ──────────────────────────────────────────────────────────────────────

function escapeHtml(text) {
  return String(text).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" })[c]);
}

/** 子进程日志末尾若干字符：把「为什么起不来」直接摆在窗口里，省得让用户去找文件。 */
function readLogTail(logPath, max = 1400) {
  try {
    const text = fs.readFileSync(logPath, "utf-8");
    return text.length > max ? "…" + text.slice(-max) : text;
  } catch {
    return "";
  }
}

function errorPage(message, logPath) {
  const tail = logPath ? readLogTail(logPath).trim() : "";
  const html = `<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<style>
 body{margin:0;background:#070b12;color:#e6e8ee;font:13px/1.7 "Microsoft YaHei",system-ui,sans-serif;
      display:flex;align-items:center;justify-content:center;height:100vh}
 .box{max-width:720px;padding:20px 24px}
 h1{font-size:15px;margin:0 0 8px}
 code{background:#171a21;padding:2px 6px;border-radius:4px}
 pre{background:#171a21;padding:10px 12px;border-radius:6px;max-height:260px;overflow:auto;
     font:12px/1.5 Consolas,monospace;color:#c9ccd4;white-space:pre-wrap}
 p{color:#8a8f99;margin:8px 0}
</style>
<div class="box">
  <h1>本地看板没能启动</h1>
  <p>${message}</p>
  ${tail ? `<pre>${escapeHtml(tail)}</pre>` : ""}
  <p>扫描器本身可能仍在运行（本看板是旁路，失败不阻断扫描）。排查：端口 <code>${LOCAL_PORT}</code>
     是否被占用（<code>RTS_LOCAL_PANEL_PORT</code> 可换端口）；子进程日志
     <code>${logPath ? escapeHtml(logPath) : "logs/desktop_child.log"}</code>。</p>
</div></html>`;
  return "data:text/html;charset=utf-8," + encodeURIComponent(html);
}

async function createWindow() {
  const state = loadWindowState();
  win = new BrowserWindow({
    width: state.width,
    height: state.height,
    ...(state.x === undefined ? {} : { x: state.x, y: state.y }),
    minWidth: MIN_WIDTH,
    minHeight: MIN_HEIGHT,
    backgroundColor: "#070b12",
    title: "创业板飙升榜 · 桌面看板",
    icon: fs.existsSync(APP_ICON) ? APP_ICON : undefined,
    autoHideMenuBar: false,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  // 最大化状态由系统恢复（必须在 show 之后；显式 maximize 会让「取消最大化」回到记忆的尺寸）
  if (state.maximized) win.maximize();
  win.on("closed", () => {
    win = null;
  });
  // 关窗前落一次盘：所有位置/尺寸变化都在这一刻收敛，避免每次 resize/move 写盘。
  // 只绑一次（createWindow 理论上只跑一次，这里防御性兜底）。
  if (!windowStateListenerBound) {
    windowStateListenerBound = true;
    win.on("close", () => saveWindowState(win));
  }
  // 外链一律走系统浏览器：看板是只读本地页，不需要在应用内开任何外部站点
  win.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: "deny" };
  });

  const ports = await allocatePorts();
  if (!ports) {
    await win.loadURL(
      errorPage(
        `端口 ${LOCAL_PORT}–${LOCAL_PORT + 19} 与 ${TASKS_PORT}–${TASKS_PORT + 19} 都被占用，桌面版无法启动。`,
        null,
      ),
    );
    return;
  }

  const started = startScanner();
  if (started.error) {
    await win.loadURL(errorPage(started.error, started.logPath));
    return;
  }
  const ok = await waitForServer(LOCAL_PORT, () => child === null);
  if (win === null) return; // 等待期间用户已关窗
  if (ok) {
    await win.loadURL(`http://${HOST}:${LOCAL_PORT}/`);
  } else if (child === null) {
    await win.loadURL(errorPage("扫描器子进程**已退出**（没能起服）。", started.logPath));
  } else {
    await win.loadURL(
      errorPage(
        `等待 <code>${HOST}:${LOCAL_PORT}</code> 超时（${SERVER_WAIT_MS / 1000}s）。`,
        started.logPath,
      ),
    );
  }
}

// ── 生命周期 ──────────────────────────────────────────────────────────────────

// 纯函数导出（放在入口分支之外）：让窗口状态的「脏值/越界回退」与「关闭时写盘」能被
// 离线 Node 脚本验证，不必真起 Electron 窗口（那会拉起扫描器子进程、碰 scanner.db）。
module.exports = {
  validateWindowState,
  loadWindowState,
  captureWindowState,
  saveWindowState,
  WINDOW_STATE_FILE,
  DEFAULT_WIDTH,
  DEFAULT_HEIGHT,
  MIN_WIDTH,
  MIN_HEIGHT,
};

// 判据用「Electron 主进程 API 是否可用」，**不是** `require.main === module`：
// Electron 的 default_app 用 `await import(url)`（ESM 动态导入 CJS 入口）加载本文件，
// 那条路径下 `require.main` 是 `undefined`，`require.main === module` 恒为 false ⇒
// 生命周期整块不执行（窗口不开、子进程不拉、关窗不 kill）——空壳应用。
// 真实主进程里单实例 API 必存在；离线 `require('./main.js')` 时
// `require('electron')` 只给一个路径字符串、app 为 undefined ⇒ 纯函数可用、零副作用。
if (app && typeof app.getPath === "function") {
  // 应用自身的单实例：第二个实例只把已有窗口顶到前台，**不再拉起第二个扫描器**
  // （扫描器侧另有自己的锁，但那边会「杀掉旧实例」，两个锁语义不同，别混）。
  const gotLock = app.requestSingleInstanceLock();
  if (!gotLock) {
    app.quit();
  } else {
    app.on("second-instance", () => {
      if (win) {
        if (win.isMinimized()) win.restore();
        win.focus();
      }
    });

    app.whenReady().then(() => {
      // 去掉 Electron 默认的开发/调试菜单（File/Edit/View/Window/Help）。本期不引入
      // 任何自定义菜单；与 autoHideMenuBar:false 不冲突（没有菜单可显示/隐藏）。
      Menu.setApplicationMenu(null);
      return createWindow();
    });

    app.on("window-all-closed", () => {
      // 关窗 = 退出应用（Windows 单窗口语义）⇒ 一并收掉 Python，不留后台扫描器
      app.quit();
    });
  }

  app.on("before-quit", () => {
    app.isQuiting = true;
    // 兜底：非常规退出（没走窗口 close）也留一份最后的窗口状态。
    // 正常关窗时 close 已写过、且此时 win 多为 null，这里自然 no-op，不重复落盘。
    saveWindowState();
    stopScanner();
  });

  process.on("exit", stopScanner);
}
