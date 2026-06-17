"""
Backtest framework for limit_up_scanner strategy.
Replays historical data, scores stocks with configurable params, measures forward returns.

Usage:
    python backtest.py                          # Run with default params
    python backtest.py --live                   # Fetch missing K-line from API for fwd returns
    python backtest.py --optimize               # Grid search for optimal params
    python backtest.py --params custom.json     # Load params from JSON file
"""

import sqlite3
import json
import argparse
import copy
import time
import requests
from datetime import date, timedelta
from dataclasses import dataclass, field
from itertools import product

YI = 100_000_000

# ── Default parameters (mirrors current limit_up_scanner.py logic) ──

DEFAULT_PARAMS = {
    "new_face": {
        "min_score": 20,
        "today_pct": {
            "golden_min": 2.0, "golden_max": 6.0, "golden_score": 20,
            "low_score": 5,
            "high_min": 6.0, "high_max": 8.0, "high_score": 5,
            "overheat_score": -15,
        },
        "accumulated": {
            "sweet_min": -5.0, "sweet_max": 15.0, "sweet_score": 15,
            "warn_threshold": 15.0, "warn_penalty": -10,
            "danger_threshold": 25.0, "danger_penalty": -10,
        },
        "bottom": {
            "max_daily_loss": -3.0,
            "min_vol_ratio": 1.3,
            "near_low_pct": 0.05,
            "confirmed_score": 15,
            "volume_surge_score": 10,
        },
        "rank_change": {
            "strong_threshold": 2000, "strong_score": 12,
            "medium_threshold": 1000, "medium_score": 6,
        },
        "value": {
            "high_threshold": 10000, "high_score": 5,
            "medium_threshold": 5000, "medium_score": 2,
        },
        "combo": {
            "max_today_pct": 5.0, "max_accumulated": 8.0, "score": 8,
        },
    },
    "momentum": {
        "min_score": 15,
        "today_pct": {
            "golden_min": 2.0, "golden_max": 6.0, "golden_score": 26,
            "low_score": 5,
            "overheat_threshold": 8.0, "overheat_score": 0,
        },
        "accumulated": {
            "sweet_min": 10, "sweet_score": 19,
            "mid_threshold": 15, "mid_score": 10,
            "high_threshold": 20, "high_score": 5,
            "danger_threshold": 30, "danger_score": -15,
        },
        "volume": {
            "healthy_min": 0.7, "healthy_max": 2.0, "healthy_score": 5,
            "surge_min": 2.0, "surge_score": -4,
            "low_max": 0.7, "low_score": -5,
        },
        "no_crash": {
            "crash_threshold": -7, "recent_2_return": -3, "score": 13,
        },
        "rank_change": {
            "strong_threshold": 2000, "strong_score": 8,
            "medium_threshold": 1000, "medium_score": 4,
        },
        "value": {
            "high_threshold": 10000, "high_score": 5,
            "medium_threshold": 5000, "medium_score": 2,
        },
    },
    "top_n": 40,
    "lookback_days": 3,
    "max_market_cap": 500 * YI,
    "max_stock_price": 100.0,
    "sector_bonus": {3: 8, 2: 4},
    "rank_proxy": {5: 2000, 15: 1000},
}


# ── Scoring (parameterized versions of the scanner logic) ──

def score_new_face(today_pct, accumulated, vol_ratio, recent_3_pcts, stock_rank, stock_value, params):
    p = params["new_face"]
    score = 0
    tp = p["today_pct"]

    if tp["golden_min"] <= today_pct <= tp["golden_max"]:
        score += tp["golden_score"]
    elif today_pct < tp["golden_min"]:
        score += tp["low_score"]
    elif today_pct > tp["golden_max"]:
        if today_pct > tp["golden_max"] + (tp.get("overheat_threshold_delta", 2)):
            score += tp["overheat_score"]
        else:
            score += tp["high_score"]

    # accumulated
    acc = p["accumulated"]
    if acc["sweet_min"] < accumulated < acc["sweet_max"]:
        score += acc["sweet_score"]
    if accumulated >= acc["warn_threshold"]:
        score += acc["warn_penalty"]
    if accumulated >= acc["danger_threshold"]:
        score += acc["danger_penalty"]

    # bottom confirmation
    bt = p["bottom"]
    no_heavy_loss = all(pct > bt["max_daily_loss"] for pct in recent_3_pcts)
    volume_surge = vol_ratio > bt["min_vol_ratio"]
    if no_heavy_loss and volume_surge:
        score += bt["confirmed_score"]
    if volume_surge:
        score += bt["volume_surge_score"]

    # rank_change proxy
    rc = p["rank_change"]
    rc_val = _rank_to_rank_change(stock_rank, params["rank_proxy"])
    if rc_val >= rc["strong_threshold"]:
        score += rc["strong_score"]
    elif rc_val >= rc["medium_threshold"]:
        score += rc["medium_score"]

    # value
    v = p["value"]
    if stock_value >= v["high_threshold"]:
        score += v["high_score"]
    elif stock_value >= v["medium_threshold"]:
        score += v["medium_score"]

    # combo
    cb = p["combo"]
    if today_pct <= cb["max_today_pct"] and accumulated < cb["max_accumulated"]:
        score += cb["score"]

    return score


