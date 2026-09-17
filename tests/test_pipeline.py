"""scanner.pipeline 纯函数单测（第 3 步等价变换，2026-09-13）。

**为什么需要这组测试**：`orchestrator.scan_with_raw`（507 行 / CC 95，主数据通路）
**零测试覆盖**——`tests/test_orchestrator.py` 只测辅助函数，没有一个用例调用它。
拆出来的每个纯函数在这里补上直接覆盖，让"主通路的组成部分"第一次有了单测。

**与黄金样本的分工**（两者都要有，不可互相替代）：
- `scripts/golden_scan.py` 证明**整体等价**（同一输入 → `ScanResult` 逐字段一致）；
  但它有覆盖缺口（comeback 桶恒为 0、new_face/momentum 多数日期 0~1）。
- 本文件证明**局部正确**，且能覆盖黄金样本走不到的分支（如大市值过滤、
  现价超限、current<=0 剔除、各桶排序键）。

全程离线，不读 scanner.db、不联网。
"""

from __future__ import annotations

import pytest

from scanner.models import V2_CATEGORY, Candidate, KlineSummary, StockInfo
from scanner.pipeline import (
    accumulate_final_scores,
    attach_minute_trends,
    attach_tactic_tags,
    build_current_quotes,
    build_rps_inputs,
    filter_by_market_cap,
    filter_excluded_by_risk,
    rebuild_pool_picks,
    report_market_cap_availability,
    split_and_sort_categories,
)
from scanner.pipeline import scoring as pm_scoring
from scanner.pipeline import tactics as pm_tactics
from scanner.pipeline.features import build_kline_dimension_patch
from scanner.pipeline.pool import report_market_cap_availability as _rca

YI = 100_000_000


def _stock(symbol: str, current: float = 10.0, market_cap: float = 0.0) -> StockInfo:
    return StockInfo(
        symbol=symbol, name=f"N-{symbol}", code=symbol[2:] if len(symbol) > 6 else symbol,
        percent=5.0, current=current, value=10_000, rank_change=0, rank=1,
        market_cap=market_cap,
    )


def _cand(symbol: str, category: str = "new_face", score: int = 20,
          kline_dims: dict | None = None) -> Candidate:
    stock = _stock(symbol)
    return Candidate(
        stock=stock, category=category, score=score, reason="t",
        kline=KlineSummary(trend="t", accumulated_pct=2.0, volume_ratio=1.5,
                           bottom_confirmed=True, score=score,
                           dimensions=kline_dims or {}, avg_volume=1_000_000),
        first_seen="09:30",
    )


def _klines(n: int = 8, today: str = "2026-09-11", base: float = 100.0,
            step: float = 1.0) -> list[dict]:
    """构造 n 根 K 线：日期从 today 往前推（含 today），收盘价等差递增。"""
    from datetime import date, timedelta

    d0 = date.fromisoformat(today)
    bars = []
    for i in range(n):
        d = (d0 - timedelta(days=n - 1 - i)).isoformat()
        bars.append({"date": d, "open": base, "high": base, "low": base,
                     "close": base + i * step, "volume": 1.0, "percent": 1.0})
    return bars


# ── filter_by_market_cap ──


