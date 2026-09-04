"""次日大涨归因仪表盘（next-day spike attribution）。

本模块与 scanner.backtest（--metric next_day_pct）共同构成「次日大涨」目标的校准闭环；
综合排序 / 类别优先级 / 档位 / 建议列（2026-08-18 起）统一以本模块口径（next_day≥7% hit）
为唯一决策口径，cum_3d 不再参与排序与调参。

背景（2026-08-10）：用户偏好次日大涨票，但系统决策口径曾是 cum_3d（持有 2-3 天），
两者是不同目标——cum_3d 好的「8-12% 档」在 next_day 口径反而是 -1.32%（次日回吐）。
2026-08-18 用户决定统一口径为「次日大涨」，本模块从「子目标校准」升级为**主决策口径**。

定位（与 AGENTS 回测定位一致）：本项目是筛选系统，本模块是**校准仪表盘**，
只回答「哪些信号/涨幅带/类别对次日大涨有区分度」，不进入实时扫描路径。
已知局限：next_day_pct 为单日口径，次日大涨票次日多为高开冲高，切勿把结论
当作「能赚钱」的裁判（收益验证仍需组合回测，但排序/调参不再以 cum_3d 复核）。

输出：
  1. 分策略：样本 / hit 率(≥5%/≥7%/≥10%) / 平均次日 / rank-IC(score→next_day)
  2. 涨幅带矩阵：推荐时刻盘中涨幅 × 次日大涨 hit 率（找甜蜜区）
  3. score 分桶：score × 次日大涨（找分数反指区）
  4. 维度归因：落库 score_breakdown 各维度 hit 组 vs 非 hit 组差值
  5. 条件 hit：overbought / 弱转强 / rank 加分 / MA 排列 等二元因子的条件 hit 率

用法：
  python -m scanner.nextday_attribution [--days N] [--threshold 7] [--csv prefix]
  python -m scanner.nextday_attribution --excess          # 超额口径（剔除大盘 beta）

口径说明见下方 METRIC_RAW / METRIC_EXCESS 常量注释。
"""
from __future__ import annotations

import argparse
import csv
import sqlite3
from collections import Counter, defaultdict
from typing import Any, Callable

from scanner.backtest import load_attribution_rows, spearman
from scanner.config import DB_PATH, NEXTDAY_HIT_THRESHOLD
from scanner.data_health import check_kline_health, health_banner
from scanner.database import get_prominence_map
from scanner.models import parse_score_breakdown
from scanner.utils import EXTERNAL_FAILURES, clear_screen

DEFAULT_THRESHOLD = NEXTDAY_HIT_THRESHOLD   # 单源见 config，兼容旧 import
DEFAULT_RECENT_DAYS = 0   # 0=全部历史；>0=最近 N 天

# 分桶样本门槛（2026-08-18，防噪声行动）：组样本 < MIN_SAMPLE 标「⚠样本不足」，
# 结论不可信、仅作观察——8-10% 反转案例（n=41 的 14.6% vs 全期 n=1184 的 7.5%）
# 证明小样本差异大概率是噪声。与 AGENTS.md 回测纪律（样本不足不下结论）对齐。
MIN_SAMPLE = 20

# ── 归因口径（2026-09-04 P0-1）──
# METRIC_RAW：原始次日收益（历史默认口径，config 里几十个阈值均按此拟合）。
# METRIC_EXCESS：超额收益 = 次日收益 − T+1 创业板指涨幅。
#
# 为什么必须有 EXCESS：market_index_log 表建于 2026-08-19，此前 68 个交易日无基准，
# 于是所有历史归因算的都是**含市场 beta 的原始收益**。实测 7 月推荐池日均 -1.573%，
# 而同期创业板指日均 -1.066% —— 约 2/3 的"亏损"是大盘在跌，不是选股选得差，
# 却可能被当成因子特性拟合进阈值。
#
# 用 `python backfill_market_index.py` 补齐基准后，加 --excess 切换本口径。
# ⚠ 两个口径的 hit 率含义不同：EXCESS 下 threshold 衡量的是"跑赢大盘 7 个点"，
#   样本 hit 率会显著低于 RAW，不要跨口径比较数字。
METRIC_RAW = "next_day"
METRIC_EXCESS = "excess"
METRIC_LABEL = {METRIC_RAW: "平均次日", METRIC_EXCESS: "平均超额"}
# 基准缺失率超过该比例即告警（fail-loud：宁可报错也不要拿残缺样本下结论）
EXCESS_COVERAGE_WARN = 0.90

