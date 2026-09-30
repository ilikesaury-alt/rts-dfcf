"""榜内异动段（2026-09-30）测试。

本段的三条纪律，每条对应一个真实会踩的坑：

1. **口径同源，不新造阈值** —— 门槛/分层/排序必须与榜外段复用同一实现
   （`offboard_gate` / `classify_tier` / `sort_key`）。锁 `classify_tier` 的
   `allowed_tiers` 默认值不变 ⇒ B 段行为逐字节不变。
2. **只有 T1，没有 T2** —— 榜内票按定义已启动，T2 在榜内几乎不可能命中。
3. **fail-open / fail-closed 边界** —— 开关关闭/无 conn/榜单空 → 空；
   K 线缺失 → 该票不产出（验不了就不该报）。

⚠ 本段**无样本外预测力**（榜外段同源同门，现有样本次日均值 −0.46%）。
本测试锁口径与边界，**不**断言它有效 —— 它是观察段，不是信号段。
"""

from __future__ import annotations

import dataclasses
import sqlite3
from datetime import date, timedelta

from scanner.config import (
    ONBOARD_ANOMALY_ENABLED,
    ONBOARD_DISPLAY_TOP,
)
from scanner.offboard_watch import (
    T1,
    T2,
    OffboardCandidate,
    classify_tier,
)
from scanner.onboard_anomaly import (
    backfill_next_day_pct,
    build_onboard_candidates,
    persist_round,
    run_onboard_anomaly,
)


# ── 夹具 ────────────────────────────────────────────────────────────────────
def _bars(n: int = 30, start: str = "2026-06-01", drift: float = 0.004) -> list[dict]:
    """构造温和上行日线（MA5>MA10，5日累计落在带内）。"""
    base = date.fromisoformat(start)
    out = []
    px = 10.0
    for i in range(n):
        px *= 1.0 + drift
        out.append(
            {
                "date": (base + timedelta(days=i)).isoformat(),
                "close": px,
                "high": px * 1.01,
                "low": px * 0.99,
                "open": px,
                "volume": 1e6,
            }
        )
    return out


def _cand(**kw) -> OffboardCandidate:
    """构造一个默认过门的候选；kw 覆盖任意字段。

    用 `dataclasses.replace` 而非 `**d` —— 后者把异构字典传给 dataclass，
    mypy 会把每个值推成联合类型（`str | float`），直接报参数类型不兼容。
    """
    base = OffboardCandidate(
        symbol="SZ300001",
        code="300001",
        name="测试股",
        tier=T1,
        current=10.0,
        percent=2.0,
        accum_5d=0.0,
        volume_ratio=2.5,
        main_pct=1.0,
        volume=1e7,
        amount=5e8,
        market_capital=5e9,
        float_market_capital=4e9,
        turnover_rate=3.0,
        exchange="SZ",
        status=1,
    )
    return dataclasses.replace(base, **kw) if kw else base


def _memdb():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE daily_kline (symbol TEXT, date TEXT, close REAL, high REAL, low REAL, open REAL, volume REAL)"
    )
    return conn


# ── 1. 不改变 B 段行为（allowed_tiers 默认值）─────────────────────────────────
def test_classify_tier_default_keeps_both_layers():
    """默认值必须仍是 (T2, T1) —— 否则 B 段会静默少一层。"""
    import inspect

    sig = inspect.signature(classify_tier)
    assert sig.parameters["allowed_tiers"].default == (T2, T1)


def test_allowed_tiers_t1_disables_t2():
    """allowed_tiers=(T1,) 时 T2 带内不产出，且理由串说明原因。"""
    c = _cand(percent=5.0)  # T2 带 [3.5, ...]
    tier, why = classify_tier(c, _bars(), "2026-09-30", allowed_tiers=(T1,))
    assert tier is None
    assert "T2" in why


def test_allowed_tiers_t1_still_allows_t1():
    """allowed_tiers=(T1,) 时 T1 仍正常产出。"""
    c = _cand(percent=2.0)  # T1 带 (0, 3.5)
    tier, _ = classify_tier(c, _bars(), "2026-09-30", allowed_tiers=(T1,))
    assert tier == T1


# ── 2. fail-open 边界 ────────────────────────────────────────────────────────
def test_run_returns_empty_without_conn():
    assert run_onboard_anomaly(None, None, [{"symbol": "SZ300001"}]) == []


def test_run_returns_empty_without_board():
    assert run_onboard_anomaly(_memdb(), None, []) == []


def test_switch_off_returns_empty(monkeypatch):
    import scanner.onboard_anomaly as mod

    monkeypatch.setattr(mod, "ONBOARD_ANOMALY_ENABLED", False)
    assert mod.run_onboard_anomaly(_memdb(), None, [{"symbol": "SZ300001"}]) == []


def test_switch_on_by_default():
    assert ONBOARD_ANOMALY_ENABLED is True
    assert ONBOARD_DISPLAY_TOP >= 1


# ── 3. 样本面 = 创业板 ───────────────────────────────────────────────────────
def test_only_chinext_enters_candidates():
    board = [
        {"symbol": "SZ300001", "name": "创A", "percent": 2.0, "exchange": "SZ"},
        {"symbol": "SZ301001", "name": "创B", "percent": 2.0, "exchange": "SZ"},
        {"symbol": "SH600001", "name": "沪A", "percent": 2.0, "exchange": "SH"},
        {"symbol": "SZ000001", "name": "深A", "percent": 2.0, "exchange": "SZ"},
    ]
    quotes = {
        s["symbol"]: {
            "code": s["symbol"][2:],
            "name": s["name"],
            "exchange": s["exchange"],
            "current": 10.0,
            "percent": 2.0,
            "volume_ratio": 2.0,
            "volume": 1e7,
            "amount": 5e8,
            "market_capital": 5e9,
            "float_market_capital": 4e9,
            "turnover_rate": 3.0,
            "status": 1,
        }
        for s in board
    }
    cands, _ = build_onboard_candidates(board, quotes, None)
    codes = {c.code for c in cands}
    assert "300001" in codes and "301001" in codes
    assert "600001" not in codes and "000001" not in codes


