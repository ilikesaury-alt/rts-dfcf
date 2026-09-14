# 策略核心流程合理性审查（静态·不依赖数据与回测）

日期：2026-09-14
范围：v1 五桶（new_face / known_new_face / momentum / rebound / short_term）+ v2 池管道（pool → danger → matcher）
+ comeback / core_dip / hot_watch + 排序档位层（composite_tier / entry_tier）+ 概率终选（nextday_prob / final_pick / decision）
方法：**纯静态阅读**。不查 scanner.db、不跑回测、不读归因数据 —— 只判断「流程本身的逻辑是否自洽」。
基线：HEAD = `e09ff97`；`pytest tests/` 1478 passed / 13 skipped（流程问题都在无测试覆盖的路径上）。

> 说明：本项目的注释密度极高，且大量分支是「回测结论驱动」的刻意设计。本报告**只挑逻辑上不自洽、
> 或与项目自己写下的结论相矛盾**的地方；已被注释明确声明为「已知观察缺口/先观测不调参」的，不计入问题。

---

## 结论摘要

| # | 严重度 | 问题 | 关键位置 |
|---|---|---|---|
| 1 | **P0** | 三个「买什么」的出口用了**两个互相矛盾的目标函数**（次日≥7% hit 率 vs 平均超额收益），导致同一类别在系统内既是最好又是最差 | `nextday_prob.py:70-81` vs `decision.py:14-15` vs `config_scoring.py:271-281` |
| 2 | **P0** | `core_dip` 的「低吸质量分」与落库去重用的「最佳时刻」判据**符号完全相反**，实际保留了最差的低吸时刻 | `core_themes.py:248-259` vs `:393`、`:345` |
| 3 | **P1** | 分类是「优先级抢占」而非独立多标签 —— 每类 base rate 是被抢占后的**条件分布**，任何按类调参都在优化一个会随级联顺序漂移的量 | `candidates.py:115-153` |
| 4 | **P1** | 8% 追涨门砍掉了 `short_term` **权重最高**的 8~12% 档：该档权重校准过，但永远不出现在任何输出里 | `config_sources.py:163` + `config_scoring.py` vs `view/assemble.py:367,436`、`final_pick.py:234`、`decision.py:128` |
| 5 | **P1** | `known_new_face` 的「分数反指」在展示/终选两个排序键上方向丢失（仍按高分优先） | `view/assemble.py:168-169`、`final_pick.py:270` vs `candidates.py:156-163`、`ranking.py:827` |
| 6 | **P1** | `momentum` 的顶背离「门」形同虚设：注释声称强制其它维度补偿，实际 MA+量能本来就必须同时为正 | `validator.py:397-401` |
| 7 | **P1** | `_fund_flow_norm` 给「强流入」赋**最大正权** +0.5，同函数注释承认强流入是反指且已下线 | `ranking.py:719-738` |
| 8 | **P1** | 「主力出货」在系统里有 3 套不同定义/阈值（-5% / -8% / 5 种 K 线形态），共用同一个名字 | `enhancer.py:250-307`、`danger.py:40,102`、`enhancer.py:218` |
| 9 | **P1** | v2 池管道与 matcher **不做盘中量能投影**，早盘系统性看到「缩量」；v1 侧已修 | `orchestrator.py:117-119`、`matcher.py:329-348` vs `analysis.py:98-134` |
| 10 | P2 | 近常数加分（无暴跌 +8 / 近2日不差 +5）几乎人人命中，使跨类 score 不可比、门限失去参照 | `analysis.py:221-239` |
| 11 | P2 | 累计涨幅存在「求和」与「复利」两套口径混用（同一模块内） | `analysis.py:954` vs `:961`、`comeback.py:301-311` |
| 12 | P2 | `hot_watch` 量能满分 25 恒不触发（打分先于量比取值）；资金流过滤在 `conn=None` 时恒放行 | `hot_watch.py:403-404,548-567`、`:396` |
| 13 | P2 | 两套档位并存：展示走 `composite_tier`，报告/存证/归因走 `entry_tier`（含 🎯 口径差异） | `view/assemble.py:379` vs `today_report.py:252`、`ranking_snapshot.py:50` |
| 14 | P2 | `composite_score` 阈值（6/4/2）与分量值域（除 cat_base 外上限 2.65）不匹配 → 部分类别数学上不可能进档 0/1 | `config_scoring.py:283-288`、`ranking.py:780` |

