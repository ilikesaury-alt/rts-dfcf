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

const db = path.join(REPO, "scanner.db");
if (existsSync(db)) {
  console.log(`[prepare] ok    ${db}（${(statSync(db).size / 1024 / 1024).toFixed(0)} MB，打包态首启会种进 userData）`);
} else {
  console.warn("[prepare] 提示  仓库根没有 scanner.db：打包产物首启将建空库（K 线/历史要重新积累）");
}

if (failed) {
  console.error("[prepare] 前置检查未通过，已中止打包。");
  process.exit(1);
}
console.log("[prepare] 前置检查通过。");
