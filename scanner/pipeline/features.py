"""增强阶段的特征计算纯函数（从 `orchestrator.scan_with_raw` 等价抽出，2026-09-13）。

**等价纪律**：见 `scanner/pipeline/__init__.py`。改动本模块后必须跑
`python scripts/golden_scan.py --date <四个日期>`，全部退出码 0 才算等价。
"""

from __future__ import annotations

from typing import Any

from scanner.models import KlineBar


def _hist_accum(kline_bars: list[KlineBar] | None, today: str) -> float | None:
    """历史 5 日累计涨幅（**排除今日**）；数据不足 6 根收盘价时返回 None。

    口径说明（原注释，勿改）：`short_term` 的 `accumulated` 本身包含今日（策略语义），
    所以调用方要用 `accum_map` 把它覆盖成历史口径，否则 RPS 百分位系统性偏高。
    """
    if not kline_bars:
        return None
    hist = [k for k in kline_bars if k["date"] != today]
    closes = [k["close"] for k in hist]
    if len(closes) < 6:
        return None
    return (closes[-1] - closes[-6]) / closes[-6] * 100


def build_rps_inputs(
    gem_stocks: list, candidates: list, klines: dict[str, list[KlineBar] | None], today: str
) -> tuple[list[float], dict[str, float]]:
    """构建 RPS 的基准分布与各候选的历史口径累计涨幅。

    返回 `(rps_baseline, accum_map)`：
      - `rps_baseline`：全 GEM 监控集（**过滤后、含未入选候选**）的历史 5 日累计涨幅列表。
        用全监控集而非仅已入选票，RPS 才表达「相对全市场强弱」，而不是在已涨票里比谁涨得多。
      - `accum_map`：symbol → 历史 5 日累计涨幅，供 `compute_rps` 覆盖 short_term 的含今日口径。

    两趟循环的顺序与过滤条件（`len(closes) >= 6`）与原实现逐字一致。
    """
    rps_baseline: list[float] = []
    for s in gem_stocks:
        acc = _hist_accum(klines.get(s.symbol), today)
        if acc is not None:
            rps_baseline.append(acc)

    accum_map: dict[str, float] = {}
    for c in candidates:
        acc = _hist_accum(klines.get(c.stock.symbol), today)
        if acc is not None:
            accum_map[c.stock.symbol] = acc
    return rps_baseline, accum_map


# 分时趋势写入候选维度的字段映射（原 scan_with_raw L481-488，逐字搬移）
_MINUTE_DIM_KEYS = {
    "minute_steady_rise": ("steady_rise_ratio", 0.0),
    "minute_day_high": ("day_high_pct", 0.0),
    "minute_am_high": ("am_high_pct", 0.0),
    "minute_vol_trend": ("vol_trend", 1.0),
}


def attach_minute_trends(candidates: list, minute_trends: dict[str, dict | None]) -> None:
    """把分时趋势摘要写进候选的 kline.dimensions（原地修改）。

    纯展示用途：盘中操作纪律 rule 3/5/7 读这些字段，**不参与评分**。
    `c.kline` 为空或无该票趋势数据时跳过（与原实现一致）。
    """
    for c in candidates:
        trend = minute_trends.get(c.stock.symbol)
        if c.kline and trend:
            c.kline.dimensions.update(
                {dim: trend.get(src, default) for dim, (src, default) in _MINUTE_DIM_KEYS.items()}
            )


def build_kline_dimension_patch(trend: dict) -> dict[str, Any]:
    """分时趋势 → dimensions 补丁（供单测与未来复用，不落任何状态）。"""
    return {dim: trend.get(src, default) for dim, (src, default) in _MINUTE_DIM_KEYS.items()}
