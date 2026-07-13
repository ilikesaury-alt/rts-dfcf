import json
import logging
import sqlite3
from datetime import date, timedelta
from typing import Any

from scanner.config import (
    CHAIN_CONCENTRATION_HOT,
    CHAIN_CONCENTRATION_LOW,
    CHAIN_CONCENTRATION_WARM,
    CHAIN_PERSISTENCE_FADING,
    now_beijing,
    CHAIN_PERSISTENCE_MIN,
)
from scanner.industry_chain.chains import match_chains
from scanner.trading_session import is_trading_day
from scanner.industry_chain.models import ChainTrend, IndustryScanSession

logger = logging.getLogger(__name__)


def judge_chain_trends(
    raw_items: list[dict],
    conn: sqlite3.Connection,
    session: IndustryScanSession,
    scan_id: str,
) -> dict[str, ChainTrend]:
    now = now_beijing().isoformat()

    chain_stocks: dict[str, list[dict]] = {}
    chain_rank_changes: dict[str, list[int]] = {}
    chain_newcomers: dict[str, list[str]] = {}

    seen_symbols = set()

    for item in raw_items:
        name = item.get("name", "")
        symbol = item.get("symbol", "")
        seen_symbols.add(symbol)
        matches = match_chains(name)
        if not matches:
            continue

        for chain_name in matches:
            if chain_name not in chain_stocks:
                chain_stocks[chain_name] = []
                chain_rank_changes[chain_name] = []
                chain_newcomers[chain_name] = []

            chain_stocks[chain_name].append(item)
            chain_rank_changes[chain_name].append(abs(item.get("rank_change") or 0))

            is_new = _is_newcomer(symbol, conn)
            if is_new:
                chain_newcomers[chain_name].append(symbol)

    chain_trend_results: dict[str, ChainTrend] = {}

    for chain_name, stocks in chain_stocks.items():
        if not stocks:
            continue

        concentration = len(stocks)
        newbie_rate = len(chain_newcomers.get(chain_name, [])) / max(concentration, 1)
        avg_rank_change = (
            sum(chain_rank_changes.get(chain_name, [])) / max(len(chain_rank_changes.get(chain_name, [])), 1)
        )
        persistence = _calc_persistence(chain_name, conn)

        signals = []
        signals.append(f"链集聚度{concentration}")
        if concentration >= CHAIN_CONCENTRATION_HOT:
            signals.append("爆发")
        elif concentration >= CHAIN_CONCENTRATION_WARM:
            signals.append("活跃")
        elif concentration >= CHAIN_CONCENTRATION_LOW:
            signals.append("形成")
        if newbie_rate > 0.3:
            signals.append(f"新人率{newbie_rate:.0%}")
        if persistence >= CHAIN_PERSISTENCE_FADING:
            signals.append(f"持续{persistence}轮")
        if avg_rank_change > 50:
            signals.append(f"排名势能{avg_rank_change:.0f}")

        phase, score = _classify_phase(
            concentration=concentration,
            persistence=persistence,
            newbie_rate=newbie_rate,
            avg_rank_change=avg_rank_change,
        )

        chain_trend_results[chain_name] = ChainTrend(
            chain_name=chain_name,
            phase=phase,
            score=score,
            signals=signals,
            bottleneck_activated=False,
            stock_count=concentration,
            avg_rank_change=round(avg_rank_change, 1),
        )

    _save_trend_history(conn, scan_id, now, chain_trend_results)
    _update_session(session, chain_trend_results)

    return chain_trend_results


def _is_newcomer(symbol: str, conn: sqlite3.Connection) -> bool:
    try:
        today = now_beijing().strftime("%Y-%m-%d")
        row = conn.execute(
            "SELECT 1 FROM chokepoint_recommendations WHERE symbol = ? AND date = ? LIMIT 1",
            (symbol, today),
        ).fetchone()
        if row:
            return False
        row = conn.execute(
            "SELECT 1 FROM appearances WHERE symbol = ? AND date = ? LIMIT 1",
            (symbol, today),
        ).fetchone()
        return row is None
    except Exception as e:
        logger.warning("  [产业链] 查询新人失败 %s: %s", symbol, e)
        return False


