"""行情增强数据源（涨停池 + 个股资金流）。

基于 AKShare 东财接口，为候选股补充主力资金流向与涨停/连板信息，
作为新增评分维度与风险标签的输入（不影响现有打分逻辑，失败静默降级）。

数据源：
- 涨停池   `ak.stock_zt_pool_em(date)`        全市场 1 次请求/轮
- 资金流   `ak.stock_individual_fund_flow_rank` 全市场 1 次请求/轮（今日主力净流入）
  资金流接口 host（push2.eastmoney.com）在网络代理环境下可能不可达，
  本模块严格 fail-soft：任何失败返回空 dict + 打印告警，绝不影响主扫描。

与 concept.py 相同的可靠性模式：
- lazy import akshare（未安装时自动禁用，返回空）
- 进程内 TTL 缓存（按交易日键、线程安全、带上限淘汰）→ DB 缓存（当日 +
  盘中新鲜度）→ 拉取落库
- 单次拉取带限时（_bounded_call），失败缓存空结果短退避，绝不阻塞扫描循环
- 符号格式转换复用 data_source 的 _ak_to_xq（300001 → SZ300001）
"""
import logging
import os
import threading
import time

from scanner.config import (
    CACHE_MAX_ENTRIES,
    ENABLE_FUND_FLOW,
    ENABLE_ZT_POOL,
    FUND_FLOW_FETCH_TIMEOUT,
    FUND_FLOW_TTL_SEC,
    ZT_POOL_FETCH_TIMEOUT,
    ZT_POOL_TTL_SEC,
    now_beijing,
)
from scanner.data_source import _ak_to_xq
from scanner.database import get_market_extra_cache, save_market_extra_cache

logger = logging.getLogger(__name__)

_ZT_POOL = "zt_pool"
_FUND_FLOW = "fund_flow"

_ak = None
_ak_lock = threading.Lock()


def _get_ak():
    """lazy import akshare；不可用返回 None（上层按空数据降级）。

    顺带禁用 tqdm 进度条（资金流接口全市场分页会向 stderr 打印进度，
    在 supervised 长跑模式下污染日志）。
    """
    global _ak
    if _ak is not None:
        return _ak
    with _ak_lock:
        if _ak is None:
            try:
                os.environ.setdefault("TQDM_DISABLE", "1")
                import akshare  # noqa: PLC0415
                _ak = akshare
            except Exception as e:  # ImportError 等
                logger.warning("AKShare 不可用，行情增强数据禁用: %s", e)
                _ak = False
    return _ak if _ak is not False else None


def _cache_put(cache: dict, key, value):
    """带上限写入：超限淘汰最旧，防长跑内存膨胀（调用方持有锁）。"""
    if key in cache:
        cache.pop(key)
    cache[key] = value
    while len(cache) > CACHE_MAX_ENTRIES:
        cache.pop(next(iter(cache)))


_zt_cache: dict[str, tuple[dict, float]] = {}
_ff_cache: dict[str, tuple[dict, float]] = {}
_extra_lock = threading.Lock()


def _today_key() -> str:
    return now_beijing().date().strftime("%Y%m%d")


def _cache_hit(cache: dict, ttl: float, key: str) -> dict | None:
    with _extra_lock:
        entry = cache.get(key)
        if entry and time.time() - entry[1] < ttl:
            return entry[0]
        return None


def _cache_put_all(cache: dict, data: dict, key: str, now: float | None = None):
    with _extra_lock:
        _cache_put(cache, key, (data, now if now is not None else time.time()))


def _bounded_call(fn, timeout: float):
    """带限时执行网络调用：超时抛 TimeoutError，调用方按失败降级。

    用 daemon 线程 + join(timeout) 而非 ThreadPoolExecutor，超时后线程在后台
    自然结束（daemon 不阻塞进程退出），主扫描循环不被外部 host 挂死。
    """
    box: dict = {}

    def _run():
        try:
            box["value"] = fn()
        except BaseException as e:  # noqa: BLE001
            box["error"] = e

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=timeout)
    if t.is_alive():
        raise TimeoutError(f"AKShare 调用超过 {timeout}s 已放弃")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def fetch_zt_pool(today: str | None = None) -> dict[str, dict]:
    """拉取今日涨停池（东财），返回 {6位代码: {lianban, zt_stat, fengban_amt, zhaban, industry}}。

    非交易日接口返回空表 → 空 dict。失败打印告警、缓存空结果短退避并返回 {}（软降级）。
    """
    key = today or _today_key()
    cached = _cache_hit(_zt_cache, ZT_POOL_TTL_SEC, key)
    if cached is not None:
        return cached
    ak = _get_ak()
    if ak is None:
        return {}
    try:
        df = _bounded_call(lambda: ak.stock_zt_pool_em(date=key), ZT_POOL_FETCH_TIMEOUT)
        result: dict[str, dict] = {}
        if df is not None and not df.empty:
            for _, row in df.iterrows():
                code = str(row["代码"]).strip()
                if not code:
                    continue
                result[code] = {
                    "lianban": int(_num(row, "连板数") or 0),
                    "zt_stat": str(row.get("涨停统计") or ""),
                    "fengban_amt": float(_num(row, "封板资金") or 0),
                    "zhaban": int(_num(row, "炸板次数") or 0),
                    "industry": str(row.get("所属行业") or ""),
                }
        _cache_put_all(_zt_cache, result, key)
        return result
    except Exception as e:
        print(f"  [!] 涨停池获取失败: {e}")
        _cache_put_all(_zt_cache, {}, key)  # 短退避：TTL 内不重复重试，避免每轮刷屏/轰击
        return {}


