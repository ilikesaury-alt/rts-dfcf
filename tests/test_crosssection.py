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
    # 现有用例全部针对「东财全量 + 环形缓冲差分」这条路径（测的是差分/分位/陈旧判定），
    # 故强制走 eastmoney 源；雪球源有独立的类单独覆盖（TestXueqiuSource）。
    monkeypatch.setattr(cs, "CS_SOURCE", "eastmoney")
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

# ── 9. 层内一致性判据（2026-11-16）──
#
# 这三条判据的**唯一数据源是本层自算字段**（环形缓冲差分 / 成交额增量）。
# 它们的存在理由是：用户要「在区域展示时就直接看到」，不必另跑命令；
# 且必须**不引入任何外部区域**（榜单/资金流缓存/B 段）的数据 ——
# 本层的存在理由就是与榜单解耦，借外部数据等于把滞后加回来。


class TestConsistency:
    """`_consistency`：1m/3m/5m 是否同号。"""

    def test_all_positive(self):
        assert cs._consistency(0.5, 1.0, 1.5) == "全正"

    def test_all_negative(self):
        assert cs._consistency(-0.5, -1.0, -1.5) == "全负"

    def test_mixed_signs(self):
        assert cs._consistency(0.5, -1.0, 1.5) == "混合"

    def test_zero_is_not_positive(self):
        # 0 不算「朝上走」：三窗口必须严格同号才算一致
        assert cs._consistency(0.0, 1.0, 1.5) == "混合"

    def test_missing_value_degrades_to_mixed(self):
        # 不足 5 轮快照时 speed_5 为 None ⇒ 无法判定，保守记「混合」
        assert cs._consistency(0.5, 1.0, None) == "混合"

    def test_nan_degrades_to_mixed(self):
        assert cs._consistency(0.5, 1.0, float("nan")) == "混合"


class TestVolumePrice:
    """`_volume_price`：放量是否伴随价格。"""

    def test_cooperating(self):
        # 放量（>1×）且 1m 在涨
        assert cs._volume_price(2.0, 0.3) == "配合"

    def test_divergence(self):
        # 明显放量（>2×）但 1m 不涨 ⇒ 滞涨/派发嫌疑
        assert cs._volume_price(3.0, 0.0) == "背离"
        assert cs._volume_price(3.0, -0.2) == "背离"

    def test_neutral_when_surge_mild(self):
        assert cs._volume_price(0.5, 0.3) == "—"

    def test_neutral_when_missing(self):
        assert cs._volume_price(None, 0.3) == "—"
        assert cs._volume_price(2.0, None) == "—"


class TestPersistCounter:
    """持续性计数器：当日出现过几轮，跨日清零。"""

    def test_persist_accumulates_across_rounds(self, monkeypatch, db):
        feed = _Feed()
        _install_feed(monkeypatch, feed)
        # 预热 6 轮后才出信号；ROCKET 每轮都在
        _run_rounds(monkeypatch, db, feed, 7)
        with cs._lock:
            sigs = list(cs._state["last_signals"])
            persist = dict(cs._state["persist"])
        assert sigs, "预热后应有信号"
        rocket = [s for s in sigs if s["symbol"] == "SZ" + ROCKET]
        assert rocket, "ROCKET 应在信号里"
        # 出现轮次应随轮数增长（>=2 而不是恒为 1）
        assert rocket[0]["persist"] >= 2
        assert persist.get("SZ" + ROCKET, 0) >= 2

    def test_persist_resets_on_new_day(self, monkeypatch, db):
        feed = _Feed()
        _install_feed(monkeypatch, feed)
        _run_rounds(monkeypatch, db, feed, 7)
        with cs._lock:
            before = dict(cs._state["persist"])
        assert before, "跨日前应有累计"

        # 推进到次日（跨日重置分支）
        next_day = _T0 + 24 * 3600
        monkeypatch.setattr(cs, "_clock", lambda: next_day)
        cs.observe(db, [])
        with cs._lock:
            after = dict(cs._state["persist"])
        # 跨日先清空再累计本轮 ⇒ 不可能保留昨天的轮次数
        assert all(v <= 1 for v in after.values()), f"跨日未清零: {after}"


