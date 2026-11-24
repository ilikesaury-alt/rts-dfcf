"""创业板全集横截面异动观察层（2026-11-13）。

## 为什么要有这个模块

雪球飙升榜（`hot_stock/new_list.json?order_by=rank_change`）是**热度榜**——排序键
是排名跃升，天然滞后：票先动、后上榜。本模块提供一条**与榜单解耦**的并列观察通道：
每一轮取东财 `ulist.np/get` 的创业板全集报价快照（`market_extra.fetch_fund_flow_rank`，
零新增外部接口/字段），在本地环形缓冲里做**横向（同一轮全市场分位）+ 纵向（相邻轮
差分）**比较，把「榜外、但当轮价量异动最靠前」的票以 `≡` 段打印出来。

本层输出是**当前截面快照**：不承诺、也不度量对飙升榜的领先时长（`appearances` 无上榜时刻，
榜内票在信号侧被剔除，无法核对领先时长）。「持续」只是抗闪烁的展示分组，不是跟踪轨迹。

三条边界（违反即破坏既有口径）：

1. **并列观察层，不是结论源**：不写 `offboard_launch_log` / `offboard_rejections` /
   `recommendations` / `market_extra_cache`，不改 B 段任何冻结列与回测口径。
   唯一产物 = `crosssection_signal` 表（m027 建表 / m028 补存档维度）+ 终端 `≡` 块。
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

**展示分两组（2026-11-17）**：`持续`（当日累计 `>=CS_PERSIST_MIN` 轮，最多
`CS_TOP_PERSIST=8` 条）在前，`新进`（当轮首现，最多 `CS_TOP_NEW=5` 条，压成一行摘要）在后，中间
2~4 轮的票不展示。动因：用户反馈「变化太快、眼花缭乱不知道咋选」；实测当日展示行
相邻两轮重叠中位数只有 1/8，而反复出现 ≥5 轮的票有 180/756 只 —— 稳定子集本就在，
只是旧排序把单轮噪声（「全正·配合」在 1 轮窗口极易达成）排在了轮次前面。
分组只改展示，不增删票、不给方向、不改落库口径。

## 画像 / 分节（2026-11-19 方案二，用户决策）

终端 `≡` 段的数字列（1m/3m/5m/额涌/量比/分位）全部撤出终端（仍落库供回看），
改打**中文画像串**：`位置档·持续N轮·动向·主力资金`（单源 `profile_text()`）。

- **位置档**：按当日涨幅分 蓄势(<3%)/启动(3~7%)/加速(7~12%)/高位(≥12%)，
  阈值单源 `config_crosssection.CS_POS_*`。回答用户「低位启动 vs 已冲高」。
- **动向**：一致性×量价压成一个短语（量价齐升/稳步走强/反复放量/放量滞涨/
  一致回落/动向不明）。
- **主力资金**：**单源 = 东财 f62**（`market_extra_cache.fund_flow.main_net`，主循环
  每轮已在写，零新增请求），在 `observe()` 里合并进信号。⚠ 雪球 screener 的
  `main_net_inflows` **不是**这个口径（2026-11-20 实测：同日 57 只信号票 42% 符号
  相反、量级系统性偏大，且与雪球自身 `capital/history.json` 当日净额也矛盾），
  已从取数映射中剔除，勿再加回。展示拼成 `主力+4213万(占额6.8%)`/`主力-2.1亿(占额-3.2%)`
  ——净额给体量、占成交额比例给可比强度（`main_net / amount`，两者都是当日累计）。
  f62 缺失时显「资金—」、不带占额，展示资金闸 fail-open。

持续组内再按**分节**归堆（描述性归类，不是档位推荐）：
`低位·资金强 → 持续走强 → 高位加速 → 量价背离`（单源 `_section()`）；
新进组压成一行摘要（无逐行）。三条边界不变：零新增请求、不给结论、不改落库口径。

**展示涨幅闸（2026-11-23，同日 10→12%）**：`percent > CS_DISPLAY_MAX_PERCENT`（默认 12%）
的票不占展示位；与资金闸同款 —— 只过滤展示，不改信号/落库/排序，缺值 fail-open。
阈值与 `CS_POS_HIGH`（≥12% 判「高位」）对齐，否则「高位加速」节永远为空。

**展示资金闸（2026-11-20，11-24 下调至 300 万）**：`main_net <= CS_DISPLAY_MIN_MAIN_NET`
（默认 300 万，严格大于才放行）的票不进终端/面板两组，避免资金不足的票占掉展示位；
当日下跌但主力净流入 >300 万的票保留（只看资金、不看涨跌）。缺值 fail-open 照常展示。`observe` 仍全部落库，排序键也不变 —— 只过滤展示，不制造第二结论源。

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
from typing import Any, Mapping

from scanner import market_extra
from scanner.config import (
    BEIJING_TZ,
    CS_AMOUNT_SURGE_WEIGHT,
    CS_BUFFER_ROUNDS,
    CS_DISPLAY_MAX_PERCENT,
    CS_DISPLAY_MIN_MAIN_NET,
    CS_DIVERGENCE_TOL,
    CS_ENABLED,
    CS_KEEP_DAYS,
    CS_LIMIT_UP_NEAR,
    CS_LIMIT_UP_PCT,
    CS_MIN_AMOUNT,
    CS_NOMINAL_ROUND_SEC,
    CS_P95,
    CS_PERSIST_MIN,
    CS_POS_ACCEL,
    CS_POS_HIGH,
    CS_POS_LAUNCH,
    CS_SOURCE,
    CS_SPEED_1_WEIGHT,
    CS_SPEED_3_WEIGHT,
    CS_STALE_RATIO,
    CS_STALE_WARN_INTERVAL_SEC,
    CS_TOP_N,
    CS_TOP_NEW,
    CS_TOP_PERSIST,
    CS_TTL_SEC,
    CS_VOL_RATIO_WEIGHT,
    CS_WARMUP_ROUNDS,
    CS_XQ_PAGE_SIZE,
    CS_XQ_PAGES,
    CS_XQ_RANK_WINDOW,
)
from scanner.data_source import ak_to_xq
from scanner.db.queries import get_market_extra_snapshot
from scanner.trading_session import is_trading_time
from scanner.utils import EXTERNAL_FAILURES, to_float

logger = logging.getLogger(__name__)

# Windows GBK 控制台会把中文输出打成 mojibake；`python -m` 独立入口不经过
# unified_scanner 的 stdout 设置，故按项目惯例（unified_scanner.py / backtest.py /
# 各 scripts 同款）在此自设 UTF-8。getattr 模式：重定向/pty 下没有 reconfigure。
if sys.platform == "win32":
    _reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(_reconfigure):
        _reconfigure(encoding="utf-8")

__all__ = ["observe", "health", "format_terminal_block", "signal_sort_key", "backfill_next_day", "main"]

# 环形缓冲：主循环单线程写（observe），仅测试/CLI 读。deque 的 maxlen 自动淘汰最旧。
_ring: deque[dict[str, Any]] = deque(maxlen=CS_BUFFER_ROUNDS)
_lock = threading.Lock()
_state: dict[str, Any] = {
    "date": "",
    "last_ts": 0.0,
    "stale_rounds": 0,
    "last_stale_warn_ts": 0.0,
    "last_signals": [],
    # 当日各票「出现过信号」的轮次数（层内持续性，2026-11-16）。跨日清零。
    "persist": {},
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
        "native_speed": False,
        "sealed_skipped": 0,
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


# ── 数据源：雪球 screener（2026-11-16 新增，默认源）──
#
# 为什么换源：东财 `ulist.np/get` 是**无参全量**接口 —— 本层要 p95 就必须拉 3000 个
# secid（6 请求/轮）自己算分位，且每轮 force=True 强制新取数 ⇒ 计入 push2 家族
# **IP 级限频**（AGENTS.md 已记这个坑）。2026-11-16 10:58 起整族 TLS 断连，本层哑掉。
#
# 雪球 screener 提供**服务端排序分页**（`order_by=percent5m`），只取前 N 页即可，
# 且 `percent5m` 是原生字段（东财 ulist 根本没有这个字段，f11/f22 恒为 0）。
#
# ⚠ 口径差异（必须记住）：东财是真全市场分位（分母 ~1500），雪球是「前 N 页内位次」
# （分母 CS_XQ_RANK_WINDOW=200）。尾部（percent5m<=0）拿不到真实分位 —— 但本层
# 只留 >=CS_P95 的票，那部分必落在前 N 页内，故不影响入选集合，只影响展示的分位值。

_xq_session: Any = None
_xq_lock = threading.Lock()


def _xq_get_session() -> Any:
    """复用 `scanner.api` 的雪球会话（含 WAF 握手兜底 UA 轮试），惰性创建。"""
    global _xq_session
    with _xq_lock:
        if _xq_session is None:
            from scanner import api as _api  # 惰性：避免 import 期依赖重量级模块

            _xq_session = _api.make_session()
    return _xq_session


def _fetch_from_xueqiu() -> dict[str, dict]:
    """从雪球 screener 取创业板 `percent5m` 降序前 N 页。

    返回结构与东财路径**完全同形**（`{6位码或xq符号: 报价dict}`），供下游
    `_valid_snapshot` / `_speed_pct` 等原样消费 —— 这是换源不改口径的关键。

    雪球行字段 → 本层字段映射：
      `percent5m`  → `percent5m`（**原生涨速**，无需环形缓冲差分）
      `percent`    → `percent`（当日涨幅）
      `current`    → `price`    `amount` → `amount`
      `volume`     → `volume`   `volume_ratio` → `vol_ratio`
      `turnover_rate` → `turnover`
      `main_net_inflows` → **不映射**（2026-11-20：实测非东财 f62 口径，见模块 docstring；
        主力资金统一在 `observe()` 里从 `market_extra_cache` 的 f62 补）
      `float_market_capital` → `float_cap`
    另带 `name`，与东财 f14 同用途。
    """
    session = _xq_get_session()
    base = "https://stock.xueqiu.com/v5/stock/screener/quote/list.json"
    out: dict[str, dict] = {}
    for page in range(1, max(1, CS_XQ_PAGES) + 1):
        try:
            resp = session.get(
                base,
                params={
                    "page": page,
                    "size": CS_XQ_PAGE_SIZE,
                    "order": "desc",
                    "order_by": "percent5m",
                    "market": "CN",
                    "type": "cyb",  # 创业板（实测 count=1412，正是本层取数域）
                },
                timeout=15,
            )
            if resp.status_code != 200:
                logger.warning("横截面雪球取数 HTTP %s（第 %d 页）", resp.status_code, page)
                break
            data = (resp.json() or {}).get("data") or {}
            rows = data.get("list") or []
        except Exception as e:  # noqa: BLE001 - 诚实吞掉并降级，由调用点判空
            logger.warning("横截面雪球取数失败（第 %d 页）: %s: %s", page, type(e).__name__, e)
            break
        if not rows:
            break
        for row in rows:
            sym = _normalize_symbol(row.get("symbol"))
            if not sym:
                continue
            price = to_float(row.get("current"), None)
            volume = to_float(row.get("volume"), None)
            amount = to_float(row.get("amount"), None)
            if price is None or volume is None or amount is None:
                continue
            pct5 = to_float(row.get("percent5m"), None)
            out[sym] = {
                "price": price,
                "percent": to_float(row.get("percent"), 0.0) or 0.0,
                "volume": volume,
                "amount": amount,
                "vol_ratio": to_float(row.get("volume_ratio"), None),
                "name": str(row.get("name") or ""),
                # 雪球原生 5 分钟涨速（%）。东财路径没有该字段，靠环形缓冲差分算。
                "percent5m": pct5,
                "turnover": to_float(row.get("turnover_rate"), None),
                # 不映射 main_net_inflows：与 f62 口径不一致（见模块 docstring）。
                "main_net": None,
                "float_cap": to_float(row.get("float_market_capital"), None),
            }
        # 已取够 CS_XQ_RANK_WINDOW 就停（省请求）
        if len(out) >= CS_XQ_RANK_WINDOW:
            break
    return out


def fetch_crosssection_snapshot(*, force: bool = False) -> dict[str, dict]:
    """**本层取数唯一入口**：按 `CS_SOURCE` 选择源，返回同形快照。

    - `xueqiu`（默认）：雪球 screener 服务端排序，2 页/轮，原生 percent5m。
    - `eastmoney`：东财 ulist 全量 + 本地环形缓冲差分（旧路径，保留可回退）。
    """
    if CS_SOURCE == "eastmoney":
        return fetch_fund_flow_rank(force=force)
    return _fetch_from_xueqiu()


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
            # 雪球原生 5 分钟涨速（东财路径恒为 None）。保留原始值供 _native_speed。
            "percent5m": to_float(row.get("percent5m"), None),
            # 当日主力净额（元）：东财路径 rows 本就带 f62；雪球路径不带，
            # 由 observe() 从 market_extra_cache 合并。只进画像/展示，不进评分。
            "main_net": to_float(row.get("main_net"), None),
        }
    return out


def _merge_main_net_f62(conn: sqlite3.Connection, symbols: dict[str, dict[str, Any]], date: str) -> int:
    """把 `market_extra_cache` 的东财 f62 合并进快照 `main_net`，返回填入条数。

    为什么不以快照自带字段为准（2026-11-20 实测）：雪球 screener 的
    `main_net_inflows` 与 f62 在同日 57 只信号票里 42% 符号相反、量级系统性偏大，
    也不是雪球自身 `capital/history.json` 的当日净额 ⇒ 不是「主力净额」口径，
    已从 `_fetch_from_xueqiu` 的映射中剔除。东财回退路径的 `main_net` 本就是 f62。

    合并规则：**快照已有非 None 值就保留**（东财路径已有本轮新鲜 f62），只把 None
    填上 DB 当日 f62（雪球路径）；DB 无值则留 None ⇒ 画像「资金—」、展示资金闸
    fail-open。只读库、零新增请求，与 B 段共用 `get_market_extra_snapshot`。
    """
    flow = get_market_extra_snapshot(conn, "fund_flow", as_of=date)
    filled = 0
    for sym, row in symbols.items():
        if row.get("main_net") is not None:
            continue
        payload = flow.get(sym)
        if not payload:
            continue
        net = to_float(payload.get("main_net"), None)
        if net is not None:
            row["main_net"] = net
            filled += 1
    return filled


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


def _speed(symbols: dict[str, dict], prior: dict[str, dict], elapsed: float, rounds_n: int) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for sym, row in symbols.items():
        prev = prior.get(sym)
        out[sym] = _speed_pct(row["price"], prev["price"], elapsed, rounds_n) if prev else None
    return out


def _limit_up_price(row: dict) -> float | None:
    """由 `price` 与 `percent` **反推**该票涨停价（**纯层内**，不借外部数据）。

    推导：`percent = (price / prev_close - 1) * 100`
    ⇒ `prev_close = price / (1 + percent/100)`
    ⇒ `涨停价 = prev_close * (1 + CS_LIMIT_UP_PCT/100)`

    精度实测（2026-11-16，已封板票）：领湃科技反推涨停价 27.83 = 其现价 27.83，
    北鼎股份 8.60 = 8.60 ⇒ 误差 < 0.01 元。故无需额外取 `prev_close` 字段
    （雪球 screener 也不直接给昨收）。

    `percent <= -100`（异常值）或价格非正 ⇒ None（不参与判定，fail-open 放行）。
    """
    price = row.get("price")
    pct = row.get("percent")
    if not isinstance(price, (int, float)) or not isinstance(pct, (int, float)):
        return None
    if not math.isfinite(price) or not math.isfinite(pct) or price <= 0:
        return None
    denom = 1.0 + pct / 100.0
    if denom <= 1e-6:
        return None
    return price / denom * (1.0 + CS_LIMIT_UP_PCT / 100.0)


def _is_limit_up_sealed(row: dict) -> bool:
    """是否**已封涨停**（现价 ≥ 涨停价 × CS_LIMIT_UP_NEAR）。

    为什么剔除：封板后价格不再变动（`percent5m=0`），但量比极高仍能把复合分顶到
    p95 以上 ⇒ 持续占据观察位。**买不进的东西不该占观察位**。
    """
    limit = _limit_up_price(row)
    if limit is None:
        return False
    return row["price"] >= limit * CS_LIMIT_UP_NEAR


def _native_speed(symbols: dict[str, dict]) -> dict[str, float | None]:
    """**雪球原生 5 分钟涨速**（`percent5m`），无则 None。

    东财路径没有这个字段（f11/f22 实测恒为 0），只能靠环形缓冲差分估算，
    并且必须等满 `CS_WARMUP_ROUNDS=6` 轮才出第一个 `speed_5`。
    雪球直接给 ⇒ **本层不再需要预热**（这是换源最大的实收益，不只是省请求）。

    ⚠ 语义对齐：`percent5m` 是「5 分钟涨速」，对应本地 5 轮窗口（60s/轮）。
    本地把它填进 `speed_5`；`speed_1`/`speed_3` 仍由差分算（雪球只给 5 分钟档）。
    ```
    """
    out: dict[str, float | None] = {}
    for sym, row in symbols.items():
        v = row.get("percent5m")
        out[sym] = v if isinstance(v, (int, float)) and math.isfinite(v) else None
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
    # `persist` 从 DB 恢复（2026-11-19）：盘中午重启时当日轮次已在 `crosssection_signal`，
    # 不恢复 ⇒ 「持续」组要重新攒满 CS_PERSIST_MIN 轮才出现（用户实测：每次重启持续组就空）。
    with _lock:
        if _state["date"] != today:
            _ring.clear()
            _state["date"] = today
            _state["stale_rounds"] = 0
            _state["last_signals"] = []
            _state["persist"] = _hydrate_persist(conn, today)

    board = _board_map(board_items)
    if board:
        # 把榜内票并入本轮取数域（不新增请求路径，只是 secids 清单）：①榜内主板票
        # 本就在 market_extra 的既有取数语义内（collect_market_extra 同款注册）；
        # ②若等 collect_market_extra 稍后注册，`fetch_fund_flow_rank(force=True)` 刚写
        # 的缓存会因「注册码 ⊄ 上次拉取域」失效 ⇒ 同轮多打一次全集请求。先注册即消除。
        market_extra.register_fund_flow_codes(board.keys())

    payload = fetch_crosssection_snapshot(force=True)
    symbols = _valid_snapshot(payload)
    # 主力资金单源（2026-11-20）：雪球 screener 的 main_net_inflows 非 f62 口径，
    # 从主循环每轮已在写的 market_extra_cache 补当日 f62（零新增请求，缺失 fail-open）。
    _merge_main_net_f62(conn, symbols, today)

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
    sealed_skipped = 0
    cur_native = _native_speed(rounds[-1]["symbols"]) if rounds else {}
    has_native = any(v is not None for v in cur_native.values())
    # 雪球源给原生 percent5m ⇒ **不需要预热**（东财路径仍需 CS_WARMUP_ROUNDS 轮攒差分）。
    warm = has_native or depth >= CS_WARMUP_ROUNDS
    if warm and not stale:
        cur_round = rounds[-1]["symbols"]

        # elapsed 一律取**相邻快照实际墙钟差**（不假定 60s）；N 轮窗口 = 当前与
        # N 轮前快照的差值，等价于逐轮经过秒数累加。
        # 深度不足时（雪球首轮）短窗口取 None，_zscore 里当 0 参与。
        def _diff_win(back: int, n: int) -> dict[str, float | None]:
            if depth < back + 1:
                return dict.fromkeys(cur_round)
            prior = rounds[-(back + 1)]["symbols"]
            return _speed(cur_round, prior, now_ts - rounds[-(back + 1)]["ts"], n)

        speeds1 = _diff_win(1, 1)
        speeds3 = _diff_win(3, 3)
        speeds5 = _diff_win(5, 5)
        # 原生值优先（雪球 percent5m = 5 分钟涨速 ⇒ 填 speed_5 同档）
        for sym, v in cur_native.items():
            if v is not None:
                speeds5[sym] = v
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
        sealed_skipped = 0
        # 先累计本轮各票的持续性（在过滤前计数：只要本轮进了 p95+ 就算一次出现）。
        with _lock:
            persist_counts = _state["persist"]
        for sym in cur_round:
            if pcts[sym] >= CS_P95:
                persist_counts[sym] = persist_counts.get(sym, 0) + 1
        for sym, row in cur_round.items():
            if pcts[sym] < CS_P95:
                continue  # 只留当轮横截面 p95+
            if sym in board:
                continue  # 榜内票只作跨源校验样本，不算「榜外异动」
            if row["amount"] < CS_MIN_AMOUNT:
                continue  # 与 B 段同源的成交额门（p95 之后再加，不进分位分母）
            if _is_limit_up_sealed(row):
                # 已封涨停：买不进的票不该占观察位（封板后 percent5m=0 但量比极高，
                # 复合分仍能顶到 p95 以上 ⇒ 纯噪音）。剔除以计数留痕，不静默。
                sealed_skipped += 1
                continue
            signals.append(
                {
                    "symbol": sym,
                    "name": row["name"] or sym,
                    "price": row["price"],
                    # 画像「占额」的分母；只进展示，不落库/不进评分。
                    "amount": row["amount"],
                    "percent": row["percent"],
                    "speed_1": speeds1.get(sym),
                    "speed_3": speeds3.get(sym),
                    "speed_5": speeds5.get(sym),
                    "amount_surge": surges.get(sym),
                    "vol_ratio": row["vol_ratio"],
                    "pct_rank": pcts[sym],
                    "on_board": False,
                    "main_net": row.get("main_net"),
                    # 层内一致性判据（2026-11-16）：全部由本层自算字段得出，
                    # 不引入任何外部区域数据。供终端 `≡` 段直接展示。
                    "consistency": _consistency(speeds1.get(sym), speeds3.get(sym), speeds5.get(sym)),
                    "volume_price": _volume_price(surges.get(sym), speeds1.get(sym)),
                    "persist": persist_counts.get(sym, 1),
                }
            )
        signals.sort(key=signal_sort_key)

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
        native_speed=has_native,
        sealed_skipped=sealed_skipped,
    )


def _persist(conn: sqlite3.Connection, date: str, round_seq: int, signals: list[dict], now: datetime) -> None:
    """写本轮 p95+ 榜外信号并按天淘汰旧轮次（用主循环已有 conn，不新开连接/线程）。

    保留策略（2026-11-19 从「50 轮」改）：按**天**保留最近 `CS_KEEP_DAYS` 天的
    **整日全部轮次** —— persist 可离线从行数派生，次日标签可回填（m028）。
    `main_net` 同批落库（m028）：画像「主力资金」段的离线回放依据
    （占额的分母 `amount` 未落库 ⇒ 占比只做实时展示，不回放）。
    """
    updated = now.isoformat()
    conn.executemany(
        "INSERT OR REPLACE INTO crosssection_signal("
        "date, round_seq, symbol, name, price, percent, speed_1, speed_3, speed_5, "
        "amount_surge, vol_ratio, pct_rank, main_net, updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                s.get("main_net"),
                updated,
            )
            for s in signals
        ],
    )
    # 只留最近 CS_KEEP_DAYS 个**交易日**：按 date 整日淘汰（轮次行全留），
    # 跨日历史不再被截断。date 是 ISO 字符串，字典序 = 时间序。
    conn.execute(
        "DELETE FROM crosssection_signal WHERE date NOT IN ("
        "SELECT DISTINCT date FROM crosssection_signal ORDER BY date DESC LIMIT ?)",
        (CS_KEEP_DAYS,),
    )
    conn.commit()


def backfill_next_day(conn: sqlite3.Connection, adapter: Any, signal_date: str | None = None) -> int:
    """回填 `crosssection_signal.next_day_pct`（信号日收盘 → 次一交易日收盘，%），返回回填行数。

    动机（2026-11-19 用户决策「存档+次日标签」）：旧策略只留 50 轮且无标签，
    这层永远无法验证。标签是**行情事实**，不是结论 —— 分析只进离线脚本，
    终端/飞书不读它（不给结论纪律不变）。

    口径与 B 段同款（`offboard_watch.backfill_next_day`）：收盘 → 收盘，
    K 线数据复用**榜外 K 线池** `offboard_kline_cache`（横截面票本就是榜外创业板票，
    不另建第三张 K 线表；`daily_kline` 是榜单衍生池，语义不容污染）。

    三个必须的防呆（与 B 段/onboard 同源，防呆①②复用 `_next_bar_is_next_session`）：
      ① 信号日必须早于今天（WHERE `date < ?`）；
      ② `nxt` 必须是 `d` 之后**紧邻的下一个交易日**（不得夹已登记交易日，
         否则停牌/跳空会把「几天后」误当「次日」）；
      ③ 🔴 `nxt < today` —— K 线池末根可能是当日盘中 bar，放行 `nxt == today`
         会把「次日收盘」冻结成「次日 09:45 盘中价」且永不修正（B 段实测偏差
         46% 的行 >2pp）。代价：标签**延后一个交易日**（T+2 交易日起补），同 B 段。

    同一 (date, symbol) 当天可能有多轮行，标签只算一次、UPDATE 该组全部行。
    交易时段每轮调用（与 B 段同节奏）；NULL 集通常只剩近 1~2 个交易日，
    K 线池命中后零网络请求。
    """
    if conn is None:
        return 0
    today = _now().date().isoformat()
    sql = "SELECT DISTINCT date, symbol FROM crosssection_signal WHERE next_day_pct IS NULL AND date < ?"
    params: tuple = (today,)
    if signal_date:
        sql += " AND date = ?"
        params = (signal_date,)
    try:
        rows = conn.execute(sql, params).fetchall()
    except EXTERNAL_FAILURES as e:
        logger.warning("横截面次日标签回填查询失败: %s: %s", type(e).__name__, e)
        return 0
    if not rows:
        return 0

    # 惰性 import：`offboard_watch` 是重量级模块，且避免任何潜在环（它不依赖本层）。
    # `_next_bar_is_next_session` 复用 B 段单源（onboard_anomaly 同款），不复制第二份。
    from scanner.offboard_watch import _next_bar_is_next_session, load_offboard_klines

    by_symbol: dict[str, list[str]] = {}
    for d, sym in rows:
        by_symbol.setdefault(str(sym), []).append(str(d))

    filled = 0
    now = _now().isoformat(timespec="seconds")
    bars_map = load_offboard_klines(conn, adapter, list(by_symbol))
    for sym, dates in by_symbol.items():
        bars = bars_map.get(sym) or []
        by_date = {k["date"]: k for k in bars if isinstance(k.get("date"), str)}
        for d in dates:
            later = sorted(x for x in by_date if x > d)
            if not later:
                continue
            nxt = later[0]
            # 防呆③：nxt 就是今天 ⇒ 那根是当日盘中 bar，绝不当「次日收盘」（冻结不可逆）。
            if nxt >= today:
                continue
            # 防呆②：nxt 必须紧邻 d（中间不得夹已登记交易日），跨国庆/春节放行。
            if not _next_bar_is_next_session(d, nxt):
                continue
            # 信号日/次日 bar 任一缺失（停牌/脏数据）⇒ 跳过（验不了就不产出，不编标签）。
            b0 = by_date.get(d)
            b1 = by_date.get(nxt)
            p0 = (to_float(b0.get("close"), 0.0) or 0.0) if b0 else 0.0
            p1 = (to_float(b1.get("close"), 0.0) or 0.0) if b1 else 0.0
            if p0 <= 0 or p1 <= 0:
                continue
            try:
                conn.execute(
                    "UPDATE crosssection_signal SET next_day_pct=?, updated_at=? WHERE date=? AND symbol=?",
                    (round((p1 - p0) / p0 * 100.0, 4), now, d, sym),
                )
            except EXTERNAL_FAILURES as e:
                logger.warning("横截面次日标签写入失败 %s %s: %s", d, sym, e)
                continue
            filled += 1
    if filled:
        try:
            conn.commit()
        except EXTERNAL_FAILURES as e:
            logger.warning("横截面次日标签提交失败: %s", e)
    return filled


def _hydrate_persist(conn: sqlite3.Connection, date: str) -> dict[str, int]:
    """从当日的 `crosssection_signal` 行**恢复 persist 计数**（重启/跨日首轮）。

    语义必须与写侧完全同源：写侧「本轮进了 p95+ 就算一次出现」⇒ 行数 =
    该票当日出现过的**不同轮次**数（`COUNT(DISTINCT round_seq)`，`INSERT OR REPLACE`
    保证同轮重写不重计）。不恢复的后果（用户实测 2026-11-19）：每次重启持续组就空，
    要重新攒满 `CS_PERSIST_MIN` 轮才恢复。

    fail-open：表未建/查询失败 ⇒ `{}`（退化成旧行为，绝不阻断 observe）；
    每次进程/每日只跑一次（只在 `_state["date"] != today` 分支），开销可忽略。
    """
    try:
        rows = conn.execute(
            "SELECT symbol, COUNT(DISTINCT round_seq) FROM crosssection_signal WHERE date=? GROUP BY symbol",
            (date,),
        ).fetchall()
    except EXTERNAL_FAILURES as e:
        logger.warning("横截面 persist 恢复失败（退化为从零计数）: %s: %s", type(e).__name__, e)
        return {}
    return {str(sym): int(n) for sym, n in rows}


def health() -> dict:
    """最近一轮健康摘要（进程内；无观测记录时返回初始态）。"""
    with _lock:
        return dict(_state["health"])


def _consistency(speed_1: float | None, speed_3: float | None, speed_5: float | None) -> str:
    """三窗口方向一致性（**纯层内**：全部来自本层环形缓冲的 1/3/5 轮差分）。

    为什么要它：单窗口为正可能只是一次噪声尖刺；三个窗口同号才说明价格在
    **持续朝一个方向走**。这是本层唯一能自证的「信号真伪」判据 —— 不需要
    任何外部区域（榜单/资金流/B 段）的数据参与。

    返回 `全正` / `全负` / `混合`。
    """
    vals = [v for v in (speed_1, speed_3, speed_5) if isinstance(v, (int, float)) and math.isfinite(v)]
    if len(vals) < 3:
        return "混合"
    if all(v > 0 for v in vals):
        return "全正"
    if all(v < 0 for v in vals):
        return "全负"
    return "混合"


def _volume_price(amount_surge: float | None, speed_1: float | None) -> str:
    """量价配合（**纯层内**：`amount_surge` 与 `speed_1` 都是本层自算字段）。

    `配合` = 放量且价在动；`背离` = 明显放量但价格没跟（滞涨/派发嫌疑）；
    其余记 `—`（不足判断）。
    """
    if not isinstance(amount_surge, (int, float)) or not isinstance(speed_1, (int, float)):
        return "—"
    if not math.isfinite(amount_surge) or not math.isfinite(speed_1):
        return "—"
    if amount_surge > 2.0 and speed_1 <= 0:
        return "背离"
    if amount_surge > 1.0 and speed_1 > 0:
        return "配合"
    return "—"


#: 画像分节的展示顺序（描述性归类，非档位推荐；单源 `_section`，只进展示）。
#: 背离放最后：它是负向提示，不是候选。
SECTION_ORDER: tuple[str, ...] = ("低位·资金强", "持续走强", "高位加速", "量价背离")


def _pos_tier(percent: float | None) -> str:
    """当日涨幅 → 位置档（蓄势/启动/加速/高位；缺值 `—`）。阈值单源 `CS_POS_*`。

    回答「低位启动 vs 已冲高」：同是 p95+ 异动，+4% 刚起步和 +14% 已冲高的
    追入风险完全不同，但旧展示里这一信息淹没在数字列里。
    """
    if not isinstance(percent, (int, float)) or not math.isfinite(percent):
        return "—"
    if percent < CS_POS_LAUNCH:
        return "蓄势"
    if percent < CS_POS_ACCEL:
        return "启动"
    if percent < CS_POS_HIGH:
        return "加速"
    return "高位"


def _motion(consistency: str | None, volume_price: str | None) -> str:
    """一致性 × 量价 → 动向短语（画像串第三段；只压缩已知判据，不发明新含义）。

    优先级：背离 > 全正 > 全负 > 其余 —— 背离是风险提示必须先说；
    `量价齐升` = 三窗同向且放量被价格确认（旧「全正·配合」）；
    `放量滞涨` = 明显放量但价没跟（旧「背离」）；`一致回落` = 三窗同负
    （本层不预判方向，负向异动同样入围，如实描述）。
    """
    cons = consistency or "混合"
    vp = volume_price or "—"
    if vp == "背离":
        return "放量滞涨"
    if cons == "全正" and vp == "配合":
        return "量价齐升"
    if cons == "全正":
        return "稳步走强"
    if cons == "全负":
        return "一致回落"
    if vp == "配合":
        return "反复放量"
    return "动向不明"


def _fmt_main_net(main_net: float | None) -> str:
    """主力净流入（元）→ 人类可读短语（万/亿；与 ai_digest 工具同纪律：给人读单位）。"""
    if not isinstance(main_net, (int, float)) or not math.isfinite(main_net):
        return "资金—"
    if main_net == 0:
        return "主力持平"
    sign = "+" if main_net > 0 else "-"
    mag = abs(main_net)
    if mag >= 1e8:
        return f"主力{sign}{mag / 1e8:.1f}亿"
    return f"主力{sign}{mag / 1e4:.0f}万"


def _fmt_flow_threshold(value: float) -> str:
    """展示资金闸阈值 → 人读单位（元/万/亿；与 `_fmt_main_net` 同纪律，别打 5000000 元）。"""
    if value >= 1e8:
        return f"{value / 1e8:g}亿"
    if value >= 1e4:
        return f"{value / 1e4:g}万"
    return f"{value:g}元"


def _flow_gate_note(hidden_flow: int) -> str:
    """展示资金闸的**人读说明**（终端 `≡` 块与网页版口径共用，前端不重拼）。

    文案由本函数单源生成：终端直接打印，面板经 `snapshot_payload.display_flow_note`
    原样渲染。任一端另写一份措辞即会漂移（阈值单位、fail-open 语义、剔除数）。
    """
    return (
        f"展示资金闸：主力净流入 > {_fmt_flow_threshold(CS_DISPLAY_MIN_MAIN_NET)} 才占位"
        f"（当日跌但流入保留；本轮剔 {hidden_flow} 只，仍落库，资金缺失照常展示）"
    )


def _pct_gate_note(hidden_pct: int) -> str:
    """展示涨幅闸的人读说明（终端/网页共用单源，与 `_flow_gate_note` 同纪律）。"""
    return (
        f"展示涨幅闸：当日涨幅 ≤ {CS_DISPLAY_MAX_PERCENT:g}% 才占位"
        f"（本轮剔 {hidden_pct} 只，仍落库，涨幅缺失照常展示）"
    )


def _fmt_main_pct(main_net: Any, amount: Any) -> str:
    """净额 ÷ 当日成交额（%）→ `(占额X.X%)`；分母缺失/非正时返回空串（不硬凑 0）。

    只做展示：净额给体量、占比给跨票可比强度。两者都是当日累计，口径同源、零新增请求。
    """
    if not isinstance(main_net, (int, float)) or not isinstance(amount, (int, float)):
        return ""
    if not math.isfinite(main_net) or not math.isfinite(amount) or amount <= 0:
        return ""
    return f"(占额{main_net / amount * 100:.1f}%)"


def profile_text(s: Mapping[str, Any]) -> str:
    """单条信号 → **中文画像串**（终端 `≡` 行、面板 `画像` 列、AI 素材三出口同源）。

    格式：`位置·持续N轮·动向·主力资金(占额X%)`，如
    `启动·持续8轮·量价齐升·主力+4213万(占额6.8%)`。
    全部由本层自算/快照自带字段派生（零新增请求、不读其它区域），改口径只改这里
    与 `_pos_tier`/`_motion`/`_fmt_main_net`/`_fmt_main_pct` —— 不在终端/前端/提示词里重拼。
    """
    persist = int(s.get("persist") or 1)
    return (
        f"{_pos_tier(s.get('percent'))}·持续{persist}轮"
        f"·{_motion(s.get('consistency'), s.get('volume_price'))}"
        f"·{_fmt_main_net(s.get('main_net'))}"
        f"{_fmt_main_pct(s.get('main_net'), s.get('amount'))}"
    )


def _section(s: Mapping[str, Any]) -> str:
    """持续组内的**分节**（描述性归类，非推荐档位；展示顺序见 `SECTION_ORDER`）。

    判定优先级：低位·资金强 > 高位加速 > 量价背离 > 持续走强（默认）。
    每票只归一节。判据全部层内自算 + 快照自带 main_net，不读其它区域数据。
    """
    percent = s.get("percent")
    main_net = s.get("main_net")
    if (
        isinstance(percent, (int, float))
        and math.isfinite(percent)
        and percent < CS_POS_ACCEL
        and isinstance(main_net, (int, float))
        and math.isfinite(main_net)
        and main_net > 0
        and s.get("consistency") == "全正"
        and s.get("volume_price") == "配合"
    ):
        return "低位·资金强"
    if isinstance(percent, (int, float)) and math.isfinite(percent) and percent >= CS_POS_HIGH:
        return "高位加速"
    if s.get("volume_price") == "背离":
        return "量价背离"
    return "持续走强"


def _rank_tier(s: Mapping[str, Any]) -> int:
    """展示质量层：全正·配合 > 全正·— > 全正·背离/混合·配合 > 其余。

    只看本层自算字段（consistency / volume_price），不引入外部区域数据。
    """
    cons = s.get("consistency")
    vp = s.get("volume_price")
    if cons == "全正" and vp == "配合":
        return 3
    if cons == "全正" and vp == "—":
        return 2
    if cons == "全正" and vp == "背离":
        return 1
    if cons == "混合" and vp == "配合":
        return 1
    return 0


#: 展示分组码（`signal_sort_key` 的首个分量，也是一一对应）：越大越靠前。
#: 终端 `≡` 块按它分两段展示；中间组（`_GROUP_MID`）**不展示**（见 config 注释）。
_GROUP_PERSIST = 2
_GROUP_MID = 1
_GROUP_NEW = 0


def _display_group(s: Mapping[str, Any]) -> int:
    """展示分组（**纯层内**：只用 `persist`，即本层自己的轮次计数器）。

    持续 = 当日累计出现 >=`CS_PERSIST_MIN` 轮；新进 = 当轮首现（persist <= 1）；
    其余（2 <= persist < CS_PERSIST_MIN）归中间组，**不展示、但仍参与排序**
    （要让 AI 素材与终端同源，就不能从排序里挖一个洞）。
    """
    persist = int(s.get("persist") or 0)
    if persist >= CS_PERSIST_MIN:
        return _GROUP_PERSIST
    if persist <= 1:
        return _GROUP_NEW
    return _GROUP_MID


def _display_pct_ok(s: Mapping[str, Any]) -> bool:
    """展示涨幅闸：只挡明确超过上限的票；缺值无法判 ⇒ fail-open 照常展示。
    与 `_display_flow_ok` 同纪律：只在两个展示出口调用，信号仍全部落库、排序键不变。
    """
    pct = s.get("percent")
    if not isinstance(pct, (int, float)) or not math.isfinite(pct):
        return True
    return float(pct) <= CS_DISPLAY_MAX_PERCENT


def _display_flow_ok(s: Mapping[str, Any]) -> bool:
    """展示层资金闸（2026-11-20）：只挡**明确**主力净流入不足的票。

    判据只有 `main_net > CS_DISPLAY_MIN_MAIN_NET`（默认 300 万，严格大于），
    **不看当日涨跌** —— 跌 8% 但主力净流入 >300 万的票照常留，涨 8% 但净流入
    不足 300 万的票不占展示位。快照没给
    `main_net` 时无法判资金 ⇒ fail-open 照常展示（不把「没数据」当「流出」）。
    只在 `snapshot_payload` / `format_terminal_block` 出口调用：信号仍全部落库、
    排序键不变，避免展示层变成第二结论源。
    """
    main_net = s.get("main_net")
    if not isinstance(main_net, (int, float)) or not math.isfinite(main_net):
        return True
    return float(main_net) > CS_DISPLAY_MIN_MAIN_NET


def signal_sort_key(s: Mapping[str, Any]) -> tuple[float, ...]:
    """终端 `≡` 块与 AI 素材**共用**的展示排序键（2026-11-17 改分组优先）。

    先分组（稳定锚点），再比质量/持续/强度：
      展示分组（持续 > 中间 > 新进）
      → 质量层(全正·配合 > 全正·— > 其余) → 当日轮次 → 横截面分位 → 额涌 → 量比。
    返回「越小越靠前」的元组（各键取负）。轮次/分位等缺值按 0 参与，
    与 `_zscore` 的缺值纪律一致：缺值是「未知」而非「很强」。

    ## 为什么分组提到质量层前面（2026-11-17）

    旧键是「质量层 → 轮次 → 分位…」，而 `全正·配合` 在 **1 轮窗口**里噪声极易
    达成 ⇒ 实测当日展示行相邻两轮重叠**中位数 1/8**（几乎整屏换新），人读不出重点
    （用户原话：「变化太快，眼花缭乱不知道咋选」）。稳定的票确实存在（当日 756 只
    里 180 只出现过 >=5 轮），但被单轮噪声顶下去了。
    分组后：持续组内部仍按质量→轮次→分位排（组内质量闸不变）。
    """
    return (
        -float(_display_group(s)),
        -float(_rank_tier(s)),
        -float(s.get("persist") or 0),
        -float(s.get("pct_rank") or 0.0),
        -float(s.get("amount_surge") or 0.0),
        -float(s.get("vol_ratio") or 0.0),
    )


def _fmt_num(v: float | None, digits: int = 2, suffix: str = "") -> str:
    if v is None or not isinstance(v, (int, float)) or not math.isfinite(v):
        return "—"
    return f"{v:.{digits}f}{suffix}"


#: 本层**面向面板（web）的列投影**。终端 `≡` 块是自由文本（宽度靠 f-string 拼），
#: 故这张表只服务面板 —— 但字段名与 `_row()` 逐个对应，改一边要同改另一边。
#: 2026-11-19（方案二）：数字列撤出终端/面板（仍落库），改画像 + 分节；
#: `画像` 列吃 `profile_text`（Python 拼，前端不重拼——重拼即第二份口径）。
COLS_CS: tuple = (
    ("#", 3, "r"),
    ("代码", 12, "l"),
    ("名称", 10, "l"),
    ("现价", 8, "r"),
    ("涨幅", 8, "r"),
    ("轮次", 6, "r"),
    ("分节", 12, "l"),
    ("画像", 34, "l"),
)


def _signal_row(s: Mapping[str, Any]) -> dict[str, Any]:
    """单条信号 → 面板行（**纯标量**，键名与 `COLS_CS` 的单元格取值一一对应）。

    `profile`/`section` 由 Python 单源拼（`profile_text`/`_section`），前端原样渲染，
    **不在 TS 里重拼**（重拼即第二份口径）。数字列不再下发（仍落库可回查）。
    """
    return {
        "symbol": s.get("symbol"),
        "name": s.get("name"),
        "price": s.get("price"),
        "percent": s.get("percent"),
        "persist": int(s.get("persist") or 1),
        "profile": profile_text(s),
        "section": _section(s),
    }


def snapshot_payload() -> dict[str, Any]:
    """本层 → **面板载荷**（纯数据，无 ANSI、无文本块）。关闭/非交易时段 → `{"enabled": False}`。

    与 `format_terminal_block()`（终端文本）是**同一份进程内状态的两种投影**：
    分组/截断/排序全部复用 `_display_group` + `signal_sort_key`，不另写一套。
    唯一的分叉是「怎么排版」（终端拼字符串、面板下发列 + 行），这正是 D1 的允许范围。

    ⚠ 中间组（2~`CS_PERSIST_MIN-1` 轮）**不展示**，与终端一致；它不是「没异动」，
    而是「尚未证明持续性」—— 终端会打印一句说明，面板侧见 `hidden_mid` 计数。
    """
    h = health()
    if not h.get("enabled", True) or h.get("state") in ("disabled", "non_trading"):
        return {"enabled": False}
    with _lock:
        signals = list(_state["last_signals"])
    persist_rows = [
        s for s in signals
        if _display_group(s) == _GROUP_PERSIST and _display_flow_ok(s) and _display_pct_ok(s)
    ][:CS_TOP_PERSIST]
    new_rows = [
        s for s in signals
        if _display_group(s) == _GROUP_NEW and _display_flow_ok(s) and _display_pct_ok(s)
    ][:CS_TOP_NEW]
    shown = {id(s) for s in persist_rows + new_rows}
    hidden_mid = sum(1 for s in signals if id(s) not in shown)
    shown_groups = (s for s in signals if _display_group(s) in (_GROUP_PERSIST, _GROUP_NEW))
    gate_stats = [
        (not _display_flow_ok(s), not _display_pct_ok(s))
        for s in shown_groups
    ]
    hidden_flow = sum(1 for f, _ in gate_stats if f)
    hidden_pct = sum(1 for _, p in gate_stats if p)
    return {
        "enabled": True,
        "state": h.get("state"),
        "time": h.get("time") or _now().strftime("%H:%M:%S"),
        "valid_rows": int(h.get("valid_rows") or 0),
        "buffer_depth": int(h.get("buffer_depth") or 0),
        "buffer_rounds": CS_BUFFER_ROUNDS,
        "p95": CS_P95,
        "signals": int(h.get("signals") or 0),
        "divergence": h.get("divergence"),
        "divergence_ok": h.get("divergence_ok"),
        "sealed_skipped": int(h.get("sealed_skipped") or 0),
        "warmup": bool(h.get("warmup")),
        "stale_rounds": int(h.get("stale_rounds") or 0),
        "persist_min": CS_PERSIST_MIN,
        "top_persist": CS_TOP_PERSIST,
        "top_new": CS_TOP_NEW,
        "display_flow_min": CS_DISPLAY_MIN_MAIN_NET,
        "hidden_mid": hidden_mid,
        "hidden_flow": hidden_flow,
        "hidden_pct": hidden_pct,
        "display_pct_max": CS_DISPLAY_MAX_PERCENT,
        # 与终端 `≡` 块同一函数生成，网页版直接渲染（不在 TS 里重拼阈值/语义）。
        "display_flow_note": _flow_gate_note(hidden_flow),
        "display_pct_note": _pct_gate_note(hidden_pct),
        "groups": {
            "persist": [_signal_row(s) for s in persist_rows],
            "new": [_signal_row(s) for s in new_rows],
        },
    }


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
    sealed = int(h.get("sealed_skipped") or 0)
    if sealed:
        # 剔除必留痕：不允许静默少票（与 push_gate 的 gate_note 同一纪律）。
        header += f" | 已剔封板 {sealed}"
    lines = [header]
    if state == "throttled":
        lines.append("  本轮快照节流（沿用上一轮结果，未重新取数）")
    elif state == "no_data":
        lines.append("  无有效快照（东财取数失败或全市场停牌）")
    elif state == "stale":
        lines.append(f"  陈旧轮：上游快照与上轮相同，本轮不产出（累计 {h.get('stale_rounds', 0)} 轮）")
    elif not h.get("warmup"):
        if h.get("native_speed"):
            pass  # 有原生涨速时不需要预热（不该走到这里，防御性留白）
        else:
            left = max(CS_WARMUP_ROUNDS - depth, 0)
            lines.append(f"  预热中 {depth}/{CS_WARMUP_ROUNDS}（60s 一轮，约 {left} 分钟后产出）")

    with _lock:
        signals = list(_state["last_signals"])
    # 已按 `signal_sort_key` 排序（分组码在最前）⇒ 按组切片即保持组内顺序。
    persist_all = [s for s in signals if _display_group(s) == _GROUP_PERSIST]
    new_all = [s for s in signals if _display_group(s) == _GROUP_NEW]
    # 展示层资金闸 + 涨幅闸：只挡明确不达标的票；信号仍全部落库、排序键不变。
    persist_rows = [s for s in persist_all if _display_flow_ok(s) and _display_pct_ok(s)][:CS_TOP_PERSIST]
    new_rows = [s for s in new_all if _display_flow_ok(s) and _display_pct_ok(s)][:CS_TOP_NEW]
    hidden_flow = sum(1 for s in persist_all + new_all if not _display_flow_ok(s))
    hidden_pct = sum(1 for s in persist_all + new_all if not _display_pct_ok(s))
    top = (persist_rows + new_rows)[:CS_TOP_N]
    if top:
        # 图例（画像串是层内自算字段的中文压缩，单源 `profile_text`；
        # 分节是描述性归类不是推荐档位，单源 `_section`，顺序见 `SECTION_ORDER`）：
        lines.append(
            "  画像：位置(蓄势<3/启动3~7/加速7~12/高位≥12%)·持续N轮·动向(量价齐升/稳步走强/"
            "反复放量/放量滞涨/一致回落/动向不明)·主力资金(净额万/亿+占额%，—=源未给)"
        )
        lines.append(
            "  排序：持续 > 新进；持续组内分节(" + "→".join(SECTION_ORDER) + ")，节内 质量 → 轮次 → 分位 → 额涌 → 量比"
        )
        lines.append("  " + _flow_gate_note(hidden_flow))
        lines.append("  " + _pct_gate_note(hidden_pct))

    def _row(i: int, s: Mapping[str, Any]) -> str:
        # 行 = 代码 名称 现价 涨幅 | 画像串（数字列已撤出终端，仍落库供回看）。
        # 画像由 `profile_text` 单源拼 —— 前端/素材不重拼。
        return f"  {i}. {s['symbol']} {s['name']} {s['price']:.2f} {s['percent']:+.2f}% | {profile_text(s)}"

    # 组标题/分节行是**普通续行**：不以 `◆`/`—` 起头（那会污染 `ai_digest/panel.py` 切块），
    # 也不匹配 `panel._CROSSSECTION_ROW_RE`（无 `序号.` 前缀）⇒ 解析器跳过组标题，
    # 但 `_CROSSSECTION_SECTION_RE` / `_CROSSSECTION_NEW_RE` 会读它们。
    idx = 0
    if persist_rows:
        lines.append(f"  持续（当日 ≥{CS_PERSIST_MIN} 轮 · {len(persist_rows)} 只，先看这组）")
        # 持续组内按分节归堆：先算每节行数，再按 SECTION_ORDER 顺序出节。
        by_sec: dict[str, list[Mapping[str, Any]]] = {}
        for s in persist_rows:
            by_sec.setdefault(_section(s), []).append(s)
        for sec in SECTION_ORDER:
            rows = by_sec.get(sec)
            if not rows:
                continue
            lines.append(f"  · {sec}（{len(rows)} 只）")
            for s in rows:
                idx += 1
                lines.append(_row(idx, s))
    elif top:
        if persist_all:
            lines.append(f"  （本轮 {len(persist_all)} 只 ≥{CS_PERSIST_MIN} 轮持续票均主力净流入不足，不占展示位）")
        else:
            lines.append(f"  （本轮无 ≥{CS_PERSIST_MIN} 轮持续票）")
    if new_rows:
        # 新进组压成**一行摘要**（单轮噪声高发，不逐行展开；仍按 signal_sort_key 序）。
        entries = " / ".join(f"{s['symbol']} {s['name']} {s['price']:.2f} {s['percent']:+.2f}%" for s in new_rows)
        lines.append(f"  新进（本轮首现 · {len(new_rows)} 只，单轮噪声高发，只看动静）：{entries}")
    if h.get("warmup") and state not in ("stale", "no_data") and not top and not signals:
        lines.append("  （本轮无 p95+ 榜外异动）")
    elif not top:
        # 有信号但都不进两组：要么全在 2~5 轮中间带，要么被资金闸挡掉 ⇒ 如实说明，不能当成「没异动」。
        if persist_all or new_all:
            lines.append(
                f"  （本轮 {len(signals)} 只 p95+ 信号均未进两组：轮次不足或主力净流入不足，不占展示位；仍落库）"
            )
        else:
            lines.append(
                f"  （本轮 {len(signals)} 只 p95+ 信号均不足 {CS_PERSIST_MIN} 轮，尚未证明持续性，本轮不展示）"
            )
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
