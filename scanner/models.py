import json
from dataclasses import dataclass, field
from typing import Any, Dict, NotRequired, TypedDict

from scanner.utils import to_float


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


class Dimensions(TypedDict, total=False):
    """recommendations.score_breakdown JSON 解析后的统一维度字典（P1-7 单源契约）。

    字段由 enhancer._record_dimensions / orchestrator 写入，跨「候选行（最新扫描
    dims）」与「掉榜/重启行（DB score_breakdown 落库）」共用同一套键。
    本 TypedDict 仅作文档化契约，实际取值按需用 dict.get() 读取（值类型 int/float/
    str/bool 混合，故未逐键标注类型）。

    🎯 分型判定的关键维度（ranking._entry_dims 统一消费）:
      - st_weak_to_strong / v_st_weak: short_term 弱转强（非超买时次日最强单信号）
      - st_overbought_flag / mo_overbought_flag / v_st_overbought / v_mo_overbought /
        v_nf_overbought: 超买死亡信号
      - fund_flow_main_pct: 主力净占比（%）
      - v_st_sector / v_pb_sector / v_nf_sector: 板块共振（配合板块规模 count）
      - run / pullback / flow_pct: 核心方向低吸（core_dip）展示用
      - today_pct: 推荐时刻涨幅（甜蜜带判定）
    热度放大器组件（portfolio_backtest 去热度分用）: heat_zhishu / heat_eastmoney /
      heat_xueqiu / heat_concept（与 enhancer.HEAT_AMPLIFIER_BONUS_ATTRS 对应）。
    """


class RecommendationRow(TypedDict, total=False):
    """get_today_recommendations 返回行（P1-7 契约化）。

    score_breakdown 统一经 parse_score_breakdown 解析为 Dimensions；_candidate 为
    实时候选对象（掉榜/重启行无，为 None）。
    """
    symbol: str
    name: str
    category: str
    score: float | int
    date: str
    trend: str | None
    time: str
    percent: float
    concept: str
    accumulated_pct: float | None
    score_breakdown: Dimensions
    _candidate: Any  # scanner.models.Candidate | None（避免双向 import）


def parse_score_breakdown(sb: str | dict | None) -> Dict[str, Any]:
    """解析 recommendations.score_breakdown（JSON 字符串或已解析 dict）为 dict。

    P1-7 收敛单源：backtest / nextday_attribution / portfolio_backtest / display /
    ranking 此前各自 json.loads + try/except，默认值与异常分支不一致易漂移（曾导致
    掉榜行 🎯 分型判错）。统一契约：
      - None / "" / 非法 JSON / 非 dict  → 返回 {}（消费方按「无维度」处理，不报错）
      - 已是 dict                  → 原样返回（database.get_today_recommendations
        已预解析，ranking 直接消费，不再二次解析）
    运行时不抛异常、不静默返回 None（后者会让 .get() 抛 AttributeError）。
    """
    if sb is None or sb == "":
        return {}
    if isinstance(sb, dict):
        return sb
    if isinstance(sb, str):
        try:
            parsed = json.loads(sb)
        except (json.JSONDecodeError, TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _bar_float(v, default: float = 0.0) -> float:
    """数值强转：None/NaN/inf/不可解析字符串/空 → default（统一走 utils.to_float）。"""
    return to_float(v, default)


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
    turnover_rate: float = 0.0  # 换手率（%），leaderboard 原样带入，供停牌/僵尸股识别


@dataclass
class KlineSummary:
    trend: str
    accumulated_pct: float
    volume_ratio: float
    bottom_confirmed: bool
    score: int
    # 维度值混合 int/float/str（validator 的 detail 字符串也写入），用 object 显式表达。
    dimensions: dict[str, object] = field(default_factory=dict)
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
    driving_concept: str = ""  # 当前推动概念（仅展示，不参与打分）
    off_list: bool = False    # 掉榜跟踪候选（回马枪）：不在当次热榜上，无热榜背书
    comeback_variant: str = ""  # 回马枪变体："反转" / "回踩"（展示与持久化区分）
    stale_kline: bool = False  # 评分所用 K 线缺今日 bar（补拉失败旧缓存兜底）——审计用（2026-08-14）
    excluded_reason: str = ""  # 硬过滤命中标签串（审计用，2026-08-20）：excluded=1 时记录
                               # 命中哪些 RISK_FLAGS_HARD_FILTER 标签，消除"无依据误杀"盲点


@dataclass
class ScanResult:
    """单轮扫描的输出汇总（替代 scan_with_raw 的 9 元组返回）。

    new_faces 已按 _new_face_sort_key 排序，其余各桶按 score 降序。
    today_pool 为本轮候选池快照（symbol → Candidate，含掉榜 stale 条目），
    供展示层读取实时候选数据，避免 display 直接访问 orchestrator 内部全局。
    """
    new_faces: list[Candidate] = field(default_factory=list)
    momentum: list[Candidate] = field(default_factory=list)
    rebound: list[Candidate] = field(default_factory=list)
    short_term: list[Candidate] = field(default_factory=list)
    comeback: list[Candidate] = field(default_factory=list)
    gem_stocks: list[StockInfo] = field(default_factory=list)
    filtered_large_cap: int = 0
    current_quotes: dict[str, dict] = field(default_factory=dict)
    today_pool: dict[str, Candidate] = field(default_factory=dict)
