# 命令速查表（COMMANDS.md）

> 面向**人**的操作手册：每条命令干什么、怎么调、什么时候跑。
> 面向 AI 的约束（口径、纪律、不可破的规则）在 [`AGENTS.md`](AGENTS.md)；两者别混。
>
> 维护：加新命令时同步补一张表；命令行为变了先改这里再改代码。

---

## 0. 最常用的 5 条

```bash
python unified_scanner.py                 # 跑扫描器（60s 一轮，按 c 复制两区完整原文）
python scripts/buy_priority_report.py     # 盘中买入优先级反馈报告（独立脚本，~8s，见 §2.1）
python pick.py --clipboard                # 把剪贴板里的两区表格（自动抽码）丢给 AI 调研 → 出报告
python stock_report.py 300319             # 单只票深报（代码或名字都行）
python -m pytest tests/ -q                # 全量测试
```

---

## 1. 日常扫描（终端）

| 命令 | 说明 |
|---|---|
| `python unified_scanner.py` | 主循环，默认 60s 刷新；`120` = 自定义间隔 |
| `python unified_scanner.py --no-feishu` | 不推飞书（只看终端时用，省一次外呼） |
| `python unified_scanner.py --no-panel` | 不上报 Web 面板（优先级最高，覆盖 `.env`） |
| `python unified_scanner.py --no-lock` | 跳过单实例锁（**仅调试**，会和已有实例抢库） |

**终端里按 `c`**：把本轮 `v1 池选` + `沪深飙升` 三段两区的**完整原文**送进剪贴板
（区/段标题、口径说明、表头、每行全部列、区尾分隔线，只去 ANSI）—— 不精简、不去重，
粘出去和屏幕所见一致；头部摘要、`◆ 飞书过门` 不在复制范围。接着
`python pick.py --clipboard` 就能让 AI 调研这一批票（抽码只认表格行代码列，
成交量这类 6 位数字不会被误当成股票代码）。

终端三段结构（`v1 池选` 有序，其余三段各自成表、互不排名）：`榜内飙升 / 榜内异动 / 榜外异动`。

---

## 2. 看盘 / 报表

| 命令 | 说明 |
|---|---|
| `python stock_report.py 300319` | 个股深度报告（代码或名称） |
| `python stock_report.py 麦捷科技 --quick` | 快速模式：只用本地数据，不调 API |
| `python today_report.py` | 今日综合排序分析报告（默认最近有数据的交易日） |
| `python today_report.py --date 2026-11-03 --top 10` | 指定日期 + 只看前 10 只 |
| `python today_report.py --json` | 机器可读 |
| `python prevday_perf.py` | 历史复盘：各组次日表现汇总 |
| `python prevday_perf.py --days 0` | 全期（0 = 不限天数） |
| `python prevday_perf.py --force` | 跳过数据健康检查（交叉验证不符也强行出） |
| `python query_today.py` / `query_summary.py` | 查今日推荐 / 推荐汇总（默认昨日） |

---

### 2.1 盘中买入优先级反馈报告（**独立脚本**，随跑随出）

把「当日候选 → 第一/第二/第三梯队 + 回避 + 明日动作」打出来。**只读库、不写库、
不改任何生产逻辑、不进主循环/终端/飞书**（`scanner/` 零改动）。

```bash
python scripts/buy_priority_report.py                    # 默认档：~8-11s（本地+公告12只+龙虎榜4只）
python scripts/buy_priority_report.py --no-ann           # 纯本地零网络：~0.5s（只看资金/位置/板块）
python scripts/buy_priority_report.py --out 今日.md       # 同时落盘（utf-8-sig，Excel 可直接开）
python scripts/buy_priority_report.py --symbols 300638,300568   # 指定清单（默认当日推荐池）
python scripts/buy_priority_report.py --top 10           # 只打前 10 只
python scripts/buy_priority_report.py --date 2026-11-04  # 复盘某个交易日（需当日有标签）
```

**耗时档位**（实测 2026-11-05，46 只候选）：

