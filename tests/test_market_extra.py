"""行情增强数据层测试（涨停池 + 个股资金流）：解析/符号转换/缓存/失败降级。

用 monkeypatch 替换 akshare 拉取函数，不依赖外网。
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


def _ff_df():
    return pd.DataFrame([
        {"序号": 1, "代码": "300001", "名称": "特锐德", "最新价": 15.0,
         "今日涨跌幅": 10.0, "今日主力净流入-净额": 123456789.0,
         "今日主力净流入-净占比": 8.5, "今日超大单净流入-净额": 60000000.0,
         "今日超大单净流入-净占比": 4.1, "今日大单净流入-净额": 1.0,
         "今日大单净流入-净占比": 1.0, "今日中单净流入-净额": 1.0,
         "今日中单净流入-净占比": 1.0, "今日小单净流入-净额": 1.0,
         "今日小单净流入-净占比": 1.0},
    ])


class FakeAk:
    def __init__(self):
        self.zt_calls = 0
        self.ff_calls = 0

    def stock_zt_pool_em(self, date):
        self.zt_calls += 1
        return _zt_df()

    def stock_individual_fund_flow_rank(self, indicator="今日"):
        self.ff_calls += 1
        return _ff_df()


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
    def test_parse(self, monkeypatch):
        fake = FakeAk()
        monkeypatch.setattr(me, "_get_ak", lambda: fake)
        result = me.fetch_fund_flow_rank()
        assert "300001" in result
        assert result["300001"]["main_net"] == 123456789.0
        assert result["300001"]["main_pct"] == 8.5
        assert result["300001"]["super_net"] == 60000000.0

    def test_fail_soft(self, monkeypatch):
        class Bad:
            def stock_individual_fund_flow_rank(self, indicator="今日"):
                raise RuntimeError("blocked by proxy")
        monkeypatch.setattr(me, "_get_ak", lambda: Bad())
        assert me.fetch_fund_flow_rank() == {}


class TestCollect:
    def test_maps_to_xq_symbols(self, monkeypatch):
        fake = FakeAk()
        monkeypatch.setattr(me, "_get_ak", lambda: fake)
        monkeypatch.setattr(me, "get_market_extra_cache", lambda *a, **k: {})
        saved = {}
        monkeypatch.setattr(me, "save_market_extra_cache", lambda conn, m, dt: saved.update({dt: dict(m)}))
        result = me.collect_market_extra(None, ["SZ300001", "SZ300002"])
        assert "SZ300001" in result
        assert result["SZ300001"]["zt"]["lianban"] == 1
        assert result["SZ300001"]["fund_flow"]["main_pct"] == 8.5
        assert saved.get("zt_pool", {}).get("SZ300002", {}).get("zhaban") == 2
        assert saved.get("fund_flow", {}).get("SZ300001", {}).get("main_net") == 123456789.0

    def test_db_hit_skips_fetch(self, monkeypatch):
        fake = FakeAk()
        monkeypatch.setattr(me, "_get_ak", lambda: fake)
        monkeypatch.setattr(me, "get_market_extra_cache",
                            lambda conn, syms, dt, intraday_ttl_sec=None: {"SZ300001": {"lianban": 5}})
        saved = {}
        monkeypatch.setattr(me, "save_market_extra_cache", lambda conn, m, dt: saved.update({dt: dict(m)}))
        result = me.collect_market_extra(None, ["SZ300001"], include_flow=False)
        assert result["SZ300001"]["zt"]["lianban"] == 5
        assert fake.zt_calls == 0
        assert fake.ff_calls == 0

    def test_db_stale_entry_falls_through_to_fetch(self, monkeypatch):
        fake = FakeAk()
        monkeypatch.setattr(me, "_get_ak", lambda: fake)
        # DB 层 intraday_ttl 过期返回空 → 视为缺失 → 触发 fetch
        monkeypatch.setattr(me, "get_market_extra_cache",
                            lambda conn, syms, dt, intraday_ttl_sec=None: {})
        saved = {}
        monkeypatch.setattr(me, "save_market_extra_cache", lambda conn, m, dt: saved.update({dt: dict(m)}))
        result = me.collect_market_extra(None, ["SZ300001"], include_flow=False)
        assert result["SZ300001"]["zt"]["lianban"] == 1  # 来自 fetch 而非 DB
        assert fake.zt_calls == 1

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
        class Bad:
            calls = 0
            def stock_individual_fund_flow_rank(self, indicator="今日"):
                Bad.calls += 1
                raise RuntimeError("blocked")
        monkeypatch.setattr(me, "_get_ak", lambda: Bad())
        assert me.fetch_fund_flow_rank() == {}
        assert me.fetch_fund_flow_rank() == {}
        assert Bad.calls == 1, "失败缓存空结果：TTL 内第二次调用不再打网络"

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