def score_momentum(today_pct, accumulated, vol_ratio, recent_5_pcts, stock_rank, stock_value, params):
    p = params["momentum"]
    score = 0

    if today_pct <= 0:
        return 0

    tp = p["today_pct"]
    if today_pct >= tp.get("overheat_threshold", 8.0):
        return 0
    if tp["golden_min"] <= today_pct <= tp["golden_max"]:
        score += tp["golden_score"]
    elif today_pct < tp["golden_min"]:
        score += tp["low_score"]

    acc = p["accumulated"]
    if accumulated < acc["sweet_min"]:
        return 0
    if accumulated >= acc["danger_threshold"]:
        score += acc["danger_score"]
    elif accumulated >= acc["high_threshold"]:
        score += acc["high_score"]
    elif accumulated >= acc["mid_threshold"]:
        score += acc["mid_score"]
    else:
        score += acc["sweet_score"]

    vol = p["volume"]
    if vol["healthy_min"] < vol_ratio < vol["healthy_max"]:
        score += vol["healthy_score"]
    elif vol_ratio >= vol["surge_min"]:
        score += vol["surge_score"]
    elif vol_ratio <= vol["low_max"]:
        score += vol["low_score"]

    no_crash = p["no_crash"]
    has_crash_day = any(pct <= no_crash["crash_threshold"] for pct in recent_5_pcts)
    recent_2_return = sum(recent_5_pcts[-2:]) if len(recent_5_pcts) >= 2 else 0
    if not has_crash_day and recent_2_return > no_crash["recent_2_return"]:
        score += no_crash["score"]

    rc = p["rank_change"]
    rc_val = _rank_to_rank_change(stock_rank, params["rank_proxy"])
    if rc_val >= rc["strong_threshold"]:
        score += rc["strong_score"]
    elif rc_val >= rc["medium_threshold"]:
        score += rc["medium_score"]

    v = p["value"]
    if stock_value >= v["high_threshold"]:
        score += v["high_score"]
    elif stock_value >= v["medium_threshold"]:
        score += v["medium_score"]

    return score


def _rank_to_rank_change(rank, proxy_map):
    for threshold_rank, rc_val in sorted(proxy_map.items()):
        if rank <= threshold_rank:
            return rc_val
    return 0


def classify_sector(name):
    m = {
        "半导体": ["半导体", "芯片", "集成电路", "封测"],
        "新能源": ["新能源", "新能", "光伏", "风电", "锂电", "电池", "氢能", "储能", "阳光"],
        "医药": ["医药", "医疗", "生物", "制药", "药", "医"],
        "电子": ["电子", "元器件"],
        "计算机": ["计算机", "软件", "信息", "数字", "数据", "智能", "AI"],
        "通信": ["通信", "5G", "物联网", "光通信", "卫星"],
        "军工": ["军工", "航天", "航空", "船舶", "国防"],
        "汽车": ["汽车", "汽配", "新能源车", "整车", "无人驾驶"],
        "机械": ["机械", "装备", "设备", "工业", "自动化", "机器人"],
        "化工": ["化工", "化学", "材料", "纤维", "塑料", "橡胶"],
        "消费": ["消费", "食品", "饮料", "白酒", "家电", "家居", "纺织"],
        "金融": ["银行", "证券", "保险", "金融", "信托"],
    }
    for sector, keywords in m.items():
        for kw in keywords:
            if kw in name:
                return sector
    return "其他"


# ── K-line fetching (from API, mirrors limit_up_scanner) ──

REQUEST_TIMEOUT = 15

def _xueqiu_session():
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
    s.get("https://xueqiu.com/hq", timeout=REQUEST_TIMEOUT)
    return s