class TestFilterByMarketCap:
    def test_backfills_current_when_zero(self):
        """现价为 0 时用市值数据的 current 回填（下游小叶美/展示都依赖此字段）。"""
        s = _stock("SZ300001", current=0.0)
        _filtered, _n = filter_by_market_cap([s], {"SZ300001": {"current": 12.5}})
        assert s.current == 12.5

    def test_does_not_overwrite_nonzero_current(self):
        s = _stock("SZ300001", current=9.0)
        filter_by_market_cap([s], {"SZ300001": {"current": 12.5}})
        assert s.current == 9.0

    def test_backfills_market_cap_in_yi_circ_first(self):
        """流通市值优先，且转成亿元。"""
        s = _stock("SZ300001")
        filter_by_market_cap([s], {"SZ300001": {"circ_market_cap": 30 * YI,
                                                "market_cap": 90 * YI}})
        assert s.market_cap == pytest.approx(30.0)

    def test_falls_back_to_total_market_cap(self):
        s = _stock("SZ300001")
        filter_by_market_cap([s], {"SZ300001": {"market_cap": 90 * YI}})
        assert s.market_cap == pytest.approx(90.0)

    def test_price_gate_filters_and_does_not_count_large_cap(self):
        """价格门在前：被价格门拦下的**不计入** filtered_large_cap。"""
        s = _stock("SZ300001", current=999.0)
        filtered, n = filter_by_market_cap([s], {"SZ300001": {"market_cap": 900 * YI}})
        assert filtered == [] and n == 0

    def test_large_cap_gate_counts_only_after_price_gate(self):
        s = _stock("SZ300001", current=10.0)
        filtered, n = filter_by_market_cap([s], {"SZ300001": {"market_cap": 900 * YI}})
        assert filtered == [] and n == 1

    def test_missing_cap_data_passes_through(self):
        """无市值数据 → 不拦（fail-open，不能因缺数据而整轮无候选）。"""
        s = _stock("SZ300001")
        filtered, n = filter_by_market_cap([s], {})
        assert filtered == [s] and n == 0

    def test_zero_market_cap_is_not_treated_as_large(self):
        s = _stock("SZ300001")
        filtered, _n = filter_by_market_cap([s], {"SZ300001": {"market_cap": 0}})
        assert filtered == [s]

    def test_preserves_input_order(self):
        syms = [f"SZ30000{i}" for i in range(5)]
        stocks = [_stock(s) for s in syms]
        filtered, _n = filter_by_market_cap(stocks, {})
        assert [s.symbol for s in filtered] == syms

    def test_counts_multiple_large_caps(self):
        stocks = [_stock("SZ300001"), _stock("SZ300002"), _stock("SZ300003")]
        caps = {s.symbol: {"market_cap": 900 * YI} for s in stocks}
        _filtered, n = filter_by_market_cap(stocks, caps)
        assert n == 3


class TestReportMarketCapAvailability:
    def test_stale_cache_prints_degraded_not_warning(self, capsys):
        report_market_cap_availability({"A": {}}, ["A"], used_stale_mc=True)
        out = capsys.readouterr().out
        assert "[~]" in out and "[!]" not in out
        assert "小叶美规则基于旧市值生效" in out

    def test_no_data_at_all_prints_warning(self, capsys):
        report_market_cap_availability({}, ["A"], used_stale_mc=False)
        out = capsys.readouterr().out
        assert "[!]" in out

    def test_fresh_data_is_silent(self, capsys):
        report_market_cap_availability({"A": {}}, ["A"], used_stale_mc=False)
        assert capsys.readouterr().out == ""

    def test_empty_symbol_list_is_silent(self, capsys):
        """没有要查的票 → 不打告警（否则非交易时段空榜会刷屏）。"""
        report_market_cap_availability({}, [], used_stale_mc=False)
        assert capsys.readouterr().out == ""


# ── build_current_quotes ──


class TestBuildCurrentQuotes:
    def test_drops_entries_without_current(self):
        """current<=0（停牌/字段缺失强转 0）必须剔除——2026-08-14 fail-open 修复的口径。"""
        quotes = build_current_quotes({"A": {"current": 0, "percent": 5.0},
                                       "B": {"current": 12.0, "percent": 3.0}})
        assert set(quotes) == {"B"}

    def test_missing_current_key_is_dropped(self):
        assert build_current_quotes({"A": {"percent": 5.0}}) == {}

    def test_defaults_and_high_pct_passthrough(self):
        quotes = build_current_quotes({"A": {"current": 12.0}})
        assert quotes["A"] == {"percent": 0.0, "current": 12.0, "high_pct": None}

    def test_high_pct_none_is_kept_as_none(self):
        """high_pct=None 表示无数据，纪律内部据此 fail-open 跳过依赖项——不能填 0。"""
        assert build_current_quotes({"A": {"current": 1.0, "high_pct": None}})["A"]["high_pct"] is None

    def test_empty_input(self):
        assert build_current_quotes({}) == {}


