"""scanner.db 包：database 层按职责拆分（设计审查 P1-6，2026-08-21）。

- schema.py   连接工厂 get_conn() + init_db() DDL/迁移 + SCHEMA_VERSION
- queries.py  只读查询（SELECT，不写库）
- dal.py      写入（INSERT/UPDATE/DELETE + commit）
- _common.py  包内共享 helper（交易日回溯）

对外兼容：scanner/database.py 是本包的 re-export 门面，既有调用方
（orchestrator/display/comeback/各脚本/tests）一律 `from scanner.database import X`
不变。新代码建议直接从 scanner.db 取。
"""
from scanner.db._common import _n_trading_days_ago
from scanner.db.dal import (
    mark_reversed_recommendations,
    mark_watch_evaluated,
    prune_minute_snapshots,
    prune_watch_pool,
    record_appearances,
    record_leaderboard_log,
    save_concepts_cache,
    save_kline_to_db,
    save_market_caps,
    save_market_extra_cache,
    save_market_index_log,
    save_minute_snapshots,
    save_recommendations,
    save_scan_quality,
    upsert_watch_symbol,
    upsert_watch_symbols,
)
from scanner.db.queries import (
    _assign_rank_scores,
    count_recent_appearances,
    get_cached_kline,
    get_cached_klines,
    get_cached_market_caps,
    get_concepts_cache,
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
    is_prominent,
)
from scanner.db.schema import SCHEMA_VERSION, get_conn, init_db

__all__ = [
    # schema
    "SCHEMA_VERSION",
    "get_conn",
    "init_db",
    # dal（写入）
    "mark_reversed_recommendations",
    "mark_watch_evaluated",
    "prune_minute_snapshots",
    "prune_watch_pool",
    "record_appearances",
    "record_leaderboard_log",
    "save_concepts_cache",
    "save_kline_to_db",
    "save_market_caps",
    "save_market_extra_cache",
    "save_market_index_log",
    "save_minute_snapshots",
    "save_recommendations",
    "save_scan_quality",
    "upsert_watch_symbol",
    "upsert_watch_symbols",
    # queries（只读）
    "count_recent_appearances",
    "get_cached_kline",
    "get_cached_klines",
    "get_cached_market_caps",
    "get_concepts_cache",
    "get_consecutive_appearance_days",
    "get_consecutive_appearance_days_batch",
    "get_fund_flow_pct_map",
    "get_loss_rates_batch",
    "get_market_extra_cache",
    "get_market_index_log",
    "get_prominence_map",
    "get_recent_recommendations",
    "get_symbol_appearances",
    "get_today_recommendations",
    "get_watch_symbols",
    "is_prominent",
    # 包内私有名，tests 直接 import（re-export 供兼容）
    "_assign_rank_scores",
    "_n_trading_days_ago",
]
