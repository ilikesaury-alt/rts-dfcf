# scanner/config_scoring.py — 评分/打分阈值与交叉验证权重（原 config.py 的一部分）
# 2026-09-13 从 config.py 拆出。定义集中在此，由 scanner/config.py 统一 re-export，
# 保持 `from scanner.config import ...` 导入路径不变。

# Vol-rank combo scoring thresholds
VOL_RANK_VOL_THRESHOLD = 1.5
VOL_RANK_STRONG_RC = 2000
VOL_RANK_MEDIUM_RC = 1000
VOL_RANK_WEAK_RC = 500
VOL_RANK_STRONG_PTS = 15
VOL_RANK_MEDIUM_PTS = 12
VOL_RANK_WEAK_PTS = 8

# Peak volume ratio thresholds (current / max volume in lookback window)
VOL_PEAK_LOOKBACK = 20
VOL_PEAK_MOMENTUM_WARN = 0.5  # volume < 50% of peak → momentum exhaustion
VOL_PEAK_NEW_FACE_MIN = 0.3  # volume < 30% of peak → insufficient reversal volume

# Peak volume ratio scoring (analysis.py hardcoded values migrated)
VOL_PEAK_NEW_FACE_PENALTY = -5  # new_face vol_peak < threshold → penalty
VOL_PEAK_MOMENTUM_PENALTY = -8  # momentum vol_peak < threshold → penalty

# Analysis.py thresholds — previously hardcoded in analysis.py, centralized for tuning
# Weak-form filter thresholds
WEAK_FORM_MIN_DOWN_DAYS = 3
WEAK_FORM_MAX_ACCUM = 5
WEAK_FORM_MIN_ACCUM = -5
WEAK_FORM_MAX_TODAY_PCT = 3
WEAK_FORM_CRASH_THRESHOLD = -10

# Gap-up thresholds
GAP_UP_STRONG = 2.0
GAP_UP_MEDIUM = 1.0
GAP_UP_WEAK = 0.5
GAP_UP_STRONG_PTS = 8
GAP_UP_MEDIUM_PTS = 5
GAP_UP_WEAK_PTS = 3

# Bottom confirmation thresholds
BOTTOM_MAX_LOSS = -3.0
BOTTOM_VOL_SURGE = 1.5
BOTTOM_NEAR_LOW_PCT = 0.08

# Crash detection thresholds
CRASH_THRESHOLD = -12.0
RECENT_2_RETURN_THRESHOLD = -3.0
NO_CRASH_SAFE_BONUS = 8  # 拆分自 no_crash：无 crash day 基础安全分
RECENT_2D_BONUS = 5  # 拆分自 no_crash：近2日不差附加分
MOMENTUM_VOL_HEALTHY_MIN = 0.7
MOMENTUM_VOL_HEALTHY_MAX = 2.0

# MA alignment scoring
# 2026-08-29：这三个常量此前在 features.py 里被复制成一份私有副本（注释写"避免循环
# 导入"，实测不成立——holidays/weights/indicators 均无任何 scanner 内部导入，
# features → config 无环）。现改回单一来源：features.py 从此处 import。
MA_BULL_3_TIER_SCORE = 6  # MA5 > MA10 > MA20（完全多头排列）
MA_BULL_2_TIER_SCORE = 3  # MA5 > MA10（部分多头）
MA_BEAR_SCORE = -3  # MA5 <= MA10（空头排列）

# List momentum scoring (consecutive surge list appearances + rank trajectory)
LIST_STREAK_BONUS_2 = 3
LIST_STREAK_BONUS_3 = 5
LIST_STREAK_BONUS_5 = 6
TOP40_THRESHOLD = 40
TOP40_BONUS = 3
TOP40_ADVANCE_PER_10 = 2
TOP20_EXTRA = 2

# ── momentum "首次启动" 子模式 ──
# 目标：今日 4-6%（下限3.5%）放量启动 + 累计涨幅尚低（0~7%）的票，
# 提前 1-2 天进 momentum 池，不必等涨到 6-10% 才上榜。
# 数据：momentum 4-6% 带 cum2d +6.56% / cum3d +9.46%, 胜率 49%；2-4% 全策略负收益为噪音。
MOMENTUM_LAUNCH_ACCUM_MIN = 0.0
MOMENTUM_LAUNCH_ACCUM_MAX = 7.0
MOMENTUM_LAUNCH_TODAY_MIN = 3.5
MOMENTUM_LAUNCH_TODAY_MAX = 8.0
MOMENTUM_LAUNCH_VOL = 1.5  # 放量启动门槛（压掉缩量假阳）
MOMENTUM_LAUNCH_WORD = "启动首日"

