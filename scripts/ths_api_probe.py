"""同花顺金融数据API 接入前探测脚本。

验证四个关键问题：
  1. 鉴权 / 连通性 / 快照延迟
  2. 行情快照字段契约（是否有换手率/量比/市值）
  3. 历史K线复权口径 vs 本地 daily_kline（雪球源）抽样对账
  4. QPS 限流行为（突发连发存活率与错误码）

用法：
  set HITHINK_FINANCE_API_KEY=xxx
  python scripts/ths_api_probe.py              # 全部四项
  python scripts/ths_api_probe.py --skip-burst # 跳过限流压测（省配额）
"""
import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

BASE = "https://fuyao.aicubes.cn"
KEY = os.environ.get("HITHINK_FINANCE_API_KEY", "")
DB_PATH = Path(__file__).resolve().parent.parent / "scanner.db"
SEP = "=" * 62


def call(path, params=None, timeout=15):
    t0 = time.perf_counter()
    try:
        r = requests.get(f"{BASE}{path}", params=params or {},
                         headers={"X-api-key": KEY}, timeout=timeout)
        return r.status_code, r.json(), (time.perf_counter() - t0) * 1000
    except Exception as e:
        return None, {"error": repr(e)}, (time.perf_counter() - t0) * 1000


# ------------------------------------------------------------- 1. 快照连通性
def probe_auth_and_snapshot():
    print(SEP)
    print("[1] 鉴权 + 行情快照连通性")
    code, body, ms = call("/api/a-share/prices/snapshot",
                          {"thscodes": "300033.SZ,300059.SZ,600519.SH"})
    if code is None:
        print(f"  网络失败: {body.get('error')}")
        return None
    biz = body.get("code")
    print(f"  HTTP {code}  biz_code={biz}  msg={body.get('message')}  耗时 {ms:.0f}ms")
    if biz != 0:
        hint = {2001: "API Key 缺失/无效，检查 HITHINK_FINANCE_API_KEY",
                4001: "频率超限"}.get(biz, json.dumps(body, ensure_ascii=False)[:300])
        print(f"  !! {hint}")
        return None
    data = body.get("data") or {}
    items = data.get("item") or []
    ts = data.get("timestamp")
    lag = f"数据时间戳滞后 {(time.time()*1000-ts)/1000:.0f}s" if ts else "timestamp=None"
    print(f"  返回 {len(items)} 条  {lag}")
    for it in items:
        print(f"    {it.get('thscode')}: last={it.get('last_price')} "
              f"pct={it.get('price_change_ratio_pct')} vol={it.get('volume')}")
    fields = sorted(items[0].keys()) if items else []
    missing = [f for f in ("turnover_rate", "volume_ratio", "market_cap", "name")
               if f not in fields]
    print(f"  字段清单: {fields}")
    print(f"  缺失字段(项目需要): {missing}")
    return items


# ------------------------------------------- 2. K线口径 vs 本地雪球源对账
def _pick_local_symbols(conn, n=4):
    """选本地 daily_kline 最新交易日有数据的 GEM 票（缓存新鲜才有对账意义）"""
    latest = conn.execute("SELECT MAX(date) FROM daily_kline").fetchone()[0]
    rows = conn.execute(
        "SELECT DISTINCT symbol FROM daily_kline WHERE date=? AND symbol "
        "LIKE 'SZ3%' LIMIT ?", (latest, n)).fetchall()
    return [r[0][2:] for r in rows], latest


