# rts-dfcf-max 核心流程梳理

> 更新：2026-09-09（HEAD `339963f`，2026-09-08）。上一版 2026-09-04。
> 本次补齐 09-05~09-08 的变更：决策层与终选区合并展示、统一复合评分排序（v1+v2 合一）、
> M1–M3 离线工具链（持有期分化 / 复权指纹 / 三屏障标签 / 模型桶）、`scanner/db/` 包拆分。
> 与代码不符之处见 §10。

## 一、项目定位

A 股创业板飙升榜（雪球 biaosheng）实时扫描器：盘中每 60s（可配）拉一次飙升榜，对候选池按多策略
打分，输出"次日大涨概率高"的推荐，SQLite 落库 + 飞书推送 + 终端渲染。

- **定位**：筛选系统，**不是交易系统**（`scanner/backtest.py` / `portfolio_backtest.py` 顶部均有此禁令）。
- **唯一决策口径**：`next_day`（次日涨幅 ≥ `NEXTDAY_HIT_THRESHOLD`=7% 的 hit 率）。排序、档位、🎯 画像、
  终选概率全部校准于此；`cum_3d` 语义类别（comeback/core_dip）经 `HOLD_DAYS_BY_CATEGORY` 单独持有 3 日。
- **数据源双轨**：雪球主源 + THS 官方 API 兜底（`FallbackAdapter` 逐请求降级）。
- **策略桶**：v1 五桶（new_face / known_new_face / momentum / rebound / short_term）+ v2 池选（pool_pick）
  + 回马枪（comeback）+ 核心方向低吸（core_dip）。`pullback` 已下线但保留注册（历史回测/归因需要）。

## 二、系统分层

```text
外部数据源            实时扫描层                        展示/决策层                 离线验证层
雪球 / THS ──► unified_scanner.run_scanner
               └─ orchestrator.scan_with_raw ──► ScanResult ──► display.build_scan_view
                                                                 ├─ render_terminal（终端）
                                                                 └─ feishu.push_feishu（卡片）
               scanner.db（SQLite，WAL）◄────────── 落库/回读 ──┘
离线：scanner.backtest（信号归因/IC）· scanner.nextday_attribution（因子校准）
      scanner.portfolio_backtest（组合自检尺）· scanner.triple_barrier（M2 标签）
      scanner.model_bucket（M3 模型桶可行性）· prevday_perf.py / today_report.py / stock_report.py
```

## 三、主循环 `unified_scanner.run_scanner`

CLI：`python unified_scanner.py [interval] [--no-feishu]`，`interval = max(60, args.interval)`，
默认 `REFRESH_INTERVAL=60`。启动时 `load_dotenv()` 读 `.env`；win32 下把 stdout 重配为 UTF-8。

初始化：`conn = init_db()`、`adapter = get_adapter()`、进程内状态 `last_ranks`（上轮榜单排名）、
`prev_board_syms`（上轮榜成员，算重叠率）。

### 3.1 每轮迭代骨架

```text
while True:
  ① clear_screen()
  ② is_trading_time(now)?
     ├─ 否 → 非交易分支（见 3.2）
     └─ 是 → 盘中分支（见 3.3）
  异常兜底（见 3.4）
```

### 3.2 非交易时段分支

1. `_ensure_conn(conn)`：`SELECT 1` 失败即重建连接（隔夜锁死/损坏自愈，2026-09-04 审查修复前置）。
2. `_finalize_today_klines(conn, adapter)`：**收盘定稿今日 K 线**（每交易日一次）。
   - 条件：交易日 且 非交易时段 且 `now.time() >= AFTERNOON_END`(15:00)，再等到 15:00+2min；
   - 只取 `date=今日 AND COALESCE(finalized,0)=0` 的票（盘中残留 bar），逐票重拉覆盖为最终收盘 bar；
   - `KLINE_FETCH_DEADLINE`=45s 预算，超时票保持 `finalized=0` 下轮续传；
   - 统计 `refreshed/corrected/pending` 写 `logs/finalize.log`；
   - **仅 `pending==0` 才置当日已定稿**（deadline 截断时下轮继续）。
3. `_persist_ranking_snapshot_once(conn)`：写当日档位快照 `ranking_snapshot`（历史存证，见 §6.4）。
4. `_check_kline_fingerprint_once(conn)`：复权漂移 SHA256 指纹比对（M1.2，每交易日一次，漂移写 finalize.log）。
5. `seconds_until_next_session` → 分段 sleep（每段 ≤60s，剩余不足按实际睡，避免错过开盘首分钟）。

### 3.3 交易时段分支（单轮 11 步）

| # | 动作 | 关键点 |
|---|---|---|
| 1 | `_ensure_conn` | 连接健康检查 + 自愈 |
| 2 | `adapter.fetch_biaosheng()` | 空榜 → 告警 + `sleep(interval)` 跳过本轮 |
| 3 | 打 `source_tag="xueqiu"` | 血缘标记 |
| 4 | `record_leaderboard_log(conn, "biaosheng", xq_raw, prev_board_syms)` | 榜单成分/中位涨幅/重叠率落库，探测上游口径漂移；返回本轮 symbol 集供下轮 |
| 5 | `scan_with_raw(xq_raw, conn, adapter)` | 核心管线，见 §4 |
| 6 | `save_recommendations(conn, new_faces+pool_picks, momentum+rebound+short_term+comeback)` | **先落库再展示**（终端/飞书/DB 三端同源） |
| 7 | 补拉今日已推荐票实时行情 | 只补 `today_recs` 中缺行情的票；`current<=0` 的降级条目不入 `live_quotes` |
| 8 | `mark_reversed_recommendations` | 今日曾推荐但已不在候选池的票：转负且回落≥`REVERSAL_TURNED_RED_DROP`(5%) 或回落≥`REVERSAL_OVERSHOOT_DROP`(10%) → `excluded=1` |
| 9 | `display(...)` → `ScanView` | 终端渲染 + 返回视图供飞书复用 |
| 10 | `log_results` + `push_feishu(view, ...)` | 日志 CSV + 飞书（冷却去重，见 §5.5） |
| 11 | `backfill_outcomes(conn)` → 倒计时 | 回填历史推荐收益；倒计时期间若跌出交易时段立即退出 |

