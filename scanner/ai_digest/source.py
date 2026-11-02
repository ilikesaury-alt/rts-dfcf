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

__all__ = ["OffboardSourceError", "connect_readonly", "latest_signal_date", "load_offboard_rows"]

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


# ── 视图快照源（2026-11-03，用户需求：拿「最近一次终端打印」的数据分析）─────────
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
