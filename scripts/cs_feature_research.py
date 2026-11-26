"""横截面信号离线特征研究（只读库、不给交易结论）。

口径纪律：
- 按 (date, symbol) 聚合：同票当日多轮 → 取该票**末轮**（round_seq 最大），
  避免同一行情事实被重复计数；persist 从当日行数派生。
- 次日标签：next_day_pct（收盘→次一交易日收盘，%），目标按项目唯一口径 ≥7% hit。
- 日内分位（cs_rank）：每日内特征排名/当日样本数，控制不同日市场整体差异。
- 输出：各特征的分层 hit 率 / 平均次日收益 / Spearman IC，不做多空组合、不给建议。
"""
from __future__ import annotations

import math
import sqlite3
from collections import defaultdict

DB = "scanner.db"


def load_rows() -> list[dict]:
    conn = sqlite3.connect(DB, timeout=15)
    conn.row_factory = sqlite3.Row
    # 只取有标签的；按 (date,symbol) 分组，用窗口函数取末轮 + persist
    sql = """
    WITH ranked AS (
      SELECT *, ROW_NUMBER() OVER (PARTITION BY date,symbol ORDER BY round_seq DESC) rn,
             COUNT(*) OVER (PARTITION BY date,symbol) cnt
      FROM crosssection_signal WHERE next_day_pct IS NOT NULL
    )
    SELECT * FROM ranked WHERE rn=1
    """
    out = [dict(r) for r in conn.execute(sql)]
    conn.close()
    return out


def cs_rank(values: list[float]) -> list[float | None]:
    """日内百分位（0~1，越大越高）；缺值 None。"""
    idx = [(v, i) for i, v in enumerate(values) if isinstance(v, (int, float)) and math.isfinite(v)]
    ranks: list[float | None] = [None] * len(values)
    if not idx:
        return ranks
    idx.sort(key=lambda x: x[0])
    n = len(idx)
    for pos, (_, i) in enumerate(idx):
        ranks[i] = pos / max(n - 1, 1)
    return ranks


def add_features(rows: list[dict]) -> list[dict]:
    by_day: dict[str, list[int]] = defaultdict(list)
    for i, r in enumerate(rows):
        by_day[r["date"]].append(i)
    feats = ["percent", "speed_1", "speed_3", "speed_5", "amount_surge", "vol_ratio", "pct_rank", "main_net"]
    for _day, idxs in by_day.items():
        for f in feats:
            vals = [rows[i].get(f) for i in idxs]
            rk = cs_rank(vals)
            for i, v in zip(idxs, rk, strict=False):
                rows[i][f + "_rank"] = v
        # persist = 当日行数（去重后 cnt 已在 SQL 给出）
        for i in idxs:
            r = rows[i]
            r["persist"] = r.get("cnt", 1)
            s1, s3, s5 = r.get("speed_1"), r.get("speed_3"), r.get("speed_5")
            nums = [x for x in (s1, s3, s5) if isinstance(x, (int, float)) and math.isfinite(x)]
            r["cons_pos"] = len(nums) == 3 and all(x > 0 for x in nums)
            r["cons_neg"] = len(nums) == 3 and all(x < 0 for x in nums)
            r["amount_surge_x_speed"] = (
                r["amount_surge"] * max(s1 or 0.0, 0.0)
                if isinstance(r.get("amount_surge"), (int, float)) and isinstance(s1, (int, float))
                else None
            )
    return rows


def spearman(xs: list[float], ys: list[float]) -> float | None:
    def rank(vals):
        idx = sorted(range(len(vals)), key=lambda i: vals[i])
        r = [0.0] * len(vals)
        j = 0
        while j < len(idx):
            k = j
            while k + 1 < len(idx) and vals[idx[k + 1]] == vals[idx[j]]:
                k += 1
            avg = (j + k) / 2 + 1
            for t in range(j, k + 1):
                r[idx[t]] = avg
            j = k + 1
        return r
    rx, ry = rank(xs), rank(ys)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=False))
    dx = sum((a - mx) ** 2 for a in rx) ** 0.5
    dy = sum((b - my) ** 2 for b in ry) ** 0.5
    return num / (dx * dy) if dx and dy else None