def fetch_kline_api(session, symbol, days=25):
    now_ms = int(time.time() * 1000)
    begin_ms = now_ms - days * 86400 * 1000
    url = f"https://stock.xueqiu.com/v5/stock/chart/kline.json?symbol={symbol}&begin={begin_ms}&period=day&count={days}&_={now_ms}"
    try:
        resp = session.get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json().get("data")
        if not data:
            return None
        raw_items = data.get("item", [])
        if not raw_items:
            return None
        result = []
        for item in raw_items:
            ts = item[0]
            result.append({
                "timestamp": ts,
                "date": time.strftime("%Y-%m-%d", time.localtime(ts / 1000)),
                "open": item[2],
                "high": item[3],
                "low": item[4],
                "close": item[5],
                "volume": item[1],
                "percent": item[7],
            })
        return result
    except Exception as e:
        return None


# ── Data loading ──

@dataclass
class BacktestRec:
    date: str
    symbol: str
    name: str
    rank: int
    percent: float
    value: float
    category: str
    score: int
    entry_close: float
    fwd_1d: float | None = None
    fwd_3d: float | None = None
    fwd_5d: float | None = None


def load_data(db_path="scanner.db"):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    app_rows = conn.execute(
        "SELECT symbol, name, date, rank, percent, value FROM appearances ORDER BY date, rank"
    ).fetchall()

    kline_rows = conn.execute(
        "SELECT symbol, date, close, percent, volume FROM daily_kline ORDER BY symbol, date"
    ).fetchall()

    conn.close()

    appearances_by_date: dict[str, list[dict]] = {}
    for r in app_rows:
        d = r["date"]
        if d not in appearances_by_date:
            appearances_by_date[d] = []
        appearances_by_date[d].append(dict(r))

    kline_by_symbol: dict[str, list[dict]] = {}
    for r in kline_rows:
        sym = r["symbol"]
        if sym not in kline_by_symbol:
            kline_by_symbol[sym] = []
        kline_by_symbol[sym].append(dict(r))

    return appearances_by_date, kline_by_symbol


def get_kline_up_to(kline_list, end_date):
    return [k for k in kline_list if k["date"] <= end_date]


def ensure_kline_full(symbol, kline_by_symbol, session=None, live=False):
    """Get kline data for forward returns. Extends cache with API data if live=True."""
    kline = kline_by_symbol.get(symbol, [])
    if not kline or live:
        if session and live:
            fresh = fetch_kline_api(session, symbol)
            if fresh:
                kline = fresh
    return kline


def forward_return(kline_list, entry_date, entry_close, days):
    future = [k for k in kline_list if k["date"] > entry_date]
    target_idx = days - 1
    if target_idx < len(future):
        future_close = future[target_idx]["close"]
        return (future_close - entry_close) / entry_close
    return None


# ── Backtest engine ──

