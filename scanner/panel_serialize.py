"""面板载荷序列化（design §3.1/§3.3/§3.6/§3.7，SCHEMA_VERSION=2）。

**纯函数模块：零 I/O、零网络、零云端依赖** —— 这是整个面板工程里最早能进 CI 的资产
（T0.1/T0.2），也是两端契约（`worker/src/serialize.ts`）的唯一本地真源。

三个隐性类型陷阱（design §3.3，改动前必读）：

1. **tuple 键**：`ScanView.breakout_mark` / `beauty_mark` 的键是 `(symbol, category)`。
   `json.dumps` 遇 tuple 键抛 `TypeError: keys must be str, int, float, bool or None`。
   → `_marks()` 转 `"sym|cat"`。**不要**用 `{str(k): v}`（会产出 `"(300319, 'MOM')"`
   这种不可逆键，云端与前端都无法再按 symbol 检索）。
2. **`_candidate` 活对象**：`MainRow.entry`（`RecommendationRow`）里挂着实时候选对象，
   `dataclasses.asdict` 会把它**递归展开**成整个树。本模块**手工剥字段**，不走 asdict。
3. **`score_breakdown` 双形态**：DB 行里可能是 JSON 字符串、也可能是已解析 dict。
   这里统一 `json.loads` 一次转 dict 上报，云端存原样、前端不再解析（避免两端各解析一次）。

反模式（design §3.3）：`dataclasses.asdict(view)` 一把梭。
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any

from scanner.config import REFRESH_INTERVAL
from scanner.view.model import COLS_HIST, COLS_HOT, COLS_POOL, ScanView

# 契约版本。**必须**与 `rts-panel-cloud/worker/src/serialize.ts` 的同值常量相等 ——
# 不等即云端 400 `SCHEMA_MISMATCH`（双端同值 2；改契约必须同步两端并提版本）。
# ⚠ 该值**不进** wrangler vars（vars 一律字符串，会变成 `2 !== "2"`），见 design §4.1。
SCHEMA_VERSION = 2

# FR-C2 规则 5：每轮最多带 20 只票的 K 线（≈160KB）。与云端 `ingest.ts` 的
# `STOCK_QUOTA` 同一语义、同一数值 —— 双端各有一份是刻意的（云端不信任上报器自律）。
STOCK_QUOTA = 20
# K 线上送根数（云端 `/api/stock/{symbol}/kline` 的 `days` 上限 120，见 design §4.4）。
STOCK_KLINE_DAYS = 120
# 体积探针阈值（design §3.3）：超限只截断 `stocks`（新票优先），**不丢整轮**。
BODY_LIMIT = 400_000

# MainRow.entry 里必须剥离的展示层活对象（只剥这一个，不剥 `_tier`/`_accum` 等标量键 ——
# 它们是纯标量、可序列化，且对详情页有用）。
_STRIP_KEYS = frozenset({"_candidate"})


def _marks(m: dict | None) -> dict[str, Any]:
    """tuple 键 → `'sym|cat'` 字符串键（D3 陷阱 1）。None/空 → `{}`。"""
    if not m:
        return {}
    return {"|".join(k) if isinstance(k, tuple) else str(k): v for k, v in m.items()}


def _jsonable(value: Any) -> Any:
    """把一个字段值压成 json 可序列化的形态（不可序列化的对象一律丢弃为 None）。

    只在 `_flat_row` 的兜底里用到：正常字段都是标量/列表。宁可丢一个字段，
    也不让整轮上报因一个意外对象失败（FR-C1 的旁路隔离优先于字段完整性）。
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items() if not str(k).startswith("_")}
    return None


def _flat_row(row: Any) -> dict[str, Any]:
    """dataclass / TypedDict 混合体 → 纯标量 dict（D3 陷阱 2/3）。

    `MainRow` 是 dataclass 且持有 `entry`（TypedDict），其余三区是纯 dataclass。
    这里**逐字段取值**而非 `dataclasses.asdict`：后者会递归展开 `entry["_candidate"]`
    指向的整个候选对象树（design §3.3 反模式）。
    """
    if dataclasses.is_dataclass(row) and not isinstance(row, type):
        d = {f.name: getattr(row, f.name) for f in dataclasses.fields(row)}
    else:
        d = dict(row)

    entry = d.pop("entry", None) or {}
    if not isinstance(entry, dict):
        entry = dict(entry)
    for k in _STRIP_KEYS:
        entry.pop(k, None)

    out: dict[str, Any] = {}
    for k, v in entry.items():
        # 展示层注入键（live_* / _accum / _tier / _core_stock）不是契约字段，
        # 前端不认（也无需认）——但它们都是标量，留着无害且对排查有用。
        if k == "score_breakdown":
            out[k] = _parse_breakdown(v)
        else:
            out[k] = _jsonable(v)
    for k, v in d.items():
        if k == "score_breakdown":
            out[k] = _parse_breakdown(v)
        else:
            out[k] = _jsonable(v)
    return out


def _parse_breakdown(v: Any) -> dict:
    """`score_breakdown` 统一为 dict（字符串/None/非法 → `{}`，单源 models.parse_score_breakdown）。"""
    from scanner.models import parse_score_breakdown

    return parse_score_breakdown(v)


def col_spec(cols: tuple) -> list[dict]:
    """`COLS_*`（`(label, width, align)` 三元组）→ `[{key,label,align,width}]`（D7 §3.7）。"""
    return [{"key": c[0], "label": c[0], "align": c[2], "width": c[1]} for c in cols]


