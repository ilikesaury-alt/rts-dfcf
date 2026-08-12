"""基本面风险过滤层（pywencai 问财条件查询，2026-08-12 新增）。

定位：**排除式过滤器**（filter），不做评分加分。本项目历史反复证明加分类因子
最终都反指被归零（资金流加分、validation_bonus、辨识度加分），而排除类
（资不抵债/退市风险）是纯规避语义，与现有硬过滤（主力出货/趋势破位）同架构。

数据源：同花顺问财 pywencai（lazy import，未安装/失败自动返回空集，fail-open）。
查询方式：**反向条件查询**——问财一次返回全市场命中集合（实测"每股净资产小于0"
→ 42 只，其中 GEM 10 只），比逐票拉取稳定（实测批量单票查询丢代码/返回无关数据），
且一次请求覆盖全市场。

缓存：
- 进程内缓存：FUND_RISK_TTL_SEC 内命中即复用，不重复打问财
- DB 缓存：复用 market_extra_cache 表（data_type="fund_risk"），
  保存候选符号 → payload（reason），重启不丢、展示层可读

健壮性：
- pywencai 未安装 / 查询异常 / 空结果 → 返回 {}，主扫描不受影响（fail-open）
- _bounded_call 限时：pywencai 内部无 timeout，daemon 线程 + join(timeout) 兜底
  超时返回已收集部分或空，不阻塞 60s 扫描循环
"""

import re
import threading
import time

from scanner.config import (
    FUND_RISK_FETCH_TIMEOUT,
    FUND_RISK_QUERY,
    FUND_RISK_REASON,
    FUND_RISK_TTL_SEC,
    now_beijing,
)
from scanner.database import get_market_extra_cache, save_market_extra_cache

_DATA_TYPE = "fund_risk"

_fund_risk_cache: dict[str, tuple[dict, float]] = {}
_fund_risk_lock = threading.Lock()

_logged_missing = False


def _today_key() -> str:
    return now_beijing().date().strftime("%Y%m%d")


def reset_fund_risk_cache():
    """清空进程内缓存（测试用）。"""
    global _logged_missing
    with _fund_risk_lock:
        _fund_risk_cache.clear()
        _logged_missing = False


def _cache_put(data: dict, now: float | None = None):
    with _fund_risk_lock:
        _fund_risk_cache[_today_key()] = (data, now if now is not None else time.time())


def _cache_get() -> dict | None:
    with _fund_risk_lock:
        entry = _fund_risk_cache.get(_today_key())
        if entry and time.time() - entry[1] < FUND_RISK_TTL_SEC:
            return entry[0]
    return None


def _bounded_call(fn, timeout: float):
    """带限时执行网络调用：超时抛 TimeoutError，调用方按失败降级。

    与 market_extra._bounded_call 同构：daemon 线程 + join(timeout)，
    超时后线程在后台自然结束，主扫描循环不被外部 host 挂死。
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
        raise TimeoutError(f"pywencai 查询超过 {timeout}s 已放弃")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _extract_xq_symbols(df) -> set[str]:
    """从问财返回的 DataFrame 提取雪球 symbol 集合（SZ300001 格式）。

    问财代码列通常是"股票代码"（300027.SZ）或"code"/"__code"，列名随查询
    条件变化。兼容多种列名 + 数字后缀格式，提取 6 位代码并映射交易所前缀。
    """
    symbols: set[str] = set()
    if df is None:
        return symbols
    try:
        if hasattr(df, "columns"):
            code_col = None
            for cand in ("股票代码", "代码", "code", "__code"):
                if cand in df.columns:
                    code_col = cand
                    break
            if code_col is not None:
                for v in df[code_col].tolist():
                    sym = _code_to_xq(v)
                    if sym:
                        symbols.add(sym)
        elif isinstance(df, dict):
            # pywencai 某些查询返回 dict[str, DataFrame]，取第一个非空表
            for v in df.values():
                if hasattr(v, "columns"):
                    symbols |= _extract_xq_symbols(v)
                    break
    except Exception:
        pass
    return symbols


def _code_to_xq(v) -> str | None:
    """6 位代码（可带 .SZ/.SH 后缀或交易所前缀）→ 雪球 symbol。非法返回 None。"""
    if v is None:
        return None
    s = str(v).strip()
    m = re.search(r"(\d{6})", s)
    if not m:
        return None
    code = m.group(1)
    if code.startswith(("4", "8", "92")) or s.startswith("BJ") or s.upper().endswith("BJ"):
        return "BJ" + code
    if code.startswith(("6", "9")):
        return "SH" + code
    return "SZ" + code


def fetch_fund_risk_map() -> dict[str, str]:
    """全市场问财查询资不抵债股，返回 {xq_symbol: reason}。失败返回 {}。

    进程缓存优先：FUND_RISK_TTL_SEC（默认一天）内只查一次问财。
    """
    cached = _cache_get()
    if cached is not None:
        return cached
    result: dict[str, str] = {}
    try:
        import pywencai  # noqa: PLC0415
    except Exception:
        return result
    try:
        df = _bounded_call(lambda: pywencai.get(query=FUND_RISK_QUERY, loop=True),
                           FUND_RISK_FETCH_TIMEOUT)
        for sym in _extract_xq_symbols(df):
            result[sym] = FUND_RISK_REASON
    except Exception:
        # fail-open：查询异常/超时不缓存失败结果，下轮扫描重试
        return {}
    if result:
        _cache_put(result)
    return result


def collect_fund_risk(conn, symbols: list[str]) -> dict[str, str]:
    """收集候选符号的财务风险标记，返回 {xq_symbol: reason}。

    流程：进程缓存 → DB 缓存（当日 data_type=fund_risk）→ 全市场问财查询一次。
    任一环节缺失即触发下一环；全程 fail-open，返回空 dict 不影响主扫描。
    命中符号将被打"财务风险"硬过滤标签（orchestrator 组装候选时应用）。
    """
    if not symbols:
        return {}
    uniq = list(dict.fromkeys(symbols))
    fetched = fetch_fund_risk_map()
    if not fetched:
        return {}
    # 仅保留当前候选命中子集（问财返回全市场，只需本批符号的）
    result = {sym: fetched[sym] for sym in uniq if sym in fetched}
    # DB 落库：重启/掉榜后展示层（stock_report）仍可读当日财务风险状态
    if result and conn is not None:
        try:
            save_market_extra_cache(
                conn,
                {sym: {"reason": reason} for sym, reason in result.items()},
                _DATA_TYPE,
            )
        except Exception:
            pass
    return result


def get_fund_risk_from_db(conn, symbol: str) -> str | None:
    """从 DB 读当日某符号的财务风险 reason（stock_report 展示用）。无则 None。"""
    try:
        db = get_market_extra_cache(conn, [symbol], _DATA_TYPE)
        payload = db.get(symbol)
        if payload:
            return str(payload.get("reason") or "")
    except Exception:
        pass
    return None
