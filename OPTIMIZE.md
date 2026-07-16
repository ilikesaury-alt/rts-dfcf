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

### 67. [Bug] 动量策略 MA 多头判定「分析用 EMA / 验证用 SMA」不一致 🔴 P0 ✅

- **位置**: `scanner/analysis.py:84-94` (`_ma_bull_score` EMA) vs `scanner/validator.py:162-180` (`_mo_ma_alignment` 原 SMA)
- **问题**: 动量打分用 EMA 给 MA 多头 +6 分，交叉验证用 SMA 重新判定。两者对波动序列可给出相反结论，导致「分析加分但验证维度拿不到正分被静默剔除」或「分析未加分但验证判多头放行」，交叉验证 MA 维度失去意义。
- **修复**: 2026-07-16 `_mo_ma_alignment` 改用 `compute_ma(closes, n, ema=True)`，与 `analysis._ma_bull_score` 完全统一（EMA 统一方案）。新增 `test_ma_alignment_uses_ema_consistent_with_analysis` 与 `test_ma_alignment_ema_differs_from_sma_rejected` 回归。同时修正 `analysis.py:81` 注释（原「与 MACD 同一 EMA 约定」错误，MACD 内部 EMA 从 closes[0] 播种，与此处 window[0] 播种不同）
- **影响**: 部分历史票的多动验证结果改变（原本 SMA 判 ma_none 的可能变 partial/full 而更易通过），属预期修复行为
- **备注**: pullback 两侧均 SMA 已一致；new_face 验证无 MA 维度，不受影响

### 68. [Bug] 超短策略 `st_weak_to_strong` 弱转强信号重复计分 🔴 P0 ✅

- **位置**: `scanner/analysis.py:957-959`（`analyze_short_term` 计入 +8） + `scanner/validator.py:370-372`（`validate_short_term` 从 `kline_summary.dimensions` 读回同一 8 再加进 `total`）
- **问题**: 弱转强信号在最终分被加两次（分析分 +8 + 验证分 +8 = +16）。其余 3 策略验证维度均为 validator 独立重算（两层独立信号），唯独此信号是 validator 逐字复制分析 `dims` 同一值再加一遍，纯属重复计值。orchestrator `_try_candidate` 把 `total` 累加进 `score` 触发重复。
- **修复**: 2026-07-16 `validate_short_term` 保留 `wts_bonus` 作为第 4 软维度计入 `pos_dims`（门控仍生效），但**不加入 `total`**；新增 `test_weak_to_strong_not_double_counted` 回归（含对照断言：修复后 wts 不影响 total）。
- **影响**: 弱转强类 short_term 票最终分下降 8 分（回到 +8 单计），排名可能微调，属预期修正；通过/不通过判定不变。
- **备注**: `st_wts_gap`(+4) 仅分析计分、验证不读，单计正确，不受影响。

### 69. [Bug] 超短策略 sector 单维度即可放行，板块普涨日批量刷屏 🔴 P0 ✅

- **位置**: `scanner/validator.py:386-389` (`validate_short_term` 门禁) + `tests/test_validator.py::TestValidateShortTerm`
- **问题**: 软维度门禁为 `pos_dims >= 1`，而 4 个软维度含 sector（板块同列计数）。板块普涨日（如 2026-07-16 医药板块集体上榜）每只票互相把对方计入 `v_st_sector_count` → 全部拿到 `V_ST_SECTOR_HOT=+10`，仅凭 sector 单一正维度即可放行。当日 short_term 一次性推荐 9 只（历史从未触发），其中 4 只（光线传媒/爱朋医疗/爱美客/舒泰神）仅靠 sector 一维、rank 恒为 -3（榜单排名>30，非超短该抓的领涨票）、量比仅 1.0-1.1x，不符合超短次日强势放量画像；且 sector 维度可在板块行情下自我证明、批量通过。
- **修复**: 2026-07-16 门禁改为 `passed = wts_bonus > 0 or (pos_dims >= 2 and non_sector_pos >= 1)`：弱转强仍单独放行（保留 P0-68 设计），否则要求 ≥2 正维度且至少 1 项非 sector（rank/MA/weak）。量比硬门维持 ≥1.0（保留健康放量档与弱转强低量比用例）。
- **影响**: 当日 9 只 → 5 只（保留 泓博医药/博济医药 弱转强放量、常山药业/我武生物/卫宁健康 含 MA 支撑+板块共振）；滤掉纯板块跟风的 4 只。存量测试 `test_healthy_volume_gives_bonus`/`test_top10_rank_bonus` 补 HOT sector 提供第 2 维度后保留；`test_single_positive_dimension_passes` 拆为 `test_single_rank_dimension_now_rejected`(淘汰)、`test_sector_only_rejected`(板块单维淘汰)、`test_two_dims_with_non_sector_passes`(放行) 三例回归。
- **TDD**: 新增回归 `test_sector_only_rejected` 直接对应今日病根（HOT sector 单维应被拒）。

