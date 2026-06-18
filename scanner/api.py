import threading
import time
from datetime import datetime

import requests

from scanner.config import HEADERS, REQUEST_TIMEOUT


def _request_with_retry(session: requests.Session, url: str,
                        max_retries: int = 3, base_delay: float = 1.0,
                        timeout: int | None = None) -> requests.Response:
    last_exc = None
    for attempt in range(max_retries):
        try:
            _throttle()
            resp = session.get(url, timeout=timeout or REQUEST_TIMEOUT)
            resp.raise_for_status()
            return resp
        except requests.RequestException as e:
            last_exc = e
            if attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                print(f"  [重试] {url[:60]}... 第{attempt+1}次失败: {e}, {delay:.0f}s后重试")
                time.sleep(delay)
    raise last_exc  # type: ignore


_last_api_call: float = 0
_throttle_lock = threading.Lock()


def _throttle(min_interval: float = 0.15):
    global _last_api_call
    with _throttle_lock:
        elapsed = time.time() - _last_api_call
        if elapsed < min_interval:
            time.sleep(min_interval - elapsed)
        _last_api_call = time.time()


_market_index_cache: tuple[float | None, float] = (None, 0)


def fetch_market_index(session: requests.Session) -> float | None:
    global _market_index_cache
    now = time.time()
    if _market_index_cache[0] is not None and now - _market_index_cache[1] < 180:
        return _market_index_cache[0]
    try:
        ts_ms = int(time.time() * 1000)
        url = f"https://stock.xueqiu.com/v5/stock/chart/kline.json?symbol=SZ399006&begin={ts_ms - 86400*1000*3}&period=day&count=2&_={ts_ms}"
        resp = _request_with_retry(session, url)
        items = resp.json().get("data", {}).get("item", [])
        if items:
            pct = items[-1][7]
            _market_index_cache = (pct, now)
            return pct
    except Exception as e:
        print(f"  [!] 获取大盘指数失败: {e}")
    return None


def make_session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False
    s.headers.update(HEADERS)
    s.get("https://xueqiu.com/hq", timeout=REQUEST_TIMEOUT)
    return s


def fetch_kline(session: requests.Session, symbol: str, days: int = 15) -> list[dict] | None:
    now_ms = int(time.time() * 1000)
    begin_ms = now_ms - days * 86400 * 1000
    url = (
        f"https://stock.xueqiu.com/v5/stock/chart/kline.json"
        f"?symbol={symbol}&begin={begin_ms}&period=day&count={days}&_={now_ms}"
    )
    try:
        resp = _request_with_retry(session, url)
    except Exception as e:
        print(f"  [!] K线获取失败 {symbol}: {e}")
        return None
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
    try:
        resp = _request_with_retry(session, url)
        return resp.json().get("data", {}).get("items", [])
    except Exception as e:
        print(f"  [!] 飙升榜获取失败: {e}")
        return []


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
            resp = _request_with_retry(session, url)
            items = resp.json().get("data", {}).get("items", [])
            for item in items:
                q = item.get("quote") if isinstance(item, dict) else {}
                if not q or not q.get("symbol"):
                    q = item if isinstance(item, dict) else {}
                sym = q.get("symbol", "")
                if sym:
                    mc = q.get("market_capital") or 0
                    cmc = q.get("float_market_capital") or 0
                    turnover = q.get("turnover_rate")
                    result[sym] = {"market_cap": mc, "circ_market_cap": cmc,
                                   "turnover_rate": turnover,
                                   "current": q.get("current", 0),
                                   "percent": q.get("percent", 0)}
        except Exception as e:
            print(f"  [!] 市值批量查询失败(批次{i // 50 + 1}): {e}")
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
        ts_ms = int(time.time() * 1000)
        url = f"https://stock.xueqiu.com/v5/stock/chart/minute.json?symbol={symbol}&period=1d&_={ts_ms}"
        resp = _request_with_retry(session, url)
        d = resp.json()
        items = d.get("data", {}).get("items", [])
        if items and len(items) >= 10:
            _MINUTE_DATA_CACHE[symbol] = (items, now)
            return items
        _MINUTE_DATA_CACHE[symbol] = (None, now)
        return None
    except Exception as e:
        print(f"  [!] 获取分时数据失败 {symbol}: {e}")
        _MINUTE_DATA_CACHE[symbol] = (None, now)
        return None