def fetch_fund_flow_rank() -> dict[str, dict]:
    """拉取全市场个股今日资金流（东财），返回 {6位代码: {main_net, main_pct, super_net}}。

    main_net = 今日主力净流入额（元），main_pct = 今日主力净流入净占比（%），
    super_net = 今日超大单净流入额（元）。失败打印告警、缓存空结果短退避并返回 {}（软降级）。
    """
    key = _today_key()
    cached = _cache_hit(_ff_cache, FUND_FLOW_TTL_SEC, key)
    if cached is not None:
        return cached
    ak = _get_ak()
    if ak is None:
        return {}
    try:
        df = _bounded_call(lambda: ak.stock_individual_fund_flow_rank(indicator="今日"),
                           FUND_FLOW_FETCH_TIMEOUT)
        result: dict[str, dict] = {}
        if df is not None and not df.empty:
            for _, row in df.iterrows():
                code = str(row["代码"]).strip()
                if not code:
                    continue
                result[code] = {
                    "main_net": float(_num(row, "今日主力净流入-净额") or 0),
                    "main_pct": float(_num(row, "今日主力净流入-净占比") or 0),
                    "super_net": float(_num(row, "今日超大单净流入-净额") or 0),
                }
        _cache_put_all(_ff_cache, result, key)
        return result
    except Exception as e:
        print(f"  [!] 个股资金流获取失败: {e}")
        _cache_put_all(_ff_cache, {}, key)  # 短退避：TTL 内不重复重试，避免每轮刷屏/轰击
        return {}


def _num(row, key) -> float:
    """单元格安全取值：None/NaN/空字符串 → 0.0。"""
    try:
        v = row.get(key)
    except (AttributeError, TypeError):
        return 0.0
    if v is None:
        return 0.0
    try:
        if v != v:  # NaN
            return 0.0
    except Exception:
        pass
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _merge_from_db(conn, symbols: list[str], data_type: str,
                   intraday_ttl_sec: int, cache: dict) -> tuple[dict[str, dict], list[str]]:
    """DB 缓存命中补齐，返回 (result, missing)。

    intraday_ttl_sec：DB 条目超过该秒数视为过期（盘中刷新），过期条目与缺失
    一样落入 missing，交由拉取流程刷新 → 保证资金流/涨停池盘中持续更新而非
    冻结首次扫描快照。
    """
    if not symbols:
        return {}, []
    db = get_market_extra_cache(conn, symbols, data_type, intraday_ttl_sec)
    result: dict[str, dict] = {}
    missing: list[str] = []
    for sym in symbols:
        if sym in db and db[sym]:
            result[sym] = db[sym]
        else:
            missing.append(sym)
    return result, missing


def collect_market_extra(conn, symbols: list[str],
                         include_zt: bool | None = None,
                         include_flow: bool | None = None) -> dict[str, dict]:
    """收集候选符号的行情增强数据。

    返回 {symbol: {"zt": {...}|None, "fund_flow": {...}|None}}。
    仅返回本次有数据（涨停池/资金流至少一类命中）的 symbol。
    流程：进程 TTL 缓存 → DB 缓存（当日 + 盘中新鲜度，过期重拉）→ 全市场
    一次拉取并落库。所有异常均已 fail-soft，返回空 dict 不影响主扫描。
    """
    include_zt = ENABLE_ZT_POOL if include_zt is None else include_zt
    include_flow = ENABLE_FUND_FLOW if include_flow is None else include_flow
    if not symbols or (not include_zt and not include_flow):
        return {}
    uniq = list(dict.fromkeys(symbols))
    result: dict[str, dict] = {}

    if include_zt:
        db_map, missing_zt = _merge_from_db(conn, uniq, _ZT_POOL,
                                            ZT_POOL_TTL_SEC, _zt_cache)
        for sym, payload in db_map.items():
            result.setdefault(sym, {})["zt"] = payload
        if missing_zt:
            fetched = fetch_zt_pool()
            if fetched:
                # 全市场涨停池映射回 xq 符号；未涨停票无数据，不写库（None 语义）
                mapped = {_ak_to_xq(code): payload for code, payload in fetched.items()}
                save_map = {sym: mapped[sym] for sym in missing_zt if sym in mapped}
                if save_map:
                    save_market_extra_cache(conn, save_map, _ZT_POOL)
                for sym in missing_zt:
                    if sym in mapped:
                        result.setdefault(sym, {})["zt"] = mapped[sym]

    if include_flow:
        db_flow, miss_flow = _merge_from_db(conn, uniq, _FUND_FLOW,
                                            FUND_FLOW_TTL_SEC, _ff_cache)
        for sym, payload in db_flow.items():
            result.setdefault(sym, {})["fund_flow"] = payload
        if miss_flow:
            fetched = fetch_fund_flow_rank()
            if fetched:
                mapped = {_ak_to_xq(code): payload for code, payload in fetched.items()}
                save_map = {sym: mapped[sym] for sym in miss_flow if sym in mapped}
                if save_map:
                    save_market_extra_cache(conn, save_map, _FUND_FLOW)
                for sym in miss_flow:
                    if sym in mapped:
                        result.setdefault(sym, {})["fund_flow"] = mapped[sym]

    return result


def reset_extra_cache():
    """重置进程内缓存（仅用于测试）。"""
    global _ak
    with _extra_lock:
        _zt_cache.clear()
        _ff_cache.clear()
    _ak = None
