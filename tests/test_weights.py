"""权重调整回归测试。

基于回测 IC（scanner/backtest.py）对 config.py / analysis.py 的权重改动做断言：
- new_face_bottom 已清零（IC=-0.376，均值回归陷阱）
- new_face_gap_up 已不再加分（IC=-0.180，n=136）
- NEW_FACE_MIN_SCORE 与 bottom_confirmed 清零原子下调（防列表饥饿）
- momentum_volume / market_env / live_vol / first_today 已降权
- new_face_kdj 已降权（小样本，仅弱化）
- sector_bonus 暂缓未动（阶段2新分类未累积）
"""
from scanner.config import (
    NEW_FACE_MIN_SCORE,
    NEW_FACE_WEIGHTS,
    MOMENTUM_WEIGHTS,
    MARKET_ENV_STRONG,
    MARKET_ENV_WEAK,
    LIVE_VOL_BONUS,
    FIRST_TODAY_BONUS,
    SECTOR_CLUSTER_BONUS_5,
    SECTOR_CLUSTER_BONUS_2,
)
from scanner.analysis import analyze_new_face, _detect_gap_up


def test_new_face_bottom_cleared():
    assert NEW_FACE_WEIGHTS["bottom_confirmed"] == 0


def test_new_face_min_score_lowered_with_bottom_clear():
    # bottom_confirmed 清零(10->0) 与 MIN_SCORE 下调(22->18) 必须原子配套，
    # 否则 new_face 列表会因最高减 10 分而饥饿。
    assert NEW_FACE_MIN_SCORE == 18
    assert NEW_FACE_MIN_SCORE < 22


def test_new_face_gap_up_no_longer_scored():
    # analyze_new_face 不再给高开加分：制造一个明显高开的票，确认无 new_face_gap_up 维度。
    from tests.helpers import _kline
    from scanner.models import StockInfo

    def _stock(percent=7.0, rank_change=2000, value=12000, current=20.0):
        return StockInfo(
            symbol="300999", name="测试", code="300999",
            percent=percent, current=current,
            value=value, rank_change=rank_change, rank=10, market_cap=0.0,
        )

    kline = _kline([1, 2, 1, 2, 3], volumes=[1.0] * 5)
    result = analyze_new_face(_stock(percent=7.0, current=20.0), kline)
    if result is not None:
        assert "new_face_gap_up" not in result.dimensions


def test_momentum_volume_downweighted():
    assert MOMENTUM_WEIGHTS["vol_healthy"] == 2
    assert MOMENTUM_WEIGHTS["vol_low"] == -3


def test_market_env_downweighted():
    assert MARKET_ENV_STRONG == 2
    assert MARKET_ENV_WEAK == -2


def test_live_vol_downweighted():
    assert LIVE_VOL_BONUS == 3


def test_first_today_downweighted():
    assert FIRST_TODAY_BONUS == 3


def test_new_face_kdj_downweighted():
    # 小样本(n=30)只降权不消除
    assert NEW_FACE_WEIGHTS["kdj_bonus"] == 1


def test_sector_bonus_unchanged_deferred():
    # 阶段2板块分类刚升级，旧 data 算出的 sector IC 不代表新逻辑，暂缓改动。
    assert SECTOR_CLUSTER_BONUS_5 == 8
    assert SECTOR_CLUSTER_BONUS_2 == 2


def test_gap_up_helper_still_computes():
    # _detect_gap_up 本身仍工作（momentum 侧仍用），仅 new_face 不再采用其结果。
    from tests.helpers import _kline
    kline = _kline([1, 2, 1, 2, 18], volumes=[1.0] * 5)
    gap_pct, pts = _detect_gap_up(20.5, kline, "2026-07-16")
    assert isinstance(pts, int)
