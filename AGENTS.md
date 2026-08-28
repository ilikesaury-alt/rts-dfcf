# AGENTS.md

## What this is

A-share (创业板) stock scanner that watches the Xueqiu biaosheng (飙升) leaderboard, scores candidates across 5 strategy buckets (rebound, known_new_face, momentum, new_face, short_term), and recommends stocks with a focus on "next-day big-rise" (次日大涨) probability. SQLite-backed, dual data source (Xueqiu primary + THS fallback).

## Commands

### Run the scanner
```
python unified_scanner.py          # default 60s refresh
python unified_scanner.py 120      # custom interval
python unified_scanner.py --no-feishu  # disable Feishu push
```

### Individual tools
```
python stock_report.py 300319      # deep-dive report for one stock
python stock_report.py 麦捷科技    # name works too
python today_report.py             # daily priority-report analysis
python today_report.py --date 2026-08-20 --top 3  # historical report
python prevday_perf.py             # 30-day attribution summary
python prevday_perf.py --days 0    # full history
python backfill_kline.py           # manual kline backfill after market close
```

### Validate changes
```
python -m pytest tests/                    # unit tests (skips smoke)
python -m pytest tests/ --run-smoke        # integration (needs real scanner.db + network)
python -m pytest tests/test_weights.py     # single file
python -m pytest tests/test_weights.py::test_foo  # single test

ruff check .                               # lint
ruff format .                              # format
ruff check --fix .                         # auto-fix
mypy scanner/                              # type check (lenient, ignore_missing_imports=true)
```

### Full P&L validation after weight/scoring changes
```
python -m scanner.portfolio_backtest --compare --rescore --buy-delay 0 --buy-at open --hold-days 3
```
This rebuilds scores via `scanner/historical_rescan.py --rescore` (faithful to the live orchestrator pipeline). Read-only changes to config.py thresholds do NOT retroactively affect `recommendations` — you must use `--rescore`.

## Verification order

After code changes: `ruff check` → `mypy` → `pytest tests/` (unit) → optionally `--run-smoke`.

## Architecture

```
unified_scanner.py          # CLI entry point, main loop, DB init
scanner/
  orchestrator.py           # scan_with_raw(): candidate pool → classify → score → return ScanResult
  data_source.py            # adapter pattern: XueqiuAdapter / ThsAdapter / FallbackAdapter
  api.py                    # Xueqiu HTTP calls (session, kline, biaosheng, market caps)
  ths_api.py                # THS official finance API (K-line fallback)
  database.py               # SQLite CRUD: recommendations, appearances, daily_kline
  config.py                 # ALL thresholds, weights re-exports, env flags (single source)
  categories.py             # Category registry (CATEGORY_REGISTRY): single truth for label/color/priority/suggest
  models.py                 # KlineBar, StockInfo, Candidate, ScanResult, RecommendationRow
  analysis.py               # K-line pattern analysis (new_face/momentum/rebound/short_term scoring)
  enhancer.py               # Live enrichment: fund flow, market cap, turnover, RPS, heat amplification
  validator.py              # Post-score validation (overbought, risk flags)
  ranking.py                # Composite ranking + tier assignment (档0-3) + nextday mark
  display.py                # Terminal rendering with ANSI colors
  feishu.py                 # Feishu webhook push
  backtest.py               # Historical backtest + outcome backfill
  portfolio_backtest.py     # Portfolio-level backtest with --rescore support
  historical_rescan.py      # Re-run live pipeline on historical data (--rescore)
  nextday_attribution.py    # Next-day return attribution
  prevday_perf.py           # (top-level) Multi-day performance summary
  core_themes.py            # Core theme dip-buying opportunities
  comeback.py               # Comeback (回马枪) strategy
  concept.py                # Concept/theme board fetching (East Money F10)
  sector.py                 # Sector clustering
  weights.py                # Scoring weight tables (NEW_FACE_WEIGHTS, MOMENTUM_WEIGHTS, etc.)
  holidays.py               # Chinese trading calendar
  trading_session.py        # Trading hours detection
  indicators.py             # Technical indicators (RSI, KDJ, MACD, BOLL, etc.)
  minute_bar.py             # Intraday minute bar merging
  intraday_fetch.py         # Parallel minute data fetching
  kline_fetch.py            # Parallel K-line fetching
  features.py               # Feature engineering
  walkforward.py            # Walk-forward optimization
  market_extra.py           # ZT pool + fund flow data (East Money push2delay)
  data_health.py            # Data quality checks
  net.py                    # HTTP utilities, East Money tokens
  utils.py                  # Shared utilities (to_float, clear_screen, etc.)
  log_utils.py              # Result logging
  candidates.py             # Candidate pool management
  candidate_pool.py         # Pool selection logic
  rank_trend.py             # Rank trend tracking
  ranking_snapshot.py       # Ranking snapshot persistence
  patterns.py               # Candlestick patterns
  fundamentals.py           # Fundamentals filtering (pywencai)
scripts/                    # Analysis/verification scripts (not production)
tests/                      # pytest suite (~564 tests)
```

## Key facts an agent would miss

- **Category registry** (`scanner/categories.py`) is the single source of truth for all strategy categories. When adding/modifying a category, edit ONLY `CATEGORY_REGISTRY` there — all other modules (`config.py`, `display.py`, `backtest.py`, etc.) derive from it.
- **Config is the single source for all thresholds** (`scanner/config.py`). Do not hardcode magic numbers elsewhere. If you need a new threshold, add it to config.py and import it.
- **`scanner/config.py` re-exports** from `scanner/weights.py`, `scanner/holidays.py`, and `scanner/categories.py`. The public import path `from scanner.config import X` is used throughout the codebase — maintain backward compatibility.
- **DB file** is `scanner.db` at repo root (gitignored). Created automatically by `init_db()`.
- **Data source env vars**: `RTS_DATA_SOURCE` (auto/xueqiu/ths), `HITHINK_FINANCE_API_KEY` (for THS fallback), `RTS_FEISHU_WEBHOOK`.
- **Beijing timezone** (`BEIJING_TZ`, UTC+8) is used everywhere for time logic. Never use naive local time.
- **Fail-open design**: Most external data fetches (K-line, fund flow, concept, ZT pool) wrap in try/except and degrade gracefully. Tests should not assume external APIs are available.
- **Smoke tests** are marked with `@pytest.mark.smoke` — they need real `scanner.db` + network. Default `pytest` skips them.
- **`--rescore` is required for P&L validation**: `recommendations` table stores frozen scores from old weights. Changing thresholds in config.py does NOT retroactively change past scores. Use `--rescore` to rebuild from historical data.
- **Windows encoding**: `unified_scanner.py` sets `sys.stdout.reconfigure(encoding="utf-8")` on win32. Console output uses Chinese text + emoji.
- **Feishu webhook** (`FEISHU_WEBHOOK` in config.py) is hardcoded in source (exposed in git history). Should be rotated and moved to env var injection.
- **`pullback` category** is retired (live_produced=False) but kept in `CATEGORY_REGISTRY` for historical backtest/attribution. Do NOT remove it.
- **`.env`** file contains secrets (THS API key, Feishu webhook override). Never commit it.

## Testing notes

- 2 tests (`test_data_source.py`, `test_market_extra.py`) may fail if `pandas` is not installed (they use akshare). These are optional dependencies.
- `test_robustness.TestSuperviseLoop` failures are a known environment issue (safe-delete shim), not a regression.
- Integration tests (`--run-smoke`) require `scanner.db` with real data + network access.
