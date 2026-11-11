"""候选票公告预取（2026-11-11，m025 公告缓存的**主循环侧**写入口）。

## 为什么存在

`announcement_cache`（m025，读侧 `scanner/announcement_cache.py`）只覆盖「历史上曾进过
候选池的票」：实测 2026-11-11 库内 15651 行 / 529 只，而当日候选里的 `SZ300613`、
`SZ300415` 都是 **0 行** ⇒ `ai_digest` 的 `get_announcements` 每次跑这几只票都要联网
（东财 `np-anotice-stock`，实测约 1.2s/票，多票累加很可观）。

本模块在**主循环产出候选之后**把这批票的公告异步预取进缓存，使 AI 模块的公告查询
稳定命中本地、零联网。慢手段（网络）留在后台线程，主循环节拍不受影响。

## 三条硬约束（改动前必读）

1. **不阻塞主循环**。`request_prefetch` 只做「取票 → 过闸 → 起 daemon 线程」，
   零网络、零 DB；网络与落库全在后台线程里。**不得**在 `request_prefetch` 里加
   `join`/`sleep`/同步外呼 —— 60 秒一轮的单线程串行渲染会被整轮拖垮。
2. **fail-open**。只 catch `scanner.utils.EXTERNAL_FAILURES`（OSError/timeout/
   requests/sqlite3.Error/ValueError/KeyError）。网络挂了、超时、库被锁 ⇒ 记一条
   warning 并在下一个节流窗口重试，**绝不**影响扫描主循环。禁裸 `except Exception`
   （编程错误必须冒泡，否则一个未定义变量会让预取静默变成空转）。
3. **不新建表、不另写取数**。落库一律走 `announcement_cache.sync_symbols`
   （批量请求 + `INSERT OR IGNORE` + 水位 `scope='candidates'`）。那条路径的
   **「取数失败不推进水位」**语义是读侧判断新鲜度的唯一依据，不能被这里绕过或破坏。

## 挂点与票集

挂点为 `unified_scanner.run_scanner` 里 **`display(...)` 之后**（与 `save_panel_round`
同侧）：那里 `ScanView` 已是终稿，四区（v1 池选 / 榜内飙升 / 榜内异动 / 榜外异动）
都已按展示上限截断 ⇒ `symbols_from_view` 取到的正是**终端实际展示**、也正是
`ai_digest`（panel / print / view 三个数据源）会分析的那批票，而不是全市场
（全市场回填走 `announcement_cache.sync_market_day`，属离线建库，与本模块无关）。
刻意不挂 `offboard_watch.persist_round`：它只覆盖 B 段（榜外异动），拿不到 v1 池选与
A/C 段，会漏掉 AI 的主要分析对象。

## 并发与节流

- **单飞**：同一时刻只允许一个预取线程（`_BUSY`）。上一批还没跑完（网络慢/超时）时
  新请求直接丢弃，不会每轮叠一个线程把外呼量翻倍。
- **独立短连接**：后台线程**不碰主循环的 `conn`**（sqlite3 连接默认不允许跨线程使用，
  且共用连接上 `commit()` 会把主循环未打算本轮提交的写提前落库 —— `local_view_cache`
  踩过同一个坑）。worker 自开连接、用完即关，WAL + `busy_timeout=10000` 兜写锁竞争。
- **节流**：两道闸。①**按票按交易日**：已成功预取的票当天不再请求（`_DONE`）；
  ②**整批 N 秒**（`ANN_PREFETCH_MIN_INTERVAL_SEC`，缺省 300s）：60 秒一轮最多 5 分钟
  一次外呼。⚠ 失败**不计入** `_DONE` —— 「没取到」不等于「没有公告」，下一个窗口重试。

两条都不依赖 `single_instance` 锁：它保证的是「一个进程」，本模块保证的是
「一个进程内一批一次」—— 多进程跑同一份 DB 时由 WAL 写锁 + `busy_timeout` 兜底。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections.abc import Callable, Sequence
from typing import Any

from scanner.announcement_cache import sync_symbols
from scanner.utils import EXTERNAL_FAILURES

__all__ = [
    "prefetch_symbols",
    "request_prefetch",
    "symbols_from_view",
]

logger = logging.getLogger(__name__)

#: 进程内节流状态。`_DONE` 的值是**交易日**（`YYYY-MM-DD`）：跨日自动失效。
_LOCK = threading.Lock()
_DONE: dict[str, str] = {}
_BUSY = False
_LAST_ATTEMPT = 0.0  # time.monotonic() 基准，与墙钟无关（NTP 跳变不影响节流）

#: 视图里承载「终端实际展示的票」的三段（沪深飙升：榜内飙升 / 榜内异动 / 榜外异动）。
_VIEW_ROWS: tuple[str, ...] = ("hot_rows", "offboard_rows", "onboard_rows")

#: 预取结果类型（`announcement_cache.sync_symbols` 的返回形状 + `requested`）。
Result = dict[str, Any]
Fetcher = Callable[..., dict[str, list[dict[str, Any]]]]


def _cfg():
    """调用时读 `scanner.config` 模块属性（测试可 monkeypatch，与 `panel_md` 同款）。"""
    from scanner import config

    return config


def _today() -> str:
    from scanner.config import now_beijing

    return now_beijing().date().isoformat()


def symbols_from_view(view: Any) -> list[str]:
    """`ScanView` → 终端本轮实际展示的票（去重、保序）。

    - `main_rows`（v1 池选）：票码在行对象的 `entry["symbol"]`（`RecommendationRow`）；
    - `hot_rows` / `offboard_rows` / `onboard_rows`（沪深飙升三段）：候选对象带 `.symbol`。

    刻意**不含** `hist_rows`（v1 回捞）：终端自 2026-09-28 起已隐藏该区、`panel_md`
    的面板原文里也没有它 ⇒ 它不在「AI 会看到的那批票」里（飞书卡片仍画，但那是另一条
    通路，不在本模块的职责范围内）。
    """
    out: list[str] = []
    for row in getattr(view, "main_rows", None) or []:
        entry = getattr(row, "entry", None)
        sym = entry.get("symbol") if isinstance(entry, dict) else None
        if sym:
            out.append(str(sym))
    for attr in _VIEW_ROWS:
        for cand in getattr(view, attr, None) or []:
            sym = getattr(cand, "symbol", "")
            if sym:
                out.append(str(sym))
    return list(dict.fromkeys(out))


def prefetch_symbols(
    symbols: Sequence[str],
    *,
    fetcher: Fetcher | None = None,
    days: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> Result:
    """**阻塞**地同步一批票的公告（后台线程的实质工作 / 单测与脚本的同步入口）。

    `conn` 缺省时自开连接 —— 后台线程必须走这条：主循环的连接不可跨线程共用。
    显式传入则复用之（仅限**同一个线程内**调用；单测喂内存库即此路）。

    `fetcher` 透传给 `announcement_cache.sync_symbols`（测试喂假数据 ⇒ 零联网）。
    返回同 `sync_symbols` 的形状 + `requested`（本次请求的票数）。任何外部失败
    （网络/库锁）只记 warning 并返回 `failed=True`，不上抛。
    """
    ordered = list(dict.fromkeys(str(s) for s in symbols if s))
    if not ordered:
        return {"rows": 0, "waterline": None, "symbols": 0, "requested": 0, "failed": False}

    window = _cfg().ANN_PREFETCH_DAYS if days is None else days
    try:
        if conn is not None:
            result = sync_symbols(conn, ordered, days=window, fetcher=fetcher)
        else:
            from scanner.db.schema import get_conn  # 调用时读 config.DB_PATH，测试可 patch

            own = get_conn()
            try:
                result = sync_symbols(own, ordered, days=window, fetcher=fetcher)
            finally:
                try:
                    own.close()
                except sqlite3.Error:
                    pass  # 关闭失败无害（连接随线程结束回收）
    except EXTERNAL_FAILURES as e:
        logger.warning("候选公告预取失败（扫描不受影响，下一个节流窗口重试）: %s", e)
        return {"rows": 0, "waterline": None, "symbols": len(ordered), "requested": len(ordered), "failed": True}

    if not result.get("failed"):
        _mark_done(ordered)
    return {**result, "requested": len(ordered)}


def request_prefetch(view: Any, *, fetcher: Fetcher | None = None) -> bool:
    """主循环挂点：**非阻塞**触发一批预取。返回是否真的起了线程（单测可断言）。

    三道闸全在这里：开关关闭 / 本批票当天已全部预取过 / 节流窗口内或已有单飞线程
    ⇒ 直接返回 False，零网络零 DB。通过闸才起 daemon 线程，主循环立刻继续渲染。
    """
    global _BUSY, _LAST_ATTEMPT
    try:
        # 读配置放在锁外：`_cfg()` 是 `from scanner import config`（首次用时可能触发
        # 模块导入），持锁做导入会给主循环制造无谓的锁窗口。
        cfg = _cfg()
        if not cfg.ANN_PREFETCH_ENABLED:
            return False
        symbols = symbols_from_view(view)
        if not symbols:
            return False
        day = _today()
        with _LOCK:
            pending = _pending_locked(symbols, day)
            if not pending or _BUSY:
                return False
            now = time.monotonic()
            if now - _LAST_ATTEMPT < cfg.ANN_PREFETCH_MIN_INTERVAL_SEC:
                return False
            _LAST_ATTEMPT = now
            _BUSY = True
    except EXTERNAL_FAILURES as e:
        logger.warning("候选公告预取触发失败（扫描不受影响）: %s", e)
        return False

    started = False
    try:
        _start_worker(pending, fetcher)
        started = True
    except EXTERNAL_FAILURES as e:
        logger.warning("候选公告预取线程启动失败（扫描不受影响）: %s", e)
    finally:
        # 没起来就立刻释放单飞闸 —— 否则预取被永久关掉且无声。
        # 编程错误（`RuntimeError: can't start new thread` 等）走同一分支释放闸后
        # **原样上抛**：纪律是只 fail-open 外部依赖，bug 必须冒泡让主循环记录。
        if not started:
            _clear_busy()
    return started


# ── 内部：节流状态 / 线程 ────────────────────────────────────────────────────


def _pending_locked(symbols: Sequence[str], day: str) -> list[str]:
    """当天还没成功预取过的票（调用方必须已持 `_LOCK`）。跨日清空 `_DONE`。"""
    if any(d != day for d in _DONE.values()):
        _DONE.clear()
    return [s for s in symbols if _DONE.get(s) != day]


def _mark_done(symbols: Sequence[str]) -> None:
    """记「这批票今天已成功预取」。**失败不记**：下个窗口重试。"""
    day = _today()
    with _LOCK:
        for s in symbols:
            _DONE[s] = day


def _clear_busy() -> None:
    global _BUSY
    with _LOCK:
        _BUSY = False


def _start_worker(symbols: Sequence[str], fetcher: Fetcher | None) -> threading.Thread:
    """起 daemon 线程（单测可替换本函数使其同步执行，避免线程竞态断言）。"""
    t = threading.Thread(target=_run, args=(tuple(symbols), fetcher), name="ann-prefetch", daemon=True)
    t.start()
    return t


def _run(symbols: tuple[str, ...], fetcher: Fetcher | None) -> None:
    """后台线程体：预取 + 释放单飞闸。

    ⚠ 必须自己兜住 `EXTERNAL_FAILURES`：线程里未捕获的异常会把 traceback 打到 stderr，
    而 stderr 与终端面板共用同一块屏幕（主循环 `clear_screen` 每轮重绘）—— 一次网络
    抖动就能把 `◆ v1 池选` 表打散。编程错误仍会上抛（纪律：不吞 bug）。
    """
    try:
        prefetch_symbols(symbols, fetcher=fetcher)
    except EXTERNAL_FAILURES as e:
        logger.warning("候选公告预取线程异常（扫描不受影响）: %s", e)
    finally:
        _clear_busy()


def _reset_state() -> None:
    """清节流状态（**仅测试用**：模拟进程重启后的空状态）。"""
    global _BUSY, _LAST_ATTEMPT
    with _LOCK:
        _DONE.clear()
        _BUSY = False
        _LAST_ATTEMPT = 0.0
