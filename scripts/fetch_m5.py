"""抓取 5 分钟 K 线并缓存（本地分析用，不进生产路径）。

用途：回答「当日推荐时刻买入、次日 10:00 卖出」这类盘中口径的收益问题。
scanner.db 里只有日线，没有分钟数据，必须外部补。

数据源：东方财富 push2his klt=5（5 分钟 K 线）。
注意：该接口单次最多返回约 1500 根 bar ≈ 31 个交易日，
无法覆盖 recommendations 全历史（2026-05-28 起），只能回溯到 ~2026-07-22。

用法: python scripts/fetch_m5.py [--workers 8] [--refresh]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import ssl
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "scanner.db"
CACHE = ROOT / "scripts" / ".cache_m5.sqlite3"

BEG = "20260701"
END = "20260930"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

ssl._create_default_https_context = ssl._create_unverified_context


def secid(symbol: str) -> str:
    """把库里的 symbol 归一成东财 secid。

    scanner.db 里 symbol 形如 ``SZ300001`` / ``300001`` / ``SH600000``，
    东财只认 ``0.300001``（深）/ ``1.600000``（沪），必须剥掉交易所前缀。
    """
    s = (symbol or "").strip().upper()
    code = "".join(ch for ch in s if ch.isdigit())
    if not code:
        return s
    if code.startswith(("6", "9")):
        return "1." + code
    return "0." + code


def fetch_one(symbol: str) -> tuple[str, list[tuple], str]:
    """返回 (symbol, [(date, time, close, open, high, low)], err)"""
    url = (
        "https://push2his.eastmoney.com/api/qt/stock/kline/get?"
        f"secid={secid(symbol)}&ut=fa5fd1943c7b386f172d6893dbfba10b"
        "&fields1=f1,f2,f3,f4,f5,f6"
        "&fields2=f51,f52,f53,f54,f55,f56,f57,f58"
        f"&klt=5&fqt=1&beg={BEG}&end={END}&lmt=100000"
    )
    last_err = ""
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=20) as resp:
                d = json.loads(resp.read())
            ks = (d.get("data") or {}).get("klines") or []
            out = []
            for k in ks:
                p = k.split(",")
                if len(p) < 4:
                    continue
                dt = p[0].split(" ")
                if len(dt) != 2:
                    continue
                try:
                    out.append((dt[0], dt[1], float(p[2]), float(p[1]),
                                float(p[3]), float(p[4])))
                except ValueError:
                    continue
            return symbol, out, ""
        except Exception as e:  # noqa: BLE001 - 网络抓取，收窄成本高，统一重试
            last_err = f"{type(e).__name__}: {e}"
            time.sleep(0.8 * (attempt + 1))
    return symbol, [], last_err


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--refresh", action="store_true", help="忽略缓存重新抓")
    ap.add_argument("--since", default="2026-07-22")
    args = ap.parse_args()

    conn = sqlite3.connect(DB)
    syms = [r[0] for r in conn.execute(
        "SELECT DISTINCT symbol FROM recommendations "
        "WHERE symbol IS NOT NULL AND symbol != '' AND date >= ? ORDER BY symbol",
        (args.since,))]
    conn.close()
    print(f"目标 {len(syms)} 只股票（date >= {args.since}）", flush=True)

    cache = sqlite3.connect(CACHE)
    cache.execute("""
        CREATE TABLE IF NOT EXISTS m5 (
            symbol TEXT, date TEXT, time TEXT,
            close REAL, open REAL, high REAL, low REAL,
            PRIMARY KEY (symbol, date, time)
        )""")
    cache.execute("CREATE TABLE IF NOT EXISTS m5_fail (symbol TEXT PRIMARY KEY, err TEXT)")
    if args.refresh:
        cache.execute("DELETE FROM m5")
        cache.execute("DELETE FROM m5_fail")
    done = {r[0] for r in cache.execute("SELECT DISTINCT symbol FROM m5")}
    failed = {r[0] for r in cache.execute("SELECT symbol FROM m5_fail")}
    todo = [s for s in syms if s not in done and s not in failed]
    print(f"缓存已覆盖 {len(done)} 只，失败 {len(failed)} 只，待抓 {len(todo)} 只", flush=True)
    if not todo:
        print("无需抓取。")
        cache.close()
        return

    ok = err = bars = 0
    lock_ok = [0]
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(fetch_one, s): s for s in todo}
        for i, f in enumerate(as_completed(futs), 1):
            sym, rows, e = f.result()
            if rows:
                cache.executemany(
                    "INSERT OR REPLACE INTO m5 VALUES (?,?,?,?,?,?,?)",
                    [(sym, d, t, c, o, h, l) for (d, t, c, o, h, l) in rows])
                ok += 1
                bars += len(rows)
            else:
                cache.execute("INSERT OR REPLACE INTO m5_fail VALUES (?,?)", (sym, e))
                err += 1
            if i % 40 == 0:
                cache.commit()
                print(f"  {i}/{len(todo)}  成功{ok} 失败{err} bars={bars}", flush=True)
    cache.commit()
    cache.close()
    print(f"完成：成功 {ok}，失败 {err}，共 {bars} 根 5 分钟 bar")
    if err:
        print(f"失败样本（前10）：")
        for r in sqlite3.connect(CACHE).execute("SELECT * FROM m5_fail LIMIT 10"):
            print("   ", r)


if __name__ == "__main__":
    sys.exit(main())
