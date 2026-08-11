# AGENTS.md

## Quick Commands

- **Run scanner**: `python unified_scanner.py` (default 60s interval) or `python unified_scanner.py 120` (custom seconds)
- **Crash-proof run**: `python unified_scanner.py --supervise` (父进程拉起子进程，崩溃后指数退避自动重启；重启事件写入 `logs/supervisor.log`)
- **Run tests**: `python -m pytest tests/ -v`
- **Single test**: `python -m pytest tests/test_analysis.py::TestAnalysis::test_new_face_bollinger_oversold -v`
- **组合级回测**: `python -m scanner.portfolio_backtest --compare`（多策略对比：各现役类别+综合+基准）/ `--days 60`（窗口）/ `--category new_face`（单策略）/ `--export nav.csv`（导出净值序列）/ `--include-capped`（纳入被板块上限隐藏的票，默认排除）
- **归因回测**: `python -m scanner.backtest`（默认排除 `sector_capped=1` 校准"用户实际看到"的集合；`--include-capped` 恢复全样本）

## Long-Run Robustness (P-robust)

- 主循环整个迭代体（含非交易时段等待、倒计时打印）被 `try/except` 保护，任何意外异常打印告警 + 写入 `logs/scanner_error.log` 后自动续跑，不杀进程。
- 每轮 `SELECT 1` DB 健康检查，连接损坏/锁死自动重建。
- 输出管道关闭/终端异常时 stdout 降级到 devnull（`_silence_stdout`），不崩溃。
- K 线串行拉取有 `KLINE_FETCH_DEADLINE=45s` 限时：API 故障时超时即停止补拉、剩余票回退旧缓存，单轮扫描有界不假死。
- `_request_with_retry` 用 `(REQUEST_CONNECT_TIMEOUT=5, REQUEST_TIMEOUT=15)` 双段超时，连不上的主机不再长时间挂起。
- 进程内缓存（`_MINUTE_DATA_CACHE`/`_INTRADAY_CACHE`/`_concept_ttl_cache`/`_last_kline_fetch`）上限 `CACHE_MAX_ENTRIES=2000`，超限淘汰最旧，防长跑内存膨胀。

## Project Structure

A-share stock momentum scanner merging Xueqiu surge + hot stock ranking APIs. Scores ChiNext (300xxx) stocks using "new face" / "momentum" / "pullback" / "rebound" / "short_term" strategies, plus a "comeback"（回马枪）off-list watch pool bucket.

```
unified_scanner.py        # Single entry point (dual-source fusion)
stock_report.py           # Individual stock deep-dive report tool
scanner/
  orchestrator.py         # Core scan pipeline
  analysis.py             # Scoring engines (new_face, momentum, pullback, rebound, short_term)
  comeback.py             # 回马枪：掉榜跟踪池（反转 + 回踩变体）
  validator.py            # Cross-validation per strategy
  config.py               # All thresholds and weights
  api.py                  # Xueqiu API calls (biaosheng, hot_list, kline, market cap)
  database.py             # SQLite CRUD (appearances, kline, recommendations)
  models.py               # StockInfo, Candidate, KlineSummary dataclasses
  indicators.py           # RSI, KDJ, MACD, ADX, ATR, OBV, Bollinger computation
  patterns.py             # K-line pattern detection (detect_*_patterns)
  features.py             # Unified feature extraction (build_features)
  enhancer.py             # Bonus scoring + accumulate_final_score
  candidate_pool.py       # ScanSession with list presence tracking
  rank_trend.py           # RankTracker with trajectory scoring
  sector.py               # Sector cluster detection
  market_extra.py         # 行情增强数据层（涨停池 AKShare + 资金流自实现直连 push2delay）
  backtest.py             # Backtest / IC attribution framework
  trading_session.py      # Trading hours/holidays
  display.py              # Terminal display formatting (ANSI + wcwidth)
  feishu.py               # Feishu webhook card push
  utils.py                # Utility functions (is_gem, is_st, is_hk_stock)
  log_utils.py            # Log formatting utilities
tests/                    # pytest test suite
```

## Key Facts

