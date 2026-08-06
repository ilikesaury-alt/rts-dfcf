"""历史推荐跟踪显示：默认只显示"到买点"，"观察中"隐藏（TRACK_DISPLAY_WATCH_MAX=0）。"""
from types import SimpleNamespace

import scanner.display as disp_mod
from scanner.models import Candidate, KlineSummary, StockInfo


def _tracked(status: str, name: str, symbol: str) -> SimpleNamespace:
    return SimpleNamespace(
        rec_date="07-28", name=name, symbol=symbol, rec_category="new_face",
        status=status, buy_signals=4, today_pct=1.2, cum_return=-2.0,
        signals=["未破位", "BOLL中轨", "缩量"],
        prominence_labels=[],
    )


def test_tracked_only_buy_point_when_watch_max_zero(monkeypatch, capsys):
    """watch_max=0：仅"到买点"出现，"观察中"整段（含提示）不渲染。"""
    monkeypatch.setattr(disp_mod, "TRACK_DISPLAY_WATCH_MAX", 0)
    disp_mod.display([], [], 0, 60,
                     tracked_recs=[_tracked("到买点", "买点股", "300001"),
                                   _tracked("观察中", "观察股", "300002")])
    out = capsys.readouterr().out
    assert "买点股" in out
    assert "观察股" not in out
    assert "观察中" not in out


def test_tracked_watch_shown_when_watch_max_positive(monkeypatch, capsys):
    """watch_max>0：到买点不足时"观察中"作为补充尾部出现。"""
    monkeypatch.setattr(disp_mod, "TRACK_DISPLAY_WATCH_MAX", 5)
    disp_mod.display([], [], 0, 60,
                     tracked_recs=[_tracked("到买点", "买点股", "300001"),
                                   _tracked("观察中", "观察股", "300002")])
    out = capsys.readouterr().out
    assert "买点股" in out
    assert "观察股" in out


# ── 资金流强弱档位（5 档图标规则，2026-08-06）──
def _candidate(pct):
    k = KlineSummary(trend="", accumulated_pct=0.0, volume_ratio=1.0,
                     bottom_confirmed=False, score=50,
                     dimensions={} if pct is None else {"fund_flow_main_pct": pct})
    return Candidate(
        stock=StockInfo(symbol="SZ300001", name="测试", code="300001", percent=1.0,
                        current=10.0, value=1e8, rank_change=0, rank=1),
        category="new_face", score=50, reason="", kline=k)


def test_fund_flow_signal_boundaries():
    """阈值端点语义：≥8 强流入、[5,8) 流入、(-5,5) 中性、( -8,-5] 流出、≤-8 强流出。"""
    assert disp_mod.fund_flow_signal(None) == ""
    assert disp_mod.fund_flow_signal(8.0) == "strong_in"
    assert disp_mod.fund_flow_signal(7.9) == "in"
    assert disp_mod.fund_flow_signal(5.0) == "in"
    assert disp_mod.fund_flow_signal(4.9) == "neutral"
    assert disp_mod.fund_flow_signal(0.0) == "neutral"
    assert disp_mod.fund_flow_signal(3.1) == "neutral"
    assert disp_mod.fund_flow_signal(-3.1) == "neutral"
    assert disp_mod.fund_flow_signal(-5.0) == "out"
    assert disp_mod.fund_flow_signal(-7.9) == "out"
    assert disp_mod.fund_flow_signal(-8.0) == "strong_out"


def test_market_extra_str_fund_flow_icon():
    """资金流以图标替代原「资+x.x% ±xxx万」文本，纯图标展示。"""
    s = disp_mod._market_extra_str(_candidate(8.5))
    assert "▲▲" in s
    assert "资" not in s
    assert "万" not in s
    assert "亿" not in s


def test_market_extra_str_no_fund_flow_data():
    """无资金流数据时资金段为空，连板信息仍保留。"""
    s = disp_mod._market_extra_str(_candidate(None))
    assert s == ""


def test_market_extra_str_zt_kept():
    """连板/炸板标记不受资金流图标改造影响。"""
    c = _candidate(6.0)
    c.kline.dimensions["zt_lianban"] = 2
    c.kline.dimensions["zt_zhaban"] = 1
    s = disp_mod._market_extra_str(c)
    assert "▲" in s
    assert "连2炸1" in s
