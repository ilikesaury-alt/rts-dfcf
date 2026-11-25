#!/usr/bin/env python3
"""按表分级保留数据库与备份（只读预览默认，删除需显式 --apply --yes）。

分级：
- 快照滚动表：`market_extra_cache` / `crosssection_signal` 按 `date` 保留最近
  `KEEP_DAYS` 个自然日。前者是东财快照缓存（可重取）；后者生产侧 `_persist`
  已有同口径淘汰，这里再跑一次是幂等的统一视图。
- 其余表一律**永久**：K 线、冻结信号、标签、公告缓存等都不是「可重取的当日快照」，
  删了会造成回测/复盘不可逆缺口。新增表默认落这一档（不会被脚本误删）。
- 备份文件：根目录 `scanner.db.bak-*`，按 mtime 保留最近 `KEEP_BACKUPS` 份。

用法：
    python scripts/db_retention.py                       # dry-run
    python scripts/db_retention.py --keep-days 60        # 自定义保留期
    python scripts/db_retention.py --apply --yes         # 真删（先自行备份，见下）
    python scripts/db_retention.py --apply --yes --vacuum  # 删后 VACUUM（需无写者）

⚠ 删除前请自行按 AGENTS.md 纪律备份 WAL 库（Online Backup API + 行数校验），
  本脚本不自动备份；dry-run 的输出就是将被删除的确切范围。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:  # 直接 `python scripts/xxx.py` 时 scripts/ 才是 sys.path[0]
    sys.path.insert(0, str(ROOT))
DEFAULT_DB = ROOT / "scanner.db"

#: 表名 → 日期列。只放「可重取的当日快照」，新增表默认永久。
SNAPSHOT_TABLES: dict[str, str] = {
    "market_extra_cache": "date",
    "crosssection_signal": "date",
}

DEFAULT_KEEP_DAYS = 90
DEFAULT_KEEP_BACKUPS = 2


def _now_date():
    from scanner.config import now_beijing

    return now_beijing().date()


def _scan(db: Path, cutoff: str, keep_backups: int) -> tuple[list[tuple[str, int, str, str]], list[Path]]:
    """只读统计：返回 (表名, 将删行数, min日期, max日期) 与将删备份文件。"""
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=15)
    try:
        rows: list[tuple[str, int, str, str]] = []
        for table, col in SNAPSHOT_TABLES.items():
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if not exists:
                continue
            cols = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
            if col not in cols:
                raise ValueError(f"{table} 缺日期列 {col}，拒绝猜测删除范围")
            n = conn.execute(f'SELECT COUNT(*) FROM "{table}" WHERE "{col}" < ?', (cutoff,)).fetchone()[0]
            lo, hi = conn.execute(
                f'SELECT MIN("{col}"), MAX("{col}") FROM "{table}" WHERE "{col}" < ?', (cutoff,)
            ).fetchone()
            rows.append((table, int(n), str(lo or "—"), str(hi or "—")))
        return rows, _backups_to_drop(keep_backups)
    finally:
        conn.close()


def _backups_to_drop(keep: int) -> list[Path]:
    # 只管主库备份；同组 -shm/-wal 是伴随文件，不能单独算一份。
    backups = [
        p for p in ROOT.glob("scanner.db.bak-*")
        if not p.name.endswith(("-shm", "-wal"))
    ]
    return sorted(backups, key=lambda p: p.stat().st_mtime, reverse=True)[keep:]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--keep-days", type=int, default=DEFAULT_KEEP_DAYS)
    ap.add_argument("--keep-backups", type=int, default=DEFAULT_KEEP_BACKUPS)
    ap.add_argument("--apply", action="store_true", help="真删；不带此开关只预览")
    ap.add_argument("--yes", action="store_true", help="--apply 的二次确认")
    ap.add_argument("--vacuum", action="store_true", help="删除后 VACUUM（需无其他写者）")
    args = ap.parse_args(argv)

    if args.keep_days < 1 or args.keep_backups < 0:
        print("保留期/备份份数非法", file=sys.stderr)
        return 2
    if not args.db.is_file():
        print(f"数据库不存在：{args.db}", file=sys.stderr)
        return 4
    if args.apply and not args.yes:
        print("删除需显式 --yes；先不带头预览删除量", file=sys.stderr)
        return 2

    cutoff = (_now_date() - timedelta(days=args.keep_days)).isoformat()
    targets, backups = _scan(args.db, cutoff, args.keep_backups)
    print(f"库：{args.db}（{args.db.stat().st_size / 1e6:.1f} MB）")
    print(f"快照保留期：{args.keep_days} 天，删除 date < {cutoff}")
    print(f"{'表':28s} {'将删行数':>10s}  旧日期范围")
    for table, n, lo, hi in targets:
        print(f"{table:28s} {n:>10d}  [{lo} .. {hi}]")
    total = sum(n for _, n, _, _ in targets)
    print(f"\n备份保留 {args.keep_backups} 份；将删：")
    for p in backups:
        print(f"  {p.name}  ({p.stat().st_size / 1e6:.1f} MB)")

    if not args.apply:
        print(f"\n[DRY-RUN] 未删除任何数据（快照 {total} 行 / 备份 {len(backups)} 份）。")
        return 0

    conn = sqlite3.connect(args.db, timeout=15)
    try:
        for table, col in SNAPSHOT_TABLES.items():
            cur = conn.execute(f'DELETE FROM "{table}" WHERE "{col}" < ?', (cutoff,))
            print(f"deleted {table}: {cur.rowcount} rows")
        conn.commit()
        if args.vacuum:
            conn.execute("VACUUM")
            print("VACUUM 完成")
    finally:
        conn.close()
    for p in backups:
        p.unlink()
        print(f"deleted backup: {p.name}")
    print("完成。请按 AGENTS.md 纪律校验留存行数。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
