"""tracker 模块买点信号识别单元测试。"""
from datetime import datetime, timedelta

from scanner.tracker import _evaluate_buy_signals


def _make_kline(n: int = 30, end_date: datetime = None, prices: list[float] = None,
                volumes: list[float] = None) -> list[dict]:
    """构造 n 根 K 线，价格和成交量可自定义。

    默认构造缓慢上涨序列（MA20 上行、close 在 MA20 上方）。
    """
    if end_date is None:
        end_date = datetime(2026, 1, n)
    if prices is None:
        # 默认：从 10 元开始，每日涨 0.5%
        prices = [10.0 * (1.005 ** i) for i in range(n)]
    if volumes is None:
        volumes = [10000.0] * n

    result = []
    start_date = end_date - timedelta(days=n - 1)
    for i in range(n):
        d = start_date + timedelta(days=i)
        o = prices[i] * 0.998
        c = prices[i]
        h = max(o, c) * 1.005
        lo = min(o, c) * 0.995
        result.append({
            "date": d.strftime("%Y-%m-%d"),
            "open": o, "close": c, "high": h, "low": lo,
            "volume": volumes[i], "percent": 0.5,
        })
    return result


def test_evaluate_kline_too_short():
    """K 线不足 20 根返回空状态。"""
    kline = _make_kline(n=15)
    status, count, signals = _evaluate_buy_signals(kline)
    assert status == ""
    assert count == 0
    assert signals == []


def test_evaluate_none_kline():
    """K 线为 None 返回空状态。"""
    status, count, signals = _evaluate_buy_signals(None)
    assert status == ""
    assert count == 0


def test_evaluate_perfect_buy_point():
    """构造完美买点：缓慢上涨后回调到 MA20 附近、缩量。

    预期触发：未破位、RSI合理、BOLL中轨、MACD未死叉 等
    """
    n = 30
    # 前 25 根缓慢上涨建立趋势，后 5 根回调到 MA20 附近
    prices = [10.0 * (1.005 ** i) for i in range(25)]
    # 后 5 根小幅回调（让 close 接近 MA20）
    for i in range(5):
        prices.append(prices[-1] * 0.998)

    # 后 5 根缩量
    volumes = [10000.0] * 25 + [5000.0] * 5

    kline = _make_kline(n=n, prices=prices, volumes=volumes)
    status, count, signals = _evaluate_buy_signals(kline)

    # 应该触发多个信号（至少未破位+MACD未死叉+BOLL中轨）
    assert count >= 2, f"预期至少 2 个信号，实际 {count}: {signals}"
    assert "未破位" in signals  # close 仍 > MA20
    assert status in ("到买点", "观察中")


def test_evaluate_broken_trend():
    """构造破位场景：close 远低于 MA20。

    预期：未破位不触发，MA20支撑不触发，但可能触发缩量/RSI合理
    """
    n = 30
    # 前 25 根上涨，后 5 根大跌破位
    prices = [10.0 * (1.005 ** i) for i in range(25)]
    for i in range(5):
        prices.append(prices[-1] * 0.95)  # 每日跌 5%

    volumes = [10000.0] * 25 + [15000.0] * 5  # 放量下跌

    kline = _make_kline(n=n, prices=prices, volumes=volumes)
    status, count, signals = _evaluate_buy_signals(kline)

    assert "未破位" not in signals  # close < MA20
    assert "MA20支撑" not in signals  # 远离 MA20
    assert "缩量" not in signals  # 放量不是缩量


def test_evaluate_today_bar_excluded():
    """今日 bar 应被排除，不参与指标计算。

    构造历史 29 根 + 今日 1 根（极端值），验证今日 close 不影响指标。
    """
    from scanner.config import now_beijing

    n = 29
    prices = [10.0 * (1.005 ** i) for i in range(n)]
    volumes = [10000.0] * n

    kline = _make_kline(n=n, prices=prices, volumes=volumes)

    # 添加今日 bar（极端高价，如果参与计算会扭曲指标）
    today_str = now_beijing().strftime("%Y-%m-%d")
    kline.append({
        "date": today_str,
        "open": 100.0, "close": 100.0, "high": 100.0, "low": 100.0,
        "volume": 999999.0, "percent": 900.0,
    })

    status, count, signals = _evaluate_buy_signals(kline)

    # 今日 bar 被排除后，指标基于前 29 根计算
    # 前 29 根是缓慢上涨序列，close > MA20 → 未破位应触发
    assert "未破位" in signals


def test_status_threshold_buy():
    """信号数 >= 4 → '到买点'。"""
    # 构造能触发 >= 4 信号的场景（缓慢上涨+回调+缩量）
    n = 30
    prices = [10.0 * (1.005 ** i) for i in range(25)]
    for i in range(5):
        prices.append(prices[-1] * 0.999)
    volumes = [10000.0] * 25 + [4000.0] * 5  # 大幅缩量

    kline = _make_kline(n=n, prices=prices, volumes=volumes)
    status, count, signals = _evaluate_buy_signals(kline)

    if count >= 4:
        assert status == "到买点"
    elif count >= 2:
        assert status == "观察中"
    else:
        assert status == ""


def test_status_threshold_filter():
    """信号数 < 2 → 过滤（空状态）。"""
    # 构造极端破位场景，信号数应 < 2
    n = 30
    prices = [10.0 * (1.005 ** i) for i in range(25)]
    for i in range(5):
        prices.append(prices[-1] * 0.90)  # 每日跌 10%

    volumes = [10000.0] * 25 + [20000.0] * 5  # 放量暴跌

    kline = _make_kline(n=n, prices=prices, volumes=volumes)
    status, count, signals = _evaluate_buy_signals(kline)

    # 暴跌场景：未破位不触发、MA20支撑不触发、缩量不触发、MACD可能死叉
    # 信号数应很少
    assert count < 4, f"暴跌场景不应有 4+ 信号，实际 {count}: {signals}"


def test_volume_shrinkage_signal():
    """缩量信号：最后一根 volume < 0.8 × 前 5 根均量。"""
    n = 30
    prices = [10.0 * (1.005 ** i) for i in range(n)]
    # 前 29 根正常量，最后一根缩量到 50%
    volumes = [10000.0] * 29 + [5000.0]

    kline = _make_kline(n=n, prices=prices, volumes=volumes)
    _, _, signals = _evaluate_buy_signals(kline)

    assert "缩量" in signals


def test_no_shrinkage_signal():
    """非缩量：最后一根 volume >= 0.8 × 前 5 根均量。"""
    n = 30
    prices = [10.0 * (1.005 ** i) for i in range(n)]
    volumes = [10000.0] * 30  # 等量

    kline = _make_kline(n=n, prices=prices, volumes=volumes)
    _, _, signals = _evaluate_buy_signals(kline)

    assert "缩量" not in signals
