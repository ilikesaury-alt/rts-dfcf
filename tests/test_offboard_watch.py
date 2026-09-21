"""offboard_watch（沪深飙升区 **B 段·榜外异动**）单测。

覆盖：门槛（通用 8 门 + 本段专属收紧门）、候选构建（榜外/已推荐/非创业板过滤）、
T1/T2 分层、排序键（A/B 不混排的结构性理由）、榜外 K 线池（**独立于 daily_kline**）、
逐日落库与次日收益回填（含两个防呆）、主流程端到端、两出口渲染、CLI 离线自检。

全部为密封单测：不触网、不依赖真实 `scanner.db`（用内存库 + 假 adapter）。
"""

import sqlite3
from datetime import date as _date
from datetime import timedelta as _td

import pytest

from scanner.config import (
    HOT_MAX_MARKET_CAP,
    HOT_MAX_PERCENT,
    HOT_MIN_PERCENT,
    MAX_MARKET_CAP,
    MOMENTUM_LAUNCH_ACCUM_MAX,
    MOMENTUM_LAUNCH_ACCUM_MIN,
    MOMENTUM_LAUNCH_TODAY_MAX,
    MOMENTUM_LAUNCH_TODAY_MIN,
    MOMENTUM_LAUNCH_VOL,
    OFFBOARD_KLINE_FETCH_LIMIT,
    OFFBOARD_MIN_AMOUNT,
    OFFBOARD_MIN_FLOAT_CAP,
    OFFBOARD_T1_MAIN_PCT_MIN,
    OFFBOARD_T1_TODAY_MAX,
    OFFBOARD_T2_TODAY_MAX,
    TREND_MARK_ENABLED,
    now_beijing,
)
from scanner.db.migrations import _OFFBOARD_KLINE_DDL, _OFFBOARD_LAUNCH_LOG_DDL
from scanner.display_gates import UNIVERSAL_GATES, beauty_marks_daily, common_hard_gate
from scanner.offboard_watch import (
    T1,
    T2,
    OffboardCandidate,
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
    persist_round,
    run_offboard_watch,
    sort_key,
)

# ── 夹具 / 构造器 ────────────────────────────────────────────────────────────


