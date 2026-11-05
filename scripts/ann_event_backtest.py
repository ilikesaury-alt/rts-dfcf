"""公告事件「可回溯数据源」的历史样本外检验（2026-11-05）。

ruff: noqa: S311  # bootstrap 重采样用 random，非密码学用途


## 为什么需要它

`scripts/shadow_feature_backtest.py`（2026-11-03）已经检验过 6 类公告事件，结论是：
业绩预告 / 重组并购 / 监管风险 三类**方向为负**，但**参与日只有 2~5 天** ⇒ 样本不足，
且 CI 跨 0 的口径与「逐日符号检验」口径结论并不一致（同族的龙虎榜就是
「CI 看着显著 +9.19pp，但逐日正 8/负 9，p=0.815」= 假信号）。

本脚本在同一套口径上做三件事：

1. **换总体**：从 B 段（`offboard_launch_log`）扩到 **v1 主线**（`recommendations`）——
   用户实际看到、实际会买的池子在那里；B 段样本的「被推送」条件是另一套。
2. **换事件类型**：补上 2026-11-05 盘中复核里真实出现、而旧表没有的类型：
   股份质押 / 计提减值 / 终止投资·注销子公司 / 解禁·限售股上市流通 / 对外担保 /
   激励作废 / 高管变动。
3. **补判据**：在「按日配对 bootstrap 95% CI」之外，**加逐日符号检验与 jackknife** ——
   CI 显著但逐日正负对半 ⇒ 判为假信号（这是龙虎榜的先例）。

## 口径

- 样本 = `recommendations` 里 `next_day_pct` 非空的行（v1 主线），按 (symbol, date) 去重取当日最后一轮；
- 命中 = 次日 ≥7%（系统唯一目标函数）；
- 特征 = 信号日**之前**窗口内的公告事件（默认 10 日），**严禁**用信号日之后的信息；
  `--strict` 模式额外把**当日**公告也剔除（当日公告多在收盘后发布 ⇒ 含当日=轻度前视）；
- 配对 = 同一天内「命中该事件的票」命中率 − 「当日全量」命中率，**按日等权**。

## 它明确不做什么

- **不改任何阈值、不写生产表**；采集结果写缓存 JSON（默认 `logs/`，可用 `--cache` 改）；
- **不给结论**：只报 Δ / CI / 逐日符号检验 / jackknife。证据判据写在这里，读者自己下结论：
  「Δ>0 且 CI 不跨 0 **且** 逐日符号检验显著 **且** 有效参与日 ≥ 10」才算可用的筛选条件。

用法：
    python scripts/ann_event_backtest.py                      # v1 主线 + 全事件类型
    python scripts/ann_event_backtest.py --no-fetch           # 只用已有缓存
    python scripts/ann_event_backtest.py --strict             # 额外剔除信号日当日公告
    python scripts/ann_event_backtest.py --population offboard  # 与旧脚本对照
"""

from __future__ import annotations

import argparse
import json
import math
import random  # noqa: S311 - bootstrap 重采样，非密码学用途
import sqlite3
import statistics as st
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date as _date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scanner.ai_digest.em_data import fetch_announcements  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CACHE_DEFAULT = ROOT / "logs" / "ann_event_cache.json"
OLD_CACHE = ROOT / "logs" / "shadow_feature_cache.json"

#: 旧脚本的事件分类（单一事实源，直接复用，不手抄第二份）
from scripts.shadow_feature_backtest import ANN_KEYWORDS  # noqa: E402

