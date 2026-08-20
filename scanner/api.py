import logging
import threading
import time
from datetime import datetime

import requests

from scanner.config import (
    BEIJING_TZ,
    HEADERS,
    REQUEST_CONNECT_TIMEOUT,
    REQUEST_TIMEOUT,
    SENTIMENT_AVG_TOP10_BOILING,
    SENTIMENT_AVG_TOP10_COOL,
    SENTIMENT_AVG_TOP10_WARM,
    SENTIMENT_BOILING,
    SENTIMENT_COOL,
    SENTIMENT_FROZEN,
    SENTIMENT_PCT_GT5_BOILING,
    SENTIMENT_PCT_GT5_COOL,
    SENTIMENT_PCT_GT5_WARM,
    SENTIMENT_WARM,
)
from scanner.models import KlineBar, make_kline_bar
from scanner.utils import cache_put as _cache_put
from scanner.utils import to_float as _num

logger = logging.getLogger(__name__)


# ── 雪球 cookie 失效自愈（2026-08-12）──
# 长驻扫描进程的 session 在启动时经 make_session() 建立一次，运行中永不刷新。
# cookie 失效后所有 API 请求返回 401/403 或被重定向到 passport 登录页，K 线补拉
# 静默失败（上层拿 None 回退旧缓存），四策略基于数日前旧数据评分 → 列表饿死，
# 直到手动重启进程才恢复（实测 8-12 榜上 24 只仅 7 只拉到当日 bar，新 session 4.4s 拉全）。
# 在统一请求层检测失效信号并原地重建 cookie 重试一次，所有调用方
# （kline/分时/榜单/市值/指数）自动受益，无需各自处理。
_session_refresh_lock = threading.Lock()


def _is_session_expired(resp: requests.Response) -> bool:
    """session 失效信号：401/403、400(token 过期)、被重定向到 passport 登录、
    200 却返回 HTML 登录页。

    2026-08-19 实测补 400/400016：cookie 失效/被清空后，雪球所有 v5 接口
    （batch/quote、kline、biaosheng、minute）统一返回
    HTTP 400 + {"error_code":"400016","error_description":"遇到错误，请刷新页面或者重新登录帐号后再试"}，
    而不是 401/403。此前只识别 401/403 → 自愈不触发 → 全批 400 重试失败 →
    市值批量查询返回空（"市值数据获取失败，小而美规则暂不生效"），K 线/榜单
    同链路静默失败。
    """
    if resp.status_code in (401, 403):
        return True
    if resp.status_code == 400:
        # token 过期信号（400016）—— 只对真正的失效签名触发重建，普通 400
        # 业务错误（参数错等）不误伤。
        try:
            body = resp.json()
        except (ValueError, AttributeError):
            return False
        if isinstance(body, dict) and str(body.get("error_code")) == "400016":
            return True
        return False
    resp_url = getattr(resp, "url", "") or ""
    if "passport" in resp_url:
        return True
    ct = (resp.headers.get("Content-Type") or "") if hasattr(resp, "headers") else ""
    if resp.status_code == 200 and "text/html" in ct.lower():
        # 正常雪球 API 返回 JSON；HTML = 被重定向到登录页
        return True
    return False


def _refresh_session(session: requests.Session) -> None:
    """原地重建雪球 cookie：清空旧 cookie 后重新 GET /hq 握手。

    调用方持有的 session 引用不变（thread-local / adapter 单例均无感）。
    并发重建用锁串行，避免 _parallel_fetch 多线程同时清 cookie 竞态。
    """
    with _session_refresh_lock:
        session.cookies.clear()
        session.get("https://xueqiu.com/hq", timeout=REQUEST_TIMEOUT)


