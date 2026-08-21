"""K 线补拉数据层（P2 从 orchestrator.py 抽出，2026-08-21）。

盘中/收盘后的 K 线批量补拉：TTL 节流（_last_kline_fetch）+ KLINE_FETCH_DEADLINE
限时 + 补拉失败回退旧缓存 + 分时今日 bar 兜底（minute_bar.merge_minute_today_bar）。
只依赖通用件（config/database/models/trading_session），不感知扫描流水线，
orchestrator.scan_with_raw 是唯一生产调用方。
"""

import sqlite3
from datetime import date

from scanner.config import (
    CACHE_MAX_ENTRIES,
    KLINE_FETCH_DAYS,
    KLINE_FETCH_DEADLINE,
    KLINE_REFRESH_TTL,
    MINUTE_FALLBACK_PHASE_DEADLINE,
    now_beijing,
)
from scanner.database import get_cached_klines, save_kline_to_db
from scanner.minute_bar import merge_minute_today_bar
from scanner.models import KlineBar, StockInfo
from scanner.trading_session import is_trading_time

# 盘中今日 K 线刷新 TTL（秒）：盘中时段已缓存今日 bar 时，超过该时长仍强制补拉，
# 避免整日复用早盘残次 bar（stock.current 实时价与缓存 close 脱节）。
# 300→120：缩短到 2 分钟，让盘中异动更快反映到 K 线打分（缓解"涨起来了才推"滞后）。
# 常量已收敛至 config.KLINE_REFRESH_TTL（P2-12 单源）。
_last_kline_fetch: dict[str, float] = {}


