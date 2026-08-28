"""统一技术指标特征抽取层。

问题背景：原 analysis 与 validator 各策略内部反复调用 compute_rsi /
compute_macd / compute_kdj 等,同一只股票的同一段序列被计算数十次(且无缓存),
既浪费 CPU,又让"指标计算"散落各处、难以统一口径。

本模块提供 build_features(closes, highs, lows, volumes)：一次性计算全部指标,
供 analysis 各 analyze_* 与 validator 各 validate_* 共用,实现"抽特征一次"。

口径约定：各序列均为「历史 K 线」(不含今日),与分析端 _compute_*_indicators
和 validator 各 validate_* 的输入完全一致,确保与旧实现逐值相等、分数语义不变。
"""

from __future__ import annotations

from scanner.indicators import (
    compute_adx,
    compute_atr,
    compute_bollinger_bands,
    compute_kdj,
    compute_ma,
    compute_macd,
    compute_obv,
    compute_rsi,
)

# MA 多头结构评分常量（与 config.py 同源，此处避免循环导入）
_MA_BULL_3_TIER_SCORE = 6   # MA5 > MA10 > MA20（完全多头排列）
_MA_BULL_2_TIER_SCORE = 3   # MA5 > MA10（部分多头）
_MA_BEAR_SCORE = -3         # MA5 <= MA10（空头排列）


def build_features(closes: list[float],
                   highs: list[float] | None = None,
                   lows: list[float] | None = None,
                   volumes: list[float] | None = None) -> dict:
    """一次性计算单只股票的全部技术指标,返回 dict 供下游读取。

    返回的键:
        rsi6 / rsi14       RSI(6) / RSI(14)
        macd                compute_macd 结果 (macd/signal/histogram/histogram_prev)
        boll                compute_bollinger_bands 结果 (含 b_pct)
        kdj / adx / atr    仅在 highs/lows 提供时计算
        obv                仅在 volumes 提供时计算
        ma5_ema/ma10_ema/ma20_ema   EMA 均线(与分析._ma_bull_score / validator._mo_ma_alignment 同约定)
        ma20_sma / ma20_sma_prev    简单 MA20 及其 5 日前窗口
    """
    feats: dict = {}
    feats["rsi6"] = compute_rsi(closes, 6)
    feats["rsi14"] = compute_rsi(closes, 14)
    feats["macd"] = compute_macd(closes)
    feats["boll"] = compute_bollinger_bands(closes)

    if highs is not None and lows is not None:
        feats["kdj"] = compute_kdj(highs, lows, closes)
        feats["adx"] = compute_adx(highs, lows, closes)
        feats["atr"] = compute_atr(highs, lows, closes)

    if volumes is not None:
        feats["obv"] = compute_obv(closes, volumes)

    if len(closes) >= 5:
        feats["ma5_ema"] = compute_ma(closes, 5, ema=True)
    if len(closes) >= 10:
        feats["ma10_ema"] = compute_ma(closes, 10, ema=True)
    if len(closes) >= 20:
        feats["ma20_ema"] = compute_ma(closes, 20, ema=True)
        feats["ma20_sma"] = sum(closes[-20:]) / 20
    if len(closes) >= 25:
        feats["ma20_sma_prev"] = sum(closes[-25:-5]) / 20

    return feats


def ma_alignment_score(closes: list[float], feats: dict | None = None) -> tuple[int, str]:
    """MA 多头结构判定（EMA 口径，analysis/validator 共用）。

    返回 (分数, 细节描述)：
      - MA5 > MA10 > MA20（完全多头）→ +6, "ma_full_5gt10gt20"
      - MA5 > MA10（部分多头）       → +3, "ma_partial_5gt10"
      - MA5 <= MA10（空头排列）      → -3, "ma_none"
      - 数据不足                     →  0, "data_short"

    与 compute_macd 内部 EMA 口径一致，避免 analysis/validator/short_term
    三处各自实现 MA 判定导致的口径漂移。
    """
    if feats is not None:
        ma5 = feats.get("ma5_ema")
        ma10 = feats.get("ma10_ema")
        ma20 = feats.get("ma20_ema")
    else:
        if len(closes) < 10:
            return 0, "data_short"
        ma5 = compute_ma(closes, 5, ema=True)
        ma10 = compute_ma(closes, 10, ema=True)
        ma20 = compute_ma(closes, 20, ema=True) if len(closes) >= 20 else None

    if ma5 is None or ma10 is None:
        return 0, "data_short"

    if ma20 is not None and ma5 > ma10 > ma20:
        return _MA_BULL_3_TIER_SCORE, "ma_full_5gt10gt20"
    if ma5 > ma10:
        return _MA_BULL_2_TIER_SCORE, "ma_partial_5gt10"
    return _MA_BEAR_SCORE, "ma_none"
