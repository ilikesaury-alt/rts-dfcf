# AGENTS.md

> **本文件只保留当前生效的规则/阈值/口径快照。** 全部历史决策链（「XX日已修/已下线/曾改为」的演进过程与回测证据）在 `docs/decisions.md`，溯源时读它；新决策追加到该文件对应条目末尾，本文件同步更新现状。

## Quick Commands

- **Run scanner**: `python unified_scanner.py`（默认每 60s 一轮；`python unified_scanner.py 120` 自定义间隔；`--no-feishu` 禁用飞书推送）
- **综合排序分析报告**: `python today_report.py`（今日档0🎯逐票分析+选股决策报告；`--date YYYY-MM-DD` 历史回放 / `--top N` / `--json`）。独立命令不刷屏扫描输出，配合 skill `priority-report`（`.opencode/skills/`，触发语「综合排序怎么选」「/today-report」）
- **综合排序历史复盘**: `python prevday_perf.py`（历史 N 日「综合排序各组 → 次日表现」，检验档位排序有效性；`--days 0` 全期 / `--days N` 自定义 / `--json`）。08-04 前缺超买/弱转强维度自动标注口径退化
- **档3 劣后归因**: `python scripts/tier3_reason_perf.py`（档3 按劣后原因拆桶的次日表现；优先读 ranking_snapshot 快照，无快照回退现算）
- **扣分制对照测量**: `python scripts/tier_penalty_replay.py`（档位级联 vs 扣分制分离度对照，只测不切；`--days 0` 全期）
- **K线数据修复**: `python repair_kline.py`（全表比对雪球 qfq 权威源覆盖盘中残留脏 bar；`--dry-run` 先看差异量 / `--since YYYY-MM-DD` 限窗口）
- **榜单可观测性**: `python leaderboard_obs.py`（雪球飙升榜/热搜榜逐扫描成分+排名分布；`--date` 指定日 / `--days N` 回看 / `--source hot` 切热搜榜；口径漂移自动标 `⚠`）
- **Run tests**: `python -m pytest tests/ -v`（默认跳过真实库/外网集成测试，~5s 快跑；加 `--run-smoke` 跑全部含集成，~27s）
- **Single test**: `python -m pytest tests/test_analysis.py::TestAnalysis::test_new_face_bollinger_oversold -v`
- **组合级回测**: `python -m scanner.portfolio_backtest --compare`（多策略对比：各现役类别+综合+基准）/ `--days 60`（窗口）/ `--category new_face`（单策略）/ `--export nav.csv`（导出净值序列）
- **归因回测**: `python -m scanner.backtest`（权重校准仪表盘，默认 `next_day_pct` 口径）
- **walk-forward 滚动检验**: `python -m scanner.walkforward`（结论跨周期稳定性测量；`--train 20 --test 5` 自定义窗口 / `--json`）

## Long-Run Robustness (P-robust)

- 主循环整个迭代体（含非交易时段等待、倒计时打印）被 `try/except` 保护，任何意外异常打印告警 + 写入 `logs/scanner_error.log` 后自动续跑，不杀进程。
- 每轮 `SELECT 1` DB 健康检查，连接损坏/锁死自动重建。
- 输出管道关闭/终端异常时 stdout 降级到 devnull（`_silence_stdout`），不崩溃。
- K 线串行拉取有 `KLINE_FETCH_DEADLINE=45s` 限时：API 故障时超时即停止补拉、剩余票回退旧缓存，单轮扫描有界不假死。
- `_request_with_retry` 用 `(REQUEST_CONNECT_TIMEOUT=5, REQUEST_TIMEOUT=15)` 双段超时。
- 进程内缓存上限 `CACHE_MAX_ENTRIES=2000`，超限淘汰最旧，防长跑内存膨胀。

## Project Structure

A-share stock momentum scanner merging Xueqiu surge + hot stock ranking APIs. Scores ChiNext (300xxx) stocks using "new face" / "momentum" / "rebound" / "short_term" strategies, plus "comeback"（回马枪）and "core_dip"（核心方向低吸）off-list buckets.

