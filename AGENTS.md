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
python -m scanner.portfolio_backtest --compare --rescore --buy-delay 0 --buy-at close --hold-days 3
python -m scanner.portfolio_backtest --compare-horizons "1,3" --buy-delay 0 --buy-at close --hold-days-auto
```

**`--buy-at open` is rejected** — 信号收盘后才产生，无法以当日开盘价买入。必须用 `--buy-at close`。
`--hold-days-auto`（M1.1）：类别级持有期——cum_3d 语义类（comeback/core_dip）按
`config.HOLD_DAYS_BY_CATEGORY` 覆盖，next_day 靶点类沿用 `--hold-days` 基准；
`--compare-horizons "1,3"` 逐持有期跑全套对比（学术依据：涨停类信号次日高开随后反转，
next_day 靶点不应与 3 日 P&L 混算）。

### Offline label / data-quality tooling

```
python -m scanner.triple_barrier --report   # 三重屏障标签重建 + 新旧标签一致性（M2）
```

- **triple_barrier_labels 表**（M2）：(止盈 +7% / 止损 -5% / 时间 3 日) 三屏障标注，
  样本口径与 load_attribution_rows 一致（excluded=0 + 同票同日取最后一轮）；
  幂等重建，旧 next_day 标签链路不动。
- **kline_drift（M1.2，自动运行）**：unified_scanner 非交易分支每日对 daily_kline
  锚定窗口做价格 SHA256 指纹比对（雪球前复权价会被除权事件静默重算 → 回测/rescore
  跨期不可复现）；漂移即告警 + 写 logs/finalize.log（同一变更只告警一次）。
  首次自动锚定「截至昨日的 min(可用历史, 250) 根 × 当时 symbol 集合」。

This rebuilds scores via `scanner/historical_rescan.py --rescore` (faithful to the live orchestrator pipeline). Read-only changes to config.py thresholds do NOT retroactively affect `recommendations` — you must use `--rescore`.

> ⚠️ 排序/档位/🎯 画像校准于 `next_day`（次日≥7% hit），但回测默认 `--hold-days 3`。改权重/阈值前先确认优化哪个口径。

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
tests/                      # pytest suite
```

## Key facts an agent would miss

- **Category registry** (`scanner/categories.py`) is the single source of truth for all strategy categories. When adding/modifying a category, edit ONLY `CATEGORY_REGISTRY` there — all other modules derive from it.
- **Config is the single source for all thresholds** (`scanner/config.py`). Do not hardcode magic numbers elsewhere. If you need a new threshold, add it to config.py and import it.
- **`scanner/config.py` re-exports** from `scanner/weights.py`, `scanner/holidays.py`, and `scanner/categories.py`. The public import path `from scanner.config import X` is used throughout the codebase — maintain backward compatibility.
- **DB file** is `scanner.db` at repo root (gitignored). Created automatically by `init_db()`.
- **Data source env vars**: `RTS_DATA_SOURCE` (auto/xueqiu/ths), `HITHINK_FINANCE_API_KEY` (for THS fallback), `RTS_FEISHU_WEBHOOK`.
- **Beijing timezone** (`BEIJING_TZ`, UTC+8) is used everywhere for time logic. Never use naive local time.
- **Fail-open design**: External data fetches degrade gracefully. Catch only `scanner.utils.EXTERNAL_FAILURES` (OSError / timeout / requests / sqlite3.Error / ValueError / KeyError). Never bare `except Exception`. Programming errors must bubble to main loop.
- **Smoke tests** are marked with `@pytest.mark.smoke` — they need real `scanner.db` + network. Default `pytest` skips them.
- **`--rescore` is required for P&L validation**: `recommendations` table stores frozen scores from old weights. Changing thresholds in config.py does NOT retroactively change past scores.
- **Windows encoding**: `unified_scanner.py` sets `sys.stdout.reconfigure(encoding="utf-8")` on win32. Console output uses Chinese text + emoji.
- **Feishu webhook** (`FEISHU_WEBHOOK` in config.py): check for leaked tokens in git history (`git log -p -S "open.feishu.cn" -- scanner/config.py`). **Bot needs rotation.**
- **`pullback` category** is retired (live_produced=False) but kept in `CATEGORY_REGISTRY` for historical backtest/attribution. Do NOT remove it.
- **`.env`** file contains secrets (THS API key, Feishu webhook override). Never commit it.

## Testing notes

- `test_data_source.py` / `test_market_extra.py` depend on optional `pandas`/akshare — may fail on different environments.
- Integration tests (`--run-smoke`, ~16 cases) require `scanner.db` with real data + network access.
- **Avoid vacuous assertions**: If a test depends on a mock callback being called, explicitly assert the call happened.
