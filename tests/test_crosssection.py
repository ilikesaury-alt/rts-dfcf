"""横截面异动观察层测试（`scanner/crosssection.py`，2026-11-13）。

全离线：东财取数用 monkeypatch 顶替，时钟用假 `_clock`，库用 `:memory:` +
生产迁移单源 `run_migrations`。覆盖设计要求的 8 类行为：差分口径/有效行过滤/
榜内排除/跨源价差/陈旧轮/预热/落库淘汰/开关接线，外加 `force=True` 缓存语义。
"""

from __future__ import annotations

import copy
import sqlite3
from datetime import datetime

import pytest

import scanner.crosssection as cs
import scanner.market_extra as me
from scanner.config import BEIJING_TZ
from scanner.db.migrations import run_migrations

# 假时钟起点：2026-11-13（周五）10:00:00 北京时间；每轮 +60s。
_T0 = datetime(2026, 11, 13, 10, 0, 0, tzinfo=BEIJING_TZ).timestamp()

STABLE = [f"30{i:04d}" for i in range(1, 20)]  # 300001..300019（19 只横盘票）
ROCKET = "300999"  # 每轮 +1%、成交额增量递增的「异动票」

_INITIAL_STATE = copy.deepcopy(cs._state)


@pytest.fixture
def db():
    """内存库：生产迁移单源（m027 会建 crosssection_signal）。

    先建迁移清单会 ALTER 的既有表（与 tests/test_offboard_watch.py 同款），
    否则 m001/m002 等会因表不存在而失败。
    """
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE appearances (symbol TEXT, name TEXT, date TEXT)")
    conn.execute("CREATE TABLE market_extra_cache (symbol TEXT, data_type TEXT, date TEXT, payload_json TEXT)")
    conn.execute("CREATE TABLE recommendations (symbol TEXT, date TEXT)")
    conn.execute("CREATE TABLE daily_kline (symbol TEXT, date TEXT, close REAL)")
    run_migrations(conn, verbose=False)
    conn.commit()
    yield conn
    conn.close()


@pytest.fixture(autouse=True)
def _offline(monkeypatch, db):
    """每个用例：清进程状态 + 交易时段恒真 + 时钟可推进。"""
    cs._ring.clear()
    cs._state.clear()
    cs._state.update(copy.deepcopy(_INITIAL_STATE))
    monkeypatch.setattr(cs, "is_trading_time", lambda now=None: True)
    monkeypatch.setattr(cs, "CS_ENABLED", True)
    yield
    cs._ring.clear()


def _payload(stable_volume_base: int, round_no: int) -> dict[str, dict]:
    """构造一轮东财全集 payload（6 位码 → 报价快照同结构）。"""
    out: dict[str, dict] = {}
    for i, code in enumerate(STABLE):
        out[code] = {
            "price": 10.0,
            "percent": 0.0,
            # volume 每轮都变 ⇒ 正常轮不会被陈旧判定误伤
            "volume": stable_volume_base + round_no * 1000 + i * 10,
            "amount": 1e8 + round_no * 1e6 + i,
            "vol_ratio": 1.0 + i * 0.01,
            "name": f"稳{i}",
            "main_pct": 1.0,
        }
    price = 10.0 * (1.01 ** (round_no - 1))
    out[ROCKET] = {
        "price": price,
        "percent": (price / 10.0 - 1) * 100,
        "volume": 2_000_000 + round_no * 5000,
        # 增量 = 1e6 * round_no（递增 ⇒ amount_surge > 1）
        "amount": 5e7 + 1e6 * round_no * (round_no + 1) / 2,
        "vol_ratio": 3.0,
        "name": "火箭",
        "main_pct": 5.0,
    }
    return out


