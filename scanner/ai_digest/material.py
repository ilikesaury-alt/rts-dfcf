"""榜外异动段的**确定性素材组装**（2026-11-03）。

本模块是 AI 转述的**事实源边界**：零 I/O、零时钟、零随机 —— 同输入 → 字节级同输出。
模型看到的只有这里产出的 items，它没有别的输入（ADR 0001 第一道闸）。

## 数据口径（三条必须先读）

1. **值取自 `offboard_launch_log`，那是「信号值首次写入即冻结」的快照**
   （`offboard_watch.persist_round`）。所以 `信号时涨幅` 列显示的是**该票当初以什么身份
   被推出**的涨幅，不是当前价 —— 素材里的 label 就这么写，不做「看起来更新」的美化。
   这与 2026-10-14 `_apply_live_quote` 那条故障的教训同源：同一行里现价与涨幅必须
   同刻，否则读者会以为算错了。本模块一律用冻结值，不混入任何实时量。
2. **段内顺序与终端同源**：直接用 `offboard_watch.sort_key`（T2 在前 → 量比降序 →
   主力净占比降序），不在这里写第二份排序口径。
3. **次日收益（`next_day_pct`）刻意不进素材**：它是绩效读数，模型一转述就会变成
   「这个信号有效 / 无效」的判断 —— 而那正是 `scripts/offboard_nextday.py` 刻意
   拒绝下结论的东西（那份报告连「建议」二字都不许出现）。要做绩效复盘请跑那个脚本。

## 两种模式（2026-11-03 用户决策）

- `transcribe`：纯转述（云端 `/api/ai-explain` 同款纪律，过全部红线槽位）；
- `analyze`（**默认**）：综合分析 + 筛选值得关注/买入候选。这是用户明确要求打破
  「第二结论源」纪律后的产物（个人使用；决策记录见 AGENTS.md），因此：① 字段表
  多给板块/资金流/换手/形态/入选理由；② **不过红线词表**（拦了就没法给候选）；
  ③ 保留唯一一道确定性检查 —— **幻觉巡检**（正文数字必须能在素材里找到，
  `unverified_numbers` 非空就明打出来）。

## 缺失即「暂无数据」

拿不到的字段写 `暂无数据`，**不让模型补**（「不得编造」的落地点）。缺失项同时登记进
`missing`，供输出侧如实披露「模型没收到哪些数据」。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from scanner.offboard_watch import OffboardCandidate, sort_key

__all__ = [
    "NA",
    "SEGMENT_NOTE",
    "TIER_LABEL",
    "MaterialItem",
    "OffboardMaterials",
    "build_materials",
    "render_material_items",
    "render_materials_block",
    "unverified_numbers",
]

NA = "暂无数据"

#: 层级词表。⚠ 任何出现裸 `T1`/`T2` 的地方都必须带这张词表（AGENTS.md 2026-10-13 第 3 条），
#: 否则屏幕上只有一个无从解读的 `T1`。
TIER_LABEL: dict[str, str] = {
    "T1": "T1 量先动·价未动",
    "T2": "T2 启动首日",
}

#: 本段口径（作为一条素材发给模型，避免它自己发明「榜外异动是什么」）。
SEGMENT_NOTE = (
    "榜外异动段（B 段）：创业板榜外票的两层观察段，层级只有两层 —— "
    "T1 量先动·价未动（当日涨幅低于启动线）、T2 启动首日（当日涨幅达启动线且量能放大）；"
    "本段不给操作建议，不参与复合评分/档位/排名，其阈值尚无历史背书。"
)

#: 字段白名单：`(列名, 中文标签, 格式)`。列名即 `offboard_launch_log` 的列名。
_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("name", "名称", "text"),
    ("tier", "层", "tier"),
    ("percent", "信号时涨幅（首次推出时冻结）", "pct"),
    ("accum_5d", "5日累计", "pct"),
    ("vol_ratio", "量比", "dec2"),
    ("main_pct", "主力净占比", "pct"),
    ("amount", "成交额(亿)", "yi"),
    ("float_cap", "流通市值(亿)", "yi"),
    ("price", "价格", "price"),
    ("first_time", "首次入选", "time"),
)

#: analyze 模式附加字段：视图行才有的量（launch_log 源没有时显「暂无数据」）。
_ANALYZE_EXTRA: tuple[tuple[str, str, str], ...] = (
    ("sector", "板块", "text"),
    ("ff_pct", "主力净占比(实时)", "pct"),
    ("turnover_rate", "换手%", "dec2"),
    ("beauty", "形态标签", "text"),
    ("reasons", "入选理由", "text"),
)


@dataclass(frozen=True)
class MaterialItem:
    """一条素材。`id` 是稳定键（输出侧与测试都按它取项），`label`/`value` 直接展示。"""

    id: str
    label: str
    value: str


@dataclass(frozen=True)
class OffboardMaterials:
    date: str
    items: tuple[MaterialItem, ...]
    missing: tuple[str, ...]
    symbols: tuple[str, ...]
    #: 多区域合并时的分区表：`((区域标签, 该区的代码元组), ...)`。
    #: 单区域时留空 ⇒ `render_materials_block` 走老路径（`symbols` + 固定列），
    #: 输出与合并前**逐字一致**（不因为并入新区域而改动既有区域的呈现）。
    groups: tuple[tuple[str, tuple[str, ...]], ...] = ()
    #: 本区的素材 id 前缀（`sb` = 榜外段 / `cs` = 横截面层）。
    #: ⚠ **必须有**：核对视图按前缀找数据 —— 前缀不对 ⇒ 整张表「暂无数据」。
    #:   实测踩过：横截面区单跑时核对表全空（`build_materials` 默认 `sb`，
    #:   而素材项是 `cs.` 前缀 ⇒ 一行都取不到 ⇒ 核对能力形同虚设）。
    prefix: str = "sb"


def merge_materials(parts: Sequence[tuple[str, OffboardMaterials]]) -> OffboardMaterials:
    """多个区域的素材合并成一份（2026-11-16：横截面层并入统一 AI 分析）。

    ⚠ **合并的是「区域」，不是「把两只票的字段混在一行」**：
    B 段（榜外异动）与横截面层的字段集根本不同（tier/主力占比 vs 1m/3m/分位/判据），
    硬拼成一行会出现大半字段「暂无数据」，读者与模型都无法读。
    ⇒ 合并后仍是**两个分区**，各自一行一只票、各自的列；提示词里以分区小标题隔开。

    合并后的 `symbols` = 各区代码按顺序去重（供旧路径与 JSON 输出使用）。
    """
    if not parts:
        raise ValueError("merge_materials 需要至少一个区域")
    items: list[MaterialItem] = []
    missing: list[str] = []
    symbols: list[str] = []
    groups: list[tuple[str, tuple[str, ...]]] = []
    for label, m in parts:
        items.extend(m.items)
        missing.extend(m.missing)
        symbols.extend(m.symbols)
        groups.append((label, m.symbols))
    return OffboardMaterials(
        date=parts[0][1].date,
        items=tuple(items),
        missing=tuple(missing),
        symbols=tuple(dict.fromkeys(symbols)),
        groups=tuple(groups),
        # 合并后各区前缀不同 ⇒ 单区路径不适用（走 groups 分支）。
        prefix="",
    )


def _num(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int | float):
        f = float(v)
        return f if f == f and f not in (float("inf"), float("-inf")) else None
    if isinstance(v, str) and v.strip():
        try:
            f = float(v)
        except ValueError:
            return None
        return f if f == f and f not in (float("inf"), float("-inf")) else None
    return None


def _fmt(kind: str, v: Any) -> str:
    """数值格式化：**固定小数位**（不依赖 locale/时区），保证确定性。"""
    if kind == "text":
        if isinstance(v, str) and v.strip():
            return v.strip()
        return NA
    if kind == "tier":
        s = v.strip() if isinstance(v, str) else ""
        # 词表里没有的层值原样带出并标注（不猜它是哪一层）。
        return TIER_LABEL.get(s, f"{s}（未登记层）" if s else NA)
    if kind == "time":
        # `first_time` 是 ISO 串（`2026-11-02T09:45:44+08:00`）⇒ 只取时刻部分，
        # 日期已由「信号日」那一条素材统一给出（同一行重复三个日期只会浪费 token）。
        s = v.strip() if isinstance(v, str) else ""
        if not s:
            return NA
        return s.split("T", 1)[1][:8] if "T" in s else s
    n = _num(v)
    if n is None:
        return NA
    if kind == "pct":
        return f"{n:+.2f}%"
    if kind == "price":
        return f"{n:.2f}"
    if kind == "dec2":
        return f"{n:.2f}"
    if kind == "dec3":
        return f"{n:.3f}"
    if kind == "int":
        # 整数展示：`14.0` → `14`（持续轮次是计数，带 .0 是噪声）。
        return str(int(round(n)))
    if kind == "yi":
        return f"{n / 1e8:.2f}"
    return f"{n}"


def _bare_code(symbol: str) -> str:
    """`SZ300758` → `300758`（其余原样；只用于构造 `OffboardCandidate` 的 code 字段）。"""
    return symbol[2:] if len(symbol) == 8 and symbol[:2] in ("SH", "SZ", "BJ") else symbol


def _order_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    """段内顺序 = 终端顺序（复用 `offboard_watch.sort_key`，末尾再按 symbol 兜底定序）。

    不在这里重复写「T2 在前 / 量比降序」—— 那是 B 段的排序口径，第二份定义必然漂移。
    """
    symbol = str(row.get("symbol") or "")
    cand = OffboardCandidate(
        symbol=symbol,
        code=_bare_code(symbol),
        name=str(row.get("name") or ""),
        tier=str(row.get("tier") or "").strip(),
        current=_num(row.get("price")) or 0.0,
        percent=_num(row.get("percent")) or 0.0,
        accum_5d=_num(row.get("accum_5d")) or 0.0,
        volume_ratio=_num(row.get("vol_ratio")) or 0.0,
        main_pct=_num(row.get("main_pct")) or 0.0,
    )
    return (*sort_key(cand), symbol)


def build_materials(
    date: str,
    rows: Sequence[Mapping[str, Any]],
    *,
    limit: int | None = None,
    mode: str = "analyze",
    market: Mapping[str, Any] | None = None,
    baseline: str | None = None,
    fields: Sequence[tuple[str, str, str]] | None = None,
    segment_note: str | None = None,
    id_prefix: str = "sb",
    sort_key: Any = None,
    with_market: bool = True,
) -> OffboardMaterials:
    """源行 → 素材（纯函数）。

    `limit` 在**排序之后**截断（保证截出来的是终端会显示的前 N 行，而不是数据库前 N 行）。
    `mode`：`analyze`（默认）附加板块/资金流/形态/入选理由；`transcribe` 只用冻结信号字段。
    `market`：analyze 模式下附一条当轮市况（来自同一份快照的 `ctx`，不是模型自己猜的）。
    `baseline`：analyze 模式下附一条**本区历史分层命中率**（`source.render_baseline_text`
    的产物，948 条标签实测）。这是「模型自己调研」的替代实现 —— 进程内的模型没有联网、
    没有 tool，唯一能给它真证据的方式就是把项目自己的统计当素材喂进去。

    ⚠ **多区域支持（2026-11-16：横截面层并入）**：
    - `fields`：本区的字段白名单（`(列名, 中文标签, 格式)`）。不给则用 B 段默认集。
    - `segment_note`：本区口径说明（模型据此知道「这是什么区域」）。
    - `id_prefix`：素材 id 前缀（`sb` = 榜外段；`cs` = 横截面层）。**必须不同**，
      否则两区同代码的票会互相覆盖。
    - `sort_key`：排序键（默认 `_order_key`）。横截面层按「持续轮次降序」排。
    - `with_market`：是否附市况条目（B 段有，横截面层独立 ⇒ 不附）。
    """
    ordered = sorted(rows, key=sort_key or _order_key)
    if limit is not None:
        ordered = ordered[: max(limit, 0)]

    if fields is None:
        fields = _FIELDS + (_ANALYZE_EXTRA if mode == "analyze" else ())

    items: list[MaterialItem] = [
        MaterialItem(id="segment.note", label="本段口径", value=segment_note or SEGMENT_NOTE),
        MaterialItem(id="segment.count", label="本次只数", value=f"{len(ordered)} 只"),
    ]
    if mode == "analyze" and baseline:
        items.append(
            MaterialItem(
                id="baseline.stats",
                label="本区历史分层命中率（本项目 948 条标签实测，非市场先验）",
                value=baseline,
            )
        )
    if mode == "analyze" and with_market and market is not None:
        idx = market.get("market_idx_pct")
        weak = market.get("weak")
        items.append(
            MaterialItem(
                id="ctx.market",
                label="当轮市况",
                value=(
                    f"创业板指 {idx:+.2f}%；市况标记：{'弱市' if weak else '非弱市'}"
                    if isinstance(idx, int | float)
                    else NA
                ),
            )
        )
    missing: list[str] = []
    symbols: list[str] = []

    for row in ordered:
        symbol = str(row.get("symbol") or "").strip()
        if not symbol:
            # 没有代码的行无法被核对，也不该进素材（正常库里不可能出现）。
            continue
        symbols.append(symbol)
        for key, label, kind in fields:
            value = _fmt(kind, row.get(key))
            if value == NA:
                missing.append(f"{symbol}.{key}")
            items.append(
                MaterialItem(
                    id=f"{id_prefix}.{symbol}.{key}",
                    label=f"第{len(symbols)}只·{label}",
                    value=value,
                )
            )

    return OffboardMaterials(
        date=date,
        items=tuple(items),
        missing=tuple(missing),
        symbols=tuple(symbols),
        prefix=id_prefix,
    )


def render_materials_block(m: OffboardMaterials) -> str:
    """素材的**人可核验**紧凑视图：一只票一行（顺序与素材、与终端一致）。

    为什么要打出来：转述的价值前提是「人能把文案和数字对上」。云端 UI 用可折叠表格做这件事，
    终端没有折叠，就用一行一只票 —— 读者一眼能发现「解读说的量比和这行不一样」。

    ⚠ **多区域（2026-11-16）**：`m.groups` 非空时按区渲染，每区一套列。
    单区域（`groups` 为空）走老路径，输出与合并前**逐字一致**。
    """
    if not m.groups:
        # ⚠ 用 `m.prefix`（不是写死 "sb"）—— 否则横截面区单跑时整张表取不到数据。
        _cols = _COLUMNS_BY_PREFIX.get(m.prefix, _SB_COLUMNS)
        return _render_segment_rows(_cols, m.prefix, m.symbols, m.items)

    lines: list[str] = []
    for label, symbols in m.groups:
        prefix = "sb" if label.startswith("榜外") else "cs"
        lines.append(f"【{label}】")
        lines.append(_render_segment_rows(_COLUMNS_BY_PREFIX[prefix], prefix, symbols, m.items))
    return "\n".join(lines)


#: B 段（榜外异动）的紧凑列（**逐字沿用旧格式**，改动即破坏既有输出）。
_SB_COLUMNS: tuple[tuple[str, str], ...] = (
    ("name", ""),
    ("tier", ""),
    ("percent", "涨幅"),
    ("vol_ratio", "量比"),
    ("main_pct", "主占"),
    ("accum_5d", "5日"),
    ("amount", "成交额"),
    ("float_cap", "流通"),
    ("price", "价"),
    ("first_time", "首发"),
)


#: 横截面区的紧凑列（`cs.` 前缀的素材项）。
_CS_COLUMNS: tuple[tuple[str, str], ...] = (
    ("name", ""),
    ("percent", "涨幅"),
    ("price", "价"),
    ("vol_ratio", "量比"),
    ("speed_1", "1m"),
    ("speed_3", "3m"),
    ("speed_5", "5m"),
    ("amount_surge", "额涌"),
    ("pct_rank", "分位"),
    ("persist", "轮次"),
    ("consistency", "一致性"),
    ("volume_price", "量价"),
)

#: 前缀 → 列规格。
_COLUMNS_BY_PREFIX: dict[str, tuple[tuple[str, str], ...]] = {
    "sb": _SB_COLUMNS,
    "cs": _CS_COLUMNS,
}


def _render_segment_rows(
    columns: Sequence[tuple[str, str]],
    prefix: str,
    symbols: Sequence[str],
    items: Sequence[MaterialItem],
) -> str:
    """一只票一行。`columns` = `(字段名, 前缀文字)`；前缀文字为空则直接显示值。"""
    per_symbol: dict[str, dict[str, str]] = {}
    for item in items:
        if not item.id.startswith(f"{prefix}."):
            continue
        _, symbol, key = item.id.split(".", 2)
        per_symbol.setdefault(symbol, {})[key] = item.value

    lines: list[str] = []
    for symbol in symbols:
        cells = per_symbol.get(symbol, {})
        parts = []
        for key, label in columns:
            v = cells.get(key, NA)
            parts.append(f"{label}{v}" if label else v)
        lines.append(f"  {symbol} " + " | ".join(parts))
    return "\n".join(lines)


def render_material_items(m: OffboardMaterials) -> str:
    """素材的逐项原样视图（`--materials`）：与发给模型的 `id | label | value` 完全一致。"""
    return "\n".join(f"  {i.id} | {i.label} | {i.value}" for i in m.items)


# ── 幻觉巡检（analyze 模式的唯一确定性闸，2026-11-03）────────────────────────────
#
# 红线词表在 analyze 模式被刻意关掉（拦了就没法给候选），但「编造数字」必须仍然拦 ——
# 手段是事后核对：正文里出现的**每一个数字**都必须能在「模型实际看到的提示词」里找到。
# 按浮点值比对（素材里是 +6.20%，模型写 6.2 算同一个数）；比对基准是**完整 user 消息**
# 而不是 items —— 这样日期、「第N只」序号、缺失项披露这些提示词里的合法数字不会误报。

_NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def unverified_numbers(text: str, user_prompt: str) -> list[str]:
    """`text` 里出现、但 `user_prompt`（模型实际看到的素材）里找不到的数字（升序去重）。

    非空 = 模型至少编了一个数 —— 输出侧必须把它**显式打出来**，而不是悄悄当没看见。

    ⚠ **容差**：允许 1% 相对误差（33.4 ≙ 33.35 这类四舍五入不算编造）。
    没有容差时，「模型把 33.35 写成 33.4」会被误报，久了就没人看这个告警了 ——
    守卫一旦变成噪声，就等于没有守卫（AGENTS.md 同款纪律）。
    单位换算（元↔万↔亿）**不算容差**：那是模型自己做的算术，工具已直接给人读单位
    （`research._money`），模型照抄即可。
    """
    material_values = sorted({float(s) for s in _NUM_RE.findall(user_prompt or "")})
    out: set[str] = set()
    for token in _NUM_RE.findall(text or ""):
        try:
            v = float(token)
        except ValueError:  # pragma: no cover - 正则已保证是数字
            continue
        if any(abs(v - b) <= max(0.01, abs(b) * 0.01) for b in material_values):
            continue
        out.add(token)
    return sorted(out, key=lambda s: (len(s), s))