# ── build_rps_inputs ──


class TestBuildRpsInputs:
    def test_baseline_excludes_today(self):
        """历史口径：排除今日 bar（否则与 short_term 的含今日口径混算，百分位偏高）。"""
        klines = {"A": _klines(7, today="2026-09-11")}
        baseline, _m = build_rps_inputs([_stock("A")], [], klines, "2026-09-11")
        assert len(baseline) == 1
        # n=7 → 日期 09-05..09-11，收盘 100..106；剔除今日剩 6 根 100..105
        # → (105-100)/100*100 = 5%（注意是「倒数第 6 根」= 5 日累计，不是首根）
        assert baseline[0] == pytest.approx(5.0)

    def test_skips_symbols_without_kline(self):
        _baseline, _m = build_rps_inputs([_stock("A")], [], {}, "2026-09-11")
        assert _baseline == []

    def test_skips_when_fewer_than_six_closes(self):
        klines = {"A": _klines(6, today="2026-09-11")}  # 含 today → 历史只有 5 根
        baseline, _m = build_rps_inputs([_stock("A")], [], klines, "2026-09-11")
        assert baseline == []

    def test_accum_map_covers_candidates(self):
        klines = {"A": _klines(7, today="2026-09-11")}
        _b, accum = build_rps_inputs([], [_cand("A")], klines, "2026-09-11")
        assert accum["A"] == pytest.approx(5.0)

    def test_accum_map_uses_same_scale_as_baseline(self):
        """两趟循环口径必须一致（否则 RPS 百分位系统性偏移）。"""
        klines = {"A": _klines(7, today="2026-09-11")}
        baseline, accum = build_rps_inputs([_stock("A")], [_cand("A")], klines, "2026-09-11")
        assert baseline[0] == pytest.approx(accum["A"])

    def test_negative_accum_is_allowed(self):
        """跌的票保留负号（不是取绝对值，也不是被过滤掉）。"""
        klines = {"A": _klines(7, today="2026-09-11", base=100.0, step=-1.0)}
        baseline, _m = build_rps_inputs([_stock("A")], [], klines, "2026-09-11")
        # 历史 6 根收盘 100..95 → (95-100)/100*100 = -5%
        assert baseline[0] == pytest.approx(-5.0)


# ── attach_minute_trends ──


class TestAttachMinuteTrends:
    def test_writes_all_four_dimensions(self):
        c = _cand("A")
        attach_minute_trends([c], {"A": {"steady_rise_ratio": 0.8, "day_high_pct": 5.0,
                                         "am_high_pct": 3.0, "vol_trend": 1.4}})
        d = c.kline.dimensions
        assert d["minute_steady_rise"] == 0.8
        assert d["minute_day_high"] == 5.0
        assert d["minute_am_high"] == 3.0
        assert d["minute_vol_trend"] == 1.4

    def test_empty_trend_dict_is_treated_as_no_data(self):
        """空字典视为无数据 → 整段跳过（原实现 `if c.kline and trend`，空 dict 是 falsy）。

        这与「部分字段缺失」不同：见下一条。
        """
        c = _cand("A")
        attach_minute_trends([c], {"A": {}})
        assert "minute_vol_trend" not in c.kline.dimensions

    def test_missing_keys_fall_back_to_defaults(self):
        """部分字段缺失 → 缺的填默认值，不是抛 KeyError。"""
        c = _cand("A")
        attach_minute_trends([c], {"A": {"day_high_pct": 5.0}})
        d = c.kline.dimensions
        assert d["minute_day_high"] == 5.0
        assert d["minute_steady_rise"] == 0.0
        assert d["minute_am_high"] == 0.0
        assert d["minute_vol_trend"] == 1.0  # 默认 1.0（不是 0.0）

    def test_skips_when_no_trend(self):
        c = _cand("A")
        attach_minute_trends([c], {})
        assert "minute_vol_trend" not in c.kline.dimensions

    def test_skips_when_no_kline(self):
        c = _cand("A")
        c.kline = None
        attach_minute_trends([c], {"A": {"vol_trend": 2.0}})  # 不应抛异常

    def test_does_not_touch_foreign_symbols(self):
        a, b = _cand("A"), _cand("B")
        attach_minute_trends([a, b], {"A": {"vol_trend": 2.0}})
        assert a.kline.dimensions["minute_vol_trend"] == 2.0
        assert "minute_vol_trend" not in b.kline.dimensions

    def test_patch_helper_matches_attached_dims(self):
        trend = {"steady_rise_ratio": 0.5, "day_high_pct": 1.0, "am_high_pct": 2.0, "vol_trend": 3.0}
        c = _cand("A")
        attach_minute_trends([c], {"A": trend})
        assert build_kline_dimension_patch(trend) == {
            k: c.kline.dimensions[k] for k in
            ("minute_steady_rise", "minute_day_high", "minute_am_high", "minute_vol_trend")
        }