class _Feed:
    """按轮次产出 payload；`freeze=True` 时重复上一轮（模拟上游快照未更新）。"""

    def __init__(self):
        self.round_no = 0
        self.freeze = False
        self.last: dict[str, dict] | None = None

    def next(self) -> dict[str, dict]:
        if not self.freeze:
            self.round_no += 1
            self.last = _payload(1_000_000, self.round_no)
        assert self.last is not None
        return copy.deepcopy(self.last)


def _install_feed(monkeypatch, feed: _Feed, calls: list[int] | None = None):
    def _fake(*, force: bool = False):
        if calls is not None:
            calls.append(1)
        return feed.next()

    monkeypatch.setattr(me, "fetch_fund_flow_rank", _fake)


def _advance(monkeypatch, round_idx: int) -> None:
    monkeypatch.setattr(cs, "_clock", lambda: _T0 + round_idx * 60)


def _run_rounds(monkeypatch, db, feed: _Feed, n: int, board=None, start: int = 0) -> dict:
    health = {}
    for i in range(n):
        _advance(monkeypatch, start + i)
        health = cs.observe(db, board or [])
    return health


def _rows(db):
    return db.execute("SELECT symbol, round_seq, pct_rank FROM crosssection_signal").fetchall()


# ── 1. 差分口径 + p95 ──


class TestSpeedAndPercentile:
    def test_speed_windows_match_60s_rounds(self, monkeypatch, db):
        feed = _Feed()
        calls: list[int] = []
        _install_feed(monkeypatch, feed, calls)
        h = _run_rounds(monkeypatch, db, feed, 6)

        assert calls == [1] * 6, "每轮都应强制取一次新快照（TTL 60s > 55s）"
        assert h["warmup"] is True and h["signals"] == 1

        rows = db.execute(
            "SELECT symbol, price, speed_1, speed_3, speed_5, amount_surge, pct_rank FROM crosssection_signal"
        ).fetchall()
        assert len(rows) == 1
        sym, _price, s1, s3, s5, surge, pct = rows[0]
        assert sym == "SZ" + ROCKET
        # 60s 一轮 ⇒ 折算到每分钟后 = 原始涨跌幅
        assert s1 == pytest.approx(1.0, rel=1e-9)
        assert s3 == pytest.approx((1.01**3 - 1) * 100, rel=1e-9)
        assert s5 == pytest.approx((1.01**5 - 1) * 100, rel=1e-9)
        assert surge == pytest.approx(1.5, rel=1e-6)  # 6e6 ÷ avg(5e6,4e6,3e6)
        assert pct >= cs.CS_P95

    def test_invalid_rows_do_not_enter_cross_section(self, monkeypatch, db):
        feed = _Feed()

        def _fake(*, force: bool = False):
            p = feed.next()
            # 停牌 / 未发行 / 无成交：三种 0 值都不得进入有效行，也不当分位分母
            p["300901"] = {"price": 0.0, "percent": 0.0, "volume": 0.0, "amount": 0.0, "vol_ratio": 99.0}
            p["300902"] = {"price": 12.0, "percent": 5.0, "volume": 0.0, "amount": 1e8, "vol_ratio": 99.0}
            p["300903"] = {"price": 12.0, "percent": 5.0, "volume": 1e6, "amount": 0.0, "vol_ratio": 99.0}
            return p

        monkeypatch.setattr(me, "fetch_fund_flow_rank", _fake)
        h = _run_rounds(monkeypatch, db, feed, 6)

        assert h["valid_rows"] == 20, "只有 20 只有效行（19 横盘 + 火箭）"
        symbols = {r[0] for r in _rows(db)}
        assert not symbols & {"SZ300901", "SZ300902", "SZ300903"}


# ── 2. 榜内排除 + 跨源价差 ──