def _request_with_retry(session: requests.Session, url: str,
                        max_retries: int = 3, base_delay: float = 1.0,
                        timeout: int | tuple | None = None) -> requests.Response:
    last_exc = None
    rebuilt = False
    # 2026-08-20 修复：session 重建后必须再给一次真正用上新 cookie 的尝试。
    # 原 range(max_retries)：若最后一次尝试（attempt=max_retries-1）才检测到 400016 失效，
    # refresh 成功后 continue 出循环 → raise 的是前几次的陈旧 Timeout（且 refresh 白做）。
    # 现多留 1 个配额给「已重建」场景；未重建则第 max_retries 次直接 break 保持原语义。
    for attempt in range(max_retries + 1):
        if attempt == max_retries and not rebuilt:
            break
        try:
            _throttle()
            # (connect, read) 双段超时：connect 短超时避免连不上挂死拖垮整轮扫描
            resp = session.get(url, timeout=timeout or (REQUEST_CONNECT_TIMEOUT, REQUEST_TIMEOUT))

            # session 失效自愈：cookie 失效（401/403/登录页）时重建后重试一次
            if not rebuilt and _is_session_expired(resp):
                rebuilt = True
                logger.warning("  雪球 session 疑似失效(HTTP %s)，重建 cookie 后重试 %s",
                               resp.status_code, url[:60])
                try:
                    _refresh_session(session)
                except Exception as e:
                    logger.warning("  雪球 session 重建失败，按原逻辑继续: %s", e)
                time.sleep(1)
                continue

            if resp.status_code == 429:
                try:
                    retry_after = int(resp.headers.get("Retry-After", "5"))
                except (ValueError, TypeError):
                    retry_after = 5
                wait = min(retry_after, 30)
                if attempt < max_retries - 1:
                    logger.warning("  限流(429) %s, 等待%d秒后重试", url[:60], wait)
                    time.sleep(wait)
                    continue
                resp.raise_for_status()

            if resp.status_code >= 500 and attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                logger.warning("  服务端错误(%d) %s, %.0f秒后重试", resp.status_code, url[:60], delay)
                time.sleep(delay)
                continue

            resp.raise_for_status()
            return resp

        except requests.Timeout as e:
            last_exc = e
            if attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                logger.warning("  超时 %s, %.0f秒后重试", url[:60], delay)
                time.sleep(delay)
        except requests.RequestException as e:
            last_exc = e
            if attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                logger.warning("  请求失败 %s, %.0f秒后重试: %s", url[:60], delay, e)
                time.sleep(delay)
    raise last_exc  # type: ignore


_last_api_call: float = 0
_throttle_lock = threading.Lock()


def _throttle(min_interval: float = 0.15):
    global _last_api_call
    # 锁内仅计算等待时间并预占槽位，锁外睡眠——多线程可并发预约再并发等待，
    # 速率限制不变但不再串行化 ThreadPoolExecutor 工作线程。
    with _throttle_lock:
        now = time.time()
        wait = max(0, min_interval - (now - _last_api_call))
        _last_api_call = now + wait
    if wait > 0:
        time.sleep(wait)


_cache_lock = threading.Lock()
_index_cache_lock = threading.Lock()
_kline_cache_lock = threading.Lock()
_intraday_cache_lock = threading.Lock()
_minute_data_cache_lock = threading.Lock()

_market_index_cache: tuple[float | None, str | None, float] = (None, None, 0)

_kline_cache: dict[str, tuple[list[KlineBar] | None, float]] = {}
# TTL=0 禁用 API 层缓存：避免与 orchestrator.KLINE_REFRESH_TTL 双层叠加导致 K 线陈旧。
# K 线新鲜度统一由 orchestrator 层 TTL 控制，补拉时真正发请求而非命中旧缓存。
_kline_cache_ttl = 0


def _bar_date_of(item: list) -> str | None:
    """kline item 首列时间戳 → 'YYYY-MM-DD'；脏值/缺列返回 None。"""
    try:
        ts = item[0]
        if isinstance(ts, (int, float)) and ts > 0:
            return datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001
        pass
    return None


def _warn_stale_index_bar(bar_date: str | None) -> None:
    """交易时段内读到非当日 bar → 告警（大盘标签可能按昨日涨幅失真）。

    2026-08-19 曾因 kline 接口 begin/count 语义错位，把当日 -6.26% 崩盘读成
    昨日 -0.93%（展示"大盘中性"）。count 修复后正常必取当日 bar，这里兜住
    "接口本身滞后"的残留场景——fail-loud 不静默。
    """
    if bar_date is None:
        return
    try:
        from scanner.trading_session import is_trading_day  # 惰性导入避免低层依赖上移

        now = datetime.now(BEIJING_TZ)
        if now.strftime("%H:%M") < "09:30":
            return  # 开盘前今日 bar 尚未生成，读到昨日属正常
        today = now.date().isoformat()
        if bar_date < today and is_trading_day(now.date()):
            print(f"  [!] 大盘指数读到旧 bar (date={bar_date}, 今日={today})——大盘标签可能失真")
    except Exception:  # noqa: BLE001  交易日历异常不阻塞指数取数
        pass


