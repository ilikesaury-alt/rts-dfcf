import time
from datetime import datetime

import requests

from scanner.config import HEADERS, REQUEST_TIMEOUT


_last_api_call: float = 0


def _throttle(min_interval: float = 0.15):
    global _last_api_call
    elapsed = time.time() - _last_api_call
    if elapsed < min_interval:
        time.sleep(min_interval - elapsed)
    _last_api_call = time.time()


def make_session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False
    s.headers.update(HEADERS)
    s.get("https://xueqiu.com/hq", timeout=REQUEST_TIMEOUT)
    return s


def fetch_kline(session: requests.Session, symbol: str, days: int = 25) -> list[dict] | None:
    _throttle()
    now_ms = int(time.time() * 1000)
    begin_ms = now_ms - days * 86400 * 1000
    url = (
        f"https://stock.xueqiu.com/v5/stock/chart/kline.json"
        f"?symbol={symbol}&begin={begin_ms}&period=day&count={days}&_={now_ms}"
    )
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
            "date": datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d"),
            "open": item[2],
            "high": item[3],
            "low": item[4],
            "close": item[5],
            "volume": item[1],
            "percent": item[7],
        })
    return result


def fetch_biaosheng(session: requests.Session, size: int = 100) -> list[dict]:
    ts = int(time.time() * 1000)
    url = (
        f"https://stock.xueqiu.com/v5/stock/hot_stock/new_list.json"
        f"?page=1&size={size}&order=desc&order_by=rank_change&type=10&_={ts}"
    )
    resp = session.get(url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json().get("data", {}).get("items", [])


_market_cap_cache: dict[str, dict] = {}
_market_cap_cache_time: float = 0


def fetch_market_caps_batch(session: requests.Session, symbols: list[str]) -> dict[str, dict]:
    global _market_cap_cache, _market_cap_cache_time

    now = time.time()
    if _market_cap_cache and now - _market_cap_cache_time < 300:
        return _market_cap_cache

    if not symbols:
        return {}

    result: dict[str, dict] = {}

    for i in range(0, len(symbols), 50):
        batch = symbols[i:i + 50]
        sym_str = ",".join(batch)
        url = (f"https://stock.xueqiu.com/v5/stock/batch/quote.json"
               f"?symbol={sym_str}")
        try:
            resp = session.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            items = resp.json().get("data", {}).get("items", [])
            for item in items:
                q = item.get("quote") if isinstance(item, dict) else {}
                if not q or not q.get("symbol"):
                    q = item if isinstance(item, dict) else {}
                sym = q.get("symbol", "")
                if sym:
                    mc = q.get("market_capital") or 0
                    cmc = q.get("float_market_capital") or 0
                    result[sym] = {"market_cap": mc, "circ_market_cap": cmc}
        except Exception:
            continue

    if result:
        _market_cap_cache = result
        _market_cap_cache_time = now
    elif not _market_cap_cache:
        print(f"\n  [!] 警告: 市值数据获取失败，小而美规则暂不生效")

    return result or _market_cap_cache


_INTRADAY_CACHE: dict[str, tuple[float | None, float]] = {}
_INTRADAY_CACHE_TTL = 300
_INTRADAY_CACHE_FAIL_TTL = 60

_MINUTE_DATA_CACHE: dict[str, tuple[list[dict] | None, float]] = {}
_MINUTE_DATA_CACHE_TTL = 120


def _fetch_minute_data(session: requests.Session, symbol: str) -> list[dict] | None:
    now = time.time()
    if symbol in _MINUTE_DATA_CACHE:
        data, ts = _MINUTE_DATA_CACHE[symbol]
        if now - ts < _MINUTE_DATA_CACHE_TTL:
            return data
    try:
        _throttle()
        ts_ms = int(time.time() * 1000)
        url = f"https://stock.xueqiu.com/v5/stock/chart/minute.json?symbol={symbol}&period=1d&_={ts_ms}"
        resp = session.get(url, timeout=15)
        d = resp.json()
        items = d.get("data", {}).get("items", [])
        if items and len(items) >= 10:
            _MINUTE_DATA_CACHE[symbol] = (items, now)
            return items
        _MINUTE_DATA_CACHE[symbol] = (None, now)
        return None
    except Exception:
        _MINUTE_DATA_CACHE[symbol] = (None, now)
        return None


def estimate_live_volume(session: requests.Session, symbol: str) -> float | None:
    items = _fetch_minute_data(session, symbol)
    if not items:
        return None
    total_vol = sum(item.get("volume", 0) for item in items)
    minutes_elapsed = len(items)
    trading_minutes_total = 240
    estimated = total_vol * trading_minutes_total / max(minutes_elapsed, 1)
    return estimated


def analyze_intraday(session: requests.Session, symbol: str) -> float | None:
    now = time.time()
    if symbol in _INTRADAY_CACHE:
        val, ts = _INTRADAY_CACHE[symbol]
        if val is not None and now - ts < _INTRADAY_CACHE_TTL:
            return val
        if val is None and now - ts < _INTRADAY_CACHE_FAIL_TTL:
            return None

    items = _fetch_minute_data(session, symbol)
    if not items:
        _INTRADAY_CACHE[symbol] = (None, now)
        return None

    try:
        first_px = items[0]["current"]
        last_px = items[-1]["current"]
        prices = [item["current"] for item in items]
        high = max(prices)
        low = min(prices)
        score = 0.0

        if high > low and high > first_px * 1.005:
            fade_pct = (high - last_px) / high * 100
            if fade_pct > 3:
                score -= 5.0
            elif fade_pct > 1.5:
                score -= 3.0
            elif fade_pct > 0.5:
                score -= 1.0

        if high > low:
            position = (last_px - low) / (high - low)
            if position > 0.7:
                score += 2.5
            elif position < 0.3:
                score -= 2.5

        segments = 10
        seg_size = len(items) // segments
        if seg_size > 0:
            seg_prices = [items[min(i * seg_size, len(items) - 1)]["current"] for i in range(segments + 1)]
            seg_changes = [(seg_prices[i + 1] - seg_prices[i]) / seg_prices[i] * 100 for i in range(segments)]
            attack_waves = sum(1 for c in seg_changes if c > 0.2)
            decline_waves = sum(1 for c in seg_changes if c < -0.2)
            net_waves = attack_waves - decline_waves
            if net_waves >= 3:
                score += 3.0
            elif net_waves >= 1:
                score += 1.0
            elif net_waves <= -3:
                score -= 3.0
            elif net_waves <= -1:
                score -= 1.0

        capital = items[-1].get("capital", {})
        xlarge = capital.get("xlarge", 0) if capital else 0
        if xlarge > 5:
            score += 2.0
        elif xlarge < -5:
            score -= 2.0

        split = len(items) // 3
        morning_end = items[split]["current"]
        morning_chg = (morning_end - first_px) / first_px * 100
        total_chg = (last_px - first_px) / first_px * 100
        if total_chg > 0 and morning_chg > total_chg * 0.5:
            score += 1.5
        elif total_chg > 0 and morning_chg < total_chg * 0.3:
            score -= 1.5
        elif total_chg < 0 and morning_chg < total_chg * 1.2:
            score -= 1.0

        score = max(-10.0, min(10.0, score))
        _INTRADAY_CACHE[symbol] = (score, now)
        return score
    except Exception:
        _INTRADAY_CACHE[symbol] = (None, now)
        return None
