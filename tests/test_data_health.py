"""数据真实性前置检查回归测试（2026-08-18 拓斯达脏数据事故）。

覆盖：
  - check_kline_health 交叉验证：不符检测 / 阻断阈值 / 源不可达 fail-open
  - health_banner 渲染：阻断 / 低比例警告 / 无异常空串
  - count_unfinalized_today：finalized=0 计数 / 旧库无列容错
  - save_kline_to_db 的 finalized 标记（盘中今日=0，收盘后/历史=1）
"""
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from scanner import data_health as dh
from scanner.database import get_cached_kline, save_kline_to_db

BEIJING = timezone(timedelta(hours=8))


def _now(h: int, m: int = 0) -> datetime:
    return datetime(2026, 8, 18, h, m, tzinfo=BEIJING)


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.execute("""CREATE TABLE daily_kline (
        symbol TEXT NOT NULL, timestamp INTEGER, date TEXT NOT NULL, open REAL, close REAL,
        high REAL, low REAL, volume REAL, percent REAL, finalized INTEGER DEFAULT 1,
        PRIMARY KEY(symbol, date))""")
    yield c
    c.close()


def _seed(conn, rows):
    conn.executemany(
        "INSERT INTO daily_kline (symbol, date, close, percent, finalized) VALUES (?,?,?,?,?)",
        rows,
    )
    conn.commit()


