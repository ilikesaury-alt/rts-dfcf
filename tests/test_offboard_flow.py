"""B 段「N 日主力累计净流入门」的守卫测试（2026-11-03）。

这道门是 B 段**唯一**有样本外证据的新增筛选条件（948 条已标注样本 / 25 个交易日，
配对 bootstrap CI 不跨 0，符号检验 p=0.0009），因此它的口径必须被钉死：

1. **不可判定 ≠ 净流出**：历史不足 N 天 ⇒ fail-open 放行（否则上市越晚越容易被砍）。
2. **只取已收盘交易日**（`date < 今日`）：当天那行会被当天后续快照覆盖成
   「接近收盘」的值，而 B 段信号是**盘中**产生的 ⇒ 含当天行 = 前视偏差。
3. **与当日主力净占比不共线**（r=+0.051）：一条看中期方向、一条看今日瞬时状态。
4. **零新增网络请求**：序列来自 `market_extra_cache`（主循环每天已在写）。
5. 阈值取**零**（> 0），不是拟合出来的最优值（阈值越高 Δ 越大同时样本骤减 = 分母效应）。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date as _date
from datetime import timedelta as _td
from typing import Any

import pytest

from scanner.config_hot_watch import OFFBOARD_FLOW_MIN_SUM, OFFBOARD_FLOW_WINDOW
from scanner.db.migrations import run_migrations
from scanner.offboard_flow import load_flow_history, sum_main_net
from scanner.offboard_watch import (
    T1,
    OffboardCandidate,
    _flow_gate_reason,
    build_candidates,
    offboard_gate,
)


@pytest.fixture
def db():
    """内存库：含 `market_extra_cache`（资金流历史）+ B 段迁移单源。"""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE market_extra_cache (symbol TEXT, data_type TEXT, date TEXT, "
        "payload_json TEXT, updated TEXT, PRIMARY KEY (symbol, date, data_type))"
    )
    conn.execute("CREATE TABLE recommendations (symbol TEXT, date TEXT)")
    conn.execute("CREATE TABLE daily_kline (symbol TEXT, date TEXT, close REAL)")
    conn.execute("CREATE TABLE appearances (symbol TEXT, name TEXT, date TEXT)")
    run_migrations(conn, verbose=False)
    conn.commit()
    yield conn
    conn.close()


# ── 夹具 ────────────────────────────────────────────────────────────────────


def _cand(**kw) -> OffboardCandidate:
    base: dict[str, Any] = {
        "symbol": "SZ300101",
        "code": "300101",
        "name": "样本",
        "tier": T1,
        "current": 10.0,
        "percent": 2.5,
        "accum_5d": 3.0,
        "volume_ratio": 2.0,
        "main_pct": 2.0,
        "volume": 2.0e6,
        "amount": 8.0e7,
        "market_capital": 3.6e9,
        "float_market_capital": 3.0e9,
        "turnover_rate": 3.0,
        "exchange": "SZ",
    }
    base.update(kw)
    return OffboardCandidate(**base)


def _days_before(end: str, n: int) -> list[str]:
    """`end` 之前的 n 个自然日（倒序返回，调用方自行 order）。"""
    d = _date.fromisoformat(end)
    return [(d - _td(days=i)).isoformat() for i in range(1, n + 1)]


def _seed_flow(conn: sqlite3.Connection, symbol: str, days: list[str], nets: list[float]) -> None:
    """写入 N 天的资金流快照（`market_extra_cache`）。"""
    for d, net in zip(days, nets, strict=True):
        conn.execute(
            "INSERT OR REPLACE INTO market_extra_cache (symbol, date, data_type, payload_json, updated) "
            "VALUES (?, ?, 'fund_flow', ?, ?)",
            (symbol, d, json.dumps({"main_net": net, "main_pct": 1.0, "super_net": net}), f"{d}T15:00:00+08:00"),
        )
    conn.commit()


def _snapshot(symbol: str, **over) -> dict:
    base = {
        "name": "样本",
        "price": 10.0,
        "percent": 2.5,
        "vol_ratio": 2.0,
        "main_pct": 2.0,
        "volume": 2.0e6,
        "amount": 8.0e7,
        "total_cap": 3.6e9,
        "float_cap": 3.0e9,
        "turnover": 3.0,
    }
    base.update(over)
    return base


# ── 门控语义 ────────────────────────────────────────────────────────────────


def test_negative_flow_sum_is_rejected():
    """中期资金净流出的票 ⇒ 拒绝，理由串带窗口与金额。"""
    reason = _flow_gate_reason(_cand(flow_sum=-1.0e6))
    assert reason is not None
    assert f"{OFFBOARD_FLOW_WINDOW}日主力累计" in reason


def test_positive_flow_sum_passes():
    assert _flow_gate_reason(_cand(flow_sum=1.0e6)) is None


def test_unknown_flow_sum_passes_fail_open():
    """⚠ **不可判定 ≠ 净流出**：历史不足 N 天 ⇒ None ⇒ 放行。

    这是本门最容易被后来者改坏的一条 —— 把 None 当成 0 会砍掉所有新股。
    """
    assert _flow_gate_reason(_cand(flow_sum=None)) is None


def test_zero_flow_sum_passes_at_zero_threshold():
    """阈值取 0 ⇒ 恰好 0 **放行**（语义是「至少不是净流出」，不是「必须净流入」）。

    ⚠ 这个边界语义必须钉死：`> 0` 与 `>= 0` 在阈值 0 处差一整个边界，
    而实测样本里「累计恰好为 0」极罕见 ⇒ 两种写法在检验中无法区分 ⇒
    取**与当日主力净占比门一致的语义**（`OFFBOARD_MAIN_PCT_MIN = 0.0`，
    那边也是「等于 0 放行」）。
    """
    assert OFFBOARD_FLOW_MIN_SUM == 0.0
    assert _flow_gate_reason(_cand(flow_sum=0.0)) is None


def test_gate_reason_includes_window_and_amount():
    reason = _flow_gate_reason(_cand(flow_sum=-5.0e6))
    assert reason is not None
    assert "-500" in reason  # -5000万 / 1e4


# ── 历史序列口径 ────────────────────────────────────────────────────────────


def test_history_excludes_signal_day(db):
    """⚠ **只取已收盘交易日**（`date < as_of`）：当天那行会被当天后续快照覆盖成
    「接近收盘」的值，而信号是盘中产生的 ⇒ 含当天行 = 前视偏差。"""
    days = ["2026-10-01", "2026-10-02", "2026-10-03"]
    _seed_flow(db, "SZ300201", days, [1.0e6, 2.0e6, 3.0e6])
    hist = load_flow_history(db, ["SZ300201"], window=10, as_of="2026-10-03")
    assert "SZ300201" in hist
    assert [d for d, _ in hist["SZ300201"]] == ["2026-10-01", "2026-10-02"]


def test_sum_is_none_when_history_shorter_than_window(db):
    """历史不足 N 天 ⇒ None（不可判定）⇒ 调用方 fail-open。"""
    days = ["2026-10-01", "2026-10-02"]
    _seed_flow(db, "SZ300202", days, [1.0e6, 2.0e6])
    hist = load_flow_history(db, ["SZ300202"], window=10, as_of="2026-10-03")
    assert sum_main_net(hist.get("SZ300202"), window=10) is None


def test_sum_is_none_for_empty_history(db):
    assert sum_main_net([], window=10) is None
    assert sum_main_net(None, window=10) is None


def test_sum_of_full_window(db):
    days = [f"2026-09-{d:02d}" for d in range(1, 11)]
    nets = [float(i) * 1e6 for i in range(1, 11)]
    _seed_flow(db, "SZ300203", days, nets)
    hist = load_flow_history(db, ["SZ300203"], window=10, as_of="2026-10-01")
    assert sum_main_net(hist["SZ300203"], window=10) == pytest.approx(sum(nets))


def test_history_is_capped_at_window_rows(db):
    """取最近 N 行，不是一次拿回全部历史。"""
    days = [f"2026-08-{d:02d}" for d in range(1, 21)]
    _seed_flow(db, "SZ300204", days, [1.0e6] * 20)
    hist = load_flow_history(db, ["SZ300204"], window=10, as_of="2026-09-01")
    assert len(hist["SZ300204"]) == 10


# ── 端到端：build_candidates 两阶段判定 ─────────────────────────────────────


def test_build_candidates_applies_flow_gate(db):
    """过其它门但资金净流出的票 ⇒ 被流门剔除，且理由串进 rejects。"""
    days = _days_before("2026-10-11", OFFBOARD_FLOW_WINDOW)
    _seed_flow(db, "SZ300205", days, [-1.0e6] * len(days))
    snapshot = {"SZ300205": _snapshot("SZ300205")}
    cands, rejects = build_candidates(snapshot, [], {}, None, conn=db)
    assert cands == []
    assert any("日主力累计" in why for _, why in rejects)


def test_build_candidates_keeps_positive_flow(db):
    days = _days_before("2026-10-11", OFFBOARD_FLOW_WINDOW)
    _seed_flow(db, "SZ300206", days, [1.0e6] * len(days))
    snapshot = {"SZ300206": _snapshot("SZ300206")}
    cands, _ = build_candidates(snapshot, [], {}, None, conn=db)
    assert [c.symbol for c in cands] == ["SZ300206"]
    assert cands[0].flow_sum == pytest.approx(1.0e6 * OFFBOARD_FLOW_WINDOW)


def test_build_candidates_without_conn_keeps_everything(db):
    """离线/测试路径（不传 conn）⇒ 无流门 ⇒ 行为与改动前一致。"""
    days = _days_before("2026-10-11", OFFBOARD_FLOW_WINDOW)
    _seed_flow(db, "SZ300207", days, [-1.0e6] * len(days))
    snapshot = {"SZ300207": _snapshot("SZ300207")}
    cands, _ = build_candidates(snapshot, [], {}, None)
    assert [c.symbol for c in cands] == ["SZ300207"]
    assert cands[0].flow_sum is None


def test_build_candidates_unknown_history_is_fail_open(db):
    """历史不足 N 天 ⇒ 不在 sums 里 ⇒ flow_sum=None ⇒ 放行（不被砍）。"""
    _seed_flow(db, "SZ300208", ["2026-10-03"], [-1.0e6])
    snapshot = {"SZ300208": _snapshot("SZ300208")}
    cands, _ = build_candidates(snapshot, [], {}, None, conn=db)
    assert [c.symbol for c in cands] == ["SZ300208"]
    assert cands[0].flow_sum is None


def test_flow_gate_is_not_redundant_with_main_pct(db):
    """⚠ 与「当日主力净占比」**不共线**（r=+0.051）⇒ 是增量信息不是重复。

    当日净占比为正（`main_pct` 门放行）但 10 日累计为负 ⇒ 仍应被流门拦下。
    """
    days = _days_before("2026-10-11", OFFBOARD_FLOW_WINDOW)
    _seed_flow(db, "SZ300209", days, [-1.0e6] * len(days))
    c = _cand(main_pct=5.0, flow_sum=-3.0e6)
    assert c.main_pct >= 0.0, "当日净占比门应当放行"
    assert _flow_gate_reason(c) is not None, "中期方向门应当拦下"


def test_offboard_gate_without_flow_field_still_passes():
    """默认构造的候选（无 flow_sum）⇒ 门放行 —— 旧调用方零改动。"""
    assert offboard_gate(_cand()) is None
