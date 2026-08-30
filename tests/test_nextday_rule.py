"""次日大涨高概率规则测试。

注意（2026-08-30 review 教训）：fixture 的 daily_kline 必须带 `finalized` 列——
get_cached_klines 会 SELECT 该列，缺列时内部吞异常返回空 map，所有 scan_rule
测试会静默走「无 K 线」降级路径变成空转断言（AGENTS.md 2026-08-29 同款事故）。
集成测试必须无条件 assert rule_hit，禁止 `if result.rule_hit > 0:` 守卫。
"""

import sqlite3

import pytest

from scanner.nextday_rule import _compute_features, scan_rule

TODAY = "2026-08-30"


# ── _compute_features 单元测试 ──

def _make_bar(date: str, close: float, high: float = 0.0, low: float = 0.0, **kw):
    """构造一根 kline bar dict。"""
    return {
        "date": date,
        "open": kw.get("open", close),
        "high": high or close * 1.02,
        "low": low or close * 0.98,
        "close": close,
        "volume": kw.get("volume", 1e6),
        "percent": kw.get("percent", 0.0),
    }


def _seq_dates(n: int, start_day: int = 1):
    """生成 n 个递增的 2026-08 日期字符串。"""
    return [f"2026-08-{start_day + i:02d}" for i in range(n)]


def test_compute_features_insufficient_data():
    """数据不足时返回 None。"""
    bars = [_make_bar(d, 10.0) for d in _seq_dates(5)]
    assert _compute_features(bars, 5) is None  # 需要 22+ 根
    assert _compute_features(bars, 3) is None


def test_compute_features_basic():
    """基本特征计算：平稳序列 ma5r=0、ret20=0、atrpct>0。"""
    bars = [_make_bar(d, 10.0) for d in _seq_dates(25)]
    feat = _compute_features(bars, today_idx=24)
    assert feat is not None
    ma5r, atrpct, ret20 = feat
    assert ma5r == pytest.approx(0.0, abs=1e-9)
    assert ret20 == pytest.approx(0.0, abs=1e-9)
    assert atrpct > 0


def test_compute_features_ma5r_positive():
    """ma5r > 0 当近期价格高于均线。

    MA5 窗口 = bars[19..23]（最后 5 根已完成 bar）。
    bars[19]=8.0, bars[20..23]=12.0 → MA5=11.2, close[T-1]=12 → ma5r≈7.14%。
    """
    bars = [_make_bar(d, 10.0) for d in _seq_dates(25)]
    bars[19] = _make_bar(bars[19]["date"], 8.0)
    for i in range(20, 24):
        bars[i] = _make_bar(bars[i]["date"], 12.0)
    feat = _compute_features(bars, today_idx=24)
    assert feat is not None
    ma5r, _, _ = feat
    assert ma5r == pytest.approx((12.0 / 11.2 - 1) * 100, abs=1e-6)
    assert ma5r > 5.0


def test_compute_features_ret20_positive():
    """ret20 = close[T-1]/close[T-21] - 1。"""
    bars = [_make_bar(d, 10.0) for d in _seq_dates(25)]
    for i in range(20, 24):
        bars[i] = _make_bar(bars[i]["date"], 15.0)
    feat = _compute_features(bars, today_idx=24)
    assert feat is not None
    _, _, ret20 = feat
    assert ret20 == pytest.approx(50.0, abs=1e-6)


def test_compute_features_atrpct_scales_with_volatility():
    """波动大 → atrpct 高。"""
    bars_low = [_make_bar(d, 10.0, high=10.1, low=9.9) for d in _seq_dates(25)]
    bars_high = [_make_bar(d, 10.0, high=11.0, low=9.0) for d in _seq_dates(25)]
    feat_low = _compute_features(bars_low, today_idx=24)
    feat_high = _compute_features(bars_high, today_idx=24)
    assert feat_low is not None and feat_high is not None
    assert feat_high[1] > feat_low[1]