### 3.4 长跑健壮性

- 单轮迭代体整体被 try/except 包裹：`KeyboardInterrupt` 上抛；`requests.RequestException` /
  `BrokenPipeError`/`OSError` / 未知异常分别打印告警 + 写 `logs/scanner_error.log` + `sleep(min(interval,60))` 续跑。
- stdout 管道关闭时 `_silence_stdout()` 降级到 devnull（只降一次，不关旧 stdout）。
- `logs/scanner_error.log` 超 5MB 截断保留最后 1MB。
- `finally` 关闭连接（`except sqlite3.Error: pass`）。

## 四、单轮扫描管线 `orchestrator.scan_with_raw`

返回 `ScanResult`（new_faces/momentum/rebound/short_term/comeback/pool_picks/gem_stocks/
filtered_large_cap/current_quotes/today_pool）。**本函数不落 recommendations、不渲染**。

### 4.1 样本预处理
`session_state.reset_if_new_day(today)` → `compute_surge_sentiment(raw)` → `filter_gem_stocks(raw)`
（创业板过滤、去重、脏值强转，脏票跳过）→ `record_appearances` → `session_state.update_list_presence`。

### 4.2 市值与硬过滤
批量拉市值（含掉榜池 stale 票）→ 成功即 `save_market_caps`；**全失败回退陈旧缓存**
（盘中 `max_age=0`、非交易 `MCAP_CACHE_MAX_AGE_DAYS=7`），并打印 `[~]` 降级提示。
硬过滤：`current > MAX_STOCK_PRICE`(200) 直接丢弃；`market_cap > MAX_MARKET_CAP`(500亿) 计入
`filtered_large_cap` 并丢弃。

### 4.3 回马枪跟踪池维护
`upsert_watch_symbols`（在榜票保活刷新 `last_list_date`；`percent > SHORT_TERM_MAX_TODAY_PCT`(12%)
的票标 `over_limit` 盯防）+ `prune_watch_pool(WATCH_OFFLIST_KEEP_DAYS=15)`。

### 4.4 K 线与板块
`fetch_all_klines(conn, adapter, gem_stocks_filtered, deadline=now+KLINE_FETCH_DEADLINE(45s),
stats=quality_stats)` → `get_sector_clusters`。K 线链细节见 §5.2。

### 4.5 双跑候选生成
**v2 池管道**（`ENABLE_POOL_PIPELINE`，回滚杠杆 `RTS_ENABLE_POOL=0`）：
`get_prev_ranks` → `pool.build_pool`（bias20 / acc5 / rank_trend / on_board）→
`danger.evaluate_pool(pool_rows, klines, {}, {}, today)` 首轮排雷（硬信号剔除）→
构建 `Candidate(category="pool_pick", score=0, kline=_v2_kline_summary)` + 软信号进 `risk_flags` →
`matcher.label_all_candidates`（语义标注，只标注不淘汰）→ 构建 `pool_log_rows`（落库延后到二次排雷）。

**v1 五桶**：逐票 `candidates.score_stock`（内含 4 路 `analyze_*` + 交叉验证 + `classify_category`），
`momentum` 受 `ENABLE_MOMENTUM`、`short_term` 受 `ENABLE_SHORT_TERM` 门控。
`all_candidates = pool_picks + new_faces + momentum + rebound + short_term`。

**回马枪**（`ENABLE_COMEBACK`）：`comeback.evaluate_comeback(conn, adapter, fetch_klines, today,
on_list_symbols, clusters)` → (反转, 回踩, quotes)，用**独立** `COMEBACK_KLINE_DEADLINE`=15s 预算。

随后 `enrich_candidate_market_cap` 补齐市值字段。

### 4.6 行情增强与基本面风险
`market_extra.collect_market_extra`（涨停池 + 个股资金流，全市场各 1 次请求）+
`fundamentals.collect_fund_risk`（问财"每股净资产<0"反向查询，命中打"财务风险"硬过滤标签）。

### 4.7 v2 二次排雷
数据就绪后对 `pool_rows` 再跑一轮 `evaluate_pool(pool_rows, klines, market_extra, fund_risk, today)`：
新命中硬信号者从候选剔除（**只作用于 pool_pick/comeback**，v1 五桶保持自身 validator 口径）；
合并后的 `danger_flags` 补进 `pool_log` 落库；存留 v2 候选补挂软信号。
⚠ 覆盖边界：`evaluate_pool` 输入是在榜池，回马枪是掉榜票 → danger 通道对 comeback 实际不触发；
comeback 的风险覆盖来自 enhancer 硬过滤 + `candidate_excluded_by_risk`。

