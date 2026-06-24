# AGENTS.md

## Quick Commands

- **Run scanner**: `python limit_up_scanner.py` (default 60s interval) or `python limit_up_scanner.py 120` (custom seconds)
- **Run industry chain scanner**: `python industry_chain_scanner.py`
- **Run tonghuashun scanner**: `python tonghuashun_scanner.py` (default 60s interval) or `python tonghuashun_scanner.py 120` (custom seconds)
- **Run tests**: `python -m pytest tests/ -v`
- **Single test**: `python -m pytest tests/test_analysis.py::test_new_face -v`

## Project Structure

A-share stock momentum scanner monitoring Xueqiu's surge ranking API. Scores ChiNext (300xxx) stocks using "new face" vs "old face" strategies.

```
limit_up_scanner.py       # Main scanner entry point (Xueqiu source)
industry_chain_scanner.py # Industry chain scanner entry point
tonghuashun_scanner.py    # Tonghuashun hot list scanner entry point
scanner/
  orchestrator.py         # Core scan pipeline
  analysis.py             # Scoring engines (new_face, momentum, pullback)
  validator.py            # Cross-validation (3-dim check per strategy)
  config.py               # All thresholds and weights
  api.py                  # Xueqiu API calls (biaosheng, kline, market cap)
  ths_api.py              # Tonghuashun hot list API calls
  database.py             # SQLite CRUD (appearances, kline, recommendations)
  models.py               # StockInfo, Candidate, KlineSummary dataclasses
  indicators.py           # RSI, KDJ, MACD computation
  enhancer.py             # Bonus scoring (sector, sentiment, RPS, indicators)
  candidate_pool.py       # ScanSession with list presence tracking
  rank_trend.py           # RankTracker with trajectory scoring
  sector.py               # Sector cluster detection
  trading_session.py      # Trading hours/holidays
  log_utils.py            # Log formatting utilities
  industry_chain/         # Chokepoint industry chain scanner
tests/                    # pytest test suite
```

## Key Facts

- **Database**: SQLite at `scanner.db` (auto-created). Tables: appearances, daily_kline, recommendations, sector_cache
- **Python version**: 3.12+ (uses f-strings, dataclasses, type hints)
- **Dependencies**: `requests`, `wcwidth` (see `requirements.txt`)
- **Trading hours**: Auto-sleeps outside 09:30-11:30 / 13:00-15:00 on trading days
- **Encoding**: Windows-specific `sys.stdout.reconfigure(encoding="utf-8")` for Chinese output

## Architecture Notes

- Scanner filters: GEM stocks only (300xxx), excludes ST/*ST, HK stocks, market cap >300亿, price >100元
- Three strategies: new_face (bottom breakout), momentum (trend continuation), pullback (reversion)
- Cross-validation (`validator.py`): each candidate must pass ≥2 of 3 independent dimensions before final acceptance
  - **new_face**: indicator convergence (RSI+MACD+KDJ), higher-low structure, sector resonance
  - **momentum**: MA5>10>20 alignment, no divergence, volume uniformity
  - **pullback**: MA20 trending up, volume shrinkage, sector still active
- Priority chain: primary strategy attempted first; if cross-validation fails, falls through to next strategy
- Industry chain scanner (`industry_chain/`): independent subsystem implementing a chokepoint investment thesis — detects chain phases (潜伏→形成→成长→爆发→消退), verifies bottleneck node participation, picks technically strong bottleneck stocks from active chains

## Testing

Tests use pytest with helper factories `_stock()` and `_kline()` in `tests/test_analysis.py` for creating mock data. No external services required.