---

## 一、P0：目标函数混用（这是所有下游矛盾的根）

系统同时用两个不同的目标量做决策，而它们的类别排序**方向相反**：

| 类别 | 次日≥7% hit 率（`nextday_prob.BASE_RATE_BY_CAT`） | 平均超额（`decision.py` 类别先验） |
|---|---|---|
| rebound | **0.179（最高）** | +0.74% |
| known_new_face | 0.127 | +1.03% |
| momentum | 0.100（第 3 高） | **−0.70%（永禁）** |
| new_face | 0.097 | — |
| core_dip | **0.065（倒数第 3）** | **+1.69%（最高）** |
| short_term | 0.062 | — |
| comeback | 0.028（最低） | — |

- `momentum` 在 `nextday_prob` 里 base rate 排第 3（高于 new_face / short_term），却被 `final_pick.py:269`
  用一句 `category != "momentum"` **无条件剔除**；理由是「唯一负超额类别」，即用的是**均值**口径。
- `core_dip` 在 `decision.py:44` 是**第一优先级**（+1.69% 超额最高），但在 hit 率口径里倒数第 3，
  `COMPOSITE_CAT_BASE` 只给 +0.7、`BASE_RATE_BY_CAT` 只给 0.065 → 在综合排序里排倒数。

**为什么这是真问题**：hit 率高 ≠ 期望收益高（大涨票往往同时大涨大跌）。两套口径混在一条流水线里时，
「过滤」用均值、「排序」用 hit 率，会产生互相抵消的效果：系统会把 hit 率高但均值为负的票排在前面，
再用一句硬编码把它整体删掉；同时把它自己算出的「最优类别」排到列表尾部。

**建议**：先选定**唯一**决策目标并写进 `AGENTS.md`，然后：
- 若目标是「次日大涨且能落袋」→ 用 `E[r] = P(hit)·E[r|hit] − P(miss)·E[r|miss]` 作为排序键，
  把「hit 率」与「幅度」显式合成一个量，而不是两个量各管一半；
- 若坚持 hit 率为主 → 删除 `momentum 永禁` 这类用均值支撑的硬编码，改为让 `nextday_prob` 如实打分；
- 无论选哪个，`COMPOSITE_CAT_BASE`、`BASE_RATE_BY_CAT`、`DECISION_CATEGORY_SPECS` 三张类别先验表
  **必须由同一个脚本从同一口径同一天生成**（现在三张表分别手抄，且数值来源已不同）。

---

## 二、P0：`core_dip` 的低吸质量分方向反了

`core_themes.py:248-254` 的注释声明 `_dip_score` 与 `low_buy_quality`「单调一致」，
但两者符号**相反**：

- `_dip_score`（`:306`）：**分越高越好**（`-30` 是惩罚项）
- `low_buy_quality`（`:434`）：**越小越优**（同样写 `-30`）

后果：`save_core_dips`（`:345`）取 `max` 去重，实际保留的是**最差的低吸时刻**，与注释「最佳低吸时刻」相反。
这不是调参问题，是可读性掩盖下的真反向 bug。

**建议**：二选一并统一（推荐保留「质量分越高越好」，把 `low_buy_quality` 改名为 `low_buy_penalty`
且全部取负号，或直接删掉重复概念）。改完补一条单测：同一天同一票两个不同时刻的低吸特征，
断言落库保留的是「质量分更高」的那个时刻 —— 这类**方向性**断言必须用手工构造的对照样本，
不能只测「有落库」。

