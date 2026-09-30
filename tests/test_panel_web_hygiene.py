"""前端**代码卫生**断言（T1.2 / T1.3 验收的 CI grep 部分）。

这些是**结构**断言，不是行为测试：它们防的是「约定被悄悄破掉」，而这类破坏
不会让任何功能测试变红 —— 直到某天线上出现一个绕过收口的请求。

`web/src` 的 TypeScript 行为测试另见 `web/src/api/client.test.ts`（vitest）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

WEB_SRC = Path(__file__).resolve().parents[1] / "rts-panel-cloud" / "web" / "src"

pytestmark = pytest.mark.skipif(
    not WEB_SRC.exists(),
    reason="前端工程 rts-panel-cloud/web 未检出",
)

# 裸 fetch：不在 client.ts 里的 fetch 调用违反「唯一网络收口」。
_FETCH_RE = re.compile(r"(?<![\w.])fetch\s*\(")
# 块注释 /* */ 与 JSDoc。
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)
# 整行行注释（只认行首的 //，避免把 "https://" 里的 // 当注释）。
_LINE_COMMENT_RE = re.compile(r"^\s*//.*$", re.M)

# ⚠ 必须同时扫 .ts 与 .tsx：`Path("App.tsx").suffix` 是 ".tsx" 而非 ".ts"，
#   只 glob "*.ts" 会把**整个组件层**静默漏掉 —— 守卫对最该管的文件完全失明，
#   却照样报绿（2026-09-30 变异测试实测：注入 App.tsx 的裸 fetch 曾被漏过）。
_SUFFIXES = (".ts", ".tsx")


def _ts_files() -> list[Path]:
    return sorted(
        p
        for p in WEB_SRC.rglob("*")
        if p.is_file() and p.suffix in _SUFFIXES and not p.name.endswith((".test.ts", ".test.tsx"))
    )


def _code_only(text: str) -> str:
    """去掉注释，只留代码。

    为什么要去注释：注释里出现 `fetch(` 往往只是**在讲这个禁令**
    （例：App.tsx 的落点表就写着「组件内禁止裸 fetch」），把它算成违规会让
    守卫对自己的文档开火。而「把真代码藏进注释里」本来就不是风险 —— 注释不执行。
    """
    return _LINE_COMMENT_RE.sub("", _BLOCK_COMMENT_RE.sub("", text))


def test_web_src_exists():
    """先确认工程在，否则下面所有 grep 都会「因为没文件」而空转通过。"""
    assert WEB_SRC.exists(), f"前端源码目录不存在：{WEB_SRC}"
    assert _ts_files(), "web/src 下没有任何 .ts/.tsx 文件，grep 断言会空转"


def test_guard_actually_scans_component_files():
    """防「守卫空转」：断言扫描列表里**真的有 .tsx 组件**。

    背景：曾因只 glob `*.ts`（`App.tsx` 的 suffix 是 .tsx）而漏掉整个组件层，
    守卫对最该管的文件完全失明。**变异测试两次都是先靠这条才发现问题的**，
    故它必须在场，而不是等下一次漏网。
    """
    names = {p.name for p in _ts_files()}
    assert "App.tsx" in names, f"扫描列表漏掉组件层，守卫已失明：{sorted(names)}"
    assert "client.ts" in names, f"收口文件本身未纳入扫描：{sorted(names)}"


def test_no_bare_fetch_outside_client():
    """**唯一网络收口**（design §2.2 / T1.2 验收）：除 client.ts 外 0 处裸 fetch。"""
    offenders: list[str] = []
    for p in _ts_files():
        if p.name == "client.ts":
            continue
        code = _code_only(p.read_text(encoding="utf-8"))
        for i, line in enumerate(code.splitlines(), 1):
            if _FETCH_RE.search(line):
                offenders.append(f"{p.relative_to(WEB_SRC)}:{i}: {line.strip()}")
    assert not offenders, "禁止裸 fetch（一律走 api/client.ts）：\n" + "\n".join(offenders)


def test_client_is_the_only_network_module():
    """收口只此一个：不允许出现第二个 http 封装旁路。"""
    for p in _ts_files():
        if p.name in ("client.ts", "types.ts"):
            continue
        code = _code_only(p.read_text(encoding="utf-8"))
        assert "XMLHttpRequest" not in code, f"{p.name} 不得绕过收口直接用 XMLHttpRequest"


def test_table_components_have_no_hardcoded_column_labels():
    """列头必须来自云端下发的 colSpec（D7 / T1.3 验收），组件里不得写死列名。

    作用域**只限 components/** 下的表格渲染文件：区块标题里的固定文案
    （如「新票优先·类别优先·**排名**升序·资金流降序」）是设计规定的展示文本，
    不是列定义，拿它当违规会误报。当前 components/ 尚不存在（T1.4 才建），
    故此条现在等于「有则必查」的前置守卫。
    """
    comps = sorted((WEB_SRC / "components").rglob("*")) if (WEB_SRC / "components").is_dir() else []
    comps = [p for p in comps if p.is_file() and p.suffix in _SUFFIXES]
    forbidden = ["涨幅", "5日累计", "现价", "板块", "评分", "策略", "量比", "换手%", "市值(亿)", "连击", "排名上升"]
    offenders: list[str] = []
    for p in comps:
        code = _code_only(p.read_text(encoding="utf-8"))
        for word in forbidden:
            if word in code:
                offenders.append(f"{p.relative_to(WEB_SRC)}: 硬编码列名 {word!r}")
    assert not offenders, "列头必须来自云端 colSpec（D7）：\n" + "\n".join(offenders)
