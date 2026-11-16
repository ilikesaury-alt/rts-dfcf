"""横截面异动层的 **AI 分析素材构建器**（2026-11-16，Lichun 要求）。

## 边界（硬性契约，不可协商）

本模块的输入源**只有一张表**：`crosssection_signal`。

- ❌ 不读 `appearances` / `offboard_launch_log` / `offboard_print_snapshot`
- ❌ 不读 `recommendations` / `market_extra_cache` / `daily_kline` / `concept_cache`
- ❌ 不联网、不查行情、不补任何外部数据

这是 Lichun 2026-11-16 定下的产品纪律：**区域与区域之间不该有任何关联**。
横截面层的存在理由就是与飙升榜解耦（榜滞后 ⇒ 本层要早一步看到异动）；
往分析里掺入榜单/B 段数据，等于把滞后性加回来，自我否定。

⚠ **方向说明**：禁止的是「本层去读别人的表」。让 AI 分析来读本层的表
（即本模块做的事）不影响本层的独立性 —— 本层自身仍然不读任何人。
两者不是一回事，不要混。

## 为什么直接读表而不是解析终端文本

终端 `≡` 块来自进程内存（`_state["last_signals"]`），**扫描器一停就没了**；
盘后想复盘必须从表重建。因此本模块**复刻判定逻辑**（`_consistency` /
`_volume_price` 直接 import，不重写），从表重建同一套判据。

`docs/CROSSSECTION-TERMINAL-BLOCK.md` 是两边共同的口径真源。

## 定位：结构摘要，不是选股

素材刻意只给**分布与结构**（多少只、判据怎么分布、哪些持续），
外加少量代表样本。理由：

1. 全量 43~68 行塞进 prompt 成本高且无助于理解结构；
2. 只给结构能**大幅降低模型编造数字的空间**（幻觉巡检更容易过）；
3. 本层本身不判方向、不推荐 —— 素材若筛成"最好的几只"，就变成了推荐，
   违背本层设计契约。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from scanner.crosssection import _consistency, _volume_price

__all__ = [
    "CrosssectionSummary",
    "build_summary",
    "resolve_date",
    "render_materials",
]

# 代表样本上限：持续性最高的前 N + 分位最高的前 N，去重后合并展示。
_SAMPLE_PERSIST = 5
_SAMPLE_RANK = 5


@dataclass
class CrosssectionSummary:
    """一层一日横截面异动的结构摘要（全部数据来自 `crosssection_signal`）。"""

    date: str
    rounds: int = 0
    first_time: str = ""
    last_time: str = ""
    total_rows: int = 0
    # 去重后的只数
    symbols: int = 0
    # 判据分布（按「只」统计，不按行 —— 同一只票多轮只算一次）
    consistency: dict[str, int] = field(default_factory=dict)
    volume_price: dict[str, int] = field(default_factory=dict)
    # 持续性分桶
    persist_buckets: dict[str, int] = field(default_factory=dict)
    max_persist: int = 0
    max_persist_symbol: str = ""
    max_persist_name: str = ""
    # 当日涨幅分布的粗统计（只给区间计数，不给推论）
    pct_up: int = 0
    pct_down: int = 0
    pct_flat: int = 0
    pct_min: float | None = None
    pct_max: float | None = None
    # 代表样本（每项 = 一只票的一条最终轮记录）
    samples: list[dict[str, Any]] = field(default_factory=list)


def resolve_date(conn: sqlite3.Connection, date: str | None = None) -> str | None:
    """`date=None` ⇒ 取表里最大的日期。表空或无该表 ⇒ `None`。"""
    try:
        if date:
            row = conn.execute(
                "SELECT 1 FROM crosssection_signal WHERE date=? LIMIT 1", (date,)
            ).fetchone()
            return date if row else None
        row = conn.execute("SELECT MAX(date) AS d FROM crosssection_signal").fetchone()
        return (row[0] if row else None) or None
    except sqlite3.Error:
        return None


def _select_rows(conn: sqlite3.Connection, date: str) -> list[sqlite3.Row]:
    """查本层当日全部行。

    ⚠ `row_factory` 必须在**查询之前**设：设晚了拿到裸 tuple，后面按列名取会 TypeError。
    用 try/finally 保证还原 —— 调用方可能共用连接并依赖原 factory，
    在每个 `return` 点手工还原迟早漏一个（这里踩过）。
    """
    prev = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT round_seq, symbol, name, price, percent, speed_1, speed_3, speed_5, "
            "amount_surge, vol_ratio, pct_rank, updated_at "
            "FROM crosssection_signal WHERE date=? ORDER BY round_seq, pct_rank DESC",
            (date,),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.row_factory = prev


def build_summary(conn: sqlite3.Connection, date: str) -> CrosssectionSummary | None:
    """从 `crosssection_signal` 重建一层一日的结构摘要。无数据 ⇒ `None`。

    只做 SQL 聚合 + 复用 `crosssection` 的判据函数；不引入任何其他表。
    """
    # ⚠ `row_factory` 必须在查询**之前**设：设晚了拿到的是裸 tuple，按列名取会 TypeError。
    rows = _select_rows(conn, date)
    if not rows:
        return None

    s = CrosssectionSummary(date=date)
    s.total_rows = len(rows)
    s.rounds = len({r["round_seq"] for r in rows})
    stamps = sorted({r["updated_at"] for r in rows if r["updated_at"]})
    if stamps:
        s.first_time = _hhmmss(stamps[0])
        s.last_time = _hhmmss(stamps[-1])

    # ── 按「只」聚合：每只票取最后一次出现的记录做判据快照 ──
    # 为什么取最后一次：判据（一致性/量价）是**当轮**状态，
    # 取最后一轮才代表"收盘时它是什么样"。
    last: dict[str, sqlite3.Row] = {}
    seen_rounds: dict[str, set[int]] = {}
    for r in rows:
        sym = r["symbol"]
        last[sym] = r  # 行已按 round_seq 升序，后来者覆盖 ⇒ 最后一条
        seen_rounds.setdefault(sym, set()).add(int(r["round_seq"]))

    s.symbols = len(last)
    for sym, r in last.items():
        c = _consistency(r["speed_1"], r["speed_3"], r["speed_5"])
        v = _volume_price(r["amount_surge"], r["speed_1"])
        s.consistency[c] = s.consistency.get(c, 0) + 1
        s.volume_price[v] = s.volume_price.get(v, 0) + 1

    # ── 持续性：当日出现过几轮 ──
    for sym, rounds in seen_rounds.items():
        n = len(rounds)
        key = _persist_bucket(n)
        s.persist_buckets[key] = s.persist_buckets.get(key, 0) + 1
        if n > s.max_persist:
            s.max_persist = n
            s.max_persist_symbol = sym
            s.max_persist_name = last[sym]["name"] or sym

    # ── 当日涨幅分布（按只，取最后一条）──
    pcts = [r["percent"] for r in last.values() if r["percent"] is not None]
    if pcts:
        s.pct_up = sum(1 for p in pcts if p > 0)
        s.pct_down = sum(1 for p in pcts if p < 0)
        s.pct_flat = sum(1 for p in pcts if p == 0)
        s.pct_min = min(pcts)
        s.pct_max = max(pcts)

    # ── 代表样本：持续性最高 + 分位最高，去重合并 ──
    picked: dict[str, dict[str, Any]] = {}
    by_persist = sorted(seen_rounds.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    for sym, rounds in by_persist[:_SAMPLE_PERSIST]:
        picked[sym] = _sample_row(sym, last[sym], len(rounds))
    by_rank = sorted(last.items(), key=lambda kv: -(kv[1]["pct_rank"] or 0.0))
    for sym, r in by_rank[:_SAMPLE_RANK]:
        picked.setdefault(sym, _sample_row(sym, r, len(seen_rounds[sym])))
    s.samples = list(picked.values())
    return s


def _sample_row(sym: str, r: sqlite3.Row, persist: int) -> dict[str, Any]:
    return {
        "symbol": sym,
        "name": r["name"] or sym,
        "price": r["price"],
        "percent": r["percent"],
        "speed_1": r["speed_1"],
        "speed_3": r["speed_3"],
        "speed_5": r["speed_5"],
        "amount_surge": r["amount_surge"],
        "vol_ratio": r["vol_ratio"],
        "pct_rank": r["pct_rank"],
        "consistency": _consistency(r["speed_1"], r["speed_3"], r["speed_5"]),
        "volume_price": _volume_price(r["amount_surge"], r["speed_1"]),
        "persist": persist,
    }


def _persist_bucket(n: int) -> str:
    if n <= 1:
        return "1轮"
    if n <= 3:
        return "2-3轮"
    if n <= 10:
        return "4-10轮"
    if n <= 30:
        return "11-30轮"
    return "30轮以上"


def _hhmmss(stamp: str) -> str:
    """`2026-11-16T13:05:01.24+08:00` → `13:05:01`。解析失败原样返回。"""
    t = stamp.split("T", 1)
    if len(t) == 2:
        return t[1][:8]
    return stamp


def _fmt(v: Any, digits: int = 2, suffix: str = "") -> str:
    if v is None or not isinstance(v, (int, float)):
        return "—"
    return f"{v:.{digits}f}{suffix}"


def render_materials(s: CrosssectionSummary) -> str:
    """把摘要渲染成**给模型看的素材文本**（纯结构，不含任何推论）。"""
    lines = [
        f"【横截面异动层 · {s.date}】",
        f"数据范围：{s.rounds} 轮 | {s.first_time} ~ {s.last_time} | "
        f"唯一只数 {s.symbols} | 信号行 {s.total_rows}",
        "",
        "【判据分布（按只，取各票当日最后一次出现的状态）】",
        f"  一致性：全正 {s.consistency.get('全正', 0)} · "
        f"全负 {s.consistency.get('全负', 0)} · 混合 {s.consistency.get('混合', 0)}",
        f"  量价：配合 {s.volume_price.get('配合', 0)} · "
        f"背离 {s.volume_price.get('背离', 0)} · — {s.volume_price.get('—', 0)}",
        "",
        "【持续性分布（当日进入 p95+ 的轮次数）】",
    ]
    for key in ("1轮", "2-3轮", "4-10轮", "11-30轮", "30轮以上"):
        n = s.persist_buckets.get(key, 0)
        if n:
            lines.append(f"  {key}：{n} 只")
    # ⚠ 预置常用分组合计：否则模型会自己把分桶相加，算错即被判「编造数字」。
    #    实测踩过两次（578 / 31 都是这样来的）—— 不是模型编造，是它做了算术。
    #    给出合计既省得它算，也让幻觉巡检不再误报。
    long_lived = s.persist_buckets.get("11-30轮", 0) + s.persist_buckets.get("30轮以上", 0)
    repeated = s.symbols - s.persist_buckets.get("1轮", 0)
    lines.append(f"  合计：出现≥2轮 {repeated} 只 · 出现≥11轮 {long_lived} 只 · 全部 {s.symbols} 只")
    # ⚠ 同因第三例（2026-11-16）：模型算了「平均每只 3.7 次」= 信号行÷只数。
    #    预置平均值 + 明确口径，省得它算除法。
    avg = (s.total_rows / s.symbols) if s.symbols else 0.0
    lines.append(f"  平均每只进入 {avg:.1f} 轮（= 信号行 {s.total_rows} ÷ 只数 {s.symbols}）")
    if s.max_persist:
        lines.append(
            f"  最长持续：{s.max_persist_symbol} {s.max_persist_name} {s.max_persist} 轮"
        )

    lines += [
        "",
        "【当日涨跌幅分布（按只，各票最后一条）】",
        f"  上涨 {s.pct_up} · 下跌 {s.pct_down} · 平盘 {s.pct_flat}",
        f"  区间：{_fmt(s.pct_min, 2, '%')} ~ {_fmt(s.pct_max, 2, '%')}",
        "",
        "【代表样本（持续性最高 / 分位最高，去重合并）】",
        # ⚠ 口径必读：样本行是**当日最后一次出现时**的快照，不是"该票最猛的那一刻"。
        # 少了这句，模型会把「当日累计跌幅」和「短窗口急拉」读成矛盾（实际是不同时点），
        # 实测踩过：把 10:09 的短窗口值当收盘状态描述。
        "  （口径：每行是该票当日**最后一次进入本层**时的快照；"
        "「当日」= 相对昨收的累计涨跌幅，「1m/3m/5m」= 该时点的短窗口涨速。"
        "两者时点相同但含义不同，累计为跌的票仍可能短窗口急拉。）",
    ]
    for x in s.samples:
        lines.append(
            f"  {x['symbol']} {x['name']} 价 {_fmt(x['price'])} "
            f"当日 {_fmt(x['percent'], 2, '%')} | "
            f"1m {_fmt(x['speed_1'], 2, '%')} 3m {_fmt(x['speed_3'], 2, '%')} "
            f"5m {_fmt(x['speed_5'], 2, '%')} | "
            f"额涌 {_fmt(x['amount_surge'], 2, '×')} 量比 {_fmt(x['vol_ratio'])} "
            f"分位 {x['pct_rank']:.3f} | "
            f"{x['consistency']}·{x['volume_price']}·{x['persist']}轮"
        )
    return "\n".join(lines)