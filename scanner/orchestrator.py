import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace as dataclass_replace
from datetime import date

import requests

from scanner.analysis import analyze_momentum, analyze_new_face, analyze_pullback, analyze_rebound, analyze_short_term
from scanner.validator import validate
from scanner.features import build_features
from scanner.api import (
    analyze_intraday,
    analyze_opening_strength,
    compute_surge_sentiment,
    estimate_live_volume,
    fetch_kline,
    fetch_market_caps_batch,
    fetch_market_index,
    make_session,
)
from scanner.candidate_pool import ScanSession
from scanner.config import (
    now_beijing,
    YI,
    FIRST_BREAKOUT_BONUS,
    FIRST_BREAKOUT_RANK_CHANGE,
    FIRST_BREAKOUT_VOL_RATIO,
    FIRST_TODAY_BONUS,
    HIGH_RISK_TRENDS,
    KLINE_FETCH_DAYS,
    KLINE_MIN_LENGTH,
    MAX_MARKET_CAP,
    MAX_STOCK_PRICE,
    MOMENTUM_MIN_SCORE,
    NEW_FACE_LOOKBACK_DAYS,
    PULLBACK_MIN_SCORE,
    NEW_FACE_MIN_SCORE,
    REBOUND_MIN_SCORE,
    SHORT_TERM_MIN_SCORE,
    SHORT_TERM_MAX_PER_SECTOR,
    RPS_BONUS_HIGH,
    RPS_BONUS_LOW,
    RPS_BONUS_MEDIUM,
    RPS_PCTILE_HIGH,
    RPS_PCTILE_LOW,
    RPS_PCTILE_MEDIUM,
    RISK_FLAGS_HARD_FILTER,
)
from scanner.database import (
    get_cached_kline,
    get_loss_rates_batch,
    get_symbol_appearances,
    record_appearances,
    save_kline_to_db,
)
from scanner.enhancer import (
    accumulate_final_score,
    apply_all_bonuses,
    compute_market_env_bonus,
    compute_time_bonus,
)
from scanner.models import Candidate, KlineSummary, StockInfo
from scanner.rank_trend import update_rank_history
from scanner.sector import get_sector_clusters, classify_sector
from scanner.trading_session import is_trading_day, is_trading_time
from scanner.utils import is_gem, is_hk_stock, is_st

_thread_local = threading.local()


def _get_session() -> requests.Session:
    if not hasattr(_thread_local, "session"):
        _thread_local.session = make_session()
    return _thread_local.session

_session_state = ScanSession()

# 盘中今日 K 线刷新 TTL（秒）：盘中时段已缓存今日 bar 时，超过该时长仍强制补拉，
# 避免整日复用早盘残次 bar（stock.current 实时价与缓存 close 脱节）。
# 300→120：缩短到 2 分钟，让盘中异动更快反映到 K 线打分（缓解"涨起来了才推"滞后）。
KLINE_REFRESH_TTL = 120
_last_kline_fetch: dict[str, float] = {}


