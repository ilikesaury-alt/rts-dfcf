"""行情增强数据层测试（涨停池 + 个股资金流）：解析/符号转换/缓存/失败降级。

涨停池用 monkeypatch 替换 akshare 拉取函数；资金流为自实现直连东财 ulist API
（2026-10-15 从 clist 分页迁移），monkeypatch _requests.get 返回伪 JSON，
均不依赖外网。
"""

import time

import pandas as pd
import pytest

import scanner.market_extra as me


def _zt_df():
    return pd.DataFrame(
        [
            {
                "代码": "300001",
                "名称": "特锐德",
                "涨跌幅": 10.0,
                "最新价": 15.0,
                "成交额": 1e8,
                "流通市值": 5e9,
                "总市值": 6e9,
                "换手率": 5.0,
                "封板资金": 3e7,
                "首次封板时间": "093000",
                "最后封板时间": "093000",
                "炸板次数": 0,
                "涨停统计": "1/1",
                "连板数": 1,
                "所属行业": "充电桩",
            },
            {
                "代码": "300002",
                "名称": "测试",
                "涨跌幅": 10.0,
                "最新价": 20.0,
                "成交额": 2e8,
                "流通市值": 8e9,
                "总市值": 9e9,
                "换手率": 8.0,
                "封板资金": 5e7,
                "首次封板时间": "093000",
                "最后封板时间": "103000",
                "炸板次数": 2,
                "涨停统计": "3/3",
                "连板数": 3,
                "所属行业": "软件",
            },
        ]
    )


def _ff_rows(*codes):
    """构造 ulist diff 行（字段码命名同东财接口）。"""
    rows = []
    for i, code in enumerate(codes):
        rows.append(
            {
                "f12": code,
                "f14": "测试",
                "f2": 15.0,
                "f3": 10.0,
                "f62": 123456789.0 + i,
                "f184": 8.5,
                "f66": 60000000.0 + i,
                # 报价快照字段（与资金流同一响应行，见 _absorb 注释）
                "f5": 1000.0 + i,
                "f6": 15000000.0 + i,
                "f8": 3.2,
                "f10": 2.5,
                "f18": 13.64,
                "f20": 4.0e9 + i,
                "f21": 3.0e9 + i,
            }
        )
    return rows


def _ff_table(*codes):
    """{6位代码: row} 表，供 _NetCounter 按 secid 回行。"""
    return dict(zip(codes, _ff_rows(*codes), strict=True))


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
    """ulist 批量伪服务端：按请求参数 secids 回行，可控失败/延迟/原始响应。"""

    def __init__(self, rows_by_code=None, error=None, fail_codes=(), slow_codes=(), delay=0.05, raw=None):
        self.calls = 0
        self.requests: list[list[str]] = []  # 每次请求的 6 位码列表（按调用顺序）
        self.rows_by_code = rows_by_code if rows_by_code is not None else {}
        self.error = error
        self.fail_codes = set(fail_codes)  # 请求含任一码则抛 error（模拟某批失败）
        self.slow_codes = set(slow_codes)  # 请求含任一码则 sleep（超时用例）
        self.delay = delay
        self.raw = raw  # 非 None 时直接替换响应体（data=None 等）

    def get(self, *a, **k):
        self.calls += 1
        params = k.get("params", {})
        codes = [s.split(".", 1)[1] for s in str(params.get("secids", "")).split(",") if s]
        self.requests.append(codes)
        if self.error and (not self.fail_codes or set(codes) & self.fail_codes):
            raise self.error
        if set(codes) & self.slow_codes:
            time.sleep(self.delay)
        if self.raw is not None:
            return FakeResp(self.raw)
        diff = [self.rows_by_code[c] for c in codes if c in self.rows_by_code]
        return FakeResp({"rc": 0, "data": {"total": len(diff), "diff": diff}})


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    me.reset_extra_cache()
    # 默认屏蔽 THS 主源（返回 None = 未配置/失败），既有用例走 AKShare 兜底路径；
    # THS 主源行为在 TestFetchZtPoolThsSource 中单独覆盖。
    monkeypatch.setattr(me, "_fetch_zt_pool_ths", lambda date_key=None: None)
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
        import requests as _requests

        class Bad:
            def stock_zt_pool_em(self, date):
                raise _requests.RequestException("network down")

        monkeypatch.setattr(me, "_get_ak", lambda: Bad())
        assert me.fetch_zt_pool("20260805") == {}

    def test_ak_unavailable(self, monkeypatch):
        monkeypatch.setattr(me, "_get_ak", lambda: None)
        assert me.fetch_zt_pool("20260805") == {}


