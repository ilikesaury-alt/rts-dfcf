import os
from datetime import datetime, time as dtime, timezone, timedelta

BEIJING_TZ = timezone(timedelta(hours=8), name="CST")


def now_beijing() -> datetime:
    """Return current datetime in Beijing timezone (UTC+8)."""
    return datetime.now(BEIJING_TZ)

# 数据源策略："auto"（雪球优先+AKShare兜底）/ "xueqiu" / "akshare"
# 通过环境变量 RTS_DATA_SOURCE 可覆盖，用于雪球反爬封禁时强制切换。
DATA_SOURCE = os.environ.get("RTS_DATA_SOURCE", "auto")

REFRESH_INTERVAL = 60
REQUEST_TIMEOUT = 15
# 连接超时（秒）：与 REQUEST_TIMEOUT 组成 (connect, read) 元组，
# 避免连不上的主机在 connect 阶段长时间挂起拖垮整个扫描周期。
REQUEST_CONNECT_TIMEOUT = 5
NEW_FACE_LOOKBACK_DAYS = 3

# 单轮 K 线串行拉取 deadline（秒）：超过即停止补拉，剩余票回退旧缓存。
# 防止 API 故障时串行重试让单轮扫描假死数十分钟。
KLINE_FETCH_DEADLINE = 45

# 分时数据（分时强度/开盘强度/实时量比）单相拉取 deadline（秒）。
# minute API 挂死时单只请求最坏 ~48s（15s×3 重试），40 只候选 6 线程并发
# 会让 as_completed 无限等待最长 ~5 分钟；加 deadline 后超时部分降级为
# 无分时信号（None），与 K 线 KLINE_FETCH_DEADLINE 的限时语义对齐。
MINUTE_FETCH_PHASE_DEADLINE = 30

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
PULLBACK_MIN_SCORE = 18  # 保留常量供 analyze_pullback 测试使用，orchestrator 已下线 pullback
SHORT_TERM_MIN_SCORE = 15
REBOUND_MIN_SCORE = 18

YI = 100_000_000
MAX_MARKET_CAP = 500 * YI
MAX_STOCK_PRICE = 200.0
MAX_NEW_FACE_TODAY_PCT = 12
MAX_MOMENTUM_TODAY_PCT = 10  # P1-2: 8→10，让 9-10% 加速票能进 momentum（主升浪中段）
PULLBACK_MAX_TODAY_PCT = 0.0      # 仅今日平盘/下跌才算回调，消除 today∈(0,2] 死区
SHORT_TERM_MIN_TODAY_PCT = 2.0
SHORT_TERM_MAX_TODAY_PCT = 12.0  # P1-1: 8→12，覆盖 8-12% 强势股（创业板涨停 20% 仍排除）
# 超跌反弹：今日企稳阳线（温和涨幅），前期暴跌
REBOUND_MIN_TODAY_PCT = 0.5
REBOUND_MAX_TODAY_PCT = 8.0
REBOUND_CRASH_THRESHOLD = -10.0      # 前5日内至少一日跌幅 ≤ 此值（有暴跌日额外加分）
REBOUND_5D_DROP_THRESHOLD = -10.0    # 前5日累计跌幅 ≤ 此值即进入 rebound 评估
                                      # -10~-15% 无暴跌日 = 阴跌企稳场景（P0-1 修复）
REBOUND_NEAR_LOW_PCT = 0.10          # 收盘距20日低点 ≤ 此比例

# 超短同板块数量上限：板块普涨日防止单板块淹没超短列表（P0-69 后再加一道闸）
SHORT_TERM_MAX_PER_SECTOR = 2

# 弱转强（分歧转一致）判定阈值
ST_SMALL_CAP = 100          # 流通市值 ≤100亿 视为小盘（超短偏好）
ST_MID_CAP = 300            # 100~300亿 中盘，>300亿 超短弹性差不加分
ST_DIVERGE_UPPER_SHADOW = 0.04   # 昨日上影线比例阈值
ST_DIVERGE_CLOSE_WEAK = 0.03     # 收盘/最高 - 1 < 此值 视为未封住高位
ST_BOMB_HIGH = 0.18              # 昨日最高/前收 - 1 ≥ 此值 视为曾触板（创业板≈20%）
ST_BOMB_CLOSE = 0.10             # 昨日收盘/前收 - 1 < 此值 视为收盘大回落（炸板/烂板）

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
VOL_PEAK_NEW_FACE_MIN = 0.3   # volume < 30% of peak → insufficient reversal volume
VOL_PEAK_PULLBACK_CONFIRM = 0.3  # volume < 30% of peak → genuine shrinkage

# Peak volume ratio scoring (analysis.py hardcoded values migrated)
VOL_PEAK_NEW_FACE_PENALTY = -5   # new_face vol_peak < threshold → penalty
VOL_PEAK_MOMENTUM_PENALTY = -8   # momentum vol_peak < threshold → penalty
VOL_PEAK_PULLBACK_BONUS = 5     # pullback vol_peak < threshold → shrinkage bonus

# MA bull extra bonus (pullback)
MA_BULL_EXTRA_BONUS = 5

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
NO_CRASH_SAFE_BONUS = 8      # 拆分自 no_crash：无 crash day 基础安全分
RECENT_2D_BONUS = 5           # 拆分自 no_crash：近2日不差附加分
MOMENTUM_VOL_HEALTHY_MIN = 0.7
MOMENTUM_VOL_HEALTHY_MAX = 2.0

# MA alignment scoring
MA_BULL_3_TIER_SCORE = 6
MA_BULL_2_TIER_SCORE = 3
MA_BEAR_SCORE = -3