| 开关 | 耗时 | 加什么 |
|---|---|---|
| `--no-ann` | **0.5s** | 纯本地库：资金/位置/个股历史/板块排位/市场环境 |
| 默认 | **8~11s** | + 公告 12 只（东财）+ 龙虎榜 4 只（东财） |
| `--dtb 0` | −2.6s | 关掉龙虎榜 |
| `--ann-top 0` | −6s | 关掉公告（等价 `--no-ann`） |
| `--minute N` | +18s | 分时均价（雪球 minute.json，只对第一/二梯队前 N 只） |
| `--ai N` | **≈60s/只** | AI 深度复核（`scanner.ai_digest` research 工具箱，模型自己查真实数据） |

**报告里每只票给什么**：现价/涨幅/类别·先验/评分 → 资金（主力净占比·净额·超大单·5日累计与净流入天数）
→ 位置（MA5 偏离·历史5日·量比·换手）→ 个股历史兑现率 + 弹性（市值/价格）→ **板块排位**
（它在主板块当日第几，跟随还是掉队）→ **大涨之后模式**（该票历史上大涨之后的次日表现）→
公告事件（已验证负面 / 未验证）→ 龙虎榜（最近一次上榜后的 D1/D5）→ **✅成立条件 / ❌失效信号**
（带具体价位）。末尾是**明日动作** + **口径与已知缺口**。

**分档口径**（写死在脚本里，可改可查）：
- 综合分 = 先验25 + 当日资金20 + 中期资金20 + 位置20 + 个股历史10 + 弹性10 − 惩罚项；
- **封顶①** 先验 C 档（< 7.8% 基线）⇒ 最高第三梯队；
- **封顶②** 命中**已验证负面**公告（减持/对外担保/股权激励）⇒ 最高第二梯队；
- **硬否决 → 回避**：主力 ≤ −8% / 当日涨幅 ≥ 8% / 历史5日累计 ≥ 50% / 超买硬信号；
- 公告证据分层与实测数字见脚本 docstring 与 `scripts/ann_event_backtest.py`。

⚠ 三个已知缺口（报告尾部也会打）：① 板块排位**未做样本外检验** ⇒ 只提示、不进分数；
② 资金累计用本地每日快照（与东财日频收盘值有差）；③ 公告只对排序靠前 N 只拉 ⇒ 未标 ≠ 没有。

---

## 3. AI 分析

### 3.0 面板原文调研（**默认入口，2026-11-11**）

```bash
python -m scanner.ai_digest                       # 读 logs/panel_md/股票池.md（终端最新一轮原文）
python -m scanner.ai_digest --brief               # 一屏报告：每票一行 + 整体判断
python -m scanner.ai_digest --limit 3             # 每区只取前 3 只（区是并列段，按区截断）
python -m scanner.ai_digest --materials           # 只打模型会收到的提示词，不出网
python -m scanner.ai_digest --out 报告.txt --max-calls 40
```

**输入 = 终端面板原文**（`◆ v1 池选` + `◆ 沪深飙升` 三段），不是扁平代码清单：模型拿到的是
各区自己那一套列（池选的 `类别·先验` / 飙升段的 `层`·`量比`·`换手`）**和各自的口径原文**，
并被明确要求「各区口径互不相同，不要跨区排名」。来源缺失时自动回退 print → view → log。

⚠ `analyze` / `transcribe` 不吃面板 —— 它们按设计就是**榜外异动段（B 段）转述**，
`--source panel` 只对 `--mode research`（默认）生效。

### 3.1 自选清单批量调研（指定代码）

```bash
python pick.py                            # 交互粘贴（空行结束；粘贴整段文本也行）
python pick.py 300319 300666 SZ300770     # 直接给代码（30/00/60/68 开头，一次 ≤50 只）
python pick.py --clipboard                # 读 Windows 剪贴板（终端按 c 复制后用这个；完整表格也行）
python pick.py --symbols-file list.txt    # 从文本文件读（一行一个代码或名字）
python pick.py --clipboard --limit 10     # 面板票多时只取前 10 只（按面板顺序 = 优先级）
python pick.py --clipboard --out 报告.txt --max-calls 50   # 存盘 + 调工具预算
```

