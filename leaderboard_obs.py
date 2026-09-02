import argparse
import sqlite3
import sys

sys.stdout.reconfigure(encoding='utf-8')

parser = argparse.ArgumentParser(
    description='榜单可观测性：雪球飙升榜/热搜榜成分与排名分布时间序列')
parser.add_argument('--date', default=None,
                    help='目标日期 (YYYY-MM-DD)，默认今日（盘中有则看盘中，无则看最近有数据的一天）')
parser.add_argument('--days', type=int, default=7, help='分日汇总回看天数（默认7）')
parser.add_argument('--source', default='biaosheng', choices=['biaosheng', 'hot'],
                    help='数据源，默认飙升榜')
args = parser.parse_args()

from scanner.config import DB_PATH, now_beijing  # noqa: E402
from scanner.display import clear_screen  # noqa: E402

clear_screen()
conn = sqlite3.connect(DB_PATH, timeout=10.0)
source = args.source
src_label = '飙升榜' if source == 'biaosheng' else '热搜榜'

# 表由扫描器 init_db 在下次启动时创建（CREATE TABLE IF NOT EXISTS）。工具读不到时
# 给指引而不是抛 SQL 异常；也可由工具自身补建表以便先跑（幂等）。
has_table = conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table' AND name='leaderboard_log'"
).fetchone()
if not has_table:
    print('leaderboard_log 表尚未创建。')
    print('请先启动一次扫描器（python unified_scanner.py），init_db 会自动建表；')
    print('或在交易时段跑一轮后数据才会落库。')
    conn.close()
    sys.exit(0)


def fmt(v, nd=2):
    if v is None:
        return '  -  '
    if isinstance(v, float):
        return f'{v:.{nd}f}'
    return f'{v}'


def detect_anomalies(conn, source, target):
    """与昨日（或最近一个有数据的交易日）对比，识别口径/结构突变信号。"""
    signs = []
    row = conn.execute(
        """SELECT median_pct, up_count, down_count, total, gem_listed, overlap_prev
           FROM leaderboard_log WHERE date=? AND source=? ORDER BY time DESC LIMIT 1""",
        (target, source)).fetchone()
    if not row:
        return signs
    med, _up, _down, total, _gem, ov = row
    prev = conn.execute(
        """SELECT date, median_pct, up_count, down_count, total, gem_listed
           FROM leaderboard_log WHERE source=? AND date<? ORDER BY date DESC LIMIT 1""",
        (source, target)).fetchone()
    if not prev:
        return signs
    pdate, pmed, _pup, _pdown, ptot, _pgem = prev
    if med is not None and pmed is not None and abs(med - pmed) >= 1.0:
        signs.append(
            f'{target} 榜单中位涨幅 {med:.2f}% vs 昨日 {pdate} {pmed:.2f}%，'
            f'突变≥1pp，检查是否上游排序口径变更')
    if ov is not None and ov < 0.3:
        signs.append(f'{target} 最近一轮重叠率仅 {ov:.0%}，'
                     f'榜单成员剧烈抖动（分页/排序不稳）')
    if total and ptot and abs(total - ptot) / ptot >= 0.3:
        signs.append(f'{target} 榜单条数 {total} vs 昨日 {ptot}，'
                     f'变动≥30%（样本过滤条件疑似变更）')
    return signs


def find_date():
    if args.date:
        return args.date
    today = now_beijing().date().isoformat()
    row = conn.execute(
        "SELECT date FROM leaderboard_log WHERE source=? AND date=? LIMIT 1",
        (source, today)).fetchone()
    if row:
        return today
    # 盘中还没数据 → 回退最近有数据的一天
    row = conn.execute(
        "SELECT MAX(date) FROM leaderboard_log WHERE source=?", (source,)).fetchone()
    return row[0] if row and row[0] else today


print('=' * 78)
print(f'榜单可观测性 · {source} ({src_label})')
print('=' * 78)

# ---------- 今日 / 目标日盘中时序 ----------
target = find_date()
rows = conn.execute(
    """SELECT time, total, gem_listed, up_count, down_count, flat_count,
              median_pct, mean_pct, top10_mean_pct, max_pct, overlap_prev, median_rank_change
       FROM leaderboard_log WHERE date=? AND source=? ORDER BY time""",
    (target, source)).fetchall()
if rows:
    print(f'\n■ {target} 盘中时序（共 {len(rows)} 轮扫描，约每60s一轮，此处采样展示）\n')
    print(f'{"时间":<8}{"条数":>5}{"GEM":>5}{"涨":>4}{"跌":>4}'
          f'{"中位%":>7}{"前10均%":>8}{"重叠率":>7}{"中位换位":>8}')
    step = max(1, len(rows) // 12)
    for i, r in enumerate(rows):
        if i % step != 0 and i != len(rows) - 1:
            continue
        t, total, gem, up, down, flat, med, _mean, top10, mx, ov, rc = r
        print(f'{t:<8}{total:>5}{gem:>5}{up:>4}{down:>4}'
              f'{fmt(med):>7}{fmt(top10):>8}{fmt(ov, 2):>7}{fmt(rc):>8}')
    # 异常信号检测：与昨日同源对比
    print('\n■ 口径漂移检测')
    signs = detect_anomalies(conn, source, target)
    if signs:
        for s in signs:
            print('   ⚠ ' + s)
    else:
        print('   ✓ 未检出明显上游口径漂移')
else:
    print(f'\n{target} 无 {source} 数据'
          f'（盘中扫描时间序列逐轮落库，需启动扫描器并交易时段运行）')

# ---------- 分日汇总 ----------
print(f'\n■ 近 {args.days} 日逐日汇总（{src_label}）\n')
print(f'{"日期":<12}{"扫描轮数":>7}{"均条数":>7}{"均中位%":>8}'
      f'{"涨/跌":>8}{"前10均%":>8}{"均重叠率":>8}')
day_rows = conn.execute(
    """SELECT date,
              COUNT(*) AS scans,
              ROUND(AVG(total),1), ROUND(AVG(median_pct),2),
              ROUND(AVG(up_count)), ROUND(AVG(down_count)),
              ROUND(AVG(top10_mean_pct),2), ROUND(AVG(overlap_prev),3)
       FROM leaderboard_log WHERE source=? GROUP BY date ORDER BY date DESC LIMIT ?""",
    (source, args.days)).fetchall()
for d, scans, tot, med, up, down, top10, ov in day_rows:
    print(f'{d:<12}{scans:>7}{tot:>7}{fmt(med):>8}'
          f'{f"{int(up or 0)}/{int(down or 0)}":>8}{fmt(top10):>8}{fmt(ov, 3):>8}')

# ---------- 成分持久性（近 N 日 + 今日） ----------
print('\n■ 今日榜单成分持久性（跨轮重叠率）')
print('   说明：跨轮重叠率低 = 榜单剧烈抖动（上游排序/分页不稳）')
if rows:
    avg_ov = sum(r[10] or 0 for r in rows) / max(1, len(rows))
    tag = '稳定' if avg_ov > 0.6 else '抖动明显'
    print(f'   今日平均重叠率 {avg_ov:.2%}（{tag}）')

conn.close()
