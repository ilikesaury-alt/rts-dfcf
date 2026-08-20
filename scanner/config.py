import os
from datetime import datetime, timedelta, timezone
from datetime import time as dtime

# 子模块 re-export：权重表（scanner/weights.py）与交易日历（scanner/holidays.py）
# 拆出后由 config 统一导出，保持既有 `from scanner.config import ...` 导入路径不变。
from scanner.holidays import HOLIDAYS, HOLIDAYS_FILE  # noqa: F401  (re-export)
from scanner.weights import (  # noqa: F401  (re-export)
    MOMENTUM_WEIGHTS,
    NEW_FACE_WEIGHTS,
    REBOUND_WEIGHTS,
    SHORT_TERM_WEIGHTS,
)

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

# 分时兜底全阶段总预算（秒，2026-08-17 审查新增）：K 线补拉失败时的分时今日 bar 兜底
# （_merge_minute_today_bar）对每只补拉失败票 join(TODAY_BAR_MINUTE_TIMEOUT=8s) 串行，
# 单只限时存在但 N 只串行叠加无总量上限——API 整体故障时 100 只 × 8s = 800s 停滞，
# 违反"单轮扫描有界"承诺（KLINE_FETCH_DEADLINE 只包住拉取阶段，不含兜底阶段）。
# 给整个兜底阶段设总预算，超时即停止剩余票兜底（维持旧缓存回退），与
# KLINE_FETCH_DEADLINE / MINUTE_FETCH_PHASE_DEADLINE 的限时语义对齐。
MINUTE_FALLBACK_PHASE_DEADLINE = 30

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
REBOUND_CRASH_THRESHOLD = -10.0      # 前5日内至少一日跌幅 ≤ 此值（有暴跌日额外加分）
REBOUND_5D_DROP_THRESHOLD = -10.0    # 前5日累计跌幅 ≤ 此值即进入 rebound 评估
                                      # -10~-15% 无暴跌日 = 阴跌企稳场景（P0-1 修复）
REBOUND_NEAR_LOW_PCT = 0.10          # 收盘距20日低点 ≤ 此比例

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

# Peak volume ratio scoring (analysis.py hardcoded values migrated)
VOL_PEAK_NEW_FACE_PENALTY = -5   # new_face vol_peak < threshold → penalty
VOL_PEAK_MOMENTUM_PENALTY = -8   # momentum vol_peak < threshold → penalty

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

# 安全：webhook URL 含 token，已泄露于 git 历史（应轮换飞书机器人并改用环境变量注入）
FEISHU_WEBHOOK = os.environ.get("RTS_FEISHU_WEBHOOK",
                                "https://open.feishu.cn/open-apis/bot/v2/hook/"
                                "d0caf1dd-54b6-4b86-b83d-861e4c79afda")
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
# 概念拉取阶段总限时（秒）：首次/DB 过期时全量补拉，接口挂起时最坏 ceil(N/8)×8s
# 无上限（2026-08-20 修复）。与 KLINE_FETCH_DEADLINE 同族：保证单轮扫描有界。
CONCEPT_FETCH_PHASE_DEADLINE = 30
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

# Scoring weights — 已拆至 scanner/weights.py（顶部 re-export），此处保留 section 注释。
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