class TestCheckKlineHealth:
    """主参照 THS（相对容差 0.5%）→ 回退新浪（绝对容差 0.011）。"""

    def test_no_mismatch(self, conn, monkeypatch):
        _seed(conn, [("SZ300607", "2026-08-18", 37.90, 0.99, 1),
                     ("SZ300012", "2026-08-18", 15.22, 0.0, 1)])
        monkeypatch.setattr(dh, "_ths_close", lambda sym, d: 37.90 if sym == "SZ300607" else 15.22)
        r = dh.check_kline_health(conn, dates=["2026-08-18"], sample_n=10)
        assert r.checked == 2 and r.mismatched == 0 and r.source_ok
        assert not r.blocked

    def test_mismatch_blocked(self, conn, monkeypatch):
        """不符比例 ≥30% 且样本足够 → blocked（数据疑似污染）。"""
        rows = []
        for i in range(10):
            rows.append((f"SZ3000{i:02d}", "2026-08-18", 10.0 + i, 0.0, 1))
        _seed(conn, rows)
        # 6/10 不符（60% ≥ 30%）：ref=99 vs db≈10~15，远超相对容差
        monkeypatch.setattr(dh, "_ths_close",
                            lambda sym, d: 99.0 if int(sym[-2:]) < 6 else float(sym[-2:]) + 10)
        r = dh.check_kline_health(conn, dates=["2026-08-18"], sample_n=10)
        assert r.checked == 10 and r.mismatched == 6 and r.blocked
        assert len(r.samples) == 6

    def test_low_mismatch_not_blocked(self, conn, monkeypatch):
        _seed(conn, [("SZ300607", "2026-08-18", 100.00, 0.99, 1)])
        monkeypatch.setattr(dh, "_ths_close", lambda sym, d: 99.40)  # 差 0.6% > 相对容差 0.5% = 不符
        r = dh.check_kline_health(conn, dates=["2026-08-18"], sample_n=10)
        assert r.checked == 1 and r.mismatched == 1
        assert not r.blocked  # 样本 < MIN_CHECKED，只警告不阻断

    def test_sina_fallback_used_when_ths_down(self, conn, monkeypatch):
        """THS 不可达 → 回退新浪 qfq（绝对容差 1 分钱），验证不中断。"""
        _seed(conn, [("SZ300607", "2026-08-18", 37.90, 0.99, 1)])
        monkeypatch.setattr(dh, "_ths_close", lambda sym, d: None)
        monkeypatch.setattr(dh, "_sina_close", lambda sym, d: 37.90)  # 一致
        r = dh.check_kline_health(conn, dates=["2026-08-18"])
        assert r.checked == 1 and r.mismatched == 0 and r.source_ok

    def test_ths_relative_tolerance(self, conn, monkeypatch):
        """THS forward 与雪球 qfq 偶有 ~0.36% 锚点微差（300012 案例）：
        相对容差 0.5% 内不算不符——若按新浪式逐分对齐会误报。"""
        _seed(conn, [("SZ300012", "2026-08-18", 100.00, 0.99, 1)])
        monkeypatch.setattr(dh, "_ths_close", lambda sym, d: 99.70)  # 差 0.30%
        r = dh.check_kline_health(conn, dates=["2026-08-18"])
        assert r.checked == 1 and r.mismatched == 0

    def test_ths_beyond_tolerance_mismatch(self, conn, monkeypatch):
        _seed(conn, [("SZ300607", "2026-08-18", 100.00, 0.99, 1),
                     ("SZ300608", "2026-08-18", 50.00, 0.99, 1)])
        monkeypatch.setattr(dh, "_ths_close",
                            lambda sym, d: 101.0 if sym.endswith("300607") else 49.5)
        # 100→101 差 1%、50→49.5 差 1%，均超相对容差 0.5%
        r = dh.check_kline_health(conn, dates=["2026-08-18"], sample_n=10)
        assert r.checked == 2 and r.mismatched == 2

    def test_source_unavailable_fail_open(self, conn, monkeypatch):
        _seed(conn, [("SZ300607", "2026-08-18", 37.90, 0.99, 1)])
        monkeypatch.setattr(dh, "_ths_close", lambda sym, d: None)   # 主参照不可达
        monkeypatch.setattr(dh, "_sina_close", lambda sym, d: None)  # 回退亦不可达
        r = dh.check_kline_health(conn, dates=["2026-08-18"])
        assert r.checked == 0 and not r.source_ok and not r.blocked

    def test_dirty_close_row_skipped_no_crash(self, conn, monkeypatch):
        """2026-08-19 修复回归：历史脏行 close 为字符串/NULL 时（契约重构前遗留）
        跳过该样本（无法与独立源交叉验证），abs() 不再对 None/str 抛 TypeError
        崩溃整检查——此工具的目的正是处理脏数据，不能遇脏即崩。"""
        _seed(conn, [("SZ300001", "2026-08-18", "abc", 0.0, 1),  # 非数值字符串脏行
                     ("SZ300002", "2026-08-18", None, 0.0, 1),   # NULL 脏行
                     ("SZ300003", "2026-08-18", 37.90, 0.0, 1)])  # 干净行
        monkeypatch.setattr(dh, "_ths_close", lambda sym, d: 37.90)
        r = dh.check_kline_health(conn, dates=["2026-08-18"], sample_n=10)
        assert r.checked == 1 and r.mismatched == 0  # 仅干净行参与统计
        assert r.source_ok and not r.blocked

    def test_empty_db(self, conn, monkeypatch):
        r = dh.check_kline_health(conn, dates=["2026-08-18"])
        assert r.checked == 0 and not r.blocked


class TestHealthBanner:
    def test_ok_empty(self):
        assert dh.health_banner(dh.HealthReport(checked=5, mismatched=0)) == ""

    def test_blocked_banner(self):
        r = dh.HealthReport(checked=10, mismatched=7,
                            samples=[("SZ300607", "2026-08-18", 36.27, 37.90)])
        b = dh.health_banner(r)
        assert "疑似污染" in b and "repair_kline" in b and "36.27" in b

    def test_low_ratio_warning(self):
        r = dh.HealthReport(checked=10, mismatched=1)
        b = dh.health_banner(r)
        assert "低于阈值" in b

    def test_source_unavailable_banner(self):
        b = dh.health_banner(dh.HealthReport(checked=0, source_ok=False))
        assert "不可达" in b


