# AGENTS.md

## Quick Commands

- **Run scanner**: `python unified_scanner.py` (default 60s interval) or `python unified_scanner.py 120` (custom seconds)
- **Crash-proof run**: `python unified_scanner.py --supervise` (父进程拉起子进程，崩溃后指数退避自动重启；重启事件写入 `logs/supervisor.log`)
- **Run tests**: `python -m pytest tests/ -v`
- **Single test**: `python -m pytest tests/test_analysis.py::TestAnalysis::test_new_face_bollinger_oversold -v`

## Long-Run Robustness (P-robust)

- 主循环整个迭代体（含非交易时段等待、倒计时打印）被 `try/except` 保护，任何意外异常打印告警 + 写入 `logs/scanner_error.log` 后自动续跑，不杀进程。
- 每轮 `SELECT 1` DB 健康检查，连接损坏/锁死自动重建。
- 输出管道关闭/终端异常时 stdout 降级到 devnull（`_silence_stdout`），不崩溃。
- K 线串行拉取有 `KLINE_FETCH_DEADLINE=45s` 限时：API 故障时超时即停止补拉、剩余票回退旧缓存，单轮扫描有界不假死。
- `_request_with_retry` 用 `(REQUEST_CONNECT_TIMEOUT=5, REQUEST_TIMEOUT=15)` 双段超时，连不上的主机不再长时间挂起。
- 进程内缓存（`_MINUTE_DATA_CACHE`/`_INTRADAY_CACHE`/`_concept_ttl_cache`/`_last_kline_fetch`）上限 `CACHE_MAX_ENTRIES=2000`，超限淘汰最旧，防长跑内存膨胀。

## Project Structure

A-share stock momentum scanner merging Xueqiu surge + hot stock ranking APIs. Scores ChiNext (300xxx) stocks using "new face" / "momentum" / "pullback" / "rebound" / "short_term" strategies.

