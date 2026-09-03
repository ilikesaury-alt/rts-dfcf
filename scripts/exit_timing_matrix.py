"""进出时点 × 选法 的完整矩阵。

把「选股能力」与「进出时点」解耦：
  - 若同一选法下只是卖出时点变化 → 是执行问题（卖早了）
  - 若所有时点下某选法都负 → 是选股问题（池子/规则无效）

用法: python scripts/exit_timing_matrix.py
"""

from __future__ import annotations

import sqlite3
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from intraday_exit_test import (BAD_CAT, GOOD_TREND, build,  # noqa: E402
                                portfolio)

CACHE = ROOT / "scripts" / ".cache_m5.sqlite3"
EXITS = ["10:00", "11:30", "14:30", "15:00"]


def load_next_bars(symbols, pairs):
    conn = sqlite3.connect(CACHE)
    out: dict[tuple, dict] = defaultdict(dict)
    for sym, d in pairs:
        for t, c in conn.execute(
                "SELECT time, close FROM m5 WHERE symbol=? AND date=?", (sym, d)):
            out[(sym, d)][t] = c
    conn.close()
    return out


def main() -> None:
    rows, notes = build("2026-07-22")
    nb = load_next_bars(sorted({r["symbol"] for r in rows}),
                        [(r["symbol"], r["_nxt"]) for r in rows])

    # 给每条样本挂上各卖出时点的收益（两种入场 × 四种出场）
    keep = []
    for r in rows:
        bars = nb.get((r["symbol"], r["_nxt"])) or {}
        ok = True
        for t in EXITS:
            c = bars.get(t)
            if not c:
                ok = False
                break
            r[f"e_{t}"] = c
        if ok:
            keep.append(r)
    print(f"完整覆盖 {len(keep)}/{len(rows)} 条"
          f"（缺失=次日停牌或该时点无成交）\n")
    rows = keep

    for r in rows:
        for t in EXITS:
            r[f"R_scan_{t}"] = (r[f"e_{t}"] / r["_p_first"] - 1) * 100
            r[f"R_close_{t}"] = (r[f"e_{t}"] / r["_p_close"] - 1) * 100

        # 🎯 / 档位
    try:
        from scanner.ranking import _entry_tier, _is_nextday_marked
        c2 = sqlite3.connect(ROOT / "scanner.db")
        for r in rows:
            try:
                accum = float(r["_bd"].get("accumulated_incl_today"))
            except (TypeError, ValueError):
                accum = None
            entry = {"category": r["category"], "symbol": r["symbol"],
                     "percent": r["percent"], "score_breakdown": r["_bd"],
                     "_candidate": None}
            try:
                r["_mark"] = _is_nextday_marked(entry, c2, accum=accum)
                r["_tier"] = _entry_tier(entry, c2, accum=accum, marked=r["_mark"])
            except Exception:
                r["_mark"] = r["_tier"] = None
        c2.close()
    except Exception as e:  # noqa: BLE001
        print(f"[!] 🎯 重算跳过：{e}")

    r5 = [r for r in rows if r["trend"] in GOOD_TREND
          and r["category"] not in BAD_CAT and (r["percent"] or 0) < 10]
    mark = [r for r in rows if r.get("_mark") is True]
    subsets = [("全买（不筛选）", rows), ("只买 🎯", mark), ("R5 规则", r5)]

    ds = sorted({r["date"] for r in rows})
    cut = ds[int(len(ds) * 0.6)]

    for entry, ename in [("R_scan", "推荐时刻买入"), ("R_close", "T日收盘买入")]:
        print("=" * 100)
        print(f"【{ename}】各行=卖出时点  日均(累计)  ｜ IS/OOS 拆分")
        print("=" * 100)
        for nm, sub in subsets:
            if not sub:
                continue
            line = f"  {nm:<12s}"
            for t in EXITS:
                res = portfolio(sub, f"{entry}_{t}")
                line += f" {t}:{res['daily']:+.2f}%({res['cum']:+.0f}%)" if res else f" {t}:  --"
            print(line)
            # OOS 拆分
            oos = [r for r in sub if r["date"] >= cut]
            isr = [r for r in sub if r["date"] < cut]
            line2 = f"  {'  └OOS':<12s}"
            for t in EXITS:
                a = portfolio(isr, f"{entry}_{t}")
                b = portfolio(oos, f"{entry}_{t}")
                if a and b:
                    line2 += f" {t}:{b['daily']:+.2f}%({a['daily']:+.2f})"
            print(line2)
            print()

    print("=" * 100)
    print("【成本影响】创业板双边 0.15%~0.30%。上表日均需扣减后再判断可执行性。")
    print("=" * 100)
    print(f"IS = {ds[0]}~{ds[int(len(ds) * 0.6) - 1]}   OOS = {cut}~{ds[-1]}")


if __name__ == "__main__":
    main()
