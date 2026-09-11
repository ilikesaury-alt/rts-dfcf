"""market_extra_cache v4 PK 迁移的原子性回归（2026-09-11 修复）。

背景：原迁移把 RENAME / CREATE / INSERT SELECT / DROP 四条 DDL 裹在
`try: ... except sqlite3.Error: pass` 里。Python `sqlite3` 默认
`isolation_level=""` 下 **DDL 自开事务并即刻提交**，四步各自落盘；中途失败
（磁盘写满 / 进程被杀 / 名字冲突）即留下半迁移现场：旧表还在、新表已建、
数据未搬完。且因新表 PK 已含 date，下一轮 init_db 的 old_pk 检测为 False →
**永不重试**，`market_extra_cache_old` 沦为孤儿表，其中历史资金流数据不可查。

现改为 executescript 单事务 + 失败 rollback + fail-loud 上抛。
"""

import sqlite3

import pytest

_OLD_DDL = """
CREATE TABLE market_extra_cache (
    symbol TEXT NOT NULL,
    data_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    updated TEXT NOT NULL,
    date TEXT NOT NULL,
    PRIMARY KEY(symbol, data_type)
)
"""
_ROW = (
    "INSERT INTO market_extra_cache (symbol, data_type, payload_json, updated, date) "
    "VALUES ('SZ300001', 'fund_flow', '{\"main_pct\": 1.5}', '2026-09-01', '2026-09-01')"
)


def _init_db_at(path, monkeypatch):
    """在指定路径上跑真实 init_db（不走包级导入缓存导致的 DB_PATH 固化）。"""
    import scanner.config as cfgmod
    import scanner.database as dbmod

    monkeypatch.setattr(cfgmod, "DB_PATH", str(path))
    return dbmod.init_db()


def _pk_cols(conn, table="market_extra_cache"):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall() if r[5] > 0]


def _tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}


class TestMarketExtraCachePkMigration:
    """v4 迁移：旧 PK (symbol,data_type) → (symbol,data_type,date)，保留历史行。"""

    def test_migrates_old_pk_preserving_rows(self, tmp_path, monkeypatch):
        p = tmp_path / "mec.db"
        raw = sqlite3.connect(p)
        raw.executescript(_OLD_DDL + ";" + _ROW + ";")
        raw.commit()
        raw.close()

        conn = _init_db_at(p, monkeypatch)
        try:
            # PRAGMA table_info 的 pk 序号按 PK 定义顺序给出：(symbol, date, data_type)
            assert _pk_cols(conn) == ["symbol", "date", "data_type"]
            assert conn.execute("SELECT COUNT(*) FROM market_extra_cache").fetchone()[0] == 1
            assert "market_extra_cache_old" not in _tables(conn), "迁移成功不得留下临时表"
        finally:
            conn.close()

    def test_migration_idempotent(self, tmp_path, monkeypatch):
        """PK 已正确时再跑 init_db 不得重建（否则每轮扫描全表重建 + 锁表）。"""
        p = tmp_path / "mec2.db"
        raw = sqlite3.connect(p)
        raw.executescript(_OLD_DDL + ";" + _ROW + ";")
        raw.commit()
        raw.close()

        conn = _init_db_at(p, monkeypatch)
        conn.close()
        conn2 = _init_db_at(p, monkeypatch)
        try:
            assert _pk_cols(conn2) == ["symbol", "date", "data_type"]
            assert conn2.execute("SELECT COUNT(*) FROM market_extra_cache").fetchone()[0] == 1
        finally:
            conn2.close()

    def test_migration_failure_rolls_back_and_raises(self, tmp_path, monkeypatch):
        """迁移中途失败：必须整事务回滚（原表原封不动）+ 上抛（不再静默 pass）。

        构造：预先占住 RENAME 的目标名，使脚本内第一条 ALTER 即失败。
        """
        p = tmp_path / "mec3.db"
        raw = sqlite3.connect(p)
        raw.executescript(_OLD_DDL + ";" + _ROW + ";")
        raw.execute("CREATE TABLE market_extra_cache_old (x TEXT)")
        raw.commit()
        raw.close()

        with pytest.raises(sqlite3.Error):
            _init_db_at(p, monkeypatch)

        # 失败后原表未被改动（半迁移现场 = 旧表已 RENAME 而新表未建 → 此处必须不成立）
        raw = sqlite3.connect(p)
        try:
            assert _pk_cols(raw) == ["symbol", "data_type"], "失败后原表 PK 不得变化（整事务应回滚）"
            assert raw.execute("SELECT COUNT(*) FROM market_extra_cache").fetchone()[0] == 1
        finally:
            raw.close()

    def test_unknown_pk_skipped_without_raise(self, tmp_path, monkeypatch):
        """非预期 PK（人工改过 schema）：跳过迁移而非按旧 PK 假设硬搬，且不抛。"""
        p = tmp_path / "mec4.db"
        raw = sqlite3.connect(p)
        raw.executescript(_OLD_DDL.replace("PRIMARY KEY(symbol, data_type)", "PRIMARY KEY(symbol, updated)"))
        raw.execute(_ROW)
        raw.commit()
        raw.close()

        conn = _init_db_at(p, monkeypatch)
        try:
            assert _pk_cols(conn) == ["symbol", "updated"], "未知 PK 应原样保留"
            assert conn.execute("SELECT COUNT(*) FROM market_extra_cache").fetchone()[0] == 1
        finally:
            conn.close()
