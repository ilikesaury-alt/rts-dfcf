"""scanner.ranking 单源不变量（设计审查 P0 #1）。

防止档位/🎯 排序逻辑再次散落为 display 内的副本：
- ranking 必须定义全部排序纯函数；
- display 必须 re-export **同一个对象**（不是重新实现一份）；
  一旦有人在 display 里又写 `def _entry_tier`，`display._entry_tier is
  ranking._entry_tier` 即变 False，本测试报警，挡住口径漂移。
"""

import sqlite3

import pytest

import scanner.display as D
import scanner.ranking as R
from scanner.ranking import _nextday_entry_accum, build_accum_map

RANKING_FUNCS = [
    "_entry_band",
    "_entry_dims",
    "_entry_fund_flow_pct",
    "_entry_overbought",
    "_entry_sector_resonance",
    "_entry_tier",
    "_entry_weak_to_strong",
    "_fresh_candidate",
    "_in_nextday_sweet_band",
    "_is_breakout_setup",
    "_is_nextday_marked",
    "_is_relist_breakout_setup",
    "_nextday_entry_accum",
    "_nextday_entry_percent",
    "build_breakout_kline_map",
]


def test_ranking_defines_all_functions():
    missing = [n for n in RANKING_FUNCS if not hasattr(R, n)]
    assert not missing, f"scanner.ranking 缺少函数: {missing}"


def test_display_reexports_same_objects():
    """display 必须指向与 ranking 完全相同的函数对象（单源，非副本）。"""
    for n in RANKING_FUNCS:
        assert hasattr(D, n), f"display 未 re-export {n}"
        assert getattr(D, n) is getattr(R, n), (
            f"display.{n} 不是 scanner.ranking.{n} 的同一对象——疑似在 display 内重写了排序逻辑（口径漂移风险）"
        )


def test_sweet_band_pure_logic():
    """甜蜜带纯函数行为不变量（<2% 或 4~8% 命中，2~4% 死区不命中）。"""
    assert R._in_nextday_sweet_band(1.0) is True
    assert R._in_nextday_sweet_band(5.0) is True
    assert R._in_nextday_sweet_band(3.0) is False
    assert R._in_nextday_sweet_band(9.0) is False


def _mk_db():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE daily_kline ("
        " symbol TEXT NOT NULL, date TEXT NOT NULL, open REAL,"
        " close REAL, high REAL, low REAL, volume REAL, percent REAL,"
        " PRIMARY KEY(symbol, date))"
    )
    return conn


def _insert_kline(conn, sym, rows):
    # rows: list of (date, close, percent)
    for dt, close, pct in rows:
        conn.execute(
            "INSERT OR REPLACE INTO daily_kline"
            " (symbol, date, open, close, high, low, volume, percent)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (sym, dt, close, close, close, close, 0, pct),
        )


def _entry(sym, rec_date, accumulated_pct=None):
    e = {"symbol": sym, "date": rec_date, "category": "momentum", "score": 50}
    if accumulated_pct is not None:
        e["accumulated_pct"] = accumulated_pct
    return e


class TestBuildAccumMap:
    """P1-9：build_accum_map 单批次回放 ≡ 逐行 _nextday_entry_accum，且保留 DB 落库兜底。"""

    def test_matches_per_row_replay(self):
        conn = _mk_db()
        closes = [10.0, 10.5, 11.0, 11.5, 12.0, 12.6]
        pcts = [0.0, 5.0, 5.0, 5.0, 5.0, 5.0]
        dates = [f"2026-08-1{i}" for i in range(1, 7)]
        _insert_kline(conn, "SZ300001", list(zip(dates, closes, pcts, strict=True)))
        entries = [_entry("SZ300001", "2026-08-16")]
        batch = build_accum_map(conn, entries)
        per_row = _nextday_entry_accum(entries[0], conn)
        assert batch["SZ300001"] == per_row
        assert batch["SZ300001"] == pytest.approx(26.0)  # (12.6-10.0)/10.0*100

    def test_db_fallback_when_no_kline(self):
        conn = _mk_db()  # 无 daily_kline 行
        entries = [_entry("SZ300002", "2026-08-16", accumulated_pct=12.0)]
        batch = build_accum_map(conn, entries)
        # 回放无数据 → 兜底 DB 落库 accumulated_pct
        assert batch["SZ300002"] == 12.0

    def test_candidate_row_uses_dimensions(self):
        conn = _mk_db()
        cand = type("C", (), {})()
        kline = type("K", (), {})()
        kline.dimensions = {"accumulated_incl_today": 8.5}
        kline.accumulated_pct = 99.0
        cand.kline = kline
        e = {"symbol": "SZ300003", "date": "2026-08-16", "category": "momentum", "score": 50, "_candidate": cand}
        batch = build_accum_map(conn, [e])
        assert batch["SZ300003"] == 8.5  # 维度优先，不查 DB


