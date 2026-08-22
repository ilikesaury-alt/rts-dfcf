"""盘中分时信号并行拉取层（P2 从 orchestrator.py 抽出，2026-08-21）。

三相（分时强度/开盘强度/实时量比）+ 拉取相共四相并行，每相带 phase_deadline
限时。分时数据统一经 adapter.fetch_minute 拉取一次供各相共用；AKShare 源
（fetch_minute 返回 None）时整体降级为无分时信号。compute 相不发网络请求
（session 传 None，2026-08-21 审查修复）。orchestrator.scan_with_raw 是唯一
生产调用方。
"""

from concurrent.futures import Future, ThreadPoolExecutor, wait

from scanner.api import analyze_intraday, analyze_opening_strength, estimate_live_volume
from scanner.config import MINUTE_FETCH_PHASE_DEADLINE
from scanner.models import Candidate


def parallel_fetch(pool: ThreadPoolExecutor,
                    candidates: list[Candidate],
                    intraday_scores: dict[str, float | None],
                    opening_scores: dict[str, float | None],
                    live_volumes: dict[str, float | None],
                    adapter,
                    phase_deadline: float = MINUTE_FETCH_PHASE_DEADLINE):
    """并行拉取分时强度/开盘强度/实时量比。

    分时数据统一经 adapter.fetch_minute 拉取一次（每 symbol 一条），再并行喂给
    三个评分函数——AKShare 源（fetch_minute 返回 None）时整体降级为无分时信号，
    不再静默回退雪球（此前 api.analyze_* 直接走雪球分钟接口）。
    三相 + 拉取相每相都带 phase_deadline 限时——minute API 挂死时单只请求最坏
    ~48s（15s×3 重试），若用 as_completed 无限等待，40 只候选 6 线程并发可卡死
    扫描循环最长 ~5 分钟。超时的票降级为 None（无分时信号）。
    """
    syms: list[str] = []
    seen: set[str] = set()
    for c_ in candidates:
        sym = c_.stock.symbol
        if sym in seen:
            continue
        seen.add(sym)
        syms.append(sym)
    if not syms:
        return

    # 拉取相：经 adapter 取分时数据（每 symbol 一条，供三相共用）
    items_map: dict[str, list[dict] | None] = {}

    def _fetch_one(s: str, adp) -> list[dict] | None:
        return adp.fetch_minute(s)

    fetch_futs = {pool.submit(_fetch_one, sym, adapter): sym for sym in syms}
    done, pending = wait(fetch_futs, timeout=phase_deadline)
    for fut in pending:
        fut.cancel()
        items_map[fetch_futs[fut]] = None
    for fut in done:
        sym = fetch_futs[fut]
        try:
            items_map[sym] = fut.result()
        except Exception:
            items_map[sym] = None

    def _run_phase(fn, store):
        futs: dict[Future, str] = {}

        def _compute_one(s: str, it: list[dict]):
            # items 路径下三相 compute 不发网络请求（session 仅 items=None 旧路径用）。
            # 传 None 避免每轮新建的工作线程经 thread-local 触发 make_session() 的
            # 阻塞握手（每轮最多 max_workers 次无用 GET，白耗 phase_deadline 预算）。
            return fn(None, s, it)

        for sym in syms:
            items = items_map.get(sym)
            if items is None:
                store[sym] = None  # 无分时数据（AKShare 或拉取失败），整体降级
                continue
            futs[pool.submit(_compute_one, sym, items)] = sym
        if not futs:
            return
        phase_done, phase_pending = wait(futs, timeout=phase_deadline)
        if phase_pending:
            print(f"  [!] 分时数据拉取超时（>{phase_deadline:.0f}s），"
                  f"{len(phase_pending)} 只降级为无分时信号")
            for fut in phase_pending:
                fut.cancel()
                store[futs[fut]] = None
        for fut in phase_done:
            sym = futs[fut]
            try:
                store[sym] = fut.result()
            except Exception:
                store[sym] = None

    _run_phase(analyze_intraday, intraday_scores)
    _run_phase(analyze_opening_strength, opening_scores)
    _run_phase(estimate_live_volume, live_volumes)
