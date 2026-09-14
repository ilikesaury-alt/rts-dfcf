# scanner/config_categories.py — 策略桶门槛 / 市值价格限制 / 回马枪·核心低吸配置
# 2026-09-13 从 config.py 拆出。定义集中在此，由 scanner/config.py 统一 re-export，
# 保持 `from scanner.config import ...` 导入路径不变。

# 跨模块引用：资金流扣分档（与「资金流出」标签同源，避免阈值漂移）
from scanner.config_sources import FUND_FLOW_MAIN_PCT_WEAK  # noqa: F401 (定义见 config_sources)

# Normal mode thresholds
# 首日新面孔（首次上榜）与二次上榜（known_new_face）分开设门槛（2026-08-10）：
# - known_new_face 分数反指（低分档[18,37) cum_3d +5.58/64% 胜率 vs 高分档[77,98) -3.76），
#   故 NEW_FACE_MIN_SCORE 保持低门槛，不砍"低调二次上榜"的低分档。
# - 首日 new_face 全 score 档均负收益（旧权重下 1018 条 cum_3d -1.58）曾设
#   NEW_FACE_FIRST_MIN_SCORE=50 砍量；但 2026-08-10 恢复 9826399 权重重平衡后
#   （today_pct 20→8 等，分数体系整体下移），50 在新权重下饿死列表（历史重扫 0 信号）。
#   回测（rescore）验证新权重下 18 → 29 信号 +12.79%、12 → 55 信号 +18.40%，
#   故 FIRST 阈值回到 18（与 9826399 配套；新权重下"反转信号主导"本身已砍掉大量动量票）。
NEW_FACE_MIN_SCORE = 18
NEW_FACE_FIRST_MIN_SCORE = 18
# 2026-08-10: 16→50——回测分桶 momentum 低分档[16,49) 55 条 cum_3d -0.95%，>=50 档 379 条
# +2.82%；「首次启动」子模式分数实测全 >=64 不受影响。切掉最差 ~12% 量。
MOMENTUM_MIN_SCORE = 50
SHORT_TERM_MIN_SCORE = 15
REBOUND_MIN_SCORE = 18

YI = 100_000_000
MAX_MARKET_CAP = 500 * YI

# 市值缓存兜底窗口（2026-08-20）：fetch_market_caps_batch 全失败时回退陈旧缓存，
# 非交易时段批量接口可能滞后，放宽到近 N 天（盘中仍限当日，由 scan_with_raw 按
# is_trading_time() 决定 max_age_days=0 vs 此值）。
MCAP_CACHE_MAX_AGE_DAYS = 7
MAX_STOCK_PRICE = 200.0
MAX_NEW_FACE_TODAY_PCT = 12
MAX_MOMENTUM_TODAY_PCT = 10  # P1-2: 8→10，让 9-10% 加速票能进 momentum（主升浪中段）
SHORT_TERM_MIN_TODAY_PCT = 2.0
SHORT_TERM_MAX_TODAY_PCT = 12.0  # P1-1: 8→12，覆盖 8-12% 强势股（创业板涨停 20% 仍排除）
# 超跌反弹：今日企稳阳线（温和涨幅），前期暴跌
REBOUND_MIN_TODAY_PCT = 0.5
REBOUND_MAX_TODAY_PCT = 8.0
REBOUND_CRASH_THRESHOLD = -10.0  # 前5日内至少一日跌幅 ≤ 此值（有暴跌日额外加分）
REBOUND_5D_DROP_THRESHOLD = -10.0  # 前5日累计跌幅 ≤ 此值即进入 rebound 评估
# -10~-15% 无暴跌日 = 阴跌企稳场景（P0-1 修复）
REBOUND_NEAR_LOW_PCT = 0.10  # 收盘距20日低点 ≤ 此比例

# 弱转强（分歧转一致）判定阈值
ST_SMALL_CAP = 100  # 流通市值 ≤100亿 视为小盘（超短偏好）
ST_MID_CAP = 300  # 100~300亿 中盘，>300亿 超短弹性差不加分
ST_DIVERGE_UPPER_SHADOW = 0.04  # 昨日上影线比例阈值
ST_DIVERGE_CLOSE_WEAK = 0.03  # 收盘/最高 - 1 < 此值 视为未封住高位
ST_BOMB_HIGH = 0.18  # 昨日最高/前收 - 1 ≥ 此值 视为曾触板（创业板≈20%）
ST_BOMB_CLOSE = 0.10  # 昨日收盘/前收 - 1 < 此值 视为收盘大回落（炸板/烂板）

