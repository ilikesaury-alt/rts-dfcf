#!/usr/bin/env python3
"""临时脚本：将 scanner/display.py 物理拆为 scanner/view/{model,assemble,render}.py。

纯文件切分（不改任何逻辑）：
- model.py   : 叶子格式化/辅助函数 + MainRow/ScanView 数据类 + COLS_* + ANSI/CAT_COLOR 常量
- assemble.py : build_scan_view + _regime_weak + _adjusted_picks（纯计算，产出 ScanView）
- render.py   : display / display_priority / render_terminal + 行渲染辅助（只画不算）
- display.py  : re-export 聚合器（消费方 `from scanner.display import X` 零改动）

依赖方向（无环）：model ← assemble；model ← render；render ← assemble。
"""

from __future__ import annotations

import ast
import importlib
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
# 以项目根为导入基（脚本以 scripts/ 为入口，scanner 包不在默认 sys.path）
sys.path.insert(0, str(ROOT))
SRC = ROOT / "scanner" / "display.py"
VIEW = ROOT / "scanner" / "view"
VIEW.mkdir(exist_ok=True)
# 包标记必须在任何 scanner.view.* 导入前存在
(VIEW / "__init__.py").write_text("", encoding="utf-8")

lines = SRC.read_text(encoding="utf-8").splitlines(keepends=True)

# ⚠ 硬闸门（2026-09-14 补）：本脚本按**行号区间**切分「拆分前的 display.py 单体」。
# 拆分完成后 SRC 已经只是 24 行的 re-export 聚合器，再跑一次会把 view/*.py 写成垃圾、
# 并把 display.py 覆盖掉（不可逆的静默破坏）。所以先确认 SRC 仍是单体再动手。
_MONOLITH_MARKERS = ("def render_terminal(", "def build_scan_view(", "class ScanView")
_SRC_TEXT = "".join(lines)
if not all(m in _SRC_TEXT for m in _MONOLITH_MARKERS):
    raise SystemExit(
        "scanner/display.py 已不是拆分前的单体（缺少 "
        + " / ".join(m for m in _MONOLITH_MARKERS if m not in _SRC_TEXT)
        + "）。本脚本是一次性切分工具，重复运行会破坏 scanner/view/*.py 与 display.py。\n"
        "如需再拆：先从拆分提交 b68c044~1 取回单体快照另存，再改本脚本的行号区间。"
    )


def slice_(ranges: list[tuple[int, int]]) -> str:
    out: list[str] = []
    for a, b in ranges:  # 1-indexed inclusive
        out.extend(lines[a - 1 : b])
    return "".join(out)


def _target_names(node) -> set[str]:
    out: set[str] = set()
    if isinstance(node, ast.Name):
        out.add(node.id)
    elif isinstance(node, (ast.Tuple, ast.List)):
        for e in node.elts:
            out |= _target_names(e)
    elif isinstance(node, ast.Starred):
        out |= _target_names(node.value)
    return out


def module_level_names(node) -> set[str]:
    """模块级定义名（含 if/else/try/with 内赋值），但不进入函数/类作用域。"""
    names: set[str] = set()
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(child.name)  # 仅取定义名，不递归进函数体
            continue
        if isinstance(child, ast.Assign):
            for t in child.targets:
                names |= _target_names(t)
            continue
        if isinstance(child, ast.AnnAssign):
            names |= _target_names(child.target)
            continue
        # 控制流语句：继续向下（仍在模块作用域）
        names |= module_level_names(child)
    return names


# 相对导入在 view 子包内会指向错误模块，统一改为绝对导入（所有子文件共用）
HEADER = slice_([(1, 97)]).replace(
    "from .trend_beauty import beauty_mark",
    "from scanner.trend_beauty import beauty_mark",
)

# ── model.py ──
model_imports = HEADER + "\n# 叶子辅助 + 视图模型（model 层，不依赖 assemble/render）\n"
model_body = slice_([(100, 389), (616, 665), (682, 737)])