等价长命令：`python -m scanner.ai_digest --symbols ...`（`pick.py` 只是简写）。

**一条完整链路**：扫描器终端按 `c` → 两区完整原文进剪贴板 → `python pick.py --clipboard` → 报告。
面板三段加起来常超 20 只，**上限 50**；超了用 `--limit N` 取前 N 只。

模型会**自己查真实数据**（K线 / 实时报价 / 资金流 / 分时 / 多日资金流趋势 / 概念板块 /
板块热力图 / 市场概览 / 飙升榜 / 公告 / 龙虎榜 / 系统出现史），最后给**排序 + 入选依据 +
风险 + 证据强弱 + 整体判断**。
- 幻觉巡检：正文里每个数字必须在工具输出里逐字出现，编造就当场标出（退出码 5）。
- 退出码：0 成功 / 1 未配凭据 / 2 上游失败 / 3 参数或代码有问题 / 5 幻觉巡检拦截。
- 凭据：`MAGPIE_BASE_URL` / `MAGPIE_API_KEY` / `MAGPIE_MODEL` 放 `.env`。
- ⚠ **网关连不上先 ping 域名**：`trycloudflare` 临时隧道进程一停域名就停止解析
  （表现为 `ConnectionError`，与凭据/代码无关）。本地网关 `http://127.0.0.1:3425/v1`
  是同一个 magpie 网关，但**依赖 pi 在跑**。

### 3.2 外部 AI 分析库（**已评估，不采用**）

曾接入 `simonlin1212/TradingAgents-astock`（上游 TradingAgents 119k★ 的 A 股特化 fork：
7 个 A 股分析师 + 多空辩论 + 三方风控），实测能出 17 段 / 2.5 万字的中文决策报告。
**2026-11-03 用户决策：不采用，已删除。**

理由：**效率是前提**。外部库单只票 2~5 分钟（30~50 次 LLM 调用）、批量半小时以上，
而次日异动是分钟级生意 —— 分析再好，出得晚就没用了。它的价值投资视角
（财报/估值/护城河）与本项目的目标函数（次日 ≥7% hit 率）也不必然同向。

⇒ 本项目只保留**自建 research 模式**（`python pick.py`，单只约 1~2 分钟）。
新增维度一律先过延迟闸；慢手段只能进离线旁路，不进主循环/终端/飞书。


### 3.3 榜外异动（B 段）AI 分析

```bash
python -m scanner.ai_digest                      # 默认 research：模型自己查数据后给判断
python -m scanner.ai_digest --mode analyze       # 按项目历史分层命中率算账（主观判断，不落库）
python -m scanner.ai_digest --mode transcribe    # 纯转述（过全部红线；触红线 exit 3）
python -m scanner.ai_digest --materials          # 只打素材，不出网
python -m scanner.ai_digest --demo               # 内置样例（零库零网，验证整条链路）
python -m scanner.ai_digest --json               # 机器可读（含幻觉巡检结果）
python -m scanner.ai_digest --brief              # 简洁报告：每票一行、≤12 行，不打印查询记录/素材核对表
python -m scanner.ai_digest --brief --out 简报.md  # 落盘同一份简洁报告（不进库）
python -m scanner.ai_digest --source print --materials  # 核对输入=终端最后打印行（带 seq 与数据年龄）
```

数据源 `--source auto|print|view|log`（默认 auto / print = `offboard_print_snapshot` 终端最后
一次打印的行，含留存行、顺序即屏幕顺序；输出带 `print_seq` 与「数据 X 分钟前 / 非实时」）。
`view`=桌面版 view_snapshot，`log`=launch_log 整日过门账。`--limit N` 控制分析前 N 只。
`--brief` 只压篇幅与命令侧附加内容：system 提示词、红线守卫、基准率排序、幻觉巡检全部照旧。

### 3.4 报告归档（每次跑完自动留一份，人工复盘用）

```bash
python -m scanner.ai_digest                       # 自动归档 + 终端打印（无需加参数）
python -m scanner.ai_digest --no-archive          # 本次不留档
python -m scanner.ai_digest --archive-dir D:/复盘  # 改归档根目录
ls logs/ai_digest/2026-11-13/                   # 按发起日分目录，同秒跑两次加 _2 后缀
```

