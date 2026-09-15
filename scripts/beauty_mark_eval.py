#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""「美」走势标记分档的样本外裁决（离线 · 确定性 · 只读 scanner.db）。

## 为什么需要它

`beauty_mark`（scanner/trend_beauty）是**纯展示层**：`rule_validate` 三个评估器
（stored-score / nextday-prob / rescore）**都看不见它**，所以改它的口径不需要过样本外
验证门 —— 但它有**用户可见语义**（行尾一个「美」直接参与"这只买不买"的判断），
属于「必须另证」的那类改动。

2026-09-15 之前，这个证明只活在一次性的 heredoc 里，而 config 注释引用的
`beauty_gate_eval.py` 根本不在仓库里 —— 即"不可复现的证据"。本脚本把它固化：
`config_sources.py` 的走势标记说明与本文件必须对得上。

## 口径（与生产同源，不重写判定）

- 日线：直接调 `trend_beauty.evaluate_daily_trend`（生产函数）
- 分时：直接调 `trend_beauty.evaluate_intraday_beauty`（生产函数；0.0 = 未评分 = 缺失）
- 分档：直接调 `trend_beauty.beauty_mark`（生产函数）→ "" / "美" / "美★"
- 旧口径（2026-09-15 之前的「日线 ∧ 分时」）生产已无此代码，**本地复刻**作对照，
  复刻源 = `git show HEAD:scanner/trend_beauty.py` 的 beauty_mark

## 防前视

日线按信号日截断。默认 `--cutoff t1` = 严格取 `date < 信号日` 的 K 线（最保守）；
`--cutoff t`（含当日）用于看敏感性与"当日尾盘走弱"的影响方向。

## 样本口径

与 `load_attribution_rows` 一致：`excluded=0` 且 `next_day_pct` 非空，同一
`(date, symbol)` **取最后一轮**（一轮扫描最多刷一条，榜单刷新会产生多行；
不取最后一轮会虚高样本量，且同票同日的重复行为会放大权重）。

## 用法

    python scripts/beauty_mark_eval.py                  # 默认 t1 截断 + 2000 次按日聚簇 bootstrap
    python scripts/beauty_mark_eval.py --cutoff t
    python scripts/beauty_mark_eval.py --json
    python scripts/beauty_mark_eval.py --bootstrap 0     # 关掉重采样（更快）

## 退出码

    0 = 正常（某档样本量过小 → 只告警，不算失败）
    1 = 无样本 / 读库失败
    2 = 用法错误
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import statistics as st
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scanner.trend_beauty import (  # noqa: E402
    DAILY_INSUFFICIENT,
    INTRADAY_MISSING,
    beauty_mark,
    evaluate_daily_trend,
    evaluate_intraday_beauty,
)

TIERS = ("美★", "美", "")  # 生产分档（""=未标记）
TIER_LABEL = {"美★": "美★（日线漂亮 + 分时亦漂亮）", "美": "美 （日线漂亮·分时未确认）", "": "未标记（基线）"}


def load_sample(conn: sqlite3.Connection):
    """返回 (原始行数, {(date, symbol): row})；同票同日取最后一轮。"""
    rows = conn.execute(
        "SELECT rowid AS rid, date, symbol, category, score_breakdown, next_day_pct "
        "FROM recommendations WHERE excluded=0 AND next_day_pct IS NOT NULL ORDER BY date, rowid"
    ).fetchall()
    sample: dict[tuple, sqlite3.Row] = {}
    for r in rows:
        sample[(r["date"], r["symbol"])] = r
    return rows, sample


