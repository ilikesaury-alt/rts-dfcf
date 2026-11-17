"""横截面异动层 → **统一 AI 分析管线**的区域实现（2026-11-16）。

## 这个模块做什么

把 `股票池.md` 里 `≡ 横截面异动` 那一区（由 `panel.py` 解析）翻译成
主管线 `material.build_materials()` 认识的**源行**，让它作为**一个区域**
并入 `python -m scanner.ai_digest` —— 走完全相同的素材构建 / 提示词 /
幻觉巡检 / 归档 / 报告格式，而不是另起一条命令。

## 边界（硬性契约）

- **唯一真源 = `股票池.md`**（终端面板存档）。本模块**零 I/O**：
  不连库、不联网、不读文件 —— 只做「panel 行 → 素材行」的翻译。
  读取由 `panel.py` 负责（那是全项目读面板的既有入口）。
- **区域之间彼此独立**：本区的素材 id 前缀是 `cs.`（榜外段是 `sb.`），
  同一只票在两区都有时不会互相覆盖；提示词也各用各的（见 `llm.py`）。
- **不预判涨跌方向**：跌得快的票同样是异常，一样会进这个层。
- **无历史命中率基准**：本层结构上无法自证胜率（票掉榜后表里就没有它的价格 ⇒
  前瞻样本有偏），所以**不挂** `baseline.stats`。

## 为什么用面板存档而不是数据库表

Lichun 2026-11-16 拍板：「数据源都是从文件 `<股票.md>` 读取」。
横截面块本来就**已经**在 `股票池.md` 里（终端每轮整份覆盖写入），
只是解析器此前不认 `≡` ⇒ 整块被静默丢弃。修好解析器之后，
它就是终端面板的**第三个并列区**（v1 池选 / 沪深飙升 / 横截面异动）——
与「终端面板是唯一真源」的既有设计一致。

⚠ 代价：面板存档只保留**最近一次快照** ⇒ 盘后复盘看到的是最后一轮，
不是全天。这是「单一真源」设计的固有取舍，不是 bug。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from scanner.ai_digest.material import OffboardMaterials, build_materials
from scanner.crosssection import signal_sort_key

__all__ = [
    "CROSSSECTION_FIELDS",
    "CROSSSECTION_NOTE",
    "build_crosssection_materials",
    "from_panel_rows",
]

#: 本区口径说明（发给模型的一条素材，避免它自己发明「横截面层是什么」）。
CROSSSECTION_NOTE = (
    "横截面异动层（并行观察层）：与榜外异动段**彼此独立**，靠 1/3/5 分钟涨速、"
    "成交额增量、量比四项横截面分位，找出创业板里当下异动最显著的票，"
    "目的是**早于飙升榜**看到动静。本层**不预判涨跌方向**（跌得快的票同样是异常），"
    "也不产生操作建议；本层暂无历史命中率基准（结构上无法自证），"
    "排序只能依据字段本身的相对强弱。"
)

#: 本区的字段白名单（`(列名, 中文标签, 格式)`）。
CROSSSECTION_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("name", "名称", "text"),
    ("percent", "当日涨幅", "pct"),
    ("price", "价格", "price"),
    ("vol_ratio", "量比", "dec2"),
    ("speed_1", "1m涨速", "pct"),
    ("speed_3", "3m涨速", "pct"),
    ("speed_5", "5m涨速", "pct"),
    ("amount_surge", "额涌", "dec2"),
    ("pct_rank", "横截面分位", "dec3"),
    ("persist", "持续轮次", "int"),
    ("consistency", "一致性", "text"),
    ("volume_price", "量价", "text"),
)


def _num(v: Any) -> float | None:
    """`'−3.95%'` / `'1005.24×'` / `'—'` → 数值；解析不了返回 None。

    ⚠ 缺值必须返回 `None`（⇒ 素材显示「暂无数据」），**不能变成 0.0** ——
    `0.0` 的语义是「算了，确实是零」，与「没数据」完全不同。
    """
    if isinstance(v, bool):
        return None
    if isinstance(v, int | float):
        return float(v)
    if not isinstance(v, str):
        return None
    t = v.strip().rstrip("%×").strip()
    if not t or t == "—":
        return None
    try:
        return float(t)
    except ValueError:
        return None


def from_panel_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """`panel.py` 解析出的横截面区行 → **统一管线的源行**（字段名对齐素材白名单）。

    panel 的行键是**中文列名**（`现价`/`涨幅`/`1m`/…，与其它区同一套约定），
    这里翻译成素材层要的字段名。
    """
    out: list[dict[str, Any]] = []
    for r in rows:
        判据 = str(r.get("判据") or "")
        cons, _, vp = 判据.partition("·")
        out.append(
            {
                "symbol": r.get("symbol"),
                "name": r.get("名称"),
                "price": _num(r.get("现价")),
                "percent": _num(r.get("涨幅")),
                "vol_ratio": _num(r.get("量比")),
                "speed_1": _num(r.get("1m")),
                "speed_3": _num(r.get("3m")),
                "speed_5": _num(r.get("5m")),
                "amount_surge": _num(r.get("额涌")),
                "pct_rank": _num(r.get("分位")),
                "persist": _num(r.get("轮次")),
                "consistency": cons,
                "volume_price": vp,
            }
        )
    return out


def _cs_sort_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    """排序：**展示分组(持续>中间>新进) → 质量层 → 轮次 → 分位 → 额涌 → 量比**
    （与终端展示同源，口径只写在 `crosssection.signal_sort_key`）。

    排序键定义在 `scanner.crosssection.signal_sort_key`，终端 `≡` 块与这里
    共用同一份，避免「面板看到一套顺序、AI 素材又是另一套」的漂移。
    """
    return signal_sort_key(row)


def build_crosssection_materials(
    date: str,
    rows: Sequence[Mapping[str, Any]],
    *,
    limit: int | None = None,
    mode: str = "analyze",
) -> OffboardMaterials:
    """本区源行 → 素材（走主管线同一套 `build_materials`）。

    - `id_prefix="cs"`：与 B 段的 `sb` 区分 ⇒ 同一只票在两个区域都有时不会互相覆盖。
    - `segment_note`：本区口径（模型据此知道这是什么区域、有什么约束）。
    - `with_market=False`：市况是 B 段那条链路的，跨区引用会把两层口径绑在一起。
    - **不带 baseline**：本层没有历史命中率基准（结构上无法自证），不硬塞。
    """
    return build_materials(
        date,
        rows,
        limit=limit,
        mode=mode,
        fields=CROSSSECTION_FIELDS,
        segment_note=CROSSSECTION_NOTE,
        id_prefix="cs",
        sort_key=_cs_sort_key,
        with_market=False,
    )
