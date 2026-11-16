/**
 * 打包前置检查（Issue D）：`electron-builder` 之前先确认**静默缺失**的三样东西。
 *
 * 为什么要有它：这些缺了以后打包**照样成功**，只在用户双击时暴露 ——
 *   1. `resources/python` 缺 → 子进程 ENOENT，窗口显示「本地看板没能启动」；
 *   2. `web/dist` 缺 → Python 静态路由 404，窗口里是一页 JSON 错误；
 *   3. 自带解释器**导入不了主入口**（依赖漏进 requirements）→ 窗口里一段
 *      ModuleNotFoundError 回显。实测踩过：`python-dotenv` 被 `unified_scanner.py`
 *      硬导入，却两个依赖文件都没写，一直是「本机碰巧装过」。
 * 打包脚本宁可在 CI/终端里红一次，也不要产出一个「能装上但打不开」的包。
 *
 * 用法：`node prepare.mjs`（`pnpm run dist` 已经先调它）。
 */

import { execFileSync } from "node:child_process";
import { existsSync, statSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, "..");
const PY_EXE = path.join(HERE, "resources", "python", process.platform === "win32" ? "python.exe" : "bin/python3");
const CHECKS = [
  {
    path: PY_EXE,
    hint: "先跑 `pnpm run fetch-python`（取 python-build-standalone + 装 requirements.lock）",
  },
  {
    path: path.join(REPO, "rts-panel-cloud", "web", "dist", "index.html"),
    hint: "先跑 `cd rts-panel-cloud/web && pnpm build`（Python 同源托管 dist）",
  },
];

let failed = false;
for (const check of CHECKS) {
  if (existsSync(check.path)) {
    console.log(`[prepare] ok    ${check.path}`);
  } else {
    failed = true;
    console.error(`[prepare] 缺失  ${check.path}\n            → ${check.hint}`);
  }
}

// 自带解释器 import 主入口：把「缺依赖」从用户双击时前移到打包时。
// ⚠ 必须与运行期同口径（禁用户 site），否则用户那份 site-packages 会让它**假绿**。
if (existsSync(PY_EXE)) {
  const env = { ...process.env, PYTHONNOUSERSITE: "1" };
  for (const key of ["PYTHONHOME", "PYTHONPATH", "PYTHONUSERBASE"]) delete env[key];
  try {
    execFileSync(PY_EXE, ["-c", "import unified_scanner"], { cwd: REPO, env, stdio: "pipe", timeout: 120_000 });
    console.log("[prepare] ok    自带解释器能 import 主入口（unified_scanner）");
  } catch (e) {
    failed = true;
    const detail = String(e.stderr || e.message)
      .trim()
      .split("\n")
      .slice(-6)
      .join("\n");
    console.error(`[prepare] 失败  自带解释器 import 不上主入口：\n${detail}`);
    console.error("            → 主入口**硬导入**的包是否漏进 requirements.lock？补上后重跑 `pnpm run fetch-python`");
  }
}

// 主进程 `main.js` 能否**真的加载**：把「装完双击就崩」前移到打包时。
//
// 为什么必须真 require 一次：2026-11-16 亡羊事故 —— `let TASKS_PORT = TASKS_PORT_DEFAULT;`
// 写在 `const TASKS_PORT_DEFAULT = 8900;` 之前，`const` 的 TDZ 让主进程一 require 就抛
// `ReferenceError: Cannot access 'TASKS_PORT_DEFAULT' before initialization`，
// 整包**启动即崩**（用户只看到 Electron 的错误对话框）。
// 而当时的 16 条守卫全是文本 grep、`node --check` 只做语法解析 —— **TDZ 是运行期错误**，
// 两者都查不出来，于是这个包一路绿灯发了出去。⇒ 打包前必须执行一遍。
//
// 离线 `require("electron")` 只返回一个路径字符串，`main.js` 尾部用
// `if (app && typeof app.getPath === "function")` 把生命周期整块包住 ⇒ 零副作用。
if (!failed) {
  try {
    execFileSync(process.execPath, ["-e", "require(process.argv[1])", path.join(HERE, "main.js")], {
      stdio: "pipe",
      timeout: 60_000,
    });
    console.log("[prepare] ok    主进程 main.js 能正常加载（无 TDZ/初始化顺序错误）");
  } catch (e) {
    failed = true;
    const detail = String(e.stderr || e.message)
      .trim()
      .split("\n")
      .slice(0, 8)
      .join("\n");
    console.error(`[prepare] 失败  主进程 main.js 加载即抛错：\n${detail}`);
    console.error("            → 这类错**装上也打不开**（启动即崩），必须先修好再打包");
  }
}

