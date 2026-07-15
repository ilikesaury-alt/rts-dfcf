# 优化项清单

优先级标记：🔴 高（明显bug/问题） 🟡 中（策略改进） 🟢 低（体验增强）
✅ = 已完成

---

## 🔴 Bug 修复

### 1. [Bug] 双重排序覆盖评分排序 ✅

- **位置**: 已修复
- **问题**: 先按评分降序排列，紧接着按排名升序排列，第二次排序**完全覆盖**第一次
- **修复**: 改为仅按评分降序，`sort(key=lambda c: -c.score)`

### 2. [Bug] Intraday 缓存被空 ✅

- **位置**: `limit_up_scanner.py` (原 L988，已删除)
- **修复**: 删除了 `_INTRADAY_CACHE.clear()`，缓存可跨候选复用
- **状态**: ✅ 已完成

### 3. [Bug] 昨日回顾功能被注释

- **位置**: `limit_up_scanner.py` (原 L1286/1297，已注释)
- **问题**: `get_tracking_summary()` 调用被注释掉，终端不显示胜率统计
- **修复**: 取消注释

---

## 🟡 策略改进

### 4. [策略] 新面孔底部确认窗口太短 ✅

- **问题**: 只用近 3 天无大跌判断"底部企稳"，参考价值有限
- **修复**: 增加了 20 日低点判断——当前收盘价须在近 20 日最低点 5% 以内
- **改动**: `analyze_new_face()` 中新增 `near_20d_low` 条件，纳入 `bottom_confirmed`
- **状态**: ✅ 已完成

### 5. [策略] 新面孔评分权重失衡 ✅

- **问题**: 底部确认 +25 分（占总分近半），其他维度沦为点缀
- **修复**: `bottom_confirmed` 分值从 +25 降至 **+15**
- **状态**: ✅ 已完成

### 6. [策略] 累计涨幅惩罚过严 ✅

- **问题**: 5 日累计 ≥10% 扣 15 分，正常震荡容易触发
- **修复**: 阈值放宽到 **≥15% 扣 10 分，≥25% 再额外扣 10 分**
- **状态**: ✅ 已完成

### 7/8. [策略] 旧面孔策略已删除 ✅

- **状态**: `analyze_old_face()` 整函数已删除（含未定义常量引用）。所有非新面孔候选统一走动量延续。

### 9. [策略] 小而美加分未实现

- **状态**: 阈值保持 300亿/100元（用户确认），暂无加分计划

### 10. [策略] 行业分类太粗糙

- **问题**: 纯名称关键词匹配，"阳光"误匹配新能源，"信息"误匹配计算机
- **建议**: 引入 `sector_cache` 表数据，先查 DB 缓存再 fallback 关键词
- **改动量**: 中

---

## 🟢 体验优化

### 11. [体验] 周末推荐跟踪逻辑缺陷 ✅

- **位置**: `database.py` — `_last_trading_day(conn)`
- **问题**: 用 `date.today() - 1` 硬查"昨天"，周一会查周日（永远无数据）
- **修复**: 新增 `_last_trading_day()` 函数，从 appearances 表取最近交易日
- **状态**: ✅ 已完成

### 12. [体验] 无请求限流 ✅

- **位置**: 新增 `_throttle()` 全局限流器
- **修复**: 每次 API 调用（K 线/分时）前检查间隔，确保 ≥0.15 秒
- **状态**: ✅ 已完成

### 13. [体验] 单数据源依赖 ✅

- **问题**: 完全依赖雪球飙升榜，API 变动即瘫痪
- **修复**: 新增 `scanner/ths_api.py` + `tonghuashun_scanner.py`，同花顺热股榜作为独立入口
- **数据源**: `https://dq.10jqka.com.cn/fuyao/hot_list_data/out/hot_list/v1/stock?list_type=skyrocket`
- **改动量**: 大 ✅ 已完成
- **后续演化** (2026-06-25): `limit_up_scanner.py` + `tonghuashun_scanner.py` → 合并为 `unified_scanner.py`，同花顺改为仅做符号集校验，不再作为独立入口

---

## 2026-06-17 迭代（代码审查修复）

### 🔴 Bug 修复

#### 15. [Bug] 4 个测试失败 ✅

