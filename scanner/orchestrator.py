import math
import sqlite3
import threading
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import replace as dataclass_replace

import requests

from scanner.analysis import analyze_momentum, analyze_new_face, analyze_rebound, analyze_short_term
from scanner.api import (
    analyze_intraday,
    analyze_opening_strength,
    compute_surge_sentiment,
    estimate_live_volume,
    make_session,
)
from scanner.candidate_pool import ScanSession
from scanner.comeback import evaluate_comeback
from scanner.concept import compute_driving_concepts
from scanner.config import (
    FIRST_BREAKOUT_BONUS,
    FIRST_BREAKOUT_RANK_CHANGE,
    FIRST_BREAKOUT_VOL_RATIO,
    FIRST_TODAY_BONUS,
    HIGH_RISK_TRENDS,
    KLINE_FETCH_DEADLINE,
    MAX_MARKET_CAP,
    MAX_STOCK_PRICE,
    MCAP_CACHE_MAX_AGE_DAYS,
    MINUTE_FETCH_PHASE_DEADLINE,
    MOMENTUM_MIN_SCORE,
    NEW_FACE_FIRST_MIN_SCORE,
    NEW_FACE_LOOKBACK_DAYS,
    NEW_FACE_MIN_SCORE,
    REBOUND_MIN_SCORE,
    RISK_FLAGS_HARD_FILTER,
    RPS_BONUS_HIGH,
    RPS_BONUS_LOW,
    RPS_BONUS_MEDIUM,
    RPS_PCTILE_HIGH,
    RPS_PCTILE_LOW,
    RPS_PCTILE_MEDIUM,
    SHORT_TERM_MAX_TODAY_PCT,
    SHORT_TERM_MIN_SCORE,
    WATCH_OFFLIST_KEEP_DAYS,
    YI,
    now_beijing,
)
from scanner.database import (
    get_cached_market_caps,
    get_symbol_appearances,
    prune_watch_pool,
    record_appearances,
    save_market_caps,
    save_market_index_log,
    save_minute_snapshots,
    save_scan_quality,
    upsert_watch_symbols,
)
from scanner.enhancer import (
    accumulate_final_score,
    apply_all_bonuses,
    compute_time_bonus,
)
from scanner.features import build_features
from scanner.kline_fetch import fetch_all_klines
from scanner.models import Candidate, KlineBar, KlineSummary, ScanResult, StockInfo
from scanner.rank_trend import update_rank_history
from scanner.sector import get_sector_clusters
from scanner.trading_session import is_trading_time
from scanner.utils import is_gem, is_hk_stock, is_st
from scanner.validator import validate

_thread_local = threading.local()


def _get_session() -> requests.Session:
    if not hasattr(_thread_local, "session"):
        _thread_local.session = make_session()
    return _thread_local.session

_session_state = ScanSession()

def _build_candidate(stock: StockInfo, kline_summary: KlineSummary | None, category: str,
                     is_first_today: bool, first_date: str, kline: list[KlineBar] | None) -> Candidate:
    first_breakout = (stock.rank_change >= FIRST_BREAKOUT_RANK_CHANGE
                      and kline_summary.volume_ratio > FIRST_BREAKOUT_VOL_RATIO)
    return Candidate(
        stock=stock, category=category, score=kline_summary.score,
        reason=kline_summary.trend, kline=kline_summary,
        first_seen=first_date,
        first_today_bonus=FIRST_TODAY_BONUS if is_first_today else 0,
        first_breakout_bonus=FIRST_BREAKOUT_BONUS if first_breakout else 0,
        history_pct=[k["percent"] for k in kline] if kline else [],
    )


