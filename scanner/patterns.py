"""K线组合形态识别模块。

纯函数，输入 historical_kline（不含今日），输出形态标签与加分。
每个形态最多 +5 分，各形态互斥（某个形态命中后不继续匹配同优先级组）。

过度设计消除：阳包阴 / 锤子线 / 3连阳 / 突破3日高点 原分散在 5 个 detect_* 函数里
各复制实现一遍（阈值/打分/维度键不同，但算法完全相同）。现抽出 4 个共享原语，
每个 detect_* 仅声明"用哪些原语 + 各自打分/维度键"，消除算法级重复。
"""

from __future__ import annotations

from scanner.models import KlineBar


def _candle_body_len(o: float, c: float) -> float:
    return abs(c - o)


def _upper_shadow_len(h: float, o: float, c: float) -> float:
    return h - max(o, c)


def _lower_shadow_len(o: float, c: float, low: float) -> float:
    return min(o, c) - low


def _is_bullish_candle(o: float, c: float) -> bool:
    return c > o


# ----------------------------------------------------------------------------
# 共享原语：算法只实现一次，下游按策略组合
# ----------------------------------------------------------------------------


def _bullish_engulfing(
    latest: KlineBar, prev: KlineBar, require_crash: bool = False, prev_prev_close: float | None = None
) -> bool:
    """阳包阴：今日阳线实体完全覆盖昨日阴线实体。

    require_crash=True 时额外要求昨日为暴跌(收盘跌幅<= -5%)，用于超跌反弹的
    "暴跌后吞没" 变体。此时需传入 prev_prev_close（前日收盘）以计算昨日日内跌幅；
    若未传入则退化为实体涨跌幅判定（兼容旧调用方，但有跳空失真风险）。
    """
    ho, hc = latest["open"], latest["close"]
    po, pc = prev["open"], prev["close"]
    if not (_is_bullish_candle(ho, hc) and not _is_bullish_candle(po, pc)):
        return False
    # 实体吞没：今收>昨开（实体顶>实体顶）且 今开<昨收（实体底<实体底）
    # 用 po 而非 prev["high"]，后者要求超过昨日最高价，过严，会漏判长上影线的实体吞没
    if hc <= po or ho >= pc:
        return False
    if require_crash:
        # 日内跌幅（收盘对收盘）才是"暴跌"的标准定义；
        # 实体涨跌幅 (pc-po)/po 在跳空时严重失真（高开低走被误判为暴跌）
        if prev_prev_close is not None and prev_prev_close > 0:
            prev_pct = (pc - prev_prev_close) / prev_prev_close * 100
        else:
            prev_pct = (pc - po) / po * 100 if po > 0 else 0
        if prev_pct > -5:
            return False
    return True


def _hammer(latest: KlineBar) -> bool:
    """长下影锤子线：下影线>实体2倍，上影线<实体0.5倍。"""
    ho, hc, hh, hl = latest["open"], latest["close"], latest["high"], latest["low"]
    body = _candle_body_len(ho, hc)
    if body <= 0:
        return False
    upper = _upper_shadow_len(hh, ho, hc)
    lower = _lower_shadow_len(ho, hc, hl)
    return lower > body * 2 and upper < body * 0.5


def _three_bullish(bars: list[KlineBar]) -> bool:
    """3连阳：近3日全阳线且收盘递增。"""
    if len(bars) < 3:
        return False
    last3 = bars[-3:]
    if not all(_is_bullish_candle(k["open"], k["close"]) for k in last3):
        return False
    c3 = [k["close"] for k in last3]
    return c3[0] < c3[1] < c3[2]


def _breakout_3d(latest: KlineBar, bars: list[KlineBar]) -> bool:
    """突破3日高点：今日收盘>近3日最高价（需 bars 长度>=4）。"""
    if len(bars) < 4:
        return False
    hc = latest["close"]
    prev3_highs = [bars[-i - 1]["high"] for i in range(1, 4)]
    return hc > max(prev3_highs)


# ----------------------------------------------------------------------------
# 各策略组合：仅声明原语 + 打分/维度键
# ----------------------------------------------------------------------------


def detect_new_face_patterns(historical_kline: list[KlineBar]) -> tuple[int, dict]:
    """new_face 适用的底部形态：阳包阴、锤子线、3连阳。"""
    if len(historical_kline) < 2:
        return 0, {}
    score = 0
    dims: dict[str, int] = {}
    latest = historical_kline[-1]
    prev = historical_kline[-2]

    if _bullish_engulfing(latest, prev):
        score += 5
        dims["nf_pattern_engulfing"] = 5
    elif _hammer(latest):
        score += 4
        dims["nf_pattern_hammer"] = 4
    elif _three_bullish(historical_kline):
        score += 3
        dims["nf_pattern_3bull"] = 3

    return score, dims


def detect_momentum_patterns(historical_kline: list[KlineBar]) -> tuple[int, dict]:
    """momentum 适用的形态：突破3日高点、3连阳。"""
    if len(historical_kline) < 4:
        return 0, {}
    score = 0
    dims: dict[str, int] = {}
    latest = historical_kline[-1]

    if _breakout_3d(latest, historical_kline):
        score += 5
        dims["mo_pattern_breakout"] = 5
    elif _three_bullish(historical_kline):
        score += 3
        dims["mo_pattern_3bull"] = 3

    return score, dims


def detect_short_term_patterns(historical_kline: list[KlineBar]) -> tuple[int, dict]:
    """short_term 适用的形态：突破3日高点。"""
    if len(historical_kline) < 4:
        return 0, {}
    score = 0
    dims: dict[str, int] = {}
    latest = historical_kline[-1]
    if _breakout_3d(latest, historical_kline):
        score += 5
        dims["st_pattern_breakout"] = 5
    return score, dims


def detect_rebound_patterns(kline: list[KlineBar]) -> tuple[int, dict]:
    """rebound（超跌反弹）适用的形态：底部吞没、锤子线、3连阳企稳。

    与 new_face 形态不同：rebound 形态必须包含今日 bar，
    因为反弹信号是"今日阳线吞没昨日暴跌阴线"。故接收完整 kline（含今日），
    用 kline[-1] 作为今日、kline[-2] 作为昨日。
    """
    if len(kline) < 2:
        return 0, {}
    score = 0
    dims: dict[str, int] = {}
    latest = kline[-1]  # 今日
    prev = kline[-2]  # 昨日
    # 传入前日收盘用于计算昨日日内跌幅，避免实体涨跌幅在跳空时失真
    prev_prev_close = kline[-3]["close"] if len(kline) >= 3 else None

    if _bullish_engulfing(latest, prev, require_crash=True, prev_prev_close=prev_prev_close):
        score += 6
        dims["rb_pattern_engulfing_crash"] = 6
    elif _hammer(latest):
        score += 4
        dims["rb_pattern_hammer"] = 4
    elif _three_bullish(kline):
        score += 3
        dims["rb_pattern_3bull_stabilize"] = 3

    return score, dims
