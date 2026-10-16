"""offboard_watch（沪深飙升区 **B 段·榜外异动**）单测。

覆盖：门槛（通用 8 门 + 本段专属收紧门）、候选构建（榜外/已推荐/非创业板过滤）、
T1/T2 分层、排序键（T2 在前的实测依据 + A/B 不混排的结构性理由）、榜外 K 线池
（**独立于 daily_kline**）、逐日落库（信号值冻结 + last_hit_time）与次日收益回填
（含两个防呆）、**开盘静默窗口**、主流程端到端、两出口渲染、CLI 离线自检。

全部为密封单测：不触网、不依赖真实 `scanner.db`（用内存库 + 假 adapter）。
"""

import json
import sqlite3
from datetime import date as _date
from datetime import datetime, time
from datetime import timedelta as _td
from typing import Any
from unittest.mock import patch

import pytest

from scanner.config import (
    HOT_MAX_MARKET_CAP,
    HOT_MAX_PERCENT,
    HOT_MIN_PERCENT,
    MAX_MARKET_CAP,
    MOMENTUM_LAUNCH_ACCUM_MIN,
    MOMENTUM_LAUNCH_TODAY_MAX,
    MOMENTUM_LAUNCH_TODAY_MIN,
    MOMENTUM_LAUNCH_VOL,
    OFFBOARD_ACCUM_MAX,
    OFFBOARD_DISPLAY_TOP,
    OFFBOARD_DWELL_MIN,
    OFFBOARD_KLINE_FETCH_LIMIT,
    OFFBOARD_MAIN_PCT_MIN,
    OFFBOARD_MIN_AMOUNT,
    OFFBOARD_MIN_FLOAT_CAP,
    OFFBOARD_OPENING_SILENCE_MIN,
    OFFBOARD_REPEAT_MIN_HITS,
    OFFBOARD_REPEAT_WINDOW_DAYS,
    OFFBOARD_T1_TODAY_MAX,
    OFFBOARD_T2_TODAY_MAX,
    TREND_MARK_ENABLED,
    now_beijing,
)
from scanner.db.migrations import run_migrations
from scanner.display_gates import UNIVERSAL_GATES, beauty_marks_daily, common_hard_gate
from scanner.offboard_watch import (
    T1,
    T2,
    OffboardCandidate,
    _apply_live_fund_flow,
    _apply_live_quote,
    _clean_bars,
    _read_cached_klines,
    _save_klines,
    _snapshot_row_to_candidate,
    annotate,
    backfill_next_day,
    build_candidates,
    classify_tier,
    load_offboard_klines,
    main,
    offboard_gate,
    opening_silence_active,
    persist_round,
    recent_repeat_counts,
    run_offboard_watch,
    sort_key,
)
from scanner.trading_session import is_trading_day

# ── 夹具 / 构造器 ────────────────────────────────────────────────────────────


def _cand(**kw) -> OffboardCandidate:
    """构造一个默认「各项健康」的榜外候选（创业板、放量、温和上涨、主力净流入）。"""
    base: dict[str, Any] = {
        "symbol": "SZ300101",
        "code": "300101",
        "name": "自检样本",
        "tier": T1,
        "current": 10.0,
        "percent": 2.5,
        "accum_5d": 3.0,
        "volume_ratio": 2.0,
        "main_pct": 2.0,
        "volume": 2.0e6,
        "amount": 8.0e7,
        "market_capital": 3.6e9,
        "float_market_capital": 3.0e9,
        "turnover_rate": 3.0,
        "exchange": "SZ",
        "status": 1,
        "ff_pct": 2.0,
    }
    base.update(kw)
    return OffboardCandidate(**base)


def _payload(**kw) -> dict:
    """全市场快照的一行（字段码见 market_extra._FUND_FLOW_FIELDS 的语义映射）。"""
    base = {
        "name": "自检样本",
        "price": 10.0,
        "percent": 2.5,
        "vol_ratio": 2.0,
        "main_pct": 2.0,
        "volume": 2.0e6,
        "amount": 8.0e7,
        "total_cap": 3.6e9,
        "float_cap": 3.0e9,
        "turnover": 3.0,
    }
    base.update(kw)
    return base


def _lin(start: float, end: float, n: int) -> list[float]:
    """等差序列（自检样本的日线收盘序列，保证可复现）。"""
    step = (end - start) / (n - 1) if n > 1 else 0.0
    return [round(start + step * i, 4) for i in range(n)]


def _bars(series: list[float], today: str, volume: float = 2.0e6) -> list[dict]:
    """收盘序列（**末位 = 今日**）→ 合法日线 bar 列表（日期倒推，末根 date == today）。

    日期用自然日倒推：本模块的三个 K 线判定（5 日累计 / MA / 顶背离）都只看**顺序**，
    不看交易日间隔；真正需要「相邻交易日」语义的地方是 `backfill_next_day`，那里
    另有 ≤4 自然日的防呆（见 _bars_by_date 的用例）。
    """
    end = _date.fromisoformat(today)
    out = []
    for i, close in enumerate(series):
        prev = series[i - 1] if i else close
        out.append(
            {
                "date": (end - _td(days=len(series) - 1 - i)).isoformat(),
                "open": round(prev, 4),
                "close": round(close, 4),
                "high": round(max(close, prev) * 1.004, 4),
                "low": round(min(close, prev) * 0.996, 4),
                "volume": volume,
                "percent": round((close - prev) / prev * 100.0, 4) if prev else 0.0,
            }
        )
    return out


def _bars_by_date(pairs: list[tuple[str, float]], volume: float = 2.0e6) -> list[dict]:
    """[(date, close)] → bar 列表（显式日期，供回填的日期缺口用例）。"""
    out = []
    for i, (d, close) in enumerate(pairs):
        prev = pairs[i - 1][1] if i else close
        out.append(
            {
                "date": d,
                "open": round(prev, 4),
                "close": round(close, 4),
                "high": round(max(close, prev) * 1.004, 4),
                "low": round(min(close, prev) * 0.996, 4),
                "volume": volume,
                "percent": 0.0,
            }
        )
    return out


# 平滑上行 23 根 + 今日一根（5 日累计 ≈ +2.6%、MA 多头）
_UP_SERIES = [*_lin(9.0, 10.15, 23), 10.4]
# 前 18 根横盘 + 末 6 根急拉（5 日累计 ≈ +22%，超出 [0, 7)）
_ACCUM_HIGH_SERIES = [*_lin(9.0, 9.0, 18), *_lin(9.0, 11.5, 6)]
# 不足 20 根 → MA 不可判定
_SHORT_SERIES = _lin(10.0, 10.3, 12)
# 不足 6 根有效收盘 → 5 日累计不可算
_TINY_SERIES = _lin(10.0, 10.1, 6)


@pytest.fixture
def db():
    """内存库：B 段四张表（生产迁移单源）+ 被断言「不得被写入」的两张主线表。"""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE appearances (symbol TEXT, name TEXT, date TEXT)")
    conn.execute("CREATE TABLE market_extra_cache (symbol TEXT, data_type TEXT, date TEXT, payload_json TEXT)")
    conn.execute("CREATE TABLE recommendations (symbol TEXT, date TEXT)")
    conn.execute("CREATE TABLE daily_kline (symbol TEXT, date TEXT, close REAL)")
    run_migrations(conn, verbose=False)
    conn.commit()
    yield conn
    conn.close()


@pytest.fixture(autouse=True)
def _offline_defaults(monkeypatch):
    """单测离线默认值：关开盘静默窗口 + 关板块列 F10 补拉。

    ① 默认关闭开盘静默窗口，防 flaky：本文件绝大多数用例不关心这道门，但
      `run_offboard_watch` 会按**真实时钟**判定 —— 测试若在 09:45 前跑，整批会
      返回 []（CI 在任意时刻触发，典型 flaky）。窗口边界由 `TestOpeningSilence`
      显式开关来验。
    ② 关 `OFFBOARD_BOARD_FETCH`（2026-09-22，「板块」列）：生产默认开，但本文件
      的库是内存库、无 `concept_cache` ⇒ ②级必然 miss ⇒ 不关就会真去打东财 F10
      （慢，且让单测依赖外网）。关掉后降级链走③名称关键词，正是离线该有的行为。
    """
    monkeypatch.setattr("scanner.offboard_watch.OFFBOARD_OPENING_SILENCE_MIN", 0)
    monkeypatch.setattr("scanner.offboard_watch.OFFBOARD_BOARD_FETCH", 0)


class _FakeKlineAdapter:
    """假数据源：只实现 B 段用到的 fetch_kline，并记录调用（并发下由测试串行断言）。"""

    def __init__(self, bars=None, fail=()):
        self._bars = bars or {}
        self._fail = set(fail)
        self.calls: list[str] = []

    def fetch_kline(self, symbol, days=60):
        self.calls.append(symbol)
        if symbol in self._fail:
            raise OSError("network down")
        return list(self._bars.get(symbol, []))


class _FakeQuoteAdapter(_FakeKlineAdapter):
    """假数据源：在 K 线能力之上补 `fetch_hot_quotes_batch`（B 段实时行情补全用）。"""

    def __init__(self, quotes=None, bars=None, fail=()):
        super().__init__(bars=bars, fail=fail)
        self._quotes = quotes or {}

    def fetch_hot_quotes_batch(self, symbols):
        return {s: self._quotes[s] for s in symbols if s in self._quotes}


# ── 单源派生（不许复制常量）──────────────────────────────────────────────────


def test_tier_bands_are_derived_not_copied():
    """T1/T2 的涨幅带必须派生自既有启动口径（MOMENTUM_LAUNCH_*），不得写字面量。

    这是「同一套阈值的第二个定义 = 同名不同义」的守卫：有人把 3.5 / 8.0 / 7.0 抄成
    常量时，这里会红。
    """
    assert OFFBOARD_T1_TODAY_MAX == MOMENTUM_LAUNCH_TODAY_MIN  # T1/T2 分界 = 启动定义下沿
    assert min(MOMENTUM_LAUNCH_TODAY_MAX, HOT_MAX_PERCENT) == OFFBOARD_T2_TODAY_MAX
    # 有意收紧：整条带的上界跟本区涨幅带（7.0）而不是启动定义的上界（8.0）
    assert OFFBOARD_T2_TODAY_MAX < MOMENTUM_LAUNCH_TODAY_MAX
    assert OFFBOARD_T2_TODAY_MAX == HOT_MAX_PERCENT
    assert MOMENTUM_LAUNCH_ACCUM_MIN == 0.0  # 5 日累计下界沿用启动定义
    # ⚠ 上界**有意不沿用**：`MOMENTUM_LAUNCH_ACCUM_MAX` 被主线 analysis.py 的 momentum
    # 桶消费，B 段单方面放宽它 = 动主线推荐池。故另立 OFFBOARD_ACCUM_MAX（B 段私有）。
    # 这条断言是「有人图省事把两者合并回去」的哨兵。
    from scanner.config_scoring import MOMENTUM_LAUNCH_ACCUM_MAX

    assert OFFBOARD_ACCUM_MAX != MOMENTUM_LAUNCH_ACCUM_MAX  # 显式分叉，不是抄一份
    assert OFFBOARD_ACCUM_MAX > MOMENTUM_LAUNCH_ACCUM_MAX  # B 段上界放宽（2026-09-22）


def test_region_tightens_market_cap_relative_to_mainline():
    """区域可收紧不可放宽：飙升区市值上限(300 亿)比主线(500 亿)更严，且必须显式传参。"""
    assert HOT_MAX_MARKET_CAP < MAX_MARKET_CAP
    cap = 4.0e10  # 400 亿：主线放行、本区拒绝
    assert common_hard_gate(name="样本", code="300101", current=10.0, market_cap=cap) is None
    assert "市值过大" in offboard_gate(_cand(market_capital=cap))


# ── 门槛：通用 8 门（三区同一实现）────────────────────────────────────────────


def test_gate_passes_healthy_candidate():
    assert offboard_gate(_cand()) is None


def test_gate_covers_every_universal_gate_keyword():
    """B 段确实走了通用门的全部 8 道（不是「看起来调了一个同名函数」）。

    关键字表来自 `display_gates.UNIVERSAL_GATES` 单源；本区用**显式传参收紧**市值，
    其余门与主线逐字共用。少一道就说明本区悄悄放宽了风控。
    """
    variants = {
        "ST": _cand(name="*ST自检"),
        "非创业板": _cand(code="600519", exchange="SH"),
        "非正常交易状态": _cand(status=0),
        "无有效报价": _cand(current=0.0),
        "无成交量": _cand(volume=0.0),
        "价格过高": _cand(current=260.0),
        "市值过大": _cand(market_capital=HOT_MAX_MARKET_CAP * 2),
        "主力净流出": _cand(main_pct=-9.0),
    }
    assert set(variants) == set(UNIVERSAL_GATES)
    for keyword, c in variants.items():
        reason = offboard_gate(c)
        assert reason and keyword in reason, f"通用门未命中：{keyword} → {reason}"


def test_gate_rejects_symbol_without_exchange_prefix():
    """symbol 缺交易所前缀 → exchange 解析成 "30" → 样本面第二道（is_hot_universe）兜住。

    通用门的 `is_gem` 只看代码位数，放过这种畸形 symbol；本区额外套一道
    `hot_watch.is_hot_universe`（与 A 段同一实现）把交易所维度补齐。
    """
    assert "非创业板" in offboard_gate(_cand(exchange="30"))


# ── 门槛：本段专属（方向 = 收紧）─────────────────────────────────────────────


def test_gate_rejects_below_min_amount():
    assert "成交额不足" in offboard_gate(_cand(amount=OFFBOARD_MIN_AMOUNT - 1))
    assert offboard_gate(_cand(amount=OFFBOARD_MIN_AMOUNT)) is None  # 闭区间


def test_gate_rejects_small_float_cap():
    assert "流通市值过小" in offboard_gate(_cand(float_market_capital=OFFBOARD_MIN_FLOAT_CAP - 1))
    assert offboard_gate(_cand(float_market_capital=OFFBOARD_MIN_FLOAT_CAP)) is None


