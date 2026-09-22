"""hot_watch（沪深飙升·极有可能大涨独立区）单测。

覆盖：样本面、涨跌停价推算、硬性排除各分支、打分与排序、预筛、连击跟踪、
以及终端独立区渲染。全部为密封单测（不触网、不依赖真实 scanner.db）。
"""

import sqlite3

import pytest

from scanner.config import (
    HOT_HIGHLIGHT_STREAK,
    HOT_MAX_MARKET_CAP,
    HOT_MAX_PERCENT,
    HOT_RANK_CHANGE_CAP,
)
from scanner.hot_watch import (
    HotCandidate,
    _next_round,
    build_candidates,
    compute_score,
    hard_exclude,
    is_hot_universe,
    limit_pct_for,
    limit_prices,
    prefilter_board,
    run_hot_watch,
    score_percent,
    score_price,
    score_rank_change,
    score_volume,
    update_streaks,
)

# ── 夹具 ────────────────────────────────────────────────────────────────────


def _cand(**kw) -> HotCandidate:
    """构造一个默认「各项健康」的候选（创业板、小盘、放量、温和上涨）。"""
    base = {
        "symbol": "SZ300862",
        "code": "300862",
        "name": "蓝盾光电",
        "exchange": "SZ",
        "current": 50.10,
        "percent": 5.76,
        "rank_change": 1257,
        "rank": 3,
        "volume": 27_532_000,
        "amount": 1.3569e9,
        "market_capital": 9.249e9,
        "turnover_rate": 18.18,
        "volume_ratio": 1.37,
        "limit_up": 60.12,
        "limit_down": 40.08,
        "status": 1,
    }
    base.update(kw)
    return HotCandidate(**base)


def _board_item(symbol="SZ300862", name="蓝盾光电", percent=5.76, current=50.10, rc=1257, rank=3, exch="SZ"):
    return {
        "symbol": symbol,
        "name": name,
        "percent": percent,
        "current": current,
        "rank_change": rc,
        "rank": rank,
        "exchange": exch,
    }


