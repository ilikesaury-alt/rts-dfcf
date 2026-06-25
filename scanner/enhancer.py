from datetime import datetime

from scanner.config import (
    now_beijing,
    EARLY_BONUS,
    EARLY_TRADE_CUTOFF,
    LATE_BONUS,
    LATE_TRADE_START,
    LIST_STREAK_BONUS_2,
    LIST_STREAK_BONUS_3,
    LIST_STREAK_BONUS_5,
    LIVE_VOL_BONUS,
    LIVE_VOL_RATIO_THRESHOLD,
    MARKET_ENV_STRONG,
    MARKET_ENV_WEAK,
    MARKET_STRONG_THRESHOLD,
    MARKET_WEAK_THRESHOLD,
    SECTOR_CLUSTER_BONUS_2,
    SECTOR_CLUSTER_BONUS_3,
    SECTOR_CLUSTER_BONUS_4,
    SECTOR_CLUSTER_BONUS_5,
    TOP20_EXTRA,
    TOP40_ADVANCE_PER_10,
    TOP40_BONUS,
    TOP40_THRESHOLD,
    TURNOVER_BONUS_MODERATE,
    TURNOVER_BONUS_PENALTY,
    TURNOVER_BONUS_HEALTHY,
    TURNOVER_HIGH,
    TURNOVER_LOW,
    TURNOVER_MEDIUM,
)
from scanner.models import Candidate
from scanner.rank_trend import rank_streak_score, rank_trajectory_score
from scanner.sector import classify_sector


def apply_all_bonuses(
    candidates: list[Candidate],
    gem_stocks: list,
    intraday_scores: dict[str, float | None],
    opening_scores: dict[str, float | None],
    live_volumes: dict[str, float | None],
    market_caps: dict[str, dict],
    clusters: dict[str, list[str]],
    market_idx_pct: float | None,
    time_bonus: int,
    sentiment_info: dict = None,
    rps_scores: dict[str, int] = None,
    list_streaks: dict[str, int] = None,
    conn=None,
):
    for c in candidates:
        _apply_sector_bonus(c, clusters)
        _apply_intraday_bonus(c, intraday_scores)
        _apply_live_vol_bonus(c, live_volumes)
        _apply_turnover_bonus(c, market_caps)
        _apply_sentiment_bonus(c, sentiment_info)
        _apply_rps_bonus(c, rps_scores)
        _apply_list_momentum_bonus(c, list_streaks, conn)
        c.rank_trend_bonus = rank_streak_score(c.stock.symbol)
        c.time_bonus = time_bonus
        _apply_gap_up_bonus(c)
        _record_dimensions(c, market_idx_pct, opening_scores)


def _apply_sector_bonus(c: Candidate, clusters: dict[str, list[str]]):
    sec = classify_sector(c.stock.name)
    c.sector = sec
    if sec != "其他":
        cluster_count = len(clusters.get(sec, []))
        if cluster_count >= 5:
            c.sector_bonus = SECTOR_CLUSTER_BONUS_5
        elif cluster_count >= 4:
            c.sector_bonus = SECTOR_CLUSTER_BONUS_4
        elif cluster_count >= 3:
            c.sector_bonus = SECTOR_CLUSTER_BONUS_3
        elif cluster_count >= 2:
            c.sector_bonus = SECTOR_CLUSTER_BONUS_2


def _apply_intraday_bonus(c: Candidate, intraday_scores: dict[str, float | None]):
    intra = intraday_scores.get(c.stock.symbol)
    if intra is not None:
        c.intraday_score = intra


def _apply_live_vol_bonus(c: Candidate, live_volumes: dict[str, float | None]):
    live_vol = live_volumes.get(c.stock.symbol)
    if live_vol is not None and c.kline and c.kline.avg_volume > 0:
        live_vol_ratio = live_vol / c.kline.avg_volume
        if live_vol_ratio > LIVE_VOL_RATIO_THRESHOLD:
            c.live_vol_bonus = LIVE_VOL_BONUS


def _apply_turnover_bonus(c: Candidate, market_caps: dict[str, dict]):
    if c.market_cap > 0:
        tr = market_caps.get(c.stock.symbol, {}).get("turnover_rate")
        if tr is not None:
            if tr > TURNOVER_HIGH:
                c.turnover_bonus = TURNOVER_BONUS_PENALTY
            elif tr > TURNOVER_MEDIUM:
                c.turnover_bonus = TURNOVER_BONUS_MODERATE
            elif tr > TURNOVER_LOW:
                c.turnover_bonus = TURNOVER_BONUS_HEALTHY


def _apply_sentiment_bonus(c: Candidate, sentiment_info: dict):
    if sentiment_info:
        c.market_sentiment_bonus = sentiment_info.get("bonus", 0)


