"""同花顺官方 API 客户端回归测试（2026-08-23 接入）。

全部 monkeypatch ths_api._call，不依赖外网与真实 Key。
覆盖：信封解析 / 涨停池映射契约（与 market_extra.fetch_zt_pool 对齐）/
炸板池合并 / 失败返回 None 区别合法空表 / K线脏bar剔除 / 符号转换。
"""
import pytest

from scanner import ths_api


def _pool_body(items):
    return {"code": 0, "message": "success", "data": {"timestamp": 1,
            "pagination": {"total": len(items), "pages": 1, "size": 200, "page": 1},
            "item": items}}


def _kline_body(bars):
    return {"code": 0, "message": "success",
            "data": {"timestamp": 1, "item": bars}}


class TestXqToThs:
    def test_basic(self):
        assert ths_api.xq_to_ths("SZ300033") == "300033.SZ"
        assert ths_api.xq_to_ths("SH600519") == "600519.SH"

    def test_passthrough(self):
        assert ths_api.xq_to_ths("300033.SZ") == "300033.SZ"
        assert ths_api.xq_to_ths("") == ""
        assert ths_api.xq_to_ths("300033") == "300033"  # 非 8 位原样


class TestGetApiKey:
    def test_env_priority(self, monkeypatch):
        monkeypatch.setattr(ths_api, "_api_key", None)
        monkeypatch.setattr(ths_api.os.environ, "get",
                            lambda k, d="": "env-key" if k == "HITHINK_FINANCE_API_KEY" else d)
        assert ths_api.get_api_key() == "env-key"

    def test_no_key_returns_empty(self, monkeypatch):
        monkeypatch.setattr(ths_api, "_api_key", None)
        monkeypatch.setattr(ths_api.os.environ, "get", lambda k, d="": d)
        monkeypatch.setattr(ths_api, "_read_env_file_key", lambda: "")
        assert ths_api.get_api_key() == ""


class TestFetchLimitUpPool:
    def test_mapping_contract(self, monkeypatch):
        """返回契约与 market_extra.fetch_zt_pool 字段对齐（评分链路只用
        lianban/zhaban/industry，stock_report 用 zt_stat/fengban_amt）。"""
        bodies = {
            "/api/a-share/special-data/limit-up-pool": _pool_body([
                {"thscode": "003031.SZ", "ticker": "003031", "name": "测试",
                 "continue_day_text": "首板", "continue_day_cnt": 1,
                 "seal_money": 134898370.0, "limit_up_reason": "CPO+光模块"},
                {"thscode": "300033.SZ", "ticker": "300033", "name": "同花顺",
                 "continue_day_text": "2连板", "continue_day_cnt": 2,
                 "seal_money": None, "limit_up_reason": ""},
            ]),
            "/api/a-share/special-data/limit-break-pool": _pool_body([
                {"thscode": "300033.SZ", "ticker": "300033",
                 "open_times": 3},
            ]),
        }
        monkeypatch.setattr(ths_api, "_call", lambda path, params=None: bodies[path])
        result = ths_api.fetch_limit_up_pool(date_ms=1755734400000)
        assert set(result.keys()) == {"003031", "300033"}
        r1 = result["003031"]
        assert r1["lianban"] == 1 and r1["zt_stat"] == "首板"
        assert r1["fengban_amt"] == pytest.approx(134898370.0)
        assert r1["zhaban"] == 0  # 不在炸板池 → 0
        assert r1["industry"] == "CPO+光模块"
        r2 = result["300033"]
        assert r2["lianban"] == 2 and r2["zt_stat"] == "2连板"
        assert r2["fengban_amt"] == 0.0  # 脏值 None → 0
        assert r2["zhaban"] == 3  # 炸板池 open_times 合并

    def test_break_pool_failure_keeps_main_data(self, monkeypatch):
        """炸板池失败 zhaban 兜 0，不影响涨停主数据。"""
        calls = {"n": 0}

        def fake_call(path, params=None):
            calls["n"] += 1
            if "break" in path:
                return None  # 炸板池失败
            return _pool_body([{"ticker": "003031", "continue_day_cnt": 1}])

        monkeypatch.setattr(ths_api, "_call", fake_call)
        result = ths_api.fetch_limit_up_pool()
        assert result["003031"]["lianban"] == 1 and result["003031"]["zhaban"] == 0

    def test_biz_error_returns_none(self, monkeypatch):
        """业务错误返回 None（区别于非交易日合法空表 {}）→ 调用方降级 AKShare。"""
        monkeypatch.setattr(ths_api, "_call", lambda path, params=None: {"code": 5001})
        assert ths_api.fetch_limit_up_pool() is None

    def test_network_fail_returns_none(self, monkeypatch):
        monkeypatch.setattr(ths_api, "_call", lambda path, params=None: None)
        assert ths_api.fetch_limit_up_pool() is None

    def test_legit_empty_table(self, monkeypatch):
        """非交易日：code=0 + 空 item → 合法空 dict。"""
        monkeypatch.setattr(ths_api, "_call",
                            lambda path, params=None: _pool_body([]))
        assert ths_api.fetch_limit_up_pool() == {}

    def test_no_ticker_rows_skipped(self, monkeypatch):
        monkeypatch.setattr(ths_api, "_call",
                            lambda path, params=None: _pool_body([{"ticker": ""}, {}]))
        assert ths_api.fetch_limit_up_pool(include_break=False) == {}


def _ms(date_str: str) -> int:
    from datetime import datetime
    return int(datetime.strptime(date_str, "%Y-%m-%d").timestamp() * 1000)


class TestFetchKlineCloses:
    def test_parse_and_date_format(self, monkeypatch):
        bars = [
            {"date_ms": _ms("2026-08-21"), "close_price": 222.13},
            {"date_ms": _ms("2026-08-20"), "close_price": 220.22},
        ]
        monkeypatch.setattr(ths_api, "_call", lambda path, params=None: _kline_body(bars))
        out = ths_api.fetch_kline_closes("SZ300033", "2026-08-20", "2026-08-21")
        assert out == {"2026-08-20": 220.22, "2026-08-21": 222.13}

    def test_dirty_bar_skipped(self, monkeypatch):
        bars = [
            {"date_ms": _ms("2026-08-21"), "close_price": 0},      # close<=0 剔除
            {"date_ms": _ms("2026-08-21"), "close_price": None},   # None 剔除
            {"date_ms": None, "close_price": 10.0},                # 无日期剔除
            {"date_ms": _ms("2026-08-20"), "close_price": 9.9},
        ]
        monkeypatch.setattr(ths_api, "_call", lambda path, params=None: _kline_body(bars))
        out = ths_api.fetch_kline_closes("SZ300033", "2026-08-19", "2026-08-21")
        assert out == {"2026-08-20": 9.9}

    def test_error_returns_none(self, monkeypatch):
        monkeypatch.setattr(ths_api, "_call", lambda path, params=None: {"code": 1003})
        assert ths_api.fetch_kline_closes("SZ300033", "bad-date", "2026-08-21") is None

    def test_thscode_conversion_in_params(self, monkeypatch):
        captured = {}

        def fake_call(path, params=None):
            captured.update(params or {})
            return _kline_body([])

        monkeypatch.setattr(ths_api, "_call", fake_call)
        ths_api.fetch_kline_closes("SH600519", "2026-08-01", "2026-08-02")
        assert captured["thscode"] == "600519.SH"
        assert captured["adjust"] == "forward"
