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
"""
from __future__ import annotations

import argparse
import csv
import sqlite3
from collections import Counter, defaultdict
from datetime import timedelta
from typing import Any, Callable

from scanner.backtest import ACTIVE_CATEGORIES, spearman
from scanner.config import DB_PATH, NEXTDAY_HIT_THRESHOLD, now_beijing
from scanner.data_health import check_kline_health, health_banner
from scanner.database import get_prominence_map
from scanner.models import parse_score_breakdown
from scanner.utils import clear_screen

DEFAULT_THRESHOLD = NEXTDAY_HIT_THRESHOLD   # 单源见 config，兼容旧 import
DEFAULT_RECENT_DAYS = 0   # 0=全部历史；>0=最近 N 天

# 分桶样本门槛（2026-08-18，防噪声行动）：组样本 < MIN_SAMPLE 标「⚠样本不足」，
# 结论不可信、仅作观察——8-10% 反转案例（n=41 的 14.6% vs 全期 n=1184 的 7.5%）
# 证明小样本差异大概率是噪声。与 AGENTS.md 回测纪律（样本不足不下结论）对齐。
MIN_SAMPLE = 20

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
    """加载现役类别推荐，同 (date, symbol) 去重保留最高分（综合排序展示口径）。"""
    params = list(ACTIVE_CATEGORIES)
    if days > 0:
        cutoff = (now_beijing() - timedelta(days=days)).date().isoformat()
        date_filter = "AND date >= ? "
        params.append(cutoff)
    else:
        date_filter = ""
    rows = conn.execute(
        f"SELECT date, symbol, name, category, score, percent, next_day_pct, score_breakdown "  # noqa: S608 - 占位符由 ",".join("?" * n) 生成，值经参数化传入
        f"FROM recommendations "
        f"WHERE category IN ({','.join('?' * len(ACTIVE_CATEGORIES))}) "
        f"AND next_day_pct IS NOT NULL {date_filter}",
        tuple(params),
    ).fetchall()
    best: dict[tuple[str, str], dict] = {}
    for date, symbol, name, category, score, percent, ndp, sb in rows:
        key = (date, symbol)
        if key not in best or score > best[key]["score"]:
            best[key] = {
                "date": date, "symbol": symbol, "name": name, "category": category,
                "score": score, "percent": percent or 0.0, "next_day": ndp,
                "breakdown": sb,
            }
    return list(best.values())


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
    except Exception:
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


def _hit_stats(recs: list[dict], threshold: float) -> tuple[int, float, float]:
    """返回 (hit 数, hit 率, 平均次日)。"""
    n = len(recs)
    if not n:
        return 0, 0.0, 0.0
    hits = [r for r in recs if r["next_day"] >= threshold]
    avg = sum(r["next_day"] for r in recs) / n
    return len(hits), len(hits) / n, avg


def strategy_table(recs: list[dict], threshold: float) -> list[dict[str, Any]]:
    """分策略：样本/hit率/平均/rank-IC。"""
    by_cat: dict[str, list[dict]] = defaultdict(list)
    for r in recs:
        by_cat[r["category"]].append(r)
    out: list[dict[str, Any]] = []
    for cat in sorted(by_cat):
        g = by_cat[cat]
        hits, hr, avg = _hit_stats(g, threshold)
        scores = [r["score"] for r in g]
        ic = spearman(scores, [r["next_day"] for r in g]) or 0.0
        out.append({"category": cat, "n": len(g), "hits": hits, "hit_rate": hr,
                    "avg_next": avg, "ic": ic, "warn": len(g) < MIN_SAMPLE})
    out.sort(key=lambda x: -x["hit_rate"])
    return out


def gain_band_matrix(recs: list[dict], threshold: float) -> list[dict]:
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
        hits, hr, avg = _hit_stats(g, threshold)
        out.append({"band": lab, "n": len(g), "hits": hits, "hit_rate": hr, "avg_next": avg,
                    "warn": len(g) < MIN_SAMPLE})
    return out


def score_bucket_table(recs: list[dict], threshold: float) -> list[dict]:
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
        hits, hr, avg = _hit_stats(g, threshold)
        out.append({"bucket": lab, "n": len(g), "hits": hits, "hit_rate": hr, "avg_next": avg,
                    "warn": len(g) < MIN_SAMPLE})
    return out


def dim_compare(recs: list[dict], threshold: float) -> list[dict[str, Any]]:
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

    hits = [r for r in recs if r["next_day"] >= threshold]
    non = [r for r in recs if r["next_day"] < threshold]
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


def conditional_hit_table(recs: list[dict], threshold: float) -> list[dict[str, Any]]:
    """二元因子条件 hit 率（样本/hit/hit率/平均次日），供 [5] 节与单测复用。"""
    out: list[dict[str, Any]] = []
    for label, fn in FACTOR_CONDITIONS:
        g = [r for r in recs if fn(r)]
        if not g:
            continue
        hits, hr, avg = _hit_stats(g, threshold)
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


def print_report(recs: list[dict], threshold: float) -> None:
    n = len(recs)
    hits, hr, avg = _hit_stats(recs, threshold)
    print("=" * 78)
    print(f"次日大涨归因 (threshold≥{threshold:.0f}%, 去重样本 {n})")
    print("=" * 78)
    print(f"整体: hit={hits} ({hr*100:.1f}%)  平均次日={avg:+.2f}%\n")

    print("[1] 分策略")
    rows = []
    for s in strategy_table(recs, threshold):
        label = f"{s['category']} ⚠样本不足" if s["warn"] else s["category"]
        rows.append([label, str(s["n"]), str(s["hits"]),
                     f"{s['hit_rate']*100:.1f}%", f"{s['avg_next']:+.2f}%", f"{s['ic']:+.3f}"])
    _print_table(["类别", "样本", "hit", "hit率", "平均次日", "rank-IC"], rows)

    print("\n[2] 推荐时刻盘中涨幅带（找次日大涨甜蜜区/陷阱）")
    rows = []
    for b in gain_band_matrix(recs, threshold):
        label = f"{b['band']} ⚠样本不足" if b["warn"] else b["band"]
        rows.append([label, str(b["n"]), str(b["hits"]),
                     f"{b['hit_rate']*100:.1f}%", f"{b['avg_next']:+.2f}%"])
    _print_table(["涨幅带", "样本", "hit", "hit率", "平均次日"], rows)

    print("\n[3] score 分桶（找分数反指区）")
    rows = []
    for b in score_bucket_table(recs, threshold):
        label = f"{b['bucket']} ⚠样本不足" if b["warn"] else b["bucket"]
        rows.append([label, str(b["n"]), str(b["hits"]),
                     f"{b['hit_rate']*100:.1f}%", f"{b['avg_next']:+.2f}%"])
    _print_table(["score桶", "样本", "hit", "hit率", "平均次日"], rows)

    print("\n[4] 维度归因（hit 组 vs 非 hit 组 正值率差，|Δ|≥5%）")
    rows = []
    for d in dim_compare(recs, threshold):
        rows.append([d["dim"], f"{d['hit_pos']*100:.0f}%", f"{d['non_pos']*100:.0f}%",
                     f"{d['diff']*100:+.0f}%"])
    _print_table(["维度", "hit组正值", "非hit组", "Δ"], rows)

    print("\n[5] 二元因子条件 hit 率")
    rows = []
    for f in conditional_hit_table(recs, threshold):
        label = f"{f['factor']} ⚠样本不足" if f["warn"] else f["factor"]
        rows.append([label, str(f["n"]), str(f["hits"]),
                     f"{f['hit_rate']*100:.1f}%", f"{f['avg_next']:+.2f}%"])
    _print_table(["因子", "样本", "hit", "hit率", "平均次日"], rows)

    print("\n  注: next_day 为单日口径，次日大涨票多为高开冲高；结论只用于「次日大涨」")
    print("      子目标校准。2026-08-18 起本口径为综合排序唯一决策口径（cum_3d 不再复核）。")
    print(f"      ⚠样本不足 = 该组样本 < {MIN_SAMPLE}，差异大概率是噪声，仅作观察不下结论")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="次日大涨归因仪表盘")
    parser.add_argument("--days", type=int, default=DEFAULT_RECENT_DAYS,
                        help="仅最近 N 天（0=全部）")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                        help="次日大涨阈值 %%（默认 7）")
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
    conn.close()
    print_report(recs, args.threshold)
    if args.csv:
        with open(f"{args.csv}.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["metric", "key", "n", "hits", "hit_rate", "avg_next", "ic"])
            for s in strategy_table(recs, args.threshold):
                w.writerow(["strategy", s["category"], s["n"], s["hits"],
                            s["hit_rate"], s["avg_next"], s["ic"]])
            for b in gain_band_matrix(recs, args.threshold):
                w.writerow(["band", b["band"], b["n"], b["hits"], b["hit_rate"], b["avg_next"], ""])
            for b in score_bucket_table(recs, args.threshold):
                w.writerow(["score", b["bucket"], b["n"], b["hits"], b["hit_rate"], b["avg_next"], ""])
            for d in dim_compare(recs, args.threshold):
                w.writerow(["dim", d["dim"], "", "", "", "", f"{d['diff']:.3f}"])
        print(f"\n[导出] {args.csv}.csv")


if __name__ == "__main__":
    main()
