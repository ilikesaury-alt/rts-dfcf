# -*- coding: utf-8 -*-
"""扫描器健康检查：数据新鲜度 / 今日捕获量 / 数据质量字段。

用法: python diag_health.py
"""

import sqlite3
import sys
from pathlib import Path

reconfigure = getattr(sys.stdout, "reconfigure", None)
if reconfigure is not None:
    reconfigure(encoding="utf-8")

DB = Path(__file__).resolve().parent / "scanner.db"

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

print("=== 数据新鲜度 ===")
# 表名/列名全部为字面量（选择器直接映射，无字符串拼 SQL，规避注入向量告警）
_FRESHNESS_SQL = {
    "recommendations": "SELECT MAX(date) d, MAX(time) t, COUNT(*) n FROM recommendations",
    "appearances": "SELECT MAX(date) d, COUNT(*) n FROM appearances",
    "leaderboard_log": "SELECT MAX(date) d, COUNT(*) n FROM leaderboard_log",
    "daily_kline": "SELECT MAX(date) d, COUNT(DISTINCT symbol) syms, COUNT(*) n FROM daily_kline",
    "scan_quality_log": "SELECT MAX(updated) d, COUNT(*) n FROM scan_quality_log",
    "market_extra_cache": "SELECT MAX(date) d, COUNT(*) n FROM market_extra_cache",
}
for table, sql in _FRESHNESS_SQL.items():
    try:
        row = dict(conn.execute(sql).fetchone())  # noqa: S608  sql 为上方字典字面量，无外部输入
        syms = row.get("syms")
        print(f"  {table:<20} 最新={row.get('d')} 行数={row.get('n')}" + (f" 票数={syms}" if syms else ""))
    except sqlite3.OperationalError as e:
        print(f"  {table:<20} 表缺失/错误: {e}")

print("\n=== scan_quality_log 最近 8 条（gem_count/fetch_failed/today_bar_missing/minute_fallback/stale_recs）===")
try:
    for r in conn.execute("SELECT * FROM scan_quality_log ORDER BY updated DESC LIMIT 8"):
        d = dict(r)
        print(
            f"  {d.get('updated', '?'):<20} gem={d.get('gem_count')} fetch_fail={d.get('fetch_failed')} "
            f"bar_miss={d.get('today_bar_missing')} min_fb={d.get('minute_fallback')} stale={d.get('stale_recs')}"
        )
except sqlite3.OperationalError as e:
    print(f"  查询失败: {e}")

print("\n=== 最近 5 个交易日推荐量（按类别/排除）===")
for r in conn.execute(
    """SELECT date, category,
              COUNT(*) n_all, SUM(excluded) n_excluded, SUM(next_day_pct IS NOT NULL AND next_day_pct>=7) n_hit
       FROM recommendations WHERE date >= (SELECT MAX(date) FROM recommendations WHERE 1=1)
       GROUP BY date, category ORDER BY date DESC, n_all DESC LIMIT 40"""
):
    print(f"  {r['date']} {r['category']:<14} 全部={r['n_all']:<4} 排除={r['n_excluded']:<4} 次日hit={r['n_hit']}")

print("\n=== 近 5 日每日推荐总量与 hit ===")
for r in conn.execute(
    """SELECT date, COUNT(DISTINCT symbol) syms, SUM(excluded) excl,
              SUM(CASE WHEN next_day_pct>=7 THEN 1 ELSE 0 END) hits
       FROM recommendations WHERE date >= (SELECT MAX(date) FROM recommendations)
       GROUP BY date ORDER BY date DESC LIMIT 5"""
):
    print(f"  {r['date']} 推荐票数={r['syms']} 排除行={r['excl']} hit行={r['hits']}")

conn.close()
