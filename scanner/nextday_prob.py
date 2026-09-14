"""次日大涨概率估计（决策级终选排序单源，2026-09-05 新增）。

背景：终选区（scanner.final_pick）此前排序键是「verdict 星级 → 🎯 → score」——
verdict 是粗粒度整数、score 跨类别尺度不可比。在 ≤2 只的真实买入预算下，
「池内谁更可能次日大涨」需要连续可比的排序量。

模型：朴素贝叶斯式 odds 乘积（log-odds 可加）
  P = σ( logit(base) + SHRINK × Σ log(OR_i) )
base 按类别取当日口径命中率；OR 为因子条件命中率对参照组的 odds ratio。

防重复计费：🎯 复合画像本身 = 甜蜜带 + 非超买（short_term 为弱转强∩非超买），
**marked 时不再叠加 band / 超买 / 弱转强单因子**（它们的 lift 已含在复合 OR 内）；
仅 unmarked 行使用单因子。

不引入任何未在统一口径下验证的因子；样本 <MIN_SAMPLE(20) 或字段存在率混杂的候选
因子一律不纳入（过热 n=6 / am_high 存在率仅 5% 且方向与维度归因矛盾 /
rank_trend_bonus n=26 贴门槛且与辨识度相关）。

**类别先验单源（2026-09-14）**：类别 base rate 的定义已上移到
`config_scoring.CATEGORY_HIT_RATE`——它是全系统「类别先验」的唯一手抄源，本模块的
`BASE_RATE_BY_CAT` 只是它的别名。同源派生出 `COMPOSITE_CAT_BASE`（综合评分）与
`decision.DECISION_CATEGORY_SPECS`（决策层准入/顺序）。此前这三处各抄一份且口径
互相矛盾（本模块用 hit 率、决策层用平均超额），已统一为 **hit 率**。

校准来源与复核纪律（2026-09-13 重写，audit §B2）：
  常数**不再靠人眼读数手抄**。重算与漂移巡检单源：
      python -m scanner.nextday_calib            # 逐因子口径 + 实测 + 漂移（退出码 1 = 有漂移）
      python -m scanner.nextday_calib --write    # 重写 scanner/nextday_calib.json
      python -m pytest tests/test_nextday_calib.py -q   # 离线守护（代码常数 vs 快照）
  每个因子的口径（适用行集合 + 参照组）声明在 scanner/nextday_calib.py 的 FACTOR_SPECS；
  快照 scanner/nextday_calib.json 记录常数、实测值、样本量与口径，可审计。

  ⚠ 2026-09-13 修正的两处口径错误（详见 nextday_calib 模块 docstring）：
  1. **OR_MARKED 2.6 → 1.56**。原 2.6 记的读数（marked 13.6% n=543 / unmarked 5.8%
     n=794）是按「甜蜜带 ∩ 非超买」拟合的，**漏了线上 is_nextday_marked 自 2026-08-14
     起含的「5 日累计 ≥ NEXTDAY_ACCUM_MIN」门槛**；按线上真实口径重算为 1.56
     （marked 10.8% n=344 / unmarked 7.2% n=1213，2185 去重样本）。
     原 docstring 反把正确口径标为「已失效」，是口径取错。
  2. **OR_OVERBOUGHT 0.68 → 0.84**。原值按**全体样本**补集拟合（含 marked 行），
     而该因子只在 unmarked 行生效；按适用集合重算为 0.84。
  两处修正经排序 A/B 验证（逐日 top-1 12.2%→14.9%、top-2 23.0%→24.3%，见
  audit-2026-09-13 §B2），属口径纠错而非调参。

  ⚠ 已知未处理项（已量化、待 §B1 样本外验证）：涨幅带 4 个 OR 仍是「按全样本 vs 全体」
  拟合，而模型只对「未标记且非 short_term」行生效——参照组不是适用集合的补集。
  按适用集合重算为 0.690 / 0.806 / 2.040 / 1.091（MID 漂移 51%）。A/B 显示单独改
  涨幅带不改善 top-2 且 rank-IC 由 0.0516 降到 0.0474，故**暂不改动**，已在
  nextday_calib.ACKNOWLEDGED_DRIFT 登记为已知项。

  ⚠ 概率是排序量不是预测值：因子间相关使朴素乘积系统性高估，已用 SHRINK<1 收缩 +
  区间截断抑制；数字只用于「谁排前面」，不可当作胜率承诺。

  ⚠ 常数会随市场 regime 漂移（本项目文档注释数字已多次过期）。任何常数改动都属
  **行为变更**，须先过样本外验证（audit §B1），再同步更新 nextday_calib.json 快照。
"""