#: 2026-11-05 盘中复核新增的事件类型（旧表没有 ⇒ 从未做过样本外检验）
NEW_ANN_KEYWORDS: dict[str, tuple[str, ...]] = {
    "股份质押": ("质押", "解质", "冻结"),
    "计提减值": ("计提资产减值", "减值准备", "商誉减值", "计提减值"),
    "终止与注销": ("终止对外投资", "注销子公司", "终止重组", "终止募集", "终止"),
    "解禁流通": ("解除限售", "上市流通", "限售股份"),
    "对外担保": ("对外担保", "担保额度", "提供担保"),
    "激励作废": ("作废", "回购注销限制性股票", "限制性股票"),
    "高管变动": ("辞职", "聘任", "董事会秘书", "总经理", "独立董事"),
}
ALL_KEYWORDS: dict[str, tuple[str, ...]] = {**ANN_KEYWORDS, **NEW_ANN_KEYWORDS}


def _days_between(a: str, b: str) -> int | None:
    """`a - b` 的天数（YYYY-MM-DD）；解析失败返回 None。"""
    try:
        ya, ma, da = (int(x) for x in a.split("-")[:3])
        yb, mb, db = (int(x) for x in b.split("-")[:3])
        return (_date(ya, ma, da) - _date(yb, mb, db)).days
    except (ValueError, TypeError):
        return None


# ── 样本 ──────────────────────────────────────────────────────────────
def load_v1_samples(conn: sqlite3.Connection, start: str, end: str) -> list[dict]:
    """v1 主线：`(symbol, date)` 去重，取当日**最后一轮**（同 config_push 的口径）。"""
    rows = conn.execute(
        "SELECT symbol, date, next_day_pct, category, percent FROM recommendations "
        "WHERE next_day_pct IS NOT NULL AND date >= ? AND date <= ? ORDER BY date, id",
        (start, end),
    ).fetchall()
    seen: dict[tuple[str, str], tuple] = {}
    for sym, d, ndp, cat, pct in rows:
        seen[(sym, d)] = (sym, d, ndp, cat, pct)
    return [
        {"symbol": s, "date": d, "ndp": ndp, "category": cat, "percent": pct}
        for (s, d), (_s, _d, ndp, cat, pct) in seen.items()
    ]


def load_offboard_samples(conn: sqlite3.Connection, start: str, end: str) -> list[dict]:
    rows = conn.execute(
        "SELECT symbol, date, next_day_pct, tier FROM offboard_launch_log "
        "WHERE next_day_pct IS NOT NULL AND date >= ? AND date <= ? ORDER BY date",
        (start, end),
    ).fetchall()
    return [{"symbol": s, "date": d, "ndp": ndp, "category": tier, "percent": None} for s, d, ndp, tier in rows]


# ── 采集 ──────────────────────────────────────────────────────────────
def collect(symbols: list[str], workers: int, cache: dict) -> dict:
    """采集公告（每票 50 条）。已有缓存（含旧脚本的 shadow_feature_cache）直接复用。"""
    todo = [s for s in symbols if s not in cache]
    print(f"  需采集 {len(todo)} 只（已缓存 {len(symbols) - len(todo)}）", flush=True)
    if not todo:
        return cache
    done = 0
    t0 = time.time()

    def one(sym: str) -> tuple[str, list[dict]]:
        return sym, fetch_announcements(sym, 50)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(one, s): s for s in todo}
        for fut in as_completed(futures):
            sym = futures[fut]
            try:
                k, v = fut.result()
            except Exception as e:  # noqa: BLE001 - 采集脚本：单票失败不该中断整批
                print(f"    [!] {sym} 采集异常 {type(e).__name__}", flush=True)
                continue
            cache[k] = {"ann": v}
            done += 1
            if done % 25 == 0:
                el = time.time() - t0
                print(
                    f"    {done}/{len(todo)}  用时 {el:.0f}s  预计剩余 {el / done * (len(todo) - done):.0f}s",
                    flush=True,
                )
    return cache


def load_caches(paths: list[Path]) -> dict:
    cache: dict = {}
    for p in paths:
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        for sym, v in data.items():
            if isinstance(v, dict) and v.get("ann") is not None:
                cache.setdefault(sym, {"ann": v.get("ann") or []})
            elif isinstance(v, list):
                cache.setdefault(sym, {"ann": v})
    return cache


