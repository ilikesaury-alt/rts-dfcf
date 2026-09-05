"""次日大涨概率估计（决策级终选排序单源，2026-09-05 新增）。

背景：终选区（scanner.final_pick）此前排序键是「verdict 星级 → 🎯 → score」——
verdict 是粗粒度整数、score 跨类别尺度不可比。在 ≤2 只的真实买入预算下，
「池内谁更可能次日大涨」需要连续可比的排序量。

模型：朴素贝叶斯式 odds 乘积（log-odds 可加）
  P = σ( logit(base) + SHRINK × Σ log(OR_i) )
base 按类别取当日口径命中率；OR 为因子条件命中率对参照组的 odds ratio。

校准来源（2026-09-05，1786 去重样本，threshold≥7%，load_attribution_rows 统一口径
= excluded=0 + (date,symbol) 去重取最后一轮）：
  - 类别 base / 辨识度 / 超买 / 弱转强 / 涨幅带：`python -m scanner.nextday_attribution`
  - 🎯 复合画像：marked 13.6% (n=543) vs unmarked 5.8% (n=794) → OR≈2.6。
    注意 docstring/旧注释里的「甜蜜带+累计≥6 hit 20%」是更小样本期的读数，已失效。
  - 主力流出≤-8%：1.5% (n=66) vs 资金流可得子集 4.6% → OR≈0.32
  - 小板块共振（cnt<15）：5.4% (n=558) vs 无共振 9.0% → OR≈0.58
不引入任何未在该口径下验证的因子；样本 <MIN_SAMPLE(20) 或字段存在率混杂的候选因子
一律不纳入（过热 n=6 / am_high 存在率仅 5% 且方向与维度归因矛盾 / rank_trend_bonus
n=26 贴门槛且与辨识度相关）。

防重复计费：🎯 复合画像本身 = 甜蜜带 + 非超买（short_term 为弱转强∩非超买），
**marked 时不再叠加 band / 超买 / 弱转强单因子**（它们的 lift 已含在复合 OR 内）；
仅 unmarked 行使用单因子。

⚠ 概率是排序量不是预测值：因子间相关使朴素乘积系统性高估，已用 SHRINK<1 收缩 +
区间截断抑制；数字只用于「谁排前面」，不可当作胜率承诺。

校准会漂移：因子命中率随市场 regime 变化（本项目文档注释数字已两次过期）。重算：
  python -m scanner.nextday_attribution
并复核本文件常数（见 tests/test_nextday_prob.py 文档注释的补算口径）。
"""

from __future__ import annotations

import math
from typing import Any

from scanner.config import (
    FUND_OUTFLOW_NET_PCT,
    NEXTDAY_SPIKE_MID_MAX,
    NEXTDAY_SPIKE_MID_MIN,
    NEXTDAY_SPIKE_SWEET_LOW,
)
from scanner.ranking import (
    _entry_fund_flow_pct,
    _entry_overbought,
    _entry_sector_resonance,
    _nextday_entry_percent,
)

# ── 类别 base rate（nextday_attribution [1] 分策略，2026-09-05）──
BASE_RATE_BY_CAT: dict[str, float] = {
    "rebound": 0.179,
    "known_new_face": 0.127,
    "momentum": 0.100,
    "new_face": 0.097,
    "core_dip": 0.089,
    "short_term": 0.062,
    "pullback": 0.056,
    "pool_pick": 0.028,
    "comeback": 0.028,
}
BASE_RATE_DEFAULT = 0.078  # 全体 hit 率（未知类别兜底）

# ── 因子 odds ratio（条件命中率 OR，推导见模块 docstring）──
OR_MARKED = 2.6  # 🎯 复合画像 13.6% vs unmarked 5.8%（n=543/794；含甜蜜带+非超买/弱转强效应）
OR_PROMINENCE = 2.0  # 辨识度↻ 12.4% vs 非辨识度 6.5%（n=411/1375）
OR_OVERBOUGHT = 0.68  # 超买 5.6% vs 非超买 8.0%（n=108/1678；仅 unmarked 行）
OR_BAND_SWEET_LOW = 0.88  # 0-2% 低吸带 6.9%（含 <0：最近可得分桶 <1% 6.6%，n=605）
OR_BAND_MID = 1.35  # 4-8% 甜蜜中段 10.3% vs 全体 7.8%（n=556）
OR_BAND_DEAD = 0.70  # 2-4% 死区 5.6%（n=411）
OR_BAND_TRAP = 1.07  # ≥8% 8.3%（8-10% n=108 与 ≥10% n=106 合并：hit 不差但平均 -1.37%）
OR_OUTFLOW = 0.32  # 主力净流出≤-8% 1.5% vs 资金流可得子集 4.6%（n=66）
OR_SMALL_SECTOR = 0.58  # 小板块共振 5.4% vs 无共振 9.0%（n=558/1228）

