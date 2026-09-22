# AGENTS.md

## What this is

A-share (创业板) stock scanner that watches the Xueqiu biaosheng (飙升) leaderboard, scores candidates across 5 strategy buckets (rebound, known_new_face, momentum, new_face, short_term), and surfaces them for a "next-day big-rise" (次日大涨) read. SQLite-backed, dual data source (Xueqiu primary + THS fallback).

**输出形态（2026-09-21 定稿）**：终端与飞书**只有四个区块**，各自成表、互不排名、不给结论：
`v1 池选`（榜上主线五桶）/ `v1 回捞`（前 N 日 v1 产出今日回调到位）/ `沪深飙升 · 极有可能大涨` A 段（榜内飙升）/ 同节 B 段（榜外异动）。
其中**只有 `v1 池选` 有序**：**新票优先 → 类别优先级 → 榜单排名升序 → 资金流降序 → 形态加分**
（实现在 `scanner/view/assemble.py::build_scan_view`；终端标题 = 前四键：
`◆ v1 池选 — 新票优先·类别优先·排名升序·资金流降序`，形态加分是稀有 tie-breaker 不入题。
**改键必须同批改标题**。沿革：2026-09-16~21 的键在类别前还有第 2 键 tier（composite_tier
过热劣后 / composite 分档），2026-09-22 按用户决策删除 —— 与类别键高度重合（kNF→tier1、
MOM/NEW→tier2、ST→tier3）且 composite 部分无法向用户解释。**副作用**：accum≥50% 过热票
不再被排序劣后，只靠行内 ⚠ 标记提示）。「新票」= 本轮新进入今日推荐池，
判据是扫描循环持有的跨轮票集差集（`new_symbols` 入参），**不是** `recommendations.time` —— 后者在「分数提高」时
会被覆盖，`MIN(time)`/`first_time` 是「最后一次提分时刻」而非首次出现。无新票时第 1 键恒等，排序完全还原。
系统**不再产出任何「短名单」或「综合判断」** —— 历史上的决策层（≤3 只短名单 + 空仓判定，2026-09-14 删除）、终选参考区（「若必须持仓买谁」，2026-09-21 删除）、综合判断摘要（分区体检报告，2026-09-21 删除）都已移除。理由是它们各自构成第二个结论源，而系统唯一有证据的口径只有类别先验（见「目标函数」）。需要复原请查 git 历史。

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

### 样本外验证门（rule_validate）—— 改权重/常数**必须**先跑这个

```
python -m scanner.rule_validate                      # 基线自检：样本量/窗口/基线指标（不改任何东西）
python -m scanner.rule_validate --set scanner.nextday_prob.OR_OVERBOUGHT=5.0   # 看真实可检测下限（MDE）
python -m scanner.rule_validate --set scanner.nextday_prob.OR_OVERBOUGHT=1.56
python -m scanner.rule_validate --evaluator rescore --set scanner.config.MIN_SCORE=60
python -m scanner.rule_validate --list-evaluators     # 各评估器能"看见"哪些模块
```

**退出码：0 = 样本外支持 / 1 = 证据不足（默认拒绝）/ 2 = 样本外显著变差 / 3 = 用法或可见性错误。**

主指标 = test 窗**按日等权 top-N 次日 hit 率**（N = `rule_validate.DEFAULT_TOP_N` = 3，
即**名次带宽度**；2026-09-21 终选参考区删除后系统已无任何"短名单"，N 只是报告宽度，
取 3 是为了与历史报告可比），
显著性 = 按日配对 bootstrap 的 95% CI。**判定只看 test 窗**；train 窗 Δ 用于暴露过拟合。
报告里的 **MDE** 正面回答"以当前样本量，多小的改善才可能被检出"——若 MDE 远大于你观察到的 Δ，
那"指标变好"不构成上生产的理由。

> ⚠ **MDE 不可从「基线自检」获得**（2026-09-14 修正）：MDE 由「观测到的配对差值」的
> bootstrap 标准误得来，**改动无效果时必然退化为 0**。基线自检是空操作，MDE 恒打印
> **n/a**——这不代表灵敏度无穷大，恰恰相反，代表不可估。
> 报告同时给出**「改动实际翻转了 X/N 个交易日的 top-N 结果」**：这是可检测性的真正来源，
> **翻转天数为 0 时样本量再大也检不出任何东西**（此时判定必然是"证据不足"）。
> 要估计真实可检测下限，用能实际翻转 top-N 的扰动量级跑一次（实测 ≈ 2.3pp）。