def _try_candidate(stock: StockInfo, kline_summary: KlineSummary | None, category: str,
                   is_first_today: bool, first_date: str, kline: list[KlineBar] | None,
                   closes: list[float], historical: list[KlineBar],
                   clusters: dict[str, list[str]] | None,
                   feats: dict | None = None) -> Candidate | None:
    if kline_summary is None:
        return None
    if kline_summary.trend in HIGH_RISK_TRENDS:
        return None
    min_score = {
        # 首日 new_face 全档负收益提门槛砍量；known_new_face 分数反指（低分档最优）保持低门槛
        "new_face": NEW_FACE_FIRST_MIN_SCORE,
        "known_new_face": NEW_FACE_MIN_SCORE,
        "momentum": MOMENTUM_MIN_SCORE,
        "rebound": REBOUND_MIN_SCORE,
        "short_term": SHORT_TERM_MIN_SCORE,
    }[category]
    if kline_summary.score < min_score:
        return None
    passed, bonus, dims = validate(category, stock, kline_summary, closes, historical, clusters, feats, kline=kline)
    if not passed:
        return None
    new_dims = dict(kline_summary.dimensions)
    new_dims["validation_bonus"] = bonus
    new_dims.update(dims)
    # 2026-08-10: validation_bonus 全期 cum_3d IC -0.139（反指）——交叉验证只做通过门禁，
    # 加分不再进 score。bonus 仍写入 dims 供展示与 backtest dimension_ic 归因。
    kline_summary = dataclass_replace(kline_summary, dimensions=new_dims)
    return _build_candidate(stock, kline_summary, category, is_first_today, first_date, kline)


def _classify_category(stock: StockInfo, is_new: bool,
                       c_mo: Candidate | None,
                       c_nf: Candidate | None, c_st: Candidate | None = None,
                       c_rb: Candidate | None = None) -> str | None:
    """按价格结构（而非尝试顺序）选最贴合的策略标签。

    pullback 已于 2026-07-30 下线（回测 cum_2d 均亏 -8.33%，胜率 15.8%），
    _classify_category 不再返回 "pullback"。
    """
    if is_new:
        if c_nf is not None:
            return "new_face"
        if c_rb is not None:
            return "rebound"
        if c_st is not None:
            return "short_term"
        if c_mo is not None:
            return "momentum"
        return None
    # 老股：超跌企稳优先归反弹；弱转强优先归超短；
    # 其它"非弱转强超短合格票"若同时过动量则归动量（避免掏空动量桶），
    # 仅过超短不过动量的票仍留超短（不丢票）。
    # 超跌反弹：前期暴跌+今日企稳阳线，优先于动量/超短（场景互斥，避免反弹票被误归动量）
    if c_rb is not None:
        return "rebound"
    st_is_wts = bool(c_st is not None and c_st.kline is not None
                     and c_st.kline.dimensions.get("st_weak_to_strong"))
    if c_st is not None and st_is_wts:
        return "short_term"
    if c_mo is not None:
        return "momentum"
    if c_st is not None:
        return "short_term"
    if c_nf is not None:
        return "known_new_face"
    return None


def _new_face_sort_key(c: Candidate) -> float:
    """new_face 桶排序键：known_new_face 分数反指（低分档收益更好）→ 升序；new_face → 降序。

    与 display._score_sort_key 同口径，保证终端/飞书的新面孔列表与综合排序一致。
    """
    if c.category == "known_new_face":
        return c.score
    return -c.score


