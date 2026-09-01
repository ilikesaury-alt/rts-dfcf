"""低吸匹配层（重构 Phase 3）：池 → 排雷 → 低吸语义。

三条匹配链：
  Chain A — 在榜回调观察：在榜 + 当日翻绿（close<open 且近期有涨幅）→ watch_pool 标记
  Chain B — 次日转强 → rebound 推荐：观察票满足企稳信号（缩量/MA20支撑/转涨≥2%）
            → 产出 rebound 语义候选（复用 comeback._evaluate_buy_signals 五维信号）
  Chain C — 既有 rebound/comeback 入口原样接入（行为不变，仅换管道）

设计原则：
  - 不评分、不分类——只产出 match 记录，由 orchestrator 接入 ranking
  - fail-open：单票数据缺失跳过，不中断整轮
  - 阈值集中 config（DANGER_* / COMEBACK_REENTRY_* / REBOUND_*），本模块不硬编码
"""

from dataclasses import dataclass
from dataclasses import replace as dataclass_replace

from scanner.config import (
    COMEBACK_REENTRY_BOLL_MID_PCT,
    COMEBACK_REENTRY_MA20_SLOPE_MIN,
    COMEBACK_REENTRY_MA20_SUPPORT_PCT,
    COMEBACK_REENTRY_RSI_HIGH,
    COMEBACK_REENTRY_RSI_LOW,
    COMEBACK_REENTRY_STATUS_BUY,
    COMEBACK_REENTRY_VOL_SHRINK_RATIO,
    REBOUND_MIN_SCORE,
    now_beijing,
)
from scanner.indicators import (
    compute_bollinger_bands,
    compute_ma,
    compute_macd,
    compute_rsi,
)
from scanner.models import Candidate, KlineBar, StockInfo
from scanner.validator import validate

# ── Chain A 阈值（在榜回调观察）──
_WATCH_TODAY_RED_MAX_PCT = -0.5  # 今日涨幅 ≤ 此值 → 翻绿（收盘<开盘 且 涨幅转负）
_WATCH_RECENT_GAIN_MIN = 2.0  # 近 3 日至少一日涨幅 ≥ 此值 → 近期有涨幅（非持续阴跌）


@dataclass
class MatchResult:
    """匹配层产出。"""

    watch_additions: list[dict]  # Chain A: 需加入 watch_pool 的票
    rebound_candidates: list[Candidate]  # Chain B: 企稳转强 → rebound 候选


def _kline_today(klines_sym: list[dict], today: str) -> dict | None:
    """取当日 bar；无今日 bar 返回 None。"""
    for k in klines_sym:
        if k.get("date") == today:
            return k
    return None


def _prev_close(kline_today: dict) -> float | None:
    """由当日 bar 反推昨收。"""
    close = kline_today.get("close")
    pct = kline_today.get("percent")
    if not isinstance(close, (int, float)) or close <= 0:
        return None
    if not isinstance(pct, (int, float)):
        return None
    denom = 1.0 + pct / 100.0
    return close / denom if denom != 0 else None


# ── Chain A: 在榜回调观察 ──────────────────────────────────────────────


def chain_a_watch(
    pool_rows: list,
    klines: dict,
    today: str,
    danger_map: dict[str, list[str]],
    v1_syms: set[str],
) -> list[dict]:
    """在榜回调观察：在榜 + 当日翻绿 + 近期有涨幅 + 无排雷信号 → 加入 watch_pool。

    返回需 upsert_watch_symbols 的元数据列表。
    """
    additions: list[dict] = []
    for row in pool_rows:
        if not row.on_board:
            continue
        # 已被 v1 推荐的票不需要观察（已有评分管道）
        if row.symbol in v1_syms:
            continue
        # 有排雷信号的票不观察（已被 danger 标记）
        if danger_map.get(row.symbol):
            continue

        kl = klines.get(row.symbol) or []
        kt = _kline_today(kl, today)
        if not kt:
            continue

        today_pct = kt.get("percent")
        if not isinstance(today_pct, (int, float)):
            continue

        # 翻绿：今日涨幅 ≤ 阈值（收盘<开盘 的量化表达）
        if today_pct > _WATCH_TODAY_RED_MAX_PCT:
            continue

        # 近期有涨幅：近 3 日（不含今日）至少一日涨幅 ≥ 阈值
        hist = [k for k in kl if k.get("date") != today]
        recent_pcts = [k.get("percent", 0) for k in hist[-3:]]
        if not any(p >= _WATCH_RECENT_GAIN_MIN for p in recent_pcts):
            continue

        additions.append(
            {
                "symbol": row.symbol,
                "name": row.name,
                "last_list_date": today,
                "over_limit": False,
            }
        )

    return additions


# ── Chain B: 次日转强 → rebound 推荐 ──────────────────────────────────


