from dataclasses import dataclass, field


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