def test_gate_rejects_low_volume_ratio():
    """量比门 = MOMENTUM_LAUNCH_VOL（T1/T2 的共性条件），提前施加省下白补的 K 线。"""
    assert "量比不足" in offboard_gate(_cand(volume_ratio=MOMENTUM_LAUNCH_VOL - 0.01))
    assert offboard_gate(_cand(volume_ratio=MOMENTUM_LAUNCH_VOL)) is None


def test_gate_percent_band_is_closed_on_both_ends():
    """涨幅带 (HOT_MIN_PERCENT, OFFBOARD_T2_TODAY_MAX]：不追高、不接跌。"""
    assert "当前非上涨状态" in offboard_gate(_cand(percent=HOT_MIN_PERCENT))
    assert "当前非上涨状态" in offboard_gate(_cand(percent=-2.0))
    assert "涨幅过高" in offboard_gate(_cand(percent=OFFBOARD_T2_TODAY_MAX + 0.01))
    assert offboard_gate(_cand(percent=OFFBOARD_T2_TODAY_MAX)) is None


def test_gate_t1_band_upper_bound_is_t2_lower_bound():
    """两层涨幅带互斥且穷尽（T1 < TODAY_MIN ≤ T2），不存在既非 T1 也非 T2 的漏口。"""
    assert OFFBOARD_T1_TODAY_MAX == MOMENTUM_LAUNCH_TODAY_MIN
    assert OFFBOARD_T2_TODAY_MAX > OFFBOARD_T1_TODAY_MAX


def test_gate_order_st_before_universe():
    """ST 判定优先于样本面（ST 的科创板票报 ST，不报"非创业板"）。"""
    assert "ST" in offboard_gate(_cand(name="ST某某", code="688260", exchange="SH"))


# ── 候选构建（快照 → 榜外候选）───────────────────────────────────────────────


def test_build_candidates_filters_in_board_and_recommended_and_non_gem():
    """三类结构性排除：在榜（A 段的事）/ 今日已推荐 / 非创业板。"""
    snapshot = {
        "SZ300101": _payload(),
        "SH600519": _payload(name="贵州茅台"),
        "SZ000001": _payload(name="平安银行"),
        "SZ301104": _payload(),
    }
    board = [{"symbol": "SZ300102"}]  # 在榜票压根不进 snapshot，另测
    cands, rejects = build_candidates(snapshot, board, {}, {"SZ301104"})
    assert [c.code for c in cands] == ["300101"]
    assert rejects == []  # 结构性排除不算「门槛剔除」，不污染落选明细


def test_build_candidates_skips_board_symbols():
    snapshot = {"SZ300102": _payload(), "SZ300103": _payload()}
    cands, rejects = build_candidates(snapshot, [{"symbol": "SZ300102"}], {}, None)
    assert [c.code for c in cands] == ["300103"]
    assert rejects == []


def test_build_candidates_ignores_non_gem_prefix_before_gating():
    """零成本前缀过滤：非 300/301 不进门槛（5000+ 行全跑浮点比较是纯浪费）。"""
    snapshot = {"SZ002443": _payload(), "SH688260": _payload(), "SZ300101": _payload()}
    cands, rejects = build_candidates(snapshot, [], {}, None)
    assert [c.code for c in cands] == ["300101"]
    assert rejects == []


def test_build_candidates_skips_empty_payload():
    snapshot = {"SZ300101": {}, "SZ300102": _payload()}
    cands, _ = build_candidates(snapshot, [], {}, None)
    assert [c.code for c in cands] == ["300102"]


def test_build_candidates_sorted_by_volume_ratio_desc():
    snapshot = {
        "SZ300101": _payload(vol_ratio=1.6),
        "SZ300102": _payload(vol_ratio=3.2),
        "SZ300103": _payload(vol_ratio=2.4),
    }
    cands, _ = build_candidates(snapshot, [], {}, None)
    assert [c.code for c in cands] == ["300102", "300103", "300101"]


def test_build_candidates_reports_gate_rejections_with_reason():
    snapshot = {"SZ300101": _payload(vol_ratio=1.0), "SZ300102": _payload(amount=1e6)}
    cands, rejects = build_candidates(snapshot, [], {}, None)
    assert cands == []
    reasons = dict(rejects)
    assert "量比不足" in reasons["SZ300101"]
    assert "成交额不足" in reasons["SZ300102"]


def test_snapshot_row_maps_fields_and_falls_back_name():
    """快照字段 → 候选字段；名称缺失时依次回落 appearances → 6 位代码。"""
    c = _snapshot_row_to_candidate("SZ300101", _payload(name=""), {"SZ300101": "回退名"})
    assert c.name == "回退名"
    assert c.exchange == "SZ"
    assert c.market_capital == pytest.approx(3.6e9)  # total_cap
    assert c.float_market_capital == pytest.approx(3.0e9)  # float_cap
    assert c.turnover_rate == pytest.approx(3.0)  # turnover
    assert c.volume_ratio == pytest.approx(2.0)  # vol_ratio
    assert c.ff_pct == pytest.approx(2.0)  # main_pct → 行尾标记用

    c2 = _snapshot_row_to_candidate("SZ300102", _payload(name=""), {})
    assert c2.name == "300102"  # 两个来源都缺 → 回落代码，不留空名


def test_snapshot_row_tolerates_dirty_values():
    """脏值不崩整轮：None/NaN → 0；字符串数字仍按 `utils.to_float` 解析（宽容）。"""
    c = _snapshot_row_to_candidate(
        "SZ300101",
        _payload(vol_ratio=None, amount=float("nan"), total_cap="3.6e9", float_cap=None),
        {},
    )
    assert c.volume_ratio == 0.0
    assert c.amount == 0.0
    assert c.market_capital == pytest.approx(3.6e9)  # to_float 认字符串数字
    assert c.float_market_capital == 0.0  # None → 0（缺市值只影响「市值过大」门，fail-open 放过）


# ── T1 / T2 分层 ────────────────────────────────────────────────────────────


def test_classify_tier_t1():
    today = now_beijing().date().isoformat()
    c = _cand(percent=2.5, main_pct=2.0)
    tier, why = classify_tier(c, _bars(_UP_SERIES, today), today)
    assert tier == T1
    assert "量先动" in why
    assert c.accum_5d == pytest.approx(2.64, abs=0.05)  # 5 日累计已回写到行上


def test_classify_tier_t2_at_band_lower_bound():
    """涨幅恰为 3.5（= TODAY_MIN）→ T2（闭区间下界）。"""
    today = now_beijing().date().isoformat()
    tier, why = classify_tier(_cand(percent=MOMENTUM_LAUNCH_TODAY_MIN), _bars(_UP_SERIES, today), today)
    assert tier == T2
    assert "启动首日" in why


def test_classify_tier_t1_just_below_bound():
    """涨幅略低于 3.5 → T1（两层带互斥、无缝隙）。"""
    today = now_beijing().date().isoformat()
    tier, _ = classify_tier(_cand(percent=MOMENTUM_LAUNCH_TODAY_MIN - 0.01), _bars(_UP_SERIES, today), today)
    assert tier == T1


def test_classify_tier_no_klines_is_fail_closed():
    """K 线缺失 → 不产出（**fail-closed**，与风险门的 fail-open 语义相反）。"""
    today = now_beijing().date().isoformat()
    tier, why = classify_tier(_cand(), None, today)
    assert tier is None
    assert "无K线数据" in why


def test_classify_tier_too_few_closes_for_accum():
    today = now_beijing().date().isoformat()
    tier, why = classify_tier(_cand(), _bars(_TINY_SERIES, today), today)
    assert tier is None
    assert "K线不足6根" in why


def test_classify_tier_accum_out_of_range():
    today = now_beijing().date().isoformat()
    tier, why = classify_tier(_cand(), _bars(_ACCUM_HIGH_SERIES, today), today)
    assert tier is None
    assert "5日累计" in why
    assert f"[{MOMENTUM_LAUNCH_ACCUM_MIN:g},{OFFBOARD_ACCUM_MAX:g})" in why


def test_classify_tier_t2_uses_the_same_gate_as_t1(monkeypatch):
    """T2 与 T1 **共用同一条 MA 门**（2026-09-22 取消 09-21 的按层分化）。

    两个方向的哨兵，缺一不可：

    ① 门还在：桩 `_ma_not_bearish` → False，T1/T2 **都**不产出，且理由串**逐字相同**
       —— 若有人给 T2 另开一条分支（恢复分化），两串会分叉，这里会红。
    ② 门是「非空头」不是「完全多头」：见
       `test_partial_bull_series_separates_the_two_ma_predicates`（单点区分力证明）与
       离线自检 300120（端到端形态）。

    曾经的写法是桩 `trend_beauty.ma_bullish` 并断言 `why == "MA未完全多头"` ——
    该桩在两层统一后已**不再被调用**，桩掉它对结果毫无影响，用例会假绿。
    """
    import scanner.offboard_watch as ow

    today = now_beijing().date().isoformat()
    bars = _bars(_UP_SERIES, today)

    monkeypatch.setattr(ow, "_ma_not_bearish", lambda kline: False)
    t1, r1 = classify_tier(_cand(percent=2.5), bars, today)
    t2, r2 = classify_tier(_cand(percent=MOMENTUM_LAUNCH_TODAY_MIN, volume_ratio=2.4), bars, today)
    assert (t1, r1) == (t2, r2), "两层的 MA 拒绝理由必须一致（说明是同一条门）"
    assert (t1, r1) == (None, "MA空头排列(MA5<=MA10)")


def test_partial_bull_series_separates_the_two_ma_predicates():
    """「部分多头」样本必须对两条 MA 判据给出**相反结论** —— 否则「同门」这件事无从验证。

    `_ma_not_bearish`（现用，MA5>MA10）= True；`trend_beauty.ma_bullish`（旧的完全
    头门，MA5>MA10>MA20）= False。这是「T2 被改回完全多头」最有区分力的单点证据：
    离线自检 300120 是它的端到端形态，这里做单点复核，把失败面缩到两个纯函数上。

    若哪天两条判据在该样本上变得等价，说明**样本失去区分力** —— 应该换样本，
    而不是删掉断言。
    """
    from scanner.offboard_watch import _DEMO_HIST_PARTIAL_BULL, _exclude_today, _ma_not_bearish
    from scanner.trend_beauty import ma_bullish

    today = now_beijing().date().isoformat()
    hist = _exclude_today(_bars([*_DEMO_HIST_PARTIAL_BULL, 10.3], today), today)
    assert len(hist) >= 20, "样本历史根数不足 → 两条判据都返回 None，断言会假绿"
    assert _ma_not_bearish(hist) is True, "非空头门应放行部分多头"
    assert ma_bullish(hist) is False, "完全多头门应拦下部分多头（否则本样本已无区分力）"


def test_classify_tier_t1_ma_not_bearish_gate(monkeypatch):
    """T1 的 MA 非空头门：空头 → 不产出；部分多头 → 放行。"""
    import scanner.offboard_watch as ow

    today = now_beijing().date().isoformat()
    bars = _bars(_UP_SERIES, today)

    monkeypatch.setattr(ow, "_ma_not_bearish", lambda kline: False)
    tier, why = classify_tier(_cand(), bars, today)
    assert tier is None
    assert why == "MA空头排列(MA5<=MA10)"

    monkeypatch.setattr(ow, "_ma_not_bearish", lambda kline: True)
    tier, _ = classify_tier(_cand(), bars, today)
    assert tier == T1


def test_classify_tier_ma_uses_history_excluding_today(monkeypatch):
    """🔴 MA 判定必须收到**剔除今日 bar** 的序列（与 accum_5d 同口径）。

    生产 K 线池末根就是当日盘中 bar（实测 09-21 215/215 末根 date == fetch_date）。
    若把含今日 bar 的整段丢给 MA：① 用未收盘价判趋势，判定随盘中漂移；
    ② 实测翻转 7.5% 的完全多头 / 25.0% 的非空头判定，且方向全是「含今日 → 更易过门」
    （今日上涨把 MA5 抬上去）→ 系统性放大过门率。
    """
    import scanner.offboard_watch as ow

    today = now_beijing().date().isoformat()
    seen: list[list] = []

    def _spy(kline):
        seen.append([b.get("date") for b in kline])
        return True

    monkeypatch.setattr(ow, "_ma_not_bearish", _spy)
    c = _cand(percent=MOMENTUM_LAUNCH_TODAY_MIN, volume_ratio=2.4)
    classify_tier(c, _bars(_UP_SERIES, today), today)
    assert seen, "_ma_not_bearish 未被调用"
    assert today not in seen[0], "MA 判定收到了今日 bar（应剔除）"


def test_classify_tier_ma_undecidable_when_bars_short():
    """不足 20 根 → `_ma_not_bearish` 返回 None → 按「不可判定」不产出（不当作空头，也不放过）。"""
    today = now_beijing().date().isoformat()
    tier, why = classify_tier(_cand(), _bars(_SHORT_SERIES, today), today)
    assert tier is None
    assert "不足20根" in why


def test_classify_tier_t2_rejects_bear_divergence(monkeypatch):
    """T2 需无顶背离：背离判定是 `validator.mo_divergence` 的职责，此处桩掉它验证本模块
    在「背离成立」时的分支（真造一条顶背离序列要凑价格新高 + RSI/OBV 不新高，脆且
    与被测职责无关）。"""
    import scanner.offboard_watch as ow
    from scanner.validator import V_MO_DIVERGENCE_BEAR

    today = now_beijing().date().isoformat()
    monkeypatch.setattr(ow, "mo_divergence", lambda closes, klines, *a, **k: (V_MO_DIVERGENCE_BEAR, "rsi_bear"))
    tier, why = classify_tier(_cand(percent=5.0), _bars(_UP_SERIES, today), today)
    assert tier is None
    assert "顶背离" in why


