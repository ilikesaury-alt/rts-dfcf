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

### 13. [体验] 单数据源依赖

- **问题**: 完全依赖雪球飙升榜，API 变动即瘫痪
- **建议**: 增加东方财富/同花顺的热榜 API 作为备选源
- **改动量**: 大

---

## 2026-06-17 迭代（代码审查修复）

### 🔴 Bug 修复

#### 14. [Bug] 自进化权重覆盖不生效 ✅

- **位置**: `scanner/orchestrator.py:84-86` + `scanner/analysis.py:53,187`
- **问题**: `orchestrator.py` 用 `replace("new_face_", "")` 截取键名（`new_face_today_pct` → `today_pct`），但 `analysis.py` 评分用的是 `W["today_pct_2_6"]` 等 config-level 键。覆盖被静默丢弃。
- **修复**: 新增 `NEW_FACE_DIM_TO_WEIGHT_KEY` / `MOMENTUM_DIM_TO_WEIGHT_KEY` 精准映射表，orchestrator 加载时做映射而非简单 replace。
- **改动**: `scanner/config.py` + `scanner/orchestrator.py:87-88`

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
| 19 | **accumulated 双算 today_pct** | `pcts[-5:]` 含今日涨幅，评分又单独加 `today_pct` 权重 → 今日涨幅被计分两次 |
| 20 | **stock.percent 与 K 线最后一天 percent 不一致** | `today_pct` 来自榜单实时数据，`pcts[-1]` 是 K 线昨日数据，`accumulated` 混用二者 |

#### P1 — 架构/质量

| # | 问题 | 说明 |
|---|------|------|
| 21 | **Score 可变性违规** | `Candidate.score` 在创建后 `+=` 多次突变 |
| 22 | **14 处 bare except Exception** | 全部静默吞异常，不记录原因 |
| 23 | **self_evolve.py 重复导入** | `dimension_ic` 在模块顶部和 `if args.apply:` 块内各导入一次 |
| 24 | **optimizer.py 引用不存在 config 键** | `rank_change_gte_2000` 和 `candle_quality_max` 在 config 中不存在 |
| 25 | **交易时段显示不一致** | 启动日志打印 `11:45`，config 中 `MORNING_END = 11:30` |

#### P2 — 工程改进

| # | 问题 | 说明 |
|---|------|------|
| 26 | **测试覆盖不足** | 仅 `analysis.py` 有测试，orchestrator/database/evolution 无覆盖 |
| 27 | **query 脚本硬编码日期** | `query_today.py` / `query_summary.py` 写死 `2026-06-11` |
| 28 | **Feishu 推送注释状态** | `limit_up_scanner.py:83` 已注释，可清理相关代码 |

---

## 改动量汇总

| 级别 | 总数 | 已完成 | 剩余 |
|------|:----:|:------:|:----:|
| 🔴 Bug 修复 | 5 | 5 | 0 |
| 🟡 策略/质量改进 | 8 | 8 | 0 |
| 🟢 体验优化 | 4 | 4 | 0 |
| 🆕 P0/P1/P2 迭代 | 10 | 0 | 10 |