默认路径 `logs/ai_digest/YYYY-MM-DD/HHMMSS_<mode>_<source>.md`（在 gitignore 的 `/logs` 下）。
`--out FILE` 是**额外**再存一份到指定路径；`--json` 也归档（`[归档]` 走 stderr，不污染 stdout）。
`--demo` / `--materials` / 无凭据 / 上游失败不产生档案（那些没有模型报告）。

⚠ **只归档，不回灌**：档案是「模型当时说了什么」的主观判断，不是事实。它不进库、
不进飞书、不进主循环，**也不进任何提示词**；文件里刻意不含次日绩效（防前视偏差）。
要验证 AI 判断有没有用，自己按 `(date, symbol)` 去库里 join 标签，别把结果补写回档案。
真正持续累计的信息在 DB：冻结信号快照 + 次日标签 → 分层命中率。

## 4. 数据回填 / 修复 / 诊断

| 命令 | 说明 |
|---|---|
| `python backfill_kline.py` | 收盘后回填推荐历史票的缺失 K 线，重算 `next_day_pct` |
| `python backfill_kline.py --dry-run --days 120` | 只统计缺失，不调 API 不写库 |
| `python backfill_market_index.py` | 回填 `market_index_log` 历史（默认 200 天） |
| `python backfill_market_index.py --dry-run --overwrite-live` | 预演 / 覆盖被冻结的基准值 |
| `python repair_kline.py --since 2026-10-01` | 修 `daily_kline` 里的非法 bar（默认 dry-run） |
| `python diag_health.py` | 数据新鲜度体检（各表最新日期/行数 + 扫描质量日志） |
| `python diag_capture.py` | 榜单捕获率诊断（当日收盘 ≥7% 的票有没有被抓住） |
| `python leaderboard_obs.py --days 7` | 飙升榜/热搜榜成分与排名分布时间序列 |
| `python query_today.py --date 2026-11-03` | 查指定日推荐 |

> **K 线漂移**：系统每日对 `daily_kline` 做价格指纹比对，除权导致的静默重算会告警并写
> `logs/finalize.log` —— 回测跨期不可复现时先看这里。

---

## 5. 验证 / 回测

```bash
python -m scanner.backtest --days 60 --metric next_day_pct     # 次日涨幅分布
python -m scanner.backtest --ranking --backfill               # 按综合排序回放 + 回填标签
python -m scanner.nextday_attribution --days 30 --excess      # 次日归因
```

**口径提醒**：唯一口径是**次日 ≥7% hit 率**；排序/档位/类别准入全用它。
⚠ 旧样本外验证门（`rule_validate`）、概率校准（`nextday_calib`）、P&L 回测
（`portfolio_backtest`）、三重屏障/模型桶已整体归档 `legacy/`（见 `legacy/README.md`）：
它们的常数**没有任何生产读取方**。当前终端主链路没有自动化样本外门。

---

## 6. 独立区自检（回归哨兵）

```bash
python -m scanner.hot_watch --offline-demo       # 沪深飙升区 12 条内置样本逐条核对，全绿 exit 0
python -m scanner.historical_watch --offline-demo # v1 回捞区同理
python -m scanner.offboard_watch --offline-demo  # 榜外异动段同理
```

---

## 7. 离线研究脚本（`scripts/`，只读、不改库、不进生产链路）

### 7.1 B 段（榜外异动）专用

```bash
python scripts/offboard_nextday.py               # B 段次日表现追踪（刻意不给结论）
python scripts/offboard_nextday.py --days 20 --tier T2 --json
python scripts/offboard_nextday.py --repair-next-day          # 修历史错标签（默认 dry-run）
python scripts/offboard_nextday.py --repair-next-day --apply   # 真写
python scripts/shadow_feature_backtest.py --no-fetch           # B 段新数据源的回溯样本外检验
python scripts/ann_cache_sync.py --migrate-legacy             # 旧 12MB JSON 历史公告一次性导入（幂等）
python scripts/ann_cache_sync.py                              # 当日最后一轮榜外行批量刷新公告（多码 1 请求）
# B 段 AI 分析走 python -m scanner.ai_digest / pick.py（见 §3），不要另写报告脚本
```