# ── split_and_sort_categories ──


class TestSplitAndSortCategories:
    def test_known_new_face_joins_new_faces(self):
        b = split_and_sort_categories([_cand("A", "new_face"), _cand("B", "known_new_face")])
        assert [c.stock.symbol for c in b["new_faces"]] == ["A", "B"]

    def test_buckets_are_disjoint_and_complete(self):
        cands = [_cand(f"SZ30000{i}", cat) for i, cat in enumerate(
            ["new_face", "momentum", "rebound", "short_term", "pool_pick", "core_dip"])]
        b = split_and_sort_categories(cands)
        covered = sum(len(v) for v in b.values())
        # 四个桶；pool_pick（由 V2_CATEGORY 单独重建）与 core_dip（独立区）不在桶内
        assert covered == 4

    def test_score_buckets_sorted_descending(self):
        cands = [_cand("A", "momentum", 10), _cand("B", "momentum", 30), _cand("C", "momentum", 20)]
        b = split_and_sort_categories(cands)
        assert [c.score for c in b["momentum"]] == [30, 20, 10]

    def test_new_face_sorted_descending_by_score(self):
        b = split_and_sort_categories([_cand("A", "new_face", 10), _cand("B", "new_face", 30)])
        assert [c.score for c in b["new_faces"]] == [30, 10]

    def test_known_new_face_sorted_ascending_by_score(self):
        """known_new_face 分数**反指**：低分档收益更好 → 升序（与 new_face 相反）。"""
        b = split_and_sort_categories([_cand("A", "known_new_face", 30),
                                       _cand("B", "known_new_face", 10)])
        assert [c.score for c in b["new_faces"]] == [10, 30]
        # 与 new_face 的方向确实相反（用同一个键函数交叉验证）
        nf = split_and_sort_categories([_cand("A", "new_face", 30), _cand("B", "new_face", 10)])
        assert [c.score for c in nf["new_faces"]] == [30, 10]

    # 2026-09-16：原 `test_comeback_uses_comeback_sort_key_not_raw_score` /
    # `test_comeback_falls_back_to_score_when_amplitude_ties` 随 comeback 桶与
    # `ranking.comeback_sort_key` 删除。

    def test_rebuilds_from_all_candidates_not_stale_refs(self):
        """加分循环用 dataclass_replace 造了新对象——必须从 all_candidates 重建。

        这个坑踩过两次（v1 与 pool_picks 各一次），故用测试锁死：
        传入替换后的新对象，桶里必须是新对象（带新 score）。
        """
        old = _cand("A", "momentum", 10)
        new = _cand("A", "momentum", 99)
        b = split_and_sort_categories([new])
        assert b["momentum"][0].score == 99
        assert old.score == 10  # 旧对象不受影响

    def test_empty_input_gives_all_empty_buckets(self):
        b = split_and_sort_categories([])
        assert set(b) == {"new_faces", "momentum", "rebound", "short_term"}
        assert all(v == [] for v in b.values())


# ── accumulate_final_scores / filter_excluded_by_risk / rebuild_pool_picks ──


