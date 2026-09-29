"""动量加速观察画像（⚡）测试：ranking._is_breakout_setup + build_breakout_kline_map。

2026-09-29 重设计：结构门由「缩量蓄势突破」（前5日≤5% + T-1缩量 + 回撤8~18% + MA多头）
翻转为「动量加速」（截至 T-1 前5日累计 > +20%）。翻转依据是样本外检验：旧口径三门与
341 只真实涨停票的 T-1 形态**全部反向**（见 config_scoring BREAKOUT_ACCUM_MIN 注释）。

定位仍是纯展示层观察标记——本测试锁定判定条件、fail-closed 行为与「不影响档位」，
防止静默漂移。凡涉及旧口径的用例都写成**反例**：那些形态现在必须被拒。
"""

import sqlite3

import pytest

from scanner.config_scoring import BREAKOUT_ACCUM_MIN, BREAKOUT_MIN_BARS
from scanner.ranking import (
    _breakout_structure_ok,
    _is_breakout_setup,
    _is_relist_breakout_setup,
    build_breakout_kline_map,
)


def _mk_db():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE daily_kline ("
        " symbol TEXT NOT NULL, date TEXT NOT NULL, open REAL,"
        " close REAL, high REAL, low REAL, volume REAL, percent REAL,"
        " PRIMARY KEY(symbol, date))"
    )
    return conn


def _bars(closes, vols=None):
    """把收盘序列包成 (date, high, close, volume) —— 判定函数的输入契约。"""
    out = []
    for i, close in enumerate(closes):
        vol = 1_000_000.0 if vols is None else vols[i]
        out.append((f"2026-08-{i + 1:02d}", close * 1.01, close, vol))
    return out


def _accel(n=12, gain=BREAKOUT_ACCUM_MIN + 6.0):
    """正例形态：末 5 根连续拉升至累计恰好 +gain%（%）。

    构造保证 (closes[-1] / closes[-6] - 1) * 100 **精确等于** gain ——
    判定函数读的就是这两个下标，夹具有偏差会把边界用例（恰好 = / 恰好 > 阈值）测成假阳性。
    """
    ref = 20.0
    top = ref * (1.0 + gain / 100.0)
    closes = [ref] * n
    for k in range(5):  # 索引 n-5..n-1；closes[n-6] 保持 ref 不动
        closes[n - 5 + k] = ref + (top - ref) * k / 4.0
    return _bars(closes)


def _flat(n=12):
    """反例形态：长期横盘（**旧 ⚡ 口径会标的形态**，现必须被拒）。"""
    return _bars([20.0 + (0.02 if i % 2 else 0.0) for i in range(n)])


def _entry(sym="SZ300001", rec_date="2026-08-23", category="new_face", first_push=False):
    e = {"symbol": sym, "date": rec_date, "category": category, "score": 40, "score_breakdown": {}}
    if first_push:
        e["score_breakdown"] = {"first_today_bonus": 3}
    return e


def _insert(conn, sym, bars):
    for dt, high, close, vol in bars:
        conn.execute(
            "INSERT OR REPLACE INTO daily_kline"
            " (symbol, date, open, close, high, low, volume, percent)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (sym, dt, close, close, high, close, vol, 0.0),
        )