// 「漏 git 追踪的 Python 文件」检查（2026-11-16 补）。
//
// 起因：当天写了 `scanner/ai_digest/crosssection.py` + `crosssection_view.py`（横截面层
// 专属 AI 分析），**忘了 `git add`** ⇒ 它们是 untracked。
// 而 electron-builder 的 `extraResources` 从 **git 追踪的文件**取内容
// ⇒ 这两个文件**根本没进包**（实测 `diff -rq` 才发现：包内 `ai_digest/` 缺这两个）。
// 后果很阴：包能装、能跑，只有那一个功能静默失效（`ModuleNotFoundError` 在运行时才炸）。
//
// ⇒ 打包前把「仓库里存在、但 git 没追踪」的 .py 文件列出来 —— 有就**中止打包**，
// 逼开发者先 `git add`。（本仓纪律：发布内容必须进 git，否则 CI/回滚都对不上。）
if (!failed) {
  try {
    const out = execFileSync("git", ["ls-files", "--others", "--exclude-standard"], {
      cwd: REPO,
      encoding: "utf8",
      timeout: 30_000,
    });
    const strays = out
      .split(/\r?\n/)
      .map((s) => s.trim())
      .filter(Boolean)
      .filter((p) => p.endsWith(".py"))
      // `.alma-snapshots/` 是本地版本快照目录，不参与发布
      .filter((p) => !p.startsWith(".alma-snapshots/"));
    if (strays.length) {
      failed = true;
      console.error(
        `[prepare] 失败  以下 .py 文件存在但**未被 git 追踪**，不会进安装包：\n` +
          strays.map((p) => `            ${p}`).join("\n"),
      );
      console.error("            → electron-builder 从 git 追踪的文件取内容；先 `git add` 再打包");
    } else {
      console.log("[prepare] ok    无漏追踪的 .py 文件（untracked 检查通过）");
    }
  } catch (e) {
    // git 不可用（无 git 环境 / 非仓库目录）⇒ 不阻断打包，只提示
    console.warn(`[prepare] 提示  无法执行 git 未追踪检查（跳过）：${String(e.message).slice(0, 120)}`);
  }
}

const db = path.join(REPO, "scanner.db");
if (existsSync(db)) {
  console.log(`[prepare] ok    ${db}（${(statSync(db).size / 1024 / 1024).toFixed(0)} MB，本机活库）`);
  console.log("[prepare] 提示  安装包**不携带** scanner.db（A1）⇒ 安装包瘦到 ~63MB；");
  console.log("            打包态优先共用本机这份活库（RTS_DB_PATH / RTS_LIVE_DB / RTS_REPO_ROOT 命中即走它，");
  console.log("            绝不拷贝 —— WAL 是活文件，冷拷贝必不一致）；解析不到时由 Python 在");
  console.log("            userData 建空库，桌面版自己的扫描器会把它填起来。");
} else {
  console.warn("[prepare] 提示  仓库根没有 scanner.db：打包态无外部活库可用，首启将由 Python 建空库");
}

if (failed) {
  console.error("[prepare] 前置检查未通过，已中止打包。");
  process.exit(1);
}
console.log("[prepare] 前置检查通过。");
