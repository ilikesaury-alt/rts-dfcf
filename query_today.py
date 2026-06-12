import sqlite3, json
import sys
sys.stdout.reconfigure(encoding='utf-8')
conn = sqlite3.connect('scanner.db')
# Check columns
cur = conn.execute("PRAGMA table_info(recommendations)")
cols = cur.fetchall()
col_names = [c[1] for c in cols]
print('columns:', col_names)
cur = conn.execute("SELECT * FROM recommendations r WHERE r.date = '2026-06-11' ORDER BY r.score DESC")
rows = cur.fetchall()
cur.close()
conn.close()
print(f'今日推荐: {len(rows)} 条')
print()
for r in rows:
    dims = {}
    idx = col_names.index('score_breakdown')
    if r[idx] and str(r[idx]).strip():
        try: dims = json.loads(r[idx])
        except: pass
    sym_idx = col_names.index('symbol')
    sc_idx = col_names.index('score')
    cat_idx = col_names.index('category')
    tr_idx = col_names.index('trend')
    print(f'{r[sym_idx]:10s} {r[cat_idx]:8s} score={r[sc_idx]:3d} trend={r[tr_idx]!r}')
    for k, v in sorted(dims.items()):
        print(f'    {k}: {v}')
    print()