# 朴素乘积高估抑制：因子间相关（🎯~band、辨识度~rank 趋势），log-odds 收缩系数。
LOG_ODDS_SHRINK = 0.7
# 概率截断区间：排序量不是预测值，抑制模型过度自信/过度自卑。
P_MIN = 0.01
P_MAX = 0.50


def _logit(p: float) -> float:
    return math.log(p / (1.0 - p))


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def _band_or(percent: float) -> float:
    """推荐时刻涨幅带 OR（仅 unmarked 非 short_term 行；带语义与 ranking._entry_band 同源）。"""
    if percent < NEXTDAY_SPIKE_SWEET_LOW:
        # <2%（含下跌）：0-2% 桶 6.9% / <1% 桶 6.6%，同取低吸带 OR
        return OR_BAND_SWEET_LOW
    if percent < NEXTDAY_SPIKE_MID_MIN:
        return OR_BAND_DEAD  # 2-4% 死区
    if percent < NEXTDAY_SPIKE_MID_MAX:
        return OR_BAND_MID  # 4-8% 甜蜜中段
    return OR_BAND_TRAP  # ≥8% 陷阱带（hit 不差但平均次日为负）


def next_day_hit_probability(
    entry: Any,
    *,
    marked: bool,
    prominence: bool | None = None,
    flow: float | None = None,
) -> float:
    """估计「推荐后次日涨幅≥NEXTDAY_HIT_THRESHOLD」的概率（float ∈ [P_MIN, P_MAX]）。

    纯函数：不改评分、不落库、无 IO。因子数据缺失（flow=None / prominence=None）
    时该因子跳过（fail-open），与 ranking 判定链的缺数据语义一致。

    entry：综合排序行 dict（需 category；band/超买/板块共振经 ranking 单源助手从
    _candidate dims 或 score_breakdown 读取）。
    marked：🎯 次日大涨画像判定结果（调用方经 ranking._is_nextday_marked 预计算；
    必传——该因子与 band/超买存在复合关系，由调用方保证口径单一）。
    prominence：辨识度（scanner.database.get_prominence_map 批量预计算）；None=无数据跳过。
    flow：主力净占比（%）；None 时读 entry dims（_entry_fund_flow_pct 同口径）。
    """
    cat = entry.get("category", "")
    base = BASE_RATE_BY_CAT.get(cat, BASE_RATE_DEFAULT)
    ors: list[float] = []
    if marked:
        # 🎯 复合画像已含甜蜜带+非超买（short_term 弱转强）效应，防重复计费不再叠加。
        ors.append(OR_MARKED)
    else:
        if _entry_overbought(entry):
            ors.append(OR_OVERBOUGHT)
        if cat != "short_term":
            # short_term 豁免涨幅带（其规律在弱转强，与 ranking._entry_tier 同口径）；
            # marked 行已含带效应，也在此豁免（marked 时上面分支已 return 前置）。
            ors.append(_band_or(_nextday_entry_percent(entry)))
    if prominence:
        ors.append(OR_PROMINENCE)
    if flow is None:
        flow = _entry_fund_flow_pct(entry)
    if flow is not None and flow <= FUND_OUTFLOW_NET_PCT:
        ors.append(OR_OUTFLOW)
    if _entry_sector_resonance(entry):
        ors.append(OR_SMALL_SECTOR)

    log_odds = _logit(base) + LOG_ODDS_SHRINK * sum(math.log(o) for o in ors)
    p = _sigmoid(log_odds)
    return min(max(p, P_MIN), P_MAX)


# 校准漂移巡检入口：python -m scanner.nextday_attribution（对比本文件常数）