# List momentum scoring (consecutive surge list appearances + rank trajectory)
LIST_STREAK_BONUS_2 = 3
LIST_STREAK_BONUS_3 = 5
LIST_STREAK_BONUS_5 = 6
TOP40_THRESHOLD = 40
TOP40_BONUS = 3
TOP40_ADVANCE_PER_10 = 2
TOP20_EXTRA = 2

FEISHU_WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/d0caf1dd-54b6-4b86-b83d-861e4c79afda"
FEISHU_KEYWORD = "lichun"
FEISHU_MIN_INTERVAL = 300  # 飞书最小推送间隔（秒），防止触发 Lark 限流

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://xueqiu.com/",
}

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "scanner.db")
LOG_DIR = os.path.join(BASE_DIR, "logs")

# ── 行情增强数据（涨停池 AKShare + 个股资金流自实现直连 push2delay）──
# 开关：环境变量可覆盖（RTS_ENABLE_ZT_POOL / RTS_ENABLE_FUND_FLOW），0/1/false/true
def _env_flag(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")

ENABLE_ZT_POOL = _env_flag("RTS_ENABLE_ZT_POOL", True)          # 涨停池
ENABLE_FUND_FLOW = _env_flag("RTS_ENABLE_FUND_FLOW", True)      # 个股资金流
# 资金流 API host：默认 push2delay（与 akshare 的 push2 相同 clist API）。
# push2.eastmoney.com 在本机网络直连/代理均不可达（连接被重置），push2delay 可达
# （数据可能延迟约15分钟）。网络可直连 push2 的环境可用 RTS_FUND_FLOW_HOST 切回。
def _env_str(name: str, default: str) -> str:
    v = os.environ.get(name)
    return v.strip() if v and v.strip() else default

FUND_FLOW_HOST = _env_str("RTS_FUND_FLOW_HOST", "push2delay.eastmoney.com")
# 盘中刷新间隔（进程内缓存 TTL + DB 缓存盘中新鲜度共用）：扫描期内数据
# 过期后视为缺失，触发重拉，实现"盘中每 5 分钟更新"（而非全天冻结首次快照）
ZT_POOL_TTL_SEC = 300
FUND_FLOW_TTL_SEC = 300
# 资金流超时部分结果的进程缓存 TTL：仅缓存一扫描周期，下一轮重试补全缺页，
# 避免"超时拿到的部分数据"被冻结 5 分钟造成静默缺数据
FUND_FLOW_PARTIAL_TTL_SEC = 60
# 单次拉取限时：AKShare 内部请求可能无 timeout（涨停池）或全市场分页很慢
# （资金流约 53 页，6 线程并行实测 ~17s）。限时保护 60s 扫描循环不被外部 host 挂死。
ZT_POOL_FETCH_TIMEOUT = 20        # 涨停池单次拉取上限（秒）
FUND_FLOW_FETCH_TIMEOUT = 30      # 资金流全市场分页拉取上限（秒，超时返回已收集部分）
# 资金流评分阈值（主力净流入净占比 %）
FUND_FLOW_MAIN_PCT_STRONG = 5.0   # 主力净占比 ≥5% → 加分
FUND_FLOW_MAIN_PCT_WEAK = -5.0    # 主力净占比 ≤-5% → 扣分
# FUND_FLOW_MAIN_PCT_EXTREME 定义见下方「风险标签阈值」——与 FUND_OUTFLOW_NET_PCT 同源，避免档位漂移
# 2026-08-10: FUND_FLOW_BONUS_STRONG 归零——回测分组显示强流入(≥5%)组 next_day 均 -1.13%
# （n=22）差于无数据基线 -0.85%，momentum/short_term 内同为负：今日主力净流入与当日涨幅
# 正相关，是追涨资金次日兑现，加分方向反指。仅保留 FUND_FLOW_BONUS_WEAK=-3 流出扣分、
# 「资金流出」标签（规避语义，与预测语义无关）。字段仍写入 dims 供展示/归因。
FUND_FLOW_BONUS_STRONG = 0
FUND_FLOW_BONUS_WEAK = -3
# 综合排序「档位置顶」历史说明（2026-08-06 引入，2026-08-11 起仅保留辨识度分档）：
# 排序键 (档位, CAT_DISPLAY_PRIORITY, 分数键)。档位原含资金流——强流入 ≥ FUND_FLOW_MAIN_PCT_STRONG
# 置前、强流出 ≤ FUND_FLOW_MAIN_PCT_WEAK 劣后（覆盖辨识度）。2026-08-11 去掉资金流排序：
# 档0 = 辨识度(↻)、档1 = 其余，资金流不再置前/劣后（净流出票正常展示，仅保留图标与
# 「资金流出」标签）。展示层不改最终评分/不落库/不影响策略桶与回测。
# 连板评分：连板数（今日涨停池涨停统计口径）加分/追高降权
ZT_LIANBAN_BONUS_2 = 5
ZT_LIANBAN_BONUS_3 = 8
ZT_LIANBAN_GT3_PENALTY = -5       # ≥4 板追高降权
# 风险标签阈值
FUND_OUTFLOW_NET_PCT = -8.0       # 主力净流出占比 ≤-8% → 「资金流出」标签
# 资金流图标强档阈值：与「资金流出」标签同源（负值取绝对值），避免两处分别改造成漂移
FUND_FLOW_MAIN_PCT_EXTREME = -FUND_OUTFLOW_NET_PCT
ZT_ZHA_BAN_MIN = 1                # 炸板次数 ≥1 且今日曾涨停 → 「炸板」标签
# 展示层「硬信号」风险标签（display/feishu 共用）：展开文字显示；软信号折叠成 +N 角标。
# 注意与 RISK_FLAGS_HARD_FILTER（硬过滤，命中即从推荐列表移除）不同——此处仅影响展示分级。
RISK_FLAGS_DISPLAY_HARD = {"超买", "主力出货", "趋势破位"}

# ── 概念板块数据源（东财 F10）─
CONCEPT_API_TIMEOUT = 8          # 单只个股概念拉取超时（秒）
CONCEPT_CACHE_TTL_DAYS = 7       # concept_cache 缓存天数（概念归属低频变动，7 天足够新鲜）
CONCEPT_MAX_FETCH_THREADS = 8    # 概念归属并行拉取线程数
# 噪音板块黑名单：地域/风格/指数成分/涨停梯队等不反映"推动逻辑"的标签，不参与驱动概念聚合
CONCEPT_NOISE_BOARDS: set[str] = {
    "北京板块", "上海板块", "广东板块", "深圳板块", "江苏板块", "浙江板块",
    "深圳特区",
    "中盘成长", "中盘价值", "中盘股", "大盘股", "大盘价值", "大盘成长",
    "小盘股", "小盘价值", "小盘成长", "微盘股",
    "融资融券", "转融券标的", "深股通", "沪股通", "富时罗素", "MSCI中国",
    "中证500", "中证1000", "中证800", "中证100", "沪深300", "上证50", "上证A股",
    "深证成指", "深成500", "深证500", "创业板综", "创业板指", "科创50", "北证50",
    "昨日涨停", "昨日连板", "昨日连板_含一字", "昨日打板", "昨日炸板", "昨日二板",
    "昨日打二板以上表现", "连续涨停", "涨停股", "强势股",
    "股权分散", "股权激励", "股份回购", "高送转", "高股息", "破净", "破发股",
    "破发次新", "转债标的", "东方财富热股", "题材股", "百元股", "低价股",
    "预盈预增", "机构重仓", "基金重仓", "社保重仓", "QFII重仓",
    "注册制次新股", "ST股", "近期摘帽",
    "最近多板", "昨日高换手", "最近异动", "昨收新高", "创历史新高",
}
# 地域类板块统一按后缀"板块"排除（东财地域板块命名均以"板块"结尾，如"安徽板块"）
CONCEPT_NOISE_BOARD_SUFFIXES: tuple[str, ...] = ("板块",)

MORNING_START = dtime(9, 30)
MORNING_END = dtime(11, 30)
AFTERNOON_START = dtime(13, 0)
AFTERNOON_END = dtime(15, 0)

# Scoring weights — used by analysis.py
# Step 2 (2026-08-07, 9826399, merge 24443ff 中丢失后于 2026-08-10 恢复):
# IC 归因重平衡——new_face 是「超卖反转」策略，原权重过度奖励动量确认信号
# （今日大涨 / 放量 / 累计涨幅，cum_3d IC 均为负），真正有预测力的反转触发信号
# （KDJ K<20金叉/J<0、RSI<30、MACD金叉）权重过小。重平衡后 reconstruct_score
# rank-IC：new_face +0.045→+0.109、Combined +0.041→+0.099（ic_attribution.py）。
# 2026-08-10 独立复核：KDJ超卖金叉触发组 cum_3d +0.54 vs 未触发 -0.52（IC +0.42）、
# 放量 >1.3 触发组 -2.83 vs 未触发 -0.51（IC ≈0），方向一致。
NEW_FACE_WEIGHTS: dict[str, int] = {
    "today_pct_2_6": 8,
    "today_pct_1_2": 6,
    "today_pct_0_5_1": 4,
    "today_pct_lt_0_5": 3,
    "today_pct_6_8": 5,
    "today_pct_gt_8": -15,
    "accum_neg5_10": 6,
    "accum_lt_neg5": 0,
    "accum_10_15": 3,
    "accum_15_20": -5,
    "bottom_confirmed": 0,
    "v_shape": 8,
    "volume_surge": 0,
    "value_gte_10000": 2,
    "value_gte_5000": 1,
    "rsi_bonus": 5,
    "macd_bonus": 6,
    "rsi14_oversold_bonus": 4,
    "bollinger_oversold": 5,
    "kdj_bonus": 6,
    "atr_contraction": 2,
    "obv_not_negative": 3,
}

# ── momentum "首次启动" 子模式 ──
# 目标：今日 4-6%（下限3.5%）放量启动 + 累计涨幅尚低（0~7%）的票，
# 提前 1-2 天进 momentum 池，不必等涨到 6-10% 才上榜。
# 数据：momentum 4-6% 带 cum2d +6.56% / cum3d +9.46%, 胜率 49%；2-4% 全策略负收益为噪音。
MOMENTUM_LAUNCH_ACCUM_MIN = 0.0
MOMENTUM_LAUNCH_ACCUM_MAX = 7.0
MOMENTUM_LAUNCH_TODAY_MIN = 3.5
MOMENTUM_LAUNCH_TODAY_MAX = 8.0
MOMENTUM_LAUNCH_VOL = 1.5       # 放量启动门槛（压掉缩量假阳）
MOMENTUM_LAUNCH_WORD = "启动首日"

# P0 IC 重平衡 (2026-08-08, e3ee10e4, merge 24443ff 中丢失后于 2026-08-10 恢复)。
# 依据 cum_3d 口径 dimension_ic（当前库复核一致）：
#   momentum_volume -0.32 强反指 → 健康量能清零；momentum_value +0.22 正指 → 提权；
#   momentum_macd -0.21 / momentum_rsi -0.06 / momentum_accumulated -0.09 反指 → 清零/降权；
#   momentum_adx +0.22 强正指 → 提权；momentum_kdj ≈0 → 中性略提。
MOMENTUM_WEIGHTS: dict[str, int] = {
    "today_pct_2_6": 20,
    "today_pct_1_2": 10,
    "today_pct_0_5_1": 5,
    "today_pct_lt_0_5": 5,
    "today_pct_6_8": 5,
    "today_pct_8_10": 3,   # P1-2: 新增 8-10% 档（加速赶顶风险，进一步降权）
    "accum_10_15": 8,
    "accum_15_20": 5,
    "accum_20_30": 3,
    "accum_gte_30": -15,
    "vol_healthy": 0,
    "vol_surge": 0,
    "vol_low": -3,
    "value_gte_10000": 5,
    "value_gte_5000": 2,
    "rsi_bonus": 0,
    "kdj_bonus": 4,
    "macd_bonus": 0,
    "adx_bonus": 7,
    "adx_weak": -3,
    "atr_healthy": 0,
    "atr_overheated": -3,
    "obv_uptrend": 3,
}

PULLBACK_WEIGHTS: dict[str, int] = {
    "today_neg1_0": 10,
    "today_neg3_neg1": 15,
    "today_neg5_neg3": 8,
    "accum_5_10": 10,
    "accum_10_20": 18,
    "accum_20_30": 8,
    "accum_gte_30": -10,
    "vol_healthy": 12,
    "vol_low": 0,
    "vol_surge": -8,
    "ma_support": 12,
    "ma_broken": -10,
    "rsi_oversold": 5,
    "rsi_mid": 3,
    "macd_bonus": 3,
    "rank_top10": 8,
    "rank_top30": 5,
    "kdj_bonus": 3,
    "bollinger_mid_support": 5,
}

SHORT_TERM_WEIGHTS: dict[str, int] = {
    "today_pct_2_4": 15,
    # 2026-08-10: 4-6% 20→8（分桶最差档：41 条 cum_3d -1.41%，权重却是最高）、
    # 8-12% 8→15（分桶最好档：21 条 +3.84%，接近涨停梯队次日惯性）——按数据反向修正，
    # 替换原"涨幅偏大降权"的拍脑袋设定。2-4% / 6-8% 保持（+0.43% / +0.45% 中性）。
    "today_pct_4_6": 8,
    "today_pct_6_8": 12,
    "today_pct_8_12": 15,   # P1-1: 8-12% 档（2026-08-10 由 8 上调，数据支持）
    "accum_5_10": 10,
    "accum_10_15": 15,
    "accum_15_20": 8,
    "accum_gte_20": -5,
    "accum_lt_0": -5,
    "vol_healthy": 8,
    "vol_surge": 12,
    "vol_low": -5,
    "value_small_cap": 6,
    "value_mid_cap": 2,
    "st_weak_to_strong": 8,
    "rsi_bonus": 3,
    "kdj_bonus": 3,
    "macd_bonus": 3,
    "rank_top10": 8,
    "rank_top20": 5,
    "rank_top30": 3,
}

REBOUND_WEIGHTS: dict[str, int] = {
    # 今日企稳阳线涨幅档（温和涨幅为主，避免追高）
    "today_pct_0_5_2": 15,      # 0.5~2%：温和企稳
    "today_pct_2_4": 18,        # 2~4%：明显企稳
    "today_pct_4_6": 12,        # 4~6%：较强企稳
    "today_pct_6_8": 5,         # 6~8%：涨幅偏大降权
    # 超跌深度档（越深反弹空间越大）
    "drop_15_20": 10,           # 前5日累计跌15~20%
    "drop_20_30": 15,           # 跌20~30%
    "drop_gte_30": 20,          # 跌≥30%
    "crash_day_bonus": 5,       # 前5日有单日暴跌(≤-10%)额外加分
    # 量能配合
    "vol_healthy": 8,           # 量比1.0~2.0：正常企稳量能
    "vol_surge": 12,            # 量比≥2.0：放量企稳（主力介入）
    "vol_low": -3,              # 量比<0.8：缩量企稳不可信
    # 技术面确认
    "rsi_oversold": 8,          # RSI<30：超卖反弹
    "rsi_mid": 3,               # RSI 30~50：低位企稳
    "bollinger_lower": 5,       # 触及BOLL下轨
    "v_shape": 8,               # V型反转特征（缩量低点+放量阳线）
    # 板块/市值
    "sector_active": 5,         # 同板块≥3只（板块共振）
    "value_small_cap": 4,       # 小盘弹性大
    "value_mid_cap": 2,
}

# 超短末周期（鱼尾段）超买防护：validator 单点判断阈值。
# 20日涨幅阈值直接复用 PULLBACK_20D_GAIN_*（避免常量膨胀）。
# 惩罚已移除：超买时仅靠 validator passed 门禁否决 + enhancer 标记，不再做 score 压制。
# 2026-07-28 收紧：原 BOLL=1.0 / KDJ=105 在强势股主升浪中几乎必中（健康强趋势 J 常 90~115，
# 单日大涨即破上轨），导致"超买"标签沦为废话、短炒票全民告警。改为仅"极端超买"才触发：
#   - BOLL %B > 1.10：明显脱离上轨（而非刚触碰）
#   - KDJ J > 115：超过健康强趋势上限（90~115）
# 20日涨幅>60% 维持（ genuinely 过热），三项仍任一即判（否决用，宁可漏放不可误杀主升浪）。
ST_OVERBOUGHT_BOLL = 1.10         # BOLL %B > 此值 = 明显破上轨（极端高位）
ST_OVERBOUGHT_KDJ = 115           # KDJ J > 此值 = 极端超买（健康强趋势 J 常 90~115，旧 105 误伤）

# ── rebound 交叉验证常量 ──
V_RB_OVERSOLD_STRONG = 8      # RSI<30 + KDJ J<0 / MACD翻红 ≥2 命中
V_RB_OVERSOLD_PARTIAL = 4     # 仅1命中
V_RB_VOL_SURGE = 6            # 量比≥2.0 放量企稳
V_RB_VOL_HEALTHY = 4          # 量比1.0~2.0 正常企稳
V_RB_VOL_LOW = -3             # 量比<1.0 缩量企稳不可信
V_RB_SECTOR_ACTIVE = 4        # 同板块≥3只 板块共振
V_RB_SECTOR_MOD = 2           # 同板块=2只 板块温和共振
V_RB_PATTERN_STRONG = 6       # 暴跌后阳包阴（强反转信号）
V_RB_PATTERN_HAMMER = 4       # 锤子线（低位承接）
V_RB_PATTERN_3BULL = 3        # 3连阳企稳

# Bonus constants
CROSS_SOURCE_BONUS = 5
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
MCAP_MID_THRESHOLD = 300    # 亿，≤此值且 > 小市值 → 中等市值加分

# RPS bonus
RPS_BONUS_HIGH = 4
RPS_BONUS_MEDIUM = 2
RPS_BONUS_LOW = -3
RPS_PCTILE_HIGH = 80
RPS_PCTILE_MEDIUM = 60
RPS_PCTILE_LOW = 30

# K-line fetch configuration
KLINE_FETCH_DAYS = 45     # Number of days to fetch from API
KLINE_MIN_LENGTH = 32     # Minimum kline bars required for analysis

# ── 长跑健壮性：内存缓存上限 ──
# 按 symbol 累积的进程内缓存超过该条数时淘汰最旧条目，防止数周运行后内存缓慢膨胀。
# 2000 条远超 A 股活跃标的数量（飙升榜 100 只 + 候选池），正常不会触发淘汰。
CACHE_MAX_ENTRIES = 2000

# ── 崩溃自动重启（--supervise）──
SUPERVISE_RESTART_DELAY = 10        # 重启基础延迟（秒），失败后指数退避
SUPERVISE_RESTART_MAX_DELAY = 300   # 退避封顶（秒）
SUPERVISE_RESET_AFTER_SECONDS = 600 # 子进程存活超过此秒数则重置退避计数
SUPERVISE_CHILD_TIMEOUT = 1800      # 子进程无输出/心跳超时（秒）：超时判定假死并强制重启
# 启动宽限期：Popen 后此窗口内不判心跳超时。目的有二：
#  1) 子进程完成导入/建连需要时间，宽限内不论心跳文件状态都不强杀，避免与首拍 touch 竞态；
#  2) 配合父进程启动前清理陈旧心跳文件，杜绝"上一轮冻结残留旧 mtime → 首轮 poll 误杀健康新进程"的死循环。
SUPERVISE_CHILD_GRACE = 60          # 子进程启动宽限期（秒）
SUPERVISE_LOG_FILE = os.path.join(LOG_DIR, "supervisor.log")

# Fatigue detection for multi-day list appearances
FATIGUE_PRICE_WARN_ACCUM = 8    # 5-day accum below this after 3+ days → price fatigue
FATIGUE_VOL_WARN_RATIO = 1.0   # vol_ratio below this → volume fatigue
FATIGUE_STREAK_MIN = 3          # minimum streak before fatigue applies
FATIGUE_PENALTY_PER_DAY = -3   # penalty per streak day when fatigued
FATIGUE_PENALTY_CAP = -15      # max fatigue penalty
FATIGUE_ACCELERATE_PCT = 3     # today pct above this + healthy vol → acceleration bonus
FATIGUE_ACCELERATE_BONUS_PER_DAY = 2  # bonus per streak day when accelerating
FATIGUE_ACCELERATE_BONUS_CAP = 15     # max acceleration bonus（与 FATIGUE_PENALTY_CAP 对称）

# ── 主力出货风险标签阈值 ──
# 满足任一复合条件即标记"主力出货"，用于识别高位派发迹象。
# 2026-07-28 收紧：原 Rule 2（高位高换手+超买）仅要求换手率>5% + 宽松超买，
# 几乎把所有活跃强势股都打成"主力出货"。改为高确信条件，且依赖已收紧的极端超买。
DISTRIBUTION_ACCUM_HIGH = 20.0      # 累计涨幅高位阈值（放量滞涨场景）
DISTRIBUTION_ACCUM_MID = 15.0       # 累计涨幅中高位阈值（高换手超买场景，需配合 genuine 过热换手）
DISTRIBUTION_ACCUM_PULLBACK = 15.0  # 冲高回落场景的累计涨幅下限（原 10 → 15，提升确信度）
DISTRIBUTION_VOL_RATIO = 2.5        # 量比阈值（放量滞涨场景，原 2.0 → 2.5，需更明确放量）
# 滞涨判定用带宽阈值避免闪烁：today_pct 在 1.0% 附近震荡时不应反复触发/消失。
# 0.5% 以下才算明确滞涨（1.0%~0.5% 为过渡区，不触发）。
DISTRIBUTION_TODAY_PCT_LOW = 0.5
DISTRIBUTION_OPENING_STRONG = 4.0   # 开盘强度阈值（冲高回落场景，opening_score 范围 -5~5）
# 分时走弱判定用负带宽避免闪烁：intraday_score 在 0 附近震荡时不应反复触发/消失。
# intraday_score 范围 -10~10，0 只是中性，<-1.0 才算明确分时转弱。
DISTRIBUTION_INTRADAY_WEAK = -1.0
# 主力出货 Rule 2 的换手率门槛：要求"真正过热"而非单纯活跃。
# enhancer 中以 c.turnover_bonus < 0 判定（turnover_rate > TURNOVER_HIGH=20%，即派发级过热）。

# ── 涨幅过大风险标签阈值 ──
# 累计涨幅超过此值时标记"涨幅过大"，提示追高风险
OVERVALUED_ACCUM_THRESHOLD = 25.0

# Trend-label hard filter: exclude trends with avg next-day return < -2%
# Based on 2729 historical recommendations analysis
# Only includes labels actually produced by current analysis.py
HIGH_RISK_TRENDS: set[str] = {
    # "缩量回调" 已移除：avg -2.09% win 39.2% 在候选池场景可接受，
    # 保留 MA 支撑 + 小幅回调的合理 pullback 候选。
    "回踩整理",   # pullback: avg -3.89%, win 21.6%
}

# ── 风险标签硬排除集合 ──
# 命中即直接从所有推荐列表移除（推荐输出只保留可买票）。
# 仅纳入"卖出/止损"级信号：
#   - 主力出货：高位派发，明确的卖出信号
#   - 趋势破位：MA 破位，止损信号
# 其余标签保留为展示型警告（不在此过滤）：
#   - 超买：上下文语义（仅 short_term 条件性否决，其余策略展示）
#   - 涨幅过大 / 疲劳 / 弱市：追高/后劲不足/大盘环境提示
#   - 量价背离：含轻度负面（回踩却不缩量），不足以单独排除
RISK_FLAGS_HARD_FILTER: set[str] = {
    "主力出货",
    "趋势破位",
}

# Time-based bonus thresholds (minutes since midnight)
STALE_TIMEOUT_MINUTES = 30  # 掉榜后保留时长（进程内 today_pool 掉榜保留，与 watch_pool 无关）

# ── 回马枪（掉榜跟踪）— 独立策略桶（2026-08-07 新增）──
# 背景：候选池由"当次热榜"驱动，掉榜超跌股（如志特新材 07-09→07-31 三周掉榜）完全不可见，
# 反弹企稳日（07-14~07-30）无法被 rebound 评估。回马枪补上这块盲区。
# 评估域 = watch_pool（上过榜的 GEM 股）∪ 近 N 日推荐，减去今日在榜票；
# 每票每日最多评估一次（last_eval_date 落库，重启不丢）。
WATCH_POOL_MAX = 600                 # 掉榜跟踪池上限（超限时淘汰 last_list_date 最旧）
WATCH_OFFLIST_KEEP_DAYS = 15         # 掉榜后保留交易日数（覆盖三周级掉榜，见志特新材案例）
COMEBACK_MIN_SCORE = 18              # 回马枪最低分（反转变体复用 rebound 阈值）
# 反转变体：超跌企稳（复用 analyze_rebound 语义，off_list 收紧）
COMEBACK_MIN_TODAY_PCT = 2.0         # off_list 今日涨幅下限（比 rebound 0.5 更严，反转确认）
COMEBACK_MAX_TODAY_PCT = 12.0        # 与 short_term 上限同源：覆盖 8-12% 续涨（掉榜日无热榜背书）
COMEBACK_PREFILTER_5D_DROP = -8.0    # 5日累计跌幅≤此值才补拉当日 bar（成本预过滤，从~600降到数十/日）
COMEBACK_POS_DIMS = 3                # off_list 交叉验证维度下限（榜上为2，掉榜无热榜背书更严）
# 回踩变体（吸收原历史推荐跟踪 tracker）：近 N 日推荐回调到买点 → 二次上车
COMEBACK_REENTRY_DAYS = 5            # 回踩跟踪窗口（交易日）
COMEBACK_REENTRY_BASE_SCORE = 40     # 回踩基础分（每命中一个买点信号 +15）
COMEBACK_REENTRY_SIGNAL_SCORE = 15
COMEBACK_REENTRY_FILTER_TODAY_HIGH = 5.0    # 今日涨幅≥此值 → 过滤（不追高）
COMEBACK_REENTRY_FILTER_TODAY_LOW = -5.0    # 今日跌幅≤此值 → 过滤（可能破位）
COMEBACK_REENTRY_FILTER_CUM_HIGH = 10.0     # 累计收益≥此值 → 过滤（已错过）
COMEBACK_REENTRY_FILTER_CUM_LOW = -10.0     # 累计收益≤此值 → 过滤（信号失效）
# 资金流硬过滤：主力净占比 ≤ -5% → 剔除（回调可能是出货）；无当日数据 → 保留（视同中性）
COMEBACK_REENTRY_FUND_FLOW_LOW = FUND_FLOW_MAIN_PCT_WEAK  # 与评分扣分档同源，避免阈值漂移
# 买点信号阈值（满足条件计 1 分，信号数决定状态分类）
COMEBACK_REENTRY_MA20_SUPPORT_PCT = 3.0     # |close-MA20|/MA20 < 此值 且 MA20 上行 → MA20 支撑
COMEBACK_REENTRY_VOL_SHRINK_RATIO = 0.8     # vol_ratio < 此值 → 缩量回调
COMEBACK_REENTRY_RSI_LOW = 30               # RSI 合理区下限
COMEBACK_REENTRY_RSI_HIGH = 50              # RSI 合理区上限（回落但不超卖）
COMEBACK_REENTRY_BOLL_MID_PCT = 3.0         # 距 BOLL 中轨±此值内 → 位置合理
COMEBACK_REENTRY_MA20_SLOPE_MIN = 0.5       # MA20 日涨幅>此值 → 上行（百分比）
COMEBACK_REENTRY_STATUS_BUY = 4             # 信号数≥此值 → "到买点"
# 观察中门槛由 2 提到 3：原 2 个信号极易由同源指标（MA20支撑/未破位/BOLL中轨
# 三者本质都是"价格在均线附近"）一次凑齐，导致大量横盘票涌入观察列表、噪声过大。
COMEBACK_REENTRY_STATUS_WATCH = 3           # 信号数≥此值 → "观察中"，否则 "未到买点"（过滤）
COMEBACK_REENTRY_DISPLAY_BUY_MAX = 10       # "到买点"最多显示条数
COMEBACK_REENTRY_DISPLAY_WATCH_MAX = 0      # "观察中"补充最多显示条数（0 = 不显示，只看到买点）
# 回马枪独立区仅作"无推荐兜底参考"：主区（榜上五类）有推荐时不展示回马枪，避免刷屏；
# 且只在主区为空时最多显示前 N 条（回马枪为掉榜无热榜背书票，评分语义弱于榜上推荐）。
COMEBACK_DISPLAY_MAX = 10                   # 回马枪区最多显示条数

# 辨识度标签 — 反复上榜
PROMINENCE_LOOKBACK_DAYS = 5     # 回溯 N 个交易日
PROMINENCE_REPEAT_THRESHOLD = 3  # 出现 ≥ N 天 → "↻"
PROMINENCE_MAX_AVG_RANK = 70    # 近 N 日平均排名 ≤ 此值

EARLY_TRADE_CUTOFF = 10 * 60 + 30   # 10:30
LATE_TRADE_START = 14 * 60           # 14:00
# -5→-2：原 -5 会把刚过 MIN_SCORE 的早盘票压到门槛下，导致 9:30-10:30 票迟迟不推。
# -2 保留早盘噪音轻度抑制，但不再系统性压杀早盘异动。
EARLY_BONUS = -2
LATE_BONUS = 3

HOLIDAYS_FILE = os.path.join(BASE_DIR, "holidays.json")

_HOLIDAYS_FALLBACK: set[str] = {
    "2025-01-01",                              # 元旦
    "2025-01-28", "2025-01-29", "2025-01-30", "2025-01-31",  # 春节
    "2025-02-01", "2025-02-02", "2025-02-03", "2025-02-04",  # 春节
    "2025-04-04", "2025-04-05", "2025-04-06",                # 清明
    "2025-05-01", "2025-05-02", "2025-05-03", "2025-05-04", "2025-05-05",  # 劳动节
    "2025-05-31", "2025-06-01", "2025-06-02",                # 端午
    "2025-10-01", "2025-10-02", "2025-10-03",                # 国庆
    "2025-10-04", "2025-10-05", "2025-10-06", "2025-10-07", "2025-10-08",  # 国庆
    "2026-01-01",                              # 元旦
    "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20",  # 春节
    "2026-02-21", "2026-02-22", "2026-02-23", "2026-02-24",  # 春节
    "2026-04-05", "2026-04-06",                               # 清明
    "2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04", "2026-05-05",  # 劳动节
    "2026-06-19", "2026-06-20", "2026-06-21",                # 端午
    "2026-09-30",                          # 国庆前 (调休)
    "2026-10-01", "2026-10-02", "2026-10-03",                # 国庆
    "2026-10-04", "2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08",  # 国庆
}


def _load_holidays_from_file(path: str) -> set[str] | None:
    try:
        import json
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return set(data)
        return None
    except (FileNotFoundError, json.JSONDecodeError, TypeError):
        return None


HOLIDAYS: set[str] = _load_holidays_from_file(HOLIDAYS_FILE) or _HOLIDAYS_FALLBACK

# == Cross-validation weights ==
# New face
V_NF_CONVERGE_STRONG = 13
V_NF_CONVERGE_PARTIAL = 8
V_NF_HL_CLEAR = 5
V_NF_HL_STABLE = 2
V_NF_HL_FAIL = -5
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

# Pullback
V_PB_MA_UP = 10
V_PB_MA_FLAT = 0
V_PB_MA_DOWN = -10
V_PB_SHRINK_YES = 10
V_PB_SHRINK_MOD = 5
V_PB_SHRINK_NO = -5

# 回踩量能比阈值（分析端与验证端共用，避免口径矛盾）
#   vol_ratio < PULLBACK_VOL_LOW        -> 极度缩量（确认）
#   vol_ratio <= PULLBACK_VOL_HEALTHY   -> 健康缩量
#   vol_ratio > PULLBACK_VOL_HIGH       -> 放量（非缩量，惩罚）
# 中间带 (HEALTHY, HIGH] 视为中性，两端一致。
PULLBACK_VOL_LOW = 0.4
PULLBACK_VOL_HEALTHY = 0.9
PULLBACK_VOL_HIGH = 1.3
V_PB_SECTOR_HOT = 8
V_PB_SECTOR_DEAD = -5
V_PB_SECTOR_NEUTRAL = 0

# New face — added in P0
V_NF_DIVERGENCE_BULL = 8
V_NF_VOLUME_CONFIRM = 5

# Pullback — added in P0
V_PB_BOLLINGER_TOUCH = 5
V_PB_BOLLINGER_MID = 2

# Short term
V_ST_VOL_HEALTHY = 8
V_ST_VOL_SURGE = 12
V_ST_SECTOR_HOT = 10
V_ST_SECTOR_WARM = 5
V_ST_SECTOR_COLD = 0
V_ST_RANK_TOP10 = 8
V_ST_RANK_TOP20 = 5
V_ST_RANK_TOP30 = 2
V_ST_RANK_LOW = -3
V_ST_MA_SUPPORT = 5
V_ST_MA_BROKEN = -5

# Pullback 20-day gain penalty (late-stage lifecycle protection)
# PULLBACK_20D_GAIN_WARN/EXTREME 同时用于 validator 超买判定阈值。
PULLBACK_20D_GAIN_WARN = 40       # 20-day gain > 40% → warn
PULLBACK_20D_GAIN_EXTREME = 60    # 20-day gain > 60% → extreme
PULLBACK_20D_WARN_PENALTY = -10
PULLBACK_20D_EXTREME_PENALTY = -15

# ── 综合排序展示优先级与操作建议（2026-08-07 复核重排）──
# CAT_DISPLAY_PRIORITY：仅决定综合排序「类别分组展示顺序」，与操作建议解耦。
# 校准依据（recommendations 历史 cum_3d / next_day 双口径 + 全期/近期双窗口）：
#   - 全期（库内约60个交易日）：rebound(+4.74) > momentum(+2.58) > short_term(-0.82)
#     > known_new_face(-0.44) > new_face(-1.61) > pullback(-7.14, 已下线)
#   - 近30天 cum_3d：rebound(+4.74) > short_term(-0.82) > new_face(-4.07)
#     > momentum(-4.92) ≈ known_new_face(-4.92) > pullback(-7.44)
#   - 近30天 next_day：rebound(+3.45) > known_new_face(+0.87) > short_term(-0.58)
#     > new_face(-0.98) > momentum(-1.20) > pullback(-5.63)
# 2026-08-07 调整：short_term 上移至 1（两口径均稳定、IC 正效、近30天唯一接近打平）；
# momentum 由 1 下调至 2（近30天 cum_3d -4.92 垫底且 next_day 亦负，动量策略弱市天然脆弱；
# 但全期 +2.58 仍居第 2，故只下调一位折中，不按单一近期窗口过度反应）。
# known_new_face 维持 3：next_day 近期 +0.87 系"次日冲高"，cum_3d -4.92 为 3 日高开低走，
# 且 score IC 反指（-0.134/-0.179 双口径），类别内分数不可靠，不置顶。
# 用 `python -m scanner.backtest --ranking` 校准此顺序，人工复核后更新。
CAT_DISPLAY_PRIORITY = {
    "known_new_face": 4, "rebound": 0, "new_face": 5,
    "momentum": 3, "short_term": 2, "pullback": 6,
    "comeback": 1,
}

# SUGGEST_BY_CAT：操作建议按类别独立映射（与优先级解耦，语义不变）。
# 值含 ANSI 颜色码，由 display 端渲染；排序位置变化不影响建议。
# 2026-08-10: known_new_face 由「推荐」改「超短」——next_day +0.91 但 cum_3d -0.44
# （3 日高开低走回吐），且分数反指，正确操作是次日卖而非持有 3 日。
SUGGEST_BY_CAT = {
    "known_new_face": "\033[91m超短\033[0m",
    "rebound": "\033[96m推荐\033[0m",
    "new_face": "参考",
    "momentum": "参考",
    "short_term": "\033[91m超短\033[0m",
    "pullback": "\033[91m回避\033[0m",
    "comeback": "\033[96m回马\033[0m",
}

# ── 次日大涨候选独立区（display-only，2026-08-10）──
# 依据 scanner.nextday_attribution（去重 1006 条，next_day≥7% hit 10.2%）：
#   - 涨幅带甜蜜区：推荐时刻盘中涨幅 <2%（低吸潜伏 hit 11.7%/13.2%）与 4~8%
#     （中段启动 hit 11.8%）；2~4% 是死区（6.2%）、8~10% 是陷阱（7.5%，平均 -1.42%）。
#   - score 低分反指（<30 桶 hit 16.7% vs 70-90 桶 7.4%）。
#   - short_term 超买是死亡信号（hit 5% vs 非超买 10.5%）。
# 本区只筛出"形态符合次日大涨画像"的票：综合排序行尾 🎯 标记 + 档位置顶（display._sort_tier 档0，
# 2026-08-12 与辨识度一起置顶），不改 score / 不落库。
NEXTDAY_SPIKE_SWEET_MIN = 0.0     # 低吸潜伏带下限（推荐时刻盘中涨幅）
NEXTDAY_SPIKE_SWEET_LOW = 2.0     # 低吸潜伏带上限（<2%）
NEXTDAY_SPIKE_MID_MIN = 4.0       # 中段启动带下限
NEXTDAY_SPIKE_MID_MAX = 8.0       # 中段启动带上限（<8%，排除 8-10% 陷阱）
# 分类别展示优先：rebound（hit 32%）> short_term（弱转强 11.8%）> momentum（MA3头 11.8%）
# > known_new_face > new_face。comeback 无 hit 不入区。
NEXTDAY_CAT_PRIORITY = {
    "rebound": 0, "short_term": 1, "momentum": 2,
    "known_new_face": 3, "new_face": 4,
}
