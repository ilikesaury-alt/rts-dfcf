"""scanner.ranking 单源不变量（设计审查 P0 #1）。

防止档位/🎯 排序逻辑散落到其它模块重写副本：
- ranking 必须定义全部排序纯函数；
- 消费方（display/today_report/scripts）直接 import ranking，不再持有副本
  （2026-08-30 起 display 的全量 re-export 已下线，消费方直接 import）。
"""

import sqlite3

import pytest

import scanner.ranking as R
from scanner.ranking import _nextday_entry_accum, build_accum_map


class _StubStock:
    """StockInfo 鸭子替身（替代 type("S", (), {}) 动态 stub，属性可静态检查）。"""

    def __init__(self, percent: float = 0.0, current: float = 0.0, rank: int | None = None):
        self.percent = percent
        self.current = current
        self.rank = rank


class _StubKline:
    """KlineSummary 鸭子替身。"""

    def __init__(self, dimensions: dict | None = None, accumulated_pct: float | None = None):
        self.dimensions = dimensions if dimensions is not None else {}
        self.accumulated_pct = accumulated_pct


class _StubCandidate:
    """Candidate 鸭子替身。"""

    def __init__(self) -> None:
        self.stock: _StubStock | None = None
        self.kline: _StubKline | None = None
        self.category: str | None = None
        self.is_stale: bool = False


RANKING_FUNCS = [
    "_entry_band",
    "entry_dims",
    "_entry_fund_flow_pct",
    "_entry_overbought",
    "_entry_sector_resonance",
    "entry_tier",
    "_entry_weak_to_strong",
    "fresh_candidate",
    "_in_nextday_sweet_band",
    "_is_breakout_setup",
    "_is_relist_breakout_setup",
    "_nextday_entry_accum",
    "_nextday_entry_percent",
    "build_breakout_kline_map",
]


def test_ranking_defines_all_functions():
    missing = [n for n in RANKING_FUNCS if not hasattr(R, n)]
    assert not missing, f"scanner.ranking 缺少函数: {missing}"


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
        cand = _StubCandidate()
        cand.kline = _StubKline(dimensions={"accumulated_incl_today": 8.5}, accumulated_pct=99.0)
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
    cand = _StubCandidate()
    cand.stock = _StubStock(percent=9.5)  # 冻结快照（掉榜时刻），若被消费会判 8-10% 陷阱带
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

    fresh = _StubCandidate()
    fresh.stock = _StubStock(percent=9.5)
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


# 2026-09-16：原 `TestComebackSortKeyFlowFallback` / `TestComebackSortKeyTodayExtremity`
# 随 `ranking.comeback_sort_key` 与回马枪桶删除（两处排序单源已无消费方）。
# `entry_fund_flow_pct` 本身仍在（展示层资金流出硬门用），其回退链由
# tests/test_view_flow_gate.py 守护。


class TestFreshCandidate:
    """fresh_candidate 单源收口（2026-08-24 第二轮审查）。

    两类快照不得参与展示/判定：stale 掉榜候选（冻结在掉榜时刻）+ 双挂票
    类别错位候选（today_pool 按 symbol 只存一个对象，恒 short_term）。
    """

    @staticmethod
    def _mk_cand(category="short_term", dims=None, stale=False):
        cand = _StubCandidate()
        cand.stock = _StubStock(percent=9.5)
        cand.kline = _StubKline(dimensions=dims if dims is not None else {}, accumulated_pct=77.0)
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
        assert R.entry_dims(e) == {"v_st_overbought": False}

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
        assert R.entry_dims(e) == {"accumulated_incl_today": 7.0}
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
        assert R.entry_dims(e) == {"a": 1}
        assert R._nextday_entry_percent(e) == 9.5

    @staticmethod
    def _mk_cat_cand(category="short_term", dims=None, stale=False):
        return TestFreshCandidate._mk_cand(category=category, dims=dims, stale=stale)


class TestFundFlowNormDirection:
    """`_fund_flow_norm` 五档的方向与量级守护（2026-09-14 收口）。

    修复前 `strong_in` 拿 **+0.5（五档最大值）**，而同函数 docstring 声称「强流入正向
    加分已于 2026-08-10 因反指下线」——代码 / docstring / config_sources 三处口径互相
    矛盾。实测（n=382）：strong_in 次日 −0.774%，好于有资金流数据的全样本 −0.880%，
    不是反指；但也没有任何证据支持「强流入 > 流入」（in 组 −1.178%，n=229，两者差
    0.4pp 且未做显著性检验）⇒ 与 in 同权 +0.3。本组用例锁住「强流入不享有最高权」。
    """

    @staticmethod
    def _flow(pct: float) -> dict:
        return {"score_breakdown": {"fund_flow_main_pct": pct}}

    def test_strong_inflow_not_above_inflow(self):
        """★ 强流入不得比流入更高分（此前 +0.5 > +0.3，独占五档最大值）。"""
        assert R._fund_flow_norm(self._flow(12.0)) <= R._fund_flow_norm(self._flow(6.0))

    def test_strong_outflow_stays_lowest(self):
        """流出档的相对次序不得被本次收口打乱。"""
        vals = {
            "strong_out": R._fund_flow_norm(self._flow(-9.0)),
            "out": R._fund_flow_norm(self._flow(-6.0)),
            "neutral": R._fund_flow_norm(self._flow(0.0)),
            "in": R._fund_flow_norm(self._flow(6.0)),
            "strong_in": R._fund_flow_norm(self._flow(12.0)),
        }
        assert vals["strong_out"] == min(vals.values())
        assert vals["strong_out"] < vals["out"] < vals["neutral"]

    def test_no_data_is_neutral(self):
        """无资金流数据按中性 +0.1（fail-open，不惩罚无数据票）。"""
        assert R._fund_flow_norm({}) == 0.1

    def test_range_capped_at_inflow_weight(self):
        """值域上界 = 流入档 +0.3（旧实现的 +0.5 不再可达）。"""
        for pct in (-20.0, -9.0, -6.0, 0.0, 6.0, 12.0, 50.0):
            assert -0.5 <= R._fund_flow_norm(self._flow(pct)) <= 0.3
