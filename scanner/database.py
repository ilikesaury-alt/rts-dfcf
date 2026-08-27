"""database 兼容门面（P1-6 拆分，2026-08-21）。

实现已按职责拆到 scanner/db/ 包（schema=连接+DDL / queries=只读 / dal=写入），
本模块保留为纯 re-export：既有调用方 `from scanner.database import X` 一行不改。
私有名（_n_trading_days_ago/_assign_rank_scores）也被 tests 直接 import，一并导出。

注意：monkeypatch 打点应指向实现所在模块（scanner.db.dal / scanner.db.queries），
patch scanner.database 命名空间不再影响实现行为。
"""

from scanner.db import (  # noqa: F401
    SCHEMA_VERSION,
    _assign_rank_scores,
    _n_trading_days_ago,
    count_recent_appearances,
    get_cached_kline,
    get_cached_klines,
    get_cached_market_caps,
    get_concepts_cache,
    get_conn,
    get_consecutive_appearance_days,
    get_consecutive_appearance_days_batch,
    get_fund_flow_pct_map,
    get_loss_rates_batch,
    get_market_extra_cache,
    get_market_index_log,
    get_prominence_map,
    get_recent_recommendations,
    get_symbol_appearances,
    get_today_recommendations,
    get_watch_symbols,
    init_db,
    is_prominent,
    mark_reversed_recommendations,
    mark_watch_evaluated,
    prune_watch_pool,
    record_appearances,
    record_leaderboard_log,
    save_concepts_cache,
    save_kline_to_db,
    save_market_caps,
    save_market_extra_cache,
    save_market_index_log,
    save_recommendations,
    save_scan_quality,
    upsert_watch_symbol,
    upsert_watch_symbols,
)