def _fetch_all_klines(conn: sqlite3.Connection, session: requests.Session, stocks: list[StockInfo]) -> dict[str, list[dict] | None]:
    result: dict[str, list[dict] | None] = {}
    needs_fetch: list[str] = []
    stale_cache: dict[str, list[dict]] = {}

    for s in stocks:
        cached = get_cached_kline(conn, s.symbol)
        if cached:
            if len(cached) < KLINE_MIN_LENGTH:
                stale_cache[s.symbol] = cached
                needs_fetch.append(s.symbol)
                continue
            max_date_str = max(k["date"] for k in cached)
            max_date = date.fromisoformat(max_date_str)
            today = now_beijing().date()
            if not is_trading_time():
                # 非交易时段：直接复用缓存，不补拉
                result[s.symbol] = cached
                continue
            # 交易时段
            if max_date < today:
                # 缓存尚未含今日 Bar：必须补拉（否则全天无今日行情）
                stale_cache[s.symbol] = cached
                needs_fetch.append(s.symbol)
                continue
            # 已含今日 Bar：仅当超过刷新 TTL 才补拉，否则复用缓存
            last_fetch = _last_kline_fetch.get(s.symbol, 0.0)
            if (now_beijing().timestamp() - last_fetch) < KLINE_REFRESH_TTL:
                result[s.symbol] = cached
                continue
            stale_cache[s.symbol] = cached
        needs_fetch.append(s.symbol)

    if not needs_fetch:
        return result

    def _fetch_one(sym: str) -> tuple[str, list[dict] | None]:
        sess = _get_session()
        kline = fetch_kline(sess, sym, KLINE_FETCH_DAYS)
        _last_kline_fetch[sym] = now_beijing().timestamp()
        return sym, kline

    # 拉取阶段：仅采集数据，不写 DB（避免多线程并发写 SQLite）
    fetched: dict[str, list[dict] | None] = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        fut_map = {pool.submit(_fetch_one, sym): sym for sym in needs_fetch}
        for fut in as_completed(fut_map):
            sym = fut_map[fut]
            try:
                sym, kline = fut.result()
                fetched[sym] = kline
            except Exception as e:
                print(f"  [!] K线获取失败 {sym}: {e}")

    # 写入阶段：主线程顺序写 DB，确保 SQLite 线程安全
    for sym, kline in fetched.items():
        if kline:
            result[sym] = kline
            try:
                save_kline_to_db(conn, sym, kline)
            except Exception as e:
                print(f"  [!] K线写入DB失败 {sym}: {e}")
        elif sym in stale_cache:
            result[sym] = stale_cache[sym]

    for sym in needs_fetch:
        if sym not in result and sym in stale_cache:
            result[sym] = stale_cache[sym]

    # P1-3: K线数据缺失汇总（首次拉取失败且无 stale_cache 兜底的票）
    missing = [sym for sym in needs_fetch if sym not in result]
    if missing:
        preview = ", ".join(missing[:5])
        suffix = f" 等{len(missing)}只" if len(missing) > 5 else ""
        print(f"  [!] K线数据缺失{len(missing)}只: {preview}{suffix}（已跳过评分，下次刷新重试）")

    return result


def _build_candidate(stock: StockInfo, kline_summary: KlineSummary | None, category: str,
                     is_first_today: bool, first_date: str, kline: list[dict] | None) -> Candidate:
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
                   is_first_today: bool, first_date: str, kline: list[dict] | None,
                   closes: list[float], historical: list[dict],
                   clusters: dict[str, list[str]] | None,
                   feats: dict | None = None) -> Candidate | None:
    if kline_summary is None:
        return None
    if kline_summary.trend in HIGH_RISK_TRENDS:
        return None
    min_score = {
        "new_face": NEW_FACE_MIN_SCORE,
        "known_new_face": NEW_FACE_MIN_SCORE,
        "momentum": MOMENTUM_MIN_SCORE,
        "pullback": PULLBACK_MIN_SCORE,
        "rebound": REBOUND_MIN_SCORE,
        "short_term": SHORT_TERM_MIN_SCORE,
    }[category]
    if kline_summary.score < min_score:
        return None
    passed, bonus, dims = validate(category, stock, kline_summary, closes, historical, clusters, feats)
    if not passed:
        return None
    new_dims = dict(kline_summary.dimensions)
    new_dims["validation_bonus"] = bonus
    new_dims.update(dims)
    kline_summary = dataclass_replace(kline_summary, score=kline_summary.score + bonus, dimensions=new_dims)
    return _build_candidate(stock, kline_summary, category, is_first_today, first_date, kline)


