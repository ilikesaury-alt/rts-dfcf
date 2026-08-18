"""修复 daily_kline 盘中残留脏 bar（2026-08-18 拓斯达案例的根治脚本）。

问题机制：盘中扫描/回填把「未收盘的今日 bar」（盘中价 + 部分量能）写入
daily_kline；收盘后若无定稿覆盖（backfill 只写缺口日期、扫描器收盘后不补拉），
残留 bar 永久留在库里——次日复盘/回测/个股报告读到错误收盘价
（拓斯达 08-18：盘中 36.27 残留，真实收盘 37.90，DB 显示 -3.36% 实际 +0.99%）。

修复策略：以雪球 qfq K 线（项目主数据源，与新浪源逐日核对一致）为权威，
对 daily_kline 全表逐 symbol 比对，差异超过阈值（1 分钱以上）即 INSERT OR REPLACE
覆盖为权威值。顺带把历史 qfq 锚点对齐到当前（dividend 调整一致性）。

用法：
    python repair_kline.py            # 全表修复（默认 --dry-run 关闭，直接写）
    python repair_kline.py --dry-run  # 只统计不写库
    python repair_kline.py --since 2026-07-27   # 只修该日期之后的行
"""
from __future__ import annotations

import argparse
import sqlite3
import time
from collections import Counter

from scanner.api import fetch_kline, make_session
from scanner.config import DB_PATH

EPS = 0.011  # 1 分钱以上视为差异


def main() -> None:
    parser = argparse.ArgumentParser(description="修复 daily_kline 盘中残留脏 bar")
    parser.add_argument("--dry-run", action="store_true", help="只统计不写库")
    parser.add_argument("--since", default=None, help="只修复该日期(含)之后的行，如 2026-07-27")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 个 symbol（调试用）")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    symbols = [r[0] for r in cur.execute("SELECT DISTINCT symbol FROM daily_kline ORDER BY symbol").fetchall()]
    if args.limit:
        symbols = symbols[: args.limit]

    since = args.since or "0000-01-01"
    session = make_session()
    total_checked = 0
    total_fixed = 0
    fixed_rows: list[tuple[str, str, float, float]] = []
    fetch_fail = 0
    try:
        for i, sym in enumerate(symbols, 1):
            try:
                kline = fetch_kline(session, sym, days=60)
            except Exception as e:  # noqa: BLE001
                print(f"  [{i}/{len(symbols)}] {sym}: fetch 失败 {e}")
                fetch_fail += 1
                continue
            if not kline:
                continue
            db_rows = {
                r[0]: (r[1], r[2])  # date -> (close, volume)
                for r in cur.execute(
                    "SELECT date, close, volume FROM daily_kline WHERE symbol=?", (sym,)
                ).fetchall()
            }
            for bar in kline:
                d = bar["date"]
                if d < since:
                    continue
                if d not in db_rows:
                    continue  # 只修已有行，不新增
                db_close, db_vol = db_rows[d]
                if abs(db_close - bar["close"]) <= EPS:
                    continue
                total_fixed += 1
                fixed_rows.append((sym, d, db_close, bar["close"]))
                if not args.dry_run:
                    conn.execute(
                        "INSERT OR REPLACE INTO daily_kline "
                        "(symbol, timestamp, date, open, close, high, low, volume, percent) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (sym, bar.get("timestamp"), d, bar["open"], bar["close"],
                         bar["high"], bar["low"], bar["volume"], bar["percent"]),
                    )
                total_checked += 1
            time.sleep(0.15)  # 节流，防雪球风控
            if i % 100 == 0:
                print(f"  ... {i}/{len(symbols)} symbols 处理中，已发现 {total_fixed} 处差异")
    finally:
        session.close()

    if not args.dry_run:
        conn.commit()

    print("\n=== 修复统计 ===")
    print(f"symbols: {len(symbols)} | fetch失败: {fetch_fail} | 差异行: {total_fixed} "
          f"| 模式: {'dry-run' if args.dry_run else '已写库'}")
    if fixed_rows:
        by_date = Counter(r[1] for r in fixed_rows)
        print("按日期分布(前10):")
        for d, n in sorted(by_date.items(), reverse=True)[:10]:
            print(f"  {d}: {n} 行")
        print("差异样本(前10):")
        for sym, d, dbc, xqc in fixed_rows[:10]:
            print(f"  {sym} {d}: DB={dbc} -> 权威={xqc}")
    conn.close()


if __name__ == "__main__":
    main()
