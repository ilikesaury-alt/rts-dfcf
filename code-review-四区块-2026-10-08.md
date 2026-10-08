# 核心业务流程代码审查报告 —— 四个候选区块

- **审查日期**：2026-10-08
- **审查范围**：v1 池选（`scanner/view/assemble.py`）、v1 回捞（`scanner/historical_watch.py`）、飙升A段（`scanner/hot_watch.py`）、B段/榜外异动（`scanner/offboard_watch.py`，含榜内异动段 `scanner/onboard_anomaly.py`），以及集成层（`unified_scanner.py` 主循环）与推送门（`scanner/push_gate.py`）。
- **总体评价**：四个区块的异常纪律整体良好（`EXTERNAL_FAILURES` 收窄、fail-open/fail-closed 语义显式、留痕表齐全）。本轮发现的问题高度集中在**一族同根风险：数据新鲜度**——`market_extra_cache` 快照与 K 线缓存的多层时滞，叠加"判定/排序/风险门/粗筛"分别取自不同时点的数据。以下按严重程度分级列出。

---

## 高（影响核心产出正确性）

### H1. B段粗筛涨幅带/量比排序键使用陈旧快照值，未按自身"宽阈值"原则放宽 → 系统性漏票

- **位置**：`offboard_watch.run_offboard_watch` 的 prefilter 段（约 L931–945）
- **触发条件**：`market_extra_cache` 快照滞后（`_apply_live_quote` docstring 自认实测可滞后 2h11m）时：
  1. 快照 `percent` 在带外（`≤HOT_MIN_PERCENT` 或 `>OFFBOARD_T2_TODAY_MAX`）而实时价已进 T1/T2 带 → 该票**永远拿不到实时价补全**，直接漏掉；
  2. 快照 `vol_ratio` 陈旧偏低 → 排序掉出 `OFFBOARD_LIVE_QUOTE_LIMIT` 头部 → 同样漏掉。
- **影响**：B段的核心目标是"价刚启动"的票，而"价刚启动"恰恰最可能发生在快照冻结之后——目标票被系统性漏掉，恰好复刻了本次 2026-10-08 修复要解决的同类问题。prefilter 注释声称"各项一律取比真门更宽的阈值"，但成交额/流通市值取了宽比（`OFFBOARD_PREFILTER_*_RATIO`），**涨幅带却用精确边界**，与自述原则自相矛盾。
- **修复方向**：涨幅带粗筛加缓冲（如 ±3pp）；或对快照写入时间超过 N 分钟的行无条件纳入实时补全；prefilter 排序键避免单一依赖陈旧 `vol_ratio`（可并入 amount 加权）。

---

## 中

### M1. B段风险门（资金流出 / ST 判定）运行在陈旧数据上 【已修 2026-10-08（晚）：换源实时覆盖】

- **位置**：`offboard_watch.offboard_gate` ← `build_candidates`；`_LIVE_FIELD_MAP` **刻意不覆盖** `main_pct`，`name` 只对补到实时行情的行刷新
- **触发条件**：快照冻结期间主力净占比恶化（实时已 ≤ -8% 出货）或股票更名为 ST/`*ST`。
- **影响**：风险门 fail 方向是**放行本应否决的票**（与 fail-open"宁可放过"不同，这是数据时滞造成的静默失效）；且与 A 段的实时资金流门口径不一致。
- **~~修复方向~~ → 实际修复**：用户决策「换其他源获取」。发现项目已有东财全市场资金流源 `market_extra.fetch_fund_flow_rank`（与主循环同源同进程缓存 TTL 300s）——B 段在 DB 快照路径上调用它，把全快照的 `main_pct` 逐行覆盖为实时值（`_apply_live_fund_flow`，fail-open）；`name` 已在 H1 修复中由雪球批量行情刷新。仅生产主循环路径触发（snapshot 注入的测试/离线/CLI 路径不联网）；稳态下缓存命中零额外请求，冷缓存一次有界全市场拉取（30s 上限）并顺带给主循环预热。配套 4 个单测（覆盖/fail-open/注入路径不联网/DB 路径生效）。量比仍为显式记录的残留口径差。

### M2. B段顶背离/美感判定使用当日盘中缓存 bar（当日一次性抓取、全天不更新）

- **位置**：`offboard_watch.classify_tier` L504–507（`closes` 含今日 bar → `mo_divergence`）；`annotate` → `beauty_marks_daily(klines)`；数据源 `load_offboard_klines` 当日缓存（末根=抓取时刻的盘中价）
- **触发条件**：首次抓取在早盘，其后价格大幅变化；或跨轮价格漂移。
- **影响**：MA 判据与 5 日累计已显式剔除今日 bar（2026-09-21 修复），**唯独顶背离与美感未对齐同一口径**——顶背离判定全天基于一张过期的"今日收盘"快照，可能误杀/误放，且判定随抓取时刻随机化。`_exclude_today` docstring 已自认"未改口径"，但它是当前活跃判定路径上的真实缺陷。
- **修复方向**：`mo_divergence` 改吃 `_exclude_today` 后的序列（需单独评估其对 bars 结构的要求）；美感标记同口径对齐。

