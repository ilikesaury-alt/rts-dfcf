"""沪深飙升区 **B 段「榜外异动」**（2026-09-18）：榜外创业板 · 量先动 / 启动首日 两层观察段。

为什么要有这一段
----------------
「沪深飙升」区（`hot_watch`）的候选**只来自雪球飙升榜** —— 榜内前 `HOT_ENRICH_LIMIT`
个名额。榜单天然滞后（好票等上榜时已涨一截），而榜外票结构上永远进不来。
B 段把**项目已在手但从未被消费**的资金流快照（`market_extra_cache` 的 fund_flow 行，
创业板全集 ~2100 只 + 榜内主板票，除资金流外还带价/涨幅/量比/换手/成交额/流通市值）
拿来做**榜外创业板**的提前观察：T1「量先动·价未动」与 T2「启动首日」。
（2026-10-15 取数域收敛：原 clist 全市场 5305 只被 WAF 封锁后改 ulist 显式取数，
非创业板非榜单的 ~3000 只主板票从无消费方，不再拉取。）

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
1. `market_extra_cache`（date=today, data_type='fund_flow'）：全集快照，零额外请求；
2. **榜外 K 线池** `offboard_kline_cache`：🔴 **独立于 `daily_kline`** —— 后者的既定
   语义是「榜单衍生池」（999 只里 982 只在 appearances），塞入榜外票会污染所有基于
   它的回测基准与归因（portfolio_backtest / prevday_perf / 召回率口径）。
   按 (symbol, fetch_date) 缓存，当日抓一次当日复用（5 日累计/MA 只依赖抓取日之前的 bar）；
3. 通用 8 门 + 两道**收紧**的榜外专属门（显式传参，见 `offboard_gate`）；
4. **实时资金流补拉**（2026-10-08 修复 M1，2026-10-09 扩展量比；**仅生产主循环
   路径**）：东财全集 `fetch_fund_flow_rank`（与主循环同源同缓存，响应行自带
   f10 量比）覆盖快照 main_pct / vol_ratio。snapshot 注入路径（测试/离线自检/CLI）
   不触发 —— 保持「测试与自检零联网」的纪律。

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
   ⚠ **2026-11-05 重做**：15 分钟**并没有把失真等掉**（09:45 的读数仍虚高 3.5 倍，
   量比门在 09:54 的通过率 94.6% = 等于没筛）⇒ 改成「**测量修正代替时间等待**」：
   逐轮实测全市场量能前置系数 k(m) 并把上报量比除掉（`scanner/volume_bias.py`），
   窗口随之缩到 5 分钟（详见下一节）。
2. **落库冻结**（`persist_round` + 迁移 `m016`）：信号值改为「首次写入即冻结」，
   另立 `last_hit_time` 列记最后命中。旧语义「最后写入即收盘值」会把票的**信号身份**
   （T1/T2、捕获时的涨幅）覆盖掉，事后无法还原它以什么身份被推出。

⚠ 本段**筛选常量一个没动**：样本只有 2 个交易日 / 19 行 —— 够证伪「量比在开盘几分钟
可用」（那是数学性质，不是统计推断），远不够支撑任何阈值调整（项目铁律 observe-first）。

2026-11-05 量比去偏（把「等 15 分钟」换成「把读数算准」）
----------------------------------------------------------
用户决策：「榜外异动开盘后 15 分钟才显示，是否有点太长了」。核查结论：**是，而且
那 15 分钟本来就没解决问题**——开盘的量比失真是**全市场共同的时间形状因子**
（集合竞价 + 早盘集中放量），可测可扣：

    k(m) = 全市场中位[今日累计量 ÷ 近 5 日收盘量均值] ÷ (m/240)
    实测（2026-11-05）：m=1 → 13.6x，m=3 → 7.3x，m=5 → 5.7x，m=15 → 3.5x，m=30 → 2.7x

故本段的量比一律改用 `上报量比 ÷ k(m)`（**扣掉全市场共同的量能前置形状**后的值，
与收盘口径同一量级），`k(m)` 由本地库逐轮实测（`scanner/volume_bias.py`，**零新增
网络**）。三条纪律：
1. **阈值一个字没改**：`MOMENTUM_LAUNCH_VOL`(1.5) 的语义（「相对自身近 5 日均速的
   放量倍数」）去偏前后完全一致 ⇒ 不存在无标签调参；
2. **k(m) 不可得 ⇒ 读数不可信 ⇒ 退回旧的 15 分钟窗口**（`OFFBOARD_OPENING_
   SILENCE_MIN_UNADJUSTED`），不在读数不可信时假装可信；
3. **榜内异动段同改**（`onboard_anomaly`）：它与本段共用 `offboard_gate` 的**同一个**
   量比门、同**一份**快照 ⇒ 同一个阈值必须配同一个尺度，否则「同门同层」是假的。
   ⚠ 榜内**飙升**段（A 段）**未改**（用户决策）—— 它的量比进入复合评分，改动面更大。

⚠ 副作用（已量化，早盘会「更空」）：量比门在早盘从「几乎不过滤」变成真过滤 ——
09:59 实测同一快照 330 只候选，原始口径过量比门 327 只（99.1%），去偏后 72 只
（21.8%）⇒ 早盘产出**变少但变真**（不是「更早更多」）。这是「修测量」的必然结果，
不是调松了门。

2026-10-09 留存（dwell）
------------------------
价 / 主占 / 量比改吃实时值后（数据通路第 4 条），在活跃度阈值（量比 1.5 / 成交额
下限）附近横跳的票会**反复进出**本段（上线当日用户实测）。修法是短窗留存：近
`OFFBOARD_DWELL_MIN` 分钟内严格过门的票，本轮只挂活跃度阈值时以「留存」行续展
（`offboard_gate(retain=True)`，追加在严格行尾部、不占 `top_n` 名额）。三条纪律：
风险门与 `classify_tier` 全部照严；留存行**不写** launch_log ⇒ 窗口自最后一次严格
过门起算、不会自我续命；留存行带理由串标记，且因量比已回落，几乎必然过不了飞书
推送门的量比兜底（≥2）—— 终端有连续性，推送面仍精选。
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date as _date
from datetime import datetime
from datetime import timedelta as _td
from typing import Any, Collection, Sequence

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
    OFFBOARD_DWELL_MAX,
    OFFBOARD_DWELL_MIN,
    OFFBOARD_FLOW_MIN_SUM,
    OFFBOARD_FLOW_WINDOW,
    OFFBOARD_KLINE_DAYS,
    OFFBOARD_KLINE_FETCH_LIMIT,
    OFFBOARD_KLINE_WORKERS,
    OFFBOARD_LIVE_QUOTE_LIMIT,
    OFFBOARD_MAIN_PCT_MIN,
    OFFBOARD_MIN_AMOUNT,
    OFFBOARD_MIN_FLOAT_CAP,
    OFFBOARD_OPENING_SILENCE_MIN,
    OFFBOARD_OPENING_SILENCE_MIN_UNADJUSTED,
    OFFBOARD_PREFILTER_AMOUNT_RATIO,
    OFFBOARD_PREFILTER_FLOAT_CAP_RATIO,
    OFFBOARD_PREFILTER_PCT_BUFFER,
    OFFBOARD_T1_TODAY_MAX,
    OFFBOARD_T2_TODAY_MAX,
    TREND_MARK_ENABLED,
    V_MO_DIVERGENCE_BEAR,
    now_beijing,
)
from scanner.data_source import ak_to_xq
from scanner.db.dal import save_offboard_print_snapshot, save_offboard_rejections
from scanner.db.queries import get_market_extra_snapshot, get_symbol_names
from scanner.display_gates import beauty_marks_daily, code_of, common_hard_gate
from scanner.features import ma_alignment_score
from scanner.hot_watch import is_hot_universe
from scanner.market_extra import fetch_fund_flow_rank
from scanner.models import make_kline_bar
from scanner.offboard_flow import load_flow_history, sum_main_net
from scanner.trading_session import is_trading_day, trading_minutes_elapsed
from scanner.trend_beauty import DAILY_BEAUTY_MIN_BARS
from scanner.utils import EXTERNAL_FAILURES, accum_5d, is_st, to_float
from scanner.validator import mo_divergence
from scanner.volume_bias import _UNSET as _BIAS_UNSET  # noqa: PLC2701 - 哨兵必须与 volume_bias 同一个对象
from scanner.volume_bias import biased_snapshot

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
    # N 日主力净额合计（元，2026-11-03）：**None = 不可判定**（历史不足 N 天）。
    # ⚠ 不可判定 ≠ 净流出 —— 门控时按 fail-open 放行（新股不该因「没历史」被砍）。
    flow_sum: float | None = None
    # 上报原值（2026-11-05 量比去偏）：`volume_ratio` 是**去偏后**的决策/展示口径，
    # 这里留一份**上报原值**落库（`offboard_launch_log.vol_ratio_raw`），供事后按
    # 原始尺度重放阈值。0 = 本轮没有原值（未去偏时）。
    volume_ratio_raw: float = 0.0
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
    「量比已去偏」**（`scanner/volume_bias.py`：上报量比 ÷ 全市场量能前置系数 k(m)）
    与 `OFFBOARD_OPENING_SILENCE_MIN` 那道静默窗口，两者是一组，不可只删其一。
    ⚠ k(m) 是**全市场同一个标量** ⇒ 去偏**不改变同一轮内的相对序**（排序键本身
    不受影响），改的是**读数的绝对尺度**（门槛判定 + 展示数字）。

    ⚠ A/B 两段**不混排**（同一张表内分段并列）：A 段的复合分里 `rank_change` 独占
    35/100，榜外票恒缺该项 ⇒ 混排必被永久压到最末。这不是「口径不可比」的抽象说法，
    而是具体的 35/100 结构性缺失。
    """
    return (_TIER_RANK.get(c.tier, 9), -c.volume_ratio, -c.main_pct)


# ── 门槛 ────────────────────────────────────────────────────────────────────


def _flow_gate_reason(c: OffboardCandidate) -> str | None:
    """N 日主力累计净流入门（2026-11-03）：不通过返回理由串。

    ⚠ `flow_sum is None` = 历史不足 N 天（**不可判定**）⇒ fail-open 放行，
    绝不当成「净流出」—— 否则上市越晚的票越容易被砍（隐性偏差）。
    ⚠ 这条与 `OFFBOARD_MAIN_PCT_MIN`（当日主力净占比）**不共线**（r=+0.051）：
    一条看资金的中期方向，一条看今日的瞬时状态 ⇒ 是增量信息，不是重复。
    详见 `scanner/offboard_flow.py` 模块 docstring 与
    `scripts/shadow_feature_backtest.py` 的检验输出。
    """
    if c.flow_sum is not None and c.flow_sum < OFFBOARD_FLOW_MIN_SUM:
        return f"{OFFBOARD_FLOW_WINDOW}日主力累计{c.flow_sum / 1e4:.0f}万<{OFFBOARD_FLOW_MIN_SUM / 1e4:.0f}万"
    return None


