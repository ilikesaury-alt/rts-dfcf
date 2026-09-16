"""跨展示区通用风险门（2026-09-16 抽取，单一真相）。

为什么要这一层
--------------
三个展示区（v1 池选 / 沪深飙升 / v1 回捞）的候选来源、样本域、排序口径各不相同，
但「什么算危险」不该有三套答案。抽取前，ST / 样本面 / 状态 / 报价 / 量 / 价格 /
市值 / 资金流出 这八道门散在四处各写一遍：

  - 主线扫描侧 `candidates.filter_gem_stocks`（ST / 港股 / 创业板）+ `pipeline.pool`（价格 / 市值）
  - 主展示侧 `view.assemble`（资金流出，压在 `today_recs` 单点）
  - `hot_watch.hard_exclude`（ST / 样本面 / 状态 / 报价 / 量 / 涨跌停 / 涨幅 / 市值）
  - `historical_watch.hard_gate`（ST / 样本面 / 报价 / 价格 / 市值 / 回调 / 量比 / 资金流出）

阈值同源的、各写一份的都有，「改一处漏三处」是真实存在的风险——历史上
「资金流出门开关是死的」（switch 开了但没有任何票被剔除）就是这么来的。

统一原则（改之前先读，这是本模块的全部契约）
------------------------------------------
**通用门 = 一套判定实现 + 一组单源阈值；区域可以「收紧」，不得「放宽」。**
  - 收紧（阈值更保守：更小的市值上限、更严的涨幅上限）→ 允许，但必须**显式传参**，
    在调用点一眼可见（例：`max_market_cap=HOT_MAX_MARKET_CAP`）；
  - 放宽 → 不允许。它会让某个区悄悄比另一个区更宽松，而「某个区悄悄漏了一道门」
    正是本次统一要消灭的东西。

通用门集合（三个区都必须施加，顺序即判定顺序）
------------------------------------------
  ① ST/退市（`utils.is_st`）
  ② 样本面：仅创业板 300/301（`utils.is_gem`）
  ③ 非正常交易状态（status != 1；只有提供该字段的区检查）
  ④ 无有效报价（current <= 0）
  ⑤ 无成交量（volume <= 0；只有提供该字段的区检查）
  ⑥ 价格上限 `MAX_STOCK_PRICE`（200 元）
  ⑦ 市值上限 `MAX_MARKET_CAP`（500 亿；区域可收紧）
  ⑧ 资金流出：主力净占比 ≤ `FUND_OUTFLOW_NET_PCT`（-8%）

不纳入通用门（都有明确理由，勿「顺手统一」）
--------------------------------------
1. **风险标签硬过滤**（主力出货 / 趋势破位 / 财务风险 / 弱转强失效 / 当日翻绿+高开回落）：
   由候选引擎 `enhancer.set_risk_flags` 在**扫描时**判定，依赖 dims 里**分类专属**的
   validate_* 产物（`v_mo_ma` 只在 validate_momentum 写、`v_st_rank` 只在
   validate_short_term 写）。飙升/回捞两区刻意不跑候选引擎，无法重建这些 dims；
   要在展示层补一套判定，就是本仓最忌讳的「同名不同义」复制。主线侧该过滤发生在
   扫描链路（`candidates.candidate_excluded_by_risk`），不在展示层。
2. **各区核心取样条件**：飙升区要求上涨（> HOT_MIN_PERCENT 且 ≤ HOT_MAX_PERCENT）、
   回捞区要求回调到位（≤ HIST_DIP_PCT）且量能承接（≥ HIST_MIN_VOL_RATIO）。
   两个方向的阈值**语义相反**（一个要涨、一个要跌），它们是各区的取样定义而非风险门，
   合并会把两个区一起毁掉。
3. **涨跌停位置**（飙升区专有）：要由昨收推算涨跌停价，只有飙升区拿得到 last_close。

标记层（美感 / 资金流）
--------------------
本模块只管**判定**，不管成形：终端画 ANSI 三角（`view.model._fund_flow_icon_str`）、
卡片画 emoji（`feishu._FUND_FLOW_EMOJI`），两份成形表各自单源、判定都回到
`signals.fund_flow_signal`（资金流）与 `trend_beauty`（美感）——与主线行尾标记
（`view.model._entry_row_suffix`）同一分工。各区共用 `beauty_marks_daily` 取标记，
避免「同一个'美'字在三处各有一套准入条件」。
"""

from __future__ import annotations

from scanner.config import (
    FUND_OUTFLOW_NET_PCT,
    MAX_MARKET_CAP,
    MAX_STOCK_PRICE,
)
from scanner.trend_beauty import BEAUTY_MARK, DAILY_INSUFFICIENT, evaluate_daily_trend
from scanner.utils import _strip_exchange, is_gem, is_st, to_float

