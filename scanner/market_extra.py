"""行情增强数据源（涨停池 + 个股资金流）。

数据源：
- 涨停池   主源同花顺官方 API（ths_api.py，2026-08-23 接入）；AKShare
           `stock_zt_pool_em(date)` 降为兜底。全市场 1 次请求/轮
- 资金流   自实现直连东财 clist API（push2delay.eastmoney.com）全市场分页拉取，
           主因 akshare `stock_individual_fund_flow_rank` 硬编码 host
           push2.eastmoney.com 在本机网络直连/代理均不可达；push2delay 提供相同
           API 且可达（数据可能延迟约15分钟）。host 可用 RTS_FUND_FLOW_HOST 覆盖。

与 concept.py 相同的可靠性模式：
- lazy import akshare（仅涨停池兜底依赖；未安装时自动禁用，返回空）
- 进程内 TTL 缓存（按交易日键、线程安全、带上限淘汰）→ DB 缓存（当日 +
  盘中新鲜度）→ 拉取落库
- 拉取带限时/分页 deadline，失败缓存空结果短退避，绝不阻塞扫描循环
- 符号格式转换复用 data_source 的 _ak_to_xq（300001 → SZ300001）
"""
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests as _requests

from scanner.config import (
    ENABLE_FUND_FLOW,
    ENABLE_ZT_POOL,
    FUND_FLOW_FETCH_TIMEOUT,
    FUND_FLOW_HOST,
    FUND_FLOW_PARTIAL_TTL_SEC,
    FUND_FLOW_TTL_SEC,
    ZT_POOL_FETCH_TIMEOUT,
    ZT_POOL_TTL_SEC,
    now_beijing,
)
from scanner.data_source import _ak_to_xq
from scanner.database import get_market_extra_cache, save_market_extra_cache
from scanner.net import EASTMONEY_HEADERS, EASTMONEY_UT_TOKEN, _bounded_call
from scanner.utils import cache_put as _cache_put
from scanner.utils import to_float

logger = logging.getLogger(__name__)

_ZT_POOL = "zt_pool"
_FUND_FLOW = "fund_flow"

# 资金流 clist API：与 akshare stock_individual_fund_flow_rank("今日") 同参数。
# 字段码：f12=代码, f62=主力净流入净额, f184=主力净流入净占比, f66=超大单净流入净额
_FUND_FLOW_FIELDS = "f12,f14,f2,f3,f62,f184,f66,f69,f72,f75,f78,f81,f84,f87,f204,f205,f124"
_FUND_FLOW_FS = ("m:0+t:6+f:!2,m:0+t:13+f:!2,m:0+t:80+f:!2,m:1+t:2+f:!2,"
                 "m:1+t:23+f:!2,m:0+t:7+f:!2,m:1+t:3+f:!2")
_FUND_FLOW_PAGE_SIZE = 100

_ak = None
_ak_lock = threading.Lock()


