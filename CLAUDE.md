# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

- **Run scanner**: `python limit_up_scanner.py` (default 60s interval) or `python limit_up_scanner.py 120` (custom seconds)
- **Run tests**: `python -m pytest tests/ -v`
- **Run backtest**: `python backtest.py` or `python backtest.py --live`
- **Grid search**: `python backtest.py --optimize`
- **Chain watch**: `python chain_watch.py` (single run) or `python chain_watch.py --interval 300` (every 5 min)
- **Quick data check**: `python xueqiu_hot.py` (dump raw surge ranking)
- **Self-evolution**: `python self_evolve.py` (performance report + IC dimension analysis)
- **Backfill**: `python self_evolve.py --backfill` (fill missing outcome data)
- **Weekly report**: `python self_evolve.py --report --week-start YYYY-MM-DD --week-end YYYY-MM-DD`
- **Auto-apply**: `python self_evolve.py --apply` (apply IC-based weight adjustments)
- **Kill all**: `taskkill /f /im python.exe` (Windows)

## Project Overview

An A-share stock momentum scanner that monitors Xueqiu's (雪球) "飙升榜" (surge ranking) API in real-time during trading hours. It scores and recommends ChiNext (创业板, 300xxx) stocks using three strategies:

- **New Face** (bottom breakout): First-time appearance in surge list within 3 days, today_pct ≤ 8% — looks for early-stage capital inflows with volume confirmation
- **Momentum** (trend continuation): Stocks with 10%+ 5-day gain ≤ 8% today — fills the gap between New Face and Old Face (Old Face was removed 2026-06-10)
- **Pullback** (回调介入, 2026-06-22): Strong-momentum stocks (accum ≥ 5%) on a pullback day (-8% < today_pct ≤ 2%) — low-entry reversion play

A companion tool `chain_watch.py` monitors the same surge list for industrial chain (产业链) trend signals — detects which chains are heating up (AI算力/半导体/新能源车/光伏储能/机器人/低空经济/军工) and scores individual stocks by MA alignment, pullback health, volume trend, and bottleneck position.

## Architecture

```
雪球API → fetch_biaosheng() → filter GEM/ST
  → batch quote API (market cap)
  → DB lookup (check if new face)
  → fetch K-line (DB cache or Xueqiu API, 45-day)
  → scoring (new_face → momentum → pullback → known_new_face)
  → enhance (sector cluster, rank trend, list momentum, sentiment, RPS, indicators)
  → display + CSV log
```

### Backtest (`backtest.py` → `scanner/backtest/`)

- **Engine** (`engine.py`): Loads historical appearances + K-line from SQLite, calls production `analyze_new_face`/`analyze_momentum` directly — no separate scoring logic
- **Grid search** (`grid_search.py`): Iterates over flat weight dict overrides (`NEW_FACE_WEIGHTS`/`MOMENTUM_WEIGHTS` keys) to find optimal parameters
- **Params format**: `{"new_face": {"today_pct_2_6": 25, ...}, "momentum": {"accum_10_15": 19, ...}}`
- **Usage**: `python backtest.py --optimize` or `python backtest.py --params custom.json --live`

### Data Flow (`limit_up_scanner.py`)