from __future__ import annotations

import math
from typing import Any

from scanner.config import (
    CATEGORY_HIT_RATE,
    CATEGORY_HIT_RATE_DEFAULT,
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

# ── 类别 base rate（口径：全体样本分策略 hit 率）──
# 2026-09-14：唯一手抄源上移到 config_scoring.CATEGORY_HIT_RATE（config 是叶子层，
# 而本模块反向依赖 config，故表不能留在本模块——否则 ranking 无法从同源派生
# COMPOSITE_CAT_BASE，就会再长出第二份手抄副本）。此处仅做**别名**，不复制，
# 保证「改一处、全系统一致」。维护入口仍是：
#     python -m scanner.nextday_calib [--write]
# 快照守护 tests/test_nextday_calib.py 经本别名照常生效（BASE_RATE_BY_CAT 名字保留，
# 既有消费方与 scripts/nextday_calib_ab.py 的 clear/update 语义不变）。
BASE_RATE_BY_CAT: dict[str, float] = CATEGORY_HIT_RATE
BASE_RATE_DEFAULT = CATEGORY_HIT_RATE_DEFAULT  # 全体 hit 率（未知类别兜底）

# ── 因子 odds ratio（条件命中率 OR；口径逐项声明见 nextday_calib.FACTOR_SPECS）──
OR_MARKED = 1.56  # 🎯 复合画像：marked 10.8%(n=344) vs unmarked 7.2%(n=1213)，按线上判据口径
OR_PROMINENCE = 2.0  # 辨识度↻ 11.5% vs 非辨识度 5.6%（n=454/1731）
OR_OVERBOUGHT = 0.84  # 超买 5.2% vs 非超买 6.2%（unmarked 内，n=153/1688）
# ⚠ 以下 4 个涨幅带常数**不是 OR 的无偏估计，也不应被「修正」成实测值**：
#   它们的遗留口径按「全样本条件组 vs 全体」拟合（正确口径应按适用集合重算，
#   实测 0.690 / 2.040 / 0.806 / 1.091），但 2026-09-13 §B1 样本外验证显示
#   换成实测值后**终选 top-3 hit 反而下降**（OR_BAND_MID：test Δ −1.7pp，
#   train Δ −3.3pp 且 CI [−6.7,−0.6] 不含 0）。即：这些数是**按终选目标校准**
#   的，与「OR 估计」不是同一个量。漂移豁免与完整证据见 nextday_calib.ACKNOWLEDGED_DRIFT。
#   复核入口：python -m scanner.rule_validate --set scanner.nextday_prob.OR_BAND_MID=2.04
OR_BAND_SWEET_LOW = 0.88  # 0-2% 低吸带（见上方警告）
OR_BAND_MID = 1.35  # 4-8% 甜蜜中段（见上方警告）
OR_BAND_DEAD = 0.70  # 2-4% 死区（见上方警告）
OR_BAND_TRAP = 1.07  # ≥8%（hit 不差但平均次日为负；见上方警告）
OR_OUTFLOW = 0.32  # 主力净流出≤-8%：1.1%(n=92) vs 资金流可得子集非流出 3.8%(n=917)
OR_SMALL_SECTOR = 0.58  # 小板块共振 5.0% vs 无共振 7.5%（n=635/1550）

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
    marked：🎯 次日大涨画像判定结果（调用方经 ranking.is_nextday_marked 预计算；
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
            # short_term 豁免涨幅带（其规律在弱转强，与 ranking.entry_tier 同口径）；
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


# 校准漂移巡检入口：python -m scanner.nextday_calib（重算 + 对比本文件常数 + 快照）