---

## 三、P1：分类是抢占式，不是多标签

`classify_category`（`candidates.py:115-153`）按固定优先级抢占：老股 **rebound > 弱转强 short_term >
momentum > short_term > known_new_face**。注释写明目的是「避免掏空动量桶」—— 即这是**桶位管理**，
不是策略语义。

**为什么这构成问题**：
1. 一只票的类别标签不表达「它符合哪个策略」，而表达「它在哪个桶还有位置」。`short_term` 桶实质收下
   的是「动量不合格的票 + 弱转强票」，所以短线的 hit 率 6.2% 一部分来自这个残留效应，而不是策略本身无效。
2. 所有按类别的调参（`SHORT_TERM_WEIGHTS`、`BASE_RATE_BY_CAT`、`COMPOSITE_CAT_BASE`）都是在优化一个
   **条件分布**。一旦级联顺序变化（历史上改过一次：弱转强优先于动量），所有类别先验全部需要重估，
   但代码里没有任何地方记录这种耦合。
3. 系统已经需要多标签了 —— `score_stock`（`:329-333`）为「首板票同时满足超短」开了一个特例双挂口子。
   特例的存在说明模型选错了。

**建议**：改成**多标签**：每只票独立跑 4 个策略，各自通过门禁就各自成候选（可同 symbol 多行），
再做两件事：
- 桶内展示保留「按 composite_score / 类别优先级」排序（`final_pick._CAT_PRIORITY`、`dedup_candidates`
  已经是现成的归一机制）；
- 每个桶的样本量会显著上升（现在被抢占吃掉的票回到各自桶），此时类别先验才有统计意义。

改动面不小，且会改变 `recommendations` 的落库行数与所有历史对比口径 —— **需要先确认是否愿意接受
一次断点**；若暂不做，至少应在 `classify_category` 上加一段「类别先验与该级联强耦合，改顺序必须重估
三张先验表」的显式警告。

---

## 四、P1：8% 追涨门与 short_term 的最优档自相矛盾

- `SHORT_TERM_MAX_TODAY_PCT = 12.0`（`config_categories`）→ 涨 8~12% 的票**是** short_term 候选；
- `SHORT_TERM_WEIGHTS["today_pct_8_12"] = 15` —— 该表**最高**权重，注释写明由数据反向修正而来
  （21 条 cum_3d +3.84%）；
- 但 `DISPLAY_MAX_TODAY_PCT = 8.0` 在**所有**输出路径上砍掉 >8%：
  `view/assemble.py:367`（v1 主表）、`:436`（v2 池选区）、`final_pick.py:234`（终选）、
  `decision.py:128`（决策层 SQL `percent <= ?`）。

结论：**该档权重校准于一个永远不回吐给用户的区间**。回测能看见（落库在展示前），用户看不见。
这会让「按回测调权重」与「用户实际看到什么」系统性错位 —— 权重优化在一个不可观测的子集上。

**建议**（二选一，别维持现状）：
- **A（推荐）**：把追涨门做成**按类别**的：`short_term` 豁免到 12%（理由：该策略语义就是「今日放量启动」，
  买点本就在强势区），并在 `AGENTS.md` 说明「追涨门的目的是过滤买不到的高开票，而对 short_term 这类
  以当日强度为信号的策略，8% 以上仍是有效买点」；
- **B**：承认 8~12% 不可输出，把该档权重归零并从回测口径中剔除，避免它继续污染权重校准的参照系。

顺带：追涨门按**实时涨幅**判定，同一只票在 09:35（+3%）出现在表里、10:30（+9%）消失 —— 对已经买入的人
是「消失的持仓」。建议在门被触发时保留一行灰底提示（「已涨 +9%，移出追涨门」）而不是静默删除。

---

## 五、P1：`known_new_face` 的反指方向在两个排序键上丢失

`SCORE_DESCENDING_BY_CAT` 是单源正确实现（`ranking.py:827`、`candidates.py:156-163`），
但两处仍硬编码「高分优先」：