# ── 特征 ──────────────────────────────────────────────────────────────
def build_event_flags(sample: dict, ann: list[dict], *, window: int, strict: bool) -> dict[str, int]:
    """信号日**当时可知**的公告事件标记。`strict` 额外剔除信号日当天的公告。"""
    d = sample["date"]
    flags: dict[str, int] = {}
    count = 0
    for a in ann or []:
        ad = str(a.get("date") or "")
        delta = _days_between(d, ad)
        if delta is None or delta < 0 or delta > window:
            continue
        if strict and delta == 0:
            continue  # 当日公告多在收盘后发布 ⇒ 含当日 = 轻度前视
        count += 1
        title = str(a.get("title") or "")
        for kind, kws in ALL_KEYWORDS.items():
            if any(k in title for k in kws):
                flags[kind] = 1
    flags["有公告"] = 1 if count else 0
    flags["公告条数"] = count
    return flags


# ── 统计 ──────────────────────────────────────────────────────────────
def paired_test(rows: list[dict], predicate: Callable[[dict], bool], *, n_boot: int = 20000) -> dict:
    """按日配对 bootstrap + 逐日符号检验 + jackknife。

    Δ = 同一天内「命中条件的票」命中率 − 「当日全量」命中率（按日等权）。
    过滤后不足 2 只的交易日剔除（单只票的日命中率非 0 即 100，噪声压倒信号）。
    """
    by_day: dict[str, list[dict]] = {}
    for r in rows:
        by_day.setdefault(r["date"], []).append(r)

    def hit_rate(rs: list[dict]) -> float:
        return sum(1 for r in rs if r["ndp"] >= 7) / len(rs) * 100

    diffs: list[float] = []
    for rs in by_day.values():
        sub = [r for r in rs if predicate(r)]
        if len(sub) < 2:
            continue
        diffs.append(hit_rate(sub) - hit_rate(rs))
    res: dict = {
        "days": len(diffs),
        "active_days": 0,
        "rows_treated": 0,
        "rows_total": 0,
        "delta": 0.0,
        "lo": 0.0,
        "hi": 0.0,
        "pos": 0,
        "neg": 0,
        "sign_p": None,
        "jack_lo": 0.0,
        "jack_hi": 0.0,
    }
    if not diffs:
        return res
    random.seed(42)
    means = []
    for _ in range(n_boot):
        s = [random.choice(diffs) for _ in diffs]  # noqa: S311
        means.append(st.mean(s))
    means.sort()
    pos = sum(1 for d in diffs if d > 0)
    neg = sum(1 for d in diffs if d < 0)
    n = pos + neg
    p = None
    if n:
        k = min(pos, neg)
        p = min(1.0, 2 * sum(math.comb(n, i) for i in range(k + 1)) / (2**n))
    # jackknife：留一日
    total = sum(diffs)
    jack = [(total - d) / (len(diffs) - 1) for d in diffs] if len(diffs) > 1 else [total]
    res.update(
        {
            "delta": round(st.mean(diffs), 2),
            "lo": round(means[int(0.025 * n_boot)], 2),
            "hi": round(means[int(0.975 * n_boot)], 2),
            "pos": pos,
            "neg": neg,
            "sign_p": None if p is None else round(p, 4),
            "jack_lo": round(min(jack), 2),
            "jack_hi": round(max(jack), 2),
        }
    )
    return res


