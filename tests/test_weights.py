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
    # P0 IC 重平衡（cum_3d，n=423）：momentum_volume IC=-0.317 强反指，
    # vol_healthy 从 2 清零（vol_surge 早已为 0）。
    assert MOMENTUM_WEIGHTS["vol_healthy"] == 0
    assert MOMENTUM_WEIGHTS["vol_low"] == -3


def test_market_env_downweighted():
    assert MARKET_ENV_STRONG == 2
    assert MARKET_ENV_WEAK == -2


def test_live_vol_downweighted():
    assert LIVE_VOL_BONUS == 3


def test_first_today_downweighted():
    assert FIRST_TODAY_BONUS == 3


def test_new_face_kdj_upweighted():
    # Step 2 (2026-08-07) IC 归因（cum_3d，n=136）：KDJ(K<20金叉/J<0) 是 new_face 中
    # 预测力最强的维度（触发组胜率 51.5% vs 33.6%，均值 -0.15% vs -1.66%，二元 IC +0.40），
    # 故从原 1 强提到 6。先前按 next_day 小样本(n=30)降权，但 3 日持有口径(cum_3d)大样本
    # 证据反转——KDJ 金叉/超卖才是真正的超卖反转触发信号。
    assert NEW_FACE_WEIGHTS["kdj_bonus"] == 6


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


def test_volume_surge_zeroed_for_hold_metric():
    # Step 2 (2026-08-07) IC 归因（cum_3d）：放量(volume_surge) 对 3 日持有收益 IC 为负
    # （触发组均值 -3.11% vs 未触发 -0.53%，胜率 29% vs 40%），故归零。
    # 注：此前按 next_day 口径 IC=+0.244 上调到 10，但「当日冲高、3 日回落」形态说明
    # 放量对新面孔是动量确认而非反转信号；本策略持有 2-3 天（cum_3d 为决策口径），
    # 故不再奖励。若改做「次日了结」变种，应再按 next_day 口径复原。
    assert NEW_FACE_WEIGHTS["volume_surge"] == 0


def test_momentum_value_raised():
    # P0 IC 重平衡（cum_3d，n=423）：momentum_value IC=+0.219 正指，
    # value_gte_10000 从 3 上调到 5（提权小/大市值质量因子）。
    assert MOMENTUM_WEIGHTS["value_gte_10000"] == 5
