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
 *   （`resources/` 只读；WAL 需要写权限）。种子拷贝**只在首次**做，绝不覆盖既有库。
 *
 * 调试用的两个 env（不改生产语义）：
 * - `RTS_PYTHON`：指定解释器，跳过 `findPython()` 的推导；
 * - `RTS_DESKTOP_SCANNER_ARGS`：追加扫描器参数（空白分隔）。本机的正当用法是
 *   `--no-lock --no-feishu` —— 手边已有一个终端扫描器时，把桌面壳**单独**拉起做联调，
 *   既不去杀那个实例（`stop_existing_scanners` 按脚本路径匹配，与锁路径无关），
 *   也不重复推飞书。
 */

"use strict";

const { app, BrowserWindow, shell } = require("electron");
const { execFile } = require("node:child_process");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");

/** 与 `scanner.config.LOCAL_PANEL_PORT` 同值（改一处要同步另一处）。 */
const LOCAL_PORT = Number(process.env.RTS_LOCAL_PANEL_PORT || 8899);
const HOST = "127.0.0.1";
/** 等 Python 起服的上限：首轮扫描要联网，起服本身只 DU 到几百毫秒，给足 40s。 */
const SERVER_WAIT_MS = 40_000;
const POLL_MS = 500;

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

// ── 路径解析（开发态 / 打包态两条）────────────────────────────────────────────

/** 仓库根：开发态 = `desktop/` 的上一级；打包态 = `resources/repo`（extraResources）。 */
function repoRoot() {
  return app.isPackaged ? path.join(process.resourcesPath, "repo") : path.join(__dirname, "..");
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
 * 打包态的**可写落点**准备（开发态直接返回 null：用仓库根的原生布局）。
 *
 * `scanner.db` 与 `logs/` 都必须离开 `resources/`：
 * - WAL 需要写权限，安装态 `resources/` 可能只读；
 * - 库与日志是**运行时状态**，不属于程序本体（升级不覆盖）。
 *
 * ⚠ 种子拷贝只在 `userData/scanner.db` **不存在**时做一次：
 * 覆盖会把用户跑到一半的库抹掉。种子本身是打包时的冷快照（不含 `-wal`）。
 */
function preparePackagedState() {
  if (!app.isPackaged) return null;
  const userData = app.getPath("userData");
  const dbPath = path.join(userData, "scanner.db");
  const logDir = path.join(userData, "logs");
  fs.mkdirSync(logDir, { recursive: true });
  if (!fs.existsSync(dbPath)) {
    const bundled = path.join(repoRoot(), "scanner.db");
    if (fs.existsSync(bundled)) {
      fs.copyFileSync(bundled, dbPath);
    }
  }
  return { dbPath, logDir, envFile: path.join(userData, ".env") };
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
  env.PYTHONUNBUFFERED = "1";
  env.PYTHONIOENCODING = "utf-8";
  // ⚠ 实测（2026-10-19）：删掉 PYTHONPATH 还不够 —— CPython 的 `site` 模块**默认**会把
  // `%APPDATA%\Python\Python312\site-packages`（用户 site，同 minor 版本即命中）插进
  // `sys.path`，且**排在自带 site-packages 之前** ⇒ 打包态可能 import 到用户那份依赖
  // （版本不同即静默漂移），也正是 `fetch-python.mjs` 里 pip 报 sqlfluff 依赖冲突的原因。
  env.PYTHONNOUSERSITE = "1";
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
 body{margin:0;background:#0f1115;color:#e6e8ee;font:13px/1.7 "Microsoft YaHei",system-ui,sans-serif;
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
  win = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 900,
    minHeight: 600,
    backgroundColor: "#0f1115",
    title: "创业板飙升榜 · 桌面看板",
    autoHideMenuBar: false,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  win.on("closed", () => {
    win = null;
  });
  // 外链一律走系统浏览器：看板是只读本地页，不需要在应用内开任何外部站点
  win.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: "deny" };
  });

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

  app.whenReady().then(createWindow);

  app.on("window-all-closed", () => {
    // 关窗 = 退出应用（Windows 单窗口语义）⇒ 一并收掉 Python，不留后台扫描器
    app.quit();
  });
}

app.on("before-quit", () => {
  app.isQuiting = true;
  stopScanner();
});

process.on("exit", stopScanner);