def cols_payload() -> dict[str, list[dict]]:
    """五区块的列定义投影（静态 ~1KB，随载荷下发 ⇒ 前端词表与运行时数据同源）。"""
    return {
        "pool": col_spec(COLS_POOL),
        "hot": col_spec(COLS_HOT),
        "hist": col_spec(COLS_HIST),
    }


def _gate(view: ScanView) -> dict:
    """本地算好飞书过滤门的结果（D6 §3.6）：云端**只展示，不重算**。

    复用 `push_gate.apply_push_gate` —— 与飞书卡片**同一函数、同一份结果**，
    故两端不可能漂移。云端若按 `config_push` 阈值重算，改了阈值不同步即两端不一致。
    """
    from scanner.push_gate import apply_push_gate

    g = apply_push_gate(view)
    return {
        "main": [_flat_row(r) for r in g.main],
        "hist": [_flat_row(r) for r in g.hist],
        "hot": [_flat_row(r) for r in g.hot],
        "offboard": [_flat_row(r) for r in g.offboard],
        "stats": dataclasses.asdict(g.stats),
    }


def _config_snapshot() -> dict:
    """`ctx.config`：**只读回显**本地阈值，供面板展示「门当前的口径」。

    ⚠ 云端**不得**据此重算门（D6 §3.6）—— 这里的值只用于**显示**（如「A 档 ≥0.10」），
    真值永远是本地这次算出来的 `regions.gate`。
    """
    from scanner import config_push

    return {
        "refreshInterval": REFRESH_INTERVAL,
        "tierAMin": config_push.PUSH_TIER_A_MIN,
        "tierBMin": config_push.PUSH_TIER_B_MIN,
        "fallbackMaxRank": config_push.PUSH_FALLBACK_MAX_RANK,
        "fallbackMinStreak": config_push.PUSH_FALLBACK_MIN_STREAK,
        "fallbackMinRankRise": config_push.PUSH_FALLBACK_MIN_RANK_RISE,
        "fallbackMinVolRatio": config_push.PUSH_FALLBACK_MIN_VOL_RATIO,
        "minInterval": config_push.PUSH_MIN_INTERVAL,
        "stockQuota": STOCK_QUOTA,
    }


def _ctx(view: ScanView) -> dict:
    return {
        "marketIdxPct": view.market_idx_pct,
        "weak": view.weak,
        "flowFiltered": view.flow_filtered,
        "warnings": list(view.warnings or []),
        "ruleResult": dataclasses.asdict(view.rule_result) if dataclasses.is_dataclass(view.rule_result) else None,
        "config": _config_snapshot(),
    }


def serialize_view(view: ScanView, *, extra: dict | None = None) -> dict:
    """`ScanView` → 上报载荷 dict。

    **云端不得重排**（FR-V2）：排序已在 `assemble.build_scan_view` 完成，这里只做
    结构转换。`extra` 承载本轮元信息（`seq/date/time/durationMs/stocks`）。
    """
    extra = extra or {}
    return {
        "schema": SCHEMA_VERSION,
        "seq": extra.get("seq", 0),
        "date": extra["date"],
        "time": extra["time"],
        "durationMs": extra.get("durationMs", 0),
        "regions": {
            "main": [_flat_row(r) for r in view.main_rows or []],
            "hist": [_flat_row(r) for r in view.hist_rows or []],
            "hot": [_flat_row(r) for r in view.hot_rows or []],
            "offboard": [_flat_row(r) for r in view.offboard_rows or []],
            "gate": _gate(view),
            "cols": cols_payload(),
            "marks": {"breakout": _marks(view.breakout_mark), "beauty": _marks(view.beauty_mark)},
        },
        "ctx": _ctx(view),
        "stocks": dict(extra.get("stocks") or {}),
    }


def enforce_stock_quota(
    stocks: dict[str, Any],
    *,
    quota: int = STOCK_QUOTA,
    new_first: set[str] | None = None,
) -> dict[str, Any]:
    """增量收集器的**最后一道**截断（FR-C2 规则 5，双端同语义）。

    新票（`new_first`）优先保留 —— 重启后 3 轮收敛（design §3.1 增量收集器第 4 条）里，
    新票是用户最可能点开的一批。截断而非拒绝：拒绝会让上报器一个配额 bug 丢掉整轮
    regions（design §4.2 复核修 6 的同款理由，云端侧是「不因 stocks 丢整轮」）。
    """
    if len(stocks) <= quota:
        return stocks
    new = set(new_first or ())
    ordered = sorted(stocks.items(), key=lambda kv: (kv[0] not in new,))
    return dict(ordered[:quota])


def enforce_body_limit(payload: dict, *, limit: int = BODY_LIMIT) -> tuple[dict, int]:
    """体积探针（design §3.3）：`len(body)` > 400KB 时截断 `stocks`，**不丢整轮**。

    返回 `(payload, dropped_count)`。用二分收缩（每轮砍半）而非逐票试删 ——
    探针每轮只跑一次、且要在 50ms 预算内（design §3.1）。
    """
    body = json.dumps(payload, ensure_ascii=False).encode()
    if len(body) <= limit or not payload.get("stocks"):
        return payload, 0
    stocks = dict(payload["stocks"])
    dropped = 0
    while stocks and len(body) > limit:
        keep = len(stocks) // 2
        dropped += len(stocks) - keep
        stocks = dict(list(stocks.items())[:keep])
        trial = dict(payload)
        trial["stocks"] = stocks
        body = json.dumps(trial, ensure_ascii=False).encode()
    payload = dict(payload)
    payload["stocks"] = stocks
    return payload, dropped
