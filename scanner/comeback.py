"""回马枪策略模块（掉榜跟踪池，2026-08-07 新增）。

背景：候选池只由"当次热榜"驱动，掉榜超跌股（如志特新材 07-09→07-31 三周掉榜）
完全不可见，反弹企稳日无法被评估。回马枪补上这块盲区。两个变体均
category="comeback"（recommendations 表结构不变，用 trend 列存变体）：

- 反转（off-list rebound）：掉榜票 + 5日跌幅 ≤ -8%（DB 缓存预过滤，零网络成本）
  + 今日 2~12% 企稳首阳。复用 analyze_rebound(off_list=True) +
  validator(off_list=True, pos_dims>=3)——掉榜票无热榜背书，比榜上更严。
- 回踩（吸收原 tracker 模块）：近 N 日推荐回调到买点（6 维买点信号 ≥4 到买点），
  二次上车。硬过滤：今日±5% / 累计±10% / 主力净占比≤-5%（无数据 fail-open）。

成本控制：WATCH_POOL_MAX 上限 + 预过滤 + kline_fetch.fetch_all_klines 的 KLINE_FETCH_DEADLINE
限时兜底，掉榜票每交易日每票最多评估一次（last_eval_date 落库，重启不丢）。
"""
from collections.abc import Callable
from dataclasses import replace as dataclass_replace

from scanner.analysis import analyze_rebound
from scanner.config import (
    COMEBACK_PREFILTER_5D_DROP,
    COMEBACK_REENTRY_BASE_SCORE,
    COMEBACK_REENTRY_BOLL_MID_PCT,
    COMEBACK_REENTRY_DAYS,
    COMEBACK_REENTRY_FILTER_CUM_HIGH,
    COMEBACK_REENTRY_FILTER_CUM_LOW,
    COMEBACK_REENTRY_FILTER_TODAY_HIGH,
    COMEBACK_REENTRY_FILTER_TODAY_LOW,
    COMEBACK_REENTRY_FUND_FLOW_LOW,
    COMEBACK_REENTRY_MA20_SLOPE_MIN,
    COMEBACK_REENTRY_MA20_SUPPORT_PCT,
    COMEBACK_REENTRY_RSI_HIGH,
    COMEBACK_REENTRY_RSI_LOW,
    COMEBACK_REENTRY_SIGNAL_SCORE,
    COMEBACK_REENTRY_STATUS_BUY,
    COMEBACK_REENTRY_STATUS_WATCH,
    COMEBACK_REENTRY_VOL_SHRINK_RATIO,
    HIGH_RISK_TRENDS,
    MAX_MARKET_CAP,
    MAX_STOCK_PRICE,
    REBOUND_MIN_SCORE,
    now_beijing,
)
from scanner.database import (
    get_cached_kline,
    get_fund_flow_pct_map,
    get_recent_recommendations,
    get_watch_symbols,
    mark_watch_evaluated,
    upsert_watch_symbols,
)
from scanner.indicators import (
    compute_bollinger_bands,
    compute_ma,
    compute_macd,
    compute_rsi,
)
from scanner.models import Candidate, KlineBar, KlineSummary, StockInfo
from scanner.validator import validate

_KlineFetcher = Callable[[list[StockInfo]], dict[str, list[KlineBar] | None]]


def collect_comeback_symbols(conn, today: str, on_list_symbols: set[str]) -> list[dict]:
    """回马枪候选域 = watch_pool ∪ 近 N 日推荐，减去今日在榜票与今日已评估票。

    近 N 日推荐但不在 watch_pool 的票一并 upsert 入池（统一评估去重/剪枝口径）。
    """
    watch = get_watch_symbols(conn)
    recs = get_recent_recommendations(conn, COMEBACK_REENTRY_DAYS, exclude_today=True)
    symbols: dict[str, dict] = {}
    for w in watch:
        if w["symbol"] not in on_list_symbols:
            symbols[w["symbol"]] = w
    for r in recs:
        # 2026-08-19：core_dip（核心方向低吸）是展示/复盘用途的独立类别，不入回马枪
        # 回踩候选域，避免两个低吸区跨区互换污染（回马枪=榜上推荐回调，core_dip=主线核心股回调）。
        if r.get("category") == "core_dip":
            continue
        sym = r["symbol"]
        if sym in on_list_symbols:
            continue
        entry = symbols.setdefault(sym, {
            "symbol": sym, "name": r["name"],
            "last_list_date": r["date"], "over_limit": 0,
        })
        entry.setdefault("_rec", r)

    # 排除今日已评估（防同日重复评估/重复补拉）
    result = [w for w in symbols.values() if w.get("last_eval_date") != today]
    if result:
        try:
            upsert_watch_symbols(conn, [
                {"symbol": w["symbol"], "name": w["name"],
                 "last_list_date": w.get("last_list_date"), "over_limit": w.get("over_limit", 0)}
                for w in result
            ])
        except Exception as e:
            print(f"  [!] 回马枪入池写入失败: {e}")
    return result