# 超短末周期（鱼尾段）超买防护：validator 单点判断阈值。
# 20日涨幅阈值复用 PULLBACK_20D_GAIN_EXTREME（超买判定常量，与 pullback 策略本身无关）。
# 惩罚已移除：超买时仅靠 validator passed 门禁否决 + enhancer 标记，不再做 score 压制。
# 2026-07-28 收紧：原 BOLL=1.0 / KDJ=105 在强势股主升浪中几乎必中（健康强趋势 J 常 90~115，
# 单日大涨即破上轨），导致"超买"标签沦为废话、短炒票全民告警。改为仅"极端超买"才触发：
#   - BOLL %B > 1.10：明显脱离上轨（而非刚触碰）
#   - KDJ J > 115：超过健康强趋势上限（90~115）
# 20日涨幅>60% 维持（genuinely 过热），三项仍任一即判（否决用，宁可漏放不可误杀主升浪）。
ST_OVERBOUGHT_BOLL = 1.10  # BOLL %B > 此值 = 明显破上轨（极端高位）
ST_OVERBOUGHT_KDJ = 115  # KDJ J > 此值 = 极端超买（健康强趋势 J 常 90~115，旧 105 误伤）

# ── rebound 交叉验证常量 ──
V_RB_OVERSOLD_STRONG = 8  # RSI<30 + KDJ J<0 / MACD翻红 ≥2 命中
V_RB_OVERSOLD_PARTIAL = 4  # 仅1命中
V_RB_VOL_SURGE = 6  # 量比≥2.0 放量企稳
V_RB_VOL_HEALTHY = 4  # 量比1.0~2.0 正常企稳
V_RB_VOL_LOW = -3  # 量比<1.0 缩量企稳不可信
V_RB_SECTOR_ACTIVE = 4  # 同板块≥3只 板块共振
V_RB_SECTOR_MOD = 2  # 同板块=2只 板块温和共振
V_RB_PATTERN_STRONG = 6  # 暴跌后阳包阴（强反转信号）
V_RB_PATTERN_HAMMER = 4  # 锤子线（低位承接）
V_RB_PATTERN_3BULL = 3  # 3连阳企稳

# Bonus constants
FIRST_TODAY_BONUS = 3
FIRST_BREAKOUT_BONUS = 8
FIRST_BREAKOUT_RANK_CHANGE = 500
FIRST_BREAKOUT_VOL_RATIO = 1.15

LIVE_VOL_BONUS = 3
LIVE_VOL_RATIO_THRESHOLD = 1.3

TURNOVER_BONUS_MODERATE = 3
TURNOVER_BONUS_HEALTHY = 5
TURNOVER_BONUS_PENALTY = -3
TURNOVER_HIGH = 20
TURNOVER_MEDIUM = 10
TURNOVER_LOW = 5

SECTOR_CLUSTER_BONUS_5 = 8
SECTOR_CLUSTER_BONUS_4 = 6
SECTOR_CLUSTER_BONUS_3 = 4
SECTOR_CLUSTER_BONUS_2 = 2

MARKET_ENV_STRONG = 2
MARKET_ENV_WEAK = -2
MARKET_STRONG_THRESHOLD = 0.5
MARKET_WEAK_THRESHOLD = -1.0

# Sentiment cycle thresholds
SENTIMENT_BOILING = 5
SENTIMENT_WARM = 2
SENTIMENT_COOL = -2
SENTIMENT_FROZEN = -5
SENTIMENT_AVG_TOP10_BOILING = 6.5
SENTIMENT_PCT_GT5_BOILING = 0.30
SENTIMENT_AVG_TOP10_WARM = 4.0
SENTIMENT_PCT_GT5_WARM = 0.15
SENTIMENT_AVG_TOP10_COOL = 1.0
SENTIMENT_PCT_GT5_COOL = 0.05

