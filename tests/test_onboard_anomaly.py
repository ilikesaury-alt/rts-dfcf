"""榜内异动段（2026-09-30）测试。

本段的四条纪律，每条对应一个真实会踩的坑：

1. **口径同源，不新造阈值** —— 门槛/分层/排序必须与榜外段复用同一实现
   （`offboard_gate` / `classify_tier` / `sort_key`）。锁 `classify_tier` 的
   `allowed_tiers` 默认值不变 ⇒ B 段行为逐字节不变。
2. **只有 T1，没有 T2** —— 榜内票按定义已启动，T2 在榜内几乎不可能命中。
3. **fail-open / fail-closed 边界** —— 开关关闭/无 conn/榜单空 → 空；
   K 线缺失 → 该票不产出（验不了就不该报）。
4. **量比必须来自快照，不是 `quotes`**（2026-10-08 修复）—— 生产通道的
   `quotes` 结构上没有 `volume_ratio`，读它恒得 0.0 ⇒ 全段被量比门拒 ⇒ 恒空。
   夹具一律用 `_snapdb`（真建 `market_extra_cache`）提供量比，并在
   `test_volume_ratio_absent_from_quotes_still_produces` 里显式锁这条。

⚠ 本段**无样本外预测力**（榜外段同源同门，现有样本次日均值 −0.46%）。
本测试锁口径与边界，**不**断言它有效 —— 它是观察段，不是信号段。
"""

from __future__ import annotations

import dataclasses
import json
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


def _snapdb(vol_ratios: dict[str, float]):
    """带 `market_extra_cache` 的内存库 —— 量比快照的真实来源（2026-10-08 修复）。

    ⚠ 这张表的存在本身就是回归守卫：修复前本段从 `quotes` 读量比，而生产通道
    （`api.fetch_hot_quotes_batch` → `_HOT_QUOTE_FIELDS`）**结构上不产出**
    `volume_ratio`，故量比恒 0、全段被 `offboard_gate` 的量比门拒掉 ⇒ 恒空。
    修复后量比改读本表，故**测试必须建这张表**，否则会退化回「无 conn ⇒ 量比 0
    ⇒ 全段被拒」的空断言（正是修复前那个骗人的守卫形态）。
    """
    conn = _memdb()
    conn.execute(
        "CREATE TABLE market_extra_cache (symbol TEXT, date TEXT, data_type TEXT,"
        " payload_json TEXT, updated TEXT, PRIMARY KEY(symbol, data_type, date))"
    )
    today = date.today().isoformat()
    conn.executemany(
        "INSERT INTO market_extra_cache(symbol, date, data_type, payload_json, updated) VALUES(?,?,?,?,?)",
        [
            (sym, today, "fund_flow", json.dumps({"vol_ratio": vr}), f"{today}T10:00:00")
            for sym, vr in vol_ratios.items()
        ],
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
            "volume": 1e7,
            "amount": 5e8,
            "market_capital": 5e9,
            "float_market_capital": 4e9,
            "turnover_rate": 3.0,
            "status": 1,
        }
        for s in board
    }
    conn = _snapdb({"SZ300001": 2.0, "SZ301001": 2.0, "SH600001": 2.0, "SZ000001": 2.0})
    cands, _ = build_onboard_candidates(board, quotes, conn)
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
            "volume": 1e7,
            "amount": 5e8,
            "market_capital": 5e9,
            "float_market_capital": 4e9,
            "turnover_rate": 3.0,
            "status": 1,
        }
    }
    conn = _snapdb({"SZ300001": 2.0})
    cands, rejects = build_onboard_candidates(board, quotes, conn, exclude_symbols={"SZ300001"})
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
            "volume": 1e7,
            "amount": 5e8,
            "market_capital": 5e9,
            "float_market_capital": 4e9,
            "turnover_rate": 3.0,
            "status": 1,
        }
    }
    conn = _snapdb({"SZ300001": 2.5})
    # klines={} —— 该票无 K 线
    assert run_onboard_anomaly(conn, None, board, quotes, klines={}) == []


