from scanner.indicators import compute_kdj, compute_macd, compute_rsi


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
