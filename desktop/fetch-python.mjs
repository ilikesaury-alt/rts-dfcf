/**
 * 取 `python-build-standalone` 的 Windows 3.12 运行时 → `desktop/resources/python/`，
 * 并在其中安装 `requirements.lock`（Issue D，2026-10-19）。
 *
 * 为什么必须自带解释器（design §7.1，Datasette Desktop 范式）：
 * 打包后 `spawn('python')` 走系统 PATH，用户机器上未必有 Python，或有也是别的版本
 * （本项目要 3.12）⇒ 必然 ENOENT 或版本错配。把解释器放进 `resources/python`
 * 并让 `main.js` 的 `findPython()` 在打包态走 `process.resourcesPath` 才是可复现的。
 *
 * ⚠ 与 `main.js` 的 `SCRUBBED_ENV` 是一对：本解释器一旦被 `PYTHONHOME/PYTHONPATH`
 * 指到用户系统 Python 就会「串味」（import 到别的 site-packages）。
 *
 * 依赖装的是 `requirements.lock`（精确版），不是 `requirements.txt`：后者的
 * akshare 无上界，兜底数据源的返回结构会随 minor 静默改变（见 lock 文件抬头）。
 *
 * 用法：`pnpm run fetch-python`（幂等：已有 python.exe 且未加 `--force` 就跳过）。
 */

import { execFileSync } from "node:child_process";
import { createWriteStream, existsSync, mkdirSync, rmSync, statSync } from "node:fs";
import { Readable } from "node:stream";
import { pipeline } from "node:stream/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, "..");
const RESOURCES = path.join(HERE, "resources");
const PY_DIR = path.join(RESOURCES, "python");
const PY_EXE = path.join(PY_DIR, process.platform === "win32" ? "python.exe" : "bin/python3");
const RELEASE_API = "https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest";
const PY_MINOR = "3.12";
const FORCE = process.argv.includes("--force");

/**
 * 子进程 env：与 `main.js` 的净化同一套（这里跑的是同一个自带解释器）。
 *
 * ⚠ 必须净化，否则内置解释器的 `site` 会把**用户 site-packages** 插进 `sys.path`
 * （同 3.12 minor 即命中 `%APPDATA%\Python\Python312\site-packages`，且排在前面）——
 * 后果是 pip 认为某些依赖「已满足」而不装进 `resources/python`，于是**换台机器就缺依赖**；
 * 实测症状是 pip 打印一堆 `sqlfluff ... is not installed` 的无关冲突。
 */
function cleanEnv() {
  const env = { ...process.env };
  for (const key of ["PYTHONHOME", "PYTHONPATH", "PYTHONUSERBASE", "PYTHONSTARTUP"]) delete env[key];
  env.PYTHONNOUSERSITE = "1";
  return env;
}

function log(...args) {
  console.log("[fetch-python]", ...args);
}

function pickAsset(release) {
  const pattern = new RegExp(`^cpython-${PY_MINOR}\\.\\d+\\+\\d+-x86_64-pc-windows-msvc-install_only\\.tar\\.gz$`);
  const asset = (release.assets || []).find((a) => pattern.test(a.name));
  if (!asset) throw new Error(`未找到 ${PY_MINOR} 的 windows install_only 资产（release ${release.tag_name}）`);
  return asset;
}

async function download(url, target) {
  const res = await fetch(url, { headers: { "User-Agent": "rts-desktop-fetch-python" }, redirect: "follow" });
  if (!res.ok) throw new Error(`下载失败 HTTP ${res.status}: ${url}`);
  await pipeline(Readable.fromWeb(res.body), createWriteStream(target));
}

async function main() {
  if (existsSync(PY_EXE) && !FORCE) {
    log(`已存在 ${PY_EXE}（要重装加 --force）`);
  } else {
    mkdirSync(RESOURCES, { recursive: true });
    log("查询最新 python-build-standalone release …");
    const release = await (await fetch(RELEASE_API, { headers: { "User-Agent": "rts-desktop" } })).json();
    const asset = pickAsset(release);
    const archive = path.join(RESOURCES, asset.name);
    log(`下载 ${asset.name}（${(asset.size / 1024 / 1024).toFixed(1)} MB，release ${release.tag_name}）…`);
    await download(asset.browser_download_url, archive);
    log("解压（tar 归档根目录即 python/）…");
    if (existsSync(PY_DIR)) rmSync(PY_DIR, { recursive: true, force: true });
    execFileSync("tar", ["-xzf", archive, "-C", RESOURCES], { stdio: "inherit" });
    rmSync(archive, { force: true });
    if (!existsSync(PY_EXE)) throw new Error(`解压后仍找不到 ${PY_EXE}`);
    log(`解释器就绪：${statSync(PY_EXE).size} bytes`);
  }

  log("安装 requirements.lock …");
  execFileSync(
    PY_EXE,
    [
      "-m",
      "pip",
      "install",
      "--disable-pip-version-check",
      "--no-warn-script-location",
      "-r",
      path.join(REPO, "requirements.lock"),
    ],
    { stdio: "inherit", cwd: PY_DIR, env: cleanEnv() },
  );
  log("自检（import 主依赖，且必须来自自带的 site-packages）：");
  execFileSync(
    PY_EXE,
    [
      "-c",
      "import requests, psutil, wcwidth, sys;"
      " p = requests.__file__;"
      " print('deps ok', requests.__version__, psutil.__version__, p);"
      " assert 'site-packages' in p and sys.prefix.startswith(sys.base_prefix), p",
    ],
    { stdio: "inherit", env: cleanEnv() },
  );
}

main().catch((e) => {
  console.error("[fetch-python] 失败:", e.message);
  process.exit(1);
});
