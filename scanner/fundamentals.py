"""基本面风险过滤层（2026-08-23 主源切换 THS 官方估值快照，pywencai 降兜底）。

定位：**排除式过滤器**（filter），不做评分加分。本项目历史反复证明加分类因子
最终都反指被归零（资金流加分、validation_bonus、辨识度加分），而排除类
（资不抵债/退市风险）是纯规避语义，与现有硬过滤（主力出货/趋势破位）同架构。

数据源：
- 主源：同花顺官方 API 估值快照（ths_api.fetch_valuations）——**pb_mrq < 0
  ⟺ 每股净资产为负（资不抵债）**，文档明确负值原样返回；pb_mrq=null
  （未披露/停牌）不算命中，与问财"只返回命中集合"同语义，不误杀。
  查询范围 = 创业板代码表（ths_api.fetch_gem_codes，单请求圈定；扫描器
  只做创业板）。THS 软限流 ≈3.8 req/s 下 ~1400 只 GEM 分 14 批约需 15-40s，
  故做**跨轮增量拉取**：每轮只拉未完成批次（FUND_RISK_FETCH_TIMEOUT 预算内），
  命中集单调增长，全部完成后按日级 TTL 定稿。
- 兜底：同花顺问财 pywencai（lazy import，未安装/失败自动跳过）——保留原
  "每股净资产小于0"反向条件查询实现，THS 未配置 Key / 接口失败时启用。

缓存：
- 进程内缓存：成功结果 FUND_RISK_TTL_SEC 内复用；增量未完成/失败按
  FUND_RISK_FAIL_TTL_SEC 短退避（一扫描周期后重试续传）
- DB 落库：仅写路径，供 stock_report 展示层读取当日财务风险状态

健壮性：所有异常 fail-open 返回 {}，主扫描不受影响。
"""

import logging
import re
import threading
import time

from scanner.config import (
    ENABLE_FUND_RISK,
    FUND_RISK_FAIL_TTL_SEC,
    FUND_RISK_FETCH_TIMEOUT,
    FUND_RISK_QUERY,
    FUND_RISK_REASON,
    FUND_RISK_TTL_SEC,
    now_beijing,
)
from scanner.database import get_market_extra_cache, save_market_extra_cache
from scanner.net import _bounded_call

logger = logging.getLogger(__name__)

_DATA_TYPE = "fund_risk"
_VALUATION_BATCH_SIZE = 100  # THS 估值快照服务端单批上限

_fund_risk_cache: dict[str, tuple[dict, float]] = {}
_fund_risk_lock = threading.Lock()

# THS 跨轮增量进度：{date_key: {"codes": [...], "done": int, "hits": {sym: reason}}}
_ths_progress: dict[str, dict] = {}
_ths_progress_lock = threading.Lock()

_logged_missing = False  # pywencai 未安装告警只打一次，避免每轮刷屏


def _today_key() -> str:
    return now_beijing().date().strftime("%Y%m%d")


def reset_fund_risk_cache():
    """清空进程内缓存与增量进度（测试用）。"""
    global _logged_missing
    with _fund_risk_lock:
        _fund_risk_cache.clear()
        _logged_missing = False
    with _ths_progress_lock:
        _ths_progress.clear()


def _warn_missing_pywencai():
    """pywencai 未安装时打印一次告警：ENABLE_FUND_RISK=1 下财务风险硬过滤静默 no-op。"""
    global _logged_missing
    if not _logged_missing:
        _logged_missing = True
        print("[财务风险] 警告：pywencai 未安装 → FUND_RISK 硬过滤静默失效，"
              "资不抵债票将照常进推荐。请 `pip install pywencai` 启用（optional 重依赖）。")


def _cache_put(data: dict, now: float | None = None, ttl: float | None = None):
    with _fund_risk_lock:
        _fund_risk_cache[_today_key()] = (data, now if now is not None else time.time(), ttl)


def _cache_get() -> dict | None:
    with _fund_risk_lock:
        entry = _fund_risk_cache.get(_today_key())
        if entry:
            # 失败/空结果走短退避 TTL（一扫描周期）；成功结果走日级 TTL
            eff_ttl = entry[2] if len(entry) > 2 and entry[2] is not None else FUND_RISK_TTL_SEC
            if time.time() - entry[1] < eff_ttl:
                return entry[0]
    return None


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


