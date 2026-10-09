# scanner/config_hot_watch.py — 沪深飙升榜「极有可能大涨」独立区配置（原 config.py 的一部分）
# 2026-09-13 从 config.py 拆出。定义集中在此，由 scanner/config.py 统一 re-export，
# 保持 `from scanner.config import ...` 导入路径不变。

import os

from scanner.config_categories import YI  # noqa: F401 (市值上限复用单一 YI 单位)
from scanner.config_scoring import (  # noqa: F401 (B 段两层涨幅带派生自启动口径单源)
    MOMENTUM_LAUNCH_TODAY_MAX,
    MOMENTUM_LAUNCH_TODAY_MIN,
)
from scanner.config_sources import FUND_OUTFLOW_NET_PCT  # noqa: F401 (资金流出阈值单源)

HOT_WATCH_ENABLED = os.environ.get("RTS_HOT_WATCH", "1") != "0"

# 硬性排除阈值
HOT_MAX_MARKET_CAP = 300 * YI  # 总市值上限：300 亿（超过则大盘股弹性不足）
HOT_MAX_PERCENT = 7.0  # 涨幅上限(%)：超过即排除，避免追高
HOT_MIN_PERCENT = 0.0  # 涨幅下限：≤0 即排除（必须是上涨状态）
HOT_LIMIT_DOWN_TOLERANCE = 1.005  # 现价 ≤ 跌停价×该系数 视为跌停
HOT_LIMIT_UP_NEAR = 0.985  # 现价 ≥ 涨停价×该系数 视为已封涨停

# A 股涨跌停幅度（%）：本区样本面已剔除科创板，故只有 10% 与 20% 两档。
# batch 行情接口不返回 limit_up/limit_down（实测为 None），改由 last_close 推算。
HOT_LIMIT_PCT_MAIN = 10.0  # 主板（600/601/603/605、000/001/002/003）
HOT_LIMIT_PCT_GEM = 20.0  # 创业板（300/301）

# 打分权重（合计 100）
HOT_W_RANK_CHANGE = 35.0  # 榜单排名上升幅度（对数归一，压缩极值）
HOT_W_PERCENT = 25.0  # 涨幅（越接近上限动能越强）
HOT_W_PRICE = 15.0  # 价格（低价弹性好）
HOT_W_VOLUME = 25.0  # 量能（量比 + 换手率）

# 打分辅助参数
HOT_RANK_CHANGE_CAP = 9000.0  # rank_change 对数归一化上限
HOT_PRICE_IDEAL_LOW = 3.0  # 理想价格区间下限（元）
HOT_PRICE_IDEAL_HIGH = 40.0  # 理想价格区间上限（元）
HOT_PRICE_DECAY_TO = 300.0  # 价格高于理想上限时，到该价衰减至 0
HOT_VR_FULL = 3.0  # 量比 ≥ 此值满分
HOT_TR_FULL = 10.0  # 换手率 ≥ 此值满分
HOT_VR_WEIGHT = 0.6  # 量比在量能分中的占比（两者齐全时）
HOT_VOLUME_SINGLE_FACTOR = 0.85  # 仅量比或仅换手时的折扣系数
HOT_VOLUME_NO_DATA = 0.15  # 量比/换手全缺失时的中性低分

# 连击（跨轮连续命中）跟踪
HOT_HIGHLIGHT_STREAK = 3  # 连续出现 ≥ 该轮数 → 终端标记「★重点关注」
HOT_STREAK_RESET_DAYS = 7  # 超过该天数未再命中的记录清理（防表无限增长）

# 美感门（2026-09-14）：日线走势「漂亮」判定，过滤掉走势不佳的候选
# 判定函数与终选参考区曾有过的美感门同源（scanner.trend_beauty.evaluate_daily_trend）；
# 后者已于 2026-09-21 随终选参考区删除，本门保留且独立开关控制
# 1=开启（默认，过滤掉日线不漂亮的候选），0=关闭
HOT_BEAUTY_GATE_ENABLED = int(os.environ.get("RTS_HOT_BEAUTY_GATE", "1"))

