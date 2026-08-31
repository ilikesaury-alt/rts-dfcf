"""盘中操作纪律（2026-08-31）。

12 条操盘纪律的参数化实现。纯展示层，不参与评分/排序/落库。
两条主路径：
  - session_advice(now) → 全局时段提醒（规则 4/8/9 的时段部分）
  - stock_actions(cand, now, kline_bars, high_pct) → 个股操作标签

数据源（审查修复 2026-08-31，勿回退到日K/猜测值）：
  - 高开幅度：kline bars 今日 open vs 昨收；今日 bar 缺失 → None（未知，跳过判定），
    绝不用现价涨幅冒充高开（stale_kline 时会把「平开现涨」误判成高开）。
  - 日内高点：high_pct 参数（quote 的 high/昨收，api._quote_high_pct），
    缺失时回退分时摘要 dims["minute_day_high"]；两者皆无 → 跳过依赖项。
  - 早盘高点/量能趋势：分时摘要 dims["minute_am_high"] / dims["minute_vol_trend"]
    （intraday_fetch 第 4 相 analyze_minute_trend 产出）。

所有阈值集中在 config.TACTICS_*，模块本身无硬编码。
fail-open：数据缺失时返回空/跳过，不抛异常、不误标。
"""
from __future__ import annotations

import logging
from datetime import datetime, time
from typing import TYPE_CHECKING

from scanner.config import (
    ENABLE_INTRADAY_TACTICS,
    TACTICS_AM_NOT_OVER_HIGH_MINS,
    TACTICS_FLAT_OPEN_HIGH,
    TACTICS_FLAT_OPEN_LOW,
    TACTICS_HIGH_OPEN_REDUCE_PCT,
    TACTICS_LIMITUP_WEAK_START,
    TACTICS_LIMITUP_WINDOW_MINS,
    TACTICS_LOWBUY_END,
    TACTICS_LOWBUY_START,
    TACTICS_MIDDAY_WINDOW_END,
    TACTICS_MORNING_CRASH_PCT,
    TACTICS_MORNING_SPIKE_MINS,
    TACTICS_MORNING_SPIKE_START,
    TACTICS_SELL_WINDOW_END,
    TACTICS_SELL_WINDOW_START,
    TACTICS_SHRINK_VOL_RATIO,
    TACTICS_SPIKE_REDUCE_PCT,
    TACTICS_STEADY_RATIO_MIN,
    TACTICS_STEADY_RISE_MINS,
    TACTICS_STEADY_VOL_RATIO,
    TACTICS_TAIL_DIVE_PCT,
    TACTICS_TAIL_DIVE_START,
    TACTICS_TOP_WINDOW_1_END,
    TACTICS_TOP_WINDOW_2_END,
    TACTICS_TOP_WINDOW_2_START,
    now_beijing,
)
from scanner.trading_session import is_trading_time

if TYPE_CHECKING:
    from scanner.models import Candidate, KlineBar

logger = logging.getLogger(__name__)


def _time_to_min(t: time) -> int:
    """time → minutes since midnight。"""
    return t.hour * 60 + t.minute


# ── 全局时段提醒 ──

def session_advice(now: datetime | None = None) -> str | None:
    """当前时段的全局操盘纪律提醒。

    规则 4/8/9 的时段部分（不依赖个股数据）。
    返回提醒文字或 None（非交易时段/无提醒）。
    is_trading_time 守卫：00:00-09:30 的盘前扫描不输出任何提醒（审查修复）。
    """
    if not ENABLE_INTRADAY_TACTICS:
        return None
    now = now or now_beijing()
    if not is_trading_time(now):
        return None
    t = _time_to_min(now.time())
    # 规则 9：冲高见顶段
    if t <= TACTICS_TOP_WINDOW_1_END:
        return "⏰ 09:30-09:33 冲高见顶段，冲高勿追，可适当减仓"
    if TACTICS_TOP_WINDOW_2_START <= t <= TACTICS_TOP_WINDOW_2_END:
        return "⏰ 13:20-13:30 午后冲高见顶段，冲高勿追，可适当减仓"
    # 规则 8：卖出黄金窗口
    if TACTICS_SELL_WINDOW_START <= t <= TACTICS_SELL_WINDOW_END:
        return "⏰ 09:45-10:00 卖出黄金窗口，短线高点常现"
    # 规则 4：低吸窗口
    if TACTICS_LOWBUY_START <= t <= TACTICS_LOWBUY_END:
        return "⏰ 14:30-14:45 相对低吸窗口，可关注回调到位个股"
    return None