class TestFetchZtPoolThsSource:
    """THS 官方涨停池主源（2026-08-23 接入）：主源成功不触 AKShare，
    主源失败/未配置 → AKShare 兜底。"""

    def test_ths_primary(self, monkeypatch):
        ths_payload = {
            "300001": {"lianban": 1, "zt_stat": "首板", "fengban_amt": 1.3e8, "zhaban": 0, "industry": "充电桩"},
            "300002": {"lianban": 3, "zt_stat": "3天3板", "fengban_amt": 2.4e8, "zhaban": 2, "industry": "CPO+光模块"},
        }
        calls = []
        monkeypatch.setattr(me, "_fetch_zt_pool_ths", lambda date_key=None: calls.append(date_key) or ths_payload)
        fake = FakeAk()
        monkeypatch.setattr(me, "_get_ak", lambda: fake)
        result = me.fetch_zt_pool("20260821")
        assert result["300001"]["lianban"] == 1
        assert result["300002"]["industry"] == "CPO+光模块"
        assert calls == ["20260821"]
        assert fake.zt_calls == 0  # 主源成功，AKShare 不被触发

    def test_ths_fail_falls_back_to_ak(self, monkeypatch):
        monkeypatch.setattr(me, "_fetch_zt_pool_ths", lambda date_key=None: None)
        fake = FakeAk()
        monkeypatch.setattr(me, "_get_ak", lambda: fake)
        result = me.fetch_zt_pool("20260805")
        assert result["300001"]["lianban"] == 1
        assert fake.zt_calls == 1

    def test_ths_exception_falls_back_to_ak(self, monkeypatch):
        import requests as _requests

        def _boom(date_key=None):
            raise _requests.RequestException("ths down")

        monkeypatch.setattr(me, "_fetch_zt_pool_ths", _boom)
        fake = FakeAk()
        monkeypatch.setattr(me, "_get_ak", lambda: fake)
        assert me.fetch_zt_pool("20260805")["300002"]["zhaban"] == 2


