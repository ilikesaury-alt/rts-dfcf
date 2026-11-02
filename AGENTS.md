# AGENTS.md

## What this is

A-share (创业板) stock scanner: watches Xueqiu biaosheng (飙升) leaderboard, scores candidates across 5 strategy buckets (rebound / known_new_face / momentum / new_face / short_term), surfaces them for a "next-day big-rise" read. SQLite-backed, dual data source (Xueqiu primary + THS fallback).

**输出形态**：各自成表、互不排名、不给结论。终端区块：`v1 池选`（榜上主线五桶）/
`沪深飙升 · 极有可能大涨` 三段（榜内飙升 / 榜内异动 / 榜外异动）。`v1 回捞` 终端隐藏、
飞书卡片保留。系统**不产出任何短名单或综合判断**——历史上决策层/终选参考区/综合摘要
均已删除（它们构成第二结论源；系统唯一有证据的口径只有类别先验，见「目标函数」）。

## Commands

```
python unified_scanner.py            # 60s refresh；120 = custom interval；--no-feishu
python stock_report.py 300319        # 个股深报（名字也行）
python today_report.py [--date D --top N]
python prevday_perf.py [--days 0]
python backfill_kline.py             # 收盘后手动回填 K 线

python -m pytest tests/ [--run-smoke]        # --run-smoke 需真库+网络
ruff check . / ruff format . / mypy scanner/

python -m scanner.portfolio_backtest --compare --rescore --buy-delay 0 --buy-at close --hold-days 3
python -m scanner.portfolio_backtest --compare-horizons "1,3" --buy-at close --hold-days-auto
# --buy-at open 被拒绝：信号收盘后才产生。--hold-days-auto：类别级持有期
# （cum_3d 语义类按 config.HOLD_DAYS_BY_CATEGORY）。

# 样本外验证门（改权重/常数必须先跑）
python -m scanner.rule_validate                 # 基线自检
python -m scanner.rule_validate --evaluator rescore --set scanner.config.MIN_SCORE=60
python -m scanner.rule_validate --list-evaluators
# 退出码：0 支持挂出 / 1 证据不足(默认拒绝) / 2 显著变差 / 3 用法或可见性错误
# 主指标 = test 窗按日等权 top-N 次日 hit 率；显著性 = 按日配对 bootstrap 95% CI。
# 判定只看 test 窗。MDE 来自观测差值的 bootstrap SE，基线自检恒为 n/a；
# 真正的可检测性来源是「翻转了 X/N 个交易日的 top-N」——翻转 0 天 = 检不出任何东西。
# ⚠ 可见性硬校验：--set 的模块不在评估器可见集合内 → 退出码 3（防验证了没生效的改动）。
#   可见集合：stored-score=无 / nextday-prob=scanner.nextday_prob(默认,快) /
#   rescore=config/weights/analysis/enhancer/validator/ranking/categories(约29s)。
# ⚠ 快照式导入陷阱：`from scanner.config import X` 把值绑进导入方命名空间，
#   只改 scanner.config.X 对消费方无效；报告显示"传播 0 个模块"先怀疑改错模块。

# Offline label / data-quality
python -m scanner.triple_barrier --report    # 三重屏障标签（+7%/-5%/3日），幂等
python -m scanner.model_bucket               # M3 walkforward LightGBM 可行性
python -m scanner.nextday_calib              # 校准漂移巡检；改 nextday_prob 常数后 --write

# B 段（榜外异动）次日表现追踪（只读库，刻意不给结论）
python scripts/offboard_nextday.py [--days N] [--tier T2] [--json]
python scripts/offboard_nextday.py --repair-next-day [--apply]   # 修历史错标签，默认 dry-run

# B 段 AI 分析（手动触发，不轮询，不挂主循环）
python -m scanner.ai_digest                  # research（默认）：模型自己调工具查真实数据后给判断
python -m scanner.ai_digest --mode analyze      # 按项目自己的历史分层命中率（948 条标签）算账
python -m scanner.ai_digest --mode transcribe    # 纯转述（过全部红线；exit 3 = 被拦）
python -m scanner.ai_digest --materials | --demo | --json
# 退出码 0/1未配置凭据/2上游失败/3触红线/4读库失败/5幻觉巡检发现编造数字
# 凭据 MAGPIE_* 放 .env。红线守卫与 worker 侧 explain/guard.ts 双端同源：
# 只改一侧即红（test_ts_slot_vocabulary_is_covered_by_python_guard）。

# 沪深飙升独立区自检（改规则后必跑，回归哨兵）
python -m scanner.hot_watch --offline-demo   # 12 条内置样本逐条核对，全绿 exit 0
```

