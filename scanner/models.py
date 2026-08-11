import math
from dataclasses import dataclass, field
from typing import NotRequired, TypedDict


# ── K 线 bar 数据契约（2026-08-11 重构 P0-1）──
# 此前 kline 全程裸 dict，键名（"date"/"close"/...）在 analysis/validator/comeback/
# orchestrator 数十处硬编码，且 API/DB 脏数据（字符串/None/NaN/close<=0）靠各处
# `or 0.0` / `_safe_float` 散落防御（同一缺陷多处爆）。改为：入口统一 make_kline_bar()
# 强转+校验，内部全程带类型标注的 KlineBar；TypedDict 保持 dict 行为（k["date"] 访问、
# merge/sort 不变），仅把「值域合法」收敛到生产端单点，零运行时行为变化。
class KlineBar(TypedDict):
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    percent: float
    timestamp: NotRequired[int | float]  # API 源有（ms）；DB 读回无（SELECT 不含该列）


def _bar_float(v, default: float = 0.0) -> float:
    """数值强转：None/NaN/inf/不可解析字符串/空 → default（与 api._num / enhancer._safe_float 同族）。"""
    try:
        f = float(v)
        return default if not math.isfinite(f) else f  # NaN/inf → default
    except (TypeError, ValueError):
        return default


def make_kline_bar(raw: dict) -> KlineBar | None:
    """把外部输入的 kline bar dict 规范化为 KlineBar；无效 bar 返回 None（调用方跳过）。

    唯一新增的运行时约束（与既有消费端行为一致，只是从散落防御集中到入口）：
      1. date 必须是非空字符串（否则下游 date.fromisoformat 抛 ValueError 拖垮整轮）；
      2. close 必须能解析为正数（close<=0 的停牌/脏 bar 直接剔除——
         get_cached_kline 原已有此过滤，fetch_kline 原靠 analyze_* 兜底，现在统一）。
    open/high/low/volume/percent 缺失或脏 → 0（与 api._num 行为一致，不新增过滤）。
    """
    if not isinstance(raw, dict):
        return None
    date = raw.get("date")
    if not isinstance(date, str) or not date:
        return None
    close = _bar_float(raw.get("close"))
    if close <= 0:
        return None
    return {
        "date": date,
        "open": _bar_float(raw.get("open")),
        "high": _bar_float(raw.get("high")),
        "low": _bar_float(raw.get("low")),
        "close": close,
        "volume": _bar_float(raw.get("volume")),
        "percent": _bar_float(raw.get("percent")),
    }


@dataclass
class StockInfo:
    symbol: str
    name: str
    code: str
    percent: float
    current: float
    value: float
    rank_change: int
    rank: int
    source_tag: str = "xueqiu"
    market_cap: float = 0.0  # 流通市值（亿元），由 orchestrator 富集


@dataclass
class KlineSummary:
    trend: str
    accumulated_pct: float
    volume_ratio: float
    bottom_confirmed: bool
    score: int
    dimensions: dict = field(default_factory=dict)
    avg_volume: float = 0.0


@dataclass
class Candidate:
    stock: StockInfo
    category: str
    score: int
    reason: str
    kline: KlineSummary | None
    sector: str = ""
    sector_bonus: int = 0
    live_vol_bonus: int = 0
    intraday_score: float = 0.0
    first_seen: str = ""
    last_seen: str = ""
    history_pct: list[float] = field(default_factory=list)
    market_cap: float = 0.0
    circ_market_cap: float = 0.0
    first_today_bonus: int = 0
    turnover_bonus: int = 0
    first_breakout_bonus: int = 0
    gap_up_bonus: int = 0
    time_bonus: int = 0
    market_sentiment_bonus: int = 0
    rps_bonus: int = 0
    market_cap_bonus: int = 0
    list_momentum_bonus: int = 0
    fund_flow_bonus: int = 0      # 主力资金净流入加分（行情增强，enhancer）
    zt_lianban_bonus: int = 0     # 涨停连板加分/追高降权（行情增强，enhancer）
    is_stale: bool = False
    stale_since: str = ""
    risk_flags: list[str] = field(default_factory=list)  # 复合风险标签（超买/出货/破位等）
    prominence_labels: list[str] = field(default_factory=list)  # 辨识度标签（反复上榜等）
    hist_loss_rate: float | None = None  # 历史大跌率（近90天推荐中次日<=-5%占比），None=样本不足
    driving_concept: str = ""  # 当前推动概念（仅展示，不参与打分）
    off_list: bool = False    # 掉榜跟踪候选（回马枪）：不在当次热榜上，无热榜背书
    comeback_variant: str = ""  # 回马枪变体："反转" / "回踩"（展示与持久化区分）
    sector_capped: bool = False  # 同板块上限（2026-08-12）：该 short_term 候选本轮被板块限流。
    # 仍照常落库（保留回测全样本），但综合排序/飞书等对外展示隐藏；双挂票（如新面孔+超短）豁免。