---

## 🟡 策略改进

### 8. [策略] 超短次日指标扩展（弱转强/小市值已落地，余下待做）

- **已落地（2026-07-15）**: `analyze_short_term` 新增「弱转强（分歧转一致）」维度（昨日长上影+收盘弱/炸板 + 今日高开转强 → trend="弱转强"），并翻转市值为「小市值偏好」（流通市值≤100亿加分、100~300亿小加分、>300亿不加分）；`validate_short_term` 把弱转强作为第4个软维度放行。
- **修复（2026-07-15）**: 小市值原误用 `stock.value`（雪球热度值，非市值）作判断，生产环境永不触发。改为用真实的 `stock.market_cap`（流通市值，亿元，由 orchestrator 富集填充），`StockInfo` 新增 `market_cap` 字段；测试同步改用 `market_cap`。
- **待做（需新数据源或更大改动）**:
  - 连板/涨停计数（辨识度与高位风险）
  - 板块内前排/领涨辨识度（同板块按涨幅/封板排序）
  - 高位风险（20/60日阶段涨幅翻倍见顶减分）
  - 资金净流入/主力大单/封单量/龙虎榜（需 Level-2 或新 API）
- **状态**: 🟡 部分完成

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
| 26 | **测试覆盖不足** | 仅 `analysis.py` 有测试，orchestrator/database 无覆盖 |
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

#### 65. [工程] `validator.py` 死代码 `V_ST_VOL_WEAK` ✅

- **位置**: `scanner/validator.py` — `validate_short_term()` else 分支
- **问题**: `V_ST_VOL_WEAK` (-8) 永远不可达，因为硬门 `vol_ratio < 1.0` 已提前 return
- **修复**: 2026-07-16 删除死 else 分支与 `config.V_ST_VOL_WEAK` 常量（已确认无其他引用），合并为单一 `vol_healthy` 分支
- **影响**: 无评分影响，纯死代码清理

#### 66. [工程] `candidate_pool.py:179-184` 死代码

- **位置**: `scanner/candidate_pool.py` — `_apply_list_momentum_bonus()` 外层 else (streak < 3)
- **问题**: `streak >= 5` 和 `streak >= 3` 条件在 `streak < 3` 时不可达，仅 `streak >= 2` 有效
- **影响**: 无评分影响，纯死代码

---

## 2026-07-16 迭代（回测驱动权重调整）

### 背景

新增 `scanner/backtest.py` 回测归因框架（基于 `recommendations` 表 2829 条记录 +
`daily_kline` 历史）。分维度 IC 分析揭示多个反指维度（详见 `python -m scanner.backtest`）。
据此做数据驱动权重调整，原则：**只改 IC 符号 + 机制逻辑双确认的维度，幅度保守，小样本只降不删。**

### 🔴 权重调整（执行）

| # | 维度 | 改动 | IC 依据 |
|---|------|------|---------|
| 70 | `new_face_bottom` (`bottom_confirmed`) | 10 → **0** | IC=-0.376 (n=48)，加分组均收益 -0.87%（全表唯一负均值），底部确认→次日均值回归陷阱 |
| 71 | `NEW_FACE_MIN_SCORE` | 22 → **18** | 与 #70 原子配套，防 new_face 列表饥饿（最高减 10 分） |
| 72 | `new_face_gap_up` | 移除 new_face 侧 gap 加分块 | IC=-0.180 (n=136)，高开次日多冲高回落 |
| 73 | `MOMENTUM_WEIGHTS.vol_healthy` | 5 → **2** | IC=-0.235 (n=240) |
| 74 | `MOMENTUM_WEIGHTS.vol_low` | -5 → **-3** | 同上，弱化方向保留 |
| 75 | `MARKET_ENV_STRONG/WEAK` | 3/-3 → **2/-2** | IC=-0.216 (n=182) |
| 76 | `LIVE_VOL_BONUS` | 5 → **3** | IC=-0.127 (n=342) |
| 77 | `FIRST_TODAY_BONUS` | 5 → **3** | IC=-0.246 (n=164) |
| 78 | `NEW_FACE_WEIGHTS.kdj_bonus` | 3 → **1** | IC=-0.184 但 n=30（小样本），仅降权不消除 |

### 🟡 暂缓（数据混淆，待新样本）

| # | 维度 | 原因 |
|---|------|------|
| 79 | `sector_bonus` | IC=-0.181 但基于**阶段2改版前**旧分类数据；阶段2板块分类已升级（最长匹配+同花顺概念标签），需累积 2 周新样本再评 |

### 📌 已知死键（无需处理）

`new_face_candle` / `momentum_candle` / `momentum_kdj` / `high_pos` 出现在回测 IC 表但**无评分代码**，
属已删除功能的 `score_breakdown` JSON 历史残留，不影响当前信号。已在 `backtest.py` 的 `dimension_ic`
中过滤显示（#83）。

