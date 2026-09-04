"""danger 排雷器测试：当日信号只认 date==today 的 bar（2026-09-04 审查修复回归）。

此前 evaluate_pool 用 kl[-1] 当"今日 bar"且无日期校验：K 线补拉失败（缺今日
bar）时会拿昨日形态触发 DANGER_OVERSHOOT / DANGER_TURNED_RED_GAP，误杀 v2 候选。
"""

from types import SimpleNamespace

from scanner.danger import (
    DANGER_MAIN_OUTFLOW,
    DANGER_OVERSHOOT,
    DANGER_TURNED_RED_GAP,
    evaluate_pool,
    hard_flags,
    soft_flags,
)

TODAY = "2026-09-04"
YESTERDAY = "2026-09-03"


def _row(symbol: str = "SZ300999", bias20: float | None = None):
    return SimpleNamespace(symbol=symbol, bias20=bias20)


def _bar(date: str, open_: float, close: float, high: float, percent: float) -> dict:
    return {"date": date, "open": open_, "close": close, "high": high, "percent": percent}


class TestEvaluatePoolTodayBarGuard:
    def test_stale_bar_no_day_signals(self):
        """末位 bar 是昨日（补拉失败场景），形态本身是冲高回落+翻绿高开回落，
        但 date != today → 不得消费昨日 bar 误报当日信号。"""
        klines = {"SZ300999": [_bar(YESTERDAY, 11.0, 10.5, 12.0, 2.0)]}
        flags = evaluate_pool([_row()], klines, {}, {}, today=TODAY)["SZ300999"]
        assert DANGER_OVERSHOOT not in flags
        assert DANGER_TURNED_RED_GAP not in flags

    def test_today_bar_signals_fire(self):
        """同一形态、date==today → 正常触发两个当日硬信号。

        prev_close = 10.5/1.02 ≈ 10.29：high=12 → high_pct≈16.6%，drop≈14.6% ≥10
        → 冲高回落；open=11 > prev_close 且 close=10.5 < open → 翻绿+高开回落。
        """
        klines = {
            "SZ300999": [
                _bar(YESTERDAY, 10.0, 10.4, 10.5, 1.0),
                _bar(TODAY, 11.0, 10.5, 12.0, 2.0),
            ]
        }
        flags = evaluate_pool([_row()], klines, {}, {}, today=TODAY)["SZ300999"]
        assert DANGER_OVERSHOOT in flags
        assert DANGER_TURNED_RED_GAP in flags
        # 信号分级：冲高回落属 K 线软信号（DANGER_KLINE_SOFT 默认开启 → 不剔除），
        # 翻绿+高开回落为硬信号（实测有害）
        assert DANGER_OVERSHOOT not in hard_flags(flags)
        assert DANGER_OVERSHOOT in soft_flags(flags)
        assert DANGER_TURNED_RED_GAP in hard_flags(flags)

    def test_main_outflow_hard(self):
        """主力净占比 ≤ -5%（DANGER_MAIN_OUTFLOW_PCT）→ 硬剔除信号，与 K 线无关。"""
        flags = evaluate_pool([_row()], {}, {"SZ300999": {"main_pct": -6.0}}, {}, today=TODAY)["SZ300999"]
        assert DANGER_MAIN_OUTFLOW in flags

    def test_soft_kline_flags_demoted(self):
        """bias20 过高（>28%）为软信号：DANGER_KLINE_SOFT 默认开启时不剔除，只标记。"""
        klines = {"SZ300999": [_bar(TODAY, 11.0, 10.5, 12.0, 2.0)]}
        flags = evaluate_pool([_row(bias20=35.0)], klines, {}, {}, today=TODAY)["SZ300999"]
        assert any("bias20" in f for f in flags)
        assert soft_flags(flags), "bias20 应降级为软标记"
        assert not [f for f in hard_flags(flags) if "bias20" in f], "软信号不得进入硬剔除"
