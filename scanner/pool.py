"""池层（重构 Phase 2）：榜上全量 CYB → PoolRow 技术特征。

职责：把通过市值/价格准入的榜上 GEM 全量票（orchestrator 的 gem_stocks_filtered）
转成带技术特征的 PoolRow，供 danger.py 排雷与后续 matcher 低吸匹配消费。
不评分、不分类、不影响 v1 推荐——本模块只做「全量特征快照」。

特征来源：
  - K 线（indicators.compute_ma）：bias20（偏离 MA20）、acc5（5 日累计涨幅）
  - 榜单排名轨迹：rank_trend = 上一交易日排名 − 今日排名（>0 上升，<0 下降），
    历史排名从 appearances 表取（进程级 RankTracker 仅存内存最后 5 轮，无跨日可靠性）

fail-open：单票 K 线缺失/脏数据只跳过该票特征（bias20/acc5 置 None），不中断整轮。
"""

from dataclasses import dataclass

from scanner.indicators import compute_ma


@dataclass
class PoolRow:
    symbol: str
    name: str
    percent: float
    rank: int
    prev_rank: int | None
    rank_trend: int  # prev_rank - rank（>0 上升，<0 下降）
    bias20: float | None  # (close - MA20)/MA20*100
    acc5: float | None  # 5 日累计涨幅（排除今日外推）
    on_board: bool
    market_cap: float | None


def _closes_upto(klines_sym: list[dict], today: str) -> list[float]:
    """取截至今日（含）的收盘价序列，按日期升序，跳过脏值（close<=0/非数）。"""
    rows: list[tuple[str, float]] = []
    for k in klines_sym:
        d = k.get("date")
        if not d or d > today:
            continue
        c = k.get("close")
        if isinstance(c, (int, float)) and c > 0:
            rows.append((d, float(c)))
    rows.sort(key=lambda x: x[0])
    return [c for _, c in rows]


def compute_bias20(closes: list[float]) -> float | None:
    """偏离 MA20 百分比；不足 20 根 K 线返回 None。"""
    if len(closes) < 20:
        return None
    ma20 = compute_ma(closes, 20)
    if ma20 is None or ma20 <= 0:
        return None
    return (closes[-1] - ma20) / ma20 * 100.0


def compute_acc5(closes: list[float]) -> float | None:
    """5 日累计涨幅（今日收盘 vs 6 根 K 线前收盘）；不足 6 根返回 None。"""
    if len(closes) < 6:
        return None
    base = closes[-6]
    if base <= 0:
        return None
    return (closes[-1] - base) / base * 100.0


def build_pool(gem_stocks_filtered, klines: dict, today: str, prev_ranks: dict) -> list[PoolRow]:
    """把榜上全量 GEM（已通过市值/价格准入）转成 PoolRow 列表。

    gem_stocks_filtered: list[StockInfo]，全部在榜。
    klines: symbol -> list[KlineBar]。
    prev_ranks: symbol -> 上一交易日排名（来自 appearances，可为空）。
    """
    rows: list[PoolRow] = []
    for s in gem_stocks_filtered:
        kl = klines.get(s.symbol) or []
        closes = _closes_upto(kl, today)
        bias20 = compute_bias20(closes)
        acc5 = compute_acc5(closes)
        prev = prev_ranks.get(s.symbol)
        rank_trend = (prev - int(s.rank)) if prev is not None else 0
        rows.append(
            PoolRow(
                symbol=s.symbol,
                name=s.name,
                percent=float(getattr(s, "percent", 0.0) or 0.0),
                rank=int(s.rank),
                prev_rank=prev,
                rank_trend=rank_trend,
                bias20=bias20,
                acc5=acc5,
                on_board=True,
                market_cap=getattr(s, "market_cap", None),
            )
        )
    return rows