def _fetch_fund_risk_ths() -> tuple[dict, bool]:
    """THS 估值快照增量拉取资不抵债票（pb_mrq<0），返回 (hits, complete)。

    - 首轮拉创业板代码表圈定范围，按 100 只/批切批；
    - 每次调用在 FUND_RISK_FETCH_TIMEOUT 预算内只拉未完成批次，命中跨轮累积
      （THS 软限流下全量 ~15-40s，一轮 60s 扫描循环内做不完）；
    - complete=True：全部批次完成 → hits 定稿（调用方按日级 TTL 缓存并清进度）；
    - complete=False：预算用尽/接口失败 → 返回当前已累积 hits（调用方按短退避
      缓存，下轮续传）；THS 未配置 Key / 代码表失败 → ({}, False)。

    2026-08-24 审查修复：锁内只做进度快照读写，网络请求全部移到锁外——原实现
    整段拉取循环持 _ths_progress_lock 最长 25s（fetch_gem_codes + ~14 批估值
    请求），任何并发进入者被整段阻塞。现快照→锁外拉取→重加锁提交（CAS 式推进：
    done 取 max、hits 合并；并发调用方重复拉同批结果幂等无害）。单线程主循环下
    行为与原实现完全一致。
    """
    from scanner import ths_api

    if not ths_api.get_api_key():
        return {}, False
    key = _today_key()
    # ── 快照阶段（锁内）：读当前进度 ──
    codes: list | None = None
    with _ths_progress_lock:
        st = _ths_progress.get(key)
        if st is not None:
            codes = list(st["codes"])
            done = st["done"]
    if codes is None:
        got_codes = ths_api.fetch_gem_codes()
        if not got_codes:
            return {}, False
        with _ths_progress_lock:
            st = _ths_progress.get(key)
            if st is None:
                # 顺手清理旧日期残留 key（当日未完成即永久留存，每个含 ~1400
                # code 列表，防长跑内存累积；2026-08-24 审查卫生项）
                for k in [k for k in _ths_progress if k != key]:
                    _ths_progress.pop(k, None)
                st = {"codes": got_codes, "done": 0, "hits": {}}
                _ths_progress[key] = st
            codes = list(st["codes"])
            done = st["done"]
    # ── 拉取阶段（锁外）：只处理本地快照之后的未完成批次 ──
    deadline = time.time() + FUND_RISK_FETCH_TIMEOUT
    batches = [codes[i:i + _VALUATION_BATCH_SIZE]
               for i in range(0, len(codes), _VALUATION_BATCH_SIZE)]
    new_hits: dict[str, str] = {}
    while done < len(batches) and time.time() < deadline:
        vals = ths_api.fetch_valuations(batches[done])
        if vals is None:
            break  # 本批失败：不推进 done，下轮从该批重试
        for code, pb in vals.items():
            # 防御：pb 为 None/脏值跳过（真实 fetch_valuations 已过滤，双保险）
            if pb is not None and pb < 0:
                sym = _code_to_xq(code)
                if sym:
                    new_hits[sym] = FUND_RISK_REASON
        done += 1
    # ── 提交阶段（锁内）：合并命中、推进 done ──
    with _ths_progress_lock:
        st = _ths_progress.setdefault(key, {"codes": codes, "done": 0, "hits": {}})
        st["hits"].update(new_hits)
        st["done"] = max(st["done"], done)
        merged_hits = dict(st["hits"])
        complete = st["done"] >= len(batches)
        if complete:
            _ths_progress.pop(key, None)  # 定稿后清进度（防长跑内存累积）
    if complete:
        logger.info("财务风险 THS 增量拉取完成：%d 只 GEM / %d 只资不抵债",
                    len(codes), len(merged_hits))
        return merged_hits, True
    return merged_hits, False


def fetch_fund_risk_map() -> dict[str, str]:
    """获取当日资不抵债股集合，返回 {xq_symbol: reason}。失败返回 {}。

    主源 THS 官方估值快照（增量），兜底 pywencai 问财（未安装/失败自动跳过）。
    进程缓存优先：成功结果 FUND_RISK_TTL_SEC（默认一天）内只查一次；
    失败/增量未完成/空结果短退避缓存 FUND_RISK_FAIL_TTL_SEC（一扫描周期）
    后重试——避免故障期每轮扫描都白等。
    """
    cached = _cache_get()
    if cached is not None:
        return cached
    # ── 主源：THS 官方估值快照（增量跨轮）──
    try:
        result, complete = _fetch_fund_risk_ths()
        if result or complete:
            # 部分完成也先返回已命中子集（硬过滤多覆盖好过漏），按短退避缓存
            # 以便下轮续传；完全完成则按结果是否为空选 TTL。
            ttl = (FUND_RISK_TTL_SEC if complete else FUND_RISK_FAIL_TTL_SEC)
            if not result and not complete:
                ttl = FUND_RISK_FAIL_TTL_SEC
            _cache_put(result, ttl=ttl)
            return result
    except Exception as e:  # noqa: BLE001  THS 层任何异常 → 问财兜底
        logger.warning("财务风险 THS 主源异常，降级 pywencai: %s", e)
    # ── 兜底：pywencai 问财（原实现）──
    result: dict[str, str] = {}
    try:
        import pywencai  # noqa: PLC0415
    except Exception:
        _warn_missing_pywencai()
        return result
    try:
        df = _bounded_call(lambda: pywencai.get(query=FUND_RISK_QUERY, loop=True),
                           FUND_RISK_FETCH_TIMEOUT)
        for sym in _extract_xq_symbols(df):
            result[sym] = FUND_RISK_REASON
    except Exception:
        # fail-open + 短退避：查询异常/超时缓存空结果 FUND_RISK_FAIL_TTL_SEC，下轮扫描重试
        _cache_put({}, ttl=FUND_RISK_FAIL_TTL_SEC)
        return {}
    # 成功但空结果（异常，正常应恒有资不抵债股）：同样短退避，避免每轮重复查询
    _cache_put(result, ttl=FUND_RISK_TTL_SEC if result else FUND_RISK_FAIL_TTL_SEC)
    return result


def collect_fund_risk(conn, symbols: list[str]) -> dict[str, str]:
    """收集候选符号的财务风险标记，返回 {xq_symbol: reason}。

    流程：进程缓存 → THS 估值快照增量拉取（主源）→ pywencai 问财（兜底）
    （**读取路径无 DB 回退**，见模块 docstring）。
    命中符号将被打"财务风险"硬过滤标签（orchestrator 组装候选时应用）。
    受 ENABLE_FUND_RISK（RTS_ENABLE_FUND_RISK 环境变量）总开关控制：关闭时直接返回空。
    两源皆不可用 → 返回空 dict（fail-open）。
    """
    if not ENABLE_FUND_RISK:
        return {}
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