- **K 线数据契约（2026-08-11 重构 P0-1，`models.py:KlineBar`）**：kline 从裸 `list[dict]` 统一为 TypedDict `KlineBar`（date/open/high/low/close/volume/percent + 可选 timestamp），**唯一生产入口 `make_kline_bar()`**（`api.fetch_kline` / `database.get_cached_kline` / `historical_rescan._load_all_klines` / `ic_attribution.load_kline_by_symbol` / AKShare adapter 全部接入），内部消费端（analysis/validator/patterns/comeback/orchestrator）全部带类型标注。契约规则：date 必须非空字符串；close 必须能解析为正数（close<=0/None/NaN/`inf`/非法串 → 整 bar 剔除）；其余字段脏值（含 `inf`）→ 0。**行为收紧点**：旧 `fetch_kline` 保留 close=0 脏 bar 靠 analyze_* 兜底，现与 `get_cached_kline` 口径统一为剔除（消除两个生产端不一致）。**AKShare adapter 行为变更**：旧代码 `float(row[...])` 遇 NaN 抛异常 → 整只票返回 None 跳过评分；现走 `make_kline_bar` NaN/`inf`→0 保留 bar → 该票会被评分（volume=0/percent=0 参与 vol_ratio，大概率更优）。**AKShare 东财→新浪兜底**（2026-08-11）：`AkshareAdapter.fetch_kline` 先走东财 `stock_zh_a_hist`（push2his，本机间歇性连接重置 RemoteDisconnected），失败/空时降级到新浪 `stock_zh_a_daily`（qfq，实测稳定 3/3）；新浪源无涨跌幅列 → percent 由收盘价推算（首根 0）。两份输出统一走 `make_kline_bar`。TypedDict 保持 dict 行为（`k["date"]` 访问、merge/sort 不变），除上述行为变更外零运行时变化。测试：`tests/test_models.py`（契约用例）。
- **Database**: SQLite at `scanner.db` (auto-created). Tables: appearances, daily_kline, recommendations, sector_cache, concept_cache, market_extra_cache
- **Python version**: 3.12+ (uses f-strings, dataclasses, type hints)
- **Dependencies**: `requests`, `wcwidth` (see `requirements.txt`)
- **Trading hours**: Auto-sleeps outside 09:30-11:30 / 13:00-15:00 on trading days
- **Encoding**: Windows-specific `sys.stdout.reconfigure(encoding="utf-8")` for Chinese output

## Architecture Notes