### 4.8 RPS 相对强弱
基准 = 全 GEM 过滤集的历史 5 日累计涨幅（**排除今日**）；候选统一用 `accum_map` 历史口径
（short_term 的 `accumulated_pct` 含今日，须覆盖）→ `compute_rps(baseline, accum_map)`。

### 4.9 分时数据
`ThreadPoolExecutor(max_workers=6)` + `intraday_fetch.parallel_fetch`（4 相，每相
`MINUTE_FETCH_PHASE_DEADLINE`=30s 限时，`shutdown(wait=False, cancel_futures=True)`）。
产出：分时强度 / 开盘强度 / 实时量比 / 分钟趋势（后者的 4 个指标写入 `kline.dimensions`，
供盘中操作纪律 rule 3/5/7 消费，不参与评分）。

### 4.10 大盘、加分与终分
`adapter.fetch_market_index()` + `compute_time_bonus()`；大盘涨幅血缘（含 bar 日期）写
`market_index_log`（`get_market_index_meta`，防止"读错日期"无痕 bug）。
`enhancer.apply_all_bonuses` 逐票执行 12 项加分器（板块/分时/实时量/换手/情绪/RPS/市值/在榜惯性/
高开/资金流/涨停连板）+ `_record_dimensions`（写 `score_breakdown` 宽表）+ `_set_risk_flags`
（风险标签，含硬过滤标签）+ 辨识度标签。
`accumulate_final_score` 逐票累加终分并 `dataclass_replace`——**双挂候选各自独立计算**（防串桶）。
`update_rank_history` → `session_state.update_pool`。

### 4.11 风险硬过滤
`candidate_excluded_by_risk`（命中 `RISK_FLAGS_HARD_FILTER` = 主力出货 / 趋势破位 / 财务风险 /
弱转强失效 / 当日翻绿+高开回落）→ 从 `all_candidates` 移除 → `_update_excluded_marks`
（置 1 带 `NOT IN ('comeback','core_dip')` 类别守卫；通过者按 `(date,symbol,category)` 置回 0）
→ `save_rejections` 写 `scan_rejections`（审计被杀票次日收益，不污染回测样本）。

### 4.12 收尾
各桶列表**从 `all_candidates` 重建**（`dataclass_replace` 已生成新对象）并排序
（new_face 用 `new_face_sort_key`，其余 `-score`，comeback 用 `ranking.comeback_sort_key`）→
`concept.compute_driving_concepts`（仅展示）→ `current_quotes`（丢弃 `current<=0`）→
`intraday_tactics.stock_actions` 逐票打 12 条操盘纪律标签（单票失败只跳过该票）→
`save_scan_quality`（gem_count/fetch_failed/today_bar_missing/minute_fallback/stale_recs）→
`ENABLE_CORE_DIP` 时 `find_core_theme_dips` + `save_core_dips` → 返回 `ScanResult`。

## 五、展示与决策层

### 5.1 `display.build_scan_view`（只算不画）

1. `intraday_tactics.session_advice` 时段提醒 → warnings；
2. `get_today_recommendations(conn)`：`excluded=0` 当日推荐，按 symbol 去重（榜上类别优先于
   comeback/core_dip，同级取最高分），注入 `first_time`、`live_percent`（取当日 appearances，
   无行置 None）、`score_breakdown`（已解析 dict）、`rank_score`（类内百分位）；
3. 绑定 `today_pool` 候选（`entry["_candidate"]`）→ 叠加 `live_quotes` / `rank_map` 实时覆盖；
4. `flow_pct_map = get_fund_flow_pct_map`；`accum_map = build_accum_map`（单查询批量回放，
   替代逐行 N+1）；
5. `nextday_mark[(symbol, category)] = _is_nextday_marked(...)`；**双挂票归一**（同票有
   short_term 行时其余类别沿用 st 行的 🎯 判定）；
6. 分区：`main_recs`（榜上五类，排除 comeback/core_dip/pool_pick）、`pool_pick_recs`、
   `comeback_recs`、`core_dip_recs`；
7. `core_stock_symbols` → `_core_stock`（名称高亮）；
8. `build_breakout_kline_map` + `breakout_mark`（⚡ 蓄势突破观察，纯展示）；
9. **v1 主表**：过滤减仓类标签（⬇减仓/⬇减半/🔻勿接/💰落袋）与不追涨门
   （`DISPLAY_MAX_TODAY_PCT`=8.0）→ 逐行 `composite_score` + `composite_tier` →
   排序 `(tier, -composite_score, CAT_DISPLAY_PRIORITY)`；
10. **v2 池选区**：同口径排序，排除已在 v1 主表的票，取前 `V2_POOL_DISPLAY_TOP`=10，
    `pool_total` 记全量；
11. `_regime_weak`（近端主表档次日均值 <0 = 弱市）→ `_adjusted_picks` 动态推荐（数据保留）；
12. 显示门：`_show_comeback` / `_show_core_dip`（主区条数 ≤ `COMEBACK_DISPLAY_MIN_MAIN` 或弱市）；
13. `nextday_rule.scan_rule`（次日大涨高概率规则，纯 DB-only 展示增量）；
14. **决策层** `decision.decision_lines`（有落库副作用，见 5.3）；
15. **终选区** `final_pick.final_pick_lines`（v1+v2+回马枪+低吸合池，见 5.4）；
16. 返回 `ScanView`（main_rows / pool_rows / comeback_rows / core_dip_rows / 各标记 map /
    flow_pct_map / decision_lines / final_pick_lines / warnings / rule_result / adj_picks）。