def _filter_gem_stocks(raw: list[dict]) -> list[StockInfo]:
    gem_stocks: list[StockInfo] = []
    seen_symbols: set[str] = set()
    for i, item in enumerate(raw, 1):
        # symbol/code/name 强转 str：API 偶发返回 None（键存在但值为 null）或数值
        # 类型（int/float）时，is_hk_stock(None).isdigit() / is_gem(300001).startswith()
        # / is_st(None) 抛 AttributeError/TypeError，整轮扫描异常丢失。脏值统一转
        # 空串或 str（下游过滤/比对不崩）。
        symbol = str(item.get("symbol") or "")
        code = str(item.get("code") or "")
        name = str(item.get("name") or "")
        if is_hk_stock(symbol) or not is_gem(code) or is_st(name):
            continue
        # 去重：API 异常返回重复 symbol 时只保留首条，避免下游重复打分/显示
        if symbol in seen_symbols:
            continue
        seen_symbols.add(symbol)
        # 数值强转：API 偶发返回字符串（如 rank_change="-"）时，保持字符串会让下游
        # 的 str vs float/int 比较抛 TypeError（s.current > MAX_STOCK_PRICE、
        # _vol_rank_combo_score 等），整轮扫描异常丢失。脏数据直接跳过该票。
        try:
            percent = float(item.get("percent") or 0.0)
            current = float(item.get("current") or 0.0)
            value = float(item.get("value") or 0.0)
            rc_val = float(item.get("rank_change") or 0)
            # rank 与 rank_change 同口径 float 中转：API 偶发返回 "5.0" 这类数值字符串时，
            # 直接 int("5.0") 抛 ValueError 会让整只票被跳过（漏推荐），float 中转则正常解析。
            rank_val = float(item.get("rank") or i)
        except (TypeError, ValueError):
            continue
        # NaN/inf 防御（Python json 默认解析 JSON 字面量 NaN/Infinity，与字符串脏值同族）：
        # NaN 与任何数值比较均为 False，会绕过 s.current > MAX_STOCK_PRICE / 涨幅档位判断，
        # 产出 NaN 评分并写库为 NULL（sqlite 把 NaN 存为 NULL）；inf 同理。统一按 0 处理
        # （与 api._num 口径一致）。rank/rank_change 为 NaN 时 int(nan) 抛 ValueError
        # 会让整只票被跳过（漏推荐），改回退默认值（0 / 列表下标）。
        if not math.isfinite(percent):
            percent = 0.0
        if not math.isfinite(current):
            current = 0.0
        if not math.isfinite(value):
            value = 0.0
        rank_change = int(rc_val) if math.isfinite(rc_val) else 0
        rank = int(rank_val) if math.isfinite(rank_val) else i
        # 换手率（2026-08-20）：与 rank_change 同族脏值（"-"/空串/NaN/inf）——非数字串
        # 时 float() 直接抛 ValueError（曾放 try 外，整批扫描崩溃）。此处 fail-soft 到
        # 0.0（仅停牌/僵尸识别用，不应整只票跳过）。bool 也算脏（float(True)=1.0）。
        tr_raw = item.get("turnover_rate")
        try:
            tr = float(tr_raw) if tr_raw not in (None, "", True, False) else 0.0
            turnover_rate = tr if math.isfinite(tr) else 0.0
        except (TypeError, ValueError):
            turnover_rate = 0.0
        gem_stocks.append(StockInfo(
            symbol=symbol, name=name, code=code,
            percent=percent, current=current, value=value,
            rank_change=rank_change, rank=rank,
            source_tag=item.get("source_tag", "xueqiu"),
            turnover_rate=turnover_rate,
        ))
    return gem_stocks


