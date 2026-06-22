import sqlite3
import time
from datetime import datetime

from scanner.api import make_session, fetch_biaosheng
from scanner.utils import is_gem, is_hk_stock, is_st
from scanner.database import DB_PATH
from scanner.config import MAX_STOCK_PRICE
from scanner.chain_watch.chains import match_chains, match_chain_simple
from scanner.chain_watch.heat_detect import detect_hot_chains
from scanner.chain_watch.trend_score import score_stock, fetch_kline_for_symbol, WATCH_MIN_SCORE
from scanner.chain_watch.display import (
    _safe_print, print_watch_table, print_header, print_summary,
)


def run_once() -> dict:
    session = make_session()
    conn = sqlite3.connect(DB_PATH)

    raw = fetch_biaosheng(session)
    if not raw:
        _safe_print("  [!] 飙升榜数据为空")
        session.close()
        conn.close()
        return {"total_watch": 0, "hot_count": 0,
                "raw_count": 0, "gem_count": 0, "chain_count": 0}

    gem_raw = []
    for item in raw:
        symbol = item.get("symbol", "")
        code = item.get("code", "")
        name = item.get("name", "")
        if is_hk_stock(symbol) or not is_gem(code) or is_st(name):
            continue
        gem_raw.append(item)

    hot_chains = detect_hot_chains(gem_raw)

    chains_with_stocks = {k for k, v in hot_chains.items() if v["heat"] in ("hot", "warm")}

    print_header(len(raw), len(gem_raw), len(chains_with_stocks))

    total_watch = 0
    hot_count = 0

    for chain_name, info in hot_chains.items():
        if info["heat"] not in ("hot", "warm"):
            continue
        hot_count += 1

        scored = []
        for item in info["stocks"]:
            symbol = item.get("symbol", "")
            name = item.get("name", "")
            today_pct = item.get("percent") or 0

            current = item.get("current") or 0
            if current > 0 and current > MAX_STOCK_PRICE:
                continue

            node_matches = match_chains(name)
            is_bn = any(bn for _, _, bn in node_matches)

            kline = fetch_kline_for_symbol(symbol, conn, session)
            if kline is None:
                continue

            result = score_stock(symbol, kline, is_bn, today_pct)
            if result["score"] < WATCH_MIN_SCORE:
                continue

            total_watch += 1

            node_name = ""
            for cn, nn, _ in node_matches:
                if cn == chain_name:
                    node_name = nn
                    break

            scored.append({
                "name": name,
                "symbol": symbol,
                "score": result["score"],
                "node": node_name if node_name else "其他",
                "percent": today_pct,
                "signals": result["signals"],
            })

        print_watch_table(
            chain_name=chain_name,
            heat=info["heat"],
            stock_count=info["stock_count"],
            bottleneck_active=info["bottleneck_active"],
            avg_rank_change=info["avg_rank_change"],
            scored_stocks=scored,
        )

    print_summary(total_watch, hot_count)
    session.close()
    conn.close()

    return {"total_watch": total_watch, "hot_count": hot_count,
            "raw_count": len(raw), "gem_count": len(gem_raw),
            "chain_count": len(chains_with_stocks)}


def main_loop(interval: int = 300):
    try:
        while True:
            run_once()
            if interval <= 0:
                break
            _safe_print(f"  下次刷新 {interval}秒后... (Ctrl+C退出)\n")
            try:
                time.sleep(interval)
            except KeyboardInterrupt:
                _safe_print("\n  退出")
                break
    except KeyboardInterrupt:
        _safe_print("\n  退出")