# Market cap bonus (enhancer)
MCAP_BONUS_SMALL = 3
MCAP_BONUS_MID = 1
MCAP_SMALL_THRESHOLD = 100  # 亿，≤此值 → 小市值加分
MCAP_MID_THRESHOLD = 300  # 亿，≤此值且 > 小市值 → 中等市值加分

# RPS bonus
RPS_BONUS_HIGH = 4
RPS_BONUS_MEDIUM = 2
RPS_BONUS_LOW = -3
RPS_PCTILE_HIGH = 80
RPS_PCTILE_MEDIUM = 60
RPS_PCTILE_LOW = 30

# K-line fetch configuration
KLINE_FETCH_DAYS = 45  # Number of days to fetch from API
KLINE_MIN_LENGTH = 32  # Minimum kline bars required for analysis

# ── 长跑健壮性：内存缓存上限 ──
# 按 symbol 累积的进程内缓存超过该条数时淘汰最旧条目，防止数周运行后内存缓慢膨胀。
# 2000 条远超 A 股活跃标的数量（飙升榜 100 只 + 候选池），正常不会触发淘汰。
CACHE_MAX_ENTRIES = 2000

# Fatigue detection for multi-day list appearances
FATIGUE_PRICE_WARN_ACCUM = 8  # 5-day accum below this after 3+ days → price fatigue
FATIGUE_VOL_WARN_RATIO = 1.0  # vol_ratio below this → volume fatigue
FATIGUE_STREAK_MIN = 3  # minimum streak before fatigue applies
FATIGUE_PENALTY_PER_DAY = -3  # penalty per streak day when fatigued
FATIGUE_PENALTY_CAP = -15  # max fatigue penalty
FATIGUE_ACCELERATE_PCT = 3  # today pct above this + healthy vol → acceleration bonus
FATIGUE_ACCELERATE_BONUS_PER_DAY = 2  # bonus per streak day when accelerating
FATIGUE_ACCELERATE_BONUS_CAP = 15  # max acceleration bonus（与 FATIGUE_PENALTY_CAP 对称）

# 辨识度标签 — 反复上榜
PROMINENCE_LOOKBACK_DAYS = 5  # 回溯 N 个交易日
PROMINENCE_REPEAT_THRESHOLD = 3  # 出现 ≥ N 天 → "↻"
PROMINENCE_MAX_AVG_RANK = 70  # 近 N 日平均排名 ≤ 此值

# == Cross-validation weights ==
# New face
V_NF_CONVERGE_STRONG = 13
V_NF_CONVERGE_PARTIAL = 8
V_NF_SECTOR_STRONG = 8
V_NF_SECTOR_MOD = 5
V_NF_SECTOR_WEAK = 0

# Momentum
V_MO_MA_FULL = 6
V_MO_MA_PARTIAL = 3
V_MO_MA_NONE = -5
V_MO_DIVERGENCE_NONE = 0
V_MO_DIVERGENCE_BEAR = -10
V_MO_VOL_UP = 8
V_MO_VOL_STABLE = 5
V_MO_VOL_SPIKE = -5

# New face — added in P0
V_NF_DIVERGENCE_BULL = 8
V_NF_VOLUME_CONFIRM = 5

# Short term
V_ST_VOL_HEALTHY = 8
V_ST_VOL_SURGE = 12
V_ST_SECTOR_HOT = 10
V_ST_SECTOR_WARM = 5
V_ST_SECTOR_COLD = 0
V_ST_RANK_TOP10 = 8
V_ST_RANK_TOP20 = 5
V_ST_RANK_TOP30 = 2
# rank>30（后排/边际上榜）惩罚。2026-08-14 校准：short_term 去重 218 条中
# rank>30 桶 141 条 next_day -0.71%/胜率43% vs rank≤30 77 条 +1.06%/胜率57%
# （cum_3d +0.67% vs +2.65%）——65% 的 short_term 都在后排，不能硬砍只能降分。
V_ST_RANK_LOW = -8
V_ST_MA_SUPPORT = 5
V_ST_MA_BROKEN = -5

# 超买判定 20 日涨幅阈值（validator._is_overbought 使用；pullback 下线后仅此一处消费）
PULLBACK_20D_GAIN_EXTREME = 60  # 20-day gain > 60% → extreme (overbought)

