"""行情增强数据层测试（涨停池 + 个股资金流）：解析/符号转换/缓存/失败降级。

涨停池用 monkeypatch 替换 akshare 拉取函数；资金流为自实现直连东财 clist API，
monkeypatch _requests.get 返回伪 JSON，均不依赖外网。
"""
import pandas as pd
import pytest

import scanner.market_extra as me


def _zt_df():
    return pd.DataFrame([
        {"代码": "300001", "名称": "特锐德", "涨跌幅": 10.0, "最新价": 15.0,
         "成交额": 1e8, "流通市值": 5e9, "总市值": 6e9, "换手率": 5.0,
         "封板资金": 3e7, "首次封板时间": "093000", "最后封板时间": "093000",
         "炸板次数": 0, "涨停统计": "1/1", "连板数": 1, "所属行业": "充电桩"},
        {"代码": "300002", "名称": "测试", "涨跌幅": 10.0, "最新价": 20.0,
         "成交额": 2e8, "流通市值": 8e9, "总市值": 9e9, "换手率": 8.0,
         "封板资金": 5e7, "首次封板时间": "093000", "最后封板时间": "103000",
         "炸板次数": 2, "涨停统计": "3/3", "连板数": 3, "所属行业": "软件"},
    ])


def _ff_rows(*codes):
    """构造 clist 分页 diff 行（字段码命名同东财接口）。"""
    rows = []
    for i, code in enumerate(codes):
        rows.append({"f12": code, "f14": "测试", "f2": 15.0, "f3": 10.0,
                     "f62": 123456789.0 + i, "f184": 8.5, "f66": 60000000.0 + i})
    return rows


def _ff_json(page, total=100, page_size=100):
    """构造 clist 接口响应 {data: {total, diff}}，按 pn 分页。"""
    all_rows = _ff_rows("300001", "300002")
    idx = (page - 1) * page_size
    diff = all_rows[idx:idx + page_size]
    return {"rc": 0, "data": {"total": total, "diff": diff}}


class FakeAk:
    def __init__(self):
        self.zt_calls = 0

    def stock_zt_pool_em(self, date):
        self.zt_calls += 1
        return _zt_df()


class FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _NetCounter:
    """统计 _requests.get 调用次数并可控返回/抛错。"""
    def __init__(self, pages=None, error=None):
        self.calls = 0
        self.pages = pages
        self.error = error

    def get(self, *a, **k):
        self.calls += 1
        if self.error:
            raise self.error
        pn = int(k.get("params", {}).get("pn", "1"))
        return FakeResp(self.pages.get(pn, {"rc": 0, "data": {"total": 0, "diff": []}}))


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    me.reset_extra_cache()
    yield
    me.reset_extra_cache()


class TestFetchZtPool:
    def test_parse(self, monkeypatch):
        fake = FakeAk()
        monkeypatch.setattr(me, "_get_ak", lambda: fake)
        result = me.fetch_zt_pool("20260805")
        assert set(result.keys()) == {"300001", "300002"}
        assert result["300001"]["lianban"] == 1
        assert result["300001"]["zt_stat"] == "1/1"
        assert result["300001"]["zhaban"] == 0
        assert result["300001"]["industry"] == "充电桩"
        assert result["300002"]["lianban"] == 3
        assert result["300002"]["zhaban"] == 2

    def test_process_cache(self, monkeypatch):
        fake = FakeAk()
        monkeypatch.setattr(me, "_get_ak", lambda: fake)
        me.fetch_zt_pool("20260805")
        me.fetch_zt_pool("20260805")
        assert fake.zt_calls == 1

    def test_fail_soft(self, monkeypatch):
        class Bad:
            def stock_zt_pool_em(self, date):
                raise RuntimeError("network down")
        monkeypatch.setattr(me, "_get_ak", lambda: Bad())
        assert me.fetch_zt_pool("20260805") == {}

    def test_ak_unavailable(self, monkeypatch):
        monkeypatch.setattr(me, "_get_ak", lambda: None)
        assert me.fetch_zt_pool("20260805") == {}


