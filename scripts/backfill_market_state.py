"""回填 market_state 历史（local-only 运维脚本，不进生产路径）。

redesign 路线要求「攒够 ≥40 交易日市场特征」才能校准 L0。本脚本用：
- 雪球 kline（api._request_with_retry）按历史日期抓三大指数涨跌幅（3 次请求覆盖全历史）
- 同花顺涨停池（ths_api.fetch_limit_up_pool(date_ms)）按日回填涨停/连板

用法：
  python scripts/backfill_market_state.py --start 2026-05-28 --end 2026-09-02
"""
import argparse
import datetime
import sqlite3
import sys
import time

sys.path.insert(0, ".")

import scanner.api as api  # noqa: E402
from scanner.market_state import _COLS, init_market_state  # noqa: E402
from scanner.utils import EXTERNAL_FAILURES  # noqa: E402

_XQ = {"cyb": "SZ399006", "sh": "SH000001", "sz": "SZ399001"}


def _fetch_index_history(s, sym: str, start: str, end: str) -> dict:
    """雪球 kline 抓历史日线，返回 {date: (close, pct)}。"""
    ts = int(datetime.datetime.fromisoformat(start).timestamp() * 1000)
    url = (
        f"https://stock.xueqiu.com/v5/stock/chart/kline.json"
        f"?symbol={sym}&begin={ts - 86400 * 1000 * 8}&period=day&count=200&_={int(time.time() * 1000)}"
    )
    resp = api._request_with_retry(s, url)
    items = resp.json().get("data", {}).get("item", [])
    out: dict[str, tuple[float, float]] = {}
    for it in items:
        bar_date = datetime.datetime.fromtimestamp(it[0] / 1000).strftime("%Y-%m-%d")
        if start <= bar_date <= end:
            out[bar_date] = (float(it[5]), float(it[7]))  # close, pct
    return out


def backfill(conn: sqlite3.Connection, start: str, end: str, with_limit: bool = False) -> int:
    s = api.make_session()
    series = {k: _fetch_index_history(s, v, start, end) for k, v in _XQ.items()}
    all_dates = sorted(set().union(*[set(d) for d in series.values()]))
    n = 0
    for d in all_dates:
        row = {"date": d, "fetched_at": "backfill"}
        for k in _XQ:
            if d in series[k]:
                row[f"{k}_close"], row[f"{k}_pct"] = series[k][d]
        if with_limit:
            import scanner.ths_api as ths  # lazy

            try:
                ts_ms = int(datetime.datetime.fromisoformat(d).timestamp() * 1000)
                res = ths.fetch_limit_up_pool(date_ms=ts_ms)
                if res:
                    row["limit_up"] = len(res)
                    row["limit_break"] = sum(1 for it in res.values() if it.get("zhaban"))
                    row["limit_up_prev"] = max((it.get("lianban") or 0) for it in res.values())
            except EXTERNAL_FAILURES:
                # 单日涨停池取数失败静默跳过，不阻断指数回填主流程。
                pass
            time.sleep(0.15)  # 同花顺限速
        vals = [row.get(c) for c in _COLS]
        placeholders = ",".join("?" * len(_COLS))
        conn.execute(
            f"INSERT OR REPLACE INTO market_state ({','.join(_COLS)}) VALUES ({placeholders})", vals  # noqa: S608
        )
        n += 1
    conn.commit()
    return n


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-05-28")
    ap.add_argument("--end", default="2026-09-02")
    ap.add_argument("--with-limit", action="store_true", help="同时回填涨停池（慢，约 70 次请求）")
    args = ap.parse_args()
    c = sqlite3.connect("scanner.db")
    init_market_state(c)
    n = backfill(c, args.start, args.end, args.with_limit)
    print(f"backfilled {n} days ({args.start} ~ {args.end})")
