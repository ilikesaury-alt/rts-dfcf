#!/usr/bin/env python3
"""视图层物理拆分的等价性证明（display.py → view/{model,assemble,render}.py）。

为什么需要它：`scripts/golden_scan.py` 比对的是 `ScanResult`，**覆盖不到 display/view
这条渲染链**；`tests/test_display.py` 只测被显式写到的分支。物理拆分若漏搬一个函数、
搬错一个常量、或漏 re-export 一个被消费的名字，两者都可能全绿。

它回答三个问题：
1. **逻辑有没有被改动** —— `git show <rev>:scanner/display.py` 的每个顶层 def/class
   与其模块级常量，与拆分后同名定义的 AST 逐一对比（AST 不含行号）。拆分之后**合法的
   功能迭代**会改动它们，两类改动分别登记在 `EXPECTED_BODY_DIVERGENCE`（函数体）与
   `EXPECTED_CONST_DIVERGENCE`（模块级常量值，2026-09-18 补：此前常量没有白名单，
   任何合法列变更都会让本工具**永久红**，常红的守卫等于没有守卫）。两者都双向校验。
2. **被消费的名字有没有缺口** —— 全仓扫描 `from scanner.display import X` /
   `scanner.display.X`，这些 X 必须都能从新的 `scanner.display` 取到。（这是硬门禁）
3. **缩掉的面是不是显式登记的** —— 旧模块把大量"顺带导入"的名字暴露成了模块属性
   （如 `os` / `now_beijing` / `Candidate`）。本次拆分**有意不再透传**这些
   泄漏出来的名字；它们必须逐条列在 `EXPECTED_SURFACE_REDUCTION` 里，
   否则报错——防止"悄悄少一个名字"和"悄悄多登记一个名字"。

退出码：0 = 等价；1 = 发现差异；2 = 运行失败。

用法：
    python scripts/_verify_view_split.py            # 对比拆分前的单体外快照（默认）
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

# 拆分提交：b68c044「第N步完成: display 物理拆 scanner/view/{model,assemble,render}（等价变换）」。
# 比对基线必须是它的**父提交** —— 那才是 display.py 还是单体（monolith）的最后状态。
# ⚠ 不能用默认的 HEAD：拆分后的 display.py 只是 re-export 聚合器（0 个顶层 def），
# 拿它当基线会得到「旧：0 定义 / 0 常量」，白名单双向校验必然误报「表已过期」。
SPLIT_COMMIT = "b68c044"
DEFAULT_BASELINE_REV = f"{SPLIT_COMMIT}~1"

# 旧 display.py 的相对导入在"独立模块"语境下会失效，导入时等价替换为绝对导入。
# 这只影响模块级 import 语句，不影响任何函数/类体的 AST。
RELATIVE_FIXES = (("from .trend_beauty import beauty_mark", "from scanner.trend_beauty import beauty_mark"),)

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
    # ── 有意缩掉的**真实定义**（不属于"泄漏名"，机制上同表登记）──
    # 2026-09-16「🎯 标记与回马枪都删除」：`_adjusted_picks` 是 display 的顶层 def，
    # 其排序语义完全由被删的两个特性构成（见 assemble.py 的删除说明），故整体删除。
    "_adjusted_picks",
    # 2026-09-21「终选参考区整体删除」：`_core_dip_entry_quality` 是 display 的顶层 def，
    # 唯一生产调用方是终选合池用的 core_dips 序列（同批删除），故整体删除。
    "_core_dip_entry_quality",
    # 私有名泄漏（第 1 步已升公共名，旧 display 仍留着旧别名）
    "_breakout_profile_key",
    "_breakout_structure_ok",
    "_core_dip_quality",
    "split_risk_flags",
    "to_float",
    "to_int",
    # Windows 分支专有的终端探测中间量（2026-09-14）：它们**只在 `if os.name=="nt"` 分支
    # 存在**，却被拆分脚本推导出的 __all__ 意外导出。已从 model/assemble/render 的 __all__
    # 全部移除 —— 既不是契约，Linux/macOS 下 `import *` 还会因缺名直接 AttributeError。
    "_handle",
    "_kernel32",
    "_mode",
}

# ── 拆分后**有意**删除的定义白名单（2026-09-16 新增）──
# 拆分本身是等价变换，定义一个都不能少；但**功能迭代**会合法地删掉整个函数（例如
# 「🎯 标记与回马枪都删除」带走了 `_adjusted_picks`）。这类删除必须显式登记，否则
# 检查 1 的「定义缺失」会永久变红 —— 而常红守卫的下场是被无视，真出现拆分走样时
# 就没人看得见了（与 `_render_hot_watch_region` 脚注改动同理）。
# 双向校验：登记了却仍然存在 = 表过期，同样报错。
EXPECTED_DEFINITION_REMOVAL = {
    "_adjusted_picks": "2026-09-16 🎯/回马枪删除：该序列的排序语义完全由 marked(🎯) 与 "
    "comeback_sort_key(回马枪) 构成，两者删除后无剩余语义可保留",
    "_core_dip_entry_quality": "2026-09-21 终选参考区删除：它是低吸质量排序键，唯一生产调用方是 assemble 里"
    "为终选合池准备的 core_dips 序列；序列消失后排序键无消费方（底层 core_themes.low_buy_quality 仍在用）",
}

# ── 拆分后**有意**改动的模块级常量白名单（2026-09-18 新增）──
# 与 EXPECTED_BODY_DIVERGENCE 对称：检查 2 原先**没有**白名单，于是「拆分之后任何
# 合法的列定义变更」都会让本工具永久红 —— 而常红守卫的下场是被无视，真出现拆分走样
# 时反而没人看得见（本仓已因同类理由给脚注改动开过白名单，见下面那条 61631bc 的登记）。
# 双向校验：登记了却已不再与基线分歧 = 表已过期，同样报错。
EXPECTED_CONST_DIVERGENCE = {
    # 2026-09-18 commit 5efd8cb「统一终端/飞书列表列顺序，三区均显示5日累计」：
    # COLS_HOT 在「现价」之后插入「5日累计」列（13 → 14 列）。基线是 09-13 拆分时的旧列集，
    # 故这不是拆分走样而是功能迭代。
    # ⚠ 本工具**够不着**列变更的下游一致性（飞书压缩列规格的列数、两出口行宽）——
    # 那由 tests/test_feishu.py::test_hot_row_columns_match_terminal /
    # test_hot_row_width_is_uniform 守，改列必须同时跑它们。
    "COLS_HOT": "列集变更（5efd8cb 插入「5日累计」列）：功能迭代，非拆分走样",
}

# ── 拆分后**有意**改动的定义体白名单 ──
# 拆分本身是等价变换；此后的功能迭代会合法地改动 view/ 里的函数体，那不属于「拆分走样」。
# 每条都必须写明改了什么、归属哪次改动，避免这张表变成「把红灯涂绿」的垃圾桶。
# 双向校验：登记了却已不再与基线分歧 = 表过期，同样报错。
EXPECTED_BODY_DIVERGENCE = {
    # 2026-09-14 资金流出口径统一（commit 02ae8af）：展示层新增「资金流出」过滤。
    # 同日第二批（决策层删除 + v2/核心低吸展示区隐藏）又改了同一批函数，两条理由合并记录。
    # 2026-09-21 第三批：终选参考区整体删除（scanner/final_pick.py + scanner/decision.py），
    # 本表四条相关登记同步追加该批次说明。
    "ScanView": "新增 flow_filtered 字段；随后移除 core_dip_rows/show_core_dip（低吸区隐藏）、"
    "pool_rows/pool_total（v2 隐藏）、decision_lines（决策层删除）四组字段；"
    "2026-09-21 再移除 final_pick_lines（终选参考区删除）",
    "build_scan_view": "新增资金流出过滤（today_recs 单点过滤，下游区域自动继承）；"
    "随后不再构建 v2 pool_rows/pool_total、不再算 _show_core_dip/decision_lines；"
    "2026-09-21 再移除终选合池调用与 core_dips/pool_pick_recs 两个中间序列",
    "render_terminal": "顶部输出「▸ 资金流出已剔除 N 只」；随后移除「今日决策」区块"
    "（决策层删除、终选参考改独立区块）、v2 池选区、核心方向低吸区；"
    "2026-09-21 再移除「终选参考」区块本身（终端只剩四区），"
    "同日把 v1 池选标题更正为「过热劣后·类别优先·资金流降序」（旧标题是 08-28 旧实现）",
    # ⚠ `_build_summary` **不登记**（两个规则外的事实）：
    #   ① 它是拆分之后新增的函数，不在基线 display.py 里 —— 本工具只比对「基线里已有的
    #      定义」，登记一个基线里没有的名字会被判「表已过期」（2026-09-21 实测）；
    #   ② 它本身已在 2026-09-21 随「综合判断摘要」整块删除（用户决策：终端只留四区），
    #      删除也无需登记 —— 它从不在基线的 missing 集合里。
    # 2026-09-14 哑参清理：🎯 行尾渲染自 2026-09-04 停用后遗留的两个入参
    "_entry_row_suffix": "删除从未被读取的 marked 入参（🎯 行尾渲染已停用）",
    "_print_priority_row": "删除无任何调用方传入的 nextday_mark 入参",
    # 2026-09-16 🎯/回马枪删除：函数体未变，docstring 补注「SQL 里的
    # `NOT IN ('comeback',…)` 必须保留」（那是对**历史 recommendations 行**的过滤，
    # 与桶删除无关），并删掉已不存在的「动态推荐」消费方提法。
    "_regime_weak": "docstring 补注：comeback 历史行过滤必须保留；动态推荐消费方已删除",
    # 2026-09-14 第二批：决策层删除 + v2/核心低吸展示区隐藏（用户决策）
    "display": "移除 decision_lines 入参（决策层删除后无处可注入）；"
    "2026-09-21 新增 new_symbols 入参并透传（v1 新票优先）",
    "display_priority": "同上：移除 decision_lines 入参与其透传；"
    "2026-09-21 新增 new_symbols 入参并透传给 build_scan_view",
    "_beauty_mark_for": "docstring 口径更新（v2 池选展示区已隐藏，标记现状只落 v1 池选行）；"
    '2026-09-15 再更新为分档口径（日线定准入、分时定级别 → ""/"美"/"美★"）',
    # 2026-09-11 飙升区合入后、拆分提交之前的脚注文案改动（commit 61631bc：
    # 「沪深主板+创业板」→「创业板」、「ST·科创板/北交所/ETF」→「非创业板」）。
    # 该改动发生在**拆分之前、基线之后**，与拆分等价性无关，但按本工具的规则必须登记
    # —— 否则守卫常年红 1 行，真出现等价性破坏时会被这条噪音淹没。
    "_render_hot_watch_region": "脚注文案口径修正（61631bc，拆分前引入）：样本面收窄为创业板；"
    "2026-09-18 本区扩为 A 段榜内飙升 + B 段榜外异动两段并列（行渲染抽成 _hot_row_cells 共用）；"
    "2026-09-21 A 段空时不再打区块标题与列头（列头改由 B 段自带，此前会留一张只有列头的空表）",
    # 2026-09-18 沪深飙升区 B 段（榜外异动）：独立运行 `python -m scanner.offboard_watch`
    # 也要能画 B 段，故新增 offboard_rows 入参并透传给 _render_hot_watch_region。
    "render_hot_watch_standalone": "新增 offboard_rows 入参（B 段榜外异动的独立渲染入口）",
    # 这两个纯函数**函数体未改**，只是补了 docstring（docstring 属于函数体 AST，故须登记）：
    # 隐藏的是渲染与 ScanView 字段，排序/标签口径本身完整保留，供恢复 v2 区时零成本复原。
    "_v2_pool_sort_key": "docstring 补注：v2 展示区隐藏后本函数无生产调用方（有意保留）",
    "_entry_dip_labels": "docstring 补注：唯一调用方 _v2_pool_sort_key 失去生产消费（有意保留）",
    # 2026-09-21「v1 主表新票优先」（用户决策 ④B）：MainRow 新增 is_new_entry 字段 ——
    # 它既是 v1 排序键第 1 项，也是终端/飞书行尾「新」标记的判定源。判据来自
    # build_scan_view 的 new_symbols 入参（跨轮票集差集），**不读** recommendations.time
    # （后者会被提分覆盖，MIN(time) 是「最后一次提分」而非首次出现）。
    "MainRow": "新增 is_new_entry 字段（本轮新进池：v1 排序第 1 键 + 两出口行尾「新」标记）",
}

SKIP_MODULES = {
    "__builtins__",
    "__cached__",
    "__file__",
    "__loader__",
    "__spec__",
    "__name__",
    "__doc__",
    "__package__",
}


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


# 执行历史快照时要换成"宽容 shim"的模块：快照里有 `from <mod> import <name>`，
# 而这些名字会被后续功能迭代合法删除。仅替换这几个模块 —— 不换 `scanner.view.*`
# 等被测对象，否则会把真实的等价性差异一起吞掉。
LEGACY_TOLERATED_MODULES = (
    "scanner.config",
    "scanner.ranking",
)


def _shim_for_legacy(module_name: str) -> types.ModuleType:
    """镜像 `module_name`，但对**已被删除的名字**返回哨兵而非抛 ImportError。

    为什么需要：`load_legacy` 是**真实执行**历史快照（基线 rev 的 display.py），而快照里
    写着当时存在的 `from scanner.config import ...` / `from scanner.ranking import ...`。
    之后这些模块一旦删名，本工具就会自己先 ImportError 挂掉 —— 那是**与「拆分是否
    等价」无关的假失败**，且会随正常演进反复出现，不能靠"别删常量"来回避。

    实例：2026-09-14 删决策层时移除了 `DECISION_LAYER_ENABLED`；2026-09-16 删 🎯/回马枪
    时移除了 `scanner.config.COMEBACK_DISPLAY_MAX` / `COMEBACK_DISPLAY_MIN_MAIN` 与
    `scanner.ranking.comeback_sort_key` / `is_nextday_marked` —— 快照全都引用了它们。

    故仅在执行快照期间替换 `sys.modules[module_name]`：属性取值照搬真实模块，
    未知名回退哨兵并打印告警。判定不受影响 —— 检查 3 比对的是**属性名集合**（旧快照
    暴露了哪些名字），不是取值；这些名字本就该出现在 `EXPECTED_SURFACE_REDUCTION` 里。
    """
    import importlib

    real = importlib.import_module(module_name)

    shim = types.ModuleType(module_name)
    shim.__dict__.update(real.__dict__)

    def _missing(name: str) -> object:
        if name.startswith("__"):
            raise AttributeError(name)
        print(f"    [warn] 历史快照引用了已删除的 {module_name}.{name} → 哨兵顶替（非等价性差异）")
        return None

    shim.__getattr__ = _missing
    return shim


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
    # 快照执行期间换入宽容 shim，跑完立刻还原（否则会污染其后 `import scanner.display`）。
    saved = {m: sys.modules.get(m) for m in LEGACY_TOLERATED_MODULES}
    for m in LEGACY_TOLERATED_MODULES:
        sys.modules[m] = _shim_for_legacy(m)
    try:
        spec.loader.exec_module(mod)
    finally:
        for m, mod_or_none in saved.items():
            if mod_or_none is not None:
                sys.modules[m] = mod_or_none
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
    ap.add_argument("--rev", default=DEFAULT_BASELINE_REV, help=f"对比的 git rev（默认 {DEFAULT_BASELINE_REV}）")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    try:
        legacy_src = git_show(args.rev, "scanner/display.py")
    except RuntimeError as exc:
        print(f"[FAIL] {exc}")
        return 2

    legacy_defs, legacy_consts = collect_defs(ast.parse(legacy_src))

    if not legacy_defs:
        print(f"[FAIL] {args.rev}:scanner/display.py 里没有任何顶层 def —— 这几乎肯定是**拆分之后**")
        print("       的 re-export 聚合器，拿它当基线只会得到「旧：0 定义」，比对无意义。")
        print(f"       请指向拆分前的单体快照，默认基线 = {DEFAULT_BASELINE_REV}（拆分提交 {SPLIT_COMMIT} 的父提交）。")
        return 2

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

    # ── 检查 1：每个旧的顶层 def/class 必须存在且 AST 一致（已登记的有意分歧/删除除外） ──
    missing = sorted(set(legacy_defs) - set(new_defs))
    # 已登记的有意删除不算走样；登记了却还在 = 表过期。
    removed_decl = sorted(n for n in missing if n in EXPECTED_DEFINITION_REMOVAL)
    missing = sorted(n for n in missing if n not in EXPECTED_DEFINITION_REMOVAL)
    stale_removal = sorted(set(EXPECTED_DEFINITION_REMOVAL) - set(removed_decl))
    diverged = sorted(n for n in set(legacy_defs) & set(new_defs) if legacy_defs[n] != new_defs[n])
    changed = sorted(n for n in diverged if n not in EXPECTED_BODY_DIVERGENCE)
    declared_div = sorted(n for n in diverged if n in EXPECTED_BODY_DIVERGENCE)
    stale_div = sorted(set(EXPECTED_BODY_DIVERGENCE) - set(declared_div))
    if missing:
        failures.append(f"定义缺失 {len(missing)} 个：{missing}")
    if stale_removal:
        failures.append(
            f"EXPECTED_DEFINITION_REMOVAL 有 {len(stale_removal)} 项其实还在（表已过期）：{stale_removal}"
        )
    if changed:
        failures.append(f"定义体被改动 {len(changed)} 个（未登记为有意分歧）：{changed}")
    if stale_div:
        failures.append(f"EXPECTED_BODY_DIVERGENCE 有 {len(stale_div)} 项其实已不再与基线分歧（表已过期）：{stale_div}")

    # ── 检查 2：每个旧的模块级常量必须存在且值一致（已登记的有意变更除外） ──
    c_missing = sorted(set(legacy_consts) - set(new_consts))
    c_diverged = sorted(n for n in set(legacy_consts) & set(new_consts) if legacy_consts[n] != new_consts[n])
    c_changed = sorted(n for n in c_diverged if n not in EXPECTED_CONST_DIVERGENCE)
    declared_cdiv = sorted(n for n in c_diverged if n in EXPECTED_CONST_DIVERGENCE)
    stale_cdiv = sorted(set(EXPECTED_CONST_DIVERGENCE) - set(declared_cdiv))
    if c_missing:
        failures.append(f"常量缺失 {len(c_missing)} 个：{c_missing}")
    if c_changed:
        failures.append(f"常量值被改动 {len(c_changed)} 个（未登记为有意变更）：{c_changed}")
    if stale_cdiv:
        failures.append(
            f"EXPECTED_CONST_DIVERGENCE 有 {len(stale_cdiv)} 项其实已不再与基线分歧（表已过期）：{stale_cdiv}"
        )

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
            f"被消费的名字缺失 {len(used_missing)} 个（`from scanner.display import` 会 ImportError）：{used_missing}"
        )

    # 3b. 缩掉的面必须与白名单**精确**一致（双向）
    undeclared = sorted(dropped - EXPECTED_SURFACE_REDUCTION)
    over_declared = sorted(EXPECTED_SURFACE_REDUCTION - dropped)
    if undeclared:
        failures.append(f"有 {len(undeclared)} 个属性被缩掉但未登记进 EXPECTED_SURFACE_REDUCTION：{undeclared}")
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
    print(
        f"  已登记的有意分歧（拆分后功能迭代，不算走样）：{len(declared_div)} 个"
        + (f" {declared_div}" if declared_div else "")
    )
    print(
        f"  已登记的常量变更（拆分后功能迭代）：{len(declared_cdiv)} 个"
        + (f" {declared_cdiv}" if declared_cdiv else "")
    )
    print(
        f"  已登记的有意删除（拆分后功能迭代）：{len(removed_decl)} 个"
        + (f" {removed_decl}" if removed_decl else "")
    )
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