# 超短末周期（鱼尾段）超买防护：validator 单点判断阈值。
# 20日涨幅阈值复用 PULLBACK_20D_GAIN_EXTREME（超买判定常量，与 pullback 策略本身无关）。
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
# 熔断：连续短时崩溃达到阈值即停止自动重启，避免"确定性逻辑 bug → 无限重启
# → 每轮重复污染 DB"的死循环。区分两类失败：
#  - 启动/早期崩溃（uptime < 基线）：多为确定性导入/建连/逻辑 bug，重启也必崩，应熔断等人修；
#  - 长跑后崩溃（uptime >= 基线）：多为偶发 I/O/网络故障，应继续容忍重启。
SUPERVISE_CRASH_BASELINE_SECONDS = 120  # uptime < 此秒数视为"启动/早期崩溃"
SUPERVISE_MAX_CONSECUTIVE_CRASHES = 5   # 连续短崩溃达此数 → 熔断停止自动重启
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
# Rule 5（后排+盘中走弱）的 intraday 阈值：比 Rule 3 更严（-1.5 vs -1.0），
# 因后排边际放量票无累计涨幅背书，需更明确的分时走弱才判派发。
# 2026-08-14 数据校准：short_term 去重 218 条中该画像 12 条，next_day -1.90%/胜率25%，
# cum_3d -4.24%/胜率12%（n=8）——全历史最强负向组合（300317 珈伟新能 08-13 案例）。
DISTRIBUTION_RANK_WEAK_INTRADAY = -1.5
# ── 弱转强失效标签（2026-08-14 新增）──
# 弱转强（v_st_weak>0）当日分时明确走弱（intraday<=-1.0，与 Rule 3 冲高回落同阈值）
# → 转强失败：前日分歧/炸板今日转强失败，是次日大跌高发画像。
# 数据校准：全期 12 样本，大跌(≤-7%) 25%、大涨 8.3%、平均次日 -2.61%
# （基线 10.4% / 9.8% / -0.26%）；含 -16.34 / -18.44 两个极端日，均为弱转强+盘中弱。
# 阈值取 -1.0 而非 -1.5：两个极端日分别位于 -1.2 / -1.0，-1.5 会漏掉最坏样本。
WTS_FAIL_TAG = "弱转强失效"
# 主力出货 Rule 2 的换手率门槛：要求"真正过热"而非单纯活跃。
# enhancer 中以 c.turnover_bonus < 0 判定（turnover_rate > TURNOVER_HIGH=20%，即派发级过热）。

# ── 涨幅过大风险标签阈值 ──
# 累计涨幅超过此值时标记"涨幅过大"，提示追高风险
OVERVALUED_ACCUM_THRESHOLD = 25.0

# Trend-label hard filter: exclude trends with avg next-day return < -2%
# Based on 2729 historical recommendations analysis
# Only includes labels actually produced by current analysis.py
# pullback 已下线（2026-07-30），"回踩整理" 已无任何策略产出，保留为惰性防线。
HIGH_RISK_TRENDS: set[str] = {
    "回踩整理",   # (原 pullback 标签: avg -3.89%, win 21.6%)
}

# ── 基本面风险过滤（pywencai 问财条件查询，2026-08-12 新增）──
# 定位：排除式过滤器（filter），不做评分加分。本项目历史反复证明加分类因子
# 最终都反指被归零（资金流加分、validation_bonus、辨识度加分），而排除类
# （资不抵债/退市风险）是纯规避语义，与现有硬过滤（主力出货/趋势破位）同架构。
# 数据源：同花顺问财 pywencai（lazy import，未安装/失败自动返回空集，fail-open）。
# 查询方式：反向条件查询一次返回全市场命中集合（实测"每股净资产小于0"→42只，
# 其中 GEM 10 只），比逐票拉取稳定（实测批量单票查询丢代码/返回无关数据）。
ENABLE_FUND_RISK = _env_flag("RTS_ENABLE_FUND_RISK", True)  # 总开关
FUND_RISK_QUERY = "每股净资产小于0"  # 问财条件查询语句（资不抵债=退市风险级）
FUND_RISK_FETCH_TIMEOUT = 25         # 单次问财查询限时（秒，pywencai 无内部 timeout）
FUND_RISK_TTL_SEC = 86400            # 进程/DB 缓存 TTL（基本面日级更新，当日不重复查询）
FUND_RISK_FAIL_TTL_SEC = 60          # 失败/空结果短退避（秒）：pywencai 故障期不每轮重复打 25s 限时，
                                     # 一扫描周期后重试，避免 60s 轮循环白白等超时
