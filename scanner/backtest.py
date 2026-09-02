"""回测归因框架。

目标：把策略评分权重从"逐案例调参"收敛到"数据驱动"。
基于 recommendations 表（推荐记录）与 daily_kline 表（历史K线）计算：

1. 结果回填（backfill_outcomes）：用 daily_kline 推导推荐后 N 日收益，
   回填 recommendations 的收益字段：
   - next_day_pct / fwd_3d / fwd_5d：单日涨幅口径（旧，次日/第3/第5日当日涨幅）
   - cum_2d / cum_3d：累计收益口径（新，T+0 close 到 T+N close 累计涨幅）
2. 分策略表现（strategy_performance）：按 category 聚合胜率、盈亏比、
   平均收益、IC（评分 vs 收益的相关性）。
3. 分维度 IC（dimension_ic）：解析 score_breakdown JSON，逐维度计算
   IC，输出"正 IC 维度 / 反指维度"表，指导权重调整。

口径选择（2026-08-18 统一为「次日大涨」）：
- 综合排序 / 类别优先级 / 档位 / 建议列全部校准于 next_day_pct（次日≥7% hit 口径，
  scanner.nextday_attribution 1184 去重样本）——筛选系统当前唯一决策口径。
- IC 决策与分桶解读均基于 next_day_pct；cum_2d / cum_3d 保留为对照（--metric 可选），
  不再作为排序与调参依据（曾因「持有 2-3 天」语义与综合排序 🎯 置顶口径冲突，现统一）。
- next_day_pct 为单日口径，次日大涨票多为高开冲高，结论只用于「次日大涨」子目标。

定位（必读）：本项目是**筛选系统，不是交易系统**，本模块是**权重校准仪表盘**，
只回答"分数排序是否等于好坏排序"（IC / 胜率 / 分桶 / 维度 IC），用于调 MIN_SCORE、
权重、CAT_DISPLAY_PRIORITY——这是筛选系统唯一该用的回测形态。
本模块**不产出、也不该产出**"实盘收益预测"；盘中推荐以收盘价为起点导致 cum_*d
系统性高估（见 print_report 的 P1-6 标注），任何把本模块数值当"能否赚钱"裁判的
用法都是越界。组合资金模拟在 portfolio_backtest（可选自检尺，降级看待）。

本模块不进入实时扫描路径，仅通过 `python -m scanner.backtest` 运行。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

from scanner.config import BACKFILL_OUTCOMES_WINDOW_DAYS, DB_PATH, now_beijing
from scanner.models import parse_score_breakdown
from scanner.trading_session import _nth_trading_day_after
from scanner.utils import clear_screen

# Windows GBK 控制台无法编码 ‱ 等字符，统一走 UTF-8（项目其它入口同款处理）
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")


# 当前有效策略类别（过滤已废弃的 old_face / early_momentum）。
# 2026-08-20 收敛：类别宇宙单一事实来源见 scanner/categories.py；
# 回测/归因处理 DB 中全部已知类别（含已下线的 pullback，用于历史校准）。
from scanner.categories import ATTRIBUTION_CATEGORIES as ACTIVE_CATEGORIES  # noqa: E402


@dataclass
class Outcome:
    next_day: float | None = None
    fwd_3d: float | None = None
    fwd_5d: float | None = None
    # 累计收益：匹配用户「持有 2-3 天卖出」的真实操作
    # 公式：(close[T+N] - close[T]) / close[T] * 100
    cum_2d: float | None = None
    cum_3d: float | None = None


def _load_sym_kline(conn: sqlite3.Connection, symbol: str) -> dict[str, dict[str, float]]:
    """{date: {"close": float, "percent": float}} for a single symbol.

    同时返回 close 和 percent：
    - percent 用于单日涨幅指标（next_day_pct / fwd_3d / fwd_5d）
    - close 用于累计收益指标（cum_2d / cum_3d）
    """
    out: dict[str, dict[str, float]] = {}
    for dt, close, pct in conn.execute(
        "SELECT date, close, percent FROM daily_kline WHERE symbol=? ORDER BY date",
        (symbol,),
    ).fetchall():
        out[dt] = {"close": close, "percent": pct}
    return out


def compute_outcome(
    kline_map: dict[str, dict[str, dict[str, float]]],
    symbol: str,
    rec_date: str,
    rec_percent: float,
) -> Outcome:
    """推导推荐后收益。

    单日涨幅指标（旧口径）：
    - next_day: 推荐日次一交易日涨幅（percent 字段）
    - fwd_3d / fwd_5d: 推荐日之后第 3/5 个交易日的"当日涨幅"（代理，非累计）

    累计收益指标（对照口径，--metric 可选））：
    - cum_2d: (close[T+2] - close[T]) / close[T] * 100
    - cum_3d: (close[T+3] - close[T]) / close[T] * 100

    2026-08-18 统一口径：决策/排序/调参以 next_day_pct（次日大涨≥7% hit）为准，
    cum_2d / cum_3d 仅保留为对照，不再作为调参依据。
    """
    sym_kl = kline_map.get(symbol)
    if not sym_kl or rec_date not in sym_kl:
        return Outcome()
    rec_dt = date.fromisoformat(rec_date)
    rec_bar = sym_kl[rec_date]
    rec_close = rec_bar.get("close")
    out = Outcome()

    # 单日涨幅（旧口径）
    nxt = _nth_trading_day_after(rec_dt, 1)
    if nxt and nxt.isoformat() in sym_kl:
        out.next_day = sym_kl[nxt.isoformat()].get("percent")

    d3 = _nth_trading_day_after(rec_dt, 3)
    if d3 and d3.isoformat() in sym_kl:
        out.fwd_3d = sym_kl[d3.isoformat()].get("percent")
    d5 = _nth_trading_day_after(rec_dt, 5)
    if d5 and d5.isoformat() in sym_kl:
        out.fwd_5d = sym_kl[d5.isoformat()].get("percent")

    # 累计收益（新口径）：需推荐日 close + T+N close
    if rec_close is not None and rec_close > 0:
        d2 = _nth_trading_day_after(rec_dt, 2)
        if d2 and d2.isoformat() in sym_kl:
            close_n = sym_kl[d2.isoformat()].get("close")
            if close_n is not None:
                out.cum_2d = (close_n - rec_close) / rec_close * 100
        d3 = _nth_trading_day_after(rec_dt, 3)
        if d3 and d3.isoformat() in sym_kl:
            close_n = sym_kl[d3.isoformat()].get("close")
            if close_n is not None:
                out.cum_3d = (close_n - rec_close) / rec_close * 100

    return out


def backfill_outcomes(
    conn: sqlite3.Connection,
    dry_run: bool = False,
    since_days: int = BACKFILL_OUTCOMES_WINDOW_DAYS,
) -> int:
    """回填 recommendations 的 N 日收益字段，返回更新行数。

    重新计算收益字段并覆盖（daily_kline 是最新数据，重算可纠正旧错误值），
    但新值为 None 时不覆盖已有有效 float，避免 K 线临时缺失导致数据丢失。

    since_days > 0 时只处理最近 N 天（自然日）的推荐行（默认
    BACKFILL_OUTCOMES_WINDOW_DAYS）。实时扫描每个刷新周期都调用本函数，全表
    重算（SELECT 全部 recommendations + 逐 symbol 全量 K 线）会随库增长线性
    拖长扫描周期；而超过窗口的历史行其 K 线早已定稿、不会再变，重算纯属浪费。
    需要全量重算（修数/迁移）时显式传 since_days=0。
    """
    if since_days > 0:
        cutoff = (now_beijing() - timedelta(days=since_days)).date().isoformat()
        rows = conn.execute(
            "SELECT id, symbol, date, percent, next_day_pct, fwd_3d, fwd_5d, cum_2d, cum_3d "
            "FROM recommendations WHERE date >= ?",
            (cutoff,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, symbol, date, percent, next_day_pct, fwd_3d, fwd_5d, cum_2d, cum_3d FROM recommendations"
        ).fetchall()
    if not rows:
        return 0
    needed_syms = {r[1] for r in rows}
    kline_map = {sym: _load_sym_kline(conn, sym) for sym in needed_syms}
    updated = 0
    for rid, sym, dt, pct, ndp, f3, f5, c2, c3 in rows:
        occ = compute_outcome(kline_map, sym, dt, pct)
        # 新值非 None 时覆盖（纠正旧错误值）；新值 None 时保留已有值（防数据丢失）
        new_ndp = occ.next_day if occ.next_day is not None else ndp
        new_f3 = occ.fwd_3d if occ.fwd_3d is not None else f3
        new_f5 = occ.fwd_5d if occ.fwd_5d is not None else f5
        new_c2 = occ.cum_2d if occ.cum_2d is not None else c2
        new_c3 = occ.cum_3d if occ.cum_3d is not None else c3
        if (new_ndp != ndp) or (new_f3 != f3) or (new_f5 != f5) or (new_c2 != c2) or (new_c3 != c3):
            updated += 1
            if not dry_run:
                conn.execute(
                    "UPDATE recommendations SET next_day_pct=?, fwd_3d=?, fwd_5d=?, cum_2d=?, cum_3d=? WHERE id=?",
                    (new_ndp, new_f3, new_f5, new_c2, new_c3, rid),
                )
    if not dry_run and updated:
        conn.commit()
    return updated


def backfill_rejection_outcomes(
    conn: sqlite3.Connection,
    dry_run: bool = False,
    since_days: int = BACKFILL_OUTCOMES_WINDOW_DAYS,
) -> int:
    """回填 scan_rejections（被硬过滤移出推荐的候选）的 N 日收益字段，返回更新行数。

    与 backfill_outcomes 同源口径：
    - next_day_pct = 次一交易日 percent（硬过滤「次日避开了什么」主指标）
    - fwd_3d = T+3 交易日 percent
    - nd10_pct = T+10 交易日 percent（弱市低吸扩容的中长期漂移观测）

    新值非 None 时覆盖（纠正旧错误值）；新值 None 时保留已有值（防 K 线临时缺失丢数据）。
    实时扫描不直接写 outcome——被拒票的次日 K 线收盘后才可得，由 backfill_kline.py
    收盘后批量回填，从而让「硬过滤到底有没有用」每月可审计（避免幸存者偏差）。
    scan_rejections 无 id 列，用隐式 rowid 定位更新行。
    """
    if since_days > 0:
        cutoff = (now_beijing() - timedelta(days=since_days)).date().isoformat()
        rows = conn.execute(
            "SELECT rowid, symbol, date, next_day_pct, fwd_3d, nd10_pct "
            "FROM scan_rejections WHERE date >= ?",
            (cutoff,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT rowid, symbol, date, next_day_pct, fwd_3d, nd10_pct FROM scan_rejections"
        ).fetchall()
    if not rows:
        return 0
    needed_syms = {r[1] for r in rows}
    kline_map = {sym: _load_sym_kline(conn, sym) for sym in needed_syms}
    updated = 0
    for rid, sym, dt, ndp, f3, nd10 in rows:
        sym_kl = kline_map.get(sym)
        new_ndp, new_f3, new_nd10 = ndp, f3, nd10
        if sym_kl and dt in sym_kl:
            rec_dt = date.fromisoformat(dt)
            nxt = _nth_trading_day_after(rec_dt, 1)
            if nxt and (nk := nxt.isoformat()) in sym_kl and sym_kl[nk].get("percent") is not None:
                new_ndp = sym_kl[nk]["percent"]
            d3 = _nth_trading_day_after(rec_dt, 3)
            if d3 and (dk := d3.isoformat()) in sym_kl and sym_kl[dk].get("percent") is not None:
                new_f3 = sym_kl[dk]["percent"]
            d10 = _nth_trading_day_after(rec_dt, 10)
            if d10 and (d10k := d10.isoformat()) in sym_kl and sym_kl[d10k].get("percent") is not None:
                new_nd10 = sym_kl[d10k]["percent"]
        if (new_ndp != ndp) or (new_f3 != f3) or (new_nd10 != nd10):
            updated += 1
            if not dry_run:
                conn.execute(
                    "UPDATE scan_rejections SET next_day_pct=?, fwd_3d=?, nd10_pct=? WHERE rowid=?",
                    (new_ndp, new_f3, new_nd10, rid),
                )
    if not dry_run and updated:
        conn.commit()
    return updated


def spearman(values: list[float], returns: list[float]) -> float | None:
    """Rank IC（Spearman 秩相关）：values 与 returns 的秩 Pearson 系数。

    2026-08-20 收敛单源：ic_attribution / nextday_attribution 此前各抄一份等价实现，
    tie 处理规则不同（1e-9 容差 vs 严格相等）导致浮点近邻分数算出不同 IC。统一到此。
    """
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


# 允许直接拼接进 SQL 的收益列名白名单（2026-08-29）。
# metric 是列名而非值，无法用 ? 占位符参数化，只能拼接；此前三个函数直接
# f-string 内插，唯一调用方靠 argparse choices 挡住（当前不可利用），
# 但函数边界自身毫无防护——任何绕过 argparse 的新调用方即成注入点。
# 列名集合固定，统一在此校验：不认识的 metric 立即抛错而非拼进 SQL。
ALLOWED_METRICS: frozenset[str] = frozenset({"next_day_pct", "fwd_3d", "fwd_5d", "cum_2d", "cum_3d"})


def _check_metric(metric: str) -> str:
    """校验收益列名，非白名单直接拒绝（防 SQL 标识符注入）。"""
    if metric not in ALLOWED_METRICS:
        raise ValueError(f"非法 metric={metric!r}；允许值：{sorted(ALLOWED_METRICS)}")
    return metric


def load_attribution_rows(
    conn: sqlite3.Connection,
    metric: str,
    cols: str = "",
    categories: list[str] | None = None,
    days: int = 0,
    require_breakdown: bool = False,
) -> list[sqlite3.Row]:
    """归因样本统一加载：**excluded=0 + 指标非空 + 按 (date, symbol) 去重取最后一轮**。

    2026-09-02 修复（样本口径统一——三处归因此前各写一套 SQL 且互不一致）：

    1. **重复样本有偏加权**：recommendations 是**每轮扫描一行**（60s 一轮），同票同日
       在榜 N 轮即有 N 行。实测 3882 行仅对应 1774 个 (date, symbol)，虚高 **2.19x**，
       单票单日最多 43 行。更关键的是重复次数与**停留榜上时长正相关**——这不是中性
       放大，而是系统性偏向"停留久"的票：动量票天然停留久 → momentum hit 由 16.5%
       (n=508) 修正为 10.0%(n=290)，**-6.5pp**；old_face 反向 6.6%(n=1269)→11.4%(n=88)。
    2. **未过滤硬过滤票**：旧查询缺 `excluded = 0`，把已被风险标签判定"不该买"的票
       （当前 70 行）计入策略收益，等于给"明知不可买"的票记成绩。
    3. **去重取哪一轮不统一**：walkforward 取最后一轮（无偏）；nextday_attribution
       旧逻辑取 **score 最高**——按分数挑选样本会系统性偏向高分档，正是"高分档 hit
       更高"结论的选择偏差来源。现统一取最后一轮（不按 score 挑选）。

    口径与 scanner.walkforward.load_rows 对齐（后者本就正确，本次未改）。

    cols：附加列（逗号分隔，调用方传入内部字面量，不接受外部输入）。
    require_breakdown：为 True 时额外要求 score_breakdown IS NOT NULL。
    返回 sqlite3.Row 列表（含 date/symbol/category/score + cols + metric 列）。
    """
    metric = _check_metric(metric)
    cats = list(categories if categories is not None else ACTIVE_CATEGORIES)
    if not cats:
        return []
    # 统一行工厂：调用方（含单测内存库）未必设置 row_factory，本函数按列名取数。
    # Row 仅影响本连接本次读取，无副作用。
    conn.row_factory = sqlite3.Row
    params: list = list(cats)
    date_filter = ""
    if days > 0:
        cutoff = (now_beijing() - timedelta(days=days)).date().isoformat()
        date_filter = "AND date >= ? "
        params.append(cutoff)
    extra = f", {cols}" if cols else ""
    bd_filter = "AND score_breakdown IS NOT NULL " if require_breakdown else ""
    # 排序用 rowid 而非 time 列：time 只有 HH:MM 精度，同一分钟内的多轮扫描
    # time 完全相同 → "取最后一轮"的结果在并列行间不确定（去重结果不稳定）。
    # rowid 按插入顺序单调递增，而 recommendations 正是逐轮追加，故 rowid 即
    # 真实的轮次序，且无额外列依赖（测试夹具的精简 schema 也适用）。
    rows = conn.execute(
        f"SELECT rowid AS _rid, date, symbol, category, score{extra}, {metric} "  # noqa: S608 - metric 已白名单校验
        f"FROM recommendations "
        f"WHERE category IN ({','.join('?' * len(cats))}) "
        f"AND excluded = 0 AND {metric} IS NOT NULL {bd_filter}{date_filter}"
        f"ORDER BY date, rowid",
        tuple(params),
    ).fetchall()
    # 去重：同 (date, symbol) 保留最后一轮（不按 score 挑选，避免分数选择偏差）。
    # symbol 为空（脏数据/测试夹具）时回退 rowid 作键——否则所有空 symbol 行会塌缩
    # 成一行，静默丢掉整批样本。
    dedup: dict[tuple, sqlite3.Row] = {}
    for r in rows:
        sym = r["symbol"]
        key = (r["date"], sym) if sym else ("__no_symbol__", r["_rid"])
        dedup[key] = r
    return list(dedup.values())


def strategy_performance(
    conn: sqlite3.Connection,
    metric: str = "next_day_pct",
    days: int = 0,
) -> list[StrategyStat]:
    """按策略类别聚合表现。metric ∈ {next_day_pct, fwd_3d, fwd_5d, cum_2d, cum_3d}。

    - next_day_pct：单日涨幅口径（2026-08-18 起为唯一决策口径，统一「次日大涨」）
    - cum_2d / cum_3d：累计收益对照口径（--metric 可选）

    days > 0 时仅分析最近 N 天的推荐（基于 date 列过滤）。
    """
    metric = _check_metric(metric)
    # 用 Beijing UTC+8 计算截止日，避免服务器本地时区导致日期偏移
    # （'localtime' 修饰符依赖服务器时区，违反项目硬约束）
    # 样本口径统一：excluded=0 + 同票同日取最后一轮（详见 load_attribution_rows）
    rows = load_attribution_rows(conn, metric, days=days)

    by_cat: dict[str, list[tuple[int, float]]] = defaultdict(list)
    for r in rows:
        by_cat[r["category"]].append((r["score"], r[metric]))

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
        ic = spearman(scores, returns) or 0.0
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
        "new_face_candle",
        "momentum_candle",
        "high_pos",
    }
    metric = _check_metric(metric)
    # 用 Beijing UTC+8 计算截止日，避免服务器本地时区导致日期偏移
    # 样本口径统一：excluded=0 + 同票同日取最后一轮（详见 load_attribution_rows）
    rows = load_attribution_rows(conn, metric, cols="score_breakdown",
                                 days=days, require_breakdown=True)

    # dim -> (list_of_dim_value, list_of_return)
    dim_vals: dict[str, list[float]] = defaultdict(list)
    dim_rets: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        d = parse_score_breakdown(r["score_breakdown"])
        ret = r[metric]
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
        ic = spearman(vals, dim_rets[dim]) or 0.0
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


@dataclass
class RankCategoryStat:
    """综合排序类别优先级校准用的单类别表现统计。"""

    category: str
    count: int
    avg_return: float
    win_rate: float
    ic: float


# 样本量低于此值视为不可靠（小样本均值噪声大，校准需谨慎）
RANK_MIN_SAMPLE = 20


def rank_category_stats(
    conn: sqlite3.Connection, metric: str = "next_day_pct", days: int = 0
) -> list[RankCategoryStat]:
    """各现役类别在给定口径下的表现（按均收益降序）。

    metric ∈ {next_day_pct, cum_2d, cum_3d, ...}。days > 0 时仅用最近 N 天推荐。
    类别内 IC 为 IC(score → return)，用于识别「分数反指」的类别（IC 为负）。
    2026-08-18 默认口径改为 next_day_pct（统一「次日大涨」）。
    """
    metric = _check_metric(metric)
    # 样本口径统一：excluded=0 + 同票同日取最后一轮（详见 load_attribution_rows）
    rows = load_attribution_rows(conn, metric, days=days)

    by: dict[str, list[tuple[int, float]]] = defaultdict(list)
    for r in rows:
        by[r["category"]].append((r["score"], r[metric]))

    stats: list[RankCategoryStat] = []
    for cat, pairs in by.items():
        rets = [p[1] for p in pairs]
        scores = [p[0] for p in pairs]
        ic = spearman(scores, rets) or 0.0
        stats.append(
            RankCategoryStat(
                category=cat,
                count=len(pairs),
                avg_return=sum(rets) / len(rets),
                win_rate=sum(1 for r in rets if r > 0) / len(rets),
                ic=ic,
            )
        )
    stats.sort(key=lambda s: -s.avg_return)
    return stats


def suggest_priority(stats: list[RankCategoryStat]) -> list[str]:
    """按均收益降序给出建议类别展示优先级（供人工复核后更新 CAT_DISPLAY_PRIORITY）。"""
    return [s.category for s in sorted(stats, key=lambda s: -s.avg_return)]


def print_ranking_report(conn: sqlite3.Connection, metric: str = "next_day_pct", recent_days: int = 30) -> None:
    """综合排序类别优先级校准报告。

    同时展示全期与近期两个窗口：全期样本大但混入历史配置，近期反映当前市场环境但样本小。
    建议优先级 = 按近期均收益排序；近期样本不足（<RANK_MIN_SAMPLE）时回退全期值，仍不足则打 ⚠。
    """
    from scanner.config import CAT_DISPLAY_PRIORITY

    full = rank_category_stats(conn, metric, days=0)
    recent = rank_category_stats(conn, metric, days=recent_days)
    full_map = {s.category: s for s in full}
    recent_map = {s.category: s for s in recent}

    current_order = sorted(CAT_DISPLAY_PRIORITY, key=CAT_DISPLAY_PRIORITY.get)

    def _pick(cat: str) -> RankCategoryStat:
        # 近期样本足够用近期；否则用全期；再否则用近期并打标
        if cat in recent_map and recent_map[cat].count >= RANK_MIN_SAMPLE:
            return recent_map[cat]
        if cat in full_map and full_map[cat].count >= RANK_MIN_SAMPLE:
            return full_map[cat]
        return recent_map.get(cat) or full_map.get(cat)

    repr_stats = []
    for cat in sorted(set(list(full_map) + list(recent_map))):
        s = _pick(cat)
        if s is not None:
            repr_stats.append(s)
    suggested = suggest_priority(repr_stats)

    print("=" * 70)
    print(f"综合排序类别优先级校准 (metric={metric}, 近期窗口={recent_days}天)")
    print("=" * 70)
    print("[当前 config 顺序]  " + " > ".join(current_order))
    print("[建议优先级]       " + " > ".join(suggested))

    print("\n[各类别表现]（按建议用的口径降序；近期样本不足回退全期）")
    print(f"{'类别':<16}{'窗口':>6}{'样本':>6}{'均收益':>9}{'胜率':>8}{'IC(score)':>10}  备注")
    for s in sorted(repr_stats, key=lambda x: -x.avg_return):
        if s.category in recent_map and recent_map[s.category].count >= RANK_MIN_SAMPLE:
            src = "近期"
        elif s.category in full_map and full_map[s.category].count >= RANK_MIN_SAMPLE:
            src = "全期"
        else:
            src = "近期*"
        note = ""
        if s.ic < -0.05:
            note = "<== 反指(分数越高越差)"
        elif s.ic > 0.05:
            note = "<== 分数正效"
        if s.count < RANK_MIN_SAMPLE:
            note = note + " [样本不足]" if note else "[样本不足]"
        print(f"{s.category:<16}{src:>6}{s.count:>6}{s.avg_return:>9.2f}{s.win_rate * 100:>7.1f}%{s.ic:>10.3f}  {note}")

    diff = [c for c in current_order if c in suggested and current_order.index(c) != suggested.index(c)]
    diff_str = "无，顺序一致" if not diff else " ".join(f"{c}(当前{c}→建议{suggested.index(c)})" for c in diff)
    print(f"\n[与当前差异] {diff_str}")
    print("确认后人工更新 config.CAT_DISPLAY_PRIORITY")


def print_report(conn: sqlite3.Connection, metric: str = "next_day_pct", days: int = 0) -> None:
    print("=" * 70)
    days_label = f", 最近{days}天" if days > 0 else ", 全部历史"
    print(f"回测归因报告 (metric={metric}{days_label})")
    print("=" * 70)
    # P1-6 (2026-08-10): 高估方向标注。cum_2d/cum_3d 以推荐日收盘价(T+0 close)为起点，
    # 但推荐发生在盘中——推荐后到收盘的涨幅已计入策略收益，早盘推荐高估最明显。
    # 全库无推荐时刻买入价（无分钟快照），暂无法精确还原，先标注方向供解读。
    if metric in ("cum_2d", "cum_3d"):
        print('  注: cum_*d 用推荐日收盘价为起点，盘中推荐的票把"推荐后到收盘涨幅"计入了收益，')
        print("      结果系统性高估（早盘推荐尤甚）。解读时需保留这一余量。")
    print()

    print("\n[1] 分策略表现")
    print(f"{'类别':<16}{'样本':>6}{'胜率':>8}{'均收益':>9}{'盈亏比':>8}{'IC':>8}{'均分':>8}")
    stats = strategy_performance(conn, metric, days=days)
    for s in stats:
        print(
            f"{s.category:<16}{s.count:>6}{s.win_rate * 100:>7.1f}%"
            f"{s.avg_return:>9.2f}{s.profit_loss_ratio:>8.2f}"
            f"{s.ic_score:>8.3f}{s.avg_score:>8.1f}"
        )
    # P1-5 (2026-08-10): 全推荐汇总作为"不挑选买入全部推荐"的无选择基准行，
    # 便于判断各策略是否跑赢"全量摊大饼"。指数历史（创业板指）库内无数据，
    # 暂用此候选池基准代替。
    tot_n = sum(s.count for s in stats)
    if tot_n:
        tot_ret = sum(s.avg_return * s.count for s in stats) / tot_n
        tot_win = sum(s.win_rate * s.count for s in stats) / tot_n
        print(f"{'ALL(全推荐基准)':<16}{tot_n:>6}{tot_win * 100:>7.1f}%{tot_ret:>9.2f}")

    print("\n[2] 分维度 IC（降序，正=加分越多收益越好）")
    print(f"{'维度':<28}{'样本':>6}{'IC':>8}{'加正分均收益':>14}{'零分均收益':>12}")
    for d in dimension_ic(conn, metric, days=days):
        ap = f"{d.avg_return_when_positive:.2f}" if d.avg_return_when_positive == d.avg_return_when_positive else "n/a"
        az = f"{d.avg_return_when_zero:.2f}" if d.avg_return_when_zero == d.avg_return_when_zero else "n/a"
        tag = "  <== 反指" if d.ic < -0.02 else ("  <== 强信号" if d.ic > 0.02 else "")
        print(f"{d.dimension:<28}{d.hits:>6}{d.ic:>8.3f}{ap:>14}{az:>12}{tag}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="创业板扫描策略回测归因")
    parser.add_argument("--days", type=int, default=0, help="仅分析最近 N 天的推荐（0=全部）")
    parser.add_argument(
        "--metric", default="next_day_pct", choices=["next_day_pct", "fwd_3d", "fwd_5d", "cum_2d", "cum_3d"]
    )
    parser.add_argument(
        "--ranking", action="store_true", help="综合排序类别优先级校准报告（默认 next_day_pct，近期30天窗口）"
    )
    parser.add_argument("--backfill", action="store_true", help="回填 N 日收益字段")
    parser.add_argument("--dry-run", action="store_true", help="回填预览不写库")
    return parser


def main() -> None:
    args = build_parser().parse_args()

    clear_screen()
    conn = sqlite3.connect(DB_PATH)
    if args.backfill:
        n = backfill_outcomes(conn, dry_run=args.dry_run)
        print(f"回填更新行数: {n}" + (" (dry-run)" if args.dry_run else ""))
    if args.ranking:
        print_ranking_report(conn, metric=args.metric, recent_days=args.days or 30)
    else:
        print_report(conn, metric=args.metric, days=args.days)
    conn.close()


if __name__ == "__main__":
    main()