# ── 策略桶开关（2026-08-30 简化聚焦）──
# 归因结论（nextday_attribution 去重 1559 样本，P0 修复后）：
#   rebound 17.9% / known_new_face 13.1% → 核心，保留
#   comeback 3.8% → 已禁用（全场最差）
#   short_term 6.8%（低于基准 8.7%）→ 冻结（不产出新推荐，历史数据保留）
#   momentum 10.3%（≈基准但回测 P&L -7.6%）→ 冻结
#   core_dip 9.4%（高于基准但仅 53 样本）→ 不进主推荐，保留数据采集
ENABLE_COMEBACK = True
ENABLE_CORE_DIP = True
# 2026-09-02 用户决策：重开（此前 Phase 3 冻结，因盘中 v1 主表多日仅 new_face/rebound
# 两桶在产、策略优选池频繁空表）。重开前归因基线见上方注释；如需回滚改回 False 即可。
ENABLE_SHORT_TERM = True
ENABLE_MOMENTUM = True
# 实验开关：放开 MIN_SCORE 门槛，让所有评分的候选都进入推荐。
# 归因数据显示 score 0-20 hit 21.8%（全场最高），MIN_SCORE=18 把它们全砍了。
# 开启后观察：① 信号量暴增是否稀释质量 ② 组合 P&L 是否改善 ③ hit rate 是否变化。
DISABLE_MIN_SCORE = False  # 实验结论：关闭后 P&L 变差，保留作为过滤器

# ── 回马枪（掉榜跟踪）— 独立策略桶（2026-08-07 新增）──
# 背景：候选池由"当次热榜"驱动，掉榜超跌股（如志特新材 07-09→07-31 三周掉榜）完全不可见，
# 反弹企稳日（07-14~07-30）无法被 rebound 评估。回马枪补上这块盲区。
# 评估域 = watch_pool（上过榜的 GEM 股）∪ 近 N 日推荐，减去今日在榜票；
# 每票每日最多评估一次（last_eval_date 落库，重启不丢）。
WATCH_POOL_MAX = 600  # 掉榜跟踪池上限（超限时淘汰 last_list_date 最旧）
WATCH_OFFLIST_KEEP_DAYS = 15  # 掉榜后保留交易日数（覆盖三周级掉榜，见志特新材案例）
# 反转变体：超跌企稳（复用 analyze_rebound 语义，off_list 收紧）
COMEBACK_MIN_TODAY_PCT = 2.0  # off_list 今日涨幅下限（比 rebound 0.5 更严，反转确认）
COMEBACK_MAX_TODAY_PCT = 12.0  # 与 short_term 上限同源：覆盖 8-12% 续涨（掉榜日无热榜背书）
COMEBACK_PREFILTER_5D_DROP = -8.0  # 5日累计跌幅≤此值才补拉当日 bar（成本预过滤，从~600降到数十/日）
COMEBACK_POS_DIMS = 3  # off_list 交叉验证维度下限（榜上为2，掉榜无热榜背书更严）
COMEBACK_KLINE_DEADLINE = 15  # 回马枪独立 K 线拉取 deadline（秒）：不与主榜共用，避免主榜耗尽预算后回马枪全 stale
# 回踩变体（吸收原历史推荐跟踪 tracker）：近 N 日推荐回调到买点 → 二次上车
COMEBACK_REENTRY_DAYS = 5  # 回踩跟踪窗口（交易日）
COMEBACK_REENTRY_BASE_SCORE = 40  # 回踩基础分（每命中一个买点信号 +15）
COMEBACK_REENTRY_SIGNAL_SCORE = 15
COMEBACK_REENTRY_FILTER_TODAY_HIGH = 5.0  # 今日涨幅≥此值 → 过滤（不追高）
COMEBACK_REENTRY_FILTER_TODAY_LOW = -5.0  # 今日跌幅≤此值 → 过滤（可能破位）
COMEBACK_REENTRY_FILTER_CUM_HIGH = 10.0  # 累计收益≥此值 → 过滤（已错过）
COMEBACK_REENTRY_FILTER_CUM_LOW = -10.0  # 累计收益≤此值 → 过滤（信号失效）
# 回马枪**扫描期**前置门：主力净占比 ≤ -5% → 候选直接丢弃（回调可能是出货）；
# 无当日数据 → 保留（视同中性，fail-open）。
# ⚠ 这不是「资金流出」档（那档单源在 config_sources.FUND_OUTFLOW_NET_PCT = -8.0%，
# 由 ranking.is_fund_outflow 判定、在展示层执行）。本处刻意更严：它是候选生成门
# （决定 recommendations 落库内容），一旦放宽到 -8% 会让 (-8%, -5%] 区间的票重新
# 进入落库与展示——那是放宽，不是"统一"。故保留 -5%，与展示门构成子集关系。
COMEBACK_REENTRY_FUND_FLOW_LOW = FUND_FLOW_MAIN_PCT_WEAK  # 与评分扣分档同源，避免阈值漂移
# 买点信号阈值（满足条件计 1 分，信号数决定状态分类）
COMEBACK_REENTRY_MA20_SUPPORT_PCT = 3.0  # |close-MA20|/MA20 < 此值 且 MA20 上行 → MA20 支撑
COMEBACK_REENTRY_VOL_SHRINK_RATIO = 0.8  # vol_ratio < 此值 → 缩量回调
COMEBACK_REENTRY_RSI_LOW = 30  # RSI 合理区下限
COMEBACK_REENTRY_RSI_HIGH = 50  # RSI 合理区上限（回落但不超卖）
COMEBACK_REENTRY_BOLL_MID_PCT = 3.0  # 距 BOLL 中轨±此值内 → 位置合理
COMEBACK_REENTRY_MA20_SLOPE_MIN = 0.5  # MA20 日涨幅>此值 → 上行（百分比）
COMEBACK_REENTRY_STATUS_BUY = 3  # 信号数≥此值 → "到买点"（5维信号：均线支撑/缩量/RSI/BOLL/MACD）
# 观察中门槛调整：原 6 维信号时为 3（防同源指标凑数），合并 MA20支撑+未破位→均线支撑后
# 降为 5 维，阈值相应下调到 2（仍需至少 2 个独立维度确认）。
COMEBACK_REENTRY_STATUS_WATCH = 2  # 信号数≥此值 → "观察中"，否则 "未到买点"（过滤）
COMEBACK_REENTRY_DISPLAY_WATCH_MAX = 0  # "观察中"补充最多显示条数（0 = 不显示，只看到买点）
# 回马枪独立区仅作兜底参考：主区（榜上五类）推荐条数 > COMEBACK_DISPLAY_MIN_MAIN 时
# 隐藏回马枪/核心低吸两低吸区（避免刷屏）；主区推荐条数 ≤ 该值（含为空）时补充展示，
# 最多前 COMEBACK_DISPLAY_MAX 条（回马枪为掉榜无热榜背书票，评分语义弱于榜上推荐）。
# 2026-08-24 用户决策：阈值 3→5，且删除大盘弱势 OR 门——主表 >5 条一律隐藏。
COMEBACK_DISPLAY_MAX = 3  # 回马枪最多显示条数
# 核心低吸单独放宽（2026-09-08 用户要求多显示几条）：核心股有主线方向背书，
# 语义强于回马枪，且仅主区稀少时才补充展示，多几条不刷屏。
CORE_DIP_DISPLAY_MAX = 6  # 核心方向低吸区最多显示条数
COMEBACK_DISPLAY_MIN_MAIN = 5  # 主区推荐条数大于此值 → 隐藏回马枪/核心低吸区；≤ 此值 → 补充显示