# 维度归因只关心「正值与否」即可区分，避免数值口径差异
_BIN_DIMS = ("v_st_overbought", "v_st_weak", "v_mo_divergence", "v_nf_volume",
             "v_st_ma", "rank_trend_bonus", "validation_bonus")

# 二元因子条件 hit 率表（2026-08-10 新增辨识度）：
#   辨识度 = 近 5 交易日上榜 ≥3 天 + 历史日平均排名 ≤ 70（复用 database.get_prominence_map，
#   与 enhancer/display 同一实现，防口径漂移）。数据：辨识度 hit 16~24% vs 非辨识度 6~10%，
#   当前最强单因子；「前N日曾推」与其 67% 重合、独立增量≈0，故用辨识度而非推荐历史。
# 2026-08-29：第二元素原标注为 object，导致 `fn(r)` 被 mypy 判为 "object not callable"，
# 且掩盖了 lambda 签名错误。实为「记录 → 是否命中」的谓词。
FACTOR_CONDITIONS: list[tuple[str, Callable[[dict], bool]]] = [
    ("辨识度(↻反复上榜)", lambda r: r.get("_prominent") is True),
    ("非辨识度", lambda r: r.get("_prominent") is False),
    ("short_term 超买", lambda r: bool(_parse(r).get("v_st_overbought"))),
    ("short_term 非超买", lambda r: not _parse(r).get("v_st_overbought")),
    ("short_term 弱转强", lambda r: _parse(r).get("st_weak_to_strong", 0) > 0),
    ("momentum MA3头", lambda r: _parse(r).get("v_mo_ma", 0) == 6),
    ("momentum 超买", lambda r: bool(_parse(r).get("v_mo_overbought"))),
    ("new_face 收敛≥2", lambda r: (_parse(r).get("v_nf_convergence_hits", 0) or 0) >= 2),
]


def _load_dedup(conn: sqlite3.Connection, days: int = 0) -> list[dict]:
    """加载现役类别推荐，同 (date, symbol) 去重（口径统一：excluded=0 + 取最后一轮）。

    2026-09-02 修复（样本口径统一，本模块是档位/画像阈值的校准数据源，影响面最大）：
    1. **缺 excluded = 0**：把已被硬过滤判定"不该买"的票计入归因，等于给明知不可买
       的票记成绩（当前 70 行），直接污染「档3 劣后因子是否真的更差」的判定。
    2. **去重取最高分 → 分数选择偏差**：按 score 挑选样本会系统性偏向高分档，而本
       模块正是"分数分档 → hit 率"的校准源，该偏差会自我强化（高分档 hit 被人为抬
       高，进而佐证"高分更好"）。现与 walkforward / backtest 统一为「取最后一轮」，
       不按 score 挑选。
    3. **去重前样本虚高 2.19x**（3882 行 → 1774 个 (date, symbol)），且重复次数与
       停留榜上时长正相关，属有偏加权：momentum hit 16.5%(n=508) → 10.0%(n=290)。

    注：函数名保留 _load_dedup（去重语义不变），仅口径改为统一标准。
    """
    rows = load_attribution_rows(
        conn, "next_day_pct", cols="name, percent, score_breakdown", days=days
    )
    return [
        {
            "date": r["date"], "symbol": r["symbol"], "name": r["name"],
            "category": r["category"], "score": r["score"],
            "percent": r["percent"] or 0.0, "next_day": r["next_day_pct"],
            "breakdown": r["score_breakdown"],
        }
        for r in rows
    ]


def _parse(d: dict) -> dict:
    return parse_score_breakdown(d.get("breakdown"))


