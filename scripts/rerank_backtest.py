# ruff: noqa: S311  # bootstrap 重采样用 random，非密码学用途
"""**池内排序依据的样本外检验**（2026-11-04）。

回答一个问题：v1 池选排序依据（类别先验 + `nextday_prob`
系统校准概率）**到底有没有边际**？没有边际的话，listwise 那一步只是把噪声排得更漂亮。

## 为什么需要这个脚本（它补的是哪块证据）

`rule_validate` 的三个评估器**都看不见 LLM 输出** ⇒ rerank 的产出永远过不了
样本外验证门。且 rerank 本身只是「把证据排个序」，真正可检验的对象是**排序依据**
（先验 + 系统概率），不是模型措辞。故本脚本检验的是：
**按「系统概率」降序取 top-N，是否比按别的键取 top-N 更容易次日 ≥7%？**

## 口径（全部复用现成单源，不另立）

- 样本：`nextday_attribution.load_dedup`（校准样本唯一实现，excluded=0 + 取最后一轮）；
- 命中：次日涨幅 ≥7%（系统唯一目标函数口径）；
- 主指标：**按日等权 top-N 次日 hit 率**（与 `rule_validate` 同口径）；
- 显著性：**按日配对 bootstrap 95% CI**（与 `scripts/shadow_feature_backtest.py` 同口径：
  同一天内「top-N 命中率 − 当日全量命中率」逐日配对后重采样）；
- ⚠ 配对天数不足 3 天时**故意不出区间**（`MIN_PAIRED_DAYS`）：n=1 的 CI 退化成宽度 0
  的 `[d,d]`，看着极显著实则是样本量为一（AGENTS.md 硬规则）。

## 比较的键（每个都是一种排序依据）

| 键 | 含义 | 期望 |
|---|---|---|
| `system_p` | `nextday_prob` 系统校准概率（**rerank 的主依据**） | 应最高 |
| `prior` | 类别先验 `CATEGORY_HIT_RATE` | 与 `system_p` 同向 |
| `score` | 落库分数（**对照组**：已知无区分力） | 应最差 |
| `flow` | 主力净占比 | 检验资金流单独有没有边际 |
| `reverse_system_p` | 系统概率**倒序**（负对照：应显著为负） | 应为负 |

`reverse_system_p` 是**负对照**：如果连它都不显著为负，说明这套指标根本测不出东西
（样本量不够 / 口径失效），此时任何正结论都不可信。

## 用法

```bash
python scripts/rerank_backtest.py                # 主指标：各键的 top-N hit 率 + 配对 CI
python scripts/rerank_backtest.py --top 3        # top-3（默认 3）
python scripts/rerank_backtest.py --days 60      # 只看近 60 天
python scripts/rerank_backtest.py --json         # 机器可读
```

⚠ **本脚本不给任何买卖建议、不产出短名单**（与 `scripts/offboard_nextday.py` 同纪律）：
它只报告「某个排序键在样本外是否比另一个键更容易命中」，把结论留在读者脑子里。
"""

from __future__ import annotations

import argparse
import json
import random  # noqa: S311 - bootstrap 重采样，非密码学用途
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scanner.ai_digest.source import connect_readonly  # noqa: E402
from scanner.config import CATEGORY_HIT_RATE_DEFAULT, resolve_db_path  # noqa: E402
from scanner.models import parse_score_breakdown  # noqa: E402
from scanner.nextday_attribution import attach_prominence, load_dedup  # noqa: E402
from scanner.nextday_prob import next_day_hit_probability  # noqa: E402
from scanner.ranking import entry_fund_flow_pct  # noqa: E402
from scanner.walkforward import walkforward_windows  # noqa: E402

#: 配对天数下限：不足则**不出区间**（n=1 的 CI 是宽度 0 的假显著）。
MIN_PAIRED_DAYS = 3
HIT_THRESHOLD = 7.0

#: walkforward 窗口参数（与 `scanner.rule_validate` 同款，避免两套切分口径）。
DEFAULT_TRAIN_DAYS = 30
DEFAULT_TEST_DAYS = 10
#: embargo：标签 horizon=1，不空窗时 train 实际见过 test 首日的答案（样本外 hit 被高估）。
WF_EMBARGO_DAYS = 2

#: **v1 池选口径**（rerank 排的就是这个池）：类别单源 = `view.assemble` 的 `main_recs`
#: 排除条件（comeback / core_dip / pool_pick）+ 现役五类。
#: ⚠ 不加这个过滤会把 `core_dip`（696 行，实测最差桶之一）当普通候选排进来 ——
#:   rerank 线上永远排不到 core_dip 票，回放却排它 = **口径错配**，测的是不存在的东西。
V1_POOL_CATEGORIES: frozenset[str] = frozenset({"rebound", "known_new_face", "momentum", "new_face", "short_term"})

