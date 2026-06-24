from dataclasses import dataclass, field


@dataclass
class ChainTrend:
    chain_name: str
    phase: str
    score: int
    signals: list[str]
    bottleneck_activated: bool
    stock_count: int = 0
    avg_rank_change: float = 0.0


@dataclass
class ChokepointCandidate:
    symbol: str
    name: str
    chain_name: str
    node_name: str
    is_bottleneck: bool
    chain_phase: str
    score: int
    chain_trend_score: int
    bottleneck_bonus: int
    tech_score: int
    signals: list[str]
    percent: float = 0.0
    current: float = 0.0
    rank: int = 0
    rank_change: int = 0


@dataclass
class IndustryScanSession:
    chain_scan_history: list[dict] = field(default_factory=list)
    max_history_rounds: int = 20


CHAIN_PHASE_NAMES = {
    "erupting": "爆发期",
    "growing": "成长期",
    "forming": "形成期",
    "fading": "消退期",
    "dormant": "潜伏期",
}
