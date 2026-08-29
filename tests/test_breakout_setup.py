"""蓄势突破观察画像（⚡）测试：ranking._is_breakout_setup + build_breakout_kline_map。

画像来源：历史涨停复盘（「推荐后当日封板」20 只 vs 全部推荐对照，2026-08-21）。
定位为纯展示层观察标记——本测试锁定判定条件与 fail-closed 行为，防止静默漂移。
"""
import sqlite3

from scanner.ranking import _is_breakout_setup, _is_relist_breakout_setup, build_breakout_kline_map


def _mk_db():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE daily_kline ("
        " symbol TEXT NOT NULL, date TEXT NOT NULL, open REAL,"
        " close REAL, high REAL, low REAL, volume REAL, percent REAL,"
        " PRIMARY KEY(symbol, date))"
    )
    return conn


def _bars(n=22, shrink_vol=True, ma_bull=True):
    """构造 n 根 K 线 (date, high, close, volume)，默认满足全部画像条件。

    形态：温和上行 → 冲高 39（20日高点）→ 深回撤至 31.8（约 -20%）→
    尾部 5 根连续修复至 35.2（T-1 距高点 -11.5%，落入回撤窗口），
    MA5(33.36)>MA10(33.13)>MA20(29.15)，全程缩量（T-1/前5均 ≈ 0.80）。
    各开关用于逐条破坏单一条件做反例。
    """
    closes = [28.0, 28.3, 28.6, 28.9, 29.2, 29.5, 29.8, 30.1, 30.4,  # 0-8 温和上行
              35.0, 39.0,                                            # 9-10 冲高
              37.0, 35.0, 33.5, 32.5, 32.0, 31.8,                    # 11-16 深回撤
              ]
    if ma_bull:
        closes += [31.5, 32.3, 33.2, 34.3, 35.2]                     # 17-21 连续修复
    else:
        closes += [31.5, 32.3, 31.5, 31.0, 30.5]                     # 修复失败，MA 走坏
    closes = closes[:n]
    bars = []
    vol = 2_000_000.0
    for i, close in enumerate(closes):
        high = close * 1.02 if i == 10 else close * 1.01
        # shrink_vol：全程缩量；否则恒量（不缩量）
        vol = max(400_000.0, vol * 0.93) if shrink_vol else 2_000_000.0
        bars.append((f"2026-08-{i + 1:02d}", high, close, vol))
    return bars


def _insert(conn, sym, bars):
    for dt, high, close, vol in bars:
        conn.execute(
            "INSERT OR REPLACE INTO daily_kline"
            " (symbol, date, open, close, high, low, volume, percent)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (sym, dt, close, close, high, close, vol, 0.0),
        )


def _entry(sym="SZ300001", rec_date="2026-08-23", category="new_face",
           first_push=False):
    e = {"symbol": sym, "date": rec_date, "category": category,
         "score": 40, "score_breakdown": {}}
    if first_push:
        e["score_breakdown"] = {"first_today_bonus": 3}
    return e


class TestIsBreakoutSetup:
    def test_positive_new_face(self):
        assert _is_breakout_setup(_entry(), accum=2.0, klines=_bars()) is True

    def test_positive_first_push_other_category(self):
        # 非 new_face 类别但首推（first_today_bonus>0）同样命中
        assert _is_breakout_setup(_entry(category="short_term", first_push=True),
                                  accum=2.0, klines=_bars()) is True

    def test_reject_non_new_face_without_first_push(self):
        assert _is_breakout_setup(_entry(category="momentum"),
                                  accum=2.0, klines=_bars()) is False

    def test_reject_accum_too_high(self):
        assert _is_breakout_setup(_entry(), accum=8.0, klines=_bars()) is False

    def test_reject_accum_missing(self):
        # 观察标记 fail-closed：累计缺失不标（与 🎯 的 fail-open 不同）
        assert _is_breakout_setup(_entry(), accum=None, klines=_bars()) is False

    def test_reject_no_shrink_volume(self):
        assert _is_breakout_setup(_entry(), accum=2.0,
                                  klines=_bars(shrink_vol=False)) is False

    def test_reject_ma_broken(self):
        assert _is_breakout_setup(_entry(), accum=2.0,
                                  klines=_bars(ma_bull=False)) is False

    def test_reject_insufficient_bars(self):
        assert _is_breakout_setup(_entry(), accum=2.0,
                                  klines=_bars(n=15)) is False

    def test_reject_no_klines_no_conn(self):
        assert _is_breakout_setup(_entry(), accum=2.0, klines=None) is False

    def test_single_symbol_conn_fallback(self):
        conn = _mk_db()
        _insert(conn, "SZ300001", _bars())
        e = _entry()
        assert _is_breakout_setup(e, conn=conn, accum=2.0) is True

    def test_pullback_out_of_range(self):
        # 全程新高无回调（距20日高点≈0）→ 回撤窗口外不标
        bars = []
        for i in range(22):
            close = 30.0 * (1.0 + 0.005 * i)
            bars.append((f"2026-08-{i + 1:02d}", close * 1.01, close,
                         max(400_000.0, 2_000_000.0 * 0.93 ** i)))
        assert _is_breakout_setup(_entry(), accum=2.0, klines=bars) is False