#: 排序键 → 取值函数。返回 None = 该键无数据（该行在该键下不可用）。
_KEYS: tuple[tuple[str, str], ...] = (
    ("system_p", "系统校准次日概率（rerank 主依据）"),
    ("prior", "类别先验命中率"),
    ("score", "落库分数（对照：已知无区分力）"),
    ("flow", "主力净占比"),
    ("reverse_system_p", "系统概率倒序（负对照，应为负）"),
)


def _num(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int | float):
        f = float(v)
        return f if f == f and f not in (float("inf"), float("-inf")) else None
    return None


def build_samples(conn, days: int = 0) -> list[dict]:
    """校准样本 → 带各排序键取值的行（口径复用 `load_dedup`，不另造样本）。

    ⚠ **只保留 v1 池选五类**（`V1_POOL_CATEGORIES`）—— rerank 线上排的就是这个池，
    回放必须同池，否则测的是不存在的场景（core_dip 占样本 26%，先验 6.5% 最低）。
    """
    recs = load_dedup(conn, days=days)
    attach_prominence(conn, recs)
    out: list[dict] = []
    for r in recs:
        if r["next_day"] is None:
            continue
        if r["category"] not in V1_POOL_CATEGORIES:
            continue
        entry = dict(r)
        # ⚠ `load_dedup` 的列名是 `breakdown`（原始 JSON 串），而 `entry_dims` /
        #   `entry_fund_flow_pct` 读的是 `score_breakdown`（已解析 dict）——
        #   不解析 ⇒ 资金流全空（实测：2663 行里 0 行有 flow）。口径同 `rule_validate.load_sample`。
        entry["score_breakdown"] = parse_score_breakdown(r.get("breakdown"))
        entry["_candidate"] = None
        flow = entry_fund_flow_pct(entry)
        p = next_day_hit_probability(entry, prominence=r.get("_prominent"), flow=flow)
        out.append(
            {
                "date": r["date"][:10],
                "symbol": r["symbol"],
                "category": r["category"],
                "ndp": float(r["next_day"]),
                "system_p": p,
                "score": _num(r.get("score")) or 0.0,
                "flow": flow,
                # `prior` 留空：先验要用 **train 窗标签重算**（walkforward 模式），
                # 否则就是「拿全历史校准的先验去检验全历史」= 不干净的样本外。
                "prior": None,
            }
        )
    return out


def train_prior(train_rows: list[dict]) -> dict[str, float]:
    """**只用 train 窗的标签**重算类别先验（walkforward 的样本外关键）。

    这就是「先验」在样本外应有的样子：训练期看到什么就算什么，测试期不许回头改。
    线上跑时先验是手抄常量（`CATEGORY_HIT_RATE`），而那份常量本身就是全历史校准的 ——
    故这里重算一遍是**对线上更严格**的检验（线上用的先验见过测试期）。
    """
    n: dict[str, int] = defaultdict(int)
    h: dict[str, int] = defaultdict(int)
    for r in train_rows:
        c = r["category"]
        n[c] += 1
        if r["ndp"] >= HIT_THRESHOLD:
            h[c] += 1
    out: dict[str, float] = {}
    for c, cnt in n.items():
        # 样本太少（<10）的类别回退到**全局基线**（与线上 CATEGORY_HIT_RATE_DEFAULT 同义）
        out[c] = (h[c] / cnt) if cnt >= 10 else CATEGORY_HIT_RATE_DEFAULT
    return out


def with_prior(rows: list[dict], prior: dict[str, float]) -> list[dict]:
    """给样本行填上（给定口径下的）先验与它的倒序键。"""
    out = []
    for r in rows:
        p = prior.get(r["category"], CATEGORY_HIT_RATE_DEFAULT)
        out.append({**r, "prior": p, "reverse_system_p": -r["system_p"]})
    return out


def topn_hit_rate(rows: list[dict], key: str, top_n: int) -> float | None:
    """该键取 top-N 的次日 hit 率（%）。无可用值 / 不足 N 只 → None。"""
    usable = [r for r in rows if r.get(key) is not None]
    if len(usable) < top_n:
        return None
    ordered = sorted(usable, key=lambda r: r[key], reverse=True)[:top_n]
    return sum(1 for r in ordered if r["ndp"] >= HIT_THRESHOLD) / len(ordered) * 100


