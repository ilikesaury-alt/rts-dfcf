# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

- **Run scanner**: `python limit_up_scanner.py` (default 180s interval) or `python limit_up_scanner.py 120` (custom seconds)
- **Quick data check**: `python xueqiu_hot.py` (dump raw surge ranking)
- **Self-evolution**: `python self_evolve.py` (performance report + IC dimension analysis)
- **Backfill**: `python self_evolve.py --backfill` (fill missing outcome data)
- **Weekly report**: `python self_evolve.py --report --week-start YYYY-MM-DD --week-end YYYY-MM-DD`
- **Auto-apply**: `python self_evolve.py --apply` (apply IC-based weight adjustments)
- **Kill all**: `taskkill /f /im python.exe` (Windows)

## Project Overview

An A-share stock momentum scanner that monitors Xueqiu's (雪球) "飙升榜" (surge ranking) API in real-time during trading hours. It scores and recommends ChiNext (创业板, 300xxx) stocks using two strategies:

- **New Face** (bottom breakout): First-time appearance in surge list within 3 days — looks for early-stage capital inflows with volume confirmation
- **Old Face** (consolidation/2nd wave): Recurring appearances — looks for pullback/consolidation entries on previous hot stocks

## Architecture

```
雪球API → fetch_biaosheng() → filter GEM/ST
  → batch quote API (market cap)
  → DB lookup (new vs old face)
  → fetch K-line (DB cache or Xueqiu API)
  → scoring (trend, volume, rank momentum, sector cluster, intraday strength, 小而美)
  → display + CSV log + Feishu push (optional)
```

### Data Flow (`limit_up_scanner.py`)

1. **Fetch**: `fetch_biaosheng()` — GET Xueqiu hot stock list (rank_change sorted)
2. **Filter**: Remove HK stocks, non-GEM, ST/*ST/退市; apply 小而美 (market cap ≤100亿, price ≤50元)
3. **Classify**: `get_recent_symbols()` — check DB for appearances in last 3 days → new vs old face
4. **Analyze**: `ensure_kline()` — 25-day K-line from cache or Xueqiu API, then:
   - `analyze_new_face()` — check bottom breakout signals (volume surge, accumulated gain, price range)
   - `analyze_old_face()` — check pullback health (volume shrinkage, support level, trend intact)
5. **Enhance**: Add sector cluster bonus, rank trend bonus, intraday strength score, 小而美 bonus
6. **Output**: Terminal table (color-coded), CSV log (`logs/scan_YYYY-MM-DD.csv`), optional Feishu push

### Persistence (`scanner.db` SQLite)

| Table | Purpose |
|-------|---------|
| `appearances` | Daily snapshots per stock (detect new/old face) |
| `daily_kline` | Cached K-line data (avoid redundant API calls) |
| `recommendations` | Scan recommendations with next-day % tracking |
| `sector_cache` | Stock-to-sector mapping |

### Self-Evolution Loop (`evolution/`)

1. **Tracker** (`tracker.py`): Every scan cycle backfills outcome data (1d/3d/5d forward returns) from cached K-line
2. **Analytics** (`analytics.py`): Computes Information Coefficient (IC) per scoring dimension — which weights actually predict returns
3. **Optimizer** (`optimizer.py`): Generates weekly tuning report with dimension IC analysis + weight adjustment suggestions
4. **Apply** (`self_evolve.py --apply`): Auto-adjusts weights for dimensions with |IC| < 0.05 (neutral → halve) or IC < -0.1 (anti-predictive → reduce/flip)

### Key Thresholds

- No limit (all GEM stocks from surge list), New face lookback: 3 days
- New face min score: 20, Old face min score: 10
- 小而美: Max market cap 100亿, Max price 50元
- Scoring: bottom confirmation +25, pullback +20, sector cluster up to +8, rank trend up to +6, 小而美 up to +16

### Key Files

- `limit_up_scanner.py` — Main scanner (~1300 lines, all logic in one file)
- `xueqiu_hot.py` — Standalone surge-ranking data fetcher
- `self_evolve.py` — Self-evolution entry point (weekly tuning, backfill, IC analysis)
- `STRATEGY.md` — Full strategy documentation with scoring tables
- `REVIEW.md` — Iteration history and known issues

## Non-Trading Hours

The scanner auto-sleeps outside 9:30-11:30 / 13:00-15:00 on trading days, and on weekends/holidays. Holiday list is hardcoded and needs annual updates.
