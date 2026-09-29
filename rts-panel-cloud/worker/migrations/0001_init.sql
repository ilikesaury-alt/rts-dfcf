-- rts-panel-cloud D1 初始表（design §5.2 / DR-4 / DR-6）
--
-- 幂等重放的基石是 `PRIMARY KEY (date, time)`：同一轮的 (date, time) 无论重试、
-- 重跑、换密钥重发，都只会 upsert 覆盖，**不会产生第二行**。
-- ⚠ 主键**不要**改成 `seq` —— 那是进程内单调轮号，重启后重排，会把新一轮
--   误判成已存在的行（design §5.1 复核修 2）。

CREATE TABLE IF NOT EXISTS snapshots (
  date         TEXT NOT NULL,   -- 交易日 YYYY-MM-DD
  time         TEXT NOT NULL,   -- 轮次时刻 HH:MM:SS
  seq          INTEGER NOT NULL,-- 本地进程内轮号，**仅展示**
  duration_ms  INTEGER NOT NULL DEFAULT 0,
  schema       INTEGER NOT NULL,
  payload      TEXT NOT NULL,   -- {regions, ctx}，目标 ≤200KB（单行硬限 2MB）
  ingested_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  PRIMARY KEY (date, time)
);

-- 保留策略只按 date 扫描（cron 的 DELETE / /api/history?date=）
CREATE INDEX IF NOT EXISTS idx_snapshots_date_time ON snapshots (date, time DESC);

-- 个股详情：K 线按 (symbol, kline_date) 幂等 upsert，详情端点取最新一根
CREATE TABLE IF NOT EXISTS kline_cache (
  symbol      TEXT NOT NULL,
  kline_date  TEXT NOT NULL,
  kline       TEXT NOT NULL,   -- [[date, open, close, low, high, volume], ...]
  appearances TEXT,            -- 该票近期出现史 JSON
  updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  PRIMARY KEY (symbol, kline_date)
);

CREATE INDEX IF NOT EXISTS idx_kline_symbol_date ON kline_cache (symbol, kline_date DESC);