def get_market_index_meta() -> tuple[float | None, str | None]:
    """返回最近一次大盘指数取数的 (涨幅, bar 日期)，供落库审计。

    bar 日期是「读到的是哪一天的数据」的权威证据——2026-08-19 曾把当日 -6.26%
    读成昨日 -0.93%，单看涨幅无法分辨（两者都是合法 float），bar 日期直接暴露隔日错位。
    """
    with _index_cache_lock:
        return _market_index_cache[0], _market_index_cache[1]


def fetch_market_index(session: requests.Session) -> float | None:
    global _market_index_cache
    now = time.time()
    with _index_cache_lock:
        if _market_index_cache[0] is not None and now - _market_index_cache[2] < 60:
            return _market_index_cache[0]
    try:
        ts_ms = int(time.time() * 1000)
        # count 必须 ≥ 窗口内可能出现的最大 bar 数（3 天窗口最多 3 根交易日 bar）。
        # 雪球 kline 接口按 begin 返回**窗口内前 count 根**（非最近 count 根）：
        # begin=now-3d & count=2 只会拿到最旧两根 → items[-1] 错取昨日涨幅，
        # 当日崩盘/大涨全部失真（2026-08-19 实测创业板指 -6.26% 被读成昨日 -0.93%）。
        url = (f"https://stock.xueqiu.com/v5/stock/chart/kline.json"
               f"?symbol=SZ399006&begin={ts_ms - 86400*1000*3}&period=day&count=5&_={ts_ms}")
        resp = _request_with_retry(session, url)
        items = resp.json().get("data", {}).get("item", [])
        if items and len(items[-1]) > 7:
            pct_raw = items[-1][7]
            bar_date = _bar_date_of(items[-1])
            if pct_raw is not None:
                # 类型防御（与 _num 同族）：大盘涨幅列偶发为字符串/NaN/inf 时统一强转，
                # 否则下游 enhancer._record_dimensions 的 `market_idx_pct > MARKET_STRONG_THRESHOLD`
                # 对字符串抛 TypeError，整轮扫描异常丢失。强转后 0.0 语义=中性，无加分。
                pct = _num(pct_raw)
                _warn_stale_index_bar(bar_date)
                with _index_cache_lock:
                    _market_index_cache = (pct, bar_date, now)
                return pct
    except Exception as e:
        print(f"  [!] 获取大盘指数失败: {e}")
    return None


def compute_surge_sentiment(raw_items: list[dict]) -> dict:
    top10 = raw_items[:10]

    top10_pcts = [_num(item.get("percent")) for item in top10]
    avg_top10 = sum(top10_pcts) / len(top10_pcts) if top10_pcts else 0

    all_pcts = [_num(item.get("percent")) for item in raw_items]
    pct_gt_5 = sum(1 for p in all_pcts if p > 5)
    pct_lt_0 = sum(1 for p in all_pcts if p < 0)
    total = len(all_pcts) or 1

    rank_changes = [abs(_num(item.get("rank_change"))) for item in raw_items]
    avg_rank_churn = sum(rank_changes) / len(rank_changes) if rank_changes else 0

    pct_gt_5_ratio = pct_gt_5 / total

    if avg_top10 > SENTIMENT_AVG_TOP10_BOILING or pct_gt_5_ratio > SENTIMENT_PCT_GT5_BOILING:
        phase = "boiling"
        bonus = SENTIMENT_BOILING
    elif avg_top10 > SENTIMENT_AVG_TOP10_WARM or pct_gt_5_ratio > SENTIMENT_PCT_GT5_WARM:
        phase = "warm"
        bonus = SENTIMENT_WARM
    elif avg_top10 < SENTIMENT_AVG_TOP10_COOL and pct_gt_5_ratio < SENTIMENT_PCT_GT5_COOL and pct_lt_0 > total * 0.5:
        phase = "frozen"
        bonus = SENTIMENT_FROZEN
    elif avg_top10 < SENTIMENT_AVG_TOP10_COOL and pct_gt_5_ratio < SENTIMENT_PCT_GT5_COOL:
        phase = "cool"
        bonus = SENTIMENT_COOL
    else:
        phase = "neutral"
        bonus = 0

    return {
        "phase": phase,
        "bonus": bonus,
        "avg_top10_pct": round(avg_top10, 2),
        "pct_gt_5_ratio": round(pct_gt_5_ratio, 3),
        "pct_lt_0_count": pct_lt_0,
        "avg_rank_churn": round(avg_rank_churn, 0),
    }


