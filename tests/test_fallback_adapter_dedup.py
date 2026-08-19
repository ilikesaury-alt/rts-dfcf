"""FallbackAdapter 统一兜底回归测试。

锁定 _call 的"返回空也降级"语义（覆盖原 _call_with_none_fallback，已删除）：
- kline/index：仅 None 触发 secondary（空 list 是合法结果，不降级；0.0 平盘合法不降级）；
- caps：空 dict {} 也触发 secondary；
- primary 正常 / 抛异常 / 无 secondary 各路径与预期一致。

不依赖 pandas，作为 _call 契约的独立回归锁（test_data_source 的 TestFallbackAdapter
也覆盖同等语义，但它在受管 Python 下因 pandas import 被跳过）。
"""

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


def test_index_zero_is_legal_no_fallback():
    # 大盘平盘 0.0 是合法值，绝不能当失败去兜底
    fa = _make(_Ad("pri", index=0.0), _Ad("sec", index=-6.26))
    assert fa.fetch_market_index() == 0.0  # 不调 secondary


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