def fetch_all_klines(conn: sqlite3.Connection, adapter, stocks: list[StockInfo],
                     deadline: float | None = None,
                     stats: dict | None = None) -> dict[str, list[KlineBar] | None]:
    result: dict[str, list[KlineBar] | None] = {}
    needs_fetch: list[str] = []
    stale_cache: dict[str, list[KlineBar]] = {}
    today = now_beijing().date()
    _stats = stats if stats is not None else {}

    # 批量读缓存（单次 SQL，避免对每只票各发一条查询）
    cached_map = get_cached_klines(conn, [s.symbol for s in stocks]) if stocks else {}
    stock_map = {s.symbol: s for s in stocks}
    for s in stocks:
        cached = cached_map.get(s.symbol)
        if cached:
            max_date_str = max(k["date"] for k in cached)
            try:
                max_date = date.fromisoformat(max_date_str)
            except (ValueError, TypeError):
                # 脏日期（非 ISO 格式的历史数据）视为无今日 bar，走补拉路径，
                # 避免 date.fromisoformat 抛 ValueError 拖垮整轮扫描。
                stale_cache[s.symbol] = cached
                needs_fetch.append(s.symbol)
                continue
            last_fetch = _last_kline_fetch.get(s.symbol, 0.0)
            within_ttl = (now_beijing().timestamp() - last_fetch) < KLINE_REFRESH_TTL
            if not is_trading_time():
                # 非交易时段：直接复用缓存，不补拉（含短缓存，避免收盘后反复重拉同一段历史）
                result[s.symbol] = cached
                continue
            # 交易时段
            if max_date < today:
                # 缓存尚未含今日 Bar：必须补拉（否则全天无今日行情）
                stale_cache[s.symbol] = cached
                needs_fetch.append(s.symbol)
                continue
            # 已含今日 Bar：仅当超过刷新 TTL 才补拉，否则复用缓存。
            # 短缓存（len<KLINE_MIN_LENGTH）同样受 TTL 节流：同一交易日内反复重拉
            # 不会增长 K 线根数，只会耗尽 KLINE_FETCH_DEADLINE 拖累全列表，故不再
            # 每轮强制补拉（此前 <32 根的票每扫描周期都重拉一次）。
            if within_ttl:
                result[s.symbol] = cached
                continue
            stale_cache[s.symbol] = cached
        needs_fetch.append(s.symbol)

    if not needs_fetch:
        return result

    # 拉取阶段：串行调 adapter（D4: AKShare 非线程安全，adapter 层统一串行）。
    # 雪球模式性能影响可接受——K 线有 KLINE_REFRESH_TTL=120s 缓存，多数周期命中缓存跳过拉取。
    # P-robust: KLINE_FETCH_DEADLINE 限时——API 故障时单只 15s×3 重试会让串行拉取假死数十分钟，
    # 超时后停止补拉，剩余票回退旧缓存（下方 stale_cache 兜底），保证单轮扫描有界。
    deadline = deadline if deadline is not None else now_beijing().timestamp() + KLINE_FETCH_DEADLINE
    fetched: dict[str, list[KlineBar] | None] = {}
    deadline_skipped = 0
    for i, sym in enumerate(needs_fetch):
        if now_beijing().timestamp() >= deadline:
            # 含当前这只在内，所有尚未拉取的票
            deadline_skipped = len(needs_fetch) - i
            break
        try:
            kline = adapter.fetch_kline(sym, KLINE_FETCH_DAYS)
            _last_kline_fetch[sym] = now_beijing().timestamp()
            if len(_last_kline_fetch) > CACHE_MAX_ENTRIES:
                _last_kline_fetch.pop(next(iter(_last_kline_fetch)))
            fetched[sym] = kline
            if not kline:
                _stats["fetch_failed"] = _stats.get("fetch_failed", 0) + 1
        except Exception as e:
            _stats["fetch_failed"] = _stats.get("fetch_failed", 0) + 1
            print(f"  [!] K线获取失败 {sym}: {e}")
    if deadline_skipped:
        _stats["fetch_failed"] = _stats.get("fetch_failed", 0) + deadline_skipped
        print(f"  [!] K线补拉超时（>{KLINE_FETCH_DEADLINE}s），剩余{deadline_skipped}只回退旧缓存")

    # 写入阶段：主线程顺序写 DB，确保 SQLite 线程安全
    # 分时兜底共享总预算（2026-08-17 审查修复）：两个兜底循环（拉取失败 + deadline 跳过）
    # 串行逐票 join(8s)，API 故障时 N×8s 无总量上限；设共享 deadline 后整个兜底阶段有界。
    fallback_deadline = now_beijing().timestamp() + MINUTE_FALLBACK_PHASE_DEADLINE
    for sym, kline in fetched.items():
        if kline:
            if sym in stale_cache:
                merged = {k["date"]: k for k in stale_cache[sym]}
                for k in kline:
                    merged[k["date"]] = k
                result[sym] = sorted(merged.values(), key=lambda x: x["date"])
            else:
                result[sym] = kline
            try:
                save_kline_to_db(conn, sym, kline)
            except Exception as e:
                print(f"  [!] K线写入DB失败 {sym}: {e}")
        elif sym in stale_cache:
            # 补拉失败回退旧缓存。盘中时尝试用分时构造今日 bar 兜底（2026-08-14）：
            # 缺今日 bar 会让量比硬门误杀放量启动票（网宿案例），构造 bar 仅本轮使用。
            _merged = merge_minute_today_bar(adapter, stock_map.get(sym), today,
                                              stale_cache[sym], deadline=fallback_deadline)
            if _merged is not None:
                _stats["minute_fallback"] = _stats.get("minute_fallback", 0) + 1
            result[sym] = _merged if _merged is not None else stale_cache[sym]

    for sym in needs_fetch:
        if sym not in result and sym in stale_cache:
            # deadline 超时未轮到拉取：同样尝试分时今日 bar 兜底
            _merged = merge_minute_today_bar(adapter, stock_map.get(sym), today,
                                              stale_cache[sym], deadline=fallback_deadline)
            if _merged is not None:
                _stats["minute_fallback"] = _stats.get("minute_fallback", 0) + 1
            result[sym] = _merged if _merged is not None else stale_cache[sym]

    # P1-3: K线数据缺失汇总（首次拉取失败且无 stale_cache 兜底的票）
    missing = [sym for sym in needs_fetch if sym not in result]
    if missing:
        preview = ", ".join(missing[:5])
        suffix = f" 等{len(missing)}只" if len(missing) > 5 else ""
        print(f"  [!] K线数据缺失{len(missing)}只: {preview}{suffix}（已跳过评分，下次刷新重试）")

    # C修复: 盘中已有 K 线但缺今日 bar 的票（静默回退旧缓存会基于昨日数据打分）。
    # 仅交易时段统计——收盘后缺今日 bar 属正常，避免噪音。下次周期 max_date<today 仍会强制补拉。
    # 2026-08-20 拆分：换手率≈0 + 末端非今日的票多为停牌/僵尸股（确无今日 bar 与分时，
    # 告警准确但非故障）→ 降级为 [~] 提示，避免每轮假警；其余（真未知缺数据）保持 [!]。
    if is_trading_time():
        today_bar_missing = [
            sym for sym, kl in result.items()
            if kl and max(k["date"] for k in kl) < today.isoformat()
        ]
        if today_bar_missing:
            halted = [sym for sym in today_bar_missing
                      if stock_map.get(sym) is not None and stock_map[sym].turnover_rate == 0.0]
            genuine = [sym for sym in today_bar_missing if sym not in halted]
            _stats["today_bar_missing"] = _stats.get("today_bar_missing", 0) + len(today_bar_missing)
            if halted:
                preview = ", ".join(halted[:5])
                suffix = f" 等{len(halted)}只" if len(halted) > 5 else ""
                print(f"  [~] 今日K线缺失{len(halted)}只(停牌/僵尸股,换手率0): {preview}{suffix}（旧缓存评分，非故障）")
            if genuine:
                _stats["today_bar_missing_genuine"] = _stats.get("today_bar_missing_genuine", 0) + len(genuine)
                preview = ", ".join(genuine[:5])
                suffix = f" 等{len(genuine)}只" if len(genuine) > 5 else ""
                print(f"  [!] 今日K线缺失{len(genuine)}只(数据异常): {preview}{suffix}（旧缓存评分，下次刷新重试）")

    return result