**可见性硬校验（最重要的一道防线）**：`--set` 改的模块若不在所选评估器的可见集合内，
直接退出码 3 —— 防止"验证了一个根本没生效的改动"。可见集合：
`stored-score`=无（冻结分，仅对照）/ `nextday-prob`=`scanner.nextday_prob`（默认，快）/
`rescore`=config/weights/analysis/enhancer/validator/ranking/categories（覆盖评分链，约 29s）。

> 陷阱：项目用**快照式导入**（`from scanner.config import X` 把值绑进导入方命名空间），
> 只改 `scanner.config.X` 对消费方无效。工具会自动传播 override 并打印传播到的模块数，
> 跑完自动回滚。若报告显示"传播改写 0 个模块"且改动未产生输出差异，先怀疑改错了模块。

### 主通路黄金样本（拆 scan_with_raw 前必跑）

```
python scripts/golden_scan.py                    # 跑最新交易日并与基线对比（离线·确定性）
python scripts/golden_scan.py --date 2026-09-09  # 指定日期（各日期桶覆盖不同）
python scripts/golden_scan.py --write            # （重新）生成基线
python scripts/golden_scan.py --diff             # 打印首个不一致字段的上下文
```

`orchestrator.scan_with_raw`（507 行 / CC 95）是**主数据通路**，但 `tests/test_orchestrator.py`
对它**本身零覆盖**（只测辅助函数）。要把它拆成 `scanner/pipeline/` 纯函数，必须能证明
「拆完输出逐字段一致」——本工具就是那把尺子：从 `scanner.db` 重建某天的真实榜单输入，
用**离线桩 adapter + 断网**跑一遍，把 `ScanResult` 规范化成 JSON 快照后逐字段对比。

- **退出码：0 一致 / 1 不一致（等价变换被破坏）/ 2 运行失败。**
- 确定性靠三根钉子：钉死时间（覆盖**所有已加载 `scanner.*` 模块**的 `now_beijing`——
  只改 `scanner.config.now_beijing` 对快照式导入的消费方无效）、断网（装 requests 总闸，
  `EXTERNAL_FAILURES` 含 `RequestException` 所以 fail-open 分支照常跑）、复制 DB 到临时文件。
- ⚠ **覆盖有缺口**：工具会打印空桶告警。`comeback` 在离线模式下恒为 0（依赖 adapter 实时
  行情）；`new_face` / `momentum` 多数日期 0~1。**空桶对应的路径本基线保护不到**，
  拆它们时必须另补针对性单测。

### Offline label / data-quality tooling

```
python -m scanner.triple_barrier --report   # 三重屏障标签重建 + 新旧标签一致性（M2）
python -m scanner.model_bucket              # v3 模型桶离线可行性（M3，walkforward LightGBM）
python -m scanner.nextday_calib             # 概率模型校准漂移巡检（常数 vs 数据，漂移即退出码 1）
```

### 沪深飙升独立区（hot_watch）—— 自检入口

主循环内每轮自动执行，**通常无需手动跑**。此入口用于改规则后快速自检：

```
python -m scanner.hot_watch --offline-demo       # 离线自检：12 条内置样本逐条核对，不联网
python -m scanner.hot_watch --top 5              # 联网跑一轮（连击落内存库，不污染 scanner.db）
python -m scanner.hot_watch --offline-demo --json
python -m scanner.hot_watch --max-percent 5      # 临时覆盖阈值，验证边界
```

`--offline-demo` 全绿退出码 0，任一条不符即 1 —— 改动阈值/排除条件后应跑它，
它是本区筛选规则的回归哨兵（对应单测 `test_offline_demo_all_cases_match_expectation`）。

- **triple_barrier_labels 表**（M2）：(止盈 +7% / 止损 -5% / 时间 3 日) 三屏障标注，
  样本口径与 load_attribution_rows 一致（excluded=0 + 同票同日取最后一轮）；
  幂等重建，旧 next_day 标签链路不动。
- **model_bucket**（M3 第一步）：用已有标签 + score_breakdown 宽表离线训练 LightGBM
  （原生 API，不依赖 sklearn；训练/验证窗带 embargo），输出 walkforward AUC/头部
  提升度基线。小样本可行性验证——结论只用于「是否继续 M3」判断，非权重替换依据。
- **kline_drift（M1.2，自动运行）**：unified_scanner 非交易分支每日对 daily_kline
  锚定窗口做价格 SHA256 指纹比对（雪球前复权价会被除权事件静默重算 → 回测/rescore
  跨期不可复现）；漂移即告警 + 写 logs/finalize.log（同一变更只告警一次）。
  首次自动锚定「截至昨日的 min(可用历史, 250) 根 × 当时 symbol 集合」。

This rebuilds scores via `scanner/historical_rescan.py --rescore` (faithful to the live orchestrator pipeline). Read-only changes to config.py thresholds do NOT retroactively affect `recommendations` — you must use `--rescore`.

