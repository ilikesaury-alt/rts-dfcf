# scanner/config_sources.py — 外部数据源 / 飞书推送 / 展示层开关配置（原 config.py 的一部分）
# 2026-09-13 从 config.py 拆出。定义集中在此，由 scanner/config.py 统一 re-export，
# 保持 `from scanner.config import ...` 导入路径不变。本模块只依赖 config_core（叶子模块）。

import os

from scanner.config_core import (  # noqa: F401
    _env_flag,
    _env_float,
    _env_str,
)

# 安全：webhook URL 含 token，必须通过环境变量注入（已泄露于 git 历史，旧 token 已轮换）
FEISHU_WEBHOOK = os.environ.get("RTS_FEISHU_WEBHOOK", "")
FEISHU_KEYWORD = "lichun"
FEISHU_MIN_INTERVAL = 300  # 飞书最小推送间隔（秒），防止触发 Lark 限流
FEISHU_TOP_N = 10  # 飞书卡片精选池展示条数（去重/门控同源，勿与 build_feishu_card 双处硬编码）

# ── 行情增强数据（涨停池 AKShare + 个股资金流自实现直连 push2delay）──
# 开关：环境变量可覆盖（RTS_ENABLE_ZT_POOL / RTS_ENABLE_FUND_FLOW），0/1/false/true
ENABLE_ZT_POOL = _env_flag("RTS_ENABLE_ZT_POOL", True)  # 涨停池
ENABLE_FUND_FLOW = _env_flag("RTS_ENABLE_FUND_FLOW", True)  # 个股资金流