# 资金流过滤（2026-09-14）：主力净流出占比 ≤ 阈值 → 过滤
# 1=开启（默认，过滤资金流出的候选），0=关闭
HOT_FUND_FLOW_FILTER_ENABLED = int(os.environ.get("RTS_HOT_FUND_FLOW_FILTER", "1"))
# 资金流过滤阈值（主力净占比 %）：**派生**自单源 FUND_OUTFLOW_NET_PCT（-8.0%），
# 不写字面量——2026-09-14 前此处手抄 -8.0，改一处漏一处的风险由派生消除。
HOT_FUND_FLOW_FILTER_THRESHOLD = FUND_OUTFLOW_NET_PCT

# 单轮工作量上限（保护主循环刷新节拍：主线 60s 一轮，本区不得显著拖长）
HOT_ENRICH_LIMIT = 60  # 每轮最多补全行情的候选数（预筛后按 rank_change 取前 N）
HOT_BATCH_SIZE = 50  # 批量行情单批 symbol 数（雪球 batch/quote 上限附近）
HOT_DETAIL_TOP = 5  # 仅对最终前 N 名补拉 detail（拿量比/涨跌停价），0=关闭
HOT_DISPLAY_TOP = 5  # 终端独立区展示行数

# ── B 段「榜外异动」（2026-09-18）：本区扩容，候选来自全市场快照的**榜外创业板** ──
# 动机：飙升榜天然滞后（好票等上榜已涨一截）。榜外票没有榜单排名，但有全市场快照
# 里的价/涨幅/量比/换手/成交额/流通市值 + 主力净占比 ⇒ 可做「量先动·价未动」的提前观察。
# 三条边界（与 A 段一致，见 hot_watch 模块 docstring）：不进 recommendations、不参与
# 复合评分/档位/画像、不给操作建议 —— 本段定位是**信号观察段**，且**尚无历史背书**
# （阈值的常量是在**榜上**样本校准的，域迁移到榜外不保证成立）。
OFFBOARD_WATCH_ENABLED = os.environ.get("RTS_HOT_OFFBOARD", "1") != "0"
# 榜外专属新增门（方向 = **收紧**，符合 display_gates 的「区域可收紧不可放宽」契约）。
# 依据（2026-09-18 实盘快照）：榜外主体是「冷门低换手小微盘」（换手 p50 仅 1.0%、
# 流通市值 p50 仅 25 亿），成交额过低的票少量资金即可操纵分时、指标本身无参考价值。
OFFBOARD_MIN_AMOUNT = 3000 * 1e4  # 成交额下限（元）：3000 万
OFFBOARD_MIN_FLOAT_CAP = 10 * YI  # 流通市值下限（元）：10 亿

# ── 开盘静默窗口（2026-09-21）：本段延后到开盘满 N 分钟才产出 ──
# 🔴 这不是调阈值，是修一个**测量缺陷**。量比 = 当日累计量 ÷（近 5 日均每分钟量 ×
# 已交易分钟数）：开盘头几分钟的集合竞价量被一个只有几分钟的分母摊薄，读数**虚高
# 一个量级**；而量比正是 B 段 sort_key 的**第一排序键** ⇒ 不设窗口等于「越早出现
# 越靠前」，把失真最严重的行推到第一屏。
# 实测（`offboard_launch_log` 19 行 / 2 个交易日，对照 `market_extra_cache` 收盘快照）：
#   09:33 捕获的 3 只 T1 量比虚高 **11.5 / 8.6 / 6.6 倍**，当日唯三由涨转跌的恰好就是
#   这三只；09:45 之后捕获的行，虚高倍数收敛到 ≈1.0。
# 设计稿 §6 自己点名过这个风险（「建议在 09:35–09:45 窗口复核」）—— 那次复核没做，
# 文档里的两源量比一致性实测是在 11:18 做的，正好避开了失真窗口。既然失真是量纲性质
# （分母趋 0 时商发散），此处按数学性质直接设窗，不等统计证据（19 行样本也支撑不了）。
# 窗口用 `trading_session.trading_minutes_elapsed` 判定（午休 120 分钟已排除），
# 故「开盘满 15 分钟」在任何时刻都只指「上午连续竞价满 15 分钟」。
# 0 = 关闭窗口（离线自检 / 回放用）。
OFFBOARD_OPENING_SILENCE_MIN = 15