def test_classify_tier_main_inflow_gate_applies_to_both_tiers():
    """主力净占比 ≥ `OFFBOARD_MAIN_PCT_MIN` 是 **T1/T2 同门**（2026-09-22 起 T2 也设）。

    旧口径 T2 不设该条，其理由（「加进来会变成同名不同义的第三种启动定义」）建立在
    「T2 几乎不产出」的前提上（上线 3 日 31 行里 T2 仅 9 行）。T2 的 MA 门放宽后该
    前提失效（同日实测 T2 0 → 12），不补这道门榜首就是主力净流出的票 —— 实测前二为
    -6.61% / -3.97%，两者都高于 `common_hard_gate` 的 -8% 阈值，**通用门拦不住**。
    """
    today = now_beijing().date().isoformat()
    bars = _bars(_UP_SERIES, today)
    outflow = OFFBOARD_MAIN_PCT_MIN - 0.01

    for pct in (2.5, MOMENTUM_LAUNCH_TODAY_MIN):  # T1 带 / T2 带
        tier, why = classify_tier(_cand(percent=pct, main_pct=outflow), bars, today)
        assert tier is None, f"涨幅 {pct}% 的主力净流出票未被拦（该门必须两层同设）"
        assert "主力净占比" in why

    # 净流入则两带都放行（证明拦的是资金流、不是顺手把某一带堵死了）
    t1, _ = classify_tier(_cand(percent=2.5, main_pct=2.0), bars, today)
    t2, _ = classify_tier(_cand(percent=MOMENTUM_LAUNCH_TODAY_MIN, main_pct=2.0), bars, today)
    assert (t1, t2) == (T1, T2)


def test_annotate_sets_tier_and_beauty_consistently():
    """`annotate` 的 tier 与美感标记必须与单源判定一致（生产/自检共用这一份后处理）。"""
    today = now_beijing().date().isoformat()
    bars = _bars(_UP_SERIES, today)
    c = _cand(percent=2.5)
    assert annotate(c, bars, today) == T1
    blocked, mark, detail = beauty_marks_daily(bars)
    assert c.beauty == (mark if TREND_MARK_ENABLED else "")
    assert ("美感:" in " ".join(c.reasons)) == blocked
    if not blocked:
        assert c.reasons[0].startswith("量先动")


def test_annotate_records_reason_when_not_produced():
    """不产出时理由串要能区分「条件不满足」与「数据不足」——事后归因全靠它。"""
    today = now_beijing().date().isoformat()
    c = _cand()
    assert annotate(c, None, today) is None
    assert c.reasons == ["无K线数据(榜外池未覆盖)"]


# ── 排序键（A/B 不混排的结构性理由）──────────────────────────────────────────


def test_sort_key_puts_t2_first_regardless_of_volume_ratio():
    """**T2 在前**（2026-09-21 翻转）：T1 是「今日涨幅 < 3.5%」= 当日势能最弱的一层。

    实测 T1 当日 −0.76pp / 09-18 次日 +0.29%，均弱于 T2 的 +1.28pp / +4.34%
    （见 sort_key docstring）。把 T1 置顶 = 系统性把最不符合价值函数的那层放第一屏。
    本用例是那次翻转的回归哨兵：量比再高，只要是 T1 也排后。
    """
    a = _cand(symbol="SZ300101", code="300101", tier=T1, volume_ratio=9.9)
    b = _cand(symbol="SZ300102", code="300102", tier=T2, volume_ratio=1.6)
    assert [c.code for c in sorted([a, b], key=sort_key)] == ["300102", "300101"]


def test_sort_key_orders_by_volume_ratio_then_main_pct():
    a = _cand(symbol="SZ300101", code="300101", volume_ratio=2.0, main_pct=1.0)
    b = _cand(symbol="SZ300102", code="300102", volume_ratio=2.0, main_pct=5.0)
    c = _cand(symbol="SZ300103", code="300103", volume_ratio=3.0, main_pct=0.0)
    assert [x.code for x in sorted([a, b, c], key=sort_key)] == ["300103", "300102", "300101"]


# ── B 段小标题单源（2026-09-22：终端/飞书曾各写一份，且**两份都与 sort_key 不符**）──


def test_offboard_subtitle_sort_text_matches_sort_key_behavior():
    """小标题声称的排序链必须与 `sort_key` 的**真实行为**逐级对应。

    这是漂移的直接守卫：文案此前与代码各写各的（终端漏了「主力净占比」一整级，
    飞书更是 09-21 层序翻转前的旧文案），而既有用例只断言 `"榜外异动" in out`、
    从不看排序键内容 ⇒ 漂移长期无人拦。三级各用一个**反例**钉住 —— 任一级次序
    被写反、或整级被删，对应断言必挂。
    """
    from scanner.view.model import offboard_subtitle

    text = offboard_subtitle(1)

    # ── 第 1 级：T2 启动首日 → T1 量先动（2026-09-21 层序翻转）──
    assert text.index("T2 启动首日") < text.index("T1 量先动"), text
    t1 = _cand(symbol="SZ300101", code="300101", tier=T1, volume_ratio=9.9)
    t2 = _cand(symbol="SZ300102", code="300102", tier=T2, volume_ratio=1.6)
    assert [c.code for c in sorted([t1, t2], key=sort_key)] == ["300102", "300101"]

    # ── 第 2 级：同层内量比降序（且排在主力净占比之前）──
    assert text.index("→量比") < text.index("→主力净占比"), text
    vr_hi = _cand(symbol="SZ300103", code="300103", tier=T2, volume_ratio=5.0, main_pct=0.0)
    vr_lo = _cand(symbol="SZ300104", code="300104", tier=T2, volume_ratio=2.0, main_pct=9.0)
    assert [c.code for c in sorted([vr_lo, vr_hi], key=sort_key)] == ["300103", "300104"]

    # ── 第 3 级：主力净占比（末位 tie-breaker —— 终端旧文案正是漏了这一级）──
    assert "→主力净占比" in text, "漏掉末级 ⇒ 文案声称的排序链不完整"
    mp_hi = _cand(symbol="SZ300105", code="300105", tier=T2, volume_ratio=3.0, main_pct=8.0)
    mp_lo = _cand(symbol="SZ300106", code="300106", tier=T2, volume_ratio=3.0, main_pct=1.0)
    assert [c.code for c in sorted([mp_lo, mp_hi], key=sort_key)] == ["300105", "300106"]


def test_offboard_subtitle_silence_window_comes_from_config():
    """静默窗数值与规模数必须取自 config / 入参，不写字面量（否则改配置文案不跟）。"""
    from scanner.view.model import offboard_subtitle

    assert f"开盘 {OFFBOARD_OPENING_SILENCE_MIN} 分内不产出" in offboard_subtitle(7)
    assert "7 只" in offboard_subtitle(7)


def test_offboard_subtitle_documents_active_marker():
    """粉名/🔥 的判定含义必须写进段标题（图例行 2026-09-28 双端移除后，段标题是
    标识含义的唯一出口）。四版（2026-10-15）判定 = 反复进前十（窗内总轮次）。"""
    from scanner.view.model import offboard_subtitle

    text = offboard_subtitle(1)
    assert "粉名/🔥=反复进前十" in text
    assert f"近{OFFBOARD_REPEAT_WINDOW_DAYS}日≥{OFFBOARD_REPEAT_MIN_HITS}次" in text, "词表必须含具体阈值"


def test_terminal_offboard_subtitle_uses_single_source(capsys):
    """终端出口的 B 段括号正文必须**逐字等于**单源（去 ANSI 后比对）。

    两出口以单源为基准对齐：飞书侧的等价断言在 tests/test_feishu.py，
    任一出口改回自持文案，两边各自那条用例都会挂。
    """
    from scanner.view.model import _ANSI_ESCAPE, offboard_subtitle
    from scanner.view.render import render_offboard_standalone

    c = _offboard_row(tier=T2, accum_5d=6.0, volume_ratio=2.5, main_pct=3.0)
    render_offboard_standalone([c])
    out = capsys.readouterr().out
    line = next(ln for ln in out.splitlines() if "榜外异动" in ln)
    plain = _ANSI_ESCAPE.sub("", line)
    assert plain.endswith("— 榜外异动" + offboard_subtitle(1)), plain
    # 旧的错误层序不得复活
    assert "T1 量先动/T2" not in plain


def test_offboard_candidate_has_no_board_only_fields():
    """B 段结构上没有榜单排名/连击（None，渲染为 —，不是 0）。"""
    c = _cand()
    assert c.rank_change is None
    assert c.streak is None
    assert c.score == 0.0  # B 段不引入复合分


# ── 榜外 K 线池（独立于 daily_kline）─────────────────────────────────────────


def test_clean_bars_drops_dirty_bars():
    raw = [
        {"date": "2026-09-01", "close": 10.0},
        {"date": "", "close": 10.0},  # 无日期
        {"date": "2026-09-02", "close": 0.0},  # 停牌/脏 close
        {"date": "2026-09-03", "close": 10.0, "high": 0, "low": 9.0},  # 显式 0 的 high
        "junk",
        None,
    ]
    bars = _clean_bars(raw)
    assert [b["date"] for b in bars] == ["2026-09-01"]


def test_clean_bars_empty_is_empty():
    assert _clean_bars(None) == []
    assert _clean_bars([]) == []


def test_save_and_read_cached_klines_roundtrip(db):
    day = now_beijing().date().isoformat()
    bars = _bars(_UP_SERIES, day)
    _save_klines(db, {"SZ300101": bars}, day)
    got = _read_cached_klines(db, ["SZ300101", "SZ300999"], day)
    assert set(got) == {"SZ300101"}
    assert len(got["SZ300101"]) == len(bars)
    assert got["SZ300101"][-1]["date"] == day


def test_read_cached_klines_skips_bad_json(db):
    day = now_beijing().date().isoformat()
    db.execute(
        "INSERT INTO offboard_kline_cache(symbol,fetch_date,payload_json,updated) VALUES(?,?,?,?)",
        ("SZ300101", day, "{not json", "x"),
    )
    db.commit()
    assert _read_cached_klines(db, ["SZ300101"], day) == {}


def test_read_cached_klines_miss_on_other_fetch_date(db):
    """缓存键含抓取日：跨日自动 miss（5 日累计/MA 只依赖抓取日之前的 bar，当日复用）。"""
    today = now_beijing().date().isoformat()
    yesterday = (now_beijing().date() - _td(days=1)).isoformat()
    _save_klines(db, {"SZ300101": _bars(_UP_SERIES, today)}, yesterday)
    assert _read_cached_klines(db, ["SZ300101"], today) == {}


def test_load_offboard_klines_uses_cache_without_fetching(db):
    day = now_beijing().date().isoformat()
    _save_klines(db, {"SZ300101": _bars(_UP_SERIES, day)}, day)
    adp = _FakeKlineAdapter({"SZ300101": _bars(_UP_SERIES, day)})
    got = load_offboard_klines(db, adp, ["SZ300101"])
    assert "SZ300101" in got
    assert adp.calls == []  # 当日缓存命中 → 零请求


def test_load_offboard_klines_fetches_missing_and_persists(db):
    day = now_beijing().date().isoformat()
    adp = _FakeKlineAdapter({"SZ300101": _bars(_UP_SERIES, day)})
    got = load_offboard_klines(db, adp, ["SZ300101"])
    assert len(got["SZ300101"]) == len(_UP_SERIES)
    assert adp.calls == ["SZ300101"]
    # 落库后第二次 → 不再请求
    adp2 = _FakeKlineAdapter({"SZ300101": _bars(_UP_SERIES, day)})
    load_offboard_klines(db, adp2, ["SZ300101"])
    assert adp2.calls == []


def test_load_offboard_klines_single_failure_is_fail_soft(db):
    """单票补取失败只丢该票，不牵连其他票、不抛异常。"""
    day = now_beijing().date().isoformat()
    adp = _FakeKlineAdapter({"SZ300102": _bars(_UP_SERIES, day)}, fail={"SZ300101"})
    got = load_offboard_klines(db, adp, ["SZ300101", "SZ300102"])
    assert set(got) == {"SZ300102"}


def test_load_offboard_klines_empty_symbols_noop(db):
    adp = _FakeKlineAdapter()
    assert load_offboard_klines(db, adp, []) == {}
    assert adp.calls == []


# ── 逐日落库（上线前的硬前置）────────────────────────────────────────────────


def test_persist_round_writes_signal_snapshot(db):
    c = _cand(tier=T1, percent=2.5, accum_5d=2.64, volume_ratio=2.0, main_pct=1.5)
    persist_round(db, [c])
    day = now_beijing().date().isoformat()
    row = db.execute(
        "SELECT name, tier, percent, accum_5d, vol_ratio, main_pct, amount, float_cap, price,"
        " first_time, last_hit_time, updated, next_day_pct FROM offboard_launch_log"
        " WHERE date=? AND symbol=?",
        (day, c.symbol),
    ).fetchone()
    assert row[0] == "自检样本"
    assert row[1] == T1
    assert row[2] == pytest.approx(2.5)
    assert row[3] == pytest.approx(2.64)
    assert row[4] == pytest.approx(2.0)
    assert row[5] == pytest.approx(1.5)
    assert row[6] == pytest.approx(8.0e7)
    assert row[7] == pytest.approx(3.0e9)
    assert row[8] == pytest.approx(10.0)
    assert row[9] == row[10] == row[11]  # 首次写入：first_time / last_hit_time / updated 同为当轮时刻
    assert row[12] is None  # 待回填