# ── 4b. 量比来源（2026-10-08 修复的回归守卫）────────────────────────────────
def test_volume_ratio_absent_from_quotes_still_produces():
    """回归守卫：本段恒空的那个 bug。

    `quotes` 是**生产通道真实形态** —— `api.fetch_hot_quotes_batch` 只回传
    `_HOT_QUOTE_FIELDS`（16 个字段，**不含** `volume_ratio`）。修复前本段从
    `quotes` 读量比 ⇒ 恒 0.0 ⇒ `offboard_gate` 的 `volume_ratio < 1.5` 全员拒
    ⇒ 本段自 2026-09-30 上线起恒定产出 0 行（实测 `onboard_anomaly_log` 0 行）。

    本测试刻意**不在 quotes 里放 volume_ratio**，只靠快照提供量比：
    若有人把取值改回 `quotes`，本测试立刻失败（而不是像修复前那样绿灯通过）。
    """
    board = [{"symbol": "SZ300001", "name": "创A", "percent": 2.0, "exchange": "SZ"}]
    quotes = {
        "SZ300001": {
            "code": "300001",
            "name": "创A",
            "exchange": "SZ",
            "current": 10.0,
            "percent": 2.0,
            "volume": 1e7,
            "amount": 5e8,
            "market_capital": 5e9,
            "float_market_capital": 4e9,
            "turnover_rate": 3.0,
            "status": 1,
        }
    }
    # 前置：quotes 里确实没有量比（否则本测试就是自欺）
    assert "volume_ratio" not in quotes["SZ300001"]

    conn = _snapdb({"SZ300001": 2.5})
    cands, _ = build_onboard_candidates(board, quotes, conn)
    assert len(cands) == 1, "quotes 无量比时仍应凭快照量比过门 —— 修复前这里恒为 0"
    assert cands[0].volume_ratio == 2.5


def test_no_snapshot_means_no_rows():
    """快照缺该票 ⇒ 量比 0 ⇒ 被门拒（fail-closed，不凭空产出）。

    这是「量比验不了就不该报」：与 K 线缺失同款边界。
    """
    board = [{"symbol": "SZ300001", "name": "创A", "percent": 2.0, "exchange": "SZ"}]
    quotes = {
        "SZ300001": {
            "code": "300001",
            "name": "创A",
            "exchange": "SZ",
            "current": 10.0,
            "percent": 2.0,
            "volume": 1e7,
            "amount": 5e8,
            "market_capital": 5e9,
            "float_market_capital": 4e9,
            "turnover_rate": 3.0,
            "status": 1,
        }
    }
    cands, rejects = build_onboard_candidates(board, quotes, _snapdb({}))
    assert cands == []
    assert any("量比不足" in r for _, r in rejects)


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


def _logdb_with_klines(symbol: str = "SZ300001", signal_day: str = "2026-09-30"):
    """`onboard_anomaly_log` + `daily_kline`，K 线含信号日与其下一交易日。"""
    conn = _logdb()
    conn.execute(
        "CREATE TABLE daily_kline (symbol TEXT, date TEXT, close REAL, high REAL,"
        " low REAL, open REAL, volume REAL, percent REAL, finalized INTEGER)"
    )
    nxt = (date.fromisoformat(signal_day) + timedelta(days=1)).isoformat()
    conn.executemany(
        "INSERT INTO daily_kline(symbol, date, close, high, low, open, volume,"
        " percent, finalized) VALUES(?,?,?,?,?,?,?,?,?)",
        [(symbol, signal_day, 10.0, 10.1, 9.9, 10.0, 1e6, 0.4, 1), (symbol, nxt, 11.0, 11.1, 10.9, 11.0, 1e6, 0.4, 1)],
    )
    conn.commit()
    return conn


def test_backfill_next_day_pct():
    """次日收益回填口径 = (次日收盘/当日收盘-1)×100。

    2026-10-08 重写：改为**自驱动**（查待回填行 → 读 `daily_kline` 找次一交易日），
    不再要求调用方传入 `{symbol: (次日前收, 当日收盘)}` —— 原实现没有任何生产调用方，
    且拿「今天」当 `WHERE date=?`，历史回放时命中 0 行却报成功。
    """
    conn = _logdb_with_klines()
    persist_round(conn, [_cand(symbol="SZ300001")], "2026-09-30")
    n = backfill_next_day_pct(conn)
    assert n == 1
    v = conn.execute("SELECT next_day_pct FROM onboard_anomaly_log").fetchone()[0]
    assert v == 10.0  # 11/10-1 = 10%
    conn.close()


def test_backfill_targets_signal_date_not_today():
    """回填按**信号日**定位行，不是「今天」—— 历史回放/隔日补数才对得上。"""
    conn = _logdb_with_klines(signal_day="2026-09-30")
    persist_round(conn, [_cand(symbol="SZ300001")], "2026-09-30")
    assert backfill_next_day_pct(conn, "2026-09-30") == 1
    v = conn.execute("SELECT next_day_pct FROM onboard_anomaly_log").fetchone()[0]
    assert v == 10.0
    conn.close()


