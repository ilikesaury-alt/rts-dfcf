"""高确定性「精选」评分（confidence 0~100）。

聚合用户关心的四类信号：
  1. 多维度交叉验证（validator 共振、RPS、双源、板块、排名轨迹、重复上榜）
  2. 趋势/位置安全（MA 多头、非鱼尾超买、累计涨幅黄金区间）
  3. 强度确认（分时强度、开盘强度、实时量能、量比健康）
  4. 历史胜率（可选软信号：recommendations.next_day_pct 均值）

硬排除（不进精选）：鱼尾超买、累计过高、验证未过、含 crash day。
阈值门禁由 orchestrator 在 confidence >= CONF_MIN 后取 Top CONF_TOP_N。
"""
from __future__ import annotations

import sqlite3

from scanner.config import (
    CONF_MIN,
    CONF_TOP_N,
    CONF_EXCLUDE_ACCUM,
)
from scanner.rank_trend import rank_trajectory_score


def _symbol_win_rate(conn: sqlite3.Connection, symbol: str) -> float | None:
    """返回 symbol 历史推荐次一交易日收益均值（可选软信号）。

    仅当 recommendations 中该 symbol 有回填的 next_day_pct 时才计算。
    查不到 / 异常 -> 返回 None（不加分、不阻塞）。
    """
    try:
        cur = conn.execute(
            "SELECT AVG(next_day_pct) FROM recommendations "
            "WHERE symbol = ? AND next_day_pct IS NOT NULL",
            (symbol,),
        )
        row = cur.fetchone()
        if row and row[0] is not None:
            return float(row[0])
    except Exception:
        return None
    return None


def compute_confidence(
    candidates: list,
    conn: sqlite3.Connection | None,
    list_presence: dict[str, int] | None = None,
) -> dict[str, int]:
    """对每个候选算 confidence 分，返回 {symbol: int}。

    candidates 元素为 Candidate dataclass（scanner.models）。
    硬排除项直接记 0（不进精选）；其余按信号软累加并 clamp 到 [0, 100]。
    """
    list_presence = list_presence or {}
    result: dict[str, int] = {}

    for c in candidates:
        sym = c.stock.symbol
        k = c.kline
        dims = k.dimensions if k else {}

        # ---- 硬排除（确定性直接归零）----
        if dims.get("v_st_overbought") or dims.get("v_mo_overbought"):
            result[sym] = 0  # 鱼尾段超买
            continue
        accum = k.accumulated_pct if k else 0.0
        if accum > CONF_EXCLUDE_ACCUM:
            result[sym] = 0  # 累计过高（鱼尾风险）
            continue
        validation_bonus = dims.get("validation_bonus", 0) or 0
        if validation_bonus <= 0:
            result[sym] = 0  # 未过交叉验证（validator 无正共振）
            continue
        if "momentum_no_crash" not in dims and c.category == "momentum":
            result[sym] = 0  # 含 crash day
            continue

        # ---- 软性累加 ----
        score = 0

        # 1. 多维度交叉验证（+）
        score += min(validation_bonus, 10)  # 验证共振（上限 10）
        if (dims.get("rps_bonus") or 0) > 0:
            score += 4
        if c.stock.source_tag == "both":
            score += 4  # 雪球+同花顺双源交叉
        if (dims.get("sector_bonus") or 0) > 0:
            score += 3
        traj = rank_trajectory_score(sym)
        if traj > 0:
            score += min(traj, 6)  # 排名轨迹正向（最多 6）
        if list_presence.get(sym, 0) >= 2:
            score += 4  # 重复上榜（资金持续关注）

        # 2. 趋势/位置安全（+/-）
        ma_bull = dims.get("momentum_ma_bull") or dims.get("v_pb_ma_trend")
        if ma_bull and ma_bull > 0:
            score += 4
        # 累计涨幅黄金区间 10%~30% 最佳；过高已在上方硬排除
        if 10.0 <= accum <= 30.0:
            score += 6
        elif 5.0 <= accum < 10.0:
            score += 3

        # 3. 强度确认
        intraday = dims.get("intraday_score") or 0
        if isinstance(intraday, (int, float)) and intraday > 0:
            score += 3
        opening = dims.get("opening_score") or 0
        if isinstance(opening, (int, float)) and opening > 0:
            score += 3
        if (dims.get("live_vol_bonus") or 0) > 0:
            score += 2
        vr = k.volume_ratio if k else 0.0
        if 1.0 <= vr <= 2.0:
            score += 2  # 量比健康（非爆量非萎缩）

        # 4. 历史胜率（可选软信号）
        if conn is not None:
            wr = _symbol_win_rate(conn, sym)
            if wr is not None and wr > 0:
                score += min(int(wr), 8)  # 次日均收益为正，最多 +8

        result[sym] = max(0, min(100, score))

    return result


def select_picks(
    candidates: list,
    confidence_map: dict[str, int],
) -> list:
    """按 confidence 降序取 Top CONF_TOP_N（仅 >= CONF_MIN）。"""
    eligible = [
        c for c in candidates
        if confidence_map.get(c.stock.symbol, 0) >= CONF_MIN
    ]
    eligible.sort(key=lambda c: -confidence_map.get(c.stock.symbol, 0))
    return eligible[:CONF_TOP_N]
