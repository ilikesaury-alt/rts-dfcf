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
    rank_trend_bonus: int = 0
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
    is_stale: bool = False
    stale_since: str = ""