### 5.2 统一复合评分（2026-09-08，v1+v2 合一）

`ranking.composite_score = cat_base + tech_norm + rank_norm + fund_norm + dip_bonus`（上限 10.0）：

| 分量 | 定义 |
|---|---|
| `cat_base` | `COMPOSITE_CAT_BASE`：rebound 10.0 / kNF 5.4 / momentum 2.6 / new_face 2.3 / core_dip 0.7 / short_term −1.4 / comeback −4.6 / pool_pick −5.0（按 hit 率线性映射） |
| `tech_norm` | `score/100`（kNF 分数反指 → `(100−score)/100`，方向来自 `SCORE_DESCENDING_BY_CAT`） |
| `rank_norm` | 榜单排名 `exp(-(rank-1)/30)`，rank 1 → 1.0 |
| `fund_norm` | 资金流信号映射：强流出 −0.5 / 流出 −0.2 / 中性 0.1 / 流入 0.3 / 强流入 0.5 |
| `dip_bonus` | 低吸标签取最高：超跌反转 0.15 / 弱转强·缩量回调·均线支撑 0.10 / 放量突破 0.05 |

`composite_tier`：过热硬门（5 日累计 ≥ `OVERHEAT_ACCUM_MAX`=50% → 档3）优先，随后按
`COMPOSITE_TIER_THRESHOLDS` = {0: ≥6.0, 1: ≥4.0, 2: ≥2.0, else 3}。
**注意**：这是展示排序键，不落库、不改 `c.score`；`_entry_tier`（旧四级级联）仍被
`today_report` / `ranking_snapshot` / `walkforward` / scripts 使用，两者并存。

### 5.3 决策层 `scanner/decision.py`

回答「现在该不该买」。三道门（回滚杠杆 `RTS_DECISION_LAYER=0`）：

1. **市场门** `market_gate`：读 `market_index_log` 最新日，要求创业板指当日 >0 且 5 日累计 >−3%
   （`GATE_INDEX_MIN_PCT` / `GATE_CUM5_MIN_PCT`）；基准缺失时 **fail-closed**（宁空仓）。
2. **类别先验门**：`DECISION_CATEGORY_SPECS` 只允许 core_dip(≤2, 降序) / known_new_face(≤1, **升序**——
   分数反指) / rebound(≤2, 降序)；momentum 永禁。
3. **配额门**：全局 `DECISION_MAX_PICKS`=3，按先验顺序占位；空仓是合法输出。

`decision_lines()` 有**落库副作用**（`decision_picks`，含 `symbol='__gate__'` 的市场门行，幂等覆盖）。
CLI：`python -m scanner.decision [--date]`。

### 5.4 终选区 `scanner/final_pick.py` + `scanner/nextday_prob.py`

回答「若必须持仓，买谁」（回滚杠杆 `RTS_FINAL_PICK=0`）。纯展示、不落库。

1. `dedup_candidates`：同 symbol 多类别行归一（`short_term` 优先，其余按 `_CAT_PRIORITY`）；
2. 过滤：不追涨门（>8.0%）、减仓类纪律标签；
3. 逐票 `today_report._tier0_verdict`（评级/风险单源）+ `_is_nextday_marked`（🎯）+
   `horizon_label`（comeback/core_dip → "3日修复"，其余 → "次日靶点"）；
4. **概率排序**：`next_day_hit_probability` = `σ(logit(cat_base_rate) + 0.7 × Σ log(OR_i))`，
   截断 [0.01, 0.50]。类别 base rate 见 `BASE_RATE_BY_CAT`（rebound 0.179 … comeback 0.028，
   兜底 0.078）；OR 因子：🎯 2.6 / 辨识度 2.0 / 超买 0.68 / 涨幅带 0.88~1.35 / 主力流出 0.32 /
   小板块共振 0.58。**🎯 命中时不再叠加 band/超买/弱转强**（防重复计费）。P 是排序量不是胜率承诺；
5. 排序键：`(-P, -verdict, -score)`；momentum 直接出局（唯一负超额类别）；
6. **去相关**：买满 ≥2 只时，同驱动概念的第 2 只先跳过，名额不满再回填并标注"⚠与#1同板块"；
7. 输出 ≤`FINAL_PICK_MAX`=3 只 + 落选理由（`FINAL_PICK_REJECT_TOP`=4，单条最强缺陷：
   同板块 > momentum 先验 > 首个风险 > 涨幅带 > 评级不足）。
   校准复算：`python -m scanner.nextday_attribution`。

### 5.5 终端渲染与飞书推送

`render_terminal(view)` 区块顺序：
`warnings` → **◆ 今日决策**（市场门 + 决策推荐 + 终选参考，2026-09-08 合并为一块）→
**◆ v1 池选**表 → **◆ v2 池选**表（前 N/共 M）→ ⚡ 蓄势突破观察提示 → **◆ 核心方向低吸**表。

`feishu.push_feishu(view, gem_total, filtered_large_cap)`：
`should_push`（webhook 缺失=disabled / 无票=empty / 票集未变且未过 `FEISHU_MIN_INTERVAL`=300s=cooldown）
→ `build_feishu_card`（决策+终选合并区块 → v1 池选 → v2 池选 → 核心低吸 → warnings → note）
→ `_post_card`（非 0 code 不重试，连接类异常退避 1s 重试 1 次）→ 成功回写 `PushState`。
卡片与终端共用同一 `ScanView`，保证「终端看得到什么，卡片就推什么」。

