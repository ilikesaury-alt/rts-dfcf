"""公告缓存同步 CLI（2026-11-11，m025 的写侧）。

**离线旁路，不挂主循环**（AGENTS.md「效率是前提」：慢手段只进离线旁路）。
盘中候选筛选走 `sync_symbols`：东财 `stock_list` 支持逗号分隔多码，实测 12 只票
**1 个请求**拿全（旧实现逐票 12 请求）；历史回填走 `--market-day`（单日约 900 条 / 9 页）。

```bash
python scripts/ann_cache_sync.py                          # 取当日最后一轮 offboard 打印行的票，批量刷新
python scripts/ann_cache_sync.py --symbols 301512,300533  # 指定票
python scripts/ann_cache_sync.py --days 60                # 回看窗口（默认 30 天）
python scripts/ann_cache_sync.py --migrate-legacy         # 从 12MB 旧 JSON 一次性导入历史公告（幂等）
python scripts/ann_cache_sync.py --market-day --date 2026-11-10  # 全市场按日回填（仅创业板）
```

水位语义：取数失败**不推进水位**（下次重试），读侧 `coverage()` 会显式告警
「缓存截止 X，可能缺最近公告」——绝不静默把「没取到」当作「没有公告」。
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import scanner.config as cfg  # noqa: E402
from scanner.announcement_cache import (  # noqa: E402
    LEGACY_CACHE,
    migrate_legacy_json,
    sync_market_day,
    sync_symbols,
    waterline,
)
from scanner.database import init_db  # noqa: E402


def _default_symbols(conn, day: str) -> list[str]:
    """缺省候选 = 指定日**最后一轮**终端打印的榜外异动行（与报告输入同一口径）。"""
    row = conn.execute("SELECT MAX(print_seq) FROM offboard_print_snapshot WHERE date = ?", (day,)).fetchone()
    if not row or row[0] is None:
        return []
    seq = int(row[0])
    return [
        str(r[0])
        for r in conn.execute(
            "SELECT symbol FROM offboard_print_snapshot WHERE date = ? AND print_seq = ? ORDER BY rank_in_print",
            (day, seq),
        )
    ]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="公告缓存同步（批量候选 / 全市场按日 / 旧 JSON 迁移）")
    p.add_argument("--date", default=date.today().isoformat(), help="信号日 / 回填日（默认今天）")
    p.add_argument("--symbols", default="", help="逗号分隔带或不带前缀；缺省取当日最后一轮 offboard 打印行")
    p.add_argument("--days", type=int, default=30, help="按票回看天数（默认 30）")
    p.add_argument("--market-day", action="store_true", help="按交易日全市场回填（仅创业板）")
    p.add_argument("--migrate-legacy", action="store_true", help="从 logs/shadow_feature_cache.json 导入历史公告")
    p.add_argument("--legacy-path", default=str(LEGACY_CACHE), help="旧缓存路径（配合 --migrate-legacy）")
    p.add_argument("--db", default=cfg.DB_PATH, help="SQLite 路径（默认 scanner.db）")
    args = p.parse_args(argv)

    # 独立进程，改 DB_PATH 只影响本进程；init_db 会跑版本化迁移（含 m025 建表）。
    cfg.DB_PATH = args.db
    conn = init_db()
    try:
        if args.migrate_legacy:
            r = migrate_legacy_json(conn, args.legacy_path)
            print(
                f"[迁移] 导入 {r['rows']} 行 / {r['symbols']} 只 / 最新公告日 {r['latest']}"
                + (f" | 失败: {r['error']}" if r.get("error") else "")
            )
        if args.market_day:
            r = sync_market_day(conn, args.date)
            print(f"[全市场] {args.date} 写入 {r['rows']} 行 | 水位 {r['waterline']} | 失败={r['failed']}")
        else:
            syms = [s.strip() for s in args.symbols.split(",") if s.strip()]
            if not syms:
                syms = _default_symbols(conn, args.date)
            if not syms:
                print(f"[跳过] {args.date} 没有 offboard_print_snapshot 行，且未指定 --symbols")
                return 0
            r = sync_symbols(conn, syms, days=args.days)
            print(f"[批量] {len(syms)} 只 / 新写入 {r['rows']} 行 / 水位 {r['waterline']} | 失败={r['failed']}")
        wl = waterline(conn)
        for scope in ("legacy", "market_daily", "candidates"):
            if scope in wl:
                w = wl[scope]
                print(f"  水位 {scope}: 截止 {w['end_date']} / {w['rows']} 行 / 更新于 {w['updated']}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
