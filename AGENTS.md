# AGENTS.md

## Quick Commands

- **Run scanner**: `python unified_scanner.py` (default 60s interval) or `python unified_scanner.py 120` (custom seconds)
- **Run industry chain scanner**: `python industry_chain_scanner.py`
- **Run tests**: `python -m pytest tests/ -v`
- **Single test**: `python -m pytest tests/test_analysis.py::test_new_face -v`

## Project Structure

A-share stock momentum scanner merging Xueqiu + Tonghuashun surge ranking APIs. Scores ChiNext (300xxx) stocks using "new face" / "momentum" / "pullback" strategies.

```
unified_scanner.py        # Single entry point (dual-source fusion)
industry_chain_scanner.py # Industry chain scanner entry point
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

- Scanner filters: GEM stocks only (300xxx), excludes ST/*ST, HK stocks, market cap >500亿, price >200元
- Three strategies: new_face (bottom breakout), momentum (trend continuation), pullback (reversion)
- Cross-validation (`validator.py`): each candidate must pass ≥2 of 3 independent dimensions before final acceptance
  - **new_face**: indicator convergence (RSI<30 + MACD golden cross + KDJ K<20 & K>D), higher-low structure, sector resonance
  - **momentum**: MA5>10>20 alignment (penalty -5 if broken), no RSI divergence, volume uniformity (5-day window)
  - **pullback**: MA20 trending up (>+0.5%), volume shrinkage (<0.6x), sector still active (≥3 same-sector in list)
- Priority chain: primary strategy attempted first; if cross-validation fails, falls through to next strategy
- Industry chain scanner (`industry_chain/`): independent subsystem implementing a chokepoint investment thesis — detects chain phases (潜伏→形成→成长→爆发→消退), verifies bottleneck node participation, picks technically strong bottleneck stocks from active chains

## Testing

Tests use pytest with helper factories `_stock()` and `_kline()` in `tests/test_analysis.py` for creating mock data. No external services required.

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