def run_backtest(params=None, db_path="scanner.db", session=None, live=False):
    if params is None:
        params = copy.deepcopy(DEFAULT_PARAMS)

    appearances_by_date, kline_by_symbol = load_data(db_path)
    sorted_dates = sorted(appearances_by_date.keys())

    all_recs: list[BacktestRec] = []

    for di, current_date in enumerate(sorted_dates):
        today_apps = appearances_by_date[current_date]

        for app in today_apps:
            stock_rank = app["rank"]
            if stock_rank > params["top_n"]:
                continue

            symbol = app["symbol"]
            name = app["name"]
            today_pct = app["percent"]
            value = app.get("value") or 0

            if symbol not in kline_by_symbol:
                continue

            full_kline = kline_by_symbol[symbol]
            kline = get_kline_up_to(full_kline, current_date)

            if len(kline) < 5:
                continue

            entry_close = kline[-1]["close"]

            if entry_close > params["max_stock_price"]:
                continue

            prev_apps = [
                a for a_date, apps in appearances_by_date.items()
                if a_date < current_date
                for a in apps if a["symbol"] == symbol
            ]
            lookback = date.fromisoformat(current_date) - timedelta(days=params["lookback_days"])
            prev_in_window = [a for a in prev_apps if date.fromisoformat(a["date"]) >= lookback]
            is_new = len(prev_in_window) == 0

            if not is_new:
                strong_prev = any(
                    a["percent"] >= 5 for a in prev_apps
                    if date.fromisoformat(a["date"]) >= lookback
                )
                if not strong_prev:
                    continue

            pcts = [k["percent"] for k in kline]
            volumes = [k["volume"] for k in kline]
            closes = [k["close"] for k in kline]

            recent_5 = pcts[-6:-1] if len(pcts) >= 6 else pcts[:-1]
            if not recent_5:
                recent_5 = [0]
            accumulated = sum(recent_5)

            vol_window = volumes[-11:-1] if len(volumes) >= 11 else volumes[:-1]
            avg_vol = sum(vol_window) / max(len(vol_window), 1)
            today_vol = volumes[-1] if volumes else 0
            vol_ratio = today_vol / avg_vol if avg_vol > 0 else 1.0

            if is_new:
                if today_pct <= 0:
                    continue
                recent_3_pcts = pcts[-3:] if len(pcts) >= 3 else pcts
                score = score_new_face(today_pct, accumulated, vol_ratio, recent_3_pcts,
                                       stock_rank, value, params)
                if score >= params["new_face"]["min_score"]:
                    rec = BacktestRec(date=current_date, symbol=symbol, name=name,
                                      rank=stock_rank, percent=today_pct, value=value,
                                      category="new_face", score=score, entry_close=entry_close)
                elif accumulated >= 10:
                    m_score = score_momentum(today_pct, accumulated, vol_ratio, recent_5_pcts,
                                             stock_rank, value, params)
                    if m_score >= params["momentum"]["min_score"]:
                        rec = BacktestRec(date=current_date, symbol=symbol, name=name,
                                          rank=stock_rank, percent=today_pct, value=value,
                                          category="momentum", score=m_score, entry_close=entry_close)
                    else:
                        continue
                else:
                    continue
            else:
                if accumulated < 10:
                    continue
                m_score = score_momentum(today_pct, accumulated, vol_ratio, recent_5_pcts,
                                         stock_rank, value, params)
                if m_score >= params["momentum"]["min_score"]:
                    rec = BacktestRec(date=current_date, symbol=symbol, name=name,
                                      rank=stock_rank, percent=today_pct, value=value,
                                      category="momentum", score=m_score, entry_close=entry_close)
                else:
                    new_score = score_new_face(today_pct, accumulated, vol_ratio, pcts[-3:],
                                               stock_rank, value, params)
                    if new_score >= params["new_face"]["min_score"]:
                        rec = BacktestRec(date=current_date, symbol=symbol, name=name,
                                          rank=stock_rank, percent=today_pct, value=value,
                                          category="known_new_face", score=new_score,
                                          entry_close=entry_close)
                    else:
                        continue

            # Forward returns (extend kline cache via API if live)
            fwd_kline = ensure_kline_full(symbol, kline_by_symbol, session, live)
            rec.fwd_1d = forward_return(fwd_kline, current_date, entry_close, 1)
            rec.fwd_3d = forward_return(fwd_kline, current_date, entry_close, 3)
            rec.fwd_5d = forward_return(fwd_kline, current_date, entry_close, 5)
            all_recs.append(rec)

    for rec in all_recs:
        sector = classify_sector(rec.name)
        if sector != "其他":
            same_sector = sum(1 for r in all_recs if r.date == rec.date and classify_sector(r.name) == sector)
            sb_map = params["sector_bonus"]
            for threshold in sorted(sb_map.keys(), reverse=True):
                if same_sector >= threshold:
                    rec.score += sb_map[threshold]
                    break

    new_recs = [r for r in all_recs if r.category in ("new_face", "known_new_face")]
    momentum_recs = [r for r in all_recs if r.category == "momentum"]

    if __name__ == "__main__":
        report(all_recs, new_recs, momentum_recs, params)
    return new_recs, momentum_recs


def _sharpe(returns):
    if len(returns) < 2 or _std(returns) == 0:
        return 0.0
    trades_per_year = 252
    return _avg(returns) / _std(returns) * (trades_per_year ** 0.5)


def _std(vals):
    if len(vals) < 2:
        return 0.0
    avg = _avg(vals)
    return (sum((v - avg) ** 2 for v in vals) / (len(vals) - 1)) ** 0.5


def _max_drawdown(returns):
    if not returns:
        return 0.0
    cum = 1.0
    peak = 1.0
    max_dd = 0.0
    for r in returns:
        cum *= (1 + r)
        peak = max(peak, cum)
        dd = (cum - peak) / peak
        max_dd = min(max_dd, dd)
    return max_dd


