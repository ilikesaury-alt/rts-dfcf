"""scripts/push_gate_impact.py — 飞书严格过滤门的**离线影响评估**（只读 scanner.db）。

回答三个问题，全部用真实历史数据，不联网：
  1. 门开之后，每天还剩几只？（对比现状 = 全推）
  2. 被剔掉的是不是确实是 hit 最低的那批？（用 next_day_pct >= 7 验证）
  3. 通过集稳不稳？（决定去重键能不能压住推送频率）

用法：
    python scripts/push_gate_impact.py            # 近 10 个交易日
    python scripts/push_gate_impact.py --days 30
    python scripts/push_gate_impact.py --json

⚠ 本脚本**不产出**「门应该怎么设」的结论，只如实报数 —— 阈值依据写在
scanner/config_push.py 的 docstring 里，改阈值前先跑本脚本看效果。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scanner.push_gate import TIER_A, TIER_B, TIER_C, classify  # noqa: E402
from scanner.utils import to_float  # noqa: E402

DB = Path(__file__).resolve().parent.parent / "scanner.db"


@dataclass
class Row:
    """最小行替身：只带门用到的字段（与 push_gate 的入参面一致）。"""

    symbol: str
    name: str
    category: str
    rank: int | None
    accum: float | None
    ff_pct: float | None
    risk_flags: list[str]

    @property
    def hit(self) -> bool | None:
        return None


def _rank_of(sym: str, appearances: dict[str, int]) -> int | None:
    return appearances.get(sym)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=10)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if not DB.exists():
        print(f"找不到 {DB}", file=sys.stderr)
        return 1
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    days = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT date FROM recommendations WHERE excluded=0 ORDER BY date DESC LIMIT ?",
            (args.days,),
        )
    ]

    from scanner.config import (
        FUND_OUTFLOW_NET_PCT,
        OVERHEAT_ACCUM_MAX,
        PUSH_FALLBACK_ENABLED,
        PUSH_FALLBACK_MAX_RANK,
        PUSH_TIER_A_MIN,
        PUSH_TIER_B_MIN,
    )

    report = []
    tot_before = tot_after = 0
    kept_hits = kept_n = drop_hits = drop_n = 0
    tier_counter: Counter = Counter()

    for d in days:
        # 榜单排名：取当日 appearances 表最后一条
        rank_map: dict[str, int] = {}
        for r in conn.execute("SELECT symbol, rank FROM appearances WHERE date=? ORDER BY id", (d,)):
            if r["rank"] is not None:
                rank_map[r["symbol"]] = r["rank"]

        # 每票当日最后一轮
        last: dict[str, sqlite3.Row] = {}
        for r in conn.execute(
            """SELECT * FROM recommendations WHERE date=? AND excluded=0 ORDER BY time""",
            (d,),
        ):
            last[r["symbol"]] = r

        kept, dropped = [], []
        for sym, r in last.items():
            bd = {}
            try:
                bd = json.loads(r["score_breakdown"] or "{}")
            except ValueError:
                pass
            ff = to_float(bd.get("fund_flow_main_pct"), default=None)
            accum = to_float(r["accumulated_pct"], default=None)
            # 否决（与 push_gate._veto_common 同式）
            if (ff is not None and ff <= FUND_OUTFLOW_NET_PCT) or (accum is not None and accum >= OVERHEAT_ACCUM_MAX):
                dropped.append((sym, r, "veto"))
                continue
            tier, _hit = classify(r["category"])
            tier_counter[tier] += 1
            rank = rank_map.get(sym)
            fb = PUSH_FALLBACK_ENABLED and rank is not None and rank <= PUSH_FALLBACK_MAX_RANK
            if tier in (TIER_A, TIER_B) or fb:
                kept.append((sym, r, tier))
            else:
                dropped.append((sym, r, "no_fallback"))

        for _s, r, _t in kept:
            nd = to_float(r["next_day_pct"], default=None)
            if nd is not None:
                kept_n += 1
                kept_hits += nd >= 7
        for _s, r, _t in dropped:
            nd = to_float(r["next_day_pct"], default=None)
            if nd is not None:
                drop_n += 1
                drop_hits += nd >= 7

        n_before = len(last)
        n_after = len(kept)
        tot_before += n_before
        tot_after += n_after
        cats = Counter(r["category"] for _s, r, _t in kept)
        report.append(
            {
                "date": d,
                "before": n_before,
                "after": n_after,
                "kept_cats": dict(cats),
                "kept_names": [r["name"] for _s, r, _t in kept][:8],
            }
        )

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    print("=" * 78)
    print("飞书严格过滤门 · 离线影响评估（hit = 次日≥7%，可标注的样本）")
    print(
        f"门参数：A≥{PUSH_TIER_A_MIN:.0%}  B≥{PUSH_TIER_B_MIN:.1%}  "
        f"兜底=榜内排名≤{PUSH_FALLBACK_MAX_RANK}（{PUSH_FALLBACK_ENABLED}）"
    )
    print("=" * 78)
    print(f"{'日期':<12}{'现状':>6}{'门后':>6}{'削减':>8}   通过票的类别构成")
    for r in report:
        cut = f"−{r['before'] - r['after']}" if r["after"] < r["before"] else "0"
        cats_txt = " ".join(f"{k}×{v}" for k, v in sorted(r["kept_cats"].items())) or "—"
        print(f"{r['date']:<12}{r['before']:>6}{r['after']:>6}{cut:>8}   {cats_txt}")
    print("-" * 78)
    cut_pct = 100 - tot_after / tot_before * 100 if tot_before else 0.0
    print(f"合计：现状 {tot_before} 只 → 门后 {tot_after} 只（削减 {cut_pct:.0f}%）")
    print(f"档位分布（全期）：A={tier_counter[TIER_A]} B={tier_counter[TIER_B]} C={tier_counter[TIER_C]}")
    print("-" * 78)
    print("** 门是否剔掉了该剔的（用次日≥7% 实测）**")
    if kept_n:
        print(f"  通过 {kept_n} 只 → hit {kept_hits} = {kept_hits / kept_n * 100:.1f}%")
    if drop_n:
        print(f"  剔除 {drop_n} 只 → hit {drop_hits} = {drop_hits / drop_n * 100:.1f}%")
    if kept_n and drop_n:
        lift = kept_hits / kept_n - drop_hits / drop_n
        print(f"  差值 {lift * 100:+.1f}pp  {'✅ 门在提高质量' if lift > 0 else '❌ 门在误杀（需重设阈值）'}")
    print("-" * 78)
    print("** 最近一日通过名单（人工核对用）**")
    if report:
        print(f"  {report[0]['date']}: " + ("、".join(report[0]["kept_names"]) or "（空）"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