# 资金流 API host：默认 push2delay（与 akshare 的 push2 相同 clist API）。
# push2.eastmoney.com 在本机网络直连/代理均不可达（连接被重置），push2delay 可达
# （数据可能延迟约15分钟）。网络可直连 push2 的环境可用 RTS_FUND_FLOW_HOST 切回。
FUND_FLOW_HOST = _env_str("RTS_FUND_FLOW_HOST", "push2delay.eastmoney.com")
# 盘中刷新间隔（进程内缓存 TTL + DB 缓存盘中新鲜度共用）：扫描期内数据
# 过期后视为缺失，触发重拉，实现"盘中每 5 分钟更新"（而非全天冻结首次快照）
ZT_POOL_TTL_SEC = 300
FUND_FLOW_TTL_SEC = 300
# 资金流超时部分结果的进程缓存 TTL：仅缓存一扫描周期，下一轮重试补全缺页，
# 避免"超时拿到的部分数据"被冻结 5 分钟造成静默缺数据
FUND_FLOW_PARTIAL_TTL_SEC = 60
# 进程内缓存 TTL 收敛（P2-12，2026-08-20）：原散落 api/orchestrator/concept 模块内定义，
# 与上方 *_TTL_SEC 双份且指数缓存曾内联魔法数 60，难追溯。统一收到此处单源。
INTRADAY_CACHE_TTL_SEC = 120  # api 分时强度评分缓存（2 分钟刷新）
INTRADAY_CACHE_FAIL_TTL_SEC = 60  # api 分时拉取失败短退避
MINUTE_DATA_CACHE_TTL_SEC = 60  # api 分时数据缓存（1 分钟刷新）
INDEX_CACHE_TTL_SEC = 60  # api 大盘指数缓存（原内联 60）
CONCEPT_PROCESS_TTL_SEC = 300  # concept 进程内短 TTL（5 分钟，避免同轮重复读 DB）
KLINE_REFRESH_TTL = 120  # orchestrator K 线补拉节流间隔（刷新时机，非缓存 TTL）
# 单次拉取限时：AKShare 内部请求可能无 timeout（涨停池）或全市场分页很慢
# （资金流约 53 页，6 线程并行实测 ~17s）。限时保护 60s 扫描循环不被外部 host 挂死。
ZT_POOL_FETCH_TIMEOUT = 20  # 涨停池单次拉取上限（秒）
FUND_FLOW_FETCH_TIMEOUT = 30  # 资金流全市场分页拉取上限（秒，超时返回已收集部分）
# 资金流评分阈值（主力净流入净占比 %）
FUND_FLOW_MAIN_PCT_STRONG = 5.0  # 强流入分界（图标 ▲；仅作展示分级，不单独加分——见下方 2026-09-14 复核）
FUND_FLOW_MAIN_PCT_WEAK = -5.0  # 主力净占比 ≤-5% → 扣分
# FUND_FLOW_MAIN_PCT_EXTREME 定义见下方「风险标签阈值」——与 FUND_OUTFLOW_NET_PCT 同源，避免档位漂移
# 2026-08-10: 正向加分（原 FUND_FLOW_BONUS_STRONG）回测证实反指已删除——强流入(≥5%)组 next_day 均
# -1.13%（n=22）差于无数据基线 -0.85%：今日主力净流入与当日涨幅正相关，是追涨资金次日兑现。
# 仅保留 FUND_FLOW_BONUS_WEAK=-3 流出扣分、「资金流出」标签（规避语义）。字段仍写入 dims 供展示/归因。
#
# 2026-09-14 复核（n=22 → n=382）：上面那条「反指」结论已不成立——strong_in 次日 -0.774%，
# 好于有资金流数据的全样本 -0.880%；in 组(n=229) -1.178% 反而更差。
# 裁定：不是反指（不该给负分），但也没有证据支持「强流入 > 流入」⇒ 展示层
# ranking._fund_flow_norm 取消 strong_in 独享的 +0.5，与 in 同权 +0.3（第三处口径收口）。
FUND_FLOW_BONUS_WEAK = -3
# 风险标签阈值
FUND_OUTFLOW_NET_PCT = -8.0  # 主力净流出占比 ≤-8% → 「资金流出」标签
# 资金流图标强档阈值：与「资金流出」标签同源（负值取绝对值），避免两处分别改造成漂移
FUND_FLOW_MAIN_PCT_EXTREME = -FUND_OUTFLOW_NET_PCT
# 连板评分：连板数（今日涨停池涨停统计口径）加分/追高降权
ZT_LIANBAN_BONUS_2 = 5
ZT_LIANBAN_BONUS_3 = 8
ZT_LIANBAN_GT3_PENALTY = -5  # ≥4 板追高降权
ZT_ZHA_BAN_MIN = 1  # 炸板次数 ≥1 且今日曾涨停 → 「炸板」标签