## Verification order

代码改动：`ruff check` → `mypy` → `pytest tests/`（可选 `--run-smoke`）。
**权重/阈值/常数**改动：加跑 `python -m scanner.rule_validate`；改 `nextday_prob`
常数还要 `python -m scanner.nextday_calib --write`，否则 test_nextday_calib 红。
**`scanner/pipeline/`**（或 `scan_with_raw` 其余部分）：`pytest tests/test_pipeline.py`
（`scan_with_raw` 本身无自动守护，只能 `scripts/replay_source_swap.py` 离线回放人工比对；
黄金样本工具已删除——常红的守卫等于没有守卫，复原方式查 git 历史）。
**`scanner/db/`**：`pytest tests/test_migrations.py tests/test_schema_migration.py`。
⚠ 排序/档位/🎯 画像校准于 `next_day`（次日≥7% hit），回测默认 `--hold-days 3`；
改权重/阈值前先确认优化哪个口径。

## 目标函数（唯一口径）

**次日≥7% hit 率**是系统唯一类别先验口径——排序、档位、🎯 画像、类别准入全用它。
**唯一手抄源 = `config_scoring.CATEGORY_HIT_RATE`**（+ `_DEFAULT`），下游一律派生不得复制：
`nextday_prob.BASE_RATE_BY_CAT` = **别名**（`is` 同一对象）；
`config_scoring.COMPOSITE_CAT_BASE` = `(hit−基准)/(最高hit−基准)×10`。
守护：`tests/test_category_priors.py`（结构/派生）；数值漂移另有 `test_nextday_calib.py`。
⚠ 改这类**结构性**口径别只跑 `rule_validate`——三个评估器都看不见 `COMPOSITE_CAT_BASE`，
纯搬迁必报「证据不足/翻转 0 天」。等价变换要另证（常量取值 + 生产函数体逐字节相同）；
行为变更要列出受影响类别集合差异。
**退池类别**（`comeback` 0.028 / `pool_pick` 0.021，均低于默认 0.078）已从先验表删除、
注册表保留条目只翻 `live_produced=False`（`pullback` 同理，勿删）。守卫
`test_retired_categories_are_absent_from_prior_table`；表外类别在 push_gate 必判 C 档。

## 单源纪律（改前必读）

- **Category registry**（`scanner/categories.py::CATEGORY_REGISTRY`）：类别 label/color/
  priority/suggest 的唯一真相。改类别只改这里，其他模块派生。
- **Config 单源**（`scanner/config.py`）：所有阈值/权重的唯一来源，勿在别处硬编码。
  它 re-export 自 `weights.py` / `holidays.py` / `categories.py`，公共导入路径
  `from scanner.config import X` 全仓使用，保持兼容。
- **push_gate 否决阈值**复用上游单源，本模块不新造风险阈值。其门公式：
  通过 = ¬否决 ∧ 分级≥兜底档；分级 = CATEGORY_HIT_RATE（表外→C）；兜底(C) = 榜内热度
  （v1 rank≤40 / 飙升 streak≥3∨排名跃升≥30 / 回捞·榜外量比≥2）；
  否决 = 资金流出 ∨ 5日累计≥OVERHEAT_ACCUM_MAX ∨ 风险硬信号；节流 300s。
  纯展示层（不写库/不进回测），开关 `RTS_PUSH_GATE=0` 回滚；**终端不套用**（终端全量、
  飞书精选，故卡片强制打剔除数；终端「◆ 飞书过门」区 = 下一张卡会推什么，非已推）。