FUND_RISK_TAG = "财务风险"           # 命中时打的风险标签（入 RISK_FLAGS_HARD_FILTER）
FUND_RISK_REASON = "资不抵债"        # 命中原因说明（payload 落库 + stock_report 展示）

# ── 风险标签硬排除集合 ──
# 命中即直接从所有推荐列表移除（推荐输出只保留可买票）。
# 仅纳入"卖出/止损"级信号：
#   - 主力出货：高位派发，明确的卖出信号
#   - 趋势破位：MA 破位，止损信号
#   - 财务风险：资不抵债（每股净资产<0），退市风险级，基本面硬伤
#   - 弱转强失效：弱转强当日分时明确走弱 → 转强失败（2026-08-14 新增）
# 其余标签保留为展示型警告（不在此过滤）：
#   - 超买：上下文语义（仅 short_term 条件性否决，其余策略展示）
#   - 涨幅过大 / 疲劳 / 弱市：追高/后劲不足/大盘环境提示
#   - 量价背离：含轻度负面（回踩却不缩量），不足以单独排除
RISK_FLAGS_HARD_FILTER: set[str] = {
    "主力出货",
    "趋势破位",
    FUND_RISK_TAG,
    WTS_FAIL_TAG,
}

# Time-based bonus thresholds (minutes since midnight)

# 推荐后快速反转移出（2026-08-13）：今日已推荐（榜上主类别，不含回马枪跟踪池）且当前不在
# 候选池的票，命中以下任一条件即视为推荐失败，标 excluded=1 移出综合排序展示（保留落库记录）：
#   **回落幅度口径**：drop = ref − live，ref 优先取「当日最高涨幅 high_pct」（行情 API 的
#   high/昨收 计算），缺失时回退推荐时刻涨幅——以最高点为锚衡量"动量从峰值衰减"，不受推荐
#   时刻择时影响。
#   ① REVERSAL_TURNED_RED_DROP=5.0：已转负（live<0）且 drop ≥ 5——滤掉高位仅小幅回落就微幅
#      翻绿的噪音；
#   ② REVERSAL_OVERSHOOT_DROP=10.0：drop ≥ 10，**无论红绿**——从最高点大幅回吐即使未转负也
#      "不敢买"（如从 +12% 高点回落到 +2%，动量已破）。
# 阈值来源（2026-08-13 历史数据校准，非单票凑参）：全量推荐「当日最高涨幅−收盘涨幅」回落分布
# p50=2.58 / p75=4.49 / p90=7.92 / p95=10.54 → 路①取 5（p75 之上）、路②取 10（≈p95，前 5% 异常
# 回吐）。教训（2026-08-13 三次修正）：① 不能以推荐时刻价为锚（推荐择时噪声大），改最高价；
# ② 阈值不可为凑单票（行云科技 最高 +12.33% → 收盘 -3.15%，从最高回落 15.48，任何 ≥10 阈值都
# 会捕获）而设；③ 从最高回落天然大于从推荐时刻回落（任何票都会从日内高点回吐），阈值必须按
# 新高分布上探，否则会成批误杀（曾"过滤掉一半"）。历史回放（路①∪路②，54 交易日）日均命中
# ~8 条、中位 2，崩盘日爆量属合理。回马枪为掉榜跟踪池（推荐时刻涨幅=企稳点），不参与自动移出。
# 仅作用于展示层，backtest/nextday_attribution 读 recommendations 不过滤。
REVERSAL_TURNED_RED_DROP = 5.0
REVERSAL_OVERSHOOT_DROP = 10.0

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
# 回马枪独立区仅作兜底参考（2026-08-12 放宽，2026-08-20 收紧隐藏条件）：主区（榜上五类）
# 推荐条数 > COMEBACK_DISPLAY_MIN_MAIN 时才隐藏回马枪（避免刷屏）；主区推荐条数 ≤ 该值
# （含为空）时补充展示，最多前 COMEBACK_DISPLAY_MAX 条（回马枪为掉榜无热榜背书票，评分语义弱于榜上推荐）。
COMEBACK_DISPLAY_MAX = 10                   # 回马枪区最多显示条数
COMEBACK_DISPLAY_MIN_MAIN = 3               # 主区推荐条数大于此值 → 隐藏回马枪区；≤ 此值 → 补充显示

