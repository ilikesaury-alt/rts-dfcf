"""沪深飙升区 **B 段「榜外异动」**（2026-09-18）：榜外创业板 · 量先动 / 启动首日 两层观察段。

为什么要有这一段
----------------
「沪深飙升」区（`hot_watch`）的候选**只来自雪球飙升榜** —— 榜内前 `HOT_ENRICH_LIMIT`
个名额。榜单天然滞后（好票等上榜时已涨一截），而榜外近 4300 只票结构上永远进不来。
B 段把**项目已在手但从未被消费**的全市场快照（`market_extra_cache` 的 fund_flow 行，
5305 只，除资金流外还带价/涨幅/量比/换手/成交额/流通市值）拿来做**榜外创业板**的
提前观察：T1「量先动·价未动」与 T2「启动首日」。

三条硬边界（与 A 段一致，改动前先读）
------------------------------------
1. **样本面**：仅创业板（300/301），走 `hot_watch.is_hot_universe` 同一实现；
2. **不进主线任何链路**：不写 `recommendations`、不参与复合评分/档位/🎯 画像、
   不进飞书主卡片的去重键；落库只进本区自己的 `offboard_launch_log`；
3. **不给操作建议**：本项目已证「系统整体不赚钱」，且榜外票流动性更差、滑点更大
   ⇒ B 段定位是**信号观察段**，且**尚无历史背书**（见下）。

口径来源（不许复制）
--------------------
- T2「启动首日」的阈值**直接 import** `config_scoring.MOMENTUM_LAUNCH_*` ——
  同一套阈值的第二个定义就是本仓最忌讳的「同名不同义」；
- T1/T2 的分界点也取 `MOMENTUM_LAUNCH_TODAY_MIN`(3.5%)：低于它 = 「价还没动」，
  达到它 = 「已启动」。T1 不是新发明的语义，而是既有启动定义的**下沿延伸**；
- **两层共用同一条 MA 判据**「MA 非空头」= `features.ma_alignment_score > 0`
  （经本模块 `_ma_not_bearish` 收口 None 语义）—— 2026-09-22 起两层不再分化，
  见 `classify_tier` docstring 里那段「为什么要取消分化」；
  「顶背离」= `validator.mo_divergence`；「5 日累计」= `utils.accum_5d`（下沿
  `MOMENTUM_LAUNCH_ACCUM_MIN` 同源，上沿 `OFFBOARD_ACCUM_MAX` **B 段私有**）；
  「主力净占比 ≥ `OFFBOARD_MAIN_PCT_MIN`」= T1/T2 同门；
  「通用风险门」= `display_gates.common_hard_gate`。本模块不自造任何一条判定。

⚠ 尚未回测：T2 的常量是在**榜上**样本校准的，域迁移到榜外不保证成立。榜外 K 线池 +
`offboard_launch_log` 逐日落库是上线前唯一的硬前置 —— 没有按日样本，调阈值就是
无标签调参（项目铁律 observe-first）。

数据通路（三条，全部离线可得）
----------------------------
1. `market_extra_cache`（date=today, data_type='fund_flow'）：全市场快照，零额外请求；
2. **榜外 K 线池** `offboard_kline_cache`：🔴 **独立于 `daily_kline`** —— 后者的既定
   语义是「榜单衍生池」（999 只里 982 只在 appearances），塞入榜外票会污染所有基于
   它的回测基准与归因（portfolio_backtest / prevday_perf / 召回率口径）。
   按 (symbol, fetch_date) 缓存，当日抓一次当日复用（5 日累计/MA 只依赖抓取日之前的 bar）；
3. 通用 8 门 + 两道**收紧**的榜外专属门（显式传参，见 `offboard_gate`）。

fail-open / fail-closed
-----------------------
- 快照缺失（本轮未落库）→ 返回 []（本区留空，不告警噪音）；
- K 线补取失败 / 不足 20 根 → **该票不产出**。T1/T2 的条件里含「5 日累计 ∈ [0,7)」
  与「MA 判据」两个**必须**成立项，验不了就不该报 —— 这与风险门的 fail-open 语义
  **不同**（风险门宁可放过，信号门不能凭空产出）。这正是两个 MA 判定函数都返回
  `bool | None` 而不替调用方兜底的原因；
- 落库失败 → 只告警，本轮结果照常返回。

2026-09-21 的两处修正（都落在「测量」层，**不是**阈值层）
--------------------------------------------------------
1. **开盘静默**（`OFFBOARD_OPENING_SILENCE_MIN` / `opening_silence_active`）：
   开盘满 15 分钟才产出，作为 `run_offboard_watch` 的**第一道门**。依据是量比在开盘
   头几分钟存在**量纲性失真**（商的分母趋 0 时发散），而量比正是本段第一排序键 ——
   实测 09:33 捕获的 3 只 T1 量比虚高 6.6~11.5 倍，且是当日唯三由涨转跌的票。
2. **落库冻结**（`persist_round` + 迁移 `m016`）：信号值改为「首次写入即冻结」，
   另立 `last_hit_time` 列记最后命中。旧语义「最后写入即收盘值」会把票的**信号身份**
   （T1/T2、捕获时的涨幅）覆盖掉，事后无法还原它以什么身份被推出。

⚠ 本段**筛选常量一个没动**：样本只有 2 个交易日 / 19 行 —— 够证伪「量比在开盘几分钟
可用」（那是数学性质，不是统计推断），远不够支撑任何阈值调整（项目铁律 observe-first）。
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date as _date
from datetime import datetime
from datetime import timedelta as _td
from typing import Sequence

from scanner.concept import attach_display_boards
from scanner.config import (
    HOT_MAX_MARKET_CAP,
    HOT_MIN_PERCENT,
    MOMENTUM_LAUNCH_ACCUM_MIN,
    MOMENTUM_LAUNCH_TODAY_MIN,
    MOMENTUM_LAUNCH_VOL,
    OFFBOARD_ACCUM_MAX,
    OFFBOARD_BOARD_FETCH,
    OFFBOARD_DISPLAY_TOP,
    OFFBOARD_KLINE_DAYS,
    OFFBOARD_KLINE_FETCH_LIMIT,
    OFFBOARD_KLINE_WORKERS,
    OFFBOARD_MAIN_PCT_MIN,
    OFFBOARD_MIN_AMOUNT,
    OFFBOARD_MIN_FLOAT_CAP,
    OFFBOARD_OPENING_SILENCE_MIN,
    OFFBOARD_T1_TODAY_MAX,
    OFFBOARD_T2_TODAY_MAX,
    TREND_MARK_ENABLED,
    V_MO_DIVERGENCE_BEAR,
    now_beijing,
)
from scanner.data_source import ak_to_xq
from scanner.db.dal import save_offboard_rejections
from scanner.db.queries import get_market_extra_snapshot, get_symbol_names
from scanner.display_gates import beauty_marks_daily, code_of, common_hard_gate
from scanner.features import ma_alignment_score
from scanner.hot_watch import is_hot_universe
from scanner.models import make_kline_bar
from scanner.trading_session import trading_minutes_elapsed
from scanner.trend_beauty import DAILY_BEAUTY_MIN_BARS
from scanner.utils import EXTERNAL_FAILURES, accum_5d, to_float
from scanner.validator import mo_divergence

logger = logging.getLogger(__name__)

# MA 判定的最小 bar 数（与 `trend_beauty.ma_bullish` 同源，不写字面量 20）
_MA_MIN_BARS = DAILY_BEAUTY_MIN_BARS

# 两层标记（也是 `offboard_launch_log.tier` 的取值域）
T1 = "T1"  # 量先动·价未动
T2 = "T2"  # 启动首日
# 层序（2026-09-21 **翻转**）：T2 在前。T1 按定义就是「今日涨幅 < 3.5%」= 当日势能最弱
# 的一层，置顶等于必然把最不符合价值函数的那层放第一屏（实测见 sort_key docstring）。
_TIER_RANK = {T2: 0, T1: 1}

_KLINE_TABLE = "offboard_kline_cache"
_LOG_TABLE = "offboard_launch_log"


@dataclass
class OffboardCandidate:
    """B 段一行。字段名与 `hot_watch.HotCandidate` 的**渲染面**对齐 —— 两段同区同一张表，
    两个出口（终端 / 飞书）的取值代码才可能共用一份，不必为 B 段再写一套列宽规则。

    `rank_change` / `streak` 是榜单（A 段）专属量：B 段**结构上**没有，故为 None，
    两出口渲染为 `—`（不是 0 —— 0 会被读成「排名没动」）。
    """

    symbol: str
    code: str
    name: str
    tier: str
    current: float
    percent: float
    accum_5d: float
    volume_ratio: float
    main_pct: float
    volume: float = 0.0
    amount: float = 0.0
    market_capital: float = 0.0  # 总市值（快照 f20）
    float_market_capital: float = 0.0  # 流通市值（快照 f21）
    turnover_rate: float = 0.0  # 换手率（快照 f8，东财口径 %）
    exchange: str = ""
    status: int = 1
    limit_up: float = 0.0
    limit_down: float = 0.0
    # B 段**没有**复合分：排序键是 sort_key 的元组（不引入复合权重）。渲染时该列改显 tier。
    score: float = 0.0
    rank_change: int | None = None
    streak: int | None = None
    ff_pct: float | None = None
    beauty: str = ""
    # 板块（2026-09-22）：与 A 段 `HotCandidate.sector` 同义同源（`concept.attach_display_boards`
    # 在返回前就地填），取值与 v1 池选「板块」列同一条回退链。空 = 取数失败 → 渲染 `—`。
    sector: str = ""
    reasons: list[str] = field(default_factory=list)


def sort_key(c: OffboardCandidate) -> tuple:
    """B 段排序键：**T2 在前** → 量比降序 → 主力净占比降序。

    层序 2026-09-21 由「T1 在前」**翻转**为「T2 在前」。原序的理由是「T1 = 真正的
    提前」，但 T1 按定义就是「今日涨幅 < 3.5%」（带 `(HOT_MIN_PERCENT,
    OFFBOARD_T1_TODAY_MAX)`）—— 与 T2「启动首日」相比，它是**当日势能最弱的一层**；
    把它置顶等于系统性把最不符合价值函数的那层放第一屏。实测（同一份落库快照）：

      - 09-21 当日：T1 −0.76pp vs T2 +1.28pp（n=8 / 6）；
      - 09-18 次日：T1 +0.29% vs T2 +4.34%。

    两层都**保留**（T1 的「价未动」样本对观察段仍有价值），只是不再占据首位。

    不引入复合权重：项目已证复合排序在无标签时必然过拟合（推荐池 `score` 排序
    AUC 0.469 < 0.5）。段内主键取量比，机制依据是「量比 = 当日每分钟均量 ÷ 近 5 日
    每分钟均量」= **增量资金**的直接代理，且与「量能领先于价格」的假设同向。
    ⚠ 量比在开盘头几分钟虚高一个量级（分母只有几分钟）⇒ **本排序键的正确性依赖
    `OFFBOARD_OPENING_SILENCE_MIN` 那道静默窗口**，两者是一组，不可只删其一。

    ⚠ A/B 两段**不混排**（同一张表内分段并列）：A 段的复合分里 `rank_change` 独占
    35/100，榜外票恒缺该项 ⇒ 混排必被永久压到最末。这不是「口径不可比」的抽象说法，
    而是具体的 35/100 结构性缺失。
    """
    return (_TIER_RANK.get(c.tier, 9), -c.volume_ratio, -c.main_pct)


# ── 门槛 ────────────────────────────────────────────────────────────────────


def offboard_gate(c: OffboardCandidate) -> str | None:
    """B 段门槛：通用 8 门（单源）+ 本段专属条件。通过返回 None。

    分两类，**刻意放在一起**是因为两者的性质都是「这一票不该进候选集」，
    分开写会让 K 线补取额度浪费在注定被刷掉的票上：

    1. **通用风险门**（`display_gates.common_hard_gate`）：ST / 样本面 / 状态 /
       报价 / 量 / 价格 / 市值 / 资金流出。市值上限按本区口径显式**收紧**到
       `HOT_MAX_MARKET_CAP`(300 亿，比主线的 500 亿更严)。
    2. **榜外专属门（方向 = 收紧）与取样条件**：
       - 成交额 ≥ `OFFBOARD_MIN_AMOUNT`、流通市值 ≥ `OFFBOARD_MIN_FLOAT_CAP`：
         实测榜外主体是冷门低换手小微盘（换手 p50 1.0%、流通市值 p50 25 亿），
         成交额过低的票少量资金即可操纵分时，**指标本身没有参考价值**；
       - 涨幅带 `HOT_MIN_PERCENT < percent ≤ OFFBOARD_T2_TODAY_MAX`：与 A 段同一
         涨幅带（不追高、不接跌）；
       - 量比 ≥ `MOMENTUM_LAUNCH_VOL`：T1/T2 的**共性**条件，提前施加可避免为
         低量比的票白补 K 线（它同时是启动定义与 T1 的放量门）。
    """
    reason = common_hard_gate(
        name=c.name,
        code=c.code,
        current=c.current,
        market_cap=c.market_capital,
        volume=c.volume,
        status=c.status,
        ff_pct=c.main_pct,
        max_market_cap=HOT_MAX_MARKET_CAP,
    )
    if reason:
        return reason
    if not is_hot_universe(c.exchange, c.code):
        return "非创业板个股(非沪深交易所)"
    if c.percent <= HOT_MIN_PERCENT:
        return "当前非上涨状态"
    if c.percent > OFFBOARD_T2_TODAY_MAX:
        return f"涨幅过高({c.percent:.2f}%>{OFFBOARD_T2_TODAY_MAX:.0f}%)"
    if c.volume_ratio < MOMENTUM_LAUNCH_VOL:
        return f"量比不足({c.volume_ratio:.2f}<{MOMENTUM_LAUNCH_VOL})"
    if c.amount < OFFBOARD_MIN_AMOUNT:
        return f"成交额不足({c.amount / 1e4:.0f}万<{OFFBOARD_MIN_AMOUNT / 1e4:.0f}万)"
    if c.float_market_capital < OFFBOARD_MIN_FLOAT_CAP:
        return f"流通市值过小({c.float_market_capital / 1e8:.1f}亿<{OFFBOARD_MIN_FLOAT_CAP / 1e8:.0f}亿)"
    return None


# ── 候选构建（快照 → 榜外候选）────────────────────────────────────────────────


def _snapshot_row_to_candidate(symbol: str, payload: dict, names: dict[str, str]) -> OffboardCandidate:
    """全市场快照的一行 → `OffboardCandidate`（**未过门**）。

    字段名映射（东财 clist 字段码见 `market_extra._FUND_FLOW_FIELDS`）：
    price→现价、percent→今日涨幅、amount→成交额、float_cap→流通市值、
    total_cap→总市值、turnover→换手率、vol_ratio→量比、main_pct→主力净占比。
    """
    code = code_of(symbol)
    return OffboardCandidate(
        symbol=symbol,
        code=code,
        name=str(payload.get("name") or names.get(symbol) or code),
        tier="",  # 由 classify_tier 决定
        current=to_float(payload.get("price"), 0.0) or 0.0,
        percent=to_float(payload.get("percent"), 0.0) or 0.0,
        accum_5d=0.0,  # 由 K 线算
        volume_ratio=to_float(payload.get("vol_ratio"), 0.0) or 0.0,
        main_pct=to_float(payload.get("main_pct"), 0.0) or 0.0,
        volume=to_float(payload.get("volume"), 0.0) or 0.0,
        amount=to_float(payload.get("amount"), 0.0) or 0.0,
        market_capital=to_float(payload.get("total_cap"), 0.0) or 0.0,
        float_market_capital=to_float(payload.get("float_cap"), 0.0) or 0.0,
        turnover_rate=to_float(payload.get("turnover"), 0.0) or 0.0,
        exchange=symbol[:2],
        ff_pct=to_float(payload.get("main_pct"), None),
    )


def build_candidates(
    snapshot: dict[str, dict],
    board_items: Sequence[dict],
    names: dict[str, str],
    exclude_symbols: set[str] | None = None,
) -> tuple[list[OffboardCandidate], list[tuple[str, str]]]:
    """全市场快照 → **榜外创业板**、已过 `offboard_gate` 的候选（按量比降序）。

    返回 (候选, 排除明细 [(symbol, 原因)])。明细只用于日志/自检，不落库。

    「榜外」= 不在本轮飙升榜里（`board_items` 的 6 位代码集合）。`exclude_symbols`
    另排今日已推荐票 —— 与 v1 回捞区同款先例，避免同一只票在主表和 B 段同屏两现。
    候选按量比降序：决定 K 线补取额度（`OFFBOARD_KLINE_FETCH_LIMIT`）优先给谁。
    """
    board_codes = {code_of(str(it.get("symbol") or "")) for it in board_items if it.get("symbol")}
    skip = exclude_symbols or set()
    out: list[OffboardCandidate] = []
    rejects: list[tuple[str, str]] = []

    for symbol, payload in snapshot.items():
        if not payload:
            continue
        code = code_of(symbol)
        # 先做零成本过滤（代码前缀 / 榜内 / 已推荐），再构造对象跑门 —— 门里有价格、
        # 市值等多个浮点比较，对 5000 多行全跑一遍是纯浪费。
        if not code.startswith(("300", "301")):
            continue
        if code in board_codes or symbol in skip:
            continue
        c = _snapshot_row_to_candidate(symbol, payload, names)
        reason = offboard_gate(c)
        if reason:
            rejects.append((symbol, reason))
            continue
        out.append(c)

    out.sort(key=lambda x: -x.volume_ratio)
    return out, rejects


# ── T1 / T2 分层 ────────────────────────────────────────────────────────────


def classify_tier(c: OffboardCandidate, klines: list | None, today: str) -> tuple[str | None, str]:
    """K 线相关的分层判定 → (tier | None, 理由)。

    None = **不产出**：既包含「条件不满足」，也包含「数据不足无法验证」——
    两者对展示的后果相同（不显示），但理由串不同，便于事后归因（落 `offboard_rejections`）。

    判定顺序（先验不可得的条件，再分层；两层的涨幅带互斥且穷尽）：
      1. 5 日累计（剔除今日）必须落在 `[MOMENTUM_LAUNCH_ACCUM_MIN, OFFBOARD_ACCUM_MAX)`；
      2. MA 判据（**两层同一条**）：`_ma_not_bearish(hist)` = MA5 > MA10；
      3. 涨幅带 → T2（`[TODAY_MIN, OFFBOARD_T2_TODAY_MAX]`，另需主力净占比
         ≥ `OFFBOARD_MAIN_PCT_MIN` 且无顶背离）
         或 T1（`(HOT_MIN_PERCENT, TODAY_MIN)`，另需主力净占比 ≥ `OFFBOARD_MAIN_PCT_MIN`）。

    🔑 两层 MA 判据已统一（2026-09-22）—— 取消 09-21 的按层分化
    -----------------------------------------------------------------
    09-21 的分化是「T1 非空头 / T2 完全多头」。取消的理由是**分化让 T2 的定义自相矛盾**：

      T2 = 「启动首日」，其前一道门 `5日累计 ∈ [0, X)` 要求**近 5 天不能涨**；
      而「完全多头」要求 MA5 > MA10 > MA20，即**近 20 天一直涨**。两者交集只剩
      「缓涨趋势股」—— 基底突破（盘整后今天跳）与超跌反弹这两类**最典型的启动形态
      恰好同时被两边排除**。实测 2026-09-22 同快照：T2 带内有 K 线的 54 只候选，
      `ma_bullish` 杀掉 30 只（56%），当日 T2 产出 **0**；统一到非空头后
      T2 0 → 12，涨幅上限 3.15% → 6.93%、量比上限 3.19 → 5.40。

    09-21 留下「T2 保持完全多头」的原始理由是「与主线 momentum 池同域、该域已在榜上
    样本校准过，本模块不应单方面放宽」。但本模块自己的 docstring（「三条硬边界」之后
    那段）同时写着「⚠ 尚未回测：T2 的常量是在**榜上**样本校准的，域迁移到榜外不保证
    成立」—— **两句话互相打架**：既然域迁移不保证成立，「已在榜上校准」就不是保持
    严格的依据，而是恰恰说明它需要在榜外重验。取消分化即按后一句执行。

    为什么不反过来把 T1 收紧回「完全多头」：T1 的语义是「价还没动」，要求已走出
    20 日多头趋势**在逻辑上自相矛盾**（见 `_ma_not_bearish` docstring 里
    `analysis.py` 首次启动子模式的放宽背书），且收紧会把产出打回个位数。

    ⚠ 这是**放宽信号门**（扩大产出面），与风险门「只能收紧」的契约方向相反 ——
    且**没有样本外证据**：`rule_validate` 三个评估器都看不见本模块（`--set` 会被
    可见性硬校验拦在退出码 3），D = 3 交易日、按日 bootstrap MDE ±39.6pp，支撑不了
    任何阈值判断。属观察段的探索性调整，样本由 `offboard_rejections` 留痕表积累至
    D ≥ 47 交易日后再复盘。

    回退方式：T2 分支的 `_ma_not_bearish(hist)` 改回 `ma_bullish(hist)`
    （需重新 `from scanner.trend_beauty import ma_bullish`），T1 不动 —— 即还原
    09-21 的按层分化。`OFFBOARD_ACCUM_MAX` 与 `OFFBOARD_MAIN_PCT_MIN` 是独立开关，
    各自回退只影响产出量级，不影响分层语义。
    """
    if not klines:
        return None, "无K线数据(榜外池未覆盖)"
    accum = accum_5d(klines, today)
    if accum is None:
        return None, "K线不足6根有效收盘"
    if not (MOMENTUM_LAUNCH_ACCUM_MIN <= accum < OFFBOARD_ACCUM_MAX):
        return None, f"5日累计{accum:+.2f}%不在[{MOMENTUM_LAUNCH_ACCUM_MIN:g},{OFFBOARD_ACCUM_MAX:g})"

    c.accum_5d = round(accum, 2)

    # 🔴 MA 判据必须**剔除今日 bar**（2026-09-21 修）：kline 池的最后一根就是当日
    # 盘中未收盘 bar（实测 09-21 215 只 100% 末根 date == fetch_date），拿它算 MA5/MA10
    # 等于用「还在变的收盘价」判趋势。同一份快照里，含今日 vs 剔除今日会翻转
    # 完全多头 7.5%（16/212）、非空头 25.0%（53/212）的判定 —— 且翻转全是 True→False，
    # 即含今日 bar 会**系统性放大过门率**（今日上涨把 MA5 抬上去），并让判定随盘中时间漂移。
    # `utils.accum_5d` 早已按「剔除今日」定义（其 docstring 明写），此处对齐同一口径。
    hist = _exclude_today(klines, today)

    if MOMENTUM_LAUNCH_TODAY_MIN <= c.percent <= OFFBOARD_T2_TODAY_MAX:
        ma = _ma_not_bearish(hist)
        if ma is None:
            return None, "K线不足20根(MA不可判定)"
        if ma is not True:
            return None, "MA空头排列(MA5<=MA10)"
        # 2026-09-22 起 T2 与 T1 **同门**：旧注释的「T2 不设该条」建立在
        # 「T2 几乎不产出」的前提上，MA 放宽后前提失效（见 config_hot_watch）。
        if c.main_pct < OFFBOARD_MAIN_PCT_MIN:
            return None, f"主力净占比{c.main_pct:+.2f}%<{OFFBOARD_MAIN_PCT_MIN:g}"
        closes = [to_float(k.get("close"), 0.0) or 0.0 for k in klines]
        div, div_detail = mo_divergence(closes, klines)
        if div == V_MO_DIVERGENCE_BEAR:
            return None, f"顶背离({div_detail})"
        return T2, f"启动首日(累计{accum:+.2f}% 今日{c.percent:+.2f}% 量比{c.volume_ratio:.2f})"

    if HOT_MIN_PERCENT < c.percent < OFFBOARD_T1_TODAY_MAX:
        ma = _ma_not_bearish(hist)
        if ma is None:
            return None, "K线不足20根(MA不可判定)"
        if ma is not True:
            return None, "MA空头排列(MA5<=MA10)"
        if c.main_pct < OFFBOARD_MAIN_PCT_MIN:
            return None, f"主力净占比{c.main_pct:+.2f}%<{OFFBOARD_MAIN_PCT_MIN:g}"
        return T1, f"量先动·价未动(累计{accum:+.2f}% 今日{c.percent:+.2f}% 量比{c.volume_ratio:.2f})"

    return None, f"今日涨幅{c.percent:.2f}%不在两层带内"


def _exclude_today(klines: list | None, today: str) -> list:
    """剔除今日 bar（与 `utils.accum_5d` 同口径）。

    K 线池的末根就是当日盘中 bar（实测 09-21 215/215 的末根 date == fetch_date），
    它不是「已收盘的历史」：拿它算 MA 会用还在变的收盘价判趋势，且判定随盘中漂移。
    MA 判据（T1/T2 两侧）都须先过本函数；`accum_5d` 内部自行剔除，不必再包。

    ⚠ `mo_divergence`（顶背离）**未**改口径：它同时吃 closes 与 klines，
    改动需单独评估，不在本次 MA 修复范围内。
    """
    if not klines:
        return []
    return [k for k in klines if k.get("date") != today]


def _ma_not_bearish(klines: list | None) -> bool | None:
    """「MA 非空头」判定（**T1/T2 共用**，2026-09-22 起两层同一门）：True / False；**数据不足返回 None**。

    定义 = `features.ma_alignment_score(closes) > 0`，即 MA5 > MA10（含完全多头）。
    与 `analysis.py` 首次启动子模式的 `ma_boost >= 0` 同源 —— 那里注释写明放宽理由：
    「MA 多头排列**通常滞后于价格启动**，导致信号量过少」。

    🔴 为什么不复用 `trend_beauty.ma_bullish`：那个函数返回的是**完全多头**
    （MA5 > MA10 > MA20），其 `False` 无法区分「仅 MA5>MA10 的部分多头」与
    「MA5<=MA10 的真空头」—— 用它做非空头判定会把部分多头误杀。这正是本函数存在的原因。
    阈值/评分口径全部委托 `ma_alignment_score`，本函数只做「None 语义」的收口。

    ⚠ 调用方须传**已剔除今日 bar** 的序列（见 `_exclude_today`）。这里的 `_MA_MIN_BARS`
    门槛补齐 `ma_alignment_score` 的一个口径缺口：后者在 `len<20` 时仍会算 EMA5/EMA10
    并返回 ±3（不返回 data_short），只有 `<10` 根才报 data_short —— 若不拦，
    19 根也能过 MA 门，与 `ma_bullish` 的 20 根门槛不一致。
    """
    if not klines or len(klines) < _MA_MIN_BARS:
        return None
    closes = [to_float(k.get("close"), 0.0) or 0.0 for k in klines]
    if any(c <= 0 for c in closes[-_MA_MIN_BARS:]):
        return None
    score, _detail = ma_alignment_score(closes)
    if score == 0:  # data_short
        return None
    return score > 0


def annotate(c: OffboardCandidate, klines: list | None, today: str) -> str | None:
    """给候选打上 tier / 美感标记 / 理由串，返回 tier（None = 不产出）。

    **生产路径与离线自检共用**：自检说「通过」而生产实际不产出，是这类哨兵最坏的
    失效方式（守卫绿灯、线上空转）。故后处理只有这一份。
    """
    tier, why = classify_tier(c, klines, today)
    if tier is None:
        c.reasons = [why]
        return None
    c.tier = tier
    blocked, mark, detail = beauty_marks_daily(klines)
    c.beauty = mark if TREND_MARK_ENABLED else ""
    c.reasons = [why] + ([f"美感:{detail}"] if blocked else [])
    return tier


# ── 榜外 K 线池（独立于 daily_kline）────────────────────────────────────────


def _clean_bars(raw) -> list:
    """原始日线 → 通过 `make_kline_bar` 契约的 bar 列表（脏 bar 直接丢弃）。

    与 `db/queries.get_cached_klines` 同一契约：close<=0 / date 非法 / 缺字段的 bar
    不进池。K 线池服务的是「5 日累计 + MA + 顶背离」三个对**序列连续性**敏感的判定，
    放脏 bar 进去比缺数据更糟。
    """
    if not raw:
        return []
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        bar = make_kline_bar(item)
        if bar is not None:
            out.append(bar)
    return out


def _read_cached_klines(conn, symbols: list[str], fetch_date: str) -> dict[str, list]:
    """读当日已缓存的榜外日线 → {symbol: [bar, ...]}（缺失者不出现在结果里）。"""
    if conn is None or not symbols:
        return {}
    out: dict[str, list] = {}
    for i in range(0, len(symbols), 400):
        chunk = symbols[i : i + 400]
        placeholders = ",".join("?" * len(chunk))
        try:
            rows = conn.execute(
                f"SELECT symbol, payload_json FROM {_KLINE_TABLE} "  # noqa: S608 - 常量表名 + 占位符
                f"WHERE fetch_date = ? AND symbol IN ({placeholders})",
                (fetch_date, *chunk),
            ).fetchall()
        except EXTERNAL_FAILURES as e:
            logger.warning("榜外K线池读取失败（按无数据继续）: %s", e)
            continue
        for sym, payload in rows:
            try:
                bars = json.loads(payload)
            except (json.JSONDecodeError, TypeError):
                continue
            cleaned = _clean_bars(bars)
            if cleaned:
                out[sym] = cleaned
    return out


def _fetch_klines_batch(adapter, symbols: list[str]) -> dict[str, list]:
    """并发补取榜外日线。单票失败只丢该票（fail-soft），**不抛**。

    只捕获 `EXTERNAL_FAILURES`：编程错误照旧冒泡（本仓的失败纪律）。
    """
    out: dict[str, list] = {}
    if adapter is None or not symbols:
        return out
    pool = ThreadPoolExecutor(max_workers=max(1, min(OFFBOARD_KLINE_WORKERS, len(symbols))))
    try:
        futs = {pool.submit(adapter.fetch_kline, s, OFFBOARD_KLINE_DAYS): s for s in symbols}
        for fut in as_completed(futs):
            sym = futs[fut]
            try:
                bars = fut.result()
            except EXTERNAL_FAILURES as e:
                logger.warning("榜外K线补取失败 %s: %s", sym, e)
                continue
            cleaned = _clean_bars(bars)
            if cleaned:
                out[sym] = cleaned
    finally:
        # 显式 shutdown(wait=False)：不等在跑的 ≤8 个请求排空，避免相邻两轮重叠。
        pool.shutdown(wait=False, cancel_futures=True)
    return out


def _save_klines(conn, data: dict[str, list], fetch_date: str) -> None:
    if conn is None or not data:
        return
    now = now_beijing().isoformat(timespec="seconds")
    try:
        conn.executemany(
            f"INSERT OR REPLACE INTO {_KLINE_TABLE} "  # noqa: S608 - 常量表名
            f"(symbol, fetch_date, payload_json, updated) VALUES (?, ?, ?, ?)",
            [(s, fetch_date, json.dumps(bars, ensure_ascii=False), now) for s, bars in data.items()],
        )
        conn.commit()
    except EXTERNAL_FAILURES as e:
        logger.warning("榜外K线池写入失败（本轮结果不受影响）: %s", e)


def load_offboard_klines(conn, adapter, symbols: list[str], fetch_date: str | None = None) -> dict[str, list]:
    """榜外 K 线池：当日缓存优先，缺失者并发补取后落库 → {symbol: [bar, ...]}。

    **为何不复用 `daily_kline`**：那张表的既定语义是「榜单衍生池」（999 只里 982 只在
    `appearances`），塞入榜外票会污染所有基于它的回测基准与归因（portfolio_backtest /
    prevday_perf / 召回率口径）。B 段需要的是「榜外票的日线」，只能另起一张表。

    `fetch_date` 缺省今日：5 日累计（剔除今日）与 MA 只依赖抓取日之前的 bar，
    故盘中抓一次当日复用；跨日自动 miss 重取。
    """
    if not symbols:
        return {}
    day = fetch_date or now_beijing().date().isoformat()
    uniq = list(dict.fromkeys(symbols))
    result = _read_cached_klines(conn, uniq, day)
    missing = [s for s in uniq if s not in result]
    if missing and adapter is not None:
        fetched = _fetch_klines_batch(adapter, missing)
        if fetched:
            _save_klines(conn, fetched, day)
            result.update(fetched)
    return result


# ── 落库与回填（可验证性的硬前置）────────────────────────────────────────────


def persist_round(conn, rows: Sequence[OffboardCandidate]) -> None:
    """B 段逐日落库（(date, symbol) 唯一）。失败只告警，不影响本轮结果返回。

    🔴 **信号值首次写入即冻结**（2026-09-21 修）：`name / tier / percent / accum_5d /
    vol_ratio / main_pct / amount / float_cap / price` 九列在 ON CONFLICT 时**不更新**，
    只有 `last_hit_time` 与 `updated` 每轮刷新。

    旧语义是「当日最后一次写入即收盘值」—— 那对 `market_extra_cache`（收盘价缓存）
    是对的，对**这张表**是错的：一只票上午以 T1 身份被产出、下午涨过 3.5% 变成 T2，
    旧的 `tier` / `percent` 就被改写，事后**无法还原它当初以什么身份被推出**。
    实测 09-21 有 3 只 T1 信号只存活 4 分钟，但库里既看不出它出没过、也看不出只出没
    过 4 分钟 —— 一张「信号观测表」丢失了信号本身，就失去了唯一的存在理由。

    `first_time` 仍只在首次写入时记（「当日何时首次产出」），`last_hit_time` 记录
    「当日最后一次命中」。**为什么不让 `updated` 兼作命中时刻**：`backfill_next_day`
    也写 `updated`，09-18 那 5 行的 `updated` 全被回填写成 09-21 的时刻，与当日行的
    `first_time` 撞在一起 → 命中时间维度事实上丢失。拆出专列后语义唯一。

    这张表是**本类信号上线前的唯一硬前置**：`hot_watch_hits` 以 symbol 为主键、
    只存滚动最新态，回答不了「T1/T2 的次日 hit 率是多少」。
    """
    if conn is None or not rows:
        return
    day = now_beijing().date().isoformat()
    now = now_beijing().isoformat(timespec="seconds")
    try:
        conn.executemany(
            f"""INSERT INTO {_LOG_TABLE}
                (date, symbol, name, tier, percent, accum_5d, vol_ratio, main_pct,
                 amount, float_cap, price, first_time, last_hit_time, updated)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(date, symbol) DO UPDATE SET
                  last_hit_time=excluded.last_hit_time,
                  updated=excluded.updated""",  # noqa: S608 - 常量表名
            [
                (
                    day,
                    c.symbol,
                    c.name,
                    c.tier,
                    round(c.percent, 3),
                    round(c.accum_5d, 3),
                    round(c.volume_ratio, 3),
                    round(c.main_pct, 3),
                    c.amount,
                    c.float_market_capital,
                    c.current,
                    now,
                    now,
                    now,
                )
                for c in rows
            ],
        )
        conn.commit()
    except EXTERNAL_FAILURES as e:
        logger.warning("B段逐日落库失败（本轮结果不受影响）: %s", e)


def backfill_next_day(conn, adapter, signal_date: str | None = None) -> int:
    """回填 `offboard_launch_log.next_day_pct`（次日收益 %），返回回填行数。

    口径：信号日收盘价 → **次一交易日**收盘价。榜外票不在 `daily_kline`，数据源是本区
    自己的榜外 K 线池（会按需补取，结果留在池里，下一轮直接命中）。

    ⚠ 两个必须的防呆（都源自 `daily_kline` 已踩过的坑）：
      1. **不能假设「下一根 bar = 下一交易日」** —— 日线源在停牌/跳空时会缺 bar，
         故要求下一根 bar 与信号日相差 ≤4 个自然日（覆盖周末），否则跳过；
      2. **信号日必须早于今天**（`date < today`）—— 否则每轮都会为当天的信号去找
         「次日 bar」，既永远找不到、又会把当天的 K 线池反复翻一遍。
    """
    if conn is None:
        return 0
    today = now_beijing().date().isoformat()
    sql = f"SELECT date, symbol FROM {_LOG_TABLE} WHERE next_day_pct IS NULL AND date < ?"  # noqa: S608 - 常量表名
    params: tuple = (today,)
    if signal_date:
        sql += " AND date = ?"
        params = (today, signal_date)
    try:
        rows = conn.execute(sql, params).fetchall()
    except EXTERNAL_FAILURES as e:
        logger.warning("B段回填查询失败: %s", e)
        return 0
    if not rows:
        return 0

    by_symbol: dict[str, list[str]] = {}
    for d, sym in rows:
        by_symbol.setdefault(sym, []).append(d)

    filled = 0
    now = now_beijing().isoformat(timespec="seconds")
    for sym, dates in by_symbol.items():
        bars = load_offboard_klines(conn, adapter, [sym]).get(sym) or []
        by_date = {k.get("date"): k for k in bars}
        for d in dates:
            later = sorted(x for x in by_date if isinstance(x, str) and x > d)
            if not later:
                continue
            nxt = later[0]
            if (_date.fromisoformat(nxt) - _date.fromisoformat(d)).days > 4:
                continue
            p0 = to_float((by_date[d] or {}).get("close"), 0.0) or 0.0
            p1 = to_float((by_date[nxt] or {}).get("close"), 0.0) or 0.0
            if p0 <= 0 or p1 <= 0:
                continue
            try:
                conn.execute(
                    f"UPDATE {_LOG_TABLE} SET next_day_pct=?, updated=? "  # noqa: S608 - 常量表名
                    f"WHERE date=? AND symbol=?",
                    (round((p1 - p0) / p0 * 100.0, 4), now, d, sym),
                )
            except EXTERNAL_FAILURES as e:
                logger.warning("B段回填写入失败 %s %s: %s", d, sym, e)
                continue
            filled += 1
    if filled:
        try:
            conn.commit()
        except EXTERNAL_FAILURES as e:
            logger.warning("B段回填提交失败: %s", e)
    return filled


# ── 单轮主流程 ──────────────────────────────────────────────────────────────


def opening_silence_active(now: datetime | None = None) -> bool:
    """开盘静默窗口是否生效（True = 本轮不产出）。

    窗口依据见 `config_hot_watch.OFFBOARD_OPENING_SILENCE_MIN` 的注释：量比 = 当日累计量
    ÷（近 5 日均每分钟量 × 已交易分钟数），开盘头几分钟的分母只有几分钟，商**虚高一个
    量级**；而量比是本段 `sort_key` 的第一排序键。实测 09:33 捕获的 3 只 T1 虚高
    11.5/8.6/6.6 倍且当日全数转跌，09:45 之后收敛到 ≈1.0。

    用 `trading_minutes_elapsed` 而**不是**墙上时间差：它已把午休 120 分钟排除在外
    （11:30 与 13:00 都返回 120），故「开盘满 N 分钟」在任何时刻都只指「上午连续竞价
    满 N 分钟」，下午开盘不会被误判为仍在窗口内。非交易日与开盘前返回 0 → 同样静默
    （那两种时段本就不该产出）。

    `OFFBOARD_OPENING_SILENCE_MIN <= 0` = 关闭窗口（离线自检 / 回放路径）。
    """
    if OFFBOARD_OPENING_SILENCE_MIN <= 0:
        return False
    return trading_minutes_elapsed(now) < OFFBOARD_OPENING_SILENCE_MIN


def run_offboard_watch(
    adapter,
    conn,
    board_items: Sequence[dict],
    *,
    exclude_symbols: set[str] | None = None,
    top_n: int = OFFBOARD_DISPLAY_TOP,
    klines: dict[str, list] | None = None,
    snapshot: dict[str, dict] | None = None,
) -> list[OffboardCandidate]:
    """跑一轮 B 段筛选，返回待展示的前 `top_n` 行。

    依赖注入 `board_items`：复用主循环**已抓取**的飙升榜，只为确定「谁在榜内」
    （B 段的候选来自快照，不来自榜单）—— 本段因此**不因榜单熔断而空**。

    `klines` 显式传入时跳过 K 线池（离线自检路径，不联网）；
    `snapshot` 显式传入时跳过 DB 读取（CLI 用生产快照 + 内存库的组合，见 `main`）。

    异常处理见模块 docstring 的 fail-open / fail-closed 一节；编程错误不吞。
    """
    if conn is None:
        return []

    # 第一道门：开盘静默窗口。**放在读任何数据之前** —— 否则要把 5305 行全市场快照
    # 读进来、跑完 5000 次门判定，再整批丢弃。窗口语义与实测见 opening_silence_active。
    if opening_silence_active():
        return []

    if snapshot is None:
        snapshot = get_market_extra_snapshot(conn, "fund_flow")
    if not snapshot:
        return []

    # 名称：快照自 2026-09-18 起带 f14；此前落库的行没有，回落到 appearances
    # （榜外 ≠ 从未上过榜）。名称不只用于展示 —— 通用门的 ST 判定依赖它。
    gem_offboard = [s for s, p in snapshot.items() if p and code_of(s).startswith(("300", "301"))]
    names = get_symbol_names(conn, gem_offboard)

    cands, gate_rejects = build_candidates(snapshot, board_items, names, exclude_symbols)
    today = now_beijing().date().isoformat()

    # 拒绝留痕（2026-09-22 / 迁移 m017）：gate 段的 rejects 原先被 `_rejects` 丢弃、
    # 分层段的理由只写进 `c.reasons` 就地消失 ⇒ 只剩幸存者样本，任何阈值调整无从证伪。
    # 两段互斥（过门才可能被分层拒），同一 symbol 不会重复入表。
    if not cands:
        _save_rejections(conn, _rejection_rows(gate_rejects, (), snapshot, names), today)
        return []

    if klines is None:
        klines = load_offboard_klines(conn, adapter, [c.symbol for c in cands[:OFFBOARD_KLINE_FETCH_LIMIT]])

    passed: list[OffboardCandidate] = []
    tier_rejects: list[tuple[str, str]] = []
    for c in cands:
        if annotate(c, klines.get(c.symbol), today) is None:
            tier_rejects.append((c.symbol, c.reasons[0] if c.reasons else "不产出"))
            continue
        passed.append(c)

    _save_rejections(conn, _rejection_rows(gate_rejects, tier_rejects, snapshot, names), today)

    passed.sort(key=sort_key)
    persist_round(conn, passed)
    # 顺手回填历史日的次日收益（无待回填行时只是一条索引查询）。不放在这里的话，
    # 影子期的样本永远没有标签，验收（§5.3）就无从谈起。
    try:
        backfill_next_day(conn, adapter)
    except EXTERNAL_FAILURES as e:
        logger.warning("B段次日收益回填失败（不影响本轮）: %s", e)

    top = passed[:top_n]
    # 板块列（2026-09-22）：只对最终展示行取值 —— ②级 F10 补拉的量因此被压到
    # ≤ OFFBOARD_DISPLAY_TOP 只，且缓存命中时零请求（见 OFFBOARD_BOARD_FETCH 注释）。
    _attach_boards(top, conn)
    return top


def _rejection_rows(
    gate_rejects: Sequence[tuple[str, str]],
    tier_rejects: Sequence[tuple[str, str]],
    snapshot: dict[str, dict],
    names: dict[str, str],
) -> list[tuple]:
    """两段拒绝明细 → `offboard_rejections` 行 `(symbol, name, reason, %, 量比, 主占)`。

    gate 段的候选对象在 `build_candidates` 内部就地丢弃（对外契约只有 (symbol, reason)），
    故 percent / vol_ratio / main_pct 回**快照**取 —— 这三列正是「事后重放阈值」的输入
    （2026-09-22 那次杠杆测量就得手工重建这一步，跑完即失）。快照缺行时留 None，
    不阻断留痕：留痕存在的意义就是**下次不必再手工重建**。

    `name` 优先用 `names`（`get_symbol_names` 的回退链），快照自 2026-09-18 起也带 f14。
    """
    rows: list[tuple] = []
    for sym, reason in (*gate_rejects, *tier_rejects):
        p = snapshot.get(sym) or {}
        rows.append(
            (
                sym,
                names.get(sym) or p.get("name") or "",
                reason,
                to_float(p.get("percent"), None),
                to_float(p.get("vol_ratio"), None),
                to_float(p.get("main_pct"), None),
            )
        )
    return rows


def _save_rejections(conn, rows: list[tuple], today: str) -> None:
    """留痕写入的 fail-open 护栏：**任何失败都不能中断本轮扫描**。

    DB 层（`dal.save_offboard_rejections`）已收窄到 `sqlite3.Error`；这里再套一层
    `EXTERNAL_FAILURES`，是因为留痕是**观测**、不是产出 —— 它出问题时宁可少记一天，
    也不能让 B 段整段空掉（这正是 `offboard_launch_log` 当初的 fail-open 约定）。
    """
    if not rows:
        return
    try:
        save_offboard_rejections(conn, rows, today)
    except EXTERNAL_FAILURES as e:  # pragma: no cover - 护栏路径
        logger.warning("B段拒绝留痕失败（不影响本轮产出）: %s", e)


def _attach_boards(rows: list[OffboardCandidate], conn) -> None:
    """B 段「板块」列填充（2026-09-22）：只对最终展示行取值。

    榜外票按定义不进主线候选 ⇒ concept_cache 恒 miss ⇒ 必须允许 F10 补拉（默认开，
    见 `OFFBOARD_BOARD_FETCH`）。开关关掉时只读缓存、miss 回退名称关键词 —— 单测与
    离线路径靠它阻断外网。任何失败只让本列留空（渲染 `—`），不影响本段产出。
    """
    try:
        attach_display_boards(conn, rows, fetch=bool(OFFBOARD_BOARD_FETCH))
    except EXTERNAL_FAILURES as e:
        logger.warning("B段板块列填充失败（本列留空）: %s", e)


# ── 独立运行 CLI（python -m scanner.offboard_watch）────────────────────────
# 主循环内本段随扫描自动跑；此入口用于**离线自检 / 次日收益回填 / 临时查看**：
#   --offline-demo 不联网，用内置样本逐条核对门槛与分层
#   --backfill     只回填历史日的 next_day_pct（供影子期量化）
#   联网查看读**生产快照**但落**内存库**（不污染 scanner.db）—— 快照本身只存在于
#   生产库，这是唯一能既看到真实候选、又不给生产库写观测数据的组合。


def _lin(start: float, end: float, n: int) -> list[float]:
    """等差序列（自检样本的日线收盘序列用，保证样本可复现）。"""
    step = (end - start) / (n - 1) if n > 1 else 0.0
    return [round(start + step * i, 4) for i in range(n)]


# ⚠ 样本长度约定（2026-09-21 修）：`_demo_klines` 会在尾部**追加今日 bar**，
# 而所有「历史态」判定（MA / 5 日累计）都**剔除今日**。故这里给的 hist 必须是
# 「真正的历史根数」——要让 T2 的 `_ma_not_bearish` 可判定（需 ≥20 根历史），hist 至少 20 根。
# 此前样本只有 19 根（+今日=20），剔除今日后恒为 19 → T2 永远「MA不可判定」，
# 而当时的 `ma_bullish(klines)` 却因**含今日**而恰好过门 —— 自检一直在验证错误的口径。
_DEMO_HIST_UP = _lin(9.0, 10.2, 20)  # 完全多头(EMA +6)、5 日累计 ≈ +3.2%
_DEMO_HIST_ACCUM_HIGH = _lin(9.0, 9.0, 14) + _lin(9.0, 11.34, 7)  # 5 日累计 ≈ +26%（超上限 OFFBOARD_ACCUM_MAX=15）
_DEMO_HIST_MA_BEAR = _lin(12.0, 10.0, 15) + _lin(10.2, 10.6, 5)  # 5 日累计 +6.0% 但 MA 空头（两层同门下都拦）
_DEMO_HIST_PARTIAL_BULL = _lin(10.6, 9.8, 12) + _lin(9.8, 10.25, 8)  # EMA 部分多头(+3)：两层同门下都放行
_DEMO_HIST_SHORT = _lin(10.0, 10.2, 11)  # 剔除今日后 <20 根 → MA 不可判定


def _demo_klines(hist_closes: list[float], today_close: float, today: str) -> list[dict]:
    """(历史 closes + 今日 bar) → 日线序列；最后一根 date == today。

    ⚠ 追加的今日 bar 与生产一致（K 线池末根即当日盘中 bar），**不会被本函数剔除**；
    是否剔除由消费方决定（`accum_5d` / `_exclude_today` 各自处理）。
    """
    series = [*hist_closes, today_close]
    base = _date.fromisoformat(today)
    bars = []
    for i, close in enumerate(series):
        prev = series[i - 1] if i else close
        bars.append(
            {
                "date": (base - _td(days=len(series) - 1 - i)).isoformat(),
                "open": round(prev, 4),
                "close": round(close, 4),
                "high": round(max(close, prev) * 1.004, 4),
                "low": round(min(close, prev) * 0.996, 4),
                "volume": 2.0e6,
                "percent": round((close - prev) / prev * 100.0, 4) if prev else 0.0,
            }
        )
    return bars


_DEMO_DEFAULTS = {
    "name": "自检样本",
    "amount": 8.0e7,  # 8000 万（> OFFBOARD_MIN_AMOUNT）
    "float_cap": 3.0e9,  # 30 亿（> OFFBOARD_MIN_FLOAT_CAP）
    "total_cap": 3.6e9,
    "vol_ratio": 2.0,
    "main_pct": 2.0,
    "turnover": 3.0,
    "volume": 2.0e6,
}

# (期望, 代码, 是否在榜, 日线序列, 今日涨幅%, payload 覆盖项)
# 期望取值：'T1' / 'T2' = 应产出该层；'跳过' = 结构上不进候选面；其余 = 排除原因子串。
_DEMO_CASES: list[tuple[str, str, bool, list[float] | None, float, dict]] = [
    ("T1", "300101", False, _DEMO_HIST_UP, 2.5, {}),
    ("T2", "300102", False, _DEMO_HIST_UP, 5.0, {"vol_ratio": 2.4, "main_pct": 0.5}),
    # —— 结构上不进候选面 ——
    ("跳过", "300103", True, _DEMO_HIST_UP, 2.5, {}),  # 在榜 → A 段的事
    ("跳过", "000001", False, _DEMO_HIST_UP, 2.5, {"name": "平安银行"}),  # 主板非创业板
    ("跳过", "301104", False, _DEMO_HIST_UP, 2.5, {}),  # 今日已推荐 → exclude_symbols
    # —— 门槛排除（build_candidates 阶段）——
    ("ST", "300105", False, _DEMO_HIST_UP, 2.5, {"name": "*ST自检"}),
    ("主力净流出", "300106", False, _DEMO_HIST_UP, 2.5, {"main_pct": -9.0}),
    ("涨幅过高", "300107", False, _DEMO_HIST_UP, 9.0, {}),
    ("量比不足", "300108", False, _DEMO_HIST_UP, 2.5, {"vol_ratio": 1.2}),
    ("成交额不足", "300109", False, _DEMO_HIST_UP, 2.5, {"amount": 2.0e7}),
    ("流通市值过小", "300110", False, _DEMO_HIST_UP, 2.5, {"float_cap": 8.0e8}),
    ("市值过大", "300111", False, _DEMO_HIST_UP, 2.5, {"total_cap": 4.0e10}),
    ("价格过高", "300112", False, _DEMO_HIST_UP, 2.5, {"price_override": 260.0}),
    ("当前非上涨状态", "300113", False, _DEMO_HIST_UP, -2.0, {}),
    # —— 分层阶段排除 ——
    ("5日累计", "300114", False, _DEMO_HIST_ACCUM_HIGH, 2.5, {}),
    ("MA空头排列", "300115", False, _DEMO_HIST_MA_BEAR, 2.5, {}),
    ("K线不足", "300116", False, _DEMO_HIST_SHORT, 2.5, {}),
    ("无K线数据", "300117", False, None, 2.5, {}),
    ("主力净占比", "300118", False, _DEMO_HIST_UP, 2.5, {"main_pct": -3.0}),  # T1 需 ≥0
    # —— 两层 MA **同门**（2026-09-22 起取消按层分化）—— 三条样本互为交叉验证：
    #    ① 同一条「EMA 部分多头」序列在 T1/T2 两个涨幅带**都应放行** —— 这条同时证明
    #       T2 用的是「非空头」而非「完全多头」（若被改回完全多头，T2 那条会红）；
    #    ② 同一条「MA 空头」序列在两个带**都应被拦** —— 证明 T2 确实还有 MA 门，
    #       不是被整个删掉了（若两层门都没了，②的两条会同时红）。
    ("T1", "300119", False, _DEMO_HIST_PARTIAL_BULL, 2.5, {}),
    ("T2", "300120", False, _DEMO_HIST_PARTIAL_BULL, 5.0, {"vol_ratio": 2.4, "main_pct": 0.5}),
    ("MA空头排列", "300121", False, _DEMO_HIST_MA_BEAR, 5.0, {"vol_ratio": 2.4, "main_pct": 0.5}),
    # —— T2 的主力净占比门（2026-09-22 新增，与 T1 同门；旧口径 T2 不设此条）——
    ("主力净占比", "300122", False, _DEMO_HIST_UP, 5.0, {"vol_ratio": 2.4, "main_pct": -3.0}),
]


def run_offline_demo(top_n: int, emit_json: bool) -> int:
    """离线自检：不联网，内置样本逐条核对门槛与分层。

    与 `hot_watch.run_offline_demo` 同一形态 —— 改动门槛/分层条件后应跑它，
    它是 B 段筛选规则的回归哨兵（对应单测 `test_offline_demo_all_cases_match`）。
    """
    today = now_beijing().date().isoformat()
    snapshot: dict[str, dict] = {}
    board_items: list[dict] = []
    klines: dict[str, list] = {}
    exclude: set[str] = {"SZ301104"}

    for _expect, code, on_board, series, today_pct, over in _DEMO_CASES:
        sym = ak_to_xq(code)
        payload = dict(_DEMO_DEFAULTS)
        payload.update(over)
        price_override = payload.pop("price_override", None)
        today_close = 10.0
        if series is not None:
            today_close = round(series[-1] * (1 + today_pct / 100.0), 4)
            klines[sym] = _demo_klines(series, today_close, today)
        elif price_override is None:
            today_close = 10.0
        price = to_float(price_override, today_close) if price_override is not None else today_close
        payload["price"] = price
        payload["percent"] = today_pct
        payload["prev_close"] = round(price / (1 + today_pct / 100.0), 4)
        snapshot[sym] = payload
        if on_board:
            board_items.append({"symbol": sym})

    cands, gate_rejects = build_candidates(snapshot, board_items, {}, exclude)
    gate_map = dict(gate_rejects)
    tier_map: dict[str, str] = {}
    tier_reason: dict[str, str] = {}
    for c in cands:
        tier = annotate(c, klines.get(c.symbol), today)
        if tier is None:
            tier_reason[c.symbol] = c.reasons[0] if c.reasons else "不产出"
        else:
            tier_map[c.symbol] = tier

    print("【B 段离线自检】不联网，内置样本逐条核对（榜外创业板 · T1 量先动 / T2 启动首日）")
    print(f"{'代码':<8}{'名称':<16}{'期望':<16}{'实际':<24}结果")
    print("-" * 72)
    ok = True
    for expect, code, _on_board, _series, _pct, _over in _DEMO_CASES:
        sym = ak_to_xq(code)
        name = snapshot[sym]["name"]
        # 三个映射覆盖了所有可能去向：过门未产出（tier_reason）/ 被门剔除（gate_map）/
        # 产出（tier_map）。三者都不在 = 结构上没进候选面（榜内 / 主板 / 已推荐）。
        in_candidate_face = sym in gate_map or sym in tier_reason or sym in tier_map
        if expect in (T1, T2):
            actual = tier_map.get(sym) or gate_map.get(sym) or tier_reason.get(sym) or "缺失"
            good = tier_map.get(sym) == expect
        elif expect == "跳过":
            actual = "进入了候选面" if in_candidate_face else "未进入候选面"
            good = not in_candidate_face
        else:
            actual = gate_map.get(sym) or tier_reason.get(sym) or ("产出" if sym in tier_map else "缺失")
            good = sym not in tier_map and expect in actual
        ok = ok and good
        print(f"{code:<8}{name:<16}{expect:<16}{actual:<24}{'OK' if good else 'FAIL'}")

    print("-" * 72)
    print(
        f"过门 {len(cands)} 只 / T1 {sum(1 for v in tier_map.values() if v == T1)} · "
        f"T2 {sum(1 for v in tier_map.values() if v == T2)} / 样本 {len(_DEMO_CASES)} 条"
    )

    ranked = sorted((c for c in cands if c.symbol in tier_map), key=sort_key)
    if emit_json:
        print(json.dumps([_row_json(c) for c in ranked[:top_n]], ensure_ascii=False, indent=2))
    elif ranked:
        from scanner.view.render import render_offboard_standalone

        render_offboard_standalone(ranked[:top_n])
    return 0 if ok else 1


def _row_json(c: OffboardCandidate) -> dict:
    return {
        "code": c.code,
        "symbol": c.symbol,
        "name": c.name,
        "tier": c.tier,
        "percent": round(c.percent, 2),
        "accum_5d": round(c.accum_5d, 2),
        "vol_ratio": round(c.volume_ratio, 2),
        "main_pct": round(c.main_pct, 2),
        "amount": round(c.amount),
        "float_cap_yi": round(c.float_market_capital / 1e8, 2),
        "price": round(c.current, 3),
        "beauty": c.beauty,
        "reasons": c.reasons,
    }


def _cli_conn():
    """CLI 专用内存库：DDL 与生产迁移同一份（不复制 SQL），但不碰 scanner.db。"""
    import sqlite3

    from scanner.db.migrations import _OFFBOARD_KLINE_DDL, _OFFBOARD_LAUNCH_LOG_DDL

    conn = sqlite3.connect(":memory:")
    conn.execute(_OFFBOARD_KLINE_DDL)
    conn.execute(_OFFBOARD_LAUNCH_LOG_DDL)
    conn.commit()
    return conn


def main(argv: "list[str] | None" = None) -> int:
    import argparse

    p = argparse.ArgumentParser(
        prog="python -m scanner.offboard_watch",
        description="沪深飙升 B 段（榜外异动）：榜外创业板 · 量先动 / 启动首日（主循环内已自动执行）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--top", type=int, default=OFFBOARD_DISPLAY_TOP, help="展示前 N 只")
    p.add_argument("--json", action="store_true", help="额外输出 JSON")
    p.add_argument("--offline-demo", action="store_true", help="离线自检：内置样本，不联网")
    p.add_argument("--backfill", action="store_true", help="只回填历史日 next_day_pct 后退出（写生产库，有意为之）")
    p.add_argument("--date", help="配合 --backfill：只回填该信号日（YYYY-MM-DD）")
    p.add_argument("--verbose", action="store_true", help="调试日志")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="[%(levelname)s] %(message)s",
    )

    if args.offline_demo:
        return run_offline_demo(args.top, args.json)

    from scanner.data_source import get_adapter
    from scanner.db.queries import get_market_extra_snapshot as _snap
    from scanner.db.schema import get_conn

    adapter = get_adapter()

    if args.backfill:
        conn = get_conn()
        try:
            filled = backfill_next_day(conn, adapter, args.date)
        finally:
            conn.close()
        print(f"  B 段次日收益回填 {filled} 行")
        return 0

    # 手动运行时的可解释性：静默窗口生效 → 直接说明为何本轮无结果。生产主循环里
    # 这段逻辑由 `run_offboard_watch` 内部的第一道门承担，此处只是给人看的提示。
    if opening_silence_active():
        print(
            f"  开盘静默窗口生效中：已交易 {trading_minutes_elapsed()} 分钟 < "
            f"OFFBOARD_OPENING_SILENCE_MIN={OFFBOARD_OPENING_SILENCE_MIN} 分钟"
            " —— 量比在此窗口内虚高一个量级，本轮不产出"
        )
        return 0

    board = adapter.fetch_biaosheng(100)
    if not board:
        print("  [!] 飙升榜为空（熔断中/网络异常）——本段候选不依赖榜单，继续尝试快照")
    prod = get_conn()
    try:
        snapshot = _snap(prod, "fund_flow")
    finally:
        prod.close()
    if not snapshot:
        print("  [!] 全市场快照为空（主循环未落库？），无法运行 B 段")
        return 1
    print(f"  飙升榜 {len(board)} 条 / 全市场快照 {len(snapshot)} 只 | 数据源 {adapter.name}")

    rows = run_offboard_watch(adapter, _cli_conn(), board, top_n=args.top, snapshot=snapshot)
    if not rows:
        print("  本轮无标的通过（榜外无异动 / 全被排除）")
        return 0
    if args.json:
        print(json.dumps([_row_json(c) for c in rows], ensure_ascii=False, indent=2))
    else:
        from scanner.view.render import render_offboard_standalone

        render_offboard_standalone(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
