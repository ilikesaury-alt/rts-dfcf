"""`get_fund_flow` 本地缓存分档的守卫（2026-11-11）。

## 为什么需要这组测试

`get_fund_flow` 原先是**无条件**调东财（`fetch_fund_flow_rank`），而主扫描器每轮已把
全市场今日资金流落进 `market_extra_cache` —— 同一天同源的数据白跑一次网络。

改成本地优先后有个**陷阱**：本地最后一轮落在 14:54（实测），而 A 股 15:00 收盘；
尾盘 + 集合竞价会改变成交量与主力净额终值。实测 2026-11-11 伊之密：

- 本地 14:54：`main_pct=21.73%`、`volume=1277.78万`
- 收盘终值：`main_pct=24.38%`、`volume=1374.48万`

差 +2.65pp —— 足以改变「首选」排序。故必须**按收盘时点分档**，而不是简单本地优先。
这组测试锁住：

1. 盘中 → 只读本地、**零联网**（这是引入本地缓存的本意）；
2. 收盘后 → 取联网终值（不是默认用定格快照）；
3. 收盘后联网失败 → 退回本地，但输出里**明说这是盘中快照**；
4. 本地无当日条目 → 走联网，不因缺一只就判整批失败。
"""

from __future__ import annotations

import datetime
import sqlite3
from typing import Any

import pytest

import scanner.ai_digest.research as research_mod
import scanner.config as config_mod
from scanner.ai_digest.research import ResearchContext, build_tools, run_tool


def _ctx(conn: sqlite3.Connection) -> ResearchContext:
    return ResearchContext(conn=conn)


def _tool():
    return {t.name: t for t in build_tools()}["get_fund_flow"]


@pytest.fixture()
def conn() -> sqlite3.Connection:
    """带一只票当日资金流快照的库。"""
    c = sqlite3.connect(":memory:")
    c.execute(
        "CREATE TABLE market_extra_cache (symbol TEXT, date TEXT, data_type TEXT, "
        "payload_json TEXT, updated TEXT)"
    )
    c.execute(
        "INSERT INTO market_extra_cache VALUES (?,?,?,?,?)",
        (
            "SZ300415",
            "2026-11-11",
            "fund_flow",
            '{"name":"伊之密","main_net":52884735.0,"main_pct":21.73,'
            '"super_net":17145700.0,"turnover":2.81,"vol_ratio":2.46,'
            '"amount":243319226.46,"float_cap":8798000000.0}',
            "2026-11-11T14:54:32+08:00",
        ),
    )
    c.commit()
    return c


@pytest.fixture()
def freeze(monkeypatch: pytest.MonkeyPatch):
    """把 `now_beijing` 冻在指定时刻（工具内部按它判是否收盘）。"""

    def _set(dt: datetime.datetime) -> None:
        monkeypatch.setattr(config_mod, "now_beijing", lambda: dt)

    return _set


def _remote(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any] | Exception) -> list[int]:
    """替换出网的 `fetch_fund_flow_rank`，返回调用计数（空列表=没被调用）。"""
    import scanner.market_extra as me

    calls: list[int] = []

    def fake() -> dict[str, Any]:
        calls.append(1)
        if isinstance(payload, Exception):
            raise payload
        return dict(payload)

    monkeypatch.setattr(me, "fetch_fund_flow_rank", fake)
    return calls


def test_intraday_uses_local_without_network(
    conn: sqlite3.Connection, freeze, monkeypatch: pytest.MonkeyPatch
) -> None:
    """盘中（10:30）：本地就是最新 ⇒ 只读本地，**一次网络都不发**。"""
    freeze(datetime.datetime(2026, 11, 11, 10, 30))
    calls = _remote(monkeypatch, OSError("不该被调用"))

    out = run_tool(_tool(), {"symbols": ["SZ300415"]}, _ctx(conn))

    assert calls == [], f"盘中不应联网，实际调用了 {len(calls)} 次"
    assert "本地 market_extra_cache" in out
    assert "21.73%" in out  # 盘中快照值


