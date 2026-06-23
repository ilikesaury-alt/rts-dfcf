# AGENTS.md

## Quick Commands

- **Run scanner**: `python limit_up_scanner.py` (default 60s interval) or `python limit_up_scanner.py 120` (custom seconds)
- **Run tests**: `python -m pytest tests/ -v`
- **Single test**: `python -m pytest tests/test_analysis.py::test_new_face -v`

## Project Structure

A-share stock momentum scanner monitoring Xueqiu's surge ranking API. Scores ChiNext (300xxx) stocks using "new face" vs "old face" strategies.

```
limit_up_scanner.py   # Entry point - main loop
scanner/
  orchestrator.py     # Core scan pipeline (~336 lines)
  analysis.py         # Scoring engines (new_face, momentum, pullback)
  config.py           # All thresholds, weights, dimension mappings
  api.py              # Xueqiu API calls (biaosheng, kline, market cap)
  database.py         # SQLite CRUD (appearances, kline, recommendations)
  models.py           # StockInfo, Candidate dataclasses
  enhancer.py         # Bonus scoring (sector, sentiment, RPS, indicators)
  candidate_pool.py   # ScanSession with list presence tracking
  rank_trend.py       # RankTracker with trajectory scoring
  sector.py           # Sector cluster detection
  trading_session.py  # Trading hours/holidays
  evolution/          # Self-evolution loop (tracker, analytics, optimizer)
tests/                # pytest test suite
```

## Key Facts

- **Database**: SQLite at `scanner.db` (auto-created). Tables: appearances, daily_kline, recommendations, sector_cache, parameter_snapshots
- **Python version**: 3.12+ (uses f-strings, dataclasses, type hints)
- **Dependencies**: `requests` only (sqlite3 is stdlib). No requirements.txt file.
- **Trading hours**: Auto-sleeps outside 09:30-11:30 / 13:00-15:00 on trading days
- **Encoding**: Windows-specific `sys.stdout.reconfigure(encoding="utf-8")` for Chinese output

## Architecture Notes

- Scanner filters: GEM stocks only (300xxx), excludes ST/*ST, HK stocks, market cap >300亿, price >100元
- Three strategies: new_face (bottom breakout), momentum (trend continuation), pullback (reversion)
- Self-evolution loop adjusts scoring weights based on IC (Information Coefficient) analysis
- Feishu push currently commented out in `limit_up_scanner.py:83`

## Testing

Tests use pytest with helper factories `_stock()` and `_kline()` in `tests/test_analysis.py` for creating mock data. No external services required.