# ── 概念板块数据源（东财 F10）─
CONCEPT_API_TIMEOUT = 8  # 单只个股概念拉取超时（秒）
CONCEPT_CACHE_TTL_DAYS = 7  # concept_cache 缓存天数（概念归属低频变动，7 天足够新鲜）
CONCEPT_MAX_FETCH_THREADS = 8  # 概念归属并行拉取线程数
# 概念拉取阶段总限时（秒）：首次/DB 过期时全量补拉，接口挂起时最坏 ceil(N/8)×8s
# 无上限（2026-08-20 修复）。与 KLINE_FETCH_DEADLINE 同族：保证单轮扫描有界。
CONCEPT_FETCH_PHASE_DEADLINE = 30
# 噪音板块黑名单：地域/风格/指数成分/涨停梯队等不反映"推动逻辑"的标签，不参与驱动概念聚合
CONCEPT_NOISE_BOARDS: set[str] = {
    "北京板块",
    "上海板块",
    "广东板块",
    "深圳板块",
    "江苏板块",
    "浙江板块",
    "深圳特区",
    "中盘成长",
    "中盘价值",
    "中盘股",
    "大盘股",
    "大盘价值",
    "大盘成长",
    "小盘股",
    "小盘价值",
    "小盘成长",
    "微盘股",
    "融资融券",
    "转融券标的",
    "深股通",
    "沪股通",
    "富时罗素",
    "MSCI中国",
    "中证500",
    "中证1000",
    "中证800",
    "中证100",
    "沪深300",
    "上证50",
    "上证A股",
    "深证成指",
    "深成500",
    "深证500",
    "创业板综",
    "创业板指",
    "科创50",
    "北证50",
    "昨日涨停",
    "昨日连板",
    "昨日连板_含一字",
    "昨日打板",
    "昨日炸板",
    "昨日二板",
    "昨日打二板以上表现",
    "连续涨停",
    "涨停股",
    "强势股",
    "股权分散",
    "股权激励",
    "股份回购",
    "高送转",
    "高股息",
    "破净",
    "破发股",
    "破发次新",
    "转债标的",
    "东方财富热股",
    "题材股",
    "百元股",
    "低价股",
    "预盈预增",
    "机构重仓",
    "基金重仓",
    "社保重仓",
    "QFII重仓",
    "注册制次新股",
    "ST股",
    "近期摘帽",
    "最近多板",
    "昨日高换手",
    "最近异动",
    "昨收新高",
    "创历史新高",
}
# 地域类板块统一按后缀"板块"排除（东财地域板块命名均以"板块"结尾，如"安徽板块"）
CONCEPT_NOISE_BOARD_SUFFIXES: tuple[str, ...] = ("板块",)

# v2 池选区展示条数（2026-09-03）：池为「榜上全量快照」（matcher 只标注不淘汰），
# 终端/飞书只渲染涨幅降序前 N 行，尾部注明总数——过滤属消费层，落库/pool_log/回测不受影响。
V2_POOL_DISPLAY_TOP = 10

# 显示层「不追涨」过滤（2026-09-04 用户决策）：主表+v2 池选区里今日实时涨幅超过该值
# 的票不再展示（只影响显示，不改评分/落库/回测）。回滚杠杆：RTS_DISPLAY_MAX_TODAY_PCT
# （脏值回退默认 8.0）。
# 2026-09-04 P1 修正：默认 14.0 → 8.0（超额口径复核，N≈1500 去重样本）。
# 推荐时刻盘中涨幅带 × 次日超额收益：
#   4-6% +0.42 / 6-8% +0.23（正常区）→ 8-10% -0.59 / 10-12% -0.88（大跌率 12.7%/14.1%，
#   全场最差陷阱带）→ 12-14% +1.83（n=13，被旧值 14 误砍的好区）。
# 8-12% 才是真陷阱（n=211），旧值 14 只砍掉了陷阱上方的噪音/好区。
DISPLAY_MAX_TODAY_PCT = _env_float("RTS_DISPLAY_MAX_TODAY_PCT", 8.0)

# ── 决策层（2026-09-04）：≤3 只「现在值得买什么」短名单，置顶渲染 ──
# 三道门：市场门（创业板指>0 且 5日>-3%，实测唯一正期望状态）→ 类别先验门
# （仅 core_dip/kNF/rebound 等正超额类别）→ 稀缺配额（全局 ≤3）。
# 空仓是合法输出。详见 scanner/decision.py 模块 docstring 与 docs/review-2026-09-04.md。
# 回滚杠杆：RTS_DECISION_LAYER=0 关闭。
DECISION_LAYER_ENABLED = _env_flag("RTS_DECISION_LAYER", True)

# 决策层分时门（2026-09-09 上线，当日双窗口实测后降级）：数据结论（
# beauty_gate_eval.py，2307 样本）——分时走弱桶（≤-3）hit 反而全场最高（11.1%/11.2%），
# 「低吸不接回落刀」对次日大涨口径被证伪，故默认关闭（仅作可选纪律）。
# 重开：RTS_DECISION_BEAUTY_INTRADAY=1。判定单源 scanner.trend_beauty。
DECISION_INTRADAY_BEAUTY_ENABLED = _env_flag("RTS_DECISION_BEAUTY_INTRADAY", False)

