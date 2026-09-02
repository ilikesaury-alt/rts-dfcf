"""展示层信号语义（display / feishu 共用，单一真相）。

此前 `fund_flow_signal` 与 `split_risk_flags` 定义在 display.py，但飞书推送
同样依赖它们（五档资金流、风险标签分级）。推送模块因此被迫从「终端渲染模块」
import，依赖方向别扭。

2026-08 重构：把这两个「信号语义」函数迁到本模块，display / feishu 都从这里
import——display 只负责把信号画成终端 ANSI 文本，feishu 只负责画成卡片 lark_md，
两者共享同一套阈值与分级口径（阈值集中在 config.py，避免两处漂移）。
"""

from scanner.config import (
    FUND_FLOW_MAIN_PCT_EXTREME,
    FUND_FLOW_MAIN_PCT_STRONG,
    FUND_FLOW_MAIN_PCT_WEAK,
    RISK_FLAGS_DISPLAY_HARD,
)


def fund_flow_signal(main_pct: float | None) -> str:
    """主力净占比 → 强弱档位（与 enhancer 加分/资金流出标签阈值同源）。

    返回 strong_in / in / neutral / out / strong_out；无数据返回 ""。
    """
    if main_pct is None:
        return ""
    if main_pct >= FUND_FLOW_MAIN_PCT_EXTREME:
        return "strong_in"
    if main_pct >= FUND_FLOW_MAIN_PCT_STRONG:
        return "in"
    if main_pct <= -FUND_FLOW_MAIN_PCT_EXTREME:
        return "strong_out"
    if main_pct <= FUND_FLOW_MAIN_PCT_WEAK:
        return "out"
    return "neutral"


def split_risk_flags(risk_flags: list[str]) -> tuple[list[str], int]:
    """风险标签分级：返回 (硬信号列表, 软信号数量)。

    硬信号（RISK_FLAGS_DISPLAY_HARD：超买/主力出货/趋势破位）展开文字显示，
    软信号折叠成 +N 角标。display/feishu 共用，避免两处各自维护阈值集合。
    """
    hard = [f for f in risk_flags if f in RISK_FLAGS_DISPLAY_HARD]
    return hard, len(risk_flags) - len(hard)