def make_session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False
    s.headers.update(HEADERS)
    s.get("https://xueqiu.com/hq", timeout=REQUEST_TIMEOUT)
    return s


def fetch_kline(session: requests.Session, symbol: str, days: int = 15) -> list[KlineBar] | None:
    now = time.time()

    with _kline_cache_lock:
        cached = _kline_cache.get(symbol)
        if cached:
            data, ts = cached
            if data is not None and now - ts < _kline_cache_ttl:
                return data

    now_ms = int(now * 1000)
    begin_ms = now_ms - days * 86400 * 1000
    url = (
        f"https://stock.xueqiu.com/v5/stock/chart/kline.json"
        f"?symbol={symbol}&begin={begin_ms}&period=day&count={days}&type=qfq&_={now_ms}"
    )
    try:
        resp = _request_with_retry(session, url)
    except Exception as e:
        logger.warning("K线获取失败 %s: %s", symbol, e)
        if cached and cached[0] is not None:
            logger.info("  返回缓存K线(%s, %.0fs前)", symbol, now - cached[1])
            return cached[0]
        return None
    data = resp.json().get("data")
    if not data:
        return None
    raw_items = data.get("item", [])
    if not raw_items:
        return None
    result = []
    for item in raw_items:
        # 短数组防护：API 偶发返回缺列 item 时跳过该根（与 _normalize_minute_item 同族），
        # 避免 IndexError 整批丢失。正常 kline 列序:
        # [timestamp, volume, open, high, low, close, chg, percent, turnoverrate, ...]
        if not isinstance(item, (list, tuple)) or len(item) < 8:
            continue
        ts = item[0]
        # 时间戳强转防御（与 _num 同族，数据入口单点）：API 偶发返回 None/字符串/
        # 非数字时 fromtimestamp(ts/1000) 抛 TypeError 会拖垮整只票的 K 线解析；
        # ts<=0（epoch 前）无法映射为有效交易日，同样跳过该根，避免产出 1970 脏日期。
        try:
            ts_f = float(ts)
            if ts_f <= 0:
                continue
            bar_date = datetime.fromtimestamp(ts_f / 1000, tz=BEIJING_TZ).strftime("%Y-%m-%d")
        except (TypeError, ValueError, OverflowError, OSError):
            continue
        # 统一走 make_kline_bar 契约：date 从北京时间戳解析、OHLCV/percent 数值强转、
        # close<=0 剔除。此前脏值按 0 处理由 analyze_* 兜底，现收敛到入口单点。
        bar = make_kline_bar({
            # 雪球时间戳按北京时间生成，必须用北京时区解析，否则非 UTC+8 部署日期错位
            "date": bar_date,
            "open": item[2],
            "high": item[3],
            "low": item[4],
            "close": item[5],
            "volume": item[1],
            "percent": item[7],
        })
        if bar is not None:
            bar["timestamp"] = ts_f
            result.append(bar)
    if _kline_cache_ttl > 0:
        with _kline_cache_lock:
            _cache_put(_kline_cache, symbol, (result, now))
    return result


# Circuit breaker state for biaosheng
_biaosheng_cb = {"failures": 0, "last_ok": 0.0, "cached": [], "cooldown_until": 0.0, "stale_warned": False}


def _warn_stale_cached(name: str, seconds: float) -> None:
    """获取失败返回陈旧榜单 → 终端醒目告警（一次/冷却期），否则下游无感知。"""
    print(f"  [!] {name} 获取失败，使用 {int(seconds)} 秒前缓存榜单（信号可能失真）", flush=True)