# 核心方向低吸（2026-08-19，`scanner/core_themes.py` + display 独立区）：
# 大跌市中找「当前市场主线方向（核心概念）的核心股低吸」机会。纯展示层推导（DB-only，
# 零新增网络请求，不写 recommendations、不进综合排序/回测口径），作为独立区块参考。
# 方法论：① 近 N 日推荐按概念聚合「持续上榜天数 + 主题相对强度」识别核心方向；
# ② 核心方向里近期已走强的成员股（龙头属性）；③ 从近期高点健康回调（非破位）的低吸窗口。
CORE_THEME_LOOKBACK_DAYS = 10       # 识别核心方向回看交易日数
CORE_THEME_MIN_DAYS = 3             # 概念持续上榜 ≥ 此天数才算“核心方向”（吃频次）
CORE_THEME_TOP_N = 4                # 核心方向最多取前 N 个（防板块普涨刷屏）
CORE_THEME_MAX_PER_THEME = 3        # 每核心方向最多显示核心股数
CORE_THEME_MAX_TOTAL = 9            # 低吸区总显示条数上限
CORE_RUN_MIN = 0.12                 # 20日累计涨幅 ≥ 此值 → 有上涨（核心/龙头属性）
CORE_PULLBACK_MIN = -0.18           # 距20日高点回撤 ≥ 此值（更深）才可能够便宜
CORE_PULLBACK_MAX = -0.03           # 回撤 ≤ 此值（不能过早，还在尖顶附近）
CORE_NOT_OVERHEATED = 0.60          # 20日涨幅 > 此值 = 超买死亡区，排除低吸
CORE_TODAY_FLOOR = -6.0             # 今日涨幅 ≥ 此值（不追崩盘票）
CORE_MA20_BELOW_SLACK = 0.03        # 允许跌破 MA20 不超过此比例（未破位）
CORE_FLOW_FLOOR = -10.0             # 主力净占比 ≥ 此值（资金未大幅出逃）
CORE_THEME_NOISE = {"其他", ""}    # 聚合时排除的噪声概念
# 落库类别（2026-08-19）：核心方向低吸候选写入 recommendations 表的 category，
# 以便进 prevday_perf / nextday_attribution 复盘验证「主线回调低吸」假设（回马枪同款路径）。
# 与 comeback 同族（掉榜/跟踪类）：不入综合排序主表、不参与 mark_reversed 反转移出、
# 不进回马枪回踩候选域（避免跨区互换污染）。
CORE_DIP_CATEGORY = "core_dip"

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

# 交易日历 — 已拆至 scanner/holidays.py（顶部 re-export），此处保留位置标记。

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
PULLBACK_20D_GAIN_EXTREME = 60    # 20-day gain > 60% → extreme (overbought)

