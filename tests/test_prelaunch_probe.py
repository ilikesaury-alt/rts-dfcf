"""scripts/prelaunch_probe.py 的守卫测试。

这个脚本的全部价值在于**它会拒绝**那些在 TRAIN 窗好看的形态条件。若哪天有人
把它改成「TRAIN 好看就算通过」，本文件会红 —— 所以这里主要测 `verdict` 的
判定方向，而不是测它算得准不准（后者依赖真实 DB，不在单测里做）。
"""

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "prelaunch_probe.py"


def _load():
    spec = importlib.util.spec_from_file_location("prelaunch_probe", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["prelaunch_probe"] = mod
    spec.loader.exec_module(mod)
    return mod


pp = _load()


def _memdb():
    """建一个只含 daily_kline 最小 schema 的内存库。

    DDL 无法参数化（sqlite3 的占位符只作用于 DML 的 VALUES），且下面这句是
    纯静态字面量、无任何插值。
    """
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE daily_kline ("
        "symbol TEXT, date TEXT, open REAL, close REAL, high REAL,"
        " low REAL, volume REAL, percent REAL)"
    )
    return conn


def _row(**kw):
    """构造一行 evaluate() 风格的 dict，缺的键补成不可判定的 nan。"""
    base = {
        "key": "cond",
        "desc": "测试条件",
        "train_n": 500,
        "test_n": 500,
        "min_n": 30,
        "train_hit": 9.0,
        "test_hit": 9.0,
        "train_delta": None,
        "test_delta": None,
        "train_mde": None,
        "test_mde": None,
    }
    base.update(kw)
    return base


class TestVerdict:
    """判定方向 —— 本脚本存在的理由。"""

    def test_train_good_test_flat_is_insufficient(self):
        """TRAIN 好看、TEST 塌回基准 ⇒ 证据不足（这正是善水型的结局）。"""
        code, _ = pp.verdict(_row(train_delta=3.4, test_delta=0.1, test_mde=0.9))
        assert code == pp.EXIT_INSUFFICIENT

    def test_test_delta_below_mde_is_insufficient(self):
        code, why = pp.verdict(_row(test_delta=0.4, test_mde=0.9))
        assert code == pp.EXIT_INSUFFICIENT
        assert "MDE" in why

    def test_test_delta_at_or_above_mde_is_supported(self):
        code, _ = pp.verdict(_row(test_delta=1.2, test_mde=0.9))
        assert code == pp.EXIT_OK

    def test_negative_delta_beyond_mde_is_worse(self):
        """Δ ≤ −MDE ⇒ 该条件有害，必须能判成 2（不能一律当'不足'吞掉）。"""
        code, _ = pp.verdict(_row(test_delta=-1.5, test_mde=0.9))
        assert code == pp.EXIT_WORSE

    def test_unestimable_delta_is_insufficient_not_ok(self):
        """MDE 不可估（nan）时**不得**判支持 —— 否则任何改动都能蒙混过关。"""
        code, why = pp.verdict(_row(test_delta=float("nan"), test_mde=1.0))
        assert code == pp.EXIT_INSUFFICIENT
        assert "不可估" in why

    def test_small_test_sample_is_insufficient(self):
        code, _ = pp.verdict(_row(test_n=10, min_n=30, test_delta=5.0, test_mde=0.1))
        assert code == pp.EXIT_INSUFFICIENT

    def test_sign_flip_is_called_out(self):
        """TRAIN/TEST 方向相反要显式点名为疑似拟合，便于一眼看出拟合痕迹。"""
        _code, why = pp.verdict(_row(train_delta=2.0, test_delta=-0.2, test_mde=0.9))
        assert "疑似拟合" in why

    def test_baseline_never_judged(self):
        assert pp.verdict(_row(key="baseline"))[0] == pp.EXIT_OK


class TestCellRendering:
    def test_nan_renders_as_na_not_zero(self):
        """MDE 报 0 会让'不可估'读成'毫无不确定性'—— 必须显示 n/a。"""
        assert pp._cell(float("nan"), 6).strip() == "n/a"
        assert pp._cell(None, 6).strip() == "n/a"

    def test_real_number_is_formatted(self):
        assert pp._cell(1.234, 6).strip() == "1.23"

    def test_non_numeric_does_not_raise(self):
        assert pp._cell("oops", 6).strip() == "n/a"


class TestAccum5UsesClose:
    """回归守卫：`accum5` 曾误取 `bars[i-5][1]`（open）而非 close。

    加入 open 列后元组索引整体右移一位，这个 bug 让前5日累计虚高约 1.3pp
    （善水 T-1 读成 +13.30%，真值 +11.95%），会直接改变形态条件的分组。
    """

    def test_accum5_uses_close_column(self):
        import datetime as _dt

        # 构造 25 根 bar：收盘恒为 10.0，但 open 从 9.0 线性升到 9.9。
        # 若 accum5 误取 open，5 日前那根的 open 明显低于 close → 结果偏大。
        conn = _memdb()
        d0 = _dt.date(2026, 1, 5)
        rows = [
            ("SZ300001", (d0 + _dt.timedelta(days=k)).isoformat(), 9.0 + k * 0.04, 10.0, 10.5, 1000.0, 0.0)
            for k in range(25)
        ]
        # 末根（=T-1）收盘抬到 12.0，次日收盘 12.0 → f1_close = 0
        rows[-1] = ("SZ300001", rows[-1][1], 11.9, 12.0, 12.1, 1000.0, 0.0)
        rows.append(("SZ300001", (d0 + _dt.timedelta(days=25)).isoformat(), 12.0, 12.0, 12.1, 1000.0, 0.0))
        # 全参数化占位符，无字符串插值
        conn.executemany(
            "INSERT INTO daily_kline(symbol,date,open,close,high,volume,percent) VALUES(?,?,?,?,?,?,?)",
            rows,
        )
        conn.commit()

        events = pp.build_events(conn)
        conn.close()

        assert events, "应产出至少一个事件"
        e = events[-1]
        # 5 根之前那根的 close = 10.0 → (12.0/10.0-1)*100 = +20.00%
        assert e.accum5 == pytest.approx(20.0, abs=1e-6)
        # 若误取 open（9.0+19*0.04=9.76），会得到 +22.95% —— 两者必须可区分
        assert e.f1_close == pytest.approx(0.0, abs=1e-6)


class TestDataHealth:
    def test_missing_trading_day_is_detected(self):
        """整日空洞必须报出来 —— 它会静默污染所有 20 日窗口。"""
        import datetime as _dt

        from scanner.trading_session import is_trading_day

        conn = _memdb()
        today = _dt.date.today()
        # 造交易日的 bar，故意跳过中间某个交易日。
        # ⚠ 窗口取 21 个日历日而非 8：A 股最长假期（国庆/春节 8 天）叠加前后周末
        # 可产生 ~12 个连续非交易日，8 日窗口在长假期间只剩 0~1 个交易日，前提断言
        # 必挂（2026-10 国庆实测）。21 日最坏情形仍有 ~9 个交易日。
        days = [today - _dt.timedelta(days=i) for i in range(21, 0, -1)]
        trading = [d for d in days if is_trading_day(d)]
        assert len(trading) >= 3, "测试前提：近 21 日至少 3 个交易日"
        for i, day in enumerate(trading):
            if i == len(trading) // 2:
                continue  # 挖一个洞
            conn.execute(
                "INSERT INTO daily_kline(symbol,date,open,close,high,volume,percent)"
                " VALUES('SZ300001',?,9,10,11,1000,0)",
                (day.isoformat(),),
            )
        conn.commit()
        problems = pp.data_health(conn)
        conn.close()
        assert problems, "挖了洞却没报出=data_health 失效"
        assert any("无任何 bar" in p for p in problems)
        assert any("backfill_kline" in p for p in problems)


class TestCondsAreReproducible:
    def test_no_condition_uses_future_data(self):
        """所有候选条件只能读 T-1 及之前的量 —— 形态标签不允许未来函数。"""
        e = pp.Event(
            symbol="SZ300001",
            date="2026-01-01",
            close=10.0,
            accum5=1.0,
            bias20=1.0,
            pos20=50.0,
            near_hi=95.0,
            t1_volratio=0.8,
            t1_pct=-1.0,
            up10=5,
            two_probe=False,
            f1_close=99.0,  # 未来值：条件若偷看它，命中率会虚高到不可信
            f1_open=99.0,
        )
        for cond in pp.CONDS:
            if cond.key == "baseline":
                continue
            # 条件返回 False 才说明它没依赖 f1_*（True 也可能是巧合，如 bias_mid）
            assert isinstance(cond.fn(e), bool)
        # 显式确认：改变次日结果不应改变任何条件判定
        e2 = pp.Event(**{**e.__dict__, "f1_close": -99.0, "f1_open": -99.0})
        for cond in pp.CONDS:
            assert cond.fn(e) == cond.fn(e2), f"条件 {cond.key} 疑似使用了未来数据"
