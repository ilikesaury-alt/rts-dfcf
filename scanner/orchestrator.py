import sqlite3
from concurrent.futures import ThreadPoolExecutor

from scanner.api import compute_surge_sentiment
from scanner.candidate_pool import ScanSession
from scanner.candidates import (
    compute_rps,
    enrich_candidate_market_cap,
    filter_gem_stocks,
    score_stock,
)
from scanner.concept import compute_driving_concepts
from scanner.config import (
    ENABLE_CORE_DIP,
    ENABLE_MOMENTUM,
    ENABLE_SHORT_TERM,
    KLINE_FETCH_DEADLINE,
    MCAP_CACHE_MAX_AGE_DAYS,
    SHORT_TERM_MAX_TODAY_PCT,
    WATCH_OFFLIST_KEEP_DAYS,
    now_beijing,
)
from scanner.database import (
    get_cached_market_caps,
    prune_watch_pool,
    record_appearances,
    save_market_caps,
    save_market_index_log,
    save_rejections,
    save_scan_quality,
    upsert_watch_symbols,
)
from scanner.enhancer import (
    apply_all_bonuses,
    compute_time_bonus,
)
from scanner.intraday_fetch import parallel_fetch
from scanner.kline_fetch import fetch_all_klines
from scanner.models import Candidate, ScanResult
from scanner.pipeline import (
    accumulate_final_scores,
    attach_minute_trends,
    attach_tactic_tags,
    build_current_quotes,
    build_rps_inputs,
    filter_by_market_cap,
    filter_excluded_by_risk,
    report_market_cap_availability,
    split_and_sort_categories,
)
from scanner.rank_trend import update_rank_history
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
        # 类别守卫（2026-09-05 审查修复）：置 1 侧原按 (date,symbol) 全量排除——同 symbol
        # 当日早先落库的 comeback/core_dip 行（本轮回马枪未重建候选，如评估失败或
        # 变体未触发）会被 v1 候选的硬过滤连带排除，违反「回马枪/核心低吸不参与
        # v1 硬过滤连带」的设计语义（mark_reversed 2026-08-17 已加同款守卫，
        # d3c519b 只补了置回侧，置 1 侧同族遗漏）。
        # ⚠ 2026-09-16 删除回马枪桶后，SQL 里的 `'comeback'` **仍须保留**：这里约束的是
        # **库里存量历史行**（历史日期仍有该类别），删掉会让 `--date` 回放的置位结果改变。
        conn.executemany(
            "UPDATE recommendations SET excluded=1, excluded_reason=? WHERE date=? AND symbol=? "
            "AND COALESCE(category, '') NOT IN ('comeback', 'core_dip')",
            [(c.excluded_reason, today, c.stock.symbol) for c in excluded_by_risk],
        )
    passed_syms = [(today, c.stock.symbol, c.category) for c in all_candidates]
    if passed_syms:
        conn.executemany(
            "UPDATE recommendations SET excluded=0, excluded_reason='' WHERE date=? AND symbol=? AND category=?",
            passed_syms,
        )
    conn.commit()


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

    gem_stocks_filtered, filtered_large_cap = filter_by_market_cap(gem_stocks, market_caps)
    # 市值数据可用性三态判定（2026-08-20）：实时取到→静默；陈旧缓存兜底→[~] 降级提示
    # （小而美规则仍基于旧值生效）；全失败且无缓存→[!] 真正告警。详见 pipeline.pool。
    report_market_cap_availability(market_caps, mc_syms, used_stale_mc)

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

    # 主榜 K 线拉取 deadline（45s）。
    kline_deadline = now_beijing().timestamp() + KLINE_FETCH_DEADLINE
    quality_stats: dict = {}
    klines = fetch_all_klines(conn, adapter, gem_stocks_filtered, deadline=kline_deadline, stats=quality_stats)

    clusters = get_sector_clusters(gem_stocks_filtered)

    new_faces: list[Candidate] = []
    momentum: list[Candidate] = []
    rebound_list: list[Candidate] = []
    short_term_list: list[Candidate] = []

    # ── v2 池管道已整体移除（2026-09-28，pool_pick 退池）────────────────────
    # 移除前的历史（详见 git 51b80d8）：这里曾无条件执行 v2 池管道（build_pool →
    # evaluate_pool → pool_pick 候选），与 v1 五桶合并进 all_candidates 走同一套下游。
    # 删除依据 = 「以当前终端输出为准」：pool_pick 自 2026-09-14 展示区隐藏、2026-09-21
    # 合池消费方删除后，在终端四区块与飞书卡片里**均无任何呈现**（assemble.py 早已把它
    # 硬编码排除出 main_recs，historical_watch.V1_CATEGORIES 也不含它）。它唯一还在做的事
    # 是每天往 recommendations 写 27~76 行（占全部行 47~65%），而 sym-day 去重口径
    # hit≥7% 仅 3.0%（n=986），低于全体基准 0.078 —— 纯负超额。
    #
    # 删除对 v1 五桶**等价**（已逐条核对，勿重开此论证）：
    #   • rps_baseline 来自 gem_stocks 全监控集，与 all_candidates 无关 → RPS 基准不变；
    #   • accum_map 按 symbol 覆盖，双挂票同值重算无差异；
    #   • collect_market_extra / collect_fund_risk 均为「全市场一次拉取 + 本批过滤」，
    #     无批次上限 → v1 票取数不受候选集缩小影响；
    #   • parallel_fetch / enrich_candidate_market_cap 逐候选无跨票效应（仅省一次网络开销）。
    # 保留物：scanner/pool.py、scanner/danger.py、scanner/matcher.py、pool_log 表与
    # save_pool_log —— 它们是研究原料与 scripts/ 的依赖，不属「推荐内容」。
    # 若要复原：git show 51b80d8:scanner/orchestrator.py。

    # v1 五桶评分（口径与历史 v1 完全一致）
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

    all_candidates = new_faces + momentum + rebound_list + short_term_list

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

    # ── v2 二次排雷块已于 2026-09-28 随 v2 池管道一并移除 ──
    # 该块（原 L275~316）只在 pool_log_rows 非空时运行，现已无任何触发源。
    # 它当时只作用于 v2 域（`c.category == V2_CATEGORY` 的防御分支），
    # v1 五桶保持自身 validator/硬过滤口径 —— 故删除对 v1 候选集**零影响**。
    # 若要复原：git show 51b80d8:scanner/orchestrator.py

    # RPS 基准：全 GEM 监控集（过滤后、含未入选候选）的 5 日累计涨幅列表，
    # 使 RPS 表达「相对全市场强弱」而非仅在已涨票中比谁涨得多。
    # 口径统一为"历史5日累计"（排除今日）—— short_term 的 accumulated 含今日
    # （策略语义），由 accum_map 覆盖为历史口径，避免百分位偏高。见 pipeline.features。
    rps_baseline, accum_map = build_rps_inputs(gem_stocks_filtered, all_candidates, klines, today)
    rps_scores: dict[str, int] = {}
    rps_scores.update(compute_rps(all_candidates, baseline=rps_baseline, accum_map=accum_map))

    intraday_scores: dict[str, float | None] = {}
    opening_scores: dict[str, float | None] = {}
    live_volumes: dict[str, float | None] = {}
    minute_trends: dict[str, dict | None] = {}

    if all_candidates:
        # wait=False 关闭：_parallel_fetch 各相已有 phase_deadline 限时，超时被
        # cancel 的任务仍在后台跑（受请求自身超时约束，最坏 ~48s 后自然结束），
        # 不能让 with-exit 的 shutdown(wait=True) 阻塞主扫描循环等待它们。
        # cancel_futures=True（2026-09-04 审查修复）：未开始的排队任务一并取消，
        # 否则 interval 调小时上一轮残余任务与下一轮 6 线程叠加。
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
            pool.shutdown(wait=False, cancel_futures=True)

    # 分时趋势摘要写入候选维度（盘中操作纪律 rule 3/5/7 数据源，纯展示不参与评分）
    attach_minute_trends(all_candidates, minute_trends)

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
        klines=klines,
        conn=conn,
        today=today,
    )

    # 双挂候选（首板票同时挂 new_face + short_term）需各自独立计算 extra：
    # accumulate_final_score 依赖 c.gap_up_bonus / c.list_momentum_bonus 等，
    # 这些 bonus 在 apply_all_bonuses 中按 candidate 独立计算（如 apply_gap_up_bonus
    # 依据 c.category 选 key，_apply_list_momentum_bonus 依据 c.category 判 is_reversal）。
    # 若复用同一 extra，short_term 桶会拿到 new_face 桶的 bonus，排名错位。
    accumulate_final_scores(all_candidates, opening_scores)

    update_rank_history({s.symbol: s.rank for s in gem_stocks_filtered})

    session_state.update_pool(all_candidates)

    # 风险硬过滤：命中"卖出/止损"级标签（主力出货/趋势破位）的候选直接移出推荐列表。
    # 此步在 update_pool/update_stale 之后执行，不影响候选池掉榜与排名历史，
    # 仅作用于最终对外展示的推荐列表，确保推荐输出只含可买票。
    # 2026-09-11：判定结果复用（原两处 list comprehension 对同一批候选各调一次判定函数）。
    # 实现见 pipeline.scoring.filter_excluded_by_risk（含 8 只上限的打印口径）。
    all_candidates, excluded_by_risk = filter_excluded_by_risk(all_candidates)

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
    _buckets = split_and_sort_categories(all_candidates)
    new_faces = _buckets["new_faces"]
    momentum = _buckets["momentum"]
    rebound_list = _buckets["rebound"]
    short_term_list = _buckets["short_term"]

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

    # 行情降级条目（current<=0，如停牌/字段缺失被强转 0）不入实时行情——
    # 与主循环补拉路径同口径（2026-08-14 fail-open 修复只堵了补拉路径，此处
    # 此前仍会把 0.00% 当真实涨幅喂给 display/mark_reversed）。见 pipeline.pool。
    current_quotes = build_current_quotes(market_caps)

    # 盘中操作纪律（2026-08-31）：12 条操盘纪律的个股标签。逐票 try/except——
    # 一只票的脏数据只跳过该票，不再静默放弃全部票的标签（审查修复）。
    # fail-open：单票异常不阻塞扫描，不影响评分/排序/落库。
    attach_tactic_tags(all_candidates, current_quotes, klines)

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

    # ── pool_picks 重建已移除（2026-09-28 随 v2 池管道退池）──
    # 若要复原：git show 51b80d8:scanner/orchestrator.py

    return ScanResult(
        new_faces=new_faces,
        momentum=momentum,
        rebound=rebound_list,
        short_term=short_term_list,
        gem_stocks=gem_stocks_filtered,
        filtered_large_cap=filtered_large_cap,
        current_quotes=current_quotes,
        today_pool=session_state.today_pool,
    )
