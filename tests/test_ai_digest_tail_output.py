"""research 模式**尾部输出**的守卫（2026-11-12）。

## 问题

不加 `--brief` 时，research 模式会在报告后打两大块**完全无用**的内容：

```
素材核对（模型看到的初始数据；逐行可对）：
  SZ300030 阳普医疗 | 暂无数据 | 涨幅暂无数据 | 量比暂无数据 | 主占暂无数据 | ...
  （14 只 × 10 个字段，全是「暂无数据」）

缺失项（素材里是「暂无数据」，模型不得补）：
  SZ300030.tier、SZ300030.percent、SZ300030.accum_5d、... （一百多项）
```

## 根因

这两块是**为 analyze / transcribe 写的**：那两个模式的输入是 B 段（榜外异动）素材，
字段是 `tier` / `percent` / `vol_ratio` / `main_pct` / `accum_5d` / `price` / `first_time`。

而 research 的输入是**面板原文**或自选清单 —— **两套字段根本不是一回事**，
`render_materials_block` 必然读空 ⇒ 打出来全是「暂无数据」，再配一行一百多项的
「缺失项」。读者看到两屏空字段，只会以为数据取不到。

## 修法

research 模式改打**模型真正看到的东西**：本地特征块（与提示词同源，逐行可对）。
这也是「素材核对」在 research 语境下的正确对应物。
"""

from __future__ import annotations

from pathlib import Path

CLI = Path(__file__).resolve().parent.parent / "scanner" / "ai_digest" / "__main__.py"

#: 尾部输出的定位锚：长报告头部那一行数据源描述（brief 分支少了「（段内顺序与终端同源）」，
#: 故全文件唯一）。刻意不绑 `print(...)` —— 头部可以逐行 print，也可以先组装成一份文本
#: 再打印（还要给 `--out` 复用），守的是**后面的分支内容**，不是打印写法。
_TAIL_ANCHOR = 'f"数据源 {source_desc} · 共 {total} 行 · 本次 {shown} 只（段内顺序与终端同源）"'


def _src() -> str:
    # 统一换行符：仓库在 Windows 上是 CRLF，用 `\n` 找子串会找不到。
    return CLI.read_text(encoding="utf-8").replace("\r\n", "\n")


def _research_branch() -> str:
    """截出尾部输出的 research 分支（`if args.mode == "research":` → `else:`）。"""
    src = _src()
    anchor = src.find(_TAIL_ANCHOR)
    assert anchor > 0, "找不到尾部输出"
    idx = src.find('if args.mode == "research":', anchor)
    assert idx > 0, "找不到 research 分支"
    end = src.find("\n    else:", idx)
    assert end > idx, "找不到 research 分支的 else 结尾"
    return src[idx:end]


def _non_research_branch() -> str:
    """截出 `else:`（analyze/transcribe）分支。"""
    src = _src()
    anchor = src.find(_TAIL_ANCHOR)
    assert anchor > 0, "找不到尾部输出"
    idx = src.find("\n    else:", src.find('if args.mode == "research":', anchor))
    assert idx > 0, "找不到 else 分支"
    return src[idx : idx + 900]


def test_research_branch_does_not_render_materials_block() -> None:
    """research 分支不得调用 `render_materials_block`。

    那个函数读的是 B 段素材字段，与面板源不是一套 —— 调用它必然打出满屏「暂无数据」。
    """
    branch = _research_branch()
    # ⚠ 只查**实际调用**，不查文本提及 —— 注释里会解释「为什么不用它」，
    #   裸查子串会把注释也算成违规（本次就踩到了）。
    calls = [
        ln
        for ln in branch.splitlines()
        if "render_materials_block" in ln and not ln.lstrip().startswith("#")
    ]
    assert not calls, f"research 分支不得调用素材核对表（字段不对）：{calls}"
    assert "mats.missing" not in branch, "research 分支不得打 B 段缺失项清单"


def test_research_branch_prints_features() -> None:
    """research 分支要打**模型实际看到的本地特征**（与提示词同源）。"""
    branch = _research_branch()
    assert "render_feature_block" in branch, "research 分支应打本地特征块"
    assert "_feats" in branch, "应基于实际传给提示词的 _feats（保证与模型所见同源）"


def test_non_research_branch_keeps_materials() -> None:
    """analyze / transcribe 的素材核对表**要保留** —— 它们的字段是对的。"""
    tail = _non_research_branch()
    assert "render_materials_block" in tail
    assert "mats.missing" in tail


def test_feature_block_title_line_is_not_duplicated() -> None:
    """特征块的标题行不该重复打（外层已打「模型看到的本地特征」）。"""
    branch = _research_branch()
    assert "_block[1:]" in branch, "应跳过特征块的首行标题（外层已打过标题）"
