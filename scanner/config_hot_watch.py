# scanner/config_hot_watch.py — 沪深飙升榜「极有可能大涨」独立区配置（原 config.py 的一部分）
# 2026-09-13 从 config.py 拆出。定义集中在此，由 scanner/config.py 统一 re-export，
# 保持 `from scanner.config import ...` 导入路径不变。

import os

from scanner.config_categories import YI  # noqa: F401 (市值上限复用单一 YI 单位)
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
# 与终选参考区（final_pick）的美感门同源，但独立开关控制
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
]
