# scanner/config_tactics.py — 盘中操作纪律（intraday tactics）参数（原 config.py 的一部分）
# 2026-09-13 从 config.py 拆出。定义集中在此，由 scanner/config.py 统一 re-export，
# 保持 `from scanner.config import ...` 导入路径不变。本模块只依赖 config_core（叶子模块）。

from scanner.config_core import _env_flag  # noqa: F401

# ── 盘中操作纪律（intraday tactics，2026-08-31）──
# 12 条操盘纪律的参数化阈值。纯展示层，不参与评分/排序/落库。
# 开关：环境变量 RTS_ENABLE_INTRADAY_TACTICS=0 可关闭。
ENABLE_INTRADAY_TACTICS = _env_flag("RTS_ENABLE_INTRADAY_TACTICS", True)

# ── 时段窗口（minutes since midnight，用于 session_advice）──
TACTICS_MORNING_SPIKE_START = 570  # 09:30
TACTICS_SELL_WINDOW_START = 585  # 09:45 → rule 8 卖出黄金窗口
TACTICS_SELL_WINDOW_END = 600  # 10:00 → rule 8 卖出黄金窗口
TACTICS_TOP_WINDOW_1_END = 573  # 09:33 → rule 9 冲高见顶段（早盘）
TACTICS_TOP_WINDOW_2_START = 800  # 13:20 → rule 9 冲高见顶段（午后）
TACTICS_TOP_WINDOW_2_END = 810  # 13:30
TACTICS_LIMITUP_STRONG_END = 600  # 10:00 → rule 10 强势股封板截止
TACTICS_LIMITUP_WEAK_START = 840  # 14:00 → rule 10 弱股封板截止
TACTICS_LOWBUY_START = 870  # 14:30 → rule 4 低吸窗口
TACTICS_LOWBUY_END = 885  # 14:45
TACTICS_TAIL_DIVE_START = 870  # 14:30 → rule 5 尾盘跳水判定

# ── 状态阈值（用于 stock_actions）──
TACTICS_HIGH_OPEN_REDUCE_PCT = 5.0  # rule 2：高开≥此值且封不住板 → 减半仓
TACTICS_FLAT_OPEN_LOW = -1.0  # rule 3：平开区间下限（%）
TACTICS_FLAT_OPEN_HIGH = 1.0  # rule 3：平开区间上限（%）
TACTICS_STEADY_RISE_MINS = 20  # rule 3：平开后观察稳步走高分钟数
TACTICS_TAIL_DIVE_PCT = 2.0  # rule 5：尾盘跳水幅度（从日内高点回落≥此值）
TACTICS_SHRINK_VOL_RATIO = 0.7  # rule 7：缩量判定（当前量比 < 此值）
TACTICS_AM_NOT_OVER_HIGH_MINS = 810  # rule 7：午盘段起点（13:30 后看冲高回落）
TACTICS_MIDDAY_WINDOW_END = 870  # rule 7：午盘段终点（14:30，与尾盘跳水段衔接）
TACTICS_LIMITUP_WINDOW_MINS = 30  # rule 6：14:00 涨停落袋窗口长度（14:00-14:30）
TACTICS_SPIKE_REDUCE_PCT = 3.0  # rule 1：早盘冲高减仓线（现涨幅 ≥ 此值且未封板）
TACTICS_MORNING_CRASH_PCT = -3.0  # rule 12：早上大跌加仓线（现涨幅 ≤ 此值且无硬风险）
TACTICS_STEADY_RATIO_MIN = 0.6  # rule 3：稳步走高判定（爬升采样占比 ≥ 此值）
TACTICS_STEADY_VOL_RATIO = 1.0  # rule 3：量能同步判定（量比 ≥ 此值）
TACTICS_MORNING_SPIKE_MINS = 30  # rule 1/12：早盘窗口长度（09:30 起 30 分钟）
# 分时趋势摘要（intraday_fetch 第 4 相产出，写入 kline.dimensions["minute_*"]）
TACTICS_MINUTE_VOL_RECENT_BARS = 30  # 量能趋势对比的近期分钟窗口

