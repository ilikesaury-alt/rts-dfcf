"""FallbackAdapter 去重回归测试。

锁定 _call_with_none_fallback 与原三处手写降级（fetch_kline / fetch_market_caps_batch
/ fetch_market_index）行为等价：
- kline/index：仅 result is None 触发 secondary（空 list 是合法结果，不降级）；
- caps：空 dict 也触发 secondary（treat_empty_as_failure=True）；
- primary 正常 / 抛异常 / 无 secondary 各路径与原实现一致。
"""

from types import SimpleNamespace

import scanner.data_source as ds


class _Ad:
    def __init__(self, name, kline=None, caps=None, index=None, exc=None):
        self.name = name
        self._kline = kline
        self._caps = caps
        self._index = index
        self._exc = exc

    def fetch_kline(self, symbol, days=15):
        if self._exc:
            raise self._exc()
        return self._kline

    def fetch_market_caps_batch(self, symbols):
        if self._exc:
            raise self._exc()
        return self._caps

    def fetch_market_index(self):
        if self._exc:
            raise self._exc()
        return self._index


def _make(primary, secondary):
    fa = ds.FallbackAdapter(primary, secondary)
    fa._use_primary = True  # 跳过 is_available() 的运行时探测
    return fa


def test_kline_none_falls_to_secondary():
    sec = _Ad("sec", kline=[1, 2, 3])
    fa = _make(_Ad("pri", kline=None), sec)
    assert fa.fetch_kline("300xxx") == [1, 2, 3]


def test_kline_empty_list_is_legal_no_fallback():
    pri = _Ad("pri", kline=[])
    fa = _make(pri, _Ad("sec", kline=[9]))
    assert fa.fetch_kline("300xxx") == []  # 不调 secondary


def test_caps_empty_dict_falls_to_secondary():
    sec = _Ad("sec", caps={"x": {}})
    fa = _make(_Ad("pri", caps={}), sec)
    assert fa.fetch_market_caps_batch(["x"]) == {"x": {}}


def test_index_none_falls_to_secondary():
    sec = _Ad("sec", index=1.5)
    fa = _make(_Ad("pri", index=None), sec)
    assert fa.fetch_market_index() == 1.5


def test_primary_ok_no_fallback():
    fa = _make(_Ad("pri", kline=[7]), _Ad("sec", kline=[8]))
    assert fa.fetch_kline("300xxx") == [7]


def test_primary_exc_falls_to_secondary():
    fa = _make(_Ad("pri", exc=RuntimeError), _Ad("sec", kline=[5]))
    assert fa.fetch_kline("300xxx") == [5]


def test_no_secondary_returns_primary_result():
    fa = _make(_Ad("pri", kline=None), None)
    assert fa.fetch_kline("300xxx") is None
    fa2 = _make(_Ad("pri", caps={}), None)
    assert fa2.fetch_market_caps_batch(["x"]) == {}