| 测试 | 原因 | 修复 |
|------|------|------|
| `test_candle_quality_*` (2个) | 引用不存在的 `new_face_candle` / `momentum_candle` 维度键 | 删除测试 |
| `test_volume_surge_penalty` | 期望 `== -8`，实际配置 `vol_surge: -4` | 改为 `== -4` |
| `test_weak_form_filter_rejects_downtrend` | 测试数据 `today_pct=3` 不满足 `< 3` 条件 | K 线最后一天 `3%` → `2.5%` |

---

### 🟡 质量改进

#### 16. [质量] `rank_streak_score` 无条件 +4 ✅

- **位置**: `scanner/rank_trend.py:29-31`
- **问题**: `len(ranks) >= 3` 无条件加 4 分，纯"露脸时间"奖励，与排名趋势无关
- **修复**: 改为 `len(ranks) >= 3 and diff >= 2`，要求排名至少改善 2 位才加分

#### 17. [质量] `stale_candidates.remove(c)` 迭代修改 ✅

- **位置**: `scanner/orchestrator.py:343-348`
- **问题**: `remove()` 依赖 `__eq__` 按值比较，O(n²) 且字段变异时可能失败
- **修复**: 改为列表推导 + 统一 `_today_pool.pop()`

#### 18. [质量] 函数体内 import ✅

- **位置**: `scanner/analysis.py:21`、`scanner/orchestrator.py:27,318`
- **修复**: `__import__("datetime")` → `from datetime import date` 模块级；`timedelta` 统一提到模块级

---

### 🟢 新迭代计划（待完成）

#### P0 — 逻辑 Bug

| # | 问题 | 说明 |
|---|------|------|
| 19 | **accumulated 双算 today_pct** | `pcts[-5:]` 含今日涨幅，评分又单独加 `today_pct` 权重 → 今日涨幅被计分两次 | ✅ 已修复 (2026-06-23 重构引入 `historical_kline` 过滤今日K线) |
| 20 | **stock.percent 与 K 线最后一天 percent 不一致** | `today_pct` 来自榜单实时数据，`pcts[-1]` 是 K 线昨日数据，`accumulated` 混用二者 | ✅ 已修复 (同上，`accumulated` 已使用 `historical_kline` 的 closes 计算) |
| 31 | **intraday_score 反指仍被累加** | IC=-0.307 (353样本)，`accumulate_final_score` 仍加 `intraday_bonus` | ✅ 已修复 (2026-07-01 移除累加) |

#### P1 — 架构/质量

| # | 问题 | 说明 |
|---|------|------|
| 21 | **Score 可变性违规** | `Candidate.score` 在创建后 `+=` 多次突变 |
| 22 | **14 处 bare except Exception** | 全部静默吞异常，不记录原因 |
| 25 | **交易时段显示不一致** | 启动日志打印 `11:45`，config 中 `MORNING_END = 11:30` |

#### P2 — 工程改进

| # | 问题 | 说明 |
|---|------|------|
| 26 | **测试覆盖不足** | 仅 `analysis.py` 有测试，orchestrator/database/chain_watch 无覆盖 |
| 27 | **query 脚本硬编码日期** | `query_today.py` / `query_summary.py` 写死 `2026-06-11` |
| 28 | **Feishu 推送注释状态** | `limit_up_scanner.py:83` 已注释，可清理相关代码 |

---

## 2026-06-18 迭代（市场情绪 + 技术指标 + RPS）

### P0 — 市场情绪周期 ✅

- **位置**: `scanner/api.py:65-107`, `scanner/enhancer.py`, `scanner/orchestrator.py`
- **新增**: `compute_surge_sentiment()` 从飙升榜 top100 推导情绪阶段
- **四阶段**: 沸腾 +5, 温暖 +2, 冷却 -2, 冰封 -5
- **数据源**: 复用 `fetch_biaosheng()` 返回值，零外部依赖
- **注入点**: `orchestrator.py` → `apply_all_bonuses()` → `_record_dimensions()` IC 跟踪

### P1a — 经典技术指标 ✅

- **新文件**: `scanner/indicators.py` — RSI(6)/KDJ(9,3,3)/MACD(12,26,9) 纯函数
- **接入**: `analysis.py` 中 integrate 到 `analyze_new_face()` 和 `analyze_momentum()`
- **新面孔信号**: 超卖反转（RSI < 30, KDJ 低位金叉, MACD 转正）
- **动量信号**: 趋势确认（RSI 50~70, KDJ 中位上行, MACD > 0）
- **基础权重**: 3/维度（`rsi_bonus` / `kdj_bonus` / `macd_bonus`）