def split_half(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """按交易日切成前一半 / 后一半（H1/H2 分半稳健性检验）。

    为什么要分半：多重比较下「CI 显著」可能是偶然；**两半同向**才是真信号。
    """
    days = sorted({r["date"] for r in rows})
    mid = len(days) // 2
    h1, h2 = set(days[:mid]), set(days[mid:])
    return [r for r in rows if r["date"] in h1], [r for r in rows if r["date"] in h2]


def describe(rows: list[dict], predicate: Callable[[dict], bool]) -> dict:
    """命中率/均值/参与日的原始描述（不做配对）。"""
    treated = [r for r in rows if predicate(r)]
    days = {r["date"] for r in treated}
    ctrl = [r for r in rows if not predicate(r)]

    def hr(rs: list[dict]) -> float | None:
        return round(sum(1 for r in rs if r["ndp"] >= 7) / len(rs) * 100, 2) if rs else None

    def avg(rs: list[dict]) -> float | None:
        return round(st.mean([r["ndp"] for r in rs]), 2) if rs else None

    return {
        "n_treated": len(treated),
        "n_control": len(ctrl),
        "days_with_event": len(days),
        "hit_treated": hr(treated),
        "hit_control": hr(ctrl),
        "avg_treated": avg(treated),
        "avg_control": avg(ctrl),
        "max_treated": round(max((r["ndp"] for r in treated), default=0), 2),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="公告事件的历史样本外检验（只读，不改阈值）")
    ap.add_argument("--start", default="2026-09-18", help="样本起始日")
    ap.add_argument("--end", default="", help="样本结束日（默认 = 最后一个有标签的交易日）")
    ap.add_argument("--population", choices=("v1", "offboard"), default="v1", help="样本总体")
    ap.add_argument("--window", type=int, default=10, help="公告回看窗口（自然日）")
    ap.add_argument("--strict", action="store_true", help="额外剔除信号日当天的公告（无前视口径）")
    ap.add_argument("--workers", type=int, default=4, help="采集并发（东财有限频）")
    ap.add_argument("--cache", default=str(CACHE_DEFAULT), help="采集缓存路径")
    ap.add_argument("--no-fetch", action="store_true", help="不联网，只用缓存")
    ap.add_argument("--split", action="store_true", help="追加 H1/H2 分半稳健性检验")
    ap.add_argument("--apply-today", default="", help="把命中的事件套到该日的候选上（如 2026-11-05）")
    ap.add_argument("--out", default="", help="报告落盘路径（JSON）")
    args = ap.parse_args(argv)

    if sys.platform == "win32":
        rec = getattr(sys.stdout, "reconfigure", None)
        if callable(rec):
            rec(encoding="utf-8")

    import scanner.config as cfg

    conn = sqlite3.connect(str(cfg.resolve_db_path()))
    if not args.end:
        args.end = conn.execute("SELECT MAX(date) FROM recommendations WHERE next_day_pct IS NOT NULL").fetchone()[0]
    loader = load_v1_samples if args.population == "v1" else load_offboard_samples
    samples = loader(conn, args.start, args.end)
    conn.close()

    symbols = sorted({s["symbol"] for s in samples})
    days = sorted({s["date"] for s in samples})
    print(
        f"总体={args.population}  样本 {len(samples)} 行 / {len(symbols)} 只票 / {len(days)} 个交易日"
        f"（{args.start}..{args.end}）  窗口={args.window}日  strict={args.strict}"
    )

    cache = load_caches([Path(args.cache), OLD_CACHE])
    if not args.no_fetch:
        cache = collect(symbols, args.workers, cache)
        cp = Path(args.cache)
        cp.parent.mkdir(parents=True, exist_ok=True)
        cp.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        print(f"  缓存已写 {cp}（{len(cache)} 只）")

    rows = []
    for s in samples:
        data = cache.get(s["symbol"])
        if not data:
            continue
        r = dict(s)
        r.update(build_event_flags(s, data.get("ann") or [], window=args.window, strict=args.strict))
        rows.append(r)
    covered = len({r["symbol"] for r in rows})
    print(f"可检验样本 {len(rows)} 行（公告覆盖 {covered}/{len(symbols)} 只票）")
    if not rows:
        return 1
    base = sum(1 for r in rows if r["ndp"] >= 7) / len(rows) * 100
    print(f"基线命中率 {base:.2f}%（n={len(rows)}）\n")

    kinds = ["有公告", *ALL_KEYWORDS]
    results = {}
    print("=== 事件类型检验（按日配对 bootstrap + 逐日符号检验 + jackknife）===")
    print(
        f"  {'事件':<12} {'有效日':>5} {'参与日':>5} {'n处理':>6} {'命中%':>7} {'对照%':>7} "
        f"{'Δ(pp)':>7} {'CI95':>16} {'逐日正/负':>9} {'符号p':>7} {'jack':>14}"
    )
    for kind in kinds:
        if kind == "公告条数":
            continue
        pred = (lambda k: lambda r: r.get(k, 0) == 1)(kind)
        t = paired_test(rows, pred)
        d = describe(rows, pred)
        results[kind] = {"paired": t, "describe": d}
        if t["days"] == 0:
            print(f"  {kind:<12} 有效日 0（每天处理组不足 2 只）")
            continue
        ci = f"[{t['lo']:+.2f},{t['hi']:+.2f}]"
        sp = "—" if t["sign_p"] is None else f"{t['sign_p']:.3f}"
        jk = f"[{t['jack_lo']:+.2f},{t['jack_hi']:+.2f}]"
        print(
            f"  {kind:<12} {t['days']:>5} {d['days_with_event']:>5} {d['n_treated']:>6} "
            f"{str(d['hit_treated']):>7} {str(d['hit_control']):>7} {t['delta']:>+7.2f} {ci:>16} "
            f"{f'{t["pos"]}/{t["neg"]}':>9} {sp:>7} {jk:>14}"
        )

    print("\n=== 判据（写在脚本里，读者自己下结论）===")
    print("  可用作筛选条件需同时满足：Δ>0 · CI 不跨 0 · 逐日符号检验 p<0.05 · 有效参与日 ≥ 10")
    print("  ⚠ CI 显著但逐日正负对半 ⇒ 判为假信号（龙虎榜先例：CI +9.19pp 但逐日正8/负9，p=0.815）")

    if args.split:
        h1, h2 = split_half(rows)
        print(
            f"\n=== H1/H2 分半稳健性（H1={len({r['date'] for r in h1})} 日 / H2={len({r['date'] for r in h2})} 日）==="
        )
        for kind in kinds:
            if kind == "公告条数":
                continue
            pred = (lambda k: lambda r: r.get(k, 0) == 1)(kind)
            t1, t2 = paired_test(h1, pred), paired_test(h2, pred)
            if t1["days"] == 0 and t2["days"] == 0:
                continue
            print(
                f"  {kind:<12} H1 Δ={t1['delta']:+6.2f} ({t1['days']}日)  H2 Δ={t2['delta']:+6.2f} ({t2['days']}日)"
                f"  {'方向一致' if t1['delta'] * t2['delta'] > 0 else '⚠方向不一致'}"
            )

    if args.apply_today:
        conn2 = sqlite3.connect(str(cfg.resolve_db_path()))
        today_syms = [
            r[0]
            for r in conn2.execute(
                "SELECT DISTINCT symbol FROM recommendations WHERE date = ? ORDER BY symbol",
                (args.apply_today,),
            )
        ]
        conn2.close()
        print(f"\n=== {args.apply_today} 候选命中情况（窗口 = 信号日之前 10 日，含当日）===")
        for sym in today_syms:
            ann = (cache.get(sym) or {}).get("ann") or []
            flags = build_event_flags({"date": args.apply_today}, ann, window=args.window, strict=False)
            hit = [k for k in kinds if k != "公告条数" and flags.get(k) == 1]
            if hit:
                print(f"  {sym}  命中：{'、'.join(hit)}")

    if args.out:
        Path(args.out).write_text(
            json.dumps(
                {
                    "population": args.population,
                    "start": args.start,
                    "end": args.end,
                    "window": args.window,
                    "strict": args.strict,
                    "samples": len(rows),
                    "baseline_hit_pct": round(base, 2),
                    "results": results,
                },
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
        print(f"\n[报告已存 {args.out}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