# ── 次日大涨候选独立区（display-only，2026-08-10）──
# 依据 scanner.nextday_attribution（去重 1006 条，next_day≥7% hit 10.2%）：
#   - 涨幅带甜蜜区：推荐时刻盘中涨幅 <2%（低吸潜伏 hit 11.7%/13.2%）与 4~8%
#     （中段启动 hit 11.8%）；2~4% 是死区（6.2%）、8~10% 是陷阱（7.5%，平均 -1.42%）。
#   - score 低分反指（<30 桶 hit 16.7% vs 70-90 桶 7.4%）。
#   - short_term 超买是死亡信号（hit 5% vs 非超买 10.5%）。
# 本区只筛出"形态符合次日大涨画像"的票：综合排序行尾 🎯 标记 + 档位置顶（display._sort_tier 档0，
# 2026-08-12 与辨识度一起置顶），不改 score / 不落库。
NEXTDAY_SPIKE_SWEET_MIN = 0.0  # 低吸潜伏带下限（推荐时刻盘中涨幅）
NEXTDAY_SPIKE_SWEET_LOW = 2.0  # 低吸潜伏带上限（<2%）
NEXTDAY_SPIKE_MID_MIN = 4.0  # 中段启动带下限
NEXTDAY_SPIKE_MID_MAX = 8.0  # 中段启动带上限（<8%，排除 8-10% 陷阱）
# 5 日累计门槛（2026-08-14，🎯 判定新增维度）。数据（nextday_attribution kline 回放全量，
# 含推荐日口径）：推荐前 5 日累计 10~15% 档 hit 21.2%（最好）、0~3 平档仅 5.4%（全场最差）——
# 「5 日累计低=安全」是反指（平盘=无动量，累计 10%+ = 资金已连续介入的潜伏启动）。
# 甜蜜带 + 累计≥6 使 hit 从 16.5% 提升至 20.0%（new_face 15.7%→21.1%、momentum 23.7%→26.5%）；
# rebound（超跌反弹，负累计天然，hit 33.3%）与 short_term（其规律在超买/弱转强，不在此列）豁免。
NEXTDAY_ACCUM_MIN = 6.0
# 次日大涨口径阈值（%）：2026-08-18 起唯一决策口径（hit = next_day ≥ 7%）。
# 原散落于 nextday_attribution.DEFAULT_THRESHOLD 与 scripts/*.py 各抄一份，
# 2026-08-20 收敛到 config 单源（见 AGENTS.md「次日大涨归因」）。
NEXTDAY_HIT_THRESHOLD = 7.0

# walk-forward embargo（2026-09-05 M1.3）：train/test 窗间强制空出的交易日数。
# next_day 标签 horizon=1（T 日推荐的标签依赖 T+1 行情）——不空窗时 test 首日
# 样本的答案已被 train 窗「见过」，样本外 hit 被系统性高估。升级到多日持有
# 标签（三重屏障）时应同步调大。
WF_EMBARGO_DAYS = 1

# ── 三重屏障标签（2026-09-05 M2，López de Prado Triple Barrier）──
# 现有 next_day>=7% 是单一固定水平二元标签，不建模「先止损」路径。三重屏障
# 输出 (label, touch_date, touch_pct, ret_at_horizon)：更贴近实盘交易决策
# （止盈/止损/到期），供模型桶训练与持有期优化消费。旧标签链路全部不动。
# 上屏障复用 NEXTDAY_HIT_THRESHOLD（与 next_day 靶点同源防漂移）。
TB_STOP_LOSS_PCT = -5.0  # 下屏障：买入价（信号日收盘）下方止损线（%）
TB_HORIZON_DAYS = 3  # 时间屏障：最多持有的交易日数（对齐 cum_3d 校准口径）
# 小板块共振劣后的板块规模门槛（2026-08-17，档位4级）：板块共振整体 cum_3d -2.22 全场最差，
# 但按规模分档差异大——cnt<5 hit 5.9%/均次日 -2.14%（最差，局部抱团次日兑现）、
# cnt 5-14 hit 6.7%/-0.74、cnt>=15 hit 11.0%/+0.18（接近无共振 11.2%，大板块有持续资金）。
# 只对 cnt<15 的小板块共振档位劣后（ranking.entry_tier 档3）；⚠板块普涨 文本已按用户
# 反馈下线（太扎眼），此配置仅用于排序，不渲染任何行尾文本。
SECTOR_RESONANCE_WARN_MAX = 15
# 过热妖股档位阈值（ranking.entry_tier 第一优先级）：5日累计（含推荐日口径，
# _nextday_entry_accum 回退链）≥50% 即使命中 🎯 也劣后档3（精选区校准 hit 最低区）。
# 资金流出档位阈值复用上方 FUND_OUTFLOW_NET_PCT（与「资金流出」标签同源防漂移）。
OVERHEAT_ACCUM_MAX = 50.0