- `final_pick.py:270`：`key=lambda v: (-v["_p"], -v["verdict"], -to_float(v.get("score")))` —— 末键对所有类别降序；
- `view/assemble.py:168-169`：`yt.sort(key=lambda x: -score)`、`other.sort(...)`，
  kNF 落在 `other` 分支（`:164`）→ 同样反向。

影响有限（只在**概率/评级打平时**才轮到末键），但这是「单源原则已被违反」的信号 —— 下次新增反指类别
必然踩同一坑。**建议**：末键一律走 `ranking.score_sort_key(entry)`，并在
`scripts/_verify_view_split.py`（或单测）里加一条：扫描全仓，禁止出现 `key=lambda ... -.*score` 形式的排序键。

---

## 六、P1：`momentum` 的顶背离门是空门

`validator.py:397-401` 的注释：

> 因此出现顶背离时，候选必须通过「MA 多头 + 量能均匀」两个其它正维度（pos_dims>=2）才放行，
> 背离本身不会单独否决候选，但会强制其它维度补偿

实际：`mo_divergence` 只返回 `0` 或 `-10`（`validator.py:276-335`），**永不为正**。
`pos_dims = sum(1 for b in (ma, div, vol) if b > 0)` 因此恒等于「MA>0 的个数 + 量能>0 的个数」，
而 `passed = pos_dims >= 2` ⇒ **等价于 MA 多头 AND 量能不爆**。
换句话说：有没有顶背离，`passed` 的结果完全一样 —— 所谓「强制补偿」是恒真的。

`-10` 也没进排序（`candidates.py:109-111`：`validation_bonus` 不入 score），
所以背离惩罚现在**只**通过两条路生效：`量价背离` 展示标签、`主力出货` 模式 4（`enhancer.py:293`）。

**建议**：把意图写进代码 —— 若要「背离时必须 MA 且量能双正」（现状），就把注释改成
「背离不改变门禁，仅作展示」；若真要兑现「补偿」，需要 3 个正维度才放行（`pos_dims >= 3 and div >= 0`）。
两者差异很大（后者会砍掉可观数量的动量候选），**先想清楚要哪个**，不要留着这段读起来像有门、实际没门的注释。

---

## 七、P1：「主力出货 / 资金流出」一名三义

| 位置 | 判据 | 阈值 | 后果 |
|---|---|---|---|
| `enhancer._detect_main_force_distribution`（`:250-307`） | 5 种 K 线/量价形态（高位滞涨 / 高换手+超买 / 冲高回落 / 爆量顶背离 / 后排+分时弱） | 各不同 | 硬过滤「主力出货」（v1） |
| `danger.DANGER_MAIN_OUTFLOW`（`:40,102`） | 主力净占比 | ≤ **−5%** | 硬排除（仅 v2 池） |
| `enhancer.set_risk_flags`（`:218-221`） | 主力净占比 | ≤ **−8%** | 标签 +（`FUND_FLOW_HARD_FILTER_ENABLED` 开时）硬过滤 |

三者的**名字**都会被读成「主力在出货」，但一个是形态学、两个是资金流且阈值不同（−5 / −8）。
同一只票可能被 v2 以 −5% 排除，却在 v1 因为没到 −8% 而保留，标签还都叫「资金流出/主力出货」。

**建议**：拆成两个正交维度，各自单源：
- `流出（资金流）`：阈值单源（建议统一到 `FUND_OUTFLOW_NET_PCT`，把 `DANGER_MAIN_OUTFLOW_PCT` 删掉或指向它）；
- `派发形态（K线）`：保留现在的 5 种形态，但改名（如 `高位派发形态`），避免与资金流混读。
`nextday_prob.OR_OUTFLOW` 引用的阈值也必须与之一致（现在引用 `FUND_OUTFLOW_NET_PCT`，✓）。

---

## 八、P1：v2 / matcher 缺盘中量能投影