def _attach_prominence(conn: sqlite3.Connection, recs: list[dict]) -> list[dict]:
    """按推荐日视角给每条记录附加辨识度（↻）标记 r["_prominent"]。

    复用 database.get_prominence_map（与 enhancer/display 同一实现，防口径漂移），
    按 as_of_date 回放：判定「推荐当天」的辨识度窗口，而非真实今日。
    无 appearances 表（如单测库）时置 None = 未知，避免把「不可算」误标为「非辨识度」。
    """
    try:
        conn.execute("SELECT 1 FROM appearances LIMIT 1").fetchone()
    except EXTERNAL_FAILURES:
        for r in recs:
            r["_prominent"] = None
        return recs
    by_date: dict[str, list[dict]] = defaultdict(list)
    for r in recs:
        by_date[r["date"]].append(r)
    for d, group in by_date.items():
        syms = [r["symbol"] for r in group]
        pmap = get_prominence_map(conn, syms, as_of_date=d)
        for r in group:
            r["_prominent"] = pmap.get(r["symbol"], False)
    return recs


def _load_index_series(conn: sqlite3.Connection) -> dict[str, float]:
    """读取大盘基准序列 {date: 当日涨跌幅%}（创业板指，market_index_log）。

    无表/无数据返回空 dict，由 _attach_excess 按"不可算"处理而非静默置 0——
    把缺失基准当成 0 会让超额口径退化成原始口径，且无任何提示。
    """
    try:
        rows = conn.execute(
            "SELECT date, index_pct FROM market_index_log WHERE index_pct IS NOT NULL"
        ).fetchall()
    except EXTERNAL_FAILURES:
        return {}
    return {d: float(p) for d, p in rows if d and p is not None}


def _attach_excess(conn: sqlite3.Connection, recs: list[dict]) -> tuple[list[dict], int]:
    """为每条记录附加超额收益 r["excess"] = next_day − 指数在 T+1 的涨幅。

    ⚠ 口径要点（曾差点搞错）：推荐的 next_day 是 **T+1 单日**涨幅，因此要减的是
    **T+1 当天**的指数涨幅，不是推荐日 T 的。减错一天会让超额收益完全失真。

    T+1 取 market_index_log 中按日期排序的下一个交易日（非自然日 +1），
    已在清明/周末/长假等场景自动对齐。

    返回 (可算超额的记录, 因缺基准而丢弃的记录数)。丢弃而非置 0，避免静默失真。
    """
    idx = _load_index_series(conn)
    if not idx:
        return [], len(recs)
    days = sorted(idx)
    nxt = {d: (days[i + 1] if i + 1 < len(days) else None) for i, d in enumerate(days)}

    kept: list[dict] = []
    dropped = 0
    for r in recs:
        t1 = nxt.get(r["date"])
        if t1 is None or t1 not in idx:
            dropped += 1
            continue
        r["excess"] = r["next_day"] - idx[t1]
        kept.append(r)
    return kept, dropped


def _hit_stats(recs: list[dict], threshold: float,
               metric: str = METRIC_RAW) -> tuple[int, float, float]:
    """返回 (hit 数, hit 率, 平均收益)。metric 决定用原始还是超额口径。"""
    n = len(recs)
    if not n:
        return 0, 0.0, 0.0
    vals = [r[metric] for r in recs]
    hits = [v for v in vals if v >= threshold]
    avg = sum(vals) / n
    return len(hits), len(hits) / n, avg


def strategy_table(recs: list[dict], threshold: float,
                   metric: str = METRIC_RAW) -> list[dict[str, Any]]:
    """分策略：样本/hit率/平均/rank-IC。"""
    by_cat: dict[str, list[dict]] = defaultdict(list)
    for r in recs:
        by_cat[r["category"]].append(r)
    out: list[dict[str, Any]] = []
    for cat in sorted(by_cat):
        g = by_cat[cat]
        hits, hr, avg = _hit_stats(g, threshold, metric)
        scores = [r["score"] for r in g]
        ic = spearman(scores, [r[metric] for r in g]) or 0.0
        out.append({"category": cat, "n": len(g), "hits": hits, "hit_rate": hr,
                    "avg_next": avg, "ic": ic, "warn": len(g) < MIN_SAMPLE})
    out.sort(key=lambda x: -x["hit_rate"])
    return out