def probe_kline_reconciliation(n=4):
    print(SEP)
    print("[2] 历史K线复权口径对账 (THS forward vs 本地 daily_kline)")
    if not DB_PATH.exists():
        print("  无 scanner.db，跳过")
        return
    conn = sqlite3.connect(str(DB_PATH))
    symbols, latest = _pick_local_symbols(conn, n)
    if not symbols:
        print("  本地无 GEM 缓存，跳过")
        return
    print(f"  对账样本: {symbols} (本地最新日期 {latest})")
    end_ms = int(time.time() * 1000)
    start_ms = int((datetime.now() - timedelta(days=120)).timestamp() * 1000)
    total, match = 0, 0
    worst = []
    for sym in symbols:
        tc = f"{sym}.SZ"
        code, body, _ = call("/api/a-share/prices/historical",
                             {"thscode": tc, "interval": "1d",
                              "start": start_ms, "end": end_ms, "adjust": "forward"})
        if code is None or body.get("code") != 0:
            print(f"  {sym}: 拉取失败 {str(body)[:120]}")
            continue
        ths_map = {}
        for bar in (body.get("data") or {}).get("item") or []:
            d = datetime.fromtimestamp(bar["date_ms"] / 1000).strftime("%Y-%m-%d")
            ths_map[d] = bar["close_price"]
        rows = conn.execute(
            "SELECT date, close FROM daily_kline WHERE symbol=? "
            "ORDER BY date DESC LIMIT 90", (f"SZ{sym}",)).fetchall()
        local = dict(rows)
        common = sorted(set(ths_map) & set(local), reverse=True)[:30]
        m, dv = 0, []
        for d in common:
            lc = float(local[d])
            if lc <= 0:
                continue
            dev = abs(ths_map[d] - lc) / lc
            dv.append((dev, sym, d))
            if dev <= 0.005:  # 容差 0.5%
                m += 1
        total += len(common)
        match += m
        avg = sum(x[0] for x in dv) / len(dv) * 100 if dv else 0
        mx = max(dv) if dv else (0, "-", "-")
        print(f"  {sym}: THS bars={len(ths_map)} 对比{len(common)}天 一致{m} "
              f"平均偏差{avg:.2f}% 最大{mx[0]*100:.2f}%({mx[1]} {mx[2]})")
        worst.extend(dv)
    conn.close()
    if total:
        ratio = match / total * 100
        verdict = "PASS 口径一致，可做交叉验证源" if ratio >= 95 else \
                  "WARN 存在系统性偏差，接入前需定位复权锚点差异"
        print(f"  汇总: {match}/{total} 一致 ({ratio:.1f}%) -> {verdict}")


# ------------------------------------------------------------ 3. 特色数据
def probe_special_data():
    print(SEP)
    print("[3] 特色数据可用性 (涨停池/炸板池/热榜/竞价)")
    probes = [
        ("涨停池", "/api/a-share/special-data/limit-up-pool", {}),
        ("炸板池", "/api/a-share/special-data/limit-break-pool", {}),
        ("飙升榜", "/api/a-share/special-data/skyrocket-list", {}),
        ("集合竞价", "/api/a-share/auction/snapshot", {"thscodes": "300033.SZ"}),
    ]
    for name, path, params in probes:
        code, body, ms = call(path, params)
        biz = body.get("code")
        n = len(((body.get("data") or {}).get("item")) or []) if biz == 0 else "-"
        status = "OK" if biz == 0 else f"FAIL(code={biz})"
        print(f"  {name}: {status}  条数={n}  耗时 {ms:.0f}ms")
        if biz not in (0, None) and name == "涨停池":
            print(f"      {json.dumps(body, ensure_ascii=False)[:200]}")


# ------------------------------------------------------------ 4. 限流压测
def probe_rate_limit(n=20):
    print(SEP)
    print(f"[4] QPS 限流压测 ({n} 次突发连发同一接口)")
    results = {}
    t0 = time.perf_counter()
    for _ in range(n):
        code, body, ms = call("/api/a-share/prices/snapshot",
                              {"thscodes": "300033.SZ"})
        key = body.get("code") if code is not None else "NET_ERR"
        results[key] = results.get(key, 0) + 1
    wall = time.perf_counter() - t0
    ok = results.get(0, 0)
    print(f"  总耗时 {wall:.1f}s  吞吐 {n/wall:.1f} req/s")
    print(f"  结果分布: {results}")
    if results.get(4001):
        print("  !! 触发 4001 频率超限 -> 扫描循环热路径不可用，仅低频场景可接")
    elif ok == n:
        print(f"  未触发限流（本次实测 ≥{n/wall:.0f} req/s 无压力）")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-burst", action="store_true")
    args = ap.parse_args()
    if not KEY:
        print("!! 请先设置环境变量 HITHINK_FINANCE_API_KEY")
        print("   Key 签发: https://fuyao.aicubes.cn/admin/")
        sys.exit(1)
    probe_auth_and_snapshot()
    probe_kline_reconciliation()
    probe_special_data()
    if not args.skip_burst:
        probe_rate_limit()
    print(SEP)


if __name__ == "__main__":
    main()