def _calc_persistence(chain_name: str, conn: sqlite3.Connection) -> int:
    try:
        rows = conn.execute(
            "SELECT DISTINCT substr(scan_time,1,10) AS day FROM chain_trend_history "
            "WHERE chain_name = ? ORDER BY day DESC LIMIT ?",
            (chain_name, CHAIN_PERSISTENCE_FADING + 2),
        ).fetchall()
        if not rows:
            return 0
        dates = sorted(r[0] for r in rows)
        streak = 1
        for i in range(len(dates) - 1, 0, -1):
            d1 = date.fromisoformat(dates[i])
            d2 = date.fromisoformat(dates[i - 1])
            cursor = d1 - timedelta(days=1)
            consecutive = False
            while cursor >= d2:
                if cursor == d2:
                    consecutive = True
                    break
                if is_trading_day(cursor):
                    break
                cursor -= timedelta(days=1)
            if consecutive:
                streak += 1
            else:
                break
        return streak
    except Exception:
        return 0


def _classify_phase(
    concentration: int,
    persistence: int,
    newbie_rate: float,
    avg_rank_change: float,
) -> tuple[str, int]:
    """按集中度 / 持续性 / 扩散信号判定产业链相变。

    不再依赖"瓶颈环节"匹配（原实现用公司名去匹配上游技术关键词，
    几乎永不命中，已废弃）。相位均可达：
      erupting  集中度 >= HOT
      growing   集中度 >= WARM 且 持续性 >= MIN
      forming   集中度 >= LOW（早期入口，不要求持续性）
      fading    持续性 >= FADING 且 集中度 < LOW
      dormant  其他
    """
    score = 0

    if concentration >= CHAIN_CONCENTRATION_HOT:
        score += 40
    if concentration >= CHAIN_CONCENTRATION_WARM:
        score += 20
    if newbie_rate > 0.3:
        score += 10
    if avg_rank_change > 100:
        score += 10
    elif avg_rank_change > 50:
        score += 5
    if persistence >= CHAIN_PERSISTENCE_MIN:
        score += 10

    if concentration >= CHAIN_CONCENTRATION_HOT:
        phase = "erupting"
    elif concentration >= CHAIN_CONCENTRATION_WARM and persistence >= CHAIN_PERSISTENCE_MIN:
        phase = "growing"
    elif concentration >= CHAIN_CONCENTRATION_LOW:
        phase = "forming"
    elif persistence >= CHAIN_PERSISTENCE_FADING and concentration < CHAIN_CONCENTRATION_LOW:
        phase = "fading"
    else:
        phase = "dormant"

    return phase, min(score, 100)


def _save_trend_history(
    conn: sqlite3.Connection,
    scan_id: str,
    scan_time: str,
    results: dict[str, ChainTrend],
):
    for chain_name, trend in results.items():
        try:
            conn.execute(
                "INSERT OR REPLACE INTO chain_trend_history "
                "(scan_id, scan_time, chain_name, phase, score, stock_count, "
                "bottleneck_active, avg_rank_change, signals) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    scan_id,
                    scan_time,
                    chain_name,
                    trend.phase,
                    trend.score,
                    trend.stock_count,
                    1 if trend.bottleneck_activated else 0,
                    trend.avg_rank_change,
                    json.dumps(trend.signals, ensure_ascii=False),
                ),
            )
        except Exception as e:
            logger.warning("  [产业链] 保存链趋势历史失败 %s: %s", chain_name, e)
    conn.commit()


def _update_session(session: IndustryScanSession, results: dict[str, ChainTrend]):
    snapshot = {
        "time": now_beijing().isoformat(),
        "trends": {k: {"phase": v.phase, "score": v.score, "stock_count": v.stock_count}
                   for k, v in results.items()},
    }
    session.chain_scan_history.append(snapshot)
    if len(session.chain_scan_history) > session.max_history_rounds:
        session.chain_scan_history.pop(0)


def get_active_chains(
    chain_trends: dict[str, ChainTrend],
    min_phase: str = "forming",
) -> list[str]:
    phase_rank = {"dormant": 0, "fading": 1, "forming": 2, "growing": 3, "erupting": 4}
    min_rank = phase_rank.get(min_phase, 2)
    return [
        name for name, t in chain_trends.items()
        if phase_rank.get(t.phase, 0) >= min_rank
    ]