## 六、数据层

### 6.1 数据源适配器（`scanner/data_source.py`）

- 契约 `DataSourceAdapter`：`is_available / fetch_kline / fetch_biaosheng /
  fetch_market_caps_batch / fetch_market_index / get_market_index_meta / fetch_minute`。
- `XueqiuAdapter` 包装 `api.py`（session 懒建 + 锁）；`ThsAdapter`：K 线走 THS 官方
  `prices/historical`（forward qfq），市值走东财 `push2delay`，大盘走 akshare，分时恒 None
  （能力边界，告警一次），飙升榜返 `[]`。
- `FallbackAdapter._call`：主源抛 `EXTERNAL_FAILURES` **或** 返回 `None`/`{}` → 降级备源；
  空 list 与 `0.0` 是合法结果不降级；`fetch_minute` 不走 `_call`（None 合法，避免每票转投 THS）。
- 模式：`RTS_DATA_SOURCE` = auto（默认，雪球优先+THS 兜底）/ xueqiu / ths（旧值 akshare 兼容）。
  auto 模式**恒构造 FallbackAdapter**（启动探测失败也构造，由逐请求降级承担切换，雪球恢复自动回主源）。
- `get_adapter()` 双检锁单例。

### 6.2 K 线获取链 `kline_fetch.fetch_all_klines`

批量 `get_cached_klines` → 逐票判定：非交易时段**直接复用缓存**；交易时段且缓存缺今日 bar → 必补拉；
已含今日 bar 但距上次拉取 < `KLINE_REFRESH_TTL`=120s → 复用；否则补拉。
补拉**串行**调 `adapter.fetch_kline(KLINE_FETCH_DAYS=45)`，总预算 `KLINE_FETCH_DEADLINE`=45s；
失败/超时票回退 `stale_cache`，盘中再尝试 `minute_bar.merge_minute_today_bar` 用分时构造今日 bar
（单票 8s、共享 `MINUTE_FALLBACK_PHASE_DEADLINE`=30s 总预算，**构造 bar 仅本轮使用不写库**）。
新 bar 经 `save_kline_to_db` 落库：`finalized=0` 当且仅当"今日 bar 且交易时段"（盘中快照），否则 1。
告警口径：停牌（换手率 0）缺今日 bar 降 `[~]`，其余 `[!]`。

### 6.3 分时链 `intraday_fetch.parallel_fetch`

6 线程；拉取相每 symbol 一次 `adapter.fetch_minute`，限 30s；随后 4 相并行 compute（**不发网络**）：
`analyze_intraday`（需 ≥2 根，早/中/晚 0.4/0.3/0.3 加权）、`analyze_opening_strength`（≥6 根）、
`estimate_live_volume`（240 分钟投影）、`analyze_minute_trend`。超时或外部异常 → None（无分时信号）。

### 6.4 其余数据链

| 数据 | 来源 | 缓存 | 说明 |
|---|---|---|---|
| 涨停池 | THS limit-up/limit-break-pool，AKShare 兜底 | 进程 300s / DB `market_extra_cache` | 单次限 20s |
| 个股资金流 | 东财 `push2delay.clist`（100 行/页，6 线程） | 部分 60s / 完整 300s | 总限 30s，超时返部分 |
| 基本面风险 | THS 估值快照（100 只/批，跨轮增量）→ pywencai 兜底 | 成功 86400s / 失败 60s | 25s 预算 |
| 概念归属 | 东财 F10 `CoreConception/PageAjax`（8 线程） | 进程 300s / DB `concept_cache` 7 天 | 阶段限 30s |
| 大盘指数 | 雪球 kline（指数 begin/count 特殊口径） | 进程 60s | 血缘写 `market_index_log` |
| 榜单观测 | 每轮飙升榜成分 | 逐轮落库 | `leaderboard_log` |

雪球统一 `_request_with_retry`：3 次重试 + 1 次重建配额，connect 5s / read 15s，429 遵守
Retry-After（≤30s），全局 `_throttle` 0.15s 串行；cookie 失效（401/403/400+error 400016/重定向/HTML）
自愈重建一次。飙升榜连续失败 ≥3 熔断，冷却 60×2^(n−3) 封顶 600s。

### 6.5 交易日/交易时段

`holidays.py`（读根目录 `holidays.json`，缺失回退内置集合）+ `trading_session.py`
（`is_trading_day` / `is_trading_time` = 9:30–11:30 ∪ 13:00–15:00 / `trading_minutes_elapsed` /
`seconds_until_next_session` / `next_session_label` / `_nth_trading_day_after`）。
消费方：`kline_fetch`、`minute_bar`、`api._warn_stale_index_bar`、`candidate_pool.reset_if_new_day`。

## 七、DB 层（`scanner/db/` 包 + 兼容门面）

`scanner/db/schema.py`（连接 + DDL + 迁移）、`queries.py`（只读）、`dal.py`（写入）、
`_common.py`；`scanner/database.py` 保留为**纯 re-export 门面**（既有 `from scanner.database import X`
一行不改）。**monkeypatch 必须打在实现模块**（`scanner.db.dal` / `queries` / `schema`）。

### 7.1 表清单

