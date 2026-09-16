"""跨展示区通用风险门与标记（`scanner/display_gates.py`）单测。

被测语义（改动前先读）
------------------
1. **通用门 = 一套判定 + 一组单源阈值**：ST / 样本面 / 状态 / 报价 / 量 / 价格 /
   市值 / 资金流出 这八道门只有一个实现，三个展示区（v1 池选的主展示侧、
   沪深飙升、v1 回捞）都调它。
2. **区域可以收紧，不得放宽**：飙升区把市值上限收紧到 300 亿（主线 500 亿）、
   额外要求 exchange ∈ SH/SZ —— 这些是显式传参，不是各写一套。
3. **标记跨区通用**：`beauty_marks_daily` 是美感标记的唯一入口（日线档），
   与资金流的 `signals.fund_flow_signal` 一起，被三个区的行尾标记共用。

核心用例是 `test_three_regions_answer_the_same_for_the_same_danger`：
同一只危险票喂进**三个区各自的硬门**，必须得到同一句理由 —— 这是本次统一
真正要守住的不变量（此前三处各写一遍，改一处漏三处）。
"""

import pytest

from scanner.config import (
    FUND_OUTFLOW_NET_PCT,
    HIST_DIP_PCT,
    HIST_MAX_MARKET_CAP,
    HIST_MIN_VOL_RATIO,
    HOT_MAX_MARKET_CAP,
    MAX_STOCK_PRICE,
)
from scanner.display_gates import (
    UNIVERSAL_GATES,
    beauty_marks_daily,
    code_of,
    common_hard_gate,
    fund_outflow_hit,
)

# ── 判定语义（纯函数）──


@pytest.mark.parametrize(
    "ff,want",
    [(None, False), (0.0, False), (-7.99, False), (-8.0, True), (-20.0, True)],
)
def test_fund_outflow_boundary_is_inclusive_and_fails_open(ff, want):
    """闭区间（≤）：-8.0 命中；缺数据 fail-open（缺失 ≠ 流出，避免接口故障清空整屏）。"""
    assert fund_outflow_hit(ff) is want


def test_fund_outflow_threshold_is_single_source():
    """阈值单源 -8.0：与本仓唯一的资金流出阈值同源，别处不得出现字面量。"""
    assert FUND_OUTFLOW_NET_PCT == -8.0
    assert fund_outflow_hit(FUND_OUTFLOW_NET_PCT) is True


def test_code_of_strips_exchange_prefix():
    assert code_of("SZ300862") == "300862"
    assert code_of("300862") == "300862"
    assert code_of("SH600519") == "600519"
    assert code_of("") == ""


@pytest.mark.parametrize(
    "kwargs,keyword",
    [
        ({"name": "*ST宝馨"}, "ST"),
        ({"name": "天龙退"}, "ST"),
        ({"code": "002443"}, "非创业板"),
        ({"code": "688260"}, "非创业板"),
        ({"code": "159516"}, "非创业板"),
        ({"status": 0}, "非正常交易状态"),
        ({"current": 0.0}, "无有效报价"),
        ({"current": -1.0}, "无有效报价"),
        ({"volume": 0.0}, "无成交量"),
        ({"current": MAX_STOCK_PRICE + 1}, "价格过高"),
        ({"market_cap": 6e10}, "市值过大"),
        ({"ff_pct": FUND_OUTFLOW_NET_PCT}, "主力净流出"),
    ],
)
def test_common_gate_rejects_each_universal_branch(kwargs, keyword):
    """逐条覆盖通用门清单：每个分支都可达，且理由串含清单里的关键字。"""
    base = {"name": "正常票", "code": "300862", "current": 10.0}
    base.update(kwargs)
    reason = common_hard_gate(**base)
    assert reason is not None and keyword in reason, f"{kwargs} → {reason!r}"


def test_common_gate_passes_healthy_candidate():
    assert common_hard_gate(name="正常票", code="300862", current=10.0, market_cap=9.2e9, volume=1e7, status=1) is None


def test_common_gate_fails_open_on_missing_optional_fields():
    """可选项传 None = 本区拿不到该字段 → 跳过该门（显式缺口，不是静默放宽）。"""
    assert common_hard_gate(name="正常票", code="300862", current=10.0) is None
    assert common_hard_gate(name="正常票", code="300862", current=10.0, volume=None, status=None, ff_pct=None) is None
    # 市值为 0/缺失 → 不因市值排除（fail-open，宁可放过）
    assert common_hard_gate(name="正常票", code="300862", current=10.0, market_cap=0.0) is None


