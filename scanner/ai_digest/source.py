"""`offboard_launch_log` 的**只读**读取（2026-11-03）。

只做两件事：给出「最新信号日」，给出该日的过门行。**没有一条写语句** ——
这是本包「AI 输出不入任何计算路径」的一半（另一半是输出只 print、不落库）。
守卫：`tests/test_ai_digest.py::test_digest_package_contains_no_write_sql`。

⚠ 两个必须知道的口径事实：

1. **本表存的是「全部过门行」，不是「终端展示的那几行」**（`persist_round` 收到的是
   `passed`，展示才截 `OFFBOARD_DISPLAY_TOP`）。实测 2026-11-02：17 行过门、终端展示 8 行
   ⇒ CLI 默认按 `OFFBOARD_DISPLAY_TOP` 截断，并把「共 N 只 / 本次转述前 M 只」如实打出来。
2. **留存行（`retain=True`）不入本表**（`offboard_launch_log` 只记严格过门的行，
   留存窗口靠 `last_hit_time`）⇒ 某一轮终端上显示、但本日从未严格过门的票，
   本模块看不到。这是刻意的：转述的是**当日落库的信号**，不是「某一轮屏幕上的快照」。
   需要屏幕快照请另开（那需要进程内 `view.offboard_rows`，命令行拿不到）。
"""

from __future__ import annotations

import os
import sqlite3
from typing import TypedDict

__all__ = [
    "BaselineStats",
    "OffboardSourceError",
    "connect_readonly",
    "latest_print_snapshot",
    "latest_signal_date",
    "load_baseline_stats",
    "load_offboard_rows",
    "load_latest_view_offboard",
    "render_baseline_text",
]

_COLUMNS = (
    "symbol",
    "name",
    "tier",
    "percent",
    "accum_5d",
    "vol_ratio",
    "main_pct",
    "amount",
    "float_cap",
    "price",
    "first_time",
)


class OffboardSourceError(RuntimeError):
    """读侧失败（库不存在 / 表缺失 / 该日无行）。文案面向用户。"""


def connect_readonly(db_path: str) -> sqlite3.Connection:
    """以 `mode=ro` 打开库。

    两个理由：
    - **不建库**：`sqlite3.connect()` 对不存在的路径会**默默创建一个空库**，
      而本工具是只读的，创建空库等于留下一个「看起来正常」的假数据库；
    - **不写**：URI `mode=ro` 让任何误加的写语句直接报错，而不是悄悄落库。
      （WAL 库在 ro 下可读 —— 2026-11-03 实测 `scanner.db` 965 行可读。）
    """
    if not os.path.exists(db_path):
        raise OffboardSourceError(f"数据库不存在：{db_path}（先跑一轮扫描器？）")
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def latest_signal_date(conn: sqlite3.Connection) -> str | None:
    """最新信号日（`offboard_launch_log.date` 的最大值）；表空/表缺失返回 None。"""
    try:
        row = conn.execute("SELECT MAX(date) AS d FROM offboard_launch_log").fetchone()
    except sqlite3.Error as e:
        raise OffboardSourceError(f"读取 offboard_launch_log 失败：{e}") from None
    return row[0] if row and row[0] else None


def load_offboard_rows(conn: sqlite3.Connection, date: str) -> list[dict]:
    """取某信号日的全部过门行（未排序 —— 排序口径单源在 `material._order_key`）。"""
    sql = f"SELECT {', '.join(_COLUMNS)} FROM offboard_launch_log WHERE date = ?"  # noqa: S608 - 列名是常量元组
    try:
        rows = conn.execute(sql, (date,)).fetchall()
    except sqlite3.Error as e:
        raise OffboardSourceError(f"读取 offboard_launch_log 失败：{e}") from None
    return [dict(zip(_COLUMNS, r, strict=True)) for r in rows]


# ── 终端打印快照源（m022，2026-11-03）─────────────────────────────────────────
#
# 用户口径：「拿最近一次榜外异动的**终端打印**数据」—— 这是屏幕上最后显示的那几行
# （可能是 3 只），不是 `offboard_launch_log` 的整日累加（可能是 17 只），也不是
# `view_snapshot`（被 `RTS_LOCAL_PANEL` 门控，命令行跑扫描器时一条都不落）。

_PRINT_COLUMNS = (
    "symbol",
    "name",
    "tier",
    "percent",
    "accum_5d",
    "vol_ratio",
    "main_pct",
    "amount",
    "float_cap",
    "price",
    "sector",
    "turnover_rate",
    "ff_pct",
    "beauty",
    "reasons",
    "time",
    "print_seq",
)