def _random_benchmark(all_recs_by_date, kline_by_symbol, n_trials=1000):
    """Random picks from same date pools for comparison."""
    results = {d: [] for d in all_recs_by_date}
    import random
    random.seed(42)
    for _ in range(n_trials):
        for d, pool in all_recs_by_date.items():
            if not pool:
                continue
            pick = random.choice(pool)
            symbol = pick["symbol"]
            if symbol not in kline_by_symbol:
                continue
            kline = kline_by_symbol[symbol]
            future = [k for k in kline if k["date"] > d]
            if not future:
                results[d].append(None)
                continue
            entry_close = kline[-1]["close"]
            fwd_1d = (future[0]["close"] - entry_close) / entry_close if len(future) >= 1 else None
            results[d].append(fwd_1d)
    flat = [r for day_results in results.values() for r in day_results if r is not None]
    return flat


def report(all_recs, new_recs, momentum_recs, params):
    print(f"\n{'='*60}")
    print(f"回测报告")
    print(f"{'='*60}")
    print(f"新面孔阈值: {params['new_face']['min_score']}  动量阈值: {params['momentum']['min_score']}")
    print(f"回测天数: {len(set(r.date for r in all_recs))}")
    print(f"推荐总数: {len(all_recs)} (新面孔 {len(new_recs)}, 动量 {len(momentum_recs)})")

    for label, recs in [("新面孔", new_recs), ("动量", momentum_recs)]:
        if not recs:
            print(f"\n{label}: 无推荐")
            continue
        fwd_1d = [r.fwd_1d for r in recs if r.fwd_1d is not None]
        fwd_3d = [r.fwd_3d for r in recs if r.fwd_3d is not None]
        fwd_5d = [r.fwd_5d for r in recs if r.fwd_5d is not None]
        wins_1d = sum(1 for v in fwd_1d if v > 0)
        wins_3d = sum(1 for v in fwd_3d if v > 0)
        wins_5d = sum(1 for v in fwd_5d if v > 0)

        print(f"\n{label} ({len(recs)}次推荐):")
        print(f"  +1d 胜率: {wins_1d}/{len(fwd_1d)} ({wins_1d*100//max(len(fwd_1d),1)}%) 均值: {_avg(fwd_1d):+.2%} Sharpe: {_sharpe(fwd_1d):.2f}")
        print(f"  +3d 胜率: {wins_3d}/{len(fwd_3d)} ({wins_3d*100//max(len(fwd_3d),1)}%) 均值: {_avg(fwd_3d):+.2%}")
        print(f"  +5d 胜率: {wins_5d}/{len(fwd_5d)} ({wins_5d*100//max(len(fwd_5d),1)}%) 均值: {_avg(fwd_5d):+.2%}")
        if fwd_1d:
            print(f"  +1d 最大回撤: {_max_drawdown(fwd_1d):.2%}")

        buckets = [(50, 100), (30, 49), (20, 29), (15, 19)]
        print(f"  评分分层 (+1d均收益):")
        for lo, hi in buckets:
            subset = [r for r in recs if lo <= r.score <= hi]
            if subset:
                rets = [r.fwd_1d for r in subset if r.fwd_1d is not None]
                wins = sum(1 for v in rets if v > 0)
                n = len(rets)
                print(f"    {lo}-{hi}分 ({len(subset)}只): {_avg(rets):+.2%} 胜率 {wins}/{n} ({wins*100//max(n,1)}%)" if n > 0 else f"    {lo}-{hi}分 ({len(subset)}只): N/A")

        print(f"  Top 5 (按评分):")
        for r in sorted(recs, key=lambda x: x.score, reverse=True)[:5]:
            f1 = f"{r.fwd_1d:+.2%}" if r.fwd_1d is not None else "N/A"
            f3 = f"{r.fwd_3d:+.2%}" if r.fwd_3d is not None else "N/A"
            print(f"    {r.date} {r.name} ({r.symbol}) score={r.score} rank={r.rank} +1d={f1} +3d={f3}")

    if new_recs:
        ic_1d = _ic(new_recs)
        print(f"\n  IC (评分 vs +1d收益): {ic_1d:+.3f}")


def _ic(recs):
    scored = [(r.score, r.fwd_1d) for r in recs if r.fwd_1d is not None]
    if len(scored) < 5:
        return 0.0
    scores, returns = zip(*scored)
    n = len(scores)
    avg_s = _avg(scores)
    avg_r = _avg(returns)
    num = sum((s - avg_s) * (r - avg_r) for s, r in zip(scores, returns))
    den = (sum((s - avg_s) ** 2 for s in scores) * sum((r - avg_r) ** 2 for r in returns)) ** 0.5
    return num / den if den > 0 else 0.0