- **schema 迁移**走版本化清单（`scanner/db/migrations.py`）：`MIGRATIONS` **末尾追加**
  `Migration(id, desc, check, up)`，id `mXXX_简述` 递增，**发布后不得改名/删除**。
  `check()` 判已满足（存量库靠它回填账本）；`up()` 失败 → 回滚 + 原样上抛 + 不写账本
  → 下轮重试（绝不能静默跳过）。不要再往 `init_db()` 加 `PRAGMA table_info` 探测块。

## 硬规则 / 易踩坑

- **WAL 库备份不能 `cp`**：必须 `sqlite3` Online Backup API（`src.backup(dst)`）或先
  `PRAGMA wal_checkpoint(TRUNCATE)`；**备份后必须校验行数**。
- **Beijing timezone**（`BEIJING_TZ` UTC+8）用于一切时间逻辑，勿用 naive local time。
- **Fail-open**：外部取数优雅降级，只 catch `scanner.utils.EXTERNAL_FAILURES`
  （OSError/timeout/requests/sqlite3.Error/ValueError/KeyError），**禁裸 `except Exception`**；
  编程错误必须冒泡到主循环。
- **`--rescore` 是 P&L 验证必需**：`recommendations` 存冻结分，改 config 阈值不回溯改旧分。
- **DB** = 根目录 `scanner.db`（gitignored，WAL 模式，`init_db()` 自动建）。
- **`.env`** 含密钥（THS key、Feishu webhook 覆盖、MAGPIE_*），勿提交。
  Feishu webhook 曾泄露过，**bot 需轮换**（`git log -p -S "open.feishu.cn"` 查历史）。
- **Windows**：`unified_scanner.py` 已设 stdout utf-8；终端输出中文+emoji。
- **Smoke tests** 标 `@pytest.mark.smoke`（需真库+网络），默认 pytest 跳过。
  测试避免 vacuous assertions：mock 回调被调用要显式断言。
  `test_data_source.py`/`test_market_extra.py` 依赖可选 pandas/akshare，环境不同可能挂。

## 终端渲染 / 展示层不变量

- 区标题用 `◆`；**三段段标题一律 `—` 前缀、禁用 `◆`**（`panel_m0_check.terminal_regions`
  遇 `◆` 即重置区块会打散三段）。段名维度统一「来源域·形态」：榜内飙升/榜内异动/榜外异动
  ——**不用 A/B 段字母**（代码 `board_segment` 形参名保留）。
- `COLS_HOT` 的「评分」与「层」是**两列**（榜内飙升填复合分、榜外两段填 T1/T2，另一列显
  `—`）；守卫：`test_hot_cols_split_score_and_tier_into_separate_columns` +
  `test_hot_row_score_and_tier_split_matches_terminal`（守列含义跨端一致）。
  每个出现裸 `T1`/`T2` 的段标题必须带层级词表（`T1=量先动·价未动 / T2=启动首日`）；
  词表与排序链分两处写（`=` vs 带空格的排序键），守卫认带空格的 `T2 启动首日`。
- **只有 `v1 池选` 有序**：新票优先 → 类别优先级 → 榜单排名升序 → 资金流降序 → 形态加分
  （`scanner/view/assemble.py::build_scan_view`）。终端标题 = 前四键，**改键必须同批改标题**。
  「新票」= 本轮新进今日推荐池（跨轮票集差集 `new_symbols`），**不是** `recommendations.time`
  （那是最后一次提分时刻）。