def _biaosheng_circuit_breaker(raw_items: list[dict], success: bool = True) -> list[dict]:
    now = time.time()
    with _cache_lock:
        if success:
            _biaosheng_cb["failures"] = 0
            _biaosheng_cb["cooldown_until"] = 0
            _biaosheng_cb["last_ok"] = now
            _biaosheng_cb["stale_warned"] = False
            if raw_items:
                _biaosheng_cb["cached"] = raw_items
            return raw_items

        _biaosheng_cb["failures"] += 1
        fails = _biaosheng_cb["failures"]

        # 熔断条件：连续失败 >=3 次即进入熔断（无论缓存是否为空）。
        # 旧逻辑要求缓存非空才熔断，启动后前 3 次失败（缓存空）时永不熔断，
        # 持续请求已挂掉的 API 没有退避。
        if fails >= 3:
            cooldown = min(60 * (2 ** (fails - 3)), 600)
            _biaosheng_cb["cooldown_until"] = now + cooldown
            if _biaosheng_cb["cached"]:
                logger.warning("  [断路器] 飙升榜连续%d次失败, 进入%.0f秒熔断, 使用缓存数据",
                               fails, cooldown)
                if not _biaosheng_cb["stale_warned"]:
                    _biaosheng_cb["stale_warned"] = True
                    _warn_stale_cached("飙升榜", now - _biaosheng_cb["last_ok"])
                return _biaosheng_cb["cached"]
            logger.warning("  [断路器] 飙升榜连续%d次失败, 进入%.0f秒熔断（无缓存可用）",
                           fails, cooldown)
            return []

        if _biaosheng_cb["cached"]:
            logger.info("  飙升榜获取失败, 返回缓存数据(%.0fs前)", now - _biaosheng_cb["last_ok"])
            if not _biaosheng_cb["stale_warned"]:
                _biaosheng_cb["stale_warned"] = True
                _warn_stale_cached("飙升榜", now - _biaosheng_cb["last_ok"])
            return _biaosheng_cb["cached"]

        return []


def fetch_biaosheng(session: requests.Session, size: int = 100) -> list[dict]:
    now = time.time()
    with _cache_lock:
        if now < _biaosheng_cb["cooldown_until"]:
            logger.info("  [断路器] 熔断中(剩余%.0fs), 使用缓存数据",
                        _biaosheng_cb["cooldown_until"] - now)
            return _biaosheng_cb.get("cached") or []

    ts = int(now * 1000)
    url = (
        f"https://stock.xueqiu.com/v5/stock/hot_stock/new_list.json"
        f"?page=1&size={size}&order=desc&order_by=rank_change&type=10&_={ts}"
    )
    try:
        resp = _request_with_retry(session, url)
        j = resp.json()
        data = j.get("data") if isinstance(j, dict) else None
        items = (data or {}).get("items", []) if isinstance(data, dict) else []
        # 软错误（HTTP 200 但 data 缺失/为空）：视为失败走熔断退避，
        # 避免每次扫描重置熔断计数、持续轰击已挂掉的接口。
        if not items:
            logger.warning("飙升榜返回空 data（软错误），按失败处理")
            return _biaosheng_circuit_breaker([], success=False)
        return _biaosheng_circuit_breaker(items, success=True)
    except Exception as e:
        logger.error("飙升榜获取失败: %s", e)
        return _biaosheng_circuit_breaker([], success=False)


_xueqiu_hot_cb = {"failures": 0, "last_ok": 0.0, "cached": [], "cooldown_until": 0.0}


def _xueqiu_hot_circuit_breaker(raw_items: list[dict], success: bool = True) -> list[dict]:
    now = time.time()
    with _cache_lock:
        if success:
            _xueqiu_hot_cb["failures"] = 0
            _xueqiu_hot_cb["cooldown_until"] = 0
            _xueqiu_hot_cb["last_ok"] = now
            if raw_items:
                _xueqiu_hot_cb["cached"] = raw_items
            return raw_items

        _xueqiu_hot_cb["failures"] += 1
        fails = _xueqiu_hot_cb["failures"]

        if fails >= 3:
            cooldown = min(60 * (2 ** (fails - 3)), 600)
            _xueqiu_hot_cb["cooldown_until"] = now + cooldown
            if _xueqiu_hot_cb["cached"]:
                logger.warning("  [断路器] 热搜榜连续%d次失败, 进入%.0f秒熔断, 使用缓存数据",
                               fails, cooldown)
                return _xueqiu_hot_cb["cached"]
            logger.warning("  [断路器] 热搜榜连续%d次失败, 进入%.0f秒熔断（无缓存可用）",
                           fails, cooldown)
            return []

        if _xueqiu_hot_cb["cached"]:
            logger.info("  热搜榜获取失败, 返回缓存数据(%.0fs前)", now - _xueqiu_hot_cb["last_ok"])
            return _xueqiu_hot_cb["cached"]

        return []


