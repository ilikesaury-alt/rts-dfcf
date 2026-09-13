#!/usr/bin/env python3
"""视图层物理拆分的等价性证明（display.py → view/{model,assemble,render}.py）。

为什么需要它：`scripts/golden_scan.py` 比对的是 `ScanResult`，**覆盖不到 display/view
这条渲染链**；`tests/test_display.py` 只测被显式写到的分支。物理拆分若漏搬一个函数、
搬错一个常量、或漏 re-export 一个被消费的名字，两者都可能全绿。

它回答三个问题：
1. **逻辑有没有被改动** —— `git show <rev>:scanner/display.py` 的每个顶层 def/class
   与其模块级常量，与拆分后同名定义的 AST 逐一对比（AST 不含行号）。
2. **被消费的名字有没有缺口** —— 全仓扫描 `from scanner.display import X` /
   `scanner.display.X`，这些 X 必须都能从新的 `scanner.display` 取到。（这是硬门禁）
3. **缩掉的面是不是显式登记的** —— 旧模块把大量"顺带导入"的名字暴露成了模块属性
   （如 `os` / `now_beijing` / `Candidate`）。本次拆分**有意不再透传**这些
   泄漏出来的名字；它们必须逐条列在 `EXPECTED_SURFACE_REDUCTION` 里，
   否则报错——防止"悄悄少一个名字"和"悄悄多登记一个名字"。

退出码：0 = 等价；1 = 发现差异；2 = 运行失败。

用法：
    python scripts/_verify_view_split.py            # 对比 HEAD 的 display.py
    python scripts/_verify_view_split.py --rev X    # 指定别的 git rev
    python scripts/_verify_view_split.py --verbose
"""
from __future__ import annotations

import argparse
import ast
import importlib.util
import io
import re
import subprocess
import sys
import tempfile
import tokenize
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VIEW = ROOT / "scanner" / "view"
VIEW_MODULES = ("model", "assemble", "render")

# 旧 display.py 的相对导入在"独立模块"语境下会失效，导入时等价替换为绝对导入。
# 这只影响模块级 import 语句，不影响任何函数/类体的 AST。
RELATIVE_FIXES = (
    ("from .trend_beauty import beauty_mark", "from scanner.trend_beauty import beauty_mark"),
)

# ── 有意缩掉的"泄漏名"白名单 ──
# 这些是旧 display.py 在模块顶层 `from x import y` 顺带暴露成 `scanner.display.y`
# 的名字：它们从来不是 display 的契约，只是导入的副产物。本次拆分为聚合器时不再透传。
# 全仓已验证无消费方（`from scanner.display import` / `scanner.display.X`）。
# 增删本表都必须同步回归，否则检查 3 会报错。
EXPECTED_SURFACE_REDUCTION = {
    # 第三方 / 标准库泄漏
    "ctypes",
    "dataclass",
    "os",
    "re",
    "statistics",
    "wcwidth",
    # scanner.utils / models / core_themes 等顺带导入
    "Candidate",
    "EXTERNAL_FAILURES",
    "RecommendationRow",
    "RuleResult",
    "TACTICS_SELL_TAGS",
    "beauty_mark",
    "build_accum_map",
    "build_breakout_kline_map",
    "classify_sector",
    "comeback_sort_key",
    "composite_score",
    "composite_tier",
    "entry_dims",
    "get_cached_klines",
    "get_fund_flow_pct_map",
    "get_today_recommendations",
    "is_nextday_marked",
    "now_beijing",
    "scan_rule",
    # 配置常数泄漏（config 才是它们的真源）
    "CATEGORY_COLOR_KEYS",
    "CAT_DISPLAY_PRIORITY",
    "COMEBACK_DISPLAY_MAX",
    "COMEBACK_DISPLAY_MIN_MAIN",
    "CORE_DIP_CATEGORY",
    "CORE_PULLBACK_MAX",
    "CORE_PULLBACK_MIN",
    "DECISION_LAYER_ENABLED",
    "DISPLAY_MAX_TODAY_PCT",
    "FINAL_PICK_ENABLED",
    "HOT_HIGHLIGHT_STREAK",
    "HOT_MAX_MARKET_CAP",
    "HOT_MAX_PERCENT",
    "TOP40_THRESHOLD",
    "V2_CATEGORY",
    "V2_POOL_DISPLAY_TOP",
    # 私有名泄漏（第 1 步已升公共名，旧 display 仍留着旧别名）
    "_breakout_profile_key",
    "_breakout_structure_ok",
    "_core_dip_quality",
    "split_risk_flags",
    "to_float",
    "to_int",
}

