"""回测数据完整性补全脚本。

问题背景：
    scanner 每日扫描时仅对"当日榜单上的票"调 fetch_kline，写入 daily_kline 表。
    如果某票推荐后次日跌出榜单，就不再调 API，导致 daily_kline 缺失次日 K 线，
    backtest.compute_outcome 查不到 next_day_pct，IC 样本被静默过滤，
    形成"下跌票幸存者偏差"——IC 结果系统性偏好上涨票。

修复方案：
    独立脚本遍历 recommendations 表，找出所有曾经被推荐的 symbol，
    对每只股票调用雪球 API 拉取最近 60 日 K 线，补全 daily_kline 缺失日期，
    然后调用 backtest.backfill_outcomes 重算 next_day_pct/fwd_3d/fwd_5d。

使用方式：
    python backfill_kline.py                # 补全 + 重算收益
    python backfill_kline.py --dry-run      # 仅统计缺失，不调 API 不写 DB
    python backfill_kline.py --days 60       # 自定义拉取天数（默认 60）

注意：
    - 雪球 API 速率限制约 6.6 QPS（_throttle 0.15s），补全 200 只票约 30 秒
    - fetch_kline 内部已带 _request_with_retry（429/5xx 重试 + 指数退避）
    - PRIMARY KEY(symbol, date) 保证 INSERT OR REPLACE 幂等
"""
from __future__ import annotations

import argparse
import sqlite3
import time
from collections import defaultdict

from scanner.api import fetch_kline, make_session
from scanner.backtest import backfill_outcomes
from scanner.config import DB_PATH, now_beijing
from scanner.database import save_kline_to_db


def _get_recommended_symbols(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """返回 {symbol: [推荐日期列表]}，按推荐日期升序。"""
    rows = conn.execute(
        "SELECT symbol, date FROM recommendations ORDER BY date ASC"
    ).fetchall()
    sym_dates: dict[str, list[str]] = defaultdict(list)
    for sym, dt in rows:
        sym_dates[sym].append(dt)
    return sym_dates


def _get_existing_kline_dates(conn: sqlite3.Connection, symbol: str) -> set[str]:
    """返回该 symbol 在 daily_kline 表中已有的日期集合。"""
    cur = conn.execute(
        "SELECT date FROM daily_kline WHERE symbol=?", (symbol,)
    )
    return {row[0] for row in cur.fetchall()}


def _expected_date_range(rec_dates: list[str], days: int) -> set[str]:
    """根据推荐日期推算需要补全的日期窗口。

    策略：取最早推荐日 -7 天 至 最晚推荐日 + 10 天，
    确保覆盖 next_day / fwd_3d / fwd_5d 的回测窗口。
    """
    from datetime import date, timedelta

    first = date.fromisoformat(rec_dates[0])
    last = date.fromisoformat(rec_dates[-1])
    start = first - timedelta(days=7)
    end = last + timedelta(days=10)
    # 扩展到最近的 days 天窗口（取较大者，确保雪球 API 一次拉全）
    today = now_beijing().date()
    if end > today:
        end = today
    result = set()
    cursor = start
    while cursor <= end:
        result.add(cursor.isoformat())
        cursor += timedelta(days=1)
    return result


def backfill(dry_run: bool = False, days: int = 60, verbose: bool = True) -> dict:
    """主入口：补全推荐历史股票的 K 线数据。

    返回统计 dict：
        - total_symbols: 推荐历史涉及的股票数
        - missing_symbols: 有 K 线缺口的股票数
        - missing_dates_total: 需补全的日期缺口总数
        - fetched_symbols: 实际调用 API 的股票数
        - fetched_rows: 实际写入 DB 的 K 线行数
        - api_failures: API 调用失败数
        - outcomes_updated: backfill_outcomes 重算的 recommendations 行数
    """
    stats = {
        "total_symbols": 0, "missing_symbols": 0, "missing_dates_total": 0,
        "fetched_symbols": 0, "fetched_rows": 0, "api_failures": 0,
        "outcomes_updated": 0,
    }

    conn = sqlite3.connect(DB_PATH)
    try:
        sym_dates = _get_recommended_symbols(conn)
        stats["total_symbols"] = len(sym_dates)

        if not sym_dates:
            if verbose:
                print("[backfill] recommendations 表为空，无需补全")
            return stats

        # 第一步：扫描缺口
        missing_map: dict[str, set[str]] = {}
        for sym, rec_dates in sym_dates.items():
            existing = _get_existing_kline_dates(conn, sym)
            expected = _expected_date_range(rec_dates, days)
            missing = expected - existing
            if missing:
                missing_map[sym] = missing
                stats["missing_dates_total"] += len(missing)

        stats["missing_symbols"] = len(missing_map)

        if verbose:
            print(f"[backfill] 推荐历史涉及 {stats['total_symbols']} 只股票")
            print(f"[backfill] 其中 {stats['missing_symbols']} 只有 K 线缺口")
            print(f"[backfill] 需补全日期缺口共 {stats['missing_dates_total']} 个")

        if dry_run:
            if verbose:
                print("[backfill] --dry-run 模式，不调 API 不写 DB")
                # 打印前 10 只缺口的细节
                for sym, miss in list(missing_map.items())[:10]:
                    print(f"  {sym}: 缺 {len(miss)} 天 ({sorted(miss)[0]}~{sorted(miss)[-1]})")
            return stats

        if not missing_map:
            if verbose:
                print("[backfill] 无缺口，直接重算 outcomes")
        else:
            # 第二步：调 API 补全
            session = make_session()
            try:
                for i, (sym, missing_dates) in enumerate(sorted(missing_map.items()), 1):
                    try:
                        kline = fetch_kline(session, sym, days=days)
                        stats["fetched_symbols"] += 1
                        if kline:
                            # 仅写入缺口日期，避免无谓覆盖
                            missing_set = missing_dates
                            new_rows = [k for k in kline if k["date"] in missing_set]
                            if new_rows:
                                save_kline_to_db(conn, sym, new_rows)
                                conn.commit()
                                stats["fetched_rows"] += len(new_rows)
                            if verbose:
                                print(f"  [{i}/{len(missing_map)}] {sym}: "
                                      f"拉取 {len(kline)} 根，写入 {len(new_rows)} 根")
                        time.sleep(0.1)  # 额外节流，避免触发雪球风控
                    except Exception as e:
                        stats["api_failures"] += 1
                        if verbose:
                            print(f"  [{i}/{len(missing_map)}] {sym}: 失败 - {e}")
            finally:
                session.close()

        # 第三步：重算 outcomes
        if verbose:
            print("[backfill] 重算 recommendations.next_day_pct/fwd_3d/fwd_5d ...")
        stats["outcomes_updated"] = backfill_outcomes(conn, dry_run=False)
        if verbose:
            print(f"[backfill] 重算 {stats['outcomes_updated']} 条 recommendations")

        if verbose:
            print(f"[backfill] 完成: {stats}")
        return stats

    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(
        description="回测数据完整性补全：补全推荐历史股票的缺失 K 线，重算 next_day_pct"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="仅统计缺失，不调 API 不写 DB"
    )
    parser.add_argument(
        "--days", type=int, default=60,
        help="雪球 K 线 API 拉取天数（默认 60）"
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="静默模式，仅输出最终统计"
    )
    args = parser.parse_args()

    backfill(
        dry_run=args.dry_run,
        days=args.days,
        verbose=not args.quiet,
    )


if __name__ == "__main__":
    main()