def _quote(symbol="SZ300862", code="300862", name="蓝盾光电", exch="SZ", **kw):
    q = {
        "symbol": symbol,
        "code": code,
        "name": name,
        "exchange": exch,
        "status": 1,
        "current": 50.10,
        "percent": 5.76,
        "chg": 2.73,
        "volume": 27_532_000,
        "amount": 1.3569e9,
        "turnover_rate": 18.18,
        "market_capital": 9.249e9,
        "float_market_capital": 8.5e9,
        "last_close": 47.37,
        "high": 51.50,
        "low": 47.00,
    }
    q.update(kw)
    return q


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE hot_watch_hits (
            symbol TEXT PRIMARY KEY, name TEXT NOT NULL,
            streak INTEGER NOT NULL DEFAULT 0, last_round INTEGER NOT NULL DEFAULT 0,
            last_seen TEXT, last_percent REAL, last_price REAL, last_score REAL)
    """)
    conn.execute("CREATE TABLE hot_watch_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.commit()
    yield conn
    conn.close()


# ── 样本面 ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "exch,code,want",
    [
        ("SH", "600519", False),  # 沪主板（仅创业板）
        ("SH", "601086", False),
        ("SH", "603678", False),  # 沪主板（仅创业板）
        ("SH", "605006", False),
        ("SZ", "000001", False),  # 深主板
        ("SZ", "002443", False),
        ("SZ", "003002", False),
        ("SZ", "300862", True),  # 创业板
        ("SZ", "301176", True),
        ("SH", "688260", False),  # 科创板
        ("SZ", "159516", False),  # ETF
        ("SZ", "150001", False),  # 基金
        ("SZ", "123456", False),  # 可转债
        ("BJ", "830001", False),  # 北交所
        ("HK", "01810", False),  # 港股
        ("SZ", "00001", False),  # 位数不足
        ("SZ", "ABCDEF", False),  # 非数字
    ],
)
def test_hot_universe_whitelist(exch, code, want):
    assert is_hot_universe(exch, code) is want


def test_hot_universe_accepts_prefixed_symbol():
    """代码带 SH/SZ 前缀时同样可判定（榜单字段与补全字段混用场景）。"""
    assert is_hot_universe("SZ", "SZ300862") is True
    assert is_hot_universe("SH", "SH688260") is False


# ── 涨跌停价推算 ────────────────────────────────────────────────────────────


def test_limit_pct_by_board():
    assert limit_pct_for("300862") == 20.0  # 创业板
    assert limit_pct_for("301176") == 20.0
    assert limit_pct_for("600519") == 10.0  # 主板
    assert limit_pct_for("002443") == 10.0


def test_limit_prices_main_board():
    up, down = limit_prices(10.00, "600519")
    assert up == 11.0
    assert down == 9.0


def test_limit_prices_gem_20pct():
    up, down = limit_prices(10.00, "300862")
    assert up == 12.0
    assert down == 8.0


def test_limit_prices_rounds_to_cent():
    """A 股涨跌停价四舍五入到分（10.03 → 11.03 / 9.03）。"""
    up, down = limit_prices(10.03, "600519")
    assert up == 11.03
    assert down == 9.03


def test_limit_prices_dirty_last_close_is_failopen():
    """last_close 缺失/为 0 → 返回 (0,0)，调用方按「无法判定」不排除（不误杀）。"""
    assert limit_prices(0.0, "600519") == (0.0, 0.0)
    assert limit_prices(-1.0, "600519") == (0.0, 0.0)


def test_limit_prices_nan_is_failopen():
    assert limit_prices(float("nan"), "600519") == (0.0, 0.0)


# ── 硬性排除 ────────────────────────────────────────────────────────────────


def test_pass_healthy_candidate():
    assert hard_exclude(_cand()) is None


def test_exclude_st():
    assert "ST" in hard_exclude(_cand(name="*ST宝馨"))
    assert "ST" in hard_exclude(_cand(name="ST豆神"))


def test_exclude_delisting():
    """退市整理期三种命名全部拦截（走 utils.is_st —— 项目单一事实来源）。

    「XX退」是最大一类（2026-09-11 修复前会漏网，实测 300029 天龙退曾以 rank 11 上榜）。
    """
    assert "ST" in hard_exclude(_cand(name="某某退"))  # 退市整理期：以退结尾
    assert "ST" in hard_exclude(_cand(name="退某某"))  # 以退开头
    assert "ST" in hard_exclude(_cand(name="某某退市"))  # 含退市
    assert "ST" in hard_exclude(_cand(name="天龙退"))  # 真实样本


def test_exclude_not_in_universe():
    assert "非创业板" in hard_exclude(_cand(exchange="SH", code="688260"))


def test_exclude_abnormal_status():
    """status != 1：停牌 / 未开盘 / 已收盘。"""
    assert "非正常交易状态" in hard_exclude(_cand(status=0))


def test_exclude_no_valid_price():
    assert "无有效报价" in hard_exclude(_cand(current=0.0))


def test_exclude_zero_volume():
    """无成交（停牌或未成交）。"""
    assert "无成交量" in hard_exclude(_cand(volume=0))


def test_exclude_limit_down():
    """现价 ≤ 跌停价 × 容差 → 已触及跌停。"""
    assert "跌停" in hard_exclude(_cand(current=9.0, limit_down=9.0, percent=-9.9))


def test_exclude_not_rising():
    assert "非上涨" in hard_exclude(_cand(percent=0.0))
    assert "非上涨" in hard_exclude(_cand(percent=-3.2))


def test_exclude_sealed_limit_up():
    """现价 ≥ 涨停价 × 0.985 → 已封涨停，追高性价比低。"""
    assert "已封涨停" in hard_exclude(_cand(current=59.5, limit_up=60.12, percent=3.0))


def test_near_limit_up_but_not_sealed_passes():
    """现价 vs 涨停价的 0.985 边界：差一点没封板不排除，够到即排除。

    用 percent 在涨幅上限内(5.0%)的样本，确保命中的是「封板」而非「涨幅过高」。
    """
    not_sealed = _cand(current=59.0, limit_up=60.12, percent=5.0)  # 59.0 < 60.12*0.985=59.22
    assert hard_exclude(not_sealed) is None
    sealed = _cand(current=59.5, limit_up=60.12, percent=5.0)  # 59.5 ≥ 59.22
    assert "已封涨停" in hard_exclude(sealed)


def test_exclude_percent_over_cap():
    assert "涨幅过高" in hard_exclude(_cand(percent=HOT_MAX_PERCENT + 0.5))


def test_percent_at_cap_passes():
    """涨幅恰好等于上限 → 不排除（闭区间）。"""
    assert hard_exclude(_cand(percent=HOT_MAX_PERCENT)) is None


def test_exclude_market_cap_over_cap():
    assert "市值过大" in hard_exclude(_cand(market_capital=HOT_MAX_MARKET_CAP * 2))


def test_market_cap_missing_passes():
    """市值缺失（0）时不因市值排除 —— fail-open，宁可放过。"""
    assert hard_exclude(_cand(market_capital=0.0)) is None


def test_exclude_order_st_before_universe():
    """ST 判定优先于样本面：ST 的科创板票报 ST 而非「非沪深」。"""
    assert "ST" in hard_exclude(_cand(name="ST某某", exchange="SH", code="688260"))


# ── 打分 ────────────────────────────────────────────────────────────────────


def test_score_rank_change_log_normalized():
    assert score_rank_change(_cand(rank_change=0)) == 0.0
    assert score_rank_change(_cand(rank_change=-100)) == 0.0  # 负值为 0
    assert score_rank_change(_cand(rank_change=HOT_RANK_CHANGE_CAP)) == pytest.approx(1.0)
    # 对数归一：压缩极值 —— 100 的分数 > 0，但远小于线性比例的 1/90
    s100 = score_rank_change(_cand(rank_change=100))
    assert 0 < s100 < 0.6


def test_score_rank_change_monotonic():
    a = score_rank_change(_cand(rank_change=100))
    b = score_rank_change(_cand(rank_change=1000))
    assert a < b


def test_score_percent_bounds():
    assert score_percent(_cand(percent=0)) == 0.0
    assert score_percent(_cand(percent=-5)) == 0.0
    assert score_percent(_cand(percent=HOT_MAX_PERCENT)) == pytest.approx(1.0)
    # 硬排除已剔除 > 上限者；此处仅验证不会溢出 1.0
    assert score_percent(_cand(percent=50.0)) == pytest.approx(1.0)


def test_score_price_ideal_band_full():
    assert score_price(_cand(current=3.0)) == pytest.approx(1.0)
    assert score_price(_cand(current=20.0)) == pytest.approx(1.0)
    assert score_price(_cand(current=40.0)) == pytest.approx(1.0)


def test_score_price_decays_above_band():
    assert score_price(_cand(current=41.0)) < 1.0
    assert score_price(_cand(current=300.0)) == pytest.approx(0.0)
    assert score_price(_cand(current=1000.0)) == pytest.approx(0.0)


def test_score_price_below_band_floored():
    """仙股略降但有 0.35 下限，不归零。"""
    assert score_price(_cand(current=1.0)) == pytest.approx(max(0.35, 1.0 / 3.0))
    assert score_price(_cand(current=0.5)) == pytest.approx(0.35)


def test_score_price_invalid():
    assert score_price(_cand(current=0.0)) == 0.0


def test_score_volume_both_factors():
    c = _cand(volume_ratio=3.0, turnover_rate=10.0)
    assert score_volume(c) == pytest.approx(1.0)


def test_score_volume_turnover_only_fallback():
    """量比缺失（批量补全常态）→ 以换手率为主并打折。"""
    c = _cand(volume_ratio=0.0, turnover_rate=10.0)
    assert score_volume(c) == pytest.approx(0.85)


def test_score_volume_ratio_only_fallback():
    c = _cand(volume_ratio=3.0, turnover_rate=0.0)
    assert score_volume(c) == pytest.approx(0.85)


def test_score_volume_no_data_neutral_low():
    c = _cand(volume_ratio=0.0, turnover_rate=0.0)
    assert score_volume(c) == pytest.approx(0.15)


def test_score_volume_clamped_at_one():
    """量比/换手远超满分线时封顶 1.0，不溢出。"""
    assert score_volume(_cand(volume_ratio=99.0, turnover_rate=99.0)) == pytest.approx(1.0)


def test_compute_score_within_100():
    c = _cand(rank_change=99999, percent=HOT_MAX_PERCENT, current=20.0, volume_ratio=9, turnover_rate=99)
    assert 0 < compute_score(c) <= 100.0


def test_compute_score_floor_is_volume_neutral_only():
    """全无信号时只剩量能的「缺失中性低分」0.15×25=3.75（避免整票归零而丧失可比性）。"""
    c = _cand(rank_change=0, percent=0, current=0, volume_ratio=0, turnover_rate=0)
    assert compute_score(c) == pytest.approx(3.75)


# ── 预筛 ────────────────────────────────────────────────────────────────────


def test_prefilter_drops_st_and_foreign_and_nonrising():
    board = [
        _board_item("SZ300862", "蓝盾光电", percent=5.7),
        _board_item("SZ002514", "*ST宝馨", percent=9.9),
        _board_item("SH688260", "昀冢科技", percent=15.1, exch="SH"),
        _board_item("SH600519", "贵州茅台", percent=-0.63, exch="SH"),
        _board_item("SZ159516", "半导体ETF", percent=2.0),
    ]
    out = [b["symbol"] for b in prefilter_board(board)]
    assert out == ["SZ300862"]


def test_prefilter_sorted_by_rank_change_desc():
    board = [
        _board_item("SZ300001", "A", rc=10),
        _board_item("SZ300002", "B", rc=900),
        _board_item("SZ300003", "C", rc=200),
    ]
    out = [b["symbol"] for b in prefilter_board(board)]
    assert out == ["SZ300002", "SZ300003", "SZ300001"]


def test_prefilter_keeps_high_percent_for_later_exclusion():
    """涨幅不在此预筛过滤，留给 hard_exclude 统一判定。"""
    board = [_board_item("SZ300001", "A", percent=15.0)]
    assert len(prefilter_board(board)) == 1


# ── 候选构建 ────────────────────────────────────────────────────────────────


def test_build_candidates_merges_quote_and_excludes():
    board = [
        _board_item("SZ300862", "蓝盾光电", rc=1257),
        _board_item("SZ301176", "逸豪新材", rc=4432),
    ]
    quotes = {
        "SZ300862": _quote(),
        "SZ301176": _quote(
            "SZ301176",
            "301176",
            "逸豪新材",
            "SZ",
            current=61.06,
            percent=7.50,
            last_close=56.80,
            market_capital=1.03e10,
            volume=2.0e6,
        ),
    }
    passed, rejected = build_candidates(board, quotes)
    assert [c.code for c in passed] == ["300862"]
    assert [c.code for c in rejected] == ["301176"]


def test_build_candidates_sorted_by_score_desc():
    board = [_board_item("SZ300001", "A", rc=10), _board_item("SZ300002", "B", rc=5000)]
    quotes = {
        "SZ300001": _quote("SZ300001", "300001", "A", current=20.0, percent=3.0, last_close=19.0),
        "SZ300002": _quote("SZ300002", "300002", "B", current=20.0, percent=6.0, last_close=19.0),
    }
    passed, _ = build_candidates(board, quotes)
    assert passed[0].code == "300002"  # 排名上升更猛
    assert all(passed[i].score >= passed[i + 1].score for i in range(len(passed) - 1))


def test_build_candidates_skips_missing_quote():
    """补全失败的票直接跳过（不因缺字段误判为通过）。"""
    board = [_board_item("SZ300862", "蓝盾光电")]
    passed, rejected = build_candidates(board, {})
    assert passed == [] and rejected == []


def test_build_candidates_derives_limit_prices_from_last_close():
    """batch 接口无 limit_up/down，由 last_close 推算，硬排除不依赖 detail 补拉。"""
    board = [_board_item("SZ300862", "蓝盾光电", percent=5.0, current=59.5)]
    quotes = {"SZ300862": _quote(current=59.5, percent=5.0, last_close=56.80)}
    passed, rejected = build_candidates(board, quotes)
    assert not rejected and len(passed) == 1


def test_build_candidates_tolerates_dirty_quote_fields():
    """补全字段是字符串/None/NaN → 按 0 处理，不抛异常、不崩整轮。"""
    board = [_board_item("SZ002443", "金洲管道")]
    quotes = {
        "SZ002443": _quote(
            current="11.81",
            percent="5.73",
            amount=float("nan"),
            turnover_rate=float("nan"),
            last_close=None,
            market_capital=None,
        )
    }
    passed, _ = build_candidates(board, quotes)
    assert len(passed) == 1
    assert passed[0].current == pytest.approx(11.81)  # 字符串 → float
    assert passed[0].percent == pytest.approx(5.73)
    assert passed[0].turnover_rate == 0.0  # NaN → 0
    assert passed[0].amount == 0.0
    assert passed[0].limit_up == 0.0  # last_close 缺失 → 无法推算，fail-open 不排除


def test_dirty_volume_fails_safe_as_no_volume():
    """成交量为 NaN/缺失 → 归零 → 按「无成交量」排除（不静默放行可疑票）。"""
    board = [_board_item("SZ002443", "金洲管道")]
    quotes = {"SZ002443": _quote(volume=float("nan"))}
    passed, rejected = build_candidates(board, quotes)
    assert passed == []
    assert "无成交量" in hard_exclude(rejected[0])


# ── 连击跟踪 ────────────────────────────────────────────────────────────────


def test_next_round_increments(db):
    assert _next_round(db) == 1
    assert _next_round(db) == 2
    assert _next_round(db) == 3


def test_streak_increments_on_consecutive_rounds(db):
    c = _cand()
    update_streaks(db, [c], 1)
    assert c.streak == 1
    update_streaks(db, [c], 2)
    assert c.streak == 2
    update_streaks(db, [c], 3)
    assert c.streak == 3
    assert c.streak >= HOT_HIGHLIGHT_STREAK


def test_streak_resets_when_gap(db):
    c = _cand()
    update_streaks(db, [c], 1)
    update_streaks(db, [c], 5)  # 跳轮 → 重新计数
    assert c.streak == 1


def test_streak_zeroed_when_missing_this_round(db):
    a = _cand(symbol="SZ000001", code="000001")
    b = _cand(symbol="SZ000002", code="000002")
    update_streaks(db, [a, b], 1)
    update_streaks(db, [a], 2)  # b 本轮掉出
    row = db.execute("SELECT streak FROM hot_watch_hits WHERE symbol='SZ000002'").fetchone()
    assert row[0] == 0


def test_streak_persisted_to_db(db):
    c = _cand()
    c.score = 77.6
    update_streaks(db, [c], 1)
    db.commit()
    row = db.execute(
        "SELECT name, streak, last_round, last_percent FROM hot_watch_hits WHERE symbol='SZ300862'"
    ).fetchone()
    assert row[0] == "蓝盾光电"
    assert row[1] == 1
    assert row[2] == 1
    assert row[3] == pytest.approx(5.76)


def test_update_streaks_empty_clears_all(db):
    """全员被排除的一轮也要推进：否则连击链不会被打断（空榜被误读为持续重点）。"""
    c = _cand()
    update_streaks(db, [c], 1)
    update_streaks(db, [], 2)
    row = db.execute("SELECT streak FROM hot_watch_hits WHERE symbol='SZ300862'").fetchone()
    assert row[0] == 0


# ── run_hot_watch 端到端（假 adapter，不触网）────────────────────────────────


class _FakeAdapter:
    """假数据源：预置批量补全返回，记录 detail 调用。"""

    def __init__(self, quotes=None, detail=None):
        self._quotes = quotes or {}
        self._detail = detail or {}
        self.detail_calls: list[str] = []
        self.batch_calls = 0

    def fetch_hot_quotes_batch(self, symbols):
        self.batch_calls += 1
        return {s: self._quotes[s] for s in symbols if s in self._quotes}

    def fetch_hot_quote_detail(self, symbol):
        self.detail_calls.append(symbol)
        return self._detail.get(symbol, {})


def test_run_hot_watch_fills_sector_without_network(db):
    """A 段**产出阶段**就填好 `sector`，且**绝不发 F10**（fetch=False）。

    榜内票的概念归属由主线的 `compute_driving_concepts` 维护（覆盖面是「主线候选 ∪
    榜内票」），本区不重复拉。断言 `_fetch_many` 未被调用是**非空断言**：一旦哪天
    误传 fetch=True，`_collect_concepts` 必经 `_fetch_many`，本用例立刻变红。
    """
    from unittest.mock import patch

    board = [_board_item("SZ300001", "半导体设备", rc=100)]
    quotes = {"SZ300001": _quote("SZ300001", "300001", "半导体设备", current=20.0, percent=3.0, last_close=19.0)}
    with patch("scanner.concept._fetch_many") as mock_fetch:
        out = run_hot_watch(_FakeAdapter(quotes), db, board, top_n=1)
    mock_fetch.assert_not_called()
    assert out, "样本应产出"
    assert out[0].sector == "半导体"  # ③名称关键词兜底（本库无 concept_cache）


def test_run_hot_watch_returns_top_n_sorted(db):
    board = [_board_item("SZ300001", "A", rc=100), _board_item("SZ300002", "B", rc=5000)]
    quotes = {
        "SZ300001": _quote("SZ300001", "300001", "A", current=20.0, percent=3.0, last_close=19.0),
        "SZ300002": _quote("SZ300002", "300002", "B", current=20.0, percent=6.0, last_close=19.0),
    }
    out = run_hot_watch(_FakeAdapter(quotes), db, board, top_n=1)
    assert len(out) == 1
    assert out[0].code == "300002"


def test_run_hot_watch_empty_board_returns_empty(db):
    assert run_hot_watch(_FakeAdapter(), db, []) == []


def test_run_hot_watch_no_quotes_returns_empty(db):
    """补全全失败 → 空列表（本区留空，不告警噪音、不抛异常）。"""
    board = [_board_item("SZ000001", "A")]
    assert run_hot_watch(_FakeAdapter({}), db, board) == []


def test_run_hot_watch_adapter_without_method_returns_empty(db):
    """非雪球源（THS）无本区方法 → 干净跳过，不抛 AttributeError。"""

    class _Bare:
        pass

    assert run_hot_watch(_Bare(), db, [_board_item("SZ000001", "A")]) == []


def test_run_hot_watch_detail_bounded(db):
    """detail 单票补拉受 HOT_DETAIL_TOP 约束（1 请求/票，保护刷新节拍）。"""
    from scanner.config import HOT_DETAIL_TOP

    board = [_board_item(f"SZ30000{i}", f"N{i}", rc=5000 - i) for i in range(1, 9)]
    quotes = {
        f"SZ30000{i}": _quote(f"SZ30000{i}", f"30000{i}", f"N{i}", current=20.0, percent=5.0, last_close=19.0)
        for i in range(1, 9)
    }
    detail = {f"SZ30000{i}": {"volume_ratio": 2.5, "limit_up": 22.0, "limit_down": 18.0} for i in range(1, 9)}
    adp = _FakeAdapter(quotes, detail)
    out = run_hot_watch(adp, db, board, top_n=2)
    assert len(out) == 2
    # 只补前 HOT_DETAIL_TOP 名，而非全部 8 只通过者
    assert len(adp.detail_calls) == HOT_DETAIL_TOP
    assert out[0].volume_ratio == pytest.approx(2.5)
    # 未被 detail 覆盖的通过者量比留 0（渲染为 —），不伪造
    assert adp.detail_calls == ["SZ300001", "SZ300002", "SZ300003", "SZ300004", "SZ300005"][:HOT_DETAIL_TOP]


def test_run_hot_watch_detail_failure_is_failopen(db):
    """detail 补拉抛外部异常 → 量比留 0，排除与排序不受影响。"""
    board = [_board_item("SZ300001", "A", rc=5000)]
    quotes = {"SZ300001": _quote("SZ300001", "300001", "A", current=20.0, percent=5.0, last_close=19.0)}

    class _Boom(_FakeAdapter):
        def fetch_hot_quote_detail(self, symbol):
            raise OSError("network down")

    out = run_hot_watch(_Boom(quotes), db, board)
    assert len(out) == 1
    assert out[0].volume_ratio == 0.0  # 显示 —，不伪造


def test_run_hot_watch_persists_streak(db):
    board = [_board_item("SZ300001", "A", rc=5000)]
    quotes = {"SZ300001": _quote("SZ300001", "300001", "A", current=20.0, percent=5.0, last_close=19.0)}
    run_hot_watch(_FakeAdapter(quotes), db, board)
    run_hot_watch(_FakeAdapter(quotes), db, board)  # 第二轮连续命中
    row = db.execute("SELECT streak FROM hot_watch_hits WHERE symbol='SZ300001'").fetchone()
    assert row[0] == 2


def test_run_hot_watch_counts_all_passed_not_only_top(db):
    """连击以「全部通过者」为基数：跌出前 N 但仍在结果中的票不应被清零。"""
    board = [
        _board_item("SZ300001", "A", rc=5000),
        _board_item("SZ300002", "B", rc=4000),
        _board_item("SZ300003", "C", rc=3000),
    ]
    quotes = {
        f"SZ30000{i}": _quote(f"SZ30000{i}", f"30000{i}", n, current=20.0, percent=5.0, last_close=19.0)
        for i, n in zip((1, 2, 3), "ABC", strict=True)
    }
    run_hot_watch(_FakeAdapter(quotes), db, board, top_n=1)
    rows = dict(db.execute("SELECT symbol, streak FROM hot_watch_hits").fetchall())
    # TOP1 之外的 B/C 同为「本轮命中」，连击应为 1 而非 0
    assert rows["SZ300002"] == 1
    assert rows["SZ300003"] == 1


def test_run_hot_watch_respects_enrich_limit(db, monkeypatch):
    """补全请求量受 HOT_ENRICH_LIMIT 约束（保护主循环刷新节拍）。"""
    import scanner.hot_watch as hw

    monkeypatch.setattr(hw, "HOT_ENRICH_LIMIT", 2)
    board = [_board_item(f"SZ30000{i}", f"N{i}", rc=1000 - i) for i in range(1, 6)]
    quotes = {
        f"SZ30000{i}": _quote(f"SZ30000{i}", f"30000{i}", f"N{i}", current=20.0, percent=5.0, last_close=19.0)
        for i in range(1, 6)
    }
    adp = _FakeAdapter(quotes)
    run_hot_watch(adp, db, board)
    assert adp.batch_calls == 1
    # 只补 2 只 → 结果不超过 2 条
    assert db.execute("SELECT COUNT(*) FROM hot_watch_hits").fetchone()[0] <= 2


# ── 终端独立区渲染 ──────────────────────────────────────────────────────────


def _make_view(hot_rows):
    """构造仅含 hot_rows 的 ScanView（其余字段用最小占位）。"""
    from scanner.display import ScanView

    return ScanView(
        main_rows=[],
        breakout_mark={},
        flow_pct_map={},
        last_ranks={},
        weak=False,
        warnings=[],
        hot_rows=hot_rows,
    )


def test_render_hot_region_prints_table(capsys):
    from scanner.display import render_terminal

    c = _cand(streak=5, volume_ratio=1.37)
    c.score = 77.6
    render_terminal(_make_view([c]))
    out = capsys.readouterr().out
    assert "沪深飙升" in out
    assert "300862" in out
    assert "蓝盾光电" in out
    assert "★" in out  # streak >= 阈值
    assert "排名上升" in out


def test_render_hot_region_skipped_when_none(capsys):
    from scanner.display import render_terminal

    render_terminal(_make_view(None))
    out = capsys.readouterr().out
    # 哨兵用**区块标题**而不是「沪深飙升」四个字：v1 池选的通用风险门脚注里
    # 也提到了「沪深飙升」（说明三区同源），拿区名当哨兵会误报。
    assert "◆ 沪深飙升" not in out


def test_render_hot_region_skipped_when_empty(capsys):
    """空结果不留空表（整区跳过）。"""
    from scanner.display import render_terminal

    render_terminal(_make_view([]))
    assert "◆ 沪深飙升" not in capsys.readouterr().out


def test_render_hot_region_marks_dash_for_missing_fields(capsys):
    """量比/市值缺失显示 —，不伪造 0.00。"""
    from scanner.display import render_terminal

    c = _cand(volume_ratio=0.0, market_capital=0.0, volume=0, amount=0)
    c.score = 50.0
    render_terminal(_make_view([c]))
    out = capsys.readouterr().out
    assert "—" in out


# ── 独立 CLI / 离线自检 ─────────────────────────────────────────────────────


def test_offline_demo_all_cases_match_expectation(capsys):
    """离线自检：内置样本的期望结果必须全部命中（退出码 0）。

    这条是「筛选规则回归哨兵」——改动任何阈值/排除条件却没同步 _DEMO_CASES 时，
    这里会失败并指出具体哪条样本不符。
    """
    from scanner.hot_watch import run_offline_demo

    rc = run_offline_demo(top_n=3, emit_json=False)
    out = capsys.readouterr().out
    assert rc == 0, f"离线自检未全绿：\n{out}"
    assert "FAIL" not in out


def test_offline_demo_covers_every_exclusion_branch():
    """样本的排除原因必须覆盖 `hard_exclude` 的全部分支，防止自检漏测某条规则。

    2026-09-16：硬门拆成「通用门（display_gates.common_hard_gate，三区共用）+ 本区专有」
    之后，关键字表按**通用门清单**重列 —— 少一个关键字就说明自检样本没跟上门的演化。
    「主力净流出」不在本表：`hard_exclude` 的资金流门要传入 ff_pct（由 build_candidates
    从 DB 快照取），离线样本路径没有资金流数据；该分支由 tests/test_display_gates.py
    的「三区同答」用例覆盖。
    """
    from scanner.display_gates import UNIVERSAL_GATES
    from scanner.hot_watch import _DEMO_CASES, build_candidates

    board = [b for _, (b, _q) in _DEMO_CASES]
    quotes = {q["symbol"]: q for _, (_b, q) in _DEMO_CASES}
    _passed, rejected = build_candidates(board, quotes)

    reasons = " ".join(hard_exclude(c) or "" for c in rejected)
    for keyword in (*UNIVERSAL_GATES, "非上涨", "涨幅过高"):
        if keyword == "主力净流出":
            continue
        assert keyword in reasons, f"自检样本未覆盖排除分支：{keyword}"


def test_cli_main_offline_demo_returns_zero():
    """`python -m scanner.hot_watch --offline-demo` 退出码 0。"""
    from scanner.hot_watch import main

    assert main(["--offline-demo"]) == 0


def test_cli_json_output_is_parseable(capsys):
    import json

    from scanner.hot_watch import main

    assert main(["--offline-demo", "--json"]) == 0
    out = capsys.readouterr().out
    # JSON 在自检表格之后输出
    payload = out[out.index("[") :]
    rows = json.loads(payload)
    assert len(rows) == 1
    assert {"code", "symbol", "name", "score"} <= set(rows[0])


def test_cli_threshold_override_changes_result(capsys):
    """--max-percent 下调后排除了原本通过的票（证明 CLI 阈值覆盖真的生效）。

    ⚠️ main() 会改写模块级阈值全局（hard_exclude/compute_score 都读它），
    **必须还原**——否则会污染同进程后续用例（pytest 单进程内顺序执行）。
    """
    import scanner.hot_watch as hw

    saved = (hw.HOT_MAX_PERCENT, hw.HOT_MAX_MARKET_CAP, hw.HOT_ENRICH_LIMIT)
    try:
        hw.main(["--offline-demo", "--max-percent", "5"])
        assert hw.HOT_MAX_PERCENT == 5.0  # 覆盖确实落到全局
        out = capsys.readouterr().out
        # 蓝盾光电 5.76% 在 5% 上限下被排除（自检表会同时报 FAIL，属预期）
        assert "涨幅过高(5.76%>5%)" in out
    finally:
        hw.HOT_MAX_PERCENT, hw.HOT_MAX_MARKET_CAP, hw.HOT_ENRICH_LIMIT = saved


def test_cli_conn_is_isolated():
    """CLI 用内存库，不落生产 scanner.db。"""
    from scanner.hot_watch import _cli_conn

    conn = _cli_conn()
    conn.execute("INSERT INTO hot_watch_hits(symbol,name,streak,last_round) VALUES('SZ999999','测试',1,1)")
    assert conn.execute("SELECT COUNT(*) FROM hot_watch_hits").fetchone()[0] == 1
    conn.close()
