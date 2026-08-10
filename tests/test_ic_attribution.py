"""Step 2 回归测试：new_face 维度重平衡（IC 归因驱动）。

锁定两条不变量：
1. NEW_FACE_WEIGHTS 中 volume_surge 已归零（IC 为负的动量确认信号不再加分）。
2. 在「超卖反转」特征分布下，reconstruct_score 给真正的反转触发股（KDJ/MACD/RSI）
   的打分高于「动量确认」股（今日大涨+放量+更高低），即引擎重心回到反转信号。
"""
from scanner.config import MA_BULL_3_TIER_SCORE, NEW_FACE_WEIGHTS
from scanner.ic_attribution import reconstruct_score


def _good_reversal_feats():
    """真正有预测力的超卖反转画像：KDJ金叉 + MACD金叉 + RSI<30 + BOLL破下轨。"""
    return {
        "today_pct": 1.5, "accumulated": -8.0,
        "sig_vol_surge": False, "sig_bottom_confirmed": False,
        "sig_macd_cross": True, "sig_kdj": True, "sig_boll_oversold": True,
        "rsi6": 25.0, "rsi14": 28.0, "atr_pct": 2.5,
        "obv_trend": 1, "ma_bull": 0.0,
    }


def _bad_momentum_feats():
    """IC 为负的动量确认画像：今日大涨 + 放量 + 更高低 + 无超卖信号。"""
    return {
        "today_pct": 5.0, "accumulated": 8.0,
        "sig_vol_surge": True, "sig_bottom_confirmed": False,
        "sig_macd_cross": False, "sig_kdj": False, "sig_boll_oversold": False,
        "rsi6": 55.0, "rsi14": 58.0, "atr_pct": 6.0,
        "obv_trend": -1, "ma_bull": float(MA_BULL_3_TIER_SCORE),
    }


def test_volume_surge_weight_zeroed():
    # IC 归因：放量（volume_surge）cum_3d 上均值 -3.11% vs 未触发 -0.53%，IC 负。
    assert NEW_FACE_WEIGHTS["volume_surge"] == 0


def test_kdj_upweighted_over_rsi():
    # KDJ(K<20金叉/J<0) 是最佳信号（触发组胜率 51.5% vs 33.6%），应强于 RSI 加分。
    assert NEW_FACE_WEIGHTS["kdj_bonus"] >= NEW_FACE_WEIGHTS["rsi_bonus"]


def test_reversal_outranks_momentum():
    good = reconstruct_score(_good_reversal_feats(), NEW_FACE_WEIGHTS)
    bad = reconstruct_score(_bad_momentum_feats(), NEW_FACE_WEIGHTS)
    assert good > bad, f"反转股({good}) 应高于动量确认股({bad})"


def test_volume_surge_no_longer_affects_score():
    # 切换 sig_vol_surge 不应改变打分（权重已归零）。
    f_on = _bad_momentum_feats()
    f_off = dict(f_on, sig_vol_surge=False)
    assert reconstruct_score(f_on, NEW_FACE_WEIGHTS) == reconstruct_score(f_off, NEW_FACE_WEIGHTS)
