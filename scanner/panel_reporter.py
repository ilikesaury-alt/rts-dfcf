"""面板上报器（design §3.1 D1 / §3.3 体积探针 / T0.3·T0.4·T4.2）。

**全仓唯一持有面板网络与密钥的模块。** 纪律（design §3.1 反模式清单，违反即回退）：

- `report()` **任何异常都不上抛**（G3 旁路隔离）：序列化失败、DB 读失败、网络失败
  一律只写 `logs/panel_report.log`，主循环继续下一轮。
- 序列化在**调用线程内**（后台线程就测不到 ≤50ms 预算了）；网络在 **daemon 线程**内。
- 线程最坏时长 `8+1+8+3+8 = 28s` < 轮间隔 60s ⇒ 不堆积。
- 日志**只记状态码与字节数**，`RTS_PANEL_SECRET` / `INGEST_SECRET` 永不入日志。
- 用 `requests`（R4：不引入 httpx）。
- 密钥比较在云端用常数时间（`auth.safeEqual`），本端不参与比较。

三开关（勿新增第四个）：`RTS_PANEL_URL/SECRET` 未配置 → **连本模块都不导入**；
`RTS_PANEL=0` → 关闭；`--no-panel` → CLI 关闭（优先级最高）。
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from typing import Any

import requests

from scanner.config import LOG_DIR
from scanner.panel_serialize import (
    BODY_LIMIT,
    STOCK_KLINE_DAYS,
    STOCK_QUOTA,
    enforce_body_limit,
    enforce_stock_quota,
    serialize_view,
)

# FR-C1：退避 1s / 3s（首次 + 2 次重试 = 3 次尝试）。
_BACKOFF = (1, 3)
# FR-C1：连接 3s / 读取总 8s（requests 必须显式 timeout，见 A.3）。
_TIMEOUT = (3, 8)
# 序列化预算（design §3.1）：超了只告警，不丢轮 —— 丢轮会让面板滞后 1 轮起步。
_SERIALIZE_BUDGET_MS = 50.0
# 出现史条数（详情页展示用，design §5.1 `appearances`）。
_APPEARANCE_LIMIT = 20

_log = logging.getLogger("panel")
# 进程内连接复用（DR-2）。`trust_env=False`：不读 HTTP(S)_PROXY —— 面板是本机 → Cloudflare
# 的直连出口，走代理只会把上报打进企业代理的日志（多一份密钥副本）。
_sess = requests.Session()
_sess.trust_env = False


def _ensure_file_log() -> None:
    """把 panel logger 挂到 `logs/panel_report.log`（幂等，模块首次使用时建一次）。

    项目根 logger 未统一配置（各模块只是 `getLogger`），故本模块**自备**落盘通道：
    面板失败必须留证据（T0.9「拔网线 10min 只进 panel_report.log」）。
    """
    if getattr(_log, "_panel_handler", False):
        return
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        handler = logging.FileHandler(os.path.join(LOG_DIR, "panel_report.log"), encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        _log.addHandler(handler)
        _log.setLevel(logging.INFO)
        _log.propagate = False
    except OSError:
        pass  # 落不了盘就只剩 stderr —— fail-open，不阻断扫描
    _log._panel_handler = True  # type: ignore[attr-defined]


# ── 增量收集器：sent 表（进程内，design §3.1 第 4 条 / T4.2）────────────────
# 语义：`sent[symbol] = 已成功上报的 daily_kline 最新日期`。
# 云端 `kline_cache` 何时过期本地**不知道**（它在 D1 里），也不该知道（那会让
# 单向旁路变成双向）⇒ 用「本地 K 线是否前进」近似：**只有真的前进过才重发**。
_sent: dict[str, str] = {}
_sent_lock = threading.Lock()


def sent_snapshot() -> dict[str, str]:
    """`sent` 只读快照（测试/排查用）。"""
    with _sent_lock:
        return dict(_sent)


def reset_sent() -> None:
    """清空 `sent`（仅测试用：模拟进程重启后的收敛行为）。"""
    with _sent_lock:
        _sent.clear()


def _mark_sent(sym_date: dict[str, str]) -> None:
    with _sent_lock:
        _sent.update(sym_date)


def _view_symbols(view: Any) -> list[tuple[str, bool, int, int]]:
    """四区块去重后的 `(symbol, is_new, region_idx, order)` 列表。

    **排序键 = 新票优先 → 区块序（池选/回捞/飙升A/榜外）→ 出现次数**（design §3.1
    增量收集器第 2 条）。新区块序与终端阅读顺序一致，故配额紧张时留的是用户最可能点的票。
    """
    out: list[tuple[str, bool, int, int]] = []
    seen: dict[str, int] = {}

    def _sym_and_new(r: Any) -> tuple[str, bool]:
        """取 `(symbol, is_new_entry)` —— 兼容**两种**行形态。

        主线行是 `MainRow`（symbol 在 `entry` 子字典里），三区行是裸 dataclass
        （symbol 是顶层属性）。`view` 的字段类型本就异构（`hot_rows: list | None`），
        这里不做 isinstance 分派去猜类型，而是「先看是不是 dict-like，否则取属性」。
        """
        if isinstance(r, dict):
            return str(r.get("symbol") or ""), bool(r.get("is_new_entry"))
        entry = getattr(r, "entry", None)
        if isinstance(entry, dict):
            return str(entry.get("symbol") or ""), bool(getattr(r, "is_new_entry", False))
        return str(getattr(r, "symbol", "") or ""), bool(getattr(r, "is_new_entry", False))

    def feed(rows, region_idx: int) -> None:
        for i, r in enumerate(rows or []):
            sym, is_new = _sym_and_new(r)
            if not sym:
                continue
            if sym in seen:
                seen[sym] += 1
                # 已在更靠前的区块出现过 → 不重复登记，也不夺走其区块序
                continue
            seen[sym] = 1
            out.append((sym, is_new, region_idx, i))

    feed(view.main_rows, 0)
    feed(view.hist_rows, 1)
    feed(view.hot_rows, 2)
    feed(view.offboard_rows, 3)
    return out


def _klines_for(conn: sqlite3.Connection, symbols: list[str]) -> dict[str, list]:
    """读 `daily_kline` 近 `STOCK_KLINE_DAYS` 根（单次批量 SQL，零外网补拉）。"""
    from scanner.db.queries import get_cached_klines

    bars = get_cached_klines(conn, symbols)
    out: dict[str, list] = {}
    for sym, seq in bars.items():
        if not seq:
            continue
        last = seq[-STOCK_KLINE_DAYS:]
        out[sym] = [[b["date"], b["open"], b["close"], b["low"], b["high"], b["volume"]] for b in last]
    return out


def _appearances_for(conn: sqlite3.Connection, symbol: str, limit: int = _APPEARANCE_LIMIT) -> list[dict]:
    """该票的近期出现史（详情页用；只读本地 `recommendations`，零外网）。"""
    try:
        cur = conn.execute(
            "SELECT date, time, category, score, percent FROM recommendations "
            "WHERE symbol = ? ORDER BY date DESC, time DESC LIMIT ?",
            (symbol, limit),
        )
        return [dict(zip(("date", "time", "category", "score", "percent"), r, strict=True)) for r in cur.fetchall()]
    except sqlite3.Error:
        return []


def collect_stocks(view: Any, conn: sqlite3.Connection | None) -> dict[str, dict]:
    """本轮要上报的 `stocks`（≤20 票）：未送过 / K 线已前进 的票，新票优先。

    候选条件（design §3.1 增量收集器第 1 条）：
      ① `symbol not in sent`（首次，或进程重启后重新收敛）
      ② `daily_kline[symbol] 最新日期 > sent[symbol]`（新交易日，该票 K 线已前进）
    无 K 线的票**跳过且不阻塞**（详情页返回 404 `NOT_IN_REPORT`，不是错误）。
    """
    if conn is None:
        return {}
    cands = _view_symbols(view)
    if not cands:
        return {}
    order = [c[0] for c in cands]
    is_new = {c[0]: c[1] for c in cands}
    klines = _klines_for(conn, order)
    with _sent_lock:
        already = dict(_sent)

    need: list[str] = []
    for sym in order:
        if sym not in klines:
            continue
        if sym not in already or klines[sym][-1][0] > already[sym]:
            need.append(sym)
    if not need:
        return {}

    picked: dict[str, dict] = {}
    for sym in need[: STOCK_QUOTA * 2]:  # 多取一点，好让下面的「新票优先」截断有选择余地
        picked[sym] = {
            "klineDate": klines[sym][-1][0],
            "kline": klines[sym],
            "appearances": _appearances_for(conn, sym),
            "_new": is_new.get(sym, False),
        }
    out = enforce_stock_quota(picked, quota=STOCK_QUOTA, new_first={s for s in picked if is_new.get(s)})
    for v in out.values():
        v.pop("_new", None)  # 内部排序标记，不进契约
    return out


# ── 上报 ──────────────────────────────────────────────────────────────────


def report(
    view: Any,
    *,
    url: str,
    secret: str,
    conn: sqlite3.Connection | None = None,
    stocks: dict[str, dict] | None = None,
    **extra: Any,
) -> None:
    """主循环调用点。**任何异常都不上抛**（G3）。

    序列化在本线程（≤50ms 预算可测）；网络在 daemon 线程。

    ⚠ **两段耗时必须分开计时**（2026-09-29 实施修正）：`collect_stocks` 要读
    `daily_kline` + `recommendations`（DB I/O，实测 200~300ms），它与
    「序列化」是两回事。若把计时起点放在收集之前，DB 耗时会被记到序列化头上 ——
    实测把 1.1ms 的序列化报成 250~600ms，整轮都在刷 budget 告警，**告警也就废了**。
    故：预算只对序列化段生效；收集段单独计时、单独记录（见下）。
    """
    _ensure_file_log()
    try:
        t_collect = time.perf_counter()
        if stocks is None:
            stocks = collect_stocks(view, conn)
        collect_ms = (time.perf_counter() - t_collect) * 1000

        t0 = time.perf_counter()
        payload = serialize_view(view, extra={**extra, "stocks": stocks})
        payload, dropped = enforce_body_limit(payload, limit=BODY_LIMIT)
        body = json.dumps(payload, ensure_ascii=False).encode()
        ms = (time.perf_counter() - t0) * 1000
        if ms > _SERIALIZE_BUDGET_MS:
            _log.warning(
                "serialize %.1fms > %.0fms budget (%dB, %d stocks dropped, collect %.1fms)",
                ms,
                _SERIALIZE_BUDGET_MS,
                len(body),
                dropped,
                collect_ms,
            )
        else:
            _log.info(
                "serialize %.1fms %dB stocks=%d collect=%.1fms",
                ms,
                len(body),
                len(payload.get("stocks") or {}),
                collect_ms,
            )
    except Exception:  # 序列化失败同样不打断扫描
        _log.exception("panel: serialize failed, round dropped")
        return

    ack = {s: v["klineDate"] for s, v in (payload.get("stocks") or {}).items() if "klineDate" in v}
    threading.Thread(target=_send, args=(url, secret, body, ack), daemon=True).start()


def _send(url: str, secret: str, body: bytes, ack: dict[str, str]) -> bool:
    """POST + 退避重试。返回是否成功（成功才更新 `sent`，失败则下轮重发 → 天然自愈）。

    4xx **不重试**：`400` = 契约/schema 不符、`401` = 密钥错 —— 重试 3 次只是把同一个
    错误打三遍，密钥错时重试还可能落在轮换窗口里造成 401 噪声。
    """
    for attempt in range(len(_BACKOFF) + 1):  # 首次 + 2 次重试
        try:
            r = _sess.post(
                url,
                data=body,
                timeout=_TIMEOUT,
                headers={"Authorization": f"Bearer {secret}", "Content-Type": "application/json"},
            )
            if r.status_code < 300:
                _log.info("panel: ok %d %dB", r.status_code, len(body))
                if ack:
                    _mark_sent(ack)
                return True
            _log.error("panel: %d %s", r.status_code, (r.text or "")[:200])
            if 400 <= r.status_code < 500:
                return False  # 契约/鉴权错，重试无意义
        except Exception as exc:  # 网络失败 → 退避后重试（requests 的异常谱系很宽）
            _log.warning("panel: attempt %d/%d failed: %r", attempt + 1, len(_BACKOFF) + 1, type(exc).__name__)
        if attempt < len(_BACKOFF):
            time.sleep(_BACKOFF[attempt])  # 1s, 3s
    _log.error("panel: round dropped after %d attempts", len(_BACKOFF) + 1)
    return False