- `hot_watch` 独立区与主线完全解耦：样本面沪深主板+创业板（主线只创业板 300/301）；口径
  「当日 momentum+榜单热度跃升」（主线是 next_day hit）。不写 recommendations、不进飞书
  主卡片、连击只落 `hot_watch_hits`；`RTS_HOT_WATCH=0` 整体关闭。
- kline_drift（自动）：每日对 daily_kline 锚定窗口做价格 SHA256 指纹比对（雪球前复权价
  会被除权事件静默重算 → 回测跨期不可复现），漂移即告警 + 写 logs/finalize.log。

## Key facts

- **雪球行情两接口字段不同，勿互换**：`batch/quote.json` 无 `volume_ratio`/`limit_up`/
  `limit_down`（有 `last_close`）；`quote.json?extend=detail`（单票）才有。涨跌停价由
  `last_close` 推算，仅前 N 名补拉 detail 拿量比。
- **雪球握手只能走 `GET /hq`**，判据是拿到 `xq_a_token`（`_has_auth_cookie`）而非 HTTP 200。
  WAF 反爬会回 HTTP 567 只写 `acw_tc`；`/S/` 路径任何 UA 都**永远不发 token**（换入口是死路）。
  多档 UA 轮试（`_XUEQIU_HANDSHAKE_PROFILES`）；567 已并入 `_is_session_expired`。
  排查先看 `session.cookies.get_dict()` 有无 `xq_a_token`——别往「cookie 过期」方向查。
- **东财 push2 家族有 IP 级限频**：连续手动调试可能「ulist 也不通」（直连+代理同灭
  >10 分钟），等待冷却即恢复，勿误判代码故障。资金流接口已从 clist 全市场分页迁到
  `ulist.np/get` 显式 secid 批量（500 码/批）；取数域 = 创业板全集枚举 ∪ 注册码。
  **首批 0 行** = 整体不可用（快速失败+60s 退避）；**其余批 0 行** = 该码段无上市票（正常）；
  落库一律写本次取到的全部行。资金流快照 `volume` 单位是**手**，`_absorb` 已 ×100 归一到股
  （本仓其余 volume 是股；A 段/榜内异动段走雪球股口径本就正确，勿顺手改）。
- **实时行情覆盖**：量纲字段（price/volume/amount/turnover/市值）挡 `<= 0`（0=停牌/脏值），
  **有符号字段**（`percent`）只挡 `None`——一律 `val>0` 会让翻绿票的陈旧正涨幅永久留下。
- **标签回填三防呆**（`offboard_watch.backfill_next_day` / `onboard_anomaly.backfill_next_day_pct`
  同款，缺一即错标签永久冻结）：① `nxt < today`（K 线池末根可能是当日盘中 bar；盘中值
  会被冻结成假标签，主链路覆盖式重算能自愈、这条链路不能）；② `d` 与 `nxt` 之间不得夹
  已登记交易日（`_next_bar_is_next_session`，上限 12 自然日——只差 ≤4 天的判据会让
  国庆/春节整批丢标签）；③ 信号日必须 `< today`。历史错标签用
  `scripts/offboard_nextday.py --repair-next-day` 修（零联网、默认 dry-run、验不了的原样跳过）。
- **配对 bootstrap 天数不足 3 天时故意不出区间**（`MIN_PAIRED_DAYS`）：n=1 的 CI 退化成
  宽度 0 的 `[d,d]`，看着极显著实则是样本量为一。
- **`offboard_launch_log` 语义**：`(date,symbol)` 主键、**信号值首写冻结**（「当初以什么
  身份被推出」的存档，不是展示值；`percent` 冻结语义不受实时覆盖修复影响，刻意不动）。
  终端「最后打印的那几行」真源是 m022 表 `offboard_print_snapshot`（launch_log 是整日累加、
  view_snapshot 被 RTS_LOCAL_PANEL 门控，都不能当终端真源）。