def _score_stock(stock: StockInfo, conn: sqlite3.Connection, klines: dict[str, list[KlineBar] | None],
                 today: str, session_state: ScanSession,
                 clusters: dict[str, list[str]] | None = None,
                 now=None
                 ) -> tuple[Candidate | None, Candidate | None, Candidate | None,
                            Candidate | None]:
    """对单只票跑完 4 路引擎 + 交叉验证 + 分类，返回各桶候选 (new_face, momentum, rebound, short_term)。

    `today` 是本次扫描锚定的交易日；实时扫描传真实今日，历史回放
    （historical_rescan）传信号日。所有依赖「今天」的下游都由它驱动：
    appearances 回溯窗口（is_new）、analyze_* 的今日 bar 切分。
    `now` 仅用于盘中量能投影；回放时传当日收盘后时刻即可关闭投影，
    使结果不随「跑回测的时刻」漂移。
    """
    is_first_today = session_state.mark_seen(stock.symbol)
    app_history = get_symbol_appearances(conn, stock.symbol, NEW_FACE_LOOKBACK_DAYS, as_of=today)
    previous_dates = [a["date"] for a in app_history]
    is_new = len(previous_dates) == 0
    first_date = previous_dates[0] if previous_dates else today
    kline = klines.get(stock.symbol)
    historical = [k for k in (kline or []) if k["date"] != today]
    closes = [k["close"] for k in historical]

    # 统一特征抽取一次，下发给 5 路 analyze_* 与 validate，避免每只股票重复计算指标
    feats = None
    if len(closes) >= 5:
        highs = [k["high"] for k in historical]
        lows = [k["low"] for k in historical]
        volumes = [k["volume"] for k in historical]
        feats = build_features(closes, highs, lows, volumes)

    nk = analyze_new_face(stock, kline, today_str=today, features=feats, now=now)
    mk = analyze_momentum(stock, kline, today_str=today, features=feats, now=now)
    rk = analyze_rebound(stock, kline, today_str=today, features=feats, now=now)
    sk = analyze_short_term(stock, kline, today_str=today, features=feats, now=now)

    # 四策略独立打分 + 各自交叉验证，再按价格结构选最贴合的标签
    c_nf = _try_candidate(stock, nk, "new_face" if is_new else "known_new_face",
                          is_first_today, first_date, kline, closes, historical, clusters, feats)
    c_mo = _try_candidate(stock, mk, "momentum",
                          is_first_today, first_date, kline, closes, historical, clusters, feats)
    c_rb = _try_candidate(stock, rk, "rebound",
                          is_first_today, first_date, kline, closes, historical, clusters, feats)
    c_st = _try_candidate(stock, sk, "short_term",
                          is_first_today, first_date, kline, closes, historical, clusters, feats)

    # 审计标记（2026-08-14）：评分所用 K 线缺今日 bar（补拉失败旧缓存兜底）时打 stale_kline。
    # 缺今日 bar → 量比基于昨日量（vol_ratio 失真）→ 可能被量比硬门误杀（网宿案例）或
    # 基于旧数据误推。落库供事后审计"该推荐基于什么数据评分"。兜底已由 kline_fetch.fetch_all_klines
    # 的分时构造今日 bar 尽量消除，此处标记残留的兜底失败场景。
    # 非交易时段缓存本就停在最近交易日，缺今日 bar 属正常，不打 stale（与 fail-loud
    # 告警同口径：仅交易时段缺今日 bar 才是数据降级）。historical_rescan 直接调用本函数
    # 时 now_ref 若落在非交易时段同样不应误标。
    _stale = bool(kline) and max(k["date"] for k in kline) < today and is_trading_time()
    for _c in (c_nf, c_mo, c_rb, c_st):
        if _c is not None:
            _c.stale_kline = _stale
    # fail-loud：交易时段仍以缺今日 bar 旧缓存评分（日线补拉 + 分时兜底均失败）→ 逐票告警。
    # 不静默吞掉数据质量下降——这正是网宿类 bug 的隐蔽点（上游降级无感知，下游静默消费）。
    # 非交易时段缺今日 bar 属正常（缓存未更新），不告警。
    # 2026-08-20：停牌/僵尸股（turnover_rate==0，确无今日盘面）降级为 [~] 提示，不炸 [!]。
    if _stale and is_trading_time():
        if stock.turnover_rate == 0.0:
            print(f"  [~] {stock.name}({stock.symbol}) 停牌/僵尸股，今日无交易（旧缓存评分，非故障）")
        else:
            print(f"  [!] {stock.name}({stock.symbol}) 评分基于缺今日bar旧缓存（补拉与分时兜底均失败），"
                  f"量比/涨幅按昨日数据，可能误判")

    category = _classify_category(stock, is_new, c_mo, c_nf, c_st, c_rb)
    if category == "short_term":
        return None, None, None, c_st
    if category in ("new_face", "known_new_face"):
        # 首板票若同时满足超短次日，双挂到超短列表（保留新面孔标签）
        if is_new and c_st is not None:
            return c_nf, None, None, c_st
        return c_nf, None, None, None
    if category == "momentum":
        return None, c_mo, None, None
    if category == "rebound":
        return None, None, c_rb, None
    return None, None, None, None