def test_dropped_row_prefers_db_percent_over_live():
    """掉榜行（无候选）🎯/涨幅带判定用落库推荐时刻口径，不吃漂移的 live_quotes。

    2026-08-21 审查修复：unified_scanner 会为掉榜票主动补拉实时行情，旧实现把
    live_percent 排在 DB percent 之前 → 判定随盘中价格逐轮漂移、偏离校准口径。
    """
    e = {
        "symbol": "SZ300010",
        "date": "2026-08-21",
        "category": "momentum",
        "percent": 5.0,
        "live_quote_available": True,
        "live_percent": 15.0,
    }
    assert R._nextday_entry_percent(e) == 5.0


def test_dropped_row_live_fallback_without_db_percent():
    """落库 percent 缺失时才兜底 live_quotes。"""
    e = {
        "symbol": "SZ300011",
        "date": "2026-08-21",
        "category": "momentum",
        "live_quote_available": True,
        "live_percent": 7.5,
    }
    assert R._nextday_entry_percent(e) == 7.5


def test_stale_candidate_not_used_for_nextday_percent():
    """stale 掉榜候选的冻结快照不参与 🎯/涨幅带判定（2026-08-24 审查修复）。

    同根因同族扩散：6f92be0 只挡了 display 的 rank/current 回退，本处第一优先级
    候选分支漏守卫——冻结在掉榜时刻的 percent 会让甜蜜带/陷阱带判定偏离推荐
    时刻落库口径。stale 视同无候选，直接落 DB percent。
    """
    cand = type("C", (), {})()
    stock = type("S", (), {})()
    stock.percent = 9.5  # 冻结快照（掉榜时刻），若被消费会判 8-10% 陷阱带
    cand.stock = stock
    cand.is_stale = True
    e = {
        "symbol": "SZ300012",
        "date": "2026-08-24",
        "category": "momentum",
        "score": 50,
        "_candidate": cand,
        "percent": 4.5,
    }
    assert R._nextday_entry_percent(e) == 4.5

    fresh = type("C", (), {})()
    fresh_stock = type("S", (), {})()
    fresh_stock.percent = 9.5
    fresh.stock = fresh_stock
    fresh.is_stale = False
    e_fresh = {
        "symbol": "SZ300013",
        "date": "2026-08-24",
        "category": "momentum",
        "score": 50,
        "_candidate": fresh,
        "percent": 4.5,
    }
    assert R._nextday_entry_percent(e_fresh) == 9.5  # 非 stale 仍走最新扫描快照


class TestComebackSortKeyFlowFallback:
    """comeback_sort_key 的 flow_map 回退必须真实生效（2026-08-24 审查修复）。

    旧实现 `to_float(dims.get(...))` 默认 default=0.0 → dims 缺失时 flow 恒为
    0.0 而非 None，`if flow is None` 恒假——flow_map 死参数，掉榜行全部按中性
    0 排序，资金流优先排序对最需要它的对象失效。
    """

    @staticmethod
    def _cb(sym, score, breakdown=None):
        e = {"symbol": sym, "date": "2026-08-24", "category": "comeback", "score": score}
        if breakdown is not None:
            e["score_breakdown"] = breakdown
        return e

    def test_dropped_row_uses_flow_map(self):
        """掉榜行无 dims 但 flow_map 有值 → 资金流排序生效。"""
        out_flow = self._cb("SZ300020", 40)
        no_flow = self._cb("SZ300021", 99)
        ranked = sorted([no_flow, out_flow], key=lambda x: R.comeback_sort_key(x, {"SZ300020": 6.0}))
        assert ranked[0]["symbol"] == "SZ300020"  # ▲ 强流入在前， despite 低分

    def test_negative_flow_ranks_behind_neutral(self):
        """▼▼ 流出劣后于中性 0（flow_map 补值路径）。"""
        outflow = self._cb("SZ300022", 90)
        neutral = self._cb("SZ300023", 10)
        ranked = sorted([outflow, neutral], key=lambda x: R.comeback_sort_key(x, {"SZ300022": -8.0}))
        assert ranked[0]["symbol"] == "SZ300023"

    def test_dims_value_still_wins_over_flow_map(self):
        """有 dims 的行仍以自身维度优先（回退仅补缺失）。"""
        has_dims = self._cb("SZ300024", 10, breakdown={"fund_flow_main_pct": -8.0})
        via_map = self._cb("SZ300025", 10)
        ranked = sorted([has_dims, via_map], key=lambda x: R.comeback_sort_key(x, {"SZ300025": 6.0, "SZ300024": -8.0}))
        assert ranked[0]["symbol"] == "SZ300025"

    def test_no_data_treated_as_neutral_zero(self):
        """两源皆缺按中性 0 处理、次键评分降序（docstring 承诺）。"""
        lo = self._cb("SZ300026", 30)
        hi = self._cb("SZ300027", 80)
        ranked = sorted([hi, lo], key=R.comeback_sort_key)
        assert ranked[0]["symbol"] == "SZ300027"