class TestIsRelistBreakoutSetup:
    """⚡R 重上榜蓄势观察：非首推 short_term + 共用结构条件（肯特股份 2026-08-21 案例）。

    与 ⚡ 的唯一差异是类别门；结构条件必须与 _is_breakout_setup 同源同结果，
    防两画像口径漂移。
    """

    def test_positive_short_term_not_first_push(self):
        # 肯特型：short_term 且非首推（首推门不触发）→ 结构条件全过 → 标
        assert _is_relist_breakout_setup(
            _entry(category="short_term"), accum=2.0, klines=_bars()) is True

    def test_negative_first_push_short_term_belongs_to_bo(self):
        # 首推 short_term 归 ⚡ 管辖（首推门已覆盖），⚡R 不重复打点（按构造不相交）
        e = _entry(category="short_term", first_push=True)
        assert _is_relist_breakout_setup(e, accum=2.0, klines=_bars()) is False
        assert _is_breakout_setup(e, accum=2.0, klines=_bars()) is True

    def test_negative_other_categories(self):
        # momentum 掉榜重上暂不纳入（样本达标后经复盘再评估放宽）；
        # new_face/kNF 由 ⚡ 覆盖
        for cat in ("momentum", "new_face", "known_new_face", "rebound", "comeback"):
            assert _is_relist_breakout_setup(
                _entry(category=cat), accum=2.0, klines=_bars()) is False, cat

    def test_structure_parity_with_breakout_setup(self):
        """同一 K 线/累计下，除类别门外判定结果必须完全一致（单源结构共用）。"""
        bars = _bars()
        cases = [(2.0, True), (8.0, False), (None, False)]
        for accum, expected in cases:
            bo = _is_breakout_setup(_entry(category="new_face"),
                                    accum=accum, klines=bars)
            relist = _is_relist_breakout_setup(_entry(category="short_term"),
                                               accum=accum, klines=bars)
            assert bo is relist is expected, accum

    def test_structure_shared_fail_closed(self):
        # 缩量/MA/回撤任一破坏 → 与 ⚡ 同拒（共用 _breakout_structure_ok）
        assert _is_relist_breakout_setup(
            _entry(category="short_term"), accum=2.0,
            klines=_bars(shrink_vol=False)) is False
        assert _is_relist_breakout_setup(
            _entry(category="short_term"), accum=2.0,
            klines=_bars(ma_bull=False)) is False
        assert _is_relist_breakout_setup(
            _entry(category="short_term"), accum=2.0,
            klines=_bars(n=15)) is False

    def test_single_symbol_conn_fallback(self):
        conn = _mk_db()
        _insert(conn, "SZ300001", _bars())
        assert _is_relist_breakout_setup(
            _entry(category="short_term"), conn=conn, accum=2.0) is True

    def test_not_in_sort_tier(self):
        """⚡R 是观察标记：不得影响档位排序。"""
        from scanner.ranking import _entry_tier
        conn = _mk_db()
        _insert(conn, "SZ300001", _bars())
        e = _entry(category="short_term")
        e["_candidate"] = None
        assert _is_relist_breakout_setup(e, conn, accum=2.0) is True
        # short_term 无警示无 🎯 → 档2，不被 ⚡R 提升
        assert _entry_tier(e, conn, accum=2.0) == 2


class TestBuildBreakoutKlineMap:
    def test_filters_future_dates_and_cleans_dirty_rows(self):
        conn = _mk_db()
        bars = _bars()
        _insert(conn, "SZ300001", bars)
        # 推荐日当日 bar 与未来日期必须被滤除；脏行（close<=0/NaN）剔除
        conn.execute(
            "INSERT INTO daily_kline VALUES ('SZ300001','2026-08-23',1,1,1,1,1,0)")
        conn.execute(
            "INSERT INTO daily_kline VALUES ('SZ300001','2026-08-24',1,-5,1,1,1,0)")
        entries = [_entry()]
        kmap = build_breakout_kline_map(conn, entries)
        rows = kmap["SZ300001"]
        assert all(r[0] < "2026-08-23" for r in rows)
        assert len(rows) == len(bars)

    def test_empty_entries(self):
        conn = _mk_db()
        assert build_breakout_kline_map(conn, []) == {}

    def test_result_feeds_marker_end_to_end(self):
        conn = _mk_db()
        _insert(conn, "SZ300001", _bars())
        entries = [_entry()]
        kmap = build_breakout_kline_map(conn, entries)
        assert _is_breakout_setup(entries[0], accum=2.0,
                                  klines=kmap["SZ300001"]) is True


class TestBreakoutNotInSortTier:
    """⚡ 是观察标记：不得影响档位排序（用户决策：先观察，不改排序位置）。"""

    def test_tier_ignores_breakout_profile(self):
        from scanner.ranking import _entry_tier
        conn = _mk_db()
        _insert(conn, "SZ300001", _bars())
        e = _entry()
        e["_candidate"] = None
        kmap = build_breakout_kline_map(conn, [e])
        marked = _is_breakout_setup(e, conn, accum=2.0,
                                    klines=kmap["SZ300001"])
        assert marked is True  # 命中画像……
        # ……但档位仍按既有规则（new_face 无警示 → 档2），不被 ⚡ 提升
        assert _entry_tier(e, conn, accum=2.0) == 2