def evaluate_comeback(conn, adapter, fetch_klines: _KlineFetcher,
                      today: str, on_list_symbols: set[str],
                      clusters: dict[str, list[str]] | None = None
                      ) -> tuple[list[Candidate], list[Candidate], dict[str, dict]]:
    """回马枪主入口：评估掉榜跟踪池 + 近 N 日推荐。

    返回 (反转候选, 回踩候选, quotes)。quotes 供 orchestrator 并入 market_caps，
    保证后续市值富集/实时行情对回马枪候选生效（与在榜票同一数据源）。
    """
    metas = collect_comeback_symbols(conn, today, on_list_symbols)
    if not metas:
        return [], [], {}

    meta_by_sym = {m["symbol"]: m for m in metas}

    # 反转预过滤（DB 缓存零网络成本）：5日跌幅 > 阈值 的票不值得补拉当日 bar。
    # 回踩变体仅评估"近 N 日推荐"票（有 rec 上下文），走 6 维回踩买点信号、不要求
    # 超跌，故不套用 5 日跌幅预筛（该预筛是反转变体的语义门 + 成本控制）。
    # 反转变体必须先过此预筛，只对幸存者拉行情，避免对全池（上限 600）逐扫描轰击行情接口。
    survivors: list[dict] = []
    for sym, m in meta_by_sym.items():
        if "_rec" in m or _passes_drop_prefilter(get_cached_kline(conn, sym), today):
            survivors.append(m)
    if not survivors:
        return [], [], {}

    symbols = [m["symbol"] for m in survivors]
    quotes = adapter.fetch_market_caps_batch(symbols)
    flow_pct_map = get_fund_flow_pct_map(conn, symbols)

    # 市值/价格过滤（与在榜票同一套小而美规则）
    stocks: dict[str, StockInfo] = {}
    for m in survivors:
        sym = m["symbol"]
        q = quotes.get(sym) or {}
        current = q.get("current") or 0
        if current <= 0:
            continue
        if current > MAX_STOCK_PRICE:
            continue
        mc = q.get("market_cap") or 0
        if mc > 0 and mc > MAX_MARKET_CAP:
            continue
        stocks[sym] = StockInfo(
            symbol=sym, name=m["name"], code=sym[2:],
            percent=q.get("percent") or 0.0, current=current,
            value=0.0, rank_change=0, rank=0, source_tag="comeback",
        )

    if not stocks:
        return [], [], quotes

    # 统一补拉幸存者 K 线（受限时，KLINE_FETCH_DEADLINE 兜底）
    fetch_stocks = list(stocks.values())
    klines: dict[str, list[KlineBar] | None] = {}
    if fetch_stocks:
        try:
            klines = fetch_klines(fetch_stocks)
        except Exception as e:
            print(f"  [!] 回马枪K线拉取失败: {type(e).__name__}: {e}")
            klines = {}

    rebound_candidates: list[Candidate] = []
    reentry_candidates: list[Candidate] = []
    evaluated: list[str] = []
    for sym, st in stocks.items():
        kline = klines.get(sym)
        if not kline:
            continue  # 拉取失败，不标记已评估，下轮重试
        # 2026-08-20 修复：补拉失败回退的「stale 缓存」truthy 但无今日 bar。此前仅判
        # `not kline` 会把 stale 也当成评估成功 → mark_watch_evaluated 写 last_eval_date=today
        # → 当日永不再评估（池冻结、漏推荐）+ 用旧 K 线+实时 percent 评分（drop_5d/累计失真）。
        # 现与在榜票同款 stale 语义：无今日 bar 一律不标记、跳过本轮，下轮 KLINE_REFRESH_TTL
        # 重试。分时兜底成功时返回的 merged bar 含今日 date，可正常通过。
        if not any(k["date"] == today for k in kline):
            print(f"  [~] 回马枪 {st.name}({sym}) 缺今日bar（stale缓存），跳过本轮评估（下轮重试）")
            continue
        evaluated.append(sym)
        # 反转变体：超跌企稳首阳（off_list rebound）
        if _passes_drop_prefilter(kline, today):
            cand = _try_rebound_candidate(st, kline, today, clusters)
            if cand is not None:
                rebound_candidates.append(cand)
        # 回踩变体：近 N 日推荐回调到买点（若已过反转则不重复上桶）
        rec = meta_by_sym[sym].get("_rec")
        if rec is not None and not any(c.stock.symbol == sym for c in rebound_candidates):
            cand = _try_reentry_candidate(st, kline, today, rec, flow_pct_map)
            if cand is not None:
                reentry_candidates.append(cand)

    if evaluated:
        try:
            mark_watch_evaluated(conn, evaluated, today=today)
        except Exception as e:
            print(f"  [!] 回马枪评估标记失败: {e}")

    return rebound_candidates, reentry_candidates, quotes