def _stabilization_signals(historical: list[KlineBar]) -> tuple[int, list[str]]:
    """五维企稳信号判定（复用 comeback._evaluate_buy_signals 语义）。

    返回 (信号数, 信号列表)。信号数 ≥ COMEBACK_REENTRY_STATUS_BUY → 到买点。
    """
    if len(historical) < 20:
        return 0, []
    closes = [k["close"] for k in historical]
    volumes = [k["volume"] for k in historical]
    last_close = closes[-1]
    signals: list[str] = []

    # 1. MA20 支撑
    ma20 = compute_ma(closes, 20)
    ma20_prev = compute_ma(closes[:-1], 20) if len(closes) >= 21 else None
    if ma20 and ma20 > 0 and last_close > ma20:
        dev_pct = abs(last_close - ma20) / ma20 * 100
        ma20_up = bool(ma20_prev and ma20 > ma20_prev * (1 + COMEBACK_REENTRY_MA20_SLOPE_MIN / 100))
        if dev_pct < COMEBACK_REENTRY_MA20_SUPPORT_PCT and ma20_up:
            signals.append("均线支撑")

    # 2. 缩量回调
    if len(volumes) >= 6:
        avg_vol = sum(volumes[-6:-1]) / 5
        vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 0.0
        if vol_ratio < COMEBACK_REENTRY_VOL_SHRINK_RATIO:
            signals.append("缩量")

    # 3. RSI 合理区
    rsi = compute_rsi(closes, period=14)
    if rsi is not None and COMEBACK_REENTRY_RSI_LOW < rsi < COMEBACK_REENTRY_RSI_HIGH:
        signals.append("RSI合理")

    # 4. BOLL 中轨附近
    boll = compute_bollinger_bands(closes)
    if boll and boll["middle"] > 0:
        dev_pct = abs(last_close - boll["middle"]) / boll["middle"] * 100
        if dev_pct < COMEBACK_REENTRY_BOLL_MID_PCT:
            signals.append("BOLL中轨")

    # 5. MACD 未死叉
    macd = compute_macd(closes)
    if macd and macd["histogram"] > -0.01:
        signals.append("MACD未死叉")

    return len(signals), signals


def chain_b_rebound(
    watch_symbols: list[dict],
    klines: dict,
    today: str,
    clusters: dict[str, list[str]] | None = None,
) -> list[Candidate]:
    """次日转强 → rebound 推荐：观察票满足企稳信号（≥COMEBACK_REENTRY_STATUS_BUY 维）→ rebound 语义。

    复用 comeback 的五维信号判定逻辑 + analyze_rebound 评分。
    """
    candidates: list[Candidate] = []
    for meta in watch_symbols:
        sym = meta["symbol"]
        kl = klines.get(sym)
        if not kl or not any(k.get("date") == today for k in kl):
            continue

        hist = [k for k in kl if k.get("date") != today]
        if len(hist) < 20:
            continue

        # 企稳信号判定
        signal_count, signals = _stabilization_signals(hist)
        if signal_count < COMEBACK_REENTRY_STATUS_BUY:
            continue

        # 今日涨幅检查：需有正涨幅（确认转强，非继续下跌）
        kt = _kline_today(kl, today)
        if not kt:
            continue
        today_pct = kt.get("percent")
        if not isinstance(today_pct, (int, float)) or today_pct < 2.0:
            continue

        # 构建 StockInfo 供 analyze_rebound 使用
        stock = StockInfo(
            symbol=sym,
            name=meta.get("name", sym),
            code=sym[2:] if len(sym) > 2 else sym,
            percent=today_pct,
            current=kt.get("close", 0),
            value=0.0,
            rank_change=0,
            rank=0,
            source_tag="matcher",
        )

        # 复用 analyze_rebound 评分
        from scanner.analysis import analyze_rebound

        ks = analyze_rebound(stock, kl, today_str=today)
        if ks is None or ks.score < REBOUND_MIN_SCORE:
            continue

        # validate 过滤
        historical_kline = [k for k in kl if k["date"] != today]
        closes = [k["close"] for k in historical_kline]
        passed, bonus, vdims = validate("rebound", stock, ks, closes, historical_kline, clusters)
        if not passed:
            continue

        # 合并信号维度
        new_dims = dict(ks.dimensions)
        new_dims["validation_bonus"] = bonus
        new_dims.update(vdims)
        new_dims["matcher_signals"] = "/".join(signals)
        new_dims["matcher_signal_count"] = signal_count
        ks = dataclass_replace(ks, dimensions=new_dims, trend=f"转强·{ks.trend}")

        candidates.append(
            Candidate(
                stock=stock,
                category="rebound",
                score=ks.score,
                reason=ks.trend,
                kline=ks,
                first_seen=now_beijing().strftime("%H:%M"),
                history_pct=[k.get("percent", 0) for k in kl],
            )
        )

    return candidates


# ── 主入口 ──────────────────────────────────────────────────────────────