def test_persist_round_freezes_signal_snapshot_and_refreshes_last_hit(db):
    """信号值**首次写入即冻结**，只有 `last_hit_time` / `updated` 每轮刷新。

    旧语义「当日最后一次写入即收盘值」是照抄 `market_extra_cache` 的 —— 对收盘价缓存
    正确，对**信号观测表**错误：一只票上午以 T1 被产出、下午涨过 3.5% 变 T2，旧实现
    会把 `tier`/`percent` 改写掉，事后无法还原它当初以什么身份被推出（实测 09-21 有
    3 只 T1 信号只存活 4 分钟，库里看不出来）。本用例锁定冻结语义。
    """
    c = _cand(tier=T1, percent=2.5, volume_ratio=2.0)
    persist_round(db, [c])
    day = now_beijing().date().isoformat()
    db.execute(
        "UPDATE offboard_launch_log SET first_time='2000-01-01T00:00:00',"
        " last_hit_time='2000-01-01T00:00:00' WHERE date=? AND symbol=?",
        (day, c.symbol),
    )
    db.commit()
    # 第二轮：同一只票已涨过 3.5%，变成 T2、量比也变了
    c.percent = 5.9
    c.tier = T2
    c.volume_ratio = 4.4
    persist_round(db, [c])
    row = db.execute(
        "SELECT first_time, last_hit_time, tier, percent, vol_ratio FROM offboard_launch_log WHERE date=? AND symbol=?",
        (day, c.symbol),
    ).fetchone()
    assert row[0] == "2000-01-01T00:00:00"  # 首见时刻不被覆盖
    assert row[1] != "2000-01-01T00:00:00"  # 最后命中时刻每轮刷新
    assert row[2] == T1  # 信号身份冻结：仍记着它当初以 T1 被推出
    assert row[3] == pytest.approx(2.5)  # 捕获时的涨幅冻结（不是后来的 5.9）
    assert row[4] == pytest.approx(2.0)  # 捕获时的量比冻结（不是后来的 4.4）
    assert db.execute("SELECT COUNT(*) FROM offboard_launch_log").fetchone()[0] == 1  # (date, symbol) 唯一


def test_persist_round_empty_is_noop(db):
    persist_round(db, [])
    assert db.execute("SELECT COUNT(*) FROM offboard_launch_log").fetchone()[0] == 0


# ── 反复出现标识（recent_repeat_counts，四版 2026-10-15）────────────────


def test_recent_repeat_counts_counts_top_ten_days_within_window(db):
    """四版（2026-10-15）：窗内「进过前十的总轮次」（top_hits 求和，含今日）
    ≥ OFFBOARD_REPEAT_MIN_HITS 达标；top_hits>0 才算（过门但未进前十的不算，
    2026-10-14 收窄后口径）；窗外行整行不进统计；m019 前的存量行（top_hits=NULL）
    不计。⚠ 「次」按**轮次**计而非不同信号日数：3 日窗内不同信号日上限 3，
    阈值 5 按日数读恒不可达。

    沿革：三版（2026-10-15）回归跨日口径后，同日晚间四版改窗 5→3、阈值 2→5，
    「次」的语义随之换成轮次（用户决策）。
    """
    today = now_beijing().date()
    edge = (today - _td(days=OFFBOARD_REPEAT_WINDOW_DAYS - 1)).isoformat()  # 窗内最旧一日
    outside = (today - _td(days=OFFBOARD_REPEAT_WINDOW_DAYS)).isoformat()  # 窗外最新一日

    def _hit(d: str, sym: str, top: int) -> None:
        db.execute(
            "INSERT INTO offboard_launch_log(date,symbol,name,tier,first_time,top_hits,updated) VALUES(?,?,?,?,?,?,?)",
            (d, sym, "样本", T1, d, top, d),
        )

    _hit(today.isoformat(), "SZ300100", 3)
    _hit(edge, "SZ300100", 2)  # 窗内 5 轮进前十 → 达标识门槛（轮次求和，跨日）
    _hit(today.isoformat(), "SZ300200", OFFBOARD_REPEAT_MIN_HITS - 1)
    _hit(edge, "SZ300200", 0)  # 过门但未进前十 → 不计；今日 4 轮 < 门槛 → 未达标
    _hit(outside, "SZ300300", 9)  # 仅窗外 9 轮 → 不得计入
    _hit(today.isoformat(), "SZ300400", 6)  # 当日反复进前十 → 当场达标
    _hit(today.isoformat(), "SZ300500", 0)  # 今日未进前十 → 不计
    db.commit()

    counts = recent_repeat_counts(db, today.isoformat())
    assert counts["SZ300100"] == 5, "跨日：窗内 5 轮进前十（top_hits 求和）"
    assert counts["SZ300400"] == 6, "当日分支：今日反复进前十，取较大者"
    assert counts["SZ300200"] == 4, "过门未进前十的轮不计，今日 4 轮未达标"
    assert "SZ300300" not in counts, "窗外不计"
    assert "SZ300500" not in counts, "未进前十不计"


def test_persist_round_top_hits_only_for_top_n(db):
    """top_hits（m019）：只有前 OFFBOARD_DISPLAY_TOP 行记 1，其后行记 0；
    同票多轮 ON CONFLICT 累加（计数列，与信号冻结列相反）；跨日自然重置。"""
    rows = [_cand(symbol=f"SZ3010{i:02d}", volume_ratio=10.0 - i) for i in range(OFFBOARD_DISPLAY_TOP + 2)]
    persist_round(db, rows)
    day = now_beijing().date().isoformat()
    hits = dict(db.execute("SELECT symbol, top_hits FROM offboard_launch_log WHERE date=?", (day,)).fetchall())
    assert sum(1 for v in hits.values() if v == 1) == OFFBOARD_DISPLAY_TOP, "只有前 N 行记 1"
    top_syms = {c.symbol for c in rows[:OFFBOARD_DISPLAY_TOP]}
    assert all(v == 0 for k, v in hits.items() if k not in top_syms), "前 N 名之外的行不得记 1"
    # 同票第二轮：累加而非覆盖
    persist_round(db, rows[:1])
    assert (
        db.execute(
            "SELECT top_hits FROM offboard_launch_log WHERE date=? AND symbol=?", (day, rows[0].symbol)
        ).fetchone()[0]
        == 2
    ), "同票多轮进前十 ⇒ top_hits 累加"
    # ⚠ 2026-10-16 修复：前 N 之外的票**跨轮恒为 0**。此前 ON CONFLICT 是无条件
    # `top_hits+1`（丢掉 INSERT 时算出的 0），于是从未进过展示前 N 的票也会攒到门槛
    # —— 正是 m019 被否定的「进前十 ≈ 过门」，粉名/🔥 会被误染。
    tail = rows[OFFBOARD_DISPLAY_TOP]
    for _ in range(OFFBOARD_REPEAT_MIN_HITS + 1):
        persist_round(db, rows)
    tail_hits = db.execute(
        "SELECT top_hits FROM offboard_launch_log WHERE date=? AND symbol=?", (day, tail.symbol)
    ).fetchone()[0]
    assert tail_hits == 0, "前 N 之外的行不得跨轮累加 top_hits"
    # 对照：前 N 行同批照常每轮 +1
    head_hits = db.execute(
        "SELECT top_hits FROM offboard_launch_log WHERE date=? AND symbol=?", (day, rows[0].symbol)
    ).fetchone()[0]
    assert head_hits == 2 + OFFBOARD_REPEAT_MIN_HITS + 1, "前 N 行仍每轮 +1"


def test_recent_repeat_counts_fails_open():
    """着色是纯展示增强：conn 缺失 / 表缺失 → {}，不让本段挂掉
    （与 `_recent_dwell_symbols` 同款 fail-open 纪律）。"""
    assert recent_repeat_counts(None) == {}
    conn = sqlite3.connect(":memory:")
    try:
        assert recent_repeat_counts(conn, "2026-10-14") == {}
    finally:
        conn.close()


def test_run_offboard_watch_backfills_repeat_days(db):
    """端到端回填守卫：`run_offboard_watch` 返回前必须把 `recent_repeat_counts`
    结果写到展示行的 `repeat_days` 上 —— 渲染层只读该字段不重算，漏掉这步
    粉名/🔥 在生产链路里永不出现（字段恒默认 0，单测却全部通过）。四版（2026-10-15）
    口径 = 窗内 top_hits 求和 ≥ 门槛。"""
    sym = "SZ300101"
    today = now_beijing().date()
    prior = (today - _td(days=OFFBOARD_REPEAT_WINDOW_DAYS - 1)).isoformat()  # 窗内最旧一日
    _seed_log_row(db, sym, now_beijing().isoformat(timespec="seconds"))
    # 第二个窗内信号日直接以历史日落库（(date,symbol) 唯一，不能同日插两行）；
    # 两行都必须 top_hits>0 才算「进过前十」（收窄后口径）
    db.execute(
        "INSERT INTO offboard_launch_log(date,symbol,name,tier,first_time,top_hits,updated) VALUES(?,?,?,?,?,?,?)",
        (prior, sym, "自检样本", "T1", prior, OFFBOARD_REPEAT_MIN_HITS - 1, prior),
    )
    db.execute(
        "UPDATE offboard_launch_log SET top_hits=? WHERE symbol=? AND date=?",
        (1, sym, today.isoformat()),
    )
    db.commit()

    rows = run_offboard_watch(
        None,
        db,
        [],
        top_n=5,
        klines={sym: _bars(_UP_SERIES, today.isoformat())},
        snapshot={sym: _payload(vol_ratio=0.5)},  # 量比 0.5 < 1.5 ⇒ 严格门挂掉，走留存路径
    )
    assert len(rows) == 1
    assert rows[0].repeat_days == OFFBOARD_REPEAT_MIN_HITS, "窗内 5 轮进前十 ⇒ 必须回填，否则粉名/🔥 永不出现"


def test_run_offboard_watch_repeat_days_covers_same_day_repeat(db):
    """当日分支端到端：今日多轮进前十（top_hits≥门槛）即回填，不等明天。"""
    sym = "SZ300102"
    today = now_beijing().date().isoformat()
    _seed_log_row(db, sym, now_beijing().isoformat(timespec="seconds"))
    db.execute(
        "UPDATE offboard_launch_log SET top_hits=? WHERE symbol=? AND date=?",
        (OFFBOARD_REPEAT_MIN_HITS, sym, today),
    )
    db.commit()

    rows = run_offboard_watch(
        None,
        db,
        [],
        top_n=5,
        klines={sym: _bars(_UP_SERIES, today)},
        snapshot={sym: _payload(vol_ratio=0.5)},
    )
    assert len(rows) == 1
    assert rows[0].repeat_days >= OFFBOARD_REPEAT_MIN_HITS, "今日反复进前十 ⇒ 当场达标"


# ── 拒绝留痕表 offboard_rejections（2026-09-22 / 迁移 m017）──────────────────


def test_run_offboard_watch_records_gate_and_tier_rejections(db):
    """两段拒绝都必须落库，且带出快照三列（percent / vol_ratio / main_pct）。

    这是本用例存在的**全部理由**：`run_offboard_watch` 原先写的是
    `cands, _rejects = build_candidates(...)` —— gate 段的 rejects 被就地丢弃，
    分层段的理由也只写进 `c.reasons` 就地消失。于是「谁被哪道门杀了」只能靠手工
    重建快照反推（2026-09-22 那次就是这么干的，跑完即失）。

    没有留痕就只有幸存者样本 ⇒ 幸存者偏差 ⇒ 任何阈值调整无从证伪，而本区又是
    `rule_validate` 三个评估器的盲区。哪天有人把留痕删掉，这里会红。
    """
    snapshot = {
        "SZ300101": _payload(vol_ratio=1.6, percent=2.5, main_pct=2.0),  # 过门 → 无 K 线 → 分层拒
        "SZ300103": _payload(vol_ratio=1.0, percent=2.5, main_pct=2.0),  # 量比不足 → 门槛拒
    }
    assert run_offboard_watch(None, db, [], klines={}, snapshot=snapshot) == []

    rej = {
        r[0]: r
        for r in db.execute(
            "SELECT symbol, name, reason, percent, vol_ratio, main_pct FROM offboard_rejections"
        ).fetchall()
    }
    assert set(rej) == {"SZ300101", "SZ300103"}, "gate 段与分层段的拒绝都必须留痕"
    assert "无K线数据" in rej["SZ300101"][2]
    assert "量比不足" in rej["SZ300103"][2]
    # 快照三列 = 「事后重放阈值」的输入，缺了就得手工重建（本表存在的动因）
    assert rej["SZ300101"][3] == pytest.approx(2.5)
    assert rej["SZ300103"][4] == pytest.approx(1.0)
    # name 走「names → 快照 f14」回退链：内存库的 appearances 是空的，故取快照名
    assert rej["SZ300101"][1] == "自检样本"
    # 产出者不进留痕表（它进 offboard_launch_log）
    assert db.execute("SELECT COUNT(*) FROM offboard_launch_log").fetchone()[0] == 0


def test_save_offboard_rejections_upserts_hits_and_freezes_first_time(db):
    """同 (date, symbol) 当日重复命中：hits 累加、reason/三列刷新、first_time 不动。"""
    from scanner.db.dal import save_offboard_rejections

    day = now_beijing().date().isoformat()
    assert save_offboard_rejections(db, [("SZ300101", "自检样本", "量比不足(1.00<1.5)", 2.5, 1.0, 2.0)], day) == 1
    first_time, hits = db.execute("SELECT first_time, hits FROM offboard_rejections").fetchone()
    assert hits == 1

    # 第二轮同票：理由漂了（盘中涨幅冲破上限）→ 取最新状态
    save_offboard_rejections(db, [("SZ300101", "自检样本", "涨幅过高(9.00%>7%)", 9.0, 1.0, 2.0)], day)
    reason, percent, hits2, first2 = db.execute(
        "SELECT reason, percent, hits, first_time FROM offboard_rejections"
    ).fetchone()
    assert reason == "涨幅过高(9.00%>7%)"
    assert percent == pytest.approx(9.0)
    assert hits2 == hits + 1
    assert first2 == first_time, "first_time 记的是「当日首次被拒」，不得被后续轮次改写"


def test_save_offboard_rejections_fail_open(db):
    """fail-open：表缺失 / 空输入都不许抛 —— 留痕是观测，出问题不能中断扫描。"""
    from scanner.db.dal import save_offboard_rejections

    row = ("SZ300101", "自检样本", "量比不足(1.00<1.5)", 2.5, 1.0, 2.0)
    assert save_offboard_rejections(db, [], now_beijing().date().isoformat()) == 0

    db.execute("DROP TABLE offboard_rejections")
    db.commit()
    assert save_offboard_rejections(db, [row], now_beijing().date().isoformat()) == 0  # 不抛即通过