def _try_rebound_candidate(stock: StockInfo, kline: list[KlineBar], today: str,
                           clusters: dict[str, list[str]] | None) -> Candidate | None:
    """回马枪·反转：off-list 超跌企稳（analyze_rebound 收紧档位 + pos_dims≥3）。"""
    ks = analyze_rebound(stock, kline, today_str=today, off_list=True)
    if ks is None:
        return None
    if ks.trend in HIGH_RISK_TRENDS:
        return None
    if ks.score < REBOUND_MIN_SCORE:
        return None
    historical = [k for k in kline if k["date"] != today]
    closes = [k["close"] for k in historical]
    passed, bonus, dims = validate("rebound", stock, ks, closes, historical,
                                   clusters, off_list=True)
    if not passed:
        return None
    new_dims = dict(ks.dimensions)
    new_dims["validation_bonus"] = bonus
    new_dims.update(dims)
    new_dims["comeback_variant"] = "反转"
    new_dims["comeback_off_list"] = 1
    # 2026-08-10: validation_bonus 只做门禁不加分（全期 IC -0.139 反指），与 orchestrator 同步。
    ks = dataclass_replace(ks, dimensions=new_dims, trend=f"反转·{ks.trend}")
    return Candidate(
        stock=stock, category="comeback", score=ks.score, reason=ks.trend,
        kline=ks, first_seen=now_beijing().strftime("%H:%M"),
        off_list=True, comeback_variant="反转",
        history_pct=[k["percent"] for k in kline],
    )


def _try_reentry_candidate(stock: StockInfo, kline: list[KlineBar], today: str,
                           rec: dict, flow_pct_map: dict[str, float]) -> Candidate | None:
    """回马枪·回踩：近 N 日推荐回调到买点（6 维信号 ≥4 到买点）。"""
    if not kline or len(kline) < 20:
        return None
    historical = [k for k in kline if k["date"] != today]
    if len(historical) < 20:
        return None
    closes = [k["close"] for k in historical]

    rec_close = _get_rec_day_close(historical, rec["date"])
    if rec_close <= 0:
        rec_close = _get_rec_day_close(kline, rec["date"])
        if rec_close <= 0:
            return None
    cum_return = (stock.current - rec_close) / rec_close * 100

    # 硬过滤：排除不能买的（今日大涨追高/暴跌破位/累计已错过/信号失效/资金流出）
    # stock.percent 防御强转：None/NaN → 0.0，避免比较抛 TypeError（入口防御）
    today_pct = float(stock.percent or 0.0)
    if today_pct >= COMEBACK_REENTRY_FILTER_TODAY_HIGH:
        return None
    if today_pct <= COMEBACK_REENTRY_FILTER_TODAY_LOW:
        return None
    if cum_return >= COMEBACK_REENTRY_FILTER_CUM_HIGH:
        return None
    if cum_return <= COMEBACK_REENTRY_FILTER_CUM_LOW:
        return None
    if not _passes_fund_flow_filter(flow_pct_map, stock.symbol):
        return None

    status, count, signals = _evaluate_buy_signals(historical)
    if status != "到买点":
        return None  # "观察中"默认不进推荐（可调 COMEBACK_REENTRY_DISPLAY_WATCH_MAX 展示）

    score = COMEBACK_REENTRY_BASE_SCORE + COMEBACK_REENTRY_SIGNAL_SCORE * count
    # 5日历史累计（排除今日，与其它策略桶 RPS/展示口径一致）
    accum = 0.0
    if len(closes) >= 6:
        accum = (closes[-1] - closes[-6]) / closes[-6] * 100
    dims = {
        "comeback_variant": "回踩",
        "comeback_buy_signals": count,
        "comeback_cum_return": round(cum_return, 2),
        "comeback_signals": "/".join(signals),
        "comeback_rec_date": rec["date"],
        "comeback_rec_category": rec.get("category", ""),
    }
    # 真实量比（今日量 vs 近5日均量，含今日 bar）——此前为占位 0.0，占位值会被
    # 下游（疲劳判定/展示）当真实缩量消费，2026-08-14 改为真实计算
    vol_ratio = 0.0
    vols = [k["volume"] for k in kline]
    if len(vols) >= 6:
        avg_v = sum(vols[:-1][-5:]) / 5
        if avg_v > 0:
            vol_ratio = round(vols[-1] / avg_v, 2)
    ks = KlineSummary(trend=f"回踩·{status}", accumulated_pct=round(accum, 2),
                      volume_ratio=vol_ratio, bottom_confirmed=False,
                      score=score, dimensions=dims)
    return Candidate(
        stock=stock, category="comeback", score=score, reason=ks.trend,
        kline=ks, first_seen=now_beijing().strftime("%H:%M"),
        off_list=True, comeback_variant="回踩",
        history_pct=[k["percent"] for k in kline],
    )


