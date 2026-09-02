#!/usr/bin/env python
"""v2 池内部过滤器摸底分析（只读，plan→证据→准入闸三步走的第一步）。

目的：回答「哪些内部过滤器剔除的组次日确实更差」——为 榜单排名准入 / 在榜疲劳 /
标签准入 / acc5 分位 四个候选维度提供采纳/放弃的数据证据。
证据标准与 2026-09-02 K 线软降级一致化：只有「剔除组次日表现显著差于池内整体」
的过滤器才建议采纳，否则明确标注不采纳（防止重新误杀强势票）。

用法:
    python scripts/pool_filter_scan.py                  # 默认近 60 个自然日样本
    python scripts/pool_filter_scan.py --days 120
    python scripts/pool_filter_scan.py --dim rank board_days labels
    python scripts/pool_filter_scan.py --csv out.csv    # 明细落 CSV
    python scripts/pool_filter_scan.py --backfill       # 把 next_day_pct 回填 pool_log（显式写库）

口径（单源）：
- hit7 = 次日收益 ≥ NEXTDAY_HIT_THRESHOLD（config，与归因/回测同口径）
- 次日收益 = 次日 close / 当日 close - 1（daily_kline，按"该票下一根 K 线"推进，自然跳过非交易日）
- board_days = appearances 连续在榜天数（当日计入；按全局扫描日期序回溯连续段）
只读：默认不写任何表（--backfill 显式豁免）。fail-open 读库。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_reconfigure = getattr(sys.stdout, "reconfigure", None)
if sys.platform == "win32" and callable(_reconfigure):
    _reconfigure(encoding="utf-8")

from scanner.config import DB_PATH, NEXTDAY_HIT_THRESHOLD  # noqa: E402
from scanner.pool import compute_acc5, compute_bias20  # noqa: E402 — 口径单源，不自造

# 分桶定义（label, 谓词）——谓词接收行 dict，返回 True 表示落入本桶
RANK_BUCKETS: list[tuple[str, Callable[[dict], bool]]] = [
    ("1-10", lambda r: r["rank"] is not None and 1 <= r["rank"] <= 10),
    ("11-20", lambda r: r["rank"] is not None and 11 <= r["rank"] <= 20),
    ("21-30", lambda r: r["rank"] is not None and 21 <= r["rank"] <= 30),
    ("31-50", lambda r: r["rank"] is not None and 31 <= r["rank"] <= 50),
    ("51+", lambda r: r["rank"] is not None and r["rank"] > 50),
    ("无排名", lambda r: r["rank"] is None),
]
BOARD_DAYS_BUCKETS: list[tuple[str, Callable[[dict], bool]]] = [
    ("1(首日)", lambda r: r["board_days"] == 1),
    ("2-3", lambda r: 2 <= r["board_days"] <= 3),
    ("4-7", lambda r: 4 <= r["board_days"] <= 7),
    ("8+(疲劳)", lambda r: r["board_days"] >= 8),
]
ACC5_BUCKETS: list[tuple[str, Callable[[dict], bool]]] = [
    ("<0", lambda r: r["acc5"] is not None and r["acc5"] < 0),
    ("0-15", lambda r: r["acc5"] is not None and 0 <= r["acc5"] < 15),
    ("15-30", lambda r: r["acc5"] is not None and 15 <= r["acc5"] < 30),
    (">30(过热)", lambda r: r["acc5"] is not None and r["acc5"] >= 30),
    ("无数据", lambda r: r["acc5"] is None),
]
MCAP_BUCKETS: list[tuple[str, Callable[[dict], bool]]] = [
    ("<20亿", lambda r: r["market_cap"] is not None and r["market_cap"] < 20),
    ("20-50亿", lambda r: r["market_cap"] is not None and 20 <= r["market_cap"] < 50),
    ("50-100亿", lambda r: r["market_cap"] is not None and 50 <= r["market_cap"] < 100),
    (">100亿", lambda r: r["market_cap"] is not None and r["market_cap"] >= 100),
    ("无数据", lambda r: r["market_cap"] is None),
]
# 采纳判据：剔除该桶后剩余池 hit7 提升 ≥ MIN_LIFT_PP 个百分点 且 样本损失 ≤ MAX_LOSS_RATIO
MIN_LIFT_PP = 1.0
MAX_LOSS_RATIO = 0.5


def _pct(data: list[float], q: float) -> float:
    """分位数（0<=q<=1）；样本 <2 时退化为中位/单值。"""
    if not data:
        return 0.0
    if len(data) == 1:
        return data[0]
    try:
        qs = statistics.quantiles(data, n=100)
        return qs[min(98, max(0, int(q * 100) - 1))]
    except statistics.StatisticsError:
        return statistics.median(data)


def load_rows_replay(conn: sqlite3.Connection, days: int) -> list[dict]:
    """回放模式：用 appearances（在榜历史）+ daily_kline 重建历史池近似样本。

    pool_log 观测表上线晚（仅数日样本），replay 用跨月 appearances 提供统计功效。
    近似性偏差（报告必读）：
    - 含被 v1 市值/价格准入剔除的票（小叶美过滤未回放）；
    - 无 market_cap / dip_labels（两维度在 replay 下不可用，自动跳过）；
    - acc5/bias20 由 daily_kline 收盘价现算（复用 scanner.pool 口径单源）。
    """
    app: dict[str, dict[str, int]] = {}  # date -> {symbol: rank}
    sym_dates: dict[str, set[str]] = {}
    for d, sym, rk in conn.execute("SELECT date, symbol, rank FROM appearances WHERE rank IS NOT NULL"):
        app.setdefault(d, {})[sym] = rk
        sym_dates.setdefault(sym, set()).add(d)
    # 每票收盘序列（升序）+ date 索引
    closes: dict[str, list[tuple[str, float]]] = {}
    for sym, d, c in conn.execute("SELECT symbol, date, close FROM daily_kline WHERE close IS NOT NULL ORDER BY date"):
        try:
            closes.setdefault(sym, []).append((d, float(c)))
        except (TypeError, ValueError):
            continue
    rows: list[dict] = []
    import datetime as _dt

    cutoff = (_dt.date.today() - _dt.timedelta(days=days)).isoformat()
    for d in sorted(app):
        if d < cutoff:
            continue
        for sym, rk in app[d].items():
            lst = closes.get(sym, [])
            pos = next((i for i, (kd, _) in enumerate(lst) if kd == d), None)
            closes_upto = [c for kd, c in lst[: pos + 1]] if pos is not None else []
            r: dict[str, Any] = {
                "date": d,
                "symbol": sym,
                "name": "",
                "percent": None,
                "rank": rk,
                "rank_trend": None,
                "bias20": compute_bias20(closes_upto),
                "acc5": compute_acc5(closes_upto),
                "market_cap": None,
            }
            rows.append(r)
    _attach_next_pct(conn, rows)
    _attach_board_days(rows, sym_dates, sorted(app))
    for r in rows:
        r["labels"] = []
    return rows


def _attach_next_pct(conn: sqlite3.Connection, rows: list[dict]) -> None:
    """从 daily_kline 现算次日收益（pool_log.next_day_pct 未被回填，不可信）。"""
    klines: dict[str, list[tuple[str, float]]] = {}
    for sym, d, c in conn.execute("SELECT symbol, date, close FROM daily_kline WHERE close IS NOT NULL ORDER BY date"):
        if not sym or not d or c is None:
            continue
        try:
            klines.setdefault(sym, []).append((d, float(c)))
        except (TypeError, ValueError):
            continue
    idx: dict[str, dict[str, int]] = {s: {d: i for i, (d, _) in enumerate(lst)} for s, lst in klines.items()}
    valid = 0
    for r in rows:
        lst = klines.get(r["symbol"])
        i = idx.get(r["symbol"], {}).get(r["date"])
        r["next_pct"] = None
        r["hit7"] = None
        if lst and i is not None and i + 1 < len(lst):
            today_close = lst[i][1]
            next_close = lst[i + 1][1]
            if today_close > 0:
                r["next_pct"] = (next_close / today_close - 1) * 100.0
                r["hit7"] = r["next_pct"] >= NEXTDAY_HIT_THRESHOLD
                valid += 1
    print(f"次日收益：{valid}/{len(rows)} 行有效（其余剔除：次日停牌/数据缺失）")


def _attach_board_days(rows: list[dict], sym_dates: dict[str, set[str]], all_scan_dates: list[str]) -> None:
    """appearances 连续在榜天数：当日计入，按全局扫描日期序回溯连续段。"""
    date_pos = {d: i for i, d in enumerate(all_scan_dates)}
    for r in rows:
        dates = sym_dates.get(r["symbol"], set())
        pos = date_pos.get(r["date"])
        if pos is None or r["date"] not in dates:
            r["board_days"] = 1
            continue
        n = 1
        for back in range(1, pos + 1):
            if all_scan_dates[pos - back] in dates:
                n += 1
            else:
                break
        r["board_days"] = n


def load_rows_pool_log(conn: sqlite3.Connection, days: int) -> list[dict]:
    """pool_log 全量快照行 + 次日收益/hit7/board_days/labels 富化。"""
    start = f"{days} days ago"
    rows = [
        {
            "date": r[0],
            "symbol": r[1],
            "name": r[2],
            "percent": r[3],
            "rank": r[4],
            "rank_trend": r[5],
            "bias20": r[6],
            "acc5": r[7],
            "market_cap": r[8],
        }
        for r in conn.execute(
            "SELECT date, symbol, name, percent, rank, rank_trend, bias20, acc5, market_cap "
            "FROM pool_log WHERE date >= date('now', ?) ORDER BY date, rank",
            (start,),
        )
    ]
    if not rows:
        return []
    _attach_next_pct(conn, rows)

    # board_days：appearances 全局扫描日期序回溯连续段
    app_dates: dict[str, set[str]] = {}
    for d, sym in conn.execute("SELECT date, symbol FROM appearances"):
        app_dates.setdefault(sym, set()).add(d)
    all_scan_dates = sorted({d for s in app_dates.values() for d in s})
    _attach_board_days(rows, app_dates, all_scan_dates)

    # labels：recommendations(pool_pick) score_breakdown JSON → dip_labels
    labels_map: dict[tuple[str, str], list] = {}
    for d, sym, sb in conn.execute(
        "SELECT date, symbol, score_breakdown FROM recommendations WHERE category='pool_pick'"
    ):
        if not sb:
            continue
        try:
            parsed = json.loads(sb) if isinstance(sb, str) else sb
            labels = parsed.get("dip_labels") if isinstance(parsed, dict) else None
            if isinstance(labels, list) and labels:
                labels_map[(d, sym)] = labels
        except (json.JSONDecodeError, TypeError):
            continue
    for r in rows:
        r["labels"] = labels_map.get((r["date"], r["symbol"]), [])
    return rows


def _bucket_stats(rows: list[dict[str, Any]]) -> tuple[int, float, float, float, float]:
    """返回 (n, hit7率%, 次日均值, 中位数, P75)。"""
    pcts: list[float] = []
    for r in rows:
        v = r.get("next_pct")
        if v is None:
            continue
        try:
            pcts.append(float(v))
        except (TypeError, ValueError):
            continue
    n_valid = len(pcts)
    if not pcts:
        return 0, 0.0, 0.0, 0.0, 0.0
    try:
        hit_rate = sum(1 for p in pcts if p >= NEXTDAY_HIT_THRESHOLD) / n_valid * 100.0
        return (
            n_valid,
            float(hit_rate),
            float(statistics.mean(pcts)),
            float(statistics.median(pcts)),
            float(_pct(pcts, 0.75)),
        )
    except (statistics.StatisticsError, TypeError, ValueError):
        return n_valid, 0.0, 0.0, 0.0, 0.0


def report_dimension(
    rows: list[dict], dim: str, buckets: list[tuple[str, Callable[[dict], bool]]], overall: tuple
) -> None:
    """单维度分桶报告 + 剔除模拟 + 采纳判据。"""
    total_n, total_hit, total_mean, _, _ = overall
    print(f"\n── 维度：{dim}（全池 n={total_n}, hit7={total_hit:.1f}%, 均值={total_mean:+.2f}%）──")
    print(
        f"  {'桶':<10} {'n':>6} {'占比':>6} {'hit7%':>7} {'次日均值':>9} {'中位':>7} {'P75':>7}  剔除后hit7→(Δ)  判定"
    )
    for label, pred in buckets:
        sub = [r for r in rows if pred(r)]
        n, hit, mean, med, p75 = _bucket_stats(sub)
        if n == 0:
            print(f"  {label:<10} {0:>6} {'—':>6} {'—':>7} {'—':>9} {'—':>7} {'—':>7}  —")
            continue
        # 剔除该桶后的剩余池 hit7
        rest = [r for r in rows if not pred(r)]
        rest_n, rest_hit, _, _, _ = _bucket_stats(rest)
        delta = rest_hit - total_hit
        loss = n / total_n if total_n else 0.0
        adopt = delta >= MIN_LIFT_PP and loss <= MAX_LOSS_RATIO
        verdict = "✓建议采纳" if adopt else ("~边缘" if delta > 0 else "✗不采纳(剔除更差或无提升)")
        print(
            f"  {label:<10} {n:>6} {loss * 100:>5.1f}% {hit:>7.1f} {mean:>+9.2f} {med:>+7.2f} {p75:>+7.2f}"
            f"  {rest_hit:>7.1f} ({delta:>+5.1f})  {verdict}"
        )


def report_label_detail(rows: list[dict], overall: tuple) -> None:
    """标签细分：每个标签类型的单独表现（labels 维度的深化）。"""
    all_labels = sorted({lb for r in rows for lb in r["labels"]})
    if not all_labels:
        print("\n── 标签细分：样本期内无任何 dip_labels（matcher 上线晚于样本起点）──")
        return
    total_n, total_hit, _, _, _ = overall
    print(f"\n── 标签细分（全池 hit7={total_hit:.1f}%）──")
    for lb in all_labels:
        sub = [r for r in rows if lb in r["labels"]]
        n, hit, mean, _, _ = _bucket_stats(sub)
        delta = hit - total_hit
        print(f"  {lb:<8} n={n:>4}  hit7={hit:>5.1f}% (Δ{delta:>+5.1f})  均值={mean:+.2f}%")


def report_combos(rows: list[dict], overall: tuple) -> None:
    """组合过滤模拟：候选准入闸联合效果的预演。"""
    total_n, total_hit, _, _, _ = overall
    combos = [
        ("rank≤30", lambda r: r["rank"] is not None and r["rank"] <= 30),
        ("rank≤50", lambda r: r["rank"] is not None and r["rank"] <= 50),
        ("board_days≤3", lambda r: r["board_days"] <= 3),
        ("rank≤30 ∧ board_days≤3", lambda r: r["rank"] is not None and r["rank"] <= 30 and r["board_days"] <= 3),
        ("有低吸标签", lambda r: bool(r["labels"])),
        ("有标签 ∧ rank≤30", lambda r: bool(r["labels"]) and r["rank"] is not None and r["rank"] <= 30),
    ]
    print(f"\n── 组合准入模拟（全池 n={total_n}, hit7={total_hit:.1f}%）──")
    for name, pred in combos:
        kept = [r for r in rows if pred(r)]
        n, hit, mean, _, _ = _bucket_stats(kept)
        if n == 0:
            print(f"  {name:<24} 保留 0 行")
            continue
        delta = hit - total_hit
        print(
            f"  {name:<24} 保留 {n:>4}/{total_n}（{n / total_n * 100:>5.1f}%）  "
            f"hit7={hit:>5.1f}% (Δ{delta:>+5.1f})  均值={mean:+.2f}%"
        )


def backfill(conn: sqlite3.Connection, rows: list[dict]) -> None:
    """把算好的 next_day_pct 回填 pool_log（显式 --backfill 才执行）。"""
    filled = 0
    for r in rows:
        if r["next_pct"] is None:
            continue
        cur = conn.execute(
            "UPDATE pool_log SET next_day_pct=? WHERE date=? AND symbol=?", (r["next_pct"], r["date"], r["symbol"])
        )
        filled += cur.rowcount
    conn.commit()
    print(f"[backfill] pool_log.next_day_pct 已回填 {filled} 行")


def main() -> None:
    parser = argparse.ArgumentParser(description="v2 池内部过滤器摸底分析（只读）")
    parser.add_argument("--days", type=int, default=60, help="回看自然日数（默认 60）")
    parser.add_argument("--dim", nargs="*", default=["rank", "board_days", "acc5", "mcap", "labels"], help="分析维度")
    parser.add_argument("--csv", type=str, default=None, help="明细落 CSV 路径")
    parser.add_argument(
        "--source",
        choices=["auto", "pool_log", "replay"],
        default="auto",
        help="样本源：auto=pool_log 样本不足自动回放（默认）/ pool_log / replay",
    )
    parser.add_argument(
        "--backfill", action="store_true", help="回填 pool_log.next_day_pct（显式写库，仅 pool_log 源）"
    )
    args = parser.parse_args()

    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True) if not args.backfill else sqlite3.connect(DB_PATH)
    conn.row_factory = None
    try:
        rows = load_rows_pool_log(conn, args.days)
        if args.source in ("replay", "auto"):
            valid_pool = sum(1 for r in rows if r["next_pct"] is not None)
            if args.source == "replay" or valid_pool < 100:
                if args.source == "auto":
                    print(f"[auto] pool_log 有效样本仅 {valid_pool} 行（<100），自动切换 replay 回放模式")
                rows = load_rows_replay(conn, args.days)
                print("[replay 近似性偏差] 含被 v1 市值/价格准入剔除的票；无 market_cap/dip_labels（两维度跳过）")
        if not rows:
            print("无样本数据（appearances/pool_log 均为空）")
            return
        # 只分析有次日收益的行
        sample = [r for r in rows if r["next_pct"] is not None]
        if not sample:
            print("无有效样本（全部缺次日 K 线）")
            return
        earliest = min(r["date"] for r in sample)
        print(f"有效样本期：{earliest} ~ {max(r['date'] for r in sample)}（hit7 阈值 {NEXTDAY_HIT_THRESHOLD}%）")

        overall = _bucket_stats(sample)
        dims = {
            "rank": RANK_BUCKETS,
            "board_days": BOARD_DAYS_BUCKETS,
            "acc5": ACC5_BUCKETS,
            "mcap": MCAP_BUCKETS,
        }
        for dim in args.dim:
            if dim == "labels":
                has_label = ("有标签", lambda r: bool(r["labels"]))
                no_label = ("无标签", lambda r: not r["labels"])
                report_dimension(sample, "labels", [has_label, no_label], overall)
                report_label_detail(sample, overall)
            elif dim in dims:
                report_dimension(sample, dim, dims[dim], overall)
        report_combos(sample, overall)
        print(
            f"\n采纳判据：剔除后 hit7 提升 ≥{MIN_LIFT_PP}pp 且 样本损失 ≤{MAX_LOSS_RATIO * 100:.0f}% → ✓；"
            f"剔除更差 → ✗（与 K 线软降级证据标准一致化）"
        )

        if args.csv:
            import csv

            with open(args.csv, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(
                    [
                        "date",
                        "symbol",
                        "name",
                        "rank",
                        "board_days",
                        "acc5",
                        "bias20",
                        "market_cap",
                        "labels",
                        "next_pct",
                        "hit7",
                    ]
                )
                for r in sample:
                    w.writerow(
                        [
                            r["date"],
                            r["symbol"],
                            r["name"],
                            r["rank"],
                            r["board_days"],
                            r["acc5"],
                            r["bias20"],
                            r["market_cap"],
                            "|".join(r["labels"]),
                            f"{r['next_pct']:.2f}",
                            int(r["hit7"]),
                        ]
                    )
            print(f"明细已写入 {args.csv}")
        if args.backfill:
            if args.source == "replay":
                print("[backfill] replay 模式不回填（样本非 pool_log 快照）")
            else:
                backfill(conn, sample)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