def latest_print_snapshot(conn: sqlite3.Connection) -> tuple[str, str, list[dict], int] | None:
    """最近一次终端打印 → `(date, time, rows, print_seq)`；无数据/表缺失返回 None。

    排序取 `print_seq DESC`（秒级单调序），**不按 `time` 字符串比** —— 两者同源，
    但 `print_seq` 是整数，跨日比较不会出现字符串排序的坑。
    """
    try:
        latest = conn.execute(
            "SELECT date, print_seq FROM offboard_print_snapshot ORDER BY date DESC, print_seq DESC LIMIT 1"
        ).fetchone()
    except sqlite3.Error:
        return None  # 表缺失（老库未迁移）→ 当作没有，让 auto 回退到 log
    if not latest:
        return None
    date, print_seq = latest
    sql = (
        f"SELECT {', '.join(_PRINT_COLUMNS)} FROM offboard_print_snapshot "  # noqa: S608 - 列名是常量元组
        "WHERE date = ? AND print_seq = ? ORDER BY rank_in_print"
    )
    try:
        rows = conn.execute(sql, (date, print_seq)).fetchall()
    except sqlite3.Error:
        return None
    if not rows:
        return None
    out = [dict(zip(_PRINT_COLUMNS, r, strict=True)) for r in rows]
    tm = str(out[0].get("time") or "")
    return str(date), tm, out, int(print_seq)


# ── 历史基准率（喂给模型真证据，2026-11-03 用户决策）──────────────────────────
#
# 「模型自己调研」在进程内做不到（`/chat/completions` 是纯文本补全，无联网、无 tool）。
# 故改为**把项目自己的标签统计当素材**喂进去：模型不需要知道外部世界，它只需要知道
# 「这个信号段历史上什么桶的次日命中率更高」——那是本项目 948 条 `next_day_pct`
# 标签能给的全部证据。⚠ 这组数字**不是**市场先验，是本区自己的样本内统计，
# 必须连同样本量一起给出（n=20 的桶不能当结论用）。

#: 分桶定义：`(列名, 中文标签, [(下界, 上界, 桶名)], 单位换算)`
BASELINE_BUCKETS: tuple[tuple[str, str, tuple[tuple[float, float, str], ...], float], ...] = (
    (
        "vol_ratio",
        "量比",
        ((1.5, 2.0, "1.5~2"), (2.0, 3.0, "2~3"), (3.0, 5.0, "3~5"), (5.0, 10.0, "5~10"), (10.0, 1e9, "≥10")),
        1.0,
    ),
    (
        "main_pct",
        "主力净占比",
        ((0.0, 2.0, "0~2%"), (2.0, 5.0, "2~5%"), (5.0, 10.0, "5~10%"), (10.0, 1e9, "≥10%")),
        1.0,
    ),
    (
        "accum_5d",
        "5日累计",
        ((0.0, 3.0, "0~3%"), (3.0, 8.0, "3~8%"), (8.0, 15.0, "8~15%"), (15.0, 1e9, "≥15%")),
        1.0,
    ),
    (
        "amount",
        "成交额",
        # ⚠ `scale` 与 `cuts` 必须**同单位**：比较式是 `lo <= 原值/scale < hi`，
        #   故 cuts 用「亿」时 scale=1e8。2026-11-03 实测踩过一次反例：cuts 写成
        #   `(0.0, 0.8e8, ...)` + scale=1e8 ⇒ 原值/1e8 与 0.8e8 比，948 行全落进
        #   第一桶，打印出「<0.8亿 4.5%（n=948）」这种明显失真的读数。
        (
            (0.0, 0.8, "<0.8亿"),
            (0.8, 1.5, "0.8~1.5亿"),
            (1.5, 3.0, "1.5~3亿"),
            (3.0, 6.0, "3~6亿"),
            (6.0, 1e12, "≥6亿"),
        ),
        1e8,
    ),
)

#: 桶样本量低于该值时标注「样本少」，避免把 n=20 的 15% 当成结论。
_MIN_BUCKET_N = 30


class BucketStat(TypedDict):
    name: str
    n: int
    hit: float
    mean: float


class TierStat(TypedDict):
    n: int
    hit: float
    mean: float


class BaselineStats(TypedDict):
    """`load_baseline_stats` 的返回结构（供 `render_baseline_text` 与测试共用）。"""

    n: int
    tiers: dict[str, TierStat]
    buckets: dict[str, list[BucketStat]]


def load_baseline_stats(conn: sqlite3.Connection) -> BaselineStats:
    """B 段历史分层命中率（本项目自己的标签，**不是**市场先验）。

    → `{"n": 总数, "tiers": {...}, "buckets": {列名: [(桶名, n, 命中率%), ...]}}`

    口径与 `scripts/offboard_nextday.py` 同源：`next_day_pct` 非空的行；命中 = 次日 ≥7%
    （系统唯一目标函数）。⚠ 这里**只统计**，不下结论、不给建议 —— 结论由模型在素材内做。
    """
    import statistics as st

    try:
        rows = conn.execute(
            "SELECT tier, vol_ratio, main_pct, accum_5d, amount, next_day_pct "
            "FROM offboard_launch_log WHERE next_day_pct IS NOT NULL"
        ).fetchall()
    except sqlite3.Error:
        return {"n": 0, "tiers": {}, "buckets": {}}

    total = len(rows)
    tiers: dict[str, dict[str, float]] = {}
    for tier in ("T1", "T2"):
        vals = [float(r[5]) for r in rows if r[0] == tier]
        if vals:
            tiers[tier] = {
                "n": len(vals),
                "hit": round(sum(1 for v in vals if v >= 7) / len(vals) * 100, 1),
                "mean": round(st.mean(vals), 2),
            }

    buckets: dict[str, list[dict[str, object]]] = {}
    for col, _label, cuts, scale in BASELINE_BUCKETS:
        idx = {"vol_ratio": 1, "main_pct": 2, "accum_5d": 3, "amount": 4}[col]
        out = []
        for lo, hi, name in cuts:
            vals = [float(r[5]) for r in rows if r[idx] is not None and lo <= float(r[idx]) / scale < hi]
            if not vals:
                continue
            out.append(
                {
                    "name": name,
                    "n": len(vals),
                    "hit": round(sum(1 for v in vals if v >= 7) / len(vals) * 100, 1),
                    "mean": round(st.mean(vals), 2),
                }
            )
        buckets[col] = out

    return {"n": total, "tiers": tiers, "buckets": buckets}  # type: ignore[typeddict-item]


