import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace as dataclass_replace

from scanner.api import compute_surge_sentiment
from scanner.candidate_pool import ScanSession
from scanner.candidates import (
    candidate_excluded_by_risk,
    compute_rps,
    enrich_candidate_market_cap,
    filter_gem_stocks,
    new_face_sort_key,
    score_stock,
)
from scanner.comeback import evaluate_comeback
from scanner.concept import compute_driving_concepts
from scanner.config import (
    COMEBACK_KLINE_DEADLINE,
    ENABLE_COMEBACK,
    ENABLE_CORE_DIP,
    ENABLE_MOMENTUM,
    ENABLE_POOL_PIPELINE,
    ENABLE_REDESIGN_PICK,
    ENABLE_SHORT_TERM,
    KLINE_FETCH_DEADLINE,
    MAX_MARKET_CAP,
    MAX_STOCK_PRICE,
    MCAP_CACHE_MAX_AGE_DAYS,
    REDESIGN_POOL_WIDTH_MIN,
    SHORT_TERM_MAX_TODAY_PCT,
    WATCH_OFFLIST_KEEP_DAYS,
    YI,
    now_beijing,
)
from scanner.database import (
    get_cached_market_caps,
    get_prev_ranks,
    prune_watch_pool,
    record_appearances,
    save_market_caps,
    save_market_index_log,
    save_pool_log,
    save_rejections,
    save_scan_quality,
    upsert_watch_symbols,
)
from scanner.enhancer import (
    accumulate_final_score,
    apply_all_bonuses,
    compute_time_bonus,
)
from scanner.intraday_fetch import parallel_fetch
from scanner.intraday_tactics import stock_actions
from scanner.kline_fetch import fetch_all_klines
from scanner.models import Candidate, KlineSummary, ScanResult, StockInfo
from scanner.rank_trend import update_rank_history
from scanner.ranking import comeback_sort_key
from scanner.sector import get_sector_clusters
from scanner.trading_session import is_trading_time
from scanner.utils import EXTERNAL_FAILURES

# fail-open 异常策略（2026-08-29）：本模块所有降级分支只捕获 EXTERNAL_FAILURES
# （OSError/超时/requests/DB 运行期错误/脏值 ValueError/响应结构 KeyError），
# 不再用 `except Exception`。此前宽泛捕获会把 NameError/AttributeError/TypeError
# 这类「我们自己的 bug」和外部依赖故障一视同仁地吞成一行 print——本轮扫描静默
# 丢掉整个策略分支都无从察觉。收窄后编程错误直接冒泡到 unified_scanner 主循环的
# 兜底（记录完整 traceback 后下一轮重试），数据故障仍按设计软降级。
_session_state = ScanSession()


def _update_excluded_marks(conn: sqlite3.Connection, today: str, excluded_by_risk: list, all_candidates: list) -> None:
    """硬过滤落标 + 通过候选置回（P1-7，2026-08-24 第二轮审查抽函数补类别守卫）。

    置回按候选自身类别精确匹配——旧实现按 (date,symbol) 全量置 0 会"复活"同
    symbol 其它类别的旧行：票上午 short_term 推荐 → 回落≥10% 被 mark_reversed
    置 excluded=1 → 尾盘掉榜进回马枪回踩候选 → 全量置回让已判定"不敢买"的
    short_term 行重新进综合排序主表。mark_reversed 侧有
    NOT IN ('comeback','core_dip') 守卫，置回侧同类防线。
    """
    if excluded_by_risk:
        conn.executemany(
            "UPDATE recommendations SET excluded=1, excluded_reason=? WHERE date=? AND symbol=?",
            [(c.excluded_reason, today, c.stock.symbol) for c in excluded_by_risk],
        )
    passed_syms = [(today, c.stock.symbol, c.category) for c in all_candidates]
    if passed_syms:
        conn.executemany(
            "UPDATE recommendations SET excluded=0, excluded_reason='' WHERE date=? AND symbol=? AND category=?",
            passed_syms,
        )
    conn.commit()