### M3. A段 accum_5d 取数与美感开关耦合 → 关闭标记时过热否决整段失效

- **位置**：`hot_watch.build_candidates` L463–464（`klines_map` 仅在 `HOT_BEAUTY_GATE_ENABLED or TREND_MARK_ENABLED` 时取数）与 L515–518；下游 `push_gate.gate_hot_rows` 用 `accum_5d` 做过热否决
- **触发条件**：`RTS_TREND_MARK=0` 且 `RTS_HOT_BEAUTY_GATE=0` 的配置组合（或未来任一开关默认值变化）。
- **影响**：`klines_map` 为空 → A段全部行 `accum_5d=None` → 5日累计列显示 —，且 `push_gate._veto_common` 过热否决对 A 段**静默失效**（fail-open 放行过热票）。展示列失效是小事，风险否决静默失效是实质问题。
- **修复方向**：`accum_5d` 取数条件与美感开关解耦（独立开关或无条件取数）。

---

## 低

### L1. A段连击链路两处一致性缺口（`hot_watch`）

- **a) `_next_round` 与 `update_streaks` 非原子**（L547–604）：`_next_round` 先自增轮号（未 commit），`update_streaks` 若中途抛异常，`_safe_persist` 吞掉且**不回滚** → 轮号已进、连击未写 → 下一轮 `prev_round == round_no - 1` 不成立，**全部存量连击归 1**。影响 `push_gate` 兜底通道（streak ≥ 3）的证据连续性。
- **b) 榜单为空（熔断）轮直接 return**（L643–644）：不推进轮号也不清零连击，恢复后跨数据缺口的命中继续累加为"连续"。与"全员被排除"分支（显式 `_safe_persist(conn, [])` 打断连击链）语义不一致。
- **修复方向**：persist 异常路径显式 `rollback`；空榜轮同样推进轮次/清零连击（或明确 docstring 声明该语义为有意）。

### L2. push_gate 与 assemble 的资金流口径分叉

- **位置**：`push_gate.gate_main_rows` 只查 `flow_pct_map`；assemble 用 `is_fund_outflow`（行内 dims → flow_pct_map 回退链）
- **影响**：行内 dims 判出流出但 `flow_pct_map` 缺行时，终端剔了、推送门放行；`RTS_FUND_FLOW_HARD_FILTER=0` 时两端行为进一步漂移。
- **修复方向**：push_gate 复用 `ranking.is_fund_outflow` 单源。

### L3. MainRow.accum 口径混用做过热否决输入

- `short_term` 行的 `accumulated_pct` 含今日（策略语义），直接与 `OVERHEAT_ACCUM_MAX`（主线 5 日累计口径）比较，short_term 票的过热判定偏严一档。
- **修复方向**：否决输入统一走 `accum_map`（历史口径）。

### L4. `_board_is_chinext` 用子串匹配（`onboard_anomaly.py` L225–233）

- `"300" in code` 会把 `002300` 等主板票误入预筛（多花请求位）；与同文件 `startswith(("300","301"))` 的精确口径不一致。无漏票方向，仅浪费与风格问题。

### L5. B段 backfill 的性能与触发缺口（`offboard_watch.backfill_next_day`）

- 逐 symbol 调 `load_offboard_klines`（N+1 网络请求），停牌等"永远缺次日 bar"的行每轮重抓重试；
- `run_offboard_watch` 在 `not cands` 时 early-return **跳过 backfill** → 长期零产出的交易日标签停积，与"影子期样本必须有标签"的目标相悖。
- **修复方向**：backfill 移到 early-return 之前；按 symbol 批量读缓存后再补取缺失者。

### L6. assemble 层遗留与微瑕

- `_cb_core_pullback_ok`（L368–377）为死代码（定义后无任何调用）；
- `composite_score` 每行计算（含 conn 查询）但全仓无读取方（注释自认"只写不读"）→ 每轮纯开销；
- `e.get("live_rank") or e.get("rank")` 两处：`live_rank=0` 会静默回退（当前排名从 1 起，无实害，但与同文件对 0.00% 涨幅的防护风格不一致）。

### L7. historical_watch / onboard_anomaly 细项

- `evaluate` 中 `cum_pct` 在 `rec_date` bar 缺失（停牌）时静默为 0.0，展示 "+0.00%" 与真实持平不可区分（建议 None → 渲染 —）；
- `onboard_anomaly.persist_round` 捕获 `(sqlite3.Error, ValueError, KeyError)`，与全仓 `EXTERNAL_FAILURES` 纪律不一致——`KeyError` 可能是编程错误却被吞；
- `unified_scanner.py` L497 补拉推荐票行情用裸 `except Exception`，同循环内其余分支均已收窄，风格不齐（影响有限）。

### L8. 设计口径观察（非 bug，建议文档化）

- A段不排除今日已推荐票：v1 主表与 A段可同屏同票；B段/回捞/榜内段均与 `today_syms` 互斥，唯独 A 段无此约束。若属设计意图（同榜不同视角），建议在 `hot_watch` docstring 显式声明，避免后续被当 bug"修"掉。