# ── 次日收益回填（两个防呆）──────────────────────────────────────────────────


def _insert_signal(db, symbol: str, day: str, tier: str = T1) -> None:
    db.execute(
        "INSERT INTO offboard_launch_log(date,symbol,name,tier,percent,first_time,updated) VALUES(?,?,?,?,?,?,?)",
        (day, symbol, "回填样本", tier, 2.5, "x", "x"),
    )
    db.commit()


def test_backfill_next_day_fills_pct(db):
    today = now_beijing().date()
    d0 = (today - _td(days=3)).isoformat()
    d1 = (today - _td(days=2)).isoformat()
    sym = "SZ300201"
    _save_klines(db, {sym: _bars_by_date([(d0, 10.0), (d1, 11.0)])}, today.isoformat())
    _insert_signal(db, sym, d0)

    assert backfill_next_day(db, None) == 1
    pct = db.execute("SELECT next_day_pct FROM offboard_launch_log WHERE date=? AND symbol=?", (d0, sym)).fetchone()[0]
    assert pct == pytest.approx(10.0)


def test_backfill_next_day_filters_by_signal_date(db):
    today = now_beijing().date()
    d0 = (today - _td(days=3)).isoformat()
    d1 = (today - _td(days=2)).isoformat()
    sym = "SZ300201"
    _save_klines(db, {sym: _bars_by_date([(d0, 10.0), (d1, 11.0)])}, today.isoformat())
    _insert_signal(db, sym, d0)
    assert backfill_next_day(db, None, "2000-01-01") == 0  # 指定了别的日子 → 不处理
    assert backfill_next_day(db, None, d0) == 1


def test_backfill_skips_today_signals(db):
    """信号日必须早于今天：否则每轮都为当天找「次日 bar」，永远找不到还白翻 K 线池。"""
    today = now_beijing().date().isoformat()
    sym = "SZ300201"
    _save_klines(db, {sym: _bars(_UP_SERIES, today)}, today)
    _insert_signal(db, sym, today)
    assert backfill_next_day(db, None) == 0
    assert db.execute("SELECT next_day_pct FROM offboard_launch_log WHERE date=?", (today,)).fetchone()[0] is None


def test_backfill_skips_when_next_bar_too_far(db):
    """防呆 1：不能假设「下一根 bar = 下一交易日」——缺口 >4 自然日即跳过（可能是停牌）。"""
    today = now_beijing().date()
    d0 = (today - _td(days=10)).isoformat()
    sym = "SZ300201"
    _save_klines(db, {sym: _bars_by_date([(d0, 10.0), (today.isoformat(), 11.0)])}, today.isoformat())
    _insert_signal(db, sym, d0)
    assert backfill_next_day(db, None) == 0


def test_backfill_fills_when_gap_within_4_days(db):
    """跨周末（3 自然日）仍算次日，正常回填。"""
    today = now_beijing().date()
    d0 = (today - _td(days=4)).isoformat()
    d1 = (today - _td(days=1)).isoformat()
    sym = "SZ300201"
    _save_klines(db, {sym: _bars_by_date([(d0, 10.0), (d1, 12.0)])}, today.isoformat())
    _insert_signal(db, sym, d0)
    assert backfill_next_day(db, None) == 1
    pct = db.execute("SELECT next_day_pct FROM offboard_launch_log WHERE date=?", (d0,)).fetchone()[0]
    assert pct == pytest.approx(20.0)


def test_backfill_skips_when_no_next_bar(db):
    today = now_beijing().date()
    d0 = (today - _td(days=3)).isoformat()
    sym = "SZ300201"
    _save_klines(db, {sym: _bars_by_date([(d0, 10.0)])}, today.isoformat())
    _insert_signal(db, sym, d0)
    assert backfill_next_day(db, None) == 0


def _bar(day: str, close: float, *, open_: float | None = None) -> dict:
    """单根 bar（显式给 open/high/low，避免 `_bars_by_date` 的 prev 传导把相邻 bar 一起弄脏）。"""
    o = close if open_ is None else open_
    return {
        "date": day,
        "open": o,
        "close": close,
        "high": max(o, close) * 1.004,
        "low": min(o, close) * 0.996,
        "volume": 2.0e6,
        "percent": 0.0,
    }


def test_backfill_survives_dirty_signal_day_bar(db):
    """回归守卫：信号日当天的 bar 是**脏 bar**（close<=0）时不得崩。

    ## 崩溃路径

    `backfill_next_day` 里 `later = sorted(x for x in by_date if ... > d)` 只保证
    「有比 d 晚的 bar」，**不保证 d 自己在 `by_date` 里**。而 `by_date` 来自
    `load_offboard_klines` → `_read_cached_klines` → `_clean_bars`，后者按
    `make_kline_bar` 契约**剔除脏 bar**（close<=0 / 日期非法）。

    于是「信号日 bar 脏、次日 bar 正常」时 `by_date[d]` 抛 `KeyError`。
    它被 `run_offboard_watch` 的 `except EXTERNAL_FAILURES` 吞掉 ⇒
    **该轮所有待回填行全废**，且只有一行 warning —— 标签静默停止积累，
    正是「没有标签就没有任何阈值能被证伪」那条纪律的隐形杀手。

    正确行为：脏 bar ⇒ 无法验证信号日收盘价 ⇒ 跳过该行（返回 0），不崩。

    ⚠ 数据必须**只让信号日那根脏**：若用 `_bars_by_date([(d0, 0.0), (d1, 11.0)])`，
    次日 bar 的 open/low 会由 prev=0.0 传导成 0 而一并被剔除，整个池清空 ⇒
    `later` 为空提前 continue ⇒ **测试会因错误的原因通过**（守卫失效）。
    故这里显式构造两根独立 bar。
    """
    today = now_beijing().date()
    d0 = (today - _td(days=3)).isoformat()
    d1 = (today - _td(days=2)).isoformat()
    sym = "SZ300201"
    bars = [_bar(d0, 0.0), _bar(d1, 11.0)]  # 仅 d0 脏，d1 干净
    # 前置断言：确认「d1 留存、d0 被剔除」，否则本测试没有意义
    kept = {b["date"] for b in _clean_bars(bars)}
    assert kept == {d1}, f"测试前提不成立：实际留存 {kept}"

    _save_klines(db, {sym: bars}, today.isoformat())
    _insert_signal(db, sym, d0)
    assert backfill_next_day(db, None) == 0  # 不抛，且不产出标签


def test_backfill_survives_missing_signal_day_bar(db):
    """信号日 bar 整个不在池里（停牌/未落库）⇒ 跳过，不崩。

    这是**生产上最常见**的触发形态：信号日那天的 bar 没落进榜外 K 线池
    （票当天未进候选、或跨日后重取时池子按 `fetch_date` 分片），而次日 bar 在。
    """
    today = now_beijing().date()
    d0 = (today - _td(days=3)).isoformat()
    d1 = (today - _td(days=2)).isoformat()
    sym = "SZ300201"
    _save_klines(db, {sym: [_bar(d1, 11.0)]}, today.isoformat())  # 只有次日的 bar
    _insert_signal(db, sym, d0)
    assert backfill_next_day(db, None) == 0


def test_one_bad_symbol_does_not_abort_the_whole_round(db):
    """一只票的脏数据不得拖垮**同轮其他票**的回填。

    这是本 bug 最实际危害的形态：KeyError 从内层循环抛出后，
    整个 `backfill_next_day` 当轮返回，同批次的正常票也一起丢失标签。
    """
    today = now_beijing().date()
    d0 = (today - _td(days=3)).isoformat()
    d1 = (today - _td(days=2)).isoformat()
    bad, good = "SZ300201", "SZ300202"
    _save_klines(db, {bad: [_bar(d0, 10.0), _bar(d1, 11.0)]}, today.isoformat())
    _save_klines(db, {good: [_bar(d0, 10.0), _bar(d1, 12.0)]}, today.isoformat())
    # 只把 bad 的信号日 bar 从池里抠掉（等价于它当天没落库），good 完好
    db.execute(
        "UPDATE offboard_kline_cache SET payload_json=? WHERE symbol=?",
        (json.dumps([_bar(d1, 11.0)]), bad),
    )
    db.commit()
    _insert_signal(db, bad, d0)
    _insert_signal(db, good, d0)
    assert backfill_next_day(db, None) == 1  # 只有好的那只被回填
    pct = db.execute("SELECT next_day_pct FROM offboard_launch_log WHERE date=? AND symbol=?", (d0, good)).fetchone()[0]
    assert pct == pytest.approx(20.0)


def test_backfill_already_filled_is_not_touched(db):
    today = now_beijing().date()
    d0 = (today - _td(days=3)).isoformat()
    d1 = (today - _td(days=2)).isoformat()
    sym = "SZ300201"
    _save_klines(db, {sym: _bars_by_date([(d0, 10.0), (d1, 11.0)])}, today.isoformat())
    _insert_signal(db, sym, d0)
    assert backfill_next_day(db, None) == 1
    assert backfill_next_day(db, None) == 0  # 只回填 next_day_pct IS NULL


# ── 主流程端到端 ────────────────────────────────────────────────────────────


def test_backfill_leaves_last_hit_time_alone(db):
    """回填只写 next_day_pct / updated，不得碰 last_hit_time。

    `updated` 被两处写（每轮落库 + 回填）正是当初拆出 `last_hit_time` 的原因：
    09-18 那 5 行的 `updated` 全被回填写成 09-21 的时刻，与当日行的 `first_time` 撞车，
    命中时间维度事实上丢失。本用例锁定「回填不改命中时刻」。
    """
    today = now_beijing().date()
    d0 = (today - _td(days=3)).isoformat()
    d1 = (today - _td(days=2)).isoformat()
    sym = "SZ300201"
    _save_klines(db, {sym: _bars_by_date([(d0, 10.0), (d1, 11.0)])}, today.isoformat())
    _insert_signal(db, sym, d0)
    db.execute("UPDATE offboard_launch_log SET last_hit_time='2026-09-18T09:50:00' WHERE symbol=?", (sym,))
    db.commit()

    assert backfill_next_day(db, None) == 1
    row = db.execute(
        "SELECT next_day_pct, last_hit_time FROM offboard_launch_log WHERE date=? AND symbol=?", (d0, sym)
    ).fetchone()
    assert row[0] == pytest.approx(10.0)  # 回填成功
    assert row[1] == "2026-09-18T09:50:00"  # 命中时刻不被回填改写


def test_run_offboard_watch_end_to_end(db):
    day = now_beijing().date().isoformat()
    snapshot = {
        "SZ300101": _payload(vol_ratio=1.6, percent=2.5, main_pct=2.0),  # T1
        "SZ300102": _payload(vol_ratio=3.0, percent=5.0, main_pct=1.0),  # T2
        "SZ300103": _payload(vol_ratio=1.0),  # 量比不足 → 门槛剔除
    }
    klines = {"SZ300101": _bars(_UP_SERIES, day), "SZ300102": _bars(_UP_SERIES, day)}
    rows = run_offboard_watch(None, db, [], top_n=5, klines=klines, snapshot=snapshot)
    assert [r.tier for r in rows] == [T2, T1]  # T2 在前（2026-09-21 翻转），与量比排序无关
    assert [r.code for r in rows] == ["300102", "300101"]
    # 落库：只有产出者
    logged = dict(db.execute("SELECT symbol, tier FROM offboard_launch_log").fetchall())
    assert logged == {"SZ300101": T1, "SZ300102": T2}


def test_run_offboard_watch_respects_top_n(db):
    day = now_beijing().date().isoformat()
    snapshot = {f"SZ30010{i}": _payload(vol_ratio=1.6 + i * 0.1) for i in range(1, 4)}
    klines = {s: _bars(_UP_SERIES, day) for s in snapshot}
    rows = run_offboard_watch(None, db, [], top_n=2, klines=klines, snapshot=snapshot)
    assert len(rows) == 2
    # 落库不受 top_n 约束（展示截断 ≠ 观测截断）
    assert db.execute("SELECT COUNT(*) FROM offboard_launch_log").fetchone()[0] == 3


def test_run_offboard_watch_never_touches_mainline_tables(db):
    """三条硬边界之一：不写 recommendations / daily_kline（B 段不进主线任何链路）。"""
    day = now_beijing().date().isoformat()
    rows = run_offboard_watch(
        None,
        db,
        [],
        klines={"SZ300101": _bars(_UP_SERIES, day)},
        snapshot={"SZ300101": _payload()},
    )
    assert len(rows) == 1
    assert db.execute("SELECT COUNT(*) FROM recommendations").fetchone()[0] == 0


# ── M1 实时资金流覆盖（2026-10-08）──────────────────────────────────────────


def test_apply_live_fund_flow_overrides_and_fails_open():
    """新鲜 main_pct / vol_ratio 覆盖快照；缺行/缺字段/空 ff 保留快照值（fail-open）。

    量比 ≤0（停牌/脏值）不覆盖 —— 0 不是合法量比，覆盖会把可交易票误杀。
    """
    snap = {
        "SZ300101": {"main_pct": -9.0, "vol_ratio": 1.0, "price": 10.0},
        "SZ300102": {"main_pct": 1.0, "vol_ratio": 2.0},
        "SZ300103": None,  # 脏行跳过
    }
    ff = {
        "300101": {"main_pct": 3.2, "vol_ratio": 5.5},
        "300102": {"main_pct": 1.2},  # 无 vol_ratio 字段 → 保留快照
        "300103": {"main_pct": 0.0, "vol_ratio": 0.0},  # 脏行 + 量比 0 不覆盖
    }
    n = _apply_live_fund_flow(snap, ff)
    assert n == 2
    assert snap["SZ300101"]["main_pct"] == pytest.approx(3.2)
    assert snap["SZ300101"]["vol_ratio"] == pytest.approx(5.5)
    assert snap["SZ300102"]["main_pct"] == pytest.approx(1.2)
    assert snap["SZ300102"]["vol_ratio"] == pytest.approx(2.0)  # 保留快照
    assert _apply_live_fund_flow(snap, {}) == 0


