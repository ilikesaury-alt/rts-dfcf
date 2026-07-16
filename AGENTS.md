# AGENTS.md

## Quick Commands

- **Run scanner**: `python unified_scanner.py` (default 60s interval) or `python unified_scanner.py 120` (custom seconds)
- **Run tests**: `python -m pytest tests/ -v`
- **Single test**: `python -m pytest tests/test_analysis.py::test_new_face -v`

## Project Structure

A-share stock momentum scanner merging Xueqiu + Tonghuashun surge ranking APIs. Scores ChiNext (300xxx) stocks using "new face" / "momentum" / "pullback" / "short_term" strategies.

```
unified_scanner.py        # Single entry point (dual-source fusion)
scanner/
  orchestrator.py         # Core scan pipeline
  analysis.py             # Scoring engines (new_face, momentum, pullback, short_term)
  validator.py            # Cross-validation (3-dim check per strategy, 1-dim for short_term)
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
- Four strategies: new_face (bottom breakout), momentum (trend continuation), pullback (reversion), short_term (next-day sell)
- Cross-validation (`validator.py`): each candidate must pass ≥2 of 3 independent dimensions before final acceptance (short_term uses ≥1 of 4 dimensions)
  - **new_face**: indicator convergence (RSI<30 + MACD golden cross + KDJ K<20 & K>D), higher-low structure, sector resonance
  - **momentum**: MA5>10>20 alignment (penalty -5 if broken), no RSI divergence, volume uniformity (5-day window)
  - **pullback**: MA20 trending up (>+0.5%), volume shrinkage (<0.6x), sector still active (≥3 same-sector in list)
  - **short_term**: vol_ratio ≥ 1.0 hard gate. Pass rule: 弱转强 (weak-to-strong) passes outright; otherwise requires ≥2 positive dims with ≥1 non-sector (rank/MA/weak) — sector cluster alone cannot pass (prevents sector-wide surge days flooding the list)
- Priority chain: primary strategy attempted first; if cross-validation fails, falls through to next strategy

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

## Code Review 流程

当用户说 "全面审查项目代码" 时：

### 第一步：读 REVIEW_STATUS.md + CODE_REVIEW_CHECKLIST.md
- REVIEW_STATUS.md 告诉上次覆盖了什么、还缺什么
- CODE_REVIEW_CHECKLIST.md 是结构化检查清单

### 第二步：确定本次焦点
- 优先选 ✅ 覆盖状态为 ❌ 的模块
- 从 CODE_REVIEW_CHECKLIST.md 选 2-3 个维度
- 如果所有模块都 ❌ 过，聚焦 OPTIMIZE.md 中 P0/P1 未修复项

### 第三步：逐项检查
按 CODE_REVIEW_CHECKLIST.md 逐项过，发现的问题按优先级标记：
- 🔴 **P0** — 逻辑 bug（评分错误、数据错乱、空指针）→ 必须当次修
- 🟡 **P1** — 质量改进（重复代码、异常处理不足）→ 记录到 OPTIMIZE.md
- 🟢 **P2** — 工程优化（测试覆盖、配置迁移）→ 记录到 OPTIMIZE.md

### 第四步：更新追踪文件
- 🔴 问题当场修复（建 plan → 修 → 测试）
- 🟡/🟢 问题追加到 OPTIMIZE.md
- 更新 REVIEW_STATUS.md 覆盖状态和日志
