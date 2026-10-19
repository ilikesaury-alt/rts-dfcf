"""Electron 壳的**结构守卫**（Issue C/D，2026-10-19）。

与 `tests/test_panel_web_hygiene.py` 同一性质：这些断言防的是「约定被悄悄改掉」，
而不是行为 —— JS 侧没有 pytest 能跑的东西（真跑 Electron 要 GUI + 下载 100MB 运行时），
但**每一条都是能一眼看出的硬约束**，破了不会让任何功能测试变红：

| 守卫 | 破了会怎样 |
|---|---|
| 只绑回环 | 误改 `0.0.0.0` ⇒ 同网段任何人可读本地数据（design §6.4 风险 1） |
| 注入 `RTS_LOCAL_PANEL=1` 且不带 `--no-panel` | 不带看板开关 ⇒ 窗口起来但是空页；顺手带 `--no-panel` ⇒ 云端上报被关（那是另一个开关） |
| env 净化 `PYTHONHOME/PYTHONPATH` | 打包态的 standalone Python 被系统 Python 串味（design §7.1/§7.4） |
| 打包态走 `resourcesPath` | 走 PATH 上的 `python` ⇒ 用户机器上没有就 ENOENT（Issue D 的原始动机） |
| 可写落点 = `userData`（含种子不覆盖） | 写 `resources/` ⇒ 安装态只读、WAL 起不来；覆盖 ⇒ 抹掉用户跑到一半的库 |
| 关窗 kill 子进程 | 残留后台扫描器 + 锁被占（验收 7） |
| Node 侧不读 sqlite | 与主循环抢 SQLite 写锁（design D2） |
| `.env` 不进包、改指 userData | 密钥躺进安装目录；或反过来读不到 Key |
| 入口**硬导入**的包写进 requirements | 打包照样成功，用户双击才炸（实测 dotenv） |
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

DESKTOP = Path(__file__).resolve().parents[1] / "desktop"

pytestmark = pytest.mark.skipif(
    not (DESKTOP / "main.js").is_file(),
    reason="桌面工程 desktop/ 未检出",
)


def _main_js() -> str:
    return (DESKTOP / "main.js").read_text(encoding="utf-8")


def _code_only(text: str) -> str:
    """去注释后的代码（注释里讲禁令不算违规）。"""
    without_block = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"^\s*//.*$", "", without_block, flags=re.M)


def _package_json() -> dict:
    return json.loads((DESKTOP / "package.json").read_text(encoding="utf-8"))


# ── 1. 安全硬约束 ─────────────────────────────────────────────────────────────


def test_desktop_binds_loopback_only() -> None:
    code = _main_js()
    assert 'const HOST = "127.0.0.1";' in code, "主机常量必须写死回环"
    # 本行**就是要**断言这个字面量不出现（误绑 = 同网段可读本地数据），故豁免 S104——
    # 那不是「代码里绑了全网卡」，而是「代码里不许有它」。
    assert "0.0.0.0" not in _code_only(code), "⛔ 不得出现 0.0.0.0（同网段可读本地数据）"  # noqa: S104


# ── 2. 扫描器拉起方式 ─────────────────────────────────────────────────────────


def test_scanner_is_started_with_local_panel_switch_only() -> None:
    code = _main_js()
    assert 'env.RTS_LOCAL_PANEL = "1";' in code, "不带看板开关 ⇒ 窗口起来是空页"
    assert '"unified_scanner.py"' in code, "拉起的必须是主扫描器入口"
    # 断言**真正的 argv**，而不是全文搜字符串：注释里解释这条禁令不算违规，
    # 而 argv 里多一个 `--no-panel` 会把云端上报一起关掉（D3：两个开关互相独立）。
    argv = re.search(r"execFile\(\s*python,\s*\[(.*?)\]", code, re.S)
    assert argv, "找不到 execFile(python, [...]) 调用（改结构要同步本守卫）"
    assert "--no-panel" not in argv.group(1)


def test_python_env_is_scrubbed_of_system_interpreter_vars() -> None:
    code = _main_js()
    for name in ("PYTHONHOME", "PYTHONPATH", "PYTHONUSERBASE", "VIRTUAL_ENV", "CONDA_PREFIX"):
        assert f'"{name}"' in code, f"env 净化缺 {name} ⇒ 打包态解释器被系统 Python 串味"
    assert "delete env[key]" in code, "净化必须真的从子进程 env 里删掉，不是只列个名单"
    # ⚠ 删 PYTHONPATH 不够（2026-10-19 实测）：CPython 的 site 模块默认还会把
    # `%APPDATA%\Python\Python312\site-packages`（同 minor 即命中）插到 sys.path **最前**。
    assert 'env.PYTHONNOUSERSITE = "1";' in code, "必须禁用户 site，否则 import 到用户那份依赖"
    fetch = (DESKTOP / "fetch-python.mjs").read_text(encoding="utf-8")
    assert 'env.PYTHONNOUSERSITE = "1";' in fetch, "装依赖那一步也要禁用户 site（否则依赖不会装进包）"


def test_packaged_python_comes_from_resources_path() -> None:
    code = _main_js()
    assert "app.isPackaged" in code
    assert "process.resourcesPath" in code
    assert re.search(r'path\.join\(process\.resourcesPath, "python"', code), "打包态解释器必须落在 resources/python"
    assert "RTS_PYTHON" in code, "留一个显式覆盖口（调试/非打包运行时）"


# ── 3. 可写落点 ───────────────────────────────────────────────────────────────


def test_writable_state_goes_to_user_data_without_overwriting() -> None:
    code = _main_js()
    assert 'app.getPath("userData")' in code
    assert "RTS_DB_PATH" in code and "RTS_LOG_DIR" in code
    # 种子拷贝必须是「不存在才拷」：覆盖会把用户跑到一半的库抹掉
    seed = re.search(r"if \(!fs\.existsSync\(dbPath\)\) \{(.*?)\n  \}", code, re.S)
    assert seed, "找不到种子拷贝块（改名/改结构要同步本守卫）"
    assert "copyFileSync" in seed.group(1)
    assert "mkdirSync(logDir" in code, "logs/ 必须显式建好（扫描器的落盘全走它）"
    # 用户显式设过就尊重用户（RTS_DB_PATH / RTS_LOG_DIR 是既有覆盖口）
    assert "env.RTS_DB_PATH || state.dbPath" in code
    assert "env.RTS_LOG_DIR || state.logDir" in code


def test_node_side_never_reads_sqlite_directly() -> None:
    """design D2：Node 读 `scanner.db` 会与主循环抢锁。库只在**打包种子**里被碰一次（整文件拷贝）。"""
    code = _main_js()
    assert "better-sqlite3" not in code and 'require("node:sqlite")' not in code
    assert "copyFileSync" in code  # 唯一允许的文件级接触


# ── 4. env 位置 ───────────────────────────────────────────────────────────────


def test_packaged_env_file_points_at_user_data() -> None:
    """`.env` **不进包**（第 5 节的过滤器守着）⇒ 打包态必须把 `RTS_ENV_FILE` 指到 userData。

    否则装载态的 THS API Key / 飞书 webhook 一个都读不到：dotenv 默认只往
    `resources/repo` 及其上级找，而那里恰恰没有 `.env`（故意的）。
    """
    code = _main_js()
    assert "RTS_ENV_FILE" in code
    assert "env.RTS_ENV_FILE || state.envFile" in code, "用户显式设过要尊重用户"
    assert 'path.join(userData, ".env")' in code


def test_env_file_override_is_honored_by_entry(tmp_path) -> None:
    """`RTS_ENV_FILE` 指哪读哪 —— 与上一条配对：壳负责指路，入口负责照做。"""
    import os
    import subprocess
    import sys

    env_file = tmp_path / "custom.env"
    env_file.write_text("RTS_PROBE_FROM_ENV_FILE=yes\n", encoding="utf-8")  # 注意：不带 BOM
    env = {**os.environ}
    env.pop("RTS_PROBE_FROM_ENV_FILE", None)
    env["RTS_ENV_FILE"] = str(env_file)
    probe = "import unified_scanner, os; print('PROBE=' + str(os.environ.get('RTS_PROBE_FROM_ENV_FILE')))"
    out = subprocess.run(  # noqa: S603 - 静态 argv（本文件的 python + 字面量代码），无外部输入
        [sys.executable, "-c", probe],
        cwd=DESKTOP.parent,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    assert "PROBE=yes" in out.stdout, f"RTS_ENV_FILE 没被 load_dotenv 读取\n{out.stdout}\n{out.stderr}"


# ── 5. 生命周期 ───────────────────────────────────────────────────────────────


def test_closing_window_kills_python_child() -> None:
    code = _main_js()
    assert 'app.on("window-all-closed"' in code, "关窗必须退出应用（否则残留后台扫描器）"
    assert 'app.on("before-quit"' in code
    assert "stopScanner()" in code
    assert re.search(r"function stopScanner\(\)[\s\S]{0,400}proc\.kill\(\)", code), "stopScanner 必须真的 kill"


def test_second_instance_does_not_start_second_scanner() -> None:
    code = _main_js()
    assert "requestSingleInstanceLock" in code
    assert "second-instance" in code
    assert "app.quit()" in code.split("requestSingleInstanceLock")[1], "拿不到锁必须直接退出"


# ── 6. 打包配置 ───────────────────────────────────────────────────────────────


def test_extra_resources_bundle_python_and_repo_but_not_secrets() -> None:
    build = _package_json()["build"]
    sources = {entry["to"]: entry for entry in build["extraResources"]}
    assert "python" in sources and "repo" in sources, "打包态需要 resources/python + resources/repo"
    filters = sources["repo"]["filter"]
    assert "!.env" in filters, "⛔ 密钥文件（飞书/THS）不得进分发包"
    assert "!scanner.db-wal" in filters, "WAL 是活文件，冷拷贝会得到不一致快照"
    assert "!scanner.db.bak*" in filters, "备份库（~100MB）不进包"
    assert "!desktop/**" in filters, "别把 desktop/ 自己套进 resources/repo（递归膨胀）"


def test_package_scripts_are_self_contained() -> None:
    scripts = _package_json()["scripts"]
    assert scripts["dist"].startswith("node prepare.mjs"), "打包前必须跑前置检查（缺 dist/python 要显式失败）"
    assert "electron-builder" in scripts["dist"]
    assert "fetch-python" in scripts


# ── 7. 打包运行时的依赖契约 ───────────────────────────────────────────────────


def test_prepare_rejects_python_that_cannot_import_entry() -> None:
    """前置检查必须**真的跑一次入口 import**，而不是只看文件在不在。

    实测事故（2026-10-19）：依赖漏写时打包完全成功，只在用户双击时窗口里回显
    `ModuleNotFoundError: No module named 'dotenv'`。看文件在不在**发现不了**这种缺。
    """
    code = (DESKTOP / "prepare.mjs").read_text(encoding="utf-8")
    assert "import unified_scanner" in code, "前置检查必须试跑主入口的 import"
    assert "PYTHONNOUSERSITE" in code, "这一步必须与运行期同口径（否则用户 site 会让它假绿）"
    assert "requirements.lock" in code or "requirements" in code, "报错信息要点名去哪个文件补依赖"


def test_entry_module_level_imports_are_declared_in_requirements() -> None:
    """主入口**模块级**导入的第三方包，必须同时写进 `requirements.txt` 与 `requirements.lock`。

    `unified_scanner.py:21` 的 `from dotenv import load_dotenv` 是**硬导入**（无 try/except），
    但两个依赖文件长期都没写它 —— 本机靠「碰巧装过」跑了几个月，直到打包桌面版
    换用自带解释器才炸。这条守卫把这颗雷变成 CI 红灯。

    只看模块级：函数体内的导入是**可选依赖**的正当写法（如 pywencai/lightgbm 的降级）。
    """
    import ast
    import sys

    repo = DESKTOP.parent
    tree = ast.parse((repo / "unified_scanner.py").read_text(encoding="utf-8"))
    mods: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            mods.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            mods.add(node.module.split(".")[0])

    first_party = {"scanner", "tests", "scripts"}
    third_party = sorted(m for m in mods if m not in sys.stdlib_module_names and m not in first_party)
    assert third_party, "解析不出任何第三方导入（AST 走法要同步本守卫）"

    # 模块名 → 分发名（只有不一致的才需要登记）
    aliases = {"dotenv": "python-dotenv"}

    def _norm(name: str) -> str:
        return name.split("[")[0].strip().lower().replace("_", "-")

    declared: dict[str, set[str]] = {}
    for fname in ("requirements.txt", "requirements.lock"):
        names: set[str] = set()
        for line in (repo / fname).read_text(encoding="utf-8").splitlines():
            line = line.split("#")[0].strip()
            if not line or line.startswith("-"):
                continue
            names.add(_norm(re.split(r"[=<>!~]", line)[0]))
        declared[fname] = names

    for fname, names in declared.items():
        missing = [m for m in third_party if _norm(aliases.get(m, m)) not in names]
        assert not missing, (
            f"{fname} 漏了主入口**硬导入**的包：{missing}（打包态自带解释器会 import 失败，而本机因为碰巧装过不会暴露）"
        )

    # 具体回归锚点：曾经漏的就是它
    assert "python-dotenv" in declared["requirements.lock"]
