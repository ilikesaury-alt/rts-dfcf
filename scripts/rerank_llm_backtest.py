# ruff: noqa: S311  # bootstrap 重采样用 random，非密码学用途
"""**Phase 2：LLM 排序 vs 确定性排序**的逐日配对比较（2026-11-04）。

回答 Phase 1 没回答的问题：池内排序的**模型这一步**有没有增量？
Phase 1（`scripts/rerank_backtest.py`）只证明了**排序依据**（类别先验 + 系统概率）
有边际，没证明「让模型重排」比「直接按系统概率排」更好。

## 为什么要做（缺口在哪）

`rule_validate` 的三个评估器**看不见 LLM 输出** ⇒ rerank 的产出永远过不了样本外验证门。
本脚本是那条缺口的手工补法：把同一批历史日池子，分别交给
- **确定性基线**：按 `nextday_prob` 系统校准概率降序（= 提示词要求模型照做的第一排序依据）；
- **LLM**：走 `ai_digest` 的同一套素材组装 + 同一套提示词 + 同一个 `parse_ranking` 解析器；
逐日配对比较两者的 top-N 次日 ≥7% 命中率。

## 诚实性声明（先读这一段）

1. **历史池子是重建的，不是当时屏幕上那份**。终端 v1 池选含实时行情覆盖、展示层硬门
   （资金流出剔除 / 不追涨 / 减仓标签），这些依赖当时的进程内状态，**无法回放**。
   本脚本用「当日 recommendations 的 v1 五类行 + 当日资金流 + 当日榜单快照」重建，
   **比线上池略宽**（少了展示层剔除）。⇒ 绝对命中率不可与线上直接比，**只有
   「同一重建池上，确定性 vs LLM」的相对比较是有效的**（两边吃同一份池）。
2. **本脚本只回答「模型有没有增量」**，不回答「rerank 整体有没有用」——后者要看 Phase 1。
3. **要花钱**：每个测试日一次 LLM 调用。`--llm-max-days` 封顶（默认 15 天），
   `--no-fetch` 可用已缓存的结果重算统计（不重复花钱）。
4. **不给建议、不产短名单**（与 `scripts/offboard_nextday.py` 同纪律）。

## 用法

```bash
python scripts/rerank_llm_backtest.py                     # 默认 15 天（会出网）
python scripts/rerank_llm_backtest.py --llm-max-days 30   # 更多天（更贵）
python scripts/rerank_llm_backtest.py --no-fetch          # 用缓存重算（零成本）
python scripts/rerank_llm_backtest.py --json
```

缓存落在 `logs/rerank_llm_cache.json`（gitignored），键 = 日期 + 素材指纹。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random  # noqa: S311 - bootstrap 重采样，非密码学用途
import statistics as st
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scanner.ai_digest.guard import is_rerank_redlined  # noqa: E402
from scanner.ai_digest.llm import AIUpstreamError, chat, magpie_config  # noqa: E402
from scanner.ai_digest.rerank import (  # noqa: E402
    build_evidence_items,
    build_pool_materials,
    build_rerank_messages,
    deterministic_order,
    parse_ranking,
)
from scanner.ai_digest.source import connect_readonly  # noqa: E402
from scanner.config import CATEGORY_HIT_RATE, CATEGORY_HIT_RATE_DEFAULT, resolve_db_path  # noqa: E402
from scanner.models import parse_score_breakdown  # noqa: E402
from scanner.nextday_prob import next_day_hit_probability  # noqa: E402
from scanner.utils import EXTERNAL_FAILURES  # noqa: E402

HIT_THRESHOLD = 7.0
MIN_PAIRED_DAYS = 3
DEFAULT_TRAIN_DAYS = 30
DEFAULT_TEST_DAYS = 10
WF_EMBARGO_DAYS = 2

#: v1 池选五类（与 `scanner.ai_digest.rerank` 同口径：rerank 线上排的就是这个池）。
V1_POOL_CATEGORIES = frozenset({"rebound", "known_new_face", "momentum", "new_face", "short_term"})

CACHE_PATH = Path("logs/rerank_llm_cache.json")

#: 缓存版本：**守卫/解析逻辑变更时必须递增**（否则旧结果被当新结果用）。
#: v2 = 2026-11-04：红线守卫加了「否定从句豁免 / 动作词+风险豁免 / 英文词边界」三处修正。
#: v3 = 2026-11-04：素材补上真实先验/系统概率（重建行加了 prior/p 字段 ⇒ 指纹变）。
#: v4 = 2026-11-04：守卫再加「预测词需指向未来」（「当日已冲高」是描述不是预测）。
CACHE_VERSION = 4

#: 评估所需的最少带标签行数（top-3 至少要比池子小一半，否则等于“全选”）。
MIN_LABELED_ROWS = 5


def _load_env() -> None:
    """`.env`（`RTS_ENV_FILE` 可指路径）—— 与 `scanner.ai_digest.__main__._load_env` 同款。

    ⚠ 不加载就拿不到 `MAGPIE_*`（脚本不会经过 ai_digest 的 CLI 入口）。
    """
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - 环境相关
        return
    env_file = os.environ.get("RTS_ENV_FILE")
    if env_file:
        load_dotenv(env_file)
    else:
        load_dotenv()


def _num(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int | float):
        f = float(v)
        return f if f == f and f not in (float("inf"), float("-inf")) else None
    return None


# ── 历史池子重建 ────────────────────────────────────────────────────────────────


def load_flow_map(conn, date: str) -> dict[str, float]:
    """当日主力净占比（%）：`market_extra_cache` 的 fund_flow 快照。"""
    out: dict[str, float] = {}
    try:
        rows = conn.execute(
            "SELECT symbol, payload_json FROM market_extra_cache WHERE date = ? AND data_type = 'fund_flow'",
            (date,),
        ).fetchall()
    except EXTERNAL_FAILURES:
        return out
    for sym, payload in rows:
        try:
            d = json.loads(payload)
        except (TypeError, ValueError):
            continue
        v = _num((d or {}).get("main_pct"))
        if v is not None:
            out[sym] = v
    return out


def load_board_ranks(conn, date: str) -> dict[str, int]:
    """当日飙升榜排名（取当日最后一榜快照）。"""
    try:
        row = conn.execute(
            "SELECT symbol_snapshot FROM leaderboard_log WHERE source = 'biaosheng' AND date = ? "
            "AND symbol_snapshot IS NOT NULL AND symbol_snapshot != '' ORDER BY time DESC LIMIT 1",
            (date,),
        ).fetchone()
    except EXTERNAL_FAILURES:
        return {}
    if not row:
        return {}
    try:
        items = json.loads(row[0])
    except (TypeError, ValueError):
        return {}
    out: dict[str, int] = {}
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        sym = str(it.get("symbol") or "")
        rk = it.get("rank")
        if sym and isinstance(rk, int) and rk > 0:
            out[sym] = rk
    return out


def load_day_pool(conn, date: str) -> list[dict]:
    """重建某日的 v1 池选行（口径：`recommendations` 当日行 + 当日资金流 + 当日榜单）。

    去重口径与 `scanner.nextday_attribution.load_dedup` 一致（同 date+symbol 取最后一轮）。
    """
    try:
        rows = conn.execute(
            "SELECT symbol, name, category, score, percent, concept, accumulated_pct, score_breakdown, "
            "next_day_pct, time FROM recommendations WHERE date = ? AND COALESCE(excluded, 0) = 0 "
            "ORDER BY symbol, time",
            (date,),
        ).fetchall()
    except EXTERNAL_FAILURES as exc:
        raise RuntimeError(f"读取 {date} 的 recommendations 失败：{exc}") from None
    if not rows:
        return []
    flow_map = load_flow_map(conn, date)
    board = load_board_ranks(conn, date)

    # 同 (date, symbol) 取最后一轮（load_dedup 同款）
    last: dict[str, dict] = {}
    for r in rows:
        symbol, name, category, score, percent, concept, accum, sb_raw, ndp, _t = r
        if category not in V1_POOL_CATEGORIES:
            continue
        entry = {"score_breakdown": parse_score_breakdown(sb_raw)}
        dims = entry["score_breakdown"]
        flow = _num(dims.get("fund_flow_main_pct"))
        if flow is None:
            flow = flow_map.get(symbol)
        p = nextday_prob_for(entry, category, flow)
        last[symbol] = {
            "symbol": symbol,
            "name": name,
            "category": category,
            "score": _num(score) or 0.0,
            "percent": _num(percent),
            "concept": concept or "",
            "accum": _num(accum),
            "flow": flow,
            "rank": board.get(symbol),
            "sector": concept or "",
            "ndp": _num(ndp),
            "system_p": p,
            # 供 `ai_digest.rerank.deterministic_order` 用的字段（该模块的排序键是
            # prior/flow/p）—— 重建行与素材行的字段名对齐。
            "p": p,
            "prior": CATEGORY_HIT_RATE.get(category, CATEGORY_HIT_RATE_DEFAULT),
        }
    return list(last.values())


def nextday_prob_for(entry: dict, category: str, flow: float | None) -> float:
    """`nextday_prob` 系统校准概率（历史行口径：prominence 不回放 → 因子跳过）。"""
    e = dict(entry)
    e["category"] = category
    return next_day_hit_probability(e, prominence=None, flow=flow)


# ── LLM 调用与缓存 ──────────────────────────────────────────────────────────────


def materials_fingerprint(mats) -> str:
    """素材指纹（缓存键）：素材变了就不能复用旧结果。"""
    blob = "\n".join(f"{i.id}|{i.value}" for i in mats.items)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def load_cache() -> dict:
    if not CACHE_PATH.exists():
        return {}
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_cache(cache: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")


def llm_rank_for_day(cfg, mats, *, cache: dict, fp: str, no_fetch: bool) -> dict:
    """某日池子 → 模型排序（走缓存；`no_fetch` 时只用缓存）。"""
    key = f"v{CACHE_VERSION}:{mats.date}:{fp}"
    if key in cache:
        return cache[key]
    if no_fetch:
        return {"error": "no-cache"}
    resp = chat(cfg, build_rerank_messages(mats))
    raw = resp.get("content") or ""
    redlined = is_rerank_redlined(raw)
    text = "" if redlined else raw
    check = parse_ranking(text, mats.symbols)
    out = {
        "ordered": list(check.ordered),
        "missing": list(check.missing),
        "unknown": list(check.unknown),
        "duplicates": list(check.duplicates),
        "typos": list(check.typos),
        "loose": check.loose,
        "redlined": redlined,
        "empty": not raw.strip(),
        # 原文存进缓存（诊断用：被红线拦下时要能看到到底写了什么）
        "text": raw[:4000],
    }
    cache[key] = out
    return out


# ── 统计 ───────────────────────────────────────────────────────────────────────


def topn_hit(rows: list[dict], key: str, n: int) -> float | None:
    usable = [r for r in rows if r.get(key) is not None]
    if len(usable) < n:
        return None
    ordered = sorted(usable, key=lambda r: r[key], reverse=True)[:n]
    return sum(1 for r in ordered if r["ndp"] is not None and r["ndp"] >= HIT_THRESHOLD) / len(ordered) * 100


def order_ic(order: list[str], ndp_by: dict[str, float | None]) -> float | None:
    """一个**排序**与次日收益的 Spearman rank IC。

    为什么必须用它（而不是只看 top-N 命中率）：重建池的命中率基线只有 ~3%
    （实测 15 天里全池总共只有 ~8 只达到 ≥7%）⇒ top-3 口径几乎全是 0，
    统计功效为零。rank IC 用**整个排序**的信息（名次 vs 收益），
    每日都能算出连续值 ⇒ 15~40 天就能配对比较。
    """
    seq = [(i + 1, ndp_by.get(s)) for i, s in enumerate(order) if ndp_by.get(s) is not None]
    if len(seq) < 3:
        return None
    # 名次**越小越好** ⇒ 取负号，使「排在前面」与「次日收益高」正相关
    # （与 Phase 1 的「键值越大越好 ⇒ IC 越大越好」同一符号约定）。
    xs = [-float(p) for p, _v in seq]
    ys = [float(v) for _p, v in seq]
    return _spearman(xs, ys)


def _rank(values: list[float]) -> list[float]:
    """平均秩（并列取均值秩）。"""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 3:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    dx = sum((x - mx) ** 2 for x in xs) ** 0.5
    dy = sum((y - my) ** 2 for y in ys) ** 0.5
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def _spearman(xs: list[float], ys: list[float]) -> float | None:
    return _pearson(_rank(xs), _rank(ys))


def paired_ci(diffs: list[float], *, n_boot: int = 20000) -> dict:
    if len(diffs) < MIN_PAIRED_DAYS:
        return {"days": len(diffs), "delta": None, "lo": None, "hi": None}
    random.seed(42)
    means = []
    for _ in range(n_boot):
        s = [random.choice(diffs) for _ in diffs]  # noqa: S311 - bootstrap 重采样
        means.append(st.mean(s))
    means.sort()
    return {
        "days": len(diffs),
        "delta": round(st.mean(diffs), 2),
        "lo": round(means[int(0.025 * n_boot)], 2),
        "hi": round(means[int(0.975 * n_boot)], 2),
    }


def sign_test(diffs: list[float]) -> dict:
    pos = sum(1 for d in diffs if d > 0)
    neg = sum(1 for d in diffs if d < 0)
    zero = len(diffs) - pos - neg
    n = pos + neg
    if n < MIN_PAIRED_DAYS:
        return {"pos": pos, "neg": neg, "zero": zero, "p": None}
    from math import comb

    k = min(pos, neg)
    p = min(1.0, 2 * sum(float(comb(n, i)) for i in range(k + 1)) / (2**n))
    return {"pos": pos, "neg": neg, "zero": zero, "p": round(p, 4)}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="scripts/rerank_llm_backtest.py", description="LLM 排序 vs 确定性排序")
    p.add_argument("--top", type=int, default=3, help="每天取前 N 只（默认 3）")
    p.add_argument("--llm-max-days", type=int, default=15, help="最多用多少个测试日调模型（默认 15，控制成本）")
    p.add_argument(
        "--min-labeled",
        type=int,
        default=MIN_LABELED_ROWS,
        help=f"每天至少要有多少只带次日标签的票（默认 {MIN_LABELED_ROWS}；太少则该日 top-N 近乎全选）",
    )
    p.add_argument("--no-fetch", action="store_true", help="只用缓存，不出网（零成本重算）")
    p.add_argument("--days", type=int, default=0, help="只看近 N 天的样本（0 = 全部）")
    p.add_argument("--json", action="store_true", help="机器可读输出")
    args = p.parse_args(argv)
    _load_env()

    conn = connect_readonly(resolve_db_path())
    try:
        dates = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT date FROM recommendations WHERE COALESCE(excluded, 0) = 0 "
                "AND next_day_pct IS NOT NULL ORDER BY date DESC"
            ).fetchall()
        ]
        dates = sorted(dates)[-args.days :] if args.days else sorted(dates)
        pools: dict[str, list[dict]] = {}
        for d in dates:
            try:
                rows = load_day_pool(conn, d)
            except RuntimeError:
                rows = []
            # ⚠ **只留带次日标签的行**：没标签的票无法判定命中与否，留在池里会被
            #   当成「未命中」拉低两边；而「哪只被排前面」不同 ⇒ 两侧被拉低的程度
            #   不同 ⇒ 比较不公平。只留有标签的 ⇒ 两侧吃**完全相同**的候选集。
            rows = [r for r in rows if r["ndp"] is not None]
            if len(rows) >= args.min_labeled:
                pools[d] = rows
    finally:
        conn.close()

    if len(pools) < DEFAULT_TRAIN_DAYS + DEFAULT_TEST_DAYS:
        msg = f"可用日期不足（{len(pools)} 个；需要 ≥ {DEFAULT_TRAIN_DAYS + DEFAULT_TEST_DAYS}）"
        if args.json:
            print(json.dumps({"ok": False, "error": msg}, ensure_ascii=False, indent=2))
        else:
            print(f"[!] {msg}")
        return 4

    # 测试日 = 最近的若干天（倒序取，取够 llm-max-days 即止）
    test_dates = sorted(pools)[-args.llm_max_days :]

    cfg = None if args.no_fetch else magpie_config()
    if not args.no_fetch and cfg is None:
        msg = "未配置模型服务凭据（MAGPIE_*）；或用 --no-fetch 只跑缓存。"
        if args.json:
            print(json.dumps({"ok": False, "error": msg}, ensure_ascii=False, indent=2))
        else:
            print(f"[!] {msg}")
        return 1

    cache = load_cache()
    results: list[dict] = []
    for d in test_dates:
        rows = pools[d]
        # 证据行用**全样本**口径（与线上素材同构：先验 + 实测 + 分数段说明）
        evidence = build_evidence_items(
            {
                "label_n": 0,
                # ⚠ 先验必须是**真表**（`CATEGORY_HIT_RATE`），不能用占位值：先前传了
                #   7.8% 默认值 ⇒ 模型看到「所有类别先验相同」⇒ 失去类别信号、只能
                #   退回按资金流排（实测 2026-10-30 那日就是这样）。单源 = config。
                "prior": {
                    c: CATEGORY_HIT_RATE.get(c, CATEGORY_HIT_RATE_DEFAULT)
                    for c in sorted({r["category"] for r in rows})
                },
                "measured": {},
                "score_buckets": [],
                "board": {},
            }
        )
        mats = build_pool_materials(d, rows, evidence=evidence)
        fp = materials_fingerprint(mats)
        try:
            res = llm_rank_for_day(cfg, mats, cache=cache, fp=fp, no_fetch=args.no_fetch)
        except AIUpstreamError as e:
            print(f"[!] {d} 上游失败：{e}", file=sys.stderr)
            continue
        if res.get("error"):
            continue
        save_cache(cache)

        llm_order = res["ordered"]
        det_order = [r["symbol"] for r in sorted(rows, key=lambda r: r["system_p"], reverse=True)]
        # 证据支持的确定性排序键（先验 → 主力净占比 → 系统概率）：与生产口径同源
        # （`ai_digest.rerank.deterministic_order`），作为第三个对照列。
        evi_order = [r["symbol"] for r in deterministic_order(rows)]
        ndp_by = {r["symbol"]: r["ndp"] for r in rows}

        def _hit(seq: list[str], n: int, _ndp: dict = ndp_by) -> float | None:
            sel = [s for s in seq[:n] if s in _ndp]
            if len(sel) < n:
                return None
            return sum(1 for s in sel if _ndp[s] is not None and _ndp[s] >= HIT_THRESHOLD) / len(sel) * 100

        pool_hit = (
            sum(1 for r in rows if r["ndp"] is not None and r["ndp"] >= HIT_THRESHOLD) / len(rows) * 100
            if rows
            else 0.0
        )
        llm_h = _hit(llm_order, args.top)
        det_h = _hit(det_order, args.top)
        evi_h = _hit(evi_order, args.top)
        llm_ic = order_ic(llm_order, ndp_by)
        det_ic = order_ic(det_order, ndp_by)
        results.append(
            {
                "date": d,
                "n_pool": len(rows),
                "pool_hit": pool_hit,
                "llm_hit": llm_h,
                "det_hit": det_h,
                "evi_hit": evi_h,
                "llm_ic": llm_ic,
                "det_ic": det_ic,
                "llm_missing": res["missing"],
                "llm_unknown": res["unknown"],
                "llm_duplicates": res["duplicates"],
                "llm_loose": res["loose"],
                "llm_redlined": res.get("redlined", False),
                "llm_empty": res.get("empty", False),
                "same_order": llm_order == det_order,
            }
        )

    if not results:
        msg = "没有可用结果（缓存为空且 --no-fetch，或全部日期上游失败）。"
        if args.json:
            print(json.dumps({"ok": False, "error": msg}, ensure_ascii=False, indent=2))
        else:
            print(f"[!] {msg}")
        return 4

    diffs = [
        (r["llm_hit"] or 0.0) - (r["det_hit"] or 0.0)
        for r in results
        if r["llm_hit"] is not None and r["det_hit"] is not None
    ]
    ci = paired_ci(diffs)
    sgn = sign_test(diffs)
    ic_diffs = [r["llm_ic"] - r["det_ic"] for r in results if r["llm_ic"] is not None and r["det_ic"] is not None]
    ic_ci = paired_ci(ic_diffs)
    ic_sgn = sign_test(ic_diffs)
    llm_mean = st.mean([r["llm_hit"] for r in results if r["llm_hit"] is not None])
    det_mean = st.mean([r["det_hit"] for r in results if r["det_hit"] is not None])
    pool_mean = st.mean([r["pool_hit"] for r in results])
    llm_ic_mean = st.mean([r["llm_ic"] for r in results if r["llm_ic"] is not None]) if ic_diffs else None
    det_ic_mean = st.mean([r["det_ic"] for r in results if r["det_ic"] is not None]) if ic_diffs else None
    same_order = sum(1 for r in results if r["same_order"])

    payload = {
        "ok": True,
        "top_n": args.top,
        "days": len(results),
        "days_compared": len(diffs),
        "days_compared_ic": len(ic_diffs),
        "pool_hit_rate": round(pool_mean, 2),
        "deterministic_hit_rate": round(det_mean, 2),
        "llm_hit_rate": round(llm_mean, 2),
        "delta_llm_minus_det": ci["delta"],
        "ci_lo": ci["lo"],
        "ci_hi": ci["hi"],
        "sign_pos": sgn["pos"],
        "sign_zero": sgn["zero"],
        "sign_neg": sgn["neg"],
        "sign_p": sgn["p"],
        "deterministic_ic": round(det_ic_mean, 4) if det_ic_mean is not None else None,
        "llm_ic": round(llm_ic_mean, 4) if llm_ic_mean is not None else None,
        "delta_ic": ic_ci["delta"],
        "ic_ci_lo": ic_ci["lo"],
        "ic_ci_hi": ic_ci["hi"],
        "ic_sign_pos": ic_sgn["pos"],
        "ic_sign_zero": ic_sgn["zero"],
        "ic_sign_neg": ic_sgn["neg"],
        "ic_sign_p": ic_sgn["p"],
        "identical_order_days": same_order,
        "results": results,
    }

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print("=" * 78)
    print(f"Phase 2 · LLM 排序 vs 确定性排序（top-{args.top}）")
    print("=" * 78)
    print(
        f"测试日 {len(results)} 天（可比 {len(diffs)} 天）· 重建池均 {st.mean([r['n_pool'] for r in results]):.1f} 只"
    )
    print(f"全池基线 {pool_mean:.2f}%  ·  确定性（按系统概率）{det_mean:.2f}%  ·  LLM {llm_mean:.2f}%")
    print()
    print("【主指标】排序质量（rank IC：名次 vs 次日收益；用满整条排序的功效）")
    if ic_ci["delta"] is None:
        print(f"  ΔIC(LLM − 确定性) = —（可比 {len(ic_diffs)} 天 < {MIN_PAIRED_DAYS}，不出区间）")
    else:
        print(
            f"  确定性 IC {det_ic_mean:+.4f}  ·  LLM IC {llm_ic_mean:+.4f}  ·  "
            f"Δ {ic_ci['delta']:+.4f}  95% CI [{ic_ci['lo']:+.4f}, {ic_ci['hi']:+.4f}]"
        )
        print(f"  逐日正/零/负 {ic_sgn['pos']}/{ic_sgn['zero']}/{ic_sgn['neg']}（符号 p={ic_sgn['p']}）")
    print()
    print("【次指标】top-N 命中率（基线低时功效弱，仅作参考）")
    if ci["delta"] is None:
        print(f"  Δ(LLM − 确定性) = —（可比 {len(diffs)} 天 < {MIN_PAIRED_DAYS}，不出区间）")
    else:
        print(
            f"  Δ(LLM − 确定性) = {ci['delta']:+.2f}pp  95% CI [{ci['lo']:+.2f}, {ci['hi']:+.2f}]"
            f"  逐日正/零/负 {sgn['pos']}/{sgn['zero']}/{sgn['neg']}（符号 p={sgn['p']}）"
        )
    print(f"LLM 与确定性排序**完全相同**的天数：{same_order}/{len(results)}")
    print()
    print(f"{'日期':<12}{'池':>4}{'全池':>8}{'确定性':>9}{'LLM':>8}{'IC(确定)':>10}{'IC(LLM)':>10}  备注")
    for r in results:
        note = []
        if r["llm_missing"]:
            note.append("漏排" + str(len(r["llm_missing"])))
        if r["llm_unknown"]:
            note.append("池外码" + str(len(r["llm_unknown"])))
        if r["llm_loose"]:
            note.append("格式不规范")
        if r.get("llm_redlined"):
            note.append("触红线")
        if r.get("llm_empty"):
            note.append("空响应")

        def _f(v, fmt="{:.1f}") -> str:
            return fmt.format(v) if v is not None else "—"

        def _pct(v) -> str:
            return "—" if v is None else f"{v:.1f}%"

        print(
            f"{r['date']:<12}{r['n_pool']:>4}{_pct(r['pool_hit']):>8}"
            f"{_pct(r['det_hit']):>9}{_pct(r['llm_hit']):>8}"
            f"{_f(r['det_ic'], '{:+.3f}'):>10}{_f(r['llm_ic'], '{:+.3f}'):>10}  {' '.join(note)}"
        )
    print()
    print("⚠ 历史池子是**重建**的（少了展示层实时覆盖与硬门剔除）⇒ 绝对命中率不可与线上比，")
    print("   只有「同一重建池上 LLM vs 确定性」的相对比较有效。")
    print("⚠ `system_p` 的因子 OR 是全历史校准的常数，两边共用 ⇒ 对本比较无偏。")
    print("⚠ 本脚本不给建议、不产短名单。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
