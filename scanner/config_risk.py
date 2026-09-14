# scanner/config_risk.py — 风险标签 / 硬过滤 / 排雷阈值（原 config.py 的一部分）
# 2026-09-13 从 config.py 拆出。定义集中在此，由 scanner/config.py 统一 re-export，
# 保持 `from scanner.config import ...` 导入路径不变。本模块只依赖 config_core（叶子模块）。

from scanner.config_core import _env_flag  # noqa: F401 (env 助手，config_core 为叶子模块)

# 展示层「硬信号」风险标签（display/feishu 共用）：展开文字显示；软信号折叠成 +N 角标。
# 注意与 RISK_FLAGS_HARD_FILTER（硬过滤，命中即从推荐列表移除）不同——此处仅影响展示分级。
RISK_FLAGS_DISPLAY_HARD = {"超买", "主力出货", "趋势破位"}

# ── 主力出货风险标签阈值 ──
# 满足任一复合条件即标记"主力出货"，用于识别高位派发迹象。
# 2026-07-28 收紧：原 Rule 2（高位高换手+超买）仅要求换手率>5% + 宽松超买，
# 几乎把所有活跃强势股都打成"主力出货"。改为高确信条件，且依赖已收紧的极端超买。
DISTRIBUTION_ACCUM_HIGH = 20.0  # 累计涨幅高位阈值（放量滞涨场景）
DISTRIBUTION_ACCUM_MID = 15.0  # 累计涨幅中高位阈值（高换手超买场景，需配合 genuine 过热换手）
DISTRIBUTION_ACCUM_PULLBACK = 15.0  # 冲高回落场景的累计涨幅下限（原 10 → 15，提升确信度）
DISTRIBUTION_VOL_RATIO = 2.5  # 量比阈值（放量滞涨场景，原 2.0 → 2.5，需更明确放量）
# 滞涨判定用带宽阈值避免闪烁：today_pct 在 1.0% 附近震荡时不应反复触发/消失。
# 0.5% 以下才算明确滞涨（1.0%~0.5% 为过渡区，不触发）。
DISTRIBUTION_TODAY_PCT_LOW = 0.5
DISTRIBUTION_OPENING_STRONG = 4.0  # 开盘强度阈值（冲高回落场景，opening_score 范围 -5~5）
# 分时走弱判定用负带宽避免闪烁：intraday_score 在 0 附近震荡时不应反复触发/消失。
# intraday_score 范围 -10~10，0 只是中性，<-1.0 才算明确分时转弱。
DISTRIBUTION_INTRADAY_WEAK = -1.0
# Rule 5（后排+盘中走弱）的 intraday 阈值：比 Rule 3 更严（-1.5 vs -1.0），
# 因后排边际放量票无累计涨幅背书，需更明确的分时走弱才判派发。
# 2026-08-14 数据校准：short_term 去重 218 条中该画像 12 条，next_day -1.90%/胜率25%，
# cum_3d -4.24%/胜率12%（n=8）——全历史最强负向组合（300317 珈伟新能 08-13 案例）。
# 2026-09-04 P1 复核（超额口径）：维持 Rule 5 硬过滤（并入主力出货标签）。
# 小样本（n=8~12）不满足统计惯例，但方向与保守型定位一致且条件严苛（short_term
# + rank>30 + 分时 ≤-1.5 三重叠加）；复审触发条件：scan_rejections 中该画像
# 独立样本 ≥30 且超额均值转正时降级为软警告。
DISTRIBUTION_RANK_WEAK_INTRADAY = -1.5
# ── 弱转强失效标签（2026-08-14 新增）──
# 弱转强（v_st_weak>0）当日分时明确走弱（intraday<=-1.0，与 Rule 3 冲高回落同阈值）
# → 转强失败：前日分歧/炸板今日转强失败，是次日大跌高发画像。
# 数据校准：全期 12 样本，大跌(≤-7%) 25%、大涨 8.3%、平均次日 -2.61%
# （基线 10.4% / 9.8% / -0.26%）；含 -16.34 / -18.44 两个极端日，均为弱转强+盘中弱。
# 阈值取 -1.0 而非 -1.5：两个极端日分别位于 -1.2 / -1.0，-1.5 会漏掉最坏样本。
# 2026-09-04 P1 复核（scan_rejections 回补后 11 行、6 行有次日收益）：大跌率 33%
# （2/6）vs 基线 8%，超额均值约 -1.1%——方向仍成立，维持硬过滤。但代价真实：
# SZ301591(08-26) 被杀后次日 +20%；纯弱转强失效独立样本仅 3 行。
# 复审触发条件：独立样本 ≥30 时重估；若大跌率降至基线水平则降级为软警告。
WTS_FAIL_TAG = "弱转强失效"
# 主力出货 Rule 2 的换手率门槛：要求"真正过热"而非单纯活跃。
# enhancer 中以 c.turnover_bonus < 0 判定（turnover_rate > TURNOVER_HIGH=20%，即派发级过热）。

# ── 涨幅过大风险标签阈值 ──
# 累计涨幅超过此值时标记"涨幅过大"，提示追高风险
OVERVALUED_ACCUM_THRESHOLD = 25.0
OVERVALUED_ACCUM_MOMENTUM_THRESHOLD = 30.0  # momentum 策略累计涨幅超此值 → 涨幅过大标签

