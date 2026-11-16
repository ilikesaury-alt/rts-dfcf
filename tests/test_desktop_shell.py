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
| 可写落点 = `userData`（A1：无库拷贝，建空库） | 写 `resources/` ⇒ 安装态只读、WAL 起不来；覆盖 ⇒ 抹掉用户跑到一半的库 |
| 关窗 kill 子进程 | 残留后台扫描器 + 锁被占（验收 7） |
| Node 侧不读 sqlite | 与主循环抢 SQLite 写锁（design D2） |
| `.env` 不进包、改指 userData | 密钥躺进安装目录；或反过来读不到 Key |
| 入口**硬导入**的包写进 requirements | 打包照样成功，用户双击才炸（实测 dotenv） |
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
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
    # A1（2026-11-15）：安装包**不再携带** scanner.db ⇒ 没有「种子拷贝」这一步。
    # 守卫意图不变：**绝不覆盖**用户已有的库。
    #   · 有外部活库（RTS_LIVE_DB / RTS_DB_PATH / RTS_REPO_ROOT）⇒ 直接用它，不碰 userData；
    #   · 无 ⇒ 让 Python 在 userData 建空库（init_db 幂等建表），不拷贝任何文件。
    assert "copyFileSync" not in code, "⛔ 不该再有库拷贝（A1：包内无库，改由 Python 建空库）"
    assert "resolveLiveDb()" in code, "必须优先解析外部活库"
    assert "mkdirSync(logDir" in code, "logs/ 必须显式建好（扫描器的落盘全走它）"
    # 用户显式设过就尊重用户（RTS_DB_PATH / RTS_LOG_DIR 是既有覆盖口）
    assert "env.RTS_DB_PATH || state.dbPath" in code
    assert "env.RTS_LOG_DIR || state.logDir" in code


def test_node_side_never_reads_sqlite_directly() -> None:
    """design D2：Node 读 `scanner.db` 会与主循环抢锁。

    A1（2026-11-15）后 Node **连拷贝都不做**了（包内无库），比「只拷一次」更严格。
    """
    code = _main_js()
    assert "better-sqlite3" not in code and 'require("node:sqlite")' not in code
    assert "copyFileSync" not in code  # 唯一允许的文件级接触已随 A1 移除
    assert "fs.statSync" in code  # 解析外部活库时的存在性探测（只读元数据，不碰内容）


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


def test_ports_are_allocated_avoiding_conflicts_with_other_instances() -> None:
    """端口让位：两个 HTTP server 都设了 allow_reuse_address ⇒ 第二个实例能**静默双绑**
    同一端口（Windows 上先绑的优先响应）⇒ 桌面版窗口可能显示终端的数据、`#/tasks`
    的任务可能落到终端那个服务上执行（在错误的库上跑）。

    故桌面版启动前必须探测端口，被占则换空闲端口，并注入子进程 env。
    """
    code = _main_js()
    assert "require(\"node:net\")" in code, "需要 net 模块探测端口占用"
    assert "function isPortFree(" in code, "必须有端口占用探测"
    assert "function findFreePort(" in code, "必须有空闲端口查找"
    assert "function allocatePorts(" in code, "启动前必须分配端口"
    # 分配结果必须注入子进程 env（否则 Python 仍绑旧端口）
    assert "env.RTS_LOCAL_PANEL_PORT = String(LOCAL_PORT)" in code
    assert "env.RTS_TASKS_PORT = String(TASKS_PORT)" in code, "动作面端口也要注入"
    # 分配必须在拉起子进程**之前**
    alloc = code.index("allocatePorts()")
    start = code.index("startScanner()", alloc)
    assert alloc < start, "必须先分配端口再拉起扫描器"


def test_tasks_port_is_injected_and_never_hardcoded_in_shell() -> None:
    """动作面端口可被 env 覆盖（与 Python 侧同口径），shell 不得写死 8900。"""
    code = _main_js()
    assert "RTS_TASKS_PORT" in code
    # 允许「默认常量 8900」，但不允许把 8900 直接塞进 env 赋值
    assert 'env.RTS_TASKS_PORT = String(TASKS_PORT)' in code
    assert "String(8900)" not in code, "端口必须经变量注入，不能写死"


def test_main_js_actually_loads_without_throwing() -> None:
    """⚠ 唯一一条**会真执行** main.js 的守卫（2026-11-16 补）。

    起因：整包启动即崩 —— `let TASKS_PORT = TASKS_PORT_DEFAULT;` 写在
    `const TASKS_PORT_DEFAULT = 8900;` **之前**，`const` 的 TDZ 让主进程一 require
    就抛 `ReferenceError: Cannot access 'TASKS_PORT_DEFAULT' before initialization`。

    上面 16 条全是文本 grep 检查，`node --check` 也只做语法解析 —— **TDZ 是运行期错误**，
    两者都查不出来，于是这个「装完双击就炸」的 bug 一路绿灯溜进安装包。
    ⇒ 必须真的 `require()` 一次：模块顶层一执行，这类顺序/初始化错误立刻暴露。

    `main.js` 尾部用 `if (app && typeof app.getPath === "function")` 把生命周期整块包住，
    离线 `require('electron')` 只返回一个路径字符串 ⇒ 纯函数可加载、零副作用。
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("无 node，跳过 main.js 加载守卫")

    proc = subprocess.run(
        [node, "-e", "require(process.argv[1])", str(DESKTOP / "main.js")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    _err = proc.stderr or proc.stdout
    assert proc.returncode == 0, (
        "main.js 顶层执行抛错（装完双击即崩那一类）" + chr(10) + _err
    )


def test_tasks_port_default_is_declared_before_first_use() -> None:
    """TDZ 的顺序性守卫：`TASKS_PORT_DEFAULT` 的声明必须先于 `TASKS_PORT` 的初始化。

    与上面那条互补 —— 那条证明「现在没崩」，这条说明「别再改回去」，
    失败信息直接指向行号，比读堆栈快。
    """
    code = _code_only(_main_js())
    decl = re.search(r"const\s+TASKS_PORT_DEFAULT\s*=", code, flags=re.M)
    use = re.search(r"let\s+TASKS_PORT\s*=\s*TASKS_PORT_DEFAULT", code, flags=re.M)
    assert decl, "必须有 `const TASKS_PORT_DEFAULT = ...`"
    assert use, "必须有 `let TASKS_PORT = TASKS_PORT_DEFAULT`（端口运行时可让位）"
    assert decl.start() < use.start(), (
        "⛔ `TASKS_PORT_DEFAULT` 的 const 声明必须在 `let TASKS_PORT = TASKS_PORT_DEFAULT` "
        "之前 —— 否则 const 的 TDZ 会让主进程启动即抛 ReferenceError（2026-11-16 事故）"
    )


# ── 6. 打包配置 ───────────────────────────────────────────────────────────────


def test_extra_resources_bundle_python_and_repo_but_not_secrets() -> None:
    build = _package_json()["build"]
    sources = {entry["to"]: entry for entry in build["extraResources"]}
    assert "python" in sources and "repo" in sources, "打包态需要 resources/python + resources/repo"
    filters = sources["repo"]["filter"]
    assert "!.env" in filters, "⛔ 密钥文件（飞书/THS）不得进分发包"
    assert "!scanner.db" in filters, "⛔ 库本体不进包（A1：227MB ⇒ 安装包瘦到 ~63MB，且消灭冷快照病根）"
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