def _v2_kline_summary(row, kl: list | None, today: str) -> KlineSummary:
    """v2 池选候选的轻量 KlineSummary。

    此前池选候选 kline=None，导致：matcher 语义标签全跳过（label_all_candidates
    对 kline=None 直接 continue）、enhancer 资金流/连板/疲劳等维度不写入、
    minute_trends 不落 dims——v2 的标签与展示维度整体失效。此构造补齐最小字段集：
    - accumulated_pct：历史 5 日累计（排除今日，与 v1 各桶口径一致）；
    - volume_ratio/avg_volume：今日量 / 前 5 日均量（matcher 放量突破、疲劳判定消费）；
    - dimensions：accumulated_incl_today（🎯 含今日口径）/ bias20 / rank_trend
      （matcher 放量突破标签读 rank_trend 维度）。
    """
    hist = [k for k in (kl or []) if k.get("date") != today]
    closes = [k["close"] for k in hist if k.get("close")]
    vols = [k["volume"] for k in hist if k.get("volume")]
    avg_volume = (sum(vols[-5:]) / len(vols[-5:])) if vols else 0.0
    today_bar = kl[-1] if kl and kl[-1].get("date") == today else None
    volume_ratio = 0.0
    if today_bar and avg_volume > 0:
        volume_ratio = (today_bar.get("volume") or 0.0) / avg_volume
    accum = None
    if len(closes) >= 6 and closes[-6] > 0:
        accum = (closes[-1] - closes[-6]) / closes[-6] * 100.0
    dims: dict[str, object] = {"rank_trend": row.rank_trend}
    if row.acc5 is not None:
        dims["accumulated_incl_today"] = row.acc5
    if row.bias20 is not None:
        dims["bias20"] = round(row.bias20, 2)
    if accum is not None:
        dims["accumulated_5d"] = round(accum, 2)
    if row.acc5 is not None and row.acc5 >= 15:
        trend = "强势"
    elif row.acc5 is not None and row.acc5 <= -10:
        trend = "超跌"
    else:
        trend = "整理"
    return KlineSummary(
        trend=trend,
        accumulated_pct=round(accum, 2) if accum is not None else 0.0,
        volume_ratio=round(volume_ratio, 2),
        bottom_confirmed=False,
        score=0,
        dimensions=dims,
        avg_volume=avg_volume,
    )


