"""scanner.net 共享网络层单源测试（设计审查 P2-10）。

验证 _bounded_call（daemon 线程 + join 限时）的超时/成功/异常传播语义，
以及 EASTMONEY_HEADERS 单源常量。该模块替代原 market_extra / fundamentals 各一份
同构 _bounded_call，与 concept / data_source 各自的 _EM_HEADERS 子集。
"""
import time

from scanner.net import EASTMONEY_HEADERS, _bounded_call


def test_bounded_call_returns_value():
    assert _bounded_call(lambda: 42, 1.0) == 42


def test_bounded_call_timeout_raises():
    try:
        _bounded_call(lambda: time.sleep(2), 0.1, label="AKShare 涨停池")
    except TimeoutError as e:
        assert "AKShare 涨停池" in str(e)
    else:
        raise AssertionError("超时未抛 TimeoutError")


def test_bounded_call_propagates_exception():
    # 子线程异常应透传到主线程（而非被吞掉）
    try:
        _bounded_call(lambda: 1 / 0, 1.0)
    except ZeroDivisionError:
        pass
    else:
        raise AssertionError("子线程异常未透传")


def test_eastmoney_headers_is_complete():
    # 收敛单源：UA + Referer + Accept + Accept-Language 齐全，
    # market_extra / concept / data_source 共用，避免东财协议变更多处漏改。
    assert EASTMONEY_HEADERS["User-Agent"].startswith("Mozilla/5.0")
    assert EASTMONEY_HEADERS["Referer"] == "https://emweb.securities.eastmoney.com/"
    assert "Accept" in EASTMONEY_HEADERS
    assert "Accept-Language" in EASTMONEY_HEADERS