def _avg(vals):
    return sum(vals) / len(vals) if vals else 0.0


# ── Grid search ──

GRID_PARAMS = {
    "new_face.min_score": [15, 20, 25],
    "new_face.today_pct.golden_min": [1.0, 2.0, 3.0],
    "new_face.today_pct.golden_max": [5.0, 6.0, 8.0],
    "new_face.accumulated.sweet_max": [10.0, 15.0, 20.0],
    "new_face.accumulated.warn_threshold": [10.0, 15.0, 20.0],
    "new_face.bottom.min_vol_ratio": [1.1, 1.3, 1.5],
}


def _set_param(params, path, value):
    keys = path.split(".")
    obj = params
    for k in keys[:-1]:
        obj = obj[k]
    obj[keys[-1]] = value


def run_grid_search(db_path="scanner.db", session=None):
    keys, values = zip(*GRID_PARAMS.items())
    results = []

    total = 1
    for v in values:
        total *= len(v)
    print(f"Grid search: {total} combinations over {len(keys)} params\n")

    import sys, os
    for i, combo in enumerate(product(*values)):
        params = copy.deepcopy(DEFAULT_PARAMS)
        for k, v in zip(keys, combo):
            _set_param(params, k, v)

        old_stdout = sys.stdout
        sys.stdout = open(os.devnull, "w")
        new_recs, old_recs = run_backtest(params, db_path)
        sys.stdout.close()
        sys.stdout = old_stdout

        all_recs = new_recs + old_recs
        total_recs = len(all_recs)
        avg_score = _avg([r.score for r in all_recs]) if all_recs else 0

        results.append({
            "params": combo,
            "total": total_recs,
            "avg_score": avg_score,
            "new_count": len(new_recs),
            "old_count": len(old_recs),
        })

        if (i+1) % 50 == 0:
            print(f"  Progress: {i+1}/{total}")

    # Sort by number of recommendations (more = more selective parameters found useful)
    results.sort(key=lambda x: -x["total"])

    print(f"\n{'='*60}")
    print(f"网格搜索结果 ({len(results)}组合) — 需配合 --live 验前向收益")
    print(f"{'='*60}")

    # Show default params first for comparison
    def _get_default(path):
        obj = DEFAULT_PARAMS
        for p in path.split("."):
            obj = obj[p]
        return obj
    default_tuple = tuple(_get_default(k) for k in keys)
    default_idx = next((i for i, r in enumerate(results)
                        if r["params"] == default_tuple), -1)
    if default_idx >= 0:
        d = results[default_idx]
        print(f"\n当前参数: 新{d['new_count']}旧{d['old_count']} avg_score={d['avg_score']:.0f}")

    print(f"\nTop 10 按推荐数:")
    for r in results[:10]:
        print(f"  total={r['total']} 新={r['new_count']}旧={r['old_count']} avg_score={r['avg_score']:.0f}")
        for k, v in zip(keys, r["params"]):
            print(f"    {k} = {v}")

    print(f"\nTop 10 按均分:")
    for r in sorted(results, key=lambda x: -x["avg_score"])[:10]:
        print(f"  avg_score={r['avg_score']:.0f} total={r['total']} 新={r['new_count']}旧={r['old_count']}")
        for k, v in zip(keys, r["params"]):
            print(f"    {k} = {v}")

    return results


# ── CLI ──

def main():
    parser = argparse.ArgumentParser(description="limit_up_scanner 回测框架")
    parser.add_argument("--optimize", "-o", action="store_true", help="网格搜索最优参数")
    parser.add_argument("--params", "-p", type=str, help="从JSON文件加载参数")
    parser.add_argument("--db", type=str, default="scanner.db", help="数据库路径")
    parser.add_argument("--live", action="store_true", help="通过API补充缺失的K线数据以计算前向收益")
    args = parser.parse_args()

    session = None
    if args.live:
        print("Connecting to Xueqiu API for live data...")
        session = _xueqiu_session()

    if args.optimize:
        run_grid_search(args.db)
    else:
        params = copy.deepcopy(DEFAULT_PARAMS)
        if args.params:
            with open(args.params, encoding="utf-8") as f:
                custom = json.load(f)
            _deep_merge(params, custom)
        run_backtest(params, args.db, session, live=args.live)
        if args.live:
            print("  Tip: 使用 --live 会调用雪球API，注意频率限制")


def _deep_merge(base, override):
    for k, v in override.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


if __name__ == "__main__":
    main()