def match(
    pool_rows: list,
    danger_map: dict[str, list[str]],
    klines: dict,
    today: str,
    v1_candidates: list[Candidate],
    clusters: dict[str, list[str]] | None = None,
) -> MatchResult:
    """低吸匹配层主入口。

    1. Chain A: 在票回调观察 → watch_pool 候选
    2. Chain B: 已有 watch_pool 票企稳转强 → rebound 候选
    3. Chain C: 既有 rebound/comeback 由 orchestrator 直接接入，不在本函数处理

    v1_candidates: v1 管道已产出的候选（用于去重——已被 v1 推荐的票不再观察）
    """
    v1_syms = {c.stock.symbol for c in v1_candidates}

    # Chain A: 在榜回调观察
    watch_additions = chain_a_watch(pool_rows, klines, today, danger_map, v1_syms)

    # Chain B: 已有 watch_pool 票 + Chain A 新增票 → 企稳转强评估
    # Chain A 新增票的 last_eval_date 尚未设置，本轮即可评估
    all_watch = watch_additions  # 新增的直接参与评估
    rebound_candidates = chain_b_rebound(all_watch, klines, today, clusters)

    return MatchResult(
        watch_additions=watch_additions,
        rebound_candidates=rebound_candidates,
    )


# ── v2 语义标签层（不淘汰，只标注）──────────────────────────────────────

_DIP_LABELS = ("超跌反转", "缩量回调", "均线支撑", "放量突破", "弱转强")


def _detect_dip_labels(c: Candidate, klines: dict, today: str) -> list[str]:
    """检测单票的低吸语义标签，返回命中的标签列表（可能为空）。"""
    kl = klines.get(c.stock.symbol)
    if not kl:
        return []

    labels: list[str] = []
    hist = [k for k in kl if k.get("date") != today]
    kt = next((k for k in kl if k.get("date") == today), None)
    if not hist or not kt:
        return []

    closes = [k["close"] for k in hist]
    today_pct = kt.get("percent", 0.0)
    today_volume = kt.get("volume", 0.0)

    # 1. 超跌反转：近5日跌≥10% + 今日企稳（2~8%）
    if len(closes) >= 5:
        base5 = closes[-5]
        if base5 > 0:
            drop5 = (closes[-1] - base5) / base5 * 100.0
            if drop5 <= -10.0 and 2.0 <= today_pct <= 8.0:
                labels.append("超跌反转")

    # 2. 缩量回调：量比<0.8 + 近3日有涨幅
    if len(hist) >= 4:
        avg_vol = sum(k.get("volume", 0) for k in hist[-4:-1]) / 3
        vol_ratio = today_volume / avg_vol if avg_vol > 0 else 0.0
        recent_pcts = [k.get("percent", 0) for k in hist[-3:]]
        if vol_ratio < 0.8 and any(p >= 1.0 for p in recent_pcts):
            labels.append("缩量回调")

    # 3. 均线支撑：距MA20<3% + MA20上行
    if len(closes) >= 21:
        ma20 = compute_ma(closes, 20)
        ma20_prev = compute_ma(closes[:-1], 20)
        if ma20 and ma20 > 0:
            dev_pct = abs(closes[-1] - ma20) / ma20 * 100.0
            ma20_up = bool(ma20_prev and ma20 > ma20_prev)
            if dev_pct < 3.0 and ma20_up:
                labels.append("均线支撑")

    # 4. 放量突破：量比>1.5 + rank上升
    if len(hist) >= 4:
        avg_vol = sum(k.get("volume", 0) for k in hist[-4:-1]) / 3
        vol_ratio = today_volume / avg_vol if avg_vol > 0 else 0.0
        if vol_ratio > 1.5 and c.kline and c.kline.dimensions.get("rank_trend", 0) > 0:
            labels.append("放量突破")

    # 5. 弱转强：昨日长上影 + 今日反弹
    if len(hist) >= 1:
        yest = hist[-1]
        yest_high = yest.get("high", 0)
        yest_close = yest.get("close", 0)
        yest_open = yest.get("open", 0)
        if yest_close > 0 and yest_open > 0:
            upper_shadow_pct = (yest_high - max(yest_open, yest_close)) / yest_close * 100.0
            if upper_shadow_pct >= 3.0 and today_pct >= 2.0:
                labels.append("弱转强")

    return labels


def label_all_candidates(candidates: list[Candidate], klines: dict, today: str) -> None:
    """对全量安全票打低吸语义标签（不淘汰任何票）。

    标签写入 c.kline.dimensions["dip_labels"]，供 ranking 和展示层消费。
    """
    for c in candidates:
        if not c.kline:
            continue
        try:
            labels = _detect_dip_labels(c, klines, today)
            c.kline.dimensions["dip_labels"] = labels
        except Exception:
            # fail-open: 单票异常跳过
            c.kline.dimensions["dip_labels"] = []