class TestStructureGate:
    """结构门本体：唯一的「T-1 前5日累计 > +20%」。"""

    def test_positive_accelerating(self):
        assert _breakout_structure_ok(_entry(), klines=_accel()) is True

    def test_boundary_is_strict_greater_than(self):
        """恰好等于阈值不标（`>` 而非 `>=`）—— 边界口径不得含糊。"""
        bars = _accel(gain=BREAKOUT_ACCUM_MIN)
        assert _breakout_structure_ok(_entry(), klines=bars) is False
        bars = _accel(gain=BREAKOUT_ACCUM_MIN + 0.5)
        assert _breakout_structure_ok(_entry(), klines=bars) is True

    def test_reject_flat_consolidation(self):
        """**旧口径反例**：长期横盘蓄势现在必须被拒。

        2026-09-29 前的 ⚡ 正是靠「前5日≤5% + 缩量 + 回撤」标这种票，
        而样本外检验证明该组封板率**低于**基准。留着这条用例防止口径被改回去。
        """
        assert _breakout_structure_ok(_entry(), klines=_flat()) is False

    def test_lookahead_guard_recommendation_day_excluded(self):
        """**隐性前视守卫**：推荐日当天的涨幅不参与计算。

        旧实现收 accum/accum_map，那条 🎯 链是「含推荐日」口径 —— 推荐日尚未收盘，
        它不是「启动前」的量。构造一根「T-1 之前横盘、推荐日暴涨」的序列：
        build_breakout_kline_map 只给 date < rec_date 的行，故 T-1 仍判横盘 → 不标。
        """
        conn = _mk_db()
        flat = _flat(n=12)  # 12 根横盘，末日 = T-1
        _insert(conn, "SZ300001", flat)
        # 推荐日当天 +20%（若被误计入，5日累计会远超阈值）
        conn.execute("INSERT INTO daily_kline VALUES ('SZ300001','2026-08-23',20,24,24.5,19.9,9e6,20.0)")
        e = _entry(rec_date="2026-08-23")
        kmap = build_breakout_kline_map(conn, [_entry()])
        assert all(r[0] < "2026-08-23" for r in kmap[("SZ300001", "2026-08-23")]), "推荐日 bar 必须被滤除"
        assert _breakout_structure_ok(e, conn, klines=kmap[("SZ300001", "2026-08-23")]) is False

    def test_min_bars_fail_closed(self):
        assert _breakout_structure_ok(_entry(), klines=_accel(n=5)) is False

    def test_min_bars_is_exactly_six(self):
        """BREAKOUT_MIN_BARS=6：5 根不够，6 根刚好。"""
        assert BREAKOUT_MIN_BARS == 6
        assert _breakout_structure_ok(_entry(), klines=_accel(n=6)) is True

    def test_no_klines_no_conn_is_closed(self):
        assert _breakout_structure_ok(_entry(), klines=None) is False

    def test_conn_fallback_matches_klines_path(self):
        conn = _mk_db()
        _insert(conn, "SZ300001", _accel())
        e = _entry()
        assert _breakout_structure_ok(e, conn) is True
        assert _breakout_structure_ok(e, conn, klines=_accel()) is True

    def test_signature_drops_accum_params(self):
        """结构门不再收 accum/accum_map —— 那条 🎯 链含推荐日，是隐性前视。"""
        import inspect

        params = set(inspect.signature(_breakout_structure_ok).parameters)
        assert "accum" not in params
        assert "accum_map" not in params


class TestIsBreakoutSetup:
    def test_positive_new_face(self):
        assert _is_breakout_setup(_entry(), klines=_accel()) is True

    def test_positive_first_push_other_category(self):
        assert _is_breakout_setup(_entry(category="short_term", first_push=True), klines=_accel()) is True

    def test_reject_non_new_face_without_first_push(self):
        assert _is_breakout_setup(_entry(category="momentum"), klines=_accel()) is False

    def test_reject_flat_new_face(self):
        """类别门过了但结构门不过 → 不标。"""
        assert _is_breakout_setup(_entry(), klines=_flat()) is False

    def test_reject_insufficient_bars(self):
        assert _is_breakout_setup(_entry(), klines=_accel(n=5)) is False

    def test_reject_no_klines_no_conn(self):
        assert _is_breakout_setup(_entry(), klines=None) is False


class TestIsRelistBreakoutSetup:
    """⚡R 重上榜动量加速观察：非首推 short_term + 共用结构条件。

    与 ⚡ 的唯一差异是类别门；结构条件必须与 _is_breakout_setup 同源同结果，防口径漂移。
    """

    def test_positive_short_term_not_first_push(self):
        assert _is_relist_breakout_setup(_entry(category="short_term"), klines=_accel()) is True

    def test_negative_first_push_short_term_belongs_to_bo(self):
        e = _entry(category="short_term", first_push=True)
        assert _is_relist_breakout_setup(e, klines=_accel()) is False
        assert _is_breakout_setup(e, klines=_accel()) is True

    def test_negative_other_categories(self):
        for cat in ("momentum", "new_face", "known_new_face", "rebound", "comeback"):
            assert _is_relist_breakout_setup(_entry(category=cat), klines=_accel()) is False, cat

    def test_structure_parity_with_breakout_setup(self):
        """同一 K 线序列下，除类别门外判定结果必须完全一致（单源结构共用）。"""
        for bars, expected in ((_accel(), True), (_flat(), False), (_accel(n=5), False)):
            bo = _is_breakout_setup(_entry(category="new_face"), klines=bars)
            relist = _is_relist_breakout_setup(_entry(category="short_term"), klines=bars)
            assert bo is relist is expected

    def test_conn_fallback(self):
        conn = _mk_db()
        _insert(conn, "SZ300001", _accel())
        assert _is_relist_breakout_setup(_entry(category="short_term"), conn=conn) is True

    def test_not_in_sort_tier(self):
        """⚡R 是观察标记：不得影响档位排序。"""
        from scanner.ranking import entry_tier

        conn = _mk_db()
        _insert(conn, "SZ300001", _accel())
        e = _entry(category="short_term")
        e["_candidate"] = None
        assert _is_relist_breakout_setup(e, conn) is True
        assert entry_tier(e, conn) == 2