class TestBoardAndDivergence:
    def _six_rounds(self, monkeypatch, db):
        feed = _Feed()
        _install_feed(monkeypatch, feed)
        _run_rounds(monkeypatch, db, feed, 5)
        return feed

    def test_board_symbol_is_excluded_from_signals(self, monkeypatch, db):
        self._six_rounds(monkeypatch, db)
        row = _payload(1_000_000, 6)["300999"]
        board = [{"symbol": "300999", "current": row["price"], "percent": row["percent"]}]
        _advance(monkeypatch, 5)
        h = cs.observe(db, board)

        assert h["signals"] == 0, "榜内票只作跨源校验样本，不产出「榜外异动」"
        assert _rows(db) == []
        assert h["divergence"] == pytest.approx(0.0, abs=1e-9)
        assert h["divergence_ok"] is True

    def test_cross_source_divergence_over_tolerance_reported(self, monkeypatch, db):
        self._six_rounds(monkeypatch, db)
        row = _payload(1_000_000, 6)["300999"]
        board = [{"symbol": "300999", "current": row["price"] * 1.05, "percent": row["percent"] + 4.0}]
        _advance(monkeypatch, 5)
        h = cs.observe(db, board)

        assert h["divergence"] > cs.CS_DIVERGENCE_TOL
        assert h["divergence_ok"] is False
        assert h["signals"] == 0


# ── 3. 陈旧轮 ──


class TestStale:
    def test_identical_round_skips_signals(self, monkeypatch, db):
        feed = _Feed()
        _install_feed(monkeypatch, feed)
        _run_rounds(monkeypatch, db, feed, 6)
        before = _rows(db)
        assert len(before) == 1

        feed.freeze = True  # 第 7 轮与第 6 轮逐字段相同
        _advance(monkeypatch, 6)
        h = cs.observe(db, [])

        assert h["state"] == "stale" and h["stale_round"] is True
        assert h["stale_rounds"] == 1
        assert _rows(db) == before, "陈旧轮不得产出新信号"
        assert "陈旧轮" in cs.format_terminal_block()


# ── 4. 预热 ──


class TestWarmup:
    def test_no_signal_before_six_rounds(self, monkeypatch, db):
        feed = _Feed()
        _install_feed(monkeypatch, feed)
        h = _run_rounds(monkeypatch, db, feed, 5)

        assert h["warmup"] is False and h["signals"] == 0
        assert _rows(db) == []
        block = cs.format_terminal_block()
        assert "预热中 5/6" in block
        assert block.startswith("≡")

    def test_signal_on_sixth_round(self, monkeypatch, db):
        feed = _Feed()
        _install_feed(monkeypatch, feed)
        _run_rounds(monkeypatch, db, feed, 5)
        feed.freeze = False
        _advance(monkeypatch, 5)
        h = cs.observe(db, [])
        assert h["warmup"] is True and h["signals"] == 1


# ── 5. 落库 + 轮次淘汰 ──


class TestPersistence:
    def test_keep_rounds_prunes_old_rounds(self, monkeypatch, db):
        monkeypatch.setattr(cs, "CS_KEEP_ROUNDS", 2)
        feed = _Feed()
        _install_feed(monkeypatch, feed)
        _run_rounds(monkeypatch, db, feed, 8)  # 第 6/7/8 轮各产出一轮信号

        rounds = [
            r[0] for r in db.execute("SELECT DISTINCT round_seq FROM crosssection_signal ORDER BY round_seq").fetchall()
        ]
        # 第 6/7/8 轮（10:05 / 10:06 / 10:07）各产出一轮信号；只留最近 2 轮
        assert rounds == [100600, 100700]

    def test_rows_carry_required_columns(self, monkeypatch, db):
        feed = _Feed()
        _install_feed(monkeypatch, feed)
        _run_rounds(monkeypatch, db, feed, 6)
        row = db.execute(
            "SELECT date, round_seq, symbol, name, price, percent, speed_1, speed_3, "
            "speed_5, amount_surge, vol_ratio, pct_rank, updated_at FROM crosssection_signal"
        ).fetchone()
        assert row is not None
        assert row[0] == "2026-11-13"
        assert row[1] == 100500  # 第 6 轮（时钟 10:05）
        assert row[2] == "SZ" + ROCKET
        assert row[3] == "火箭"
        assert all(v is not None for v in row[4:12])
        assert row[12].startswith("2026-11-13T10:05:00")