SKIP_MODULES = {"__builtins__", "__cached__", "__file__", "__loader__", "__spec__", "__name__", "__doc__", "__package__"}


def git_show(rev: str, rel: str) -> str:
    out = subprocess.run(
        ["git", "show", f"{rev}:{rel}"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if out.returncode != 0:
        raise RuntimeError(f"git show {rev}:{rel} 失败：{out.stderr.strip()}")
    return out.stdout


def normalized(node: ast.AST) -> str:
    """不含行号/列号的规范化 dump（行号在拆分后必然变化，不构成差异）。"""
    return ast.dump(node, annotate_fields=True, include_attributes=False)


def collect_defs(tree: ast.Module) -> tuple[dict[str, str], dict[str, str]]:
    """返回 (定义体, 模块级常量)：name → 规范化 AST。

    定义体 = 顶层 def / async def / class（含 if/try/with 内）；常量 = 顶层
    Assign / AnnAssign 的**值**（按值比对，避开 `A, B = 1, 2` 解包写法差异）。
    """
    defs: dict[str, str] = {}
    consts: dict[str, str] = {}

    def targets(node: ast.AST) -> list[str]:
        if isinstance(node, ast.Name):
            return [node.id]
        if isinstance(node, (ast.Tuple, ast.List)):
            out: list[str] = []
            for e in node.elts:
                out.extend(targets(e))
            return out
        return []

    def walk(body: list[ast.stmt]) -> None:
        for stmt in body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                defs[stmt.name] = normalized(stmt)
                continue
            if isinstance(stmt, ast.Assign):
                value = normalized(stmt.value)
                for t in stmt.targets:
                    for name in targets(t):
                        consts[name] = value
                continue
            if isinstance(stmt, ast.AnnAssign):
                if isinstance(stmt.target, ast.Name) and stmt.value is not None:
                    consts[stmt.target.id] = normalized(stmt.value)
                continue
            for field in ("body", "orelse", "finalbody"):
                sub = getattr(stmt, field, None)
                if isinstance(sub, list) and sub:
                    walk(sub)
            for handler in getattr(stmt, "handlers", []) or []:
                walk(handler.body)

    walk(tree.body)
    return defs, consts


def load_legacy(rev: str) -> types.ModuleType:
    """把旧 display.py 作为独立模块导入（改写相对导入后）。"""
    src = git_show(rev, "scanner/display.py")
    for old, new in RELATIVE_FIXES:
        src = src.replace(old, new)
    tmp = Path(tempfile.mkdtemp(prefix="legacy_display_"))
    path = tmp / "legacy_display_snapshot.py"
    path.write_text(src, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("legacy_display_snapshot", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    spec.loader.exec_module(mod)
    return mod


def strip_literals(text: str) -> str:
    """把字符串字面量与注释替换成空格（保留换行），只留真实代码。

    不这么做的话，docstring 里出现的 ``scanner.display.y`` 会被当成消费方（本工具首版
    就栽在 display.py 自己的说明文字上）。
    """
    out: list[str] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type in (tokenize.STRING, tokenize.COMMENT):
                out.append(" " * len(tok.string))
            else:
                out.append(tok.string)
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return text
    return "".join(out)


def consumed_names() -> set[str]:
    """全仓扫描真正消费 `scanner.display` 的名字（import 形式 + 属性形式）。"""
    names: set[str] = set()
    attr_re = re.compile(r"\bscanner\.display\.([A-Za-z_]\w*)")
    self_path = Path(__file__).resolve()
    for path in ROOT.rglob("*.py"):
        if any(part in {".git", "__pycache__", ".venv"} for part in path.parts):
            continue
        if path.resolve() == self_path:
            continue
        raw = path.read_text(encoding="utf-8", errors="replace")
        names |= set(attr_re.findall(strip_literals(raw)))
        try:
            tree = ast.parse(raw)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.module != "scanner.display":
                continue
            for alias in node.names:
                names.add(alias.name)
    return names


def main() -> int:
    ap = argparse.ArgumentParser(description="视图层物理拆分等价性证明")
    ap.add_argument("--rev", default="HEAD", help="对比的 git rev（默认 HEAD）")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    try:
        legacy_src = git_show(args.rev, "scanner/display.py")
    except RuntimeError as exc:
        print(f"[FAIL] {exc}")
        return 2

    legacy_defs, legacy_consts = collect_defs(ast.parse(legacy_src))

    new_defs: dict[str, str] = {}
    new_consts: dict[str, str] = {}
    for name in VIEW_MODULES:
        mod_path = VIEW / f"{name}.py"
        if not mod_path.exists():
            print(f"[FAIL] 缺少 {mod_path.relative_to(ROOT)}")
            return 2
        d, c = collect_defs(ast.parse(mod_path.read_text(encoding="utf-8")))
        new_defs.update(d)
        new_consts.update(c)
        if args.verbose:
            print(f"    {name}.py: {len(d)} defs, {len(c)} consts")

    failures: list[str] = []

    # ── 检查 1：每个旧的顶层 def/class 必须存在且 AST 一致 ──
    missing = sorted(set(legacy_defs) - set(new_defs))
    changed = sorted(n for n in set(legacy_defs) & set(new_defs) if legacy_defs[n] != new_defs[n])
    if missing:
        failures.append(f"定义缺失 {len(missing)} 个：{missing}")
    if changed:
        failures.append(f"定义体被改动 {len(changed)} 个：{changed}")

    # ── 检查 2：每个旧的模块级常量必须存在且值一致 ──
    c_missing = sorted(set(legacy_consts) - set(new_consts))
    c_changed = sorted(n for n in set(legacy_consts) & set(new_consts) if legacy_consts[n] != new_consts[n])
    if c_missing:
        failures.append(f"常量缺失 {len(c_missing)} 个：{c_missing}")
    if c_changed:
        failures.append(f"常量值被改动 {len(c_changed)} 个：{c_changed}")

    # ── 检查 3：对外属性面 ──
    try:
        legacy_mod = load_legacy(args.rev)
        import scanner.display as new_mod  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001 - 导入失败就是要报出来
        print(f"[FAIL] 导入失败：{type(exc).__name__}: {exc}")
        return 2

    legacy_attrs = {n for n in dir(legacy_mod) if not n.startswith("__")} - SKIP_MODULES
    new_attrs = {n for n in dir(new_mod) if not n.startswith("__")} - SKIP_MODULES
    dropped = legacy_attrs - new_attrs
    extra = new_attrs - legacy_attrs

    # 3a. 被消费的名字一个都不能少（硬门禁）
    used = consumed_names()
    used_missing = sorted(n for n in used if n not in new_attrs)
    if used_missing:
        failures.append(
            f"被消费的名字缺失 {len(used_missing)} 个（`from scanner.display import` 会 ImportError）："
            f"{used_missing}"
        )

    # 3b. 缩掉的面必须与白名单**精确**一致（双向）
    undeclared = sorted(dropped - EXPECTED_SURFACE_REDUCTION)
    over_declared = sorted(EXPECTED_SURFACE_REDUCTION - dropped)
    if undeclared:
        failures.append(
            f"有 {len(undeclared)} 个属性被缩掉但未登记进 EXPECTED_SURFACE_REDUCTION：{undeclared}"
        )
    if over_declared:
        failures.append(
            f"EXPECTED_SURFACE_REDUCTION 有 {len(over_declared)} 项其实没被缩掉（表已过期）：{over_declared}"
        )

    # ── 报告 ──
    print(f"对比基线：git rev {args.rev}:scanner/display.py")
    print(f"  旧：{len(legacy_defs)} 定义 / {len(legacy_consts)} 常量 / {len(legacy_attrs)} 运行时属性")
    print(f"  新：{len(new_defs)} 定义 / {len(new_consts)} 常量 / {len(new_attrs)} 运行时属性")
    print(f"  被消费名字：{len(used)} 个，全部可取到 = {not used_missing}")
    print(f"  有意缩掉的泄漏名：{len(dropped)} 个（已登记 {len(EXPECTED_SURFACE_REDUCTION)}）")
    if args.verbose and extra:
        print(f"  新增属性（无害）：{sorted(extra)}")

    if failures:
        print("\n[FAIL] 物理拆分不是等价变换：")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("\n[OK] 等价：定义体 AST 逐字段一致、常量值一致、被消费名字零缺口、缩掉的面与白名单精确一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