class TestTerminalBlockShowsJudgement:
    """`≡` 段**直接展示**层内判据（用户要求：不用另跑命令）。"""

    def test_block_contains_judgement_columns(self, monkeypatch, db):
        feed = _Feed()
        _install_feed(monkeypatch, feed)
        # 12 轮：预热 6 轮后 ROCKET 持续入选 ⇒ persist 达 7 轮，落「持续」组（会展示）。
        _run_rounds(monkeypatch, db, feed, 12)
        block = cs.format_terminal_block()
        assert block.startswith("≡")
        # 图例 + 每条信号尾部都有「一致性·量价·轮次」
        assert "判据：一致性" in block
        assert "轮" in block
        # 至少一条信号带判据后缀
        assert "·" in block

    def test_block_still_avoids_panel_delimiters(self, monkeypatch, db):
        """`≡` 块的续行不得以 `◆` / `—` 起头（否则污染 ai_digest/panel.py 解析）。"""
        feed = _Feed()
        _install_feed(monkeypatch, feed)
        _run_rounds(monkeypatch, db, feed, 7)
        block = cs.format_terminal_block()
        for line in block.splitlines()[1:]:
            assert not line.lstrip().startswith("◆")
            assert not line.lstrip().startswith("—")


class TestSignalRanking:
    """终端排序键：**展示分组**先于质量闸（2026-11-17 用户决策，见 config 注释）。"""

    def test_persist_group_beats_quality(self):
        # 持续组（persist>=CS_PERSIST_MIN）**整组在前** —— 哪怕它的质量层更差。
        # 理由：单轮窗口里「全正·配合」噪声极易达成，实测展示行相邻两轮重叠中位数 1/8，
        # 稳定的票被单轮噪声顶掉（用户原话「眼花缭乱不知道咋选」）。
        fresh_good = {
            "symbol": "SZ300001", "persist": 3, "pct_rank": 0.95,
            "amount_surge": 1.2, "vol_ratio": 1.0,
            "consistency": "全正", "volume_price": "配合",
        }
        steady_meh = {
            "symbol": "SZ300002", "persist": 9, "pct_rank": 0.999,
            "amount_surge": 3.0, "vol_ratio": 5.0,
            "consistency": "混合", "volume_price": "配合",
        }
        assert cs.signal_sort_key(steady_meh) < cs.signal_sort_key(fresh_good)

    def test_within_persist_group_quality_still_first(self):
        """同组内质量闸不变：5 轮 全正·配合 排在 6 轮 混合·— 之前。"""
        good = {
            "symbol": "SZ300001", "persist": 5, "pct_rank": 0.95,
            "amount_surge": 1.2, "vol_ratio": 1.0,
            "consistency": "全正", "volume_price": "配合",
        }
        meh = {
            "symbol": "SZ300002", "persist": 6, "pct_rank": 0.999,
            "amount_surge": 3.0, "vol_ratio": 5.0,
            "consistency": "混合", "volume_price": "—",
        }
        assert cs.signal_sort_key(good) < cs.signal_sort_key(meh)

    def test_within_same_tier_persist_then_pct(self):
        base = {
            "symbol": "SZ300001", "persist": 4, "pct_rank": 0.99,
            "amount_surge": 2.0, "vol_ratio": 1.5,
            "consistency": "全正", "volume_price": "配合",
        }
        less_persist = {**base, "symbol": "SZ300002", "persist": 3, "pct_rank": 0.999}
        lower_pct = {**base, "symbol": "SZ300003", "persist": 4, "pct_rank": 0.98}
        assert cs.signal_sort_key(base) < cs.signal_sort_key(lower_pct)
        assert cs.signal_sort_key(lower_pct) < cs.signal_sort_key(less_persist)

    def test_block_shows_sort_legend_and_observe_sorts(self, monkeypatch, db):
        feed = _Feed()
        _install_feed(monkeypatch, feed)
        _run_rounds(monkeypatch, db, feed, 12)
        block = cs.format_terminal_block()
        assert "排序：组内质量" in block
        assert "分组：持续" in block
        with cs._lock:
            sigs = list(cs._state["last_signals"])
        assert sigs == sorted(sigs, key=cs.signal_sort_key)