# ── 类别先验单一事实源（2026-09-14：目标函数统一为「次日≥7% hit 率」）──
# 为什么要有这张表：此前「类别先验」在系统里存在三份手抄副本，且**口径不一**——
#   nextday_prob.BASE_RATE_BY_CAT       → hit 率（排序列）
#   config_scoring.COMPOSITE_CAT_BASE   → hit 率线性映射（但取自更早的快照）
#   decision.DECISION_CATEGORY_SPECS    → **平均超额收益**（准入 + 顺序）
# 第三张与另两张方向相反，导致同一类别在系统内既是最好又是最差：
#   core_dip  平均超额 +1.69%（旧表第一优先级） vs hit 率 6.5%（**低于**全体基准）
#   momentum  平均超额 −0.70%（旧表「永禁」）   vs hit 率 10.0%（**高于**全体基准）
# 2026-09-14 用户拍板：**hit 率是唯一类别先验口径**。本表即唯一手抄源，
# 下游（nextday_prob / ranking / decision）一律从它派生，不得再抄第二份。
#
# 数据来源与复核纪律：nextday_calib 按统一去重口径重算并做漂移巡检——
#   python -m scanner.nextday_calib            # 巡检（漂移即退出码 1）
#   python -m scanner.nextday_calib --write    # 重算后同步 nextday_calib.json
# ⚠ 改本表属**行为变更**：必须重跑 nextday_calib --write（否则
#   tests/test_nextday_calib.py 会 fail），并按 AGENTS.md 过样本外验证门。
CATEGORY_HIT_RATE: dict[str, float] = {
    "rebound": 0.179,
    "known_new_face": 0.127,
    "momentum": 0.100,
    "new_face": 0.097,
    "core_dip": 0.065,
    "short_term": 0.062,
    "pullback": 0.056,  # 已下线，保留供回测
    "comeback": 0.028,
    "pool_pick": 0.021,
}
# 全体兜底 hit 率（未知类别；亦作 composite 线性映射的基准点）
CATEGORY_HIT_RATE_DEFAULT = 0.078

# ── 统一复合评分（2026-09-08，v1+v2 合一）──
# composite_score = cat_base + tech_norm + rank_norm + fund_norm + dip_bonus
# 类别基值由 CATEGORY_HIT_RATE **派生**（不再手抄）：
#   cat_base = (hit − 基准) / (最高 hit − 基准) × 10，负值表示低于基准。
_CAT_BASE_SPREAD = max(CATEGORY_HIT_RATE.values()) - CATEGORY_HIT_RATE_DEFAULT
COMPOSITE_CAT_BASE: dict[str, float] = {
    cat: round((rate - CATEGORY_HIT_RATE_DEFAULT) / _CAT_BASE_SPREAD * 10.0, 1)
    for cat, rate in CATEGORY_HIT_RATE.items()
}
# 档位阈值：composite_score 推导，取代原 entry_tier 的 if/elif 级联。
COMPOSITE_TIER_THRESHOLDS: dict[int, float] = {
    0: 6.0,  # 档0：次日大涨画像区
    1: 4.0,  # 档1：强信号
    2: 2.0,  # 档2：普通
    # tier 3 = composite < 2.0 或过热硬门
}

# ── 持有期口径分化（2026-09-05 M1.1，学术对照校准）──
# 依据：Chen/Gao/He/Jiang/Xiong《Daily Price Limits and Destructive Market Behavior》
# （深交所账户级数据，Princeton）：涨停类信号次日高开（集中在次日开盘价）、随后长期反转。
# 推论：next_day 靶点类 1 日持有最优（次日兑现）；回测默认 hold 3 会把「次日兑现 +
# 后续回吐」混进同一 P&L，与 next_day 校准的排序结论系统性背离。
# 映射只收「信号校准于 cum_3d 语义」的类别：comeback（回踩买点是 3 日修复语义，
# 见 ranking.entry_tier 注释）、core_dip（低吸，非次日靶点）。next_day 靶点类
# （new_face/known_new_face/momentum/short_term/rebound/pool_pick）不在映射中，
# 沿用 base。portfolio_backtest --hold-days-auto 消费；不开该开关时回测行为
# 与历史完全一致（回归安全）。
HOLD_DAYS_BY_CATEGORY: dict[str, int] = {"comeback": 3, "core_dip": 3}