### 7.1b 已归档：池内排序 / LLM 排序回放

`legacy/scripts/rerank_backtest.py`（类别先验有效、分数无效 → v1 池选改显「类别·先验」列）
与 `legacy/scripts/rerank_llm_backtest.py`（模型排序无显著增量 → 下线 AI 排序）已归档，
复现方式见 `legacy/README.md`；两条结论仍写在 `AGENTS.md`「Key facts」。

### 7.2 其它研究/审计脚本

| 脚本 | 用途 |
|---|---|
| `scripts/profit_ledger.py` | 收益账本：按某规则买入实际拿到多少（扣真实成本） |
| `scripts/ann_event_backtest.py` | 公告事件的历史样本外检验（v1 主线 / 榜外段可切换 · 关键词表 2026-11-11 扩充 · 配对 bootstrap + 逐日符号检验 + jackknife） |
| `scripts/ann_cache_sync.py` | 公告缓存同步（m025）：候选票批量多码 1 请求 / 全市场按日回填 / 旧 JSON 迁移；取数失败**不推进水位** |
| `scripts/buy_priority_report.py` | **盘中买入优先级反馈报告**（独立脚本 · 只读库 · ~8s）：综合分档 + 成立/失效条件 + 明日动作（详见 §2.1） |
| `scripts/push_gate_impact.py` | 飞书过滤门的离线影响评估（三个问题，全部用真实历史数据） |
| `scripts/prelaunch_probe.py` | 启动前形态的样本外可提取性探针（只读，不联网） |
| `scripts/beauty_mark_eval.py` | 「稳」走势标记分档的样本外裁决 |
| `scripts/flow_mark_evidence.py` | 资金流图标收窄的实证复算 |
| `scripts/label_audit.py` | 标签登记簿审计（出处 / 证据等级 / 反误读） |
| `scripts/nextday_rule_scan.py` | 次日大涨高概率规则历史验证（H1/H2 分半） |
| `scripts/nextday_mark_split_verify.py` | 🎯 分型前后命中率对比 |
| `scripts/ranking_validate_day.py` | 单日综合排序 → 次日表现验证 |
| `scripts/review_tier_replay.py` | 忠实回放生产 `entry_tier`，验证档位区分度 |
| `scripts/tier_penalty_replay.py` | 档位「级联否决」vs「扣分制」对照回放 |
| `scripts/tier3_reason_perf.py` | 档3 警示劣后 → 按劣后原因拆开的次日归因 |
| `scripts/seq_refine_replay.py` | 排序序列细化规则回放 |
| `scripts/signal_combo_analysis.py` | 组合信号命中率分析 |
| `scripts/pool_filter_scan.py` | 池内部过滤器摸底（哪些被剔除的组次日确实更差） |
| `scripts/replay_source_swap.py` | 数据源替换实验：把昨天 v1 产出当榜单输入回放 |
| `scripts/label_registry.py` 相关 | 见 `python -m scanner.label_registry`（控制台需 UTF-8：`PYTHONIOENCODING=utf-8`） |
| `scripts/project_audit.py` | 项目量化普查（规模/复杂度热点/依赖扇入扇出） |
| `scripts/ths_api_probe.py` | 同花顺 API 接入前探测 |
| `scripts/panel_m0_check.py` | M0 门自动执行器（终端渲染 vs 云端读回逐条比对） |
| `scripts/_verify_view_split.py` | 视图层拆分等价性证明 |
| `scripts/_config_diff.py` | 对比 git HEAD 版 `config.py` 与当前导出的 API 差异（一次性小工具；需 UTF-8 控制台） |
| `scripts/_run_tests_all.py` | 一次性跑全量 pytest（内存捕获，不落临时文件） |

---

## 8. 环境变量开关（`.env`）

