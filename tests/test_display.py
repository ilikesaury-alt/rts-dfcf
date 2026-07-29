"""历史推荐跟踪显示：默认只显示"到买点"，"观察中"隐藏（TRACK_DISPLAY_WATCH_MAX=0）。"""
from types import SimpleNamespace

import scanner.display as disp_mod


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