# 核心方向低吸（2026-08-19，`scanner/core_themes.py` + display 独立区）：
# 大跌市中找「当前市场主线方向（核心概念）的核心股低吸」机会。纯展示层推导（DB-only，
# 零新增网络请求，不写 recommendations、不进综合排序/回测口径），作为独立区块参考。
# 方法论：① 近 N 日推荐按概念聚合「持续上榜天数 + 主题相对强度」识别核心方向；
# ② 核心方向里近期已走强的成员股（龙头属性）；③ 从近期高点健康回调（非破位）的低吸窗口。
CORE_THEME_LOOKBACK_DAYS = 10  # 识别核心方向回看交易日数
CORE_THEME_MIN_DAYS = 3  # 概念持续上榜 ≥ 此天数才算“核心方向”（吃频次）
CORE_THEME_TOP_N = 4  # 核心方向最多取前 N 个（防板块普涨刷屏）
CORE_THEME_MAX_PER_THEME = 3  # 每核心方向最多显示核心股数
CORE_THEME_MAX_TOTAL = 9  # 低吸区总显示条数上限
CORE_RUN_MIN = 0.12  # 20日累计涨幅 ≥ 此值 → 有上涨（核心/龙头属性）
CORE_PULLBACK_MIN = -0.18  # 距20日高点回撤 ≥ 此值（更深）才可能够便宜
CORE_PULLBACK_MAX = -0.03  # 回撤 ≤ 此值（不能过早，还在尖顶附近）
CORE_NOT_OVERHEATED = 0.60  # 20日涨幅 > 此值 = 超买死亡区，排除低吸
CORE_TODAY_FLOOR = -6.0  # 今日涨幅 ≥ 此值（不追崩盘票）
CORE_MA20_BELOW_SLACK = 0.03  # 允许跌破 MA20 不超过此比例（未破位）
CORE_FLOW_FLOOR = -10.0  # 主力净占比 ≥ 此值（资金未大幅出逃）
CORE_THEME_NOISE = {"其他", ""}  # 聚合时排除的噪声概念
# 落库类别（2026-08-19）：核心方向低吸候选写入 recommendations 表的 category，
# 以便进 prevday_perf / nextday_attribution 复盘验证「主线回调低吸」假设（回马枪同款路径）。
# 与 comeback 同族（掉榜/跟踪类）：不入综合排序主表、不参与 mark_reversed 反转移出、
# 不进回马枪回踩候选域（避免跨区互换污染）。
CORE_DIP_CATEGORY = "core_dip"

