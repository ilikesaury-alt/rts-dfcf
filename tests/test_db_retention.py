"""scripts/db_retention.py 的守卫测试。

这是唯一会**删数据**的新脚本 ⇒ 守的不是「算得准」，而是「删得对」：

1. dry-run 与 `--apply` 缺 `--yes` 一律不删（二次确认是硬闸）；
2. `--apply` 只删 cutoff 之前的快照行，永久表与窗口内行一行不动；
3. 只有白名单两表进滚动，新增表默认永久（不会被未来 schema 误删）；
4. 备份按主库文件计份数，`-shm`/`-wal` 伴随文件不算一份；
5. 日期列缺失时显式拒绝，不猜删除范围。

时钟用固定日期冻结（不是硬编码业务数据），避免重蹈「TTL/stale 时间炸弹」。
"""

from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "db_retention.py"


def _load():
    spec = importlib.util.spec_from_file_location("db_retention", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["db_retention"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def ret(tmp_path, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    monkeypatch.setattr(mod, "_now_date", lambda: date(2026, 11, 25))
    return mod


def _db(tmp_path: Path) -> Path:
    db = tmp_path / "scanner.db"
    conn = sqlite3.connect(db)
    for table in ("market_extra_cache", "crosssection_signal", "recommendations"):
        conn.execute(f"CREATE TABLE {table} (symbol TEXT, date TEXT)")
    # cutoff = 2026-11-15：旧行该删，新行与永久表必须留。
    for table in ("market_extra_cache", "crosssection_signal"):
        conn.executemany(
            f"INSERT INTO {table} VALUES (?,?)",
            [("OLD", "2026-11-01"), ("NEW", "2026-11-24")],
        )
    conn.execute("INSERT INTO recommendations VALUES ('OLD', '2026-11-01')")
    conn.commit()
    conn.close()
    return db


def _rows(db: Path, table: str) -> list[str]:
    conn = sqlite3.connect(db)
    try:
        return [str(r[0]) for r in conn.execute(f"SELECT symbol FROM {table} ORDER BY symbol")]
    finally:
        conn.close()


def _backups(tmp_path: Path) -> None:
    """三份主备份 + 最新那份的伴随文件；mtime 递增。"""
    for i, name in enumerate(
        ("scanner.db.bak-old", "scanner.db.bak-mid", "scanner.db.bak-new",
         "scanner.db.bak-new-shm", "scanner.db.bak-new-wal")
    ):
        p = tmp_path / name
        p.write_text("x", encoding="ascii")
        os.utime(p, (1_700_000_000 + i, 1_700_000_000 + i))


def test_dry_run_never_deletes(ret, tmp_path, capsys):
    db = _db(tmp_path)
    rc = ret.main(["--db", str(db), "--keep-days", "10"])
    out = capsys.readouterr().out
    assert rc == 0 and "[DRY-RUN]" in out
    assert "未删除任何数据（快照 2 行 / 备份 0 份）" in out
    assert _rows(db, "market_extra_cache") == ["NEW", "OLD"]


def test_apply_requires_yes(ret, tmp_path, capsys):
    db = _db(tmp_path)
    rc = ret.main(["--db", str(db), "--keep-days", "10", "--apply"])
    assert rc == 2
    assert "删除需显式 --yes" in capsys.readouterr().err
    assert _rows(db, "market_extra_cache") == ["NEW", "OLD"]


def test_apply_deletes_only_expired_snapshots(ret, tmp_path):
    db = _db(tmp_path)
    _backups(tmp_path)
    rc = ret.main(["--db", str(db), "--keep-days", "10", "--apply", "--yes"])
    assert rc == 0
    for table in ("market_extra_cache", "crosssection_signal"):
        assert _rows(db, table) == ["NEW"], f"{table} 不应删掉窗口内行"
    # 永久表一行不动。
    assert _rows(db, "recommendations") == ["OLD"]
    # 备份只删超出保留份数的最旧主库；-shm/-wal 不算一份。
    assert not (tmp_path / "scanner.db.bak-old").exists()
    assert (tmp_path / "scanner.db.bak-mid").exists()
    assert (tmp_path / "scanner.db.bak-new").exists()
    assert (tmp_path / "scanner.db.bak-new-shm").exists()


def test_backup_filter_excludes_sidecars(ret, tmp_path):
    _backups(tmp_path)
    assert ret._backups_to_drop(2) == [tmp_path / "scanner.db.bak-old"]


def test_missing_date_column_is_rejected(ret, tmp_path):
    """表存在但日期列改名 ⇒ 拒绝删除，不能靠猜列或整表清空。"""
    db = tmp_path / "scanner.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE market_extra_cache (symbol TEXT, renamed_date TEXT)")
    conn.commit()
    conn.close()
    with pytest.raises(ValueError, match="缺日期列"):
        ret._scan(db, "2026-11-15", 2)
