"""桌面版本地面板：**进程内视图缓存**（Issue A，2026-10-19）。

主循环每轮算好 `ScanView` 后调用 `cache_latest(serialize_view(view, extra=...))`，
`scanner/local_panel_server` 的只读 HTTP 线程直接回放**同一份**载荷 —— 与云端上报
收到的那份是**同一个函数的输出**（design §6.2 P1：零漂移，不复制第二条构建链路）。

三条纪律（改动前必读）：

1. **单写多读**：只有主循环写（`cache_latest`），HTTP 线程读（`get_latest`）。
   CPython 下引用赋值本身是原子的，但仍加锁 —— 读侧要保证拿到「整份一致」的快照，
   而不是一个半更新的对象。`get_latest()` 返回的是**同一对象引用**（不深拷贝，
   载荷可达数百 KB）：调用方**只读**，不得就地改写。
2. **失败不阻断扫描**：`cache_latest` 自身不抛；`persist_latest` 的 DB 失败只告警
   （缓存是旁路，与 `panel_reporter` 同纪律）。
3. **不新增第二结论源**：这里只缓存已有载荷，不做任何筛选/评分/排序/改写。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from typing import Any

from scanner.config import now_beijing

_log = logging.getLogger("local_panel")

# `view_snapshot` 表只作**重启回放**用（m021）：保留最近 N 轮足够回答「上次看到的是
# 什么」，再多的历史由 `recommendations`/`daily_kline` 等正表承担，不该在这里长第二份。
_SNAPSHOT_LIMIT = 50

_lock = threading.Lock()
_latest: dict[str, Any] | None = None


def cache_latest(payload: dict[str, Any]) -> None:
    """写入本轮载荷（主循环调用）。永不抛异常。"""
    global _latest
    with _lock:
        _latest = payload


def get_latest() -> dict[str, Any] | None:
    """读最近一轮载荷；首轮完成前返回 `None`（调用方必须按「尚未就绪」处理）。"""
    with _lock:
        return _latest


def clear_cache() -> None:
    """清空进程内缓存（**仅测试用**：模拟进程重启后的空缓存）。"""
    global _latest
    with _lock:
        _latest = None


def persist_latest(payload: dict[str, Any], db_path: str | None = None) -> None:
    """把本轮载荷落 `view_snapshot`（m021）。**fail-open**：失败只告警。

    落库动机只有一个：进程重启后、首个交易轮完成前面板不白屏（`server` 启动时
    用 `load_latest` 回填）。因此这里**不是**上报通道，也不参与任何口径。

    ⚠ **自开短连接、自管事务**（2026-10-19 对抗审查修复）：不在主循环的共享连接上
    写 —— 早期版本接收调用方 `conn` 并无条件 `commit()`，会把挂点时刻主连接上
    **未打算本轮提交**的写一并提前落库，破坏轮内原子性（当时逐点核对无实害，
    但属无守卫的跨模块隐式契约）。现与 `local_panel_server._connect()` 同口径：
    每次落库独立连接，用完即关；`db_path` 缺省时调用时读 `config.DB_PATH`
    （测试可 patch），传入则用于测试隔离。
    """
    from scanner.db.schema import get_conn

    try:
        conn = get_conn(db_path)
        try:
            conn.execute(
                "INSERT INTO view_snapshot(date, time, seq, payload, updated_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(date, time) DO UPDATE SET "
                "seq=excluded.seq, payload=excluded.payload, updated_at=excluded.updated_at",
                (
                    str(payload.get("date") or ""),
                    str(payload.get("time") or ""),
                    int(payload.get("seq") or 0),
                    json.dumps(payload, ensure_ascii=False),
                    now_beijing().isoformat(),
                ),
            )
            # 只留最近 N 轮（≤N 行时子查询返回全部 rowid，DELETE 天然为空转）
            conn.execute(
                "DELETE FROM view_snapshot WHERE rowid NOT IN "
                "(SELECT rowid FROM view_snapshot ORDER BY date DESC, time DESC LIMIT ?)",
                (_SNAPSHOT_LIMIT,),
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as e:
        _log.warning("view_snapshot 落库失败（不影响扫描）: %r", e)


def load_latest(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """取库里最近一轮载荷（重启回放用）。损坏/缺表 → `None`。"""
    try:
        row = conn.execute("SELECT payload FROM view_snapshot ORDER BY date DESC, time DESC LIMIT 1").fetchone()
    except sqlite3.Error:
        return None
    if not row:
        return None
    try:
        parsed = json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None