__all__ = [
    "NEW_FACE_MIN_SCORE",
    "NEW_FACE_FIRST_MIN_SCORE",
    "MOMENTUM_MIN_SCORE",
    "SHORT_TERM_MIN_SCORE",
    "REBOUND_MIN_SCORE",
    "YI",
    "MAX_MARKET_CAP",
    "MCAP_CACHE_MAX_AGE_DAYS",
    "MAX_STOCK_PRICE",
    "MAX_NEW_FACE_TODAY_PCT",
    "MAX_MOMENTUM_TODAY_PCT",
    "SHORT_TERM_MIN_TODAY_PCT",
    "SHORT_TERM_MAX_TODAY_PCT",
    "REBOUND_MIN_TODAY_PCT",
    "REBOUND_MAX_TODAY_PCT",
    "REBOUND_CRASH_THRESHOLD",
    "REBOUND_5D_DROP_THRESHOLD",
    "REBOUND_NEAR_LOW_PCT",
    "ST_SMALL_CAP",
    "ST_MID_CAP",
    "ST_DIVERGE_UPPER_SHADOW",
    "ST_DIVERGE_CLOSE_WEAK",
    "ST_BOMB_HIGH",
    "ST_BOMB_CLOSE",
    "ENABLE_COMEBACK",
    "ENABLE_CORE_DIP",
    "ENABLE_SHORT_TERM",
    "ENABLE_MOMENTUM",
    "DISABLE_MIN_SCORE",
    "WATCH_POOL_MAX",
    "WATCH_OFFLIST_KEEP_DAYS",
    "COMEBACK_MIN_TODAY_PCT",
    "COMEBACK_MAX_TODAY_PCT",
    "COMEBACK_PREFILTER_5D_DROP",
    "COMEBACK_POS_DIMS",
    "COMEBACK_KLINE_DEADLINE",
    "COMEBACK_REENTRY_DAYS",
    "COMEBACK_REENTRY_BASE_SCORE",
    "COMEBACK_REENTRY_SIGNAL_SCORE",
    "COMEBACK_REENTRY_FILTER_TODAY_HIGH",
    "COMEBACK_REENTRY_FILTER_TODAY_LOW",
    "COMEBACK_REENTRY_FILTER_CUM_HIGH",
    "COMEBACK_REENTRY_FILTER_CUM_LOW",
    "COMEBACK_REENTRY_FUND_FLOW_LOW",
    "COMEBACK_REENTRY_MA20_SUPPORT_PCT",
    "COMEBACK_REENTRY_VOL_SHRINK_RATIO",
    "COMEBACK_REENTRY_RSI_LOW",
    "COMEBACK_REENTRY_RSI_HIGH",
    "COMEBACK_REENTRY_BOLL_MID_PCT",
    "COMEBACK_REENTRY_MA20_SLOPE_MIN",
    "COMEBACK_REENTRY_STATUS_BUY",
    "COMEBACK_REENTRY_STATUS_WATCH",
    "COMEBACK_REENTRY_DISPLAY_WATCH_MAX",
    "COMEBACK_DISPLAY_MAX",
    "CORE_DIP_DISPLAY_MAX",
    "COMEBACK_DISPLAY_MIN_MAIN",
    "CORE_THEME_LOOKBACK_DAYS",
    "CORE_THEME_MIN_DAYS",
    "CORE_THEME_TOP_N",
    "CORE_THEME_MAX_PER_THEME",
    "CORE_THEME_MAX_TOTAL",
    "CORE_RUN_MIN",
    "CORE_PULLBACK_MIN",
    "CORE_PULLBACK_MAX",
    "CORE_NOT_OVERHEATED",
    "CORE_TODAY_FLOOR",
    "CORE_MA20_BELOW_SLACK",
    "CORE_FLOW_FLOOR",
    "CORE_THEME_NOISE",
    "CORE_DIP_CATEGORY",
]