def test_backfill_reports_actual_not_intended():
    """返回值是**实际更新行数**，不是「打算更新几行」。

    回归守卫：原实现 `return len(rows)`，UPDATE 命中 0 行时仍返回 1（谎报）。
    """
    conn = _logdb_with_klines()
    # 库里没有任何待回填行 ⇒ 必须返回 0
    assert backfill_next_day_pct(conn) == 0
    # 已有标签的行不应重复回填
    persist_round(conn, [_cand(symbol="SZ300001")], "2026-09-30")
    assert backfill_next_day_pct(conn) == 1
    assert backfill_next_day_pct(conn) == 0  # 第二次没有待回填行了
    conn.close()


def test_backfill_skips_today_signal_date():
    """防呆 2：信号日必须 `< today` —— 当天的信号永远找不到「次日 bar」。

    没有这道门的话，每轮都会为当天的信号反复查 K 线池（榜外段 docstring 记载的坑）。
    """
    from scanner.config import now_beijing

    conn = _logdb_with_klines(signal_day=now_beijing().date().isoformat())
    persist_round(conn, [_cand(symbol="SZ300001")], now_beijing().date().isoformat())
    assert backfill_next_day_pct(conn) == 0
    conn.close()


def test_backfill_skips_long_gap():
    """防呆 1：次一 bar 隔 >4 自然日（停牌/长假）⇒ 不是「次日」，不产出标签。"""
    conn = _logdb()
    conn.execute(
        "CREATE TABLE daily_kline (symbol TEXT, date TEXT, close REAL, high REAL,"
        " low REAL, open REAL, volume REAL, percent REAL, finalized INTEGER)"
    )
    # 信号日 09-30，次一根 bar 在 10-20（隔 20 天）
    conn.executemany(
        "INSERT INTO daily_kline(symbol, date, close, high, low, open, volume,"
        " percent, finalized) VALUES(?,?,?,?,?,?,?,?,?)",
        [
            ("SZ300001", "2026-09-30", 10.0, 10.1, 9.9, 10.0, 1e6, 0.4, 1),
            ("SZ300001", "2026-10-20", 11.0, 11.1, 10.9, 11.0, 1e6, 0.4, 1),
        ],
    )
    conn.commit()
    persist_round(conn, [_cand(symbol="SZ300001")], "2026-09-30")
    assert backfill_next_day_pct(conn) == 0
    conn.close()


def test_backfill_skips_nonpositive_close():
    """收盘价 ≤0（脏 bar）⇒ 不产出标签。"""
    conn = _logdb()
    conn.execute(
        "CREATE TABLE daily_kline (symbol TEXT, date TEXT, close REAL, high REAL,"
        " low REAL, open REAL, volume REAL, percent REAL, finalized INTEGER)"
    )
    conn.executemany(
        "INSERT INTO daily_kline(symbol, date, close, high, low, open, volume,"
        " percent, finalized) VALUES(?,?,?,?,?,?,?,?,?)",
        [
            ("SZ300001", "2026-09-30", 0.0, 0.0, 0.0, 0.0, 1e6, 0.4, 1),
            ("SZ300001", "2026-10-01", 11.0, 11.1, 10.9, 11.0, 1e6, 0.4, 1),
        ],
    )
    conn.commit()
    persist_round(conn, [_cand(symbol="SZ300001")], "2026-09-30")
    assert backfill_next_day_pct(conn) == 0
    conn.close()


def test_backfill_without_table_is_soft_fail():
    """表不存在时只告警不抛（迁移未跑的库不应崩主循环）。"""
    conn = sqlite3.connect(":memory:")  # 无该表
    assert backfill_next_day_pct(conn) == 0
    conn.close()


def test_backfill_is_wired_into_production_path():
    """回归守卫：`run_onboard_anomaly` 必须调用回填。

    原实现**没有任何生产调用方** ⇒ `next_day_pct` 永远是 NULL ⇒ 本段一行标签都没有
    ⇒ 按本仓纪律「没有标签就没有任何阈值能被证伪」，连观察纪律都建立不起来。
    """
    import inspect

    src = inspect.getsource(run_onboard_anomaly)
    assert "backfill_next_day_pct" in src, "生产路径必须回填次日收益，否则永远没有标签"


# ── 6. 不污染主线 ────────────────────────────────────────────────────────────
def test_does_not_write_recommendations():
    """本段不得写 recommendations / appearances —— 纯独立观察区。"""
    src = __import__("scanner.onboard_anomaly", fromlist=["x"])
    import inspect

    s = inspect.getsource(src)
    assert "recommendations" not in s
    assert "appearances" not in s
