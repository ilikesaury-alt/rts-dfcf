"""历史推荐跟踪模块。

持续监听近 N 个交易日内进入过推荐的票，基于技术指标判断是否回调到买点。

核心目标：找"推荐后回调到买点"的票，不是简单展示全部历史推荐。
- 硬过滤：排除大涨追高/已错过/暴跌失效的票
- 指标判断：MA20 支撑/缩量/未破位/RSI 合理区/BOLL 中轨/MACD 未死叉
- 状态分类：到买点（≥4信号）/ 观察中（2-3信号）/ 未到买点（<2信号，过滤）
"""
from dataclasses import dataclass, field

from scanner.api import fetch_kline, fetch_market_caps_batch
from scanner.config import (
    KLINE_FETCH_DAYS,
    PROMINENCE_LOOKBACK_DAYS,
    PROMINENCE_MAX_AVG_RANK,
    PROMINENCE_REPEAT_THRESHOLD,
    TRACK_BOLL_MID_PCT,
    TRACK_FILTER_CUM_HIGH,
    TRACK_FILTER_CUM_LOW,
    TRACK_FILTER_TODAY_HIGH,
    TRACK_FILTER_TODAY_LOW,
    TRACK_KLINE_REFRESH_LOOPS,
    TRACK_MA20_SLOPE_MIN,
    TRACK_MA20_SUPPORT_PCT,
    TRACK_RECOMMENDATION_DAYS,
    TRACK_RSI_HIGH,
    TRACK_RSI_LOW,
    TRACK_STATUS_BUY,
    TRACK_STATUS_WATCH,
    TRACK_VOL_SHRINK_RATIO,
    now_beijing,
)
from scanner.database import (count_recent_appearances, get_cached_kline, get_recent_recommendations,
                               get_symbol_appearances, save_kline_to_db)
from scanner.indicators import compute_bollinger_bands, compute_macd, compute_ma, compute_rsi


@dataclass
class TrackedRec:
    """历史推荐跟踪记录。"""
    symbol: str
    name: str
    rec_date: str          # 推荐日期
    rec_category: str      # 推荐时策略
    rec_score: int         # 推荐时评分
    rec_percent: float     # 推荐日涨幅
    current: float         # 今日实时价
    today_pct: float       # 今日涨幅
    cum_return: float      # 推荐日收盘→今日实时价 累计收益
    status: str = ""       # "到买点" / "观察中"
    buy_signals: int = 0   # 买点信号数
    signals: list[str] = field(default_factory=list)  # 具体信号列表
    prominence_labels: list[str] = field(default_factory=list)  # 辨识度标签


# 模块级计数器：每 N 轮给跟踪票拉一次 K 线（节流）
_loop_count: int = 0


def track_recent_recommendations(conn, session, lookback_days: int = TRACK_RECOMMENDATION_DAYS) -> list[TrackedRec]:
    """查询近 N 天推荐，基于指标判断买点状态。

    流程：
    1. 从 recommendations 表查近 N 个交易日（排除今日）的 distinct 推荐
    2. 批量获取实时行情
    3. 硬过滤：排除大涨追高/已错过/暴跌失效
    4. 取 K 线 + 计算指标，判断买点信号
    5. 状态分类：到买点 / 观察中（未到买点的不返回）
    6. 按买点信号数降序、累计收益升序（回调深的优先）排序
    """
    global _loop_count
    _loop_count += 1

    recs = get_recent_recommendations(conn, lookback_days, exclude_today=True)
    if not recs:
        return []

    symbols = [r["symbol"] for r in recs]
    quotes = fetch_market_caps_batch(session, symbols)

    # 每 N 轮给跟踪票拉一次 K 线并写入 DB（节流，避免每轮都拉）
    refresh_kline = (_loop_count % TRACK_KLINE_REFRESH_LOOPS == 1)

    result: list[TrackedRec] = []
    for r in recs:
        sym = r["symbol"]
        q = quotes.get(sym)
        if not q or not q.get("current"):
            continue
        current = q["current"]
        today_pct = q.get("percent", 0) or 0

        # 取推荐日收盘价算累计收益
        kline = get_cached_kline(conn, sym)
        rec_close = _get_rec_day_close(kline, r["date"])
        if rec_close <= 0:
            # DB 无 K 线，尝试拉一次
            kline = _fetch_and_save_kline(conn, session, sym)
            rec_close = _get_rec_day_close(kline, r["date"])
            if rec_close <= 0:
                continue
        cum_return = (current - rec_close) / rec_close * 100

        # 硬过滤：排除不能买的
        if today_pct >= TRACK_FILTER_TODAY_HIGH:
            continue
        if today_pct <= TRACK_FILTER_TODAY_LOW:
            continue
        if cum_return >= TRACK_FILTER_CUM_HIGH:
            continue
        if cum_return <= TRACK_FILTER_CUM_LOW:
            continue

        # 节流刷新 K 线（用于指标计算的最新数据）
        if refresh_kline:
            fresh = _fetch_and_save_kline(conn, session, sym)
            if fresh:
                kline = fresh

        # 指标判断买点信号
        status, buy_signals, signals = _evaluate_buy_signals(kline)
        if not status:
            continue  # 未到买点，过滤

        result.append(TrackedRec(
            symbol=sym, name=r["name"],
            rec_date=r["date"], rec_category=r["category"],
            rec_score=r["score"], rec_percent=r["percent"],
            current=current, today_pct=today_pct,
            cum_return=cum_return,
            status=status, buy_signals=buy_signals, signals=signals,
        ))
        # 辨识度标签
        try:
            recs = get_symbol_appearances(conn, sym, PROMINENCE_LOOKBACK_DAYS)
            valid_ranks = [r["rank"] for r in recs if r.get("rank") and r["rank"] > 0]
            cnt = count_recent_appearances(conn, sym, PROMINENCE_LOOKBACK_DAYS)
            if cnt >= PROMINENCE_REPEAT_THRESHOLD and valid_ranks:
                avg_rank = sum(valid_ranks) / len(valid_ranks)
                if avg_rank <= PROMINENCE_MAX_AVG_RANK:
                    result[-1].prominence_labels.append("\u21bb")
        except Exception:
            pass

    # 排序：辨识度优先 → 买点信号数降序 → 累计收益升序（回调深的优先）
    result.sort(key=lambda x: (not bool(x.prominence_labels), -x.buy_signals, x.cum_return))
    return result


