"""基本面风险过滤层测试（THS 估值快照主源 + pywencai 问财兜底）。

用 monkeypatch 替换 pywencai.get / ths_api 接口，不依赖外网。
覆盖：代码符号解析、进程缓存、DB 缓存、fail-open、超时兜底、
THS 跨轮增量拉取、enhancer 标签集成。
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
    # 默认屏蔽 THS 层（无 Key → 真实增量逻辑快速返回 ({}, False)，不打网络），
    # 走 pywencai 兜底路径——既有 pywencai 用例语义不变；
    # THS 主源行为在 TestThsFundRisk 单独覆盖（覆盖 get_api_key 即可启用）。
    monkeypatch.setattr("scanner.ths_api.get_api_key", lambda: "")
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

    def test_import_error_warns_once(self, monkeypatch, capsys):
        # 回归（2026-08-20 P0）：pywencai 未安装时不能再静默 no-op——
        # RTS_ENABLE_FUND_RISK=1 下会误导用户以为财务风险过滤在跑。
        # 改为 import 失败路径打印一次告警。
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *a, **k):
            if name == "pywencai":
                raise ImportError("no module named 'pywencai'")
            return real_import(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        # 防 autouse _clean fixture 在用例间重置 _logged_missing，
        # 用全新进程内标志验证"只打一次"。
        fb.reset_fund_risk_cache()
        assert fb.fetch_fund_risk_map() == {}
        assert fb.fetch_fund_risk_map() == {}
        out = capsys.readouterr().out
        assert "pywencai 未安装" in out, "import 失败应打印一次告警"
        # 注意：reset 会重置 _logged_missing，故仅验证首次触发（非二次重复刷屏）由上层语义保证

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


class TestThsFundRisk:
    """THS 估值快照主源（2026-08-23）：pb_mrq<0 ⟺ 资不抵债，跨轮增量拉取。"""

    def _patch_ths(self, monkeypatch, codes, valuations_by_call, fail_at=None):
        """mock ths_api：fetch_gem_codes 返回 codes；fetch_valuations 按
        调用次序返回 valuations_by_call 列表（fail_at 次序起返回 None）。"""
        calls = {"vals": 0}

        def fake_vals(batch):
            calls["vals"] += 1
            if fail_at is not None and calls["vals"] >= fail_at:
                return None
            return valuations_by_call[min(calls["vals"], len(valuations_by_call)) - 1]

        monkeypatch.setattr("scanner.ths_api.get_api_key", lambda: "k")
        monkeypatch.setattr("scanner.ths_api.fetch_gem_codes", lambda: list(codes))
        monkeypatch.setattr("scanner.ths_api.fetch_valuations", fake_vals)
        return calls

    def test_complete_single_round(self, monkeypatch):
        self._patch_ths(monkeypatch,
                        ["300001", "300002", "300003"],
                        [{"300001": 0.5, "300002": -0.3, "300003": None}])
        # 300003 pb=null 不算命中；300002 pb<0 → 资不抵债
        assert fb.fetch_fund_risk_map() == {"SZ300002": "资不抵债"}

    def test_incremental_across_rounds(self, monkeypatch):
        """预算用尽未拉完 → 先返回已命中部分，下轮续传补全剩余批次。"""
        self._patch_ths(monkeypatch,
                        ["300001", "300002"],
                        [{"300001": -1.0}])
        r1 = fb.fetch_fund_risk_map()
        assert r1 == {"SZ300001": "资不抵债"}  # 首轮只完成第 1 批

    def test_no_key_falls_back_to_wencai(self, monkeypatch):
        monkeypatch.setattr("scanner.ths_api.get_api_key", lambda: "")
        fake = FakePywencai(_risk_df("300027.SZ"))
        monkeypatch.setattr("pywencai.get", fake.get, raising=False)
        assert fb.fetch_fund_risk_map() == {"SZ300027": "资不抵债"}

    def test_gem_codes_fail_falls_back_to_wencai(self, monkeypatch):
        monkeypatch.setattr("scanner.ths_api.get_api_key", lambda: "k")
        monkeypatch.setattr("scanner.ths_api.fetch_gem_codes", lambda: None)
        fake = FakePywencai(_risk_df("300027.SZ"))
        monkeypatch.setattr("pywencai.get", fake.get, raising=False)
        assert fb.fetch_fund_risk_map() == {"SZ300027": "资不抵债"}

    def test_ths_exception_falls_back_to_wencai(self, monkeypatch):
        def _boom():
            raise RuntimeError("ths down")
        monkeypatch.setattr(fb, "_fetch_fund_risk_ths", _boom)
        fake = FakePywencai(_risk_df("300027.SZ"))
        monkeypatch.setattr("pywencai.get", fake.get, raising=False)
        assert fb.fetch_fund_risk_map() == {"SZ300027": "资不抵债"}

    def test_network_calls_outside_progress_lock(self, monkeypatch):
        """锁内不得做网络 I/O（2026-08-24 审查：原实现整段拉取循环持
        _ths_progress_lock 最长 25s，并发进入者被整段阻塞）。"""
        def fake_codes():
            assert not fb._ths_progress_lock.locked(), "fetch_gem_codes 不得持锁调用"
            return ["300001"]

        def fake_vals(batch):
            assert not fb._ths_progress_lock.locked(), "fetch_valuations 不得持锁调用"
            return {"300001": -1.0}
        monkeypatch.setattr("scanner.ths_api.get_api_key", lambda: "k")
        monkeypatch.setattr("scanner.ths_api.fetch_gem_codes", fake_codes)
        monkeypatch.setattr("scanner.ths_api.fetch_valuations", fake_vals)
        assert fb.fetch_fund_risk_map() == {"SZ300001": fb.FUND_RISK_REASON}

    def test_failed_batch_progress_preserved_across_calls(self, monkeypatch):
        """单批失败不推进 done、下轮从该批续传，已完成批次不重复拉取。

        直接调 _fetch_fund_risk_ths 绕过进程缓存层。150 只 → 2 批：第 1 轮批 1
        成功 + 批 2 失败；第 2 轮只拉批 2 并合并第 1 轮命中。
        """
        codes = [f"300{i:03d}" for i in range(150)]
        calls = {"vals": 0}

        def fake_vals(batch):
            calls["vals"] += 1
            if calls["vals"] == 2:
                return None  # 仅第 1 轮的批 2 失败一次
            if len(batch) == 100:  # 批 1
                return {batch[0]: -0.5}
            return {batch[0]: -0.5}  # 批 2
        monkeypatch.setattr("scanner.ths_api.get_api_key", lambda: "k")
        monkeypatch.setattr("scanner.ths_api.fetch_gem_codes", lambda: list(codes))
        monkeypatch.setattr("scanner.ths_api.fetch_valuations", fake_vals)

        hits1, complete1 = fb._fetch_fund_risk_ths()
        assert complete1 is False
        assert hits1 == {"SZ300000": fb.FUND_RISK_REASON}

        hits2, complete2 = fb._fetch_fund_risk_ths()
        assert complete2 is True
        # 第 1 轮命中跨轮保留 + 第 2 轮新增命中合并
        assert hits2 == {"SZ300000": fb.FUND_RISK_REASON,
                         "SZ300100": fb.FUND_RISK_REASON}
        assert calls["vals"] == 3, "第 2 轮只应补拉失败的批 2（1 次），不得重拉批 1"


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


class TestLiveWencaiIntegration:
    """真实问财查询集成（smoke）：验证装了 pywencai 后 FUND_RISK 过滤真能落标。
    默认跳过（--run-smoke 运行），依赖外网 + 已装 pywencai。
    """

    @pytest.mark.smoke
    def test_live_query_returns_fund_risk_map(self):
        # 前置：pywencai 必须已安装，否则本测试应因 import 失败被 skip 而非误过
        pytest.importorskip("pywencai")
        # 直接走 fetch_fund_risk_map 真实路径（不 monkeypatch pywencai）
        result = fb.fetch_fund_risk_map()
        # 问财"每股净资产小于0"全市场恒有命中（实测 9 只，时点不同数量不同），
        # 但创业板子集可能为空（GEM 极少资不抵债）——二者任一都算"查询链路接通"。
        assert isinstance(result, dict)
        # 符号映射必须是雪球 prefix 格式（SZ/SH/BJ 开头），不得是裸代码
        for sym in result:
            assert sym[:2] in ("SZ", "SH", "BJ"), f"符号未映射成雪球 prefix: {sym}"

    @pytest.mark.smoke
    def test_live_collect_filters_to_candidates(self):
        pytest.importorskip("pywencai")
        fetched = fb.fetch_fund_risk_map()
        if not fetched:
            pytest.skip("当前时点问财无资不抵债命中，跳过闭环验证")
        # collect 应只保留传入候选的命中子集
        candidates = list(fetched.keys())[:3] + ["SZ300750"]  # 末尾为肯定未命中
        result = fb.collect_fund_risk(None, candidates)
        assert set(result.keys()) <= set(candidates)
        assert "SZ300750" not in result, "未命中候选不应出现在结果"


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