def test_compute_features_today_bar_ignored():
    """今日 bar（today_idx）不参与特征计算——已完成 bar 才算。"""
    bars = [_make_bar(d, 10.0) for d in _seq_dates(25)]
    feat_before = _compute_features(bars, today_idx=24)
    bars[24]["close"] = 100.0
    feat_after = _compute_features(bars, today_idx=24)
    assert feat_before == feat_after


# ── scan_rule 集成测试 ──

def _setup_db(bars_map: dict[str, list], today: str, board: list[tuple[str, str]]):
    """构造内存 SQLite（含 get_cached_klines 依赖的 finalized 列）。"""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE appearances (id INTEGER PRIMARY KEY, symbol TEXT, name TEXT, "
        "date TEXT, rank INTEGER, percent REAL, value REAL, UNIQUE(symbol, date))"
    )
    conn.execute(
        "CREATE TABLE daily_kline (symbol TEXT, timestamp INTEGER, date TEXT, "
        "open REAL, close REAL, high REAL, low REAL, volume REAL, percent REAL, "
        "finalized INTEGER, PRIMARY KEY(symbol, date))"
    )
    conn.execute(
        "CREATE TABLE recommendations (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "date TEXT, time TEXT, symbol TEXT, name TEXT, category TEXT, score INTEGER, "
        "percent REAL, trend TEXT, next_day_pct REAL, fwd_3d REAL, fwd_5d REAL, "
        "score_breakdown TEXT, source TEXT)"
    )
    for sym, name in board:
        conn.execute(
            "INSERT INTO appearances (symbol, name, date, rank, percent, value) "
            "VALUES (?, ?, ?, 1, 5.0, 1e8)",
            (sym, name, today),
        )
    for sym, bars in bars_map.items():
        for b in bars:
            conn.execute(
                "INSERT INTO daily_kline (symbol, date, open, close, high, low, "
                "volume, percent, finalized) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)",
                (sym, b["date"], b["open"], b["close"], b["high"], b["low"],
                 b["volume"], b["percent"]),
            )
    conn.commit()
    return conn


def _qualifying_bars(today: str) -> list:
    """构造命中规则的 K 线（与 _compute_features 单测同参数）。

    completed = 前 24 根：bars[19]=8.0, bars[20..23]=12.0，其余 10.0，
    高低 ±10%（atrpct=20%）。
    → ma5r=7.14% ≥ 5、atrpct=20% ≥ 8、ret20=20% ≤ 40，三条件全过。
    """
    closes = [10.0] * 19 + [8.0] + [12.0] * 4
    bars = []
    for i, d in enumerate(_seq_dates(24)):
        c = closes[i]
        bars.append(_make_bar(d, c, high=c * 1.10, low=c * 0.90))
    bars.append(_make_bar(today, 13.0, high=14.0, low=12.0))  # 今日 bar，不参与计算
    return bars


def test_scan_rule_picks_qualifying():
    """满足规则的票被选中（无条件断言，禁止空转）。"""
    bars = _qualifying_bars(TODAY)
    conn = _setup_db({"SZ300001": bars}, TODAY, [("SZ300001", "测试股A")])
    result = scan_rule(conn, TODAY)
    assert result is not None
    assert result.board_size == 1
    assert result.rule_hit == 1, f"规则必须命中 1 只，实际 {result.picks}"
    pick = result.picks[0]
    assert pick.symbol == "SZ300001"
    assert pick.ma5r == pytest.approx((12.0 / 11.2 - 1) * 100, abs=0.01)
    assert pick.atrpct == pytest.approx(20.0, abs=0.01)
    assert pick.ret20 == pytest.approx(20.0, abs=0.01)
    assert pick.already_rec is False