# 主力净占比下限（0 = 至少不是净流出）。**T1/T2 同门**（2026-09-22 起 T2 也设此条）。
# 旧注释写的是「T2 不设该条 —— 复用 MOMENTUM_LAUNCH_* 口径，加进来会变成同名不同义
# 的第三种启动定义」，其隐含前提是「T2 几乎不产出」（上线 3 日 31 行里 T2 仅 9 行）。
# 当日 T2 的 MA 门由「完全多头」放宽到「非空头」后该前提失效（同日同快照实测
# T2 0 → 12）—— 不补这道门，榜首就是主力净流出的票：实测前二为 **-6.61% / -3.97%**，
# 两者都高于 `common_hard_gate` 的 -8% 阈值（`FUND_OUTFLOW_NET_PCT`），通用门拦不住。
# 与 A 段「资金流出统一口径、展示层单点过滤」(02ae8af) 及 T1 口径保持一致。
OFFBOARD_MAIN_PCT_MIN = 0.0
OFFBOARD_DISPLAY_TOP = 5  # 终端同区 B 段展示行数（与 A 段体量一致）

# ── B 段 5 日累计上界（**B 段私有**，2026-09-22）──
# 下沿仍取 `MOMENTUM_LAUNCH_ACCUM_MIN`(0)：与 momentum 同源，两边一致（近 5 天不能是
# 跌的），不重复定义。
# 上沿**不再共用** `MOMENTUM_LAUNCH_ACCUM_MAX`(7) —— 那个常量定义在 `config_scoring.py`
# 并被主线 `analysis.py:593/:643` 的 momentum 桶消费，改它等于动**主线推荐池**。
# 这里另立常量，故「B 段的启动」与「主线 momentum 的启动」自此上沿不同名不同值，
# 属显式分叉而非同名不同义（名字不同、此处注明即可）。
#
# 放宽依据（2026-09-22 同快照实测：创业板 1410 → 过门 401 → 有 K 线 143）：
#   ·「5日累计≥7」杀 63/143，是**第二大门**；被「涨幅>7%」挡掉的 18 只里 7 只死在此门；
#   · 上沿 7 → 15：产出 11 → 28（T2 0 → 3）；
#   · 与 T2 的 MA 放宽合用：11 → **37**（T2 0 → **12**），涨幅上限 3.15% → **6.93%**、
#     量比上限 3.19 → **5.40**；前 10 由全 T1 变全 T2。
#   · 单独放开涨幅带则**零效果**（7→8 与完全放开产出均为 11）—— 涨幅带不是瓶颈。
#
# ⚠ 无样本外证据：`rule_validate` 看不见 `scanner.offboard_watch`。2026-09-22 实跑两次：
#   ① `--set scanner.config_hot_watch.OFFBOARD_ACCUM_MAX=15.0` → **退出码 3**（可见性
#      硬校验：该模块不在 nextday-prob / rescore 任一可见集合内，stored-score 可见集为空）；
#   ② 绕道 `--set scanner.config.OFFBOARD_ACCUM_MAX=15.0`（`scanner.config` 在 rescore
#      可见集内，校验放行）→ **退出码 1**，但 Δ=0.0pp、**翻转 0/50 个交易日**、MDE=n/a，
#      且工具自打提示「改动未产生任何输出差异」—— 传播只改写了 1 个模块
#      （`scanner.config_hot_watch`），压根没摸到 `offboard_watch`。
#   ⇒ ② 的「证据不足」是**结构上的空转**，不是「测了没效果」，绝不能当作通过/不通过的依据。
#   本区唯一可积累的证据链是 `offboard_rejections` 留痕表，目标 D ≥ 47 交易日
#   （按日 bootstrap 检出 10pp 需 47 日；D=3 时 MDE ±39.6pp）后再复盘。
OFFBOARD_ACCUM_MAX = 15.0

