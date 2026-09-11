# 规则冻结快照 —— 2026-09-11

> **目的**：为「冻结规则 ≥20 个交易日」提供可对照的基线。从本文档签署时刻起，
> 下列文件的内容与下方列出的常量**不再变动**，直到累计 ≥20 个交易日（即
> 约 **2026-10-16** 前后）为止。届时用同一套口径重新评估，才第一次具备
> 「规则固定 → 数据可比」的条件。
>
> 背景见 `docs/diagnosis-picking-confusion-2026-09-11.md`：在规则持续变动的系统上
> 跑回测，测到的是「规则的生命周期」而非「规则的效果」。

---

## 0. 冻结元数据

| 项 | 值 |
|---|---|
| 冻结生效时刻 | 2026-09-11 11:40 (GMT+8) |
| 冻结起始交易日 | 2026-09-11（次日起用于评估） |
| 冻结解除（最早） | 第 20 个交易日之后 |
| 代码基线 commit | `e92f97a` |
| 数据库快照 | `scanner.db` — 5,026 行推荐 / 76 个交易日 / 2026-05-28 ~ 2026-09-11 / 21 张表 |

---

## 1. 受冻结的文件（SHA256 前 16 位）

任何一行改动都视为「冻结破坏」，需在 `docs/rule-freeze-log.md` 记录原因与时间。

| 文件 | 字节数 | sha256[:16] | 角色 |
|---|---:|---|---|
| `scanner/config.py` | 61,867 | `ff1d7de378471143` | 全部阈值单一来源 |
| `scanner/categories.py` | 5,439 | `90697eb00d26f129` | 类别注册表单一来源 |
| `scanner/weights.py` | 5,364 | `cf5fa9a489c3248f` | 四套评分权重表 |
| `scanner/decision.py` | 11,962 | `267003fb19a3b82f` | 终选闸门 |
| `scanner/orchestrator.py` | 32,918 | `c497f30b38273d6f` | 候选池 → 分类 → 打分管线 |
| `scanner/ranking.py` | 42,745 | `adf11cfc2e8875d6` | 综合排序 + 档位 |
| `scanner/enhancer.py` | 33,215 | `b87407e97b7899b9` | 实时富化（资金/市值/热度） |
| `scanner/validator.py` | 30,144 | `3008f127b214b5f7` | 打分校验（超买/风险旗标） |

> **允许的例外**：`display.py`（纯渲染，不参与决策）可自由重构。P2 阶段的
> `_SELL_TAGS` 收敛若触碰 `final_pick.py`，需确认不改变输出内容。

---

## 2. 决策靶点定义

| 常量 | 值 | 位置 |
|---|---|---|
| `NEXTDAY_HIT_THRESHOLD` | `7.0` | `config.py:873` |
| 表述 | 次日涨幅 ≥ 7% 记为 hit | — |
| 3 日上屏障 | 复用 `NEXTDAY_HIT_THRESHOLD` | `config.py:885` |
| `HOLD_DAYS_BY_CATEGORY` | `{'comeback': 3, 'core_dip': 3}` | `config.py` |

**排序/档位/🎯 画像均校准于 `next_day`**；回测默认 `--hold-days 3`。两个口径不可混算。

---

## 3. 终选闸门阈值

⚠️ **当前硬编码在 `scanner/decision.py:51-52`，未走 `config.py` 单一来源**（违反项目约定，
已列入 P3）。冻结期间保持原值不变：

| 常量 | 值 | 位置 |
|---|---|---|
| `GATE_INDEX_MIN_PCT` | `0.0` | `decision.py:51`（创业板指当日涨幅下限） |
| `GATE_CUM5_MIN_PCT` | `-3.0` | `decision.py:52`（5 日累计涨幅下限） |
| 触发逻辑 | `it <= GATE_INDEX_MIN_PCT or cum5 <= GATE_CUM5_MIN_PCT` → 拒绝 | `decision.py:75` |

---

## 4. 类别注册表（9 个，`CATEGORY_REGISTRY`）

| 键 | label | 颜色 | 优先级 | 建议 | 进主表 | 可标 🎯 | 实盘产出 | 分数降序 |
|---|---|---|---:|---|---|---|---|---|
| `pool_pick` | 池选 | GREEN | 0 | 推荐 | ✅ | ✅ | ✅ | ✅ |
| `rebound` | RBD | CYAN | 1 | 推荐 | ✅ | ✅ | ✅ | ✅ |
| `known_new_face` | kNF | GREEN | 2 | 推荐 | ✅ | ✅ | ✅ | ❌ |
| `momentum` | MOM | YELLOW | 3 | 参考 | ✅ | ✅ | ✅ | ✅ |
| `new_face` | NEW | GREEN | 4 | 参考 | ✅ | ✅ | ✅ | ✅ |
| `short_term` | ST | RED | 5 | 参考 | ✅ | ✅ | ✅ | ✅ |
| `comeback` | CB | CYAN | 6 | 回马 | ❌ | ❌ | ✅ | ✅ |
| `core_dip` | DIP | GREEN | 99 | 低吸 | ❌ | ❌ | ✅ | ✅ |
| `pullback` | PB | RED | 7 | 回避 | ❌ | ❌ | ❌ | ✅ |

已退役：`old_face`、`early_momentum`。

**类别引入时间线**（诊断关键证据——历史样本非同质）：