def analyze_opening_strength(session: requests.Session, symbol: str) -> float | None:
    """开盘强度因子: 分析前5分钟(9:30-9:35)的量价行为."""
    items = _fetch_minute_data(session, symbol)
    if not items or len(items) < 6:
        return None

    first_5 = items[:5]
    open_px = first_5[0]["current"]
    five_min_px = first_5[-1]["current"]

    opening_chg = (five_min_px - open_px) / open_px * 100 if open_px > 0 else 0
    opening_vol = sum(item.get("volume", 0) for item in first_5)
    avg_vol_per_min = sum(item.get("volume", 0) for item in items) / max(len(items), 1)
    vol_ratio = opening_vol / (avg_vol_per_min * 5) if avg_vol_per_min > 0 else 1.0

    score = 0.0
    if opening_chg > 2:
        score += 4.0
    elif opening_chg > 1:
        score += 3.0
    elif opening_chg > 0.5:
        score += 2.0
    elif opening_chg < -2:
        score -= 4.0
    elif opening_chg < -1:
        score -= 3.0
    elif opening_chg < -0.5:
        score -= 2.0

    if vol_ratio > 2.0:
        score += 2.0
    elif vol_ratio > 1.5:
        score += 1.0
    elif vol_ratio < 0.5:
        score -= 1.0

    return max(-5.0, min(5.0, score))


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
    if not items or len(items) < 2:
        _INTRADAY_CACHE[symbol] = (None, now)
        return None

    try:
        prices = [item["current"] for item in items]
        high = max(prices)
        low = min(prices)
        first_px = prices[0]
        last_px = prices[-1]

        def _score_period(period_items: list[dict], period_weight: float) -> float:
            if len(period_items) < 2:
                return 0.0
            pxs = [p["current"] for p in period_items]
            p_high = max(pxs)
            p_low = min(pxs)
            p_first = pxs[0]
            p_last = pxs[-1]
            p_score = 0.0

            if p_high > p_low and p_high > p_first * 1.005:
                fade = (p_high - p_last) / p_high * 100
                if fade > 3:
                    p_score -= 5.0
                elif fade > 1.5:
                    p_score -= 3.0
                elif fade > 0.5:
                    p_score -= 1.0

            position = (p_last - p_low) / (p_high - p_low) if p_high > p_low else 0.5
            if position > 0.7:
                p_score += 2.5
            elif position < 0.3:
                p_score -= 2.5

            n = len(period_items)
            segs = min(5, n)
            seg_sz = n // segs
            if seg_sz > 0:
                seg_chgs = []
                for i in range(segs):
                    idx = min(i * seg_sz, n - 1)
                    nxt = min((i + 1) * seg_sz, n - 1)
                    if idx != nxt:
                        c = (period_items[nxt]["current"] - period_items[idx]["current"]) / period_items[idx]["current"] * 100
                        seg_chgs.append(c)
                attacks = sum(1 for c in seg_chgs if c > 0.15)
                declines = sum(1 for c in seg_chgs if c < -0.15)
                net = attacks - declines
                if net >= 2:
                    p_score += 2.0
                elif net <= -2:
                    p_score -= 2.0

            chg = (p_last - p_first) / p_first * 100 if p_first > 0 else 0
            return p_score * period_weight

        total_items = len(items)
        early_end = min(total_items, 60)
        mid_end = min(total_items, 180)

        early_items = items[:early_end]
        mid_items = items[early_end:mid_end]
        late_items = items[mid_end:]

        early_score = _score_period(early_items, 0.4)
        mid_score = _score_period(mid_items, 0.3)
        late_score = _score_period(late_items, 0.3)

        score = early_score + mid_score + late_score

        capital = items[-1].get("capital", {})
        xlarge = capital.get("xlarge", 0) if capital else 0
        if xlarge > 5:
            score += 2.0
        elif xlarge < -5:
            score -= 2.0

        score = max(-10.0, min(10.0, score))
        _INTRADAY_CACHE[symbol] = (score, now)
        return score
    except Exception as e:
        print(f"  [!] 分析分时强度失败 {symbol}: {e}")
        _INTRADAY_CACHE[symbol] = (None, now)
        return None
