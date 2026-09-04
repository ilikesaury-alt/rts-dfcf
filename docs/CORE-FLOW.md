# rts-dfcf-max 核心流程梳理

> 生成日期：2026-09-04。基于 `unified_scanner.py` / `scanner/orchestrator.py` / `scanner/enhancer.py` / `scanner/ranking.py` 通读整理。

## 一、项目定位

A股创业板飙升榜（雪球 biaosheng）实时扫描器：盘中每 60s（可配）拉一次飙升榜，对候选池按多策略打分，输出"次日大涨概率高"的推荐，SQLite 落库 + 飞书推送 + 终端渲染。

- 数据源双轨：雪球主源 + THS 官方金融 API 兜底（`FallbackAdapter` 自动切换）
- 策略桶：rebound / known_new_face / momentum / new_face / short_term（v1）+ 池选（v2）+ 回马枪（comeback）
- 校准目标：next_day（次日 ≥7% hit）

## 二、主循环（`unified_scanner.run_scanner`）

```text
while True:
  ① 交易时段判断 ── 非交易时段 → 收盘定稿今日K线(_finalize_today_klines)
  │                  → 写档位快照(_persist_ranking_snapshot_once) → 分段 sleep 等开盘
  ② 拉飙升榜 adapter.fetch_biaosheng()（雪球主 / THS 兜底，FallbackAdapter 自动切换）
  ③ 榜单可观测性落库 record_leaderboard_log（探测上游口径漂移：排序键/分页/样本过滤变更）
  ④ scan_with_raw(xq_raw, conn, adapter)   ← 核心，见下
  ⑤ save_recommendations 落库（先落库再展示，保证终端/飞书同源）
  ⑥ 补拉今日已推荐票实时行情（缺行情的票；降级条目 current<=0 不入，
  │  防止 0.00% 被 mark_reversed 误判"已转负"）
  ⑦ mark_reversed_recommendations 反转移出：今日曾推荐但当前不在候选池的票，
  │  满足 ①转负且回落≥5% 或 ②回落≥10%（无论红绿）→ excluded=1 移出综合排序
  ⑧ display() 渲染终端（返回 ScanView，飞书复用同一份避免两端分叉）
  ⑨ log_results 落日志 + push_feishu 推送（冷却去重）
  ⑩ backfill_outcomes 回填历史推荐收益
  ⑪ sleep(interval)
  异常处理：网络错/输出错/未知异常均自动续跑，fail-open 不中断主循环
```

## 三、单轮扫描管线（`orchestrator.scan_with_raw`）

核心管线，按顺序分 10 个阶段：

### 1. 样本预处理

- `compute_surge_sentiment` 计算飙升榜情绪
- `filter_gem_stocks` 过滤创业板
- `record_appearances` 落库上榜记录；session_state 维护当日池、在榜天数

### 2. 市值与硬过滤

- 批量拉市值；全失败时回退陈旧缓存（盘中限当日、非交易放宽 N 天），"小而美"规则降级不失效
- 硬过滤：股价 > MAX_STOCK_PRICE / 总市值 > MAX_MARKET_CAP（大票计数上报）

### 3. 回马枪跟踪池维护

在榜票保活（刷新 last_list_date）、超限票（今日涨幅 > short_term 上限）置 over_limit 盯防、过期剪枝

### 4. K线与板块

- 并行拉全部候选 K 线（主榜 45s deadline，回马枪独立 15s deadline，互不挤占）
- `get_sector_clusters` 板块聚类

### 5. 双跑候选生成（v1 + v2 并行，合并进 all_candidates）

- **v2 池管道**（`ENABLE_POOL_PIPELINE`）：
  - `build_pool` 选池（bias20/acc5/on_board 等特征）
  - `evaluate_pool` 一轮排雷：硬信号剔除、软信号挂 risk_flags（K线动量类软信号不剔除，只展示 ⚠+N）
  - `matcher.label_all_candidates` 语义标注（只标注不淘汰）
- **v1 五桶**：`score_stock` 按策略分类 —— new_face（含 known_new_face）/ momentum / rebound / short_term
- **回马枪**（`ENABLE_COMEBACK`）：`evaluate_comeback` 评估掉榜跟踪池 + 近 N 日推荐，产出 comeback 桶

### 6. 排雷补全（二次排雷）

此时 `market_extra`（涨停池+个股资金流）与 `fund_risk`（问财资不抵债）已收集，对 v2 域再跑一轮 `evaluate_pool`：

- 新命中硬信号者从候选剔除
- 合并后 danger_flags 补进 `pool_log` 落库（首轮只含 bias20/冲高回落/翻绿）
- 只作用于 v2 域，v1 五桶保持自身 validator/硬过滤口径
- ⚠ 覆盖边界（2026-09-04 审查）：`evaluate_pool` 输入是 pool_rows（在榜池），回马枪是掉榜票、
  symbol 不在池内，故 danger 通道对 comeback 实际不触发；comeback 的风险覆盖来自
  enhancer 硬过滤（主力出货复合判定/财务风险/翻绿回落/弱转强失效）+ `candidate_excluded_by_risk`
- K 线当日信号（冲高回落/翻绿+高开回落）只认 `date==today` 的 bar
  （`utils.today_kline_bar`，2026-09-04 审查修复：此前 `kl[-1]` 无日期校验，stale K 线
  会消费昨日形态误杀候选；orchestrator/danger/enhancer 三处口径已统一）