def daily_diff(rows: list[dict], key: str, top_n: int) -> list[float]:
    """逐日「top-N 命中率 − 当日全量命中率」（配对 bootstrap 的样本）。"""
    by_day: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_day[r["date"]].append(r)
    diffs: list[float] = []
    for rs in by_day.values():
        sub = [r for r in rs if r.get(key) is not None]
        if len(sub) < top_n:
            continue
        base = sum(1 for r in rs if r["ndp"] >= HIT_THRESHOLD) / len(rs) * 100
        diffs.append((topn_hit_rate(sub, key, top_n) or 0.0) - base)
    return diffs


def paired_ci(diffs: list[float], *, n_boot: int = 20000) -> dict:
    """按日配对 bootstrap 95% CI。天数不足 `MIN_PAIRED_DAYS` → **不出区间**。"""
    if len(diffs) < MIN_PAIRED_DAYS:
        return {"days": len(diffs), "delta": None, "lo": None, "hi": None, "note": "配对天数不足，不出区间"}
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
    """**符号检验**（逐日 +/− 计数）：比 bootstrap on mean 更有功效。

    项目在 B 段新门用过同款（`AGENTS.md`：逐日 正17/负4，符号检验 p=0.0009 ⇒ 显著）。
    配对 bootstrap 压均值、符号检验压方向 —— 两者一起看更稳：
    bootstrap CI 跨 0 但符号检验显著 ⇒ 方向一致、幅度不精确（常见于样本量偏小）。

    ⚠ **零值日不进分母**（2026-11-04 实测修正）：top-3 口径下常见「top-N 命中数 = 全池
    命中数」（差值 0）——那是**无信息**，不是「负」。把它们算进负侧会让符号检验系统性偏负
    （实测 69 天里 32 天为零 ⇒ 主依据被判成「正 12 / 负 25」）。
    """
    pos = sum(1 for d in diffs if d > 0)
    neg = sum(1 for d in diffs if d < 0)
    zero = len(diffs) - pos - neg
    n = pos + neg
    if n < MIN_PAIRED_DAYS:
        return {"pos": pos, "neg": neg, "zero": zero, "p": None, "note": "有效天数不足，不做检验"}
    # 双侧二项检验 p 值（不引 scipy：手算 tail 概率）
    k = min(pos, neg)
    tail = sum(_comb(n, i) for i in range(0, k + 1)) / (2**n)
    p = min(1.0, 2 * tail)
    return {"pos": pos, "neg": neg, "zero": zero, "p": round(p, 4)}


def daily_rank_ic(rows: list[dict], key: str) -> float | None:
    """单日 **Spearman rank IC**：该键的排序 vs 次日收益的相关性。

    为什么必须加这个指标：top-N 命中率在小池子上噪声压倒信号（3 只票的命中率只能是
    0/33/67/100%），而 rank IC 用**整池**的排序信息、单日就能算，且对极端日不敏感
    （这是 `scanner.backtest.spearman` 的同一口径）。
    """
    usable = [r for r in rows if r.get(key) is not None]
    if len(usable) < 3:
        return None
    xs = [r[key] for r in usable]
    ys = [r["ndp"] for r in usable]
    return _spearman(xs, ys)


def within_category_rank_ic(rows: list[dict], key: str) -> float | None:
    """**类别内** rank IC：先把每日样本按类别分组，在组内算 IC 再平均。

    为什么必须有这个指标（2026-11-04 实测踩到）：`score` 在「全池 top-3」口径下
    看起来有 +3.10pp（CI 不含 0）——但那是**类别构成效应**：某些日子高分恰好集中在
    强类别（rebound 先验 17.9%），而 score 本身在**类别内**毫无区分力
    （实测各类别「高分−低分」的次日均值差：new_face +0.53 / momentum −0.39 /
    kNF −0.06 / short_term −0.05 / rebound +0.36，正负天数接近 1:1）。

    也就是说：**先验（类别）有效，分数（类别内相对量）无效** —— 而 rerank 的排序依据
    恰恰是类别先验，所以「类别内 IC」才是与 rerank 真正匹配的度量。
    """
    by_cat: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if r.get(key) is not None:
            by_cat[r["category"]].append(r)
    ics: list[float] = []
    for _cat, group in by_cat.items():
        if len(group) < 3:
            continue
        ic = _spearman([r[key] for r in group], [r["ndp"] for r in group])
        if ic is not None:
            ics.append(ic)
    if not ics:
        return None
    return st.mean(ics)


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


