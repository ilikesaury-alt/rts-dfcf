import argparse
import json
import sqlite3
import sys
from datetime import date, timedelta

sys.stdout.reconfigure(encoding='utf-8')

parser = argparse.ArgumentParser(description='查询今日推荐')
parser.add_argument('--date', default=None, help='目标日期 (YYYY-MM-DD)，默认为昨日')
args = parser.parse_args()

from scanner.config import DB_PATH

conn = sqlite3.connect(DB_PATH)
target_date = args.date or (date.today() - timedelta(days=1)).isoformat()
# Check columns
cur = conn.execute("PRAGMA table_info(recommendations)")
cols = cur.fetchall()
col_names = [c[1] for c in cols]
print('columns:', col_names)
cur = conn.execute("SELECT * FROM recommendations r WHERE r.date = ? ORDER BY r.score DESC", (target_date,))
rows = cur.fetchall()
cur.close()
conn.close()
print(f'今日推荐: {len(rows)} 条')
print()
for r in rows:
    dims = {}
    idx = col_names.index('score_breakdown')
    if r[idx] and str(r[idx]).strip():
        try:
            dims = json.loads(r[idx])
        except Exception as e:
            dims = {"parse_error": str(e)}
    sym_idx = col_names.index('symbol')
    sc_idx = col_names.index('score')
    cat_idx = col_names.index('category')
    tr_idx = col_names.index('trend')
    print(f'{r[sym_idx]:10s} {r[cat_idx]:8s} score={r[sc_idx]:3d} trend={r[tr_idx]!r}')
    for k, v in sorted(dims.items()):
        print(f'    {k}: {v}')
    print()
