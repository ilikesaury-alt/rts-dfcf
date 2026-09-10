# -*- coding: utf-8 -*-
"""漏抓诊断：飙升榜大涨票 → 系统捕获漏斗分析。

漏斗：榜上出现（leaderboard_log 快照首次上榜）→ 推荐（任一类别，excluded=0）
回答：当日收盘涨幅 >= 阈值的大涨票，哪些没被推荐、首次上榜时涨幅多少。

用法: python diag_capture.py [days_back=5] [rise_threshold=7]
"""

import json
import sqlite3
import sys
from pathlib import Path

reconfigure = getattr(sys.stdout, "reconfigure", None)
if reconfigure is not None:
    reconfigure(encoding="utf-8")

DB = Path(__file__).resolve().parent / "scanner.db"
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

args = sys.argv[1:]
try:
    days_back = int(args[0]) if args else 5
    rise_thr = float(args[1]) if len(args) > 1 else 7.0
except (IndexError, ValueError):
    days_back, rise_thr = 5, 7.0

dates = sorted(
    r["date"]
    for r in conn.execute("SELECT DISTINCT date FROM leaderboard_log ORDER BY date DESC LIMIT ?", (days_back,))
)
print(f"分析窗口：{dates[0]} ~ {dates[-1]}（{len(dates)} 个交易日），当日收盘涨幅阈值 >= {rise_thr}%")

tot_big = tot_missed = 0
for dt in dates:
    print(f"\n{'=' * 84}\n【{dt}】")
    # 榜上创业板票 + 首次上榜涨幅/时刻（从 leaderboard_log 快照按时间序解析）
    first_seen: dict[str, dict] = {}
    for r in conn.execute(
        "SELECT time, symbol_snapshot FROM leaderboard_log WHERE date=? AND symbol_snapshot IS NOT NULL ORDER BY time",
        (dt,),
    ):
        try:
            snap = json.loads(r["symbol_snapshot"])
        except (ValueError, TypeError):
            continue
        if not isinstance(snap, list):
            continue
        for it in snap:
            if not isinstance(it, dict):
                continue
            sym = str(it.get("symbol") or "")
            if not sym.startswith("SZ30"):
                continue
            if sym not in first_seen and it.get("percent") is not None:
                fp_val: float | None = None
                try:
                    fp_val = float(it["percent"])
                except (TypeError, ValueError):
                    fp_val = None
                if fp_val is not None:
                    first_seen[sym] = {
                        "name": str(it.get("name") or "?"),
                        "first_pct": fp_val,
                        "first_time": r["time"],
                    }
    # 当日收盘涨幅（daily_kline）
    day_pct: dict[str, float] = {}
    for r in conn.execute("SELECT symbol, percent FROM daily_kline WHERE date=? AND symbol LIKE 'SZ30%'", (dt,)):
        try:
            if r["percent"] is not None:
                day_pct[r["symbol"]] = float(r["percent"])
        except (TypeError, ValueError):
            continue

    big = [(s, p) for s, p in day_pct.items() if p >= rise_thr]
    tot_big += len(big)
    print(f"榜上创业板 {len(first_seen)} 只 · 当日收盘 >= {rise_thr}% 的 {len(big)} 只")
    missed: list[dict] = []
    caught: list[dict] = []
    for sym, p in sorted(big, key=lambda x: -x[1]):
        info = first_seen.get(sym, {})
        rec = conn.execute(
            "SELECT COUNT(*) n, SUM(excluded) excl FROM recommendations WHERE date=? AND symbol=?",
            (dt, sym),
        ).fetchone()
        cats = [
            c["category"]
            for c in conn.execute(
                "SELECT DISTINCT category FROM recommendations WHERE date=? AND symbol=? AND excluded=0",
                (dt, sym),
            )
        ]
        fp = info.get("first_pct")
        item = {
            "symbol": sym,
            "name": info.get("name", "?"),
            "day_pct": p,
            "first_pct": fp,
            "first_time": str(info.get("first_time", "?"))[:5],
            "n_recs": rec["n"],
            "excl": rec["excl"],
            "cats": cats,
        }
        (caught if rec["n"] and rec["excl"] != rec["n"] else missed).append(item)
    tot_missed += len(missed)
    print(f"捕获（有 excluded=0 推荐）{len(caught)} 只 · 漏抓（无有效推荐）{len(missed)} 只")
    if missed:
        print("--- 漏抓明细（按当日涨幅降序）---")
        for m in missed:
            fp = f"{m['first_pct']:+.1f}%@{m['first_time']}" if m["first_pct"] is not None else "未捕获到首次"
            print(
                f"  {m['symbol']} {m['name']:<10} 收盘{m['day_pct']:+.1f}%  首次上榜 {fp:<14}"
                f" 推荐行={m['n_recs']}{'(全排除)' if m['excl'] else ''} 类别={m['cats']}"
            )
    if caught:
        print("--- 捕获明细（前 10）---")
        for c in caught[:10]:
            fp = f"{c['first_pct']:+.1f}%@{c['first_time']}" if c["first_pct"] is not None else "?"
            print(f"  {c['symbol']} {c['name']:<10} 收盘{c['day_pct']:+.1f}%  首次上榜 {fp:<14} 类别={c['cats']}")

print(
    f"\n{'=' * 84}\n汇总：大涨票 {tot_big} 只，漏抓 {tot_missed} 只（捕获率 {(1 - tot_missed / max(tot_big, 1)):.0%}）"
)
conn.close()