# ── assemble.py ──
# 依赖 model 的全部定义（含 if/else 内赋值的 ANSI 等），用 * 导入最稳，避免漏名。
# 必须置顶：下面 HEADER 中的 `CAT_COLOR = {...}` 依赖 model 的 ANSI，需在它之前完成导入。
assemble_imports = "from scanner.view.model import *  # noqa: F401,F403\n" + HEADER + "\n"
assemble_body = slice_([(540, 573), (576, 613), (740, 1127)])

# ── render.py ──
# 同样把 * 导入置顶（render 也用到 model 的 ANSI / 常量）。
render_imports = (
    "from scanner.view.model import *  # noqa: F401,F403\n"
    "from scanner.view.assemble import *  # noqa: F401,F403\n" + HEADER + "\n"
)
render_body = slice_([(392, 435), (438, 537), (668, 679), (1130, 1150), (1153, 1210), (1213, 1325), (1328, 1361)])

# ── display.py 聚合器（shim）──
# 物理实现已拆到 scanner/view/{model,assemble,render}.py。
# 消费方透传名（非本文件定义、但外部从 scanner.display 导入）需在此显式重导出：
#   clear_screen(scanner.utils) / fresh_candidate(scanner.ranking) /
#   CAT_LABEL(scanner.categories) / fund_flow_signal(scanner.signals)
shim = (
    '"""display 聚合器：物理实现已拆到 scanner/view/{model,assemble,render}.py。\n\n'
    "本模块仅做 re-export，保持 `from scanner.display import X` 对外契约零变化"
    "（feishu / final_pick / hot_watch / today_report / leaderboard_obs / tests 一行不改）。\n"
    '"""\n'
    "from scanner.view.model import *  # noqa: F401,F403\n"
    "from scanner.view.assemble import *  # noqa: F401,F403\n"
    "from scanner.view.render import *  # noqa: F401,F403\n"
    "# 以下为原 display 透传名（非本文件定义），消费方仍从 scanner.display 取\n"
    "from scanner.utils import clear_screen  # noqa: F401\n"
    "from scanner.ranking import fresh_candidate  # noqa: F401\n"
    "from scanner.categories import CAT_LABEL  # noqa: F401\n"
    "from scanner.signals import fund_flow_signal  # noqa: F401\n"
    "# 原 display 还把下列 config/其它模块全局作为模块属性暴露（测试与既有消费方只读）：\n"
    "from scanner.config import CORE_DIP_DISPLAY_MAX, TREND_MARK_ENABLED  # noqa: F401\n"
    "from scanner.core_themes import core_stock_symbols  # noqa: F401\n"
)


def write_with_all(path: pathlib.Path, imports: str, body: str) -> None:
    """写文件，并在顶部插入 AST 推导 + 运行时校验的 __all__。

    ⚠ `all_names` 取「AST 模块级名 ∩ dir(mod)」，结果**随平台变化**：只在
    `if os.name == "nt"` 分支定义的 Windows 终端探测中间量（_kernel32 / _handle /
    _mode）在 Windows 上会被写进 __all__，换到 Linux/macOS 就成 __all__ 缺名 →
    `from scanner.view.model import *` 直接 AttributeError（2026-09-14 已修，
    详见 scanner/view/model.py 中该分支上方的注释）。再动本脚本时务必让派生出的
    __all__ 只含平台无关的名字，或让这些名字在 else 分支也定义。
    """
    text = imports + "\n" + body
    path.write_text(text, encoding="utf-8")
    mod = ast.parse(text)
    ast_names = module_level_names(mod)
    mod_obj = importlib.import_module("scanner.view." + path.stem)
    actual = set(dir(mod_obj))
    all_names = sorted(n for n in ast_names if n in actual)
    all_line = "__all__ = (\n" + "\n".join(f'    "{n}",' for n in all_names) + "\n)\n\n"
    path.write_text(imports + "\n" + all_line + body, encoding="utf-8")
    print(f"wrote {path.relative_to(ROOT)}  (__all__={len(all_names)} names)")


write_with_all(VIEW / "model.py", model_imports, model_body)
write_with_all(VIEW / "assemble.py", assemble_imports, assemble_body)
write_with_all(VIEW / "render.py", render_imports, render_body)
SRC.write_text(shim, encoding="utf-8")
print("wrote scanner/display.py (shim)")
print("done")