1. **Fetch**: `fetch_biaosheng()` — GET Xueqiu hot stock list (rank_change sorted)
2. **Filter**: Remove HK stocks, non-GEM, ST/*ST/退市; apply 小而美 (market cap ≤300亿, price ≤100元)
3. **Classify**: `get_recent_symbols()` — check DB for appearances in last 3 days → new vs known
4. **Analyze**: `ensure_kline()` — 45-day K-line from cache or Xueqiu API, then:
   - `analyze_new_face()` — check bottom breakout signals (volume surge, accumulated gain, price range)
   - `analyze_momentum()` — check trend continuation (accumulated gain ≥10%, no crash days); also used as fallback for non-new-face stocks
4. **Analyze**: `ensure_kline()` — 45-day K-line from cache or Xueqiu API, then:
   - `analyze_new_face()` — check bottom breakout (today_pct ≤ 8%, accum range, volume surge, bottom confirmation)
   - `analyze_momentum()` — check trend continuation (accum ≥ 10%, no crash days, today_pct ≤ 8%)
   - `analyze_pullback()` — check pullback entry (accum ≥ 5%, today_pct ≤ 2%, no crash, MA support)
   - `analyze_new_face()` fallback — known stocks that fail all three get a second chance as known_new_face
5. **Enhance**: `apply_all_bonuses()` adds sector cluster, rank trend, list momentum, sentiment cycle, RPS, live vol, turnover, time bonus, plus:
   - **List momentum** (2026-06-22): Consecutive list presence (2+/3+/5+ streaks) + rank trajectory + Top40 bonus
   - **Market sentiment cycle**: Phase derived from surge list top100 stats (boiling/warm/cool/frozen), applied uniformly
   - **RPS relative strength**: Per-category ranking (within new_face or momentum) by 5-day accumulated return, top 20% get bonus
   - **Technical indicators**: RSI(6)/KDJ(9,3,3)/MACD(12,26,9) from K-line — oversold for new_face/pullback, trend confirmation for momentum
6. **Disabled**: `intraday_score` (IC=-0.307) and `momentum_kdj` (IC=-0.369) — fetched but not scored
7. **Output**: Terminal table (color-coded), CSV log (`logs/scan_YYYY-MM-DD.csv`)

### Persistence (`scanner.db` SQLite)

| Table | Purpose |
|-------|---------|
| `appearances` | Daily snapshots per stock (detect new/old face) |
| `daily_kline` | Cached K-line data (avoid redundant API calls) |
| `recommendations` | Scan recommendations with next-day % tracking |
| `sector_cache` | Stock-to-sector mapping |
| `parameter_snapshots` | Self-evolution weight versioning (active params) |

### Self-Evolution Loop (`evolution/`)

1. **Tracker** (`tracker.py`): Every scan cycle backfills outcome data (1d/3d/5d forward returns) from cached K-line
2. **Analytics** (`analytics.py`): Computes Information Coefficient (IC) per scoring dimension — which weights actually predict returns
3. **Optimizer** (`optimizer.py`): Generates weekly tuning report with dimension IC analysis + weight adjustment suggestions
4. **Apply** (`self_evolve.py --apply`): Auto-adjusts weights for dimensions with |IC| < 0.05 (neutral → halve) or IC < -0.1 (anti-predictive → reduce/flip)

### Key Thresholds

- No limit (all GEM stocks from surge list), New face lookback: 3 days
- New face min score: 18, Momentum min score: 15, Pullback min score: 18
- 小而美: Max market cap 300亿, Max price 100元
- New Face filter: today_pct ≤ 8% (hard reject via MAX_NEW_FACE_TODAY_PCT)
- Momentum filter: today_pct ≤ 8% (hardcoded), accum ≥ 10%, no crash day
- Pullback filter: today_pct ≤ 2% and > -8%, accum ≥ 5%, no crash day
- Scoring: bottom confirmation +8~10, sector cluster up to +8, rank trend -4~+10
- New face scoring: today_pct range (+2~+20), accumulated range (-15~+10), volume (+12~+15), vol_rank_combo (+8~+15)
- Momentum scoring: today_pct range (+2~+26), accumulated range (-15~+19), volume (-5~+5), no_crash (+13)
- Pullback scoring: today_pct range (+5~+15), accumulated range (-10~+18), volume (0~+12), no_crash (+13), MA support (+12), rank (+5~+8), RSI/MACD (+3~+5)
- Sentiment bonus: boiling +5, warm +2, cool -2, frozen -5 (from surge list stats)
- RPS: within-category 5d-return percentile ranking, top 20% +4, mid 60% +2, bottom 30% -3
- Indicators: RSI(6)/KDJ/MACD per-side signal, each +3 base weight (evolvable), up to +9 per stock
- List momentum: streak 2→+3, 3→+5, 5→+8, trajectory +2, Top40 +3, Top20 extra +2

### Key Files

- `limit_up_scanner.py` — Main scanner entry point (~100 lines, orchestrator loop)
- `scanner/orchestrator.py` — Core scan orchestration (~350 lines, pipeline in one file)
- `scanner/analysis.py` — New Face, Momentum & Pullback scoring engines (production functions called by backtest)
- `scanner/config.py` — All thresholds, weights, and dimension-to-key mappings
- `scanner/candidate_pool.py` — ScanSession with list_presence tracking
- `scanner/enhancer.py` — Bonus application (sector, list momentum, sentiment, RPS, indicators)
- `scanner/rank_trend.py` — RankTracker with trajectory_score for list momentum
- `scanner/api.py` — Xueqiu API interaction (biaosheng, kline, batch quote, intraday)
- `scanner/indicators.py` — RSI/KDJ/MACD pure functions
- `scanner/database.py` — SQLite CRUD (appearances, kline, recommendations, snapshots)
- `scanner/log_utils.py` — Log formatting utilities
- `scanner/evolution/` — Self-evolution: tracker, analytics (IC), optimizer
- `scanner/backtest/` — Backtest engine (calls production scoring directly), grid search, reporting
- `backtest.py` — Backtest entry point (--live, --optimize, --params)
- `self_evolve.py` — Self-evolution entry point (weekly tuning, backfill, IC analysis)
- `scanner/chain_watch/` — Chain watch: chains.py (7-chain knowledge base), heat_detect.py, trend_score.py, display.py
- `chain_watch.py` — Chain watch entry point (standalone, not part of scan loop)
- `STRATEGY.md` — Full strategy documentation with scoring tables
- `OPTIMIZE.md` — Iteration history and known issues
- `requirements.txt` — Python dependencies (requests, wcwidth)

## Non-Trading Hours

The scanner auto-sleeps outside 9:30-11:30 / 13:00-15:00 on trading days, and on weekends/holidays. Holiday list is hardcoded and needs annual updates.


