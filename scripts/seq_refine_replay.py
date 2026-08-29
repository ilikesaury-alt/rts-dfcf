#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""排序序列细化规则回放（2026-08-27）。

背景：display_priority「排序序列」偏好视图现行 6 键 =
  有榜排名 → 档位 → 回调型核心票 → 榜单排名升序 → 新面孔 → 涨幅升序。
本脚本把每个主键拆成可判读的分桶，逐桶测次日表现（hit≥7% 口径），
回答哪些子键有真实边际、值得作为排序细化依据。

方法：
  - 逐日重建推荐（get_today_recommendations + ranking._entry_tier/_is_nextday_marked，
    与 today_report/display 同源），仅主力五类；nf∩st 双挂票按类别优先级去重。
  - 「推荐时刻榜排名」由 leaderboard_log(symbol_snapshot) 回放：取推荐时间之前
    最近一次快照中该票的排名，无则回退当日最早出现该票的快照。快照只存近几日，
    rank 相关表自动缩窗并标注；rank=None 同时含「快照未覆盖日」与「榜外」，
    读表时注意窗口标注。
  - 回调型核心/创新高核心复用 core_stock_symbols（as_of 重建主题），回撤用
    daily_kline 目标日之前 bar 重算（防未来数据泄漏）；缺 K 线 fail-closed 单列。
  - 表现 = 落库 next_day_pct；统计复用 prevday_perf._stats（主决策口径）。
  - 默认按 symbol 去重（同口径 2026-08-27 排序序列改版测量）；--no-dedup 全量对照。

判读标准：单桶 n<60 视为噪声只做参考；子键要成为排序细化规则，
需边际方向稳定且跨窗口复现。定位：离线测量工具，不调参不落库不进扫描路径。
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, cast

cast(Any, sys.stdout).reconfigure(encoding="utf-8")  # Windows 中文输出

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 项目根（脚本目录运行）

from prevday_perf import DIMS_COMPLETE_SINCE, _next_day_map, _stats  # noqa: E402
from scanner.config import CORE_PULLBACK_MAX, CORE_PULLBACK_MIN, DB_PATH  # noqa: E402
from scanner.core_themes import core_stock_symbols  # noqa: E402
from scanner.data_health import check_kline_health, health_banner  # noqa: E402
from scanner.database import get_fund_flow_pct_map, get_today_recommendations  # noqa: E402
from scanner.db.queries import get_cached_klines  # noqa: E402
from scanner.ranking import _entry_tier, _is_nextday_marked, build_accum_map  # noqa: E402
from scanner.utils import to_float  # noqa: E402

MAIN_CATS = ("rebound", "momentum", "new_face", "known_new_face", "short_term")
# 综合排序类别优先级（config.CAT_DISPLAY_PRIORITY 同序，双挂票去重取高优）
CAT_PRIO = ("rebound", "known_new_face", "momentum", "new_face", "short_term")


def _rank_scans(conn: sqlite3.Connection, dt: str) -> list[tuple[str, dict[str, int]]]:
    """当日 biaosheng 快照序列 [(time, {symbol: rank})]，按时间升序。"""
    try:
        rows = conn.execute(
            "SELECT time, symbol_snapshot FROM leaderboard_log WHERE source='biaosheng' AND date=? ORDER BY time",
            (dt,),
        ).fetchall()
    except Exception:
        return []
    out = []
    for t, snap_raw in rows:
        try:
            snap = json.loads(snap_raw)
        except Exception:  # noqa: S110, S112 - 离线分析脚本：单行数据缺失跳过即可，不落库不调参
            continue
        rk: dict[str, int] = {}
        for item in snap:
            sym = item.get("symbol")
            r = item.get("rank")
            if sym and isinstance(r, int) and r > 0:
                rk[sym] = r
        if rk:
            out.append((t, rk))
    return out


def _rec_rank(scans: list[tuple[str, dict[str, int]]], sym: str, rec_time: str) -> int | None:
    """推荐时刻榜排名：最后一个 time ≤ rec_time 的快照排名；否则当日最早出现。"""
    best: int | None = None
    earliest: int | None = None
    for t, rk in scans:
        r = rk.get(sym)
        if r is None:
            continue
        if earliest is None:
            earliest = r
        if rec_time and t <= rec_time:
            best = r
    return best if best is not None else earliest


