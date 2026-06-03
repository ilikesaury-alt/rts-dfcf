import sqlite3, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

conn = sqlite3.connect('scanner.db')
cur = conn.cursor()

cur.execute('SELECT * FROM appearances WHERE symbol=? ORDER BY date', ('SZ300085',))
rows = cur.fetchall()
print(f'appearances ({len(rows)} rows)')
for r in rows:
    print(f'  {r[3]} rank={r[4]} pct={r[5]:+.2f}% val={r[6]}')

cur.execute('SELECT * FROM recommendations WHERE symbol=? ORDER BY date DESC, time DESC LIMIT 5', ('SZ300085',))
rows = cur.fetchall()
print(f'\nrecommendations ({len(rows)} rows)')
for r in rows:
    print(f'  {r[1]} {r[2]} cat={r[5]} score={r[6]} pct={r[7]} trend={r[8]}')

cur.execute('SELECT date, open, high, low, close, percent, volume FROM daily_kline WHERE symbol=? ORDER BY date', ('SZ300085',))
rows = cur.fetchall()
print(f'\ndaily_kline ({len(rows)} rows)')
for r in rows[-30:]:
    print(f'  {r[0]} O={r[1]:.2f} H={r[2]:.2f} L={r[3]:.2f} C={r[4]:.2f} {r[5]:+.2f}% V={r[6]:.0f}')

conn.close()