class TestBuildBreakoutKlineMap:
    def test_filters_future_dates_and_cleans_dirty_rows(self):
        conn = _mk_db()
        bars = _accel()
        _insert(conn, "SZ300001", bars)
        conn.execute("INSERT INTO daily_kline VALUES ('SZ300001','2026-08-23',1,1,1,1,1,0)")
        conn.execute("INSERT INTO daily_kline VALUES ('SZ300001','2026-08-24',1,-5,1,1,1,0)")
        kmap = build_breakout_kline_map(conn, [_entry()])
        rows = kmap[("SZ300001", "2026-08-23")]
        assert all(r[0] < "2026-08-23" for r in rows)
        assert len(rows) == len(bars)

    def test_empty_entries(self):
        assert build_breakout_kline_map(_mk_db(), []) == {}

    def test_keyed_by_symbol_and_date(self):
        """**2026-09-29 回归守卫**：返回值必须用 (symbol, rec_date) 双键。

        旧实现按 symbol 单键，同一票在多个推荐日出现时后写覆盖先写 —— 先写那几行
        会拿到截止到**最后一个**推荐日的 K 线，结构门静默算错。实测症状：善水科技
        四个推荐日的 T-1 累计全被算成同一个值。
        """
        conn = _mk_db()
        # 造 3 天的 bar：日期 06/10/11
        for dt, close in (("2026-08-10", 10.0), ("2026-08-11", 12.0), ("2026-08-12", 30.0)):
            conn.execute(
                "INSERT INTO daily_kline VALUES (?,?,?,?,?,?,?,?)",
                ("SZ300001", dt, close, close, close * 1.01, close, 1e6, 0.0),
            )
        early = _entry(rec_date="2026-08-11")  # 只能看到 08-10
        late = _entry(rec_date="2026-08-13")  # 能看到 08-10/11/12
        kmap = build_breakout_kline_map(conn, [early, late])
        assert set(kmap) == {("SZ300001", "2026-08-11"), ("SZ300001", "2026-08-13")}
        assert len(kmap[("SZ300001", "2026-08-11")]) == 1, "早期行不得被晚期行覆盖"
        assert len(kmap[("SZ300001", "2026-08-13")]) == 3

    def test_result_feeds_marker_end_to_end(self):
        conn = _mk_db()
        _insert(conn, "SZ300001", _accel())
        entries = [_entry()]
        kmap = build_breakout_kline_map(conn, entries)
        assert _is_breakout_setup(entries[0], klines=kmap[("SZ300001", "2026-08-23")]) is True


class TestBreakoutNotInSortTier:
    """⚡ 是观察标记：不得影响档位排序（用户决策：先观察，不改排序位置）。"""

    def test_tier_ignores_breakout_profile(self):
        from scanner.ranking import entry_tier

        conn = _mk_db()
        _insert(conn, "SZ300001", _accel())
        e = _entry()
        e["_candidate"] = None
        kmap = build_breakout_kline_map(conn, [e])
        assert _is_breakout_setup(e, conn, klines=kmap[("SZ300001", "2026-08-23")]) is True  # 命中画像……
        assert entry_tier(e, conn) == 2  # ……但档位不被 ⚡ 提升


class TestThresholdProvenance:
    def test_threshold_is_the_validated_value(self):
        """阈值锁在 2026-09-29 样本外验证过的那个数（支撑区间 18%~25% 的中部）。"""
        assert BREAKOUT_ACCUM_MIN == 20.0

    @pytest.mark.parametrize("gain,expected", [(10.0, False), (18.0, False), (21.0, True), (25.0, True)])
    def test_sensitivity_band(self, gain, expected):
        """敏感性：10%/18% 不标，21%/25% 标 —— 记录支撑区间的实际形状。"""
        assert _breakout_structure_ok(_entry(), klines=_accel(gain=gain)) is expected