# B 段两层涨幅带（**派生**，不写字面量）：分界点就是既有启动定义的下沿 ——
# 低于 MOMENTUM_LAUNCH_TODAY_MIN(3.5%) = 「价还没动」(T1)，达到它 = 「已启动」(T2)。
# 整条带的上界取 `min(MOMENTUM_LAUNCH_TODAY_MAX, HOT_MAX_PERCENT)`：
# ⚠ 这是对设计稿（写的 8.0）的**有意收紧**。MOMENTUM_LAUNCH_TODAY_MAX(8.0) 是
# 「启动首日」这个口径的上界，而 HOT_MAX_PERCENT(7.0) 是**本区**的涨幅带 ——
# 同一张表里 A 段永不出现 >7% 的行、B 段却出现 +7.8%，是肉眼可见的区内外不一致；
# 区域「可收紧不可放宽」的契约下取严，代价是丢掉 [7,8] 这一小段。
OFFBOARD_T1_TODAY_MAX = MOMENTUM_LAUNCH_TODAY_MIN
OFFBOARD_T2_TODAY_MAX = min(MOMENTUM_LAUNCH_TODAY_MAX, HOT_MAX_PERCENT)
# 榜外 K 线池（独立于 daily_kline —— 后者的既定语义是「榜单衍生池」，塞入榜外票会
# 污染所有基于它的回测基准与归因）：5 日累计 / MA 结构 / 顶背离判定所需的日线。
OFFBOARD_KLINE_DAYS = 60  # 单票取多少根日线
OFFBOARD_KLINE_WORKERS = 8  # 补 K 线并发（实测 6→16 线程无收益，服务端受限）
OFFBOARD_KLINE_FETCH_LIMIT = 80  # 单轮最多补 K 线的候选数（按量比降序取前 N）

# ── B 段实时行情覆盖（2026-10-08）──
# 背景：`market_extra_cache` 的当日快照在全市场拉取超时时只刷新当轮候选（见
# `market_extra.collect_market_extra` 的 `_last_ff_partial` 分支），其余 5000 行
# 冻结；读侧 `get_market_extra_snapshot` 又不过 TTL ⇒ 未进过当轮候选的票会一直显示
# 旧价（实测 300890 滞后 2h11m）。修法是在**过门之前**用雪球批量行情补实时价。
#
# 为什么需要「粗筛 + 上限」而不是全量补：创业板 1411 只，`batch/quote.json` 是
# 50 票/批（`api.fetch_hot_quotes_batch`）⇒ 全量需 ~29 请求/轮，60s 刷新周期扛不住。
# 粗筛用**比真门更宽**的阈值先滤掉「明显过不了门」的票（误纳只是白花请求位，误删
# 才丢票），再按量比降序截断。两个 RATIO 必须 ≤ 1.0 —— >1 会把真能过门的票滤掉。
OFFBOARD_LIVE_QUOTE_LIMIT = 100  # 单轮最多补实时价的候选数（50 票/批 ⇒ 2 请求/轮）

