-- 孤儿表导出备份（DROP 前的安全网）
--
-- 下列 8 张表在 scanner/ 与 tests/ 中零引用，且不在 db/schema.py 的 CREATE TABLE 列表中，
-- 属重构前遗留。清理前先导出建表语句 + 全部数据，万一后续需要可原样恢复。
--
-- 导出（sqlite3 CLI）：
--   cd <repo>
--   sqlite3 scanner.db ".read scripts/dump_orphan_tables.sql" > NUL
--   → 生成 orphan_tables_backup.sql
--
-- 恢复：
--   sqlite3 scanner.db ".read orphan_tables_backup.sql"
--
-- 表与导出时行数：
--   chain_trend_history        2749
--   minute_snapshot            1603
--   chokepoint_recommendations  153
--   nd10_px_cache                92
--   cross_validated_signals      58
--   parameter_snapshots           2
--   chain_stock_cache             0
--   user_trades                   0

.bail on
.output orphan_tables_backup.sql

PRAGMA foreign_keys=off;
BEGIN TRANSACTION;

.dump chain_trend_history
.dump minute_snapshot
.dump chokepoint_recommendations
.dump cross_validated_signals
.dump nd10_px_cache
.dump parameter_snapshots
.dump chain_stock_cache
.dump user_trades

COMMIT;

.output stdout
SELECT '备份完成 → orphan_tables_backup.sql';
SELECT 'DROP 前请核对：以下 8 张表应各有 CREATE TABLE 一行';
SELECT name FROM sqlite_master WHERE type='table' AND name IN (
  'chain_trend_history','minute_snapshot','chokepoint_recommendations',
  'cross_validated_signals','nd10_px_cache','parameter_snapshots',
  'chain_stock_cache','user_trades');