def offboard_gate(c: OffboardCandidate, *, retain: bool = False) -> str | None:
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

    `retain=True`（2026-10-09 留存机制）：只放宽**活跃度类**两门 —— 量比、成交额。
    适用对象是近 `OFFBOARD_DWELL_MIN` 分钟内严格过过门、本轮只在活跃度上回落的票
    （消阈值横跳）。涨幅带上下限、通用风险门、创业板面**全部照跑** —— 「不追高、
    不接跌」与风险否决不因留存松动。注意涨幅下限（percent > 0）不在此放宽之列：
    `classify_tier` 的 T1 带会再拦一次，且「不接跌」是有意为之。
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
    if not retain and c.volume_ratio < MOMENTUM_LAUNCH_VOL:
        return f"量比不足({c.volume_ratio:.2f}<{MOMENTUM_LAUNCH_VOL})"
    if not retain and c.amount < OFFBOARD_MIN_AMOUNT:
        return f"成交额不足({c.amount / 1e4:.0f}万<{OFFBOARD_MIN_AMOUNT / 1e4:.0f}万)"
    if c.float_market_capital < OFFBOARD_MIN_FLOAT_CAP:
        return f"流通市值过小({c.float_market_capital / 1e8:.1f}亿<{OFFBOARD_MIN_FLOAT_CAP / 1e8:.0f}亿)"
    return _flow_gate_reason(c)


# ── 拒绝理由的结构化（2026-11-12，m026）─────────────────────────────────────
#
# 为什么需要：`reason` 串把**当轮具体数值**编了进去（「量比不足(0.90<1.5)」），
# 于是同一道逻辑门产生数万条互不相同的串。实测生产库 44706 条 `offboard_rejections`
# 按 reason 精确 GROUP BY，主要「门」其实是 0.90/0.82/0.89… 这些数值碎片 ⇒
# 「哪道门杀得最多」这个最基本的问题答不出来。
#
# 修法：不改造 `offboard_gate` / `classify_tier` 的返回值（那两个函数被单测逐条
# 断言理由子串，改返回类型会连带改一大片），而是在**落库边界**把串解析成
# `(code, value)`。代价是解析依赖前缀契约 —— 由 `test_reason_codes_cover_all_branches`
# 守住：任何新增的 return 串若不匹配任何规则，`code` 会落成 `'unknown'`，测试会红。
#
# `reason` 串本身**原样保留**（人读 + 旧分析脚本兼容）。

#: (正则, code, 读数分组号 | None)。顺序敏感：先匹配到的赢。
#: 用正则而不是 `startswith`：部分理由串的**数值在开头**（`10日主力累计-500万<0万`、
#: `5日累计+26.00%不在[0,15)`），纯前缀匹配会漏。
_REASON_RULES: tuple[tuple[str, str, int | None], ...] = (
    # ── 门槛段（offboard_gate / common_hard_gate）──
    (r"^ST/退市风险股", "st", None),
    (r"^非创业板个股", "not_gem", None),
    (r"^非正常交易状态", "bad_status", None),
    (r"^无有效报价", "no_quote", None),
    (r"^无成交量", "no_volume", None),
    (r"^价格过高\(([-+]?\d+(?:\.\d+)?)", "price_too_high", 1),
    (r"^市值过大\(([-+]?\d+(?:\.\d+)?)", "market_cap_too_big", 1),
    (r"^主力净流出\(([-+]?\d+(?:\.\d+)?)", "fund_outflow", 1),
    (r"^当前非上涨状态", "not_rising", None),
    (r"^涨幅过高\(([-+]?\d+(?:\.\d+)?)", "pct_too_high", 1),
    (r"^量比不足\(([-+]?\d+(?:\.\d+)?)", "vol_ratio_low", 1),
    (r"^成交额不足\(([-+]?\d+(?:\.\d+)?)", "amount_low", 1),
    (r"^流通市值过小\(([-+]?\d+(?:\.\d+)?)", "float_cap_low", 1),
    (r"^\d+日主力累计([-+]?\d+(?:\.\d+)?)", "flow_sum_negative", 1),
    # ── 分层段（classify_tier）──
    (r"^无K线数据", "no_kline", None),
    (r"^K线未补取", "kline_quota", None),
    (r"^K线不足6根", "kline_short_accum", None),
    (r"^K线不足20根", "kline_short_ma", None),
    (r"^5日累计([-+]?\d+(?:\.\d+)?)", "accum_5d_out", 1),
    (r"^MA空头排列", "ma_bearish", None),
    (r"^主力净占比([-+]?\d+(?:\.\d+)?)", "main_pct_low", 1),
    (r"^顶背离", "bear_divergence", None),
    # ⚠ 实际串形如「T2在本段不启用(今日5.00%≥3.5)」—— 数值前有「今日」二字，
    # 故捕获组要跳过它（不能假设 `\(` 紧跟数字）。
    (r"^T2在本段不启用\([^()]*?([-+]?\d+(?:\.\d+)?)", "tier_disabled_t2", 1),
    (r"^T1在本段不启用\([^()]*?([-+]?\d+(?:\.\d+)?)", "tier_disabled_t1", 1),
    (r"^今日涨幅([-+]?\d+(?:\.\d+)?)", "pct_band_miss", 1),
)

_REASON_RULES_COMPILED = tuple((re.compile(pat), code, grp) for pat, code, grp in _REASON_RULES)


def reason_code(reason: str) -> tuple[str, float | None]:
    """拒绝理由串 → `(门 ID, 该门读数)`。`code='unknown'` = 未覆盖的新分支（应补规则）。

    读数取自该门规则里**显式声明的捕获组**（不是「括号里第一个数」—— 部分串的
    数值在开头，如 `10日主力累计-500万<0万`、`5日累计+26.00%不在[0,15)`）。
    解析不出来时 `value=None`（code 仍然正确，聚合不受影响）。
    """
    text = str(reason or "")
    for pat, code, grp in _REASON_RULES_COMPILED:
        m = pat.match(text)
        if m is None:
            continue
        if grp is None:
            return code, None
        try:
            return code, float(m.group(grp))
        except (TypeError, ValueError):  # pragma: no cover - 正则已保证可解析
            return code, None
    return "unknown", None


# ── 留存（dwell，2026-10-09）────────────────────────────────────────────────


def _recent_dwell_symbols(conn, today: str) -> set[str]:
    """近 `OFFBOARD_DWELL_MIN` 分钟内**严格**入过 B 段的 symbol 集合。

    依据 `offboard_launch_log.last_hit_time` —— 该列只被 `persist_round`（严格过门
    行）刷新，留存行刻意不写 ⇒ 留存窗口自最后一次严格过门起算、自然到期，不会
    自我续命。表缺失 / 查询失败 → 空集（退化为无留存，与改动前行为一致，fail-open）。
    """
    try:
        cutoff = (now_beijing() - _td(minutes=OFFBOARD_DWELL_MIN)).isoformat(timespec="seconds")
        rows = conn.execute(
            f"SELECT symbol FROM {_LOG_TABLE} WHERE date=? AND last_hit_time>=?",  # noqa: S608 - 常量表名
            (today, cutoff),
        ).fetchall()
        return {str(r[0]) for r in rows}
    except EXTERNAL_FAILURES as e:
        logger.warning("B段留存窗口查询失败（本轮无留存）: %s", e)
        return set()


def _retain_candidates(
    snapshot: dict[str, dict],
    recent: set[str],
    strict_syms: set[str],
    board_codes: set[str],
    skip: set[str],
    names: dict[str, str],
    *,
    conn: sqlite3.Connection | None = None,
) -> list[OffboardCandidate]:
    """近期成员中本轮未严格过门者 → 活跃度门放宽重评（`offboard_gate(retain=True)`）。

    与 `build_candidates` 同款的零成本前置过滤（创业板前缀 / 榜内 / 已推荐）——
    一只票早上以 B 段产出、中午冲进飙升榜，留存机制**不能**把它再捞回来。
    返回按量比降序、截断到 `OFFBOARD_DWELL_MAX`。

    ⚠ 流门（`OFFBOARD_FLOW_WINDOW` 日主力累计）在留存路径**照跑**（2026-11-03）：
    留存只放宽**活跃度类**两门（量比、成交额），其余门照严 —— 中期资金在撤的票
    不该靠留存机制续命。查询失败/历史不足 ⇒ fail-open（与严格门同语义）。
    """
    out: list[OffboardCandidate] = []
    for sym in recent - strict_syms:
        payload = snapshot.get(sym)
        if not payload:
            continue
        code = code_of(sym)
        if not code.startswith(("300", "301")) or code in board_codes or sym in skip:
            continue
        c = _snapshot_row_to_candidate(sym, payload, names)
        if offboard_gate(c, retain=True) is None:
            out.append(c)
    if conn is not None and out:
        sums = _load_flow_sums(conn, [c.symbol for c in out])
        kept: list[OffboardCandidate] = []
        for c in out:
            c.flow_sum = sums.get(c.symbol)
            if _flow_gate_reason(c) is None:
                kept.append(c)
        out = kept
    out.sort(key=lambda x: -x.volume_ratio)
    return out[:OFFBOARD_DWELL_MAX]


# ── 候选构建（快照 → 榜外候选）────────────────────────────────────────────────


# 实时报价 → 快照字段码的映射（`api._HOT_QUOTE_FIELDS` 的键 → `_FUND_FLOW_FIELDS` 的语义）。
# 只映射**两源都有语义且都会展示/参与判定**的字段。两类字段**刻意不覆盖**：
#   · `main_pct`（主力净占比）与 `volume_ratio`（量比）：雪球批量行情不提供
#     （量比只有 `quote.json?extend=detail` 单票接口才有），整行替换会清掉它们。
#     二者由东财资金流行（`_apply_live_fund_flow`，M1 + 2026-10-09 量比扩展）
#     以**同口径**（快照的 vol_ratio/main_pct 本就来自东财 f10/f184）刷成新鲜值。
#   · 上限 `OFFBOARD_LIVE_QUOTE_LIMIT` 保证请求数有界（默认 100 ⇒ 2 请求/轮）。
_LIVE_FIELD_MAP: tuple[tuple[str, str], ...] = (
    # (实时报价键, 快照字段码)
    ("current", "price"),
    ("percent", "percent"),
    ("volume", "volume"),
    ("amount", "amount"),
    ("turnover_rate", "turnover"),
    ("market_capital", "total_cap"),
    ("float_market_capital", "float_cap"),
)

# 🔴 `_LIVE_FIELD_MAP` 里**有符号**的字段（2026-10-14 修复 P1）：负值是合法行情
# （今日跌幅），只有 None / 0 才是脏值。这些字段**不可**套用 `_apply_live_quote`
# 对量纲字段的 `val > 0` 守卫 —— 见该函数 docstring 的实测故障。
# 目前只有 `percent`：其余六项（price/volume/amount/turnover/两个市值）都是量纲，
# 0 即停牌或脏值，沿用原守卫。改动本表须同步复核「有符号 vs 量纲」的归类。
_SIGNED_LIVE_FIELDS: frozenset[str] = frozenset({"percent"})


