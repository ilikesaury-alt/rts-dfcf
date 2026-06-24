import json
import sqlite3
from datetime import datetime
from typing import Any

from scanner.config import (
    CHAIN_CONCENTRATION_HOT,
    CHAIN_CONCENTRATION_LOW,
    CHAIN_CONCENTRATION_WARM,
    CHAIN_PERSISTENCE_FADING,
    CHAIN_PERSISTENCE_MIN,
)
from scanner.industry_chain.chains import CHAINS, is_bottleneck_node, match_chains
from scanner.industry_chain.models import ChainTrend, IndustryScanSession


def judge_chain_trends(
    raw_items: list[dict],
    conn: sqlite3.Connection,
    session: IndustryScanSession,
    scan_id: str,
) -> dict[str, ChainTrend]:
    now = datetime.now().isoformat()

    chain_stocks: dict[str, list[dict]] = {}
    chain_bottleneck_active: dict[str, bool] = {}
    chain_rank_changes: dict[str, list[int]] = {}
    chain_newcomers: dict[str, list[str]] = {}
    chain_bottleneck_first: dict[str, bool] = {}

    seen_symbols = set()

    for item in raw_items:
        name = item.get("name", "")
        symbol = item.get("symbol", "")
        seen_symbols.add(symbol)
        matches = match_chains(name)
        if not matches:
            continue

        for chain_name, node_name, bottleneck, _ in matches:
            if chain_name not in chain_stocks:
                chain_stocks[chain_name] = []
                chain_bottleneck_active[chain_name] = False
                chain_rank_changes[chain_name] = []
                chain_newcomers[chain_name] = []
                chain_bottleneck_first[chain_name] = False

            chain_stocks[chain_name].append(item)
            chain_rank_changes[chain_name].append(abs(item.get("rank_change") or 0))
            if bottleneck:
                chain_bottleneck_active[chain_name] = True

            is_new = _is_newcomer(symbol, conn)
            if is_new:
                chain_newcomers[chain_name].append(symbol)

    for chain_name in chain_stocks:
        _check_diffusion_path(chain_name, chain_stocks[chain_name], chain_bottleneck_first)

    chain_trend_results: dict[str, ChainTrend] = {}

    for chain_name, stocks in chain_stocks.items():
        if not stocks:
            continue

        concentration = len(stocks)
        newbie_rate = len(chain_newcomers.get(chain_name, [])) / max(concentration, 1)
        avg_rank_change = (
            sum(chain_rank_changes.get(chain_name, [])) / max(len(chain_rank_changes.get(chain_name, [])), 1)
        )
        bottleneck_active = chain_bottleneck_active.get(chain_name, False)
        bottleneck_first = chain_bottleneck_first.get(chain_name, False)
        persistence = _calc_persistence(chain_name, conn)

        signals = []
        signals.append(f"链集聚度{concentration}")
        if bottleneck_active:
            signals.append("瓶颈激活")
        if bottleneck_first:
            signals.append("良性扩散")
        if newbie_rate > 0.3:
            signals.append(f"新人率{newbie_rate:.0%}")
        if persistence >= CHAIN_PERSISTENCE_FADING:
            signals.append(f"持续{persistence}轮")
        if avg_rank_change > 50:
            signals.append(f"排名势能{avg_rank_change:.0f}")

        phase, score = _classify_phase(
            concentration=concentration,
            bottleneck_active=bottleneck_active,
            persistence=persistence,
            newbie_rate=newbie_rate,
            avg_rank_change=avg_rank_change,
        )

        chain_trend_results[chain_name] = ChainTrend(
            chain_name=chain_name,
            phase=phase,
            score=score,
            signals=signals,
            bottleneck_activated=bottleneck_active,
            stock_count=concentration,
            avg_rank_change=round(avg_rank_change, 1),
        )

    _save_trend_history(conn, scan_id, now, chain_trend_results)
    _update_session(session, chain_trend_results)

    return chain_trend_results


def _is_newcomer(symbol: str, conn: sqlite3.Connection) -> bool:
    try:
        today = datetime.now().strftime("%Y-%m-%d")
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
    except Exception:
        return True


def _check_diffusion_path(chain_name: str, stocks: list[dict], result: dict[str, bool]):
    chain_def = CHAINS.get(chain_name)
    if not chain_def:
        return
    flow = chain_def.get("flow", [])
    if not flow:
        return

    bottleneck_node_name = None
    for node in chain_def.get("nodes", []):
        if node.get("bottleneck"):
            bottleneck_node_name = node["name"]
            break

    if not bottleneck_node_name:
        return

    bottleneck_found = False
    for item in stocks:
        name = item.get("name", "")
        matches = match_chains(name)
        for cn, nn, bn, _ in matches:
            if cn == chain_name and nn == bottleneck_node_name:
                bottleneck_found = True
                break
        if bottleneck_found:
            break

    result[chain_name] = bottleneck_found


def _calc_persistence(chain_name: str, conn: sqlite3.Connection) -> int:
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        rows = conn.execute(
            "SELECT scan_id FROM chain_trend_history WHERE chain_name = ? "
            "AND scan_time >= ? ORDER BY scan_time DESC LIMIT ?",
            (chain_name, today, CHAIN_PERSISTENCE_FADING + 2),
        ).fetchall()
        return len(rows)
    except Exception:
        return 0


def _classify_phase(
    concentration: int,
    bottleneck_active: bool,
    persistence: int,
    newbie_rate: float,
    avg_rank_change: float,
) -> tuple[str, int]:
    score = 0

    if concentration >= CHAIN_CONCENTRATION_HOT and bottleneck_active:
        score += 40
    if concentration >= CHAIN_CONCENTRATION_WARM:
        score += 20
    if bottleneck_active:
        score += 15
    if newbie_rate > 0.3:
        score += 10
    if avg_rank_change > 100:
        score += 10
    elif avg_rank_change > 50:
        score += 5
    if persistence >= CHAIN_PERSISTENCE_MIN:
        score += 10

    if concentration >= CHAIN_CONCENTRATION_HOT and bottleneck_active:
        phase = "erupting"
    elif concentration >= CHAIN_CONCENTRATION_WARM and persistence >= CHAIN_PERSISTENCE_MIN:
        phase = "growing"
    elif concentration >= CHAIN_CONCENTRATION_LOW and bottleneck_active:
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
            print(f"  [!] 保存链趋势历史失败 {chain_name}: {e}")
    conn.commit()


def _update_session(session: IndustryScanSession, results: dict[str, ChainTrend]):
    snapshot = {
        "time": datetime.now().isoformat(),
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
