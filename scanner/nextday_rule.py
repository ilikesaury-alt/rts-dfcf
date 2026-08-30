"""次日大涨高概率规则（display-only，2026-08-30）。

实证规则：ma5r ≥ 5% & atrpct ≥ 8% & ret20 ≤ 40%。
- H2 盲测 LIFT 1.53x，均值 +1.59%（扣 0.30% 净 +1.29%），跌超7% 仅 7.5%（基准 11.6%）。
- 全部只用 T-1 及之前已完成 bar，盘中任意时刻可算，全天不漂移。

本模块只做 DB-only 计算（appearances + daily_kline），不写 recommendations，
不改 score / 不进综合排序——纯展示层增量。
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

from scanner.config import (
    NEXTDAY_RULE_ATRPCT_MIN,
    NEXTDAY_RULE_BARS,
    NEXTDAY_RULE_MA5R_MIN,
    NEXTDAY_RULE_RET20_MAX,
    now_beijing,
)
from scanner.models import KlineBar
from scanner.utils import EXTERNAL_FAILURES

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RulePick:
    """规则命中一条记录。"""

    symbol: str
    name: str
    ma5r: float   # 收盘距5日均线距离（%），正=在上方
    atrpct: float # 21日平均真实波幅（%）
    ret20: float  # 21日涨幅（%）
    already_rec: bool  # 已被现有系统推荐


@dataclass(frozen=True)
class RuleResult:
    """规则扫描结果。"""

    picks: list[RulePick]
    board_size: int  # 今日上榜总数
    rule_hit: int    # 规则命中数
    today: str


def _compute_features(
    klines: list[KlineBar],
    today_idx: int,
) -> tuple[float, float, float] | None:
    """从 klines（已按 date 排序）计算 ma5r / atrpct / ret20。

    所有特征只用 T-1 及之前已完成 bar。klines[today_idx] 为当日 bar（未收盘），
    但本函数不使用它——只用 klines[:today_idx]（已完成 bars）。

    返回 (ma5r%, atrpct%, ret20%) 或 None（数据不足）。
    """
    # today_idx 指向当日 bar；已完成 bars 在其之前
    if today_idx < NEXTDAY_RULE_BARS + 1:
        # 需要至少 NEXTDAY_RULE_BARS+1 根已完成 bar（21 bars for ret20/ATR + 5 bars for MA5）
        return None

    completed = klines[:today_idx]  # 只用已完成 bar
    if len(completed) < NEXTDAY_RULE_BARS + 1:
        return None

    # ma5r：close[T-1] / mean(close[T-5..T-1]) - 1
    closes = [b["close"] for b in completed]
    c_prev = closes[-1]  # close[T-1]
    ma5 = sum(closes[-5:]) / 5
    if ma5 <= 0:
        return None
    ma5r = (c_prev / ma5 - 1) * 100

    # atrpct：mean((high-low)/close) over T-21..T-1 × 100
    atr_window = completed[-NEXTDAY_RULE_BARS:]
    atr_pairs = [
        (b["high"] - b["low"]) / b["close"]
        for b in atr_window
        if b["close"] > 0 and b["high"] > 0 and b["low"] > 0
    ]
    if len(atr_pairs) < NEXTDAY_RULE_BARS * 0.8:
        return None
    atrpct = (sum(atr_pairs) / len(atr_pairs)) * 100

    # ret20：close[T-1] / close[T-21] - 1
    c_21 = closes[-NEXTDAY_RULE_BARS]
    if c_21 <= 0:
        return None
    ret20 = (c_prev / c_21 - 1) * 100

    return ma5r, atrpct, ret20


def scan_rule(conn: sqlite3.Connection, today: str | None = None) -> RuleResult | None:
    """扫描当日榜单，返回规则命中结果。

    today 默认取真实今日。返回 None 表示今日无数据（非交易日或库空）。
    """
    today = today or now_beijing().date().isoformat()

    # 1. 获取今日上榜股票
    try:
        cur = conn.execute(
            "SELECT DISTINCT symbol, name FROM appearances WHERE date = ?",
            (today,),
        )
        board = cur.fetchall()
    except EXTERNAL_FAILURES as e:
        # 仅外部故障（sqlite3.Error 等）降级；编程错误冒泡到主循环兜底
        logger.warning("appearances query failed for %s: %s", today, e)
        return None

    if not board:
        return None

    board_syms = [s for s, _ in board]
    board_names = dict(board)

    # 2. 批量读取 klines（内部自带 EXTERNAL_FAILURES 降级，失败返回空 map）
    from scanner.db.queries import get_cached_klines  # 避免循环导入

    klines_map = get_cached_klines(conn, board_syms)

    # 3. 获取今日已有推荐（判断 already_rec）
    try:
        cur = conn.execute(
            "SELECT DISTINCT symbol FROM recommendations WHERE date = ?",
            (today,),
        )
        rec_syms = {r[0] for r in cur.fetchall()}
    except EXTERNAL_FAILURES as e:
        # 仅外部故障降级为空集（fail-open，票仍展示但标「新增」）；编程错误冒泡
        logger.warning("recommendations query failed for %s: %s", today, e)
        rec_syms = set()

    # 4. 逐票计算特征并应用规则
    picks: list[RulePick] = []
    for sym in board_syms:
        klines = klines_map.get(sym)
        if not klines or len(klines) < NEXTDAY_RULE_BARS + 2:
            continue

        # 找到今日 bar 的索引（按 date 排序，今日或最新）
        today_idx = None
        for i, b in enumerate(klines):
            if b["date"] == today:
                today_idx = i
                break

        if today_idx is None:
            # 今日 bar 尚未写入（盘前/非交易时段/K线补拉失败）：缓存里全部是已完成
            # bar，最后一根即最近完成交易日，特征以它为 T-1 锚点计算
            # （2026-08-30 review 修正：原 len-1 会错漏最后一根已完成 bar）
            today_idx = len(klines)

        # 确保 today_idx 指向的 bar 之前有足够的已完成 bar
        if today_idx < NEXTDAY_RULE_BARS + 1:
            continue

        feat = _compute_features(klines, today_idx)
        if feat is None:
            continue

        ma5r, atrpct, ret20 = feat

        if (
            ma5r >= NEXTDAY_RULE_MA5R_MIN
            and atrpct >= NEXTDAY_RULE_ATRPCT_MIN
            and ret20 <= NEXTDAY_RULE_RET20_MAX
        ):
            picks.append(
                RulePick(
                    symbol=sym,
                    name=board_names.get(sym, sym),
                    ma5r=round(ma5r, 2),
                    atrpct=round(atrpct, 2),
                    ret20=round(ret20, 2),
                    already_rec=sym in rec_syms,
                )
            )

    # 按 atrpct 降序排列（波动更大优先）
    picks.sort(key=lambda p: -p.atrpct)

    return RuleResult(
        picks=picks,
        board_size=len(board_syms),
        rule_hit=len(picks),
        today=today,
    )