def _apply_live_quote(payload: dict, quote: dict) -> bool:
    """把**实时**报价就地覆盖进快照 payload，返回是否覆盖成功。

    为什么需要（2026-10-08 修复）
    --------------------------
    本段的行情此前**只**来自 `market_extra_cache` 的当日快照，而该快照的写入侧
    （`market_extra.collect_market_extra`）在全集拉取**超时**时曾只落当轮候选那几十行
    （2026-10-16 起已改为"取到多少落多少"，但超时轮本身仍会有未刷新行），
    其余 5000 行保持旧值；读侧 `get_market_extra_snapshot`
    又**不过 TTL**。两者相乘的后果是：一只票只要没进过当轮候选，它的行就永久冻结在
    上一轮成功拉取的时刻 —— 实测 300890 翔丰华在 13:40 显示的是 **11:29** 的
    29.96/+3.74%，而实时价已是 29.20/+1.11%（同一时刻用户侧读到 29.17）。

    本段的定位是「盘中异动」，门限（`MOMENTUM_LAUNCH_VOL=1.5`、T1/T2 涨幅带）全部按
    **实时**价校准 —— 用冻结快照去判「量先动·价未动」本身自相矛盾。故在**判定之前**
    用 `adapter.fetch_hot_quotes_batch` 补实时价，覆盖判定与展示共用的同一份数据
    （口径自洽：判定即所见，不存在「判定用旧值、展示用新值」的漂移）。

    覆盖是**逐字段**的（见 `_LIVE_FIELD_MAP` 的两类排除项），不是整行替换。

    🔴 「有符号 vs 量纲」不可一刀切（2026-10-14 修复 P1，实测故障）
    ---------------------------------------------------------
    覆盖守卫曾对**所有**字段一律 `val > 0`。该守卫对量纲字段是对的（0 = 停牌/脏值，
    覆盖会把可交易票误杀），但 **`percent` 是有符号的** —— 票一翻绿，实时 percent 就是
    负数，被守卫挡在门外 ⇒ 快照里那个**陈旧的正涨幅永久留下**，而同一行的 price/
    volume/amount/turnover 照常被刷成实时值。后果三重：

      1. **展示的涨幅是假的**：2026-10-14 实测 300758 七彩化学 —— 快照冻结在 09:44:35
         的 +2.85%（12.29/11.95），10:17 实时已是 **11.80/−1.26%**（last_close 11.95），
         终端仍显示 **+2.85%**。用户看到的就是这一行。
      2. **现价与涨幅自相矛盾**：同一行 price 是实时的、percent 是陈旧的，两者对不上
         （11.80 对 +2.85%），比「整体略旧」更伤 —— 它看起来是**算错了**，而不是**旧了**。
      3. **门也在陈旧值上判定**：`offboard_gate` 的 `percent > HOT_MIN_PERCENT(=0)`
         与 `classify_tier` 的 T1/T2 涨幅带都读这列 ⇒ 一只**已经翻绿**的票持续以
         「上涨异动」的身份留在榜外异动段里，直接违反本段「不接跌」的立段前提。

    修法：有符号字段只挡 `None`，量纲字段仍挡 `<= 0`（`_SIGNED_LIVE_FIELDS`）。
    修好后该票会被 `offboard_gate` 正常拒掉 —— 这才是「不接跌」。

    返回 False = 实时行情无该票/无有效字段，调用方保留快照值（fail-open）。
    """
    if not quote:
        return False
    applied = False
    for q_key, snap_key in _LIVE_FIELD_MAP:
        val = to_float(quote.get(q_key), None)
        if val is None:
            continue
        # 有符号字段（今日涨幅）负值合法，只挡 None；量纲字段仍要挡 <= 0（停牌/脏值）。
        # 一刀切 `val > 0` 会让翻绿的票永远刷不进实时涨幅 —— 见 docstring 的实测故障。
        if q_key not in _SIGNED_LIVE_FIELDS and val <= 0:
            continue
        payload[snap_key] = val
        applied = True
    # 名称一并刷新（雪球批量行情带 name）：通用门的 ST 判定依赖名称，
    # 用陈旧名称判 ST 与用陈旧价判涨幅是同一类错误。
    nm = str(quote.get("name") or "").strip()
    if nm:
        payload["name"] = nm
    return applied


def _apply_live_fund_flow(snapshot: dict[str, dict], ff: dict[str, dict]) -> int:
    """把**实时**资金流行（东财全集）的 main_pct / vol_ratio 就地覆盖进快照，返回覆盖行数。

    为什么需要（2026-10-08 修复 M1 + 2026-10-09 扩展量比）
    ----------------------------------------------------
    `main_pct`（主力净占比）是 T1/T2 信号门的输入（≥ `OFFBOARD_MAIN_PCT_MIN`），
    `vol_ratio`（量比）是 `sort_key` 的段内主键 + T1「量先动」门的输入 —— 两者
    雪球批量行情都不提供 ⇒ 此前恒取快照。快照在拉取超时轮只刷新取到的那些行
    （未取到的行冻结在上一轮成功时刻），门在陈旧值上判定、
    排序在陈旧量比上排座次，与实时价补全修复前的涨幅带是同一类错误。

    数据源用项目**既有**的东财全集资金流（`market_extra.fetch_fund_flow_rank`，
    push2delay）：其响应行本就带 f10 量比（`_FUND_FLOW_FIELDS` 的报价快照段，
    快照里的 vol_ratio 正是同一字段）—— 故覆盖是**同口径换新鲜值**，不引入
    第二个量比定义。与主循环 `collect_market_extra` 同源同进程缓存（TTL 300s）⇒
    稳态下本调用是缓存命中、零额外请求；冷缓存时一次全集批量拉取（有界
    `FUND_FLOW_FETCH_TIMEOUT=30s`，fail-soft 返回部分结果），并顺带给主循环预热。

    覆盖范围是**全快照**而非 prefilter 头部：ff 本就是 {6位代码: {...}} 全集
    映射，逐行覆盖只是字典查找 —— 让粗筛头部选择、门判定、`sort_key`、拒绝留痕、
    落库的量比/主占全部同口径新鲜。缺行/缺字段 → 保留快照值（fail-open）；
    量比 ≤0（停牌/脏值）不覆盖 —— 0 不是合法量比，覆盖会把可交易票误杀。
    """
    if not ff:
        return 0
    applied = 0
    for sym, payload in snapshot.items():
        if not payload:
            continue
        row = ff.get(code_of(sym))
        if not row:
            continue
        hit = False
        pct = to_float(row.get("main_pct"), None)
        if pct is not None:
            payload["main_pct"] = pct
            hit = True
        vr = to_float(row.get("vol_ratio"), None)
        if vr is not None and vr > 0:
            payload["vol_ratio"] = vr
            hit = True
        if hit:
            applied += 1
    return applied


def _refresh_with_live_quotes(
    adapter,
    snapshot: dict[str, dict],
    symbols: Sequence[str],
) -> set[str]:
    """对 `symbols` 补实时报价并就地覆盖 `snapshot`，返回成功覆盖的 symbol 集。

    在**过门之前**调用，故 `symbols` 是「有机会过门」的集合而非最终展示的 TOP N ——
    TOP N 是判定的产物（`sort_key` 依赖分层与量比），不可能先知道 TOP N 再拿实时价
    去判定它。故调用方须**先用零成本的快照字段粗筛**出候选头部（见
    `run_offboard_watch`），把请求量压到 1~2 个/轮：雪球 `batch/quote.json` 是
    50 票/批（`api.fetch_hot_quotes_batch` 的 `batch_size`），实测创业板 1411 只
    全量补需~29 请求/轮 —— 相对 60s 刷新周期不可接受，故必须粗筛。

    fail-open：批量行情失败/返回空 → 返回空集，快照原样保留（本段退化为**旧口径**
    而非整段消失）。这是刻意取舍：B 段的门限本按实时价校准，但宁可展示略旧的值，
    也不要因为一次网络抖动让整段留空。
    """
    if not symbols:
        return set()
    fetch_batch = getattr(adapter, "fetch_hot_quotes_batch", None)
    if fetch_batch is None:
        return set()
    try:
        quotes = fetch_batch(list(symbols))
    except EXTERNAL_FAILURES as e:
        logger.warning("B 段实时行情补全失败（沿用快照值）: %s", e)
        return set()
    if not quotes:
        return set()
    ok: set[str] = set()
    for sym, q in quotes.items():
        payload = snapshot.get(sym)
        if payload and _apply_live_quote(payload, q):
            ok.add(sym)
    return ok