# ── 个股操作标签 ──

def stock_actions(
    cand: Candidate,
    now: datetime | None = None,
    kline_bars: list[KlineBar] | None = None,
    high_pct: float | None = None,
) -> list[str]:
    """对单只推荐股评估盘中操作纪律，返回操作标签列表。

    标签含义：
      ⬇减半   — 高开≥5% 但封不住板（规则 2）
      ⬆加仓   — 平开+稳步走高+量能同步（规则 3）
      🔻勿接   — 14:30后尾盘跳水（规则 5）
      💰落袋   — 14:00-14:30 涨停（规则 6；14:00 才封板本身即弱势画像，规则 10 并入此处）
      ⬇减仓   — 午盘冲高回落+缩量（规则 7）/ 早盘冲高（规则 1）
      ⬆加仓   — 早上大跌但无硬风险（规则 12）

    返回空列表 = 无操作建议（不干扰判断）。
    """
    if not ENABLE_INTRADAY_TACTICS:
        return []
    now = now or now_beijing()
    if not is_trading_time(now):
        return []
    t = _time_to_min(now.time())
    today_pct = cand.stock.percent
    dims = cand.kline.dimensions if cand.kline else {}

    actions: list[str] = []

    # 涨停/炸板状态（dimensions 由 enhancer 从涨停池写入；值可能是脏类型，强转防御）
    zhaban_raw = dims.get("zt_zhaban", 0)
    lianban_raw = dims.get("zt_lianban", 0)
    zhaban = int(zhaban_raw) if isinstance(zhaban_raw, (int, float)) else 0
    lianban = int(lianban_raw) if isinstance(lianban_raw, (int, float)) else 0
    is_limit_up = today_pct >= 9.8 or lianban >= 1

    # ── 规则 2：高开≥5% 封不住涨停 → 减半仓（最高优先级）──
    # 高开未知（今日 bar 缺失）→ 跳过，不猜（现价涨幅冒充高开会误杀平开现涨票）
    gap_up_pct = _gap_up_pct(cand, kline_bars, now)
    if gap_up_pct is not None and gap_up_pct >= TACTICS_HIGH_OPEN_REDUCE_PCT and (not is_limit_up or zhaban > 0):
        actions.append("⬇减半")
        return actions  # 高开减半优先级最高

    # ── 规则 6：14:00-14:30 涨停 → 落袋清仓 ──
    # 规则 10 并入：14:00 后才封板 = 实力偏弱的封板画像，正是该落袋而非赌连板的时刻。
    if (TACTICS_LIMITUP_WEAK_START <= t <= TACTICS_LIMITUP_WEAK_START + TACTICS_LIMITUP_WINDOW_MINS
            and is_limit_up and zhaban == 0):
        actions.append("💰落袋")

    # ── 规则 5：14:30后尾盘跳水 → 不抄底，次日观察 20 日线再决定 ──
    # 日内高点：quote high_pct（真实日内高点）→ 分时摘要回退 → 均无则跳过
    if t >= TACTICS_TAIL_DIVE_START:
        day_high = high_pct if high_pct is not None else _dims_float(dims, "minute_day_high")
        if day_high is not None and day_high - today_pct >= TACTICS_TAIL_DIVE_PCT:
            actions.append("🔻勿接")

    # ── 规则 3：平开+稳步走高+量能同步 → 加仓（先于规则 1：稳步上行的买点信号
    #     优先于早盘冲高的普适谨慎，两者语义相反不应被减仓覆盖）──
    steady = _dims_float(dims, "minute_steady_rise")
    vol_trend = _dims_float(dims, "minute_vol_trend")
    if (not actions
            and gap_up_pct is not None
            and TACTICS_FLAT_OPEN_LOW <= gap_up_pct <= TACTICS_FLAT_OPEN_HIGH
            and TACTICS_MORNING_SPIKE_START + TACTICS_STEADY_RISE_MINS <= t <= TACTICS_MIDDAY_WINDOW_END
            and steady is not None and steady >= TACTICS_STEADY_RATIO_MIN
            and vol_trend is not None and vol_trend >= TACTICS_STEADY_VOL_RATIO):
        actions.append("⬆加仓")

    # ── 规则 7：午盘冲高回落（不过早盘高点）+缩量 → 锁利 ──
    # 早盘高点用分时摘要（minute_am_high）；缺失 → 跳过（不拿日K历史冒充）。
    # 缩量：分时近段量能趋势 <1 优先，回退 K 线量比 < TACTICS_SHRINK_VOL_RATIO。
    if TACTICS_AM_NOT_OVER_HIGH_MINS <= t <= TACTICS_MIDDAY_WINDOW_END:
        am_high = _dims_float(dims, "minute_am_high")
        if am_high is not None and am_high > 0 and today_pct < am_high:
            shrink = vol_trend < 1.0 if vol_trend is not None else (
                cand.kline.volume_ratio < TACTICS_SHRINK_VOL_RATIO if cand.kline else False
            )
            if shrink:
                actions.append("⬇减仓")

    # ── 规则 1：早盘冲高（09:30-10:00）减仓 ──
    # not actions 守卫：与规则 3/12 的加仓信号互斥（稳步走高/大跌加仓场景不叠加普适减仓提醒）
    if (not actions
            and TACTICS_MORNING_SPIKE_START <= t <= TACTICS_MORNING_SPIKE_START + TACTICS_MORNING_SPIKE_MINS
            and today_pct >= TACTICS_SPIKE_REDUCE_PCT and not is_limit_up):
        actions.append("⬇减仓")

    # ── 规则 12：早上大跌（无硬风险）可加仓；早上大涨已在规则 1 覆盖 ──
    if (not actions
            and TACTICS_MORNING_SPIKE_START <= t <= TACTICS_MORNING_SPIKE_START + TACTICS_MORNING_SPIKE_MINS
            and today_pct <= TACTICS_MORNING_CRASH_PCT
            and "趋势破位" not in cand.risk_flags
            and "主力出货" not in cand.risk_flags):
        actions.append("⬆加仓")

    return actions


# ── 内部工具 ──


def _dims_float(dims: dict, key: str) -> float | None:
    """从 dimensions 取 float 值；缺失/脏值/0 语义由调用方处理。"""
    v = dims.get(key)
    if isinstance(v, (int, float)):
        return float(v)
    return None


def _gap_up_pct(cand: Candidate, kline_bars: list[KlineBar] | None, now: datetime) -> float | None:
    """今日高开幅度（%）；未知时返回 None（绝不拿现价涨幅冒充高开）。

    - 无 bars / 今日 bar 缺失（stale_kline 补拉失败）→ None
    - 今日 bar 的 open ≤ 0（脏数据）→ None
    """
    if not kline_bars:
        return None
    today_str = now.date().isoformat()
    today_bar = None
    yesterday_close = None
    for k in reversed(kline_bars):
        if today_bar is None and k.get("date") == today_str:
            today_bar = k
        elif yesterday_close is None and k.get("date") != today_str:
            yesterday_close = k.get("close", 0)
        if today_bar and yesterday_close:
            break
    if today_bar is None or not yesterday_close or yesterday_close <= 0:
        return None
    ref_price = today_bar.get("open", 0)
    if ref_price <= 0:
        return None
    return (ref_price - yesterday_close) / yesterday_close * 100
