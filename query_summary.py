import sqlite3, json
import sys
from datetime import date, timedelta

sys.stdout.reconfigure(encoding='utf-8')

conn = sqlite3.connect('scanner.db')
target_date = (date.today() - timedelta(days=1)).isoformat()

cur = conn.execute("SELECT DISTINCT r.symbol, r.name FROM recommendations r WHERE r.date = ? ORDER BY r.symbol", (target_date,))
stocks = cur.fetchall()
print(f'今日上榜个股: {len(stocks)} 只\n')

cur = conn.execute("""SELECT r.symbol, r.name, r.category, MAX(r.score), r.score_breakdown, r.percent
FROM recommendations r WHERE r.date = ? GROUP BY r.symbol, r.category ORDER BY MAX(r.score) DESC""", (target_date,))
rows = cur.fetchall()
print('=== 各股最高分（按评分降序）===')
for r in rows:
    dims = {}
    if r[4] and str(r[4]).strip():
        try: dims = json.loads(r[4])
        except Exception as e: dims = {"parse_error": str(e)}
    rc = dims.get('momentum_rank_change') or dims.get('new_face_rank_change', '')
    val = dims.get('momentum_value') or dims.get('new_face_value', '')
    gentle = dims.get('momentum_gentle_breakout') or dims.get('new_face_gentle_breakout', '')
    s = r[3] if isinstance(r[3], int) else 0
    print(f'  {r[0]:10s} {r[1]:8s} {r[2]:8s} score={s:3d} pct={r[5]:+.2f} rc={rc} val={val} gentle={gentle}')

print()

cur = conn.execute("SELECT r.category, COUNT(DISTINCT r.symbol) FROM recommendations r WHERE r.date = ? GROUP BY r.category", (target_date,))
cats = cur.fetchall()
print('=== 策略分布 ===')
for c in cats:
    print(f'  {c[0]}: {c[1]} 只')

print()

cur = conn.execute("""SELECT r.symbol, r.name, MAX(r.score), r.percent, r.score_breakdown
FROM recommendations r WHERE r.date = ? AND r.category='momentum'
GROUP BY r.symbol ORDER BY MAX(r.score) DESC LIMIT 10""", (target_date,))
print('=== 动量 Top 10 ===')
for r in cur:
    dims = json.loads(r[4]) if r[4] else {}
    gentle = dims.get('momentum_gentle_breakout', 0)
    print(f'  {r[0]:10s} {r[1]:8s} score={r[2]:3d} pct={r[3]:+.1f}% gentle={gentle}')

print()

cur = conn.execute("""SELECT r.symbol, r.name, MAX(r.score), r.percent, r.score_breakdown
FROM recommendations r WHERE r.date = ? AND r.category='new_face'
GROUP BY r.symbol ORDER BY MAX(r.score) DESC LIMIT 10""", (target_date,))
print('=== 新面孔 Top 10 ===')
for r in cur:
    dims = json.loads(r[4]) if r[4] else {}
    gentle = dims.get('new_face_gentle_breakout', 0)
    print(f'  {r[0]:10s} {r[1]:8s} score={r[2]:3d} pct={r[3]:+.1f}% gentle={gentle}')

print()

cur = conn.execute("SELECT r.time, COUNT(*) FROM recommendations r WHERE r.date = ? GROUP BY r.time ORDER BY r.time", (target_date,))
cycles = cur.fetchall()
print('=== 扫描轮次 ===')
for c in cycles:
    print(f'  {c[0]} → {c[1]} 条')

print()

# Gentle breakout candidates that would qualify
cur = conn.execute("""SELECT r.symbol, r.name, r.time, r.score, r.score_breakdown
FROM recommendations r WHERE r.date = ?
AND r.score_breakdown IS NOT NULL AND r.score_breakdown != ''
ORDER BY r.symbol, r.time""", (target_date,))
print('=== 哪些票符合温和启动条件？===')
# simulate gentle_breakout check
for r in cur:
    dims = json.loads(r[4]) if r[4] else {}
    rc_raw = dims.get('momentum_rank_change', 0) or dims.get('new_face_rank_change', 0)
    if isinstance(rc_raw, int) and 5 <= rc_raw <= 10:
        cat = 'momentum' if 'momentum_rank_change' in dims else 'new_face'
        candle = dims.get('momentum_candle', 0) or dims.get('new_face_candle', 0)
        val = dims.get('momentum_value', 0) or dims.get('new_face_value', 0)
        vol = dims.get('momentum_volume', 0) or dims.get('new_face_volume', 0)
        if candle >= 3 and val == 0 and vol == 10:
            print(f'  ✅ {r[0]:10s} {r[1]:8s} {cat:8s} score={r[3]:3d} rc={rc_raw} candle={candle} val={val} vol={vol}')
        else:
            print(f'  ❌ {r[0]:10s} {r[1]:8s} {cat:8s} score={r[3]:3d} rc={rc_raw} candle={candle} val={val} vol={vol}')

conn.close()