`analysis._compute_volume_metrics`（`:116-134`）与 `_project_today_vol`（`:98-113`）做了
`today_vol × 240/elapsed`（上限 10×）的投影，注释写明「避免 vol_ratio 天然 <1.0」。
但：

- `orchestrator.v2_kline_summary:117-119`：`volume_ratio = today_bar.volume / avg_volume` —— **无投影**；
- `matcher.py:329-348`：同样用裸今日量判「缩量回调 (<0.8) / 放量突破 (>1.5)」。

后果：早盘（比如 09:40，已交易 ~10 分钟）v2 侧所有票量比都只有盘中的 ~1/10 量级 →
「缩量回调」标签近乎恒真、「放量突破」近乎恒假。这些标签还经 `_dip_label_bonus`
（`ranking.py:741-758`，最高 +0.15）进入排序键 —— 即**排序在早盘被一个偏置的标签驱动**。

**建议**：把投影抽成公共函数（`analysis._project_today_vol` 已有），v2 与 matcher 一律复用；
`v2_kline_summary` 也应当接受 `now`（回测时传收盘后时刻关闭投影，与 v1 同口径）。

---

## 九、P2 组（知道就好，不急）

1. **近常数加分**：`_crash_safety_block`（`analysis.py:221-239`）在 5 日内无 ≤−12% 的日子时给 +8，
   近 2 日求和 > −3% 再给 +5。−12% 在创业板极少出现 ⇒ 绝大多数 momentum/short_term 候选都白拿 +13。
   它不改变**类内**排序，但让「跨类 score 比较」（`final_pick` 末键、`MOMENTUM_MIN_SCORE=50` 这类门限）
   失去参照。建议：改成对「有暴跌日」组做惩罚（非对称），或把常数并入门限值。
2. **求和 vs 复利**：`analyze_rebound` 的入池门用 5 日 percent **求和**（`:954`，≤−10），
   而 `accumulated` 用复利（`:961`）；`_crash_safety_block` 的 `pcts[-2]+pcts[-1]` 同样是求和；
   `comeback` 预筛用复利 ≤−8（`:301-311`）。求和比复利更负（a=b=−5% 时 −10% vs −9.75%），
   所以 comeback 预筛会放行一批注定被 rebound 门拒掉的票 —— 白补 K 线。
   建议：提供 `cum_return(pcts)` 单一实现，所有门槛统一走它；至少让 `comeback` 预筛与
   `analyze_rebound` 用同一函数。
3. **`hot_watch` 量能 25 分虚设**：`hot_watch.py:403-404` 先算 `score` 与 reasons，`:548-567` 才补
   `volume_ratio` 且不重算 ⇒ `HOT_VR_*` 权重与量比档位分支恒不触发，25 分退化为换手×0.85。
   另外 `:396` 只判开关不判 `conn`（对比 `:387` 的美感门判了），`conn=None` 时资金流 map 为空
   → `.get()` 恒 `None` → **静默放行**（正是本项目历史上「传空 dict 导致条件恒不触发」那一类 bug）。
   `--offline-demo` 走的正是这条路径 ⇒ 自检覆盖不到。
4. **两套档位并存**：展示走 `composite_tier`（`view/assemble.py:379,444`），
   报告/存证/归因走 `entry_tier`（`today_report.py:252`、`ranking_snapshot.py:50`、`walkforward.py:234`）。
   二者对 🎯 的处理不同（`entry_tier` 用它置档 0，`composite_tier` 只当展示标记，`marked` 形参收下不使用）。
   `ranking_snapshot` 自称记录「当日展示最终序号」，但算的是影子口径 ⇒ 权威存证与用户所见不一致。
   `ranking.score_sort_key`（`:821`）已无生产调用方，是死代码。
