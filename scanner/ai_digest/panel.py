"""`股票池.md` 面板原文 → **结构化两区行**（2026-11-11）。

## 为什么有这个模块

`ai_digest` 原来的两种输入都拿不到「用户屏幕上那份列表」：

- `--source print/view/log`：只覆盖**榜外异动**（B 段）。`offboard_print_snapshot` /
  `offboard_launch_log` 的列就是榜外列，`view_snapshot` 也只读 `regions["offboard"]`
  ⇒ `v1 池选` 那 20+ 行进不来。
- `--clipboard` / `--symbols`：能覆盖两区，但 `extract_symbols_from_text` **只抽 6 位码**
  ⇒ 涨幅 / 5日累计 / 现价 / 排名 / 板块 / 类别·先验 / 层 / 量比 / 换手 全部丢弃。
  模型只剩「代码 + 名字」，研究的就不再是「你看到的那份列表」。

本模块补的就是这一环：把 `panel_md.save_panel_round()` 每轮整份覆盖的那份面板原文，
按**渲染时的列规格**逐列切回去。输入是终端**已经打印出去**的文本，不是重算。

## 解析方式：按列宽切，不按空格切

面板行由 `view/render._table_row` 生成：`"  " + " ".join(_pad(cell, w, align))` ——
单元格内部可以含空格（名称、板块都可能），按空白 `split()` 会切错。
故这里**直接用渲染方的列规格**（`COLS_POOL` / `COLS_HOT` 的宽度与对齐）按可见宽度
（`wcwidth`，中文 2 列）切格 ⇒ 与渲染严格互为逆运算，不另立一份列定义。

## 三区归属（区名必须保留）

`v1 池选` 与 `沪深飙升` 三段的**排序口径互不相同**，混成一个扁平清单会丢掉
「它凭什么在这张表里」：

- `v1 池选`：新票优先·类别优先·排名升序·资金流降序（列含 `类别·先验` = 系统唯一口径）
- `榜内飙升`：评分↓（排名上升 35/涨幅 25/价格 15/量能 25）
- `榜内异动` / `榜外异动`：T2 启动首日 → T1 量先动 → 量比 → 主力净占比（**未回测**）

故产物按区分组，每组带自己的口径文案（取自面板段标题原文，不在这里重写一份）。

## 纪律

1. **只读**：本模块零 I/O 写、零网络、零时钟 —— 传文本进、结构化行出。
2. **不猜**：解析不出来的行（宽度对不上/代码形状不对）**丢弃并计数上报**，不硬塞。
3. **原样**：字段值只做 `strip()`，不做单位换算、不重排、不去重。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from scanner.utils import EXTERNAL_FAILURES
from scanner.view.model import COLS_HOT, COLS_POOL

__all__ = [
    "PanelRegion",
    "PanelSnapshot",
    "load_panel_md",
    "parse_panel_text",
    "parse_panel_md",
]

#: 代码形状（面板里池选带 SH/SZ 前缀，榜外/榜内是裸 6 位）。
_CODE_RE = re.compile(r"^(?:(SH|SZ|BJ))?(\d{6})$", re.I)

#: `## 快照 YYYY-MM-DD HH:MM` 小节标题。
_SNAPSHOT_HEAD_RE = re.compile(r"^##\s*快照\s+(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})")

#: 区标题：`◆ v1 池选 …` / `◆ 沪深飙升 · …`。段标题：`— 榜外异动（…）`。
_REGION_RE = re.compile(r"^\s*◆\s*(.+)$")
_SEGMENT_RE = re.compile(r"^\s*—\s*(\S+?)\s*[（(](.*)[)）]\s*$")

#: 区/段 → 列规格。`沪深飙升` 三段共用 `COLS_HOT`（列集不分叉）。
_POOL_KEYS = ("v1 池选", "回捞")
_HOT_KEYS = ("沪深飙升",)
_HOT_SEGMENTS = ("榜内飙升", "榜内异动", "榜外异动")

#: 数据行：前置缩进 + 序号 + 非空。只有这种行才会被尝试逐列解析。
_DATA_ROW_RE = re.compile(r"^\s{1,6}\d{1,3}\s+\S")

#: 行尾标记（不在列规格里，渲染时列在最后一列之后，例如 `稳` / `先强资弱` / `⚡`）。
_TRAILING_MARKS = ("先强资弱", "稳", "妖", "⚡", "▼", "★", "⚠")


def _vis_width(s: str) -> int:
    """终端可见宽度（中文全角 2 列）。复用渲染侧同一依赖，不手写区间表。"""
    import wcwidth

    return sum(max(0, wcwidth.wcwidth(c)) for c in s)


def _cells(line: str, spec: tuple[tuple[str, int, str], ...]) -> tuple[list[str], str] | None:
    """按列规格把一行切成 `(cells, 行尾余量)`；宽度对不上返回 None。

    严格复刻 `view/render._table_row` 的布局：列前置两格缩进，列间单个空格。
    任何一列内容越界（本该是空格的位置出现非空格）即判该行不可解析 —— 宁可丢弃并
    上报，也不把错位数据塞给模型。
    """
    if not line.startswith("  "):
        return None
    idx = 2
    out: list[str] = []
    for _name, width, _align in spec:
        start, acc = idx, 0
        while idx < len(line):
            cw = max(0, _vis_width(line[idx]))
            if acc + cw > width:
                break
            acc += cw
            idx += 1
        out.append(line[start:idx])
        if idx < len(line):
            if line[idx] != " ":
                return None  # 列界处不是留白 ⇒ 整行错位，不猜
            idx += 1
    return out, line[idx:].strip()


def _code(raw: str) -> str | None:
    """面板代码单元格 → `SZ300310`；形状不对返回 None。"""
    m = _CODE_RE.match(raw.strip())
    if not m:
        return None
    prefix, digits = m.group(1), m.group(2)
    if prefix:
        return prefix.upper() + digits
    if digits[0] in "03":
        return "SZ" + digits
    if digits[0] == "6":
        return "SH" + digits
    return None


@dataclass(frozen=True)
class PanelSnapshot:
    """一次面板快照的结构化产物。"""

    date: str
    """快照日期（`YYYY-MM-DD`），来自 `## 快照` 小节标题。"""

    time: str
    """快照时刻（`HH:MM`，面板原文只有分钟精度）。"""

    regions: tuple[PanelRegion, ...] = ()
    """按面板顺序的分区（每区自带口径原文与列名）。"""

    dropped: int = 0
    """宽度对不上/代码形状不对而被丢弃的行数（**必须如实上报**，不静默）。"""

    @property
    def total(self) -> int:
        return sum(len(r.rows) for r in self.regions)


@dataclass(frozen=True)
class PanelRegion:
    """一个区（或区内一段）。"""

    title: str
    """`◆` 区标题原文，如 `v1 池选 — 新票优先·类别优先·排名升序·资金流降序`。"""

    columns: tuple[str, ...]
    """该区的列名（与行内 cells 逐项同序）。"""

    rows: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    """每行 `{字段名: 原文}` + `symbol` / `name` / `marks`（行尾标记）。"""

    note: str = ""
    """该段口径原文（市场/层级/排序键/未回测等），取自面板，不在本模块重写。"""


def limit_per_region(snap: PanelSnapshot, limit: int | None) -> PanelSnapshot:
    """`--limit N` → 每区取前 N 只（单源：CLI 显示侧与送进模型的截断必须同一口径）。

    按**每区**而不是全局截断：`v1 池选` 与 `沪深飙升` 三段是并列的独立观察段，
    全局截断会让靠后的区整段消失（那一段的存在本身是信息）。
    """
    if not limit or limit <= 0:
        return snap
    return PanelSnapshot(
        date=snap.date,
        time=snap.time,
        dropped=snap.dropped,
        regions=tuple(
            PanelRegion(title=r.title, columns=r.columns, rows=r.rows[:limit], note=r.note)
            for r in snap.regions
            if r.rows
        ),
    )

def parse_panel_text(text: str) -> PanelSnapshot | None:
    """面板原文（含 ```text 代码块）→ 结构化快照；找不到表格返回 None。"""
    lines = text.splitlines()

    date = time = ""
    for line in lines:
        m = _SNAPSHOT_HEAD_RE.match(line.strip())
        if m:
            date, time = m.group(1), m.group(2)
            break

    regions: list[PanelRegion] = []
    dropped = 0

    # 当前上下文：区标题 / 段标题 / 列规格 / 已收集的行
    cur_title = ""
    cur_note = ""
    cur_spec: tuple[tuple[str, int, str], ...] | None = None
    cur_cols: tuple[str, ...] = ()
    cur_rows: list[dict[str, Any]] = []

    def flush() -> None:
        nonlocal cur_title, cur_note, cur_spec, cur_cols, cur_rows
        if cur_spec is not None and cur_rows:
            regions.append(
                PanelRegion(
                    title=cur_title or "（未标注区名）",
                    columns=cur_cols,
                    rows=tuple(cur_rows),
                    note=cur_note,
                )
            )
        cur_title, cur_note, cur_spec, cur_cols, cur_rows = "", "", None, (), []

    in_block = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            if in_block:
                break  # 代码块结束，面板正文到头
            in_block = True  # 进入 ```text 代码块，面板正文从这里开始
            continue
        if not in_block:
            continue  # 卷头说明（标题/更新方式/数据来源）不是面板正文

        rm = _REGION_RE.match(line)
        if rm:
            flush()
            cur_title = rm.group(1).strip()
            continue

        sm = _SEGMENT_RE.match(line)
        if sm and any(k in cur_title for k in _HOT_KEYS):
            # ⚠ 先取区名再 flush：flush() 会把 cur_title 清空，先算后清才不丢区名。
            region_head = cur_title.split("（")[0].strip()
            flush()
            seg = sm.group(1).strip()
            # 段标题自带口径：保留区名 + 段名，口径原文进 note。
            cur_title = f"{region_head} / {seg}"
            cur_note = sm.group(2).strip()
            continue

        # 表头行：首列是 `#` ⇒ 决定本段用哪份列规格（列宽/列名都来自渲染侧单源）。
        if stripped.startswith("#") and not stripped.startswith("##"):
            if any(k in cur_title for k in _HOT_KEYS):
                cur_spec, cur_cols = COLS_HOT, tuple(c[0] for c in COLS_HOT)
            elif cur_title:
                cur_spec, cur_cols = COLS_POOL, tuple(c[0] for c in COLS_POOL)
            else:
                continue
            cur_rows = []
            continue

        # 数据行：首列是序号。**只有长得像数据行却解析失败才计 dropped** ——
        # 口径说明/分隔线/飞书过门那几行本来就不是表格行，算进 dropped 会把
        # 「解析器坏了」和「这行不是数据」混成一个数，告警就没人看了。
        if cur_spec is None or not _DATA_ROW_RE.match(line):
            continue
        parsed = _cells(line, cur_spec)
        if parsed is None:
            dropped += 1
            continue
        cells, trailing = parsed
        sym = _code(cells[1])
        if sym is None:
            dropped += 1
            continue
        row: dict[str, Any] = {"symbol": sym, "name": cells[2].strip()}
        for name, cell in zip(cur_cols, cells, strict=True):
            if name in ("#", "代码", "名称"):
                continue
            row[name] = cell.strip()
        row["marks"] = trailing
        cur_rows.append(row)

    flush()
    if not regions:
        return None
    return PanelSnapshot(date=date, time=time, regions=tuple(regions), dropped=dropped)


def parse_panel_md(path: str | Path) -> PanelSnapshot | None:
    """读 `股票池.md` 并解析。文件不存在/不可读 → None（fail-open，由调用方报）。"""
    p = Path(path)
    try:
        return parse_panel_text(p.read_text(encoding="utf-8"))
    except EXTERNAL_FAILURES:
        return None


def load_panel_md() -> tuple[PanelSnapshot | None, str]:
    """按项目单源取存档路径（`RTS_PANEL_MD_PATH` 优先）→ `(快照, 路径文案)`。"""
    from scanner.panel_md import panel_md_path

    path = panel_md_path()
    return parse_panel_md(path), str(path)
