"""tests/test_volume_bias.py — 开盘量比失真系数 k(m) 的实测与去偏（2026-11-05）。

被测对象：`scanner/volume_bias.py`
  · `reference_volumes`：近 N 个交易日收盘成交量均值（D_i，纯本地库）
  · `market_bias`：全市场量能前置系数 k(m)（不可判定 ⇒ None）
  · `apply_bias`：上报量比 → 去偏量比（就地改写快照）

为什么要有它：开盘的量比失真是**全市场共同的时间形状因子**（集合竞价 + 早盘集中
放量），实测 k(m)：m=1 → 13.6x / m=3 → 7.3x / m=5 → 5.7x / m=15 → 3.5x ⇒ 可以被
算出来并扣掉，不必再靠「等 15 分钟」（那个窗口其实没把失真等掉）。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, time, timedelta

import pytest

from scanner.config import now_beijing
from scanner.trading_session import is_trading_day, trading_minutes_elapsed
from scanner.volume_bias import apply_bias, market_bias, reference_volumes


def _first_trading_day() -> datetime:
    d = now_beijing().date()
    for _ in range(14):
        if is_trading_day(d):
            return datetime.combine(d, time(9, 54))
        d += timedelta(days=1)
    raise AssertionError("14 天内找不到交易日")


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.execute(
        "CREATE TABLE market_extra_cache (symbol TEXT, data_type TEXT, date TEXT,"
        " payload_json TEXT, updated TEXT)"
    )
    c.commit()
    yield c
    c.close()


def _insert(
    conn,
    date: str,
    symbol: str,
    volume: float,
    vol_ratio: float = 1.0,
    updated: str | None = None,
) -> None:
    """写一行 fund_flow 快照。`updated` 默认取**收盘附近**（基准日的量必须是全天量）。"""
    conn.execute(
        "INSERT INTO market_extra_cache VALUES (?,?,?,?,?)",
        (
            symbol,
            "fund_flow",
            date,
            json.dumps({"volume": volume, "vol_ratio": vol_ratio}),
            updated or f"{date}T14:59:00+08:00",
        ),
    )
    conn.commit()


def _seed_history(conn, symbols: list[str], days: int = 5, day_volume: float = 1_000_000.0) -> None:
    """铺 N 个历史交易日的收盘快照（每个 symbol 每天量相同 ⇒ D_i = day_volume）。"""
    today = now_beijing().date()
    for i in range(1, days + 1):
        d = (today - timedelta(days=i)).isoformat()
        for sym in symbols:
            _insert(conn, d, sym, day_volume)


def _snapshot(symbols: list[str], volume: float, vol_ratio: float) -> dict[str, dict]:
    return {sym: {"volume": volume, "vol_ratio": vol_ratio, "name": "样本"} for sym in symbols}


# ── D_i（参考量）────────────────────────────────────────────────────────────
def test_reference_volumes_averages_close_snapshots(conn):
    syms = [f"SZ3000{i:02d}" for i in range(3)]
    _seed_history(conn, syms, days=5, day_volume=1_000_000.0)
    ref = reference_volumes(conn)
    assert set(ref) == set(syms)
    assert ref[syms[0]] == pytest.approx(1_000_000.0)


def test_reference_volumes_ignores_today(conn):
    """今天的量是**盘中**的，不能进 D_i（否则基准被当天的早盘量污染）。"""
    syms = [f"SZ3000{i:02d}" for i in range(3)]
    _seed_history(conn, syms, days=3, day_volume=1_000_000.0)
    today = now_beijing().date().isoformat()
    _insert(conn, today, syms[0], 10.0)
    ref = reference_volumes(conn)
    assert ref[syms[0]] == pytest.approx(1_000_000.0)


def test_reference_volumes_empty_without_history(conn):
    assert reference_volumes(conn) == {}


# ── k(m) ────────────────────────────────────────────────────────────────────
def test_market_bias_matches_hand_computed_value(conn):
    """k(m) = median[今日累计量 ÷ D_i] ÷ (m/240)。

    构造：每只票近 5 日收盘量 100 万股，今日 24 分钟时已成交 20 万股 ⇒ F=0.2，
    m=24 ⇒ k = 0.2 / (24/240) = 2.0。
    """
    syms = [f"SZ300{i:03d}" for i in range(300)]
    _seed_history(conn, syms, days=5, day_volume=1_000_000.0)
    snap = _snapshot(syms, volume=200_000.0, vol_ratio=3.0)
    k = market_bias(conn, snap, now=_first_trading_day())
    assert k is not None
    assert k == pytest.approx(2.0, rel=0.02)


def test_market_bias_none_without_history(conn):
    """本地没有历史 ⇒ 不可判定（调用方据此退回旧的 15 分钟窗口）。"""
    syms = [f"SZ300{i:03d}" for i in range(300)]
    snap = _snapshot(syms, volume=200_000.0, vol_ratio=3.0)
    assert market_bias(conn, snap, now=_first_trading_day()) is None


def test_market_bias_none_when_sample_too_small(conn):
    """样本数不足 `OFFBOARD_VOL_BIAS_MIN_SAMPLE` ⇒ 不可判定（中位数不稳）。"""
    from scanner.config import OFFBOARD_VOL_BIAS_MIN_SAMPLE

    syms = [f"SZ300{i:03d}" for i in range(OFFBOARD_VOL_BIAS_MIN_SAMPLE - 1)]
    _seed_history(conn, syms, days=5, day_volume=1_000_000.0)
    snap = _snapshot(syms, volume=200_000.0, vol_ratio=3.0)
    assert market_bias(conn, snap, now=_first_trading_day()) is None


def test_market_bias_none_outside_trading_session(conn):
    """非交易时段 elapsed=0 ⇒ 不可判定（没有「进度」可投影）。"""
    syms = [f"SZ300{i:03d}" for i in range(300)]
    _seed_history(conn, syms, days=5, day_volume=1_000_000.0)
    snap = _snapshot(syms, volume=200_000.0, vol_ratio=3.0)
    assert market_bias(conn, snap, now=datetime(2000, 1, 1, 10, 0)) is None


def test_market_bias_none_when_market_slower_than_usual(conn):
    """k ≤ 1（今天的量能比平时还平缓）⇒ 上报读数**没有**虚高 ⇒ 不修正。"""
    syms = [f"SZ300{i:03d}" for i in range(300)]
    _seed_history(conn, syms, days=5, day_volume=1_000_000.0)
    # 24 分钟只成交 1% ⇒ F=0.01 ⇒ k = 0.01/(24/240) = 0.1 < 1
    snap = _snapshot(syms, volume=10_000.0, vol_ratio=0.5)
    assert market_bias(conn, snap, now=_first_trading_day()) is None


# ── 去偏 ────────────────────────────────────────────────────────────────────
def test_apply_bias_divides_ratio_and_keeps_raw():
    snap = {"SZ300001": {"vol_ratio": 6.0}, "SZ300002": {"vol_ratio": 0.0}}
    assert apply_bias(snap, 3.0) == 1
    assert snap["SZ300001"]["vol_ratio"] == pytest.approx(2.0)
    assert snap["SZ300001"]["vol_ratio_raw"] == 6.0
    # 量比 ≤0（停牌/脏值）不参与改写
    assert snap["SZ300002"]["vol_ratio"] == 0.0
    assert "vol_ratio_raw" not in snap["SZ300002"]


def test_apply_bias_is_noop_without_bias():
    snap = {"SZ300001": {"vol_ratio": 6.0}}
    assert apply_bias(snap, None) == 0
    assert apply_bias(snap, 0.8) == 0
    assert snap["SZ300001"]["vol_ratio"] == 6.0


def test_apply_bias_keeps_relative_order():
    """去偏是**全市场同一个标量** ⇒ 同一轮内的相对序不变（排序键本身不受影响）。"""
    snap = {"A": {"vol_ratio": 8.0}, "B": {"vol_ratio": 4.0}, "C": {"vol_ratio": 2.0}}
    before = sorted(snap, key=lambda s: -snap[s]["vol_ratio"])
    apply_bias(snap, 4.0)
    after = sorted(snap, key=lambda s: -snap[s]["vol_ratio"])
    assert before == after


def test_reference_dates_skip_days_whose_last_snapshot_is_morning(conn):
    """某天最后一张快照停在上午 ⇒ 它的量不是全天量 ⇒ 不能当 D_i 的基准。"""
    syms = [f"SZ3000{i:02d}" for i in range(3)]
    today = now_beijing().date()
    good1 = (today - timedelta(days=1)).isoformat()
    morning = (today - timedelta(days=2)).isoformat()
    good2 = (today - timedelta(days=3)).isoformat()
    for sym in syms:
        _insert(conn, good1, sym, 1_000_000.0)
        _insert(conn, good2, sym, 1_000_000.0)
        _insert(conn, morning, sym, 400_000.0, updated=f"{morning}T11:30:00+08:00")
    ref = reference_volumes(conn)
    assert ref, "至少有一天可用"
    assert ref[syms[0]] == pytest.approx(1_000_000.0)  # 上午那天的 40 万没混进来


def test_snapshot_elapsed_uses_snapshot_timestamp(conn):
    """k(m) 必须按**快照那一刻**算（上报量比的分母是那一刻的已交易分钟数）。"""
    from scanner.volume_bias import snapshot_elapsed_minutes

    if not is_trading_day(now_beijing().date()):
        pytest.skip("非交易日：trading_minutes_elapsed 恒为 0")
    today = now_beijing().date().isoformat()
    _insert(conn, today, "SZ300001", 1.0, updated=f"{today}T10:24:00+08:00")
    # 快照在 10:24 ⇒ 已交易 54 分钟（09:30 起算，午休已排除）
    assert snapshot_elapsed_minutes(conn) == 54


def test_snapshot_elapsed_falls_back_to_clock(conn):
    """库里没有今天的快照 ⇒ 退回当前时钟（不因此判不可判定）。"""
    from scanner.volume_bias import snapshot_elapsed_minutes

    assert snapshot_elapsed_minutes(conn) == trading_minutes_elapsed()