> ⚠️ 排序/档位/🎯 画像校准于 `next_day`（次日≥7% hit），但回测默认 `--hold-days 3`。改权重/阈值前先确认优化哪个口径。

## 目标函数（2026-09-14 定稿，唯一口径）

**次日≥7% hit 率**是系统唯一的类别先验口径 —— 排序、档位、🎯 画像、类别准入**全部**用它。
（2026-09-21 起「唯一例外」不复存在：平均超额收益的最后一位消费者 `decision.market_gate`
择时门随终选参考区删除，`scanner/decision.py` 整个模块已不在仓库里。）

**唯一手抄源 = `config_scoring.CATEGORY_HIT_RATE`**（+ `_DEFAULT`）。下游一律派生，不得复制：

| 消费方 | 派生方式 |
|---|---|
| `nextday_prob.BASE_RATE_BY_CAT` | **别名**（`is` 同一对象，非 copy） |
| `config_scoring.COMPOSITE_CAT_BASE` | `(hit − 基准) / (最高 hit − 基准) × 10` |

（2026-09-14 删除的 `decision.DECISION_GATED_CATEGORIES` / `DECISION_CATEGORY_SPECS`
两行随决策层移除；重建决策层时需恢复派生关系与守护。）

守护：`tests/test_category_priors.py`（结构/派生关系；数值漂移另有 `test_nextday_calib.py`）。
改动这类**结构性**口径别只跑 `rule_validate` —— 它的三个评估器
（`stored-score` / `nextday-prob` / `rescore`）**都看不见 `COMPOSITE_CAT_BASE`**，
纯搬迁（取值未变）必然报「证据不足 / 翻转 0 天」。对**等价变换**要另证
（比较改造前后的常量取值 + 生产函数体是否逐字节相同），对**行为变更**要列出受影响的类别集合差异。

## Verification order

After code changes: `ruff check` → `mypy` → `pytest tests/` (unit) → optionally `--run-smoke`.

After **weight/threshold/constant** changes, additionally: `python -m scanner.rule_validate`
(样本外验证门，见上)。改 `nextday_prob` 常数还要重写校准快照：
`python -m scanner.nextday_calib --write`，否则 `tests/test_nextday_calib.py` 会 fail。

After touching **`scanner/pipeline/`** (or anything in `scan_with_raw`): run the golden
sample for all four dates — `python scripts/golden_scan.py --date 2026-09-08` … `09-11`.
All four must exit 0 (逐字段一致). See 主通路黄金样本 below.

> ⚠️ **黄金基线会随 `scanner.db` 生长而腐烂**（2026-09-14 实测）：输入是从**当前 DB** 重建的，
> 脚本自己会打印 `输入指纹与基线不同（DB 或重建逻辑变了）—— 此时对比结果不可信`。
> 该情形下 exit=1 **不代表等价性被破坏**；先确认自己没碰 `scripts/golden_scan.py` /
> `scanner/pipeline/` / `scanner/orchestrator.py`（`git diff --quiet HEAD -- <这些>`），
> 再决定是否 `--write` 重建基线。**别把「输入指纹变了」当成「重构改坏了输出」。**

After touching **`scanner/db/`**: `pytest tests/test_migrations.py tests/test_schema_migration.py`.

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
  nextday_calib.py          # 概率模型校准重算 + 漂移巡检单源（配 nextday_calib.json 快照）
  rule_validate.py          # 规则/常数改动的样本外验证门（B1；改权重前必跑）
  prevday_perf.py           # (top-level) Multi-day performance summary
  hot_watch.py              # 沪深飙升·极可能大涨独立区（2026-09-11 合入，与主线口径解耦）
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
  pipeline/                 # 主扫描通路的阶段纯函数（等价变换产物，改前必读包 docstring）
    pool.py                 #   市值过滤 / 现价行情组装
    features.py             #   RPS 基准 / 分时趋势挂载
    buckets.py              #   分类分桶与排序
    scoring.py              #   加分累加 / 风险硬过滤 / v2 池选重建
    tactics.py              #   盘中操作纪律标签
  db/
    migrations.py           # 版本化 schema 迁移清单 + schema_migrations 账本