def test_run_offboard_watch_injected_snapshot_skips_live_fund_flow(db, monkeypatch):
    """snapshot 注入路径（测试/离线/CLI）**不得**触发实时资金流补拉（零联网纪律）。"""

    def _boom():
        raise AssertionError("snapshot 注入路径不应调用 fetch_fund_flow_rank")

    monkeypatch.setattr("scanner.offboard_watch.fetch_fund_flow_rank", _boom)
    day = now_beijing().date().isoformat()
    rows = run_offboard_watch(
        None,
        db,
        [],
        top_n=5,
        klines={"SZ300101": _bars(_UP_SERIES, day)},
        snapshot={"SZ300101": _payload(vol_ratio=1.6, percent=2.5, main_pct=2.0)},
    )
    assert len(rows) == 1


def test_run_offboard_watch_db_path_applies_live_fund_flow(db, monkeypatch):
    """DB 快照路径（生产主循环）：T1/T2 门与产出行的 main_pct/vol_ratio 用实时覆盖值。

    快照里 main_pct=-9.0（陈旧，本会被主力净流出门拒掉）、vol_ratio=1.0（陈旧），
    实时为 main_pct=+5.5 / vol_ratio=2.2 —— 修复后应放行且产出行携带新鲜值。
    这正是 M1 描述的「应拒的票被静默放行 / 应过的票被静默拒掉」双向修正的后者。
    """
    day = now_beijing().date().isoformat()
    sym = "SZ300101"
    db.execute(
        "INSERT INTO market_extra_cache VALUES (?,?,?,?)",
        (sym, "fund_flow", day, json.dumps(_payload(vol_ratio=1.0, percent=2.5, main_pct=-9.0))),
    )
    db.commit()
    monkeypatch.setattr(
        "scanner.offboard_watch.fetch_fund_flow_rank",
        lambda: {"300101": {"main_pct": 5.5, "vol_ratio": 2.2}},
    )
    rows = run_offboard_watch(None, db, [], top_n=5, klines={sym: _bars(_UP_SERIES, day)})
    assert len(rows) == 1
    assert rows[0].main_pct == pytest.approx(5.5)
    assert rows[0].volume_ratio == pytest.approx(2.2)


# ── P1 实时涨幅覆盖：有符号字段不可套「val > 0」守卫（2026-10-14）─────────────
#
# 实测故障：2026-10-14 10:17，300758 七彩化学快照冻结在 09:44:35 的 +2.85%（12.29），
# 实时已是 11.80/−1.26%（last_close 11.95），终端仍显示 **+2.85%**。守卫对量纲字段
# （0 = 停牌/脏值）是对的，对有符号的 `percent` 是反的 —— 票一翻绿，实时涨幅就被
# 挡在门外，陈旧正值永远留下，且同行的 price/volume/amount 照刷成实时值 ⇒ 屏幕上一行
# 里「现价是新的、涨幅是旧的」，两者对不上，看起来像**算错了**而不是**旧了**。
# 门也读这列 ⇒ 已翻绿的票继续以「上涨异动」留在本段，直接违反「不接跌」的立段前提。


def test_apply_live_quote_accepts_negative_percent_over_stale_positive():
    """有符号字段（今日涨幅）负值合法：必须覆盖掉陈旧的正涨幅，且与现价同批刷新。

    用真实数值复刻 300758 七彩化学 2026-10-14 的那一行，便于对照排查。
    """
    payload = _payload(name="七彩化学", price=12.29, percent=2.85, volume=67388.0)
    ok = _apply_live_quote(
        payload,
        {
            "symbol": "SZ300758",
            "name": "七彩化学",
            "current": 11.80,
            "percent": -1.26,
            "volume": 13139600.0,
            "amount": 158698935.0,
            "turnover_rate": 3.65,
            "market_capital": 4.9e9,
            "float_market_capital": 4.4e9,
        },
    )
    assert ok is True
    # 涨幅不再停在陈旧正值，且与实时现价自洽（11.80 / 11.95 − 1 = −1.26%）
    assert payload["percent"] == pytest.approx(-1.26)
    assert payload["price"] == pytest.approx(11.80)
    assert (payload["price"] / 11.95 - 1) * 100 == pytest.approx(payload["percent"], abs=0.02)


def test_apply_live_quote_still_rejects_nonpositive_magnitude_fields():
    """量纲字段的 `<= 0` 守卫**不许**被这次修复一并放宽：0 = 停牌/脏值，覆盖会误杀可交易票。"""
    payload = _payload(price=10.0, volume=2.0e6, amount=8.0e7, total_cap=3.6e9)
    _apply_live_quote(
        payload,
        {
            "current": 10.5,  # 有效 → 覆盖
            "volume": 0.0,  # 停牌 → 不覆盖
            "amount": 0.0,
            "market_capital": 0.0,
            "float_market_capital": None,  # 缺字段 → 不覆盖
        },
    )
    assert payload["price"] == pytest.approx(10.5)
    assert payload["volume"] == pytest.approx(2.0e6)
    assert payload["amount"] == pytest.approx(8.0e7)
    assert payload["total_cap"] == pytest.approx(3.6e9)
    assert payload["float_cap"] == pytest.approx(3.0e9)


def _seed_stale_snapshot(db, sym: str, day: str, **payload_kw) -> None:
    """往内存库塞一行**陈旧**快照（模拟全市场拉取超时、该行冻结在上一轮）。"""
    db.execute(
        "INSERT INTO market_extra_cache VALUES (?,?,?,?)",
        (sym, "fund_flow", day, json.dumps(_payload(**payload_kw))),
    )
    db.commit()


def test_run_offboard_watch_db_path_drops_stock_that_turned_red(db, monkeypatch):
    """翻绿的榜外票必须被拒（「不接跌」），而不是拿着陈旧正涨幅继续展示。

    修复前该票会以 +2.85% 留在榜外异动段里 —— 屏幕显示的是 09:44 的涨幅，
    用户以为它在涨，实际早已翻绿。
    """
    day = now_beijing().date().isoformat()
    sym = "SZ300758"
    _seed_stale_snapshot(db, sym, day, name="七彩化学", price=12.29, percent=2.85)
    monkeypatch.setattr("scanner.offboard_watch.fetch_fund_flow_rank", lambda: {})
    adapter = _FakeQuoteAdapter(
        quotes={
            sym: {
                "symbol": sym,
                "name": "七彩化学",
                "current": 11.80,
                "percent": -1.26,  # 实时已翻绿
                "volume": 1.31396e7,
                "amount": 1.58698935e8,
                "turnover_rate": 3.65,
            }
        }
    )
    rows = run_offboard_watch(adapter, db, [], top_n=5, klines={sym: _bars(_UP_SERIES, day)})
    assert rows == [], "实时已翻绿的票不得留在榜外异动段"

    # 对照：实时仍为正 ⇒ 放行，且产出行携带的是**实时**涨幅而非陈旧值
    adapter2 = _FakeQuoteAdapter(
        quotes={
            sym: {
                "symbol": sym,
                "name": "七彩化学",
                "current": 12.12,
                "percent": 1.42,
                "volume": 1.31396e7,
                "amount": 1.58698935e8,
                "turnover_rate": 3.65,
            }
        }
    )
    rows2 = run_offboard_watch(adapter2, db, [], top_n=5, klines={sym: _bars(_UP_SERIES, day)})
    assert len(rows2) == 1
    assert rows2[0].percent == pytest.approx(1.42)
    assert rows2[0].current == pytest.approx(12.12)


# ── 留存（dwell，2026-10-09）────────────────────────────────────────────────


def test_offboard_gate_retain_relaxes_activity_only():
    """retain 模式只放宽量比/成交额；涨幅上限（不追高）与风险门照严。"""
    c = _snapshot_row_to_candidate("SZ300101", _payload(vol_ratio=1.0, amount=1.0e6), {})
    assert offboard_gate(c) is not None  # 严格门：量比 1.0<1.5 先被拒
    assert offboard_gate(c, retain=True) is None  # 留存门：活跃度阈值放宽后放行
    # 涨幅上限不放宽：留存票冲过 OFFBOARD_T2_TODAY_MAX 仍拒（不追高）
    hi = _snapshot_row_to_candidate("SZ300101", _payload(vol_ratio=1.0, amount=1.0e6, percent=9.9), {})
    assert offboard_gate(hi, retain=True) is not None


def _seed_log_row(db, sym: str, last_hit_time: str) -> None:
    """向 offboard_launch_log 插一行「曾严格过门」的记录（date=今日）。"""
    day = now_beijing().date().isoformat()
    db.execute(
        "INSERT INTO offboard_launch_log "
        "(date,symbol,name,tier,percent,accum_5d,vol_ratio,main_pct,amount,float_cap,"
        "price,first_time,last_hit_time,updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            day,
            sym,
            "自检样本",
            "T1",
            2.5,
            1.0,
            2.0,
            2.0,
            8.0e7,
            3.0e9,
            10.0,
            last_hit_time,
            last_hit_time,
            last_hit_time,
        ),
    )
    db.commit()


def test_run_offboard_watch_dwell_retains_recent_member(db):
    """留存窗口内：本轮只挂量比阈值的近期成员以「留存」行续展，且不写回 launch_log。

    窗口语义自「最后一次严格过门」起算：本轮留存**不得**刷新 last_hit_time，
    否则窗口自我续命、永不 expiry —— 这是本机制最关键的不变量。
    """
    sym = "SZ300101"
    day = now_beijing().date().isoformat()
    seeded_at = now_beijing().isoformat(timespec="seconds")
    _seed_log_row(db, sym, seeded_at)
    rows = run_offboard_watch(
        None,
        db,
        [],
        top_n=5,
        klines={sym: _bars(_UP_SERIES, day)},
        snapshot={sym: _payload(vol_ratio=0.5)},  # 量比 0.5 < 1.5，严格门挂掉
    )
    assert len(rows) == 1
    assert any("留存" in r for r in rows[0].reasons)
    # 留存行不刷新 last_hit_time（窗口不自我续命）—— 精确等于种子值
    hit = db.execute("SELECT last_hit_time FROM offboard_launch_log WHERE symbol=?", (sym,)).fetchone()[0]
    assert hit == seeded_at


def test_run_offboard_watch_dwell_expires_after_window(db):
    """窗口过期（last_hit_time 早于 OFFBOARD_DWELL_MIN）→ 不再留存，产出为空。"""
    sym = "SZ300101"
    day = now_beijing().date().isoformat()
    stale = (now_beijing() - _td(minutes=OFFBOARD_DWELL_MIN + 5)).isoformat(timespec="seconds")
    _seed_log_row(db, sym, stale)
    rows = run_offboard_watch(
        None,
        db,
        [],
        top_n=5,
        klines={sym: _bars(_UP_SERIES, day)},
        snapshot={sym: _payload(vol_ratio=0.5)},
    )
    assert rows == []


def test_run_offboard_watch_db_path_fund_flow_fetch_failure_fails_open(db, monkeypatch):
    """实时资金流补拉失败 → 退化为快照 main_pct，本轮照常判定（fail-open）。"""
    day = now_beijing().date().isoformat()
    sym = "SZ300101"
    db.execute(
        "INSERT INTO market_extra_cache VALUES (?,?,?,?)",
        (sym, "fund_flow", day, json.dumps(_payload(vol_ratio=1.6, percent=2.5, main_pct=2.0))),
    )
    db.commit()

    def _boom():
        raise OSError("network down")

    monkeypatch.setattr("scanner.offboard_watch.fetch_fund_flow_rank", _boom)
    rows = run_offboard_watch(None, db, [], top_n=5, klines={sym: _bars(_UP_SERIES, day)})
    assert len(rows) == 1
    assert rows[0].main_pct == pytest.approx(2.0)
    assert db.execute("SELECT COUNT(*) FROM daily_kline").fetchone()[0] == 0


def test_run_offboard_watch_empty_snapshot_returns_empty(db):
    assert run_offboard_watch(None, db, [], snapshot={}) == []


def test_run_offboard_watch_none_conn_returns_empty():
    assert run_offboard_watch(None, None, [], snapshot={"SZ300101": _payload()}) == []


def test_run_offboard_watch_all_gated_out_returns_empty(db):
    rows = run_offboard_watch(None, db, [], klines={}, snapshot={"SZ300101": _payload(vol_ratio=1.0)})
    assert rows == []
    assert db.execute("SELECT COUNT(*) FROM offboard_launch_log").fetchone()[0] == 0


def test_run_offboard_watch_excludes_recommended_symbols(db):
    day = now_beijing().date().isoformat()
    rows = run_offboard_watch(
        None,
        db,
        [],
        exclude_symbols={"SZ300101"},
        klines={"SZ300101": _bars(_UP_SERIES, day)},
        snapshot={"SZ300101": _payload()},
    )
    assert rows == []


def test_run_offboard_watch_fetches_klines_when_not_injected(db):
    """生产路径：未注入 klines → 走榜外 K 线池（并发补取 + 落库）。"""
    day = now_beijing().date().isoformat()
    adp = _FakeKlineAdapter({"SZ300101": _bars(_UP_SERIES, day)})
    rows = run_offboard_watch(adp, db, [], snapshot={"SZ300101": _payload()})
    assert [r.tier for r in rows] == [T1]
    assert adp.calls == ["SZ300101"]
    assert db.execute("SELECT COUNT(*) FROM offboard_kline_cache").fetchone()[0] == 1


def test_run_offboard_watch_kline_fetch_limit_bounds_requests(db, monkeypatch):
    """补 K 线额度受 OFFBOARD_KLINE_FETCH_LIMIT 约束（保护主循环刷新节拍）。"""
    import scanner.offboard_watch as ow

    monkeypatch.setattr(ow, "OFFBOARD_KLINE_FETCH_LIMIT", 2)
    assert OFFBOARD_KLINE_FETCH_LIMIT >= 2  # 常量本身仍然存在且合理
    day = now_beijing().date().isoformat()
    snapshot = {f"SZ30010{i}": _payload(vol_ratio=5.0 - i * 0.5) for i in range(1, 6)}  # 5 只全过门
    adp = _FakeKlineAdapter({s: _bars(_UP_SERIES, day) for s in snapshot})
    run_offboard_watch(adp, db, [], snapshot=snapshot)
    assert len(adp.calls) == 2  # 只给量比最高的 2 只补
    assert set(adp.calls) == {"SZ300101", "SZ300102"}