def hold_days_for(category: str, base: int) -> int:
    """类别级持有期覆盖：next_day 靶点类用 base，cum_3d 语义类用映射值。"""
    return HOLD_DAYS_BY_CATEGORY.get(category, base)


# ── 复权漂移指纹监控（2026-09-05 M1.2）──
# daily_kline 存雪球前复权（qfq）价，除权事件会静默重算全部历史 → 回测/rescore
# 跨期不可复现、accumulated_pct/🎯 门槛失真。收盘定稿后对锚定历史窗口做 SHA256
# 指纹比对，漂移即告警（scanner/kline_drift.py，unified_scanner 非交易分支调用）。
KLINE_DRIFT_FINGERPRINT_BARS = 250  # 指纹窗口覆盖的交易日数（约一年，上限）
KLINE_DRIFT_MIN_BARS = 30  # 初始化最低历史：不足则不锚定；可用历史在 [30,250) 时取全量锚定
KLINE_DRIFT_ROUND_DP = 4  # 价格哈希保留小数位（防浮点噪声误报）

# ── 蓄势突破观察画像（2026-08-21 新增，纯展示层 ⚡ 标记，不参与排序/评分/落库）──
# 来源：历史涨停复盘（全库去重 1453 条推荐，「推荐后当日封板」20 只 vs 全部推荐对照）：
# 涨停票共性 = new_face/kNF 或首推(61%) + 前5日横盘(累计中位 +2.0% vs 对照 +4.1%) +
# T-1 缩量(0.87x 前5均量) + 回调至20日高点下方(-13.8% 中位，非新高追涨) + MA多头(100%)。
# ⚠️ 样本仅 20 只（基础率 20/1453≈1.4%），且与 nextday_attribution「累计 0~3 带 hit 最差」
# 结论存在张力——纯展示观察标记：先观察积累样本，经 nextday_attribution 复盘后
# 再决定是否升级为排序因子。阈值取 A 组中位数附近的保守档。
BREAKOUT_ACCUM_MAX = 5.0  # 前5日累计（含推荐日口径）上限：横盘蓄势而非连涨加速
BREAKOUT_T1_VOL_RATIO = 0.9  # T-1 缩量阈值：T-1 量 / 前5日均量 ≤ 0.9
BREAKOUT_PULLBACK_MIN = -18.0  # T-1 收盘距20日高点回撤下限（%）
BREAKOUT_PULLBACK_MAX = -8.0  # 回撤上限：太浅=还在高位，太深=趋势可能已破
# NEXTDAY_CAT_PRIORITY（🎯 次日大涨画像可标记类别集合）已迁至 scanner/categories
# 单一事实来源，config 仅 re-export，见上方 from scanner.categories import。

# ── 次日大涨高概率规则（display-only，2026-08-30）──
# 实证：ma5r ≥ 5% & atrpct ≥ 8% & ret20 ≤ 40% → H2 盲测 LIFT 1.53x，
# 均值 +1.59%（扣 0.30% 成本净 +1.29%），跌超7% 仅 7.5%（基准 11.6%）。
# 全部只用 T-1 及之前已完成 bar，盘中任意时刻可算，全天不漂移。
# 阈值在 H1（2026-05-28..07-14）拟合、H2（2026-07-15..08-28）盲测确认。
NEXTDAY_RULE_MA5R_MIN = 5.0  # 收盘距5日均线最小距离（%）
NEXTDAY_RULE_ATRPCT_MIN = 7.0  # 21日平均真实波幅下限（%）
NEXTDAY_RULE_RET20_MAX = 40.0  # 21日涨幅上限（排除长期过热）
NEXTDAY_RULE_BARS = 21  # 回看 bar 数（ATP / ret20 计算窗口）