scripts/                    # Analysis/verification scripts (not production)
tests/                      # pytest suite
```

## Key facts an agent would miss

- **Category registry** (`scanner/categories.py`) is the single source of truth for all strategy categories. When adding/modifying a category, edit ONLY `CATEGORY_REGISTRY` there — all other modules derive from it.
- **Config is the single source for all thresholds** (`scanner/config.py`). Do not hardcode magic numbers elsewhere. If you need a new threshold, add it to config.py and import it.
- **`scanner/config.py` re-exports** from `scanner/weights.py`, `scanner/holidays.py`, and `scanner/categories.py`. The public import path `from scanner.config import X` is used throughout the codebase — maintain backward compatibility.
- **DB file** is `scanner.db` at repo root (gitignored). Created automatically by `init_db()`.
- **⚠ 备份 WAL 库不能 `cp`**（2026-09-13 实测踩坑）：`scanner.db` 是 WAL 模式，
  数据可能全部还在 `scanner.db-wal`（当时 12.9 MB）里。只 `cp scanner.db` 拿到的是
  checkpoint 前的旧页——副本 `recommendations` 是 **0 行**而真库 5070 行，**等于没备份**。
  必须用 `src.backup(dst)`（`sqlite3` 的 Online Backup API，支持热备），或先
  `PRAGMA wal_checkpoint(TRUNCATE)` 再 cp。**备份后必须校验行数。**
  现行完整备份：`scanner.db.bak-20260913`（5070 行已校验）。
- **schema 迁移走版本化清单**（`scanner/db/migrations.py`，2026-09-13）：
  新增迁移在 `MIGRATIONS` **末尾追加** `Migration(id, desc, check, up)`，id 用
  `mXXX_简述` 且递增——**id 发布后不得改名/删除**（老库会当新迁移重跑）。
  `check()` 判断是否已满足（存量库靠它回填账本，`up()` 不会被重跑）；
  `up()` 失败 → 回滚 + **原样上抛** + **不写账本** → 下轮重试（绝不能静默跳过，
  这是 2026-09-11 `market_extra_cache` v4 事故的根因）。
  查迁移状态：`python -c "from scanner.db.migrations import migration_state; ..."`。
  不要再往 `init_db()` 里加 `PRAGMA table_info` 探测块。
- **Data source env vars**: `RTS_DATA_SOURCE` (auto/xueqiu/ths), `HITHINK_FINANCE_API_KEY` (for THS fallback), `RTS_FEISHU_WEBHOOK`.
- **Beijing timezone** (`BEIJING_TZ`, UTC+8) is used everywhere for time logic. Never use naive local time.
- **Fail-open design**: External data fetches degrade gracefully. Catch only `scanner.utils.EXTERNAL_FAILURES` (OSError / timeout / requests / sqlite3.Error / ValueError / KeyError). Never bare `except Exception`. Programming errors must bubble to main loop.
- **Smoke tests** are marked with `@pytest.mark.smoke` — they need real `scanner.db` + network. Default `pytest` skips them.
- **`--rescore` is required for P&L validation**: `recommendations` table stores frozen scores from old weights. Changing thresholds in config.py does NOT retroactively change past scores.
- **Windows encoding**: `unified_scanner.py` sets `sys.stdout.reconfigure(encoding="utf-8")` on win32. Console output uses Chinese text + emoji.
- **Feishu webhook** (`FEISHU_WEBHOOK` in config.py): check for leaked tokens in git history (`git log -p -S "open.feishu.cn" -- scanner/config.py`). **Bot needs rotation.**
- **`pullback` category** is retired (live_produced=False) but kept in `CATEGORY_REGISTRY` for historical backtest/attribution. Do NOT remove it.
- **`hot_watch` 独立区（`scanner/hot_watch.py`，2026-09-11 合入）与主线完全解耦，改动前务必确认口径**：
  样本面是**沪深主板+创业板**（主线 `filter_gem_stocks` 只做创业板 300/301）；口径是
  「当日 momentum + 榜单热度跃升」（主线是 `next_day` 次日≥7% hit）。结果**不写
  `recommendations`**、不参与复合评分/档位/🎯 画像、不进飞书主卡片，连击只落
  `hot_watch_hits`。开关 `RTS_HOT_WATCH=0` 可整体关闭。
- **雪球两个行情接口字段不同，勿互换**（2026-09-11 实测）：`batch/quote.json`（批量，
  2 请求/100 票）**没有** `volume_ratio`/`limit_up`/`limit_down`（恒 None），但有
  `last_close`；`quote.json?extend=detail`（单票）才有这三个字段。故 hot_watch 用
  batch 做排除与打分，涨跌停价由 `last_close` 推算（`limit_prices`），仅对最终前 N 名
  补拉 detail 拿量比。
- **`.env`** file contains secrets (THS API key, Feishu webhook override). Never commit it.

## Testing notes

- `test_data_source.py` / `test_market_extra.py` depend on optional `pandas`/akshare — may fail on different environments.
- Integration tests (`--run-smoke`, ~16 cases) require `scanner.db` with real data + network access.
- **Avoid vacuous assertions**: If a test depends on a mock callback being called, explicitly assert the call happened.
