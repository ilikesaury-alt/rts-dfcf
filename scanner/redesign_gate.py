"""重新设计筛选系统 —— L0/L1 真过滤（2026-09-03 落地）。

此前 display.py 只做"展示层"划线（⛔ + 空仓红字），但候选仍照常落库、推送、展示，
等于没过滤。本模块把 L0/L1 变成*真过滤*：不过关候选从候选集移除（不进 ScanResult
→ 不落库 → 不展示 → 不推飞书）并标 excluded=1，同时写 scan_rejections 留痕供审计。

设计要点：
- 复用项目既有排除模式（orchestrator._update_excluded_marks 同款 UPDATE + scan_rejections
  独立表），与风险硬过滤并列，不污染回测样本（归因读 excluded=0 / scan_rejections 隔离）。
- 规则纯函数化（`block_reasons_from_fields`）为单一事实来源，display 的 dict 适配与
  orchestrator 的 Candidate 适配共用，避免两处逻辑漂移。
- fail-open：落库异常由调用方捕获，gate 失败则回退原逻辑（当轮不拦截），不阻塞扫描主流程。
"""
from __future__ import annotations

from scanner.config import (
    REDESIGN_BLOCK_AFTER_HOUR,
    REDESIGN_EXCLUDE_CATS,
    REDESIGN_MAX_TODAY_PCT,
    REDESIGN_POOL_WIDTH_MIN,
    REDESIGN_TREND_ALLOW,
    now_beijing,
)


def block_reasons_from_fields(
    category: str | None,
    percent: float | None,
    trend: str | None,
    hour: int | None,
) -> list[str]:
    """L1 硬 gate 纯函数（无 IO、无 Candidate/RecommendationRow 依赖）。

    返回命中原因列表；空列表 = 通过「R5 合格」。display 与 orchestrator 两套适配共用。
    fail-open：解析类异常不剔（返回空），避免误杀。
    """
    reasons: list[str] = []
    try:
        if category in REDESIGN_EXCLUDE_CATS:
            reasons.append("动量族")
        if percent is not None and percent >= REDESIGN_MAX_TODAY_PCT:
            reasons.append(f"涨幅{percent:.1f}%≥{REDESIGN_MAX_TODAY_PCT:.0f}%")
        if trend and trend not in REDESIGN_TREND_ALLOW:
            reasons.append(f"trend:{trend}")
        if hour is not None and hour >= REDESIGN_BLOCK_AFTER_HOUR:
            reasons.append(f"尾盘{hour:02d}:00后信号")
    except (ValueError, TypeError, IndexError):
        pass
    return reasons


def candidate_block_reasons(c) -> list[str]:
    """L1 适配：作用于 orchestrator 的 Candidate 对象。

    字段映射：category → c.category；当日涨幅 → c.stock.percent（推荐时刻）；
    trend → c.kline.trend；推荐时刻小时 → 当前扫描运行小时（实时扫描语义与落库 time 一致）。
    """
    pct = getattr(getattr(c, "stock", None), "percent", None)
    trend = None
    kline = getattr(c, "kline", None)
    if kline is not None:
        trend = getattr(kline, "trend", None)
    hour = now_beijing().hour
    return block_reasons_from_fields(c.category, pct, trend, hour)


def _mark_excluded(conn, today: str, items: list[tuple[str, str, str]]) -> None:
    """标 excluded=1（覆盖本轮及历史轮次同 (symbol, category) 行）。

    items = [(symbol, category, reason), ...]。按 (symbol, category) 精确标记：
    L1 是类别级规则（动量族等），与风险排除（symbol 级、主力出货等标签跨类别成立）
    不同，故不整 symbol 置位，避免误杀同 symbol 的其他通过类别。
    """
    if not items:
        return
    conn.executemany(
        "UPDATE recommendations SET excluded=1, excluded_reason=? WHERE date=? AND symbol=? AND category=?",
        [(reason, today, sym, cat) for sym, cat, reason in items],
    )


def _revoke_all_today(conn, today: str) -> None:
    """L0 空仓：撤销当日全部推荐（整体 excluded=1）。"""
    conn.execute(
        "UPDATE recommendations SET excluded=1, excluded_reason='redesign:L0空仓' WHERE date=?",
        (today,),
    )


def apply_redesign_gate(candidates: list, conn, today: str) -> tuple[list, bool]:
    """对候选集执行 L0/L1 真过滤。

    返回 (blocked_candidates, l0_closed)。
    - blocked_candidates：未过 L1 的候选（已设置 excluded_reason，供调用方 save_rejections）。
    - l0_closed：L0 池窄（R5 合格 < REDESIGN_POOL_WIDTH_MIN）→ 当日整体空仓。

    副作用（fail-open，由调用方 try 包裹）：
    - 为 blocked 候选标 recommendations.excluded=1；
    - 若 l0_closed，撤销当日全部 recommendations（blanket excluded=1）。
    落库均不 commit（调用方在 scan_with_raw 末尾统一提交）。
    """
    blocked = [c for c in candidates if candidate_block_reasons(c)]
    r5 = len(candidates) - len(blocked)
    l0_closed = r5 < REDESIGN_POOL_WIDTH_MIN

    for c in blocked:
        reasons = candidate_block_reasons(c)
        c.excluded_reason = "redesign:L1 " + ",".join(reasons)

    items = [(c.stock.symbol, c.category, c.excluded_reason) for c in blocked]
    _mark_excluded(conn, today, items)

    if l0_closed:
        _revoke_all_today(conn, today)
        return list(candidates), True, r5

    return blocked, False, r5