class TestCountUnfinalizedToday:
    def test_count(self, conn):
        _seed(conn, [("SZ300001", "2026-08-18", 10.0, 0.0, 0),
                     ("SZ300002", "2026-08-18", 11.0, 0.0, 1),
                     ("SZ300003", "2026-08-18", 12.0, 0.0, 0)])
        assert dh.count_unfinalized_today(conn, "2026-08-18") == 2

    def test_missing_column_graceful(self):
        """旧库无 finalized 列 → 返回 0 不崩溃。"""
        c = sqlite3.connect(":memory:")
        c.execute("""CREATE TABLE daily_kline (
            symbol TEXT NOT NULL, date TEXT NOT NULL, close REAL,
            PRIMARY KEY(symbol, date))""")
        assert dh.count_unfinalized_today(c, "2026-08-18") == 0
        c.close()


class TestSaveKlineFinalizedMark:
    def _bar(self, d: str, close: float) -> dict:
        return {"date": d, "open": close, "close": close, "high": close,
                "low": close, "volume": 1, "percent": 0.0, "timestamp": 1}

    def test_trading_hours_today_bar_marked_unfinalized(self, conn, monkeypatch):
        """盘中写入今日 bar → finalized=0（未定稿快照）。"""
        # P1-6 拆分后 save_kline_to_db 实现在 scanner.db.dal，patch 须打在实现模块
        monkeypatch.setattr("scanner.db.dal.now_beijing", lambda: _now(10, 30))
        monkeypatch.setattr("scanner.db.dal.is_trading_time", lambda: True)
        save_kline_to_db(conn, "SZ300607", [self._bar("2026-08-18", 36.27)])
        bars = get_cached_kline(conn, "SZ300607")
        assert bars and bars[-1]["finalized"] is False

    def test_after_close_today_bar_finalized(self, conn, monkeypatch):
        """收盘后（定稿/backfill）写入今日 bar → finalized=1。"""
        monkeypatch.setattr("scanner.db.dal.now_beijing", lambda: _now(18, 0))
        monkeypatch.setattr("scanner.db.dal.is_trading_time", lambda: False)
        save_kline_to_db(conn, "SZ300607", [self._bar("2026-08-18", 37.90)])
        bars = get_cached_kline(conn, "SZ300607")
        assert bars and bars[-1]["finalized"] is True

    def test_historical_bar_always_finalized(self, conn, monkeypatch):
        """历史 bar（非今日）盘中写入也置 finalized=1。"""
        monkeypatch.setattr("scanner.db.dal.now_beijing", lambda: _now(10, 30))
        monkeypatch.setattr("scanner.db.dal.is_trading_time", lambda: True)
        save_kline_to_db(conn, "SZ300607", [self._bar("2026-08-17", 37.53)])
        bars = get_cached_kline(conn, "SZ300607")
        assert bars and bars[-1]["finalized"] is True