def _snapshot_row_to_candidate(symbol: str, payload: dict, names: dict[str, str]) -> OffboardCandidate:
    """资金流快照的一行 → `OffboardCandidate`（**未过门**）。

    字段名映射（东财字段码见 `market_extra._FUND_FLOW_FIELDS`，ulist 与 clist 同码表）：
    price→现价、percent→今日涨幅、amount→成交额、float_cap→流通市值、
    total_cap→总市值、turnover→换手率、vol_ratio→量比、main_pct→主力净占比。

    ⚠ `payload` 可能已被 `_apply_live_quote` 就地覆盖为实时值（见其docstring）——
    本函数对「快照值」与「实时值」无感，同一套字段码解析，故覆盖对它是透明的。
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
        # 上报原值：量比去偏时由 `volume_bias.apply_bias` 写进快照（vol_ratio_raw），
        # 未去偏时为 0 ⇒ 落库时写 NULL（旧行同义）。
        volume_ratio_raw=to_float(payload.get("vol_ratio_raw"), 0.0) or 0.0,
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
    *,
    conn: sqlite3.Connection | None = None,
) -> tuple[list[OffboardCandidate], list[tuple[str, str]]]:
    """资金流快照 → **榜外创业板**、已过 `offboard_gate` 的候选（按量比降序）。

    返回 (候选, 排除明细 [(symbol, 原因)])。明细只用于日志/自检，不落库。

    「榜外」= 不在本轮飙升榜里（`board_items` 的 6 位代码集合）。`exclude_symbols`
    另排今日已推荐票 —— 与 v1 回捞区同款先例，避免同一只票在主表和 B 段同屏两现。
    候选按量比降序：决定 K 线补取额度（`OFFBOARD_KLINE_FETCH_LIMIT`）优先给谁。

    `conn`（2026-11-03）：给了就启用 **N 日主力累计净流入门**（中期资金方向）。
    ⚠ **两阶段判定**（性能）：流门放最后 —— 先跑完其它门得到幸存者，**只对幸存者**
    批量查历史资金流（全市场 1400+ 只查一次要 1.4s，幸存者通常几十只 ⇒ ~30ms）。
    ⚠ **不可判定 ≠ 净流出**：历史不足 N 天 ⇒ `flow_sum is None` ⇒ fail-open 放行
    （否则上市越晚的票越容易被砍）。查询失败（sqlite3.Error）也一律 fail-open。
    """
    board_codes = {code_of(str(it.get("symbol") or "")) for it in board_items if it.get("symbol")}
    skip = exclude_symbols or set()
    out: list[OffboardCandidate] = []
    rejects: list[tuple[str, str]] = []
    sums: dict[str, float] = {}

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

    # ── 第二阶段：N 日主力累计净流入门（2026-11-03）──
    # 放最后是**性能**考虑：全市场 1400+ 只查一次要 1.4s，而幸存者通常几十只。
    # ⚠ 这里跑的是 `_flow_gate_reason` 而不是 `offboard_gate` —— 其它门已全部跑过，
    #   不必再跑一遍（且 `offboard_gate` 内部含流门，会重复判定）。
    if conn is not None and out:
        sums = _load_flow_sums(conn, [c.symbol for c in out])
        kept: list[OffboardCandidate] = []
        for c in out:
            c.flow_sum = sums.get(c.symbol)
            reason = _flow_gate_reason(c)
            if reason:
                rejects.append((c.symbol, reason))
                continue
            kept.append(c)
        out = kept

    out.sort(key=lambda x: -x.volume_ratio)
    return out, rejects


def _load_flow_sums(conn: sqlite3.Connection, symbols: list[str]) -> dict[str, float]:
    """N 日主力净额合计 {symbol: 元}；历史不足 N 天 / 查询失败的**不在结果里**
    （调用方按「不可判定 ⇒ fail-open 放行」处理）。

    ⚠ fail-open 是**刻意的**：把「查不到」当成「净流出」会砍掉新股与数据缺失的票，
    形成「上市越晚越容易被砍」的隐性偏差（与该门想抓的「资金在撤」不是一回事）。
    """
    try:
        history = load_flow_history(conn, symbols, window=OFFBOARD_FLOW_WINDOW)
    except sqlite3.Error as e:
        logger.warning("B段资金流历史查询失败（放行本轮）: %s", e)
        return {}
    out: dict[str, float] = {}
    for sym, hist in history.items():
        total = sum_main_net(hist, window=OFFBOARD_FLOW_WINDOW)
        if total is not None:
            out[sym] = total
    return out


# ── T1 / T2 分层 ────────────────────────────────────────────────────────────


def classify_tier(
    c: OffboardCandidate,
    klines: list | None,
    today: str,
    *,
    allowed_tiers: tuple[str, ...] = (T2, T1),
) -> tuple[str | None, str]:
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
        if T2 not in allowed_tiers:
            # 2026-09-30：榜内异动段传 (T1,) 把 T2 关掉。**不是**新增阈值，
            # 只是把「两层都产出」窄化为「只产 T1」——下面所有条件仍逐条照跑，
            # 被关掉的那层返回理由串与其它不产出一致（调用方展示效果相同、归因不同）。
            return None, f"T2在本段不启用(今日{c.percent:.2f}%≥{MOMENTUM_LAUNCH_TODAY_MIN:g})"
        ma = _ma_not_bearish(hist)
        if ma is None:
            return None, "K线不足20根(MA不可判定)"
        if not ma:
            return None, "MA空头排列(MA5<=MA10)"
        # 2026-09-22 起 T2 与 T1 **同门**：旧注释的「T2 不设该条」建立在
        # 「T2 几乎不产出」的前提上，MA 放宽后前提失效（见 config_hot_watch）。
        if c.main_pct < OFFBOARD_MAIN_PCT_MIN:
            return None, f"主力净占比{c.main_pct:+.2f}%<{OFFBOARD_MAIN_PCT_MIN:g}"
        # 顶背离（2026-10-08 修复 M2）：与 MA 判据同一口径，吃**剔除今日**后的序列。
        # 此前吃含今日的 klines —— K 线池末根是当日盘中 bar（当日一次性抓取、全天不更新），
        # 拿它当「今日收盘」判背离会让判定随抓取时刻随机化（早盘抓的票全天用早盘价）。
        # MA 门已保证此时 len(hist) ≥ 20 > mo_divergence 的 15 根下限，不会退化成 data_short。
        closes = [to_float(k.get("close"), 0.0) or 0.0 for k in hist]
        div, div_detail = mo_divergence(closes, hist)
        if div == V_MO_DIVERGENCE_BEAR:
            return None, f"顶背离({div_detail})"
        return T2, f"启动首日(累计{accum:+.2f}% 今日{c.percent:+.2f}% 量比{c.volume_ratio:.2f})"

    if HOT_MIN_PERCENT < c.percent < OFFBOARD_T1_TODAY_MAX:
        if T1 not in allowed_tiers:
            return None, f"T1在本段不启用(今日{c.percent:.2f}%)"
        ma = _ma_not_bearish(hist)
        if ma is None:
            return None, "K线不足20根(MA不可判定)"
        if not ma:
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

    2026-10-08 修复 M2：`mo_divergence`（顶背离）与美感标记也已切换到本函数的
    剔除口径 —— 此前顶背离吃含今日的 klines（K 线池末根是当日盘中 bar，当日一次
    抓取全天不更新），判定随抓取时刻漂移；美感标记同理。
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


def annotate(
    c: OffboardCandidate,
    klines: list | None,
    today: str,
    *,
    allowed_tiers: tuple[str, ...] = (T2, T1),
) -> str | None:
    """给候选打上 tier / 美感标记 / 理由串，返回 tier（None = 不产出）。

    **生产路径与离线自检共用**：自检说「通过」而生产实际不产出，是这类哨兵最坏的
    失效方式（守卫绿灯、线上空转）。故后处理只有这一份。

    `allowed_tiers`（2026-09-30 新增，默认两层全开 = B 段原行为不变）：榜内异动段
    传 `(T1,)` 关掉 T2。默认值保证既有调用方行为逐字节不变。
    """
    tier, why = classify_tier(c, klines, today, allowed_tiers=allowed_tiers)
    if tier is None:
        c.reasons = [why]
        return None
    c.tier = tier
    # 美感标记（2026-10-08 修复 M2）：与 MA/顶背离同口径，吃剔除今日后的序列 ——
    # K 线池末根是当日盘中 bar（当日一次性抓取、全天不更新），拿它当「今日收盘」
    # 判美感会随抓取时刻漂移。本区美感只作标记不作门，偏差只影响展示列。
    blocked, mark, detail = beauty_marks_daily(_exclude_today(klines, today))
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


def load_offboard_klines(
    conn,
    adapter,
    symbols: list[str],
    fetch_date: str | None = None,
    *,
    fetch_symbols: Collection[str] | None = None,
) -> dict[str, list]:
    """榜外 K 线池：当日缓存优先，缺失者并发补取后落库 → {symbol: [bar, ...]}。

    `fetch_symbols`（2026-11-03）：**只允许对这批 symbol 发补取请求**，其余仅读当日
    缓存；None = 对全部缺失者补取（回填 / CLI 路径，调用方已自行定量）。
    存在的理由见 `run_offboard_watch` 里「额度只管补取、不管读缓存」那段。

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
    # 🔴 额度只管**补取请求**，不管**读缓存**（2026-11-03）：读缓存是一条分块 SELECT，
    # 零网络成本，没有理由跟着请求额度一起截断。此前调用方把额度施加在**入参**上，
    # 额度外的票连「当天已缓存的 K 线」都读不到（实测 2026-10-23：366 只被记为
    # 「无K线数据(榜外池未覆盖)」，其中 126 只当日已缓存）。
    allowed = None if fetch_symbols is None else set(fetch_symbols)
    missing = [s for s in uniq if s not in result and (allowed is None or s in allowed)]
    if missing and adapter is not None:
        fetched = _fetch_klines_batch(adapter, missing)
        if fetched:
            _save_klines(conn, fetched, day)
            result.update(fetched)
    return result


# ── 落库与回填（可验证性的硬前置）────────────────────────────────────────────


