"""展示层「入参接线」守护：调用方传的关键字必须被被调方接受。

**为什么要有这个文件**：2026-09-21 给 v1 主表加「新票优先」时，`display_priority` /
`build_scan_view` 都加上了 `new_symbols`，**唯独最外层入口 `display()` 漏加**，
而 `unified_scanner.run_scanner` 已经用 `new_symbols=new_syms` 调它。
结果：单测全绿（没有任何用例调 `display(..., new_symbols=)`）、
`mypy` 全绿（`display` 在 render.py 里没有类型标注、调用点又是 `**`-free 的普通调用）、
`ruff` 全绿，但**生产路径每一轮都抛 `TypeError: display() got an unexpected keyword
argument 'new_symbols'`**（见 logs/scanner_error.log）。实跑端到端才暴露。

这类「签名与调用点漂移」的缺口有共同特征：**被调方是运行期才解析的普通函数、调用方
用关键字传参、且没有测试穿过这条链路**。静态检查抓不到、单测又不覆盖，
所以在这里用 AST 把「调用点实际传的 kwargs」与「被调方真实签名」逐条对账。

守护是**派生式**的：不硬编码 `new_symbols` 之类的字段名，而是直接从源码里读调用点
（`unified_scanner.py` 的 `display(...)`、`render.py` 的 `display_priority(...)` /
`build_scan_view(...)`），再与实际签名比对 —— 以后再漏任何字段都会被拦下，
不需要回来补名单。
"""

import ast
import inspect
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent


def _call_kwargs(path: pathlib.Path, func_name: str) -> list[set[str]]:
    """解析文件，返回 `func_name(...)` 每次调用的关键字名集合（只认裸名字调用）。

    `mod.func(...)` 这类属性调用一律跳过：本文件只守护同模块内的直接调用
    （调用点与被调方都在仓库里、且用 `from ... import name` 引入）。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[set[str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == func_name:
            found.append({kw.arg for kw in node.keywords if kw.arg is not None})
    return found


def _assert_kwargs_accepted(caller: str, func_name: str, path: pathlib.Path, func) -> int:
    sig = inspect.signature(func)
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
        pytest.skip(f"{func_name} 接受 **kwargs，签名对账无意义")
    accepted = set(sig.parameters)
    sites = _call_kwargs(path, func_name)
    assert sites, f"{caller} 里找不到 {func_name}(...) 的调用点——守护已失效（函数被改名/删除？）"
    for i, kws in enumerate(sites, 1):
        extra = kws - accepted
        assert not extra, (
            f"{path.name} 第 {i} 处 {func_name}(...) 传了它不接受的参数：{sorted(extra)}。\n"
            f"  被调方 {func_name} 实际接受：{sorted(accepted)}\n"
            f"  —— 这正是 2026-09-21 那次 TypeError 的形态：调用点先改、被调方漏改。"
        )
    return len(sites)


def test_unified_scanner_display_kwargs_match_signature():
    """`run_scanner` 调 `display(...)` 传的每个关键字，`scanner.display.display` 都得接受。

    守护目标：`new_symbols` 那次漏加（生产每轮 TypeError）。
    """
    from scanner.display import display

    n = _assert_kwargs_accepted("unified_scanner.py", "display", REPO / "unified_scanner.py", display)
    assert n >= 1


def test_render_display_priority_kwargs_match_signature():
    """`render.display` 把入参透传给 `display_priority`，两边签名必须同构。"""
    from scanner.view.render import display_priority

    _assert_kwargs_accepted("render.py", "display_priority", REPO / "scanner" / "view" / "render.py", display_priority)


def test_render_build_scan_view_kwargs_match_signature():
    """`render.display_priority` 把入参透传给 `build_scan_view`，两边签名必须同构。"""
    from scanner.view.assemble import build_scan_view

    _assert_kwargs_accepted("render.py", "build_scan_view", REPO / "scanner" / "view" / "render.py", build_scan_view)


def test_display_accepts_new_symbols_end_to_end(capsys):
    """行为兜底：真的用 `new_symbols=` 调一次 `display()`，不得抛 TypeError。

    与上面三条的结构守护互补——结构守护断言「签名对得上」，本条断言「调用真的能跑」。
    `conn=None` 时 build_scan_view 返回 None，故只走头部打印，不碰数据库。
    该仓库此前有「Edit 未落盘」的病史（外部进程覆盖文件），结构守护读的是磁盘源码、
    本条读的是运行期对象，两者同时成立才算接线成功。
    """
    from scanner.display import display

    assert display(0, 300, conn=None, new_symbols={"SZ300001"}) is None
    out = capsys.readouterr().out
    assert "创业板飙升榜监控" in out  # 头部确实画了 → 函数体真的执行到了