class TestDisplayGrouping:
    """终端按「持续 / 新进」两组展示（2026-11-17 用户决策）。

    动因：`≡` 区每 60s 几乎整屏换新（实测展示行相邻两轮重叠中位数 1/8），
    读不出重点。分组只改**展示**：不增删票、不给方向、不改落库口径。
    """

    @staticmethod
    def _sig(symbol: str, *, persist: int, rank: float = 0.99) -> dict:
        return {
            "symbol": symbol, "name": "票" + symbol[-2:], "price": 10.0, "percent": 1.0,
            "speed_1": 0.1, "speed_3": 0.2, "speed_5": 0.3,
            "amount_surge": 2.0, "vol_ratio": 1.5, "pct_rank": rank,
            "consistency": "全正", "volume_price": "配合", "persist": persist,
        }

    def _render(self, sigs: list[dict]) -> str:
        sigs = sorted(sigs, key=cs.signal_sort_key)
        with cs._lock:
            cs._state["last_signals"] = sigs
        cs._set_health(
            enabled=True, state="ok", warmup=True, valid_rows=20, signals=len(sigs)
        )
        return cs.format_terminal_block()

    def test_config_caps_sum_to_top_n(self):
        from scanner.config import CS_PERSIST_MIN, CS_TOP_N, CS_TOP_NEW, CS_TOP_PERSIST

        assert CS_TOP_PERSIST + CS_TOP_NEW == CS_TOP_N, "三处上限需同步改（展示总数不变）"
        assert CS_PERSIST_MIN > 1, "持续门必须严于「新进」（persist<=1）"
        assert CS_TOP_PERSIST >= 1 and CS_TOP_NEW >= 1

    def test_persist_group_printed_before_new_group(self):
        block = self._render([
            self._sig("SZ300001", persist=1, rank=0.999),   # 新进（分位更高）
            self._sig("SZ300002", persist=7, rank=0.960),   # 持续（分位更低）
        ])
        i_persist = block.index("持续（")
        i_new = block.index("新进（")
        assert i_persist < i_new
        # 序号连续（面板解析器靠「序号.」取行，不重复编号）
        assert "  1. SZ300002" in block
        assert "  2. SZ300001" in block

    def test_mid_band_is_not_displayed_but_counted(self):
        block = self._render([self._sig("SZ300003", persist=3)])
        assert "SZ300003" not in block, "2~4 轮属噪声带，不展示"
        assert "均不足" in block and "1 只" in block, "有信号却不展示时必须如实说明"

    def test_group_caps_are_enforced(self):
        from scanner.config import CS_TOP_NEW, CS_TOP_PERSIST

        persist = [self._sig(f"SZ30{i:04d}", persist=9 - i // 2) for i in range(1, 9)]
        fresh = [self._sig(f"SZ31{i:04d}", persist=1) for i in range(1, 6)]
        block = self._render(persist + fresh)
        shown = [ln for ln in block.splitlines() if ln.lstrip()[:1].isdigit()]
        assert len(shown) == CS_TOP_PERSIST + CS_TOP_NEW
        # 每组各自的帽子生效：持续组的第 6 只不出现在块里
        assert block.count("SZ30") == CS_TOP_PERSIST
        assert block.count("SZ31") == CS_TOP_NEW

    def test_empty_persist_group_says_so(self):
        block = self._render([self._sig("SZ300004", persist=1)])
        assert "本轮无 ≥5 轮持续票" in block
        assert "新进（" in block

    def test_group_headers_keep_panel_parsable(self):
        """组标题行不得被 `panel._CROSSSECTION_ROW_RE` 当数据行匹配。"""
        from scanner.ai_digest.panel import _CROSSSECTION_ROW_RE

        block = self._render([
            self._sig("SZ300005", persist=8), self._sig("SZ300006", persist=1),
        ])
        headers = [ln for ln in block.splitlines() if "（" in ln and "轮" in ln]
        assert headers, "应有组标题行"
        for ln in headers:
            assert _CROSSSECTION_ROW_RE.match(ln) is None, f"组标题被误认为数据行：{ln}"

# -- 10. 雪球数据源（2026-11-16）--
#
# 换源动机：东财 ulist 是**无参全量**接口，本层要 p95 就必须拉 3000 个 secid
# 自己算分位（6 请求/轮），计入 push2 家族 IP 级限频。雪球 screener 是**服务端
# 排序**接口（order_by=percent5m），且 percent5m 原生可用。
# 这些用例全离线（假 session），只验证「映射正确/分页正确/坏行不炸」。


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self._payload


class _FakeXqSession:
    """按 page 返回预置行；记录调用，便于断言分页停止条件。"""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get(self, url, params=None, timeout=None):
        assert url.endswith("/screener/quote/list.json")
        assert params["order_by"] == "percent5m" and params["order"] == "desc"
        assert params["type"] == "cyb"  # 必须锁创业板，别把 B 股/主板混进来
        page = int(params["page"])
        self.calls.append(page)
        rows = self.pages.get(page)
        if rows is None:
            return _FakeResponse({"data": {"count": 0, "list": []}})
        return _FakeResponse({"data": {"count": len(rows), "list": rows}})


def _xq_row(symbol, name, pct5m, percent=1.0, current=10.0, volume=1e6, amount=2e8, vr=2.0):
    return {
        "symbol": symbol, "name": name, "percent5m": pct5m, "percent": percent,
        "current": current, "volume": volume, "amount": amount, "volume_ratio": vr,
        "turnover_rate": 3.0, "main_net_inflows": 1e7, "float_market_capital": 5e9,
    }


class TestXueqiuSource:
    def test_fields_mapped_to_crosssection_shape(self, monkeypatch):
        sess = _FakeXqSession({1: [_xq_row("SZ300001", "T", 2.5, percent=3.3)]})
        monkeypatch.setattr(cs, "_xq_get_session", lambda: sess)
        out = cs._fetch_from_xueqiu()
        row = out["SZ300001"]
        # 与东财路径同形的键，下游 _valid_snapshot/_speed_pct 原样消费
        assert row["price"] == 10.0
        assert row["percent"] == 3.3
        assert row["volume"] == 1e6
        assert row["amount"] == 2e8
        assert row["vol_ratio"] == 2.0
        assert row["name"] == "T"
        # 雪球特有字段
        assert row["percent5m"] == 2.5
        assert row["turnover"] == 3.0
        assert row["main_net"] == 1e7
        assert row["float_cap"] == 5e9

    def test_pagination_stops_when_empty_page(self, monkeypatch):
        sess = _FakeXqSession({1: [_xq_row("SZ300001", "A", 1.0)], 2: []})
        monkeypatch.setattr(cs, "_xq_get_session", lambda: sess)
        monkeypatch.setattr(cs, "CS_XQ_PAGES", 5)
        cs._fetch_from_xueqiu()
        assert sess.calls == [1, 2]  # 第 2 页空即停

    def test_bad_rows_are_skipped_not_fatal(self, monkeypatch):
        rows = [
            _xq_row("SZ300001", "ok1", 1.0),
            {"symbol": "SZ300002"},  # 缺 current/volume/amount
            {"symbol": "", "current": 1, "volume": 1, "amount": 1},  # 无 symbol
            _xq_row("SZ300003", "ok2", 0.5),
        ]
        monkeypatch.setattr(cs, "_xq_get_session", lambda: _FakeXqSession({1: rows}))
        out = cs._fetch_from_xueqiu()
        assert set(out) == {"SZ300001", "SZ300003"}

    def test_http_error_returns_empty_not_raises(self, monkeypatch):
        class _BadSession:
            def get(self, *a, **k):
                return _FakeResponse({}, status_code=567)

        monkeypatch.setattr(cs, "_xq_get_session", lambda: _BadSession())
        assert cs._fetch_from_xueqiu() == {}

    def test_network_exception_returns_empty_not_raises(self, monkeypatch):
        class _BoomSession:
            def get(self, *a, **k):
                raise ConnectionError("boom")

        monkeypatch.setattr(cs, "_xq_get_session", lambda: _BoomSession())
        assert cs._fetch_from_xueqiu() == {}

    def test_source_switch_respects_config(self, monkeypatch):
        """CS_SOURCE 决定走哪条路径 —— 换源必须可回退。"""
        called = {}

        def _fake_em(*, force=False):
            called["eastmoney"] = True
            return {"SZ300001": {"price": 1.0, "volume": 1, "amount": 1, "percent": 0}}

        monkeypatch.setattr(cs, "fetch_fund_flow_rank", _fake_em)
        monkeypatch.setattr(cs, "_fetch_from_xueqiu", lambda: {"SZ9X": {}})

        monkeypatch.setattr(cs, "CS_SOURCE", "eastmoney")
        out = cs.fetch_crosssection_snapshot(force=True)
        assert called.get("eastmoney") and "SZ300001" in out

        called.clear()
        monkeypatch.setattr(cs, "CS_SOURCE", "xueqiu")
        out = cs.fetch_crosssection_snapshot(force=True)
        assert not called.get("eastmoney") and "SZ9X" in out

# -- 11. 封板剔除（2026-11-16）--
#
# 用户实测反馈：涨停票封板后仍持续占据本区域（领湃 +20.01%、北鼎 +19.94%）。
# 封板后价格不再变动（percent5m=0），但量比极高（实测 62.78/34.52）仍能把
# 复合分顶到 p95 以上 ⇒ 纯噪音，买不进的东西不该占观察位。
#
# 判定完全层内：由 price 与 percent 反推昨收再算涨停价，不借外部数据。


class TestLimitUpPrice:
    def test_reverse_derive_matches_real_sealed_cases(self):
        """实测校准：已封板票反推出的涨停价应等于其现价（误差<0.01）。"""
        # 2026-11-16 真实数据
        for price, pct in ((27.83, 20.01), (8.60, 19.94)):
            limit = cs._limit_up_price({"price": price, "percent": pct})
            assert limit is not None
            assert abs(limit - price) < 0.02, f"反推涨停价 {limit} != 现价 {price}"

    def test_not_sealed_returns_none(self):
        assert cs._limit_up_price({"price": 0, "percent": 5}) is None
        assert cs._limit_up_price({"price": None, "percent": 5}) is None
        assert cs._limit_up_price({"price": 10, "percent": None}) is None
        assert cs._limit_up_price({"price": 10, "percent": -100}) is None

    def test_gem_is_20pct_not_5pct(self):
        """创业板一律 20%（含 ST）—— ST创意 +12.39% 仍在涨即证明不是 5% 档。"""
        limit = cs._limit_up_price({"price": 5.08, "percent": 12.39})
        # 若误按 5% 算，涨停价会是 4.75（现价已超过）—— 那是错的
        assert limit > 5.08


class TestIsLimitUpSealed:
    def test_sealed_stocks_detected(self):
        assert cs._is_limit_up_sealed({"price": 27.83, "percent": 20.01}) is True
        assert cs._is_limit_up_sealed({"price": 8.60, "percent": 19.94}) is True

    def test_near_but_not_sealed_kept(self):
        # 节能铁汉 +13.71%：现价/涨停 = 0.948，未达 0.985 ⇒ 保留
        assert cs._is_limit_up_sealed({"price": 1.99, "percent": 13.71}) is False
        # ST创意 +12.39%：0.937 ⇒ 保留
        assert cs._is_limit_up_sealed({"price": 5.08, "percent": 12.39}) is False

    def test_missing_fields_fail_open(self):
        """字段缺失 fail-open（放行），绝不因算不出就误剔。"""
        assert cs._is_limit_up_sealed({"price": None, "percent": 10}) is False
        assert cs._is_limit_up_sealed({}) is False
