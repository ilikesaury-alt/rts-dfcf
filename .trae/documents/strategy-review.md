# 项目策略全面审查计划

## 摘要

对 RTS-DFCF 项目的三大核心策略（new_face/momentum/pullback）、增强评分机制、回测引擎、自进化系统进行全面审查，识别逻辑Bug、架构缺陷、数据鲁棒性问题，并提出修复方案。

---

## 当前状态分析

### 策略体系概览

| 策略 | 定位 | 入场条件 | 最低分 |
|------|------|----------|--------|
| new_face | 底部突破 | 涨幅0-8%, 5日累计>-8% | 18 |
| momentum | 趋势延续 | 涨幅0-8%, 5日累计≥8% | 15 |
| pullback | 回踩买入 | 涨幅-8%~2%, 5日累计≥5% | 18 |

### 已发现的问题（按严重程度排序）

#### 一、逻辑Bug（必须修复）

**Bug 1: Pullback策略MA维度覆盖问题**
- 文件: [analysis.py](file:///d:/everything/rts-dfcf/scanner/analysis.py#L505-L514)
- 现象: 当 `ma_support=True` 且 `ma_broken=True` 时，`dims["pullback_ma"]` 先被赋值为12，后被覆盖为-10，但score是累加的（+12再-10=+2）。dims记录与实际score贡献不一致。
- 影响: 自进化系统的IC分析依赖dims记录，不一致会导致IC计算错误，进而产生错误的参数调整建议。
- 修复: 当两者同时成立时，应将dims设为净效果值(+2)或分别记录两个维度。

**Bug 2: Pullback策略vol_healthy区间遗漏**
- 文件: [analysis.py](file:///d:/everything/rts-dfcf/scanner/analysis.py#L489-L490)
- 现象: 当 `0.9 < vol_ratio <= 1.3` 时，只设置了 `dims["pullback_volume"] = 5` 但没有加到score上。dims记录了5分但实际score贡献为0。
- 影响: 同上，dims与score不一致，影响IC分析准确性。
- 修复: 补充 `score += 5` 或调整dims记录为0。

**Bug 3: `_compute_indicators` 死代码**
- 文件: [analysis.py](file:///d:/everything/rts-dfcf/scanner/analysis.py#L100-L118)
- 现象: 该函数存在但未被任何策略调用，各策略有独立的指标计算逻辑。
- 影响: 代码维护混乱，可能误导开发者。
- 修复: 删除该死代码。

#### 二、架构缺陷（建议修复）

**缺陷 1: 回测引擎缺少pullback策略**
- 文件: [engine.py](file:///d:/everything/rts-dfcf/scanner/backtest/engine.py)
- 现象: 回测只覆盖new_face和momentum，pullback完全没有被回测。
- 影响: pullback的权重调整缺乏数据支撑，自进化系统对pullback的IC分析无法通过回测验证。
- 修复: 在 `run_backtest()` 中增加pullback策略分支。

**缺陷 2: 回测入场价与自进化追踪入场价不一致**
- 文件: [engine.py](file:///d:/everything/rts-dfcf/scanner/backtest/engine.py#L123) vs [tracker.py](file:///d:/everything/rts-dfcf/scanner/evolution/tracker.py)
- 现象: 回测用当日收盘价入场，tracker用次日开盘价入场。
- 影响: 两套评估体系不可比，回测结果与实际追踪结果存在系统性偏差。
- 修复: 统一入场价逻辑，建议都使用次日开盘价（更贴近实际交易）。

**缺陷 3: 网格搜索评估指标单一**
- 文件: [grid_search.py](file:///d:/everything/rts-dfcf/scanner/backtest/grid_search.py)
- 现象: 仅按推荐数和均分排序，未考虑胜率、Sharpe、IC等关键指标。
- 影响: 可能选出"推荐多但质量差"的参数组合。
- 修复: 增加胜率、Sharpe比率等评估维度。

**缺陷 4: PULLBACK_DIM_TO_WEIGHT_KEY 映射不完整**
- 文件: [config.py](file:///d:/everything/rts-dfcf/scanner/config.py#L163-L171)
- 现象: pullback有15个权重键，但DIM_TO_WEIGHT_KEY只映射了7个维度，缺少 `pullback_ma_bull`、`pullback_no_crash`、`pullback_rank` 等维度。
- 影响: 自进化系统无法对这些未映射维度进行IC分析和权重调整。
- 修复: 补全映射关系。

#### 三、策略设计问题（值得讨论）

**问题 1: new_face与momentum的分类边界模糊**
- 当首次出现的股票涨幅2-6%、5日累计8-15%时，new_face和momentum都可能命中，但评分差异大。
- orchestrator的fallback机制（new_face不够→momentum，或momentum不够→pullback）可能导致同一只股票在不同扫描周期被归为不同策略，影响追踪一致性。

**问题 2: pullback策略与momentum策略重叠区间**
- 涨幅0-2%且5日累计8-15%的股票，两个策略都可能命中。pullback给today_pos0_2=5分，momentum给today_pct_0_5_1=5分，区分度不够。

**问题 3: 缺少止损/止盈逻辑**
- 所有策略都是"选股"而非"交易"系统，1d/3d/5d收益只是统计参考。

**问题 4: 情绪周期阈值固定**
- 沸腾/温暖/冷却/冰冻的阈值是硬编码的，未根据市场波动率动态调整。

#### 四、数据与鲁棒性（长期改进）

**问题 5: 自进化最小样本量偏低**
- `_MIN_SAMPLE_SIZE = 15`，对金融时序数据IC估计极不稳定，容易过拟合。

**问题 6: 节假日硬编码**
- `HOLIDAYS` 集合需要手动维护，未来年份需持续更新。

**问题 7: 板块分类与产业链系统独立**
- `sector.py` 的板块分类和 `chain_watch/` 的产业链定义是两套独立系统，信息未打通。

---

## 修复方案

### 第一批：逻辑Bug修复（3项）

1. **修复Pullback MA维度覆盖** — [analysis.py](file:///d:/everything/rts-dfcf/scanner/analysis.py#L505-L514)
   - 当 `ma_support` 和 `ma_broken` 同时成立时，dims记录净效果值，或拆分为两个独立维度键

2. **修复Pullback vol_healthy区间遗漏** — [analysis.py](file:///d:/everything/rts-dfcf/scanner/analysis.py#L489-L490)
   - 补充 `score += 5` 使dims与score一致

3. **删除_compute_indicators死代码** — [analysis.py](file:///d:/everything/rts-dfcf/scanner/analysis.py#L100-L118)
   - 直接删除该函数

### 第二批：架构缺陷修复（4项）

4. **回测引擎增加pullback策略** — [engine.py](file:///d:/everything/rts-dfcf/scanner/backtest/engine.py)
   - 在 `run_backtest()` 中增加pullback分支，与orchestrator的策略选择逻辑对齐
   - 返回三类推荐记录

5. **统一入场价逻辑** — [engine.py](file:///d:/everything/rts-dfcf/scanner/backtest/engine.py) + [tracker.py](file:///d:/everything/rts-dfcf/scanner/evolution/tracker.py)
   - 回测改用次日开盘价入场，与tracker一致
   - 需要在forward_return中使用次日open而非当日close

6. **补全PULLBACK_DIM_TO_WEIGHT_KEY映射** — [config.py](file:///d:/everything/rts-dfcf/scanner/config.py#L163-L171)
   - 增加 `pullback_ma_bull`、`pullback_no_crash`、`pullback_rank`、`pullback_streak` 等缺失映射

7. **网格搜索增加评估维度** — [grid_search.py](file:///d:/everything/rts-dfcf/scanner/backtest/grid_search.py)
   - 增加胜率、Sharpe比率作为排序依据

### 第三批：策略设计优化（讨论后决定）

8. **策略分类边界优化** — 需与用户讨论是否调整
9. **止损/止盈逻辑** — 需与用户讨论是否需要
10. **情绪周期动态阈值** — 需与用户讨论是否需要

---

## 验证步骤

1. 每个Bug修复后运行 `python -m pytest tests/ -v` 确保不破坏现有测试
2. 修复Pullback dims一致性后，检查自进化系统的IC分析输出是否合理
3. 回测引擎增加pullback后，运行 `python backtest.py` 验证输出包含pullback记录
4. 入场价统一后，对比回测结果与tracker追踪结果的一致性

---

## 假设与决策

- 假设用户希望先修复确定的Bug，再讨论策略设计优化
- 假设回测入场价统一为次日开盘价（更贴近实际）
- 第三批优化项需要用户确认后才实施