def _get_ak():
    """lazy import akshare；不可用返回 None（上层按空数据降级）。

    顺带禁用 tqdm 进度条（akshare 部分接口向 stderr 打印进度，
    在长跑模式下污染日志）。
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
                logger.warning("AKShare 不可用，涨停池数据禁用: %s", e)
                _ak = False
    return _ak if _ak is not False else None


_zt_cache: dict[str, tuple[dict, float]] = {}
_ff_cache: dict[str, tuple[dict, float]] = {}
_extra_lock = threading.Lock()
# 最近一次全市场资金流拉取是否超时（部分结果）。完整拉取时把全市场快照全部
# 落库，保证重启/掉榜后任一 symbol 的资金流数据仍可读；部分结果只存候选，
# 避免把不完整数据当快照冻结。
_last_ff_partial: bool = False


def _today_key() -> str:
    return now_beijing().date().strftime("%Y%m%d")


def _cache_hit(cache: dict, ttl: float, key: str) -> dict | None:
    with _extra_lock:
        entry = cache.get(key)
        if entry:
            # 第三条记录写入时的自定义 TTL（None 用调用方默认 ttl）
            eff_ttl = entry[2] if len(entry) > 2 and entry[2] is not None else ttl
            if time.time() - entry[1] < eff_ttl:
                return entry[0]
        return None


def _cache_put_all(cache: dict, data: dict, key: str, now: float | None = None,
                   ttl: float | None = None):
    with _extra_lock:
        _cache_put(cache, key, (data, now if now is not None else time.time(), ttl))



def _fetch_zt_pool_ths(date_key: str) -> dict[str, dict] | None:
    """THS 官方涨停池（2026-08-23 主源）；未配置 Key / 接口失败返回 None → AKShare 兜底。"""
    from scanner import ths_api  # 惰性导入避免无 Key 场景的模块级开销

    if not ths_api.get_api_key():
        return None
    date_ms = ths_api._date_to_ms(date_key)
    return ths_api.fetch_limit_up_pool(date_ms=date_ms, include_break=True)


def fetch_zt_pool(today: str | None = None) -> dict[str, dict]:
    """拉取今日涨停池，返回 {6位代码: {lianban, zt_stat, fengban_amt, zhaban, industry}}。

    主源：同花顺官方 API（字段更富：封单额/涨停原因/开板次数；免 AKShare 的
    _bounded_call 兜底）。THS 未配置 Key / 接口失败 → AKShare 兜底（原路径）。
    非交易日接口返回空表 → 空 dict。失败打印告警、缓存空结果短退避并返回 {}（软降级）。
    """
    key = today or _today_key()
    cached = _cache_hit(_zt_cache, ZT_POOL_TTL_SEC, key)
    if cached is not None:
        return cached
    ths_result = None
    try:
        ths_result = _fetch_zt_pool_ths(key)
    except Exception as e:  # noqa: BLE001  THS 层异常同样降级 AKShare
        logger.warning("THS 涨停池异常，降级 AKShare: %s", e)
    if ths_result is not None:
        _cache_put_all(_zt_cache, ths_result, key)
        return ths_result
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
        # 短退避：失败空结果只冻结 FUND_FLOW_PARTIAL_TTL_SEC(60s) 而非默认 300s——
        # 默认 TTL 会让失败在 5 分钟内零重试（2026-08-20 修复，此前注释声称"短退避"实际 300s）。
        _cache_put_all(_zt_cache, {}, key, ttl=FUND_FLOW_PARTIAL_TTL_SEC)
        return {}


def _collect_fund_flow(box: dict, deadline: float) -> dict:
    """并行分页拉取全市场个股今日资金流，结果实时写入 box["value"]（超时可得部分）。

    服务端 pz 封顶 100（实测 200/500/1000 仍返回 100 行），全市场约 5292 只 →
    53 页，串行太慢，故按 6 线程并行拉页。每页 timeout=10，页间检查 deadline：
    超时取消未开始任务并返回已收集部分。网络/解析异常该页返回空继续。
    返回 {6位代码: {main_net, main_pct, super_net}}。
    完成全部页数时置 box["done"]=True，供外层区分"完整快照 vs 超时部分"。
    """
    result: dict[str, dict] = {}
    if time.time() > deadline:
        box["value"] = result
        return result
    base = {
        "fid": "f62", "po": "1", "pz": str(_FUND_FLOW_PAGE_SIZE), "np": "1",
        "fltt": "2", "invt": "2", "ut": EASTMONEY_UT_TOKEN,
        "fs": _FUND_FLOW_FS, "fields": _FUND_FLOW_FIELDS,
    }
    url = f"https://{FUND_FLOW_HOST}/api/qt/clist/get"

    def _page(pn: int):
        p = dict(base)
        p["pn"] = str(pn)
        try:
            resp = _requests.get(url, params=p, timeout=10, headers=EASTMONEY_HEADERS)
            j = resp.json()
            data = j.get("data") if isinstance(j, dict) else None
            if not isinstance(data, dict):
                return [], None
            return data.get("diff") or [], data.get("total")
        except Exception:
            return [], None

    def _absorb(diff):
        for row in diff:
            if not isinstance(row, dict):
                continue
            code = str(row.get("f12") or "").strip()
            if not code:
                continue
            result[code] = {
                "main_net": _num(row, "f62"),
                "main_pct": _num(row, "f184"),
                "super_net": _num(row, "f66"),
            }

    first, total = _page(1)
    if first:
        _absorb(first)
    box["value"] = dict(result)  # 首页后即可读到部分结果
    total = total or len(first)
    total_pages = max(1, -(-total // _FUND_FLOW_PAGE_SIZE))
    if total_pages <= 1:
        box["done"] = True
        return result
    remaining = list(range(2, total_pages + 1))
    completed_all = True
    with ThreadPoolExecutor(max_workers=6) as pool:
        futs = {pool.submit(_page, pn): pn for pn in remaining}
        for fut in as_completed(futs):
            if time.time() > deadline:
                for f in futs:
                    f.cancel()
                completed_all = False
                break
            diff, _ = fut.result()
            _absorb(diff or [])
            box["value"] = dict(result)
    if completed_all:
        box["done"] = True
    return result


def _fetch_fund_flow_bounded(timeout: float) -> tuple[dict, bool]:
    """daemon 线程内执行并行拉取，join(timeout) 到点即返回 (已收集部分, 是否超时)。

    不抛错：网络异常在 _page 内被吞掉 → 空/部分结果；超时由调用方按部分结果处理。
    判定"部分"的依据不只是线程是否仍在运行（线程可能在 join 超时前恰好跑完
    内部 deadline），还须看 _collect_fund_flow 是否显式标记完成（box["done"]）。
    """
    box: dict = {"value": {}}

    def _run():
        try:
            _collect_fund_flow(box, time.time() + timeout)
        except BaseException as e:  # noqa: BLE001
            box["error"] = e

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=timeout)
    if "error" in box:
        raise box["error"]
    timed_out = t.is_alive() or not box.get("done")
    return box.get("value", {}), timed_out


def fetch_fund_flow_rank() -> dict[str, dict]:
    """拉取全市场个股今日资金流（东财 push2delay），返回 {6位代码: {main_net, main_pct, super_net}}。

    main_net = 今日主力净流入额（元），main_pct = 今日主力净流入净占比（%），
    super_net = 今日超大单净流入额（元）。超时返回已收集部分（告警 + 只缓存
    FUND_FLOW_PARTIAL_TTL_SEC，下一轮扫描重试补全缺页）；彻底失败打印告警、
    缓存空结果短退避并返回 {}（软降级）。
    """
    key = _today_key()
    cached = _cache_hit(_ff_cache, FUND_FLOW_TTL_SEC, key)
    if cached is not None:
        return cached
    global _last_ff_partial
    try:
        result, timed_out = _fetch_fund_flow_bounded(FUND_FLOW_FETCH_TIMEOUT)
        if timed_out:
            _last_ff_partial = True
            print(f"  [!] 个股资金流拉取超时，已获取 {len(result)} 只（部分数据），下一轮扫描重试补全")
            _cache_put_all(_ff_cache, result, key, ttl=FUND_FLOW_PARTIAL_TTL_SEC)
        else:
            _last_ff_partial = False
            _cache_put_all(_ff_cache, result, key)
        return result
    except Exception as e:
        _last_ff_partial = True
        print(f"  [!] 个股资金流获取失败: {e}")
        # 短退避：失败空结果只冻结 FUND_FLOW_PARTIAL_TTL_SEC(60s) 而非默认 300s
        # （2026-08-20 修复，此前注释声称"短退避"实际 300s 内零重试）。
        _cache_put_all(_ff_cache, {}, key, ttl=FUND_FLOW_PARTIAL_TTL_SEC)
        return {}


def _num(row, key) -> float:
    """单元格安全取值：None/NaN/±inf/不可解析字符串 → 0.0（统一走 utils.to_float）。"""
    try:
        v = row.get(key)
    except (AttributeError, TypeError):
        return 0.0
    if v is None:
        return 0.0
    return to_float(v) or 0.0


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
                if _last_ff_partial:
                    # 超时部分结果：只存当前缺失候选，避免把不完整数据当快照冻结
                    save_map = {sym: mapped[sym] for sym in miss_flow if sym in mapped}
                else:
                    # 完整全市场快照：全部落库。这样当日任一 symbol（含已掉榜/重启前
                    # 推荐过的票）都能在展示层读到资金流数据，而非只有当前候选有。
                    save_map = mapped
                if save_map:
                    save_market_extra_cache(conn, save_map, _FUND_FLOW)
                for sym in miss_flow:
                    if sym in mapped:
                        result.setdefault(sym, {})["fund_flow"] = mapped[sym]

    return result


def reset_extra_cache():
    """重置进程内缓存（仅用于测试）。"""
    global _ak, _last_ff_partial
    with _extra_lock:
        _zt_cache.clear()
        _ff_cache.clear()
    _ak = None
    _last_ff_partial = False