def _compute_rps(candidates: list[Candidate],
                 baseline: list[float] | None = None,
                 accum_map: dict[str, float] | None = None) -> dict[str, int]:
    """计算 RPS 相对强弱加分。

    baseline: 全 GEM 监控集的累计涨幅列表（排名基准）。若提供，候选在其中排名，
    恢复 RPS「相对全市场强弱」本意；若不提供则退化为候选池内排名（旧行为）。
    accum_map: 候选 symbol → 历史5日累计涨幅（排除今日）。用于统一 RPS 口径：
    short_term 的 c.kline.accumulated_pct 包含今日（策略语义），与 baseline
    （排除今日）口径不一致，会导致百分位偏高。传入 accum_map 后所有候选用统一
    历史口径，与 baseline 一致。
    """
    scores: dict[str, int] = {}
    # 双挂票（同代码出现在多个桶）只计一次排名，避免拉高 total 扭曲分位
    seen: set[str] = set()
    uniq = [c for c in candidates if not (c.stock.symbol in seen or seen.add(c.stock.symbol))]
    candidates = uniq
    if len(candidates) < 2:
        return {c.stock.symbol: 0 for c in candidates}
    # 优先使用 accum_map 中的统一口径；回退到 c.kline.accumulated_pct
    cand_accum = []
    for c in candidates:
        if accum_map is not None and c.stock.symbol in accum_map:
            cand_accum.append(accum_map[c.stock.symbol])
        else:
            cand_accum.append(c.kline.accumulated_pct if c.kline else 0)
    if baseline:
        base_sorted = sorted(baseline)
        base_total = len(base_sorted)
        def _pctile(v: float) -> int:
            # 在基准分布中的百分位（0~100）
            lo = sum(1 for b in base_sorted if b <= v)
            return lo * 100 // base_total
        pctiles = [_pctile(v) for v in cand_accum]
    else:
        total = len(cand_accum)
        order = sorted(range(total), key=lambda i: cand_accum[i])
        pctiles = [0] * total
        for rank, i in enumerate(order):
            pctiles[i] = (rank + 1) * 100 // total
    for c, pctile in zip(candidates, pctiles):
        # 超跌反弹/回马枪 accumulated 为负必落底部分位，RPS_LOW 惩罚违背策略初衷，豁免
        if c.category in ("rebound", "comeback"):
            scores[c.stock.symbol] = 0
            continue
        if pctile >= RPS_PCTILE_HIGH:
            scores[c.stock.symbol] = RPS_BONUS_HIGH
        elif pctile >= RPS_PCTILE_MEDIUM:
            scores[c.stock.symbol] = RPS_BONUS_MEDIUM
        elif pctile < RPS_PCTILE_LOW:
            scores[c.stock.symbol] = RPS_BONUS_LOW
        else:
            scores[c.stock.symbol] = 0
    return scores


def _enrich_candidate_market_cap(c: Candidate, cap_data: dict) -> None:
    """为候选补齐市值字段（榜上票与回马枪 off-list 票同口径）。

    - c.market_cap / c.circ_market_cap：元原始值（供行情侧/资金流查询门禁）
    - c.stock.market_cap：亿元（供 enhancer._apply_market_cap_bonus 的阈值比较）

    榜上票的 stock 对象在 _filter_gem 富集时已赋值 stock.market_cap；回马枪
    off-list 票的 StockInfo 由 evaluate_comeback 新建、market_cap 恒为 0，
    导致小市值加分系统性缺失（c.market_cap 是元原始值，与亿元阈值不是同一
    单位，不能替代）。统一在此按同口径补齐。
    """
    c.market_cap = cap_data.get("market_cap", 0)
    c.circ_market_cap = cap_data.get("circ_market_cap", 0)
    cmc = cap_data.get("circ_market_cap") or cap_data.get("market_cap", 0)
    if cmc > 0:
        c.stock.market_cap = cmc / YI