---

## 汇总表

| # | 位置 | 问题 | 严重度 | 影响 |
|---|------|------|--------|------|
| H1 | offboard_watch prefilter | 粗筛涨幅带/量比用陈旧快照且未放宽 | 高 | B段系统性漏票（目标票恰是漏掉的那批） |
| M1 | offboard_gate + _LIVE_FIELD_MAP | 风险门（流出/ST）判定于陈旧数据 | 中 | 静默放行应否决票 |
| M2 | classify_tier / annotate | 顶背离/美感含当日过期盘中 bar | 中 | 误杀/误放，判定随时点漂移 |
| M3 | hot_watch.build_candidates | accum_5d 取数与美感开关耦合 | 中 | 配置组合下过热否决整段失效 |
| L1 | hot_watch 连击 | 轮号/连击非原子；空榜不打断连击 | 低 | 兜底证据失真、连击误清零 |
| L2 | push_gate.gate_main_rows | 资金流判定与 assemble 口径分叉 | 低 | 终端/推送同票不同结论 |
| L3 | MainRow.accum | short_term 含今日口径混入过热否决 | 低 | 过热判定偏严一档 |
| L4 | onboard._board_is_chinext | 子串匹配误纳主板票 | 低 | 浪费请求位 |
| L5 | offboard backfill | N+1 重抓；空产出日跳过回填 | 低 | 标签停积、性能 |
| L6 | assemble | 死代码 / 无效计算 / 0 值回退 | 低 | 每轮纯开销 |
| L7 | hist/onboard/主循环 | cum_pct 静默 0、异常口径不齐 | 低 | 展示歧义、吞编程错误 |
| L8 | hot_watch | A段不与 today_syms 互斥（疑为设计） | 低 | 建议文档化 |

**核心结论**：四区块最值得优先处理的是 H1（B段漏票）——它使 B 段在其最核心的观察场景（盘中价启动）下恰恰失明；M1/M2/M3 是同一"数据新鲜度"根因在不同判定点的投影，可一并设计统一的数据时点标注机制（快照行带 `as_of` 时间戳，门/排序/展示按各自敏感度消费）。

---

## 修复执行记录（2026-10-08 同日完成）

| # | 状态 | 说明 |
|---|------|------|
| H1 | ✅ 已修 | 新增 `OFFBOARD_PREFILTER_PCT_BUFFER=2.0`，粗筛涨幅带 = 真带 ± 缓冲（下限 clamp 0） |
| M1 | ✅ 已修（晚，换源方案） | `main_pct` 改由既有东财全市场资金流 `fetch_fund_flow_rank` 实时覆盖（仅生产 DB 快照路径触发，测试/离线不联网；fail-open）；`name` 已随 H1 由雪球批量行情刷新。量比仍为显式记录的残留 |
| M2 | ✅ 已修 | 顶背离（classify_tier）与美感标记（annotate）改吃 `_exclude_today` 后序列 |
| M3 | ✅ 已修 | hot_watch `klines_map` 取数与美感开关解耦（conn 非 None 即取） |
| L1 | ✅ 已修 | `_safe_persist` 失败 rollback；空榜/预筛全灭轮同样打断连击 |
| L2 | ✅ 已修 | `gate_main_rows` 资金流否决走 `is_fund_outflow` 单源（前置否决，不重复计数） |
| L3 | ✅ 已修 | `MainRow` 新增 `accum_hist`（历史口径），过热否决优先用它 |
| L4 | ✅ 已修 | `_board_is_chinext` 改 `code_of` + `startswith` |
| L5 | ✅ 已修 | backfill 前移到 `not cands` 早退之前（N+1 重抓未动，属性能优化可后续做） |
| L6 | ✅ 已修 | 删死代码 `_cb_core_pullback_ok` 及 CORE_PULLBACK_* 导入；rank 取值改显式 None 检查。composite_score 计算保留（panel 序列化可能经 asdict 消费该字段，删除需单独核实） |
| L7 | ✅ 已修 | onboard `persist_round` 异常收窄到 EXTERNAL_FAILURES。cum_pct 未改（全仓无渲染方读它，改动无收益） |
| L8 | ✅ 已修 | A 段不与 today_syms 互斥已在 run_hot_watch docstring 声明为有意设计 |

**验证**：ruff 全绿；hot_watch / offboard_watch / historical_watch 三区 offline-demo 退出码全 0；pytest 1849 passed / 6 failed —— 6 个失败已用 stash 在干净树复现确认为**存量问题**（test_nextday_rule×3、test_prelaunch_probe×1、test_rule_tracking×2，属缺 docs 基线文件类），与本次修复无关。改动面：9 文件 +130/−41（`scanner/config_hot_watch.py`、`hot_watch.py`、`offboard_watch.py`、`onboard_anomaly.py`、`push_gate.py`、`view/assemble.py`、`view/model.py`、`scripts/seq_refine_replay.py`、`tests/test_push_gate.py`）。