```
unified_scanner.py        # Single entry point (dual-source fusion)
stock_report.py           # Individual stock deep-dive report tool
scanner/
  orchestrator.py         # Core scan pipeline
  analysis.py             # Scoring engines (new_face, momentum, pullback, rebound, short_term)
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
  tracker.py              # History recommendation tracking (buy-point detection)
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

- **Database**: SQLite at `scanner.db` (auto-created). Tables: appearances, daily_kline, recommendations, sector_cache, concept_cache, market_extra_cache
- **Python version**: 3.12+ (uses f-strings, dataclasses, type hints)
- **Dependencies**: `requests`, `wcwidth` (see `requirements.txt`)
- **Trading hours**: Auto-sleeps outside 09:30-11:30 / 13:00-15:00 on trading days
- **Encoding**: Windows-specific `sys.stdout.reconfigure(encoding="utf-8")` for Chinese output

## Architecture Notes

- Scanner filters: GEM stocks only (300xxx), excludes ST/*ST, HK stocks, market cap >500亿, price >200元
- Five strategies: new_face (bottom breakout), momentum (trend continuation), pullback (**offline as of 2026-07-30** — cum_2d 均亏 -8.33%, 胜率 15.8%; `analyze_pullback` retained for future revival but `_classify_category` no longer returns "pullback"), rebound (oversold reversal), short_term (next-day sell)
- Cross-validation (`validator.py`): each candidate must pass ≥2 of its independent dimensions (pos_dims ≥ 2). short_term adds a "non-sector" constraint and 弱转强 override.
  - **new_face**: requires ≥1 oversold signal (indicator convergence hit OR MACD bull divergence) AND pos_dims ≥ 2. Dimensions: convergence (RSI<30 + MACD golden cross + KDJ K<20 & K>D), higher-low structure, sector resonance, volume surge.
  - **momentum**: MA5>10>20 alignment (EMA, penalty -5 if broken), no RSI divergence, volume uniformity (5-day window). pos_dims ≥ 2 required.
  - **pullback**（offline as of 2026-07-30，保留供未来恢复）: today_pct ≤ 0 (flat/down day only; PULLBACK_MAX_TODAY_PCT=0.0 eliminates the (0,2] dead zone), accumulated ≥ 5%, MA20 trending up (>+0.5%), volume shrinkage (<0.6x), sector active (≥3 same-sector), bollinger touch. pos_dims ≥ 2 required.
  - **rebound**: 5-day cumulative drop ≤ -10% (P0-1: relaxed from -15% on 2026-07-28 to cover "阴跌企稳" scenario where drop is 10-15% without crash day), crash day (≤-10%) is now a bonus (not hard gate), today's gain 0.5%~8% (stabilizing candle). pos_dims ≥ 2 required. No overbought veto (low-position scenario by design). Dimensions: oversold (RSI<30 / KDJ J<0 / MACD turn red), volume confirmation, sector resonance, pattern (engulfing/hammer/3-bull). Note: pattern score is computed once in `analyze_rebound` (via `detect_rebound_patterns`) and **not re-counted** in validator `total` — `_rb_pattern` only reads pre-computed dims for pos_dims gating.
  - **short_term**: vol_ratio ≥ 1.0 hard gate. Pass rule: 弱转强 (weak-to-strong, st_weak_to_strong>0) passes outright; otherwise requires pos_dims ≥ 2 AND non_sector_pos ≥ 1 (rank/MA/weak — sector cluster alone cannot pass, prevents sector-wide surge days flooding the list). If overbought, 弱转强 loses its override privilege. today_pct upper bound is 12% (P1-1: relaxed from 8% on 2026-07-28 to cover 8-12% strong stocks; 8-12% tier scores +8). **盘中量比口径** (2026-07-31): 盘中 K 线含今日 bar 时，今日成交量按 `min(240/已交易分钟, 10)` 投影为全天量能再算 vol_ratio (`analysis.py:_compute_volume_metrics`)，使早盘扫描可过 1.0 硬门（此前首推要等 ~11:24 后量能累积才达标）；收盘后/无今日 bar 不投影。
- Scoring & classification: five strategies scored **in parallel** per stock, then `_classify_category` (orchestrator.py:236-277) picks the most fitting label by **price structure** (not attempt order). New stocks: new_face > rebound > short_term > momentum. Old stocks: rebound (crash + stabilizing) → rebound; 弱转强 (weak-to-strong) → short_term; momentum > short_term > known_new_face. **pullback 下线** (2026-07-30)：不再作为分类候选，`analyze_pullback` 仍被调用但不进入分类。New IPOs that pass both new_face and short_term are **dual-listed** to both buckets, each with independent `extra` bonus (orchestrator.py:347-350).
- RPS exemption: `rebound` candidates have negative `accumulated_pct` by definition and would always fall into the bottom percentile (RPS_LOW=-3 penalty). `_compute_rps` exempts `category=="rebound"` (returns 0) to avoid penalizing the strategy's core thesis.
- RPS 口径统一：所有候选用 `accum_map`（历史5日累计涨幅，排除今日）参与 RPS 排名，与 baseline（全 GEM 监控集历史5日累计）口径一致。short_term 的 `c.kline.accumulated_pct` 仍包含今日 bar（策略语义，供评分维度用），但 RPS 排名时被 `accum_map` 覆盖，避免百分位偏高（2026-07-29 修复）。
- K线新鲜度 (2026-07-31): 交易时段内扫描时若候选股缺今日 bar，orchestrator 会统计并打印 `今日K线缺失N只` 警告（`_fetch_all_klines`），缺 bar 的股票用旧缓存评分、下次周期（KLINE_REFRESH_TTL=120s）重试补拉；非交易时段不警告。早盘扫描若出现"上榜多、推荐 0"请先查此警告——short_term 量比按投影口径仍需今日 bar。
- 行情增强数据 (2026-08-06, `market_extra.py`): 涨停池 + 个股资金流（开关 `RTS_ENABLE_ZT_POOL`/`RTS_ENABLE_FUND_FLOW`，默认开）。**数据源**：涨停池走 AKShare `stock_zt_pool_em`（东财 push2ex）；资金流为**自实现直连东财 clist API**（`push2delay.eastmoney.com`，host 可用 `RTS_FUND_FLOW_HOST` 覆盖）——akshare `stock_individual_fund_flow_rank` 硬编码 `push2.eastmoney.com` 在本机直连/代理均实测不可达，push2delay 提供相同 API 且可达（数据可能延迟约 15 分钟）。**盘中新鲜度**：进程缓存 + DB 缓存共用 `ZT_POOL_TTL_SEC`/`FUND_FLOW_TTL_SEC=300s`，DB 条目按 `updated` 距今超过该 TTL 视为过期重拉（`get_market_extra_cache(..., intraday_ttl_sec)`）——避免首次扫描快照全天冻结；`stock_report` 不传 ttl 参数读取当日任意旧数据。**限时**：涨停池走 `_bounded_call`（daemon 线程 + join(timeout)，`ZT_POOL_FETCH_TIMEOUT=20s`）兜住 AKShare 内部无 timeout 的请求；资金流 `_fetch_fund_flow_bounded`（daemon 线程 + join `FUND_FLOW_FETCH_TIMEOUT=30s`，超时不抛错、返回已收集部分），内部 `_collect_fund_flow` 按 6 线程并行分页（服务端 pz 封顶 100，全市场 5292 只 → 53 页，实测并行 ~17s 全量），每页 timeout=10、页间查 deadline，均不会挂死 60s 扫描循环。**超时部分结果**：打警告 + 只缓存 `FUND_FLOW_PARTIAL_TTL_SEC=60s`（一扫描周期），下一轮扫描重试补全缺页——避免部分数据被冻结 5 分钟静默缺评分。**失败短退避**：彻底失败/接口返回空缓存空结果到 TTL，避免每轮扫描重复轰击死 host / 刷屏告警。**评分**：主力净占比 ≥5% → `fund_flow_bonus=+5`、≤-5% → -3；连板 2/3 板 → momentum/short_term +5/+8、≥4 板追高 -5（new_face 不参与）。**风险标签**：「资金流出」（净占比 ≤-8%）与「炸板」（炸板次数≥1）为展示型，不入 `RISK_FLAGS_HARD_FILTER`。**资金流展示图标**（2026-08-06）：`_market_extra_str`/feishu 用 `fund_flow_signal`（`display.py`）把主力净占比映射为 5 档图标替代原「资+x.x% ±xxx万」文本——`≥+8%` → `▲▲`/🟢🟢 强流入、`[+5%,+8%)` → `▲`/🟢 流入、`(-5%,+5%)` → `◇`/⚪ 中性、`(-8%,-5%]` → `▼`/🔴 流出、`≤-8%` → `▼▼`/🔴🔴 强流出，无数据不显示。阈值与 `FUND_FLOW_MAIN_PCT_STRONG/WEAK`（±5%，bonus）和 `FUND_OUTFLOW_NET_PCT`（±8%，极端档）同源，图标与加分永不矛盾。
- Same-sector cap: short_term list keeps top `SHORT_TERM_MAX_PER_SECTOR=2` per sector (sorted by **final** score desc, after all bonuses applied) to prevent sector-wide surge days flooding the list (orchestrator.py:216-233). Cap 必须在 `apply_all_bonuses` + `accumulate_final_score` 之后执行——`list_momentum_bonus`(±15) 等大额 bonus 会反转同板块内排名，若在 bonus 前裁剪会保留错误候选（2026-07-29 修复）。
- Trend-label hard filter (`config.py:HIGH_RISK_TRENDS`): currently only "回踩整理" is rejected before scoring (pullback: avg next-day -3.89%, win 21.6%). "缩量回调" was removed (avg -2.09%, win 39.2% — acceptable in candidate-pool context with MA support + mild pullback).
- Composite risk flags (`enhancer.py:_set_risk_flags`): candidates carry **stackable** risk labels (not single-dim reverse indicators). Seven tags with explicit trading-decision implications, centralized thresholds in `config.py:357-400`. **HARD FILTER labels** (`config.py:RISK_FLAGS_HARD_FILTER` = {主力出货, 趋势破位}) hit → removed from ALL recommendation lists at the orchestrator list-assembly stage (display/feishu receive clean lists automatically; only buyable stocks remain). Remaining tags stay display-only warnings.
  - **超买** (overbought): BOLL %B>1.10 or KDJ J>115 or 20-day gain>60% — extreme chase-high risk (收紧自 1.0/105：旧阈值在强势股主升浪中近乎必中，导致"全民超买"误报)
  - **疲劳** (fatigue): `fatigue` penalty triggered — momentum waning after extended listing
  - **弱市** (weak market): `market_env_bonus < 0` (index < -1.0%)
  - **主力出货** (main force distribution): high-position distribution composite (high-accum + high-vol-ratio≥2.5 + flat-today / high-accum + **genuine overheated turnover >20% (turnover_bonus<0)** + extreme-overbought / opening-strong-intraday-weak + accum≥15% / spike-vol + bear divergence). 2026-07-28 收紧：原 Rule 2 仅要求换手>5%（活跃常态）+ 宽松超买，几乎把所有活跃强势股误判为出货；现要求 genuine 过热换手 + 已收紧的极端超买。Anti-flicker: `intraday_score` threshold is -1.0 (not 0.0) and `today_pct` threshold is 0.5% (not 1.0%) to prevent label flapping when real-time data oscillates near the boundary. — **HARD FILTER**: hit → removed from all recommendation lists.
  - **趋势破位** (trend breakdown): MA bear alignment / MA20 decline / MA5 break / pullback MA broken — stop-loss signal — **HARD FILTER**: hit → removed from all recommendation lists.
  - **涨幅过大** (excessive gains): accumulated ≥ threshold / pullback 20d gain penalty / momentum accumulated penalty
  - **量价背离** (volume-price divergence): volume-price mismatch including top divergence
- Display layers (`display.py` / `feishu.py`): auto-concatenate `risk_flags` with `⚠` prefix; no per-tag rendering code needed. Rebound list renders in CYAN with `↗` icon between momentum and short_term sections.

## Testing

Tests use pytest with helper factories `_stock()` and `_kline()` in `tests/helpers.py` and `tests/test_analysis.py` for creating mock data. No external services required.

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