# ── 终选参考区（2026-09-04；2026-09-05 升级为概率+周期感知终选）──
# 与决策层互补：决策层答「现在该不该买」（门关→空仓），终选区答「若必须持仓买谁」
# （无论门开关都给结论）。评级单源复用 today_report._tier0_verdict（已回测口径），
# 排序单源用 scanner.nextday_prob（当日口径次日大涨概率，朴素贝叶斯式 odds 模型）；
# 买满 ≥2 只时按驱动概念去相关（同主题第 2 只劣后）。
# 2026-09-14：删除「momentum 负先验永禁」——那是平均超额口径，与终选排序的 hit 率
# 口径方向相反（momentum hit 10.0% > 基准 7.8%）。类别优劣一律由 base rate 如实反映。
# 纯展示层，不改评分/排序/落库。回滚杠杆：RTS_FINAL_PICK=0 关闭。
FINAL_PICK_ENABLED = _env_flag("RTS_FINAL_PICK", True)
FINAL_PICK_MAX = 3  # 终选最多 N 只（用户买入预算 1-3 只，2026-09-08 由 2 放宽为 3）
FINAL_PICK_REJECT_TOP = 4  # 落选理由最多展示条数（按概率降序取头部）
# 终选资金流过滤（2026-09-14）：主力净流出占比 ≤ 阈值 → 不进终选
# 与 hot_watch/comeback 同源阈值（FUND_OUTFLOW_NET_PCT = -8.0%）
# 1=开启（默认），0=关闭
FINAL_PICK_FUND_FLOW_FILTER = _env_flag("RTS_FINAL_PICK_FUND_FLOW_FILTER", True)

# ── 终选走势美感门（2026-09-09）：分时/日线走势「漂亮」是终选准入条件 ──
# 日线漂亮（scanner/trend_beauty.evaluate_daily_trend）= 干净上升趋势 6 硬门：
#   ① MA 多头排列 MA5>MA10>MA20 ② 近5日收盘趋势向上 ③ 无暴跌日
#   ④ 回调可控（单日跌幅小）⑤ 无长上影冲高回落 ⑥ 收盘未远离 20 日高点。
# 分时漂亮 = intraday_score ≥ INTRADAY_BEAUTY_MIN（复用盘中 analyze_intraday
#   评分，-10~10；>0 平稳走高/高位不回落，<0 冲高回落/走弱）。
# 数据缺失（日线不足/intraday_score 缺失即 0.0 默认值）fail-open 不判否——
#   终选是展示层，只拦「可判定的丑」，不因数据缺口误杀。
# 【2026-09-09 数据裁决：硬拦默认关】双窗口实测（beauty_gate_eval.py，2307 样本，
#   日线 T-1 前防前视）：放行组 hit 8.4%/0.0% vs 基线 9.8%/5.5%，双窗口同向低于
#   基线；且样本内放行仅 21/1346（1.6%）。「漂亮=稳但不爆」：放行组 avg/med 两窗
#   均高于基线（滤掉大亏）但 hit 反而低（滤掉爆发票）。对「次日大涨」目标负贡献，
#   硬拦降级；「美」展示标记保留（TREND_MARK_ENABLED）作为买入体验参考。
#   重开硬拦：RTS_FINAL_PICK_BEAUTY=1。
FINAL_PICK_BEAUTY_ENABLED = _env_flag("RTS_FINAL_PICK_BEAUTY", False)
# 走势展示标记（「美」，2026-09-09）：v1/v2 行尾 + 终选个股行尾，纯展示。
# 独立于硬拦开关——硬拦关了标记仍在（买入体验/回撤控制参考：放行组 avg 更高、
# 尾部风险更小）。关：RTS_TREND_MARK=0。
TREND_MARK_ENABLED = _env_flag("RTS_TREND_MARK", True)
INTRADAY_BEAUTY_MIN = 2.5  # intraday_score ≥ 此值判分时漂亮（-10~10）
DAILY_BEAUTY_MIN_BARS = 20  # 缓存日线少于此根数 → 无法判定（fail-open 放行）
DAILY_BEAUTY_MAX_CRASH_PCT = -5.0  # 近5日无单日跌幅 ≤ 此值的暴跌日
DAILY_BEAUTY_MAX_PULLBACK_PCT = 3.0  # 近5日单日跌幅超过此值 = 回调失控（丑）
DAILY_BEAUTY_MAX_UPPER_SHADOW = 4.0  # 近5日最大上影线（% vs 昨收）超过此值 = 冲高回落
DAILY_BEAUTY_MIN_SLOPE_PCT = 0.0  # 近5日收盘涨幅 ≥ 此值（趋势向上）
DAILY_BEAUTY_MAX_OFF_HIGH_PCT = 12.0  # 收盘距 20 日最高收盘回撤超过此值 = 破位