# ── B 段留存（dwell，2026-10-09）──
# 背景：价/主占/量比改吃实时值后（2026-10-08 H1 / M1 / 量比扩展），在活跃度阈值
# （量比 1.5 / 成交额下限）附近横跳的票会**反复进出** B 段（2026-10-09 用户实测）。
# 修法是「短窗留存」：近 `OFFBOARD_DWELL_MIN` 分钟内**严格过门**（写入过
# `offboard_launch_log`）的票，本轮若只挂在活跃度阈值上，仍以「留存」行继续展示
# （见 `offboard_gate(retain=True)`），让观察者能看到「放量→回落」的完整过程而不是
# 一闪一闪。
#
# 纪律（三条，缺一不可）：
#   · **风险门与分层条件一律不放宽**（ST/资金流出/追高上限/市值/5日累计/MA/顶背离）；
#   · 留存行**不写** `offboard_launch_log` ⇒ 不刷新 `last_hit_time` —— 留存窗口自
#     「最后一次严格过门」起算、自然到期，**不会自我续命**；
#   · 留存行不占 `top_n` 名额（追加在严格行之后，单轮上限 `OFFBOARD_DWELL_MAX`）。
OFFBOARD_DWELL_MIN = 30  # 留存窗口（分钟）：最后一次严格过门后多久内仍可展示
OFFBOARD_DWELL_MAX = 5   # 单轮最多留存多少行（按量比降序取前 N，防表膨胀）
OFFBOARD_PREFILTER_AMOUNT_RATIO = 0.8  # 粗筛成交额阈值 = 真门 × 本系数
OFFBOARD_PREFILTER_FLOAT_CAP_RATIO = 0.8  # 粗筛流通市值阈值 = 真门 × 本系数
# 涨幅带粗筛缓冲（2026-10-08 修复 H1）：粗筛涨幅带 = 真带 ± 本缓冲（pp）。
# 真带是 `(HOT_MIN_PERCENT, OFFBOARD_T2_TODAY_MAX]`，而粗筛读的是**快照** percent ——
# 快照可滞后 2h+（见上方背景），一只票快照时在带外、实时已进带（恰恰是「价刚启动」
# 的目标票）会拿不到实时价补全而被系统性漏掉。误纳方向只是白花一个请求位，误删才
# 丢票，故与两个 RATIO 同一「宽于真门」原则给涨幅带加缓冲。下限再与 0.0 取 max：
# 非上涨的票不可能是 T1/T2 候选，不值得花请求位。
# ⚠ 残留：缓冲只覆盖 ~1-2h 的典型漂移，漂移超过缓冲的票仍会漏（彻底消除需全量补价，
#   ~29 请求/轮不可接受）。粗筛排序键 vol_ratio 同为快照值，陈旧量比会把「实时高量比」
#   票挤出 OFFBOARD_LIVE_QUOTE_LIMIT 头部 —— 同根残留，显式记录。
OFFBOARD_PREFILTER_PCT_BUFFER = 2.0  # 粗筛涨幅带 = 真带 ± 本缓冲（百分点）

# ── 「板块」列的 F10 概念补拉开关（2026-09-22）──
# 两段行新增「板块」列，取值回退链见 `concept.display_board_map`（与 v1 池选的
# `_entry_sector` 同一批数据源）。②级读 concept_cache —— 而那张表由主线的
# `compute_driving_concepts` 维护，其覆盖面是「主线候选 ∪ 榜内票」：
#   · A 段（榜内飙升）票**必然在榜上** ⇒ 缓存必中，无需补拉（故 A 段恒 fetch=False）；
#   · B 段（榜外异动）按定义不进主线候选 ⇒ 缓存恒 miss ⇒ 只读缓存会让本列
#     恒为「其他」，等于没做这一列。
# 故 B 段默认开补拉，但**只针对最终展示行**（≤ OFFBOARD_DISPLAY_TOP 只），且
# DB/进程缓存命中时零请求 —— 稳态下每轮无额外开销，只有每天首批候选换人才发请求。
# 1=开（默认）/ 0=关（纯离线：只读缓存，miss 回退名称关键词）。
OFFBOARD_BOARD_FETCH = int(os.environ.get("RTS_OFFBOARD_BOARD_FETCH", "1"))