def test_scan_rule_excludes_high_ret20():
    """ret20 > 40% 的票被 ret20 条件排除（先验证确实因 ret20 被排除）。

    bars[23]=15.0 → MA5=11.8, ma5r=27.1% ≥ 5 ✓、atrpct=20% ≥ 8 ✓、
    ret20 = 15/10-1 = 50% > 40 → 仅被 ret20 排除。
    """
    closes = [10.0] * 19 + [8.0] + [12.0] * 3 + [15.0]
    bars = []
    for i, d in enumerate(_seq_dates(24)):
        c = closes[i]
        bars.append(_make_bar(d, c, high=c * 1.10, low=c * 0.90))
    bars.append(_make_bar(TODAY, 15.0, high=16.0, low=14.0))

    # 自证机制：特征层面 ma5r/atrpct 都过线，仅 ret20 超限
    feat = _compute_features(bars, today_idx=24)
    assert feat is not None
    ma5r, atrpct, ret20 = feat
    assert ma5r >= 5.0
    assert atrpct >= 8.0
    assert ret20 > 40.0

    conn = _setup_db({"SZ300002": bars}, TODAY, [("SZ300002", "过热股")])
    result = scan_rule(conn, TODAY)
    assert result is not None
    assert result.rule_hit == 0


def test_scan_rule_already_rec_marked():
    """已在推荐列表中的票标记 already_rec=True（无条件断言）。"""
    bars = _qualifying_bars(TODAY)
    conn = _setup_db({"SZ300001": bars}, TODAY, [("SZ300001", "测试股A")])
    conn.execute(
        "INSERT INTO recommendations (date, time, symbol, name, category, score) "
        "VALUES (?, '10:00', 'SZ300001', '测试股A', 'new_face', 50)",
        (TODAY,),
    )
    conn.commit()
    result = scan_rule(conn, TODAY)
    assert result is not None
    assert result.rule_hit == 1, f"规则必须命中 1 只，实际 {result.picks}"
    assert result.picks[0].already_rec is True


def test_scan_rule_fallback_no_today_bar():
    """今日 bar 缺失时回退到「全部缓存 bar 均已完成」：最后一根是 T-1 锚点。

    25 根已缓存 bar（今日 bar 不在库）：bars[20]=8.0, bars[21..24]=12.0。
    正确回退 today_idx=len(klines)=25 → completed 含 bars[24]，
    MA5=(8+12+12+12+12)/5=11.2 → ma5r≈7.14%。
    （旧 bug 用 len-1=24 → ma5r≈11.11%，用 ma5r 精确值区分两种回退。）
    """
    closes = [10.0] * 20 + [8.0] + [12.0] * 4
    bars = [
        _make_bar(d, closes[i], high=closes[i] * 1.10, low=closes[i] * 0.90)
        for i, d in enumerate(_seq_dates(25))
    ]
    conn = _setup_db({"SZ300004": bars}, TODAY, [("SZ300004", "无今日bar")])
    result = scan_rule(conn, TODAY)
    assert result is not None
    assert result.rule_hit == 1
    assert result.picks[0].ma5r == pytest.approx((12.0 / 11.2 - 1) * 100, abs=0.01)


def test_scan_rule_insufficient_bars():
    """K 线不足时跳过（不命中）。"""
    bars = [_make_bar(d, 10.0) for d in _seq_dates(5)]
    conn = _setup_db({"SZ300003": bars}, TODAY, [("SZ300003", "数据不足")])
    result = scan_rule(conn, TODAY)
    assert result is not None
    assert result.rule_hit == 0


def test_scan_rule_empty_board():
    """今日无上榜 → 返回 None。"""
    conn = _setup_db({}, TODAY, [])
    result = scan_rule(conn, TODAY)
    assert result is None


# ── ScanView 默认值测试 ──

def test_scanview_rule_result_default():
    """ScanView 新增字段有默认值，不传不报错（feishu.py 构造点兼容）。"""
    from scanner.display import ScanView

    sv = ScanView(
        main_rows=[],
        comeback_rows=[],
        core_dip_rows=[],
        nextday_mark={},
        tier_map={},
        breakout_mark={},
        flow_pct_map={},
        last_ranks={},
        adj_picks=None,
        weak=False,
        show_comeback=False,
        show_core_dip=False,
        warnings=[],
    )
    assert sv.rule_result is None