def test_universal_gate_list_matches_implementation():
    """清单与实现同步：清单里的每条都必须能在实现里命中（防止加了门忘了登记）。"""
    probes = {
        "ST": {"name": "*ST某"},
        "非创业板": {"code": "600519"},
        "非正常交易状态": {"status": 0},
        "无有效报价": {"current": 0.0},
        "无成交量": {"volume": 0.0},
        "价格过高": {"current": MAX_STOCK_PRICE + 1},
        "市值过大": {"market_cap": 1e12},
        "主力净流出": {"ff_pct": -30.0},
    }
    assert set(probes) == set(UNIVERSAL_GATES), "通用门清单与用例表不同步"
    for keyword in UNIVERSAL_GATES:
        base = {"name": "正常票", "code": "300862", "current": 10.0}
        base.update(probes[keyword])
        assert keyword in (common_hard_gate(**base) or ""), f"{keyword} 分支不可达"


# ── 收紧 vs 放宽 ──


def test_region_may_tighten_but_not_loosen():
    """区域收紧（更小上限）允许且必须显式传参；不得放宽。

    400 亿的票：主线通用默认（500 亿）放行，飙升区（300 亿）拦住 —— 差异来自
    调用点显式传的 `max_market_cap`，一眼可见。
    """
    cap_400 = 4e10
    assert common_hard_gate(name="票", code="300862", current=10.0, market_cap=cap_400) is None
    tightened = common_hard_gate(
        name="票", code="300862", current=10.0, market_cap=cap_400, max_market_cap=HOT_MAX_MARKET_CAP
    )
    assert tightened is not None and "市值过大" in tightened
    # 收紧只在本区生效：把同一只票喂给主线默认参数，仍然放行（不得反向放宽）
    assert common_hard_gate(name="票", code="300862", current=10.0, market_cap=cap_400) is None


# ── 三区同答（本文件的核心不变量）─────────────────────────────────────────

# 每个用例 = (通用门关键字, 构造三个区输入的字段覆盖)
# 输入刻意构造成「只命中通用门」：回捞区的回调/量比、飙升区的涨幅带全部满足，
# 于是谁报出来的理由只能是通用门 —— 三区不同答就说明某一区漏了这道门。
_UNIVERSAL_CASES = [
    ("ST", {"name": "*ST宝馨"}),
    ("非创业板", {"code": "002443"}),
    ("无有效报价", {"current": 0.0}),
    ("价格过高", {"current": MAX_STOCK_PRICE + 1}),
    ("市值过大", {"market_cap": 6e10}),
    ("主力净流出", {"ff_pct": -9.5}),
]


def _hot_reason(over: dict) -> str:
    from scanner.hot_watch import HotCandidate, hard_exclude

    fields = {
        "symbol": "SZ300862",
        "code": "300862",
        "name": "蓝盾光电",
        "exchange": "SZ",
        "current": 10.0,
        "percent": 3.0,
        "rank_change": 500,
        "rank": 3,
        "volume": 1e7,
        "market_capital": 9.2e9,
        "status": 1,
        "limit_up": 12.0,
        "limit_down": 8.0,
    }
    ff = over.pop("ff_pct", None)
    # 用例表用统一键名 market_cap，HotCandidate 的字段名是 market_capital
    if "market_cap" in over:
        over["market_capital"] = over.pop("market_cap")
    fields.update(over)
    return hard_exclude(HotCandidate(**fields), ff) or ""


def _hist_reason(over: dict) -> str:
    from scanner.historical_watch import hard_gate

    # 回捞区的取样条件（回调到位 + 量能承接）先满足，确保报出来的只能是通用门
    meta = {"symbol": "SZ300862", "name": "蓝盾光电", "rec_date": "2026-09-15", "rec_days_ago": 1,
            "rec_category": "momentum", "rec_score": 70}
    quote = {"current": 10.0, "percent": HIST_DIP_PCT - 0.5, "market_capital": 9.2e9}
    ff = over.pop("ff_pct", None)
    code = over.get("code")
    if code:
        meta = {**meta, "symbol": f"SZ{code}"}
    name = over.get("name")
    if name:
        meta = {**meta, "name": name}
    if "current" in over:
        quote = {**quote, "current": over["current"]}
    if "market_cap" in over:
        quote = {**quote, "market_capital": over["market_cap"]}
    return hard_gate(meta, quote, HIST_MIN_VOL_RATIO + 1.0, ff) or ""


@pytest.mark.parametrize("keyword,over", _UNIVERSAL_CASES, ids=[c[0] for c in _UNIVERSAL_CASES])
def test_three_regions_answer_the_same_for_the_same_danger(keyword, over):
    """同一只危险票喂进**三个区各自的硬门**，必须得到同一句理由。

    这是本次「风险过滤通用化」真正要守的不变量。三处各写一遍的年代，改一处漏三处
    是真实发生过的（历史上「资金流出门开关是死的」就是这么来的）。
    """
    reasons = {
        "通用门": common_hard_gate(
            name=over.get("name", "蓝盾光电"),
            code=over.get("code", "300862"),
            current=over.get("current", 10.0),
            market_cap=over.get("market_cap", 9.2e9),
            volume=1e7,
            status=1,
            ff_pct=over.get("ff_pct"),
        ),
        "沪深飙升": _hot_reason(dict(over)),
        "v1 回捞": _hist_reason(dict(over)),
    }
    for region, reason in reasons.items():
        assert reason and keyword in reason, f"{region} 未施加通用门「{keyword}」：{reason!r}"