def persist_round(
    conn,
    rows: Sequence[OffboardCandidate],
    display_top: int = OFFBOARD_DISPLAY_TOP,
) -> None:
    """B 段逐日落库（(date, symbol) 唯一）。失败只告警，不影响本轮结果返回。

    🔴 **信号值首次写入即冻结**（2026-09-21 修）：`name / tier / percent / accum_5d /
    vol_ratio / main_pct / amount / float_cap / price` 九列在 ON CONFLICT 时**不更新**，
    只有 `last_hit_time` 与 `updated` 每轮刷新。
    ⚠ `vol_ratio` 的语义 = **落库那刻门/排序/展示实际用的值**（跨历史一致）；2026-11-05
    的量比去偏起它是**去偏值**，同时新增 `vol_ratio_raw` 存上报原值（m024）⇒ 分析侧
    统一读 `coalesce(vol_ratio_raw, vol_ratio)` 即得原值尺度（跨改动可比）。

    `top_hits`（m019）：**前 `display_top` 行**每轮 +1，其余行恒 0 ——
    原是「反复进前十」粉名/🔥 标识（2026-10-14~15 四版沿革）的落库依据；
    **标识已于 2026-10-16 按用户决策整体删除**，本列照常累加（列已发布，id 不得
    改名/删列；与 gate_hits 同款作研究原料保留）。

    `display_top`（2026-11-03）：默认 `OFFBOARD_DISPLAY_TOP`，由 `run_offboard_watch`
    传入**本轮实际的 `top_n`**。此前写死常量 ⇒ `--top 3` / 调用方改 `top_n` 时该列
    与「实际展示了几行」不一致（生产恒等，CLI 与单测不等）。

    ⚠ 2026-10-16 修复（计数口径）：ON CONFLICT 原为 `top_hits=top_hits+1` ——
    **无条件 +1**，把 INSERT 时算出的 `1 if i < display_top else 0` 丢掉。
    而本函数收到的是**全部过门行**（`run_offboard_watch` 只对展示做
    `passed[:top_n]`）⇒ 排在前 N 之外的行首插记 0、此后每轮仍 +1，计数实为
    「过门轮次 − 1」：一只**从未进过展示前 N** 的票也能攒到门槛，正是 m019 被
    否定的「进前十 ≈ 过门」。现改为 `top_hits=top_hits+excluded.top_hits`
    （`excluded.top_hits` 就是本次算出的 0/1）⇒ 前 N 之外的行**恒为 0**。

    `gate_hits`（m020，2026-10-14）：**所有过门行**每轮 +1（不只前 N）—— 曾是
    「今日活跃」在场率标识（today_presence，已退场）的分子，现仅作研究原料。
    ⚠ 严格说它计的是「过门 **且分层通过**」的轮次（本函数收到的是 `passed`）——
    只过 `offboard_gate`、被 `classify_tier` 判掉的行**不累加**（那类行只进
    `offboard_rejections`）。2026-11-03 更正文案：旧注释写「所有过门行」会让人
    拿它当「gate 通过率」的分子，两者差一个分层。
    与 top_hits 同为计数列（ON CONFLICT 累加，跨日因 (date, symbol) 主键自然
    重置）。留存行不入本表 ⇒ 不累。

    同函数顺带记当日扫描轮数账（`offboard_round_log`，m020）：**每调用一次就是
    一轮**，与本轮过门行数无关 —— `rows` 为空也记账（run_offboard_watch 的
    「过门池空」早退分支会以 `[]` 调用本函数，那些轮同样跑过了门，分母不能漏：
    午后过门池变小时漏记会把在场率虚高）。真正不记账的只有**没跑门**的轮
    （开盘静默窗 / 快照空，那些轮在更早的分支就返回了）。UPSERT 单行（date 主
    键），ON CONFLICT 累加。

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
    if conn is None:
        return
    day = now_beijing().date().isoformat()
    now = now_beijing().isoformat(timespec="seconds")
    try:
        if rows:
            conn.executemany(
                f"""INSERT INTO {_LOG_TABLE}
                (date, symbol, name, tier, percent, accum_5d, vol_ratio, vol_ratio_raw, main_pct,
                 amount, float_cap, price, first_time, last_hit_time, top_hits, gate_hits, updated,
                 flow_sum, flow_judged, vol_bias_k)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(date, symbol) DO UPDATE SET
                  last_hit_time=excluded.last_hit_time,
                  top_hits=top_hits+excluded.top_hits,
                  gate_hits=gate_hits+1,
                  updated=excluded.updated,
                  -- 🔴 m026：这三列是**判定读数**（不是信号身份），故**每轮刷新**。
                  -- 若跟着信号列一起冻结 ⇒ 当天已存在的票永远拿不到 flow_sum
                  -- （新列只在首次写入时才有值，而那正是「信号刚被推出」那一刻 ——
                  --   彼时 flow_sum 已算出来，但若当天该票已在库，冻结就把它挡在门外）。
                  -- 与信号列的区别：信号列回答「它以什么身份被推出」（要冻结），
                  -- 这三列回答「本轮判它时用了什么数」（要最新）。
                  flow_sum=excluded.flow_sum,
                  flow_judged=excluded.flow_judged,
                  -- ⚠ `vol_bias_k` 用 COALESCE 而非直接覆盖：`vol_ratio`/`vol_ratio_raw`
                  -- 是**冻结**列（首次写入时的值），而 k 是从这对值算出来的。
                  -- 若本轮候选没带 raw（该票本轮未走去偏覆盖），excluded 里是 NULL ⇒
                  -- 直接覆盖会把迁移回填的历史 k 冲成 NULL，而 raw/ratio 还在
                  -- （实测 45 → 32 行丢 k）。COALESCE(excluded, 旧值) 保住它。
                  -- ⚠ 不能反过来写 COALESCE(旧值, excluded) —— 那样本轮的新 k 永远写不进去。
                  vol_bias_k=COALESCE(excluded.vol_bias_k, vol_bias_k)""",  # noqa: S608 - 常量表名
                [
                    (
                        day,
                        c.symbol,
                        c.name,
                        c.tier,
                        round(c.percent, 3),
                        round(c.accum_5d, 3),
                        round(c.volume_ratio, 3),
                        # 上报原值（2026-11-05 量比去偏）：0 = 本轮未去偏 ⇒ 写 NULL，
                        # 读侧 `coalesce(vol_ratio_raw, vol_ratio)` 恒得原值尺度。
                        round(c.volume_ratio_raw, 3) if c.volume_ratio_raw > 0 else None,
                        round(c.main_pct, 3),
                        c.amount,
                        c.float_market_capital,
                        c.current,
                        now,
                        now,
                        1 if i < display_top else 0,
                        1,
                        now,
                        # N 日主力净额合计（m026）：None = 不可判定（历史不足 / 查询失败）
                        # ⇒ 落 NULL，且 flow_judged=0 明确标记「没判定过」。
                        # ⚠ 不能写 0 —— 0 是「恰好持平」的合法值，与「不可判定」语义相反。
                        c.flow_sum,
                        0 if c.flow_sum is None else 1,
                        # 量比去偏系数 k(m)（m026）：本轮 raw/ratio 即 k。未去偏时 None。
                        round(c.volume_ratio_raw / c.volume_ratio, 4)
                        if c.volume_ratio_raw > 0 and c.volume_ratio > 0
                        else None,
                    )
                    for i, c in enumerate(rows)
                ],
            )
        # 当日扫描轮数账（m020）：每次调用 = 一轮（含 rows 为空的过门池空轮，
        # 见上方 docstring —— 分母完整性）。
        conn.execute(
            "INSERT INTO offboard_round_log(date, rounds, updated) VALUES(?,1,?) "
            "ON CONFLICT(date) DO UPDATE SET rounds=rounds+1, updated=excluded.updated",
            (day, now),
        )
        conn.commit()
    except EXTERNAL_FAILURES as e:
        logger.warning("B段逐日落库失败（本轮结果不受影响）: %s", e)


# 次日 bar 的「间隔合理性」自然日上限：覆盖 A 股最长连续休市（国庆 8 个自然日、
# 春节 9~12 个自然日）。原实现写死 4（只够覆盖周末）—— 见 `_next_bar_is_next_session`。
_MAX_BACKFILL_GAP_DAYS = 12


def _next_bar_is_next_session(d: str, nxt: str) -> bool:
    """`nxt` 是否就是 `d` 之后**紧邻的下一个交易日**（回填防呆 1，2026-11-03 重写）。

    旧实现 = 「`nxt - d` 相差 ≤4 个自然日」。那 4 只够覆盖周末（3 天），而**国庆/春节的
    休市跨度是 8~12 个自然日** ⇒ 节前最后一个交易日的信号会被整批判为「次日 bar 太远」
    而**永久丢失标签**（不是延后，是永不回填：`next_day_pct IS NULL` 的查询每轮都会
    重新判一次，判据本身不会变）。实测 `scanner.db`：`2026-09-30` 那 45 行
    `next_day_pct` 至今 **0/45**，而 09-30 与 10-08 两根 bar 在池里都存在
    （`(10-08 − 09-30).days = 8 > 4`）。丢的恰好是「节前一档」这整个 regime，
    不是随机样本。

    新判据 = **区间内不得有已登记的交易日**（区分停牌与休市的唯一可判据）：
      · 跨国庆（09-30 → 10-08，中间 10-01~10-07 全是登记假日）⇒ 放行 ✓
      · 跨周末（周五 → 周一）⇒ 放行 ✓
      · 停牌 3 天（周二 → 下周一，中间 3 个交易日）⇒ 拦下 ✓（验不了「次日」）
      · 停牌 + 假期混合（09-29 → 10-09，中间 09-30/10-08 是交易日）⇒ 拦下 ✓

    ⚠ `holidays.json` 只覆盖到 2026-10-07（人工维护）：**未登记的假日**会让中间某天被
    误当交易日 ⇒ 保守拦下 ⇒ 该行不产出标签。方向是对的（宁缺勿错），代价是那张表需要
    继续维护（见 `scanner/holidays.py` docstring 的双向故障说明）。
    """
    try:
        d0 = _date.fromisoformat(d)
        d1 = _date.fromisoformat(nxt)
    except ValueError:
        return False  # 脏日期字符串：验不了就不产出
    gap = (d1 - d0).days
    if gap <= 0 or gap > _MAX_BACKFILL_GAP_DAYS:
        return False
    cur = d0 + _td(days=1)
    while cur < d1:
        if is_trading_day(cur):
            return False
        cur += _td(days=1)
    return True


def backfill_next_day(conn, adapter, signal_date: str | None = None) -> int:
    """回填 `offboard_launch_log.next_day_pct`（次日收益 %），返回回填行数。

    口径：信号日收盘价 → **次一交易日**收盘价。榜外票不在 `daily_kline`，数据源是本区
    自己的榜外 K 线池（会按需补取，结果留在池里，下一轮直接命中）。

    ⚠ 三个必须的防呆（1、2 源自 `daily_kline` 已踩过的坑，3 是 2026-11-03 新增）：
      1. **不能假设「下一根 bar = 下一交易日」** —— 日线源在停牌/跳空时会缺 bar，
         故要求 `d` 与 `nxt` 之间**没有任何已登记的交易日**（`_next_bar_is_next_session`），
         否则跳过。⚠ 原实现是「相差 ≤4 个自然日」，只够覆盖周末 ⇒ 国庆/春节（8~12 个
         自然日）会把节前那一档信号**永久**判为「太远」（实测 2026-09-30 那 45 行）。
      2. **信号日必须早于今天**（`date < today`）—— 否则每轮都会为当天的信号去找
         「次日 bar」，既永远找不到、又会把当天的 K 线池反复翻一遍。
      3. 🔴 **`nxt` 必须早于今天**（`nxt < today`）—— 否则那根「次日 bar」是**当日盘中
         bar**。见下段。

    🔴 为什么 `nxt` 不能是「今天」（2026-11-03 修，实测口径故障）
    --------------------------------------------------------
    K 线池的末根就是当日盘中 bar（`_exclude_today` docstring 实测 215/215），而本函数
    在**交易时段每轮都跑**（`unified_scanner` 主循环的交易分支；非交易分支不扫）⇒
    放行 `nxt == today` 等于把「次日收益」定义成「信号日收盘 → 次日第一次成功取到
    K 线那一刻（≈09:45）」，而 `next_day_pct IS NULL` 的写入条件让这个值**永久冻结**
    （主链路 `backtest.backfill_outcomes` 是覆盖式重算、能自愈；本链路不能）。

    实测（`scanner.db` 里两根 bar 都已定稿的 703 行 / 23 个信号日，用「严格晚于次日」
    那份 `offboard_kline_cache` 重算真值）：
      · 偏差绝对值 > 2pp：322/703（46%）；符号相反：176/703（25%）；
      · ≤−5% 尾部：现库 5.26% vs 真值 **12.66%**（下行尾部被砍掉一半多）；
      · ≥7% 命中：3.70% vs 5.69%；标准差 3.59 vs 4.95（整体被压缩）；
      · 极端例：`SZ300695 2026-10-29` 现库 **+20.00**（盘中涨停价），收盘→收盘实为 **+5.50**。

    故标签**延后一个交易日**：等次日之后的某次取数（那时 `nxt` 的 bar 已定稿）再回填。
    `--backfill` 也绕不过去 —— 池里 `nxt` 那行在当日第一次取数时就冻成了盘中值，
    收盘后再读仍是它。
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
    # 批量取数（2026-10-08 修复 N+1）：原先循环内逐票 `load_offboard_klines([sym])`，
    # 每票一次 SQL 读缓存 + 一次线程池创建/销毁 + **串行**请求；该函数本就支持批量
    # （缓存 400/批 + OFFBOARD_KLINE_WORKERS 并发补取），合并为一次调用后 N 个请求
    # 并发跑、缓存读写各一次。请求总数不变（缓存命中票零请求），变的是并发度与开销。
    bars_map = load_offboard_klines(conn, adapter, list(by_symbol))
    for sym, dates in by_symbol.items():
        bars = bars_map.get(sym) or []
        # ⚠ `None` 日期的 bar 不能进映射（否则 `by_date` 会出现 `None` 键）。
        #   真实来源 `load_offboard_klines` 已清洗，但直连/回放路径未必。
        by_date = {k["date"]: k for k in bars if isinstance(k.get("date"), str)}
        for d in dates:
            later = sorted(x for x in by_date if x > d)
            if not later:
                continue
            nxt = later[0]
            # 防呆 3：`nxt` 就是今天 ⇒ 那根是当日盘中 bar（池里当天第一次取数即冻结），
            # 拿它当「次日收盘」会把口径改成「收盘→次日 09:45」且永不修正。
            if nxt >= today:
                continue
            # 防呆 1（2026-11-03 重写）：`nxt` 必须是 `d` 之后**紧邻的下一个交易日**。
            # 原实现是「相差 ≤4 自然日」，跨国庆/春节时会把整批信号判为太远。
            # 日期解析失败也在 `_next_bar_is_next_session` 内部收口（返回 False）。
            if not _next_bar_is_next_session(d, nxt):
                continue
            # 🔴 2026-10-08 修复 KeyError：`later` 只保证「有比 d 晚的 bar」，
            #   **不保证 d 自己在 by_date 里** —— `by_date` 来自 `_clean_bars`，
            #   它按 `make_kline_bar` 契约剔除脏 bar（close<=0 / 日期非法），故
            #   信号日 bar 缺失是常态（当天没落库、停牌、脏数据）。
            #   原先 `by_date[d]` 抛 KeyError，被 `run_offboard_watch` 的
            #   `except EXTERNAL_FAILURES` 吞掉 ⇒ **该轮所有 symbol 的回填全废**，
            #   标签静默停止积累。改为 `.get()` + 跳过（验不了就不产出）。
            b0 = by_date.get(d)
            b1 = by_date.get(nxt)
            p0 = (to_float(b0.get("close"), 0.0) or 0.0) if b0 else 0.0
            p1 = (to_float(b1.get("close"), 0.0) or 0.0) if b1 else 0.0
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


# ── 一次性修数：重算**已冻结**的 next_day_pct（2026-11-03）───