def gain_band_matrix(recs: list[dict], threshold: float,
                     metric: str = METRIC_RAW) -> list[dict]:
    """推荐时刻盘中涨幅分桶 × hit 率。"""
    # edges(6) 与 labels(7) 长度本就不等：最后一个 label ">=10%" 是无对应 edge 的
    # 兜底桶（下方 `if not placed` 分支兜住）。故 zip 必须 strict=False——
    # 不要"顺手"改成 strict=True，那会让每次分档都抛 ValueError。
    edges = [1, 2, 4, 6, 8, 10]
    labels = ["<1%", "1-2%", "2-4%", "4-6%", "6-8%", "8-10%", ">=10%"]
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in recs:
        p = float(r["percent"])
        placed = False
        for ed, lab in zip(edges, labels, strict=False):
            if p < ed:
                groups[lab].append(r)
                placed = True
                break
        if not placed:
            groups[labels[-1]].append(r)
    out = []
    for lab in labels:
        g = groups[lab]
        if not g:
            continue
        hits, hr, avg = _hit_stats(g, threshold, metric)
        out.append({"band": lab, "n": len(g), "hits": hits, "hit_rate": hr, "avg_next": avg,
                    "warn": len(g) < MIN_SAMPLE})
    return out


def score_bucket_table(recs: list[dict], threshold: float,
                       metric: str = METRIC_RAW) -> list[dict]:
    """score 分桶 × hit 率。"""
    edges = [30, 50, 70, 90, 110]
    labels = ["<30", "30-50", "50-70", "70-90", "90-110", ">=110"]
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in recs:
        s = r["score"]
        placed = False
        for ed, lab in zip(edges, labels, strict=False):
            if s < ed:
                groups[lab].append(r)
                placed = True
                break
        if not placed:
            groups[labels[-1]].append(r)
    out = []
    for lab in labels:
        g = groups[lab]
        if not g:
            continue
        hits, hr, avg = _hit_stats(g, threshold, metric)
        out.append({"bucket": lab, "n": len(g), "hits": hits, "hit_rate": hr, "avg_next": avg,
                    "warn": len(g) < MIN_SAMPLE})
    return out


def dim_compare(recs: list[dict], threshold: float,
                metric: str = METRIC_RAW) -> list[dict[str, Any]]:
    """落库维度：hit 组 vs 非 hit 组 正值率差。"""
    def pos_pct(group: list[dict]) -> dict[str, float]:
        cnt: Counter = Counter()
        tot: Counter = Counter()
        for r in group:
            d = _parse(r)
            for k, v in d.items():
                if isinstance(v, (int, float)) and abs(v) > 0:
                    cnt[k] += 1
                tot[k] += 1
        return {k: cnt[k] / tot[k] for k in tot}

    hits = [r for r in recs if r[metric] >= threshold]
    non = [r for r in recs if r[metric] < threshold]
    hp = pos_pct(hits)
    np_ = pos_pct(non)
    out: list[dict[str, Any]] = []
    for k in hp:
        non_val = np_.get(k, 0.0)
        diff = hp[k] - non_val
        if abs(diff) >= 0.05:
            out.append({"dim": k, "hit_pos": hp[k], "non_pos": non_val, "diff": diff})
    out.sort(key=lambda x: -abs(x["diff"]))
    return out


def conditional_hit_table(recs: list[dict], threshold: float,
                          metric: str = METRIC_RAW) -> list[dict[str, Any]]:
    """二元因子条件 hit 率（样本/hit/hit率/平均次日），供 [5] 节与单测复用。"""
    out: list[dict[str, Any]] = []
    for label, fn in FACTOR_CONDITIONS:
        g = [r for r in recs if fn(r)]
        if not g:
            continue
        hits, hr, avg = _hit_stats(g, threshold, metric)
        out.append({"factor": label, "n": len(g), "hits": hits,
                    "hit_rate": hr, "avg_next": avg, "warn": len(g) < MIN_SAMPLE})
    return out