# 通用门清单（字符串前缀，测试与文档据此断言覆盖率）。
# 顺序 = `common_hard_gate` 的判定顺序；改顺序/增删都要同步 tests/test_display_gates.py
# 的「三区同答」参数化用例。
UNIVERSAL_GATES: tuple[str, ...] = (
    "ST",
    "非创业板",
    "非正常交易状态",
    "无有效报价",
    "无成交量",
    "价格过高",
    "市值过大",
    "主力净流出",
)


def fund_outflow_hit(ff_pct: float | None) -> bool:
    """「资金流出」判定单源：主力净占比 ≤ FUND_OUTFLOW_NET_PCT(-8.0%) → True。

    阈值唯一来源 `config_sources.FUND_OUTFLOW_NET_PCT`。数据缺失（None）→ False：
    缺失不等于流出（fail-open），避免资金流接口故障时把整屏推荐清空。

    这是**原始数值**入口；推荐行（dict/RecommendationRow）走
    `ranking.is_fund_outflow`，它负责先按回退链取出行内数值，再委托到这里比较
    —— 全仓只有这一处阈值比较。
    """
    v = to_float(ff_pct, default=None)
    return v is not None and v <= FUND_OUTFLOW_NET_PCT


def common_hard_gate(
    *,
    name: str,
    code: str,
    current: float,
    market_cap: float = 0.0,
    volume: float | None = None,
    status: int | None = None,
    ff_pct: float | None = None,
    max_price: float = MAX_STOCK_PRICE,
    max_market_cap: float = MAX_MARKET_CAP,
) -> str | None:
    """通用风险门：通过返回 None，命中返回**排除原因**（可直接进日志/落选审计）。

    参数里可选的（`volume` / `status` / `ff_pct`）传 None 表示「本区拿不到该字段，
    跳过这道门」——调用方必须清楚自己跳过了什么，这是刻意的显式缺口而不是静默放宽。

    `max_price` / `max_market_cap` 允许区域**收紧**（更小的值），见模块 docstring 的统一原则：
      - 本区价格上限取 200（= `MAX_STOCK_PRICE`，项目唯一价格门，主线池 / 回马枪 /
        回捞区三处同值）；
      - 飙升区市值上限取 300 亿（`HOT_MAX_MARKET_CAP`，比主线的 500 亿更严 ——
        「大盘股弹性不足」是本区自有口径，收紧方向允许）。
    """
    if is_st(name):
        return "ST/退市风险股"
    if not is_gem(code):
        return "非创业板个股"
    if status is not None and int(status) != 1:
        return f"非正常交易状态(status={int(status)})"
    cur = to_float(current, default=0.0) or 0.0
    if cur <= 0:
        return "无有效报价"
    if volume is not None and (to_float(volume, default=0.0) or 0.0) <= 0:
        return "无成交量(停牌或未成交)"
    if max_price is not None and max_price > 0 and cur > max_price:
        return f"价格过高({cur:.2f}>{max_price:.0f})"
    cap = to_float(market_cap, default=0.0) or 0.0
    if cap > 0 and max_market_cap is not None and max_market_cap > 0 and cap > max_market_cap:
        return f"市值过大({cap / 1e8:.0f}亿>{max_market_cap / 1e8:.0f}亿)"
    if fund_outflow_hit(ff_pct):
        return f"主力净流出({to_float(ff_pct, default=0.0):.1f}%)"
    return None


def code_of(symbol_or_code: str) -> str:
    """取 6 位纯代码（去掉 SH/SZ/BJ 前缀）。委托 `utils._strip_exchange`，不复制实现。"""
    return _strip_exchange(str(symbol_or_code or ""))


def beauty_marks_daily(kline: list | None) -> tuple[bool, str, str]:
    """一次判定同时给出「日线美感门是否拦下」与「日线美感标记」。

    返回 (blocked, mark, detail)：
      blocked = 日线**可判定**且不漂亮（可判定的丑才拦；数据不足 fail-open 放行）；
      mark    = ""（不漂亮或日线不足）/ "美"（日线漂亮）；
      detail  = 不漂亮的理由串（"MA未多头/长上影" 等）或 "多头排列" / "日线不足"，
                供排除日志与落选理由使用。

    为什么合并成一个函数：门与标记的日线准入条件**必须同源**，否则会出现
    「门放行了但这行不标美」或反之的错位；且两处各调一次 `evaluate_daily_trend`
    会把同一份 K 线指标算两遍。

    只有「美」一档，没有「美★」—— ★ 需要**分时**确认（`trend_beauty.beauty_mark`），
    飙升/回捞两区都不抓分时数据，结构上不可能有 ★。刻意不回落库 score_breakdown
    把 ★ 补出来：库里那份是「上次推荐当日」的分时，拿来冒充今日是错标。
    """
    fail, _score, detail = evaluate_daily_trend(kline)
    blocked = bool(fail)
    mark = "" if (blocked or detail == DAILY_INSUFFICIENT) else BEAUTY_MARK
    return blocked, mark, detail


__all__ = [
    "UNIVERSAL_GATES",
    "beauty_marks_daily",
    "code_of",
    "common_hard_gate",
    "fund_outflow_hit",
]