class TestAccumulateFinalScores:
    def test_adds_extra_to_score(self, monkeypatch):
        monkeypatch.setattr(pm_scoring, "accumulate_final_score", lambda c, _o: 7)
        lst = [_cand("A", "new_face", 20)]
        accumulate_final_scores(lst, {})
        assert lst[0].score == 27

    def test_replaces_list_element_with_new_object(self, monkeypatch):
        """原地按索引替换（不是 `c.score += extra`）——下游多处持有对象引用，
        原地改字段会让「同一只票在两个桶里」共享同一对象时互相污染。
        """
        monkeypatch.setattr(pm_scoring, "accumulate_final_score", lambda c, _o: 5)
        c = _cand("A", "new_face", 20)
        lst = [c]
        accumulate_final_scores(lst, {})
        assert lst[0] is not c          # 列表里换成了新对象
        assert c.score == 20            # 旧对象不被就地修改
        assert lst[0].score == 25

    def test_double_hung_candidates_compute_extra_independently(self, monkeypatch):
        """双挂（首板票同时挂 new_face + short_term）必须各自算 extra。

        若复用同一 extra，short_term 桶会拿到 new_face 桶的 bonus，排名错位。
        """
        monkeypatch.setattr(
            pm_scoring, "accumulate_final_score",
            lambda c, _o: 3 if c.category == "new_face" else 11,
        )
        nf = _cand("A", "new_face", 20)
        st = _cand("A", "short_term", 20)
        lst = [nf, st]
        accumulate_final_scores(lst, {})
        assert [c.score for c in lst] == [23, 31]

    def test_passes_opening_scores_through(self, monkeypatch):
        seen: dict = {}

        def _fake(c, o):
            seen["o"] = o
            return 0

        monkeypatch.setattr(pm_scoring, "accumulate_final_score", _fake)
        accumulate_final_scores([_cand("A")], {"A": 1.0})
        assert seen["o"] == {"A": 1.0}

    def test_empty_list_is_noop(self, monkeypatch):
        monkeypatch.setattr(pm_scoring, "accumulate_final_score", lambda c, _o: 1 / 0)
        accumulate_final_scores([], {})  # 不抛异常


class TestFilterExcludedByRisk:
    def test_no_hit_returns_same_list_object(self, monkeypatch):
        """无命中时原样返回同一对象（不复制）——与原实现的对象身份一致。"""
        monkeypatch.setattr(pm_scoring, "candidate_excluded_by_risk", lambda c: False)
        lst = [_cand("A"), _cand("B")]
        kept, excluded = filter_excluded_by_risk(lst)
        assert kept is lst and excluded == []

    def test_hit_is_removed_and_reported(self, monkeypatch, capsys):
        monkeypatch.setattr(pm_scoring, "candidate_excluded_by_risk",
                            lambda c: c.stock.symbol == "B")
        a, b_ = _cand("A"), _cand("B")
        kept, excluded = filter_excluded_by_risk([a, b_])
        assert kept == [a] and excluded == [b_]
        assert "1 只命中硬排除标签" in capsys.readouterr().out

    def test_name_list_truncated_at_eight(self, monkeypatch, capsys):
        """超过 8 只只列前 8 个名字 + 「等 N 只」（防终端刷屏）。"""
        monkeypatch.setattr(pm_scoring, "candidate_excluded_by_risk", lambda c: True)
        cands = [_cand(f"SZ30000{i}") for i in range(10)]
        _kept, excluded = filter_excluded_by_risk(cands)
        out = capsys.readouterr().out
        assert len(excluded) == 10
        assert out.count("(SZ3") == 8        # 票名里 symbol 出现两次，按左括号计数
        assert "SZ300009" not in out         # 第 10 只不列名
        assert "等10只" in out

    def test_identical_symbols_in_two_categories_filtered_separately(self, monkeypatch):
        """按「对象身份」而非 symbol 剔除：双挂票一个被杀另一个保留。"""
        monkeypatch.setattr(pm_scoring, "candidate_excluded_by_risk",
                            lambda c: c.category == "short_term")
        nf = _cand("A", "new_face")
        st = _cand("A", "short_term")
        kept, excluded = filter_excluded_by_risk([nf, st])
        assert kept == [nf] and excluded == [st]