def _cap_short_term_by_sector(short_term_list: list[Candidate],
                             max_per_sector: int = SHORT_TERM_MAX_PER_SECTOR) -> list[Candidate]:
    """同板块数量上限：板块普涨日防止单板块淹没超短列表。

    按 score 降序每组保留前 max_per_sector 只；其余从超短列表移除
    （仍可能经其它策略桶保留在 all_candidates）。
    """
    if not max_per_sector or len(short_term_list) <= max_per_sector:
        return short_term_list
    by_sector: dict[str, list[Candidate]] = {}
    for c in short_term_list:
        sec = classify_sector(c.stock.name)
        by_sector.setdefault(sec, []).append(c)
    capped: list[Candidate] = []
    for group in by_sector.values():
        group.sort(key=lambda c: -c.score)
        capped.extend(group[:max_per_sector])
    return capped


def _classify_category(stock: StockInfo, is_new: bool,
                       c_mo: Candidate | None,
                       c_nf: Candidate | None, c_st: Candidate | None = None,
                       c_rb: Candidate | None = None,
                       c_pb: Candidate | None = None) -> str | None:
    """按价格结构（而非尝试顺序）选最贴合的策略标签。

    P0-2: pullback 恢复为"高风险监控"类别，填补强势回踩真空。
    pullback 与其它策略互斥（today_pct<=0 vs >0），不会抢占主列表。
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
    st_is_wts = c_st is not None and c_st.kline.dimensions.get("st_weak_to_strong", 0) > 0
    if c_st is not None and st_is_wts:
        return "short_term"
    if c_mo is not None:
        return "momentum"
    if c_st is not None:
        return "short_term"
    if c_nf is not None:
        return "known_new_face"
    # 强势回踩：前期累计涨+今日回调+缩量，归 pullback 高风险监控
    if c_pb is not None:
        return "pullback"
    return None


def _filter_gem_stocks(raw: list[dict]) -> list[StockInfo]:
    gem_stocks: list[StockInfo] = []
    seen_symbols: set[str] = set()
    for i, item in enumerate(raw, 1):
        symbol = item.get("symbol", "")
        code = item.get("code", "")
        name = item.get("name", "")
        if is_hk_stock(symbol) or not is_gem(code) or is_st(name):
            continue
        # 去重：API 异常返回重复 symbol 时只保留首条，避免下游重复打分/显示
        if symbol in seen_symbols:
            continue
        seen_symbols.add(symbol)
        gem_stocks.append(StockInfo(
            symbol=symbol, name=name, code=code,
            percent=item.get("percent") or 0.0,
            current=item.get("current") or 0.0,
            value=item.get("value") or 0.0,
            rank_change=item.get("rank_change") or 0,
            rank=item.get("rank", i),
            source_tag=item.get("source_tag", "xueqiu"),
        ))
    return gem_stocks


def _score_stock(stock: StockInfo, conn: sqlite3.Connection, klines: dict[str, list[dict] | None],
                 today: str, session_state: ScanSession,
                 clusters: dict[str, list[str]] | None = None
                 ) -> tuple[Candidate | None, Candidate | None, Candidate | None,
                            Candidate | None, Candidate | None, Candidate | None]:
    is_first_today = session_state.mark_seen(stock.symbol)
    app_history = get_symbol_appearances(conn, stock.symbol, NEW_FACE_LOOKBACK_DAYS)
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

    nk = analyze_new_face(stock, kline, features=feats)
    mk = analyze_momentum(stock, kline, features=feats)
    rk = analyze_rebound(stock, kline, features=feats)
    sk = analyze_short_term(stock, kline, features=feats)
    pk = analyze_pullback(stock, kline, features=feats)

    # 五策略独立打分 + 各自交叉验证，再按价格结构选最贴合的标签
    c_nf = _try_candidate(stock, nk, "new_face" if is_new else "known_new_face",
                          is_first_today, first_date, kline, closes, historical, clusters, feats)
    c_mo = _try_candidate(stock, mk, "momentum",
                          is_first_today, first_date, kline, closes, historical, clusters, feats)
    c_rb = _try_candidate(stock, rk, "rebound",
                          is_first_today, first_date, kline, closes, historical, clusters, feats)
    c_st = _try_candidate(stock, sk, "short_term",
                          is_first_today, first_date, kline, closes, historical, clusters, feats)
    c_pb = _try_candidate(stock, pk, "pullback",
                          is_first_today, first_date, kline, closes, historical, clusters, feats)

    category = _classify_category(stock, is_new, c_mo, c_nf, c_st, c_rb, c_pb)
    if category == "short_term":
        return None, None, None, None, c_st, None
    if category in ("new_face", "known_new_face"):
        # 首板票若同时满足超短次日，双挂到超短列表（保留新面孔标签）
        if is_new and c_st is not None:
            return c_nf, None, None, None, c_st, None
        return c_nf, None, None, None, None, None
    if category == "momentum":
        return None, c_mo, None, None, None, None
    if category == "rebound":
        return None, None, None, c_rb, None, None
    if category == "pullback":
        return None, None, c_pb, None, None, None
    return None, None, None, None, None, None


def _compute_rps(candidates: list[Candidate],
                 baseline: list[float] | None = None) -> dict[str, int]:
    """计算 RPS 相对强弱加分。

    baseline: 全 GEM 监控集的累计涨幅列表（排名基准）。若提供，候选在其中排名，
    恢复 RPS「相对全市场强弱」本意；若不提供则退化为候选池内排名（旧行为）。
    """
    scores: dict[str, int] = {}
    # 双挂票（同代码出现在多个桶）只计一次排名，避免拉高 total 扭曲分位
    seen: set[str] = set()
    uniq = [c for c in candidates if not (c.stock.symbol in seen or seen.add(c.stock.symbol))]
    candidates = uniq
    if len(candidates) < 2:
        return {c.stock.symbol: 0 for c in candidates}
    cand_accum = [c.kline.accumulated_pct if c.kline else 0 for c in candidates]
    if baseline:
        base_sorted = sorted(baseline)
        base_total = len(base_sorted)
        def _pctile(v: float) -> int:
            # 在基准分布中的百分位（0~100）
            lo = sum(1 for b in base_sorted if b <= v)
            return lo * 100 // base_total
        pctiles = [_pctile(v) for v in cand_accum]
    else:
        sorted_by_accum = sorted(cand_accum)
        total = len(sorted_by_accum)
        pctiles = [(rank + 1) * 100 // total for rank in sorted(range(total), key=lambda i: sorted_by_accum[i])]
    for c, pctile in zip(candidates, pctiles):
        # 超跌反弹 accumulated 为负必落底部分位，RPS_LOW 惩罚违背策略初衷，豁免
        if c.category == "rebound":
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


def scan_with_raw(raw: list[dict], conn: sqlite3.Connection,
                  session: requests.Session) -> tuple[
                      list[Candidate], list[Candidate], list[Candidate],
                      list[Candidate], list[Candidate], list[Candidate],
                      list[StockInfo], int]:
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
    market_caps = fetch_market_caps_batch(session, mc_syms) if mc_syms else {}

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

    klines = _fetch_all_klines(conn, session, gem_stocks_filtered)

    clusters = get_sector_clusters(gem_stocks_filtered)

    new_faces: list[Candidate] = []
    momentum: list[Candidate] = []
    pullback_list: list[Candidate] = []
    rebound_list: list[Candidate] = []
    short_term_list: list[Candidate] = []

    for stock in gem_stocks_filtered:
        nf, mo, pb, rb, st, _ = _score_stock(stock, conn, klines, today, session_state, clusters)
        if nf:
            new_faces.append(nf)
        if mo:
            momentum.append(mo)
        if pb:
            pullback_list.append(pb)
        if rb:
            rebound_list.append(rb)
        if st:
            short_term_list.append(st)

    # 同板块上限：板块普涨日防止单板块淹没超短列表（P0-69 后再加一道闸）
    short_term_list = _cap_short_term_by_sector(short_term_list)

    all_candidates = new_faces + momentum + pullback_list + rebound_list + short_term_list
    for c in all_candidates:
        cap_data = market_caps.get(c.stock.symbol, {})
        c.market_cap = cap_data.get("market_cap", 0)
        c.circ_market_cap = cap_data.get("circ_market_cap", 0)

    rps_scores: dict[str, int] = {}
    # RPS 基准：全 GEM 监控集（过滤后、含未入选候选）的 5 日累计涨幅列表，
    # 使 RPS 表达「相对全市场强弱」而非仅在已涨票中比谁涨得多。
    rps_baseline: list[float] = []
    for s in gem_stocks_filtered:
        kl = klines.get(s.symbol)
        if not kl:
            continue
        hist = [k for k in kl if k["date"] != today]
        closes = [k["close"] for k in hist]
        if len(closes) >= 6:
            acc = (closes[-1] - closes[-6]) / closes[-6] * 100
            rps_baseline.append(acc)
    rps_scores.update(_compute_rps(all_candidates, baseline=rps_baseline))

    intraday_scores: dict[str, float | None] = {}
    opening_scores: dict[str, float | None] = {}
    live_volumes: dict[str, float | None] = {}

    if all_candidates:
        with ThreadPoolExecutor(max_workers=6) as pool:
            _parallel_fetch(pool, session, all_candidates,
                            intraday_scores, opening_scores, live_volumes)

    market_idx_pct = fetch_market_index(session)
    market_env_bonus = compute_market_env_bonus(market_idx_pct)
    time_bonus = compute_time_bonus()

    apply_all_bonuses(all_candidates, gem_stocks_filtered, intraday_scores,
                      opening_scores, live_volumes, market_caps,
                      clusters, market_idx_pct, time_bonus,
                      sentiment_info=sentiment_info, rps_scores=rps_scores,
                      list_streaks=session_state.list_presence,
                      conn=conn)

    # 历史大跌率标签：批量查询近 90 天推荐中次日<=-5% 占比，供 UI 风控参考
    # 样本<3 不返回（get_loss_rates_batch 内部过滤），单次 SQL 避免 N 次查询
    syms_for_loss = list({c.stock.symbol for c in all_candidates})
    loss_rates = get_loss_rates_batch(conn, syms_for_loss)
    if loss_rates:
        for i, c in enumerate(all_candidates):
            rate = loss_rates.get(c.stock.symbol)
            if rate is not None:
                all_candidates[i] = dataclass_replace(c, hist_loss_rate=rate)

    # 双挂候选（首板票同时挂 new_face + short_term）需各自独立计算 extra：
    # accumulate_final_score 依赖 c.gap_up_bonus / c.list_momentum_bonus 等，
    # 这些 bonus 在 apply_all_bonuses 中按 candidate 独立计算（如 _apply_gap_up_bonus
    # 依据 c.category 选 key，_apply_list_momentum_bonus 依据 c.category 判 is_reversal）。
    # 若复用同一 extra，short_term 桶会拿到 new_face 桶的 bonus，排名错位。
    for i, c in enumerate(all_candidates):
        extra = accumulate_final_score(c, market_env_bonus, opening_scores)
        all_candidates[i] = dataclass_replace(c, score=c.score + extra)

    update_rank_history({s.symbol: s.rank for s in gem_stocks_filtered})

    session_state.update_pool(all_candidates)
    stale_candidates = session_state.get_stale_candidates()
    session_state.update_stale_quotes(stale_candidates, market_caps)

    # 风险硬过滤：命中"卖出/止损"级标签（主力出货/趋势破位）的候选直接移出推荐列表。
    # 此步在 update_pool/update_stale 之后执行，不影响候选池掉榜与排名历史，
    # 仅作用于最终对外展示的推荐列表，确保推荐输出只含可买票。
    excluded_by_risk = [c for c in all_candidates if _candidate_excluded_by_risk(c)]
    if excluded_by_risk:
        _names = "、".join(f"{c.stock.name}({c.stock.symbol})" for c in excluded_by_risk[:8])
        _more = f" 等{len(excluded_by_risk)}只" if len(excluded_by_risk) > 8 else ""
        print(f"  [风险过滤] {len(excluded_by_risk)} 只命中硬排除标签，已移出推荐：{_names}{_more}")
    all_candidates = [c for c in all_candidates if not _candidate_excluded_by_risk(c)]

    # 分类列表必须从 all_candidates 重建，而非沿用旧对象引用——
    # dataclass_replace 已创建新对象（含最终 score），
    # 旧列表持有的仍是未累加 extra 的过期对象。
    new_faces = [c for c in all_candidates if c.category in ("new_face", "known_new_face")]
    momentum = [c for c in all_candidates if c.category == "momentum"]
    pullback_list = [c for c in all_candidates if c.category == "pullback"]
    rebound_list = [c for c in all_candidates if c.category == "rebound"]
    short_term_list = [c for c in all_candidates if c.category == "short_term"]
    new_faces.sort(key=lambda c: -c.score)
    momentum.sort(key=lambda c: -c.score)
    pullback_list.sort(key=lambda c: -c.score)
    rebound_list.sort(key=lambda c: -c.score)
    short_term_list.sort(key=lambda c: -c.score)

    return (new_faces, momentum, pullback_list, rebound_list, short_term_list,
            stale_candidates, gem_stocks_filtered, filtered_large_cap)


def _candidate_excluded_by_risk(c: Candidate) -> bool:
    """命中硬排除风险标签的候选不进入推荐列表。

    集合见 config.RISK_FLAGS_HARD_FILTER（主力出货 / 趋势破位），
    二者均为明确的卖出 / 止损信号。其余标签（超买 / 涨幅过大 / 疲劳 /
    弱市 / 量价背离）保留为展示型警告，不在此过滤。
    """
    if not c.risk_flags:
        return False
    return bool(set(c.risk_flags) & RISK_FLAGS_HARD_FILTER)


def _parallel_fetch(pool: ThreadPoolExecutor, base_session: requests.Session,
                    candidates: list[Candidate],
                    intraday_scores: dict[str, float | None],
                    opening_scores: dict[str, float | None],
                    live_volumes: dict[str, float | None]):
    seen: set[str] = set()
    intra_futs = {}
    for c in candidates:
        sym = c.stock.symbol
        if sym in seen:
            continue
        seen.add(sym)
        def _do(sym=sym, fn=analyze_intraday):
            return fn(_get_session(), sym)
        intra_futs[pool.submit(_do)] = sym
    for fut in as_completed(intra_futs):
        sym = intra_futs[fut]
        try:
            intraday_scores[sym] = fut.result()
        except Exception as e:
            print(f"  [!] 分时强度失败 {sym}: {e}")
            intraday_scores[sym] = None

    seen = set()
    open_futs = {}
    for c_ in candidates:
        sym = c_.stock.symbol
        if sym in seen:
            continue
        seen.add(sym)
        def _open(sym=sym):
            return analyze_opening_strength(_get_session(), sym)
        open_futs[pool.submit(_open)] = sym
    for fut in as_completed(open_futs):
        sym = open_futs[fut]
        try:
            opening_scores[sym] = fut.result()
        except Exception:
            opening_scores[sym] = None

    seen = set()
    vol_futs = {}
    for c_ in candidates:
        sym = c_.stock.symbol
        if sym in seen:
            continue
        seen.add(sym)
        def _vol(sym=sym):
            return estimate_live_volume(_get_session(), sym)
        vol_futs[pool.submit(_vol)] = sym
    for fut in as_completed(vol_futs):
        sym = vol_futs[fut]
        try:
            live_volumes[sym] = fut.result()
        except Exception as e:
            print(f"  [!] 实时量比失败 {sym}: {e}")
            live_volumes[sym] = None