def test_three_regions_allow_the_same_healthy_candidate():
    """反向对照：同一只健康票三个区都不得拦（否则就是「统一成了更严」）。"""
    assert common_hard_gate(name="蓝盾光电", code="300862", current=10.0, market_cap=9.2e9, volume=1e7, status=1) is None
    assert _hot_reason({}) == ""
    assert _hist_reason({}) == ""


# ── 各区专有门：**不得**被并进通用层 ──


def test_hot_keeps_its_own_percent_band():
    """飙升区要求「必须上涨且在涨幅带内」—— 这是取样定义，不是通用风险门。"""
    assert "涨幅过高" in _hot_reason({"percent": 9.9})
    assert "非上涨" in _hot_reason({"percent": -1.0})
    # 通用门本身不管涨幅（回捞区要的正是下跌的票）
    assert common_hard_gate(name="票", code="300862", current=10.0, market_cap=1e9) is None


def test_hist_keeps_its_own_dip_and_volume_conditions():
    """回捞区要求「回调到位 + 量能承接」—— 与飙升区方向相反，不并入通用层。"""
    from scanner.historical_watch import hard_gate

    meta = {"symbol": "SZ300862", "name": "蓝盾光电"}
    healthy = {"current": 10.0, "percent": HIST_DIP_PCT, "market_capital": 9.2e9}
    assert hard_gate(meta, healthy, HIST_MIN_VOL_RATIO, None) is None
    assert "未回调到位" in (hard_gate(meta, {**healthy, "percent": HIST_DIP_PCT + 0.1}, HIST_MIN_VOL_RATIO, None) or "")
    assert "量能萎缩" in (hard_gate(meta, healthy, HIST_MIN_VOL_RATIO - 0.01, None) or "")


def test_hist_and_hot_use_different_market_cap_ceilings():
    """市值上限是**区域参数**：回捞 500 亿、飙升 300 亿，都显式传参、都见注释。"""
    assert HIST_MAX_MARKET_CAP == 500 * 1e8
    assert HOT_MAX_MARKET_CAP == 300 * 1e8
    cap_400 = 4e10
    assert _hist_reason({"market_cap": cap_400}) == "", "回捞 500 亿上限应放行 400 亿"
    assert "市值过大" in _hot_reason({"market_cap": cap_400}), "飙升 300 亿上限应拦住 400 亿"


# ── 标记：美感日线档 ──


def _kline(n=25, *, rising=True):
    """构造 n 根日线：rising=True 为单调上行（MA 多头），False 为单调下行（MA 空头）。"""
    out = []
    for i in range(n):
        close = 10.0 + i * 0.5 if rising else 20.0 - i * 0.5
        out.append({"date": f"2026-09-{i + 1:02d}", "open": close, "close": close, "high": close, "percent": 1.0})
    return out


def test_beauty_marks_daily_passes_multi_head_trend():
    blocked, mark, detail = beauty_marks_daily(_kline(rising=True))
    assert blocked is False
    assert mark == "美"
    assert detail == "多头排列"


def test_beauty_marks_daily_blocks_broken_trend():
    blocked, mark, detail = beauty_marks_daily(_kline(rising=False))
    assert blocked is True, "可判定的丑必须拦住（美感门靠这个）"
    assert mark == ""


def test_beauty_marks_daily_fails_open_on_insufficient_data():
    """日线不足 → 不拦（fail-open）也不标 —— 数据缺口不该被读成「丑」。"""
    assert beauty_marks_daily(None) == (False, "", "日线不足")
    assert beauty_marks_daily([]) == (False, "", "日线不足")
    blocked, mark, detail = beauty_marks_daily([{"close": 10.0, "percent": 1.0}] * 5)
    assert (blocked, mark) == (False, "")


def test_beauty_marks_daily_never_returns_strong_mark():
    """**结构上**只可能返回「美」，不可能返回「美★」（★ 需要分时，两区都没有）。

    这条不是形式主义：库里 score_breakdown 存的是「上次推荐当日」的分时，一旦有人
    「顺手」把它喂进来，★ 就会静默出现，读者会误以为分时也漂亮。
    """
    assert beauty_marks_daily(_kline(rising=True))[1] == "美"
    assert "★" not in beauty_marks_daily(_kline(rising=True))[1]