class TestFetchFundFlow:
    def _net(self, total=100, rows=None):
        """单页资金流网络 mock（默认单页 100 只内返回 2 行）。"""
        rows = rows if rows is not None else _ff_rows("300001", "300002")
        return _NetCounter(pages={1: {"rc": 0, "data": {"total": total, "diff": rows}}})

    def test_parse(self, monkeypatch):
        net = self._net()
        monkeypatch.setattr(me._requests, "get", net.get)
        result = me.fetch_fund_flow_rank()
        assert "300001" in result
        assert result["300001"]["main_net"] == 123456789.0
        assert result["300001"]["main_pct"] == 8.5
        assert result["300001"]["super_net"] == 60000000.0

    def test_fail_soft(self, monkeypatch):
        net = _NetCounter(error=RuntimeError("blocked by proxy"))
        monkeypatch.setattr(me._requests, "get", net.get)
        assert me.fetch_fund_flow_rank() == {}
        assert net.calls >= 1

    def test_empty_data_backoff(self, monkeypatch):
        # 接口返回空 data（非交易时段/无数据）→ {}，按失败短退避缓存不重复打网络
        net = _NetCounter(pages={1: {"rc": 0, "data": None}})
        monkeypatch.setattr(me._requests, "get", net.get)
        assert me.fetch_fund_flow_rank() == {}
        assert me.fetch_fund_flow_rank() == {}
        assert net.calls == 1

    def test_deadline_expired_returns_empty(self, monkeypatch):
        # deadline 已过：一步不拉，直接返回 {}（调用方按软降级处理）
        import time as _t
        box = {}
        pages = {1: {"rc": 0, "data": {"total": 250, "diff": _ff_rows("300001")}},
                 2: {"rc": 0, "data": {"total": 250, "diff": _ff_rows("300002")}}}
        net = _NetCounter(pages=pages)
        monkeypatch.setattr(me._requests, "get", net.get)
        result = me._collect_fund_flow(box, _t.time() - 1)
        assert result == {}
        assert net.calls == 0

    def test_multipage_merge(self, monkeypatch):
        # 多页合并：total=250 → 3 页，全部代码按页聚合
        import time as _t
        box = {}
        pages = {pn: {"rc": 0, "data": {"total": 250, "diff": _ff_rows(f"30000{i}")}}
                 for i, pn in enumerate([1, 2, 3], start=1)}
        net = _NetCounter(pages=pages)
        monkeypatch.setattr(me._requests, "get", net.get)
        result = me._collect_fund_flow(box, _t.time() + 60)
        assert set(result) == {"300001", "300002", "300003"}
        assert result["300003"]["main_net"] == 123456789.0
        assert result["300003"]["main_pct"] == 8.5
        assert box["value"] == result


class TestCollect:
    def test_maps_to_xq_symbols(self, monkeypatch):
        fake = FakeAk()
        monkeypatch.setattr(me, "_get_ak", lambda: fake)
        net = _NetCounter(pages={1: {"rc": 0, "data": {"total": 100, "diff": _ff_rows("300001")}}})
        monkeypatch.setattr(me._requests, "get", net.get)
        monkeypatch.setattr(me, "get_market_extra_cache", lambda *a, **k: {})
        saved = {}
        monkeypatch.setattr(me, "save_market_extra_cache", lambda conn, m, dt: saved.update({dt: dict(m)}))
        result = me.collect_market_extra(None, ["SZ300001", "SZ300002"])
        assert "SZ300001" in result
        assert result["SZ300001"]["zt"]["lianban"] == 1
        assert result["SZ300001"]["fund_flow"]["main_pct"] == 8.5
        assert saved.get("zt_pool", {}).get("SZ300002", {}).get("zhaban") == 2
        assert saved.get("fund_flow", {}).get("SZ300001", {}).get("main_net") == 123456789.0
        assert net.calls == 1, "资金流全市场只拉一次"

    def test_db_hit_skips_fetch(self, monkeypatch):
        fake = FakeAk()
        monkeypatch.setattr(me, "_get_ak", lambda: fake)
        net = _NetCounter(error=AssertionError("不该打网络"))
        monkeypatch.setattr(me._requests, "get", net.get)
        monkeypatch.setattr(me, "get_market_extra_cache",
                            lambda conn, syms, dt, intraday_ttl_sec=None: {"SZ300001": {"lianban": 5}})
        saved = {}
        monkeypatch.setattr(me, "save_market_extra_cache", lambda conn, m, dt: saved.update({dt: dict(m)}))
        result = me.collect_market_extra(None, ["SZ300001"], include_flow=False)
        assert result["SZ300001"]["zt"]["lianban"] == 5
        assert fake.zt_calls == 0
        assert net.calls == 0

    def test_db_stale_entry_falls_through_to_fetch(self, monkeypatch):
        fake = FakeAk()
        monkeypatch.setattr(me, "_get_ak", lambda: fake)
        net = _NetCounter(pages={1: {"rc": 0, "data": {"total": 100, "diff": _ff_rows("300001")}}})
        monkeypatch.setattr(me._requests, "get", net.get)
        # DB 层 intraday_ttl 过期返回空 → 视为缺失 → 触发 fetch
        monkeypatch.setattr(me, "get_market_extra_cache",
                            lambda conn, syms, dt, intraday_ttl_sec=None: {})
        saved = {}
        monkeypatch.setattr(me, "save_market_extra_cache", lambda conn, m, dt: saved.update({dt: dict(m)}))
        result = me.collect_market_extra(None, ["SZ300001"], include_flow=False)
        assert result["SZ300001"]["zt"]["lianban"] == 1  # 来自 fetch 而非 DB
        assert fake.zt_calls == 1
        assert net.calls == 0  # include_flow=False 不打资金流网络

    def test_empty_symbols(self, monkeypatch):
        assert me.collect_market_extra(None, []) == {}

    def test_disabled(self, monkeypatch):
        assert me.collect_market_extra(None, ["SZ300001"], include_zt=False, include_flow=False) == {}