- Scanner filters: GEM stocks only (300xxx), excludes ST/*ST, HK stocks, market cap >500亿, price >200元
- Five strategies: new_face (bottom breakout), momentum (trend continuation), pullback (**offline as of 2026-07-30** — cum_2d 均亏 -8.33%, 胜率 15.8%; `analyze_pullback` 本体保留供未来恢复，但 orchestrator 调用链已完全切断——`_score_stock` 不再调用 `analyze_pullback`/`validate_pullback`，不再进入分类候选), rebound (oversold reversal), short_term (next-day sell)
- Cross-validation (`validator.py`): each candidate must pass ≥2 of its independent dimensions (pos_dims ≥ 2). short_term adds a "non-sector" constraint and 弱转强 override.
  - **new_face**: requires ≥1 oversold signal (indicator convergence hit OR MACD bull divergence) AND pos_dims ≥ 2. Dimensions: convergence (RSI<30 + MACD golden cross + KDJ K<20 & K>D), higher-low structure, sector resonance, volume surge.
  - **momentum**: MA5>10>20 alignment (EMA, penalty -5 if broken), no RSI divergence, volume uniformity (5-day window). pos_dims ≥ 2 required.
  - **pullback**（offline as of 2026-07-30，保留供未来恢复）: today_pct ≤ 0 (flat/down day only; PULLBACK_MAX_TODAY_PCT=0.0 eliminates the (0,2] dead zone), accumulated ≥ 5%, MA20 trending up (>+0.5%), volume shrinkage (<0.6x), sector active (≥3 same-sector), bollinger touch. pos_dims ≥ 2 required.
  - **rebound**: 5-day cumulative drop ≤ -10% (P0-1: relaxed from -15% on 2026-07-28 to cover "阴跌企稳" scenario where drop is 10-15% without crash day), crash day (≤-10%) is now a bonus (not hard gate), today's gain 0.5%~8% (stabilizing candle). pos_dims ≥ 2 required. No overbought veto (low-position scenario by design). Dimensions: oversold (RSI<30 / KDJ J<0 / MACD turn red), volume confirmation, sector resonance, pattern (engulfing/hammer/3-bull). Note: pattern score is computed once in `analyze_rebound` (via `detect_rebound_patterns`) and **not re-counted** in validator `total` — `_rb_pattern` only reads pre-computed dims for pos_dims gating.
  - **short_term**: vol_ratio ≥ 1.0 hard gate. Pass rule: 弱转强 (weak-to-strong, st_weak_to_strong>0) passes outright; otherwise requires pos_dims ≥ 2 AND non_sector_pos ≥ 1 (rank/MA/weak — sector cluster alone cannot pass, prevents sector-wide surge days flooding the list). If overbought, 弱转强 loses its override privilege. today_pct upper bound is 12% (P1-1: relaxed from 8% on 2026-07-28 to cover 8-12% strong stocks; 8-12% tier scores +8). **盘中量比口径** (2026-07-31): 盘中 K 线含今日 bar 时，今日成交量按 `min(240/已交易分钟, 10)` 投影为全天量能再算 vol_ratio (`analysis.py:_compute_volume_metrics`)，使早盘扫描可过 1.0 硬门（此前首推要等 ~11:24 后量能累积才达标）；收盘后/无今日 bar 不投影。
  - **comeback（回马枪，2026-08-07 新增，`comeback.py`）**: 补"掉榜盲区"——候选池只由当次热榜驱动，掉榜超跌股（如志特新材 07-09→07-31 三周掉榜）完全不可见，反弹企稳日漏抓。两个变体均 `category="comeback"`（recommendations 表结构不变，`trend` 列存变体，`Candidate.off_list=True` + `comeback_variant`）：
    - **反转**（off-list rebound）: 掉榜票 + 历史5日跌幅 ≤ `COMEBACK_PREFILTER_5D_DROP=-8%`（DB 缓存零网络预筛，**先于行情拉取**，避免对 `WATCH_POOL_MAX=600` 全池逐扫描轰接口）+ 今日 2~12% 企稳（`COMEBACK_MIN/MAX_TODAY_PCT`）。复用 `analyze_rebound(off_list=True)` + `validate(off_list=True, pos_dims≥COMEBACK_POS_DIMS=3)`——掉榜票无热榜背书，比榜上更严。score < `REBOUND_MIN_SCORE` 拒绝。
    - **回踩**（吸收原 tracker 模块）: 近 `COMEBACK_REENTRY_DAYS=5` 交易日推荐（排除今日）且不在榜，硬过滤（今日 ±5% / 累计 ±10% / 主力净占比 ≤ `COMEBACK_REENTRY_FUND_FLOW_LOW=-5%` fail-open），6 维买点信号（MA20支撑/缩量/未破位/RSI合理/BOLL中轨/MACD未死叉）≥4「到买点」才入选；评分 `BASE=40 + 15×信号数`。
    - **跟踪池 `watch_pool`**（database.py）: orchestrator 每轮把在榜票 upsert 保活（`last_list_date` 取较新）+ 超限票（今日涨幅 > `SHORT_TERM_MAX_TODAY_PCT=12`）置 `over_limit=1` 入池，`prune_watch_pool` 按 `last_list_date` 距 `WATCH_OFFLIST_KEEP_DAYS=15` 交易日剪枝。每票每交易日最多评估一次（`last_eval_date` 落库，重启不丢）；K 线拉取走 `_fetch_all_klines`（KLINE_FETCH_DEADLINE 兜底）。
- Scoring & classification: five strategies scored **in parallel** per stock, then `_classify_category` (orchestrator.py:236-277) picks the most fitting label by **price structure** (not attempt order). New stocks: new_face > rebound > short_term > momentum. Old stocks: rebound (crash + stabilizing) → rebound; 弱转强 (weak-to-strong) → short_term; momentum > short_term > known_new_face. **pullback 下线** (2026-07-30)：不再作为分类候选，orchestrator 调用链已完全切断（`_score_stock` 不再调用 `analyze_pullback`，`_classify_category` 无 pullback 分支），`analyze_pullback` 本体保留供未来恢复。New IPOs that pass both new_face and short_term are **dual-listed** to both buckets, each with independent `extra` bonus (orchestrator.py:347-350). comeback 不入 `_classify_category`（它在榜上票之外单独评估，`evaluate_comeback` 在 `_score_stock` 主循环后调用，候选并入综合列表，同走 market_extra 加分 + 风险标签 + final score）。
- RPS exemption: `rebound` candidates have negative `accumulated_pct` by definition and would always fall into the bottom percentile (RPS_LOW=-3 penalty). `_compute_rps` exempts `category=="rebound"` (returns 0) to avoid penalizing the strategy's core thesis.
- RPS 口径统一：所有候选用 `accum_map`（历史5日累计涨幅，排除今日）参与 RPS 排名，与 baseline（全 GEM 监控集历史5日累计）口径一致。short_term 的 `c.kline.accumulated_pct` 仍包含今日 bar（策略语义，供评分维度用），但 RPS 排名时被 `accum_map` 覆盖，避免百分位偏高（2026-07-29 修复）。
- 反指加分清理（2026-08-10，依据 `--metric cum_3d` 分桶/IC 数据）：
  - **validation_bonus 只做门禁不加分**：全期 cum_3d IC -0.139（反指）——`orchestrator._try_candidate` 与 `comeback._try_rebound_candidate` 不再把 validate 的 bonus 加进 score，bonus 仍写 dims 供展示与 dimension_ic 归因；历史 score 不回填，新口径下重新积累。
  - **new_face MIN_SCORE 拆分**：首日 new_face 全 score 档均负收益（1018 条 cum_3d -1.58）→ 新增 `NEW_FACE_FIRST_MIN_SCORE=50` 砍量减噪；known_new_face 分数反指（低分档最优）→ 保持 `NEW_FACE_MIN_SCORE=18` 不砍低分，二者必须分开设门槛。
  - **momentum MIN_SCORE 16→50**：分桶 <50 档 55 条 cum_3d -0.95% vs >=50 档 379 条 +2.82%；「首次启动」子模式分数实测全 >=64 不受影响。
  - **short_term today_pct 权重反向修正**：分桶 4-6% 档最差（-1.41%, n=41，原权重却最高 20）→ 8；8-12% 档最好（+3.84%, n=21，原权重却最低 8）→ 15。替换"涨幅越大越降权"的拍脑袋设定。
- **回测定位（2026-08-10，必读）**：本项目是**筛选系统，不是交易系统**，回测有且只有两个合法用途：
  1. `scanner.backtest` = **权重校准仪表盘**（核心价值）：输出 IC/胜率/分桶/维度 IC，回答"分数排序是否等于好坏排序"，用于调 MIN_SCORE、权重、`CAT_DISPLAY_PRIORITY`。这是筛选系统唯一该用的回测形态。
  2. `scanner.portfolio_backtest` = **可选自检尺**（降级看待）：只回答"不挑选、按打分等权全买会怎样"，作评分是否带来超额的 sanity check（`--compare` 的"基准无筛选"对照）。
  **禁止**：把组合层净值当"实盘收益预测"、拿它去调权重（对历史过拟合，样本越小越危险）、把回测结果当该系统"能不能赚钱"的裁判。两个模块都不进入实时扫描路径。
- **次日大涨归因（2026-08-10，`scanner/nextday_attribution.py`）**：用户偏好「次日大涨」票（next_day ≥7%），与系统 cum_3d 决策口径是**两个不同目标**（cum_3d 好的 8-12% 档在 next_day 口径反而 -1.32% 次日回吐）。本模块是**独立的 next_day 校准仪表盘**：分策略 hit 率/rank-IC、推荐时刻涨幅带矩阵、score 分桶、落库维度归因、二元因子条件 hit 率。**当前结论**（去重 1006 条，hit 10.2%）：① 涨幅带甜蜜区在 <1% 与 1-2%（hit 11.7%/13.2%，低吸潜伏）、4-6%/6-8%（11.8%）；**8-10% 是陷阱**（7.5% 且平均 -1.42%）——与 cum_3d 口径「8-12% 最好」相反；② score <30 桶 hit 率最高（16.7%）、70-90 最差（7.4%），**低分反指**再次验证；③ rebound 仍是最强类别（hit 32%、IC+0.26）；④ short_term 超买 hit 仅 5%（非超买 10.5%），弱转强 hit 11.8%；⑤ **辨识度(↻) 是最强单因子**（2026-08-10 加入因子表，去重 1006 条聚合 hit 14.5% vs 非辨识度 8.2%，分档最高 24%）——**「前 N 日曾推」与辨识度 67% 重合、剔除辨识度后独立增量≈0（前1日非辨识度 9.5% ≈ 首推基线 8%），故弃用推荐历史信号、复用已有辨识度**；辨识度判定复用 `database.get_prominence_map(conn, syms, as_of_date=d)`（新增历史回放参数，与 enhancer/display 同实现防口径漂移）。**用法**：`python -m scanner.nextday_attribution [--days N] [--threshold 7] [--csv prefix]`，随数据积累持续迭代观察；任何「次日大涨」目标调参都必须先跑本模块，再跑 `scanner.backtest --metric next_day_pct` 复核，勿与 cum_3d 口径混淆。不进入实时扫描路径。
- **次日大涨候选已并入主表标记（2026-08-11，`display.py`）**：原「◆ 次日大涨候选」独立区（2026-08-10 方案 A display-only 观察窗）与综合排序主表**重合度 65%**（实测当日主表 17 只中 11 只甜蜜带、两表排序几乎一致、辨识度因子空转——甜蜜带∩辨识度=0），重复输出；改为**主表行尾 🎯 标记**（`_is_nextday_marked`），有标记时表尾打印一行图例。筛形条件与独立区完全一致：推荐时刻涨幅在甜蜜带（<2% 低吸潜伏 或 4~8% 中段启动，`NEXTDAY_SPIKE_SWEET_LOW/MID_*`）且**非 short_term/动量超买**（死亡信号）；**视觉标记 + 排序档位置顶**：不改 score / 不落库，主表、回马枪均不受影响。筛选用扫描快照 percent（与 nextday_attribution 口径同源），展示用 live_percent（实时）；辨识度复用 `c.prominence_labels` / `entry["_prominent"]`（`_entry_prominent`）。**2026-08-12：🎯 为排序档0唯一因子**——`_sort_tier` 档0 = 次日大涨画像(🎯)（甜蜜带+非超买），**辨识度退出排序**（次日大涨本身即辨识度属性），↻ 仅保留行内展示；档内仍按类别优先级→评分。后续样本足够、归因稳定后再评估是否并入评分（原方案 B）。
- K线新鲜度 (2026-07-31): 交易时段内扫描时若候选股缺今日 bar，orchestrator 会统计并打印 `今日K线缺失N只` 警告（`_fetch_all_klines`），缺 bar 的股票用旧缓存评分、下次周期（KLINE_REFRESH_TTL=120s）重试补拉；非交易时段不警告。早盘扫描若出现"上榜多、推荐 0"请先查此警告——short_term 量比按投影口径仍需今日 bar。
- 行情增强数据 (2026-08-06, `market_extra.py`): 涨停池 + 个股资金流（开关 `RTS_ENABLE_ZT_POOL`/`RTS_ENABLE_FUND_FLOW`，默认开）。**数据源**：涨停池走 AKShare `stock_zt_pool_em`（东财 push2ex）；资金流为**自实现直连东财 clist API**（`push2delay.eastmoney.com`，host 可用 `RTS_FUND_FLOW_HOST` 覆盖）——akshare `stock_individual_fund_flow_rank` 硬编码 `push2.eastmoney.com` 在本机直连/代理均实测不可达，push2delay 提供相同 API 且可达（数据可能延迟约 15 分钟）。**盘中新鲜度**：进程缓存 + DB 缓存共用 `ZT_POOL_TTL_SEC`/`FUND_FLOW_TTL_SEC=300s`，DB 条目按 `updated` 距今超过该 TTL 视为过期重拉（`get_market_extra_cache(..., intraday_ttl_sec)`）——避免首次扫描快照全天冻结；`stock_report` 不传 ttl 参数读取当日任意旧数据。**限时**：涨停池走 `_bounded_call`（daemon 线程 + join(timeout)，`ZT_POOL_FETCH_TIMEOUT=20s`）兜住 AKShare 内部无 timeout 的请求；资金流 `_fetch_fund_flow_bounded`（daemon 线程 + join `FUND_FLOW_FETCH_TIMEOUT=30s`，超时不抛错、返回已收集部分），内部 `_collect_fund_flow` 按 6 线程并行分页（服务端 pz 封顶 100，全市场 5292 只 → 53 页，实测并行 ~17s 全量），每页 timeout=10、页间查 deadline，均不会挂死 60s 扫描循环。**超时部分结果**：打警告 + 只缓存 `FUND_FLOW_PARTIAL_TTL_SEC=60s`（一扫描周期），下一轮扫描重试补全缺页——避免部分数据被冻结 5 分钟静默缺评分。**失败短退避**：彻底失败/接口返回空缓存空结果到 TTL，避免每轮扫描重复轰击死 host / 刷屏告警。**评分**：主力净占比 ≥5% → `fund_flow_bonus=+5`（**2026-08-10 归零**：回测分组强流入组 next_day -1.13% 差于无数据基线 -0.85%，momentum/short_term 内同为负——当日主力流入是追涨资金次日兑现，加分反指；仅保留 ≤-5% 的 -3 扣分 + 资金流出标签等规避语义）、≤-5% → -3；连板 2/3 板 → momentum/short_term +5/+8、≥4 板追高 -5（new_face 不参与）。**风险标签**：「资金流出」（净占比 ≤-8%）与「炸板」（炸板次数≥1）为展示型，不入 `RISK_FLAGS_HARD_FILTER`。**资金流展示图标**（2026-08-06）：`_market_extra_str`/feishu 用 `fund_flow_signal`（`display.py`）把主力净占比映射为 5 档图标替代原「资+x.x% ±xxx万」文本——`≥+8%` → `▲▲`/🟢🟢 强流入、`[+5%,+8%)` → `▲`/🟢 流入、`(-5%,+5%)` → `◇`/⚪ 中性、`(-8%,-5%]` → `▼`/🔴 流出、`≤-8%` → `▼▼`/🔴🔴 强流出，无数据不显示。阈值与 `FUND_FLOW_MAIN_PCT_STRONG/WEAK`（±5%，bonus）和 `FUND_OUTFLOW_NET_PCT`（±8%，极端档）同源，图标与加分永不矛盾。**全市场快照落库**（2026-08-06）：资金流完整拉取时把全市场结果全部写入 `market_extra_cache`（超时部分结果仍只存候选，`_last_ff_partial` 标志区分），保证盘中任一 symbol 的资金流数据可读。**综合排序资金流图标**（2026-08-06）：`display_priority` 从 `market_extra_cache` 读取当日资金流渲染图标，候选存在时优先用其扫描时维度、否则回退 DB——重启/掉榜后图标不丢（此前依赖进程内 `today_pool`，扫描器重启即大量缺失）。
- Same-sector cap: short_term list keeps top `SHORT_TERM_MAX_PER_SECTOR=2` per sector (sorted by **final** score desc, after all bonuses applied) to prevent sector-wide surge days flooding the list. **2026-08-12 改为「标记不裁剪」**（orchestrator.py `_cap_short_term_by_sector`）：超限候选打 `Candidate.sector_capped=True`（双挂票如新面孔+超短首板经 `skip_symbols` 豁免），**不再从列表移除**——数据层照常落库（recommendations 新增 `sector_capped` 列，保留回测全样本），综合排序（`get_today_recommendations` 过滤）/飞书/「超短首选」打印等对外展示隐藏。`save_recommendations` 对同日同股即使分数未刷新也会刷新 `sector_capped`（多轮扫描板块容量变化时限流状态跟随最新轮次）。Cap 必须在 `apply_all_bonuses` + `accumulate_final_score` 之后执行——`list_momentum_bonus`(±15) 等大额 bonus 会反转同板块内排名，若在 bonus 前裁剪会保留错误候选（2026-07-29 修复）。回测默认排除 `sector_capped=1`（校准"用户实际看到"的集合），`scanner.backtest --include-capped` / `portfolio_backtest --include-capped` 恢复全样本；`portfolio_backtest --rescore` 用 appearances+daily_kline 重跑引擎、无落库标记，默认全样本（限流属展示层，重扫语义下不适用）。
- Trend-label hard filter (`config.py:HIGH_RISK_TRENDS`): currently only "回踩整理" is rejected before scoring (pullback: avg next-day -3.89%, win 21.6%). "缩量回调" was removed (avg -2.09%, win 39.2% — acceptable in candidate-pool context with MA support + mild pullback).
- Composite risk flags (`enhancer.py:_set_risk_flags`): candidates carry **stackable** risk labels (not single-dim reverse indicators). Seven tags with explicit trading-decision implications, centralized thresholds in `config.py:357-400`. **HARD FILTER labels** (`config.py:RISK_FLAGS_HARD_FILTER` = {主力出货, 趋势破位}) hit → removed from ALL recommendation lists at the orchestrator list-assembly stage (display/feishu receive clean lists automatically; only buyable stocks remain). P1-7 (2026-08-10): 硬过滤同时把该 symbol 当日 recommendations 记录标 `excluded=1`（orchestrator 每轮按最新状态更新，通过者置 0），`get_today_recommendations` 排除 excluded=1——防"早先轮次落库、后续轮次被过滤"的票仍在综合排序展示。Remaining tags stay display-only warnings.
  - **超买** (overbought): BOLL %B>1.10 or KDJ J>115 or 20-day gain>60% — extreme chase-high risk (收紧自 1.0/105：旧阈值在强势股主升浪中近乎必中，导致"全民超买"误报)
  - **疲劳** (fatigue): `fatigue` penalty triggered — momentum waning after extended listing
  - **弱市** (weak market): `market_env_bonus < 0` (index < -1.0%)
  - **主力出货** (main force distribution): high-position distribution composite (high-accum + high-vol-ratio≥2.5 + flat-today / high-accum + **genuine overheated turnover >20% (turnover_bonus<0)** + extreme-overbought / opening-strong-intraday-weak + accum≥15% / spike-vol + bear divergence). 2026-07-28 收紧：原 Rule 2 仅要求换手>5%（活跃常态）+ 宽松超买，几乎把所有活跃强势股误判为出货；现要求 genuine 过热换手 + 已收紧的极端超买。Anti-flicker: `intraday_score` threshold is -1.0 (not 0.0) and `today_pct` threshold is 0.5% (not 1.0%) to prevent label flapping when real-time data oscillates near the boundary. — **HARD FILTER**: hit → removed from all recommendation lists.
  - **趋势破位** (trend breakdown): MA bear alignment / MA20 decline / MA5 break / pullback MA broken — stop-loss signal — **HARD FILTER**: hit → removed from all recommendation lists.
  - **涨幅过大** (excessive gains): accumulated ≥ threshold / pullback 20d gain penalty / momentum accumulated penalty
  - **量价背离** (volume-price divergence): volume-price mismatch including top divergence
- Display layers (`display.py` / `feishu.py`): auto-concatenate `risk_flags` with `⚠` prefix; no per-tag rendering code needed. **策略桶区下线**（2026-08-10，`display.py:display`）: 新面孔/动量/反弹/回马枪/超短 5 个策略桶区块不再单列（与综合排序重复列同一批票、每桶重复列头），`display()` 只打印压缩头部（创业板总数/过滤数/刷新间隔/大盘环境标签 `_market_env_tag`）后直入 `display_priority` 综合排序总表；回马枪变体（反转/回踩）改由综合排序独立区标签 `CB·反转/CB·回踩` 保留（`_print_priority_row`）。`display()` 签名同步精简为 `display(gem_total, interval, filtered_large_cap, conn, live_quotes, rank_map)`，`last_ranks` 追踪删除。档位分隔横幅（▶置顶档/普通档）与底部 3 行图例（建议映射/↻⚠说明/档位说明）已移除，档位排序逻辑保留（**2026-08-11 劣后档过滤已随资金流排序一并移除**）。**回马枪区只做无推荐兜底**（2026-08-11，`display.py:display_priority`）：主区（榜上五类）有推荐即不渲染「◆ 回马枪」区（用户反馈只在无推荐时才看回马枪，避免刷屏）；主区为空才兜底展示，且仅显示前 `COMEBACK_DISPLAY_MAX=10` 条（排序仍按档位→评分，净流出票不劣后过滤）。
- 综合排序分组顺序（2026-08-07 复核，`config.py:CAT_DISPLAY_PRIORITY`）: **rebound > comeback > short_term > momentum > known_new_face > new_face > pullback**。comeback 插入第 2（掉榜跟踪池，无热榜背书已用 pos_dims≥3 严筛；展示优先以观效）。2026-08-06 原为 rebound > momentum > short_term > kNF > new_face > pullback（依据全期 cum_3d：momentum +2.58 第 2）。2026-08-07 双口径复核（全期/近30天 × cum_3d/next_day）发现：近 30 天所有策略普跌（rebound 外全负，大盘 beta），其中 momentum cum_3d -4.92 垫底（与 kNF 并列）、next_day 亦 -1.20，动量策略弱市天然脆弱；而 short_term 两口径稳定（-0.58/-0.82）、IC 正效、近30天唯一接近打平 → short_term 上移至 1，momentum 仅下调一位至 2（全期 +2.58 仍第 2，不按单一近期窗口过度反应）。kNF 维持 3：next_day 近期 +0.87 系"次日冲高"，cum_3d -4.92 为 3 日高开低走，且 score IC 反指（-0.134/-0.179 双口径），类别内分数不可靠，不置顶。**建议列与优先级解耦**（`config.py:SUGGEST_BY_CAT`）：`CAT_DISPLAY_PRIORITY` 只决定展示顺序；rebound/comeback→「推荐」、new_face/momentum→「参考」、kNF/short_term→「超短」（2026-08-10 kNF 由「推荐」改——next_day +0.91 但 cum_3d -0.44 3 日回吐，语义=次日卖）、pullback→「回避」。**校准工具**：`python -m scanner.backtest --ranking [--metric cum_3d|next_day_pct] [--days N]` 输出全期+近期双窗口各类别均收益/胜率/IC，按近期均收益给建议顺序（近期样本<`RANK_MIN_SAMPLE=20` 回退全期并打「样本不足」），人工复核后更新 `CAT_DISPLAY_PRIORITY`。P1-5 (2026-08-10)：`scanner.backtest` 默认口径改 **cum_3d**（匹配"持有 2-3 天"操作，`next_day_pct` 次日单日口径会误导），报告附 **ALL(全推荐基准)** 汇总行（不挑选买入全部推荐的无选择基准；创业板指历史库内无数据，暂以此替代）并标注 cum_*d 用推荐日收盘价为起点、盘中推荐系统性高估（P1-6）。注意：`--ranking --days N` 的近期窗口建议优先采用，但需结合全期口径人工判断——近 30 天普跌环境下纯近期排序会过度惩罚 momentum/kNF（大盘 beta 而非策略退化）。**kNF 类别内反向排序**（2026-08-10）：此前"不做类别内反向"的决定被分桶数据推翻——按 score 分桶 kNF 低分档[18,37) cum_3d +5.58/64% 胜率 vs 高分档[77,98) -3.76/33%，单调反指且低分档样本 28 足够，`display.py:_score_sort_key` 与 `orchestrator.py:_new_face_sort_key` 对 kNF 改 score 升序（低调二次上榜在前），其余类别仍降序。
- 综合排序档位（2026-08-06 引入，`display.py:display_priority`; **2026-08-11 去掉资金流排序**）: 排序键 `(档位, CAT_DISPLAY_PRIORITY, 分数键)`，档位主键**跨类别全局生效**。档0置前 = 次日大涨画像(🎯)（2026-08-12 起为档0唯一因子，甜蜜带+非超买置顶；辨识度退出排序，↻ 仅行内展示）；档1 = 其余。**资金流不再参与档位/劣后过滤**（原档0含主力净流入≥5%置前、档2净流出≤-5%劣后覆盖辨识度——均移除；净流出票回到正常排序展示，仅保留图标与「资金流出」标签）。档内次键仍为类别优先级→评分（kNF 升序）。档位只改排序，不改评分列/不落库/不影响策略桶与回测。辨识度候选用 `c.prominence_labels`、掉榜行用 `get_prominence_map`——掉榜行 DB score 不含这些字段，展示层统一分档避免同表两套口径。
- **今日选股建议已下线（2026-08-11，`scanner/pick.py` 已删除）**：原功能从综合排序候选中挑 2 只买入（2026-08-10 引入）。下线依据为**忠实数据回放**（`build_pick_suggestion` 逻辑：去重/排除 excluded/类别优先级/板块去重/类内 score 降序）——pick2 全期 cum_3d -4.05%（n=85）vs 可买池基准 -0.66%（n=412），近期 -2.77% vs +0.60%，**选出的票系统性跑输随机选池**。根因：① short_term/momentum **类内 score 反指**（被选中的 top2 平均 -4.41% / -5.81%，均差于该类 ALL 均值），"类内取高分"假设不成立；② rebound 稀缺（52 交易日仅 7 天有候选，有 rebound 当天 pick +3.15%、无 rebound 的 45 天 -5.47%），大多数日子退化成挑 ST/momentum 最高分。功能违反"筛选系统不是交易系统"边界（直接给买卖指令）且历史上跑输随机，故移除显示区与模块。类别层区分度仍由综合排序类别标签/优先级承担。
- 历史推荐跟踪（`tracker.py`，2026-08-07 **已删除**，功能并入回马枪·回踩变体）: 曾查近 5 交易日推荐排除今日、硬过滤（今日±5%/累计±10%/主力净占比≤-5% fail-open）、按 6 维度买点信号分类（≥4 到买点、≥3 观察中）。现由 `comeback.py:_try_reentry_candidate` 承担（同口径，入 recommendations 表 `category="comeback"`），统一显示在「◆ 回马枪」分区，不再单独「历史推荐」分区。

## Testing

Tests use pytest with helper factories `_stock()` and `_kline()` in `tests/helpers.py` and `tests/test_analysis.py` for creating mock data. No external services required.

## Bug 检查规则（用户要求"检查 bug / 审查 / 排查问题"时必读）

用户每次让我检查 bug，都必须**完整执行以下流程**，不许只挑能看懂的部分，不许一轮只查上次改过的文件。

### 0. 触发词
「检查bug」「审查」「排查问题」「看看有没有问题」「第三/四/五轮检查」等，一律按本规则执行。

### 1. 基线必须先绿
1. `python -m compileall -q scanner unified_scanner.py stock_report.py backfill_kline.py query_summary.py query_today.py xueqiu_hot.py`
2. `python -m pytest tests/ -q`
3. 有失败的先修到全绿再开始查；全绿才代表"结论不是被坏代码掩盖"。
4. 若仓库出现 `ruff`/`mypy`/`pyright`/`pytest-cov` 配置则必须运行；**当前没有**，要在报告末尾注明"本轮纯人工审查，未用静态分析/覆盖率工具，可能仍不穷尽"。

### 2. 系统化逐模块过（按以下 7 个风险区，每轮都全过一遍）
- **数据入口/输入强制转换**：所有从 API/DB/外部取数的函数，搜 `or 0` / `or 0.0` / `get(...) or` / `or i` 模式，检查字符串/None/NaN 会不会漏进下游的数值比较与算术。参考已修复类：`_filter_gem_stocks`、`compute_surge_sentiment`、`fetch_market_caps_batch`（同一缺陷在 3 处各爆一次）。
- **K线/缓存/新鲜度**：`_fetch_all_klines`（TTL、今日bar缺失、KLINE_FETCH_DEADLINE）、`get_cached_kline`、`save_kline_to_db`、`_last_kline_fetch`。
- **并发/健壮性**：每个 `ThreadPoolExecutor`/`threading` 是否带 deadline（kline 有 `KLINE_FETCH_DEADLINE`、分时有 `MINUTE_FETCH_PHASE_DEADLINE`，**涨停池/概念/资金流是否也有**）、缓存是否带上限淘汰、共享全局（`_session_state`、`_last_kline_fetch`、api 各缓存）线程安全。
- **评分/验证**：`analysis.py` / `validator.py` / `enhancer.py` — None 处理、除零、窗口越界（`[-21]`、`[-5:]`、`closes[-6]` 等）、维度键是否存在、默认参数是否绑定模块常量导致 patch 失效。
- **掉榜/回马枪**：`comeback.py` + `watch_pool` — `rank=0` 被当榜上排名加分、`last_eval_date` 幂等、预筛 vs 策略门口径、off_list 加分豁免、`over_limit` 粘性。
- **展示/推送**：`display.py` / `feishu.py` — live_quotes 回退链、tier 排序、双挂去重、掉榜行 `_candidate` 为 None 的分支。
- **数值边界**：NaN/字符串/None/0 除/负值/空列表/单元素列表，用最小复现脚本或 fuzz 验证（参考 `_score_stock` 各 K 线长度随机输入）。

### 3. 找到 bug 后必须做「同族扩散检查」
同一根因在其它调用点是否有同构副本。方法：定位 bug 的函数 → grep 所有调用它 / 消费同一数据源 / 复制同一写法的函数 → 逐个验证。

### 4. 修复必须带可复现验证
顺序：先写最小脚本**复现**（确认真实存在）→ 修复 → 复现脚本通过 → **补 pytest 回归测试**到 `tests/` 对应文件 → 重跑全量。

### 5. 修完 re-read 被改函数的上游/下游调用者
防修复引入新破坏（如改 `_fetch_all_klines` 必须复查调用它的 `scan_with_raw` 与回马枪 lambda；改 executor 必须复查 `shutdown` 生命周期）。

### 6. 每轮必须输出统一报告表
```
| # | Bug | 位置 file:line | 严重度(严重/中/低) | 根因 | 同族扩散点 | 回归测试 | 状态 |
```
- 严重 = 崩溃/数据失真/漏推荐；中 = 性能/误截断；低 = 死代码/未用参数。
- 复核过但确认"非 bug / 设计如此"的项也要列出（注明依据），不许隐藏。
- 已知低危/死代码统一列出（如 pullback 下线后仍被调用、`_merge_from_db` 未用 cache 参数）。

### 7. 已知重点项（历次已发现，每轮必须 re-check 是否复发）
- comeback/off_list 候选 `rank=0` 被当榜上第 1 名计 TOP40 加分（`enhancer._apply_list_momentum_bonus`）
- 短 K 线 <32 根每轮重拉绕过 TTL（`orchestrator._fetch_all_klines`，2026-08-09 已修）
- API 字符串/None 未强转（`_filter_gem_stocks` / `compute_surge_sentiment` / `fetch_market_caps_batch`，已修）
- 同板块上限误把「其他」未分类股票当一组截断（`_cap_short_term_by_sector`，已修）
- 分时/开盘/量比拉取无 deadline（`_parallel_fetch`，已修；留意同批新引入的 `shutdown(wait=False)` 线程生命周期）
- `scan_with_raw` 里模块级全局（`_session_state`/`_last_kline_fetch`）跨扫描一致性

## Stock Report Tool

个股深度分析报告工具（Skill + Python脚本混合方案）。

### 使用方式

| 方式 | 命令 | 说明 |
|------|------|------|
| **AI完整报告** | `/stock-report 300319` | 跑脚本 + 我搜网络资讯 → 完整7板块报告 |
| **AI快速报告** | `/stock-report 300319 --quick` | 仅本地数据，不搜网络 |
| **独立运行** | `python stock_report.py 麦捷科技` | 不依赖AI，仅本地数据 |
| **独立快速** | `python stock_report.py 300319 --quick` | 同上 |

也支持直接问我"查一下麦捷科技" — stock-research Skill 会自动激活。

### 数据源
- 本地SQLite: 上榜历史、K线、推荐记录、板块缓存
- 雪球API: 实时行情、市值、换手率（非 --quick 模式）
- 技术指标: RSI/KDJ/MACD/Bollinger/ADX（通过 indicators.py 实时计算）
- 网络资讯: 我（AI）搜索补充（非 --quick 模式）

### 报告结构
1. 基本信息 — 代码/名称/实时价/市值/换手率/板块
2. 上榜轨迹 — 首次上榜→排名变化趋势图→连续天数
3. K线与技术面 — 收盘价/均线/RSI/KDJ/MACD/Bollinger/ADX
4. 推荐历史 — 策略分类/评分变化/关键分项
5. 量价结构 — 累计涨幅/回撤深度/量比
6. 疲劳与风险 — 连续上榜天数/超买超卖/系统警告
7. 综合评价 — 驱动逻辑 + 风险 + 位置判断