# ── 6. 开关 / 接线 fail-open ──


class TestWiring:
    def test_disabled_switch_does_not_call_observe(self, monkeypatch):
        import unified_scanner as us

        calls: list[int] = []
        monkeypatch.setattr(us.crosssection, "CS_ENABLED", False)
        monkeypatch.setattr(us.crosssection, "observe", lambda *a, **k: calls.append(1))
        us._observe_crosssection(None, [])
        assert calls == []

    def test_enabled_switch_calls_observe(self, monkeypatch):
        import unified_scanner as us

        calls: list[int] = []
        monkeypatch.setattr(us.crosssection, "CS_ENABLED", True)
        monkeypatch.setattr(us.crosssection, "observe", lambda *a, **k: calls.append(1))
        us._observe_crosssection(None, [{"symbol": "SZ300001"}])
        assert calls == [1]

    def test_observe_failure_is_swallowed_at_wiring(self, monkeypatch):
        import unified_scanner as us

        def _boom(*a, **k):
            raise OSError("eastmoney down")

        monkeypatch.setattr(us.crosssection, "CS_ENABLED", True)
        monkeypatch.setattr(us.crosssection, "observe", _boom)
        us._observe_crosssection(None, [])  # 不得抛出

    def test_disabled_observe_is_noop(self, monkeypatch):
        monkeypatch.setattr(cs, "CS_ENABLED", False)
        calls: list[int] = []
        monkeypatch.setattr(me, "fetch_fund_flow_rank", lambda **k: calls.append(1) or {})
        h = cs.observe(None, [])
        assert calls == [] and h["state"] == "disabled"
        assert cs.format_terminal_block() == ""


# ── 7. fetch_fund_flow_rank(force=True) 语义 ──


class TestForceFetch:
    def _fake_fetch(self, monkeypatch, calls):
        def _f(timeout, codes=None):
            calls.append(1)
            return {"300001": {"price": 10.0}}, False, ""

        monkeypatch.setattr(me, "_fetch_fund_flow_bounded", _f)

    def test_force_skips_ttl_and_still_writes_cache(self, monkeypatch):
        me.reset_extra_cache()
        calls: list[int] = []
        self._fake_fetch(monkeypatch, calls)
        try:
            me.fetch_fund_flow_rank()
            assert len(calls) == 1
            me.fetch_fund_flow_rank()  # 300s TTL 命中，不再请求
            assert len(calls) == 1
            me.fetch_fund_flow_rank(force=True)  # 跳过 TTL
            assert len(calls) == 2
            me.fetch_fund_flow_rank()  # force 的结果已写回缓存 ⇒ 仍命中
            assert len(calls) == 2
        finally:
            me.reset_extra_cache()


# ── 8. CLI（离线只读）──


class TestCLI:
    def test_health_runs_offline(self, monkeypatch, db):
        import scanner.db.schema as schema_mod

        monkeypatch.setattr(schema_mod, "get_conn", lambda *a, **k: db)
        assert cs.main(["--health", "--date", "2026-11-13"]) == 0

    def test_default_prints_latest_round(self, monkeypatch, db, capsys):
        db.execute(
            "INSERT INTO crosssection_signal VALUES("
            "'2026-11-13', 100600, 'SZ300999', '火箭', 10.5, 5.0, 1.0, 3.0, 5.0, 1.5, 3.0, 1.0, 'x')"
        )
        db.commit()
        import scanner.db.schema as schema_mod

        monkeypatch.setattr(schema_mod, "get_conn", lambda *a, **k: db)
        assert cs.main(["--date", "2026-11-13"]) == 0
        out = capsys.readouterr().out
        assert "SZ300999" in out and "100600" in out