| 表（PK） | 用途 | 写入 | 读取 |
|---|---|---|---|
| `appearances`(symbol,date) | 上榜快照，同日多轮覆盖 | `record_appearances` | `get_symbol_appearances` / `get_consecutive_appearance_days(_batch)` / `count_recent_appearances` / `get_prominence_map` |
| `daily_kline`(symbol,date) | 日线缓存（qfq） | `save_kline_to_db` | `get_cached_kline(s)` |
| `recommendations`(id) | 推荐落库（**无 (date,symbol,category) 唯一约束**，去重在 dal 内存完成） | `save_recommendations` / `mark_reversed_recommendations` | `get_today_recommendations` / `get_recent_recommendations` / `get_loss_rates_batch` |
| `market_cap_cache`(symbol) | 市值/换手/现价缓存 | `save_market_caps` | `get_cached_market_caps` |
| `market_extra_cache`(symbol,data_type,date) | zt_pool / fund_flow / fund_risk | `save_market_extra_cache` | `get_market_extra_cache` / `get_fund_flow_pct_map` |
| `concept_cache`(symbol) | 概念归属 | `save_concepts_cache` | `get_concepts_cache` |
| `sector_cache`(symbol) | 板块缓存（**无写入方**，`stock_report.py` 直查） | — | — |
| `watch_pool`(symbol) | 掉榜跟踪池（含 over_limit） | `upsert_watch_symbol(s)` / `mark_watch_evaluated` / `prune_watch_pool` | `get_watch_symbols` |
| `market_index_log`(date) | 大盘指数血缘（含 bar_date/source） | `save_market_index_log` | `get_market_index_log` |
| `leaderboard_log`(date,time,source) | 榜单口径漂移观测 | `record_leaderboard_log` | `leaderboard_obs.py` 直读 |
| `scan_quality_log`(date) | 扫描数据质量血缘 | `save_scan_quality` | `today_report.py` 直读 |
| `scan_rejections`(date,symbol,category) | 硬过滤被杀票审计 | `save_rejections` | `prevday_perf.py` 直读 |
| `pool_log`(date,symbol) | 全量池 + 排雷影子快照 | `save_pool_log` | `scripts/pool_filter_scan.py` |
| `decision_picks`(date,symbol) | 决策层短名单 + `__gate__` 门行 | `decision.save_decision_picks` | — |
| `ranking_snapshot`(date,symbol,category) | 当日档位/🎯/原因存证 | `ranking_snapshot.persist_ranking_snapshot` | `load_ranking_snapshot` |
| `kline_fingerprint`(anchor_date) | 复权漂移 SHA256 指纹 | `data_health.check_kline_fingerprint` | 同函数比对 |
| `triple_barrier_labels`(date,symbol,category) | M2 三屏障标签 | `scanner.triple_barrier` | `scanner.model_bucket` |
| `schema_version` | schema 演进 | `init_db` | — |

### 7.2 版本与迁移

`SCHEMA_VERSION=4`（v4：`market_extra_cache` PK 增加 `date`）。`init_db` 每轮做全套幂等迁移：
建表 → 版本推进（只前进）→ `ensure_observation_schema`（补 `scan_rejections` 的 outcome 列 +
建 `pool_log`/`decision_picks`）→ v4 PK 重建（RENAME→重建→INSERT SELECT→DROP）→
列迁移（`PRAGMA table_info` 内省 + `ALTER TABLE ADD COLUMN`）→ `_purge_foreign_excluded_marks`。

### 7.3 关键写/读语义

- **`save_recommendations`**：预载当日 `(symbol,category)→(id,score)`；同键仅**新分更高**才 UPDATE，
  例外是 `pool_pick`（恒 0 分，平分也刷新，否则 percent/trend 冻结在首轮）；逐行 `SAVEPOINT`
  隔离单行失败；预载失败 fail-loud 上抛（防重复行污染归因样本）。
- **`get_today_recommendations(as_of)`**：`excluded=0` + 榜上类别优先于 comeback/core_dip、
  同级取最高分；注入 `first_time`（`MIN(time)` 且排除已失效行）、`live_percent`（当日
  appearances，无行 None）、`score_breakdown`（dict）、`rank_score`（类内百分位）。返回未排序。
- **`mark_reversed_recommendations`**：`ref_pct` 优先当日最高涨幅 `high_pct`，缺失回退推荐时 percent；
  `current<=0` 视为无法度量跳过；SQL 带 `NOT IN ('comeback','core_dip')` 守卫。
- **`ranking_snapshot`**：收盘定稿后回放当日推荐、现算档位/🎯/原因并落库（`DELETE` 当日 + 重插，
  幂等）；消费端 `load_ranking_snapshot` 空 → 回退用当前代码现算。目的是**历史不可被代码演进篡改**。
- **连接**：`get_conn` = `sqlite3.connect(timeout=10.0)` + `PRAGMA busy_timeout=10000` +
  `journal_mode=WAL`；**不设 row_factory**（各查询显式按列索引）。

## 八、盘后与离线工具链