def fetch_xueqiu_hot_list(session: requests.Session, size: int = 100) -> list[dict]:
    """雪球热搜榜（按热度排序，用于与飙升榜交叉验证）"""
    now = time.time()
    with _cache_lock:
        if now < _xueqiu_hot_cb["cooldown_until"]:
            logger.info("  [断路器] 热搜榜熔断中(剩余%.0fs), 使用缓存数据",
                        _xueqiu_hot_cb["cooldown_until"] - now)
            return _xueqiu_hot_cb.get("cached") or []

    ts = int(now * 1000)
    url = (
        f"https://stock.xueqiu.com/v5/stock/hot_stock/list.json"
        f"?page=1&size={size}&order=desc&order_by=value&type=10&_={ts}&x=0.5"
    )
    try:
        resp = _request_with_retry(session, url)
        j = resp.json()
        data = j.get("data") if isinstance(j, dict) else None
        items = (data or {}).get("items", []) if isinstance(data, dict) else []
        if not items:
            logger.warning("热搜榜返回空 data（软错误），按失败处理")
            return _xueqiu_hot_circuit_breaker([], success=False)
        return _xueqiu_hot_circuit_breaker(items, success=True)
    except Exception as e:
        logger.error("热搜榜获取失败: %s", e)
        return _xueqiu_hot_circuit_breaker([], success=False)


def _quote_high_pct(q: dict) -> float | None:
    """当日最高涨幅（%）：由 quote 的 high 与昨收(last_close/prev_close)计算。

    供推荐后反转移出（mark_reversed_recommendations）以「从当日最高价回落」衡量动量衰减。
    high/base 任一缺失或 ≤0 时返回 None（无法度量 → 调用侧 fail-open）。
    """
    high = _num(q.get("high"))
    base = _num(q.get("prev_close")) or _num(q.get("last_close"))
    if high > 0 and base > 0:
        return (high / base - 1) * 100
    return None


def fetch_market_caps_batch(session: requests.Session, symbols: list[str]) -> dict[str, dict]:
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
            data = resp.json().get("data", {})
            # batch/quote.json 返回结构为 {symbol: {"quote": {...}, ...}}，
            # 也可能为 {"items": [...]} 形态，两种都兼容。
            if isinstance(data, dict) and "items" not in data:
                for sym, entry in data.items():
                    q = entry.get("quote") if isinstance(entry, dict) else entry
                    if not isinstance(q, dict) or not q.get("symbol"):
                        q = entry if isinstance(entry, dict) else {}
                    qsym = q.get("symbol", sym)
                    if qsym:
                        result[qsym] = {"market_cap": _num(q.get("market_capital")),
                                        "circ_market_cap": _num(q.get("float_market_capital")),
                                        "turnover_rate": _num(q.get("turnover_rate")),
                                        "current": _num(q.get("current")),
                                        "percent": _num(q.get("percent")),
                                        "high_pct": _quote_high_pct(q)}
            else:
                items = data.get("items", []) if isinstance(data, dict) else []
                for item in items:
                    q = item.get("quote") if isinstance(item, dict) else {}
                    if not q or not q.get("symbol"):
                        q = item if isinstance(item, dict) else {}
                    sym = q.get("symbol", "")
                    if sym:
                        result[sym] = {"market_cap": _num(q.get("market_capital")),
                                       "circ_market_cap": _num(q.get("float_market_capital")),
                                       "turnover_rate": _num(q.get("turnover_rate")),
                                       "current": _num(q.get("current")),
                                       "percent": _num(q.get("percent")),
                                       "high_pct": _quote_high_pct(q)}
        except Exception as e:
            print(f"  [!] 市值批量查询失败(批次{i // 50 + 1}): {e}")
            continue

    if not result:
        # 注意：此处只代表「雪球单源失败」。上层 FallbackAdapter 会先尝试 akshare 兜底，
        # 且 scan_with_raw 还有 DB 陈旧缓存兜底。故降级为 logger，不在此打印 [!] 告警——
        # 否则会先于兜底链路误报"市值获取失败"，造成双源抖动时虚假恐慌（2026-08-20）。
        logger.warning("雪球市值批量查询返回空（上层将尝试 akshare / DB 陈旧缓存兜底）")

    return result


