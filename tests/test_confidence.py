from scanner.models import Candidate, KlineSummary, StockInfo
from scanner.confidence import compute_confidence, select_picks
from scanner.config import CONF_MIN, CONF_TOP_N, CONF_EXCLUDE_ACCUM


def _c(symbol="300999", category="momentum", score=30, accumulated_pct=15.0,
       volume_ratio=1.2, dimensions=None, source_tag="xueqiu"):
    stock = StockInfo(symbol=symbol, name="测试", code=symbol,
                       percent=5.0, current=15.0, value=5000,
                       rank_change=100, rank=50, source_tag=source_tag)
    kline = KlineSummary(
        trend="test", accumulated_pct=accumulated_pct,
        volume_ratio=volume_ratio, bottom_confirmed=True,
        score=score, dimensions=dimensions or {}, avg_volume=1000.0,
    )
    return Candidate(stock=stock, category=category, score=score,
                   reason="test", kline=kline)


class TestComputeConfidence:
    def test_overbought_excluded(self):
        # 鱼尾段超买（v_st_overbought / v_mo_overbought）-> 确定性归零、不进精选
        c = _c(dimensions={"v_st_overbought": True, "validation_bonus": 10})
        conf = compute_confidence([c], conn=None, list_prescence={})
        assert conf[c.stock.symbol] == 0
        assert select_picks([c], conf) == []

    def test_accum_too_high_excluded(self):
        # 累计涨幅过高（>CONF_EXCLUDE_ACCUM）-> 鱼尾风险，硬排除
        c = _c(accumulated_pct=CONF_EXCLUDE_ACCUM + 5,
                dimensions={"validation_bonus": 10, "momentum_no_crash": 1})
        conf = compute_confidence([c], conn=None, list_prescence={})
        assert conf[c.stock.symbol] == 0

    def test_validation_bonus_zero_excluded(self):
        # validator 未给正共振（validation_bonus<=0）-> 未过交叉验证，硬排除
        c = _c(dimensions={"validation_bonus": 0, "momentum_no_crash": 1})
        conf = compute_confidence([c], conn=None, list_prescence={})
        assert conf[c.stock.symbol] == 0

    def test_crash_day_excluded(self):
        # momentum 含 crash day（缺 momentum_no_crash）-> 硬排除
        c = _c(dimensions={"validation_bonus": 10})
        conf = compute_confidence([c], conn=None, list_prescence={})
        assert conf[c.stock.symbol] == 0

    def test_cross_source_rps_boost(self):
        # 双源 + RPS>0 + 轨迹正向 + 无超买 + MA多头 + 黄金累计区间
        # -> 确定性远高于单一弱信号候选
        strong = _c(symbol="300001", source_tag="both",
                   accumulated_pct=18.0, volume_ratio=1.3,
                   dimensions={
                       "validation_bonus": 10, "momentum_no_crash": 1,
                       "rps_bonus": 5, "sector_bonus": 3,
                       "momentum_ma_bull": 6, "intraday_score": 3,
                       "opening_score": 2, "live_vol_bonus": 2,
                   })
        weak = _c(symbol="300002", accumulated_pct=18.0, volume_ratio=1.3,
                   dimensions={"validation_bonus": 10, "momentum_no_crash": 1})
        conf = compute_confidence([strong, weak], conn=None,
                                 list_prescence={"300001": 2})
        assert conf["300001"] > conf["300002"], \
            f"双源+多信号应高于单一弱信号: {conf}"

    def test_top_n_limit(self):
        # 多只达标时只取 Top CONF_TOP_N（默认 2）
        cs = []
        for i in range(5):
            cs.append(_c(symbol=f"30010{i}", accumulated_pct=18.0,
                          volume_ratio=1.3,
                          dimensions={
                              "validation_bonus": 10, "momentum_no_crash": 1,
                              "rps_bonus": 5, "sector_bonus": 3,
                              "momentum_ma_bull": 6, "intraday_score": 3,
                              "opening_score": 2, "live_vol_bonus": 2,
                          }))
        conf = compute_confidence(cs, conn=None,
                                 list_prescence={s.stock.symbol: 2 for s in cs})
        picks = select_picks(cs, conf)
        assert len(picks) == CONF_TOP_N, f"应只取 Top {CONF_TOP_N}, got {len(picks)}"

    def test_empty_shows_cash(self):
        # 无达标候选 -> picks 为空（display 层渲染"空仓"）
        c = _c(dimensions={"v_mo_overbought": True, "validation_bonus": 10})
        conf = compute_confidence([c], conn=None, list_prescence={})
        assert select_picks([c], conf) == []