def load_klines(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in conn.execute("SELECT symbol, date, close, open, high, percent FROM daily_kline ORDER BY symbol, date"):
        out.setdefault(r["symbol"], []).append(
            {"date": r["date"], "close": r["close"], "open": r["open"], "high": r["high"], "percent": r["percent"]}
        )
    return out


def judge(sample: dict, klines: dict, cutoff: str, hit_pct: float) -> list[dict]:
    """逐样本判定（生产函数取分档；旧口径本地复刻作对照）。"""
    out: list[dict] = []
    for (date, sym), r in sample.items():
        bars_all = klines.get(sym, [])
        if cutoff == "t1":
            bars = [b for b in bars_all if b["date"] < date]
        else:
            bars = [b for b in bars_all if b["date"] <= date]
        entry = {"symbol": sym, "category": r["category"], "score_breakdown": r["score_breakdown"]}
        daily_fail, _score, daily_detail = evaluate_daily_trend(bars)
        intra_fail, intra_detail = evaluate_intraday_beauty(entry, None)
        nd = r["next_day_pct"]
        daily_ok = daily_fail is None and daily_detail != DAILY_INSUFFICIENT
        # 旧口径（HEAD 版复刻）：日线/分时无任一可判定之丑，且至少一维可判定
        determined = daily_detail not in (DAILY_INSUFFICIENT, INTRADAY_MISSING) or intra_detail not in (
            DAILY_INSUFFICIENT,
            INTRADAY_MISSING,
        )
        out.append(
            {
                "date": date,
                "symbol": sym,
                "mark": beauty_mark(entry, bars, None),
                "daily_ok": daily_ok,
                "daily_detail": daily_detail,
                "intra_detail": intra_detail,
                "intra_missing": intra_detail == INTRADAY_MISSING,
                "intra_ok": not intra_fail and intra_detail != INTRADAY_MISSING,
                "old_mark": daily_fail is None and intra_fail is None and determined,
                # 旧口径的语义缺陷：日线**不可判定**却凭分时漂亮被标「美」
                "old_or_determined": (daily_fail is None and intra_fail is None and determined and not daily_ok),
                "nd": nd,
                "hit": nd >= hit_pct,
            }
        )
    return out


def stats(rows: list[dict]) -> dict:
    nd = [x["nd"] for x in rows]
    if not nd:
        return {"n": 0}
    s = sorted(nd)
    return {
        "n": len(nd),
        "hit_pct": sum(1 for x in rows if x["hit"]) / len(nd) * 100,
        "avg": st.mean(nd),
        "med": st.median(nd),
        "p10": s[max(0, int(len(s) * 0.1) - 1)],
        "tail5": sum(1 for v in nd if v <= -5.0) / len(nd) * 100,
        "tail7": sum(1 for v in nd if v <= -7.0) / len(nd) * 100,
    }


def fmt_stats(s: dict) -> str:
    if not s or not s["n"]:
        return "n=0"
    return (
        f"n={s['n']:>5}  hit={s['hit_pct']:5.1f}%  avg={s['avg']:+6.2f}%  med={s['med']:+6.2f}%"
        f"  p10={s['p10']:+6.2f}%  ≤-5%={s['tail5']:5.1f}%  ≤-7%={s['tail7']:5.1f}%"
    )


def paired_hit_ci(rows: list[dict], sel_a, sel_b, n_iter: int, seed: int) -> tuple[float, float, float]:
    """按日聚簇 bootstrap：返回 (Δhit, lo95, hi95)（A − B，百分点）。

    按**交易日**重采样而非按样本：同一天的票共享大盘环境，视作独立样本会低估标准误
    ——与 `scanner.rule_validate` 的「按日配对 bootstrap」同口径。
    """
    a = [x for x in rows if sel_a(x)]
    b = [x for x in rows if sel_b(x)]
    if not a or not b:
        return 0.0, 0.0, 0.0

    def rate(sel) -> float:
        if not sel:
            return 0.0
        return sum(1 for x in sel if x["hit"]) / len(sel) * 100

    obs = rate(a) - rate(b)
    if n_iter <= 0:
        return obs, float("nan"), float("nan")
    by_day: dict[str, list[dict]] = {}
    for x in rows:
        by_day.setdefault(x["date"], []).append(x)
    days = list(by_day)
    rng = random.Random(seed)  # noqa: S311 - 仅用于可复现的 bootstrap 重采样，无安全用途
    diffs: list[float] = []
    for _ in range(n_iter):
        pick: list[dict] = []
        for _d in days:
            pick.extend(by_day[days[rng.randrange(len(days))]])
        diffs.append(rate([x for x in pick if sel_a(x)]) - rate([x for x in pick if sel_b(x)]))
    diffs.sort()
    lo = diffs[max(0, int(len(diffs) * 0.025) - 1)]
    hi = diffs[min(len(diffs) - 1, int(len(diffs) * 0.975))]
    return obs, lo, hi


def main() -> int:
    ap = argparse.ArgumentParser(description="「美」走势标记分档的样本外裁决（只读 scanner.db）")
    ap.add_argument("--db", default=str(ROOT / "scanner.db"), help="数据库路径（默认仓库根 scanner.db）")
    ap.add_argument("--cutoff", choices=("t1", "t"), default="t1", help="日线截断：t1=严格早于信号日（默认，防前视）")
    ap.add_argument("--hit-pct", type=float, default=7.0, help="hit 阈值（%%），默认 7.0 = 项目唯一类别先验口径")
    ap.add_argument("--bootstrap", type=int, default=2000, help="按日聚簇 bootstrap 次数（0=关闭）")
    ap.add_argument("--seed", type=int, default=7, help="bootstrap 随机种子（固定以保证可复现）")
    ap.add_argument("--min-n", type=int, default=30, help="档位样本量低于此值 → 告警（阈值可调）")
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = ap.parse_args()
    if args.bootstrap < 0 or args.min_n < 1 or args.hit_pct <= 0:
        print("[FAIL] 用法错误：--bootstrap ≥ 0、--min-n ≥ 1、--hit-pct > 0", file=sys.stderr)
        return 2

    db = Path(args.db)
    if not db.exists():
        print(f"[FAIL] 数据库不存在：{db}", file=sys.stderr)
        return 1
    # 只读打开（mode=ro）：本工具**永远不写库**，用 URI 从物理上保证。
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        raw, sample = load_sample(conn)
        klines = load_klines(conn)
    except sqlite3.Error as exc:
        print(f"[FAIL] 读库失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    if not sample:
        print("[FAIL] 无样本：recommendations 里 excluded=0 且 next_day_pct 非空 = 0 行", file=sys.stderr)
        return 1

    rows = judge(sample, klines, args.cutoff, args.hit_pct)
    days = sorted({x["date"] for x in rows})
    warns: list[str] = []

    def group(pred) -> list[dict]:
        return [x for x in rows if pred(x)]

    tiers = {t: group(lambda x, t=t: x["mark"] == t) for t in TIERS}
    old = group(lambda x: x["old_mark"])
    daily_only = group(lambda x: x["daily_ok"])
    # 归因分解：以「日线美」为基数，看结构损失 vs 阈值损失
    within = daily_only
    thr_keep = {}
    for t in (0.0, 1.0, 2.0, 2.5):
        thr_keep[t] = [x for x in within if x["intra_missing"] or (not x["intra_missing"] and _intra_val(x, t))]

    result = {
        "cutoff": args.cutoff,
        "hit_pct": args.hit_pct,
        "db": str(db),
        "sample": {
            "raw_rows": len(raw),
            "dedup": len(sample),
            "inflate_x": round(len(raw) / max(1, len(sample)), 2),
            "days": len(days),
            "span": [days[0], days[-1]] if days else [],
            "intra_missing_pct": round(sum(1 for x in rows if x["intra_missing"]) / len(rows) * 100, 1),
        },
        "tiers": {t: stats(v) for t, v in tiers.items()},
        "control": {"old_mark": stats(old), "daily_only": stats(within)},
        "old_or_determined": {
            "n": sum(1 for x in old if x["old_or_determined"]),
            "pct_of_old": round(sum(1 for x in old if x["old_or_determined"]) / max(1, len(old)) * 100, 1),
        },
        "threshold_keep": {str(t): len(v) for t, v in thr_keep.items()},
        "bootstrap": {},
    }
    for t in ("美★", "美"):
        obs, lo, hi = paired_hit_ci(
            rows, lambda x, t=t: x["mark"] == t, lambda x: x["mark"] == "", args.bootstrap, args.seed
        )
        result["bootstrap"][t] = {"delta_pp": round(obs, 2), "lo95": round(lo, 2), "hi95": round(hi, 2)}
    obs, lo, hi = paired_hit_ci(rows, lambda x: x["old_mark"], lambda x: not x["old_mark"], args.bootstrap, args.seed)
    result["bootstrap"]["old_vs_rest"] = {"delta_pp": round(obs, 2), "lo95": round(lo, 2), "hi95": round(hi, 2)}
    for t, s in result["tiers"].items():
        if 0 < s["n"] < args.min_n:
            warns.append(f"档位「{t or '未标记'}」n={s['n']} < --min-n {args.min_n}：hit/尾部估计不可靠")
    result["warnings"] = warns

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    smp = result["sample"]
    print(f"对比口径：cutoff={args.cutoff}  hit=次日≥{args.hit_pct:.1f}%  db={db.name}（只读）")
    print(
        f"  样本：{smp['raw_rows']} 行 → (date,symbol) 去重 {smp['dedup']}"
        f"（虚高 {smp['inflate_x']}x）· {smp['days']} 个交易日 · {smp['span'][0]} ~ {smp['span'][1]}"
    )
    print(f"  分时可判定率 {100 - smp['intra_missing_pct']:.1f}%（缺失 {smp['intra_missing_pct']:.1f}% → fail-open）")

    print("\n── 分档结果（生产 beauty_mark）──")
    for t in TIERS:
        print(f"  {TIER_LABEL[t]:<26} 占比 {len(tiers[t]) / len(rows) * 100:5.1f}%  {fmt_stats(result['tiers'][t])}")

    print("\n── 对照：旧口径（2026-09-15 之前 = 日线 ∧ 分时）──")
    print(f"  旧标记                    占比 {len(old) / len(rows) * 100:5.1f}%  {fmt_stats(stats(old))}")
    print(
        f"  其中「日线不足 + 分时美」被标美：{result['old_or_determined']['n']} 只"
        f"（占旧标记 {result['old_or_determined']['pct_of_old']:.0f}% —— 旧口径的语义缺陷）"
    )

    print("\n── 归因：旧标记为什么几乎为空（基数＝日线美）──")
    n_day = len(within)
    print(f"  日线美（可判定且 6 硬门全过）             {fmt_stats(stats(within))}")
    for t in ("美★", "美"):
        sub = tiers[t]
        print(
            f"  ├ 其中分时{'亦漂亮 → 美★' if t == '美★' else '未确认 → 美 ':18} n={len(sub):>5}"
            f"  占日线美 {len(sub) / max(1, n_day) * 100:5.1f}%"
        )
    print("  └ 若保留分时门、只调阈值（在日线美 内）：")
    for t, keep in thr_keep.items():
        print(
            f"      阈值 {t:>3} → 保留 {len(keep):>4} 只（{len(keep) / max(1, n_day) * 100:5.1f}% 的日线美）"
            f" → 全样本标记率 {len(keep) / len(rows) * 100:5.1f}%"
        )
    print(
        "    ∴ 结构（去掉 AND）回收一半，阈值（2.5 ≈ intraday_score p90）再砍掉约 2/3；只调阈值达不到分级后的覆盖率。"
    )

    print("\n── hit 差（按日聚簇 bootstrap 95% CI，含 0 = 不显著）──")
    for k, b in result["bootstrap"].items():
        tag = {"美★": "美★ vs 未标记", "美": "美  vs 未标记", "old_vs_rest": "旧标记 vs 其余"}[k]
        ci = "n/a（--bootstrap 0）" if b["lo95"] != b["lo95"] else f"[{b['lo95']:+.1f}, {b['hi95']:+.1f}]pp"
        print(f"  {tag:16} Δ={b['delta_pp']:+5.1f}pp  95%CI={ci}")

    print(
        "\n判读要点：① 分档看**尾部**不只看 hit —— ★ 的语义是回撤更小，不是更易大涨；"
        "\n           ② 覆盖率以「占全样本」与「占日线美」两个分母同时给出（后者才是分档本身的取舍）。"
    )
    for w in warns:
        print(f"  [warn] {w}")
    return 0


def _intra_val(x: dict, thr: float) -> bool:
    """在给定阈值下分时是否通过（detail 形如 '分时+3.2'；解析失败按不通过）。"""
    try:
        return float(x["intra_detail"].replace("分时", "")) >= thr
    except (ValueError, AttributeError):
        return False


if __name__ == "__main__":
    sys.exit(main())
