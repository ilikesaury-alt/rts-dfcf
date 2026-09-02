"""回马枪（comeback）模块单元测试：掉榜跟踪池 + 反转/回踩变体。"""
import sqlite3
from datetime import timedelta

import scanner.comeback as cb
from scanner.config import now_beijing
from scanner.database import (
    get_watch_symbols,
    mark_watch_evaluated,
    prune_watch_pool,
    upsert_watch_symbol,
    upsert_watch_symbols,
)
from scanner.models import KlineSummary, StockInfo

# ── 内部工具函数 ──


def _stock(symbol="SZ300986", name="志特新材", percent=4.0, current=12.0):
    return StockInfo(symbol=symbol, name=name, code=symbol[2:],
                     percent=percent, current=current, value=0.0,
                     rank_change=0, rank=0, source_tag="comeback")


def _hist(closes, volumes):
    """构造历史 K 线（不含今日 bar），volume 缺失补 1.0。"""
    out = []
    for i, c in enumerate(closes):
        out.append({"date": f"2026-06-{i+1:02d}", "open": c,
                    "close": c, "high": c * 1.02, "low": c * 0.98,
                    "volume": volumes[i] if i < len(volumes) else 1.0,
                    "percent": 0.0})
    return out


def _in_mem_db():
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE watch_pool (
        symbol TEXT PRIMARY KEY, name TEXT NOT NULL, added_date TEXT NOT NULL,
        last_list_date TEXT NOT NULL, last_eval_date TEXT, over_limit INTEGER DEFAULT 0)""")
    conn.execute("""CREATE TABLE recommendations (
        id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL, time TEXT NOT NULL,
        symbol TEXT NOT NULL, name TEXT NOT NULL, category TEXT NOT NULL, score INTEGER NOT NULL,
        percent REAL, trend TEXT, next_day_pct REAL, fwd_3d REAL, fwd_5d REAL,
        score_breakdown TEXT, source TEXT DEFAULT 'xueqiu', concept TEXT, accumulated_pct REAL)""")
    conn.execute("""CREATE TABLE daily_kline (
        symbol TEXT NOT NULL, date TEXT NOT NULL, open REAL, close REAL,
        high REAL, low REAL, volume REAL, percent REAL, finalized INTEGER DEFAULT 1)""")
    conn.execute("""CREATE TABLE market_extra_cache (
        symbol TEXT NOT NULL, date TEXT NOT NULL, data_type TEXT NOT NULL,
        payload_json TEXT NOT NULL, updated TEXT NOT NULL, PRIMARY KEY(symbol, data_type))""")
    return conn


# ── 反转预过滤：5 日跌幅 ──
class TestDropPrefilter:

    def test_deep_drop_passes(self):
        k = _hist([100, 97, 94, 91, 88, 85], [])
        assert cb._passes_drop_prefilter(k, "2026-08-07") is True

    def test_shallow_drop_rejected(self):
        k = _hist([100, 99.5, 99, 98.5, 98, 97.5], [])
        assert cb._passes_drop_prefilter(k, "2026-08-07") is False

    def test_insufficient_data_rejected(self):
        k = _hist([100, 98], [])
        assert cb._passes_drop_prefilter(k, "2026-08-07") is False

    def test_none_rejected(self):
        assert cb._passes_drop_prefilter(None, "2026-08-07") is False

    def test_today_bar_excluded(self):
        # 历史 5 日跌超阈值，今日大涨 bar 不应计入跌幅计算
        hist = _hist([100, 95, 90, 85, 80, 76], [])
        hist.append({"date": "2026-08-07", "open": 76, "close": 88,
                     "high": 90, "low": 76, "volume": 3.0, "percent": 15.0})
        assert cb._passes_drop_prefilter(hist, "2026-08-07") is True


# ── 资金流硬过滤：fail-open ──
class TestFundFlowFilter:

    def test_no_data_pass(self):
        assert cb._passes_fund_flow_filter({}, "SZ300986") is True
        assert cb._passes_fund_flow_filter({"SZ300000": 6.0}, "SZ300986") is True

    def test_outflow_rejected(self):
        assert cb._passes_fund_flow_filter({"SZ300986": -6.0}, "SZ300986") is False

    def test_mild_flow_pass(self):
        assert cb._passes_fund_flow_filter({"SZ300986": -4.0}, "SZ300986") is True
        assert cb._passes_fund_flow_filter({"SZ300986": 3.0}, "SZ300986") is True


# ── 买点信号 ──
class TestBuySignals:

    def _consolidation(self):
        # 上行至 13.8 后温和回调，在上升 MA20 附近企稳 + 缩量：应到买点（≥3 信号，5维）
        base = [10.0 + 0.2 * i for i in range(20)]  # 10 -> 13.8
        tail = [13.6, 13.5, 13.4, 13.45, 13.5, 13.45, 13.55, 13.5, 13.45, 13.5, 13.6]
        closes = base + tail
        vols = [1.0] * len(closes)
        vols[-1] = 0.35
        return closes, vols

    def test_consolidation_above_ma20_buy_point(self):
        closes, vols = self._consolidation()
        status, count, signals = cb._evaluate_buy_signals(_hist(closes, vols))
        assert status == "到买点", f"expected 到买点, got {status}({signals})"
        assert count >= 3  # P2: 合并MA20支撑+未破位→均线支撑后，阈值从4降为3

    def test_broken_trend_no_buy_point(self):
        # 持续阴跌 + 放量：破位，不应到买点
        closes = [100, 98, 96, 94, 92, 90, 88, 86, 84, 82,
                  80, 78, 76, 74, 72, 70, 68, 66, 64, 62, 60]
        vols = [1.0] * 20 + [2.5]
        hist = _hist(closes, vols)
        status, count, _ = cb._evaluate_buy_signals(hist)
        assert status == ""
        assert count < 4

    def test_too_few_bars(self):
        assert cb._evaluate_buy_signals(_hist([100, 101], [])) == ("", 0, [])


# ── 反转候选：接线（monkeypatch analyze_rebound/validate 控制行为）──
class TestReboundCandidate:

    def _ks(self, score=30):
        return KlineSummary(trend="超跌企稳", accumulated_pct=-12.0,
                            volume_ratio=1.5, bottom_confirmed=True,
                            score=score, dimensions={})

    def test_passes_sets_variant_and_trend(self, monkeypatch):
        ks = self._ks()
        monkeypatch.setattr(cb, "analyze_rebound",
                            lambda stock, kline, today_str=None, off_list=False: ks)
        monkeypatch.setattr(cb, "validate",
                            lambda *a, **k: (True, 5, {"v_rb_oversold": 8}))
        stock = _stock()
        kline = [{"date": "2026-07-20", "percent": -5.0, "close": 10.0}]
        c = cb._try_rebound_candidate(stock, kline, "2026-08-07", None)
        assert c is not None
        assert c.category == "comeback"
        assert c.off_list is True
        assert c.comeback_variant == "反转"
        assert c.kline.trend.startswith("反转·")
        assert c.kline.score == 30  # 2026-08-10: validation_bonus 只做门禁不进 score

    def test_off_list_flag_passed_to_analysis(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(cb, "analyze_rebound",
                            lambda stock, kline, today_str=None, off_list=False:
                            seen.update(off_list=off_list) or self._ks())
        monkeypatch.setattr(cb, "validate",
                            lambda *a, **k: (True, 0, {}))
        cb._try_rebound_candidate(_stock(), [], "2026-08-07", None)
        assert seen.get("off_list") is True

    def test_low_score_rejected(self, monkeypatch):
        monkeypatch.setattr(cb, "analyze_rebound",
                            lambda *a, **k: self._ks(score=5))
        assert cb._try_rebound_candidate(_stock(), [], "2026-08-07", None) is None

    def test_failed_validation_rejected(self, monkeypatch):
        monkeypatch.setattr(cb, "analyze_rebound",
                            lambda *a, **k: self._ks())
        monkeypatch.setattr(cb, "validate", lambda *a, **k: (False, 0, {}))
        assert cb._try_rebound_candidate(_stock(), [], "2026-08-07", None) is None


# ── 回踩候选：硬过滤 + 状态门禁 ──
class TestReentryCandidate:

    def _hist(self):
        # 与 TestBuySignals 同一组：上行后回调至 MA20 附近企稳 + 缩量
        base = [10.0 + 0.2 * i for i in range(20)]
        tail = [13.6, 13.5, 13.4, 13.45, 13.5, 13.45, 13.55, 13.5, 13.45, 13.5, 13.6]
        closes = base + tail
        vols = [1.0] * len(closes)
        vols[-1] = 0.35
        return _hist(closes, vols)

    def _rec(self, date="2026-07-28", category="new_face"):
        return {"symbol": "SZ300986", "name": "志特新材", "category": category,
                "score": 60, "percent": 5.0, "date": date}

    def test_buy_point_candidate(self, monkeypatch):
        hist = self._hist()
        monkeypatch.setattr(cb, "_evaluate_buy_signals",
                            lambda h: ("到买点", 5, ["MA20支撑", "缩量", "未破位", "RSI合理", "BOLL中轨"]))
        stock = _stock(percent=1.0, current=13.5)
        c = cb._try_reentry_candidate(stock, hist, "2026-08-07", self._rec(), {})
        assert c is not None
        assert c.category == "comeback"
        assert c.comeback_variant == "回踩"
        assert c.kline.trend == "回踩·到买点"
        assert c.kline.dimensions["comeback_buy_signals"] == 5

    def test_today_surge_filtered(self, monkeypatch):
        hist = self._hist()
        monkeypatch.setattr(cb, "_evaluate_buy_signals",
                            lambda h: ("到买点", 5, []))
        # 今日 +6% → 追高过滤
        stock = _stock(percent=6.0, current=13.5)
        assert cb._try_reentry_candidate(stock, hist, "2026-08-07", self._rec(), {}) is None

    def test_percent_none_no_crash(self, monkeypatch):
        """回归：stock.percent=None（行情脏字段）时硬过滤比较不能抛 TypeError，
        按 0.0% 处理（不触发追高/破位过滤，进入信号判定）。"""
        hist = self._hist()
        monkeypatch.setattr(cb, "_evaluate_buy_signals",
                            lambda h: ("到买点", 5, []))
        stock = _stock(percent=None, current=13.5)
        c = cb._try_reentry_candidate(stock, hist, "2026-08-07", self._rec(), {})
        assert c is not None

    def test_cum_gain_too_high_filtered(self, monkeypatch):
        hist = self._hist()
        monkeypatch.setattr(cb, "_evaluate_buy_signals",
                            lambda h: ("到买点", 5, []))
        # 推荐日收盘 ~13.6，今日 15.2 → 累计 +11.7% 已错过
        stock = _stock(percent=1.0, current=15.2)
        assert cb._try_reentry_candidate(stock, hist, "2026-08-07", self._rec(), {}) is None

    def test_fund_flow_outflow_filtered(self, monkeypatch):
        hist = self._hist()
        monkeypatch.setattr(cb, "_evaluate_buy_signals",
                            lambda h: ("到买点", 5, []))
        stock = _stock(percent=1.0, current=13.5)
        flow = {"SZ300986": -6.0}
        assert cb._try_reentry_candidate(stock, hist, "2026-08-07", self._rec(), flow) is None

    def test_not_buy_point_rejected(self, monkeypatch):
        hist = self._hist()
        monkeypatch.setattr(cb, "_evaluate_buy_signals",
                            lambda h: ("观察中", 3, ["缩量"]))
        stock = _stock(percent=1.0, current=13.5)
        assert cb._try_reentry_candidate(stock, hist, "2026-08-07", self._rec(), {}) is None

    def test_insufficient_kline_rejected(self):
        stock = _stock()
        hist = _hist([10.0, 10.1, 10.2], [])  # 不足 20 根
        assert cb._try_reentry_candidate(stock, hist,
                                         "2026-08-07", self._rec(), {}) is None


# ── 候选域收集：watch_pool ∪ 近 N 日推荐 ──
class TestCollectSymbols:

    def _seed(self, conn):
        today = now_beijing().date().isoformat()
        rec_date = (now_beijing().date() - timedelta(days=1)).isoformat()  # 落在近5交易日窗口内
        conn.executemany(
            "INSERT INTO watch_pool (symbol, name, added_date, last_list_date, last_eval_date, over_limit) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("SZ300986", "志特新材", "2026-07-09", "2026-07-09", None, 0),
                ("SZ300000", "已评估", "2026-07-09", "2026-07-09", today, 0),
                ("SZ300111", "超限票", "2026-07-31", "2026-07-31", None, 1),
            ],
        )
        conn.execute(
            "INSERT INTO recommendations (date, time, symbol, name, category, score, percent) "
            "VALUES (?, '10:00', 'SZ300222', '仅推荐票', 'momentum', 60, 3.0)",
            (rec_date,),
        )
        conn.commit()
        return today

    def test_excludes_on_list_and_evaluated(self):
        conn = _in_mem_db()
        today = self._seed(conn)
        metas = cb.collect_comeback_symbols(conn, today,
                                            on_list_symbols={"SZ300111"})
        syms = {m["symbol"] for m in metas}
        # 掉榜票在列；今日已评估/在榜票不在列
        assert "SZ300986" in syms
        assert "SZ300222" in syms  # 仅推荐票被纳入（随后 upsert 入池）
        assert "SZ300000" not in syms  # 今日已评估
        assert "SZ300111" not in syms  # 今日在榜

    def test_evaluated_marker_blocks_recollect(self):
        conn = _in_mem_db()
        today = self._seed(conn)
        cb.collect_comeback_symbols(conn, today, set())
        cb.mark_watch_evaluated(conn, ["SZ300986"])
        metas = cb.collect_comeback_symbols(conn, today, set())
        assert "SZ300986" not in {m["symbol"] for m in metas}


# ── 掉榜跟踪池 DB 函数 ──
class TestWatchPoolDb:

    def test_upsert_refreshes_last_list_date(self):
        conn = _in_mem_db()
        upsert_watch_symbol(conn, "SZ300986", "志特新材", last_list_date="2026-07-09")
        # 再次保活：last_list_date 取较新
        upsert_watch_symbol(conn, "SZ300986", "志特新材", last_list_date="2026-07-31")
        rows = get_watch_symbols(conn)
        assert rows[0]["last_list_date"] == "2026-07-31"
        assert rows[0]["symbol"] == "SZ300986"

    def test_over_limit_flag_sticky(self):
        conn = _in_mem_db()
        upsert_watch_symbol(conn, "SZ300986", "志特新材", over_limit=True)
        # 普通保活不降级 over_limit
        upsert_watch_symbol(conn, "SZ300986", "志特新材")
        assert get_watch_symbols(conn)[0]["over_limit"] == 1

    def test_batch_upsert(self):
        conn = _in_mem_db()
        upsert_watch_symbols(conn, [
            {"symbol": "SZ300986", "name": "志特新材"},
            {"symbol": "SZ300111", "name": "乙股", "over_limit": True},
        ])
        rows = {r["symbol"]: r for r in get_watch_symbols(conn)}
        assert set(rows) == {"SZ300986", "SZ300111"}
        assert rows["SZ300111"]["over_limit"] == 1

    def test_mark_evaluated_blocks_repeat(self):
        conn = _in_mem_db()
        upsert_watch_symbol(conn, "SZ300986", "志特新材")
        today = now_beijing().date().isoformat()
        mark_watch_evaluated(conn, ["SZ300986"])
        assert get_watch_symbols(conn)[0]["last_eval_date"] == today

    def test_prune_removes_old(self):
        conn = _in_mem_db()
        upsert_watch_symbol(conn, "SZ300986", "志特新材", last_list_date="2020-01-01")
        # 近期日期动态取（曾硬编码 2026-07-31，真实时钟越过 15 交易日后双条目被剪成时间炸弹）
        recent = now_beijing().date().isoformat()
        upsert_watch_symbol(conn, "SZ300111", "乙股", last_list_date=recent)
        n = prune_watch_pool(conn, keep_trading_days=15)
        assert n == 1
        assert [r["symbol"] for r in get_watch_symbols(conn)] == ["SZ300111"]


# ── 主入口：无候选/无 K 线时优雅空返回 ──
class TestEvaluateComeback:

    def _conn(self):
        conn = _in_mem_db()
        conn.execute(
            "INSERT INTO watch_pool (symbol, name, added_date, last_list_date, last_eval_date, over_limit) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("SZ300986", "志特新材", "2026-07-09", "2026-07-09", None, 0),
        )
        # 种子缓存 K 线（5 日深跌，过反转预筛），确保走到行情拉取阶段
        for i, c in enumerate([100, 97, 94, 91, 88, 85]):
            conn.execute(
                "INSERT INTO daily_kline (symbol, date, open, close, high, low, volume, percent) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("SZ300986", f"2026-07-{28 + i:02d}", c, c, c * 1.02, c * 0.98, 1.0, 0.0),
            )
        conn.commit()
        return conn

    class _Adapter:
        def fetch_market_caps_batch(self, symbols):
            return {s: {"current": 12.0, "percent": 4.0, "market_cap": 5_000_000_000}
                    for s in symbols}

    def test_no_kline_graceful(self):
        conn = self._conn()
        today = now_beijing().date().isoformat()
        rb, re_, quotes = cb.evaluate_comeback(
            conn, self._Adapter(),
            lambda stocks: {s.symbol: None for s in stocks},  # 拉取失败
            today, set(), None)
        assert rb == [] and re_ == []
        assert quotes

    def test_empty_pool(self):
        conn = _in_mem_db()
        today = now_beijing().date().isoformat()
        rb, re_, quotes = cb.evaluate_comeback(conn, self._Adapter(),
                                               lambda s: {}, today, set(), None)
        assert rb == [] and re_ == [] and quotes == {}

    def test_stale_kline_no_today_bar_not_marked(self):
        """回归（2026-08-20）：补拉返回 stale 缓存（truthy 但无今日 bar，如 deadline
        被榜上批次耗尽/单票拉取失败回退旧缓存）时，不得标记 last_eval_date=today——
        此前把池冻结（当日永不再评估、漏推荐）+ 用旧 K 线失真评分。"""
        conn = self._conn()
        today = now_beijing().date().isoformat()

        calls: list[list] = []

        def _stale_fetcher(stocks):
            # 无今日 bar 的旧 K 线（模拟补拉失败回退旧缓存）
            # 注意：不能用 {st.symbol: [...] for st in stocks}——comprehension 的
            # key 表达式在外层作用域求值，取不到 for 子句绑定的 st（NameError）。
            # 该笔误曾让本测试全程空转（NameError 被上游 except 吞掉后断言照样通过），
            # 故下方显式断言 fetcher 被真实调用，防止回归保护再次失效。
            calls.append(list(stocks))
            out = {}
            for st in stocks:
                out[st.symbol] = [
                    {"date": "2026-08-18", "open": 10, "close": 10.5, "high": 10.6,
                     "low": 9.9, "volume": 100, "percent": 0.5}
                ]
            return out

        rb, re_, _ = cb.evaluate_comeback(conn, self._Adapter(), _stale_fetcher,
                                          today, set(), None)
        # 防空转：fetcher 必须被真实调用且带票进来，否则下方断言毫无意义
        assert calls and any(calls), "补拉 fetcher 未被调用——本测试已失去保护意义"
        assert rb == [] and re_ == []
        # 不得标记已评估 → 下轮 KLINE_REFRESH_TTL 后可重试
        rows = {r["symbol"]: r for r in get_watch_symbols(conn)}
        assert rows["SZ300986"]["last_eval_date"] is None

    def test_filters_by_market_cap(self):
        conn = self._conn()
        today = now_beijing().date().isoformat()

        class BigCapAdapter:
            def fetch_market_caps_batch(self, symbols):
                return {s: {"current": 12.0, "percent": 4.0, "market_cap": 800 * 1e8}
                        for s in symbols}

        rb, re_, _ = cb.evaluate_comeback(conn, BigCapAdapter(),
                                          lambda s: {}, today, set(), None)
        assert rb == [] and re_ == []