class TestComebackSortKeyTodayExtremity:
    """comeback_sort_key 把今日波动剧烈的（涨多/跌狠）排前（2026-08-29）。"""

    @staticmethod
    def _cb(sym, score, percent=0.0):
        return {"symbol": sym, "date": "2026-08-29", "category": "comeback", "score": score, "percent": percent}

    def test_big_gainer_before_flat(self):
        big = self._cb("SZ300030", 50, percent=9.0)
        flat = self._cb("SZ300031", 90, percent=0.0)
        ranked = sorted([flat, big], key=R.comeback_sort_key)
        assert ranked[0]["symbol"] == "SZ300030"

    def test_big_dropper_before_flat(self):
        drop = self._cb("SZ300032", 50, percent=-8.0)
        flat = self._cb("SZ300033", 90, percent=0.5)
        ranked = sorted([flat, drop], key=R.comeback_sort_key)
        assert ranked[0]["symbol"] == "SZ300032"

    def test_today_extremity_beats_flow(self):
        """今日波动优先于资金流（涨多/跌狠是主排序键，资金流为次级区分）。"""
        extreme_lowflow = self._cb("SZ300034", 50, percent=7.0)
        mild_highflow = self._cb("SZ300035", 50, percent=1.0)
        ranked = sorted([mild_highflow, extreme_lowflow], key=lambda x: R.comeback_sort_key(x, {"SZ300035": 8.0}))
        assert ranked[0]["symbol"] == "SZ300034"


class TestFreshCandidate:
    """_fresh_candidate 单源收口（2026-08-24 第二轮审查）。

    两类快照不得参与展示/判定：stale 掉榜候选（冻结在掉榜时刻）+ 双挂票
    类别错位候选（today_pool 按 symbol 只存一个对象，恒 short_term）。
    """

    @staticmethod
    def _mk_cand(category="short_term", dims=None, stale=False):
        cand = type("C", (), {})()
        stock = type("S", (), {})()
        stock.percent = 9.5
        cand.stock = stock
        kline = type("K", (), {})()
        kline.dimensions = dims if dims is not None else {}
        kline.accumulated_pct = 77.0
        cand.kline = kline
        cand.category = category
        cand.is_stale = stale
        return cand

    def test_stale_candidate_excluded_from_dims(self):
        """stale 掉榜候选的冻结 dims 不抢在 DB score_breakdown 之前。"""
        cand = self._mk_cat_cand(dims={"v_st_overbought": True}, stale=True)
        e = {
            "symbol": "SZ300030",
            "category": "momentum",
            "score": 50,
            "_candidate": cand,
            "score_breakdown": {"v_st_overbought": False},
        }
        assert R._entry_dims(e) == {"v_st_overbought": False}

    def test_dual_listed_category_mismatch_falls_to_breakdown(self):
        """双挂票：池内恒存 short_term 候选，new_face 行不得吃 st 口径维度。"""
        st_cand = self._mk_cand(category="short_term", dims={"st_weak_to_strong": 8})
        e = {
            "symbol": "SZ300031",
            "category": "new_face",
            "score": 50,
            "_candidate": st_cand,
            "score_breakdown": {"accumulated_incl_today": 7.0},
        }
        assert R._entry_dims(e) == {"accumulated_incl_today": 7.0}
        # 🎯 累计门槛走 DB 口径而非 st 候选冻结 kline
        conn = _mk_db()
        _insert_kline(conn, "SZ300031", [(f"2026-08-{d:02d}", 10.0 + d, 1.0) for d in range(11, 17)])
        e2 = {"symbol": "SZ300031", "date": "2026-08-16", "category": "new_face", "score": 50, "_candidate": st_cand}
        accum = R._nextday_entry_accum(e2, conn)
        assert accum is not None and accum != 77.0

    def test_matching_category_still_used(self):
        """类别匹配且非 stale 的候选行为不变（回归保护）。"""
        cand = self._mk_cat_cand(category="new_face", dims={"a": 1})
        e = {"symbol": "SZ300032", "category": "new_face", "score": 50, "_candidate": cand}
        assert R._entry_dims(e) == {"a": 1}
        assert R._nextday_entry_percent(e) == 9.5

    @staticmethod
    def _mk_cat_cand(category="short_term", dims=None, stale=False):
        return TestFreshCandidate._mk_cand(category=category, dims=dims, stale=stale)