_INTRADAY_CACHE: dict[str, tuple[float | None, float]] = {}
# 300→120：分时强度评分 2 分钟刷新，让盘中异动更快反映到 intraday_score
_INTRADAY_CACHE_TTL = 120
_INTRADAY_CACHE_FAIL_TTL = 60

_MINUTE_DATA_CACHE: dict[str, tuple[list[dict] | None, float]] = {}
# 120→60：分时数据 1 分钟刷新，配合 opening_strength/live_volume 更及时
_MINUTE_DATA_CACHE_TTL = 60


def _normalize_minute_item(raw) -> dict:
    """Xueqiu chart/minute.json 返回的是数组，与 kline 共用同一套 column 格式，
    需规整为 dict。

    列序（与 scanner/api.py fetch_kline 一致）:
        [timestamp(ms), volume, open, high, low, close, chg, percent,
         turnoverrate, amount, ...]
    - current(当前价) = close = raw[5]
    - avg_price(均价) = amount / volume = raw[9] / raw[1]（分钟接口无独立均价列）
    - capital(大单资金) 不在分钟 bar 中，故恒为 0（分钟接口无该字段）。
    """
    if isinstance(raw, dict):
        return raw
    try:
        ts = raw[0]
        # 数值强转：分时接口与 kline 同源（雪球 chart/minute.json），同样偶发返回
        # 字符串/None/NaN。保持字符串会让下游 analyze_opening_strength / estimate_live_volume
        # 的算术（-、/、+）抛 TypeError 丢分时信号（与 fetch_kline 的 _num 同族修复，
        # 此前仅在调用方 _parallel_fetch 兜底为 None，整相静默降级）。
        volume = _num(raw[1])
        current = _num(raw[5])
        amount = _num(raw[9]) if len(raw) > 9 else 0.0
        avg_price = (amount / volume) if (amount and volume) else current
        # 2026-08-17 审查修复：列序 [ts, volume, open, high, low, close, chg, percent, ...]，
        # 数组形态此前裁剪掉 high/low/percent → 盘中分时兜底 _build_today_bar_from_minute
        # 读到的 high/low 恒 0 → 构造今日 bar 的日内最高/最低退化为 current，振幅失真。
        # 保留三字段（下游三个评分函数只读 current/volume/avg_price，纯增量无破坏）。
        high = _num(raw[3]) if len(raw) > 3 else 0.0
        low = _num(raw[4]) if len(raw) > 4 else 0.0
        percent = _num(raw[7]) if len(raw) > 7 else 0.0
    except (IndexError, TypeError):
        return {"timestamp": 0, "volume": 0, "avg_price": 0.0, "current": 0.0,
                "high": 0.0, "low": 0.0, "percent": 0.0}
    return {
        "timestamp": ts,
        "volume": volume,
        "avg_price": avg_price,
        "current": current,
        "high": high,
        "low": low,
        "percent": percent,
    }


def _fetch_minute_data(session: requests.Session, symbol: str) -> list[dict] | None:
    now = time.time()
    with _minute_data_cache_lock:
        if symbol in _MINUTE_DATA_CACHE:
            data, ts = _MINUTE_DATA_CACHE[symbol]
            if now - ts < _MINUTE_DATA_CACHE_TTL:
                return data
    try:
        ts_ms = int(time.time() * 1000)
        url = f"https://stock.xueqiu.com/v5/stock/chart/minute.json?symbol={symbol}&period=1d&_={ts_ms}"
        resp = _request_with_retry(session, url)
        d = resp.json()
        raw_items = d.get("data", {}).get("items", [])
        # 10→2：开盘前 ~9:40 前不足 10 根分时 bar，原硬门会令 opening_strength /
        # intraday / live_volume 在开盘关键窗口整体静默，错失早盘动量触发。
        # 放宽到 >=2 后 intraday(>=2) / live_volume(>=1) 约 9:32 起可用；
        # opening_strength 仍需 >=6（自身 len() 兜底），约 9:36 起生效。
        if raw_items and len(raw_items) >= 2:
            items = [_normalize_minute_item(it) for it in raw_items]
            # 兜底强转：dict 直通形态（_normalize_minute_item 对 dict 保持原样，含
            # 单测恒等契约）的数值字段可能仍是字符串/None/NaN，数组形态已强转。
            # 统一在此收敛，保证 analyze_opening_strength / estimate_live_volume /
            # analyze_intraday 三个消费者不再对字符串做算术抛 TypeError。
            for it in items:
                for key in ("volume", "current", "avg_price"):
                    if isinstance(it, dict) and key in it:
                        it[key] = _num(it[key])
            with _minute_data_cache_lock:
                _cache_put(_MINUTE_DATA_CACHE, symbol, (items, now))
            return items
        with _minute_data_cache_lock:
            _cache_put(_MINUTE_DATA_CACHE, symbol, (None, now))
        return None
    except Exception as e:
        print(f"  [!] 获取分时数据失败 {symbol}: {e}")
        with _minute_data_cache_lock:
            _cache_put(_MINUTE_DATA_CACHE, symbol, (None, now))
        return None