# Trend-label hard filter: exclude trends with avg next-day return < -2%
# Based on 2729 historical recommendations analysis
# Only includes labels actually produced by current analysis.py
# pullback 已下线（2026-07-30），"回踩整理" 已无任何策略产出，保留为惰性防线。
HIGH_RISK_TRENDS: set[str] = {
    "回踩整理",  # (原 pullback 标签: avg -3.89%, win 21.6%)
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
FUND_RISK_FETCH_TIMEOUT = 25  # 单次问财查询限时（秒，pywencai 无内部 timeout）
FUND_RISK_TTL_SEC = 86400  # 进程/DB 缓存 TTL（基本面日级更新，当日不重复查询）
FUND_RISK_FAIL_TTL_SEC = 60  # 失败/空结果短退避（秒）：pywencai 故障期不每轮重复打 25s 限时，
# 一扫描周期后重试，避免 60s 轮循环白白等超时
FUND_RISK_TAG = "财务风险"  # 命中时打的风险标签（入 RISK_FLAGS_HARD_FILTER）
FUND_RISK_REASON = "资不抵债"  # 命中原因说明（payload 落库 + stock_report 展示）

# ── 风险标签硬排除集合 ──
# 命中即直接从所有推荐列表移除（推荐输出只保留可买票）。
# 仅纳入"卖出/止损"级信号：
#   - 主力出货：高位派发，明确的卖出信号
#   - 趋势破位：MA 破位，止损信号
#   - 财务风险：资不抵债（每股净资产<0），退市风险级，基本面硬伤
#   - 弱转强失效：弱转强当日分时明确走弱 → 转强失败（2026-08-14 新增）
#   - 当日翻绿+高开回落：高开后收阴（pool_log 实测有害，2026-09-04 新增）
# 其余标签保留为展示型警告（不在此过滤）：
#   - 超买：上下文语义（仅 short_term 条件性否决，其余策略展示）
#   - 涨幅过大 / 疲劳 / 弱市：追高/后劲不足/大盘环境提示
#   - 量价背离：含轻度负面（回踩却不缩量），不足以单独排除
RISK_FLAGS_HARD_FILTER: set[str] = {
    "主力出货",
    "趋势破位",
    FUND_RISK_TAG,
    WTS_FAIL_TAG,
    "当日翻绿+高开回落",
}

# 资金流硬过滤开关（2026-09-14）：主力净流出占比 ≤ 阈值 → 从推荐列表移除
# 默认开启，与 hot_watch/comeback/final_pick 同源阈值（FUND_OUTFLOW_NET_PCT = -8.0%）
# 关闭：RTS_FUND_FLOW_HARD_FILTER=0
FUND_FLOW_HARD_FILTER_ENABLED = _env_flag("RTS_FUND_FLOW_HARD_FILTER", True)

# ── 排雷器（池→排雷→低吸 重构 Phase 2）实证危险信号阈值 ──
# 阈值集中此处（config 单一阈值源），categories 不加新类别。
# 信号含义与 enhancer 主力出货 / validator 冲高回落口径对齐，避免双套语义漂移。
DANGER_BIAS20_MAX = 28.0  # 偏离 MA20 过大（>28%）→ 高位乖离，追高回落风险
DANGER_MAIN_OUTFLOW_PCT = -5.0  # 主力净占比(%) ≤ -5 → 主力出货派发
# 冲高回落复用 REVERSAL_OVERSHOOT_DROP=10.0（最高涨幅−收盘涨幅，口径见上）
# 当日翻绿+高开回落：open>prev_close 且 close<open（无独立阈值，布尔组合）
# 财务风险复用 FUND_RISK_TAG（资不抵债，每股净资产<0）
#
# 信号分级（2026-09-02 v2 历史回测结论）：K 线动量类信号（bias20/冲高回落/翻绿+高开
# 回落）剔除的恰是次日 hit7 更高的强势票（被剔组 hit7 13.2% vs 池内 9.0%，冲高回落
# 组 18.6%），降为软标记（进 risk_flags 展示 + pool_log 落库）不剔除；主力出货与
# 财务风险保持硬剔除。回滚杠杆：RTS_DANGER_SOFT_KLINE=0 恢复全量硬剔除。
DANGER_KLINE_SOFT = _env_flag("RTS_DANGER_SOFT_KLINE", True)

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

__all__ = [
    "RISK_FLAGS_DISPLAY_HARD",
    "DISTRIBUTION_ACCUM_HIGH",
    "DISTRIBUTION_ACCUM_MID",
    "DISTRIBUTION_ACCUM_PULLBACK",
    "DISTRIBUTION_VOL_RATIO",
    "DISTRIBUTION_TODAY_PCT_LOW",
    "DISTRIBUTION_OPENING_STRONG",
    "DISTRIBUTION_INTRADAY_WEAK",
    "DISTRIBUTION_RANK_WEAK_INTRADAY",
    "WTS_FAIL_TAG",
    "OVERVALUED_ACCUM_THRESHOLD",
    "OVERVALUED_ACCUM_MOMENTUM_THRESHOLD",
    "HIGH_RISK_TRENDS",
    "ENABLE_FUND_RISK",
    "FUND_RISK_QUERY",
    "FUND_RISK_FETCH_TIMEOUT",
    "FUND_RISK_TTL_SEC",
    "FUND_RISK_FAIL_TTL_SEC",
    "FUND_RISK_TAG",
    "FUND_RISK_REASON",
    "RISK_FLAGS_HARD_FILTER",
    "FUND_FLOW_HARD_FILTER_ENABLED",
    "DANGER_BIAS20_MAX",
    "DANGER_MAIN_OUTFLOW_PCT",
    "DANGER_KLINE_SOFT",
    "REVERSAL_TURNED_RED_DROP",
    "REVERSAL_OVERSHOOT_DROP",
]
