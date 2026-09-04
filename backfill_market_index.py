"""大盘基准回填脚本（P0-1）：为历史归因补上市场 beta 基准。

问题背景：
    market_index_log 表建于 2026-08-19，此前 68 个交易日（2026-05-28 ~ 08-18）
    没有任何基准记录。后果是所有历史归因（nextday_attribution / backtest /
    config.py 里几十个"基于 N 样本校准"的阈值）用的都是**未剔除大盘涨跌的原始
    收益**——例如 7 月推荐池日均 -1.573%，其中很大一部分是市场 beta，却被当成
    因子特性拟合进了阈值。没有基准就无法区分"选股选得差"和"那天大盘在跌"。

数据源：
    雪球 kline 接口，symbol=SZ399006（创业板指），与线上 scanner 同源
    （scanner.api.fetch_market_index）。日线 item[7] 为当日涨跌幅，
    与 daily_kline.percent / recommendations.next_day_pct 口径一致
    （均为该交易日自身的涨跌幅，可相减得超额收益）。

使用方式：
    python backfill_market_index.py                  # 回填缺失日期（默认）
    python backfill_market_index.py --dry-run        # 仅预览，不写 DB
    python backfill_market_index.py --overwrite-live # 用收盘定值覆盖线上盘中快照
    python backfill_market_index.py --days 200       # 自定义回溯天数

语义约定（重要）：
    market_index_log 的主用途是**线上血缘审计**——记录"本轮扫描当时实际读到的
    大盘涨幅是多少"，用以事后追责（曾发生把当日 -6.26% 崩盘读成昨日 -0.93%
    而无痕的事故）。因此：

    - 本脚本写入的行 source='backfill'，明确标识为**收盘定值**、非线上快照，
      与 source='xueqiu'/'akshare' 的线上行语义不同，便于 data_health 对账区分。
    - 默认 INSERT OR IGNORE，**不覆盖**已存在的线上行（保住审计证据）。
    - 对线上盘中值与收盘定值差异 > 0.3pp 的日期，脚本会 fail-loud 列出告警，
      由人决定是否用 --overwrite-live 统一为收盘定值。

    做归因分析时建议统一用收盘定值口径（否则同一序列里混着盘中瞬时值与收盘值，
    语义不一致）。差异通常 < 0.1pp，但对 13 天线上行仍建议用 --overwrite-live 对齐。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

from scanner.api import make_session
from scanner.config import DB_PATH, now_beijing

INDEX_SYMBOL = "SZ399006"  # 创业板指
BACKFILL_SOURCE = "backfill"
# 盘中快照与收盘定值的差异告警阈值（百分点）
LIVE_DRIFT_WARN_PP = 0.3


def fetch_index_history(days: int) -> dict[str, float]:
    """拉取创业板指日线序列 {YYYY-MM-DD: 当日涨跌幅%}。

    返回空 dict 表示拉取失败（调用方应视为致命错误，不静默继续）。
    """
    bj = timezone(timedelta(hours=8))
    session = make_session()
    now_ms = int(time.time() * 1000)
    begin_ms = now_ms - days * 86400 * 1000
    url = (
        f"https://stock.xueqiu.com/v5/stock/chart/kline.json"
        f"?symbol={INDEX_SYMBOL}&begin={begin_ms}&period=day&count={days}&_={now_ms}"
    )
    resp = session.get(url, timeout=20)
    resp.raise_for_status()
    items = ((resp.json() or {}).get("data") or {}).get("item") or []

    out: dict[str, float] = {}
    for it in items:
        if not it or len(it) <= 7:
            continue
        pct = it[7]
        if pct is None:
            continue
        d = datetime.fromtimestamp(it[0] / 1000, tz=bj).strftime("%Y-%m-%d")
        try:
            out[d] = float(pct)
        except (TypeError, ValueError):
            continue  # 脏值剔除（与 _num 同族防御）
    return out


def _recommendation_dates(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT date FROM recommendations ORDER BY date"
    ).fetchall()
    return [r[0] for r in rows]


def main() -> int:
    ap = argparse.ArgumentParser(description="回填 market_index_log 历史基准")
    ap.add_argument("--days", type=int, default=200, help="回溯日历天数（默认 200）")
    ap.add_argument("--dry-run", action="store_true", help="仅预览，不写 DB")
    ap.add_argument(
        "--overwrite-live",
        action="store_true",
        help="用收盘定值覆盖已存在的线上盘中快照（默认跳过保住审计证据）",
    )
    ap.add_argument("--db", default=DB_PATH, help=f"DB 路径（默认 {DB_PATH}）")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS market_index_log (
            date TEXT PRIMARY KEY,
            time TEXT,
            index_pct REAL,
            bar_date TEXT,
            source TEXT,
            updated TEXT DEFAULT ''
        )
        """
    )

    print(f"[1/4] 拉取 {INDEX_SYMBOL} 日线（回溯 {args.days} 天）…")
    try:
        series = fetch_index_history(args.days)
    except Exception as e:  # noqa: BLE001  取数失败即终止，不做部分回填
        print(f"  拉取失败：{type(e).__name__}: {e}")
        print("  未写入任何数据。请检查网络/雪球登录态后重试。")
        return 1
    print(f"  取得 {len(series)} 根 bar"
          f"（{min(series) if series else '-'} ~ {max(series) if series else '-'}）")
    if not series:
        return 1

    rec_dates = _recommendation_dates(conn)
    print(f"[2/4] recommendations 覆盖 {len(rec_dates)} 个交易日"
          f"（{rec_dates[0] if rec_dates else '-'} ~ {rec_dates[-1] if rec_dates else '-'}）")

    existing = {
        r[0]: r[1]
        for r in conn.execute("SELECT date, source FROM market_index_log")
    }
    missing = [d for d in rec_dates if d not in existing]
    no_index_bar = [d for d in rec_dates if d not in series]

    print(f"[3/4] 已有基准 {len(existing)} 天，待回填 {len(missing)} 天")
    if no_index_bar:
        print(f"  警告：{len(no_index_bar)} 个推荐日在指数序列里无对应 bar，"
              f"将留空（前 5 个：{', '.join(no_index_bar[:5])}）")

    # 盘中快照 vs 收盘定值 偏差告警（fail-loud）
    drift = []
    for d, src in existing.items():
        if src == BACKFILL_SOURCE or d not in series:
            continue
        row = conn.execute(
            "SELECT index_pct FROM market_index_log WHERE date=?", (d,)
        ).fetchone()
        if not row or row[0] is None:
            continue
        delta = abs(float(row[0]) - series[d])
        if delta > LIVE_DRIFT_WARN_PP:
            drift.append((d, float(row[0]), series[d], delta))
    if drift:
        print(f"  注意：{len(drift)} 天的线上盘中值与收盘定值偏差 > {LIVE_DRIFT_WARN_PP}pp：")
        for d, live, final, delta in drift[:8]:
            print(f"    {d}  线上 {live:+.2f}%  收盘 {final:+.2f}%  差 {delta:.2f}pp")
        print("    归因口径建议统一为收盘定值：重跑加 --overwrite-live")

    if args.dry_run:
        print("[4/4] --dry-run：未写入 DB")
        return 0

    now = now_beijing().isoformat(timespec="seconds")
    written = 0
    for d in rec_dates:
        if d not in series:
            continue
        if d in existing and not args.overwrite_live:
            continue
        conn.execute(
            """
            INSERT INTO market_index_log (date, time, index_pct, bar_date, source, updated)
            VALUES (?, '15:00:00', ?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                index_pct=excluded.index_pct,
                bar_date=excluded.bar_date,
                source=excluded.source,
                updated=excluded.updated
            """,
            (d, series[d], d, BACKFILL_SOURCE, now),
        )
        written += 1
    conn.commit()

    total = conn.execute("SELECT COUNT(*) FROM market_index_log").fetchone()[0]
    print(f"[4/4] 写入 {written} 行，market_index_log 现有 {total} 行")

    still_missing = [
        d for d in rec_dates
        if conn.execute(
            "SELECT 1 FROM market_index_log WHERE date=? AND index_pct IS NOT NULL", (d,)
        ).fetchone() is None
    ]
    if still_missing:
        print(f"  仍缺基准 {len(still_missing)} 天：{', '.join(still_missing[:10])}")
        print("  这些日期无法计算超额收益，归因时应显式剔除而非按 0 处理。")
    else:
        print("  推荐日基准覆盖 100%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