| 类别 | 首次出现 |
|---|---|
| `new_face` / `old_face` | 2026-05-28 |
| `momentum` | 2026-06-04 |
| `known_new_face` | 2026-06-15 |
| `early_momentum` | 2026-06-16 |
| `pullback` | 2026-06-23 |
| `short_term` | 2026-07-16 |
| `rebound` | 2026-07-24 |
| `comeback` | 2026-08-07 |
| `core_dip` | 2026-08-19 |
| `pool_pick` | 2026-09-02 |

---

## 5. 评分权重表（`scanner/weights.py`，四套全集）

### `NEW_FACE_WEIGHTS`
```
today_pct_2_6 8 · today_pct_1_2 6 · today_pct_0_5_1 4 · today_pct_lt_0_5 3
today_pct_6_8 5 · today_pct_gt_8 -10
accum_neg5_10 6 · accum_lt_neg5 0 · accum_10_15 3 · accum_15_20 -5
bottom_confirmed 0 · v_shape 8 · volume_surge 0
value_gte_10000 2 · value_gte_5000 1
rsi_bonus 5 · macd_bonus 6 · rsi14_oversold_bonus 4 · bollinger_oversold 5
kdj_bonus 6 · atr_contraction 2 · obv_not_negative 3
```

### `MOMENTUM_WEIGHTS`
```
today_pct_2_6 20 · today_pct_1_2 10 · today_pct_0_5_1 5 · today_pct_lt_0_5 5
today_pct_6_8 5 · today_pct_8_10 3
accum_10_15 8 · accum_15_20 5 · accum_20_30 3 · accum_gte_30 -15
vol_healthy 0 · vol_surge 0 · vol_low -3
value_gte_10000 5 · value_gte_5000 2
rsi_bonus 0 · kdj_bonus 4 · macd_bonus 0 · adx_bonus 7 · adx_weak -3
atr_healthy 0 · atr_overheated -3 · obv_uptrend 3
launch_today_pct 5 · launch_accum 8
```

### `REBOUND_WEIGHTS`
```
today_pct_0_5_2 15 · today_pct_2_4 18 · today_pct_4_6 12 · today_pct_6_8 5
drop_15_20 10 · drop_20_30 15 · drop_gte_30 20 · crash_day_bonus 5
vol_healthy 8 · vol_surge 12 · vol_low -3
rsi_oversold 8 · rsi_mid 3 · bollinger_lower 5 · v_shape 8
```

### `SHORT_TERM_WEIGHTS`
```
today_pct_2_4 15 · today_pct_4_6 8 · today_pct_6_8 12 · today_pct_8_12 15
accum_0_5 5 · accum_5_10 10 · accum_10_15 15 · accum_15_20 8
accum_gte_20 -5 · accum_lt_0 -5
vol_healthy 8 · vol_surge 12 · vol_low -5
value_small_cap 6 · value_mid_cap 2 · st_weak_to_strong 8
rsi_bonus 3 · kdj_bonus 3 · macd_bonus 3
rank_top10 8 · rank_top20 5 · rank_top30 3
```

---

## 6. 数据库 schema

| 项 | 值 |
|---|---|
| 已应用版本 | 1, 2, 4, 5, 6（**无 3** —— 该版本号被跳过/合并） |
| 表数量 | 21 |
| 当前迁移目标版本 | — |
| 关键迁移 | v4：`market_extra_cache` PK `(symbol,data_type)` → `(symbol,data_type,date)`，已于 `e92f97a` 改为单事务 + 失败回滚 + 抛出 |

> schema 变更**不属于**"规则"（不改决策逻辑），冻结期间允许继续演进，但需在
> `docs/rule-freeze-log.md` 记录，且不得改动 `recommendations`
> 的既有行语义。

---

## 7. 冻结期间允许做的事

| ✅ 允许 | ❌ 禁止 |
|---|---|
| 修 bug（不改输出内容） | 调 `weights.py` 任何数值 |
| 纯渲染层重构（`display.py`） | 改 `categories.py`（增删改类别） |
| 加测试、加日志、加可观测性 | 改 `config.py` 阈值 |
| 加 schema 迁移 | 改 `decision.py` 闸门值 |
| 写文档、写分析脚本 | 改候选池来源构成 |
| 观察、记录、积累样本 | 用新样本反过来改规则 ← **核心禁令** |

**最后一条是重点**：冻结期间**可以看数据，不可以据数据改规则**。否则周期重新开始。

---

## 8. 解冻评估清单（20 个交易日后执行）

届时（约 2026-10-16）用**同一套口径**重跑，与本文档基线对照：

1. `score` 的 rank-IC 与 t 值（基线：**IC=+0.0121, t=+0.40, n=73** → 统计上等于零）
2. Top-1 / Top-3 / 全池 的 next_day 均值（基线：**−1.038% / −1.300% / −0.782%**）
3. 各类别 next_day 均值 + 95% CI（基线：**无任何类别 CI 下界 > 0**；4 个确认负向
   —— `short_term`、`pool_pick`、`old_face`、`pullback`）
4. 日间波动率（基线：**stdev 2.386pp** vs 长期日均 −0.53% → 信噪比 ≈ 0.2）
5. 终选区间表现（基线：**−1.798% next_day / −9.620% 3日, n=6**）

**解冻判据**：只有 1、3 两项出现**统计显著**（|t| ≥ 2）且符号稳定的改善，才
认为改动有效。单日/单周的好数字不算。

---

## 9. 冻结破坏记录

新增记录请追加到 `docs/rule-freeze-log.md`（格式：日期 / 文件 / 原因 / 是否影响输出）。
