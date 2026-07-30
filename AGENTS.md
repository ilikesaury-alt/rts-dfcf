# AGENTS.md

## Quick Commands

- **Run scanner**: `python unified_scanner.py` (default 60s interval) or `python unified_scanner.py 120` (custom seconds)
- **Run tests**: `python -m pytest tests/ -v`
- **Single test**: `python -m pytest tests/test_analysis.py::TestAnalysis::test_new_face_bollinger_oversold -v`

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
  backtest.py             # Backtest / IC attribution framework
  trading_session.py      # Trading hours/holidays
  display.py              # Terminal display formatting (ANSI + wcwidth)
  feishu.py               # Feishu webhook card push
  utils.py                # Utility functions (is_gem, is_st, is_hk_stock)
  log_utils.py            # Log formatting utilities
tests/                    # pytest test suite
```

## Key Facts

- **Database**: SQLite at `scanner.db` (auto-created). Tables: appearances, daily_kline, recommendations, sector_cache
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
  - **pullback**: today_pct ≤ 0 (flat/down day only; PULLBACK_MAX_TODAY_PCT=0.0 eliminates the (0,2] dead zone), accumulated ≥ 5%, MA20 trending up (>+0.5%), volume shrinkage (<0.6x), sector active (≥3 same-sector), bollinger touch. pos_dims ≥ 2 required.
  - **rebound**: 5-day cumulative drop ≤ -10% (P0-1: relaxed from -15% on 2026-07-28 to cover "阴跌企稳" scenario where drop is 10-15% without crash day), crash day (≤-10%) is now a bonus (not hard gate), today's gain 0.5%~8% (stabilizing candle). pos_dims ≥ 2 required. No overbought veto (low-position scenario by design). Dimensions: oversold (RSI<30 / KDJ J<0 / MACD turn red), volume confirmation, sector resonance, pattern (engulfing/hammer/3-bull). Note: pattern score is computed once in `analyze_rebound` (via `detect_rebound_patterns`) and **not re-counted** in validator `total` — `_rb_pattern` only reads pre-computed dims for pos_dims gating.
  - **short_term**: vol_ratio ≥ 1.0 hard gate. Pass rule: 弱转强 (weak-to-strong, st_weak_to_strong>0) passes outright; otherwise requires pos_dims ≥ 2 AND non_sector_pos ≥ 1 (rank/MA/weak — sector cluster alone cannot pass, prevents sector-wide surge days flooding the list). If overbought, 弱转强 loses its override privilege. today_pct upper bound is 12% (P1-1: relaxed from 8% on 2026-07-28 to cover 8-12% strong stocks; 8-12% tier scores +8).
- Scoring & classification: five strategies scored **in parallel** per stock, then `_classify_category` (orchestrator.py:234-272) picks the most fitting label by **price structure** (not attempt order). New stocks: new_face > rebound > short_term > momentum. Old stocks: rebound (crash + stabilizing) → rebound; 弱转强 (weak-to-strong) → short_term; momentum > short_term > known_new_face. **pullback 下线** (2026-07-30)：不再作为分类候选，`analyze_pullback` 仍被调用但不进入分类。New IPOs that pass both new_face and short_term are **dual-listed** to both buckets, each with independent `extra` bonus (orchestrator.py:341-354).
- RPS exemption: `rebound` candidates have negative `accumulated_pct` by definition and would always fall into the bottom percentile (RPS_LOW=-3 penalty). `_compute_rps` exempts `category=="rebound"` (returns 0) to avoid penalizing the strategy's core thesis.
- RPS 口径统一：所有候选用 `accum_map`（历史5日累计涨幅，排除今日）参与 RPS 排名，与 baseline（全 GEM 监控集历史5日累计）口径一致。short_term 的 `c.kline.accumulated_pct` 仍包含今日 bar（策略语义，供评分维度用），但 RPS 排名时被 `accum_map` 覆盖，避免百分位偏高（2026-07-29 修复）。
- Same-sector cap: short_term list keeps top `SHORT_TERM_MAX_PER_SECTOR=2` per sector (sorted by **final** score desc, after all bonuses applied) to prevent sector-wide surge days flooding the list (orchestrator.py:214-231). Cap 必须在 `apply_all_bonuses` + `accumulate_final_score` 之后执行——`list_momentum_bonus`(±15) 等大额 bonus 会反转同板块内排名，若在 bonus 前裁剪会保留错误候选（2026-07-29 修复）。
- Trend-label hard filter (`config.py:HIGH_RISK_TRENDS`): currently only "回踩整理" is rejected before scoring (pullback: avg next-day -3.89%, win 21.6%). "缩量回调" was removed (avg -2.09%, win 39.2% — acceptable in candidate-pool context with MA support + mild pullback).
- Composite risk flags (`enhancer.py:_set_risk_flags`): candidates carry **stackable** risk labels (not single-dim reverse indicators). Seven tags with explicit trading-decision implications, centralized thresholds in `config.py:355-398`. **HARD FILTER labels** (`config.py:RISK_FLAGS_HARD_FILTER` = {主力出货, 趋势破位}) hit → removed from ALL recommendation lists at the orchestrator list-assembly stage (display/feishu receive clean lists automatically; only buyable stocks remain). Remaining tags stay display-only warnings.
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