def _print_table(header: list[str], rows: list[list], widths: list[int] | None = None) -> None:
    if widths is None:
        widths = [max(len(str(h)), *(len(str(r[i])) for r in rows)) if rows else len(str(h))
                  for i, h in enumerate(header)]
    hdr = "  " + "  ".join(str(h).ljust(w) for h, w in zip(header, widths, strict=False))
    print(hdr)
    print("  " + "-" * len(hdr))
    for r in rows:
        print("  " + "  ".join(str(v).ljust(w) for v, w in zip(r, widths, strict=False)))


def print_report(recs: list[dict], threshold: float,
                 metric: str = METRIC_RAW) -> None:
    n = len(recs)
    hits, hr, avg = _hit_stats(recs, threshold, metric)
    print("=" * 78)
    avg_label = METRIC_LABEL.get(metric, "平均")
    tag = "（超额口径：已剔除 T+1 创业板指涨幅）" if metric == METRIC_EXCESS else ""
    print(f"次日大涨归因 (threshold≥{threshold:.0f}%, 去重样本 {n}){tag}")
    print("=" * 78)
    print(f"整体: hit={hits} ({hr*100:.1f}%)  {avg_label}={avg:+.2f}%\n")

    print("[1] 分策略")
    rows = []
    for s in strategy_table(recs, threshold, metric):
        label = f"{s['category']} ⚠样本不足" if s["warn"] else s["category"]
        rows.append([label, str(s["n"]), str(s["hits"]),
                     f"{s['hit_rate']*100:.1f}%", f"{s['avg_next']:+.2f}%", f"{s['ic']:+.3f}"])
    _print_table(["类别", "样本", "hit", "hit率", avg_label, "rank-IC"], rows)

    print("\n[2] 推荐时刻盘中涨幅带（找次日大涨甜蜜区/陷阱）")
    rows = []
    for b in gain_band_matrix(recs, threshold, metric):
        label = f"{b['band']} ⚠样本不足" if b["warn"] else b["band"]
        rows.append([label, str(b["n"]), str(b["hits"]),
                     f"{b['hit_rate']*100:.1f}%", f"{b['avg_next']:+.2f}%"])
    _print_table(["涨幅带", "样本", "hit", "hit率", avg_label], rows)

    print("\n[3] score 分桶（找分数反指区）")
    rows = []
    for b in score_bucket_table(recs, threshold, metric):
        label = f"{b['bucket']} ⚠样本不足" if b["warn"] else b["bucket"]
        rows.append([label, str(b["n"]), str(b["hits"]),
                     f"{b['hit_rate']*100:.1f}%", f"{b['avg_next']:+.2f}%"])
    _print_table(["score桶", "样本", "hit", "hit率", avg_label], rows)

    print("\n[4] 维度归因（hit 组 vs 非 hit 组 正值率差，|Δ|≥5%）")
    rows = []
    for d in dim_compare(recs, threshold, metric):
        rows.append([d["dim"], f"{d['hit_pos']*100:.0f}%", f"{d['non_pos']*100:.0f}%",
                     f"{d['diff']*100:+.0f}%"])
    _print_table(["维度", "hit组正值", "非hit组", "Δ"], rows)

    print("\n[5] 二元因子条件 hit 率")
    rows = []
    for f in conditional_hit_table(recs, threshold, metric):
        label = f"{f['factor']} ⚠样本不足" if f["warn"] else f["factor"]
        rows.append([label, str(f["n"]), str(f["hits"]),
                     f"{f['hit_rate']*100:.1f}%", f"{f['avg_next']:+.2f}%"])
    _print_table(["因子", "样本", "hit", "hit率", avg_label], rows)

    print("\n  注: next_day 为单日口径，次日大涨票多为高开冲高；结论只用于「次日大涨」")
    print("      子目标校准。2026-08-18 起本口径为综合排序唯一决策口径（cum_3d 不再复核）。")
    if metric == METRIC_EXCESS:
        print("      ⚠ 超额口径下 threshold 含义是「跑赢大盘 N 个点」，hit 率显著低于原始口径，")
        print("        不可与原始口径数字直接比较。基准缺失的推荐日已整体剔除（未置 0）。")
    print(f"      ⚠样本不足 = 该组样本 < {MIN_SAMPLE}，差异大概率是噪声，仅作观察不下结论")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="次日大涨归因仪表盘")
    parser.add_argument("--days", type=int, default=DEFAULT_RECENT_DAYS,
                        help="仅最近 N 天（0=全部）")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                        help="次日大涨阈值 %%（默认 7）")
    parser.add_argument("--excess", action="store_true",
                        help="超额口径：收益减去 T+1 创业板指涨幅，剔除市场 beta。"
                             "需先用 python backfill_market_index.py 补基准")
    parser.add_argument("--csv", default=None, help="导出 CSV 前缀")
    parser.add_argument("--force", action="store_true",
                        help="跳过数据健康检查（交叉验证不符时仍强行出报告）")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    clear_screen()
    conn = sqlite3.connect(DB_PATH)
    recs = _load_dedup(conn, days=args.days)
    _attach_prominence(conn, recs)
    # 数据真实性前置检查（2026-08-18 拓斯达脏数据事故）：daily_kline 盘中残留未定稿
    # bar 会静默污染 next_day_pct → 全口径失真；出报告前抽样与独立源（新浪 qfq）交叉
    # 验证，不符比例超阈值即中止，防止「回测验证的是脏数据」再次发生。
    if not args.force:
        dates = sorted({r["date"] for r in recs})
        report = check_kline_health(conn, dates=dates or None)
        banner = health_banner(report)
        if banner:
            print(banner)
        if report.blocked:
            print("  [中止] 数据疑似污染，先跑 python repair_kline.py 修复后重试"
                  "（--force 强行出报告）")
            conn.close()
            return
        # 大盘指数对账（2026-08-19）：大盘标签曾把当日 -6.26% 崩盘读成昨日 -0.93%
        # （展示"大盘中性"）而无痕——涨幅不进 daily_kline，上面的 K 线交叉验证覆盖
        # 不到。读 market_index_log 血缘记录对账独立源（东财），旧 bar/偏差即告警。
        from scanner.data_health import check_market_index_health, index_health_banner

        idx_report = check_market_index_health(conn)
        idx_banner = index_health_banner(idx_report)
        if idx_banner:
            print(idx_banner)
    metric = METRIC_RAW
    if args.excess:
        recs, dropped = _attach_excess(conn, recs)
        total = len(recs) + dropped
        coverage = (len(recs) / total) if total else 0.0
        if not recs:
            print("  [中止] 无可用超额样本：market_index_log 缺基准。"
                  "先跑 python backfill_market_index.py 补齐后重试")
            conn.close()
            return
        if coverage < EXCESS_COVERAGE_WARN:
            # fail-loud：覆盖率过低时样本是有偏的（缺失日非随机），不应静默出报告
            print(f"  [!] 基准覆盖率仅 {coverage*100:.1f}%"
                  f"（{dropped}/{total} 条无 T+1 指数数据，已剔除）")
            print("      缺失日可能系统性集中在某段时间，结论有偏，建议先补基准")
        metric = METRIC_EXCESS
    conn.close()
    print_report(recs, args.threshold, metric)
    if args.csv:
        with open(f"{args.csv}.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([metric, "key", "n", "hits", "hit_rate", "avg", "ic"])
            for s in strategy_table(recs, args.threshold, metric):
                w.writerow(["strategy", s["category"], s["n"], s["hits"],
                            s["hit_rate"], s["avg_next"], s["ic"]])
            for b in gain_band_matrix(recs, args.threshold, metric):
                w.writerow(["band", b["band"], b["n"], b["hits"], b["hit_rate"], b["avg_next"], ""])
            for b in score_bucket_table(recs, args.threshold, metric):
                w.writerow(["score", b["bucket"], b["n"], b["hits"], b["hit_rate"], b["avg_next"], ""])
            for d in dim_compare(recs, args.threshold, metric):
                w.writerow(["dim", d["dim"], "", "", "", "", f"{d['diff']:.3f}"])
        print(f"\n[导出] {args.csv}.csv")


if __name__ == "__main__":
    main()