### 7. 数据增强

- RPS 相对强弱：全 GEM 监控集为基准、历史 5 日累计口径（排除今日）
- 分时并行拉取（6 线程，`wait=False` 不阻塞主循环）：分时强度/开盘强度/实时量/分钟趋势
- 大盘指数 + 时间分；大盘涨幅血缘落库（防"读错日期"无痕 bug）

### 8. 加分与终分

- `enhancer.apply_all_bonuses`：板块/分时/实时量/换手/情绪/RPS/市值/高开/资金流/涨停/在榜惯性等十余项加分 + 风险标签检测（主力出货/趋势破位/高估/量价背离）
- `accumulate_final_score` 累加终分；双挂候选（同票多桶）各自独立计算 bonus，防串桶

### 9. 风险硬过滤

- 命中"卖出/止损"级标签的候选移出推荐列表（`candidate_excluded_by_risk`）
- 落库 `scan_rejections`（审计被杀票次日收益，不污染回测样本）
- `excluded` 落标：被过滤的今日推荐置 1，通过者置 0（同日标签可能变化，以最新轮次为准）

### 10. 收尾

- 分类列表从 all_candidates 重建并各自排序（回马枪用 `comeback_sort_key`，资金流优先）
- 驱动概念计算（东财 F10，仅展示不参与打分）
- 盘中操作纪律：12 条操盘纪律逐票打标签（单票异常只跳过该票）
- 数据血缘落库 `save_scan_quality`（补拉失败/缺今日bar/兜底构造/stale 计数）
- 返回 `ScanResult`

## 四、展示与排序层（`ranking` + `display`）

- `ranking.py`：档位判定（档0-3，`_entry_tier`）、🎯 画像（`_is_nextday_marked` 次日≥7% 校准口径）、突破形态判定（`_is_breakout_setup`）、综合排序 key（`score_sort_key` / `sort_main_entries` / `comeback_sort_key`）
- `display.py`：终端渲染，双区输出（v1 主表 + v2 池选区），返回 `ScanView` 供飞书复用
- 排序/档位/🎯 画像均以 **next_day（次日≥7% hit）** 为校准目标

## 五、盘后/离线工具链

| 工具 | 用途 |
| --- | --- |
| `today_report.py` | 每日优先级报告（可回看历史 `--date`） |
| `stock_report.py` | 单票深度报告（代码或名称均可） |
| `prevday_perf.py` | 多日推荐表现归因（`--days 0` 全历史） |
| `portfolio_backtest.py` | 组合回测，`--rescore` 重算历史分数（改权重/阈值后必跑） |
| `historical_rescan.py` | 用当前管道重放历史数据（回测忠实于 live 管道） |
| `backfill_kline.py` | 收盘后 K 线补齐 |
| `walkforward.py` / `scripts/` | 滚动优化 / 各类规则挖掘验证脚本 |

## 六、关键设计原则（贯穿全流程）

1. **单一真源**：`config.py` 集中所有阈值/权重；`categories.py`（CATEGORY_REGISTRY）集中策略桶注册（label/color/priority/suggest）。其他模块只引用不定义
2. **fail-open**：外部数据失败软降级，只捕 `EXTERNAL_FAILURES`（OSError/timeout/requests/sqlite3.Error/ValueError/KeyError），编程错误必须冒泡；数据降级都有血缘落库（quality/market_index/leaderboard log），降级规模可查询
3. **先落库再展示**：终端、飞书、DB 三端同源（display_priority 从 DB 读今日推荐）
4. **可观测性优先**：榜单快照、大盘血缘、排雷 pool_log、被杀票 rejections 全部落库；历史不可篡改（ranking_snapshot 存证，代码演进不改写历史）
5. **回测口径**：`--buy-at close` 唯一合法（信号收盘后才产生，`--buy-at open` 被拒绝）；改权重/阈值后必须 `--rescore`，否则 `recommendations` 表仍是冻结的旧分数
6. **时区纪律**：全部用 `BEIJING_TZ`（UTC+8），不用 naive local time
7. **Windows 编码**：`unified_scanner.py` 设 `sys.stdout.reconfigure(encoding="utf-8")`，控制台含中文+emoji

## 附：模块速查

```text
unified_scanner.py          # CLI 入口 + 主循环
scanner/
  orchestrator.py           # scan_with_raw()：候选池 → 排雷 → 评分 → ScanResult
  data_source.py            # 适配器：Xueqiu / Ths / Fallback
  api.py                    # 雪球 HTTP（session/kline/飙升榜/市值）
  ths_api.py                # THS K线兜底
  database.py               # SQLite CRUD（recommendations / appearances / daily_kline ...）
  config.py                 # 所有阈值/权重（re-exports weights/holidays/categories）
  categories.py             # CATEGORY_REGISTRY：策略桶单一真源
  analysis.py               # K线形态分析（五桶打分）
  enhancer.py               # 加分项 + 风险标签
  validator.py              # 超买/风险标志
  ranking.py                # 综合排序 + 档位 + 🎯 画像
  display.py                # 终端渲染（返回 ScanView）
  feishu.py                 # 飞书推送
  backtest.py / portfolio_backtest.py / historical_rescan.py  # 回测链
  pool.py / matcher.py / danger.py                            # v2 池管道
  comeback.py               # 回马枪策略
  market_extra.py           # 涨停池 + 资金流（东财）
  fundamentals.py           # 问财资不抵债过滤
```
