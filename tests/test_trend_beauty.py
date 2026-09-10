"""终选走势美感门（scanner.trend_beauty）单元测试。

日线 6 硬门：MA 多头 / 趋势向上 / 无暴跌 / 回调可控 / 无长上影 / 未破位；
分时：intraday_score ≥ INTRADAY_BEAUTY_MIN，0.0 视为缺失 fail-open。
API 口径：返回「丑的理由 | None」，None = 漂亮或无法判定（detail 区分）。
"""

from datetime import timedelta
from types import SimpleNamespace

from scanner.config import INTRADAY_BEAUTY_MIN, now_beijing

# 经终选门单源导入（final_pick 相对 re-export）：pyright 会话快照不含新建子模块，
# 绝对名 scanner.trend_beauty 在其冻结缓存里解析不到，运行时/ mypy 均正常。
from scanner.final_pick import (  # noqa: F401
    DAILY_INSUFFICIENT,
    INTRADAY_MISSING,
    evaluate_daily_trend,
    evaluate_intraday_beauty,
)


def _dates(n: int) -> list[str]:
    """最近 n 个自然日（升序，末日=今天；get_cached_klines 只按日期范围过滤）。"""
    d = now_beijing().date()
    return [(d - timedelta(days=n - 1 - i)).isoformat() for i in range(n)]


def _kline(n: int = 25, step: float = 0.01, mutate=None) -> list[dict]:
    """构造 n 根日 K：默认每日 +1% 稳步上行（默认即「漂亮」K 线）。

    mutate(bars) 可在返回前篡改末几根（构造暴跌/长上影/破位等丑形态）。
    """
    bars = []
    prev = 10.0
    for d in _dates(n):
        close = prev * (1 + step)
        bars.append(
            {
                "date": d,
                "open": round(prev, 3),
                "close": round(close, 3),
                "high": round(close * 1.003, 3),
                "low": round(prev * 0.997, 3),
                "volume": 1000.0,
                "percent": round(step * 100, 2),
                "finalized": 1,
            }
        )
        prev = close
    if mutate:
        mutate(bars)
    return bars


# ── 日线 6 硬门 ──


def test_daily_beautiful_uptrend_passes_all_gates():
    """稳步上行（+1%/日，无上影无回撤）：6 硬门全过 → None（漂亮）。"""
    fail, score, detail = evaluate_daily_trend(_kline())
    assert fail is None
    assert score >= 90
    assert detail == "多头排列"


def test_daily_insufficient_bars_fail_open():
    """不足 DAILY_BEAUTY_MIN_BARS 根 → 无法判定（None + 日线不足），fail-open。"""
    fail, score, detail = evaluate_daily_trend(_kline(n=10))
    assert fail is None and score == 0 and detail == DAILY_INSUFFICIENT
    assert evaluate_daily_trend(None)[2] == DAILY_INSUFFICIENT


def test_daily_downtrend_rejected():
    """阴跌（-1%/日）：MA 空头 + 趋势向下 + 破位。"""
    fail, _score, detail = evaluate_daily_trend(_kline(step=-0.01))
    assert fail is not None
    for gate in ("MA未多头", "趋势向下", "破位"):
        assert gate in fail
    assert gate in detail or detail == fail  # detail 同步


def test_daily_crash_day_rejected():
    """末根 -6% 暴跌：暴跌日 + 深回调（趋势其余部分仍上行）。"""

    def crash(bars):
        last = bars[-1]
        prev_close = bars[-2]["close"]
        last["close"] = round(prev_close * 0.94, 3)
        last["percent"] = -6.0
        last["high"] = last["close"] * 1.001

    fail, _score, detail = evaluate_daily_trend(_kline(mutate=crash))
    assert fail is not None and "暴跌日" in fail and "深回调" in fail


def test_daily_long_upper_shadow_rejected():
    """末根长上影（上影 ~8% vs 昨收）：冲高回落 → 长上影。"""

    def wick(bars):
        bars[-1]["high"] = round(bars[-1]["close"] * 1.08, 3)

    fail, _score, detail = evaluate_daily_trend(_kline(mutate=wick))
    assert fail is not None and "长上影" in fail


# ── 分时美感 ──


def test_intraday_strong_score_passes():
    ok, detail = evaluate_intraday_beauty({}, SimpleNamespace(intraday_score=5.0))
    assert ok is None and "分时+5.0" in detail


def test_intraday_weak_score_rejected():
    fail, detail = evaluate_intraday_beauty({}, SimpleNamespace(intraday_score=-2.0))
    assert fail is not None and "分时不漂亮" in fail and "-2.0" in detail


def test_intraday_below_threshold_rejected():
    """正分但低于阈值（如 +0.5）：走势平淡，不算漂亮。"""
    fail, _detail = evaluate_intraday_beauty({}, SimpleNamespace(intraday_score=0.5))
    assert fail is not None and "分时不漂亮" in fail


def test_intraday_zero_score_treated_as_missing():
    """0.0 = 未评分默认值（开盘前/无分时数据）→ 按缺失 fail-open，不判否。"""
    fail, detail = evaluate_intraday_beauty({}, SimpleNamespace(intraday_score=0.0))
    assert fail is None and detail == INTRADAY_MISSING


def test_intraday_falls_back_to_score_breakdown_dims():
    """无实时候选 → 回退 score_breakdown 落库 dims。"""
    entry = {"score_breakdown": {"intraday_score": INTRADAY_BEAUTY_MIN + 1}}
    fail, _detail = evaluate_intraday_beauty(entry)
    assert fail is None
    entry_neg = {"score_breakdown": {"intraday_score": -1.5}}
    fail2, _d2 = evaluate_intraday_beauty(entry_neg)
    assert fail2 is not None and "分时不漂亮" in fail2


def test_intraday_missing_when_no_candidate_no_dims():
    fail, detail = evaluate_intraday_beauty({}, None)
    assert fail is None and detail == INTRADAY_MISSING


# ── beauty_mark（纯展示单源：只标美不标丑）──


def test_beauty_mark_pass_shows_me():
    from scanner.trend_beauty import BEAUTY_MARK, beauty_mark

    assert beauty_mark({}, _kline(), SimpleNamespace(intraday_score=5.0)) == BEAUTY_MARK == "美"


def test_beauty_mark_ugly_not_marked():
    """不标丑（2026-09-09 用户口径）：丑 → 空串。"""
    from scanner.trend_beauty import beauty_mark

    assert beauty_mark({}, _kline(step=-0.01), SimpleNamespace(intraday_score=-3.0)) == ""


def test_beauty_mark_both_unknown_not_marked():
    """全缺失不标（避免误导）：无日线 + 无候选无 dims → 空串。"""
    from scanner.trend_beauty import beauty_mark

    assert beauty_mark({}, None, None) == ""


def test_intraday_candidate_beats_stale_dims():
    """实时候选优先：候选 +8（漂亮）即使落库 dims 为 -9 也不误判丑。"""
    entry = {"score_breakdown": {"intraday_score": -9.0}}
    fail, detail = evaluate_intraday_beauty(entry, SimpleNamespace(intraday_score=8.0))
    assert fail is None and "分时+8.0" in detail