- **`scripts/offboard_nextday.py` 刻意不给结论**：B 段是 rule_validate 三个评估器的盲区，
  样本外裁决通道不存在；阈值归属 `config_hot_watch.py` docstring。统计脚本一旦给「建议」
  就成了第二结论源（守卫 `test_report_contains_no_verdict_wording`）。
- **ai_digest 素材口径**：值取自 launch_log 冻结快照（「信号时涨幅」= 被推出时的涨幅）；
  段内顺序用 `offboard_watch.sort_key`；`next_day_pct` 刻意不进素材（防绩效读数变成判断）。
  历史基准率（`source.load_baseline_stats`）是素材一部分，标签文本自带「本项目实测、
  非市场先验」与 n；`BASELINE_BUCKETS` 的 cuts 与 scale 必须同单位。analyze 模式是
  用户决策的「无第二结论源」纪律的**有意例外**（输出不落库/不进计算/不进飞书）。
- **research 模式（ai_digest 默认，2026-11-03）= 模型自己查真实市场数据再判断**：
  网关**支持 tool-calling**（实测 `finish_reason=tool_calls`）⇒ 工具箱
  `ai_digest/research.py`（8 个：K线/实时报价/资金流/概念/板块热力图/市场概览/飙升榜/
  系统历史）+ 循环 `ai_digest/agent.py::run_research`（≤8 轮/≤20 次调用）。
  幻觉巡检的比对基准随之升级为**全部工具输出全文**。工具箱共 **11 个**（8 + 3）：
  K线 / 实时报价 / 资金流快照 / 概念 / 板块热力图 / 市场概览 / 飙升榜 / 系统历史 /
  **公告**（东财 `np-anotice-stock`，公司层面真实事件）/ **分时**（雪球 minute.json，日内节奏：
  均价线、尾盘半小时量占比）/ **多日资金流趋势**（东财 fflow `klt=101`，121 个交易日的
  主力/超大单净额合计与净流入天数）/ **龙虎榜**（东财 `RPT_DAILYBILLBOARD_DETAILSNEW`，
  含 D1/D2/D5/D10 后续涨幅）。三个坑（都实测踩过）：
  ① **必须流式**（非流式被上游 CDN 以 524 掐断 100s，流式 5s）；② **SSE 必须显式 UTF-8
  解码**（requests 对无 charset 的 text/* 默认 ISO-8859-1 ⇒ 整篇 mojibake）；
  ③ **工具输出必须给人读单位**（元→万/亿、量→万股），否则模型自己换算的数字会被巡检
  判成编造。工具的列名/字段名**必须实测**（`daily_kline.percent` 不是 `pct`；
  `appearances` 无 category/score/excluded；东财是 `main_net`/`super_net`/`turnover`）——
  猜错会 OperationalError ⇒ 工具**静默失效**。
  ④ **东财 fflow `klt=101` 是日频不是分时**（121 个交易日；`klt=1` 本环境取不到 0 点）⇒
  工具名为 `get_fund_flow_trend`；东财公告的 `columns` 是**字符串列表**（不是 dict 列表）。
- **push_gate「削减 88%」口径已作废**（测量总体错了：recommendations 原始行含 pool_pick，
  生产卡片到不了）；真实削减量未知，需用真实卡片总体重测。
- **pool_pick 退池后三处不能跟着删**（存量 988 行仍在 DB）：`view/assemble.py` 的
  `main_recs` 排除条件（删了 pool_pick 会混进 v1 主表）；`_stg_map`/`CAT_LABEL` 标签映射
  （供存量行解析）；注册表条目。
- **榜外段「反复进前十」粉名/🔥 标识已整体删除**（2026-10-16 用户决策），守卫为反向
  （`test_offboard_subtitle_has_no_repeat_marker`）；`top_hits`/`gate_hits` 列保留作研究原料。

## Architecture

```
unified_scanner.py          # CLI entry point, main loop, DB init
scanner/
  orchestrator.py           # scan_with_raw(): candidate pool → classify → score → ScanResult
  data_source.py            # adapter pattern: XueqiuAdapter / ThsAdapter / FallbackAdapter
  api.py                    # Xueqiu HTTP (session, kline, biaosheng, market caps)
  ths_api.py                # THS official finance API (K-line fallback)
  database.py               # SQLite CRUD: recommendations, appearances, daily_kline
  config.py                 # ALL thresholds, weights re-exports, env flags (single source)
  config_push.py            # 飞书过滤门阈值（A/B 档边界、兜底、节流）
  config_hot_watch.py       # 飙升三段阈值（含改动依据 docstring）
  categories.py             # CATEGORY_REGISTRY: single truth for label/color/priority/suggest
  models.py                 # KlineBar, StockInfo, Candidate, ScanResult, RecommendationRow
  analysis.py               # K-line pattern scoring
  enhancer.py               # Live enrichment: fund flow, market cap, turnover, RPS, heat
  validator.py              # Post-score validation (overbought, risk flags)
  ranking.py                # Composite ranking + tier (档0-3) + nextday mark
  display.py                # Terminal rendering (ANSI colors)
  feishu.py                 # Feishu webhook push
  push_gate.py              # 飞书推送过滤门（纯展示层，终端不套用）
  hot_watch.py              # 沪深飙升独立区（与主线口径解耦）
  offboard_watch.py         # 榜外异动段（launch_log 写侧、实时覆盖、标签回填）
  onboard_anomaly.py        # 榜内异动段（标签回填同款三防呆）
  view/assemble.py          # build_scan_view：v1 池选排序键 + 终端行组装
  render.py                 # 终端/飞书共用渲染（COLS_HOT、三段段标题）
  historical_watch.py       # v1 回捞（hist_rows，终端隐藏、飞书保留）
  backtest.py               # Historical backtest + outcome backfill
  portfolio_backtest.py     # Portfolio backtest with --rescore
  historical_rescan.py      # Re-run live pipeline on historical data (--rescore)
  nextday_attribution.py    # Next-day return attribution
  nextday_calib.py          # 概率校准重算 + 漂移巡检单源（配 nextday_calib.json 快照）
  rule_validate.py          # 样本外验证门（改权重/常数前必跑）
  core_themes.py            # Core theme dip-buying
  comeback.py               # Comeback (回马枪) strategy
  concept.py                # Concept/theme board (East Money F10)
  sector.py                 # Sector clustering
  weights.py                # Scoring weight tables
  holidays.py               # Chinese trading calendar
  trading_session.py        # Trading hours detection
  indicators.py             # RSI, KDJ, MACD, BOLL...
  minute_bar.py             # Intraday minute bar merging
  intraday_fetch.py         # Parallel minute data fetching
  kline_fetch.py            # Parallel K-line fetching
  features.py               # Feature engineering
  walkforward.py            # Walk-forward optimization
  market_extra.py           # ZT pool + fund flow (East Money push2delay→ulist 批量)
  data_health.py            # Data quality checks
  net.py                    # HTTP utilities, East Money tokens
  utils.py                  # to_float, clear_screen, EXTERNAL_FAILURES...
  log_utils.py              # Result logging
  candidates.py             # Candidate pool management
  candidate_pool.py         # Pool selection logic
  rank_trend.py             # Rank trend tracking
  ranking_snapshot.py       # Ranking snapshot persistence
  patterns.py               # Candlestick patterns
  fundamentals.py           # Fundamentals filtering (pywencai)
  ai_digest/                # B 段 AI 分析（手动触发；material/guard/source）
  pipeline/                 # 主扫描通路阶段纯函数（改前必读包 docstring）
    pool.py / features.py / buckets.py / scoring.py / tactics.py
  db/
    migrations.py           # 版本化 schema 迁移清单 + schema_migrations 账本
scripts/                    # Analysis/verification scripts (not production)
tests/                      # pytest suite
```