def test_exclude_symbols_are_skipped():
    board = [{"symbol": "SZ300001", "name": "创A", "percent": 2.0, "exchange": "SZ"}]
    quotes = {
        "SZ300001": {
            "code": "300001",
            "name": "创A",
            "exchange": "SZ",
            "current": 10.0,
            "percent": 2.0,
            "volume_ratio": 2.0,
            "volume": 1e7,
            "amount": 5e8,
            "market_capital": 5e9,
            "float_market_capital": 4e9,
            "turnover_rate": 3.0,
            "status": 1,
        }
    }
    cands, rejects = build_onboard_candidates(board, quotes, None, exclude_symbols={"SZ300001"})
    assert cands == []
    assert any("已在A段展示或今日已推荐" in r for _, r in rejects)


def test_missing_quote_is_rejected_not_crashed():
    board = [{"symbol": "SZ300001", "name": "创A", "percent": 2.0, "exchange": "SZ"}]
    cands, rejects = build_onboard_candidates(board, {}, None)
    assert cands == []
    assert rejects and rejects[0][1] == "无补全行情"


# ── 4. K 线缺失 → 不产出（fail-closed）───────────────────────────────────────
def test_missing_klines_yield_no_rows():
    """信号门不能凭空产出：K 线缺失的票必须被丢弃。"""
    board = [{"symbol": "SZ300001", "name": "创A", "percent": 2.0, "exchange": "SZ"}]
    quotes = {
        "SZ300001": {
            "code": "300001",
            "name": "创A",
            "exchange": "SZ",
            "current": 10.0,
            "percent": 2.0,
            "volume_ratio": 2.5,
            "volume": 1e7,
            "amount": 5e8,
            "market_capital": 5e9,
            "float_market_capital": 4e9,
            "turnover_rate": 3.0,
            "status": 1,
        }
    }
    conn = _memdb()
    # klines={} —— 该票无 K 线
    assert run_onboard_anomaly(conn, None, board, quotes, klines={}) == []


# ── 5. 落库幂等 ──────────────────────────────────────────────────────────────
def _logdb() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE onboard_anomaly_log (date TEXT, symbol TEXT, name TEXT, tier TEXT, "
        "percent REAL, accum_5d REAL, vol_ratio REAL, main_pct REAL, amount REAL, "
        "float_cap REAL, price REAL, first_time TEXT, next_day_pct REAL, updated TEXT, "
        "PRIMARY KEY (date, symbol))"
    )
    return conn


def test_persist_is_idempotent():
    """主循环每轮都跑，不去重会刷出重复行 —— 同 (date,symbol) 必须 UPSERT。"""
    conn = _logdb()
    row = _cand(percent=2.5, accum_5d=3.1)
    persist_round(conn, [row], "2026-09-30")
    persist_round(conn, [row], "2026-09-30")
    n = conn.execute("SELECT COUNT(*) FROM onboard_anomaly_log").fetchone()[0]
    assert n == 1
    r = conn.execute("SELECT percent FROM onboard_anomaly_log").fetchone()
    assert r[0] == 2.5
    conn.close()


def test_persist_updates_changed_values():
    conn = _logdb()
    persist_round(conn, [_cand(percent=2.5)], "2026-09-30")
    persist_round(conn, [_cand(percent=4.0)], "2026-09-30")
    r = conn.execute("SELECT percent FROM onboard_anomaly_log").fetchone()
    assert r[0] == 4.0
    conn.close()


def test_persist_empty_is_noop():
    conn = _logdb()
    persist_round(conn, [], "2026-09-30")
    persist_round(None, [_cand()], "2026-09-30")
    assert conn.execute("SELECT COUNT(*) FROM onboard_anomaly_log").fetchone()[0] == 0
    conn.close()


def test_backfill_next_day_pct():
    """次日收益回填口径 = (次日收盘/当日收盘-1)×100。"""
    conn = _logdb()
    persist_round(conn, [_cand(symbol="SZ300001")], "2026-09-30")
    n = backfill_next_day_pct(conn, {"SZ300001": (11.0, 10.0)})
    assert n == 1
    v = conn.execute("SELECT next_day_pct FROM onboard_anomaly_log").fetchone()[0]
    assert v == 10.0  # 11/10-1 = 10%
    conn.close()


def test_backfill_skips_nonpositive():
    conn = _logdb()
    persist_round(conn, [_cand(symbol="SZ300001")], "2026-09-30")
    assert backfill_next_day_pct(conn, {"SZ300001": (0.0, 10.0)}) == 0
    assert backfill_next_day_pct(conn, {"SZ300001": (11.0, 0.0)}) == 0
    assert backfill_next_day_pct(conn, {}) == 0
    conn.close()


def test_backfill_without_table_is_soft_fail():
    """表不存在时只告警不抛（迁移未跑的库不应崩主循环）。"""
    conn = sqlite3.connect(":memory:")  # 无该表
    assert backfill_next_day_pct(conn, {"SZ300001": (11.0, 10.0)}) == 0
    conn.close()


# ── 6. 不污染主线 ────────────────────────────────────────────────────────────
def test_does_not_write_recommendations():
    """本段不得写 recommendations / appearances —— 纯独立观察区。"""
    src = __import__("scanner.onboard_anomaly", fromlist=["x"])
    import inspect

    s = inspect.getsource(src)
    assert "recommendations" not in s
    assert "appearances" not in s