def _passes_drop_prefilter(kline: list[KlineBar] | None, today: str,
                           threshold: float = COMEBACK_PREFILTER_5D_DROP) -> bool:
    """DB 缓存预过滤：历史 5 日累计跌幅 ≤ 阈值才可能过反转变体（零网络成本）。"""
    if not kline or len(kline) < 6:
        return False
    hist = [k for k in kline if k["date"] != today]
    closes = [k["close"] for k in hist]
    if len(closes) < 6:
        return False
    drop = (closes[-1] - closes[-6]) / closes[-6] * 100
    return drop <= threshold


def _passes_fund_flow_filter(flow_pct_map: dict[str, float], symbol: str,
                             low: float = COMEBACK_REENTRY_FUND_FLOW_LOW) -> bool:
    """资金流硬过滤：主力净占比 ≤ 阈值 → 剔除（回调可能是出货）；无当日数据 → 保留（视同中性）。"""
    ff_pct = flow_pct_map.get(symbol)
    if ff_pct is None:
        return True
    return ff_pct > low


def _evaluate_buy_signals(historical: list[KlineBar]) -> tuple[str, int, list[str]]:
    """6 维买点信号判定（基于历史 K 线，排除今日 bar 防盘中实时价干扰）。

    1. MA20 支撑  2. 缩量回调  3. 未破位  4. RSI 合理区
    5. BOLL 中轨附近  6. MACD 未死叉
    返回 (状态, 信号数, 信号列表)。状态为空 = 未到买点（过滤）。
    """
    if len(historical) < 20:
        return "", 0, []
    closes = [k["close"] for k in historical]
    volumes = [k["volume"] for k in historical]
    last_close = closes[-1]
    signals: list[str] = []

    # 均线支撑：合并原 MA20支撑+未破位（两者本质同源：价格在 MA20 附近）
    # 条件：收盘站上 MA20 + 偏离<3% + MA20 上行（趋势支撑）
    ma20 = compute_ma(closes, 20)
    ma20_prev = compute_ma(closes[:-1], 20) if len(closes) >= 21 else None
    if ma20 and ma20 > 0 and last_close > ma20:
        dev_pct = abs(last_close - ma20) / ma20 * 100
        ma20_up = (ma20_prev and ma20 > ma20_prev * (1 + COMEBACK_REENTRY_MA20_SLOPE_MIN / 100))
        if dev_pct < COMEBACK_REENTRY_MA20_SUPPORT_PCT and ma20_up:
            signals.append("均线支撑")

    if len(volumes) >= 6:
        avg_vol = sum(volumes[-6:-1]) / 5
        today_vol = volumes[-1]
        # avg_vol==0（脏基准）fail-closed → 0.0，避免脏量能票误判为"未缩量"放过
        vol_ratio = today_vol / avg_vol if avg_vol > 0 else 0.0
        if vol_ratio < COMEBACK_REENTRY_VOL_SHRINK_RATIO:
            signals.append("缩量")

    rsi = compute_rsi(closes, period=14)
    if rsi is not None and COMEBACK_REENTRY_RSI_LOW < rsi < COMEBACK_REENTRY_RSI_HIGH:
        signals.append("RSI合理")

    boll = compute_bollinger_bands(closes)
    if boll and boll["middle"] > 0:
        dev_pct = abs(last_close - boll["middle"]) / boll["middle"] * 100
        if dev_pct < COMEBACK_REENTRY_BOLL_MID_PCT:
            signals.append("BOLL中轨")

    macd = compute_macd(closes)
    if macd and macd["histogram"] > -0.01:
        signals.append("MACD未死叉")

    count = len(signals)
    if count >= COMEBACK_REENTRY_STATUS_BUY:
        status = "到买点"
    elif count >= COMEBACK_REENTRY_STATUS_WATCH:
        status = "观察中"
    else:
        status = ""
    return status, count, signals


def _get_rec_day_close(kline: list[KlineBar] | None, rec_date: str) -> float:
    """从 K 线中取推荐日收盘价；找不到精确匹配时取之前最近的收盘价作为基准。"""
    if not kline:
        return 0.0
    for k in kline:
        if k["date"] == rec_date:
            return k["close"]
    before = [k for k in kline if k["date"] <= rec_date]
    if before:
        return before[-1]["close"]
    return 0.0