__all__ = [
    "FEISHU_WEBHOOK",
    "FEISHU_KEYWORD",
    "FEISHU_MIN_INTERVAL",
    "FEISHU_TOP_N",
    "ENABLE_ZT_POOL",
    "ENABLE_FUND_FLOW",
    "FUND_FLOW_HOST",
    "ZT_POOL_TTL_SEC",
    "FUND_FLOW_TTL_SEC",
    "FUND_FLOW_PARTIAL_TTL_SEC",
    "INTRADAY_CACHE_TTL_SEC",
    "INTRADAY_CACHE_FAIL_TTL_SEC",
    "MINUTE_DATA_CACHE_TTL_SEC",
    "INDEX_CACHE_TTL_SEC",
    "CONCEPT_PROCESS_TTL_SEC",
    "KLINE_REFRESH_TTL",
    "ZT_POOL_FETCH_TIMEOUT",
    "FUND_FLOW_FETCH_TIMEOUT",
    "FUND_FLOW_MAIN_PCT_STRONG",
    "FUND_FLOW_MAIN_PCT_WEAK",
    "FUND_FLOW_BONUS_WEAK",
    "FUND_OUTFLOW_NET_PCT",
    "FUND_FLOW_MAIN_PCT_EXTREME",
    "ZT_LIANBAN_BONUS_2",
    "ZT_LIANBAN_BONUS_3",
    "ZT_LIANBAN_GT3_PENALTY",
    "ZT_ZHA_BAN_MIN",
    "CONCEPT_API_TIMEOUT",
    "CONCEPT_CACHE_TTL_DAYS",
    "CONCEPT_MAX_FETCH_THREADS",
    "CONCEPT_FETCH_PHASE_DEADLINE",
    "CONCEPT_NOISE_BOARDS",
    "CONCEPT_NOISE_BOARD_SUFFIXES",
    "V2_POOL_DISPLAY_TOP",
    "DISPLAY_MAX_TODAY_PCT",
    "DECISION_LAYER_ENABLED",
    "DECISION_INTRADAY_BEAUTY_ENABLED",
    "FINAL_PICK_ENABLED",
    "FINAL_PICK_MAX",
    "FINAL_PICK_REJECT_TOP",
    "FINAL_PICK_BEAUTY_ENABLED",
    "FINAL_PICK_FUND_FLOW_FILTER",
    "TREND_MARK_ENABLED",
    "INTRADAY_BEAUTY_MIN",
    "DAILY_BEAUTY_MIN_BARS",
    "DAILY_BEAUTY_MAX_CRASH_PCT",
    "DAILY_BEAUTY_MAX_PULLBACK_PCT",
    "DAILY_BEAUTY_MAX_UPPER_SHADOW",
    "DAILY_BEAUTY_MIN_SLOPE_PCT",
    "DAILY_BEAUTY_MAX_OFF_HIGH_PCT",
]