def report(rows: list[dict], key: str, label: str, bins: int = 5):
    valid = [r for r in rows if isinstance(r.get(key), (int, float)) and math.isfinite(r[key])]
    if not valid:
        return
    base_hit = sum(r["next_day_pct"] >= 7 for r in rows) / len(rows) * 100
    base_avg = sum(r["next_day_pct"] for r in rows) / len(rows)
    ic = spearman([r[key] for r in valid], [r["next_day_pct"] for r in valid])
    print(f"\n=== {label} ({key}) | IC={ic:+.3f} | 覆盖率 {len(valid)}/{len(rows)} ===")
    print(f"  基准: hit≥7% {base_hit:.2f}% | 平均次日 {base_avg:+.2f}%")
    valid.sort(key=lambda r: r[key])
    for b in range(bins):
        chunk = valid[b * len(valid) // bins:(b + 1) * len(valid) // bins]
        hit = sum(r["next_day_pct"] >= 7 for r in chunk) / len(chunk) * 100
        avg = sum(r["next_day_pct"] for r in chunk) / len(chunk)
        lo, hi = chunk[0][key], chunk[-1][key]
        print(f"  Q{b+1} [{lo:>9.3f},{hi:>9.3f}] n={len(chunk):5d} hit={hit:5.2f}% avg={avg:+6.2f}%")


def main():
    rows = load_rows()
    print(f"样本: {len(rows)} 个 (date,symbol) 对 | 日期 {min(r['date'] for r in rows)} ~ {max(r['date'] for r in rows)}")
    print(f"全局基准: hit≥7% {sum(r['next_day_pct']>=7 for r in rows)/len(rows)*100:.2f}% | "
          f"avg {sum(r['next_day_pct'] for r in rows)/len(rows):+.2f}% | "
          f"hit≥5% {sum(r['next_day_pct']>=5 for r in rows)/len(rows)*100:.2f}%")
    rows = add_features(rows)

    # 连续特征（绝对值 + 日内分位）
    for key, label in [
        ("percent_rank", "当日涨幅·分位"),
        ("speed_1_rank", "1轮涨速·分位"),
        ("speed_3_rank", "3轮涨速·分位"),
        ("speed_5_rank", "5轮涨速·分位"),
        ("amount_surge_rank", "额涌·分位"),
        ("vol_ratio_rank", "量比·分位"),
        ("pct_rank", "横截面分位"),
        ("main_net_rank", "主力净额·分位"),
        ("persist", "持续轮次"),
        ("price", "股价"),
    ]:
        report(rows, key, label)
    # 分节/画像布尔特征
    print("\n=== 分类特征 ===")
    for flag, label in [("cons_pos", "三窗全正"), ("cons_neg", "三窗全负")]:
        yes = [r for r in rows if r[flag]]
        no = [r for r in rows if not r[flag]]
        hy = sum(r["next_day_pct"] >= 7 for r in yes) / len(yes) * 100
        hn = sum(r["next_day_pct"] >= 7 for r in no) / len(no) * 100
        ay = sum(r["next_day_pct"] for r in yes) / len(yes)
        an = sum(r["next_day_pct"] for r in no) / len(no)
        print(f"  {label}: 是 n={len(yes)} hit={hy:.2f}% avg={ay:+.2f}% | 否 n={len(no)} hit={hn:.2f}% avg={an:+.2f}%")
    # 位置档
    print("\n=== 位置档（按项目阈值）===")
    bins = [("蓄势<3%", lambda p: p < 3), ("启动3~7%", lambda p: 3 <= p < 7),
            ("加速7~12%", lambda p: 7 <= p < 12), ("高位≥12%", lambda p: p >= 12)]
    for label, cond in bins:
        grp = [r for r in rows if isinstance(r.get("percent"), (int, float)) and cond(r["percent"])]
        if not grp:
            continue
        hit = sum(r["next_day_pct"] >= 7 for r in grp) / len(grp) * 100
        avg = sum(r["next_day_pct"] for r in grp) / len(grp)
        print(f"  {label}: n={len(grp)} hit={hit:.2f}% avg={avg:+.2f}%")
    # 分节（复刻 crosssection_view._section 的核心判据）
    print("\n=== 分节（简化复刻）===")
    for label, cond in [
        ("低位·资金强", lambda r: r.get("percent") is not None and r["percent"] < 7
         and (r.get("main_net") or 0) > 0 and r["cons_pos"]
         and isinstance(r.get("amount_surge"), (int, float)) and r["amount_surge"] > 1
         and isinstance(r.get("speed_1"), (int, float)) and r["speed_1"] > 0),
        ("高位加速", lambda r: r.get("percent") is not None and r["percent"] >= 12),
        ("三窗全正+放量配合", lambda r: r["cons_pos"]
         and isinstance(r.get("amount_surge"), (int, float)) and r["amount_surge"] > 1
         and isinstance(r.get("speed_1"), (int, float)) and r["speed_1"] > 0),
    ]:
        grp = [r for r in rows if cond(r)]
        if len(grp) < 30:
            print(f"  {label}: n={len(grp)} (样本不足)")
            continue
        hit = sum(r["next_day_pct"] >= 7 for r in grp) / len(grp) * 100
        avg = sum(r["next_day_pct"] for r in grp) / len(grp)
        print(f"  {label}: n={len(grp)} hit={hit:.2f}% avg={avg:+.2f}%")


if __name__ == "__main__":
    main()