# ── 榜内异动段（2026-09-30 新增，A 段与 B 段之间的中间档）──
# 填的空档：A 段要求「已涨 + 热度跃升」，B 段要求「没上榜」⇒ **「刚上榜、涨幅还小、
# 量已经动了」这一档两侧都没覆盖**。本段只做 T1（量先动·价未动）。
# ⚠ **不做 T2**：T2「启动首日」带的下沿是 MOMENTUM_LAUNCH_TODAY_MIN(3.5%)，语义是
# 「今天刚启动」；而榜内票按定义**已经启动过**（它就是为启动而上榜的），故 T2 在榜内
# 几乎不可能命中 —— 做了是死代码。故 `allowed_tiers=(T1,)`，T2 分支不参与。
# 口径一律**复用** B 段的门与分层单源（offboard_watch.offboard_gate / classify_tier），
# 本模块不新造任何一条阈值（AGENTS 铁律：同名不同义是本仓最忌讳的）。
# 唯一私有常量是「本段展示几行」与「K 线取多少根」——纯资源参数，非判定阈值。
ONBOARD_ANOMALY_ENABLED = os.environ.get("RTS_ONBOARD_ANOMALY", "1") != "0"
ONBOARD_DISPLAY_TOP = 5  # 终端同区本段展示行数（与 A/B 段体量一致）
# K 线来源是 `daily_kline`（榜内票本就在该表覆盖内 —— 它是「榜单衍生池」），
# 故本段**不需要**榜外那套补取与独立缓存；这里只声明回看根数。
ONBOARD_KLINE_BARS = 60

__all__ = [
    "HOT_WATCH_ENABLED",
    "HOT_MAX_MARKET_CAP",
    "HOT_MAX_PERCENT",
    "HOT_MIN_PERCENT",
    "HOT_LIMIT_DOWN_TOLERANCE",
    "HOT_LIMIT_UP_NEAR",
    "HOT_LIMIT_PCT_MAIN",
    "HOT_LIMIT_PCT_GEM",
    "HOT_W_RANK_CHANGE",
    "HOT_W_PERCENT",
    "HOT_W_PRICE",
    "HOT_W_VOLUME",
    "HOT_RANK_CHANGE_CAP",
    "HOT_PRICE_IDEAL_LOW",
    "HOT_PRICE_IDEAL_HIGH",
    "HOT_PRICE_DECAY_TO",
    "HOT_VR_FULL",
    "HOT_TR_FULL",
    "HOT_VR_WEIGHT",
    "HOT_VOLUME_SINGLE_FACTOR",
    "HOT_VOLUME_NO_DATA",
    "HOT_HIGHLIGHT_STREAK",
    "HOT_STREAK_RESET_DAYS",
    "HOT_ENRICH_LIMIT",
    "HOT_BATCH_SIZE",
    "HOT_DETAIL_TOP",
    "HOT_DISPLAY_TOP",
    "HOT_BEAUTY_GATE_ENABLED",
    "HOT_FUND_FLOW_FILTER_ENABLED",
    "HOT_FUND_FLOW_FILTER_THRESHOLD",
    "OFFBOARD_WATCH_ENABLED",
    "OFFBOARD_MIN_AMOUNT",
    "OFFBOARD_MIN_FLOAT_CAP",
    "OFFBOARD_OPENING_SILENCE_MIN",
    "OFFBOARD_MAIN_PCT_MIN",
    "OFFBOARD_ACCUM_MAX",
    "OFFBOARD_T1_TODAY_MAX",
    "OFFBOARD_T2_TODAY_MAX",
    "OFFBOARD_DISPLAY_TOP",
    "OFFBOARD_DWELL_MAX",
    "OFFBOARD_DWELL_MIN",
    "OFFBOARD_KLINE_DAYS",
    "OFFBOARD_KLINE_WORKERS",
    "OFFBOARD_KLINE_FETCH_LIMIT",
    "OFFBOARD_LIVE_QUOTE_LIMIT",
    "OFFBOARD_PREFILTER_AMOUNT_RATIO",
    "OFFBOARD_PREFILTER_FLOAT_CAP_RATIO",
    "OFFBOARD_PREFILTER_PCT_BUFFER",
    "OFFBOARD_BOARD_FETCH",
    "ONBOARD_ANOMALY_ENABLED",
    "ONBOARD_DISPLAY_TOP",
    "ONBOARD_KLINE_BARS",
]