def scan_with_raw(raw: list[dict], conn: sqlite3.Connection, adapter) -> ScanResult:
    global _session_state
    session_state = _session_state
    today = now_beijing().date().isoformat()
    session_state.reset_if_new_day(today)

    sentiment_info = compute_surge_sentiment(raw)
    gem_stocks = filter_gem_stocks(raw)

    record_appearances(
        conn,
        [
            {"symbol": s.symbol, "name": s.name, "percent": s.percent, "value": s.value, "rank": s.rank}
            for s in gem_stocks
        ],
    )
    session_state.update_list_presence({s.symbol for s in gem_stocks})

    stale_syms = [sym for sym, c in session_state.today_pool.items() if c.is_stale]
    mc_syms = list({s.symbol for s in gem_stocks} | set(stale_syms))
    market_caps = adapter.fetch_market_caps_batch(mc_syms) if mc_syms else {}
    # 市值缓存兜底（2026-08-20）：批量查询全失败时回退陈旧缓存，避免 小而美 规则
    # 整轮静默失效。fetch 成功即落库；全失败按"盘中限当日、非交易放宽到 N 天"取陈旧值。
    used_stale_mc = False
    if market_caps:
        save_market_caps(conn, market_caps, source=adapter.name if hasattr(adapter, "name") else "xueqiu")
    else:
        max_age = 0 if is_trading_time() else MCAP_CACHE_MAX_AGE_DAYS
        market_caps = get_cached_market_caps(conn, mc_syms, max_age_days=max_age)
        if market_caps:
            used_stale_mc = True

    gem_stocks_filtered: list[StockInfo] = []
    filtered_large_cap = 0
    for s in gem_stocks:
        cap_data = market_caps.get(s.symbol, {})
        cap_current = cap_data.get("current", 0)
        if cap_current and s.current == 0:
            s.current = cap_current
        cmc = cap_data.get("circ_market_cap") or cap_data.get("market_cap", 0)
        if cmc > 0:
            s.market_cap = cmc / YI  # 转亿元（流通市值优先）
        if s.current > 0 and s.current > MAX_STOCK_PRICE:
            continue
        mc = cap_data.get("market_cap", 0)
        if mc > 0 and mc > MAX_MARKET_CAP:
            filtered_large_cap += 1
            continue
        gem_stocks_filtered.append(s)

    # 市值数据可用性最终判定（2026-08-20）：
    # - 实时取到 → 正常（已落库）。
    # - 全失败但陈旧缓存兜底命中 → 降级提示（非 [!]），小叶美规则仍生效（基于旧值）。
    # - 全失败且无任何陈旧缓存 → 真正 [!] 告警"小叶美规则暂不生效"。
    if used_stale_mc:
        print(f"  [~] 市值实时查询失败，已回退陈旧缓存({len(market_caps)}只)——小叶美规则基于旧市值生效")
    elif not market_caps and mc_syms:
        print("  [!] 警告: 市值数据全失败且无陈旧缓存，小而美规则暂不生效")

    # 回马枪掉榜跟踪池维护（2026-08-07）：
    # 1) 在榜 GEM 票保活（刷新 last_list_date，掉榜后保留 WATCH_OFFLIST_KEEP_DAYS 个交易日）
    # 2) 超限启动票（今日涨幅 > short_term 上限，强得没法买）置 over_limit=1 持续盯防
    # 3) 剪枝过旧条目
    try:
        upsert_watch_symbols(
            conn,
            [{"symbol": s.symbol, "name": s.name, "last_list_date": today} for s in gem_stocks_filtered]
            + [
                {"symbol": s.symbol, "name": s.name, "last_list_date": today, "over_limit": True}
                for s in gem_stocks_filtered
                if s.percent > SHORT_TERM_MAX_TODAY_PCT
            ],
        )
        prune_watch_pool(conn, WATCH_OFFLIST_KEEP_DAYS)
    except EXTERNAL_FAILURES as e:
        print(f"  [!] 掉榜跟踪池维护失败: {e}")

    # 主榜 K 线拉取 deadline（45s）。回马枪使用独立 deadline（COMEBACK_KLINE_DEADLINE=15s），
    # 不再共用此 deadline，避免主榜耗尽预算后回马枪全部 stale 缓存。
    kline_deadline = now_beijing().timestamp() + KLINE_FETCH_DEADLINE
    quality_stats: dict = {}
    klines = fetch_all_klines(conn, adapter, gem_stocks_filtered, deadline=kline_deadline, stats=quality_stats)

    clusters = get_sector_clusters(gem_stocks_filtered)

    new_faces: list[Candidate] = []
    momentum: list[Candidate] = []
    rebound_list: list[Candidate] = []
    short_term_list: list[Candidate] = []

    # ── 双跑模式（2026-09-02）：v1 五桶 + v2 池管道无条件都执行，共享同一份榜单/
    # K线/资金流数据，两套候选合并进 all_candidates 走同一套下游（加分/硬过滤/落库）。
    # 显示层同屏双区输出（v1 主表 + v2 池选区，见 display.build_scan_view）；落库按
    # (date, symbol, category) 并存。RTS_PIPELINE 不再切换行为；单独关闭 v2 管道
    # （回滚杠杆）用 RTS_ENABLE_POOL=0。
    from scanner.danger import (  # 二次排雷在 gate 外引用，需无条件导入
        danger_flags_json,
        evaluate_pool,
        hard_flags,
        soft_flags,
    )
    from scanner.models import V2_CATEGORY  # 末尾 pool_picks 重建需要（gate 关闭时也执行）

    pool_rows: list = []
    danger_map: dict = {}
    danger_syms: set[str] = set()
    pool_log_rows: list[dict] = []
    pool_picks: list[Candidate] = []
    if ENABLE_POOL_PIPELINE:
        # v2 池管道：pool → danger → 候选构建（score 恒 0，matcher 只标注不淘汰）
        from scanner.matcher import label_all_candidates
        from scanner.pool import build_pool

        prev_ranks = get_prev_ranks(conn, today)
        pool_rows = build_pool(gem_stocks_filtered, klines, today, prev_ranks)
        danger_map = evaluate_pool(pool_rows, klines, {}, {})

        danger_syms = {sym for sym, flags in danger_map.items() if hard_flags(flags)}
        danger_count = len(danger_syms)
        if danger_count:
            print(f"  [排雷] {danger_count} 只命中硬危险信号，已排除")

        # 从安全池构建 Candidate
        stock_by_sym = {s.symbol: s for s in gem_stocks_filtered}
        for row in pool_rows:
            if row.symbol in danger_syms:
                continue
            stock = stock_by_sym.get(row.symbol)
            if not stock:
                continue
            c = Candidate(
                stock=stock,
                category=V2_CATEGORY,
                score=0,
                reason="池选",
                kline=_v2_kline_summary(row, klines.get(row.symbol), today),
                first_seen=now_beijing().strftime("%H:%M"),
                history_pct=[],
            )
            # 软排雷信号（DANGER_KLINE_SOFT）：K 线动量类不剔除，进 risk_flags 供
            # 行尾 ⚠+N 展示与 pool_log 审计（回测：剔的恰是次日 hit7 更高的强势票）。
            c.risk_flags.extend(soft_flags(danger_map.get(row.symbol, [])))
            pool_picks.append(c)

        # 语义标签（不淘汰）
        label_all_candidates(pool_picks, klines, today)

        # pool_log 行先构建，落库延迟到二次排雷之后（danger_flags 补全资金流/财务信号）
        pool_log_rows = [
            {
                "date": today,
                "symbol": r.symbol,
                "name": r.name,
                "percent": r.percent,
                "rank": r.rank,
                "rank_trend": r.rank_trend,
                "bias20": r.bias20,
                "acc5": r.acc5,
                "on_board": r.on_board,
                "market_cap": r.market_cap,
                "danger_flags": danger_flags_json(danger_map.get(r.symbol, [])),
                "v1_passed": False,
            }
            for r in pool_rows
        ]

    # v1 五桶评分（双跑：与 v2 消费同一份 klines，口径与历史 v1 完全一致）
    for stock in gem_stocks_filtered:
        nf, mo, rb, st = score_stock(stock, conn, klines, today, session_state, clusters)
        if nf:
            new_faces.append(nf)
        if mo and ENABLE_MOMENTUM:
            momentum.append(mo)
        if rb:
            rebound_list.append(rb)
        if st and ENABLE_SHORT_TERM:
            short_term_list.append(st)

    all_candidates = pool_picks + new_faces + momentum + rebound_list + short_term_list

    # 回马枪：评估掉榜跟踪池 + 近 N 日推荐（两变体均 category="comeback"）。
    # 开关关闭时不评估（hit 3.3% 远低于基准，不再作为活跃推荐桶产出）。
    comeback_rebound: list[Candidate] = []
    comeback_reentry: list[Candidate] = []
    if ENABLE_COMEBACK:
        try:
            on_list_symbols = {s.symbol for s in gem_stocks_filtered}
            comeback_rebound, comeback_reentry, cb_quotes = evaluate_comeback(
                conn,
                adapter,
                lambda stocks: fetch_all_klines(
                    conn,
                    adapter,
                    stocks,
                    deadline=now_beijing().timestamp() + COMEBACK_KLINE_DEADLINE,
                    stats=quality_stats,
                ),
                today,
                on_list_symbols,
                clusters,
            )
            market_caps.update(cb_quotes)  # 并入市值/行情，供后续市值富集与实时行情
        except EXTERNAL_FAILURES as e:
            print(f"  [!] 回马枪评估失败: {type(e).__name__}: {e}")

    # 双跑：v1 五桶 + pool_picks 已在上方合并，回马枪对两管道统一追加
    all_candidates = all_candidates + comeback_rebound + comeback_reentry
    for c in all_candidates:
        enrich_candidate_market_cap(c, market_caps.get(c.stock.symbol, {}))

    # 行情增强数据（涨停池 + 个股资金流）：全市场各 1 次请求，失败软降级为空。
    # 必须在 apply_all_bonuses 前收集，供资金流/连板加分与风险标签使用。
    market_extra: dict = {}
    try:
        from scanner.market_extra import collect_market_extra

        market_extra = collect_market_extra(conn, [c.stock.symbol for c in all_candidates])
    except EXTERNAL_FAILURES as e:
        print(f"  [!] 行情增强数据收集失败（忽略，不影响扫描）: {e}")

    # 基本面风险（pywencai 问财反向查询资不抵债股）：排除式过滤器，命中候选打
    # "财务风险"硬过滤标签（RISK_FLAGS_HARD_FILTER 移出推荐列表），不做任何加分。
    # 全程 fail-open：问财未安装/超时/异常 → 空 dict，不影响扫描。
    fund_risk: dict[str, str] = {}
    try:
        from scanner.fundamentals import collect_fund_risk

        fund_risk = collect_fund_risk(conn, [c.stock.symbol for c in all_candidates])
        if fund_risk:
            names = "、".join(
                f"{c.stock.name}({c.stock.symbol})" for c in all_candidates if c.stock.symbol in fund_risk
            )
            print(f"  [财务风险] {len(fund_risk)} 只资不抵债（退市风险级），将移出推荐：{names}")
    except EXTERNAL_FAILURES as e:
        print(f"  [!] 基本面风险收集失败（忽略，不影响扫描）: {e}")

    # v2 二次排雷（2026-09-01 审查修复）：首轮 evaluate_pool 时 market_extra/fund_risk
    # 尚未收集（传空 dict），主力出货（净流出≤-5%）与财务风险两个信号恒不触发。
    # 现在两类数据已就绪，补一轮带全量数据的排雷：新命中的票从候选中剔除，
    # 并把合并后的 danger_flags 补进 pool_log 落库（首轮只含 bias20/冲高回落/翻绿）。
    # 双跑语义（2026-09-02）：只作用于 v2 域（pool_pick + comeback）——v1 五桶保持
    # 自身 validator/硬过滤口径（历史上该块仅在 v2 模式运行，从未移除过 v1 候选）。
    if pool_log_rows:
        try:
            late_map = evaluate_pool(pool_rows, klines, market_extra, fund_risk)
            merged_flags: dict[str, list[str]] = {}
            for _sym in set(danger_map) | set(late_map):
                merged_flags[_sym] = list(dict.fromkeys((danger_map.get(_sym) or []) + (late_map.get(_sym) or [])))
            new_danger_syms = {sym for sym, fl in merged_flags.items() if hard_flags(fl)} - danger_syms
            if new_danger_syms:
                _names = "、".join(
                    f"{c.stock.name}({c.stock.symbol})"
                    for c in all_candidates
                    if c.stock.symbol in new_danger_syms and c.category in (V2_CATEGORY, "comeback")
                )
                print(f"  [排雷] {len(new_danger_syms)} 只命中资金流/财务危险信号，已排除：{_names}")
                all_candidates = [
                    c
                    for c in all_candidates
                    if c.stock.symbol not in new_danger_syms or c.category not in (V2_CATEGORY, "comeback")
                ]
            for _r in pool_log_rows:
                _r["danger_flags"] = danger_flags_json(merged_flags.get(str(_r["symbol"]), []))
            save_pool_log(conn, pool_log_rows)
            # 存留的 v2 候选补挂二轮新命中的软信号（硬信号已被上面剔除，不会走到这里）
            for c in all_candidates:
                if c.category not in (V2_CATEGORY, "comeback"):
                    continue
                for f in soft_flags(merged_flags.get(c.stock.symbol, [])):
                    if f not in c.risk_flags:
                        c.risk_flags.append(f)
        except EXTERNAL_FAILURES as e:
            print(f"  [!] v2 二次排雷/pool_log 落库失败: {e}")

    rps_scores: dict[str, int] = {}
    # RPS 基准：全 GEM 监控集（过滤后、含未入选候选）的 5 日累计涨幅列表，
    # 使 RPS 表达「相对全市场强弱」而非仅在已涨票中比谁涨得多。
    # 口径统一为"历史5日累计"（排除今日），与 new_face/momentum/rebound
    # 的 c.kline.accumulated_pct 一致。short_term 的 accumulated 包含今日
    # （策略语义），需通过 accum_map 覆盖为历史口径，避免百分位偏高。
    rps_baseline: list[float] = []
    accum_map: dict[str, float] = {}  # symbol → 历史5日累计涨幅（排除今日）
    for s in gem_stocks_filtered:
        kl = klines.get(s.symbol)
        if not kl:
            continue
        hist = [k for k in kl if k["date"] != today]
        closes = [k["close"] for k in hist]
        if len(closes) >= 6:
            acc = (closes[-1] - closes[-6]) / closes[-6] * 100
            rps_baseline.append(acc)
    # 为所有候选构建历史口径 accumulated（与 baseline 一致）
    for c in all_candidates:
        kl = klines.get(c.stock.symbol)
        if not kl:
            continue
        hist = [k for k in kl if k["date"] != today]
        closes = [k["close"] for k in hist]
        if len(closes) >= 6:
            accum_map[c.stock.symbol] = (closes[-1] - closes[-6]) / closes[-6] * 100
    rps_scores.update(compute_rps(all_candidates, baseline=rps_baseline, accum_map=accum_map))

    intraday_scores: dict[str, float | None] = {}
    opening_scores: dict[str, float | None] = {}
    live_volumes: dict[str, float | None] = {}
    minute_trends: dict[str, dict | None] = {}

    if all_candidates:
        # wait=False 关闭：_parallel_fetch 各相已有 phase_deadline 限时，超时被
        # cancel 的任务仍在后台跑（受请求自身超时约束，最坏 ~48s 后自然结束），
        # 不能让 with-exit 的 shutdown(wait=True) 阻塞主扫描循环等待它们。
        pool = ThreadPoolExecutor(max_workers=6)
        try:
            parallel_fetch(
                pool,
                all_candidates,
                intraday_scores,
                opening_scores,
                live_volumes,
                adapter,
                minute_trends=minute_trends,
            )
        finally:
            pool.shutdown(wait=False)

    # 分时趋势摘要写入候选维度（盘中操作纪律 rule 3/5/7 数据源，纯展示不参与评分）
    for c in all_candidates:
        trend = minute_trends.get(c.stock.symbol)
        if c.kline and trend:
            c.kline.dimensions.update(
                {
                    "minute_steady_rise": trend.get("steady_rise_ratio", 0.0),
                    "minute_day_high": trend.get("day_high_pct", 0.0),
                    "minute_am_high": trend.get("am_high_pct", 0.0),
                    "minute_vol_trend": trend.get("vol_trend", 1.0),
                }
            )

    market_idx_pct = adapter.fetch_market_index()
    time_bonus = compute_time_bonus()

    # 大盘指数血缘日志（2026-08-19）：把本轮实际使用的大盘涨幅 + 其 bar 日期落库，
    # 供 data_health.check_market_index_health 对账。大盘标签曾把当日 -6.26% 崩盘读成
    # 昨日 -0.93%（展示"大盘中性"）而无痕——涨幅不落库就永远无法审计"当时读到了什么"。
    try:
        _idx_pct, _idx_bar, _idx_src = adapter.get_market_index_meta()
        if market_idx_pct is not None:
            _idx_pct = market_idx_pct  # 以实际使用值为准（兜底路径 meta 可能滞后）
        save_market_index_log(conn, _idx_pct, _idx_bar, _idx_src or "xueqiu")
    except EXTERNAL_FAILURES as e:
        print(f"  [!] 大盘指数血缘日志落库失败: {e}")

    apply_all_bonuses(
        all_candidates,
        gem_stocks_filtered,
        intraday_scores,
        opening_scores,
        live_volumes,
        market_caps,
        clusters,
        market_idx_pct,
        time_bonus,
        sentiment_info=sentiment_info,
        rps_scores=rps_scores,
        list_streaks=session_state.list_presence,
        market_extra=market_extra,
        fund_risk=fund_risk,
        conn=conn,
    )

    # 双挂候选（首板票同时挂 new_face + short_term）需各自独立计算 extra：
    # accumulate_final_score 依赖 c.gap_up_bonus / c.list_momentum_bonus 等，
    # 这些 bonus 在 apply_all_bonuses 中按 candidate 独立计算（如 _apply_gap_up_bonus
    # 依据 c.category 选 key，_apply_list_momentum_bonus 依据 c.category 判 is_reversal）。
    # 若复用同一 extra，short_term 桶会拿到 new_face 桶的 bonus，排名错位。
    for i, c in enumerate(all_candidates):
        extra = accumulate_final_score(c, opening_scores)
        all_candidates[i] = dataclass_replace(c, score=c.score + extra)

    update_rank_history({s.symbol: s.rank for s in gem_stocks_filtered})

    session_state.update_pool(all_candidates)

    # 风险硬过滤：命中"卖出/止损"级标签（主力出货/趋势破位）的候选直接移出推荐列表。
    # 此步在 update_pool/update_stale 之后执行，不影响候选池掉榜与排名历史，
    # 仅作用于最终对外展示的推荐列表，确保推荐输出只含可买票。
    excluded_by_risk = [c for c in all_candidates if candidate_excluded_by_risk(c)]
    if excluded_by_risk:
        _names = "、".join(f"{c.stock.name}({c.stock.symbol})" for c in excluded_by_risk[:8])
        _more = f" 等{len(excluded_by_risk)}只" if len(excluded_by_risk) > 8 else ""
        print(f"  [风险过滤] {len(excluded_by_risk)} 只命中硬排除标签，已移出推荐：{_names}{_more}")
    all_candidates = [c for c in all_candidates if not candidate_excluded_by_risk(c)]

    # === 重新设计 L0/L1 真过滤（2026-09-03 落地）===
    # 不过关候选从 all_candidates 移除（不进 ScanResult → 不落库 → 不展示 → 不推飞书），
    # 并标 excluded=1 + scan_rejections 留痕；L0 池窄则当日整体空仓（撤销全部推荐）。
    # 复用风险过滤同款排除模式；fail-open：gate 异常则当轮不拦截，回退原逻辑。
    if ENABLE_REDESIGN_PICK:
        try:
            from scanner.redesign_gate import apply_redesign_gate

            redesign_blocked, l0_closed, r5 = apply_redesign_gate(all_candidates, conn, today)
        except EXTERNAL_FAILURES as e:
            print(f"  [!] 重新设计 gate 失败（跳过，保持原逻辑）: {type(e).__name__}: {e}")
            redesign_blocked, l0_closed, r5 = [], False, 0
        if l0_closed:
            print(
                f"  [L0 市场闸门·池窄] 今日 R5 合格 {r5} 只 "
                f"< {REDESIGN_POOL_WIDTH_MIN}，整体空仓（撤销全部推荐）"
            )
            all_candidates = []
        elif redesign_blocked:
            _blocked_keys = {(c.stock.symbol, c.category) for c in redesign_blocked}
            all_candidates = [
                c for c in all_candidates if (c.stock.symbol, c.category) not in _blocked_keys
            ]
            try:
                save_rejections(conn, redesign_blocked, today)
            except EXTERNAL_FAILURES as e:
                print(f"  [!] 重新设计拒绝落痕失败: {type(e).__name__}: {e}")

    # P1-7 (2026-08-10): 硬过滤落标——被过滤的今日推荐标记 excluded=1（综合排序不再展示），
    # 通过硬过滤的候选置 0（同日风险标签可能随时间变化，以最新轮次为准）。
    try:
        _update_excluded_marks(conn, today, excluded_by_risk, all_candidates)
        # 硬过滤审计（2026-08-30）：被杀候选此前完全不落库，当日首次成为候选即被
        # 过滤的票连一行都没有 → 无法统计「被杀票次日收益」，硬过滤有效性不可验证。
        # 独立表 scan_rejections 与 recommendations 隔离，不污染回测样本。
        save_rejections(conn, excluded_by_risk, today)
    except EXTERNAL_FAILURES as e:
        print(f"  [!] 风险过滤落标失败: {e}")

    # 分类列表必须从 all_candidates 重建（v1 路径）

    # 分类列表必须从 all_candidates 重建，而非沿用旧对象引用——
    # dataclass_replace 已创建新对象（含最终 score），
    # 旧列表持有的仍是未累加 extra 的过期对象。
    new_faces = [c for c in all_candidates if c.category in ("new_face", "known_new_face")]
    momentum = [c for c in all_candidates if c.category == "momentum"]
    rebound_list = [c for c in all_candidates if c.category == "rebound"]
    short_term_list = [c for c in all_candidates if c.category == "short_term"]
    comeback_list = [c for c in all_candidates if c.category == "comeback"]
    new_faces.sort(key=lambda c: new_face_sort_key(c))
    momentum.sort(key=lambda c: -c.score)
    rebound_list.sort(key=lambda c: -c.score)
    short_term_list.sort(key=lambda c: -c.score)
    # 回马枪区内排序与 display/today_report 单源（ranking.comeback_sort_key，资金流
    # 优先）——此前按 score 降序，飞书卡片与终端两种顺序（2026-08-24 审查）。
    # 候选行经 _candidate 读 kline.dimensions，无需 flow_map 回退。
    comeback_list.sort(key=lambda c: comeback_sort_key({"symbol": c.stock.symbol, "score": c.score, "_candidate": c}))

    # 综合排序「板块」列：计算当前推动概念（东财 F10 概念归属 + 今日飙升池聚合）。
    # 仅影响展示，不参与任何打分。首次拉取缺失缓存，之后 DB/进程缓存零网络开销。
    try:
        driving_map = compute_driving_concepts(
            conn,
            [c.stock.symbol for c in all_candidates],
            gem_stocks_filtered,
        )
        for c in all_candidates:
            c.driving_concept = driving_map.get(c.stock.symbol, "")
    except EXTERNAL_FAILURES as e:
        print(f"  [!] 驱动概念计算失败: {type(e).__name__}: {e}")

    current_quotes = {
        sym: {"percent": d.get("percent", 0.0), "current": d.get("current", 0.0), "high_pct": d.get("high_pct")}
        for sym, d in market_caps.items()
    }

    # 盘中操作纪律（2026-08-31）：12 条操盘纪律的个股标签。逐票 try/except——
    # 一只票的脏数据只跳过该票，不再静默放弃全部票的标签（审查修复）。
    # fail-open：单票异常不阻塞扫描，不影响评分/排序/落库。
    for c in all_candidates:
        try:
            _quote = current_quotes.get(c.stock.symbol, {})
            # high_pct 由 quote 的 high/昨收计算（api._quote_high_pct），是真实日内
            # 最高涨幅；None = 无数据 → 纪律内部 fail-open 跳过依赖项。
            _high_pct = _quote.get("high_pct") if _quote else None
            c.tactic_tags = stock_actions(c, now=None, kline_bars=klines.get(c.stock.symbol), high_pct=_high_pct)
        except EXTERNAL_FAILURES as e:
            print(f"  [!] 盘中操作纪律计算失败 {c.stock.symbol}（跳过）: {type(e).__name__}: {e}")

    # 数据血缘日志（2026-08-14）：本轮数据质量快照落库——补拉失败/缺今日bar/兜底构造/
    # stale 推荐数。跨函数静默降级是本项目最难发现的 bug 类别（网宿案例），常态计数器
    # 让降级规模可查询：某日 fetch_failed/today_bar_missing 异常升高即数据质量下降信号。
    try:
        quality_stats["gem_count"] = len(gem_stocks_filtered)
        quality_stats["stale_recs"] = sum(1 for c in all_candidates if c.stale_kline)
        save_scan_quality(conn, quality_stats)
    except EXTERNAL_FAILURES as e:
        print(f"  [!] 数据血缘日志落库失败: {e}")

    # 核心方向低吸落库：开关关闭时不产出（hit 数据不足，不再作为活跃推荐桶）。
    if ENABLE_CORE_DIP:
        try:
            from scanner.core_themes import find_core_theme_dips, save_core_dips

            save_core_dips(conn, find_core_theme_dips(conn, today), today)
        except EXTERNAL_FAILURES as e:
            print(f"  [!] 核心方向低吸落库失败: {e}")

    # 双跑：pool_picks 从 all_candidates 重建并按涨幅排序——加分循环的
    # dataclass_replace 已创建新对象，旧列表持有的是未累加 extra 的过期对象
    # （与下方 v1 分类列表重建同理）。
    pool_picks = [c for c in all_candidates if c.category == V2_CATEGORY]
    pool_picks.sort(key=lambda c: -(c.stock.percent or 0))

    return ScanResult(
        new_faces=new_faces,
        momentum=momentum,
        rebound=rebound_list,
        short_term=short_term_list,
        comeback=comeback_list,
        pool_picks=pool_picks,
        gem_stocks=gem_stocks_filtered,
        filtered_large_cap=filtered_large_cap,
        current_quotes=current_quotes,
        today_pool=session_state.today_pool,
    )
