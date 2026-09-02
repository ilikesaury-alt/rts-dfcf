# -*- coding: utf-8 -*-
"""忠实回放：用生产 scanner/ranking.py 的 _entry_tier/_is_nextday_marked 对历史推荐分档，
验证综合排序档位规则的区分度（next_day hit ≥7% / 平均次日 / cum_3d）。"""

import json
import sys
from typing import Any, cast

cast(Any, sys.stdout).reconfigure(encoding="utf-8")  # Windows 中文输出（同 today_report）
sys.path.insert(0, ".")
# E402 为刻意设计：必须先 sys.path.insert(0, ".") 才能 import scanner.*，
# 否则脚本从其他工作目录直接执行时找不到包。
from scanner import ranking  # noqa: E402
from scanner.config import NEXTDAY_CAT_PRIORITY, NEXTDAY_HIT_THRESHOLD  # noqa: E402
from scanner.database import init_db  # noqa: E402

THRESH = NEXTDAY_HIT_THRESHOLD  # 单源见 config


def main():
    conn = init_db()
    rows = conn.execute(
        "SELECT date, symbol, name, category, score, percent, accumulated_pct, "
        "next_day_pct, score_breakdown FROM recommendations "
        "WHERE date >= '2026-07-01' AND COALESCE(excluded, 0) = 0 "
        "ORDER BY date, symbol"
    ).fetchall()
    seen = {}
    for r in rows:
        key = (r[0], r[1])
        cat = r[2]
        if key not in seen or (cat != "comeback" and seen[key][2] == "comeback"):
            seen[key] = r
    recs = []
    for r in seen.values():
        if r[7] is None:
            continue
        try:
            sb = json.loads(r[8] or "{}")
        except Exception:
            sb = {}
        recs.append(
            {
                "date": r[0],
                "symbol": r[1],
                "name": r[2],
                "category": r[3],
                "score": r[4],
                "percent": r[5] or 0.0,
                "accumulated_pct": r[6],
                "next_day": r[7],
                "score_breakdown": sb,
                "_candidate": None,
            }
        )
    print(f"回放样本（去重 + 有 next_day_pct）: {len(recs)}")

    def stat(group, label):
        n = len(group)
        if n == 0:
            print(f"{label}: n=0")
            return
        hits = [r for r in group if r["next_day"] >= THRESH]
        avg = sum(r["next_day"] for r in group) / n
        print(f"{label}: n={n:<4} hit≥{THRESH}%={len(hits) / n * 100:5.1f}%  avg_next={avg:+6.2f}%")

    by_tier = {0: [], 1: [], 2: [], 3: []}
    for r in recs:
        tier = ranking._entry_tier(r, conn)
        by_tier[tier].append(r)
    print("\n== 档位区分度 ==")
    for t in (0, 1, 2, 3):
        stat(by_tier[t], f"档{t}")

    # 只统计主区（非 comeback）类别内部分
    main_recs = [r for r in recs if r["category"] != "comeback"]
    by_tier_main = {0: [], 1: [], 2: [], 3: []}
    for r in main_recs:
        by_tier_main[ranking._entry_tier(r, conn)].append(r)
    print("\n== 主区（榜上五类）档位区分度 ==")
    for t in (0, 1, 2, 3):
        stat(by_tier_main[t], f"档{t}")

    # 关键子集验证
    print("\n== 子集 ==")

    def subset(pred, label):
        g = [r for r in recs if pred(r)]
        stat(g, label)

    subset(
        lambda r: (
            r["category"] == "short_term"
            and (r["score_breakdown"].get("st_weak_to_strong") or r["score_breakdown"].get("v_st_weak"))
        ),
        "short_term 弱转强",
    )
    subset(
        lambda r: (
            r["category"] == "short_term"
            and not (r["score_breakdown"].get("st_weak_to_strong") or r["score_breakdown"].get("v_st_weak"))
        ),
        "short_term 非弱转强",
    )

    def overb(r):
        d = r["score_breakdown"]
        return bool(
            d.get("st_overbought_flag")
            or d.get("mo_overbought_flag")
            or d.get("v_st_overbought")
            or d.get("v_mo_overbought")
        )

    subset(overb, "超买(全部)")
    subset(lambda r: not overb(r), "非超买(全部)")

    # 小板块共振（非 comeback）
    def small_sec(r):
        d = r["score_breakdown"]
        if not (d.get("v_st_sector") or d.get("v_pb_sector") or d.get("v_nf_sector")):
            return False
        cnt = d.get("v_st_sector_count") or d.get("v_pb_sector_count") or d.get("v_nf_sector_count") or 0
        return cnt < 15

    subset(small_sec, "小板块共振 cnt<15")
    subset(lambda r: not small_sec(r) and r["category"] != "comeback", "非小板块共振(主区)")

    # 涨幅带
    def band(p):
        if p < 0:
            return "down"  # noqa: E701  （以下同：紧凑分档表，一行一档便于对照阈值）
        if p < 2.0:
            return "sweet_low"  # noqa: E701
        if p < 4.0:
            return "dead"  # noqa: E701
        if p < 8.0:
            return "sweet_mid"  # noqa: E701
        if p < 10.0:
            return "trap"  # noqa: E701
        return "hot"

    subset(lambda r: band(r["percent"]) == "dead", "2-4% 死区")
    subset(lambda r: band(r["percent"]) == "trap", "8-10% 陷阱")
    subset(lambda r: r["accumulated_pct"] is not None and r["accumulated_pct"] >= 50, "累计≥50% 过热")

    # 🎯 判定单独验证
    print("\n== 🎯 标记 ==")
    marked = [r for r in recs if ranking._is_nextday_marked(r, conn)]
    not_marked = [r for r in recs if r["category"] in NEXTDAY_CAT_PRIORITY and not ranking._is_nextday_marked(r, conn)]
    stat(marked, "🎯 标记")
    stat(not_marked, "非🎯(可标记类别)")


if __name__ == "__main__":
    main()