class TestRebuildPoolPicks:
    def test_keeps_only_v2_category(self):
        picks = rebuild_pool_picks([_cand("A", "new_face"), _cand("B", V2_CATEGORY)])
        assert [c.stock.symbol for c in picks] == ["B"]

    def test_sorted_by_today_percent_descending(self):
        cands = [_cand("A", V2_CATEGORY), _cand("B", V2_CATEGORY), _cand("C", V2_CATEGORY)]
        for c, p in zip(cands, (1.0, 9.0, 5.0), strict=False):
            c.stock.percent = p
        assert [c.stock.percent for c in rebuild_pool_picks(cands)] == [9.0, 5.0, 1.0]

    def test_none_percent_treated_as_zero(self):
        """percent 为 None（缺行情）按 0 处理，不抛 TypeError。"""
        c = _cand("A", V2_CATEGORY)
        c.stock.percent = None
        assert rebuild_pool_picks([c]) == [c]

    def test_returns_new_list(self):
        """必须返回新列表（重建语义），不能就地排序传入列表。"""
        cands = [_cand("A", V2_CATEGORY)]
        out = rebuild_pool_picks(cands)
        assert out is not cands

    def test_empty_when_no_v2(self):
        assert rebuild_pool_picks([_cand("A", "new_face")]) == []


# ── attach_tactic_tags ──


class TestAttachTacticTags:
    def test_writes_tags_from_stock_actions(self, monkeypatch):
        monkeypatch.setattr(pm_tactics, "stock_actions",
                            lambda c, **kw: ["减仓"])
        c = _cand("A")
        attach_tactic_tags([c], {}, {})
        assert c.tactic_tags == ["减仓"]

    def test_passes_high_pct_from_quote(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(pm_tactics, "stock_actions",
                            lambda c, **kw: seen.update(kw) or [])
        c = _cand("A")
        attach_tactic_tags([c], {"A": {"high_pct": 7.5, "current": 10.0}}, {})
        assert seen["high_pct"] == 7.5

    def test_missing_quote_gives_none_high_pct(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(pm_tactics, "stock_actions",
                            lambda c, **kw: seen.update(kw) or [])
        attach_tactic_tags([_cand("A")], {}, {})
        assert seen["high_pct"] is None  # 无数据 → 纪律内部 fail-open，不能填 0

    def test_passes_kline_bars_of_that_symbol(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(pm_tactics, "stock_actions",
                            lambda c, **kw: seen.update(kw) or [])
        bars = [{"date": "2026-09-11", "close": 1.0}]
        attach_tactic_tags([_cand("A")], {}, {"A": bars})
        assert seen["kline_bars"] is bars

    def test_single_stock_failure_does_not_stop_others(self, monkeypatch, capsys):
        """逐票 try/except：一只票脏数据只跳过该票（2026-08-31 审查修复）。"""
        def boom(c, **kw):
            if c.stock.symbol == "A":
                raise ValueError("脏数据")
            return ["加仓"]

        monkeypatch.setattr(pm_tactics, "stock_actions", boom)
        a, b_ = _cand("A"), _cand("B")
        attach_tactic_tags([a, b_], {}, {})
        assert a.tactic_tags == []       # 保留调用前的值
        assert b_.tactic_tags == ["加仓"]
        assert "盘中操作纪律计算失败 A" in capsys.readouterr().out

    def test_none_kline_is_tolerated(self, monkeypatch):
        monkeypatch.setattr(pm_tactics, "stock_actions", lambda c, **kw: [])
        attach_tactic_tags([_cand("A")], {}, {"A": None})


def test_report_market_cap_availability_is_reexported():
    """pipeline 包导出与实现模块必须指向同一函数（防改名后两处漂移）。"""
    assert _rca is report_market_cap_availability
