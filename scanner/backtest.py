"""回测归因框架。

目标：把策略评分权重从"逐案例调参"收敛到"数据驱动"。
基于 recommendations 表（推荐记录）与 daily_kline 表（历史K线）计算：

1. 结果回填（backfill_outcomes）：用 daily_kline 推导推荐后 N 日收益，
   回填 recommendations.next_day_pct / fwd_3d / fwd_5d。
2. 分策略表现（strategy_performance）：按 category 聚合胜率、盈亏比、
   平均收益、IC（评分 vs 收益的相关性）。
3. 分维度 IC（dimension_ic）：解析 score_breakdown JSON，逐维度计算
   IC，输出"正 IC 维度 / 反指维度"表，指导权重调整。

本模块不进入实时扫描路径，仅通过 `python -m scanner.backtest` 运行。
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from scanner.config import DB_PATH
from scanner.trading_session import is_trading_day


# 当前有效策略类别（过滤已废弃的 old_face / early_momentum）
ACTIVE_CATEGORIES = {"new_face", "known_new_face", "momentum", "pullback", "short_term"}


@dataclass
class Outcome:
    next_day: float | None = None
    fwd_3d: float | None = None
    fwd_5d: float | None = None


def _nth_trading_day_after(d: date, n: int) -> date | None:
    """Return the date n trading days after d (exclusive of d)."""
    cursor = d
    for _ in range(n):
        cursor += timedelta(days=1)
        while not is_trading_day(cursor):
            cursor += timedelta(days=1)
    return cursor


def _load_sym_kline(conn: sqlite3.Connection, symbol: str) -> dict[str, float]:
    """{date: close_pct_of_that_day} for a single symbol."""
    out: dict[str, float] = {}
    for dt, pct in conn.execute(
        "SELECT date, percent FROM daily_kline WHERE symbol=? ORDER BY date",
        (symbol,),
    ).fetchall():
        out[dt] = pct
    return out


def compute_outcome(
    kline_map: dict[str, dict[str, float]],
    symbol: str,
    rec_date: str,
    rec_percent: float,
) -> Outcome:
    """推导推荐后收益。

    - next_day: 推荐日次一交易日涨幅（与现有 next_day_pct 口径一致）
    - fwd_3d: 推荐日之后第 3 个交易日的"当日涨幅"（代理指标，非累计收益）
    - fwd_5d: 推荐日之后第 5 个交易日的"当日涨幅"（代理指标，非累计收益）

    注意：当前数据库仅存每日 percent（当日涨幅），无法还原绝对价格序列，
    因此无法计算相对推荐日收盘的真实累计收益。fwd_3d/fwd_5d 实为第 N 个
    交易日的单日涨幅代理，IC/胜率归因时应按此口径解读，勿与累计收益混淆。
    """
    sym_kl = kline_map.get(symbol)
    if not sym_kl or rec_date not in sym_kl:
        return Outcome()
    rec_dt = date.fromisoformat(rec_date)
    out = Outcome()

    nxt = _nth_trading_day_after(rec_dt, 1)
    if nxt and nxt.isoformat() in sym_kl:
        out.next_day = sym_kl[nxt.isoformat()]

    # fwd_3d / fwd_5d 仅为第 N 交易日当日涨幅（代理），见上方 docstring 口径说明
    d3 = _nth_trading_day_after(rec_dt, 3)
    if d3 and d3.isoformat() in sym_kl:
        out.fwd_3d = sym_kl[d3.isoformat()]
    d5 = _nth_trading_day_after(rec_dt, 5)
    if d5 and d5.isoformat() in sym_kl:
        out.fwd_5d = sym_kl[d5.isoformat()]
    return out


def backfill_outcomes(conn: sqlite3.Connection, dry_run: bool = False) -> int:
    """回填 recommendations 的 N 日收益字段，返回更新行数。

    重新计算所有行的收益字段并覆盖（daily_kline 是最新数据，重算可纠正旧错误值），
    但新值为 None 时不覆盖已有有效 float，避免 K 线临时缺失导致数据丢失。
    """
    rows = conn.execute(
        "SELECT id, symbol, date, percent, next_day_pct, fwd_3d, fwd_5d "
        "FROM recommendations"
    ).fetchall()
    if not rows:
        return 0
    needed_syms = {r[1] for r in rows}
    kline_map = {sym: _load_sym_kline(conn, sym) for sym in needed_syms}
    updated = 0
    for rid, sym, dt, pct, ndp, f3, f5 in rows:
        occ = compute_outcome(kline_map, sym, dt, pct)
        # 新值非 None 时覆盖（纠正旧错误值）；新值 None 时保留已有值（防数据丢失）
        new_ndp = occ.next_day if occ.next_day is not None else ndp
        new_f3 = occ.fwd_3d if occ.fwd_3d is not None else f3
        new_f5 = occ.fwd_5d if occ.fwd_5d is not None else f5
        if (new_ndp != ndp) or (new_f3 != f3) or (new_f5 != f5):
            updated += 1
            if not dry_run:
                conn.execute(
                    "UPDATE recommendations SET next_day_pct=?, fwd_3d=?, fwd_5d=? "
                    "WHERE id=?",
                    (new_ndp, new_f3, new_f5, rid),
                )
    if not dry_run and updated:
        conn.commit()
    return updated


def _ic(values: list[float], returns: list[float]) -> float | None:
    """Rank IC：values 与 returns 的 spearman 近似（用秩相关系数）。"""
    if len(values) < 5 or len(values) != len(returns):
        return None
    n = len(values)
    rv = _rank(values)
    rr = _rank(returns)
    mean_v = sum(rv) / n
    mean_r = sum(rr) / n
    cov = sum((rv[i] - mean_v) * (rr[i] - mean_r) for i in range(n))
    var_v = sum((x - mean_v) ** 2 for x in rv) ** 0.5
    var_r = sum((x - mean_r) ** 2 for x in rr) ** 0.5
    # 浮点判等改用阈值，避免累加误差导致本应短路的情况继续计算
    if var_v < 1e-12 or var_r < 1e-12:
        return 0.0
    return cov / (var_v * var_r)


def _rank(xs: list[float]) -> list[float]:
    # 标准秩：相等值取平均秩（tie-averaging），避免并列时 rank-IC 偏置
    # 浮点判等改用 1e-9 阈值，避免浮点累加误差让本应并列的分数分到不同秩
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    n = len(order)
    while i < n:
        j = i
        while j + 1 < n and abs(xs[order[j + 1]] - xs[order[i]]) < 1e-9:
            j += 1
        avg = (i + j) / 2 + 1  # 平均秩（1-based）
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


@dataclass
class StrategyStat:
    category: str
    count: int
    win_rate: float = 0.0
    avg_return: float = 0.0
    profit_loss_ratio: float = 0.0
    ic_score: float = 0.0
    avg_score: float = 0.0


def strategy_performance(
    conn: sqlite3.Connection, metric: str = "next_day_pct", days: int = 0
) -> list[StrategyStat]:
    """按策略类别聚合表现。metric ∈ {next_day_pct, fwd_3d, fwd_5d}。

    days > 0 时仅分析最近 N 天的推荐（基于 date 列过滤）。
    """
    date_filter = f"AND date >= date('now', 'localtime', '-{int(days)} days') " if days > 0 else ""
    rows = conn.execute(
        f"SELECT category, score, {metric} FROM recommendations "
        f"WHERE category IN ({','.join('?' * len(ACTIVE_CATEGORIES))}) "
        f"AND {metric} IS NOT NULL {date_filter}",
        tuple(ACTIVE_CATEGORIES),
    ).fetchall()

    by_cat: dict[str, list[tuple[int, float]]] = defaultdict(list)
    for cat, score, ret in rows:
        by_cat[cat].append((score, ret))

    stats: list[StrategyStat] = []
    for cat, pairs in by_cat.items():
        returns = [p[1] for p in pairs]
        scores = [p[0] for p in pairs]
        wins = sum(1 for r in returns if r > 0)
        gains = [r for r in returns if r > 0]
        losses = [-r for r in returns if r < 0]
        avg_gain = sum(gains) / len(gains) if gains else 0.0
        avg_loss = sum(losses) / len(losses) if losses else 0.0
        pl = (avg_gain / avg_loss) if avg_loss > 0 else 0.0
        ic = _ic(scores, returns) or 0.0
        stats.append(
            StrategyStat(
                category=cat,
                count=len(pairs),
                win_rate=wins / len(returns),
                avg_return=sum(returns) / len(returns),
                profit_loss_ratio=pl,
                ic_score=ic,
                avg_score=sum(scores) / len(scores),
            )
        )
    stats.sort(key=lambda s: -s.win_rate)
    return stats


@dataclass
class DimensionIC:
    dimension: str
    hits: int
    ic: float
    avg_return_when_positive: float
    avg_return_when_zero: float


def dimension_ic(conn: sqlite3.Connection, metric: str = "next_day_pct", days: int = 0) -> list[DimensionIC]:
    """解析 score_breakdown，逐维度计算 IC。

    对每一条推荐记录，将每个维度视为「该维度是否加正分」，与收益做分组比较：
    - 维度值 > 0 组的均收益 vs 维度值 == 0 组，计算 rank IC。
    这能揭示哪些维度是「正 IC（加分越多收益越好）」还是「反指」。

    days > 0 时仅分析最近 N 天的推荐。
    """
    # 已删除功能残留于历史 score_breakdown JSON，无对应评分代码，仅干扰阅读。
    dead_dim_keys = {
        "new_face_candle", "momentum_candle", "high_pos",
    }
    date_filter = f"AND date >= date('now', 'localtime', '-{int(days)} days') " if days > 0 else ""
    rows = conn.execute(
        f"SELECT score_breakdown, {metric} FROM recommendations "
        f"WHERE category IN ({','.join('?' * len(ACTIVE_CATEGORIES))}) "
        f"AND {metric} IS NOT NULL AND score_breakdown IS NOT NULL {date_filter}",
        tuple(ACTIVE_CATEGORIES),
    ).fetchall()

    # dim -> (list_of_dim_value, list_of_return)
    dim_vals: dict[str, list[float]] = defaultdict(list)
    dim_rets: dict[str, list[float]] = defaultdict(list)
    for breakdown, ret in rows:
        try:
            d = json.loads(breakdown)
        except (json.JSONDecodeError, TypeError):
            continue
        for dim, val in d.items():
            if dim in dead_dim_keys:
                continue
            if not isinstance(val, (int, float)):
                continue
            dim_vals[dim].append(float(val))
            dim_rets[dim].append(float(ret))

    results: list[DimensionIC] = []
    for dim, vals in dim_vals.items():
        if len(vals) < 20:
            continue
        ic = _ic(vals, dim_rets[dim]) or 0.0
        pos_rets = [dim_rets[dim][i] for i in range(len(vals)) if vals[i] > 0]
        # 浮点判等改用阈值，避免累加误差使 zero_rets 恒为空
        zero_rets = [dim_rets[dim][i] for i in range(len(vals)) if abs(vals[i]) < 1e-9]
        avg_pos = sum(pos_rets) / len(pos_rets) if pos_rets else float("nan")
        avg_zero = sum(zero_rets) / len(zero_rets) if zero_rets else float("nan")
        results.append(
            DimensionIC(
                dimension=dim,
                hits=len(vals),
                ic=ic,
                avg_return_when_positive=avg_pos,
                avg_return_when_zero=avg_zero,
            )
        )
    results.sort(key=lambda x: -x.ic)
    return results


def print_report(conn: sqlite3.Connection, metric: str = "next_day_pct", days: int = 0) -> None:
    print("=" * 70)
    days_label = f", 最近{days}天" if days > 0 else ", 全部历史"
    print(f"回测归因报告 (metric={metric}{days_label})")
    print("=" * 70)

    print("\n[1] 分策略表现")
    print(f"{'类别':<16}{'样本':>6}{'胜率':>8}{'均收益':>9}{'盈亏比':>8}{'IC':>8}{'均分':>8}")
    for s in strategy_performance(conn, metric, days=days):
        print(
            f"{s.category:<16}{s.count:>6}{s.win_rate*100:>7.1f}%"
            f"{s.avg_return:>9.2f}{s.profit_loss_ratio:>8.2f}"
            f"{s.ic_score:>8.3f}{s.avg_score:>8.1f}"
        )

    print("\n[2] 分维度 IC（降序，正=加分越多收益越好）")
    print(f"{'维度':<28}{'样本':>6}{'IC':>8}{'加正分均收益':>14}{'零分均收益':>12}")
    for d in dimension_ic(conn, metric, days=days):
        ap = f"{d.avg_return_when_positive:.2f}" if d.avg_return_when_positive == d.avg_return_when_positive else "n/a"
        az = f"{d.avg_return_when_zero:.2f}" if d.avg_return_when_zero == d.avg_return_when_zero else "n/a"
        tag = "  <== 反指" if d.ic < -0.02 else ("  <== 强信号" if d.ic > 0.02 else "")
        print(f"{d.dimension:<28}{d.hits:>6}{d.ic:>8.3f}{ap:>14}{az:>12}{tag}")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="创业板扫描策略回测归因")
    parser.add_argument("--days", type=int, default=0, help="仅分析最近 N 天的推荐（0=全部）")
    parser.add_argument("--metric", default="next_day_pct",
                        choices=["next_day_pct", "fwd_3d", "fwd_5d"])
    parser.add_argument("--backfill", action="store_true", help="回填 N 日收益字段")
    parser.add_argument("--dry-run", action="store_true", help="回填预览不写库")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH)
    if args.backfill:
        n = backfill_outcomes(conn, dry_run=args.dry_run)
        print(f"回填更新行数: {n}" + (" (dry-run)" if args.dry_run else ""))
    print_report(conn, metric=args.metric, days=args.days)
    conn.close()


if __name__ == "__main__":
    main()