def scan_with_raw(raw: list[dict], conn: sqlite3.Connection,
                  adapter) -> ScanResult:
    global _session_state
    session_state = _session_state
    today = now_beijing().date().isoformat()
    session_state.reset_if_new_day(today)

    sentiment_info = compute_surge_sentiment(raw)
    gem_stocks = _filter_gem_stocks(raw)

    record_appearances(conn, [
        {"symbol": s.symbol, "name": s.name, "percent": s.percent,
         "value": s.value, "rank": s.rank}
        for s in gem_stocks
    ])
    session_state.update_list_presence({s.symbol for s in gem_stocks})

    stale_syms = list(sym for sym, c in session_state.today_pool.items() if c.is_stale)
    mc_syms = list(set(s.symbol for s in gem_stocks) | set(stale_syms))
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
        upsert_watch_symbols(conn, [
            {"symbol": s.symbol, "name": s.name, "last_list_date": today}
            for s in gem_stocks_filtered
        ] + [
            {"symbol": s.symbol, "name": s.name, "last_list_date": today, "over_limit": True}
            for s in gem_stocks_filtered if s.percent > SHORT_TERM_MAX_TODAY_PCT
        ])
        prune_watch_pool(conn, WATCH_OFFLIST_KEEP_DAYS)
    except Exception as e:
        print(f"  [!] 掉榜跟踪池维护失败: {e}")

    # 双批 K 线拉取共用同一个 deadline：榜上票 45s + 回马枪幸存者再 45s 会串行 ~90s，
    # 超 60s 扫描间隔。共用一个 deadline 保证两批总耗时仍被 KLINE_FETCH_DEADLINE 兜底。
    kline_deadline = now_beijing().timestamp() + KLINE_FETCH_DEADLINE
    quality_stats: dict = {}
    klines = fetch_all_klines(conn, adapter, gem_stocks_filtered,
                               deadline=kline_deadline, stats=quality_stats)

    clusters = get_sector_clusters(gem_stocks_filtered)

    new_faces: list[Candidate] = []
    momentum: list[Candidate] = []
    rebound_list: list[Candidate] = []
    short_term_list: list[Candidate] = []

    for stock in gem_stocks_filtered:
        nf, mo, rb, st = _score_stock(stock, conn, klines, today, session_state, clusters)
        if nf:
            new_faces.append(nf)
        if mo:
            momentum.append(mo)
        if rb:
            rebound_list.append(rb)
        if st:
            short_term_list.append(st)

    # 回马枪：评估掉榜跟踪池 + 近 N 日推荐（两变体均 category="comeback"）。
    # 在榜票走正常策略，此处只处理掉榜票（on_list_symbols 排除）。K 线补拉
    # 已过 5 日跌幅预过滤 + KLINE_FETCH_DEADLINE 限时，成本有界。
    comeback_rebound: list[Candidate] = []
    comeback_reentry: list[Candidate] = []
    try:
        on_list_symbols = {s.symbol for s in gem_stocks_filtered}
        comeback_rebound, comeback_reentry, cb_quotes = evaluate_comeback(
            conn, adapter,
            lambda stocks: fetch_all_klines(conn, adapter, stocks,
                                             deadline=kline_deadline, stats=quality_stats),
            today, on_list_symbols, clusters)
        market_caps.update(cb_quotes)  # 并入市值/行情，供后续市值富集与实时行情
    except Exception as e:
        print(f"  [!] 回马枪评估失败: {type(e).__name__}: {e}")

    all_candidates = (new_faces + momentum + rebound_list
                      + short_term_list + comeback_rebound + comeback_reentry)
    for c in all_candidates:
        _enrich_candidate_market_cap(c, market_caps.get(c.stock.symbol, {}))

    # 行情增强数据（涨停池 + 个股资金流）：全市场各 1 次请求，失败软降级为空。
    # 必须在 apply_all_bonuses 前收集，供资金流/连板加分与风险标签使用。
    market_extra: dict = {}
    try:
        from scanner.market_extra import collect_market_extra
        market_extra = collect_market_extra(
            conn, [c.stock.symbol for c in all_candidates])
    except Exception as e:
        print(f"  [!] 行情增强数据收集失败（忽略，不影响扫描）: {e}")

    # 基本面风险（pywencai 问财反向查询资不抵债股）：排除式过滤器，命中候选打
    # "财务风险"硬过滤标签（RISK_FLAGS_HARD_FILTER 移出推荐列表），不做任何加分。
    # 全程 fail-open：问财未安装/超时/异常 → 空 dict，不影响扫描。
    fund_risk: dict[str, str] = {}
    try:
        from scanner.fundamentals import collect_fund_risk
        fund_risk = collect_fund_risk(conn, [c.stock.symbol for c in all_candidates])
        if fund_risk:
            names = "、".join(f"{c.stock.name}({c.stock.symbol})"
                              for c in all_candidates if c.stock.symbol in fund_risk)
            print(f"  [财务风险] {len(fund_risk)} 只资不抵债（退市风险级），将移出推荐：{names}")
    except Exception as e:
        print(f"  [!] 基本面风险收集失败（忽略，不影响扫描）: {e}")

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
    rps_scores.update(_compute_rps(all_candidates, baseline=rps_baseline,
                                  accum_map=accum_map))

    intraday_scores: dict[str, float | None] = {}
    opening_scores: dict[str, float | None] = {}
    live_volumes: dict[str, float | None] = {}

    if all_candidates:
        # wait=False 关闭：_parallel_fetch 各相已有 phase_deadline 限时，超时被
        # cancel 的任务仍在后台跑（受请求自身超时约束，最坏 ~48s 后自然结束），
        # 不能让 with-exit 的 shutdown(wait=True) 阻塞主扫描循环等待它们。
        pool = ThreadPoolExecutor(max_workers=6)
        try:
            _parallel_fetch(pool, all_candidates,
                            intraday_scores, opening_scores, live_volumes, adapter)
        finally:
            pool.shutdown(wait=False)

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
    except Exception as e:
        print(f"  [!] 大盘指数血缘日志落库失败: {e}")

    apply_all_bonuses(all_candidates, gem_stocks_filtered, intraday_scores,
                      opening_scores, live_volumes, market_caps,
                      clusters, market_idx_pct, time_bonus,
                      sentiment_info=sentiment_info, rps_scores=rps_scores,
                      list_streaks=session_state.list_presence,
                      market_extra=market_extra,
                      fund_risk=fund_risk,
                      conn=conn)

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
    excluded_by_risk = [c for c in all_candidates if _candidate_excluded_by_risk(c)]
    if excluded_by_risk:
        _names = "、".join(f"{c.stock.name}({c.stock.symbol})" for c in excluded_by_risk[:8])
        _more = f" 等{len(excluded_by_risk)}只" if len(excluded_by_risk) > 8 else ""
        print(f"  [风险过滤] {len(excluded_by_risk)} 只命中硬排除标签，已移出推荐：{_names}{_more}")
    all_candidates = [c for c in all_candidates if not _candidate_excluded_by_risk(c)]

    # P1-7 (2026-08-10): 硬过滤落标——被过滤的今日推荐标记 excluded=1（综合排序不再展示），
    # 通过硬过滤的候选置 0（同日风险标签可能随时间变化，以最新轮次为准）。
    try:
        if excluded_by_risk:
            conn.executemany(
                "UPDATE recommendations SET excluded=1, excluded_reason=? "
                "WHERE date=? AND symbol=?",
                [(c.excluded_reason, today, c.stock.symbol) for c in excluded_by_risk])
        passed_syms = [(today, c.stock.symbol) for c in all_candidates]
        if passed_syms:
            conn.executemany(
                "UPDATE recommendations SET excluded=0, excluded_reason='' WHERE date=? AND symbol=?",
                passed_syms)
        conn.commit()
    except Exception as e:
        print(f"  [!] 风险过滤落标失败: {e}")

    # 分类列表必须从 all_candidates 重建，而非沿用旧对象引用——
    # dataclass_replace 已创建新对象（含最终 score），
    # 旧列表持有的仍是未累加 extra 的过期对象。
    new_faces = [c for c in all_candidates if c.category in ("new_face", "known_new_face")]
    momentum = [c for c in all_candidates if c.category == "momentum"]
    rebound_list = [c for c in all_candidates if c.category == "rebound"]
    short_term_list = [c for c in all_candidates if c.category == "short_term"]
    comeback_list = [c for c in all_candidates if c.category == "comeback"]
    new_faces.sort(key=lambda c: _new_face_sort_key(c))
    momentum.sort(key=lambda c: -c.score)
    rebound_list.sort(key=lambda c: -c.score)
    short_term_list.sort(key=lambda c: -c.score)
    comeback_list.sort(key=lambda c: -c.score)

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
    except Exception as e:
        print(f"  [!] 驱动概念计算失败: {type(e).__name__}: {e}")

    current_quotes = {
        sym: {"percent": d.get("percent", 0.0), "current": d.get("current", 0.0),
              "high_pct": d.get("high_pct")}
        for sym, d in market_caps.items()
    }

    # 数据血缘日志（2026-08-14）：本轮数据质量快照落库——补拉失败/缺今日bar/兜底构造/
    # stale 推荐数。跨函数静默降级是本项目最难发现的 bug 类别（网宿案例），常态计数器
    # 让降级规模可查询：某日 fetch_failed/today_bar_missing 异常升高即数据质量下降信号。
    try:
        quality_stats["gem_count"] = len(gem_stocks_filtered)
        quality_stats["stale_recs"] = sum(1 for c in all_candidates if c.stale_kline)
        save_scan_quality(conn, quality_stats)
    except Exception as e:
        print(f"  [!] 数据血缘日志落库失败: {e}")

    # 分时快照落库（2026-08-21）：每轮把最终候选的 {现价, 涨幅} 采样进时间序列，
    # 历史分时形态可回放（涨停复盘曾因分时未落库只能看单例）。fail-open 不阻塞扫描。
    try:
        save_minute_snapshots(conn, [
            {"symbol": c.stock.symbol, "price": c.stock.current,
             "pct": c.stock.percent}
            for c in all_candidates
        ])
    except Exception as e:
        print(f"  [!] 分时快照落库失败: {e}")

    # 核心方向低吸落库（2026-08-19）：在榜主类别外单独 category=core_dip（与 comeback
    # 同族），供 display 独立低吸区读取 + nextday_attribution/prevday_perf 复盘验证。
    # DB-only 推导、成本 ~0.1s（无需 TTL）；fail-open 不阻塞扫描。
    try:
        from scanner.core_themes import find_core_theme_dips, save_core_dips
        save_core_dips(conn, find_core_theme_dips(conn, today), today)
    except Exception as e:
        print(f"  [!] 核心方向低吸落库失败: {e}")

    return ScanResult(
        new_faces=new_faces,
        momentum=momentum,
        rebound=rebound_list,
        short_term=short_term_list,
        comeback=comeback_list,
        gem_stocks=gem_stocks_filtered,
        filtered_large_cap=filtered_large_cap,
        current_quotes=current_quotes,
        today_pool=session_state.today_pool,
    )


def _candidate_excluded_by_risk(c: Candidate) -> bool:
    """命中硬排除风险标签的候选不进入推荐列表。

    集合见 config.RISK_FLAGS_HARD_FILTER（主力出货 / 趋势破位），
    二者均为明确的卖出 / 止损信号。其余标签（超买 / 涨幅过大 / 疲劳 /
    弱市 / 量价背离）保留为展示型警告，不在此过滤。
    """
    if not c.risk_flags:
        return False
    return bool(set(c.risk_flags) & RISK_FLAGS_HARD_FILTER)


def _parallel_fetch(pool: ThreadPoolExecutor,
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