def _apply_rps_bonus(c: Candidate, rps_scores: dict[str, int]):
    if rps_scores:
        c.rps_bonus = rps_scores.get(c.stock.symbol, 0)


def _apply_gap_up_bonus(c: Candidate):
    if c.kline and c.kline.dimensions:
        gap_key = "new_face_gap_up" if c.category in ("new_face", "known_new_face") else "momentum_gap_up"
        c.gap_up_bonus = c.kline.dimensions.get(gap_key, 0)


def _apply_list_momentum_bonus(c: Candidate, list_streaks: dict[str, int] = None, conn=None):
    intraday_streak = (list_streaks or {}).get(c.stock.symbol, 0)
    if conn:
        from scanner.database import get_consecutive_appearance_days
        cross_days = get_consecutive_appearance_days(conn, c.stock.symbol)
    else:
        cross_days = 0
    streak = max(cross_days, intraday_streak)
    traj = rank_trajectory_score(c.stock.symbol)
    rank = c.stock.rank
    bonus = 0
    if streak >= 5:
        bonus += LIST_STREAK_BONUS_5
    elif streak >= 3:
        bonus += LIST_STREAK_BONUS_3
    elif streak >= 2:
        bonus += LIST_STREAK_BONUS_2
    bonus += traj
    if rank <= TOP40_THRESHOLD:
        bonus += TOP40_BONUS
        advance = (TOP40_THRESHOLD - rank) // 10
        bonus += advance * TOP40_ADVANCE_PER_10
        if rank <= 20:
            bonus += TOP20_EXTRA
    c.list_momentum_bonus = bonus


def _record_dimensions(
    c: Candidate,
    market_idx_pct: float | None,
    opening_scores: dict[str, float | None],
):
    if not c.kline or c.kline.dimensions is None:
        return
    c.kline.dimensions["rank_trend_bonus"] = c.rank_trend_bonus
    c.kline.dimensions["sector_bonus"] = c.sector_bonus
    c.kline.dimensions["live_vol_bonus"] = c.live_vol_bonus
    c.kline.dimensions["intraday_score"] = round(c.intraday_score, 1)
    if c.market_sentiment_bonus != 0:
        c.kline.dimensions["market_sentiment_bonus"] = c.market_sentiment_bonus
    if c.rps_bonus != 0:
        c.kline.dimensions["rps_bonus"] = c.rps_bonus
    opening = opening_scores.get(c.stock.symbol)
    if opening is not None:
        c.kline.dimensions["opening_score"] = round(opening, 1)
    if c.first_today_bonus:
        c.kline.dimensions["first_today_bonus"] = c.first_today_bonus
    if c.first_breakout_bonus:
        c.kline.dimensions["first_breakout_bonus"] = c.first_breakout_bonus
    if market_idx_pct is not None:
        if market_idx_pct > MARKET_STRONG_THRESHOLD:
            c.kline.dimensions["market_env_bonus"] = MARKET_ENV_STRONG
        elif market_idx_pct < MARKET_WEAK_THRESHOLD:
            c.kline.dimensions["market_env_bonus"] = MARKET_ENV_WEAK
    if c.turnover_bonus:
        c.kline.dimensions["turnover_bonus"] = c.turnover_bonus
    if c.time_bonus:
        c.kline.dimensions["time_bonus"] = c.time_bonus
    if c.list_momentum_bonus:
        c.kline.dimensions["list_momentum_bonus"] = c.list_momentum_bonus


def compute_time_bonus(now: datetime | None = None) -> int:
    now = now or now_beijing()
    now_minutes = now.hour * 60 + now.minute
    if now_minutes < EARLY_TRADE_CUTOFF:
        return EARLY_BONUS
    if now_minutes >= LATE_TRADE_START:
        return LATE_BONUS
    return 0


def compute_market_env_bonus(market_idx_pct: float | None) -> int:
    if market_idx_pct is None:
        return 0
    if market_idx_pct > MARKET_STRONG_THRESHOLD:
        return MARKET_ENV_STRONG
    if market_idx_pct < MARKET_WEAK_THRESHOLD:
        return MARKET_ENV_WEAK
    return 0


def accumulate_final_score(c: Candidate, market_env_bonus: int, opening_scores: dict[str, float | None]) -> int:
    opening = opening_scores.get(c.stock.symbol)
    opening_bonus = int(round(opening)) if opening is not None else 0
    intraday_bonus = int(round(c.intraday_score))
    total = (c.rank_trend_bonus + c.sector_bonus + c.live_vol_bonus
             + c.first_today_bonus + c.first_breakout_bonus
             + market_env_bonus + c.turnover_bonus + c.time_bonus
             + c.market_sentiment_bonus + c.rps_bonus
             + c.list_momentum_bonus + opening_bonus
             + intraday_bonus)
    return total