def repair_next_day_labels(conn, *, dry_run: bool = True) -> dict:
    """重算已冻结的 `offboard_launch_log.next_day_pct`，返回一份差异账（不打印）。

    为什么需要这个工具（与 `backfill_next_day` 的分工）
    ----------------------------------------------------
    `backfill_next_day` **只填 NULL**（`WHERE next_day_pct IS NULL`）—— 这个「首次写入即
    冻结」的语义对信号列（`tier`/`percent`/`vol_ratio`）是对的，对标签列是错的：旧实现
    把标签定成了「信号日收盘 → 次日**盘中**价」（K 线池末根是当日盘中 bar，而回填在交易
    时段每轮都跑），而冻结让它**永不修正**。修了写入侧（防呆 3 `nxt < today`）只能保证
    今后的标签正确，库里的历史标签不会自愈。

    实测（本题就是为这 903 行写的）：两根 bar 都已定稿的 703 行里，46% 与真值差 >2pp、
    25% 符号相反、≤−5% 尾部 5.26% vs 真值 12.66%。不修，则 `scripts/offboard_nextday.py`
    会把这批错样本与正确样本混算。

    口径（三条防呆全部与写入侧同源）
    ------------------------------
    1. `nxt` = 池里 `d` 之后的第一根 bar，且必须通过 `_next_bar_is_next_session`；
    2. `nxt < today`（否则那根是当日盘中 bar）；
    3. **取数只用 `fetch_date > nxt` 的那份缓存** —— 那是唯一能保证「`nxt` 那根已定稿」的
       证据（当初写错就是因为用了 `fetch_date == nxt` 的盘中快照）。没有这样的缓存则
       该行**跳过**，不猜。

    且**零联网**：只读 `offboard_kline_cache`，不传 adapter、不发请求（修数是离线作业，
    不该因一次网络抖动写成半截）。

    `dry_run=True`（默认）只算差异不落库。返回：
        {"checked": n, "same": m, "changed": [(date, symbol, old, new), ...],
         "skipped": {reason: count}, "old_mean": f, "new_mean": f}
    """
    today = now_beijing().date().isoformat()
    report: dict = {"checked": 0, "same": 0, "changed": [], "skipped": {}, "old_mean": None, "new_mean": None}
    if conn is None:
        return report
    try:
        rows = conn.execute(
            f"SELECT date, symbol, next_day_pct FROM {_LOG_TABLE} "  # noqa: S608 - 常量表名
            "WHERE next_day_pct IS NOT NULL"
        ).fetchall()
    except EXTERNAL_FAILURES as e:
        logger.warning("B段标签重算查询失败: %s", e)
        return report
    if not rows:
        return report
    report["checked"] = len(rows)

    def _skip(reason: str) -> None:
        skipped = report["skipped"]
        skipped[reason] = skipped.get(reason, 0) + 1

    # 每票只取**最新一份**缓存：它含最长的已定稿历史，且只有它可能满足 `fetch_date > nxt`。
    symbols = sorted({sym for _d, sym, _nd in rows})
    latest: dict[str, tuple[str, dict]] = {}
    for i in range(0, len(symbols), 400):
        chunk = symbols[i : i + 400]
        placeholders = ",".join("?" * len(chunk))
        try:
            cached = conn.execute(
                f"SELECT symbol, fetch_date, payload_json FROM {_KLINE_TABLE} "  # noqa: S608 - 常量表名 + 占位符
                f"WHERE symbol IN ({placeholders}) ORDER BY fetch_date",
                tuple(chunk),
            ).fetchall()
        except EXTERNAL_FAILURES as e:
            logger.warning("B段标签重算读 K 线池失败: %s", e)
            continue
        for sym, fetch_date, payload in cached:
            try:
                latest[sym] = (fetch_date, {b["date"]: b for b in _clean_bars(json.loads(payload)) if b.get("date")})
            except (json.JSONDecodeError, TypeError):
                continue

    now = now_beijing().isoformat(timespec="seconds")
    changed: list[tuple[str, str, float, float]] = []
    for d, sym, old in rows:
        entry = latest.get(sym)
        if not entry:
            _skip("无K线池缓存")
            continue
        fetch_date, by_date = entry
        later = sorted(x for x in by_date if x > d)
        if not later:
            _skip("池里没有更晚的bar")
            continue
        nxt = later[0]
        if nxt >= today:
            _skip("次日bar未定稿")
            continue
        if not _next_bar_is_next_session(d, nxt):
            _skip("次日bar不是紧邻交易日")
            continue
        if fetch_date <= nxt:
            _skip("缓存早于次日收盘(无法确认定稿)")
            continue
        p0 = to_float((by_date.get(d) or {}).get("close"), 0.0) or 0.0
        p1 = to_float((by_date.get(nxt) or {}).get("close"), 0.0) or 0.0
        if p0 <= 0 or p1 <= 0:
            _skip("缺信号日或次日的收盘价")
            continue
        new = round((p1 - p0) / p0 * 100.0, 4)
        if old is not None and abs(new - old) < 1e-6:
            report["same"] += 1
            continue
        changed.append((d, sym, float(old) if old is not None else float("nan"), new))

    report["changed"] = changed
    vals_old = [c[2] for c in changed if c[2] == c[2]]  # 排除 nan
    report["old_mean"] = round(sum(vals_old) / len(vals_old), 4) if vals_old else None
    report["new_mean"] = round(sum(c[3] for c in changed) / len(changed), 4) if changed else None

    if dry_run or not changed:
        return report
    try:
        conn.executemany(
            f"UPDATE {_LOG_TABLE} SET next_day_pct=?, updated=? WHERE date=? AND symbol=?",  # noqa: S608 - 常量表名
            [(new, now, d, sym) for d, sym, _old, new in changed],
        )
        conn.commit()
    except EXTERNAL_FAILURES as e:
        # 修数是离线作业：失败就原样报出来（不许静默改成部分成功）
        logger.warning("B段标签重算写入失败: %s", e)
        raise
    return report


# ── 单轮主流程 ──────────────────────────────────────────────────────────────


def opening_silence_active(now: datetime | None = None) -> bool:
    """开盘静默窗口是否生效（True = 本轮不产出）。

    ⚠ **2026-11-05 重做**：窗口从 15 分钟缩到 **5 分钟**，因为开盘的量比失真现在
    可以被**算出来并扣掉**（`scanner/volume_bias.py`），不必再靠「等它自己收敛」。
    本函数只管「开盘头几分钟一律不产出」这件最朴素的事；**能不能提前放行**取决于
    去偏系数是否可得 ⇒ 见 `unadjusted_silence_active`（兜底窗口）。

    历史依据（2026-09-21 设 15 分钟时）：量比 = 当日累计量 ÷（近 5 日均每分钟量 ×
    已交易分钟数），开盘头几分钟的分母只有几分钟，商**虚高一个量级**；而量比是
    本段 `sort_key` 的第一排序键。实测 09:33 捕获的 3 只 T1 虚高 11.5/8.6/6.6 倍
    且当日全数转跌。

    ⚠ 2026-11-05 的实测反过来证明：**等 15 分钟并没有把失真等掉** —— 09:45 的读数
    仍虚高 3.5 倍，量比门在 09:54 的通过率是 94.6%（等于没筛）。故本函数从
    「等失真消失」改成「等读数可被修正」，窗口随之缩到 5 分钟。

    用 `trading_minutes_elapsed` 而**不是**墙上时间差：它已把午休 120 分钟排除在外
    （11:30 与 13:00 都返回 120），故「开盘满 N 分钟」在任何时刻都只指「上午连续竞价
    满 N 分钟」，下午开盘不会被误判为仍在窗口内。非交易日与开盘前返回 0 → 同样静默
    （那两种时段本就不该产出）。

    `OFFBOARD_OPENING_SILENCE_MIN <= 0` = 关闭窗口（离线自检 / 回放路径）。
    """
    if OFFBOARD_OPENING_SILENCE_MIN <= 0:
        return False
    return trading_minutes_elapsed(now) < OFFBOARD_OPENING_SILENCE_MIN


def unadjusted_silence_active(now: datetime | None = None) -> bool:
    """**去偏系数不可得**时的兜底静默窗（True = 本轮不产出）。

    何时走这条路：本地库没有足够的 `market_extra_cache` 历史（新库 / 数据不足）
    ⇒ k(m) 算不出来 ⇒ 量比读数仍是虚高的 ⇒ 此时放行 5 分钟窗口 = 放出一堆
    「量比 7~10」的行。故退回 2026-09-21 的 15 分钟口径（宁可晚，不可假）。

    `OFFBOARD_OPENING_SILENCE_MIN_UNADJUSTED <= 0` = 关闭兜底（离线自检 / 回放路径）。
    """
    if OFFBOARD_OPENING_SILENCE_MIN_UNADJUSTED <= 0:
        return False
    return trading_minutes_elapsed(now) < OFFBOARD_OPENING_SILENCE_MIN_UNADJUSTED