def _core_pullback_state(conn: sqlite3.Connection, dt: str, syms: list[str]) -> dict[str, str]:
    """核心股 T-1 距20日高点回撤分型 {sym: cb|shallow|no_kline|non_core}。

    与 display._cb_core_pullback_ok 同窗口 [CORE_PULLBACK_MIN, CORE_PULLBACK_MAX]，
    但用 daily_kline 目标日之前的 bar 重算（缓存含未来 bar，必须裁剪防泄漏）。
    """
    cores = core_stock_symbols(conn, today=dt)
    out: dict[str, str] = {}
    non_core_syms = [s for s in syms if s not in cores]
    if non_core_syms:
        for s in non_core_syms:
            out[s] = "non_core"
    if not cores:
        return out
    kmap = get_cached_klines(conn, list(cores))
    for sym in cores & set(syms):
        bars = [b for b in (kmap.get(sym) or []) if b.get("date") and b["date"] < dt]
        t1_close = to_float(bars[-1].get("close")) if bars else None
        highs = [to_float(b.get("high")) for b in bars[-20:]]
        highs = [h for h in highs if h is not None]
        h20 = max(highs) if highs else None
        if len(bars) < 20 or not t1_close or not h20 or h20 <= 0:
            out[sym] = "no_kline"
            continue
        pb = t1_close / h20 - 1.0
        out[sym] = "cb" if (CORE_PULLBACK_MIN <= pb <= CORE_PULLBACK_MAX) else "shallow"
    for sym in cores - set(syms):
        out.pop(sym, None)
    return out


def collect(conn: sqlite3.Connection, dates: list[str]) -> tuple[list[dict[str, Any]], int, list[str]]:
    """→ 行列表 + 有效交易日数 + 有快照覆盖的日期列表。"""
    rows: list[dict[str, Any]] = []
    n_days = 0
    lb_dates: list[str] = []
    for dt in dates:
        recs = cast("list[dict]", get_today_recommendations(conn, as_of=dt))
        if not recs:
            continue
        nd_map = _next_day_map(conn, dt)
        if not nd_map:
            continue
        main = [e for e in recs if e["category"] in MAIN_CATS]
        if not main:
            continue
        n_days += 1
        accum_map = build_accum_map(conn, recs)
        scans = _rank_scans(conn, dt)
        if scans:
            lb_dates.append(dt)
        flow_map = get_fund_flow_pct_map(conn, sorted({e["symbol"] for e in main}), as_of=dt)
        marked_map = {e["symbol"]: _is_nextday_marked(e, conn, accum_map=accum_map) for e in main}
        # nf∩st 双挂：同类多行取分数最高一行的载体，符号级再按类别优先级去重在 render 前
        for e in main:
            p = nd_map.get(e["symbol"])
            if p is None:
                continue
            d = _flow_entry(e)
            flow = d if d is not None else to_float(flow_map.get(e["symbol"]), default=None)
            rows.append(
                {
                    "date": dt,
                    "sym": e["symbol"],
                    "name": e["name"],
                    "cat": e["category"],
                    "pct": p,
                    "rec_pct": to_float(e.get("percent"), default=0.0) or 0.0,
                    "accum": accum_map.get(e["symbol"]),
                    "flow": flow,
                    "marked": bool(marked_map[e["symbol"]]),
                    "tier": _entry_tier(e, conn, accum_map=accum_map, marked=marked_map[e["symbol"]]),
                    "score": to_float(e.get("score"), default=0.0),
                    "rank": _rec_rank(scans, e["symbol"], e.get("time") or ""),
                }
            )
    return rows, n_days, lb_dates


def _flow_entry(entry: dict[str, Any]) -> float | None:
    """主力净占比：score_breakdown（查询层已转 dict）取 fund_flow_main_pct。"""
    bd = entry.get("score_breakdown")
    if not isinstance(bd, dict):
        return None
    return to_float(bd.get("fund_flow_main_pct"), default=None)