### P1b — RPS 相对强弱 ✅

- **位置**: `scanner/orchestrator.py`
- **实现**: 按策略类别分层排名（新面孔 / 动量分别计算）
- **评分**: top 20% +4, 中间 60% +2, bottom 30% -3
- **排序依据**: 5 日累计涨幅百分位
- **落库**: 记录在 `score_breakdown["rps_bonus"]` 用于 IC 跟踪，不加入 weight-key 映射

### 附带改动

- K 线获取从 15 天升级到 45 天，缓存长度检查 ≥34 条（MACD 需求）
- 废弃字段清理：`Candidate.indicator_bonus` 删除（改为 analysis.py 局部变量）

---

---

## 2026-06-23 迭代（回调介入 + 列表动量 + 反指清理）

### P0 — 新策略

#### 29. [策略] 回调介入策略（低吸建仓） ✅

- **位置**: `scanner/analysis.py:386-528`, `scanner/config.py:114-135,163-171`
- **逻辑**: 从动量候补剩余池筛选今日回调（-8% < today_pct ≤ 2%）但5日累计 ≥ 5%的强势股回踩机会
- **评分**: 今日跌幅 -3% ~ -1% 加分最高(+15)，累计 10%~20% 加分最高(+18)，缩量回踩 MA10 +12
- **最低门槛**: 18 分（配置 `PULLBACK_MIN_SCORE`）
- **显示**: `○` 青色 + `回` 标签

#### 30. [策略] 列表动量跟踪 ✅

- **位置**: `scanner/candidate_pool.py:31-42` (list_presence), `scanner/enhancer.py:110-130` (list_momentum_bonus), `scanner/rank_trend.py:33-69` (trajectory_score)
- **逻辑**: 追踪股票在飙升榜的连续出现次数 + 排名轨迹改善
- **评分**: 连续5次+8、3次+5、2次+3、排名改善+2、Top40+3、Top20额外+2
- **替代**: 取代分时强度成为主要动态加分维度
- **接入**: 所有候选统一应用（含新面孔/动量/回调/known_new_face）

### 🟡 反指维度清理

#### 31. [反指] `intraday_score` 禁用 ✅

- **位置**: `scanner/enhancer.py:188-196`
- **IC**: -0.307（353样本，反指）
- **修复**: `accumulate_final_score` 中不再累加 `intraday_score`，保留字段不变仅用于诊断

#### 32. [反指] `momentum_kdj` 禁用 ✅

- **位置**: `scanner/analysis.py` (动量评分块)
- **IC**: -0.369
- **修复**: 移除动量侧 KDJ 评分分支

### 🔴 Bug 修复

#### 33. [Bug] 新面孔今日涨幅 > 8% 不可过滤 ✅

- **位置**: `scanner/config.py` (+ `MAX_NEW_FACE_TODAY_PCT = 8`), `scanner/analysis.py`
- **问题**: 旧逻辑对 > 8% 只扣 15 分，仍可能因其他维度凑够 18 分上榜
- **修复**: 硬拒绝 `today_pct > MAX_NEW_FACE_TODAY_PCT`，权重中删除 `today_pct_gt_8`

#### 34. [Bug] 已删除的 `today_pct_gt_8` 权重仍被引用 ✅

- **位置**: `scanner/config.py` (NEW_FACE_WEIGHTS & MOMENTUM_WEIGHTS)
- **修复**: 删除两个权重字典中的 `today_pct_gt_8: -4` 条目，删除 analysis.py 中的死分支

#### 35. [Bug] `trend = "仍在探底"` 死分支 ✅

- **位置**: `scanner/analysis.py`
- **修复**: 删除不可达代码块（已被硬拒绝 `accumulated < -8` 守卫拦截）

#### 37. [Bug] 推荐结果未落库 ✅

- **位置**: `limit_up_scanner.py`
- **修复**: 扫描循环中新增 `update_recommendation_results(conn, session)` 调用

#### 38. [Bug] 列表 momentum 双重复计分（pullback） ✅