def run_offboard_watch(
    adapter,
    conn,
    board_items: Sequence[dict],
    *,
    exclude_symbols: set[str] | None = None,
    top_n: int = OFFBOARD_DISPLAY_TOP,
    klines: dict[str, list] | None = None,
    snapshot: dict[str, dict] | None = None,
    bias_conn: sqlite3.Connection | None = None,
    bias: Any = _BIAS_UNSET,
    bias_out: list | None = None,
) -> list[OffboardCandidate]:
    """跑一轮 B 段筛选，返回待展示的前 `top_n` 行。

    依赖注入 `board_items`：复用主循环**已抓取**的飙升榜，只为确定「谁在榜内」
    （B 段的候选来自快照，不来自榜单）—— 本段因此**不因榜单熔断而空**。

    `klines` 显式传入时跳过 K 线池（离线自检路径，不联网）；
    `snapshot` 显式传入时跳过 DB 读取（CLI 用生产快照 + 内存库的组合，见 `main`），
    **并跳过实时资金流补拉**（见下方 M1 修复段）—— 测试/离线路径保持零联网。

    `bias_conn`（2026-11-05）：**量比去偏系数 k(m) 的参考量来源**（读
    `market_extra_cache` 的历史收盘量）。默认用 `conn`；CLI 路径的 `conn` 是内存库
    （无历史）⇒ 那里必须显式传生产库的连接，否则去偏恒判「不可判定」⇒ 退回 15
    分钟兜底窗口（CLI 在开盘后 15 分钟内看不到东西）。

    异常处理见模块 docstring 的 fail-open / fail-closed 一节；编程错误不吞。
    """
    if conn is None:
        return []

    # 第一道门：开盘静默窗口。**放在读任何数据之前** —— 否则要把 ~2300 行全集快照
    # 读进来、跑完 5000 次门判定，再整批丢弃。窗口语义与实测见 opening_silence_active。
    if opening_silence_active():
        return []

    # snapshot 注入（测试/离线自检/CLI）⇒ 不联网补拉实时资金流（离线纪律）；
    # 生产主循环不注入（None → DB 读取）⇒ 走下方 M1 实时覆盖。
    snapshot_from_db = snapshot is None
    if snapshot is None:
        snapshot = get_market_extra_snapshot(conn, "fund_flow")
    if not snapshot:
        return []

    # 名称：快照自 2026-09-18 起带 f14；此前落库的行没有，回落到 appearances
    # （榜外 ≠ 从未上过榜）。名称不只用于展示 —— 通用门的 ST 判定依赖它。
    gem_offboard = [s for s, p in snapshot.items() if p and code_of(s).startswith(("300", "301"))]
    names = get_symbol_names(conn, gem_offboard)

    # 🔴 实时资金流覆盖（2026-10-08 修复 M1 / 2026-10-09 扩展量比）：main_pct 是
    # T1/T2 信号门的输入，vol_ratio 是 `sort_key` 段内主键 + 粗筛头部选择的排序键，
    # 两者雪球批量行情都不提供、此前恒取快照 —— 快照冻结时门在陈旧值上判定、高量比
    # 票被挤出补价头部。数据源与主循环同源同缓存（fetch_fund_flow_rank，TTL 300s，
    # 响应行自带 f10 量比 ⇒ 同口径换新鲜值），稳态零额外请求；仅 DB 快照路径（生产
    # 主循环）触发，snapshot 注入路径不联网（见 docstring）。
    # ⚠ 必须在**粗筛之前**：粗筛按量比降序取头部，覆盖放在粗筛后等于排序仍用旧值。
    if snapshot_from_db:
        try:
            ff_map = fetch_fund_flow_rank()
        except EXTERNAL_FAILURES as e:  # fetch 内部已 fail-soft，此处双保险
            logger.warning("B段实时资金流补拉失败（退化为快照 main_pct/vol_ratio）: %s", e)
            ff_map = {}
        _apply_live_fund_flow(snapshot, ff_map)

    # 🔴 量比去偏（2026-11-05）：把**上报量比**换算成「按当前进度折算的全天口径」。
    # 上报量比 = 当日累计量 ÷（近 5 日均每分钟量 × 已交易分钟数），开盘头几分钟的
    # 分母只有几分钟 ⇒ 全市场共同的读数虚高（实测 k(m)：09:31 13.6x / 09:33 7.3x /
    # 09:45 3.5x / 09:54 3.2x）。k(m) 由本地库逐轮实测（`scanner/volume_bias.py`，
    # 零新增网络）⇒ 扣掉它，量比就回到「与收盘同一量级」，门槛 `MOMENTUM_LAUNCH_VOL`
    # 的语义一字不改。
    # ⚠ 必须在**实时资金流覆盖之后**：覆盖刷进来的是新鲜的上报原值，去偏要在它上面做。
    # ⚠ 必须在**粗筛之前**：粗筛按量比降序取头部（去偏是全市场同一个标量，不改相对
    # 序，但口径要一致，免得日志/展示/门三处读到不同的数）。
    # 只有调用方**显式**给了 k 才走「复用」路径；未传时保持旧签名调用
    # （`biased_snapshot(conn, snapshot)`）—— 这既让 monkeypatch 的旧桩继续可用，
    # 也保证 CLI / 单测路径的行为逐字节不变。
    bias = (
        biased_snapshot(bias_conn or conn, snapshot)
        if bias is _BIAS_UNSET
        else biased_snapshot(bias_conn or conn, snapshot, bias=bias)
    )
    # 把本轮实际用的 k 交回调用方（主循环据此让榜内异动段复用**同一个**尺度）。
    # 用出参而不是返回值：本函数的返回值是「待展示的行」，加一个元组会打断所有调用方。
    if bias_out is not None:
        bias_out.append(bias)
    # 不可判定（本地历史不足 / 样本太少）⇒ 读数仍虚高 ⇒ 退回旧口径的 15 分钟窗口。
    if bias is None and unadjusted_silence_active():
        return []

    # 🔴 实时价覆盖（2026-10-08）：**在过门之前**就地覆盖快照 —— 判定与展示共用同一份
    # 数据，故口径自洽（判定即所见）。原因与实测见 `_apply_live_quote` docstring：
    # 快照在拉取超时时只刷新当轮候选，其余行会永久冻结（300890 实测滞后 2h11m）。
    #
    # ⚠ 取数范围**不能**是全量 gem_offboard（实测 1411 只）：`batch/quote.json` 是
    # 50 票/批，全量需 ~29 请求/轮，相对 60s 刷新周期不可接受。而 TOP N 是判定的
    # **产物**（`sort_key` 依赖分层与量比），也不能反过来当取数范围。故先用**零成本的
    # 快照字段**粗筛出「宽松过门」的候选头部再补实时价：
    #   · 粗筛只保留「明显不可能被门拒掉」的票（成交额/流通市值/ST/涨幅带），
    #     判**不完整**是故意的 —— 粗筛误纳的票补了实时价也仍会被精确门拒掉（只是白花
    #     一个请求位），粗筛误删才会丢票，故各项一律取**比真门更宽**的阈值。
    #   · 上限 `OFFBOARD_LIVE_QUOTE_LIMIT` 保证请求数有界（默认 100 ⇒ 2 请求/轮）。
    #   · 涨幅带同样加缓冲（2026-10-08 修复 H1）：粗筛读的是**快照** percent，快照可
    #     滞后 2h+，一只票快照时在带外、实时已进带（恰恰是「价刚启动」的目标票）会
    #     拿不到实时价补全而被系统性漏掉 —— 与两个 RATIO 同一「宽于真门」原则。
    #     下限与 0.0 取 max：非上涨的票不可能是 T1/T2 候选，不值得花请求位。
    #
    # ⚠ 残留口径差（M1 + 量比扩展后仅剩一处）：粗筛读的 percent/amount/float_cap
    # 仍是**快照**（雪球批量行情 50 票/批，全量补价不可接受）—— 已按「宽于真门」
    # 原则加缓冲兜住（H1），过粗筛的头部随后拿到雪球实时价。门判定与排序的关键
    # 输入（price/percent/name/main_pct/vol_ratio）已全部新鲜：前四者来自雪球
    # 实时补价 + f14 名称，量比/main_pct 来自东财资金流行内快照（见
    # `_apply_live_fund_flow`）。
    # 榜内 / 今日已推荐的票**不进粗筛**（2026-11-03）：它们随后必被 `build_candidates`
    # 的 `code in board_codes or symbol in skip` 丢掉，却会先占掉实时补价的头部名额 ——
    # 实测 2026-10-29 / 10-30 的 100 个名额里分别有 **14 / 19** 个花在 `today_syms` 上。
    # 判据与 `build_candidates` 逐字同源（同一份 code_of），不新增语义。
    _board_codes = {code_of(str(it.get("symbol") or "")) for it in board_items if it.get("symbol")}
    _skip = exclude_symbols or set()
    _pct_low = max(0.0, HOT_MIN_PERCENT - OFFBOARD_PREFILTER_PCT_BUFFER)
    _pct_high = OFFBOARD_T2_TODAY_MAX + OFFBOARD_PREFILTER_PCT_BUFFER
    prefilter = [
        s
        for s, p in ((s, snapshot[s]) for s in gem_offboard if snapshot.get(s))
        if (
            s not in _skip
            and code_of(s) not in _board_codes
            and (to_float(p.get("amount"), 0.0) or 0.0) >= OFFBOARD_MIN_AMOUNT * OFFBOARD_PREFILTER_AMOUNT_RATIO
            and (to_float(p.get("float_cap"), 0.0) or 0.0)
            >= OFFBOARD_MIN_FLOAT_CAP * OFFBOARD_PREFILTER_FLOAT_CAP_RATIO
            and not is_st(str(p.get("name") or names.get(s) or ""))
            and _pct_low < (to_float(p.get("percent"), 0.0) or 0.0) <= _pct_high
        )
    ]
    # 快照量比降序取头部（与 `build_candidates` 的候选排序同键，保证粗筛头部的选择
    # 依据与真门的候选优先级一致 —— 否则会截掉真能过门的高量比票）。量比已被上方
    # 实时资金流覆盖刷成新鲜值（f10 同口径），故此排序消费的是实时量比。
    prefilter.sort(key=lambda s: -(to_float(snapshot[s].get("vol_ratio"), 0.0) or 0.0))
    live_ok = _refresh_with_live_quotes(adapter, snapshot, prefilter[:OFFBOARD_LIVE_QUOTE_LIMIT])
    if live_ok:
        # 覆盖后名称可能更新（`_apply_live_quote` 会刷 name），合并进names ——
        # 通用门的 ST 判定与展示都读它。
        names = {**names, **{s: snapshot[s]["name"] for s in live_ok if snapshot[s].get("name")}}

    cands, gate_rejects = build_candidates(snapshot, board_items, names, exclude_symbols, conn=conn)
    today = now_beijing().date().isoformat()

    # 留存（dwell，2026-10-09）：量比/主占改吃实时值后，阈值附近横跳的票会反复进出。
    # 近 OFFBOARD_DWELL_MIN 分钟内严格过门的票，本轮只挂活跃度阈值时以「留存」行
    # 续展 —— 活跃度门放宽，风险门与分层条件照严；留存行不写 launch_log（不刷新
    # last_hit_time ⇒ 窗口自最后一次严格过门起算，不自我续命）。
    recent = _recent_dwell_symbols(conn, today)
    retained: list[OffboardCandidate] = []
    if recent:
        retained = _retain_candidates(
            snapshot, recent, {c.symbol for c in cands}, _board_codes, _skip, names, conn=conn
        )

    # 次日收益回填（2026-10-08 修复 L5）：移到「本轮是否产出」的所有提前返回**之前**
    # —— 原先 `not cands` / 零候选轮会跳过回填，长期零产出的交易日标签停积，与
    # 「影子期样本必须有标签」的目标相悖。无待回填行时只是一条索引查询，代价可忽略。
    try:
        backfill_next_day(conn, adapter)
    except EXTERNAL_FAILURES as e:
        logger.warning("B段次日收益回填失败（不影响本轮）: %s", e)

    # 拒绝留痕（2026-09-22 / 迁移 m017）：gate 段的 rejects 原先被 `_rejects` 丢弃、
    # 分层段的理由只写进 `c.reasons` 就地消失 ⇒ 只剩幸存者样本，任何阈值调整无从证伪。
    # 两段互斥（过门才可能被分层拒），同一 symbol 不会重复入表。
    if not cands and not retained:
        _save_rejections(conn, _rejection_rows(gate_rejects, (), snapshot, names), today)
        # 过门池空也是一轮跑过门的扫描：记入轮数账（历史口径：today_presence 分母
        # 完整性；三版起在场率退场，本账保留为研究原料）。
        persist_round(conn, [])
        return []

    quota_limited = klines is None
    attempted: set[str] = set()
    if klines is None:
        # 🔴 额度只管**补取请求**，不管**读缓存**（2026-11-03）。此前额度被施加在入参上
        # （`load_offboard_klines(conn, adapter, cands[:LIMIT])`）⇒ 额度外的票即使当日
        # 缓存里有 K 线也拿不到，被记成「无K线数据(榜外池未覆盖)」。实测 2026-10-23：
        # 366 只被判「池未覆盖」，其中 **126 只当日已缓存**；而 `offboard_rejections`
        # 是**最新写入覆盖**，于是一只票掉出额度后连原来真实的理由
        # （如 `MA空头排列`）也会被改写成「数据没覆盖」—— 那张表是本段唯一的证据链。
        all_syms = list(dict.fromkeys([c.symbol for c in cands] + [c.symbol for c in retained]))
        # 补取额度优先给严格候选；留存票追加在额度之后（数量有 DWELL_MAX 封顶）。
        attempted = {c.symbol for c in cands[:OFFBOARD_KLINE_FETCH_LIMIT]} | {c.symbol for c in retained}
        klines = load_offboard_klines(conn, adapter, all_syms, fetch_symbols=attempted)

    passed: list[OffboardCandidate] = []
    tier_rejects: list[tuple[str, str]] = []
    for c in cands:
        if annotate(c, klines.get(c.symbol), today) is None:
            why = c.reasons[0] if c.reasons else "不产出"
            # 区分「额度外没补取」与「补取了但没有数据」（2026-11-03）：两者对展示的
            # 后果相同（不产出），对**事后归因**完全不同 —— 前者是资源上限，后者是数据
            # 缺失。混成一句会让「按理由重放阈值」的人误判 K 线覆盖面。
            if quota_limited and c.symbol not in klines and c.symbol not in attempted:
                why = "K线未补取(额度外)"
            tier_rejects.append((c.symbol, why))
            continue
        passed.append(c)

    _save_rejections(conn, _rejection_rows(gate_rejects, tier_rejects, snapshot, names), today)

    # 留存行过**同一套**分层条件（annotate 不放宽：涨幅带 / 5日累计 / MA / 顶背离 /
    # 主占照严 —— 结构性条件与风险不因留存松动），过了的带「留存」标记追加在尾部。
    kept_retained: list[OffboardCandidate] = []
    for c in retained:
        if annotate(c, klines.get(c.symbol), today) is None:
            continue
        c.reasons = [*(c.reasons or []), f"留存:近{OFFBOARD_DWELL_MIN}分钟曾过门,活跃度已回落"]
        kept_retained.append(c)
    kept_retained.sort(key=sort_key)

    passed.sort(key=sort_key)
    # 🔴 只落严格行 —— 留存行若也刷新 last_hit_time，留存窗口会自我续命（永远不过期）。
    persist_round(conn, passed, top_n)
    # （回填已前移至 build_candidates 之后：零候选轮也执行，见上方 L5 注释。）

    # 留存行不占 top_n 名额：严格信号永远优先，续展的「已回落」票只作尾部上下文。
    top = passed[:top_n] + kept_retained
    # （历史注记：2026-10-14~15 曾有「反复进前十」repeat_days 注记回填，供终端
    #   粉名 / 飞书 🔥 判定；2026-10-16 按用户决策整体删除，见 git 历史。)
    # 板块列（2026-09-22）：只对最终展示行取值 —— ②级 F10 补拉的量因此被压到
    # ≤ OFFBOARD_DISPLAY_TOP 只，且缓存命中时零请求（见 OFFBOARD_BOARD_FETCH 注释）。
    _attach_boards(top, conn)

    # 终端打印快照（2026-11-03，m022）：落**这一轮实际要渲染的那几行**（含留存行）。
    # 与 `persist_round`（整日累加的过门账）刻意分开：AI 分析要的是「屏幕上最后显示
    # 的是哪几只」，而不是「当日过门过谁」。`print_seq` 用当前时刻的秒数做单调序，
    # 读侧 `ORDER BY print_seq DESC` 即得最后一轮。
    try:
        _print_seq = int(now_beijing().strftime("%H%M%S"))
        save_offboard_print_snapshot(conn, top, today=today, print_seq=_print_seq)
    except EXTERNAL_FAILURES as e:
        # 观测旁路，失败只告警（与 _save_rejections 同纪律）。
        logger.warning("B段终端打印快照落库失败（不影响本轮产出）: %s", e)

    return top


