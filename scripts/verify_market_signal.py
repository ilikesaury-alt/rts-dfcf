"""验证市场级特征（指数/涨停）是否有增量预测力（local-only 分析脚本）。

口径与 redesign 一致：每天推荐票（去重、excluded=0）按天等权平均次日收益
（next_day_pct），关联当日 market_state 信号分档。

核心问题：redesign 已验证「池子宽度≥25」是有效 L0 信号。这里验证
「指数涨跌幅 / 涨停家数」是否在控制池宽后仍有**增量**信号——若有，
L0 应从「池子宽度」升级为「市场状态 + 池子宽度」组合；若无，则指数冗余。
"""
import sqlite3
import statistics
from collections import defaultdict

conn = sqlite3.connect("scanner.db")
conn.row_factory = sqlite3.Row

# 每天推荐票（去重、excluded=0）次日收益
rows = conn.execute(
    """
    select date, symbol, next_day_pct from recommendations
    where excluded=0 and next_day_pct is not null
      and date >= '2026-05-28' and date <= '2026-09-03'
    """
).fetchall()

day_ret: dict[str, list[float]] = defaultdict(list)
for r in rows:
    day_ret[r["date"]].append(r["next_day_pct"])

# 池子宽度（每天去重推荐数）
width: dict[str, int] = {}
for d, n in conn.execute(
    "select date, count(distinct symbol) from recommendations "
    "where excluded=0 and date>='2026-05-28' group by date"
):
    width[d] = n

ms = {r["date"]: r for r in conn.execute("select * from market_state")}


def show(title: str, items: list[tuple[str, float]]) -> None:
    if not items:
        print(f"  {title}: 无样本")
        return
    avg = statistics.fmean([v for _, v in items])
    win = 100 * sum(1 for _, v in items if v > 0) / len(items)
    print(f"  {title:26s} 天数={len(items):3d}  日均={avg:+.3f}%  日胜率={win:.1f}%")


print("=== 全样本：创业板指涨跌幅分档 ===")
b_hi, b_lo = [], []
for d, rets in day_ret.items():
    m = ms.get(d)
    if not m or m["cyb_pct"] is None:
        continue
    (b_hi if m["cyb_pct"] > 0 else b_lo).append((d, statistics.fmean(rets)))
show("创业板指 >0", b_hi)
show("创业板指 <=0", b_lo)

print("\n=== 全样本：三大指数是否均涨 ===")
allup, notall = [], []
for d, rets in day_ret.items():
    m = ms.get(d)
    if not m or None in (m["cyb_pct"], m["sh_pct"], m["sz_pct"]):
        continue
    key = m["cyb_pct"] > 0 and m["sh_pct"] > 0 and m["sz_pct"] > 0
    (allup if key else notall).append((d, statistics.fmean(rets)))
show("三指均涨", allup)
show("非三指均涨", notall)

print("\n=== 控制池子宽度后（池宽>=25 子集）：指数增量信号 ===")
hi, lo = [], []
for d, rets in day_ret.items():
    m = ms.get(d)
    if not m or d not in width or width[d] < 25 or m["cyb_pct"] is None:
        continue
    (hi if m["cyb_pct"] > 0 else lo).append((d, statistics.fmean(rets)))
show("创业板指>0 & 池宽>=25", hi)
show("创业板指<=0 & 池宽>=25", lo)

print("\n=== 控制池子宽度后（池宽<25 子集）：指数增量信号 ===")
hi2, lo2 = [], []
for d, rets in day_ret.items():
    m = ms.get(d)
    if not m or d not in width or width[d] >= 25 or m["cyb_pct"] is None:
        continue
    (hi2 if m["cyb_pct"] > 0 else lo2).append((d, statistics.fmean(rets)))
show("创业板指>0 & 池宽<25", hi2)
show("创业板指<=0 & 池宽<25", lo2)

print("\n=== 涨停家数分档（全样本，按分位 p75=98/p25=58）===")
z_hi, z_lo = [], []
for d, rets in day_ret.items():
    m = ms.get(d)
    if not m or m["limit_up"] is None:
        continue
    (z_hi if m["limit_up"] >= 90 else z_lo).append((d, statistics.fmean(rets)))
show("涨停>=90(热)", z_hi)
show("涨停<60(冷)", z_lo)

print("\n=== 池宽>=25 子集：涨停增量信号 ===")
zh, zl = [], []
for d, rets in day_ret.items():
    m = ms.get(d)
    if not m or d not in width or width[d] < 25 or m["limit_up"] is None:
        continue
    (zh if m["limit_up"] >= 90 else zl).append((d, statistics.fmean(rets)))
show("池宽>=25 & 涨停>=90", zh)
show("池宽>=25 & 涨停<60", zl)
