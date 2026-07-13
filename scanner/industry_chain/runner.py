import time

from scanner.api import make_session
from scanner.database import init_db, init_industry_chain_tables
from scanner.trading_session import is_trading_time
from scanner.industry_chain.display import (
    _safe_print,
    clear_console,
    print_candidate_detail,
    print_candidates,
    print_chain_trends,
    print_header,
    print_summary,
)
from scanner.industry_chain.models import IndustryScanSession
from scanner.industry_chain.pipeline import scan


def run_once(session_state: IndustryScanSession | None = None) -> dict:
    clear_console()
    session_state = session_state or IndustryScanSession()

    conn = init_industry_chain_tables()
    init_db()
    try:
        if not hasattr(run_once, "_session"):
            run_once._session = make_session()
        session = run_once._session

        start = time.time()
        candidates, chain_trends = scan(conn, session, session_state)
        elapsed = time.time() - start

        raw_count = sum(t.stock_count for t in chain_trends.values())
        active_count = sum(1 for t in chain_trends.values()
                           if t.phase in ("erupting", "growing", "forming"))

        print_header(
            raw_count=raw_count,
            gem_count=len(candidates),
            active_count=active_count,
        )
        print_chain_trends(chain_trends)
        print_candidates(candidates)

        if candidates:
            for c in candidates[:3]:
                print_candidate_detail(c)

        print_summary(candidates, elapsed)

        return {
            "candidates": len(candidates),
            "active_chains": active_count,
            "total_chains": len(chain_trends),
        }
    finally:
        conn.close()


def main_loop(interval: int = 300):
    session_state = IndustryScanSession()
    conn = init_industry_chain_tables()
    init_db()
    conn.close()

    try:
        while True:
            if not is_trading_time():
                time.sleep(60)
                continue
            run_once(session_state)
            if interval <= 0:
                break
            _safe_print(f"\n  下次扫描 {interval}秒后... (Ctrl+C退出)\n")
            try:
                time.sleep(interval)
            except KeyboardInterrupt:
                _safe_print("\n  退出")
                break
    except KeyboardInterrupt:
        _safe_print("\n  退出")