class TestFetchFundFlow:
    def test_parse(self, monkeypatch):
        net = _NetCounter(rows_by_code=_ff_table("300001", "300002"))
        monkeypatch.setattr(me._requests, "get", net.get)
        result = me.fetch_fund_flow_rank()
        assert "300001" in result
        assert result["300001"]["main_net"] == 123456789.0
        assert result["300001"]["main_pct"] == 8.5
        assert result["300001"]["super_net"] == 60000000.0

    def test_universe_is_gem_enumeration_and_batch_capped(self, monkeypatch):
        """默认取数域 = 创业板全集（300000-302999），每批 ≤ _FUND_FLOW_BATCH_SIZE。"""
        net = _NetCounter()
        monkeypatch.setattr(me._requests, "get", net.get)
        me._collect_fund_flow({}, time.time() + 60)
        assert net.requests, "至少一批请求"
        all_codes = [c for req in net.requests for c in req]
        assert all_codes[0] == "300000" and all_codes[-1] == "302999"
        assert len(all_codes) == len(me._gem_universe_codes())
        assert max(len(req) for req in net.requests) <= me._FUND_FLOW_BATCH_SIZE
        expected_batches = -(-len(me._gem_universe_codes()) // me._FUND_FLOW_BATCH_SIZE)
        assert len(net.requests) == expected_batches

    def test_secid_prefix_sh_and_sz(self):
        assert me._secid("600519") == "1.600519"
        assert me._secid("688001") == "1.688001"
        assert me._secid("300001") == "0.300001"
        assert me._secid("000001") == "0.000001"

    def test_fail_soft(self, monkeypatch):
        import requests as _requests

        net = _NetCounter(error=_requests.RequestException("blocked by proxy"))
        monkeypatch.setattr(me._requests, "get", net.get)
        assert me.fetch_fund_flow_rank() == {}
        assert net.calls >= 1

    def test_zero_result_not_reported_as_partial(self, monkeypatch, capsys):
        """0 只必须报"获取失败"而非"超时（部分数据）"（2026-10-12）。

        首批失败与"部分超时"共用同一文案时，真故障会被读成"少几只、下轮补"，
        排查无从下手。失败原因须随告警打印出来。
        """
        import requests as _requests

        net = _NetCounter(error=_requests.ConnectionError("host unreachable"))
        monkeypatch.setattr(me._requests, "get", net.get)
        assert me.fetch_fund_flow_rank() == {}
        out = capsys.readouterr().out
        assert "获取失败（0 只）" in out
        assert "部分数据" not in out, "0 只不是部分数据，两种故障不得共用文案"
        assert "host unreachable" in out, "失败原因必须可见，否则无法定位网络/代理问题"
        # 首批同步失败（含一次重试）后快速失败，不对余下批次打请求
        assert net.calls == 2

    def test_empty_data_backoff(self, monkeypatch):
        # 接口返回空 data（非交易时段/无数据）→ {}，按失败短退避缓存不重复打网络
        net = _NetCounter(raw={"rc": 0, "data": None})
        monkeypatch.setattr(me._requests, "get", net.get)
        assert me.fetch_fund_flow_rank() == {}
        assert me.fetch_fund_flow_rank() == {}
        assert net.calls == 2  # 首批失败含一次重试；短退避后第二次调用零请求

    def test_deadline_expired_returns_empty(self, monkeypatch):
        # deadline 已过：一步不拉，直接返回 {}（调用方按软降级处理）
        net = _NetCounter()
        monkeypatch.setattr(me._requests, "get", net.get)
        result = me._collect_fund_flow({}, time.time() - 1)
        assert result == {}
        assert net.calls == 0

    def test_multibatch_merge(self, monkeypatch):
        # 多批合并：全集切多批，每批各自的行全部聚合；无批失败 → done 置位
        net = _NetCounter(rows_by_code=_ff_table("300001", "300501", "301001"))
        monkeypatch.setattr(me._requests, "get", net.get)
        box = {}
        result = me._collect_fund_flow(box, time.time() + 60)
        assert set(result) == {"300001", "300501", "301001"}
        assert result["300501"]["main_net"] == 123456789.0 + 1
        assert result["301001"]["main_pct"] == 8.5
        assert box["value"] == result
        assert box.get("done") is True, "完整拉取应标记完成（供外层区分完整/部分快照）"
        assert "err" not in box

    def test_batch_failure_prevents_done(self, monkeypatch):
        """任一批失败 → 不置 done → 外层按部分/失败路径 60s 短退避重试。

        clist 时代中间页失败仍 done=True、缺页数据被当完整快照静默缓存 300s ——
        ulist 批变大（500/批），单批缺失占比更高，必须显式重试。
        """
        import requests as _requests

        net = _NetCounter(
            rows_by_code=_ff_table("300001", "300501"), error=_requests.ConnectionError("flaky"), fail_codes={"300501"}
        )
        monkeypatch.setattr(me._requests, "get", net.get)
        box = {}
        result = me._collect_fund_flow(box, time.time() + 60)
        assert "300001" in result
        assert box.get("done") is not True, "缺批数据不得当完整快照"
        assert "flaky" in box.get("err", "")

    def test_absorb_captures_quote_snapshot(self, monkeypatch):
        # 报价快照：与资金流同行返回，零额外请求。这是榜外票（无 daily_kline）
        # 唯一的价量来源，落库后累积成全集日频价量面板 —— 不得再被丢弃。
        net = _NetCounter(rows_by_code=_ff_table("300001"))
        monkeypatch.setattr(me._requests, "get", net.get)
        result = me._collect_fund_flow({}, time.time() + 60)
        payload = result["300001"]
        # 报价快照字段全部落进 payload
        assert payload["price"] == 15.0
        assert payload["percent"] == 10.0
        assert payload["volume"] == 1000.0
        assert payload["amount"] == 15000000.0
        assert payload["turnover"] == 3.2
        assert payload["vol_ratio"] == 2.5
        assert payload["prev_close"] == 13.64
        assert payload["total_cap"] == 4.0e9
        assert payload["float_cap"] == 3.0e9
        # 原三字段不受影响（消费方按 .get 读取，加键为纯增量）
        assert payload["main_net"] == 123456789.0
        assert payload["main_pct"] == 8.5

    def test_absorb_quote_fields_fail_soft(self, monkeypatch):
        # 报价字段缺失/不可解析时退化为 0.0，不得抛异常影响整轮扫描
        row = {"f12": "300001", "f62": 1.0, "f184": 2.0, "f66": 0.0, "f2": None, "f3": "N/A", "f10": float("nan")}
        net = _NetCounter(rows_by_code={"300001": row})
        monkeypatch.setattr(me._requests, "get", net.get)
        result = me._collect_fund_flow({}, time.time() + 60)
        payload = result["300001"]
        assert payload["price"] == 0.0
        assert payload["percent"] == 0.0
        assert payload["vol_ratio"] == 0.0

    def test_deadline_partial_marks_not_done(self, monkeypatch):
        # 多批拉到一半超时：取消剩余任务，box["done"] 不置位 → 外层按部分快照处理
        # 第 2~6 批各放一个慢码，保证 deadline 到点时首批已收集、余批未完成。
        slow = [f"300{base}01" for base in (5, 10, 15, 20, 25)]
        net = _NetCounter(rows_by_code=_ff_table("300001"), slow_codes=slow, delay=0.2)
        monkeypatch.setattr(me._requests, "get", net.get)
        box = {}
        result = me._collect_fund_flow(box, time.time() + 0.001)
        assert box.get("done") is not True, "超时部分拉取不得标记完成"
        assert set(result) == {"300001"}, "仅首批已收集，后续批被取消"

    def test_bounded_flags_partial_when_thread_done_but_incomplete(self, monkeypatch):
        # 回归：线程在 join 超时前恰好跑完内部 deadline，_collect_fund_flow 未标记
        # done → _fetch_fund_flow_bounded 必须仍判为超时（部分结果），不得当完整快照。

        def _collect_that_finishes_early(box_, deadline, codes=None):
            box_["value"] = {"300001": {"main_net": 1.0, "main_pct": 2.0, "super_net": 0}}
            return box_["value"]  # 不置 box_["done"] → 模拟内部超时提前收工

        monkeypatch.setattr(me, "_collect_fund_flow", _collect_that_finishes_early)
        value, timed_out, err = me._fetch_fund_flow_bounded(0.01)
        assert timed_out is True, "线程提前结束但未标记完成 → 仍应按部分结果处理"
        assert value == {"300001": {"main_net": 1.0, "main_pct": 2.0, "super_net": 0}}
        assert err == ""

    def test_bounded_complete_not_timed_out(self, monkeypatch):

        def _collect_done(box_, deadline, codes=None):
            box_["value"] = {"300001": {"main_net": 1.0, "main_pct": 2.0, "super_net": 0}}
            box_["done"] = True
            return box_["value"]

        monkeypatch.setattr(me, "_collect_fund_flow", _collect_done)
        _, timed_out, _err = me._fetch_fund_flow_bounded(0.01)
        assert timed_out is False, "显式标记完成 → 判定为完整快照"


class TestRegistration:
    """注册码机制（2026-10-15 取数域收敛）：榜内主板票并入取数域。"""

    def test_registered_board_code_fetched_and_merged(self, monkeypatch):
        net = _NetCounter(rows_by_code={"600519": _ff_rows("600519")[0]})
        monkeypatch.setattr(me._requests, "get", net.get)
        me.register_fund_flow_codes(["SH600519"])
        result = me.fetch_fund_flow_rank()
        assert "600519" in result
        # 注册码出现在某批 secids 里；全集批（纯创业板）不含它
        extra_reqs = [req for req in net.requests if "600519" in req]
        assert extra_reqs, "注册码必须出现在某批 secids 里"
        gem_reqs = [req for req in net.requests if req and all(c.startswith("3") for c in req)]
        assert gem_reqs, "全集批存在"

    def test_cache_hit_when_registered_subset_of_universe(self, monkeypatch):
        net = _NetCounter(rows_by_code=_ff_table("300001"))
        monkeypatch.setattr(me._requests, "get", net.get)
        me.register_fund_flow_codes(["600519"])
        me.fetch_fund_flow_rank()
        first = net.calls
        me.fetch_fund_flow_rank()
        assert net.calls == first, "TTL 内且注册码 ⊆ 上次拉取域 → 缓存命中零请求"

    def test_new_registration_refetches(self, monkeypatch):
        net = _NetCounter(rows_by_code=_ff_table("300001", "600519"))
        monkeypatch.setattr(me._requests, "get", net.get)
        me.fetch_fund_flow_rank()  # 第一轮：仅创业板全集
        assert all(c.startswith("3") for req in net.requests for c in req)
        calls_first = net.calls
        me.register_fund_flow_codes(["600519"])  # 榜单换血：新主板票上榜
        result = me.fetch_fund_flow_rank()
        assert net.calls > calls_first, "注册新码必须触发重拉补齐"
        assert "600519" in result

    def test_registry_scoped_by_date(self, monkeypatch):
        me.register_fund_flow_codes(["600519"])
        # 模拟跨日：注册日期戳不是今天 → 读侧视为空
        me._ff_extra_codes_date = "19990101"
        assert me._registered_ff_codes() == set()


class TestCollect:
    def test_maps_to_xq_symbols(self, monkeypatch):
        fake = FakeAk()
        monkeypatch.setattr(me, "_get_ak", lambda: fake)
        net = _NetCounter(rows_by_code=_ff_table("300001"))
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
        # 创业板全集一次拉取（多批）：300002 本就在全集内，不触发额外请求
        expected_batches = -(-len(me._gem_universe_codes()) // me._FUND_FLOW_BATCH_SIZE)
        assert net.calls == expected_batches, "资金流全集只拉一次"

    def test_board_symbols_fetched_via_registration(self, monkeypatch):
        """榜内主板票（非创业板）→ 注册码 → 补拉落库（A 段量比/榜内异动段依赖）。"""
        net = _NetCounter(rows_by_code={"600519": _ff_rows("600519")[0]})
        monkeypatch.setattr(me._requests, "get", net.get)
        monkeypatch.setattr(me, "get_market_extra_cache", lambda *a, **k: {})
        saved = {}
        monkeypatch.setattr(me, "save_market_extra_cache", lambda conn, m, dt: saved.update({dt: dict(m)}))
        result = me.collect_market_extra(None, ["SH600519"], include_zt=False)
        assert result["SH600519"]["fund_flow"]["main_pct"] == 8.5
        assert saved.get("fund_flow", {}).get("SH600519", {}).get("main_net") == 123456789.0
        # 主板码不在创业板全集批内（全集批纯 0.3 开头），靠注册码补拉

    def test_complete_fetch_saves_full_market(self, monkeypatch):
        """完整拉取：保存全市场快照（含非当前候选），保证重启/掉榜后展示层可读。"""
        monkeypatch.setattr(me, "get_market_extra_cache", lambda *a, **k: {})
        saved = {}
        monkeypatch.setattr(me, "save_market_extra_cache", lambda conn, m, dt: saved.update({dt: dict(m)}))
        monkeypatch.setattr(
            me,
            "fetch_fund_flow_rank",
            lambda: {
                "300001": {"main_net": 1e7, "main_pct": 8.5, "super_net": 0},
                "300999": {"main_net": -1e7, "main_pct": -3.0, "super_net": 0},
            },
        )
        monkeypatch.setattr(me, "_last_ff_partial", False)
        result = me.collect_market_extra(None, ["SZ300001"], include_zt=False)
        assert "SZ300001" in result
        assert "SZ300999" in saved.get("fund_flow", {}), "完整拉取应保存全集快照（含非候选）"

    def test_partial_fetch_saves_candidates_only(self, monkeypatch):
        """超时部分结果：只保存当前缺失候选，不把不完整数据当快照冻结。"""
        monkeypatch.setattr(me, "get_market_extra_cache", lambda *a, **k: {})
        saved = {}
        monkeypatch.setattr(me, "save_market_extra_cache", lambda conn, m, dt: saved.update({dt: dict(m)}))
        monkeypatch.setattr(
            me,
            "fetch_fund_flow_rank",
            lambda: {
                "300001": {"main_net": 1e7, "main_pct": 8.5, "super_net": 0},
                "300999": {"main_net": 1e7, "main_pct": 8.5, "super_net": 0},
            },
        )
        monkeypatch.setattr(me, "_last_ff_partial", True)
        me.collect_market_extra(None, ["SZ300001"], include_zt=False)
        assert "SZ300999" not in saved.get("fund_flow", {}), "部分结果不应保存非候选快照"
        assert "SZ300001" in saved.get("fund_flow", {})

    def test_db_hit_skips_fetch(self, monkeypatch):
        fake = FakeAk()
        monkeypatch.setattr(me, "_get_ak", lambda: fake)
        net = _NetCounter(error=AssertionError("不该打网络"))
        monkeypatch.setattr(me._requests, "get", net.get)
        monkeypatch.setattr(
            me, "get_market_extra_cache", lambda conn, syms, dt, intraday_ttl_sec=None: {"SZ300001": {"lianban": 5}}
        )
        saved = {}
        monkeypatch.setattr(me, "save_market_extra_cache", lambda conn, m, dt: saved.update({dt: dict(m)}))
        result = me.collect_market_extra(None, ["SZ300001"], include_flow=False)
        assert result["SZ300001"]["zt"]["lianban"] == 5
        assert fake.zt_calls == 0
        assert net.calls == 0

    def test_db_stale_entry_falls_through_to_fetch(self, monkeypatch):
        fake = FakeAk()
        monkeypatch.setattr(me, "_get_ak", lambda: fake)
        net = _NetCounter()
        monkeypatch.setattr(me._requests, "get", net.get)
        # DB 层 intraday_ttl 过期返回空 → 视为缺失 → 触发 fetch
        monkeypatch.setattr(me, "get_market_extra_cache", lambda conn, syms, dt, intraday_ttl_sec=None: {})
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
        import requests as _requests

        net = _NetCounter(error=_requests.RequestException("blocked"))
        monkeypatch.setattr(me._requests, "get", net.get)
        assert me.fetch_fund_flow_rank() == {}
        assert me.fetch_fund_flow_rank() == {}
        assert net.calls == 2  # 首批失败含一次重试；短退避后第二次调用零请求, "失败缓存空结果：TTL 内第二次调用不再打网络"

    def test_zt_failure_backoff(self, monkeypatch):
        import requests as _requests

        class Bad:
            calls = 0

            def stock_zt_pool_em(self, date):
                Bad.calls += 1
                raise _requests.RequestException("network down")

        monkeypatch.setattr(me, "_get_ak", lambda: Bad())
        assert me.fetch_zt_pool("20260805") == {}
        assert me.fetch_zt_pool("20260805") == {}
        assert Bad.calls == 1, "失败缓存空结果：TTL 内第二次调用不再打网络"


class TestNumCell:
    """回归：_num 对 NaN/±inf/字符串脏值 → 0（此前仅判 `v != v`，inf 与 "NaN" 字符串漏网）。"""

    def test_inf_and_nan_coerced(self):
        import math

        for bad in (float("nan"), float("inf"), float("-inf"), "NaN", "Infinity", "abc", "", None):
            row = {"f62": bad}
            v = me._num(row, "f62")
            assert math.isfinite(v) and v == 0.0, f"f62={bad!r} → {v}"

    def test_finite_kept(self):
        assert me._num({"f62": 123.5}, "f62") == 123.5
        assert me._num({"f62": "8.25"}, "f62") == 8.25


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
        from datetime import timedelta

        from scanner.config import now_beijing
        from scanner.database import get_market_extra_cache

        old = (now_beijing() - timedelta(seconds=600)).isoformat()
        memory_db.execute(
            "INSERT INTO market_extra_cache (symbol, date, data_type, payload_json, updated) VALUES (?, ?, ?, ?, ?)",
            ("SZ300001", now_beijing().date().isoformat(), "fund_flow", '{"main_pct": 9.0}', old),
        )
        memory_db.commit()
        # 盘中 TTL 内视为过期 → 不返回
        assert get_market_extra_cache(memory_db, ["SZ300001"], "fund_flow", intraday_ttl_sec=300) == {}
        # 无 intraday_ttl 仍返回（报表读旧数据）
        got = get_market_extra_cache(memory_db, ["SZ300001"], "fund_flow")
        assert got["SZ300001"]["main_pct"] == 9.0

    def test_as_of_queries_target_date_ignoring_ttl(self, memory_db):
        """as_of 历史回放（2026-08-24 第二轮审查）：按目标日期查且忽略 intraday TTL。

        原实现硬编码今日日期——today_report --date 回放的资金流展示/归因/
        回马枪排序吃到错日或空数据。
        """
        from scanner.database import get_market_extra_cache, save_market_extra_cache

        save_market_extra_cache(memory_db, {"SZ300001": {"main_pct": 6.5}}, "fund_flow")  # 写入为今日
        today = memory_db.execute("SELECT date FROM market_extra_cache").fetchone()[0]
        # as_of=今日 → 正常返回
        got = get_market_extra_cache(memory_db, ["SZ300001"], "fund_flow", as_of=today)
        assert got["SZ300001"]["main_pct"] == 6.5
        # as_of=其它日 → 无该日条目返回空
        assert get_market_extra_cache(memory_db, ["SZ300001"], "fund_flow", as_of="2020-01-01") == {}
