"""开盘量比失真系数 k(m) 的实测与去偏（2026-11-05）。

量比的定义（东财 f10 / 雪球同口径）：

    量比 = 当日累计成交量 ÷（近 5 日均每分钟成交量 × 当日已交易分钟数）

分母里的「已交易分钟数」在开盘头几分钟只有个位数，而**成交量在一天内不是均匀
到达的**（集合竞价 + 早盘集中放量）⇒ 同一只票在 09:31 的读数被放大一个量级。
关键性质：这是**全市场共同的**放大（今天大家开盘都放量），**不是个股信号**。

实测 k(m)（2026-11-05，80 只雪球分时 + 本地 1410 只全域快照）：

    已交易分钟 m    1      3      5     10     15     20     25     29
    前置系数 k(m) 13.6x   7.3x   5.7x   4.0x   3.5x   3.2x   2.9x   2.7x

⇒ 2026-09-21 设的「开盘静默 15 分钟」**并没有把失真修好**：09:45 的读数仍虚高
3.5 倍，量比门（`MOMENTUM_LAUNCH_VOL`=1.5）在 09:54 实测通过率 **94.6%**（等于
没筛），去偏后 6.2%（对照 11-04 收盘口径 11.0%）。

k(m) 的估计（**只用本地库，零新增网络**）：

    F(m) = median_i [ V_i(m) ÷ D_i ]     V_i(m) = 今日累计量（fund_flow 快照）
                                        D_i    = 近 N 个交易日收盘成交量均值
    k(m) = F(m) ÷ (m / 240)

「近 N 日收盘成交量均值」D_i 是**每只票自己的**基准 ⇒ k(m) 表达的是「今天全市场
的量能比平时前置了多少」，扣掉它就还原出「这只票相对**自身历史**放量了多少」。

自洽性检验（2026-11-05 10:24 快照，1410 只）：我方 k(m)=2.066 vs **东财自己隐含的**
k(m)=2.030（由上报量比反解）⇒ 差 1.8% ⇒ 去偏系数与量比口径本身一致（不是另造
一个指标）。去偏后全市场中位 0.96 ≈ 1（该刻全市场中位股票投影的全天量比 ≈ 自身
5 日均量），量比门（≥1.5）在同一快照上由「通过 99.1%」变成「通过 21.8%」。

⚠ **k(m) 扣的是「全市场共同的时间形状」，保留个股自己的放量幅度** ⇒ 去偏值对
「今天全市场节奏」是相对量，不是纯粹的「相对自身 5 日均速」：一只开盘就把量
砸完的票，去偏值会高于它最终的全天量比（这正是本段要抓的「量先动」）。残差
方向是**保守**的（早盘实测我方 k 比东财隐含值高 10~25% ⇒ 去偏略过度 ⇒ 量比门
在早盘偏严）。

⚠ 本模块只做**测量修正**，不改任何阈值：门槛 `MOMENTUM_LAUNCH_VOL`(1.5) 的语义
（「相对自身近 5 日均速的放量倍数」）在去偏前后完全一致。

⚠ **k(m) 是横截面中位数 ⇒ 单票的量能前置形状仍有残差**（实测 m=5 时全市场
F 的 P25–P75 为 8.5%–16.2%）：扣掉的是**共同成分**，不是每只票各自的形状。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import statistics
from datetime import datetime

from scanner.config import (
    OFFBOARD_VOL_BIAS_DAYS,
    OFFBOARD_VOL_BIAS_MIN_SAMPLE,
    now_beijing,
)
from scanner.trading_session import trading_minutes_elapsed
from scanner.utils import EXTERNAL_FAILURES, to_float

logger = logging.getLogger(__name__)

# 一只票至少要有这么多个交易日的收盘量样本才纳入 D_i（新股/新入库票不够 ⇒ 不参与
# k(m) 的估计，也不因「没历史」被误判 —— 与 B 段流门的 fail-open 语义同向）。
_MIN_REF_DAYS = 2

# D_i 的日内缓存：{(参考交易日元组, 行数): {symbol: 均量}}。参考日集合只在跨日时变
# ⇒ 每个交易日只算一次（5×1400 行 SQL 聚合 ≈ 0.2s），其余轮次零成本。
# 行数进键：同一批日期在不同时刻可能对应不同的行集合（补写历史快照 / 单测复用日期）。
_REF_CACHE: dict[tuple, dict[str, float]] = {}


def reference_dates(conn: sqlite3.Connection, *, days: int, today: str) -> tuple[str, ...]:
    """最近 N 个**有 fund_flow 快照**的交易日（不含今日），由近及远。

    ⚠ 用「库里有哪些日期」而不是交易日历：快照缺失的日子本来就没有量比基准，
    拿日历里的空白日去平均会把 D_i 拉低（k(m) 偏大）⇒ 去偏过度。

    ⚠ 另剔掉「**收盘前就停了**」的日子：若某天最后一张快照停在上午（如 11:30），
    它的成交量只有全天的一半 ⇒ D_i 被低估 ⇒ k(m) 被高估 ⇒ 去偏过度（方向是
    偏保守：量比门更严）。故要求当天的最后一张快照落在**下午**（≥13:00）。
    """
    try:
        rows = conn.execute(
            "SELECT date, MAX(updated) FROM market_extra_cache "
            "WHERE data_type='fund_flow' AND date < ? GROUP BY date ORDER BY date DESC LIMIT ?",
            (today, days * 2),
        ).fetchall()
    except sqlite3.Error as e:
        logger.warning("量比去偏：参考交易日查询失败（判不可判定）: %s", e)
        return ()
    out: list[str] = []
    for d, last_updated in rows:
        d = str(d)
        if last_updated and str(last_updated) < f"{d}T13:00:00":
            continue  # 当天最后一张快照在下午之前 ⇒ 量不是全天量，不做基准
        out.append(d)
        if len(out) >= days:
            break
    return tuple(out)


def reference_volumes(
    conn: sqlite3.Connection,
    *,
    days: int = OFFBOARD_VOL_BIAS_DAYS,
    today: str | None = None,
) -> dict[str, float]:
    """近 N 个交易日收盘成交量均值 `{symbol: 股}`（本地库，零新增网络）。

    收盘快照的 `volume` 是当日累计量（收盘前最后一次落库）⇒ 可当「全天量」用。
    """
    today = today or now_beijing().date().isoformat()
    dates = reference_dates(conn, days=days, today=today)
    if not dates:
        return {}
    # 缓存键带**行数**：同一个日期集合在不同时刻可能对应不同的行集合（补写历史快照 /
    # 单测复用同一批日期）⇒ 只按日期缓存会把上一次的行集合当成本次的结果。
    try:
        placeholders = ",".join("?" * len(dates))
        cnt = conn.execute(
            f"SELECT COUNT(*) FROM market_extra_cache WHERE data_type='fund_flow' AND date IN ({placeholders})",  # noqa: S608 - 占位符是 `?`，值走参数绑定
            dates,
        ).fetchone()[0]
    except sqlite3.Error:
        cnt = -1
    key = (dates, int(cnt))
    cached = _REF_CACHE.get(key)
    if cached is not None:
        return cached
    placeholders = ",".join("?" * len(dates))
    sql = (
        "SELECT symbol, AVG(v), COUNT(v) FROM ("  # noqa: S608 - 占位符是 `?`，值走参数绑定；表名/字段名均为字面量
        f"SELECT symbol, json_extract(payload_json, '$.volume') AS v "
        f"FROM market_extra_cache WHERE data_type='fund_flow' AND date IN ({placeholders})"
        ") WHERE v > 0 GROUP BY symbol"
    )
    rows: list[tuple] | None
    try:
        rows = conn.execute(sql, dates).fetchall()
    except sqlite3.Error as e:
        # json_extract 遇到非法 JSON 会整条查询报错 ⇒ 退回 Python 侧解析
        # （慢，但只在数据损坏时走；失败则判不可判定，不抛）。
        logger.warning("量比去偏：SQL 聚合失败（改用 Python 解析）: %s", e)
        rows = _reference_volumes_py(conn, dates)
    if rows is None:
        return {}
    out: dict[str, float] = {}
    for sym, avg_v, cnt in rows:
        if cnt is not None and int(cnt) >= _MIN_REF_DAYS and avg_v:
            out[str(sym)] = float(avg_v)
    _REF_CACHE[key] = out
    return out


def _reference_volumes_py(conn: sqlite3.Connection, dates: tuple[str, ...]) -> list[tuple] | None:
    """SQL 聚合失败时的兜底：逐行 `json.loads` 取 volume。返回 None = 也失败。"""
    placeholders = ",".join("?" * len(dates))
    try:
        raw = conn.execute(
            "SELECT symbol, payload_json FROM market_extra_cache "  # noqa: S608 - 占位符是 `?`，值走参数绑定
            f"WHERE data_type='fund_flow' AND date IN ({placeholders})",
            dates,
        ).fetchall()
    except sqlite3.Error as e:
        logger.warning("量比去偏：Python 兜底读取也失败（判不可判定）: %s", e)
        return None
    acc: dict[str, list[float]] = {}
    for sym, pj in raw:
        try:
            v = to_float(json.loads(pj).get("volume"), 0.0) or 0.0
        except (json.JSONDecodeError, TypeError, AttributeError):
            continue
        if v > 0:
            acc.setdefault(str(sym), []).append(v)
    return [(sym, sum(vs) / len(vs), len(vs)) for sym, vs in acc.items()]


def snapshot_elapsed_minutes(conn: sqlite3.Connection | None, now: datetime | None = None) -> int:
    """快照**自身**的已交易分钟数（按它最后一次落库的时刻算）；取不到则退回当前时钟。

    ⚠ 为什么不用当前时钟：上报量比是**快照那一刻**算出来的，分母是**那一刻**的已交易
    分钟数。资金流接口抽风时快照会冻结在旧时刻（分钟数对不上）⇒ 用当前时钟算 k(m)
    会把系数算错（偏大/偏小都可能）⇒ 去偏后的量比整体失真。故按快照时刻算。
    """
    if conn is None:
        return trading_minutes_elapsed(now)
    today = (now or now_beijing()).date().isoformat()
    ts: datetime | None = None
    try:
        row = conn.execute(
            "SELECT MAX(updated) FROM market_extra_cache WHERE data_type='fund_flow' AND date=?",
            (today,),
        ).fetchone()
        if row and row[0]:
            ts = datetime.fromisoformat(str(row[0]))
    except (sqlite3.Error, ValueError) as e:
        logger.warning("量比去偏：快照时刻读取失败（退回当前时钟）: %s", e)
        ts = None
    return trading_minutes_elapsed(ts or now)


def market_bias(
    conn: sqlite3.Connection | None,
    snapshot: dict[str, dict],
    *,
    now: datetime | None = None,
) -> float | None:
    """全市场量比前置系数 k(m)；**不可判定返回 None**（调用方据此走兜底窗口）。

    k(m) ≤ 1 意味着「今天的量能比平时还平缓」⇒ 上报读数**没有**虚高 ⇒ 修正会把
    读数错误地放大 ⇒ 故 k ≤ 1 时返回 None（= 不修正），调用方按「无修正」处理。
    """
    if conn is None or not snapshot:
        return None
    elapsed = snapshot_elapsed_minutes(conn, now)
    if elapsed <= 0:
        return None
    ref = reference_volumes(conn)
    if not ref:
        return None
    fracs: list[float] = []
    for sym, payload in snapshot.items():
        if not payload:
            continue
        vol = to_float(payload.get("volume"), 0.0) or 0.0
        base = ref.get(sym)
        if vol > 0 and base and base > 0:
            fracs.append(vol / base)
    if len(fracs) < OFFBOARD_VOL_BIAS_MIN_SAMPLE:
        return None
    try:
        k = statistics.median(fracs) / (elapsed / 240)
    except (statistics.StatisticsError, ValueError) as e:  # pragma: no cover - 防御
        logger.warning("量比去偏：k(m) 计算失败（判不可判定）: %s", e)
        return None
    if k <= 1.0:
        return None
    return k


def apply_bias(snapshot: dict[str, dict], bias: float | None) -> int:
    """把快照里的**上报量比**就地换成**去偏量比**，返回改写行数。

    就地改（而非另建字段）是有意的：B 段/榜内异动段的门判定、排序、展示、落库
    全部读同一个 `vol_ratio` 键 ⇒ 一处改写即全链路口径一致，不会出现「门用新值、
    展示用旧值」的错位（这正是 A 段 `volume_ratio` 曾经踩过的坑，见 `hot_watch`）。

    原值留在 `vol_ratio_raw`（仅内存，不落库）⇒ 落库时仍能记下「上报原值」。

    `bias` 为 None 或 ≤1 ⇒ 不改（读数本就没虚高）。
    """
    if not bias or bias <= 1.0:
        return 0
    changed = 0
    for payload in snapshot.values():
        if not payload:
            continue
        raw = to_float(payload.get("vol_ratio"), None)
        if raw is None or raw <= 0:
            continue
        payload["vol_ratio"] = raw / bias
        payload["vol_ratio_raw"] = raw
        changed += 1
    return changed


def safe_market_bias(
    conn: sqlite3.Connection | None,
    snapshot: dict[str, dict],
    *,
    now: datetime | None = None,
) -> float | None:
    """`market_bias` 的 fail-open 包装：任何读取异常都判「不可判定」，不打断主循环。"""
    try:
        return market_bias(conn, snapshot, now=now)
    except EXTERNAL_FAILURES as e:
        logger.warning("量比去偏：系数计算异常（判不可判定，走兜底窗口）: %s", e)
        return None


def biased_snapshot(conn: sqlite3.Connection | None, snapshot: dict[str, dict]) -> float | None:
    """实测 k(m) 并**就地**去偏快照；返回 k（None = 不可判定 ⇒ 未修正）。

    榜外异动段（B）与榜内异动段（C）共用这一个入口 —— 两段用**同一个**量比门
    （`MOMENTUM_LAUNCH_VOL`）、**同一份**快照 ⇒ 必须是同一个尺度。
    """
    bias = safe_market_bias(conn, snapshot)
    if bias is not None:
        apply_bias(snapshot, bias)
    return bias
