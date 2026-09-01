"""排雷器（重构 Phase 2）：实证危险信号。

只装「卖出/止损」级硬信号，从 validator.py 的 RISK_FLAGS 与 enhancer 主力出货逻辑中
抽离，作用于全量池（PoolRow），不依赖候选评分对象。当前仅影子落库（pool_log），
不进入 v1 推荐。阈值全部来自 config（DANGER_* / REVERSAL_OVERSHOOT_DROP / FUND_RISK_TAG）。

信号（与 enhancer / validator 既有口径对齐，避免双套语义漂移）：
  DANGER_BIAS20       bias20 > DANGER_BIAS20_MAX（高位乖离）
  DANGER_OVERSHOOT    冲高回落 ≥ REVERSAL_OVERSHOOT_DROP（最高涨幅 − 收盘涨幅）
  DANGER_MAIN_OUTFLOW 主力净占比 ≤ DANGER_MAIN_OUTFLOW_PCT（派发）
  DANGER_FINANCIAL    资不抵债（fund_risk 命中，复用 FUND_RISK_TAG）
  DANGER_TURNED_RED_GAP 当日翻绿（close<open）且高开（open>prev_close）→ 高开回落

prev_close 由当日 bar 的 close/(1+percent/100) 反推（KlineBar 无该字段，percent 即
(close−prev_close)/prev_close，反推精确无近似）。

fail-open：单票数据缺失只跳过对应信号，不影响整轮排雷。
"""

import json

from scanner.config import (
    DANGER_BIAS20_MAX,
    DANGER_MAIN_OUTFLOW_PCT,
    FUND_RISK_TAG,
    REVERSAL_OVERSHOOT_DROP,
)

DANGER_BIAS20 = "bias20过高(>28%)"
DANGER_OVERSHOOT = "冲高回落(≥10%)"
DANGER_MAIN_OUTFLOW = "主力出货(资金净流出)"
DANGER_FINANCIAL = FUND_RISK_TAG  # "财务风险"（资不抵债）
DANGER_TURNED_RED_GAP = "当日翻绿+高开回落"


def _prev_close_of(kline_today: dict) -> float | None:
    """由当日 bar 反推昨收：close/(1+percent/100)。percent 即 (close−prev)/prev。"""
    close = kline_today.get("close")
    pct = kline_today.get("percent")
    if not isinstance(close, (int, float)) or close <= 0 or not isinstance(pct, (int, float)):
        return None
    denom = 1.0 + pct / 100.0
    if denom == 0:
        return None
    return close / denom


def check_danger(row, kline_today: dict | None, market_extra_sym: dict | None, fund_risk_reason: str | None) -> list[str]:
    """对单只池票返回命中的危险信号标签列表（可能为空）。"""
    flags: list[str] = []
    if row.bias20 is not None and row.bias20 > DANGER_BIAS20_MAX:
        flags.append(DANGER_BIAS20)

    if kline_today:
        prev_close = _prev_close_of(kline_today)
        high = kline_today.get("high")
        close = kline_today.get("close")
        open_ = kline_today.get("open")
        if prev_close and high and close:
            high_pct = (high - prev_close) / prev_close * 100.0
            drop = high_pct - (kline_today.get("percent") or 0.0)
            if drop >= REVERSAL_OVERSHOOT_DROP:
                flags.append(DANGER_OVERSHOOT)
            if open_ and open_ > prev_close and close < open_:
                flags.append(DANGER_TURNED_RED_GAP)

    if market_extra_sym:
        main_pct = market_extra_sym.get("main_pct")
        if isinstance(main_pct, (int, float)) and main_pct <= DANGER_MAIN_OUTFLOW_PCT:
            flags.append(DANGER_MAIN_OUTFLOW)

    if fund_risk_reason:
        # fund_risk 命中即资不抵债，复用 FUND_RISK_TAG 标签
        flags.append(DANGER_FINANCIAL)

    return flags


def evaluate_pool(pool_rows: list, klines: dict, market_extra: dict, fund_risk: dict) -> dict[str, list[str]]:
    """对全量池逐票排雷，返回 symbol -> 危险信号标签列表。"""
    out: dict[str, list[str]] = {}
    for row in pool_rows:
        kl = klines.get(row.symbol) or []
        kline_today = kl[-1] if kl else None
        me = market_extra.get(row.symbol)
        fr = fund_risk.get(row.symbol)
        out[row.symbol] = check_danger(row, kline_today, me, fr)
    return out


def danger_flags_json(flags: list[str]) -> str:
    """危险信号列表序列化为 JSON 字符串（落库 pool_log.danger_flags）。"""
    return json.dumps(flags, ensure_ascii=False)