def _rejection_rows(
    gate_rejects: Sequence[tuple[str, str]],
    tier_rejects: Sequence[tuple[str, str]],
    snapshot: dict[str, dict],
    names: dict[str, str],
) -> list[tuple]:
    """两段拒绝明细 → `offboard_rejections` 行。

    每条 10 个字段：
    `(symbol, name, reason, percent, vol_ratio, main_pct, gate, reason_code, reason_value, ...)`
    —— 末两项由 `dal.save_offboard_rejections` 补齐时间戳，见该函数 docstring。

    gate 段的候选对象在 `build_candidates` 内部就地丢弃（对外契约只有 (symbol, reason)），
    故 percent / vol_ratio / main_pct 回**快照**取 —— 这三列正是「事后重放阈值」的输入
    （2026-09-22 那次杠杆测量就得手工重建这一步，跑完即失）。快照缺行时留 None，
    不阻断留痕：留痕存在的意义就是**下次不必再手工重建**。

    `gate`（2026-11-12，m026）：区分「门槛段拒」与「分层段拒」—— 两者语义完全不同
    （前者是「不该进候选集」，后者是「进了候选集但信号不成立」），混在一起统计会
    把「信号条件太严」误读成「风险门太严」。

    `reason_code` / `reason_value`：结构化门 ID 与读数（见 `reason_code` docstring）。
    `reason` 串原样保留。

    `name` 优先用 `names`（`get_symbol_names` 的回退链），快照自 2026-09-18 起也带 f14。
    """
    rows: list[tuple] = []
    for gate, group in (("gate", gate_rejects), ("tier", tier_rejects)):
        for sym, reason in group:
            p = snapshot.get(sym) or {}
            code, value = reason_code(reason)
            rows.append(
                (
                    sym,
                    names.get(sym) or p.get("name") or "",
                    reason,
                    to_float(p.get("percent"), None),
                    to_float(p.get("vol_ratio"), None),
                    to_float(p.get("main_pct"), None),
                    gate,
                    code,
                    value,
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
        # 上报原值（量比去偏前）：`vol_ratio` 是去偏后的决策口径。
        "vol_ratio_raw": round(c.volume_ratio_raw, 2),
        "main_pct": round(c.main_pct, 2),
        "amount": round(c.amount),
        "float_cap_yi": round(c.float_market_capital / 1e8, 2),
        "price": round(c.current, 3),
        "beauty": c.beauty,
        "reasons": c.reasons,
    }


#: CLI 内存库的只读表 DDL（2026-11-12，从生产库 `scanner.db` 导出）。
#: ⚠ 手写会与真实 schema 漂移（曾把 `concept_cache.concepts` 写成 `concept`、
#: `offboard_print_snapshot.time` 漏掉）⇒ 改 schema 后需重新导出。
_CLI_READONLY_DDL: tuple[str, ...] = (
    "CREATE TABLE appearances ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, name TEXT NOT NULL, "
    " date TEXT NOT NULL, rank INTEGER, percent REAL, value REAL, next_day_pct REAL)",
    "CREATE TABLE recommendations ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL, time TEXT NOT NULL, "
    " symbol TEXT NOT NULL, name TEXT, category TEXT, score INTEGER, percent REAL, "
    " trend TEXT, next_day_pct REAL, fwd_3d REAL, fwd_5d REAL, score_breakdown TEXT, "
    " source TEXT, cum_2d REAL, cum_3d REAL, concept TEXT, accumulated_pct REAL, "
    " excluded INTEGER, sector_capped INTEGER, stale_kline INTEGER, excluded_reason TEXT, "
    " nd10_pct REAL)",
    "CREATE TABLE concept_cache ( symbol TEXT PRIMARY KEY, concepts TEXT NOT NULL, updated TEXT NOT NULL)",
    # ⚠ 主键必须带 `(date, print_seq, symbol)` —— 漏了它，`save_offboard_print_snapshot`
    # 的 ON CONFLICT 会报 "does not match any PRIMARY KEY or UNIQUE constraint"。
    "CREATE TABLE offboard_print_snapshot ("
    " date TEXT NOT NULL, time TEXT NOT NULL, print_seq INTEGER NOT NULL, symbol TEXT NOT NULL, "
    " name TEXT, tier TEXT, percent REAL, accum_5d REAL, vol_ratio REAL, main_pct REAL, "
    " amount REAL, float_cap REAL, price REAL, sector TEXT, turnover_rate REAL, "
    " ff_pct REAL, beauty TEXT, reasons TEXT, rank_in_print INTEGER NOT NULL DEFAULT 0, "
    " updated_at TEXT NOT NULL, PRIMARY KEY (date, print_seq, symbol))",
    "CREATE TABLE market_extra_cache ("
    " symbol TEXT NOT NULL, date TEXT NOT NULL, data_type TEXT NOT NULL, "
    " payload_json TEXT NOT NULL, updated TEXT NOT NULL, "
    " PRIMARY KEY(symbol, data_type, date))",
)


def _cli_conn():
    """CLI 专用内存库：DDL 与生产迁移同一份（不复制 SQL），但不碰 scanner.db。"""
    import sqlite3

    from scanner.db.migrations import (
        _OFFBOARD_KLINE_DDL,
        _OFFBOARD_LAUNCH_LOG_DDL,
        _OFFBOARD_REJECTIONS_DDL,
        _OFFBOARD_ROUND_LOG_DDL,
    )

    conn = sqlite3.connect(":memory:")
    conn.execute(_OFFBOARD_KLINE_DDL)
    conn.execute(_OFFBOARD_LAUNCH_LOG_DDL)
    # ⚠ 2026-10-16 修复：`persist_round` 还写 offboard_round_log、`_save_rejections`
    # 还写 offboard_rejections。缺这两张表时 "no such table" 被 EXTERNAL_FAILURES
    # 吞掉，连同一块里**已执行**的 launch_log 插入一起永不提交（CLI 路径零落库）。
    conn.execute(_OFFBOARD_ROUND_LOG_DDL)
    conn.execute(_OFFBOARD_REJECTIONS_DDL)
    # ⚠ 2026-11-12：补齐 CLI 路径会**读**的表 —— 缺它们时每次调用都刷一条
    # "no such table" 警告（fail-open 吞掉，功能不受影响，但噪音掩盖真告警）。
    # 均为**只读回退链**或旁路表，空表 = 「查不到」= 设计中的降级路径：
    #   · appearances              → 名称回退（快照自带 f14，实际很少用到）
    #   · recommendations          → 今日已推荐票（CLI 不需要真实值）
    #   · concept_cache            → 板块列缓存（miss 时回落名称关键词）
    #   · offboard_print_snapshot  → 终端打印快照（CLI 只看 stdout，不读它）
    #   · market_extra_cache       → 流门历史查询（CLI 传内存库 ⇒ 查不到 ⇒
    #                                「不可判定」fail-open 放行；生产路径用的是真库）
    #
    # DDL **从生产库导出**而非手写 —— 手写过一次，列名与真实 schema 不符
    # （concept_cache 实际是 `concepts` 不是 `concept`，offboard_print_snapshot
    # 实际有 `time` 列），结果照样刷警告。schema 变了这里要重新导出。
    for _ddl in _CLI_READONLY_DDL:
        conn.execute(_ddl)
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
    p.add_argument(
        "--repair-next-day",
        action="store_true",
        help="重算**已冻结**的 next_day_pct（2026-11-03 前写入的是「收盘→次日盘中」口径）；默认 dry-run",
    )
    p.add_argument("--apply", action="store_true", help="配合 --repair-next-day：真正写库（不带则只打差异账）")
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

    if args.repair_next_day:
        # 修数作业：**零联网**（只读 offboard_kline_cache），故提前返回、**不**构造 adapter。
        conn = get_conn()
        try:
            report = repair_next_day_labels(conn, dry_run=not args.apply)
        finally:
            conn.close()
        changed = report["changed"]
        print("【B 段 next_day_pct 重算】口径 = 信号日收盘 → 次一交易日**收盘**（三条防呆同源）")
        print(f"  待查 {report['checked']} 行 | 已一致 {report['same']} 行 | 需修 {len(changed)} 行")
        if report["skipped"]:
            bits = " / ".join(f"{k} {v}" for k, v in sorted(report["skipped"].items()))
            print(f"  跳过：{bits}")
        if changed:
            if report["old_mean"] is not None:
                print(f"  均值：旧 {report['old_mean']:+.2f}% → 新 {report['new_mean']:+.2f}%")
            else:
                print(f"  均值：新 {report['new_mean']:+.2f}%")
            for d, sym, old, new in sorted(changed, key=lambda r: -abs(r[3] - r[2]))[:5]:
                print(f"    {d} {sym}  {old:+.2f} → {new:+.2f}")
            print("  （已落库）" if args.apply else "  dry-run：加 --apply 才写库")
        return 0

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
            " —— 开盘头几分钟的量比失真最大（可测可扣，但此刻不值得先信它），本轮不产出"
        )
        return 0

    board = adapter.fetch_biaosheng(100)
    if not board:
        print("  [!] 飙升榜为空（熔断中/网络异常）——本段候选不依赖榜单，继续尝试快照")
    prod = get_conn()
    snapshot = _snap(prod, "fund_flow")
    if not snapshot:
        print("  [!] 资金流快照为空（主循环未落库？），无法运行 B 段")
        return 1
    print(f"  飙升榜 {len(board)} 条 / 资金流快照 {len(snapshot)} 只 | 数据源 {adapter.name}")

    # ⚠ `bias_conn=prod`：量比去偏系数 k(m) 要读**生产库**的历史收盘量（CLI 的
    # `conn` 是内存库、无历史）⇒ 不传则去偏恒判「不可判定」⇒ 退回 15 分钟兜底窗口。
    try:
        rows = run_offboard_watch(adapter, _cli_conn(), board, top_n=args.top, snapshot=snapshot, bias_conn=prod)
    finally:
        prod.close()
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