def render_baseline_text(stats: BaselineStats, *, label_map: dict[str, str]) -> str:
    """基准率 → 一条素材文本（确定性、无 locale 依赖）。"""
    n = int(stats.get("n") or 0)
    if n == 0:
        return "暂无数据（本区还没有带次日标签的样本）"
    parts = [f"本区历史 {n} 条带标签样本（口径：信号日收盘→次一交易日收盘；命中=次日涨幅≥7%）"]
    tiers = stats.get("tiers") or {}
    if tiers:
        seg = "；".join(f"{t} {v['hit']}%（n={v['n']}，均值{v['mean']:+.2f}%）" for t, v in tiers.items())
        parts.append(f"分层命中：{seg}")
    buckets = stats.get("buckets") or {}
    for col, _label, _cuts, _scale in BASELINE_BUCKETS:
        items = buckets.get(col) or []
        if not items:
            continue
        seg = "，".join(
            f"{b['name']} {b['hit']}%（n={b['n']}{'·样本少' if b['n'] < _MIN_BUCKET_N else ''}）" for b in items
        )
        parts.append(f"{label_map.get(col, col)}：{seg}")
    return "｜".join(parts)


# ── 视图快照源（旧口径，auto 回退用）─────────────────────────────────────────
#
# `view_snapshot` 存的是主循环每轮 `serialize_view` 的完整载荷（m021，保留 50 轮）——
# 与终端渲染、云端上报是**同一份**。B 段行在这里比 launch_log 多三样东西：
# **留存行**（终端有、launch_log 无）、**实时值**（percent/量比是打印那刻的，不是冻结值）、
# **入选理由/板块/形态标签**（analyze 模式的关键素材）。


def _normalize_view_row(r: dict) -> dict:
    """视图行键名 → 素材口径键名（两张表对同一个量的叫法不同，必须在这里收口）。"""
    reasons = r.get("reasons")
    if isinstance(reasons, list):
        reasons = "；".join(str(x) for x in reasons if str(x).strip())
    elif not isinstance(reasons, str):
        reasons = ""
    return {
        "symbol": r.get("symbol") or r.get("code") or "",
        "name": r.get("name"),
        "tier": r.get("tier"),
        "percent": r.get("percent"),
        "accum_5d": r.get("accum_5d"),
        # 视图行键是 `volume_ratio`（`OffboardCandidate` 字段名），launch_log 列是 `vol_ratio`
        "vol_ratio": r.get("volume_ratio", r.get("vol_ratio")),
        "main_pct": r.get("main_pct"),
        "amount": r.get("amount"),
        # 同上：视图 `float_market_capital` / `current` ↔ launch_log `float_cap` / `price`
        "float_cap": r.get("float_market_capital", r.get("float_cap")),
        "price": r.get("current", r.get("price")),
        "first_time": None,  # 视图行没有这个量（那是 launch_log 的「首次入选」）
        # analyze 模式附加（transcribe 模式不消费，字段表里不列即可）
        "sector": r.get("sector"),
        "ff_pct": r.get("ff_pct"),
        "turnover_rate": r.get("turnover_rate"),
        "beauty": r.get("beauty"),
        "reasons": reasons or None,
    }


def load_latest_view_offboard(conn: sqlite3.Connection) -> tuple[str, str, list[dict], dict] | None:
    """最近一轮终端打印的 B 段行 → `(date, time, rows, market)`；无快照/无行返回 None。

    `rows` 保持载荷里的顺序（= 终端显示顺序，`_flat_row` 不重排）；`market` 是当轮
    市况（`ctx.marketIdxPct` / `weak`），供 analyze 模式交代环境。
    """
    from scanner.local_view_cache import load_latest

    payload = load_latest(conn)
    if not payload:
        return None
    regions = payload.get("regions") or {}
    raw = regions.get("offboard") or []
    if not isinstance(raw, list):
        return None
    rows = [_normalize_view_row(r) for r in raw if isinstance(r, dict) and (r.get("symbol") or r.get("code"))]
    if not rows:
        return None
    ctx = payload.get("ctx") or {}
    market = {
        "market_idx_pct": ctx.get("marketIdxPct"),
        "weak": ctx.get("weak"),
    }
    return str(payload.get("date") or ""), str(payload.get("time") or ""), rows, market
