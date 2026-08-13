"""基本面风险过滤层测试（pywencai 问财条件查询）。

用 monkeypatch 替换 pywencai.get 返回伪 DataFrame / 抛异常，不依赖外网。
覆盖：代码符号解析、进程缓存、DB 缓存、fail-open、超时兜底、enhancer 标签集成。
"""
import sqlite3

import pandas as pd
import pytest

import scanner.fundamentals as fb
from scanner.enhancer import _set_risk_flags
from scanner.models import Candidate, KlineSummary, StockInfo


def _risk_df(*codes):
    """构造问财返回 DataFrame：股票代码列为 "300027.SZ" 带后缀格式。"""
    return pd.DataFrame({"股票代码": codes, "股票名称": [f"股{i}" for i in range(len(codes))]})


class FakePywencai:
    """可控的 pywencai.get 替身：记录调用次数并返回/抛错。"""

    def __init__(self, df=None, error=None):
        self.calls = 0
        self.df = df
        self.error = error

    def get(self, query, loop=False):
        self.calls += 1
        if self.error:
            raise self.error
        return self.df


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    fb.reset_fund_risk_cache()
    yield
    fb.reset_fund_risk_cache()


def _memory_db():
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS market_extra_cache (
            symbol TEXT NOT NULL,
            date TEXT NOT NULL,
            data_type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            updated TEXT NOT NULL,
            PRIMARY KEY(symbol, data_type)
        )
    """)
    conn.commit()
    return conn


class TestCodeToXq:
    @pytest.mark.parametrize("raw,expect", [
        ("300027.SZ", "SZ300027"),
        ("300027", "SZ300027"),
        ("SZ300027", "SZ300027"),
        ("603377.SH", "SH603377"),
        ("SH603377", "SH603377"),
        ("600000", "SH600000"),
        ("832000", "BJ832000"),
        ("920608", "BJ920608"),
        ("code_300027", "SZ300027"),
        ("abc", None),
        (None, None),
        ("", None),
    ])
    def test_mapping(self, raw, expect):
        assert fb._code_to_xq(raw) == expect


class TestExtractSymbols:
    def test_stock_code_col(self):
        df = _risk_df("300027.SZ", "300385.SZ", "603377.SH")
        assert fb._extract_xq_symbols(df) == {"SZ300027", "SZ300385", "SH603377"}

    def test_none_df(self):
        assert fb._extract_xq_symbols(None) == set()

    def test_dict_of_frames(self):
        df = _risk_df("300027.SZ")
        assert fb._extract_xq_symbols({"tableV1": df}) == {"SZ300027"}

    def test_empty_df(self):
        assert fb._extract_xq_symbols(pd.DataFrame()) == set()


class TestFetchFundRiskMap:
    def test_success(self, monkeypatch):
        fake = FakePywencai(_risk_df("300027.SZ", "300385.SZ"))
        monkeypatch.setattr("pywencai.get", fake.get, raising=False)
        result = fb.fetch_fund_risk_map()
        assert result == {"SZ300027": "资不抵债", "SZ300385": "资不抵债"}

    def test_process_cache(self, monkeypatch):
        fake = FakePywencai(_risk_df("300027.SZ"))
        monkeypatch.setattr("pywencai.get", fake.get, raising=False)
        assert fb.fetch_fund_risk_map() == {"SZ300027": "资不抵债"}
        assert fb.fetch_fund_risk_map() == {"SZ300027": "资不抵债"}
        assert fake.calls == 1, "TTL 内第二次调用不再打问财"

    def test_fail_open_on_error(self, monkeypatch):
        fake = FakePywencai(error=RuntimeError("blocked"))
        monkeypatch.setattr("pywencai.get", fake.get, raising=False)
        assert fb.fetch_fund_risk_map() == {}
        assert fake.calls == 1

    def test_fail_open_on_import_error(self, monkeypatch):
        # pywencai 未安装：返回 {}（不抛）
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *a, **k):
            if name == "pywencai":
                raise ImportError("no module named 'pywencai'")
            return real_import(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        assert fb.fetch_fund_risk_map() == {}

    def test_bounded_timeout_returns_empty(self, monkeypatch):
        # 超时路径：_bounded_call 抛 TimeoutError → fetch fail-open 返回 {}
        monkeypatch.setattr(fb, "_bounded_call",
                            lambda fn, timeout: (_ for _ in ()).throw(TimeoutError("timeout")))
        assert fb.fetch_fund_risk_map() == {}

    def test_failure_cached_short_backoff(self, monkeypatch):
        # 回归：失败/超时结果短退避缓存（FUND_RISK_FAIL_TTL_SEC），故障期不每轮重复打 25s 限时
        calls = {"n": 0}

        def _raise(fn, timeout):
            calls["n"] += 1
            raise TimeoutError("timeout")

        monkeypatch.setattr(fb, "_bounded_call", _raise)
        assert fb.fetch_fund_risk_map() == {}
        assert calls["n"] == 1
        assert fb.fetch_fund_risk_map() == {}
        assert calls["n"] == 1, "失败结果应在短退避 TTL 内命中缓存，不重复打问财"

    def test_empty_success_cached_short_backoff(self, monkeypatch):
        # 回归：成功但空结果（问财列名不匹配/数据缺失）同样短退避，避免每轮重复查询
        calls = {"n": 0}
        fake = FakePywencai(pd.DataFrame({"其他列": ["x"]}))

        def _bounded(fn, timeout):
            calls["n"] += 1
            return fake.get("")

        monkeypatch.setattr(fb, "_bounded_call", _bounded)
        assert fb.fetch_fund_risk_map() == {}
        assert calls["n"] == 1
        assert fb.fetch_fund_risk_map() == {}
        assert calls["n"] == 1, "空结果应在短退避 TTL 内命中缓存"


class TestCollectFundRisk:
    def test_filters_to_candidates(self, monkeypatch):
        fake = FakePywencai(_risk_df("300027.SZ", "300385.SZ"))
        monkeypatch.setattr("pywencai.get", fake.get, raising=False)
        result = fb.collect_fund_risk(None, ["SZ300027", "SZ300750", "SZ300001"])
        assert result == {"SZ300027": "资不抵债"}
        assert "SZ300385" not in result, "非候选符号不返回"

    def test_saves_to_db(self, monkeypatch):
        fake = FakePywencai(_risk_df("300027.SZ"))
        monkeypatch.setattr("pywencai.get", fake.get, raising=False)
        conn = _memory_db()
        fb.collect_fund_risk(conn, ["SZ300027"])
        # 读回 DB：展示层（stock_report）重启后仍可读
        reason = fb.get_fund_risk_from_db(conn, "SZ300027")
        assert reason == "资不抵债"
        assert fb.get_fund_risk_from_db(conn, "SZ300999") is None

    def test_empty_symbols(self, monkeypatch):
        assert fb.collect_fund_risk(None, []) == {}

    def test_switch_disabled_returns_empty(self, monkeypatch):
        # 回归：RTS_ENABLE_FUND_RISK=0 总开关必须生效（此前只定义未接线，关闭无效）
        fake = FakePywencai(_risk_df("300027.SZ"))
        monkeypatch.setattr("pywencai.get", fake.get, raising=False)
        monkeypatch.setattr(fb, "ENABLE_FUND_RISK", False)
        assert fb.collect_fund_risk(None, ["SZ300027"]) == {}
        assert fake.calls == 0, "开关关闭时不得打问财"

    def test_switch_enabled_queries(self, monkeypatch):
        fake = FakePywencai(_risk_df("300027.SZ"))
        monkeypatch.setattr("pywencai.get", fake.get, raising=False)
        monkeypatch.setattr(fb, "ENABLE_FUND_RISK", True)
        assert fb.collect_fund_risk(None, ["SZ300027"]) == {"SZ300027": "资不抵债"}
        assert fake.calls == 1

    def test_empty_result_fail_open(self, monkeypatch):
        fake = FakePywencai(error=RuntimeError("down"))
        monkeypatch.setattr("pywencai.get", fake.get, raising=False)
        assert fb.collect_fund_risk(None, ["SZ300027"]) == {}


class TestGetFromDb:
    def test_missing_returns_none(self):
        conn = _memory_db()
        assert fb.get_fund_risk_from_db(conn, "SZ300999") is None

    def test_bad_conn_returns_none(self):
        assert fb.get_fund_risk_from_db(None, "SZ300999") is None


class TestEnhancerIntegration:
    """财务风险标签集成：命中 → 打 FUND_RISK_TAG → 硬过滤移出推荐。"""

    def _candidate(self, sym):
        stock = StockInfo(symbol=sym, name="测试", code=sym[2:],
                          percent=3.0, current=10.0, value=0.0,
                          rank_change=1, rank=1)
        ks = KlineSummary(trend="test", accumulated_pct=1.0, volume_ratio=1.0,
                          bottom_confirmed=True, score=60)
        return Candidate(stock=stock, category="short_term", score=60,
                         reason="test", kline=ks)

    def test_hit_appends_tag(self):
        from scanner.config import FUND_RISK_TAG, RISK_FLAGS_HARD_FILTER
        c = self._candidate("SZ300027")
        _set_risk_flags(c, fund_risk={"SZ300027": "资不抵债"})
        assert FUND_RISK_TAG in c.risk_flags
        assert FUND_RISK_TAG in RISK_FLAGS_HARD_FILTER, "财务风险必须是硬过滤标签"

    def test_miss_no_tag(self):
        c = self._candidate("SZ300750")
        _set_risk_flags(c, fund_risk={"SZ300027": "资不抵债"})
        assert "财务风险" not in c.risk_flags

    def test_none_fund_risk_ok(self):
        c = self._candidate("SZ300027")
        _set_risk_flags(c, fund_risk=None)
        assert "财务风险" not in c.risk_flags
        _set_risk_flags(c)
        assert "财务风险" not in c.risk_flags
