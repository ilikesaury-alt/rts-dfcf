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
```
**`--buy-at open` is rejected on purpose**（2026-08-29 修正：AGENTS 旧文档写的
`--buy-at open` 现已被前视偏差守卫挡下——信号收盘后才产生，无法以当日开盘价买入。
必须用 `--buy-at close` 对齐 cum 口径）。

This rebuilds scores via `scanner/historical_rescan.py --rescore` (faithful to the live orchestrator pipeline). Read-only changes to config.py thresholds do NOT retroactively affect `recommendations` — you must use `--rescore`.

> ⚠️ **口径提醒（2026-08-29 实测）**：排序/档位/🎯 画像全部校准于 `next_day`（次日≥7% hit），
> 但组合回测默认 `--hold-days 3`。二者不是同一目标——例：rebound 次日 +1.30%/hit 17.9%
> （全场最优）却 3 日持有 −12.80%（全场最差）。改权重/阈值前先确认你在优化哪个口径，
> 需要时加 `--hold-days 1` 单独验证次日逻辑能否覆盖交易成本。

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
 tests/                      # pytest suite (~1024 tests, 2026-08-29 实测；非 smoke 1010 通过)
```

## Key facts an agent would miss

- **Category registry** (`scanner/categories.py`) is the single source of truth for all strategy categories. When adding/modifying a category, edit ONLY `CATEGORY_REGISTRY` there — all other modules (`config.py`, `display.py`, `backtest.py`, etc.) derive from it.
- **Config is the single source for all thresholds** (`scanner/config.py`). Do not hardcode magic numbers elsewhere. If you need a new threshold, add it to config.py and import it.
- **`scanner/config.py` re-exports** from `scanner/weights.py`, `scanner/holidays.py`, and `scanner/categories.py`. The public import path `from scanner.config import X` is used throughout the codebase — maintain backward compatibility.
- **DB file** is `scanner.db` at repo root (gitignored). Created automatically by `init_db()`.
- **Data source env vars**: `RTS_DATA_SOURCE` (auto/xueqiu/ths), `HITHINK_FINANCE_API_KEY` (for THS fallback), `RTS_FEISHU_WEBHOOK`.
- **Beijing timezone** (`BEIJING_TZ`, UTC+8) is used everywhere for time logic. Never use naive local time.
- **Fail-open design — 但只捕外部故障**: Most external data fetches (K-line, fund flow, concept, ZT pool) wrap in try/except and degrade gracefully. Tests should not assume external APIs are available.
  **捕获范围必须是 `scanner.utils.EXTERNAL_FAILURES`**（OSError / 超时 / requests / `sqlite3.Error` / ValueError / KeyError），
  不要写裸 `except Exception`。2026-08-29 教训：宽泛捕获曾把一个回归测试里的
  `NameError` 吞成"通过"，该测试守护的 bug 实际从未被验证过。编程错误必须冒泡到
  `unified_scanner` 主循环兜底（记录完整 traceback 后下一轮重试）。
  仅在资源清理（`conn.close()` / 文件句柄）场景可 `except sqlite3.Error: pass`。
- **Smoke tests** are marked with `@pytest.mark.smoke` — they need real `scanner.db` + network. Default `pytest` skips them.
- **`--rescore` is required for P&L validation**: `recommendations` table stores frozen scores from old weights. Changing thresholds in config.py does NOT retroactively change past scores. Use `--rescore` to rebuild from historical data.
- **Windows encoding**: `unified_scanner.py` sets `sys.stdout.reconfigure(encoding="utf-8")` on win32. Console output uses Chinese text + emoji.
- **Feishu webhook** (`FEISHU_WEBHOOK` in config.py): source is clean since 2026-08-29 (`os.environ.get("RTS_FEISHU_WEBHOOK", "")`)，
  但 `bb7d421` 曾提交过明文 token（可在 `git log -p -S "open.feishu.cn" -- scanner/config.py` 中还原）。
  **该 bot 需轮换**；历史清理由 `git filter-repo --replace-text` 单独排期（会改写历史，需协调后 force-push）。
- **`pullback` category** is retired (live_produced=False) but kept in `CATEGORY_REGISTRY` for historical backtest/attribution. Do NOT remove it.
- **`.env`** file contains secrets (THS API key, Feishu webhook override). Never commit it.

## Testing notes

- 2026-08-29 实测：非 smoke 全绿（1023 passed / 14 skipped）。此前记录的
  `test_data_source.py` / `test_market_extra.py` / `test_robustness.TestSuperviseLoop`
  失败**已不再复现**（pandas 已安装）。若再次出现，先确认是环境问题还是真回归。
- `test_data_source.py` / `test_market_extra.py` 仍依赖可选的 `pandas`/akshare，换环境时可能失败。
- Integration tests (`--run-smoke`, 16 条) require `scanner.db` with real data + network access.
- **写测试时避免"空转断言"**：若测试依赖 mock 回调被真实调用，请显式断言调用发生
  （2026-08-29 教训：`tests/test_comeback.py` 的 stale-kline 回归测试因
  comprehension 里的 `NameError` 被上游吞掉，断言全部 vacuously pass 长达一年）。