def test_after_close_prefers_settled_value(
    conn: sqlite3.Connection, freeze, monkeypatch: pytest.MonkeyPatch
) -> None:
    """收盘后（18:03）：本地已定格但**非终值** ⇒ 必须取联网终值。"""
    freeze(datetime.datetime(2026, 11, 11, 18, 3))
    calls = _remote(
        monkeypatch,
        {"SZ300415": {"name": "伊之密", "main_net": 63910877.0, "main_pct": 24.38, "turnover": 3.02}},
    )

    out = run_tool(_tool(), {"symbols": ["SZ300415"]}, _ctx(conn))

    assert calls, "收盘后应联网取终值"
    assert "东财实时" in out
    assert "24.38%" in out  # 终值，不是本地 21.73%
    assert "21.73%" not in out


def test_after_close_falls_back_and_labels_stale(
    conn: sqlite3.Connection, freeze, monkeypatch: pytest.MonkeyPatch
) -> None:
    """收盘后联网失败 ⇒ 退回本地，且**必须明说这是收盘前快照**。

    为什么标注比数值更重要：模型看到「成交量偏低」不会知道是缺了尾盘，会误判「量能不足」。
    """
    freeze(datetime.datetime(2026, 11, 11, 18, 3))
    _remote(monkeypatch, OSError("网络挂了"))

    out = run_tool(_tool(), {"symbols": ["SZ300415"]}, _ctx(conn))

    assert "21.73%" in out  # 退回本地值
    assert "非终值" in out
    assert "低于" in out  # 提示成交量偏低


def test_missing_local_row_goes_remote(
    freeze, monkeypatch: pytest.MonkeyPatch
) -> None:
    """本地无当日条目 ⇒ 走联网，不因「本地缺」就整批失败。"""
    freeze(datetime.datetime(2026, 11, 11, 10, 30))  # 即使盘中，本地没有就得联网
    c = sqlite3.connect(":memory:")
    c.execute(
        "CREATE TABLE market_extra_cache (symbol TEXT, date TEXT, data_type TEXT, "
        "payload_json TEXT, updated TEXT)"
    )
    calls = _remote(monkeypatch, {"SZ300999": {"name": "某票", "main_pct": 5.0, "main_net": 1e7}})

    out = run_tool(_tool(), {"symbols": ["SZ300999"]}, _ctx(c))

    assert calls, "本地无数据时应联网"
    assert "5.00%" in out


def test_both_sources_fail_returns_plain_reason(
    freeze, monkeypatch: pytest.MonkeyPatch
) -> None:
    """两边都取不到 ⇒ 返回一句人话（不抛异常、不返回空字符串）。"""
    freeze(datetime.datetime(2026, 11, 11, 18, 3))
    c = sqlite3.connect(":memory:")
    c.execute(
        "CREATE TABLE market_extra_cache (symbol TEXT, date TEXT, data_type TEXT, "
        "payload_json TEXT, updated TEXT)"
    )
    _remote(monkeypatch, OSError("网络挂了"))

    out = run_tool(_tool(), {"symbols": ["SZ300415"]}, _ctx(c))

    assert "失败" in out
    assert out.strip() != ""


def test_close_settle_boundary() -> None:
    """15:00~15:05 仍算「未定格」（留尾盘结算宽限），15:06 起算定格。"""
    assert research_mod._local_snapshot_final(datetime.datetime(2026, 11, 11, 15, 0)) is False
    assert research_mod._local_snapshot_final(datetime.datetime(2026, 11, 11, 15, 5)) is False
    assert research_mod._local_snapshot_final(datetime.datetime(2026, 11, 11, 15, 6)) is True
    assert research_mod._local_snapshot_final(datetime.datetime(2026, 11, 11, 10, 30)) is False
