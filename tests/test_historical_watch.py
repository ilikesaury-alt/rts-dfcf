"""historical_watch（v1 回捞独立区）单测。

覆盖：候选域查询与去重、时间归一化量比（投影/下界/数据不足）、各道硬门、
评分因子边界、纯函数 evaluate 的排序、run_historical_watch 的 fail-open 边界、
以及离线自检回归。全部密封单测（不触网、不依赖真实 scanner.db）。
"""

import sqlite3

import pytest

from scanner.config import (
    FUND_OUTFLOW_NET_PCT,
    HIST_DIP_BROKEN_FLOOR,
    HIST_DIP_IDEAL_LOW,
    HIST_DIP_PCT,
    HIST_LOOKBACK_DAYS,
    HIST_MIN_ELAPSED_MIN,
    HIST_MIN_VOL_RATIO,
    HIST_VOL_AVG_DAYS,
    HIST_W_DIP,
    HIST_W_RECENCY,
    HIST_W_VOL,
    MAX_STOCK_PRICE,
)
from scanner.historical_watch import (
    _DEMO_AVG_VOL,
    V1_CATEGORIES,
    collect_v1_history,
    compute_score,
    compute_vol_ratio,
    dip_factor,
    evaluate,
    hard_gate,
    recency_factor,
    run_historical_watch,
    run_offline_demo,
    vol_factor,
)
from scanner.trend_beauty import BEAUTY_MARK

TODAY = "2026-09-16"
REC1 = "2026-09-15"  # 距 today = 1 个交易日
REC2 = "2026-09-14"  # 距 today = 2 个交易日


# ── 夹具 ────────────────────────────────────────────────────────────────────


def _meta(symbol="SZ300806", name="斯迪克", days_ago=1, cat="momentum", score=60, rec_date=REC1):
    return {
        "symbol": symbol,
        "name": name,
        "rec_date": rec_date,
        "rec_days_ago": days_ago,
        "rec_category": cat,
        "rec_score": score,
    }


def _quote(current=9.50, percent=-5.0, volume=1.5e6, cap=5e9, symbol="SZ300806"):
    return {
        "symbol": symbol,
        "code": symbol[2:] if len(symbol) > 6 else symbol,
        "current": current,
        "percent": percent,
        "volume": volume,
        "market_capital": cap,
    }


def _hist(n=HIST_VOL_AVG_DAYS, volume=_DEMO_AVG_VOL, close=10.0, end=REC1):
    """构造 n 根历史 bar（默认日均量 = _DEMO_AVG_VOL，便于按比例设计量比）。"""
    return [{"date": f"2026-09-{10 + i:02d}", "close": close, "volume": volume} for i in range(n)]


def _hist_up(n=20, end=REC1):
    """构造一段「日线趋势漂亮」的 K 线：单调上行、无长上影、无暴跌。

    展示标记用例专用 —— 默认的 `_hist()` 是**平坦**序列，MA5=MA10=MA20 过不了
    trend_beauty 的「MA 多头排列」硬门，只能得到空标记，无法验证「美」这一档。
    六道硬门的对齐：MA 多头（逐日 +3%）/ slope 10.6% ≥ 0 / 近 5 日单日跌幅 0（percent
    全正）/ 上影 0.2% < 4% / 收盘即 20 日最高 ⇒ 回撤 0%。
    """
    out = []
    for i in range(n):
        c = 10.0 + i * 0.3
        out.append(
            {
                "date": end,
                "open": round(c - 0.05, 2),
                "high": round(c + 0.02, 2),
                "low": round(c - 0.15, 2),
                "close": round(c, 2),
                "volume": _DEMO_AVG_VOL,
                "percent": 2.5,
            }
        )
    return out