| 入口 | 用途与关键纪律 |
|---|---|
| `python unified_scanner.py` | 实时扫描主进程 |
| `python today_report.py [--date] [--top N] [--json]` | 当日/历史优先级报告（档0 分析、资金质量、回马枪小节、数据质量） |
| `python stock_report.py <代码\|名称> [--quick]` | 单票深度报告 |
| `python prevday_perf.py [--days 30\|0] [--json] [--force]` | 多日「档位 → 次日表现」自检尺（**不得用于调权重**） |
| `python -m scanner.backtest [--days] [--metric next_day_pct\|cum_3d\|...] [--ranking] [--backfill] [--dry-run]` | 信号归因仪表盘：`backfill_outcomes` 回填 next_day/fwd_3d/fwd_5d/cum_2d/cum_3d（**新值 None 不覆盖旧值**）+ 分策略胜率/IC + 分维度 IC。**唯一合法调参依据**；样本口径 2026-09-02 统一（3882 行仅 1774 票、虚高 2.19x，momentum hit 16.5%→10.0%） |
| `python -m scanner.nextday_attribution [--days 0] [--threshold 7] [--excess] [--csv] [--force]` | 因子条件命中率/OR 校准（**改 `nextday_prob.py` 常数前必跑**）。`--excess` 减 **T+1** 指数涨幅（减 T 日即失真；缺失日剔除，覆盖率 <0.90 fail-loud） |
| `python -m scanner.portfolio_backtest --compare [--rescore] ...` | 组合级自检尺（可选，降级看待；**禁止**当实盘预测或用于调权重） |
| `scanner/historical_rescan.py` | **无独立 CLI**（无 `__main__`）：仅由 `portfolio_backtest --rescore` 调 `rescan_all_signals` 用当前 config 权重重放历史（`--rescore` 的必要性来源） |
| `python -m scanner.triple_barrier [--days N] [--report]` | M2 三重屏障标签（止盈 `NEXTDAY_HIT_THRESHOLD`=+7% / 止损 `TB_STOP_LOSS_PCT`=−5% / 时间 `TB_HORIZON_DAYS`=3 日）幂等重建，与 `load_attribution_rows` 同口径 |
| `python -m scanner.model_bucket [--json]` | M3 第一步：LightGBM walkforward AUC/头部提升度可行性验证（~1700 行 / 88 交易日小样本，只判"是否继续"） |
| `python -m scanner.walkforward [--train 30 --test 10]` | 滚动窗口优化框架（`WF_EMBARGO_DAYS`=1 隔离带） |
| `python backfill_kline.py [--dry-run] [--days 60] [--quiet]` | 收盘 K 线补齐：只补缺口 + 今日 bar，`INSERT OR REPLACE` 幂等；并回填 `recommendations` 与 `scan_rejections` 的收益字段（消除"下跌票跌出榜单 → IC 被静默过滤"的幸存者偏差） |
| `python repair_kline.py [--dry-run] [--since] [--limit]` | 脏 bar 修复：仅对**已有行**按雪球 qfq 权威值比对（容差 0.011 元）才覆盖，不新增行（拓斯达 36.27→37.90 事故根治） |
| `python backfill_market_index.py [--days 200] [--dry-run] [--overwrite-live] [--db]` | 大盘基准回填 `market_index_log`（默认跳过已有行保审计证据；线上/收盘差值 >0.3pp fail-loud） |
| `scanner/data_health.py`（无 CLI） | 出报告前的**门禁**：`check_kline_health`（固定种子抽 10 条与 THS/新浪独立源交叉验证，不符比例 ≥30% 则 blocked → 先跑 `repair_kline`）、`check_market_index_health`（对账东财，容差 0.5pp）、`check_kline_fingerprint`（M1.2 复权漂移）。`prevday_perf` / `nextday_attribution` 前置调用（`--force` 跳过） |
| `python leaderboard_obs.py` / `query_today.py` / `query_summary.py` | 榜单可观测性 / 快速查询 |
| `scripts/*.py` | 各类规则挖掘与验证脚本（非生产路径） |

**回测口径纪律**（务必区分）：

- `--buy-at open` **并非整体被拒**，被拒的是 `--buy-delay 0 + --buy-at open` 的组合（前视偏差守卫，
  `portfolio_backtest.main` 中 `parser.error`）；`--buy-delay 1 --buy-at open`（次日开盘买）合法。
  与 `cum_3d` 对齐须用 `--buy-delay 0 --buy-at close`。
- `--hold-days` **默认 1**（匹配次日大涨口径）；`--hold-days-auto` 按 `HOLD_DAYS_BY_CATEGORY`
  覆盖 cum_3d 语义类；`--compare-horizons "1,3"` 逐持有期对比。
- **改权重/阈值后必须 `--rescore`**：`recommendations` 存的是旧权重冻结分，改 config 不会追溯生效。
  `--rescore` 走 `historical_rescan.rescan_all_signals`，**忠实复用线上 `score_stock` 流水线**（只把数据源
  换成历史表）。已知保真缺口：`rank_change` 恒 0、无历史市值表、资金流/涨停池/开盘分/换手等实时增强项
  不复现 → 重扫 score **不等于**线上 score 绝对值（差约 −5~+20），做分类/门禁/相对排序结论有效，
  做分数分桶/MIN_SCORE 结论无效；`comeback` 不在重扫宇宙（保持冻结分）。

## 九、贯穿全流程的设计原则

1. **单一真源**：`config.py` 集中全部阈值/权重；`categories.py` 的 `CATEGORY_REGISTRY` 是策略桶唯一注册表
   （label/color/priority/suggest/in_main_table/nextday_markable/live_produced/score_descending），
   其余模块派生。新增类别只改注册表。
2. **fail-open**：外部依赖失败软降级，只捕 `scanner.utils.EXTERNAL_FAILURES`
   （OSError / socket.timeout / sqlite3.Error / ValueError / KeyError / RequestException）；
   编程错误必须冒泡到主循环记录完整 traceback。唯一例外：决策层 `market_gate` 基准缺失时 **fail-closed**。