---

## 2026-07-16 迭代（次日了结视角下的正信号加权）

### 背景

用户确认实际操作**次日了结**，故优化目标唯一锁定 `next_day_pct` IC（fwd_3d/fwd_5d 仅诊断、
不参与评分）。在 #70-#78 清反指基础上，本轮对 next_day 强正 IC 维度**加权**，把分数与次日收益对齐。
new_face 维持独立展示（靠 `orchestrator.py:249` 双挂 short_term 兜底其弱边缘）。

### 🟢 权重上调（执行）

| # | 维度 | 改动 | next_day IC 依据 |
|---|------|------|---------|
| 80 | `NEW_FACE_WEIGHTS.volume_surge` | 8 → **10** | `new_face_volume` IC=+0.244（最强正信号），原被低估 |
| 81 | `MOMENTUM_WEIGHTS.value_gte_10000` | 2 → **3** | `momentum_value` IC=+0.215（次强正信号） |

### 🟢 工程改进

| # | 改动 | 说明 |
|---|------|------|
| 83 | `backtest.py` `dimension_ic` 过滤死键 | 跳过 `new_face_candle`/`momentum_candle`/`momentum_kdj`/`high_pos`，IC 表仅显示仍生效维度 |

### 明确排除（未改）

- `momentum_accumulated`：维持加分（next_day IC=+0.143 正；fwd_3d IC=-0.588 但仅诊断、不驱动权重）
- `sector_bonus`：维持暂缓（#79）
- new_face 权重整体：不额外加减（双挂 short_term 兜底）
- fwd_3d/fwd_5d 逻辑：不动

### 验证

- `tests/test_weights.py` 追加 `test_volume_surge_raised` / `test_momentum_value_raised`
- 全量 `pytest tests/` **291 passed**
- 回测基线（历史数据）未变：momentum 56.7%/+2.01%、new_face 45.9%/+0.27%（预期——改动效果由未来扫描累积的新 `score_breakdown` 体现）
- 死键已从 IC 表消失，维度列表仅含生效维度

### 后续复评（约 2 周后）

用新累积样本重跑 `python -m scanner.backtest --metric next_day_pct`，确认 score 与次日收益 IC 改善
（目标：momentum IC 从 -0.119 向 0 靠拢），并评估阶段2新分类对 `sector_bonus` 的实际影响（#79）。

---

## 2026-07-16 二次调研复盘（P0 修正）

### 🔴 诊断工具 bug 修正

| # | 问题 | 修复 |
|---|------|------|
| 84 | `backtest.py:217` 死键集合误含 `momentum_kdj`，但该维度在 `analysis.py:223` 仍实时产出并计入 `bonus`，被错误从 IC 表过滤 | 死键集合收窄为仅 `new_face_candle`/`momentum_candle`/`high_pos`（已 grep 确认这三者无任何 `dims[...]` writer）；`momentum_kdj`/`momentum_macd`/`momentum_adx` 为活跃维度，保留 |

### 关键阻塞（数据闭环断点，需运行时信息）

- **数据流未贯通**：DB 最新记录停在 **2026-07-16**（即 #80-#81 改动当天）；最新 `score_breakdown` 仍显示旧值（`momentum_value:1`、`live_vol_bonus:5`）。
  说明自改动后扫描器未产生新记录 → 过去两周权重迭代对实际信号零影响，回测基线自然"不变"。
  **需用户确认** `unified_scanner` 调度方式（cron/手动）后，让新权重累积 ≥2 周新样本再复评。
- `pullback`：70 条记录但 0 条带 `next_day_pct`（疑似缺 K 线致 backfill 跳过或历史残留）。
- `short_term`：全量 0 条，"双挂超短"（orchestrator.py:249）在真实数据从未触发，需验证 `c_st` 是否真产生。
- `old_face`（1269 条，完整收益）被 `ACTIVE_CATEGORIES` 排除，可作反向疲劳特征诊断（未做，待确认）。

### 验证

- `tests/test_backtest.py` 追加 `test_dimension_ic_keeps_live_momentum_kdj`
- 全量 `pytest tests/` **292 passed**
- 运行时确认：`momentum_kdj` 重新出现在 IC 表；`new_face_candle`/`momentum_candle`/`high_pos` 不再泄露

---

## 2026-07-16 运行期 crash 修复

| # | 问题 | 修复 |
|---|------|------|
| 85 | `unified_scanner.py:137` 访问 `top_s.kline.rps_rank`，但 `KlineSummary` 无此属性（RPS 仅以 `c.rps_bonus` 存于 `Candidate`），运行时 `AttributeError` | 改为显示 `top_s.rps_bonus`（enhancer.py:122 已填充所有候选） |

### 验证
- `pytest tests/` **292 passed**