```
unified_scanner.py        # Single entry point (dual-source fusion)
stock_report.py           # Individual stock deep-dive report tool
scanner/
  orchestrator.py         # Core scan pipeline
  candidates.py           # 候选构建层（榜单过滤/单票评分/分类/RPS）
  intraday_fetch.py       # 分时信号并行层（三相+拉取相 deadline 并行）
  kline_fetch.py          # K 线补拉数据层（TTL 节流 + deadline 限时 + 失败兑底）
  minute_bar.py           # 分时兑底数据层（补拉失败→分时构造今日 bar）
  analysis.py             # Scoring engines (new_face, momentum, rebound, short_term)
  comeback.py             # 回马枪：掉榜跟踪池（反转 + 回踩变体）
  core_themes.py          # 核心方向低吸（主线识别 + 低吸窗口 + 落库）
  validator.py            # Cross-validation per strategy
  config.py               # Thresholds + re-export weights/holidays（阈值权威来源）
  weights.py              # 各策略权重表
  holidays.py             # 交易日历（holidays.json + 内置兜底）
  api.py                  # Xueqiu API calls (biaosheng, hot_list, kline, market cap)
  ths_api.py              # 同花顺官方 API（低频场景：涨停池/K线兜底/财务过滤/健康验证）
  data_source.py          # FallbackAdapter（雪球主源 → THS/push2delay 逐请求降级）
  database.py             # SQLite CRUD + db/dal.py
  models.py               # StockInfo, Candidate, KlineBar(TypedDict), ScanResult
  indicators.py           # RSI, KDJ, MACD, ADX, ATR, OBV, Bollinger computation
  patterns.py             # K-line pattern detection
  features.py             # Unified feature extraction (build_features)
  enhancer.py             # Bonus scoring + accumulate_final_score + risk flags
  candidate_pool.py       # ScanSession with list presence tracking
  rank_trend.py           # RankTracker with trajectory scoring
  sector.py               # Sector cluster detection
  market_extra.py         # 行情增强数据层（涨停池 THS 主源 + 资金流 push2delay 直连）
  fundamentals.py         # 基本面风险过滤层（THS 估值快照主源，pb_mrq<0 资不抵债）
  ranking.py              # 展示层排序单源助手（tier/🎯/⚡/_fresh_candidate 等）
  ranking_snapshot.py     # 综合排序档位快照落库（收盘定稿批次写入 + 复盘读取）
  data_health.py          # 数据真实性检查（跨源对账/未定稿计数/指数血缘）
  backtest.py             # Backtest / IC attribution framework
  nextday_attribution.py  # 次日大涨归因（主决策口径）
  walkforward.py          # 滚动检验（过去校准→未来验证）
  trading_session.py      # Trading hours/holidays
  display.py              # Terminal display formatting (ANSI + wcwidth)
  feishu.py               # Feishu webhook card push
  utils.py                # to_float/to_int/cache_put + is_gem/is_st/is_hk_stock
  log_utils.py            # Log formatting utilities
tests/                    # pytest test suite
```

## Key Facts

- **K 线数据契约（`models.py:KlineBar`）**：kline 统一为 TypedDict `KlineBar`（date/open/high/low/close/volume/percent + 可选 timestamp），**唯一生产入口 `make_kline_bar()`**（api/database/historical_rescan/ic_attribution/各 adapter 全部接入）。契约规则：date 必须非空字符串；close 必须能解析为正数（close<=0/None/NaN/`inf`/非法串 → 整 bar 剔除）；其余字段脏值（含 `inf`）→ 0。TypedDict 保持 dict 行为。测试：`tests/test_models.py`。
- **Database**: SQLite at `scanner.db` (auto-created). Tables: appearances, daily_kline(含 finalized 列), recommendations(含 excluded/stale_kline 列), sector_cache, concept_cache, market_extra_cache, watch_pool, scan_quality_log, leaderboard_log, market_index_log, minute_snapshot
- **Python version**: 3.12+ | **Dependencies**: `requests`, `wcwidth`（akshare/pywencai 为可选兜底，lazy import 未安装自动降级）
- **Trading hours**: Auto-sleeps outside 09:30-11:30 / 13:00-15:00 on trading days
- **Encoding**: Windows-specific `sys.stdout.reconfigure(encoding="utf-8")` for Chinese output

## Architecture Notes