def _conn(rows=()):
    """带 recommendations 表的内存库（列对齐 collect_v1_history 的 SELECT）。"""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE recommendations (
               date TEXT, symbol TEXT, name TEXT, category TEXT,
               score INTEGER, excluded INTEGER DEFAULT 0)"""
    )
    conn.executemany("INSERT INTO recommendations VALUES (?,?,?,?,?,?)", rows)
    return conn


def _v1_row(date, symbol="SZ300806", name="斯迪克", cat="momentum", score=60, excluded=0):
    return (date, symbol, name, cat, score, excluded)


class _Adapter:
    """最小适配器桩：只实现本区用到的批量行情。"""

    def __init__(self, quotes=None, exc=None):
        self._quotes = quotes if quotes is not None else {}
        self._exc = exc
        self.calls = 0

    def fetch_hot_quotes_batch(self, symbols):
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        return {s: self._quotes[s] for s in symbols if s in self._quotes}


class _NoBatchAdapter:
    """不支持批量行情的源（如 THS 回退源）——本区应直接留空。"""


# ── 时间归一化量比 ──────────────────────────────────────────────────────────


def test_vol_ratio_identity_when_session_closed():
    """收盘后（240 分钟）投影是恒等映射：量比 = volume / 日均量。"""
    assert compute_vol_ratio(2 * _DEMO_AVG_VOL, _hist(), 240) == pytest.approx(2.0)


def test_vol_ratio_projects_intraday_partial_session():
    """盘中半天（120 分钟）量能翻倍投影 —— 消除「早盘量比天然偏低」偏置。"""
    assert compute_vol_ratio(_DEMO_AVG_VOL, _hist(), 120) == pytest.approx(2.0)


def test_vol_ratio_uses_elapsed_floor_on_open():
    """开盘首分钟必须走下界：否则 09:31 会把量比投影放大约 240 倍。"""
    got = compute_vol_ratio(_DEMO_AVG_VOL, _hist(), 1)
    assert got == pytest.approx(240 / HIST_MIN_ELAPSED_MIN)


def test_vol_ratio_none_when_history_too_short():
    assert compute_vol_ratio(_DEMO_AVG_VOL, _hist(n=HIST_VOL_AVG_DAYS - 1), 240) is None


def test_vol_ratio_none_on_bad_volume():
    assert compute_vol_ratio(0.0, _hist(), 240) is None
    assert compute_vol_ratio(_DEMO_AVG_VOL, _hist(volume=0.0), 240) is None


def test_vol_ratio_uses_only_recent_average_window():
    """只取最近 HIST_VOL_AVG_DAYS 根：更早的巨量不应污染分母。"""
    older = [{"date": "2026-08-01", "close": 10.0, "volume": 1e9}]
    got = compute_vol_ratio(_DEMO_AVG_VOL, older + _hist(), 240)
    assert got == pytest.approx(1.0)


# ── 硬门 ────────────────────────────────────────────────────────────────────


def test_hard_gate_passes_healthy_candidate():
    assert hard_gate(_meta(), _quote(), 1.5, None) is None


@pytest.mark.parametrize(
    ("expect_kw", "meta_kw", "quote_kw", "vr", "flow"),
    [
        ("未回调到位", {}, {"percent": -1.0}, 1.5, None),
        ("未回调到位", {}, {"percent": 3.0}, 1.5, None),
        ("量能萎缩", {}, {}, HIST_MIN_VOL_RATIO - 0.01, None),
        ("量能不可判定", {}, {}, None, None),
        ("ST", {"name": "*ST测试"}, {}, 1.5, None),
        ("非创业板", {"symbol": "SZ002443"}, {"symbol": "SZ002443"}, 1.5, None),
        ("无有效报价", {}, {"current": 0.0}, 1.5, None),
        ("价格过高", {}, {"current": MAX_STOCK_PRICE + 1}, 1.5, None),
        ("市值过大", {}, {"cap": 6e10}, 1.5, None),
    ],
)
def test_hard_gate_rejects(expect_kw, meta_kw, quote_kw, vr, flow):
    meta = _meta(**meta_kw)
    quote = _quote(**{"symbol": meta["symbol"], **quote_kw})
    reason = hard_gate(meta, quote, vr, flow)
    assert reason is not None and expect_kw in reason


def test_hard_gate_fund_outflow_boundary():
    """资金流门在阈值处剔除（≤），缺失（None）fail-open 放过。"""
    assert hard_gate(_meta(), _quote(), 1.5, FUND_OUTFLOW_NET_PCT) is not None
    assert hard_gate(_meta(), _quote(), 1.5, FUND_OUTFLOW_NET_PCT + 0.1) is None
    assert hard_gate(_meta(), _quote(), 1.5, None) is None


def test_hard_gate_dip_boundary_is_inclusive():
    """恰好等于阈值视为「回调到位」（≤ 判定，与离线统计口径一致）。"""
    assert hard_gate(_meta(), _quote(percent=HIST_DIP_PCT), 1.5, None) is None
    assert hard_gate(_meta(), _quote(percent=HIST_DIP_PCT + 0.01), 1.5, None) is not None


def test_hard_gate_vol_ratio_boundary_is_inclusive():
    assert hard_gate(_meta(), _quote(), HIST_MIN_VOL_RATIO, None) is None


def test_hard_gate_market_cap_missing_is_fail_open():
    """市值缺失（0）不判定 —— 宁可放过也不误杀。"""
    assert hard_gate(_meta(), _quote(cap=0.0), 1.5, None) is None


# ── 评分因子 ────────────────────────────────────────────────────────────────


def test_dip_factor_plateau_and_decay():
    assert dip_factor(HIST_DIP_PCT) == pytest.approx(1.0)
    assert dip_factor(HIST_DIP_IDEAL_LOW) == pytest.approx(1.0)
    mid = dip_factor((HIST_DIP_IDEAL_LOW - 15.0) / 2)  # -11.5
    assert HIST_DIP_BROKEN_FLOOR < mid < 1.0
    assert dip_factor(-30.0) == pytest.approx(HIST_DIP_BROKEN_FLOOR)


def test_dip_factor_zero_when_not_dipped():
    assert dip_factor(HIST_DIP_PCT + 0.5) == 0.0


def test_vol_factor_capped():
    assert vol_factor(1.0) == pytest.approx(0.5)
    assert vol_factor(999.0) == pytest.approx(1.0)
    assert vol_factor(-1.0) == 0.0


def test_recency_factor_decays():
    assert recency_factor(1) == pytest.approx(1.0)
    assert recency_factor(2) < recency_factor(1)
    assert recency_factor(0) == pytest.approx(1.0)  # 防御：非法值不放大


def test_compute_score_full_marks():
    """满分组合 = 回调到位 + 量比封顶 + 距 v1 一天。"""
    total = compute_score(-5.0, 2.0, 1)
    assert total == pytest.approx(HIST_W_DIP + HIST_W_VOL + HIST_W_RECENCY)


def test_compute_score_recency_penalty():
    assert compute_score(-5.0, 2.0, 2) < compute_score(-5.0, 2.0, 1)


# ── 纯函数 evaluate ─────────────────────────────────────────────────────────


def test_evaluate_filters_and_sorts_by_score():
    metas = [
        _meta("SZ300001", "甲", days_ago=1),
        _meta("SZ300002", "乙", days_ago=2),
        _meta("SZ300003", "丙", days_ago=1),
    ]
    quotes = {
        "SZ300001": _quote(symbol="SZ300001", percent=-5.0, volume=2 * _DEMO_AVG_VOL),
        "SZ300002": _quote(symbol="SZ300002", percent=-5.0, volume=2 * _DEMO_AVG_VOL),
        "SZ300003": _quote(symbol="SZ300003", percent=-0.5, volume=2 * _DEMO_AVG_VOL),  # 未回调
    }
    klines = {m["symbol"]: _hist() for m in metas}
    out = evaluate(metas, quotes, klines, {}, 240, TODAY)
    assert [c.symbol for c in out] == ["SZ300001", "SZ300002"]  # 丙被门剔除；甲(1日) 排在 乙(2日) 前


def test_evaluate_uses_rec_date_close_for_cum_pct():
    """自 v1 日累计以**推荐日收盘**为基准（不是昨收）。"""
    metas = [_meta("SZ300001", rec_date="2026-09-10")]
    quotes = {"SZ300001": _quote(symbol="SZ300001", current=8.0, percent=-5.0, volume=2 * _DEMO_AVG_VOL)}
    hist = [{"date": "2026-09-10", "close": 10.0, "volume": _DEMO_AVG_VOL}] * HIST_VOL_AVG_DAYS
    out = evaluate(metas, quotes, {"SZ300001": hist}, {}, 240, TODAY)
    assert out[0].cum_pct == pytest.approx(-20.0)


def test_evaluate_skips_symbol_without_quote():
    metas = [_meta("SZ300001")]
    assert evaluate(metas, {}, {"SZ300001": _hist()}, {}, 240, TODAY) == []


def test_evaluate_excludes_today_bar_from_volume_baseline():
    """今日 bar 必须排除在均量之外——否则「今日量 vs 含今日均量」自我参照。"""
    metas = [_meta("SZ300001")]
    quotes = {"SZ300001": _quote(symbol="SZ300001", volume=_DEMO_AVG_VOL)}
    klines = {"SZ300001": _hist() + [{"date": TODAY, "close": 9.5, "volume": 9e9}]}
    out = evaluate(metas, quotes, klines, {}, 240, TODAY)
    assert out[0].vol_ratio == pytest.approx(1.0)


# ── 候选域 ──────────────────────────────────────────────────────────────────


# ── 行尾展示标记（2026-09-16）：取数在本区、成形在渲染层 ──────────────────────


def test_evaluate_fills_display_marks():
    """evaluate 必须把资金流与日线美感写进 HistCandidate —— 两端行尾标记的**唯一取数口**。

    标记怎么画是渲染层的事，但值只在这里产出。少了这两个字段不会报错，只会「什么都不显示」，
    属最容易被忽略的静默降级，故单独立用例。
    """
    m = _meta()
    rows = evaluate([m], {m["symbol"]: _quote()}, {m["symbol"]: _hist_up()}, {m["symbol"]: -6.4}, 240, TODAY)

    assert len(rows) == 1
    assert rows[0].ff_pct == -6.4
    assert rows[0].beauty == BEAUTY_MARK


def test_evaluate_ff_pct_none_when_flow_missing():
    """无资金流数据 → ff_pct 为 None（**不是** 0.0）：0 是「中性」这一档，两者不可混。

    混了会让「拿不到数据」显示成中性，与「确实没流入也没流出」无法区分。
    """
    m = _meta()
    rows = evaluate([m], {m["symbol"]: _quote()}, {m["symbol"]: _hist_up()}, {}, 240, TODAY)

    assert rows[0].ff_pct is None


def test_evaluate_beauty_blank_when_daily_not_beautiful():
    """日线不漂亮 → 空标记（fail-open：不判丑，只是不标）。"""
    m = _meta()
    rows = evaluate([m], {m["symbol"]: _quote()}, {m["symbol"]: _hist()}, {}, 240, TODAY)

    assert rows[0].beauty == ""


def test_evaluate_never_marks_strong_even_if_meta_carries_breakdown():
    """结构守卫：本区**永不**产出「美★」，即使 meta 里带 score_breakdown。

    ★ 需要「分时确认漂亮」，而本区不抓分时。库里那份 score_breakdown 是**上次推荐当日**
    的分时 —— 一旦有人图省事把 meta 整个喂进 beauty_mark，evaluate_intraday_beauty 会把它
    当成今日分时，把「美」静默升级成「美★」（用户会读成「分时也漂亮」）。
    本用例故意喂一份 intraday_score=9.0 的 breakdown，断言结果仍是「美」。
    """
    m = _meta() | {"score_breakdown": '{"intraday_score": 9.0}'}
    rows = evaluate([m], {m["symbol"]: _quote()}, {m["symbol"]: _hist_up()}, {}, 240, TODAY)

    assert rows[0].beauty == BEAUTY_MARK, "回捞区不得出现「美★」—— 它没有今日分时数据"


def test_extreme_outflow_never_reaches_display_rows():
    """净流出 ≤ -8% 在硬门即被剔除 ⇒ 行尾图标结构上不会出现 ▼▼ / 🔴🔴。

    这是设计而非巧合：本区把 -8% 当**准入**（不是告警），故图标只回答「-8% 以上这一段的强弱」。
    图例里那句「不出现 ▼▼」据此写成；若哪天放宽硬门，本用例与图例会同时红。
    """
    m = _meta()
    rows = evaluate([m], {m["symbol"]: _quote()}, {m["symbol"]: _hist_up()}, {m["symbol"]: -8.5}, 240, TODAY)

    assert rows == []


def test_collect_v1_history_dedups_keeping_most_recent():
    conn = _conn([_v1_row(REC2, "SZ300001", "甲", "rebound"), _v1_row(REC1, "SZ300001", "甲", "momentum")])
    out = collect_v1_history(conn, TODAY, HIST_LOOKBACK_DAYS)
    assert len(out) == 1
    assert out[0]["rec_date"] == REC1 and out[0]["rec_days_ago"] == 1


def test_collect_v1_history_skips_excluded_and_non_v1():
    conn = _conn(
        [
            _v1_row(REC1, "SZ300001", "甲", "momentum"),
            _v1_row(REC1, "SZ300002", "乙", "momentum", excluded=1),  # 被排除
            _v1_row(REC1, "SZ300003", "丙", "comeback"),  # 非 v1 桶
            _v1_row(TODAY, "SZ300004", "丁", "momentum"),  # 今日不进候选（只回捞历史）
        ]
    )
    out = collect_v1_history(conn, TODAY, HIST_LOOKBACK_DAYS)
    assert [m["symbol"] for m in out] == ["SZ300001"]


def test_collect_v1_history_respects_lookback_window():
    """窗口按「**有 v1 产出的**交易日」计数，不是自然日：超出窗口的日期整体不进候选。

    这条语义保证了跨周末/节假日后「距上次 v1 = 2」仍等于 2 个交易日，而不是 4 个自然日。
    """
    conn = _conn(
        [
            _v1_row(REC1, "SZ300001", "最近"),
            _v1_row(REC2, "SZ300002", "次近"),
            _v1_row("2026-09-11", "SZ300003", "窗口外"),
        ]
    )
    out = collect_v1_history(conn, TODAY, HIST_LOOKBACK_DAYS)
    assert [m["symbol"] for m in out] == ["SZ300001", "SZ300002"]
    assert collect_v1_history(conn, TODAY, 1) == [m for m in out if m["symbol"] == "SZ300001"]


def test_collect_v1_history_none_conn_returns_empty():
    assert collect_v1_history(None, TODAY) == []


def test_collect_v1_history_missing_table_returns_empty():
    conn = sqlite3.connect(":memory:")
    assert collect_v1_history(conn, TODAY) == []


def test_v1_categories_exclude_other_buckets():
    """候选域只含 v1 五桶：core_dip（复盘用途）与 pool_pick/comeback（掉榜域）不在内。"""
    for other in ("core_dip", "pool_pick", "comeback", "old_face"):
        assert other not in V1_CATEGORIES


# ── run_historical_watch 的 fail-open 边界 ──────────────────────────────────


def _patch(monkeypatch, *, elapsed=240, klines=None, exclude=None):
    monkeypatch.setattr("scanner.historical_watch.trading_minutes_elapsed", lambda *a, **k: elapsed)
    monkeypatch.setattr("scanner.db.queries.get_cached_klines", lambda conn, syms: klines or {})
    monkeypatch.setattr("scanner.db.queries.get_fund_flow_pct_map", lambda conn, syms: {})
    if exclude is not None:
        monkeypatch.setattr("scanner.historical_watch.HIST_EXCLUDE_TODAY_RECS", exclude)


def test_run_disabled_by_switch(monkeypatch):
    conn = _conn([_v1_row(REC1)])
    monkeypatch.setattr("scanner.historical_watch.HIST_WATCH_ENABLED", False)
    assert run_historical_watch(conn, _Adapter()) == []


def test_run_premarket_returns_empty(monkeypatch):
    """盘前（已过交易分钟 = 0）：拿不到当日涨跌幅，本区无判断依据。"""
    conn = _conn([_v1_row(REC1)])
    _patch(monkeypatch, elapsed=0)
    assert run_historical_watch(conn, _Adapter()) == []


def test_run_none_conn_returns_empty(monkeypatch):
    _patch(monkeypatch)
    assert run_historical_watch(None, _Adapter()) == []


def test_run_adapter_without_batch_capability_returns_empty(monkeypatch):
    conn = _conn([_v1_row(REC1)])
    _patch(monkeypatch, klines={"SZ300806": _hist()})
    assert run_historical_watch(conn, _NoBatchAdapter()) == []


def test_run_quotes_failure_is_fail_open(monkeypatch):
    conn = _conn([_v1_row(REC1)])
    _patch(monkeypatch, klines={"SZ300806": _hist()})
    assert run_historical_watch(conn, _Adapter(exc=OSError("net down"))) == []


def test_run_empty_quotes_returns_empty(monkeypatch):
    conn = _conn([_v1_row(REC1)])
    _patch(monkeypatch, klines={"SZ300806": _hist()})
    assert run_historical_watch(conn, _Adapter(quotes={})) == []


def test_run_happy_path_end_to_end(monkeypatch):
    """完整链路：候选 → 行情 → 量比 → 门 → 评分。"""
    conn = _conn([_v1_row(REC1, "SZ300806", "斯迪克")])
    adapter = _Adapter({"SZ300806": _quote(percent=-5.0, volume=2 * _DEMO_AVG_VOL)})
    _patch(monkeypatch, klines={"SZ300806": _hist()})
    out = run_historical_watch(conn, adapter)
    assert len(out) == 1
    assert out[0].score == pytest.approx(HIST_W_DIP + HIST_W_VOL + HIST_W_RECENCY)
    assert out[0].vol_ratio == pytest.approx(2.0)


def test_run_excludes_today_recommended_symbols(monkeypatch):
    """今日已推荐的票不再进本区（与 v1 池选样本域互斥，避免同屏重复选择）。"""
    conn = _conn([_v1_row(REC1, "SZ300806", "斯迪克")])
    adapter = _Adapter({"SZ300806": _quote(percent=-5.0, volume=2 * _DEMO_AVG_VOL)})
    _patch(monkeypatch, klines={"SZ300806": _hist()})
    assert run_historical_watch(conn, adapter, exclude_symbols={"SZ300806"}) == []


def test_run_truncates_to_top_n(monkeypatch):
    rows = [_v1_row(REC1, f"SZ30000{i}", f"票{i}") for i in range(1, 6)]
    conn = _conn(rows)
    quotes = {f"SZ30000{i}": _quote(symbol=f"SZ30000{i}", percent=-5.0, volume=2 * _DEMO_AVG_VOL) for i in range(1, 6)}
    adapter = _Adapter(quotes)
    klines = {f"SZ30000{i}": _hist() for i in range(1, 6)}
    _patch(monkeypatch, klines=klines)
    assert len(run_historical_watch(conn, adapter, top_n=2)) == 2


# ── 离线自检（回归哨兵）────────────────────────────────────────────────────


def test_offline_demo_all_cases_match_expectation(capsys):
    """离线自检必须全绿：改动阈值/门顺序后此用例是本区规则的第一道回归防线。"""
    assert run_offline_demo(emit_json=False) == 0
    assert "FAIL" not in capsys.readouterr().out


def test_offline_demo_json_runs(capsys):
    assert run_offline_demo(emit_json=True) == 0
    out = capsys.readouterr().out
    assert '"vol_ratio"' in out