5. **composite 阈值与值域不匹配**：`composite_score` 非 cat_base 部分上限为
   `tech 1.0 + rank 1.0 + fund 0.5 + dip 0.15 = 2.65`（`ranking.py:772-781`），
   而 `COMPOSITE_TIER_THRESHOLDS = {0: 6.0, 1: 4.0, 2: 2.0}`。代入 `COMPOSITE_CAT_BASE` 可得：
   `short_term`（−1.4）最大 1.25、`comeback`（−4.6）最大 −1.95、`pool_pick`（−5.0）最大 −2.35
   ⇒ **这三类数学上不可能进档 0/1/2，恒为档 3**；`new_face`/`momentum` 上限 4.95/5.25 ⇒ 不可能进档 0。
   于是「档位」实际退化成「类别先验的粗化」，与第三排序键 `CAT_DISPLAY_PRIORITY` 语义重复。
   建议：阈值按**去掉 cat_base 后的残差分布**标定，或直接删掉 tier 这一层（`composite_score` 排序已足够）。
6. **`matcher` Chain A/B 是死代码**：`match()`（`:266`）/`chain_a_watch`（`:75`）/`chain_b_rebound`（`:179`）
   全仓无生产调用，`orchestrator` 只用 `label_all_candidates`。且即便调用也不产出：
   Chain A 门是今日涨幅 ≤ −0.5%（`:107`），Chain B 要求同一根今日 bar ≥ +2.0%（`:210`）→ 同轮互斥。
   建议：删除或明确标注「实验性·未接线」。
7. **形态维度的时间基准不统一**：`detect_new_face_patterns` 用 `historical_kline[-1]`（=**昨日**）
   （`patterns.py:108-119`），`detect_rebound_patterns` 用含今日的 `kline[-1]`（`:166`）。
   同一「形态确认」维度，一个不含今日、一个含今日，跨策略不可比（rebound 的 docstring 已说明，
   new_face 侧未说明）。建议在 `patterns.py` 顶部统一声明每个 `detect_*` 的时间基准。
8. **`PoolRow.acc5` 注释与实现相反**：字段注释写「排除今日外推」（`pool.py:29`），
   实现 `_closes_upto(kl, today)` **含今日**（`:34-45`），docstring `:59` 又写「今日收盘 vs 6 根前」=含今日。
   `orchestrator.v2_kline_summary:125` 把它写进 `accumulated_incl_today` —— 行为是对的，注释是错的，
   但这类「注释与口径相反」正是本项目口径错位类 bug 的温床。

---

## 十、建议的落地顺序

| 阶段 | 动作 | 前置条件 / 风险 |
|---|---|---|
| 1 | 修 `core_dip` 符号反向（P0-2）+ `_fund_flow_norm` 强流入（P1-7）+ `hot_watch` 量能/资金流（P2-3） | 纯 bug，补方向性单测即可；不涉口径，风险低 |
| 2 | 定唯一目标函数（P0-1）→ 三张类别先验表改为**同一脚本同日生成** | 这是**口径变更**，会改变所有排序结果；需 `rule_validate` 样本外验证 |
| 3 | 追涨门按类别豁免（P1-4）；kNF 排序键收口（P1-5）；清理死代码（`score_sort_key`/matcher Chain A-B/`entry_tier` 或 `composite_tier` 二选一） | 展示层变更，`scripts/golden_scan.py` 不受影响（不碰 `scan_with_raw`） |
| 4 | 统一量能投影口径（P1-9）+ 类别门限的求和/复利统一（P2-2） | 碰 `orchestrator`/`matcher`：改了 `scan_with_raw` 必须跑四日期黄金样本 |
| 5 | `momentum` 顶背离门（P1-6）与 `composite` 阈值（P2-5）：**先决定语义，再动代码** | 会影响候选集大小，属行为变更 |

**纪律提醒（沿用既有约定）**：任何常数/权重改动 → `python -m scanner.rule_validate`；
碰 `scanner/pipeline/` 或 `scan_with_raw` → `scripts/golden_scan.py` 四个日期全 0；
碰 `scanner/db/` → migrations 两个测试文件；`hot_watch` 阈值改动 → `--offline-demo` 全绿。