- Scanner filters: GEM stocks only (300xxx), excludes ST/*ST, HK stocks, market cap >500亿, price >200元
- Four active strategies: new_face (bottom breakout), momentum (trend continuation), rebound (oversold reversal), short_term (next-day sell)。pullback 已整体删除（需恢复从 git 历史取回并重写测试）。
- Cross-validation (`validator.py`): each candidate must pass ≥2 of its independent dimensions (pos_dims ≥ 2)，超买判定透传扫描锚定日 `today_str`。
  - **new_face**: requires ≥1 oversold signal (indicator convergence hit OR MACD bull divergence) AND pos_dims ≥ 2. Dimensions: convergence (RSI<30 + MACD golden cross + KDJ K<20 & K>D), higher-low structure, sector resonance, volume surge.
  - **momentum**: MA5>10>20 alignment (EMA, penalty -5 if broken), no RSI divergence, volume uniformity (5-day window). pos_dims ≥ 2 required.
  - **rebound**: 5-day cumulative drop ≤ -10%, crash day (≤-10%) is a bonus (not hard gate), today's gain 0.5%~8%. pos_dims ≥ 2 required. No overbought veto. Dimensions: oversold / volume confirmation / sector resonance / pattern。pattern 分只在 `analyze_rebound` 算一次，validator 不重复计入 total。
  - **short_term**: vol_ratio ≥ 1.0 hard gate. Pass rule: 弱转强 passes outright; otherwise pos_dims ≥ 2 AND non_sector_pos ≥ 1（sector cluster alone cannot pass）。overbought 时弱转强失去 override。today_pct upper bound 12%（8-12% tier +8）。**盘中量比口径**：盘中 K 线含今日 bar 时今日成交量按 `min(240/已交易分钟, 10)` 投影为全天量能再算 vol_ratio（`analysis.py:_compute_volume_metrics`）；收盘后不投影。
  - **comeback（回马枪，`comeback.py`）**: 补掉榜盲区。两变体均 `category="comeback"`（`Candidate.off_list=True` + `comeback_variant`）：
    - **反转**: 掉榜票 + 历史5日跌幅 ≤ `COMEBACK_PREFILTER_5D_DROP=-8%`（DB 预筛先于行情拉取）+ 今日 2~12% 企稳。复用 `analyze_rebound(off_list=True)` + `validate(off_list=True, pos_dims≥3)`，score < `REBOUND_MIN_SCORE` 拒绝。
    - **回踩**: 近 `COMEBACK_REENTRY_DAYS=5` 交易日推荐（排除今日）且不在榜，硬过滤（今日 ±5% / 累计 ±10% / 主力净占比 ≤-5% fail-open），6 维买点信号 ≥4 才入选；评分 `BASE=40 + 15×信号数`。
    - **跟踪池 `watch_pool`**: 在榜票 upsert 保活 + 超限票（>12%）置 `over_limit=1` 入池，按 `last_list_date` 距 15 交易日剪枝；每票每交易日最多评估一次（`last_eval_date` 锚定日落库）。
  - **core_dip（核心方向低吸，`core_themes.py`）**: 大跌市找主线核心股低吸。识别：近 10 日推荐按概念聚合（持续≥3 天且相对强度≥0，取前 4）；核心股 = 核心方向内 20 日累计≥0.12 的成员；低吸窗口 = 回撤∈[-18%,-3%] / 20日涨幅≤0.60 / 今日≥-6% / 破MA20≤3% / 主力净占比≥-10%；每主题≤3 只、总量≤9。排序 `_low_buy_quality`：资金流→回撤深→龙头强→今日企稳；同 symbol 跨主题去重。落库 `category="core_dip"`，不入综合排序主表/反转移出/回踩候选域。
- Scoring & classification: 四策略并行评分后 `_classify_category` 按 price structure 选标签。New stocks: new_face > rebound > short_term > momentum；Old stocks: rebound → rebound；弱转强 → short_term；momentum > short_term > known_new_face。nf∩st 双挂双列表。comeback/core_dip 不入 `_classify_category`（榜外单独评估，同走加分+风险标签）。`_score_stock` 返回 4 元组。
- RPS exemption: `rebound` 豁免（returns 0）。所有候选用 `accum_map`（历史5日累计，排除今日）参与 RPS 排名，short_term 的含今日值被覆盖。
- 反指加分清理（现状）：**validation_bonus 只做门禁不加分**（bonus 仍写 dims 供展示/归因）；MIN_SCORE：`NEW_FACE_MIN_SCORE`=`NEW_FACE_FIRST_MIN_SCORE`=18（config 为权威）、`MOMENTUM_MIN_SCORE`=50；short_term today_pct 权重 4-6%→8、8-12%→15。
- **回测定位（必读）**：本项目是**筛选系统，不是交易系统**，回测有且只有三个合法用途：
  1. `scanner.backtest` = **权重校准仪表盘**：IC/胜率/分桶/维度 IC，用于调 MIN_SCORE、权重、`CAT_DISPLAY_PRIORITY`。
  2. `scanner.portfolio_backtest` = **可选自检尺**：等权全买的 sanity check。
  3. `prevday_perf.py` = **档位排序自检尺**：校验 4 级档位排序是否可信（display 层问题），不得用于调权重/调档位因子——调参仍走 backtest / nextday_attribution。
  **禁止**：把组合净值当"实盘收益预测"、拿回测调权重（过拟合）、当"能不能赚钱"的裁判。三个模块都不进实时扫描路径。
- **开放假设清单（持续迭代的「待行动」队列）**：回测负责测量，清单负责何时行动。样本达标（≥门槛）且复查仍成立才行动（改 config → 复跑验证 → 记决策链）；小样本差异一律视为噪声。SOP：每次迭代先查清单。

  | 假设 | 证据 | 最小样本 | 当前样本 | 状态 |
  | ------ | ------ | --------- | --------- | ------ |
  | momentum 甜蜜带分型拖后腿 | 档0 内 momentum 子集 hit 4.2%/均-1.48% vs 档0 整体 14.5% | 80 | 24 | 观察中 |
  | 8-10% 陷阱带近端反转 | 全期 7.5%陷阱 vs 近30天 14.6%最好 | 100 | 41 | 观察中 |
  | 低分反指近端减弱 | score<30 桶全期 16.7%最强，近30天仅 1 条 | 60 | 1 | 样本不足无法验证 |
  | short_term 弱转强子集衰减？ | 近30天 hit 11.9% vs 全期 15.8% | 100 | 59 | 观察中 |
  | 档位扣分制优于级联 | 扣2桶 hit 12.8% 非单调（`tier_penalty_replay` 首跑 min桶 n=28） | 100 | 28 | 观察中·维持级联 |

  已落地结论（不回清单）：rebound 最强类别、弱转强∩非超买有效、超买死亡信号、档位排序有效（档0>档3）、被移出票负收益（排除有效）。**调参以 hit 口径为准，勿被 `backtest --ranking` 均收益建议带偏**（momentum/comeback 两案例）。
- **次日大涨归因（`scanner/nextday_attribution.py`，唯一决策口径）**：用户偏好 next_day ≥7%。2026-08-18 起综合排序优先级/档位/建议列全部按本口径校准（`backtest` 默认 metric 同步），cum_3d 仅作对照不参与调参。用法：`python -m scanner.nextday_attribution [--days N] [--threshold 7] [--csv prefix]`。关键结论：甜蜜带 <2%/4-8%；8-10% 是陷阱带（非 short_term）；score<30 低分反指；rebound 最强（hit 32%）；弱转强∩非超买有效；辨识度曾是最强单因子（现 ↻ 已下线仅 today_report 归因用）。调参即跑本模块 + `scanner.backtest` 复核。
- **🎯 次日大涨标记（`ranking._is_nextday_marked`，档0 唯一因子）**：不改 score/不落库。类别差异走规格表 `NEXTDAY_CAT_SPECS`（2026-08-26 数据驱动收口，键集合 ≡ `categories.NEXTDAY_CAT_PRIORITY`，测试守护）。分型：**short_term 要求弱转强+非超买**；其余类别甜蜜带+非超买+5日累计≥`NEXTDAY_ACCUM_MIN=6.0`（**rebound/short_term 豁免累计门槛**）。累计数据源回退链：候选池 `accumulated_incl_today`（含今日口径）→ `daily_kline` 按推荐日回放现算 → DB 落库值兜底 → 三源皆缺失 fail-open 放行。排序预计算 mark map 防 kline 回放全表扫描。
- **K线新鲜度与定稿**：交易时段候选缺今日 bar 打印 `今日K线缺失N只` 警告（旧缓存评分、TTL=120s 重试；早盘"上榜多推荐 0"先查此警告）。评分基于缺今日 bar 旧缓存 → `stale_kline=True` 标记 + 落库审计。**收盘定稿**：主循环非交易时段每日自动 `_finalize_today_klines`（等至 15:02、幂等、fail-open、写 `logs/finalize.log`）；盘中写入的今日 bar `finalized=0`、收盘后置 1；`backfill_kline.py` 回填筛选含今日。
- **数据真实性检查（`scanner/data_health.py`）**：本地契约抓不到自洽脏数据，唯一可靠是跨源比对。`check_kline_health` 抽样交叉验证（THS 主参照容差 0.5%、新浪 qfq 回退），不符比例≥30% 阻断；`count_unfinalized_today`；`check_market_index_health`（bar 日期滞后 / 涨幅 vs 东财 push2delay 偏差超 0.5pp 告警）。`nextday_attribution`/`prevday_perf` 出报告前自动跑（`--force` 逃生口）。
- **扫描数据血缘日志（`scan_quality_log` 表）**：每轮落库 `{gem_count, fetch_failed, today_bar_missing, minute_fallback, stale_recs}`，同日多轮按最新覆盖。排查"为什么没推荐/推荐异常"先查当日快照。测试 `TestSaveScanQuality`。
- **榜单可观测性（`leaderboard_log` 表 + `leaderboard_obs.py`）**：雪球榜单逐扫描成分+排名分布时序，检测上游口径漂移（中位涨幅突变≥1pp/重叠率<0.3/条数变动≥30% 标 ⚠）。**上游枯竭=市场信号，不做对冲**（不接备用榜、不硬找票）；只需区分市场性枯竭 vs 口径性枯竭（由本表 + scan_quality_log 覆盖）。
- **分时快照落库（`minute_snapshot` 表）**：每轮把最终候选 `{symbol, price, pct}` 采样进时间序列（PK(date,time,symbol) 同刻覆盖），脏值行剔除、fail-open；自动剪枝保留 60 交易日。
- **数据源拓扑（2026-08-23 收敛，外部依赖 3 个）**：① 雪球 = 核心热路径；② THS 官方（`ths_api.py`，Key 存 `.env` 的 `HITHINK_FINANCE_API_KEY`）= 低频场景：涨停池主源 / data_health 第一交叉验证源 / 财务风险过滤主源 / K线兜底适配器，软限流 ~3.8 req/s **不进盘中热路径**，时区统一 BEIJING_TZ；③ 东财 push2delay = 资金流 clist / 市值 ulist / 指数 f170 对账（THS 无对应字段）。akshare/pywencai 降级可选兜底。auto 模式双源配置恒构造 `FallbackAdapter`（逐请求降级、运行期雪球恢复自动回主源，防启动抖动锁死 THS-only）。市值链路：雪球 400016 会话自愈 → 仍空则 push2delay ulist 兜底。大盘指数 `fetch_market_index` 用 count=5 取当日 bar + 血缘落库。
- **行情增强数据（`market_extra.py`）**：涨停池（THS 主源，AKShare 兜底）+ 资金流（push2delay 直连，~15min 延迟）。进程+DB 缓存共用 TTL 300s；涨停池/资金流均有 daemon 线程限时（20s/30s），资金流部分结果短 TTL 60s 下轮补全，失败短退避；executor 显式 `shutdown(wait=False, cancel_futures=True)`。**评分**：主力净占比正向加分已归零（反指），仅 ≤-5% → -3；连板 2/3 板 momentum/short_term +5/+8、≥4 板 -5。「资金流出」（≤-8%）与「炸板」标签展示型不入硬过滤。**图标**（中性档不显示）：`fund_flow_signal` 映射 ▲▲/▲/▼/▼▼（阈值 ±5%/±8% 与 bonus 同源）。
- **基本面风险过滤（`fundamentals.py`）**：排除式过滤器，不做加分。主源 = THS 估值快照 `pb_mrq<0`（资不抵债；pb=null 不误杀），跨轮增量拉取（进度 CAS 推进、锁不跨网络 I/O）；兜底 pywencai（lazy import）。命中打 `财务风险` 硬过滤标签。进程 TTL 86400s / 失败 60s 退避 / DB 复用 market_extra_cache；开关 `RTS_ENABLE_FUND_RISK` 默认开。
- Same-sector cap 已移除（防洪峰护栏实测系统性选出最差票）。
- Trend-label hard filter (`config.py:HIGH_RISK_TRENDS`): 仅 "回踩整理" rejected before scoring（惰性防线）。
- **Composite risk flags (`enhancer.py:_set_risk_flags`)**: stackable labels，阈值集中在 `config.py`。**HARD FILTER**（`RISK_FLAGS_HARD_FILTER` = {主力出货, 趋势破位, 财务风险, 弱转强失效}）hit → 移出全部推荐列表 + 当日 recommendations 标 `excluded=1`（`get_today_recommendations` 排除）；置回按 (date,symbol,category) 精确匹配（`_update_excluded_marks`，防复活反转移出旧行）。完整标签集（10 个）：超买/疲劳/弱市/主力出货/趋势破位/涨幅过大/量价背离/资金流出/炸板（展示）+ 财务风险/弱转强失效（硬过滤）。
  - **超买**: BOLL %B>1.10 or KDJ J>115 or 20-day gain>60%
  - **疲劳**: fatigue penalty triggered
  - **弱市**: `market_env_bonus < 0` (index < -1.0%)
  - **主力出货**（硬过滤）: 高位分发复合信号（高累计+量比≥2.5+平盘 / 高累计+真实过热换手>20%+极端超买 / 开强走弱+accum≥15% / spike-vol+顶背离）；anti-flicker 阈值 intraday_score -1.0 / today_pct 0.5%
  - **趋势破位**（硬过滤）: MA bear alignment / MA5 break — stop-loss signal
  - **涨幅过大**: accumulated ≥ threshold；**量价背离**: volume-price mismatch incl top divergence
- **Display layers (`display.py`/`feishu.py`)**: risk_flags 自动 `⚠` 前缀拼接。策略桶区已下线，`display()` 压缩头部后直入 `display_priority` 综合排序总表；回马枪变体由独立区标签 CB·反转/CB·回踩 保留。**回马枪/核心低吸区显示条件 = 主区（榜上五类）推荐 ≤ `COMEBACK_DISPLAY_MIN_MAIN=3` OR 大盘弱势（`_market_is_weak`：market_index_log 优先/候选 dims 回退/fail-open 按弱势）**；回马枪区最多 `COMEBACK_DISPLAY_MAX=10` 条，区内排序 `ranking.comeback_sort_key`（主力净占比降序，flow 缺失 to_float default=None 后经 flow_map 补值，次键评分），display/orchestrator/today_report 三端同源。**掉榜快照守卫**：stale 候选与双挂票类别错位统一经 `ranking._fresh_candidate` 拦截（视同无候选降级读 DB），percent/涨幅/🎯/⚡/dims 判定全走该守卫。
- **综合排序分组顺序（`config.py:CAT_DISPLAY_PRIORITY`，next_day 口径校准）**: **rebound > known_new_face > momentum > new_face > short_term > comeback > pullback**（hit 依据 28.6/12.7/10.2/9.6/8.4/3.3%）。**建议列解耦**（`config.py:SUGGEST_BY_CAT`）：rebound/kNF→推荐、momentum/new_face/short_term→参考、comeback→回马、pullback→回避。**kNF 类别内反向排序**（score 升序，低调二次上榜在前；`display._score_sort_key` 与 `orchestrator._new_face_sort_key` 同步）。校准工具：`python -m scanner.backtest --ranking [--metric next_day_pct]`（近期样本 <20 回退全期），人工复核后更新。
- **综合排序档位（`ranking`/`display.py:_entry_tier`，纯排序层）**: 排序键 `(档位, CAT_DISPLAY_PRIORITY, 分数键)`，跨类别全局。档0 = 🎯 次日大涨画像；档1 = 强信号（rebound）；档2 = 普通；档3 = 警示劣后（累计≥50% 过热**优先于 🎯** / 超买 / 小板块共振 cnt<15 / 2-4%死区 / momentum·new_face 的 8-10%陷阱（short_term 豁免）/ 资金流出≤-8%；short_term 与 comeback 不看涨幅带）。回放验证单调有效：档0 15.4% > 档1 10.5% > 档2 9.2% > 档3 6.7%。↻ 辨识度行内展示已下线（prominence 数据仍供 today_report 归因）。
- **蓄势突破观察画像（⚡ 纯展示层标记，不参与排序/评分/落库）**：类别门单源 `ranking._breakout_profile_key`（⚡=new_face/kNF/首推、⚡R=非首推 short_term，dispatcher 按构造互斥；2026-08-26 前两门分散在两谓词内靠注释约束），结构条件共用 `_breakout_structure_ok`（前5日横盘+T-1缩量+回调至20日高点下方+MA多头），渲染合并单一青色 ⚡。fail-closed。**⚠️ 样本仅 20 只，按开放假设清单 SOP 先观察，达标后经 nextday_attribution 复盘再决定升级**。回放注意：收盘后回放需模拟推荐时刻价格（含推荐日累计会被自身涨幅推高）。
- **walk-forward 滚动检验（`scanner/walkforward.py`）**：核心校准结论滚动 train→test 验证方向稳定性 + 档位单调性复现。首跑：🎯 画像方向稳定、档位单调全窗口复现；甜蜜带+非超买方向不稳（继续观察）。定位：纯离线测量，不调参不落库不进扫描路径。
- 今日选股建议（pick.py）已下线：历史上系统性跑输随机选池，违反"筛选系统不是交易系统"边界，勿恢复。
- **综合排序分析报告（`today_report.py` + skill `priority-report`）**：档0 逐票分析独立命令，display 渲染路径零改动；评级纯展示层（正向：rebound/弱转强∩非超买/甜蜜带+累计≥6/kNF；风险：尾盘回吐/RSI顶背离/主力净流出≤-8%/超买/疲劳/8-10%陷阱带）。`get_market_extra_cache`/`get_fund_flow_pct_map` 支持 `as_of` 历史回放。
- **综合排序历史复盘（`prevday_perf.py`）**：档位/🎯 逐日重建（today_report 同源），表现用落库 next_day_pct，hit/均值复用 `nextday_attribution._hit_stats` 防漂移。不得用于调权重/档位因子。
- **综合排序档位基建（2026-08-26 重构一期）**：① 档3 劣后原因单源 `ranking.entry_tier_reasons`（`_entry_tier`/today_report 档3 汇总/`tier3_reason_perf` 三处同源；阈值 `OVERHEAT_ACCUM_MAX=50`/`FUND_OUTFLOW_NET_PCT` 入 config）；② tier_map/mark map 复合键 `(symbol, category)`，nf∩st 双挂票档位显式以 short_term 行判定为准；③ 分数方向入类别注册表 `SCORE_DESCENDING_BY_CAT`。**档位快照落库（`ranking_snapshot.py` + `ranking_snapshot` 表，schema v2）**：收盘定稿批次内 `_persist_ranking_snapshot_once` 全量回放写入（幂等 fail-open 记 finalize.log）——ranking 判定演进不篡改历史归因；`tier3_reason_perf` 优先读快照、无快照回退现算。扣分制对照测量 `scripts/tier_penalty_replay.py`（只测不切，见开放假设清单）。**重构二期（同日，画像注册表化，原 Phase 4）**：⚡ 类别门收口 `_breakout_profile_key` dispatcher（两变体按构造互斥由代码保证）；🎯 类别差异收口 `NEXTDAY_CAT_SPECS` 规格表——判定语义零变化（stash 零差异 + 行为矩阵测试 `tests/test_profile_registry.py` 守护）。
- **推荐后快速反转移出（`database.py:mark_reversed_recommendations`）**：今日已推荐（不含回马枪）但当前不在候选池的票，回落 `drop = high_pct − live`（ref 优先当日最高涨幅，缺失回退推荐时刻涨幅）：① 转负且 drop≥5.0；② drop≥10.0 无论红绿 → `excluded=1` 移出展示（保留落库）+ `[反转移出]` 告警。阈值来自全量回落分布校准（p75≈4.5/p95≈10.5），不可为单票凑参。当前候选每轮 `passed_syms` 置回 0；行情缺失 fail-open；仅展示层生效（回测/归因不过滤 excluded）。

## Testing

Tests use pytest with helper factories `_stock()` and `_kline()` in `tests/helpers.py` and `tests/test_analysis.py`. No external services required. 真实 `scanner.db` / 外网的集成测试（共 11 个：`test_historical_rescan` 全部、`test_backtest` 3 个真实库用例、`test_nextday_attribution.test_real_db_smoke`、`test_portfolio_backtest` 2 个真实库用例）标 `@pytest.mark.smoke`，默认跳过，显式 `--run-smoke` 才跑。

## Bug 检查规则（用户要求"检查 bug / 审查 / 排查问题"时必读）

触发词：「检查bug」「审查」「排查问题」「看看有没有问题」等。必须走完整流程，不许只查改过的文件。

### 1. 基线必须先绿

1. `python -m compileall -q scanner unified_scanner.py stock_report.py backfill_kline.py query_summary.py query_today.py xueqiu_hot.py`
2. `python -m pytest tests/ -q`
3. `python -m ruff check scanner/ unified_scanner.py stock_report.py backfill_kline.py query_summary.py query_today.py xueqiu_hot.py`（必须全绿）
4. 有失败先修到全绿再查。`mypy` ~94 条类型债 backlog 非阻断，报告末尾注明即可。

### 2. 系统性过风险区（每轮全过一遍）

- **数据入口/强制转换**：搜 `or 0` / `or 0.0` / `get(...) or` / `or i` 模式，字符串/None/NaN 不得漏进数值比较与算术。
- **K线/缓存/新鲜度**：`kline_fetch.fetch_all_klines`（TTL/今日bar缺失/KLINE_FETCH_DEADLINE）、`get_cached_kline`、`_last_kline_fetch`。
- **并发/健壮性**：ThreadPoolExecutor 是否带 deadline、显式 `shutdown(wait=False, cancel_futures=True)`、缓存上限淘汰、共享全局线程安全。
- **评分/验证**：analysis/validator/enhancer/candidates — None 处理、除零、窗口越界（`[-21]`、`closes[-6]`）、维度键缺失、默认参数绑定模块常量。
- **THS 官方 API 限流**：~3.8 req/s——任何调用点不得塞进盘中热路径（只允许：涨停池/财务过滤/健康验证/K线兜底）。
- **掉榜/回马枪**：comeback + watch_pool — `rank=0` 加分、`last_eval_date` 幂等、off_list 加分豁免、`over_limit` 粘性。
- **展示/推送**：display/feishu — live_quotes 回退链、tier 排序、掉榜行 `_candidate` 为 None 分支、`_fresh_candidate` 守卫覆盖。
- **数据源交叉验证**：任何落库数值先问「来自哪个源、是否被独立源验证过」。本地契约检查抓不到自洽脏数据，唯一可靠是跨源比对（data_health 三件套，见上）。
- **数值边界**：NaN/None/0 除/空列表，用最小复现脚本或 fuzz 验证。

### 3. 修复流程

同族扩散检查（grep 同根因其他调用点逐个验证）→ 最小脚本复现 → 修复 → 复现通过 → 补 pytest 回归测试 → 重跑全量 → re-read 被改函数的上游/下游调用者防新破坏（如改 executor 复查 `shutdown` 生命周期）。

### 4. 每轮输出统一报告表

```
| # | Bug | 位置 file:line | 严重度(严重/中/低) | 根因 | 同族扩散点 | 回归测试 | 状态 |
```

严重 = 崩溃/数据失真/漏推荐；中 = 性能/误截断；低 = 死代码/未用参数。复核过但确认"非 bug / 设计如此"的也列出（注明依据），不许隐藏。

### 5. 已知重点项（历次发现，每轮 re-check 是否复发）

- comeback/off_list 候选 `rank=0` 被当榜上第 1 名计 TOP40 加分（`enhancer._apply_list_momentum_bonus`）
- comeback/off_list 候选市值富集：`c.market_cap`（元）与 `c.stock.market_cap`（亿元）两套单位字段都要补
- `api.fetch_kline` 时间戳强转：None/str/0/负值应逐根跳过，不拖垮整只票
- 短 K 线 <32 根每轮重拉绕过 TTL（`kline_fetch.fetch_all_klines`）
- API 字符串/None 未强转（`_filter_gem_stocks` / `compute_surge_sentiment` / `fetch_market_caps_batch`）
- 分时/开盘/量比拉取无 deadline（留意 `shutdown(wait=False)` 线程生命周期）
- `scan_with_raw` 模块级全局（`_session_state`/`_last_kline_fetch`）跨扫描一致性

## Stock Report Tool

个股深度分析报告工具（Skill + Python脚本混合方案）。

### 使用方式

| 方式 | 命令 | 说明 |
| ------ | ------ | ------ |
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
