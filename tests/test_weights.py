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
    NEW_FACE_FIRST_MIN_SCORE,
    MOMENTUM_MIN_SCORE,
    SHORT_TERM_WEIGHTS,
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


def test_new_face_first_min_score_split():
    # 2026-08-10: 首日 new_face 全档负收益（1018 条 cum_3d -1.58）提门槛砍量；
    # known_new_face 分数反指（低分档最优）保持低门槛，二者必须拆开，不得同步抬高。
    assert NEW_FACE_FIRST_MIN_SCORE > NEW_FACE_MIN_SCORE
    assert NEW_FACE_FIRST_MIN_SCORE >= 50


def test_momentum_min_score_raised():
    # P1-8 (2026-08-10): 16→50——回测分桶 <50 档 55 条 cum_3d -0.95%，>=50 档 379 条 +2.82%，
    # 「首次启动」子模式分数实测全 >=64 不受影响。
    assert MOMENTUM_MIN_SCORE == 50


def test_short_term_today_pct_weight_rebalanced():
    # P1-8 (2026-08-10): 分桶数据 4-6% 档最差(-1.41%, n=41) 但原权重最高(20)、
    # 8-12% 档最好(+3.84%, n=21) 但原权重最低(8)——按数据反向修正，消除"涨幅越大越降权"的拍脑袋设定。
    assert SHORT_TERM_WEIGHTS["today_pct_4_6"] < SHORT_TERM_WEIGHTS["today_pct_2_4"]
    assert SHORT_TERM_WEIGHTS["today_pct_8_12"] > SHORT_TERM_WEIGHTS["today_pct_6_8"]


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


def test_volume_surge_raised():
    # new_face_volume 是 next_day 最强正 IC 维度(+0.244)，从 8 上调到 10
    assert NEW_FACE_WEIGHTS["volume_surge"] == 10


def test_momentum_value_raised():
    # momentum_value 次强正 IC 维度(+0.215)，从 2 上调到 3
    assert MOMENTUM_WEIGHTS["value_gte_10000"] == 3