# ── 盘中操作标签字面量（单一来源）──
# 2026-09-11 收敛：此前 4 处产生端（intraday_tactics 规则 1/2/5/6/7）与 3 处消费端
# （display.py 主表、display.py v2 池选区、final_pick.py）各自硬编码同一批 emoji 字面量，
# 共 7 份拷贝。改名需同步 7 处，且 final_pick.py 注释声称「与 display 同源」实为拷贝
# （误导）。现统一到此处，产生端与消费端全部改为引用。
# ⚠️ 这些字符串是 emoji + 中文，改动会同时影响展示与终选硬过滤语义，勿轻易调整。
TACTICS_TAG_REDUCE_HALF = "⬇减半"  # rule 2：高开≥5% 但封不住板
TACTICS_TAG_REDUCE = "⬇减仓"  # rule 1/7：早盘冲高 / 午盘冲高回落+缩量
TACTICS_TAG_ADD = "⬆加仓"  # rule 3/12：平开稳步走高 / 早上大跌无硬风险
TACTICS_TAG_NO_CHASE = "🔻勿接"  # rule 5：14:30 后尾盘跳水
TACTICS_TAG_TAKE_PROFIT = "💰落袋"  # rule 6/10：14:00-14:30 涨停

# 减仓类纪律标签集合（卖出信号）：命中即被三处硬过滤剔除
# —— display 主表 / display v2 池选区 / final_pick 终选。语义为「回避」，
# 不含 TACTICS_TAG_ADD（加仓是买点信号，方向相反）。
TACTICS_SELL_TAGS: frozenset[str] = frozenset(
    {
        TACTICS_TAG_REDUCE,
        TACTICS_TAG_REDUCE_HALF,
        TACTICS_TAG_NO_CHASE,
        TACTICS_TAG_TAKE_PROFIT,
    }
)

__all__ = [
    "ENABLE_INTRADAY_TACTICS",
    "TACTICS_MORNING_SPIKE_START",
    "TACTICS_SELL_WINDOW_START",
    "TACTICS_SELL_WINDOW_END",
    "TACTICS_TOP_WINDOW_1_END",
    "TACTICS_TOP_WINDOW_2_START",
    "TACTICS_TOP_WINDOW_2_END",
    "TACTICS_LIMITUP_STRONG_END",
    "TACTICS_LIMITUP_WEAK_START",
    "TACTICS_LOWBUY_START",
    "TACTICS_LOWBUY_END",
    "TACTICS_TAIL_DIVE_START",
    "TACTICS_HIGH_OPEN_REDUCE_PCT",
    "TACTICS_FLAT_OPEN_LOW",
    "TACTICS_FLAT_OPEN_HIGH",
    "TACTICS_STEADY_RISE_MINS",
    "TACTICS_TAIL_DIVE_PCT",
    "TACTICS_SHRINK_VOL_RATIO",
    "TACTICS_AM_NOT_OVER_HIGH_MINS",
    "TACTICS_MIDDAY_WINDOW_END",
    "TACTICS_LIMITUP_WINDOW_MINS",
    "TACTICS_SPIKE_REDUCE_PCT",
    "TACTICS_MORNING_CRASH_PCT",
    "TACTICS_STEADY_RATIO_MIN",
    "TACTICS_STEADY_VOL_RATIO",
    "TACTICS_MORNING_SPIKE_MINS",
    "TACTICS_MINUTE_VOL_RECENT_BARS",
    "TACTICS_TAG_REDUCE_HALF",
    "TACTICS_TAG_REDUCE",
    "TACTICS_TAG_ADD",
    "TACTICS_TAG_NO_CHASE",
    "TACTICS_TAG_TAKE_PROFIT",
    "TACTICS_SELL_TAGS",
]
