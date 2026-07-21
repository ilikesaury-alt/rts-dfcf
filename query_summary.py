import argparse
import json
import sqlite3
import sys
from datetime import timedelta

sys.stdout.reconfigure(encoding='utf-8')

parser = argparse.ArgumentParser(description='查询推荐汇总')
parser.add_argument('--date', default=None, help='目标日期 (YYYY-MM-DD)，默认为昨日')
args = parser.parse_args()

from scanner.config import DB_PATH, now_beijing

conn = sqlite3.connect(DB_PATH)
target_date = args.date or (now_beijing().date() - timedelta(days=1)).isoformat()

cur = conn.execute("SELECT DISTINCT r.symbol, r.name FROM recommendations r WHERE r.date = ? ORDER BY r.symbol", (target_date,))
stocks = cur.fetchall()
print(f'{target_date} 上榜个股: {len(stocks)} 只\n')

cur = conn.execute("""SELECT r.symbol, r.name, r.category, MAX(r.score), r.score_breakdown, r.percent
FROM recommendations r WHERE r.date = ? GROUP BY r.symbol, r.category ORDER BY MAX(r.score) DESC""", (target_date,))
rows = cur.fetchall()
print('=== 各股最高分（按评分降序）===')
for r in rows:
    dims = {}
    if r[4] and str(r[4]).strip():
        try:
            dims = json.loads(r[4])
        except Exception as e:
            dims = {"parse_error": str(e)}
    s = r[3] if isinstance(r[3], int) else 0
    pct = r[5] if r[5] is not None else 0.0
    print(f'  {r[0]:10s} {r[1]:8s} {r[2]:8s} score={s:3d} pct={pct:+.2f}')

print()

cur = conn.execute("SELECT r.category, COUNT(DISTINCT r.symbol) FROM recommendations r WHERE r.date = ? GROUP BY r.category", (target_date,))
cats = cur.fetchall()
print('=== 策略分布 ===')
for c in cats:
    print(f'  {c[0]}: {c[1]} 只')

print()

cur = conn.execute("""SELECT r.symbol, r.name, MAX(r.score), r.percent
FROM recommendations r WHERE r.date = ? AND r.category='momentum'
GROUP BY r.symbol ORDER BY MAX(r.score) DESC LIMIT 10""", (target_date,))
print('=== 动量 Top 10 ===')
for r in cur:
    pct = r[3] if r[3] is not None else 0.0
    print(f'  {r[0]:10s} {r[1]:8s} score={r[2]:3d} pct={pct:+.1f}%')

print()

cur = conn.execute("""SELECT r.symbol, r.name, MAX(r.score), r.percent
FROM recommendations r WHERE r.date = ? AND r.category='new_face'
GROUP BY r.symbol ORDER BY MAX(r.score) DESC LIMIT 10""", (target_date,))
print('=== 新面孔 Top 10 ===')
for r in cur:
    pct = r[3] if r[3] is not None else 0.0
    print(f'  {r[0]:10s} {r[1]:8s} score={r[2]:3d} pct={pct:+.1f}%')

print()

cur = conn.execute("SELECT r.time, COUNT(*) FROM recommendations r WHERE r.date = ? GROUP BY r.time ORDER BY r.time", (target_date,))
cycles = cur.fetchall()
print('=== 扫描轮次 ===')
for c in cycles:
    print(f'  {c[0]} → {c[1]} 条')

conn.close()