class TestCacheDateKey:
    def test_zt_cache_scoped_by_date(self, monkeypatch):
        fake = FakeAk()
        monkeypatch.setattr(me, "_get_ak", lambda: fake)
        me.fetch_zt_pool("20260805")
        me.fetch_zt_pool("20260806")
        assert fake.zt_calls == 2, "不同日期不应命中同一进程缓存"

    def test_fund_flow_failure_backoff(self, monkeypatch):
        net = _NetCounter(error=RuntimeError("blocked"))
        monkeypatch.setattr(me._requests, "get", net.get)
        assert me.fetch_fund_flow_rank() == {}
        assert me.fetch_fund_flow_rank() == {}
        assert net.calls == 1, "失败缓存空结果：TTL 内第二次调用不再打网络"

    def test_zt_failure_backoff(self, monkeypatch):
        class Bad:
            calls = 0
            def stock_zt_pool_em(self, date):
                Bad.calls += 1
                raise RuntimeError("network down")
        monkeypatch.setattr(me, "_get_ak", lambda: Bad())
        assert me.fetch_zt_pool("20260805") == {}
        assert me.fetch_zt_pool("20260805") == {}
        assert Bad.calls == 1, "失败缓存空结果：TTL 内第二次调用不再打网络"


@pytest.fixture
def memory_db():
    import sqlite3
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


class TestDbCacheIntradayTtl:
    def test_save_and_get_fresh(self, memory_db):
        from scanner.database import get_market_extra_cache, save_market_extra_cache
        save_market_extra_cache(memory_db, {"SZ300001": {"lianban": 2}}, "zt_pool")
        got = get_market_extra_cache(memory_db, ["SZ300001"], "zt_pool")
        assert got["SZ300001"]["lianban"] == 2
        # 无 intraday_ttl（stock_report 场景）返回当天条目
        got2 = get_market_extra_cache(memory_db, ["SZ300001"], "zt_pool", intraday_ttl_sec=300)
        assert got2["SZ300001"]["lianban"] == 2

    def test_intraday_ttl_excludes_old_entry(self, memory_db):
        from scanner.database import get_market_extra_cache
        from datetime import timedelta
        from scanner.config import now_beijing
        old = (now_beijing() - timedelta(seconds=600)).isoformat()
        memory_db.execute(
            "INSERT INTO market_extra_cache (symbol, date, data_type, payload_json, updated) "
            "VALUES (?, ?, ?, ?, ?)",
            ("SZ300001", now_beijing().date().isoformat(), "fund_flow",
             '{"main_pct": 9.0}', old),
        )
        memory_db.commit()
        # 盘中 TTL 内视为过期 → 不返回
        assert get_market_extra_cache(memory_db, ["SZ300001"], "fund_flow",
                                      intraday_ttl_sec=300) == {}
        # 无 intraday_ttl 仍返回（报表读旧数据）
        got = get_market_extra_cache(memory_db, ["SZ300001"], "fund_flow")
        assert got["SZ300001"]["main_pct"] == 9.0
