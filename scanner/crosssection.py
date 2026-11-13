"""创业板全集横截面异动观察层（2026-11-13）。

## 为什么要有这个模块

雪球飙升榜（`hot_stock/new_list.json?order_by=rank_change`）是**热度榜**——排序键
是排名跃升，天然滞后：票先动、后上榜。本模块提供一条**与榜单解耦**的并列观察通道：
每一轮取东财 `ulist.np/get` 的创业板全集报价快照（`market_extra.fetch_fund_flow_rank`，
零新增外部接口/字段），在本地环形缓冲里做**横向（同一轮全市场分位）+ 纵向（相邻轮
差分）**比较，把「榜外、但当轮价量异动最靠前」的票以 `≡` 段打印出来。

三条边界（违反即破坏既有口径）：

1. **并列观察层，不是结论源**：不写 `offboard_launch_log` / `offboard_rejections` /
   `recommendations` / `market_extra_cache`，不改 B 段任何冻结列与回测口径。
   唯一产物 = `crosssection_signal` 表（m027）+ 终端 `≡` 块。
2. **无前视**：信号只能由**已经观测到**的快照算出；`round_seq` 就是产出时刻。
3. **不新增网络请求路径**：复用 `market_extra` 的 URL / 字段码 / 批次 / 代理绕过；
   每轮通过 `fetch_fund_flow_rank(force=True)` 跳过 300s 进程 TTL，但结果仍写回
   其缓存 ⇒ 本轮稍后 `collect_market_extra()` 命中同一份快照，不产生第二次请求。

## 口径

- **环形缓冲**：`deque(maxlen=CS_BUFFER_ROUNDS=8)`，每轮一份 `{symbol: 报价}` +
  墙钟时间；**只在内存**，不落全量快照。
- **差分**：`speed_1/3/5` = 现价相对 1/3/5 轮前的变化百分比（%）。**不假定每轮
  60 秒**：窗口实际经过秒数由相邻快照的墙钟差逐轮累加，再按
  `N × CS_NOMINAL_ROUND_SEC / 实际秒数` 折算到 N 分钟的标准窗口（60s/轮时系数
  = 1，即原始累计涨跌幅；90s/轮时 1 轮窗口 ×60/90，避免把 90 秒的涨幅当成
  1 分钟涨速）。`elapsed<=0`（时钟回拨/同秒快照）⇒ `None`；不足对应轮数 ⇒
  `None`（z-score 里当 0）。
- `amount_surge` = 本轮成交额增量 ÷ 过去 3 轮平均增量（`amount` 是当日累计，故用
  相邻两轮差）；不足 5 份快照或分母 ≤0 ⇒ `None`。
- **复合分**：`speed_1/speed_3/amount_surge/vol_ratio` 各自按**当轮全部有效行**
  做 z-score（均值/标准差的总体就是这些有效行；`std≈0` 保护；缺项的值按 0 参与
  均值/标准差）后加权求和；再算每票在当轮有效行中的**横截面分位**（严格低于
  本票的占比，并列取保守侧）。只保留 `>= CS_P95`。
- **有效行**：`price<=0 / volume<=0 / amount<=0` 一律剔除（停牌、未发行、无成交），
  既不参与分位也不当分母。信号另需 `amount >= CS_MIN_AMOUNT`（与 B 段同源，
  p95 之后再加的门，不回填进分位分母）。
- **榜内票不算榜外**：`board_items` 里的 symbol（6 位码或 `SZ300001` 兼容归一化）
  只用于跨源价差校验，最终信号剔除它们。
- **陈旧轮**：本轮 ≥`CS_STALE_RATIO` 的有效行 `(price, volume)` 与上一轮完全相同 ⇒
  判上游快照未更新，**该轮仍入环形缓冲（每轮一份快照）但不产出信号**，累计
  `stale_rounds` 并限频告警。
- **预热**：缓冲不满 `CS_WARMUP_ROUNDS=6` 轮不出信号（`speed_5` 需要当前 + 5 轮前）。

## 失败纪律

模块内部允许抛异常（网络/DB），由接线点（`unified_scanner`）按 `EXTERNAL_FAILURES`
兜住；**不吞编程错误**，也不让异常冒泡打断扫描。告警日志一律带异常类型名。

## 终端块

`format_terminal_block()` 以 `≡` 开头——**刻意不用** `◆` / `—`：`ai_digest/panel.py`
靠这两个符号切区块与段，新段若用它们会污染面板解析。该块在 `capture_stdout()` 内
打印，因此自动进入 `save_panel_round()` / 一键复制 / 面板上报，无需改 ScanView。

## 开关 / CLI

`RTS_CROSSSECTION=0` 整体关闭；`RTS_CROSSSECTION_TTL_SEC` 调快照节流。
离线核对：`python -m scanner.crosssection [--date YYYY-MM-DD] [--json] [--health]`
（只读库、不联网、不改库）。
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sqlite3
import sys
import threading
import time
from collections import deque
from datetime import datetime
from statistics import median
from typing import Any

from scanner import market_extra
from scanner.config import (
    BEIJING_TZ,
    CS_AMOUNT_SURGE_WEIGHT,
    CS_BUFFER_ROUNDS,
    CS_DIVERGENCE_TOL,
    CS_ENABLED,
    CS_KEEP_ROUNDS,
    CS_MIN_AMOUNT,
    CS_NOMINAL_ROUND_SEC,
    CS_P95,
    CS_SPEED_1_WEIGHT,
    CS_SPEED_3_WEIGHT,
    CS_STALE_RATIO,
    CS_STALE_WARN_INTERVAL_SEC,
    CS_TOP_N,
    CS_TTL_SEC,
    CS_VOL_RATIO_WEIGHT,
    CS_WARMUP_ROUNDS,
)
from scanner.data_source import ak_to_xq
from scanner.trading_session import is_trading_time
from scanner.utils import to_float

logger = logging.getLogger(__name__)

# Windows GBK 控制台会把中文输出打成 mojibake；`python -m` 独立入口不经过
# unified_scanner 的 stdout 设置，故按项目惯例（unified_scanner.py / backtest.py /
# 各 scripts 同款）在此自设 UTF-8。getattr 模式：重定向/pty 下没有 reconfigure。
if sys.platform == "win32":
    _reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(_reconfigure):
        _reconfigure(encoding="utf-8")

__all__ = ["observe", "health", "format_terminal_block", "main"]

# 环形缓冲：主循环单线程写（observe），仅测试/CLI 读。deque 的 maxlen 自动淘汰最旧。
_ring: deque[dict[str, Any]] = deque(maxlen=CS_BUFFER_ROUNDS)
_lock = threading.Lock()
_state: dict[str, Any] = {
    "date": "",
    "last_ts": 0.0,
    "stale_rounds": 0,
    "last_stale_warn_ts": 0.0,
    "last_signals": [],
    "health": {
        "enabled": CS_ENABLED,
        "state": "no_data",
        "valid_rows": 0,
        "stale_rounds": 0,
        "stale_round": False,
        "divergence": None,
        "max_divergence": None,
        "divergence_samples": 0,
        "divergence_ok": True,
        "buffer_depth": 0,
        "warmup": False,
        "signals": 0,
        "p95": CS_P95,
        "round_seq": None,
        "time": None,
        "date": "",
        "last_refresh": None,
    },
}


def _clock() -> float:
    """墙钟秒（测试可 patch；生产 = time.time）。

    时间源刻意收口在模块级函数：交易时段判定、快照时刻、round_seq 全走它，
    测试可 monkeypatch `cs._clock` 回放任意节拍的 6+ 轮快照。
    """
    return time.time()


def _now() -> datetime:
    """北京时间当前时刻。

    `now_beijing()` 定义在 `scanner/config_core.py`（经 `scanner.config` 聚合导出），
    `scanner/utils.py` 里**没有**同名函数；本模块要用可 patch 的假时钟，故用
    `BEIJING_TZ` + `_clock()` 自己组装，与其等价。
    """
    return datetime.fromtimestamp(_clock(), tz=BEIJING_TZ)


def fetch_fund_flow_rank(*, force: bool = False) -> dict[str, dict]:
    """取数接缝（模块级引用，测试可直接 monkeypatch 本函数或 market_extra 同名函数）。

    动态委派而非 import 期快照绑定：生产只走 `market_extra` 现有 URL/字段/批次/代理
    绕过，`force=True` 跳过 300s 进程 TTL 且结果仍写回 `_ff_cache` ⇒ 本轮稍后
    `collect_market_extra()` 命中同一份快照，零额外网络请求。
    """
    return market_extra.fetch_fund_flow_rank(force=force)


def _normalize_symbol(raw: Any) -> str:
    """6 位码 / xq 符号 → `SZ300001`（复用 `ak_to_xq`，与 market_extra 同映射）。"""
    s = str(raw or "").strip()
    if not s:
        return ""
    return ak_to_xq(s)


def _valid_snapshot(payload: dict[str, dict]) -> dict[str, dict[str, Any]]:
    """东财全集 payload → 有效行 `{symbol: 报价}`。

    量纲字段（price/volume/amount）挡 `<=0`：0 值 = 停牌 / 未发行 / 无成交，
    既不该参与横截面分位，也不该当标准差的分母。
    """
    out: dict[str, dict[str, Any]] = {}
    for code, row in payload.items():
        if not isinstance(row, dict):
            continue
        price = to_float(row.get("price"), None)
        volume = to_float(row.get("volume"), None)
        amount = to_float(row.get("amount"), None)
        if price is None or volume is None or amount is None:
            continue
        if price <= 0 or volume <= 0 or amount <= 0:
            continue
        pct = to_float(row.get("percent"), None)
        out[_normalize_symbol(code)] = {
            "price": price,
            "percent": pct if pct is not None else 0.0,
            "volume": volume,
            "amount": amount,
            "vol_ratio": to_float(row.get("vol_ratio"), None),
            "name": str(row.get("name") or ""),
        }
    return out


def _speed_pct(cur: float, then: float, elapsed: float, rounds_n: int) -> float | None:
    """`rounds_n` 轮窗口的等效涨跌幅（%）。

    「现价相对 N 轮前变化百分比」按**实际经过秒数**折算到 N 分钟标准窗口：
    `pct × (N × 名义单轮秒数 / elapsed)`。60s/轮时系数 = 1（就是累计涨跌幅）；
    90s/轮时系数 = 60/90，避免把 90 秒的涨幅当成 1 分钟涨速。
    `elapsed<=0`（时钟回拨/同秒快照）或价格非正 ⇒ 不可用。
    """
    if elapsed <= 0 or cur <= 0 or then <= 0 or rounds_n <= 0:
        return None
    nominal = rounds_n * CS_NOMINAL_ROUND_SEC
    return (cur / then - 1.0) * 100.0 * (nominal / elapsed)


def _speed(
    symbols: dict[str, dict], prior: dict[str, dict], elapsed: float, rounds_n: int
) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for sym, row in symbols.items():
        prev = prior.get(sym)
        out[sym] = _speed_pct(row["price"], prev["price"], elapsed, rounds_n) if prev else None
    return out


def _amount_surge(symbols: dict[str, dict], rounds: list[dict[str, dict]]) -> dict[str, float | None]:
    """本轮成交额增量 ÷ 过去 3 轮平均增量（`rounds` = 最近 5 轮，末位为本轮）。

    `amount` 是**当日累计**，故增量必须用相邻两轮差。分母 ≤0（横盘/早盘首分钟）
    或历史不足 → None（z-score 里当 0，不虚构一个「暴增」）。
    """
    out: dict[str, float | None] = {}
    if len(rounds) < 5:
        return dict.fromkeys(symbols)
    for sym in symbols:
        amounts: list[float] = []
        ok = True
        for snap in rounds[-5:]:
            r = snap.get(sym)
            if not r or r["amount"] <= 0:
                ok = False
                break
            amounts.append(r["amount"])
        if not ok:
            out[sym] = None
            continue
        d1 = amounts[4] - amounts[3]
        avg_past = ((amounts[3] - amounts[2]) + (amounts[2] - amounts[1]) + (amounts[1] - amounts[0])) / 3.0
        out[sym] = d1 / avg_past if avg_past > 0 else None
    return out


def _zscore(values: dict[str, float | None]) -> dict[str, float]:
    """按**当轮全部有效行**做 z-score；缺项的值当 0 一并进均值/标准差总体。

    `std≈0`（全市场读数相同）或样本 <2 ⇒ 全 0，避免除零把噪声放大成信号。
    """
    cleaned = {k: (v if v is not None and math.isfinite(v) else 0.0) for k, v in values.items()}
    population = list(cleaned.values())
    if len(population) < 2:
        return dict.fromkeys(values, 0.0)
    mean = sum(population) / len(population)
    std = math.sqrt(sum((v - mean) ** 2 for v in population) / len(population))
    if std <= 1e-12:
        return dict.fromkeys(values, 0.0)
    return {k: (v - mean) / std for k, v in cleaned.items()}


def _percentile_ranks(scores: dict[str, float]) -> dict[str, float]:
    """横截面分位 = 严格低于本票的有效行占比；并列取**保守侧**（同一分位）。

    n=1 时退化为 1.0（只有一只票，它既是第一也是最后；预热后实际不会发生）。
    """
    ordered = sorted(scores.items(), key=lambda kv: kv[1])
    n = len(ordered)
    out: dict[str, float] = {}
    i = 0
    while i < n:
        j = i
        while j + 1 < n and ordered[j + 1][1] == ordered[i][1]:
            j += 1
        pct = (i / (n - 1)) if n > 1 else 1.0
        for k in range(i, j + 1):
            out[ordered[k][0]] = pct
        i = j + 1
    return out


def _is_stale(prev: dict[str, dict], cur: dict[str, dict]) -> bool:
    """≥CS_STALE_RATIO 的有效行 (price, volume) 与上轮完全相同 ⇒ 上游快照未更新。"""
    if not cur or not prev:
        return False
    same = 0
    for sym, row in cur.items():
        p = prev.get(sym)
        if p and row["price"] == p["price"] and row["volume"] == p["volume"]:
            same += 1
    return same / len(cur) >= CS_STALE_RATIO


def _cross_source_divergence(
    board: dict[str, dict], symbols: dict[str, dict]
) -> tuple[float | None, float | None, int]:
    """榜内票的雪球现价/涨幅 vs 东财 price/percent 的绝对偏差。

    返回 `(中位偏差, 最大偏差, 样本数)`；单位统一为 %（现价取相对偏差、涨幅取
    绝对百分点差）。榜空 / 无重叠票 / 无可用字段 ⇒ `(None, None, 0)`，明确表示
    「本项未校验」，不是「偏差为 0」。中位偏差用于 `CS_DIVERGENCE_TOL` 告警，
    最大偏差进 `health()`（设计要求的「最大跨源价差」）。
    """
    devs: list[float] = []
    for sym, item in board.items():
        row = symbols.get(sym)
        if not row:
            continue
        bp = to_float(item.get("current"), None)
        if bp is not None and bp > 0 and row["price"] > 0:
            devs.append(abs(bp - row["price"]) / row["price"] * 100.0)
        bpct = to_float(item.get("percent"), None)
        if bpct is not None:
            devs.append(abs(bpct - row["percent"]))
    if not devs:
        return None, None, 0
    return median(devs), max(devs), len(devs)


def _board_map(board_items: list[dict] | None) -> dict[str, dict]:
    """雪球榜原始项 → `{SZxxxxxx: item}`；兼容 6 位码与带前缀符号。"""
    out: dict[str, dict] = {}
    for it in board_items or ():
        if not isinstance(it, dict):
            continue
        # 雪球榜原始项用 symbol；兼容只给 code / 6 位纯码的调用方。
        sym = _normalize_symbol(it.get("symbol") or it.get("code"))
        if sym:
            out[sym] = it
    return out


def _stale_warn(now_ts: float, count: int) -> None:
    """陈旧告警限频（上游卡住时会每轮陈旧，不能每 60s 刷一条）。"""
    if now_ts - float(_state.get("last_stale_warn_ts") or 0.0) < CS_STALE_WARN_INTERVAL_SEC:
        return
    _state["last_stale_warn_ts"] = now_ts
    logger.warning("横截面快照陈旧：与上一轮几乎完全相同（累计 %d 轮），本轮不产出信号", count)


def _set_health(**kw: Any) -> dict:
    with _lock:
        h = dict(_state["health"])
        h.update(kw)
        _state["health"] = h
        return dict(h)


def observe(conn: sqlite3.Connection, board_items: list[dict] | None = None) -> dict:
    """每轮调用一次：取快照 → 入环形缓冲 → 算横截面异动 → 落库 p95+ 榜外信号。

    返回健康摘要 dict（同时更新 `health()`）。非交易时段 / 关闭 / 节流窗口内 → no-op。
    失败（网络/DB）由调用方按 fail-open 兜住；本函数不吞编程错误。
    """
    if not CS_ENABLED:
        return _set_health(enabled=False, state="disabled")

    now = _now()
    today = now.date().isoformat()
    if not is_trading_time(now):
        return _set_health(enabled=True, state="non_trading", date=today)

    now_ts = _clock()
    with _lock:
        same_day = _state["date"] == today
        last_ts = float(_state["last_ts"] or 0.0)
        depth = len(_ring)
    if same_day and depth and now_ts - last_ts < CS_TTL_SEC:
        return _set_health(enabled=True, state="throttled", date=today)

    # 跨日重置：昨天的快照不能参与今天的差分（隔夜缺口会把跳空误读成 1 分钟涨速）。
    with _lock:
        if _state["date"] != today:
            _ring.clear()
            _state["date"] = today
            _state["stale_rounds"] = 0
            _state["last_signals"] = []

    board = _board_map(board_items)
    if board:
        # 把榜内票并入本轮取数域（不新增请求路径，只是 secids 清单）：①榜内主板票
        # 本就在 market_extra 的既有取数语义内（collect_market_extra 同款注册）；
        # ②若等 collect_market_extra 稍后注册，`fetch_fund_flow_rank(force=True)` 刚写
        # 的缓存会因「注册码 ⊄ 上次拉取域」失效 ⇒ 同轮多打一次全集请求。先注册即消除。
        market_extra.register_fund_flow_codes(board.keys())

    payload = fetch_fund_flow_rank(force=True)
    symbols = _valid_snapshot(payload)

    stamp = now.strftime("%H:%M:%S")
    refreshed = now.strftime("%Y-%m-%d %H:%M:%S")
    divergence, max_divergence, div_samples = _cross_source_divergence(board, symbols)
    divergence_ok = divergence is None or divergence <= CS_DIVERGENCE_TOL
    if not divergence_ok:
        logger.warning(
            "横截面跨源价差偏大：中位偏差 %.2f%% > 容忍 %.2f%%（榜内样本 %d 只）",
            divergence,
            CS_DIVERGENCE_TOL,
            div_samples,
        )

    # 有效行 0 只 = 上游取数失败或全市场停牌：不入缓冲（没有可比较的截面），
    # 也不产出信号；健康摘要留痕，避免把「没取到」读成「没有异动」。
    if not symbols:
        return _set_health(
            enabled=True,
            state="no_data",
            date=today,
            time=stamp,
            valid_rows=0,
            buffer_depth=len(_ring),
            last_refresh=refreshed,
            signals=0,
            divergence=divergence,
            max_divergence=max_divergence,
            divergence_samples=div_samples,
            divergence_ok=divergence_ok,
        )

    with _lock:
        prev_snapshot = _ring[-1]["symbols"] if _ring else {}
        stale = _is_stale(prev_snapshot, symbols)
        if stale:
            _state["stale_rounds"] = int(_state["stale_rounds"]) + 1
        stale_rounds = int(_state["stale_rounds"])
        # 陈旧轮同样入缓冲：设计是「每轮存一份快照」，只是不产出信号。
        _ring.append({"ts": now_ts, "symbols": symbols})
        _state["last_ts"] = now_ts
        rounds = list(_ring)
        depth = len(_ring)
    if stale:
        _stale_warn(now_ts, stale_rounds)

    signals: list[dict[str, Any]] = []
    warm = depth >= CS_WARMUP_ROUNDS
    if warm and not stale:
        cur_round = rounds[-1]["symbols"]
        # elapsed 一律取**相邻快照实际墙钟差**（不假定 60s）；N 轮窗口 = 当前与
        # N 轮前快照的差值，等价于逐轮经过秒数累加。
        speeds1 = _speed(cur_round, rounds[-2]["symbols"], now_ts - rounds[-2]["ts"], 1)
        speeds3 = _speed(cur_round, rounds[-4]["symbols"], now_ts - rounds[-4]["ts"], 3)
        speeds5 = _speed(cur_round, rounds[-6]["symbols"], now_ts - rounds[-6]["ts"], 5)
        surges = _amount_surge(cur_round, [r["symbols"] for r in rounds[-5:]])
        z1 = _zscore(speeds1)
        z3 = _zscore(speeds3)
        za = _zscore(surges)
        zv = _zscore({sym: row["vol_ratio"] for sym, row in cur_round.items()})
        scores = {
            sym: CS_SPEED_1_WEIGHT * z1[sym]
            + CS_SPEED_3_WEIGHT * z3[sym]
            + CS_AMOUNT_SURGE_WEIGHT * za[sym]
            + CS_VOL_RATIO_WEIGHT * zv[sym]
            for sym in cur_round
        }
        pcts = _percentile_ranks(scores)
        for sym, row in cur_round.items():
            if pcts[sym] < CS_P95:
                continue  # 只留当轮横截面 p95+
            if sym in board:
                continue  # 榜内票只作跨源校验样本，不算「榜外异动」
            if row["amount"] < CS_MIN_AMOUNT:
                continue  # 与 B 段同源的成交额门（p95 之后再加，不进分位分母）
            signals.append(
                {
                    "symbol": sym,
                    "name": row["name"] or sym,
                    "price": row["price"],
                    "percent": row["percent"],
                    "speed_1": speeds1.get(sym),
                    "speed_3": speeds3.get(sym),
                    "speed_5": speeds5.get(sym),
                    "amount_surge": surges.get(sym),
                    "vol_ratio": row["vol_ratio"],
                    "pct_rank": pcts[sym],
                    "on_board": False,
                }
            )
        signals.sort(key=lambda s: (s["pct_rank"], s["amount_surge"] or 0.0), reverse=True)

    round_seq = int(now.strftime("%H%M%S"))
    if signals:
        _persist(conn, today, round_seq, signals, now)

    with _lock:
        _state["last_signals"] = signals
    return _set_health(
        enabled=True,
        state="stale" if stale else "ok",
        date=today,
        time=stamp,
        valid_rows=len(symbols),
        stale_rounds=stale_rounds,
        stale_round=stale,
        buffer_depth=depth,
        warmup=warm,
        signals=len(signals),
        round_seq=round_seq,
        p95=CS_P95,
        divergence=divergence,
        max_divergence=max_divergence,
        divergence_samples=div_samples,
        divergence_ok=divergence_ok,
        last_refresh=refreshed,
    )


def _persist(conn: sqlite3.Connection, date: str, round_seq: int, signals: list[dict], now: datetime) -> None:
    """写本轮 p95+ 榜外信号并淘汰旧轮次（用主循环已有 conn，不新开连接/线程）。"""
    updated = now.isoformat()
    conn.executemany(
        "INSERT OR REPLACE INTO crosssection_signal("
        "date, round_seq, symbol, name, price, percent, speed_1, speed_3, speed_5, "
        "amount_surge, vol_ratio, pct_rank, updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                date,
                round_seq,
                s["symbol"],
                s["name"],
                s["price"],
                s["percent"],
                s["speed_1"],
                s["speed_3"],
                s["speed_5"],
                s["amount_surge"],
                s["vol_ratio"],
                s["pct_rank"],
                updated,
            )
            for s in signals
        ],
    )
    # 只留最近 CS_KEEP_ROUNDS 个**轮次**（与 local_view_cache._SNAPSHOT_LIMIT 同款语义，
    # 但按 (date, round_seq) 轮级淘汰——只按行数淘汰会把最新一轮截断成半轮）。
    conn.execute(
        "DELETE FROM crosssection_signal WHERE (date, round_seq) NOT IN ("
        "SELECT date, round_seq FROM crosssection_signal "
        "GROUP BY date, round_seq ORDER BY date DESC, round_seq DESC LIMIT ?)",
        (CS_KEEP_ROUNDS,),
    )
    conn.commit()


def health() -> dict:
    """最近一轮健康摘要（进程内；无观测记录时返回初始态）。"""
    with _lock:
        return dict(_state["health"])


def _fmt_num(v: float | None, digits: int = 2, suffix: str = "") -> str:
    if v is None or not isinstance(v, (int, float)) or not math.isfinite(v):
        return "—"
    return f"{v:.{digits}f}{suffix}"


def format_terminal_block() -> str:
    """终端 `≡` 块（多行纯文本，可 `print()`）。关闭/非交易时段 → 空串。

    标题**必须**以 `≡` 开头：`◆` 是区标题、`—` 是段标题，`ai_digest/panel.py`
    依赖它们切块；用错符号会把本块解析成面板数据行或拆散既有三段。
    """
    h = health()
    if not h.get("enabled", True) or h.get("state") in ("disabled", "non_trading"):
        return ""
    state = h.get("state")
    stamp = h.get("time") or _now().strftime("%H:%M:%S")
    depth = int(h.get("buffer_depth") or 0)
    div = h.get("divergence")
    div_txt = "跨源价差 未校验" if div is None else f"跨源价差 {div:.2f}%"
    if h.get("divergence_ok") is False:
        div_txt += f" ⚠超容忍{CS_DIVERGENCE_TOL:.1f}%"
    if h.get("max_divergence") is not None:
        div_txt += f" 最大{h['max_divergence']:.2f}%"

    # 单行标题 + 多行正文：时刻 / 有效行数 / 预热·陈旧状态 / p95 阈值 / 前 N 条信号。
    # 首字符必须是 `≡`，且块内任何一行都不得以 `◆` / `—` 起头。
    header = (
        f"≡ 横截面异动 · {stamp} | 有效 {h.get('valid_rows', 0)} 只 | "
        f"缓冲 {depth}/{CS_BUFFER_ROUNDS} | p95 {CS_P95:.3f} | "
        f"信号 {h.get('signals', 0)} | {div_txt}"
    )
    lines = [header]
    if state == "throttled":
        lines.append("  本轮快照节流（沿用上一轮结果，未重新取数）")
    elif state == "no_data":
        lines.append("  无有效快照（东财取数失败或全市场停牌）")
    elif state == "stale":
        lines.append(f"  陈旧轮：上游快照与上轮相同，本轮不产出（累计 {h.get('stale_rounds', 0)} 轮）")
    elif not h.get("warmup"):
        left = max(CS_WARMUP_ROUNDS - depth, 0)
        lines.append(f"  预热中 {depth}/{CS_WARMUP_ROUNDS}（60s 一轮，约 {left} 分钟后产出）")

    with _lock:
        top = list(_state["last_signals"])[:CS_TOP_N]
    for i, s in enumerate(top, 1):
        lines.append(
            f"  {i}. {s['symbol']} {s['name']} {s['price']:.2f} {s['percent']:+.2f}% | "
            f"1m {_fmt_num(s.get('speed_1'), 2, '%')} 3m {_fmt_num(s.get('speed_3'), 2, '%')} "
            f"5m {_fmt_num(s.get('speed_5'), 2, '%')} | 额涌 {_fmt_num(s.get('amount_surge'), 2, '×')} "
            f"量比 {_fmt_num(s.get('vol_ratio'), 2)} 分位 {s['pct_rank']:.3f}"
        )
    if h.get("warmup") and state not in ("stale", "no_data") and not top:
        lines.append("  （本轮无 p95+ 榜外异动）")
    return "\n".join(lines)


# ── CLI（离线只读）──


def _load_rows(conn: sqlite3.Connection, date: str) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT date, round_seq, symbol, name, price, percent, speed_1, speed_3, speed_5, "
        "amount_surge, vol_ratio, pct_rank, updated_at "
        "FROM crosssection_signal WHERE date=? ORDER BY round_seq DESC, pct_rank DESC",
        (date,),
    ).fetchall()


def _print_db_health(conn: sqlite3.Connection, date: str) -> None:
    try:
        row = conn.execute(
            "SELECT COUNT(DISTINCT round_seq), COUNT(*), MIN(round_seq), MAX(round_seq) "
            "FROM crosssection_signal WHERE date=?",
            (date,),
        ).fetchone()
    except sqlite3.Error as e:
        print(f"crosssection_signal 不可读（{type(e).__name__}: {e}）—— 先跑一轮扫描器应用 m027")
        print(f"进程内健康摘要：{json.dumps(health(), ensure_ascii=False)}")
        return
    rounds, rows, first, last = (row[0], row[1], row[2], row[3]) if row else (0, 0, None, None)
    print(f"日期 {date} | 轮次 {rounds} | 信号行 {rows} | round_seq {first or '—'} → {last or '—'}")
    print(f"进程内健康摘要：{json.dumps(health(), ensure_ascii=False)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="创业板全集横截面异动信号（离线只读）")
    parser.add_argument("--date", default=None, help="日期 YYYY-MM-DD（默认今天，北京时间）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--health", action="store_true", help="只打印健康摘要（库内轮次 + 进程内状态）")
    args = parser.parse_args(argv)
    date = args.date or _now().date().isoformat()

    # 离线只读：走项目统一连接工厂（WAL/busy_timeout 三件套），但**只发 SELECT**、
    # 不调 init_db/apply migration、不联网、不改库。表尚未由 m027 建好时给出可读提示。
    from scanner.db.schema import get_conn

    conn = get_conn()
    try:
        if args.health:
            _print_db_health(conn, date)
            return 0
        try:
            rows = _load_rows(conn, date)
        except sqlite3.Error as e:
            print(f"crosssection_signal 不可读（{type(e).__name__}: {e}）—— 先跑一轮扫描器应用 m027")
            return 0
        if args.json:
            print(json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=2))
            return 0
        if not rows:
            print(f"{date} 无横截面信号")
            return 0
        latest = rows[0]["round_seq"]
        latest_rows = [r for r in rows if r["round_seq"] == latest]
        print(f"{date} 最新一轮 {latest}（共 {len(latest_rows)} 条 p95+ 榜外信号）")
        for r in latest_rows:
            print(
                f"  {r['symbol']} {r['name'] or ''} {r['price']:.2f} {r['percent']:+.2f}% | "
                f"1m {_fmt_num(r['speed_1'], 2, '%')} 3m {_fmt_num(r['speed_3'], 2, '%')} "
                f"5m {_fmt_num(r['speed_5'], 2, '%')} | 额涌 {_fmt_num(r['amount_surge'], 2, '×')} "
                f"量比 {_fmt_num(r['vol_ratio'], 2)} 分位 {r['pct_rank']:.3f}"
            )
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