- **位置**: `scanner/analysis.py:502-504`
- **问题**: `analyze_pullback` 内部加了一次 `streak_3`，enhancer 又加了一次，回调票双倍拿分
- **修复**: 删除 `list_streak` 参数及内部 streak 评分，统一走 enhancer

### 🟢 质量改进

#### 39. [质量] `vol_rank_combo` 阈值迁移到 config.py ✅

- **位置**: `scanner/analysis.py` → `scanner/config.py`
- **改动**: `_VOL_RANK_*` 常量从 analysis.py 移至 config.py 作为 `VOL_RANK_*`，函数读取 config 值

#### 40. [质量] 冗余 `if len(closes) >= 20` 嵌套 ✅

- **位置**: `scanner/analysis.py:478`
- **修复**: 删除始终为真的嵌套判断

## 2026-06-23 代码审查修复

### 🔴 Bug 修复

#### 42. [Bug] K 线缓存查询窗口过短 ✅

- **位置**: `scanner/database.py:172`
- **问题**: `get_cached_kline` 仅查 15 天，回填时大量有效数据缺失
- **修复**: `timedelta(days=15)` → `timedelta(days=60)`

#### 43. [Bug] 市值缓存跨周期脏读 ✅

- **位置**: `scanner/api.py` — `fetch_market_caps_batch()`
- **问题**: 300s 内存缓存导致不同扫描周期间市值数据混淆
- **修复**: 删除全局缓存，每次 fresh fetch

#### 44. [Bug] `get_stale_candidates` 日期不一致 ✅

- **位置**: `scanner/candidate_pool.py:63`
- **问题**: 用 `date.today()` 而非 `now.date()` 构造 stale_dt，mock 测试失败
- **修复**: 改为 `now.date()`

### 🟡 质量改进

#### 45. [质量] analysis.py score/dims 逻辑三重重复 ✅

- **位置**: `scanner/analysis.py`
- **问题**: `analyze_new_face`、`analyze_momentum`、`analyze_pullback` 中 score/dims 计算高度相似
- **修复**: 提取 `_score_today_pct()`、`_compute_new_face_indicators()`、`_compute_momentum_indicators()`

#### 47. [质量] 循环内 import ✅

- **位置**: `scanner/chain_watch/heat_detect.py:29`
- **修复**: `match_chains` import 移至文件顶部

### 🟢 工程改进

#### 48. [工程] `scanner/logging.py` stdlib 同名 ✅

- **修复**: 重命名为 `scanner/log_utils.py`

#### 49. [工程] `scanner/feishu.py` 死代码 ✅

- **修复**: 删除文件 + 移除 `limit_up_scanner.py` 中注释行

#### 50. [工程] 缺少 requirements.txt ✅

- **修复**: 新建 `requirements.txt`（`requests`, `wcwidth`）

#### 51. [工程] display.py 行过长 ✅

- **位置**: `scanner/display.py:133`
- **修复**: 拆分为多行 f-string

#### 52. [工程] query 脚本硬编码日期 ✅

- **位置**: `query_today.py`、`query_summary.py`
- **修复**: 添加 `--date` 参数

#### 53. [工程] indicators.py 无测试覆盖 ✅

- **修复**: 新增 `tests/test_indicators.py`（9 个 RSI/KDJ/MACD 测试）

#### 54. [Bug] `intraday_score` 反指仍被累加 ✅

- **位置**: `scanner/enhancer.py`
- **问题**: IC = -0.307 (353样本) 反指，`accumulate_final_score` 仍累加 `intraday_bonus`
- **修复**: 从 `accumulate_final_score` 中移除 `intraday_bonus` 行，保留字段仅用于诊断

---

## 2026-07-15 代码审查

### 🟢 工程改进

#### 65. [工程] `validator.py:339-340` 死代码

- **位置**: `scanner/validator.py` — `validate_short_term()` else 分支
- **问题**: `V_ST_VOL_WEAK` (-8) 永远不可达，因为 line 329 的硬门 `vol_ratio < 1.0` 已提前 return
- **影响**: 无评分影响，纯死代码

#### 66. [工程] `candidate_pool.py:179-184` 死代码

- **位置**: `scanner/candidate_pool.py` — `_apply_list_momentum_bonus()` 外层 else (streak < 3)
- **问题**: `streak >= 5` 和 `streak >= 3` 条件在 `streak < 3` 时不可达，仅 `streak >= 2` 有效
- **影响**: 无评分影响，纯死代码