def _cand(**kw) -> OffboardCandidate:
    """构造一个默认「各项健康」的榜外候选（创业板、放量、温和上涨、主力净流入）。"""
    base = {
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
    """内存库：B 段自己的两张表 + 被断言「不得被写入」的两张主线表。"""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE appearances (symbol TEXT, name TEXT, date TEXT)")
    conn.execute("CREATE TABLE market_extra_cache (symbol TEXT, data_type TEXT, date TEXT, payload_json TEXT)")
    conn.execute("CREATE TABLE recommendations (symbol TEXT, date TEXT)")
    conn.execute("CREATE TABLE daily_kline (symbol TEXT, date TEXT, close REAL)")
    conn.execute(_OFFBOARD_KLINE_DDL)
    conn.execute(_OFFBOARD_LAUNCH_LOG_DDL)
    conn.commit()
    yield conn
    conn.close()


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
    assert f"[{MOMENTUM_LAUNCH_ACCUM_MIN:g},{MOMENTUM_LAUNCH_ACCUM_MAX:g})" in why


def test_classify_tier_t2_ma_not_bullish(monkeypatch):
    """T2 的 MA 完全多头门：非完全多头 → 不产出。

    MA 计算本身是 `trend_beauty.ma_bullish` 的职责（另有单测），这里只验证本模块
    对「明确非完全多头」的处理 —— 手搓一条「5 日累计在带内但 MA 非多头」的序列既脆又
    与那个函数耦合，故直接桩掉判定结果。

    ⚠ 候选必须落在 **T2 涨幅带**（`percent >= MOMENTUM_LAUNCH_TODAY_MIN`）：
    默认 `_cand()` 是 T1 带（percent=2.5），而 T1 走的是 `_ma_not_bearish`，
    桩 `ma_bullish` 对那条路径毫无影响 —— 这正是本用例曾被写成「T1 却断言 T2 行为」
    而误过的原因（2026-09-21 修）。
    """
    import scanner.offboard_watch as ow

    today = now_beijing().date().isoformat()
    monkeypatch.setattr(ow, "ma_bullish", lambda kline: False)
    c = _cand(percent=MOMENTUM_LAUNCH_TODAY_MIN, volume_ratio=2.4)
    tier, why = classify_tier(c, _bars(_UP_SERIES, today), today)
    assert tier is None
    assert why == "MA未完全多头"


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

    monkeypatch.setattr(ow, "ma_bullish", _spy)
    c = _cand(percent=MOMENTUM_LAUNCH_TODAY_MIN, volume_ratio=2.4)
    classify_tier(c, _bars(_UP_SERIES, today), today)
    assert seen, "ma_bullish 未被调用"
    assert today not in seen[0], "MA 判定收到了今日 bar（应剔除）"


def test_classify_tier_ma_undecidable_when_bars_short():
    """不足 20 根 → ma_bullish 返回 None → 按「不可判定」不产出（不当作空头，也不放过）。"""
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


def test_classify_tier_t1_requires_main_inflow():
    """T1 额外要求主力净占比 ≥ OFFBOARD_T1_MAIN_PCT_MIN；T2 不设该条（沿用启动口径）。"""
    today = now_beijing().date().isoformat()
    bars = _bars(_UP_SERIES, today)
    tier, why = classify_tier(_cand(percent=2.5, main_pct=OFFBOARD_T1_MAIN_PCT_MIN - 0.01), bars, today)
    assert tier is None
    assert "主力净占比" in why

    # 同样净流出的票走 T2 带 → 不被该条拦（说明它不是通用门，只是 T1 的加严）
    tier2, _ = classify_tier(_cand(percent=5.0, main_pct=-3.0), bars, today)
    assert tier2 == T2


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


def test_sort_key_puts_t1_first_regardless_of_volume_ratio():
    """T1 在前 = 「真正的提前」；量比再高只要是 T2 也排后。"""
    a = _cand(symbol="SZ300101", code="300101", tier=T1, volume_ratio=1.6)
    b = _cand(symbol="SZ300102", code="300102", tier=T2, volume_ratio=9.9)
    assert [c.code for c in sorted([b, a], key=sort_key)] == ["300101", "300102"]


def test_sort_key_orders_by_volume_ratio_then_main_pct():
    a = _cand(symbol="SZ300101", code="300101", volume_ratio=2.0, main_pct=1.0)
    b = _cand(symbol="SZ300102", code="300102", volume_ratio=2.0, main_pct=5.0)
    c = _cand(symbol="SZ300103", code="300103", volume_ratio=3.0, main_pct=0.0)
    assert [x.code for x in sorted([a, b, c], key=sort_key)] == ["300103", "300102", "300101"]


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
        " first_time, updated, next_day_pct FROM offboard_launch_log WHERE date=? AND symbol=?",
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
    assert row[9] == row[10]  # 首次写入：first_time == updated
    assert row[11] is None  # 待回填


def test_persist_round_keeps_first_time_and_refreshes_values(db):
    """`first_time` 只在首次写入时记（否则「当日何时首次产出」会被最后一轮覆盖）。"""
    c = _cand(percent=2.5)
    persist_round(db, [c])
    day = now_beijing().date().isoformat()
    db.execute(
        "UPDATE offboard_launch_log SET first_time='2000-01-01T00:00:00' WHERE date=? AND symbol=?",
        (day, c.symbol),
    )
    db.commit()
    c.percent = 2.9
    c.tier = T2
    persist_round(db, [c])
    row = db.execute(
        "SELECT first_time, percent, tier FROM offboard_launch_log WHERE date=? AND symbol=?", (day, c.symbol)
    ).fetchone()
    assert row[0] == "2000-01-01T00:00:00"  # 首见时刻不被覆盖
    assert row[1] == pytest.approx(2.9)  # 信号原始值每轮刷新（同 market_extra_cache 语义）
    assert row[2] == T2
    assert db.execute("SELECT COUNT(*) FROM offboard_launch_log").fetchone()[0] == 1  # (date, symbol) 唯一


def test_persist_round_empty_is_noop(db):
    persist_round(db, [])
    assert db.execute("SELECT COUNT(*) FROM offboard_launch_log").fetchone()[0] == 0


# ── 次日收益回填（两个防呆）──────────────────────────────────────────────────


def _insert_signal(db, symbol: str, day: str, tier: str = T1) -> None:
    db.execute(
        "INSERT INTO offboard_launch_log(date,symbol,name,tier,percent,first_time,updated)"
        " VALUES(?,?,?,?,?,?,?)",
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
    pct = db.execute(
        "SELECT next_day_pct FROM offboard_launch_log WHERE date=? AND symbol=?", (d0, sym)
    ).fetchone()[0]
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
    assert (
        db.execute("SELECT next_day_pct FROM offboard_launch_log WHERE date=?", (today,)).fetchone()[0] is None
    )


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


def test_run_offboard_watch_end_to_end(db):
    day = now_beijing().date().isoformat()
    snapshot = {
        "SZ300101": _payload(vol_ratio=1.6, percent=2.5, main_pct=2.0),  # T1
        "SZ300102": _payload(vol_ratio=3.0, percent=5.0, main_pct=1.0),  # T2
        "SZ300103": _payload(vol_ratio=1.0),  # 量比不足 → 门槛剔除
    }
    klines = {"SZ300101": _bars(_UP_SERIES, day), "SZ300102": _bars(_UP_SERIES, day)}
    rows = run_offboard_watch(None, db, [], top_n=5, klines=klines, snapshot=snapshot)
    assert [r.tier for r in rows] == [T1, T2]  # T1 在前，与量比排序无关
    assert [r.code for r in rows] == ["300101", "300102"]
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


def test_render_offboard_standalone_prints_b_segment(capsys):
    from scanner.view.render import render_offboard_standalone

    c = _offboard_row(tier=T1, accum_5d=2.64, volume_ratio=2.0, main_pct=1.5)
    render_offboard_standalone([c])
    out = capsys.readouterr().out
    assert "沪深飙升" in out  # 与 A 段同区（同一张表）
    assert "榜外异动" in out
    assert "300101" in out
    assert "自检样本" in out
    assert "T1" in out  # 「评分」列改显分层标记
    assert "未回测" in out  # 小标题必须声明证据强度
    assert "—" in out  # 榜单专属列（排名上升/连击）显 —，不是 0


def test_render_region_skips_when_both_segments_empty(capsys):
    from scanner.view.render import _render_hot_watch_region

    _render_hot_watch_region([], [])
    assert capsys.readouterr().out == ""


def test_render_region_puts_b_segment_after_a_segment(capsys):
    """同区两段并列、不混排：A 段行在前，B 段小标题与其行在后。"""
    from scanner.hot_watch import HotCandidate
    from scanner.view.render import _render_hot_watch_region

    a = HotCandidate(
        symbol="SZ300862", code="300862", name="蓝盾光电", exchange="SZ",
        current=50.10, percent=5.76, rank_change=1257, rank=3,
    )
    b = _offboard_row(tier=T2, code="300201", symbol="SZ300201", name="榜外样本")
    _render_hot_watch_region([a], [b])
    out = capsys.readouterr().out
    assert out.index("300862") < out.index("榜外异动") <= out.index("300201")


def test_scan_view_offboard_rows_rendered_by_terminal(capsys):
    """终端主屏：`view.offboard_rows` 透传到独立区（与 hot_rows 分开承载）。"""
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


def test_summary_line_counts_offboard_rows():
    """综合判断摘要只各自计数、不跨区排序；B 段报「N 只（T1 x·未回测）」。"""
    from scanner.view.assemble import _build_summary

    lines = _build_summary(
        main_rows=[],
        hist_rows=[],
        hot_rows=[],
        offboard_rows=[_offboard_row(tier=T1), _offboard_row(tier=T2), _offboard_row(tier=T1)],
        beauty_mark=None,
        flow_pct_map={},
        flow_filtered=0,
        chase_filtered=0,
        tactic_filtered=0,
        weak=False,
        market_idx_pct=None,
    )
    joined = "\n".join(lines)
    assert "榜外 3 只（T1 2·未回测）" in joined


def test_summary_distinguishes_none_from_empty_offboard():
    """None（本轮未产出）≠ []（跑了无结果）：计数都是 0，但完整度行只在确有时打。"""
    from scanner.view.assemble import _build_summary

    def _lines(offboard):
        return "\n".join(
            _build_summary(
                main_rows=[],
                hist_rows=[],
                hot_rows=[],
                offboard_rows=offboard,
                beauty_mark=None,
                flow_pct_map={},
                flow_filtered=0,
                chase_filtered=0,
                tactic_filtered=0,
                weak=False,
                market_idx_pct=None,
            )
        )

    assert "榜外 0 只" in _lines([])
    assert "榜外段未产出" not in _lines([])  # 跑了但无结果 ≠ 没跑
    assert "榜外段未产出" in _lines(None)


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
    # 排序键：T1 在 T2 之前
    assert rows[0]["tier"] == T1


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
    for keyword in ("5日累计", "MA空头排列", "MA未完全多头", "K线不足", "无K线数据", "主力净占比"):
        assert keyword in out, f"自检样本未覆盖分层分支：{keyword}"
    assert sum(1 for e, *_ in _DEMO_CASES if e == "跳过") == 3  # 在榜 / 主板 / 已推荐
    # MA 判据按层分化的两极哨兵：同一条「EMA 部分多头」序列在 T1 放行、在 T2 拦下。
    assert sum(1 for e, *_ in _DEMO_CASES if e == "T1") == 2
    assert sum(1 for e, *_ in _DEMO_CASES if e == "T2") == 1