class TestCheckMarketIndexHealth:
    """大盘指数对账（2026-08-19）：大盘标签曾把当日 -6.26% 崩盘读成昨日 -0.93%
    （展示"大盘中性"）而无痕——涨幅不进 daily_kline，K 线交叉验证覆盖不到。
    check_market_index_health 用血缘记录 bar 日期 + 独立源（东财）涨幅对账。"""

    @pytest.fixture
    def idx_conn(self):
        c = sqlite3.connect(":memory:")
        c.execute("""CREATE TABLE market_index_log (
            date TEXT PRIMARY KEY, time TEXT, index_pct REAL,
            bar_date TEXT, source TEXT, updated TEXT DEFAULT '')""")
        yield c
        c.close()

    def _seed(self, conn, pct, bar, time="14:59:04", date="2026-08-19"):
        conn.execute(
            "INSERT INTO market_index_log (date, time, index_pct, bar_date, source, updated) "
            "VALUES (?,?,?,?,?,?)",
            (date, time, pct, bar, "xueqiu", time),
        )
        conn.commit()

    def test_no_record_not_auditable(self, idx_conn):
        r = dh.check_market_index_health(idx_conn)
        assert r.checked == 0 and not r.source_ok

    def test_stale_bar_detected(self, idx_conn):
        """读到旧 bar（bar 日期 < 被审计日期且扫描 ≥09:30）→ stale_bar 命中。"""
        self._seed(idx_conn, -0.93, "2026-08-18")  # 记录的是昨日涨幅（08-19 扫描）
        r = dh.check_market_index_health(idx_conn, date_str="2026-08-19")
        assert r.stale_bar and r.recorded_bar == "2026-08-18"

    def test_early_morning_stale_bar_not_flag(self, idx_conn):
        """开盘前（09:15）扫描读到昨日 bar 属正常 → 不命中。"""
        self._seed(idx_conn, -0.93, "2026-08-18", time="09:15:00")
        r = dh.check_market_index_health(idx_conn, date_str="2026-08-19")
        assert not r.stale_bar

    def test_same_day_mismatch_detected(self, idx_conn, monkeypatch):
        """同日内扫描涨幅与独立源偏差超容差 → mismatch 命中（隔日错位场景）。"""
        self._seed(idx_conn, -0.93, "2026-08-19")  # bar 对，但涨幅是错的（应 -6.26）
        monkeypatch.setattr(dh, "_eastmoney_index_pct", lambda: -6.26)
        r = dh.check_market_index_health(idx_conn, date_str="2026-08-19")
        assert r.mismatch and r.ref_pct == pytest.approx(-6.26)
        assert abs(r.recorded_pct - r.ref_pct) > dh.INDEX_PCT_TOLERANCE

    def test_same_day_ok(self, idx_conn, monkeypatch):
        self._seed(idx_conn, -6.26, "2026-08-19")
        monkeypatch.setattr(dh, "_eastmoney_index_pct", lambda: -6.28)  # 0.02pp 噪声
        r = dh.check_market_index_health(idx_conn, date_str="2026-08-19")
        assert r.checked == 1 and not r.stale_bar and not r.mismatch and r.source_ok

    def test_source_unreachable(self, idx_conn, monkeypatch):
        self._seed(idx_conn, -6.26, "2026-08-19")
        monkeypatch.setattr(dh, "_eastmoney_index_pct", lambda: None)
        r = dh.check_market_index_health(idx_conn, date_str="2026-08-19")
        assert r.checked == 1 and not r.source_ok and not r.mismatch

    def test_cross_day_value_compare_skipped(self, idx_conn, monkeypatch):
        """跨日记录（bar=昨日）只判 stale_bar，不做涨幅对比（与今日 spot 无对比意义）。"""
        self._seed(idx_conn, -0.93, "2026-08-18")
        monkeypatch.setattr(dh, "_eastmoney_index_pct", lambda: -6.26)
        r = dh.check_market_index_health(idx_conn, date_str="2026-08-19")
        assert r.stale_bar and not r.mismatch

    def test_banner_no_issue_empty(self):
        assert dh.index_health_banner(dh.IndexHealthReport(checked=1, recorded_bar="2026-08-19")) == ""

    def test_banner_stale_bar(self):
        b = dh.index_health_banner(dh.IndexHealthReport(
            checked=1, stale_bar=True, recorded_bar="2026-08-18", recorded_time="14:59"))
        assert "读到旧 bar" in b and "2026-08-18" in b

    def test_banner_no_record(self):
        b = dh.index_health_banner(dh.IndexHealthReport())
        assert "无法对账" in b

    def test_eastmoney_pct_parses(self, monkeypatch):
        """东财 f170 解析：-626 → -6.26（单位对齐项目涨幅口径）。"""
        class _Resp:
            def json(self):
                return {"data": {"f43": 347349, "f170": -626}}
        monkeypatch.setattr(dh.requests, "get", lambda *a, **k: _Resp())
        assert dh._eastmoney_index_pct() == pytest.approx(-6.26)