# ── 综合排序展示优先级与操作建议（2026-08-18 统一口径为「次日大涨」）──
# CAT_DISPLAY_PRIORITY：仅决定综合排序「类别分组展示顺序」，与操作建议解耦。
# 校准依据（scanner.nextday_attribution 去重 1184 条，next_day≥7% hit 口径，全期）：
#   rebound 28.6%/+2.78% > known_new_face 12.7%/+0.97% > momentum 10.2%/-0.74%
#   > new_face 9.6%/+0.19% > short_term 8.4%/+0.02% > pullback 5.3%/-3.74%(已下线)
#   > comeback 3.3%/+0.42%（6 维回踩买点是 cum_3d 语义，次日大涨口径全场最差）
# 2026-08-18 统一口径：此前按 cum_3d/next_day 双口径混排（comeback 第 2、short_term 第 2、
# kNF 第 4），与综合排序置顶的 🎯 次日大涨画像口径不一致；现全部按 next_day 排序——
# kNF 由 4 升 1（hit 12.7% 全场第二，🎯 子集 hit 21.2%）、short_term 由 2 降 4（整体 hit 8.4%
# 低于基准 9.7%，仅弱转强子集 10.3% 可用）、comeback 由 1 降 5（hit 3.3% 最差，但 avg +0.42
# 不亏——`backtest --ranking` 按均收益会给 comeback 排第 3，人工复核时以 hit 口径为准，
# 因为次日大涨目标是「大涨概率」而非平均收益）。
# 用 `python -m scanner.backtest --ranking --metric next_day_pct` 校准此顺序，人工复核后更新。
# 2026-08-20 收敛：类别宇宙单一事实来源见 scanner/categories.py（注册表 + 派生集合），
# 下方常量由该注册表派生并 re-export，保持既有 `from scanner.config import ...` 导入路径不变。
from scanner.categories import (  # noqa: E402, F401  (re-export)
    CAT_DISPLAY_PRIORITY,
    NEXTDAY_CAT_PRIORITY,
    SUGGEST_BY_CAT,
)

# 注意：config 不导出颜色（ANSI 在 display 层定义，避免循环依赖）；展示层从
# scanner.categories.CATEGORY_COLOR_KEYS 经 display.ANSI 解析；CAT_LABEL 由展示层直接从
# scanner.categories 导入。

# SUGGEST_BY_CAT：操作建议按类别独立映射（与优先级解耦，语义不变）。
# 值含 ANSI 颜色码，由 display 端渲染；排序位置变化不影响建议。
# 2026-08-18 统一 next_day 口径：所有建议语义 = 「次日大涨概率」排序（次日常规操作即
# 次日卖，不再区分持有 2-3 天口径）。kNF 由「超短」改「推荐」——原依据 cum_3d -0.44
# 回吐已不适用，next_day 下 kNF hit 12.7%（全场第二）理应推荐；short_term 由「超短」改
# 「参考」——整体 hit 8.4% 低于基准 9.7%，弱转强子集才高于基准。
# 具体映射值由 scanner/categories.py 注册表派生（见上文 from scanner.categories import SUGGEST_BY_CAT）。

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
# 5 日累计门槛（2026-08-14，🎯 判定新增维度）。数据（nextday_attribution kline 回放全量，
# 含推荐日口径）：推荐前 5 日累计 10~15% 档 hit 21.2%（最好）、0~3 平档仅 5.4%（全场最差）——
# 「5 日累计低=安全」是反指（平盘=无动量，累计 10%+ = 资金已连续介入的潜伏启动）。
# 甜蜜带 + 累计≥6 使 hit 从 16.5% 提升至 20.0%（new_face 15.7%→21.1%、momentum 23.7%→26.5%）；
# rebound（超跌反弹，负累计天然，hit 33.3%）与 short_term（其规律在超买/弱转强，不在此列）豁免。
NEXTDAY_ACCUM_MIN = 6.0
# 小板块共振劣后的板块规模门槛（2026-08-17，档位4级）：板块共振整体 cum_3d -2.22 全场最差，
# 但按规模分档差异大——cnt<5 hit 5.9%/均次日 -2.14%（最差，局部抱团次日兑现）、
# cnt 5-14 hit 6.7%/-0.74、cnt>=15 hit 11.0%/+0.18（接近无共振 11.2%，大板块有持续资金）。
# 只对 cnt<15 的小板块共振档位劣后（ranking._entry_tier 档3）；⚠板块普涨 文本已按用户
# 反馈下线（太扎眼），此配置仅用于排序，不渲染任何行尾文本。
SECTOR_RESONANCE_WARN_MAX = 15
NEXTDAY_CAT_PRIORITY = {
    "rebound": 0, "short_term": 1, "momentum": 2,
    "known_new_face": 3, "new_face": 4,
}