def analyze_opening_strength(session: requests.Session, symbol: str,
                             items: list[dict] | None = None) -> float | None:
    """开盘强度因子: 分析前5分钟(9:30-9:35)的量价行为.

    items 由调用方（adapter.fetch_minute）传入时直接使用，不重复拉取；
    items=None 走旧路径：内部 _fetch_minute_data 拉取（保留缓存语义）。
    """
    if items is None:
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


def estimate_live_volume(session: requests.Session, symbol: str,
                         items: list[dict] | None = None) -> float | None:
    """实时量比投影：items 由调用方传入时直接使用，否则内部拉取。"""
    if items is None:
        items = _fetch_minute_data(session, symbol)
    if not items:
        return None
    total_vol = sum(item.get("volume", 0) for item in items)
    minutes_elapsed = len(items)
    trading_minutes_total = 240
    estimated = total_vol * trading_minutes_total / max(minutes_elapsed, 1)
    return estimated


def analyze_intraday(session: requests.Session, symbol: str,
                     items: list[dict] | None = None) -> float | None:
    """分时强度评分（三段早/中/晚加权）。

    items 由调用方（adapter.fetch_minute）传入时直接使用且不读写 _INTRADAY_CACHE
    （分时源数据已由 adapter 层缓存）；items=None 走旧路径：内部拉取 + 评分缓存。
    """
    now = time.time()
    use_cache = items is None
    if use_cache:
        with _intraday_cache_lock:
            if symbol in _INTRADAY_CACHE:
                val, ts = _INTRADAY_CACHE[symbol]
                if val is not None and now - ts < _INTRADAY_CACHE_TTL:
                    return val
                if val is None and now - ts < _INTRADAY_CACHE_FAIL_TTL:
                    return None

    if items is None:
        items = _fetch_minute_data(session, symbol)
    if not items or len(items) < 2:
        if use_cache:
            with _intraday_cache_lock:
                _cache_put(_INTRADAY_CACHE, symbol, (None, now))
        return None

    try:
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
                        base = period_items[idx]["current"]
                        if base > 0:
                            # 分母守卫：current=0 的脏分时 bar（强转失败归 0）不得拖垮
                            # 整票分时评分（此前 ZeroDivisionError → 整段信号降级 None）
                            seg_chgs.append(
                                (period_items[nxt]["current"] - base) / base * 100)
                attacks = sum(1 for c in seg_chgs if c > 0.15)
                declines = sum(1 for c in seg_chgs if c < -0.15)
                net = attacks - declines
                if net >= 2:
                    p_score += 2.0
                elif net <= -2:
                    p_score -= 2.0

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

        # 大单资金(capital) 不在分钟 bar 中（分钟接口无该字段），该 ±2 加分恒为 0，
        # 属预期收敛，不伪造数据。如需大单维度应改用 capital_flow 独立接口。

        score = max(-10.0, min(10.0, score))
        if use_cache:
            with _intraday_cache_lock:
                _cache_put(_INTRADAY_CACHE, symbol, (score, now))
        return score
    except Exception as e:
        print(f"  [!] 分析分时强度失败 {symbol}: {e}")
        if use_cache:
            with _intraday_cache_lock:
                _cache_put(_INTRADAY_CACHE, symbol, (None, now))
        return None
