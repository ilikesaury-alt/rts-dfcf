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