def ic_ci(values: list[float], *, n_boot: int = 20000) -> dict:
    """IC 的 bootstrap 95% CI（**对 0**）：判「这个排序键的类内区分力是否非零」。

    与 `paired_ci` 不同：`paired_ci` 算的是「top-N 命中率 − 同日全池命中率」的均值，
    这里算的是 IC 序列本身的均值与区间（IC 本来就没有「基准」可减）。
    """
    if len(values) < MIN_PAIRED_DAYS:
        return {"mean": round(st.mean(values), 4) if values else None, "lo": None, "hi": None, "n": len(values)}
    random.seed(42)
    means = []
    for _ in range(n_boot):
        s = [random.choice(values) for _ in values]  # noqa: S311 - bootstrap 重采样
        means.append(st.mean(s))
    means.sort()
    return {
        "mean": round(st.mean(values), 4),
        "lo": round(means[int(0.025 * n_boot)], 4),
        "hi": round(means[int(0.975 * n_boot)], 4),
        "n": len(values),
    }


def _comb(n: int, k: int) -> float:
    from math import comb

    return float(comb(n, k))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="scripts/rerank_backtest.py", description="池内排序依据的样本外检验")
    p.add_argument("--top", type=int, default=3, help="每天取前 N 只（默认 3）")
    p.add_argument("--days", type=int, default=0, help="只看近 N 天（0 = 全部）")
    p.add_argument("--json", action="store_true", help="机器可读输出")
    p.add_argument(
        "--in-sample",
        action="store_true",
        help="不用 walkforward：先验直接用全历史标签重算（口径与线上常量一致，但**不干净**，仅作对照）",
    )
    args = p.parse_args(argv)

    conn = connect_readonly(resolve_db_path())
    try:
        samples = build_samples(conn, days=args.days)
    finally:
        conn.close()

    if not samples:
        msg = "没有可用样本（库内没有带次日标签的推荐行）。"
        if args.json:
            print(json.dumps({"ok": False, "error": msg}, ensure_ascii=False, indent=2))
        else:
            print(f"[!] {msg}")
        return 4

    by_day: dict[str, list[dict]] = defaultdict(list)
    for r in samples:
        by_day[r["date"]].append(r)

    # ── 样本切分：walkforward（train 算先验 → test 排序）──────────────────────
    # ⚠ `system_p`（nextday_prob 的因子 OR）是在全历史上校准的常数，**无法回滚** ——
    #   这一点如实标注（无法做到严格样本外），但先验可以回滚，故先验用 train 窗重算。
    dates = sorted(by_day)
    if args.in_sample:
        all_rows = with_prior(samples, train_prior(samples))
        eval_rows = all_rows
        split_desc = "样本内（先验用全历史标签重算；不干净，仅作对照）"
        n_windows = 0
    else:
        windows = walkforward_windows(dates, DEFAULT_TRAIN_DAYS, DEFAULT_TEST_DAYS, embargo_days=WF_EMBARGO_DAYS)
        if not windows:
            msg = (
                f"窗口不足（{len(dates)} 个交易日，train={DEFAULT_TRAIN_DAYS} "
                f"test={DEFAULT_TEST_DAYS} embargo={WF_EMBARGO_DAYS}）"
            )
            if args.json:
                print(json.dumps({"ok": False, "error": msg}, ensure_ascii=False, indent=2))
            else:
                print(f"[!] {msg}")
            return 4
        eval_rows = []
        for train_dates, test_dates in windows:
            tr = [r for d in train_dates for r in by_day[d]]
            te = [r for d in test_dates for r in by_day[d]]
            prior = train_prior(tr)
            eval_rows.extend(with_prior(te, prior))
        n_windows = len(windows)
        split_desc = (
            f"walkforward {n_windows} 窗（train={DEFAULT_TRAIN_DAYS} test={DEFAULT_TEST_DAYS} "
            f"embargo={WF_EMBARGO_DAYS}；先验只用 train 窗标签重算）"
        )

    eval_by_day: dict[str, list[dict]] = defaultdict(list)
    for r in eval_rows:
        eval_by_day[r["date"]].append(r)

    results: list[dict] = []
    for key, desc in _KEYS:
        daily: list[float] = []
        per_day_topn: list[float] = []
        ics: list[float] = []
        within_ics: list[float] = []
        days_used = 0
        for _d, rs in eval_by_day.items():
            sub = [r for r in rs if r.get(key) is not None]
            if len(sub) < args.top:
                continue
            days_used += 1
            per_day_topn.append(topn_hit_rate(sub, key, args.top) or 0.0)
            base = sum(1 for r in rs if r["ndp"] >= HIT_THRESHOLD) / len(rs) * 100
            daily.append((topn_hit_rate(sub, key, args.top) or 0.0) - base)
            ic = daily_rank_ic(sub, key)
            if ic is not None:
                ics.append(ic)
            wic = within_category_rank_ic(sub, key)
            if wic is not None:
                within_ics.append(wic)
        ci = paired_ci(daily)
        st_ = sign_test(daily)
        wci = ic_ci(within_ics)
        results.append(
            {
                "key": key,
                "desc": desc,
                "days": days_used,
                "topn_hit_rate": round(st.mean(per_day_topn), 2) if per_day_topn else None,
                "delta_vs_pool": ci["delta"],
                "ci_lo": ci["lo"],
                "ci_hi": ci["hi"],
                "sign_pos": st_["pos"],
                "sign_neg": st_["neg"],
                "sign_zero": st_["zero"],
                "sign_p": st_["p"],
                "rank_ic": round(st.mean(ics), 4) if ics else None,
                "rank_ic_days": len(ics),
                "within_cat_ic": round(st.mean(within_ics), 4) if within_ics else None,
                "within_cat_ic_days": len(within_ics),
                "within_cat_ic_lo": wci["lo"],
                "within_cat_ic_hi": wci["hi"],
                "note": ci.get("note"),
            }
        )

    base_hit = sum(1 for r in eval_rows if r["ndp"] >= HIT_THRESHOLD) / len(eval_rows) * 100

    if args.json:
        print(
            json.dumps(
                {
                    "ok": True,
                    "top_n": args.top,
                    "split": split_desc,
                    "n_windows": n_windows,
                    "samples_all": len(samples),
                    "samples_eval": len(eval_rows),
                    "days": len(eval_by_day),
                    "pool_hit_rate": round(base_hit, 2),
                    "results": results,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    print("=" * 78)
    print(f"池内排序依据 · 样本外检验（top-{args.top}）")
    print("=" * 78)
    print(f"样本 {len(samples)} 条（评估集 {len(eval_rows)} 条 / {len(eval_by_day)} 个交易日）")
    print(f"切分：{split_desc}")
    print(f"评估集全池次日≥7% 命中率 {base_hit:.2f}%")
    print()
    print(
        f"{'排序键':<18}{'天数':>5}{'top-N hit':>11}{'Δ vs 全池':>11}{'95% CI':>19}{'正/零/负':>11}{'符号p':>9}{'rankIC':>9}{'类内IC':>9}"
    )
    for r in results:
        ci = (
            f"[{r['ci_lo']:+.2f}, {r['ci_hi']:+.2f}]"
            if r["ci_lo"] is not None and r["ci_hi"] is not None
            else (r["note"] or "—")
        )
        delta = f"{r['delta_vs_pool']:+.2f}pp" if r["delta_vs_pool"] is not None else "—"
        hit = f"{r['topn_hit_rate']:.2f}%" if r["topn_hit_rate"] is not None else "—"
        sp = f"{r['sign_pos']}/{r['sign_zero']}/{r['sign_neg']}"
        pp = f"{r['sign_p']:.4f}" if r["sign_p"] is not None else "—"
        ic = f"{r['rank_ic']:+.4f}" if r["rank_ic"] is not None else "—"
        wic = (
            f"{r['within_cat_ic']:+.4f}[{r['within_cat_ic_lo']:+.3f},{r['within_cat_ic_hi']:+.3f}]"
            if r["within_cat_ic"] is not None and r["within_cat_ic_lo"] is not None
            else (f"{r['within_cat_ic']:+.4f}" if r["within_cat_ic"] is not None else "—")
        )
        print(f"{r['key']:<18}{r['days']:>5}{hit:>11}{delta:>11}{ci:>19}{sp:>11}{pp:>9}{ic:>9}  {wic}")
    print()
    print(f"说明：{chr(10).join('  · ' + k + ' = ' + d for k, d in _KEYS)}")
    print("口径：命中=次日涨幅≥7%（系统唯一目标函数）；样本=nextday_attribution.load_dedup；")
    print("显著性=按日配对 bootstrap 95% CI + 符号检验（配对天数<3 不出区间）。")
    print("⚠ `system_p` 的因子 OR 是全历史校准的常数，**无法回滚** ⇒ 不是严格样本外；")
    print("   先验已用 train 窗重算（可回滚）。`score`/`flow` 本就与未来无关。")
    print("⚠ 本脚本不给建议、不产短名单：只报告各排序键在样本外是否更容易命中。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
