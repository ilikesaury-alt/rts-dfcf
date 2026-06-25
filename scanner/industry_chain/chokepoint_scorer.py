from scanner.indicators import compute_macd, compute_rsi
from scanner.industry_chain.chains import match_chains
from scanner.industry_chain.models import ChokepointCandidate, ChainTrend
from scanner.config import (
    BOTTLENECK_BONUS,
    CHAIN_ERUPTING_BONUS,
    CHAIN_FADING_PENALTY,
    CHAIN_FORMING_BONUS,
    CHAIN_GROWING_BONUS,
    KLINE_FETCH_DAYS,
    KLINE_MIN_LENGTH,
    MAX_NEW_FACE_TODAY_PCT,
    TECH_ACCUM_HEALTHY,
    TECH_ACCUM_OVERHEAT,
    TECH_MA_BEAR,
    TECH_MA_BULL,
    TECH_MACD_GOLDEN,
    TECH_POSITIVE_RETURN_BONUS,
    TECH_RSI_OVERSOLD,
    TECH_RSI_STRONG,
    TECH_VOL_CONFIRM,
)


def _phase_to_score(phase: str) -> int:
    return {
        "erupting": CHAIN_ERUPTING_BONUS,
        "growing": CHAIN_GROWING_BONUS,
        "forming": CHAIN_FORMING_BONUS,
        "fading": CHAIN_FADING_PENALTY,
        "dormant": 0,
    }.get(phase, 0)


def _score_technical(closes: list[float], kline: list[dict], percent: float) -> tuple[int, list[str]]:
    score = 0
    signals = []

    if len(closes) < 5:
        return score, signals

    ma5 = sum(closes[-5:]) / 5
    if len(closes) >= 10:
        ma10 = sum(closes[-10:]) / 10
        if ma5 > ma10:
            score += TECH_MA_BULL
            signals.append("MA多头排列")
        else:
            score += TECH_MA_BEAR
            signals.append("MA空头排列")

    vol_ratio = _calc_vol_ratio(kline)
    if vol_ratio is not None and vol_ratio > 1.3:
        score += TECH_VOL_CONFIRM
        signals.append(f"放量{vol_ratio:.1f}倍")

    rsi = compute_rsi(closes, period=6)
    if rsi is not None:
        if 40 <= rsi <= 70:
            score += TECH_RSI_STRONG
            signals.append(f"RSI{rsi:.0f}强势区")
        elif rsi < 30:
            score += TECH_RSI_OVERSOLD
            signals.append(f"RSI{rsi:.0f}超卖")

    macd = compute_macd(closes)
    if macd is not None:
        if macd["histogram"] > 0 and macd["histogram_prev"] <= 0:
            score += TECH_MACD_GOLDEN
            signals.append("MACD金叉")

    if len(closes) >= 5:
        accum = sum(k["percent"] for k in kline[-5:] if "percent" in k)
        if 0 < accum < 15:
            score += TECH_ACCUM_HEALTHY
            signals.append(f"5日涨幅{accum:.1f}%合理")
        elif accum >= 25:
            score += TECH_ACCUM_OVERHEAT
            signals.append(f"5日涨幅{accum:.1f}%过热")

    if 0 < percent <= MAX_NEW_FACE_TODAY_PCT:
        score += TECH_POSITIVE_RETURN_BONUS
        signals.append(f"今日涨幅{percent:.1f}%")

    return score, signals


def _calc_vol_ratio(kline: list[dict]) -> float | None:
    if not kline or len(kline) < 2:
        return None
    today_vol = kline[-1].get("volume", 0)
    vol_window = kline[-11:-1] if len(kline) >= 11 else kline[:-1]
    avg_vol = sum(k.get("volume", 0) for k in vol_window) / max(len(vol_window), 1)
    return today_vol / avg_vol if avg_vol > 0 else 1.0


def score_chokepoint_stocks(
    chain_trends: dict[str, ChainTrend],
    gem_stocks: list,
    klines: dict[str, list[dict] | None],
) -> list[ChokepointCandidate]:
    from scanner.industry_chain.trend_judge import get_active_chains

    active_chains = get_active_chains(chain_trends, min_phase="forming")
    if not active_chains:
        return []

    candidates: list[ChokepointCandidate] = []

    for stock in gem_stocks:
        sym = stock["symbol"] if isinstance(stock, dict) else stock.symbol
        sname = stock["name"] if isinstance(stock, dict) else stock.name
        spct = stock["percent"] if isinstance(stock, dict) else stock.percent
        scur = stock["current"] if isinstance(stock, dict) else stock.current
        srank = stock["rank"] if isinstance(stock, dict) else stock.rank
        srchg = stock["rank_change"] if isinstance(stock, dict) else stock.rank_change

        matches = match_chains(sname)
        if not matches:
            continue

        chain_name, node_name, bn, _ = matches[0]
        if chain_name not in active_chains:
            continue

        trend = chain_trends[chain_name]
        chain_score = _phase_to_score(trend.phase)

        bottleneck_bonus = BOTTLENECK_BONUS if bn else 0

        kline = klines.get(sym)
        tech_score = 0
        tech_signals = []
        if kline and len(kline) >= 5:
            closes = [k["close"] for k in kline]
            tech_score, tech_signals = _score_technical(closes, kline, spct)

        signals = list(trend.signals)
        if bn:
            signals.append(f"瓶颈环节({node_name})")
        signals.extend(tech_signals)

        total_score = chain_score + bottleneck_bonus + tech_score

        if total_score > 0:
            candidates.append(ChokepointCandidate(
                symbol=sym,
                name=sname,
                chain_name=chain_name,
                node_name=node_name,
                is_bottleneck=bn,
                chain_phase=trend.phase,
                score=total_score,
                chain_trend_score=chain_score,
                bottleneck_bonus=bottleneck_bonus,
                tech_score=tech_score,
                signals=signals,
                percent=spct,
                current=scur,
                rank=srank,
                rank_change=srchg,
            ))

    candidates.sort(key=lambda c: -c.score)
    return candidates