3. **先落库再展示**：终端 / 飞书 / DB 三端同源（`display_priority` 从 DB 读今日推荐）。
4. **可观测性优先**：榜单快照、大盘血缘、`pool_log`、`scan_rejections`、`scan_quality_log`、
   `ranking_snapshot` 全部落库；降级规模可查询。
5. **数据降级有迹**：`stale_kline` / `today_bar_missing` / `minute_fallback` 计数入 `scan_quality_log`；
   当日 K 线信号只认 `utils.today_kline_bar`（date == today），stale K 线不得消费昨日形态。
6. **时区纪律**：全部用 `now_beijing()`（`BEIJING_TZ`，UTC+8）。
7. **Windows 编码**：`unified_scanner.py` / `triple_barrier.py` 等重配 stdout 为 UTF-8（含中文 + emoji）。
8. **回滚杠杆**（env）：`RTS_ENABLE_POOL` / `RTS_DECISION_LAYER` / `RTS_FINAL_PICK` /
   `RTS_DANGER_SOFT_KLINE` / `RTS_DISPLAY_MAX_TODAY_PCT` / `RTS_ENABLE_*`。

## 十、本次核对发现的不一致与易踩点

1. **回马枪（comeback）已无终端/飞书展示区**：`ca91d21`（2026-09-02）移除「动态推荐/回马枪/
   次日大涨高概率候选」三个展示区。`ScanView.comeback_rows` / `show_comeback` / `_adjusted_picks` /
   `COMEBACK_DISPLAY_MAX` 等数据层仍保留，但 comeback 现在只被三处消费：**终选池**（合池概率排序）、
   **飞书去重集合** `_view_symbols`、**today_report 的"六、回马枪"小节**。
   终端区块实际只有：今日决策 / v1 池选 / v2 池选 / ⚡提示 / 核心方向低吸。
2. **`scanner/kline_drift.py` 不存在**：M1.2 的复权指纹监控已并入 `scanner/data_health.py`
   （`check_kline_fingerprint`），但 `config.py:910` 与 `unified_scanner.py:331` 注释仍指向旧文件名。
3. **`AGENTS.md` 两处与代码不符**：
   - "`--buy-at open` 被拒绝"——实际只拒绝 `--buy-delay 0 + --buy-at open`（见 §8）；
   - "回测默认 `--hold-days 3`"——实际默认 `--hold-days 1`（2026-08-18 统一为 next_day 口径）。
4. **两套档位并存**：展示排序已改用 `composite_tier`（2026-09-08），但 `_entry_tier`（🎯/rebound 档1/
   comeback 档2 的旧级联）仍被 `today_report`、`ranking_snapshot`、`walkforward`、`scripts/*` 使用。
   改档位语义时须同时确认两处，否则复盘口径与实时展示会分叉。
5. **`decision_lines()` 在"只算不画"的 `build_scan_view` 中落库**（`decision_picks`）——注释已声明这是
   为与展示同源而接受的例外，重构时勿误当纯函数。
6. **`recommendations` 无唯一约束**：去重完全依赖 `save_recommendations` 的内存 map；
   预载失败会 fail-loud 上抛（防重复行污染样本）。
7. **`sector_cache` 无写入方**：`stock_report.py` 直查该表，属历史遗留。

## 附：模块速查

```text
unified_scanner.py            # CLI 入口 + 主循环 + 收盘定稿/快照/指纹
scanner/
  orchestrator.py             # scan_with_raw()：候选池 → 排雷 → 评分 → ScanResult
  data_source.py              # 适配器：Xueqiu / Ths / Fallback（get_adapter 单例）
  api.py / ths_api.py / net.py# 雪球 HTTP / THS 官方 API / 东财 token
  db/{schema,queries,dal}.py  # 连接+DDL+迁移 / 只读 / 写入；database.py 为兼容门面
  config.py                   # 所有阈值/权重（re-export weights/holidays/categories）
  categories.py               # CATEGORY_REGISTRY：策略桶单一真源
  models.py                   # KlineBar / StockInfo / Candidate / ScanResult / RecommendationRow
  analysis.py                 # 4 路 analyze_*（new_face/momentum/rebound/short_term）+ 特征
  candidates.py               # score_stock / classify_category / compute_rps / 硬过滤判定
  enhancer.py                 # 12 项加分器 + 风险标签 + score_breakdown 落维
  validator.py                # 超买/风险标志
  pool.py / danger.py / matcher.py   # v2 池管道：特征 / 排雷 / 语义标注
  comeback.py / core_themes.py       # 回马枪 / 核心方向低吸
  ranking.py                  # composite_score/composite_tier + 档位 + 🎯 + 排序键
  nextday_prob.py / final_pick.py / decision.py / nextday_rule.py  # 概率 / 终选 / 决策 / 规则
  display.py / feishu.py      # ScanView 构建 + 终端渲染 / 卡片推送
  kline_fetch.py / intraday_fetch.py / minute_bar.py / market_extra.py / fundamentals.py / concept.py
  data_health.py              # 跨源交叉验证 + 复权指纹（原 kline_drift）
  backtest.py / portfolio_backtest.py / historical_rescan.py / nextday_attribution.py
  triple_barrier.py / model_bucket.py / walkforward.py / ranking_snapshot.py
  trading_session.py / holidays.py / utils.py / log_utils.py / signals.py / indicators.py
```
