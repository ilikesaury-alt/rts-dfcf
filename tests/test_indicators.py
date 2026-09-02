from scanner.indicators import (
    compute_adx,
    compute_atr,
    compute_bollinger_bands,
    compute_kdj,
    compute_macd,
    compute_obv,
    compute_rsi,
)


def test_rsi_basic():
    closes = list(range(100, 115))  # uptrend
    rsi = compute_rsi(closes)
    assert rsi is not None
    assert rsi > 50  # uptrend should have high RSI


def test_rsi_downtrend():
    closes = list(range(115, 100, -1))  # downtrend
    rsi = compute_rsi(closes)
    assert rsi is not None
    assert rsi < 50  # downtrend should have low RSI


def test_rsi_insufficient_data():
    closes = [100, 101, 102]
    rsi = compute_rsi(closes)
    assert rsi is None


def test_rsi_all_gains():
    closes = [100, 101, 102, 103, 104, 105, 106, 107, 108, 109,
              110, 111, 112, 113, 114]
    rsi = compute_rsi(closes)
    assert rsi == 100.0


def test_rsi_all_losses():
    closes = [114, 113, 112, 111, 110, 109, 108, 107, 106, 105,
              104, 103, 102, 101, 100]
    rsi = compute_rsi(closes)
    assert rsi == 0.0


def test_kdj_basic():
    highs = [110, 112, 111, 113, 115, 114, 116, 118, 117, 119]
    lows = [99, 100, 101, 102, 103, 104, 105, 106, 107, 108]
    closes = [105, 107, 106, 108, 110, 109, 111, 113, 112, 114]
    kdj = compute_kdj(highs, lows, closes)
    assert kdj is not None
    assert 0 <= kdj["K"] <= 100
    assert 0 <= kdj["D"] <= 100


def test_kdj_insufficient_data():
    highs = [110, 112]
    lows = [99, 100]
    closes = [105, 107]
    kdj = compute_kdj(highs, lows, closes)
    assert kdj is None


def test_macd_basic():
    closes = list(range(100, 140))
    macd = compute_macd(closes)
    assert macd is not None
    assert "macd" in macd
    assert "signal" in macd
    assert "histogram" in macd


def test_macd_insufficient_data():
    closes = list(range(30))
    macd = compute_macd(closes)
    assert macd is None


def test_adx_basic():
    highs = [110, 112, 111, 113, 115, 114, 116, 118, 117, 119,
             120, 122, 121, 123, 125, 124, 126, 128, 127, 129,
             130, 132, 131, 133, 135, 134, 136, 138, 137, 139]
    lows = [99, 100, 101, 102, 103, 104, 105, 106, 107, 108,
            109, 110, 111, 112, 113, 114, 115, 116, 117, 118,
            119, 120, 121, 122, 123, 124, 125, 126, 127, 128]
    closes = [105, 107, 106, 108, 110, 109, 111, 113, 112, 114,
              115, 117, 116, 118, 119, 118, 120, 122, 121, 123,
              124, 126, 125, 127, 129, 128, 130, 132, 131, 133]
    adx = compute_adx(highs, lows, closes)
    assert adx is not None
    assert 0 <= adx["adx"] <= 100


def test_adx_insufficient_data():
    assert compute_adx([], [], []) is None


def test_bollinger_basic():
    import math
    closes = [100 + 5 * math.sin(i * 0.3) for i in range(30)]
    boll = compute_bollinger_bands(closes)
    assert boll is not None
    assert boll["upper"] > boll["middle"] > boll["lower"]
    assert 0 <= boll["b_pct"] <= 1


def test_bollinger_insufficient_data():
    assert compute_bollinger_bands([100, 101]) is None


def test_bollinger_oversold():
    closes = [100, 99, 98, 97, 96, 95, 94, 93, 92, 91,
              90, 89, 88, 87, 86, 85, 84, 83, 82, 70]
    boll = compute_bollinger_bands(closes)
    assert boll is not None
    assert boll["b_pct"] < 0


def test_atr_basic():
    lows = [100, 101, 102, 103, 104, 105, 106, 107, 108, 109,
            110, 111, 112, 113, 114, 115, 116, 117, 118, 119]
    highs = [110, 111, 112, 113, 114, 115, 116, 117, 118, 119,
             120, 121, 122, 123, 124, 125, 126, 127, 128, 129]
    closes = [105, 106, 107, 108, 109, 110, 111, 112, 113, 114,
              115, 116, 117, 118, 119, 120, 121, 122, 123, 124]
    atr = compute_atr(highs, lows, closes)
    assert atr is not None
    assert atr > 0


def test_atr_insufficient_data():
    assert compute_atr([100], [100], [100]) is None


def test_obv_basic():
    closes = [100, 102, 101, 103, 105, 104, 106, 108]
    volumes = [1000, 1500, 1200, 1800, 2000, 1600, 2200, 2500]
    obv = compute_obv(closes, volumes)
    assert obv is not None
    assert "obv" in obv
    assert "obv_trend" in obv
    assert obv["obv_trend"] in (-1, 0, 1)


def test_obv_insufficient_data():
    assert compute_obv([100], [1000]) is None