def dedup(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """窗口内 symbol 首现去重（双挂同日同行冲突按类别优先级+分数取优）。"""
    best: dict[str, tuple[int, float, dict[str, Any]]] = {}
    order: list[str] = []
    for r in rows:
        key = r["sym"]
        prio = CAT_PRIO.index(r["cat"]) if r["cat"] in CAT_PRIO else len(CAT_PRIO)
        cand = (prio, -(r["score"] or 0.0), r)
        if key not in best:
            best[key] = cand
            order.append(key)
        elif cand[:2] < best[key][:2]:
            best[key] = cand
    return [best[k][2] for k in order]


# ── 分桶定义 ──────────────────────────────────────────────────────────────
PCT_BANDS = (
    ("≤0%", lambda p: p <= 0),
    ("0~2%", lambda p: 0 < p < 2),
    ("2~4%(死区)", lambda p: 2 <= p < 4),
    ("4~8%(甜蜜带)", lambda p: 4 <= p < 8),
    ("8~10%(陷阱带)", lambda p: 8 <= p < 10),
    (">10%", lambda p: p >= 10),
)
FLOW_BANDS = (
    ("缺失", lambda f: f is None),
    ("≤-5%", lambda f: f is not None and f <= -5),
    ("-5%~0", lambda f: f is not None and -5 < f < 0),
    ("0~+5%", lambda f: f is not None and 0 <= f < 5),
    ("≥+5%", lambda f: f is not None and f >= 5),
)
RANK_BANDS = (
    ("1~3", lambda r: r <= 3),
    ("4~10", lambda r: 4 <= r <= 10),
    ("11~15", lambda r: 11 <= r <= 15),
    ("16~30", lambda r: 16 <= r <= 30),
    ("31+", lambda r: r > 30),
)


def _fmt(v: float | None, width: int, pct: bool = True) -> str:
    if v is None:
        return "—".rjust(width)
    return (f"{v:+.2f}%" if pct else f"{v:.1f}").rjust(width)


def _line(out: list[str], label: str, items: list[dict[str, Any]]) -> None:
    n, avg, hit, win, med = _stats([r["pct"] for r in items])
    flag = "" if n == 0 else (" ⚠小样本" if n < 20 else ("" if n >= 60 else " △样本<60"))
    if n == 0:
        out.append(f"  {label:<24}{0:>5} {'—':>9} {'—':>9}")
        return
    out.append(f"  {label:<24}{n:>5}{_fmt(avg, 9)}{_fmt(hit, 9, pct=False)}{flag}")


def _table(out: list[str], title: str, groups: list[tuple[str, list[dict[str, Any]]]]) -> None:
    out.append(f"\n{title}")
    out.append(f"  {'分组':<24}{'n':>5}{'均次日':>9}{'hit≥7%':>9}")
    out.append("  " + "-" * 52)
    for label, items in groups:
        _line(out, label, items)


def _sections(out: list[str], base_rows: list[dict[str, Any]], lb_dates: list[str], tag: str) -> None:
    """一组核心分桶表（基线/排名带/交叉/涨幅带/资金流/核心复核/类别），在给定窗口上跑。"""
    lb_set = set(lb_dates)
    lb_sub = [r for r in base_rows if r["date"] in lb_set]

    # 1、现行主键分层基线
    _table(
        out,
        f"[{tag}] 现行主键基线",
        [
            ("全样本", base_rows),
            ("🎯档0(marked)", [r for r in base_rows if r["tier"] == 0]),
            ("档1(rebound)", [r for r in base_rows if r["tier"] == 1]),
            ("档2(普通)", [r for r in base_rows if r["tier"] == 2]),
            ("档3(警示劣后)", [r for r in base_rows if r["tier"] == 3]),
        ],
    )

    # 2、主键①可细化点：有榜排名分带 × 无榜（仅快照日）
    def _has_rank(r):
        return isinstance(r["rank"], int) and r["rank"] > 0

    ranked = [r for r in lb_sub if _has_rank(r)]
    no_rank = [r for r in lb_sub if not _has_rank(r)]
    rb_groups: list[tuple[str, list[dict[str, Any]]]] = [("无榜排名/未覆盖", no_rank)]
    for label, fn in RANK_BANDS:
        rb_groups.append((f"榜排名 {label}", [r for r in ranked if fn(r["rank"])]))
    n_lb_days = len(lb_set & {r["date"] for r in base_rows})
    _table(out, f"[{tag}] 主键①「有榜排名」分带（仅 {n_lb_days} 个快照日）", rb_groups)

    # 3、档位内 × 有无榜排名交叉（验证两键谁解释力大）
    cross_groups: list[tuple[str, list[dict[str, Any]]]] = []
    for tier in (0, 1, 2, 3):
        sub = [r for r in lb_sub if r["tier"] == tier]
        if not sub:
            continue
        cross_groups.append((f"档{tier}·有榜排名", [r for r in sub if _has_rank(r)]))
        cross_groups.append((f"档{tier}·无榜", [r for r in sub if not _has_rank(r)]))
    _table(out, f"[{tag}] 主键交叉：档位 × 有无榜排名", cross_groups)

    # 4、涨幅带（裸涨幅升序 vs 分带替代）
    pct_groups: list[tuple[str, list[dict[str, Any]]]] = [
        (label, [r for r in base_rows if fn(r["rec_pct"])]) for label, fn in PCT_BANDS
    ]
    trap_non_st = [
        r for r in base_rows if 8 <= r["rec_pct"] < 10 and r["cat"] in ("momentum", "new_face", "known_new_face")
    ]
    pct_groups.append(("8~10%陷阱·MOM/NF", trap_non_st))
    pct_groups.append(
        ("8~10%陷阱·ST豁免", [r for r in base_rows if 8 <= r["rec_pct"] < 10 and r["cat"] == "short_term"])
    )
    _table(out, f"[{tag}] 主键⑥「涨幅升序」分带替代（推荐时刻涨幅）", pct_groups)

    # 5、资金流分带（候选新键，当前不入序列）
    flow_groups: list[tuple[str, list[dict[str, Any]]]] = [
        (label, [r for r in base_rows if fn(r["flow"])]) for label, fn in FLOW_BANDS
    ]
    _table(out, f"[{tag}] 候选新键：主力净占比分带", flow_groups)

    # 6、核心股回调型 / 创新高 / 缺K线（状态按 (date,symbol) 键防跨日覆盖）
    cc_state: dict[tuple[str, str], str] = {}
    by_date_syms: dict[str, list[str]] = {}
    for r in base_rows:
        by_date_syms.setdefault(r["date"], []).append(r["sym"])
    for dt, syms in by_date_syms.items():
        cc_state.update({(dt, s): v for s, v in _core_pullback_state(conn0, dt, syms).items()})
    core_groups: list[tuple[str, list[dict[str, Any]]]] = [
        ("回调型核心(低吸窗口)", [r for r in base_rows if cc_state.get((r["date"], r["sym"])) == "cb"]),
        ("创新高/浅回撤核心", [r for r in base_rows if cc_state.get((r["date"], r["sym"])) == "shallow"]),
        ("缺K线(fail-closed)", [r for r in base_rows if cc_state.get((r["date"], r["sym"])) == "no_kline"]),
        ("非核心股", [r for r in base_rows if cc_state.get((r["date"], r["sym"])) == "non_core"]),
    ]
    _table(out, f"[{tag}] 主键③「回调型核心票」复核（扣未来泄漏重算回撤）", core_groups)

    # 7、类别明细（参照组）
    cat_groups: list[tuple[str, list[dict[str, Any]]]] = [
        (c, [r for r in base_rows if r["cat"] == c]) for c in MAIN_CATS
    ]
    _table(out, f"[{tag}] 参照：类别明细", cat_groups)


def render(rows, n_days, lb_dates, use_dedup: bool) -> str:
    recent = [r for r in rows if r["date"] >= DIMS_COMPLETE_SINCE] or rows
    base_recent = dedup(recent) if use_dedup else recent
    base_full = dedup(rows) if use_dedup else rows
    head = (
        f"\n◆ 排序序列细化规则回放（有效交易日 {n_days} 天；去重={'开' if use_dedup else '关'}；"
        f"rank 表限有快照的 {len(lb_dates)} 日）\n"
        "  近端窗口 = 维度完整期起（与全期相同则只出一组表）；n<60 桶视为噪声参考。"
    )
    out = [head]
    _sections(out, base_recent, lb_dates, tag=f"近端 {DIMS_COMPLETE_SINCE} 起")
    if rows != recent:
        out.append("\n" + "=" * 62)
        _sections(out, base_full, lb_dates, tag="全期对照")
    return "\n".join(out)


conn0: sqlite3.Connection = sqlite3.connect(DB_PATH)  # render 内回放用（main 中替换）


def main() -> None:
    global conn0
    parser = argparse.ArgumentParser(description="排序序列细化规则回放")
    parser.add_argument("--days", type=int, default=30, help="最近 N 个交易日（0=全期）")
    parser.add_argument("--no-dedup", action="store_true", help="不去重（全量对照）")
    parser.add_argument("--force", action="store_true", help="跳过数据健康检查")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn0 = conn
    all_dates = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT date FROM recommendations WHERE next_day_pct IS NOT NULL ORDER BY date"
        ).fetchall()
    ]
    dates = all_dates[-args.days :] if args.days and args.days > 0 else all_dates
    if not args.force:
        report = check_kline_health(conn, dates=dates or None)
        banner = health_banner(report)
        if banner:
            print(banner)
        if report.blocked:
            print("  [中止] 数据疑似污染，先跑 python repair_kline.py 修复后重试")
            conn.close()
            return
    rows, n_days, lb_dates = collect(conn, dates)
    print(render(rows, n_days, lb_dates, use_dedup=not args.no_dedup))
    conn.close()


if __name__ == "__main__":
    main()