def test_run_offboard_watch_survives_backfill_failure(db, monkeypatch):
    """回填失败不影响本轮结果（fail-open 的观测链路）。"""
    import scanner.offboard_watch as ow

    def _boom(*a, **k):
        raise OSError("db locked")

    monkeypatch.setattr(ow, "backfill_next_day", _boom)
    day = now_beijing().date().isoformat()
    rows = run_offboard_watch(
        None, db, [], klines={"SZ300101": _bars(_UP_SERIES, day)}, snapshot={"SZ300101": _payload()}
    )
    assert len(rows) == 1


# ── 两出口渲染 ──────────────────────────────────────────────────────────────


def _offboard_row(**kw) -> OffboardCandidate:
    return _cand(streak=None, rank_change=None, **kw)


# ── 开盘静默窗口（量比失真的对策）───────────────────────────────────────────


def _first_trading_day() -> _date:
    """今天起第一个交易日。窗口判定只关心「是不是交易日」，不关心是哪天。"""
    d = now_beijing().date()
    for _ in range(14):
        if is_trading_day(d):
            return d
        d += _td(days=1)
    raise AssertionError("14 天内找不到交易日（holidays.json 可疑）")


def _first_non_trading_day() -> _date:
    d = now_beijing().date()
    for _ in range(14):
        if not is_trading_day(d):
            return d
        d += _td(days=1)
    raise AssertionError("14 天内找不到非交易日（holidays.json 可疑）")


class TestOpeningSilence:
    """开盘 15 分钟内不产出 —— 修的是**量比失真**（分母趋 0 时商发散），不是阈值。

    量比 = 当日累计量 ÷（近 5 日均每分钟量 × 已交易分钟数）：开盘头几分钟分母只有
    几分钟，读数虚高一个量级。实测 09:33 捕获的 3 只 T1 虚高 6.6~11.5 倍且当日全数
    转跌；09:45 之后收敛到 ≈1.0。而量比正是本段排序键的主键。

    本类显式**打开**窗口（文件级 autouse 夹具默认把它关成 0 防 flaky）。
    窗口按 `trading_minutes_elapsed` 判定：午休 120 分钟已排除 —— 这正是不能用
    墙上时间差的原因，否则 11:30 与 13:00 会被算成「刚开盘一小时」。
    """

    @staticmethod
    def _at(hh: int, mm: int) -> datetime:
        return datetime.combine(_first_trading_day(), time(hh, mm))

    @pytest.fixture(autouse=True)
    def _enable(self, monkeypatch):
        monkeypatch.setattr("scanner.offboard_watch.OFFBOARD_OPENING_SILENCE_MIN", OFFBOARD_OPENING_SILENCE_MIN)

    def test_window_uses_config_constant(self):
        """守卫：本类确实把窗口打开了（否则下面几条会静默地全测成「不静默」）。"""
        from scanner import offboard_watch as ow

        assert OFFBOARD_OPENING_SILENCE_MIN > 0, "配置侧窗口被关掉，本类失去意义"
        assert ow.OFFBOARD_OPENING_SILENCE_MIN == OFFBOARD_OPENING_SILENCE_MIN

    def test_silent_right_after_open(self):
        assert opening_silence_active(self._at(9, 30)) is True

    def test_silent_one_minute_before_boundary(self):
        assert opening_silence_active(self._at(9, 44)) is True

    def test_not_silent_at_boundary(self):
        assert opening_silence_active(self._at(9, 45)) is False

    def test_not_silent_during_and_after_lunch_break(self):
        """午休不算「开盘头几分钟」：11:30 与 13:00 的已交易分钟数都是 120。"""
        assert opening_silence_active(self._at(11, 30)) is False
        assert opening_silence_active(self._at(13, 0)) is False

    def test_silent_before_open_and_on_non_trading_day(self):
        assert opening_silence_active(self._at(9, 0)) is True  # 开盘前 elapsed=0
        non_trading = datetime.combine(_first_non_trading_day(), time(11, 0))
        assert opening_silence_active(non_trading) is True  # 非交易日 elapsed=0

    def test_window_disabled_when_constant_zero(self, monkeypatch):
        monkeypatch.setattr("scanner.offboard_watch.OFFBOARD_OPENING_SILENCE_MIN", 0)
        assert opening_silence_active(self._at(9, 30)) is False

    def test_run_offboard_watch_returns_empty_inside_window(self, db, monkeypatch):
        """整条主流程在窗口内直接返回 [] —— 第一道门在读任何数据之前。"""
        monkeypatch.setattr("scanner.offboard_watch.opening_silence_active", lambda *a, **k: True)
        day = now_beijing().date().isoformat()
        snapshot = {"SZ300101": _payload(vol_ratio=2.0, percent=2.5)}
        klines = {"SZ300101": _bars(_UP_SERIES, day)}
        rows = run_offboard_watch(None, db, [], top_n=5, klines=klines, snapshot=snapshot)
        assert rows == []
        # 静默 = 不产出且不落库（不是「产出了但没展示」）
        assert db.execute("SELECT COUNT(*) FROM offboard_launch_log").fetchone()[0] == 0


def test_render_offboard_standalone_prints_b_segment(capsys):
    """独立渲染入口只画榜外异动段：列头与列规格与主屏同源，**不打榜内飙升段标题**。

    沿革：2026-09-21 区标题带榜内飙升段口径（「当日动能+热度跃升」），故该段空时
    连区标题一起不打；2026-10-13 区标题中性化后（只说样本面 + 三段结构），口径下沉
    到各段标题，于是「区标题随榜内飙升段有无而变」这条约束**失效**，区标题回到
    「任一段非空就打」。本入口只喂榜外异动段，故形如：区标题 → 榜外段标题 → 列头 → 行。
    （「与榜内飙升段同区、同一张表」的意图由列规格守卫承担：
      `test_hot_row_columns_match_terminal` + 下面的列头断言。）
    """
    from scanner.view.render import render_offboard_standalone

    c = _offboard_row(tier=T1, accum_5d=2.64, volume_ratio=2.0, main_pct=1.5)
    render_offboard_standalone([c])
    out = capsys.readouterr().out
    assert "榜外异动" in out
    assert "300101" in out
    assert "自检样本" in out
    assert "T1" in out  # 「层」列显分层标记
    assert "未回测" in out  # 段标题必须声明证据强度
    assert "—" in out  # 榜单专属列（排名上升/连击/评分）显 —，不是 0
    assert "◆ 沪深飙升 · 极有可能大涨" in out, "区标题中性化 ⇒ 任一段非空就打"
    assert "榜内飙升" not in out, "榜内飙升段没有数据 ⇒ 不打它的段标题"
    # 但列头必须在（榜内飙升段没打，榜外段自带一份）—— 否则 16 列数字没有列名，无从解读
    assert "排名上升" in out
    assert "换手%" in out
    assert out.index("榜外异动") < out.index("排名上升"), "列头应在榜外段标题之后"


def test_render_region_column_header_printed_exactly_once(capsys):
    """三段共用一张表 ⇒ 列头只打一份，且总在该段第一行数据之前。

    榜内飙升段非空：列头打在它行之前，后两段复用（不重复）；
    榜内飙升段为空：列头由第一个非空的后段自带（否则整表无列名）。

    ⚠ 断言必须数**列头行**而不是裸 `out.count("排名上升")`：2026-10-13 起榜内飙升段
      有了自己的段标题，其排序键文案里也含「排名上升」（`排序=评分↓(排名上升35/…)`），
      裸 count 会把段标题误计成第二、三份列头 —— 这正是本用例最初被写坏的方式。
    """
    from scanner.hot_watch import HotCandidate
    from scanner.view.model import COLS_HOT, _pad
    from scanner.view.render import _render_hot_watch_region

    header = " ".join(_pad(t, w, a) for t, w, a in COLS_HOT)

    a = HotCandidate(
        symbol="SZ300862",
        code="300862",
        name="蓝盾光电",
        exchange="SZ",
        current=50.10,
        percent=5.76,
        rank_change=1257,
        rank=3,
    )
    b = _offboard_row(tier=T2, code="300201", symbol="SZ300201", name="榜外样本")

    _render_hot_watch_region([a], [b])
    both = capsys.readouterr().out
    assert both.count(header) == 1
    assert both.index(header) < both.index("300862") < both.index("榜外异动")

    _render_hot_watch_region([], [b])
    b_only = capsys.readouterr().out
    assert b_only.count(header) == 1
    # 区标题中性化后任一段非空就打（它不再描述榜内飙升段的口径）
    assert "◆ 沪深飙升" in b_only
    assert b_only.index("榜外异动") < b_only.index(header) < b_only.index("300201")

    # ⚠ 2026-10-16 修复：榜内飙升段空 + 榜内异动 + 榜外异动**都非空** ⇒ 列头仍只一份。
    # 此前榜内异动段自带一份、榜外段又自带一份 ⇒ 同一张表出现两份 16 列列头。
    onboard = _offboard_row(tier=T1, code="300301", symbol="SZ300301", name="榜内样本")
    _render_hot_watch_region([], [b], [onboard])
    ob = capsys.readouterr().out
    assert ob.count(header) == 1
    assert ob.index("榜内异动") < ob.index(header) < ob.index("榜外异动")


def test_cli_conn_creates_every_table_the_persist_path_writes():
    """CLI 内存库必须建全 persist_round / _save_rejections 用到的表。

    2026-10-16 修复：此前只建 kline + launch_log，缺 offboard_round_log /
    offboard_rejections ⇒ persist_round 里 `INSERT INTO offboard_round_log` 的
    "no such table" 被 EXTERNAL_FAILURES 吞掉，连同一块里**已执行**的 launch_log
    插入一起永不提交（CLI 路径零落库）。
    """
    from scanner.offboard_watch import _cli_conn

    conn = _cli_conn()
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"offboard_round_log", "offboard_rejections"} <= tables
        persist_round(conn, [_cand()])
        assert conn.execute("SELECT COUNT(*) FROM offboard_launch_log").fetchone()[0] == 1
        # 能查到这一行 ⇒ 同一 try 块未抛「缺表」⇒ 整块跑到 commit（缺表时此句直接报错）
        assert conn.execute("SELECT rounds FROM offboard_round_log").fetchone()[0] == 1
    finally:
        conn.close()


# ── 「评分」/「层」拆列 + 三段段标题（2026-10-13）─────────────────────────


def _hot_a_row(**kw):
    from scanner.hot_watch import HotCandidate

    return HotCandidate(
        symbol="SZ300862",
        code="300862",
        name="榜内飙升样本",
        exchange="SZ",
        current=50.10,
        percent=5.76,
        rank_change=1257,
        rank=3,
        score=71.5,
        streak=1,
        **kw,
    )


def test_hot_cols_split_score_and_tier_into_separate_columns():
    """`COLS_HOT` 必须同时有「评分」与「层」两列，且各自宽度容得下该列的取值上界。

    合并成一列时的故障是：表头写「评分」，榜内异动段 / 榜外异动段的格子里却是 `T1`/`T2`
    —— 表头与格子语义对不上。一列一名 ⇒ 两列必须同时存在，不能退回合并态。
    """
    from scanner.view.model import COLS_HOT, _vis_len

    labels = [c[0] for c in COLS_HOT]
    assert "评分" in labels and "层" in labels
    assert labels.index("评分") < labels.index("层") < labels.index("连击")
    widths = {c[0]: c[1] for c in COLS_HOT}
    # 评分的上界 = 四权重和 100.0 → "100.0" 5 字符
    assert _vis_len(f"{100.0:.1f}") <= widths["评分"]
    # 「层」的取值只有 "T1"/"T2"/"—"，且表头「层」自身 2 列
    assert max(_vis_len("T1"), _vis_len("层")) <= widths["层"]


def test_hot_row_cells_put_score_on_a_segment_and_tier_on_b_segments():
    """**每段只用自己那两列里的一列**，用不到的那列显 `—`（不是 0）。"""
    from scanner.view.render import _hot_row_cells

    a_cells = _hot_row_cells(_hot_a_row(), 1, board_segment=True)
    b_cells = _hot_row_cells(_offboard_row(tier=T2), 1, board_segment=False)

    assert a_cells[-3] == "71.5", "榜内飙升段：评分列显复合分"
    assert a_cells[-2] == "—", "榜内飙升段无分层概念 ⇒ 层列显 —"
    assert b_cells[-3] == "—", "榜外段没有复合分 ⇒ 评分列显 —（不是 0.0）"
    assert b_cells[-2] == "T2", "榜外段：层列显分层标记"


def test_hot_row_name_colored_when_repeat_top10(monkeypatch):
    """「反复进前十」粉名（四版 2026-10-15）：判定只认 `repeat_days >= OFFBOARD_REPEAT_MIN_HITS`；
    未达门槛的榜外行与没有该字段的 A 段行（HotCandidate）都不得染色（getattr 兜底 0）。
    颜色用 MAGENTA（2026-10-14 用户反馈：与其他区域统一，不要五彩斑斓）。

    测试环境可能非 TTY（ANSI 退化为空串 ⇒ `"" in s` 恒真、断言 vacuous），故显式
    注入真 SGR 码后断言 —— 验证颜色分支确实执行，而非环境侥幸。
    """
    import scanner.view.render as render_mod
    from scanner.view.render import _hot_row_cells

    monkeypatch.setattr(
        render_mod,
        "ANSI",
        {**render_mod.ANSI, "MAGENTA": "[95m", "BOLD": "[1m", "RESET": "[0m"},
    )

    hit = _hot_row_cells(_offboard_row(repeat_days=OFFBOARD_REPEAT_MIN_HITS), 1, board_segment=False)
    assert "[95m" in hit[2], "达标行：名称列必须带粉红 ANSI（与其他区域同色）"
    assert "自检样本" in hit[2], "染色包裹名称，不替换名称本体"

    miss = _hot_row_cells(_offboard_row(repeat_days=OFFBOARD_REPEAT_MIN_HITS - 1), 1, board_segment=False)
    assert miss[2] == "自检样本", "未达门槛：名称列保持原样"

    a_cells = _hot_row_cells(_hot_a_row(), 1, board_segment=True)
    assert "[95m" not in a_cells[2], "A 段行没有 repeat_days ⇒ 永不误染"


