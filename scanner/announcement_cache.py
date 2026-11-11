"""公告缓存的**单一真相**（2026-11-11，m025）：读 / 写 / 水位 / 历史迁移。

## 为什么存在

`logs/shadow_feature_cache.json`（实测 12MB / 520 票 / 每票 30 条公告 + 121 日资金流
+ 龙虎榜）的写侧是 `todo = [s for s in symbols if s not in cache]` —— **按票、命中即
永久冻结**。实测最后写入停在 2026-11-02，而 11-06 宇邦要约收购、11-10 智信回购方案
全都不在里面 ⇒ 「有缓存」等于「没有缓存」，每次离线报告还得逐票联网重查。

本模块把公告拆出来落 SQLite（m025 建表），并提供：

- `sync_symbols`：一次请求批量刷新候选票（东财 `stock_list` 支持逗号分隔多码，
  实测 3 票 1 请求 ⇒ 12 票从 12 请求压到 1），**不挂主循环**；
- `sync_market_day`：按交易日全市场分页回填（历史 / 回测用，盘中不需要）；
- `load`：**唯一读侧**，一律 `notice_date <= signal_date` 过滤（无前视）；
- `waterline` / `coverage`：读侧必须能看到「缓存截止日」，缺水位或过期显式告警，
  **绝不静默把「没取到」当成「没有公告」**；
- `migrate_legacy_json`：把 12MB JSON 里的公告部分一次性导入，幂等（hash 主键）。

## 硬约束（改错会静默算错）

1. **读侧无前视**：`notice_date <= signal_date`。缓存里含信号日之后的公告是常态
   （每天同步会写入未来公告），不过滤就是前视偏差 —— 回测结论会假显著。
2. **水位优先于「有数据」**：按票冻结的旧缓存就是因为「有数据」而永不刷新。
   读侧打印水位，过期要告警。
3. **不用裸 `except Exception`**：只 catch `scanner.utils.EXTERNAL_FAILURES`
   （含 sqlite3.Error / OSError / RequestException）；编程错误冒泡到调用方。
4. **不进主循环**：AGENTS.md「效率是前提」—— 慢手段只能进离线旁路。
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

from scanner.config import now_beijing
from scanner.utils import EXTERNAL_FAILURES

__all__ = [
    "sync_symbols",
    "sync_market_day",
    "load",
    "waterline",
    "coverage",
    "migrate_legacy_json",
]

logger = logging.getLogger(__name__)

LEGACY_CACHE = Path(__file__).resolve().parent.parent / "logs" / "shadow_feature_cache.json"


def _content_hash(symbol: str, notice_date: str, title: str, ann_type: str) -> str:
    """公告身份 = hash(symbol+date+title+type)。

    ⚠ 不能用 title 当覆盖键：上游会**重发/修订**公告（例如「更正公告」），覆盖式写入
    会抹掉历史版本，回测不可复现。`INSERT OR IGNORE` + hash 主键保留所有版本。
    """
    raw = f"{symbol}|{notice_date}|{title}|{ann_type}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]  # noqa: S324 - 非安全用途，仅做身份指纹


def _set_waterline(conn: sqlite3.Connection, scope: str, end_date: str, rows: int) -> None:
    conn.execute(
        "INSERT INTO ann_sync_waterline (scope, end_date, rows, updated) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(scope) DO UPDATE SET end_date = excluded.end_date, "
        "rows = excluded.rows, updated = excluded.updated",
        (scope, end_date, rows, now_beijing().isoformat(timespec="seconds")),
    )


def waterline(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """`{scope: {end_date, rows, updated}}`。空字典 = 从未同步过。"""
    try:
        rows = conn.execute("SELECT scope, end_date, rows, updated FROM ann_sync_waterline").fetchall()
    except sqlite3.Error as e:
        logger.warning("公告水位读取失败: %s", e)
        return {}
    return {str(r[0]): {"end_date": str(r[1]), "rows": int(r[2] or 0), "updated": str(r[3])} for r in rows}


def sync_symbols(
    conn: sqlite3.Connection,
    symbols: list[str],
    *,
    days: int = 30,
    fetcher: Callable[..., dict[str, list[dict[str, Any]]]] | None = None,
) -> dict[str, Any]:
    """批量刷新候选票公告 → 落 `announcement_cache` + 更新水位 `scope='candidates'`。

    `fetcher` 可注入（测试喂假数据，零联网）。默认在调用时才 import，避免模块级
    循环依赖；也便于 monkeypatch `scanner.ai_digest.em_data.fetch_announcements_batch`。

    返回 `{"rows": 新写入行数, "waterline": 本次覆盖到的日期, "symbols": 请求票数的,
    "failed": 是否疑似取数失败}`。**取数失败不推进水位**（否则下次会以为已同步）。
    """
    from scanner.ai_digest.em_data import fetch_announcements_batch as _default_fetcher

    ordered = list(dict.fromkeys(symbols))
    if not ordered:
        return {"rows": 0, "waterline": None, "symbols": 0, "failed": False}
    fn = fetcher or _default_fetcher
    try:
        fetched = fn(ordered) if fetcher is not None else fn(ordered, days=days)
    except EXTERNAL_FAILURES as e:
        logger.warning("公告批量同步失败（保留旧水位，下次重试）: %s", e)
        return {"rows": 0, "waterline": None, "symbols": len(ordered), "failed": True}

    now_t = now_beijing().isoformat(timespec="seconds")
    payload: list[tuple] = []
    for sym, items in (fetched or {}).items():
        for it in items or []:
            d = str(it.get("date") or "")[:10]
            title = str(it.get("title") or "").strip()
            if not d or not title:
                continue
            ann_type = str(it.get("type") or "")
            payload.append(
                (
                    sym,
                    d,
                    title,
                    ann_type,
                    str(it.get("source_url") or ""),
                    _content_hash(sym, d, title, ann_type),
                    now_t,
                )
            )
    has_data = bool(payload)
    end_date = now_beijing().date().isoformat()
    try:
        if payload:
            conn.executemany(
                "INSERT OR IGNORE INTO announcement_cache "
                "(symbol, notice_date, title, ann_type, source_url, content_hash, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                payload,
            )
        if has_data:
            _set_waterline(conn, "candidates", end_date, len(payload))
        conn.commit()
    except sqlite3.Error as e:
        logger.warning("公告缓存落库失败（不影响调用方，下轮重试）: %s", e)
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        return {"rows": 0, "waterline": None, "symbols": len(ordered), "failed": True}
    return {
        "rows": len(payload),
        "waterline": end_date if has_data else None,
        "symbols": len(ordered),
        "failed": not has_data,
    }


def sync_market_day(
    conn: sqlite3.Connection,
    day: str,
    *,
    gem_only: bool = True,
    fetcher: Callable[..., tuple[list[dict[str, Any]], int]] | None = None,
    max_pages: int = 50,
) -> dict[str, Any]:
    """按交易日全市场分页回填（历史建库用；盘中候选请用 `sync_symbols`）。

    ⚠ 2026-11-11 实测单日 900 条（page_size=100 ⇒ 9 页）。articles 的 `codes` 可能
    关联多票；`gem_only=True` 时只留 300/301 开头，避免把沪深主板灌进 B 段缓存。
    """
    from scanner.ai_digest.em_data import fetch_market_announcements as _default_fetcher

    fn = fetcher or _default_fetcher
    page, total, rows_out = 1, None, 0
    payload: list[tuple] = []
    now_t = now_beijing().isoformat(timespec="seconds")
    try:
        while page <= max_pages:
            if fetcher is not None:
                items, t = fn(day, page)
            else:
                items, t = fn(day, page=page)
            if total is None:
                total = t
            if not items:
                break
            for it in items:
                codes = [str(c) for c in (it.get("codes") or [])]
                for code in codes:
                    if gem_only and not code.startswith(("300", "301")):
                        continue
                    sym = f"SZ{code}" if code.startswith(("300", "301")) else code
                    d = str(it.get("date") or day)[:10]
                    title = str(it.get("title") or "").strip()
                    if not d or not title:
                        continue
                    ann_type = str(it.get("type") or "")
                    payload.append((sym, d, title, ann_type, "", _content_hash(sym, d, title, ann_type), now_t))
            if total is not None and page * 100 >= total:
                break
            page += 1
    except EXTERNAL_FAILURES as e:
        logger.warning("全市场公告回填失败（day=%s，保留旧水位）: %s", day, e)
        return {"rows": 0, "waterline": None, "failed": True}
    if not payload:
        return {"rows": 0, "waterline": None, "failed": True}
    try:
        conn.executemany(
            "INSERT OR IGNORE INTO announcement_cache "
            "(symbol, notice_date, title, ann_type, source_url, content_hash, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            payload,
        )
        _set_waterline(conn, "market_daily", day, len(payload))
        conn.commit()
        rows_out = len(payload)
    except sqlite3.Error as e:
        logger.warning("全市场公告落库失败: %s", e)
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        return {"rows": 0, "waterline": None, "failed": True}
    return {"rows": rows_out, "waterline": day, "failed": False}


def load(
    conn: sqlite3.Connection,
    symbols: list[str],
    *,
    signal_date: str,
    window: int = 10,
) -> dict[str, list[dict[str, Any]]]:
    """读侧**唯一入口**：`[signal_date-window, signal_date]` 内的公告，按日期降序。

    ⚠⚠ **无前视**：`notice_date <= signal_date` 是硬过滤。缓存里含信号日之后的公告
    是常态（每天同步都会写入），不过滤即前视偏差 —— 任何基于它的检验都会假显著。
    改动本函数必须同步跑 `tests/test_ai_digest.py` 的公告缓存无前视守卫。
    """
    ordered = list(dict.fromkeys(symbols))
    out: dict[str, list[dict[str, Any]]] = {s: [] for s in ordered}
    if not ordered:
        return out
    lo = (date.fromisoformat(signal_date) - timedelta(days=max(0, window))).isoformat()
    marks = ",".join("?" for _ in ordered)
    sql = (
        f"SELECT symbol, notice_date, title, ann_type FROM announcement_cache "  # noqa: S608 - 占位符拼接，表名为常量
        f"WHERE symbol IN ({marks}) AND notice_date <= ? AND notice_date >= ? "
        f"ORDER BY notice_date DESC, symbol"
    )
    try:
        rows = conn.execute(sql, (*ordered, signal_date, lo)).fetchall()
    except sqlite3.Error as e:
        logger.warning("公告缓存读取失败（退化为无公告，水位告警由 coverage 负责）: %s", e)
        return out
    for sym, d, title, ann_type in rows:
        out.setdefault(str(sym), []).append({"date": str(d), "title": str(title), "type": str(ann_type or "")})
    return out


def coverage(conn: sqlite3.Connection, symbols: list[str], *, signal_date: str) -> dict[str, Any]:
    """报告头部用：水位 + 每票条数 + 是否**可疑过期**（缺水位 / 水位落后信号日 > 1 天）。

    `stale=True` 时必须显式告警，绝不能静默把「缓存没数据」读成「公司没公告」。
    """
    wl = waterline(conn)
    scopes = [wl[s] for s in ("candidates", "market_daily", "legacy") if s in wl]
    latest = max((w["end_date"] for w in scopes), default=None)
    counts: dict[str, int] = {}
    if symbols:
        marks = ",".join("?" for _ in symbols)
        try:
            for sym, n in conn.execute(
                f"SELECT symbol, COUNT(*) FROM announcement_cache "  # noqa: S608 - 占位符拼接
                f"WHERE symbol IN ({marks}) AND notice_date <= ? GROUP BY symbol",
                (*symbols, signal_date),
            ):
                counts[str(sym)] = int(n)
        except sqlite3.Error as e:
            logger.warning("公告覆盖度统计失败: %s", e)
    stale = True
    if latest:
        try:
            stale = (date.fromisoformat(signal_date) - date.fromisoformat(latest)).days > 1
        except ValueError:
            stale = True
    return {
        "waterline": wl,
        "latest": latest,
        "counts": counts,
        "stale": stale,
        "missing": [s for s in dict.fromkeys(symbols) if not counts.get(s)],
    }


def migrate_legacy_json(conn: sqlite3.Connection, path: Path | str = LEGACY_CACHE) -> dict[str, Any]:
    """把旧 `logs/shadow_feature_cache.json` 的公告部分导入新表（**幂等**，不动 flow/dtb）。

    实测该文件 12MB / 520 票 / 最后写入 2026-11-02；历史公告仍是有价值的回测原料，
    直接删掉可惜。导入后 `sync_symbols` 按水位补 11-02 之后的新公告，旧 JSON 的公告
    部分即可废弃（flow/dtb 仍归原脚本使用，不改其结构）。
    """
    p = Path(path)
    if not p.exists():
        return {"rows": 0, "symbols": 0, "latest": None, "error": "文件不存在"}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning("旧公告缓存解析失败: %s", e)
        return {"rows": 0, "symbols": 0, "latest": None, "error": f"{type(e).__name__}: {e}"}
    now_t = now_beijing().isoformat(timespec="seconds")
    payload: list[tuple] = []
    latest: str | None = None
    for sym, v in (raw or {}).items():
        for it in (v or {}).get("ann") or []:
            d = str(it.get("date") or "")[:10]
            title = str(it.get("title") or "").strip()
            if not d or not title:
                continue
            ann_type = str(it.get("type") or "")
            payload.append((str(sym), d, title, ann_type, "", _content_hash(str(sym), d, title, ann_type), now_t))
            if latest is None or d > latest:
                latest = d
    if not payload:
        return {"rows": 0, "symbols": 0, "latest": None, "error": None}
    try:
        conn.executemany(
            "INSERT OR IGNORE INTO announcement_cache "
            "(symbol, notice_date, title, ann_type, source_url, content_hash, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            payload,
        )
        _set_waterline(conn, "legacy", latest or now_beijing().date().isoformat(), len(payload))
        conn.commit()
    except sqlite3.Error as e:
        logger.warning("旧公告缓存迁移失败: %s", e)
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        return {"rows": 0, "symbols": 0, "latest": None, "error": f"{type(e).__name__}: {e}"}
    return {"rows": len(payload), "symbols": len({r[0] for r in payload}), "latest": latest, "error": None}