__all__ = [
    "VOL_RANK_VOL_THRESHOLD",
    "VOL_RANK_STRONG_RC",
    "VOL_RANK_MEDIUM_RC",
    "VOL_RANK_WEAK_RC",
    "VOL_RANK_STRONG_PTS",
    "VOL_RANK_MEDIUM_PTS",
    "VOL_RANK_WEAK_PTS",
    "VOL_PEAK_LOOKBACK",
    "VOL_PEAK_MOMENTUM_WARN",
    "VOL_PEAK_NEW_FACE_MIN",
    "VOL_PEAK_NEW_FACE_PENALTY",
    "VOL_PEAK_MOMENTUM_PENALTY",
    "WEAK_FORM_MIN_DOWN_DAYS",
    "WEAK_FORM_MAX_ACCUM",
    "WEAK_FORM_MIN_ACCUM",
    "WEAK_FORM_MAX_TODAY_PCT",
    "WEAK_FORM_CRASH_THRESHOLD",
    "GAP_UP_STRONG",
    "GAP_UP_MEDIUM",
    "GAP_UP_WEAK",
    "GAP_UP_STRONG_PTS",
    "GAP_UP_MEDIUM_PTS",
    "GAP_UP_WEAK_PTS",
    "BOTTOM_MAX_LOSS",
    "BOTTOM_VOL_SURGE",
    "BOTTOM_NEAR_LOW_PCT",
    "CRASH_THRESHOLD",
    "RECENT_2_RETURN_THRESHOLD",
    "NO_CRASH_SAFE_BONUS",
    "RECENT_2D_BONUS",
    "MOMENTUM_VOL_HEALTHY_MIN",
    "MOMENTUM_VOL_HEALTHY_MAX",
    "MA_BULL_3_TIER_SCORE",
    "MA_BULL_2_TIER_SCORE",
    "MA_BEAR_SCORE",
    "LIST_STREAK_BONUS_2",
    "LIST_STREAK_BONUS_3",
    "LIST_STREAK_BONUS_5",
    "TOP40_THRESHOLD",
    "TOP40_BONUS",
    "TOP40_ADVANCE_PER_10",
    "TOP20_EXTRA",
    "MOMENTUM_LAUNCH_ACCUM_MIN",
    "MOMENTUM_LAUNCH_ACCUM_MAX",
    "MOMENTUM_LAUNCH_TODAY_MIN",
    "MOMENTUM_LAUNCH_TODAY_MAX",
    "MOMENTUM_LAUNCH_VOL",
    "MOMENTUM_LAUNCH_WORD",
    "ST_OVERBOUGHT_BOLL",
    "ST_OVERBOUGHT_KDJ",
    "V_RB_OVERSOLD_STRONG",
    "V_RB_OVERSOLD_PARTIAL",
    "V_RB_VOL_SURGE",
    "V_RB_VOL_HEALTHY",
    "V_RB_VOL_LOW",
    "V_RB_SECTOR_ACTIVE",
    "V_RB_SECTOR_MOD",
    "V_RB_PATTERN_STRONG",
    "V_RB_PATTERN_HAMMER",
    "V_RB_PATTERN_3BULL",
    "FIRST_TODAY_BONUS",
    "FIRST_BREAKOUT_BONUS",
    "FIRST_BREAKOUT_RANK_CHANGE",
    "FIRST_BREAKOUT_VOL_RATIO",
    "LIVE_VOL_BONUS",
    "LIVE_VOL_RATIO_THRESHOLD",
    "TURNOVER_BONUS_MODERATE",
    "TURNOVER_BONUS_HEALTHY",
    "TURNOVER_BONUS_PENALTY",
    "TURNOVER_HIGH",
    "TURNOVER_MEDIUM",
    "TURNOVER_LOW",
    "SECTOR_CLUSTER_BONUS_5",
    "SECTOR_CLUSTER_BONUS_4",
    "SECTOR_CLUSTER_BONUS_3",
    "SECTOR_CLUSTER_BONUS_2",
    "MARKET_ENV_STRONG",
    "MARKET_ENV_WEAK",
    "MARKET_STRONG_THRESHOLD",
    "MARKET_WEAK_THRESHOLD",
    "SENTIMENT_BOILING",
    "SENTIMENT_WARM",
    "SENTIMENT_COOL",
    "SENTIMENT_FROZEN",
    "SENTIMENT_AVG_TOP10_BOILING",
    "SENTIMENT_PCT_GT5_BOILING",
    "SENTIMENT_AVG_TOP10_WARM",
    "SENTIMENT_PCT_GT5_WARM",
    "SENTIMENT_AVG_TOP10_COOL",
    "SENTIMENT_PCT_GT5_COOL",
    "MCAP_BONUS_SMALL",
    "MCAP_BONUS_MID",
    "MCAP_SMALL_THRESHOLD",
    "MCAP_MID_THRESHOLD",
    "RPS_BONUS_HIGH",
    "RPS_BONUS_MEDIUM",
    "RPS_BONUS_LOW",
    "RPS_PCTILE_HIGH",
    "RPS_PCTILE_MEDIUM",
    "RPS_PCTILE_LOW",
    "KLINE_FETCH_DAYS",
    "KLINE_MIN_LENGTH",
    "CACHE_MAX_ENTRIES",
    "FATIGUE_PRICE_WARN_ACCUM",
    "FATIGUE_VOL_WARN_RATIO",
    "FATIGUE_STREAK_MIN",
    "FATIGUE_PENALTY_PER_DAY",
    "FATIGUE_PENALTY_CAP",
    "FATIGUE_ACCELERATE_PCT",
    "FATIGUE_ACCELERATE_BONUS_PER_DAY",
    "FATIGUE_ACCELERATE_BONUS_CAP",
    "PROMINENCE_LOOKBACK_DAYS",
    "PROMINENCE_REPEAT_THRESHOLD",
    "PROMINENCE_MAX_AVG_RANK",
    "V_NF_CONVERGE_STRONG",
    "V_NF_CONVERGE_PARTIAL",
    "V_NF_SECTOR_STRONG",
    "V_NF_SECTOR_MOD",
    "V_NF_SECTOR_WEAK",
    "V_MO_MA_FULL",
    "V_MO_MA_PARTIAL",
    "V_MO_MA_NONE",
    "V_MO_DIVERGENCE_NONE",
    "V_MO_DIVERGENCE_BEAR",
    "V_MO_VOL_UP",
    "V_MO_VOL_STABLE",
    "V_MO_VOL_SPIKE",
    "V_NF_DIVERGENCE_BULL",
    "V_NF_VOLUME_CONFIRM",
    "V_ST_VOL_HEALTHY",
    "V_ST_VOL_SURGE",
    "V_ST_SECTOR_HOT",
    "V_ST_SECTOR_WARM",
    "V_ST_SECTOR_COLD",
    "V_ST_RANK_TOP10",
    "V_ST_RANK_TOP20",
    "V_ST_RANK_TOP30",
    "V_ST_RANK_LOW",
    "V_ST_MA_SUPPORT",
    "V_ST_MA_BROKEN",
    "PULLBACK_20D_GAIN_EXTREME",
    "NEXTDAY_SPIKE_SWEET_MIN",
    "NEXTDAY_SPIKE_SWEET_LOW",
    "NEXTDAY_SPIKE_MID_MIN",
    "NEXTDAY_SPIKE_MID_MAX",
    "NEXTDAY_ACCUM_MIN",
    "NEXTDAY_HIT_THRESHOLD",
    "WF_EMBARGO_DAYS",
    "TB_STOP_LOSS_PCT",
    "TB_HORIZON_DAYS",
    "SECTOR_RESONANCE_WARN_MAX",
    "OVERHEAT_ACCUM_MAX",
    "COMPOSITE_CAT_BASE",
    "CATEGORY_HIT_RATE",
    "CATEGORY_HIT_RATE_DEFAULT",
    "COMPOSITE_TIER_THRESHOLDS",
    "HOLD_DAYS_BY_CATEGORY",
    "hold_days_for",
    "KLINE_DRIFT_FINGERPRINT_BARS",
    "KLINE_DRIFT_MIN_BARS",
    "KLINE_DRIFT_ROUND_DP",
    "BREAKOUT_ACCUM_MAX",
    "BREAKOUT_T1_VOL_RATIO",
    "BREAKOUT_PULLBACK_MIN",
    "BREAKOUT_PULLBACK_MAX",
    "NEXTDAY_RULE_MA5R_MIN",
    "NEXTDAY_RULE_ATRPCT_MIN",
    "NEXTDAY_RULE_RET20_MAX",
    "NEXTDAY_RULE_BARS",
]