def test_render_region_three_segments_each_have_own_subtitle(capsys):
    """三段名统一到「来源域·形态」同一维度，各自带段标题；区标题保持中性。

    区标题此前把**榜内飙升段**的口径（「当日动能+热度跃升」）挂在**整区**标题上 ——
    读者既判断不出榜内飙升段属榜内还是榜外，又在该段空时看到一张顶着区标题的空表。
    故口径必须下沉到段标题，且区标题里不得残留任一段的口径词。
    """
    from scanner.view.render import _render_hot_watch_region

    _render_hot_watch_region([_hot_a_row()], [_offboard_row(tier=T2)], [_offboard_row(tier=T1, code="300201")])
    out = capsys.readouterr().out

    for seg in ("榜内飙升", "榜内异动", "榜外异动"):
        assert f"— {seg}" in out, f"{seg} 段必须有独立段标题"
    region_line = next(ln for ln in out.splitlines() if "◆ 沪深飙升" in ln)
    # 区标题只说样本面与结构，不得夹带任一段的口径
    assert "当日动能" not in region_line
    assert "量先动" not in region_line
    assert "三段并列" in region_line


def test_render_region_tier_glossary_present_in_every_tiered_segment(capsys):
    """出现裸 `T1`/`T2` 的段，段标题里必须同时给出词表（T1=… / T2=…）。

    此前榜内异动段的段标题**完全不提层**，屏幕上只有一个无从解读的 `T1`；榜外段虽有
    排序键里的 `T1 量先动`，却没说明 T1 到底意味着什么。
    """
    from scanner.view.model import offboard_subtitle, onboard_subtitle
    from scanner.view.render import _render_hot_watch_region

    _render_hot_watch_region([], [_offboard_row(tier=T2)], [_offboard_row(tier=T1, code="300201")])
    out = capsys.readouterr().out
    assert "T1=量先动·价未动" in out, "榜内异动段段标题必须解释 T1"
    assert "T1=量先动·价未动" in offboard_subtitle(1)
    assert "T2=启动首日" in offboard_subtitle(1)
    assert "T1=量先动·价未动" in onboard_subtitle(1)


def test_render_region_skips_when_both_segments_empty(capsys):
    from scanner.view.render import _render_hot_watch_region

    _render_hot_watch_region([], [])
    assert capsys.readouterr().out == ""


def test_render_region_puts_b_segment_after_a_segment(capsys):
    """同区两段并列、不混排：A 段行在前，B 段小标题与其行在后。"""
    from scanner.hot_watch import HotCandidate
    from scanner.view.render import _render_hot_watch_region

    a = HotCandidate(
        symbol="SZ300862",
        code="300862",
        name="蓝盾光电",
        exchange="SZ",
        current=50.10,
        percent=5.76,
        rank_change=1257,
        rank=3,
    )
    b = _offboard_row(tier=T2, code="300201", symbol="SZ300201", name="榜外样本")
    _render_hot_watch_region([a], [b])
    out = capsys.readouterr().out
    assert out.index("300862") < out.index("榜外异动") <= out.index("300201")


# ── 「板块」列（2026-09-22）────────────────────────────────────────────────


def test_run_offboard_watch_fills_sector_from_cache(db):
    """B 段**产出阶段**就填好 `sector`（渲染层只读不算）：②级缓存命中 → F10 首要板块。"""
    from scanner.database import save_concepts_cache

    db.execute("CREATE TABLE concept_cache (symbol TEXT, concepts TEXT, updated TEXT)")
    save_concepts_cache(db, {"SZ300101": ["CPO概念", "光通信"]})
    day = now_beijing().date().isoformat()
    rows = run_offboard_watch(
        None, db, [], klines={"SZ300101": _bars(_UP_SERIES, day)}, snapshot={"SZ300101": _payload()}
    )
    assert rows, "内置样本应产出 T1"
    assert rows[0].sector == "CPO概念"


def test_run_offboard_watch_sector_degrades_to_name_keyword(db):
    """①②都落空 → ③名称关键词兜底，**不留空**（板块列不该整列 `—`）。"""
    day = now_beijing().date().isoformat()
    rows = run_offboard_watch(
        None, db, [], klines={"SZ300101": _bars(_UP_SERIES, day)}, snapshot={"SZ300101": _payload()}
    )
    assert rows
    assert rows[0].sector == "其他"  # 名称「自检样本」无关键词命中


def test_run_offboard_watch_never_fetches_boards_when_switch_off(db, monkeypatch):
    """开关关时②级不发任何 F10 请求（本文件 autouse 默认关，见 _offline_defaults）。"""
    import scanner.concept as concept_mod

    monkeypatch.setattr("scanner.offboard_watch.OFFBOARD_BOARD_FETCH", 0)
    day = now_beijing().date().isoformat()
    with patch.object(concept_mod, "_fetch_many") as mock_fetch:
        rows = run_offboard_watch(
            None, db, [], klines={"SZ300101": _bars(_UP_SERIES, day)}, snapshot={"SZ300101": _payload()}
        )
    mock_fetch.assert_not_called()  # 非空断言：开关传错（True）时必经 _collect_concepts → _fetch_many
    assert rows


def test_render_board_segment_shows_sector_column(capsys):
    """B 段自带列头时含「板块」，行内值就位（A 段空 → 列头打在 B 段小标题之后）。"""
    from scanner.view.render import render_offboard_standalone

    c = _offboard_row(tier=T1, accum_5d=2.64, volume_ratio=2.0, main_pct=1.5)
    c.sector = "CPO概念"
    render_offboard_standalone([c])
    out = capsys.readouterr().out
    assert "板块" in out
    assert "CPO概念" in out
    assert out.index("榜外异动") < out.index("板块") < out.index("CPO概念")


def test_render_region_shares_sector_column_header_across_segments(capsys):
    """A/B 共用一份列头 ⇒「板块」只打一次，两段行落在同一列语义下（列集不分叉）。"""
    from scanner.hot_watch import HotCandidate
    from scanner.view.render import _render_hot_watch_region

    a = HotCandidate(
        symbol="SZ300862",
        code="300862",
        name="蓝盾光电",
        exchange="SZ",
        current=50.10,
        percent=5.76,
        rank_change=1257,
        rank=3,
        sector="军工",
    )
    b = _offboard_row(tier=T2, code="300201", symbol="SZ300201", name="榜外样本", sector="CPO概念")
    _render_hot_watch_region([a], [b])
    out = capsys.readouterr().out
    assert out.count("板块") == 1, "列头必须只打一份（A/B 列集不分叉）"
    assert "军工" in out and "CPO概念" in out


def test_render_shows_dash_when_sector_blank(capsys):
    """板块取数失败（空）显 `—` 而非「其他」：两者语义不同（失败 ≠ 没匹配上）。"""
    from scanner.view.render import render_offboard_standalone

    c = _offboard_row(tier=T1, accum_5d=2.64, volume_ratio=2.0, main_pct=1.5)
    c.sector = ""
    render_offboard_standalone([c])
    out = capsys.readouterr().out
    assert "其他" not in out
    assert "板块" in out


def test_scan_view_offboard_rows_rendered_by_terminal(capsys):
    """终端主屏：`view.offboard_rows` 透传到独立区（与 hot_rows 分开承载）。

    2026-10-13：区标题中性化（只说样本面 + 三段结构，不描述榜内飙升段口径）后，
    「榜内飙升段空 → 连区标题一起不打」这条 2026-09-21 决策失效 —— 它针对的故障是
    「标题+列头孤悬在空表之上」，而标题一中性，该故障不复存在。仍不打的是**榜内飙升段
    的段标题与列头**（那一段没有数据），列头由榜外段自带。
    """
    from scanner.display import ScanView, render_terminal

    view = ScanView(
        main_rows=[],
        breakout_mark={},
        flow_pct_map={},
        last_ranks={},
        weak=False,
        warnings=[],
        hot_rows=None,
        offboard_rows=[_offboard_row()],
    )
    render_terminal(view)
    out = capsys.readouterr().out
    assert "榜外异动" in out
    assert "300101" in out
    assert "◆ 沪深飙升 · 极有可能大涨" in out, "区标题中性化 ⇒ 任一段非空就打"
    assert "榜内飙升" not in out, "榜内飙升段无数据 ⇒ 不打它的段标题"
    assert "排名上升" in out, "列头必须在（榜内飙升段没打则榜外段自带）"


# ── 已删除的摘要断言（2026-09-21）─────────────────────────────────────────
# 原 `test_summary_line_counts_offboard_rows` / `test_summary_distinguishes_none_from_empty_offboard`
# 两条断言挂在 `view.assemble._build_summary` 上（B 段入选「综合判断摘要」的计数行、
# 以及 None（本轮未产出）≠ []（跑了无结果）的完整度行语义）。
# 用户决策「终端只留四个区块」后 `_build_summary` / `ScanView.summary` /
# render_terminal 的「◆ 综合判断」区块整体删除，**被测对象已不存在**，故两条一并移除。
# 刻意留注释而不留空函数：空测试体恒真，等于给"这里守住了"的错觉 —— 本仓对此已
# 有过明确教训（常红守卫会被无视，恒绿空壳更糟）。
# ⚠ 恢复摘要时须连这两条一起从 git 历史取回：B 段「未回测」标注是**必留**的
#   ——B 段至今没有回测结论，摘要若不写「未回测」，读起来会与已回测的 A 段同权。


# ── CLI / 离线自检（回归哨兵）───────────────────────────────────────────────


def test_offline_demo_all_cases_match_expectation(capsys):
    """离线自检：内置样本逐条核对，全部命中（退出码 0）。

    这是 B 段筛选规则的**回归哨兵** —— 改动任何门槛/分层条件却没同步 `_DEMO_CASES`
    时，这里会红并指出具体哪条样本不符（对应 `python -m scanner.offboard_watch
    --offline-demo`）。
    """
    from scanner.offboard_watch import _DEMO_CASES, run_offline_demo

    rc = run_offline_demo(top_n=3, emit_json=False)
    out = capsys.readouterr().out
    assert rc == 0, f"离线自检未全绿：\n{out}"
    assert "FAIL" not in out
    # 表体每一行都给出了结论（没被中途截断/跳过）
    assert out.count("OK") == len(_DEMO_CASES)
    for expect, code, *_ in _DEMO_CASES:
        if expect in (T1, T2):
            assert code in out


def test_cli_main_offline_demo_returns_zero():
    assert main(["--offline-demo"]) == 0


def test_cli_json_output_is_parseable(capsys):
    import json

    assert main(["--offline-demo", "--json"]) == 0
    out = capsys.readouterr().out
    # 自检表里也有 "["（如排除理由「5日累计…不在[0,7)」），故从**最后一个行首** "[" 起切。
    rows = json.loads(out[out.rindex("\n[") + 1 :])
    assert rows, "自检样本应至少产出 T1/T2 各一只"
    assert {"code", "symbol", "name", "tier"} <= set(rows[0])
    # 排序键：T2 在 T1 之前（2026-09-21 翻转；实测 T1 当日/次日均弱于 T2）
    assert rows[0]["tier"] == T2


def test_cli_conn_is_isolated():
    """CLI 用内存库（DDL 与生产迁移同一份），不落生产 scanner.db。"""
    from scanner.offboard_watch import _cli_conn

    conn = _cli_conn()
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert {"offboard_kline_cache", "offboard_launch_log"} <= tables
    conn.execute(
        "INSERT INTO offboard_launch_log(date,symbol,name,tier,first_time,updated) VALUES('2026-01-01','SZ999999','测试','T1','x','x')"
    )
    assert conn.execute("SELECT COUNT(*) FROM offboard_launch_log").fetchone()[0] == 1
    conn.close()


# ── 自检样本与门槛的同步（防「自检绿灯但线上空转」）──────────────────────────


def test_demo_samples_cover_offboard_specific_branches(capsys):
    """自检样本必须覆盖本段**专属**门与两个分层失败族，否则改了阈值也不会被发现。

    通用 8 门的覆盖由 `test_gate_covers_every_universal_gate_keyword` 直接验证
    （样本表里不必每条都摆一份），这里只守本段专有的那几类。
    """
    from scanner.offboard_watch import _DEMO_CASES, run_offline_demo

    assert run_offline_demo(top_n=3, emit_json=False) == 0
    out = capsys.readouterr().out
    for keyword in ("量比不足", "成交额不足", "流通市值过小", "涨幅过高", "主力净流出", "ST"):
        assert keyword in out, f"自检样本未覆盖排除分支：{keyword}"
    for keyword in ("5日累计", "MA空头排列", "K线不足", "无K线数据", "主力净占比"):
        assert keyword in out, f"自检样本未覆盖分层分支：{keyword}"
    assert "MA未完全多头" not in out, "两层已同门（2026-09-22），不该再出现完全多头的拒绝理由"
    assert sum(1 for e, *_ in _DEMO_CASES if e == "跳过") == 3  # 在榜 / 主板 / 已推荐
    # 两层 MA 同门的三路交叉验证（样本表里的注释有完整说明）：
    #   部分多头序列在 T1/T2 两个带**都放行** —— 证明 T2 用的是「非空头」；
    #   空头序列在 T2 带被拦 —— 证明 T2 的 MA 门还在，不是被整个删了。
    assert sum(1 for e, *_ in _DEMO_CASES if e == "T1") == 2
    assert sum(1 for e, *_ in _DEMO_CASES if e == "T2") == 2
    # T2 的主力净占比门（300122）与 T1 的（300118）是两条独立样本，各算各的
    assert sum(1 for e, *_ in _DEMO_CASES if e == "主力净占比") == 2
