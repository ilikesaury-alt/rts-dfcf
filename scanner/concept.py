"""概念板块数据源（东财 F10）+ 驱动概念聚合。

个股所属概念来自东方财富 F10 CoreConception 接口（纯 HTTP GET，无需 cookie）。
「驱动概念」= 从今日飙升池聚合：个股所属概念中，今日飙升成员最多 / 成员涨幅
最强的板块，用于综合排序「板块」列展示"当前推动票上涨的概念或板块"。

概念归属低频变动，DB 缓存（concept_cache 表）按 CONCEPT_CACHE_TTL_DAYS 天复用；
进程内再叠加短 TTL 缓存，避免同一轮扫描重复读 DB。
本模块只影响展示，不参与任何打分逻辑。
"""
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from scanner import api
from scanner.config import (
    CONCEPT_API_TIMEOUT,
    CONCEPT_MAX_FETCH_THREADS,
    CONCEPT_NOISE_BOARDS,
    CONCEPT_NOISE_BOARD_SUFFIXES,
    CONCEPT_CACHE_TTL_DAYS,
    CACHE_MAX_ENTRIES,
)
from scanner.database import get_concepts_cache, save_concepts_cache
from scanner.sector import classify_sector

logger = logging.getLogger(__name__)

_EM_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://emweb.securities.eastmoney.com/",
}

# 进程内缓存：symbol → (concepts, fetched_ts)，跨 scan 复用、避免同轮重复查 DB
_concept_ttl_cache: dict[str, tuple[list[str], float]] = {}
_concept_lock = threading.Lock()
_CONCEPT_PROCESS_TTL = 300  # 5 分钟


def _cache_put(cache: dict, key, value):
    """带上限写入（调用方持有 _concept_lock）：超限淘汰最旧，防长跑内存膨胀。"""
    if key in cache:
        cache.pop(key)
    cache[key] = value
    while len(cache) > CACHE_MAX_ENTRIES:
        cache.pop(next(iter(cache)))


def _is_noise_board(name: str) -> bool:
    """判断是否为噪音板块（地域/风格/指数成分/涨停梯队等，不反映推动逻辑）。"""
    if name in CONCEPT_NOISE_BOARDS:
        return True
    # 指数成分/风格类板块常以数字结尾（深成500/中证800/央视50），真实概念板块不会
    if name and name[-1].isdigit():
        return True
    for suffix in CONCEPT_NOISE_BOARD_SUFFIXES:
        if name.endswith(suffix):
            return True
    return False


def fetch_stock_boards(symbol: str) -> list[str]:
    """拉取个股概念归属（东财 F10）。失败或全部为噪音时返回 []。"""
    try:
        api._throttle()
    except Exception:
        pass
    url = f"https://emweb.securities.eastmoney.com/PC_HSF10/CoreConception/PageAjax?code={symbol}"
    try:
        resp = requests.get(url, headers=_EM_HEADERS, timeout=CONCEPT_API_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning("概念拉取失败 %s: %s", symbol, e)
        return []
    boards: list[str] = []
    seen: set[str] = set()
    for item in data.get("ssbk", []) or []:
        name = item.get("BOARD_NAME", "")
        if not name or name in seen:
            continue
        seen.add(name)
        if _is_noise_board(name):
            continue
        boards.append(name)
    return boards


def _fetch_many(symbols: list[str]) -> dict[str, list[str]]:
    """并行拉取多只股票的概念归属（线程池 + 节流）。"""
    result: dict[str, list[str]] = {}
    if not symbols:
        return result
    with ThreadPoolExecutor(max_workers=CONCEPT_MAX_FETCH_THREADS) as pool:
        futs = {pool.submit(fetch_stock_boards, s): s for s in symbols}
        for fut in as_completed(futs):
            sym = futs[fut]
            try:
                boards = fut.result()
                if boards:
                    result[sym] = boards
            except Exception as e:
                logger.warning("概念拉取异常 %s: %s", sym, e)
    return result


def _collect_concepts(conn, symbols: list[str]) -> dict[str, list[str]]:
    """收集符号的概念归属：进程缓存 → DB 缓存 → 并行拉取缺失并落库。

    返回 {symbol: [概念名]}。只包含成功拿到且过滤噪音后的结果。
    """
    now = time.time()
    result: dict[str, list[str]] = {}
    missing: list[str] = []
    for sym in set(symbols):
        with _concept_lock:
            hit = _concept_ttl_cache.get(sym)
            if hit and now - hit[1] < _CONCEPT_PROCESS_TTL:
                result[sym] = hit[0]
                continue
        missing.append(sym)

    if not missing:
        return result

    db = get_concepts_cache(conn, missing, CONCEPT_CACHE_TTL_DAYS)
    still_missing: list[str] = []
    for sym in missing:
        if sym in db and db[sym]:
            result[sym] = db[sym]
            with _concept_lock:
                _cache_put(_concept_ttl_cache, sym, (db[sym], now))
        else:
            still_missing.append(sym)

    if still_missing:
        fetched = _fetch_many(still_missing)
        save_concepts_cache(conn, fetched)
        for sym, boards in fetched.items():
            result[sym] = boards
            with _concept_lock:
                _cache_put(_concept_ttl_cache, sym, (boards, now))
    return result


def _driving_for(sym: str, concepts_map: dict[str, list[str]],
                 board_members: dict[str, list[float]]) -> str:
    """选个股的「推动概念」：今日飙升成员最多且成员涨幅最强的概念。

    评分 = 成员数 × (1 + 平均涨幅/10)，同时奖励"参与度"与"上涨强度"。
    若所属概念今日均无飙升成员（票不在池 / 板块无共振），回退到 F10 首要板块，
    避免退化成"其他"（首要板块 = 东财 F10 顺序首个非噪音板块，通常是行业归属）。
    """
    boards = concepts_map.get(sym)
    if not boards:
        return ""
    best = ""
    best_score = -1.0
    for b in boards:
        members = board_members.get(b)
        if not members:
            continue
        avg = sum(members) / len(members)
        score = len(members) * (1 + avg / 10.0)
        if score > best_score:
            best = b
            best_score = score
    return best or boards[0]


def compute_driving_concepts(conn, symbols: list[str], surge_pool: list) -> dict[str, str]:
    """计算每只 symbol 的「当前推动概念」。

    surge_pool: 今日飙升池（含 .symbol / .percent），用于聚合每个概念今日的推动强度。
    返回 {symbol: 概念名}；无可用概念时回退 classify_sector(名称)，仍为空则 "其他"。
    """
    pool_by_sym = {s.symbol: s for s in surge_pool}
    # 驱动计数需要全飙升池的概念归属，不只候选
    all_syms = set(symbols) | set(pool_by_sym.keys())
    concepts_map = _collect_concepts(conn, all_syms)

    # board → 今日飙升成员涨幅列表
    board_members: dict[str, list[float]] = {}
    for sym, s in pool_by_sym.items():
        for board in concepts_map.get(sym, []):
            board_members.setdefault(board, []).append(s.percent or 0.0)

    result: dict[str, str] = {}
    for sym in symbols:
        s = pool_by_sym.get(sym)
        driving = _driving_for(sym, concepts_map, board_members)
        if not driving and s is not None:
            driving = classify_sector(s.name)
        result[sym] = driving or "其他"
    return result