| 变量 | 作用 |
|---|---|
| `RTS_PUSH_GATE=0` | 关掉飞书过滤门（回滚用） |
| `RTS_HOT_WATCH=0` | 整体关掉沪深飙升独立区 |
| `RTS_LOCAL_PANEL` / `RTS_LOCAL_PANEL_PORT` | 本地 Web 面板开关 / 端口 |
| `RTS_PANEL_URL` / `RTS_PANEL_SECRET` | 面板上报地址 / 密钥 |
| `RTS_PANEL_MD=0` | 关掉面板逐轮存档（默认每轮**整份覆盖** `logs/panel_md/股票池.md`，只留最近一次） |
| `RTS_PANEL_MD_PATH` | 存档 md 完整路径（整份覆盖，指向外部文件会替换其原有内容） |
| `RTS_ANN_PREFETCH=0` | 关掉候选票公告预取（默认开：候选产出后后台线程把终端展示那批票的公告同步进 `announcement_cache`；不阻塞主循环） |
| `RTS_FEISHU_WEBHOOK` | 飞书 webhook 覆盖 |
| `RTS_DB_PATH` | 指定 DB 路径（默认根目录 `scanner.db`） |
| `RTS_DATA_SOURCE` | 数据源（雪球 / 同花顺 fallback） |
| `RTS_ENABLE_POOL` / `RTS_ENABLE_FUND_FLOW` / `RTS_ENABLE_ZT_POOL` / `RTS_ENABLE_INTRADAY_TACTICS` / `RTS_ENABLE_FUND_RISK` | 各取数/策略模块开关 |
| `RTS_HIST_WATCH` / `RTS_HOT_OFFBOARD` / `RTS_ONBOARD_ANOMALY` / `RTS_OFFBOARD_BOARD_FETCH` / `RTS_HOT_BEAUTY_GATE` / `RTS_HOT_FUND_FLOW_FILTER` | 榜外段/榜内异动/回捞等分项开关 |
| `RTS_CLEAR` / `RTS_TREND_MARK` / `RTS_FINAL_PICK` / `RTS_DISPLAY_MAX_TODAY_PCT` | 终端展示相关 |
| `RTS_ENV_FILE` | 指定 `.env` 路径 |
| `MAGPIE_BASE_URL` / `MAGPIE_API_KEY` / `MAGPIE_MODEL` | AI 网关凭据（`.env`，勿提交） |

---

## 9. 改代码后的验证顺序

```bash
ruff check .            # 风格/静态
ruff format .           # 格式化
mypy scanner/           # 类型
python -m pytest tests/ -q
```

按改动范围再加：

| 改动 | 追加验证 |
|---|---|
| 类别先验 / 阈值 | `pytest tests/test_category_priors.py` + 列出受影响类别集合 |
| `scanner/pipeline/` | `pytest tests/test_pipeline.py` |
| `scanner/db/` | `pytest tests/test_migrations.py tests/test_schema_migration.py` |
| 飙升区 / 回捞区 / 榜外段规则 | 对应 `--offline-demo` 全绿 |
| `ai_digest` / `pick.py` | `pytest tests/test_ai_digest.py tests/test_ai_digest_symbols.py` |

---

## 10. 常见坑（详见 AGENTS.md 对应条目）

- **WAL 库不能 `cp` 备份**：用 `sqlite3` Online Backup API，或先 `PRAGMA wal_checkpoint(TRUNCATE)`，备份后校验行数。
- **飞书 webhook 曾泄露**，bot 已需轮换；`.env` 勿提交。
- **东财 push2 有 IP 级限频**：调试频繁会「ulist 也不通」，等冷却恢复，别误判代码故障。
- **雪球两接口字段不同**：`batch/quote.json` 没有量比/涨跌停价，`quote.json?extend=detail` 才有。
- **雪球握手只认 `xq_a_token`**（不是 HTTP 200）；排查看 `session.cookies.get_dict()`。
- **时间一律用 Beijing 时区**（`BEIJING_TZ`），别用 naive local time。
- **B 段脚本刻意不给结论**（统计脚本一旦给「建议」就成了第二结论源）。
- **`recommendations` 存的是冻结分**：改阈值不会回溯改旧分，历史回放须显式重算。
