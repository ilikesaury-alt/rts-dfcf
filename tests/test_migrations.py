"""`scanner.db.migrations` 版本化迁移的语义测试（2026-09-13，测评 A5 落地）。

本文件守护的是**失败语义**与**账本语义**，不是 SQL 本身：

- 失败 → 回滚 + 上抛 + **不写账本** → 下轮重试（2026-09-11 v4 事故的根因）；
- 存量库里已满足的迁移 → **只记账不执行**（不能重跑 ALTER，会 duplicate column）；
- 已记账的 → 跳过（每轮扫描都跑 init_db，不能重复做 PRAGMA 探测+DDL）。

全程离线，不碰生产库（需要真实 init_db 的用例用 tmp_path + DB_PATH monkeypatch）。
"""

from __future__ import annotations

import sqlite3

import pytest

from scanner.db import migrations as mig
from scanner.db.migrations import MIGRATIONS, Migration, run_migrations


def _conn(tmp_path, name="m.db"):
    c = sqlite3.connect(tmp_path / name)
    c.execute("PRAGMA journal_mode=DELETE")  # 测试用不着 WAL，避免留下 -wal 文件
    return c


def _ledger(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT id FROM schema_migrations").fetchall()}


# ── 账本语义 ──


class TestLedgerSemantics:
    def test_applied_migrations_get_recorded(self, tmp_path):
        conn = _conn(tmp_path)
        calls = []

        def _up(c):
            calls.append("up")

        m = Migration(id="t001", desc="d", check=lambda c: False, up=_up)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(mig, "MIGRATIONS", [m])
            applied = run_migrations(conn)
        assert applied == ["t001"] and calls == ["up"]
        assert _ledger(conn) == {"t001"}

    def test_already_satisfied_is_backfilled_without_running_up(self, tmp_path):
        """存量库：check() 为真 → 只记账，绝不重跑 up（重跑 ALTER 会 duplicate column）。"""
        conn = _conn(tmp_path)

        def _up(c):
            raise AssertionError("存量库不得重跑 up")

        m = Migration(id="t002", desc="d", check=lambda c: True, up=_up)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(mig, "MIGRATIONS", [m])
            applied = run_migrations(conn)
        assert applied == []              # 不算"本轮新应用"
        assert _ledger(conn) == {"t002"}  # 但要记账，否则每轮都要探测

    def test_recorded_migration_is_skipped(self, tmp_path):
        """已记账 → 连 check() 都不跑。"""
        conn = _conn(tmp_path)
        checked = []

        def _check(c):
            checked.append(1)
            return True

        m = Migration(id="t003", desc="d", check=_check, up=lambda c: None)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(mig, "MIGRATIONS", [m])
            run_migrations(conn)
            checked.clear()
            run_migrations(conn)
        assert checked == [], "已记账的迁移不得再探测"

    def test_ledger_rows_are_idempotent(self, tmp_path):
        """INSERT OR REPLACE：重复记账不产生重复行（id 是 PK）。"""
        conn = _conn(tmp_path)
        m = Migration(id="t004", desc="d", check=lambda c: True, up=lambda c: None)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(mig, "MIGRATIONS", [m])
            run_migrations(conn)
            run_migrations(conn)
        assert conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 1


# ── 失败语义（本模块存在的理由）──


class TestFailureSemantics:
    def test_failure_is_not_recorded(self, tmp_path, capsys):
        """迁移失败 → 不写账本。这是「下轮会重试」的前提。"""
        conn = _conn(tmp_path)

        def _boom(c):
            raise sqlite3.OperationalError("模拟磁盘写满")

        m = Migration(id="t010", desc="d", check=lambda c: False, up=_boom)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(mig, "MIGRATIONS", [m])
            with pytest.raises(sqlite3.Error):
                run_migrations(conn)
        assert _ledger(conn) == set(), "失败不得记账——记了就永不重试"
        assert "下轮重试" in capsys.readouterr().out

    def test_failed_migration_is_retried_next_round(self, tmp_path):
        """★ 2026-09-11 v4 事故的回归测试：失败后修好条件，下轮必须重新应用。

        探测式迁移在这里会失败：半迁移现场让下一轮的探测条件判定为"已完成"→ 永不重试。
        """
        conn = _conn(tmp_path)
        state = {"fail": True}

        def _up(c):
            if state["fail"]:
                raise sqlite3.OperationalError("第一轮失败")
            c.execute("CREATE TABLE retry_marker (x TEXT)")

        m = Migration(id="t011", desc="d", check=lambda c: False, up=_up)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(mig, "MIGRATIONS", [m])
            with pytest.raises(sqlite3.Error):
                run_migrations(conn)
            assert "retry_marker" not in _tables(conn)

            state["fail"] = False
            applied = run_migrations(conn)  # 下轮重试
        assert applied == ["t011"]
        assert "retry_marker" in _tables(conn)
        assert _ledger(conn) == {"t011"}

    def test_later_migrations_do_not_run_after_a_failure(self, tmp_path):
        """前一条失败 → 后续迁移不继续（顺序依赖：后面的可能依赖前面的列）。"""
        conn = _conn(tmp_path)
        ran = []

        def _boom(c):
            raise sqlite3.OperationalError("x")

        ms = [
            Migration(id="t020", desc="d", check=lambda c: False, up=_boom),
            Migration(id="t021", desc="d", check=lambda c: False, up=lambda c: ran.append("t021")),
        ]
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(mig, "MIGRATIONS", ms)
            with pytest.raises(sqlite3.Error):
                run_migrations(conn)
        assert ran == []
        assert _ledger(conn) == set()


def _tables(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}


# ── 迁移清单本身的约束 ──


class TestMigrationCatalog:
    def test_ids_are_unique_and_ordered(self):
        ids = [m.id for m in MIGRATIONS]
        assert len(ids) == len(set(ids)), "迁移 id 必须唯一（账本以 id 为主键）"
        assert ids == sorted(ids), "MIGRATIONS 必须按 id 升序（即执行顺序）"

    def test_ids_are_stable(self):
        """改名 = 老库把它当新迁移重跑。此断言强制「新增必须走审批」。"""
        assert [m.id for m in MIGRATIONS] == [
            "m001_daily_kline_finalized",
            "m002_rec_source",
            "m003_rec_cum_2d",
            "m004_rec_cum_3d",
            "m005_rec_concept",
            "m006_rec_accumulated_pct",
            "m007_rec_excluded",
            "m008_rec_stale_kline",
            "m009_rec_excluded_reason",
            "m010_idx_rec_source",
            "m011_observation_schema",
            "m012_market_extra_cache_pk",
            "m013_hot_watch_tables",
            "m014_ranking_snapshot_drop_marked",
            "m015_offboard_tables",
            "m016_offboard_log_last_hit",
        ]

    def test_every_migration_has_desc(self):
        for m in MIGRATIONS:
            assert m.desc and len(m.desc) > 4, f"{m.id} 缺少可读描述（运维要靠它判断要不要跑）"


# ── 与 init_db 的集成 ──


def _init_db_at(path, monkeypatch):
    import scanner.config as cfgmod
    import scanner.database as dbmod

    monkeypatch.setattr(cfgmod, "DB_PATH", str(path))
    return dbmod.init_db()


class TestInitDbIntegration:
    def test_fresh_db_applies_all_and_is_idempotent(self, tmp_path, monkeypatch):
        p = tmp_path / "fresh.db"
        conn = _init_db_at(p, monkeypatch)
        try:
            assert _ledger(conn) == {m.id for m in MIGRATIONS}
            assert "hot_watch_hits" in _tables(conn)
            assert "hot_watch_meta" in _tables(conn)
            assert _has_col(conn, "recommendations", "excluded_reason")
        finally:
            conn.close()

        # 第二轮：不得重复执行
        conn2 = _init_db_at(p, monkeypatch)
        try:
            assert _ledger(conn2) == {m.id for m in MIGRATIONS}
        finally:
            conn2.close()

    def test_legacy_db_backfills_ledger_and_adds_missing_tables(self, tmp_path, monkeypatch):
        """模拟生产库现状：结构基本齐全但缺 hot_watch 表 → 只补建缺失的那一条。"""
        p = tmp_path / "legacy.db"
        raw = sqlite3.connect(p)
        raw.execute("""CREATE TABLE recommendations (
            id INTEGER PRIMARY KEY, date TEXT, time TEXT, symbol TEXT, name TEXT,
            category TEXT, score INTEGER, percent REAL, source TEXT, cum_2d REAL,
            cum_3d REAL, concept TEXT, accumulated_pct REAL, excluded INTEGER,
            stale_kline INTEGER, excluded_reason TEXT)""")
        raw.execute("CREATE TABLE daily_kline (symbol TEXT, date TEXT, close REAL, finalized INTEGER)")
        raw.execute("CREATE TABLE scan_rejections (date TEXT, symbol TEXT, nd10_pct REAL)")
        raw.commit()
        raw.close()

        conn = _init_db_at(p, monkeypatch)
        try:
            assert "hot_watch_hits" in _tables(conn), "缺失的表必须被补建"
            assert _ledger(conn) == {m.id for m in MIGRATIONS}
            # 已存在的列不得被重复添加（重复 ALTER 会 duplicate column name 报错）
            assert _has_col(conn, "recommendations", "excluded_reason")
        finally:
            conn.close()

    def test_migration_state_reports_pending_before_and_applied_after(self, tmp_path, monkeypatch):
        p = tmp_path / "state.db"
        conn = _init_db_at(p, monkeypatch)
        try:
            state = mig.migration_state(conn)
            assert set(state.values()) == {"applied"}, state
        finally:
            conn.close()


def _has_col(conn, table, col) -> bool:
    return col in {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