def _evaluate_buy_signals(kline: list[dict] | None) -> tuple[str, int, list[str]]:
    """基于技术指标判断买点信号。

    6 个买点维度：
    1. MA20 支撑：|close-MA20|/MA20 < 3% 且 MA20 上行
    2. 缩量回调：vol_ratio < 0.8
    3. 未破位：close > MA20
    4. RSI 合理区：30 < RSI < 50
    5. BOLL 中轨附近：距 BOLL 中轨 ±3% 内
    6. MACD 未死叉：MACD > signal 或刚死叉（histogram > -0.01）

    返回：(状态, 信号数, 信号列表)。状态为空表示"未到买点"（过滤）。
    """
    if not kline or len(kline) < 20:
        return "", 0, []

    # 排除今日 bar（用历史 K 线算指标，避免盘中实时价干扰）
    today_str = now_beijing().strftime("%Y-%m-%d")
    historical = [k for k in kline if k.get("close") and k.get("date") != today_str]
    if len(historical) < 20:
        return "", 0, []

    closes = [k["close"] for k in historical]
    volumes = [k["volume"] for k in historical]
    last_close = closes[-1]
    signals: list[str] = []

    # 1. MA20 支撑
    ma20 = compute_ma(closes, 20)
    # MA20 前一日值：需要至少 21 根收盘价（前 20 根算昨日 MA20）
    ma20_prev = compute_ma(closes[:-1], 20) if len(closes) >= 21 else None
    if ma20 and ma20 > 0:
        dev_pct = abs(last_close - ma20) / ma20 * 100
        ma20_up = (ma20_prev and ma20 > ma20_prev * (1 + TRACK_MA20_SLOPE_MIN / 100))
        if dev_pct < TRACK_MA20_SUPPORT_PCT and ma20_up:
            signals.append("MA20支撑")

    # 2. 缩量回调
    if len(volumes) >= 6:
        avg_vol = sum(volumes[-6:-1]) / 5
        today_vol = volumes[-1]
        vol_ratio = today_vol / avg_vol if avg_vol > 0 else 1.0
        if vol_ratio < TRACK_VOL_SHRINK_RATIO:
            signals.append("缩量")

    # 3. 未破位
    if ma20 and last_close > ma20:
        signals.append("未破位")

    # 4. RSI 合理区
    rsi = compute_rsi(closes, period=14)
    if rsi is not None and TRACK_RSI_LOW < rsi < TRACK_RSI_HIGH:
        signals.append("RSI合理")

    # 5. BOLL 中轨附近
    boll = compute_bollinger_bands(closes)
    if boll and boll["middle"] > 0:
        dev_pct = abs(last_close - boll["middle"]) / boll["middle"] * 100
        if dev_pct < TRACK_BOLL_MID_PCT:
            signals.append("BOLL中轨")

    # 6. MACD 未死叉（histogram > -0.01 视为刚死叉但未发散）
    macd = compute_macd(closes)
    if macd and macd["histogram"] > -0.01:
        signals.append("MACD未死叉")

    count = len(signals)
    if count >= TRACK_STATUS_BUY:
        status = "到买点"
    elif count >= TRACK_STATUS_WATCH:
        status = "观察中"
    else:
        status = ""  # 未到买点，过滤

    return status, count, signals


def _fetch_and_save_kline(conn, session, symbol: str) -> list[dict] | None:
    """拉取 K 线并写入 DB。失败时返回 None。"""
    try:
        kline = fetch_kline(session, symbol, KLINE_FETCH_DAYS)
        if kline:
            save_kline_to_db(conn, symbol, kline)
        return kline
    except Exception:
        return None


def _get_rec_day_close(kline: list[dict] | None, rec_date: str) -> float:
    """从 K 线数据中取推荐日的收盘价。

    推荐日当天 K 线可能尚未落库（盘中推荐）或已落库。
    若找不到精确匹配，取 rec_date 之前最近的收盘价作为基准。
    """
    if not kline:
        return 0.0
    for k in kline:
        if k["date"] == rec_date:
            return k.get("close", 0) or 0
    before = [k for k in kline if k["date"] <= rec_date]
    if before:
        return before[-1].get("close", 0) or 0
    return 0.0
