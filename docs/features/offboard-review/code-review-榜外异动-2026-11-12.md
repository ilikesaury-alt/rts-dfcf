# 榜外异动（B 段）对抗性代码审查 · 2026-11-12

> ## ✅ 修复状态（2026-11-12 15:43 更新）
>
> | 项 | 状态 | 落地 |
> |---|---|---|
> | §2 P0 证据链两半口径打架 | ✅ **已修** | m026：`offboard_rejections` 加 `first_reason`/`first_percent`/`first_vol_ratio`/`first_main_pct`，首次被拒即冻结，与 launch_log 同锚 |
> | §4 reason 串含数值、无法聚合 | ✅ **已修** | m026：加 `reason_code`（门 ID）/`reason_value`（读数）/`gate`（阶段）；`reason` 串原样保留 |
> | §5 `flow_sum` 不落库 | ✅ **已修** | m026：加 `flow_sum` + `flow_judged`（区分「不可判定」与「恰好 0」） |
> | §6.3 `OFFBOARD_FLOW_WINDOW` 双定义 | ✅ **已修** | 删掉 `config_hot_watch.py:163` 的 `= 20` |
> | §3.1 三段各自算一次去偏 | ✅ **已修** | `biased_snapshot(bias=...)` + 主循环 `bias_out` 出参，B 段算一次、榜内异动段复用 |
> | §6.5 A 段未去偏但同区并列 | ⬜ **未修**（需产品决策） | AGENTS.md 已记「两处尺度不同，别混用」；终端/面板的用户可见提示未加 |
> | §7.1 留存行不写 rejections | ⬜ **未修** | 影响面小（留存票极少在分层被拒） |
> | §7.2 静默窗口内不回填 | ⬜ **未修** | 每天少 5~15 分钟标签累积，影响可忽略 |
> | §7.7 离线自检不覆盖去偏/留存 | ⬜ **未修** | 已由单测覆盖（`TestVolumeRatioBias` 等），但 `--offline-demo` 哨兵仍缺 |
> | §三 原 P1「k(m) 日内多值」 | ❌ **误报，已撤回** | k(m) 按定义随已交易分钟数递减，日内多值正确 |
>
> **实跑验证（2026-11-12 15:58，真实生产数据）**
>
> | 验证项 | 结果 |
> |---|---|
> | `ruff check .` | 全绿 |
> | `mypy scanner/` | 仅剩 1 条**改动前既有**的 `serialize_view` 报错 |
> | `pytest tests/` | **2258 passed, 6 skipped** |
> | `--offline-demo` | 23 条样本全 OK |
> | 生产路径实跑 | 榜单 99 条 → 产出 12 只，终端面板正常渲染 |
> | `reason_code` 聚合 | ✅ **修复核心价值已兑现**（见下） |
> | `flow_sum` / `flow_judged` | ✅ 13 行判定过、NULL 与 0 可区分 |
> | `vol_bias_k` | ✅ 45 行全有值 |
> | m026 生产库应用 | 已提交（Online Backup API 备份 + 行数校验 44706 = 44706） |
>
> 修复后的拒绝归因（此前 4 万条 reason 碎片无法聚合，现在一眼看清）：
>
> ```
> not_rising 854 | fund_outflow 297 | vol_ratio_low 80 | st 79
> market_cap_too_big 68 | flow_sum_negative 25 | price_too_high 13 | pct_too_high 10
> ma_bearish 3 | accum_5d_out 3 | no_quote 1 | main_pct_low 1 | bear_divergence 1
> ```
>
> ### 实跑中发现并修掉的三个追加缺陷（初版报告未列）
>
> 1. 🔴 **新列跟着信号列一起冻结 ⇒ 当天存量票永远拿不到**（`persist_round` 的
>    ON CONFLICT 不更新新列）。实测：跑完一轮后 `flow_judged` 全是 0 ——
>    那 45 只票当天早些时候已写入，新列只在首次写入时有值，而那正是「信号刚被推出」
>    那一刻 ⇒ 流门判定的读数被冻结在门外。
>    修法：`flow_sum`/`flow_judged`/`vol_bias_k` **每轮刷新**（它们是**判定读数**，
>    不是信号身份）。守卫 `test_persist_round_refreshes_judgement_columns_each_round`
>    + 反向守卫 `test_persist_round_keeps_signal_columns_frozen`（防顺手放宽冻结语义）。
> 2. `vol_bias_k` 被冲成 NULL：`vol_ratio`/`vol_ratio_raw` 是**冻结**列，k 是从这对值
>    算出来的；本轮候选没带 raw 时 `excluded.vol_bias_k` 为 NULL ⇒ 直接覆盖会冲掉
>    迁移回填的历史 k（实测 45 → 32 行丢 k）。修法：`COALESCE(excluded, 旧值)`。
> 3. CLI 内存库缺 5 张只读表 ⇒ 每轮刷 "no such table" 警告（fail-open 吞掉，
>    功能不受影响但噪音掩盖真告警）。修法：从生产库**导出**真实 DDL 建空表 ——
>    手写过一次就错了列名（`concept_cache.concepts` 写成 `concept`、
>    `offboard_print_snapshot` 漏了主键 `(date, print_seq, symbol)`）。

> 范围：`scanner/offboard_watch.py`（主逻辑，2072 行）、`scanner/offboard_flow.py`、
> `scanner/volume_bias.py`、`scanner/config_hot_watch.py`（阈值）、
> `scanner/onboard_anomaly.py`（榜内异动，复用同一批门）、集成层
> （`unified_scanner.py` / `scanner/push_gate.py` / `scanner/view/*` / `scanner/db/dal.py`）。
> 证据来源：源码通读 + 生产库 `scanner.db` 只读查询（2026-11-12 15:10 快照）。
> 方法：对抗性审查 —— 假设作者故意埋雷，专找「注释宣称的口径 ≠ 代码实际行为」。

---

## 一、结论摘要

B 段的**主链路结构是干净的**：单源纪律成立（阈值只在 `config_hot_watch.py` 定义）、
fail-open / fail-closed 分工明确（风险门 fail-open，信号门 fail-closed）、
冻结语义（信号值首次写入即冻结）确实兑现、剔除今日 bar 的口径在 MA / 顶背离 / 5日累计
三处一致。

但存在 **2 个真实缺陷**（会导致静默错误结果）、**4 个口径矛盾**（注释/文档与代码不符）、
**3 个证据链断裂**（导致无法复盘）。

> ⚠ **勘误（2026-11-12 15:23）**：初版把「量比去偏 k(m) 日内多值」判为 P1 缺陷，
> **该结论已撤回** —— k(m) 按定义随已交易分钟数递减，日内多值是正确的。
> 详见 §三 的撤回说明；该节降级为 P3 的一条相关缺陷。

**最要紧的一条（P0）**：`offboard_rejections` 的 `percent` / `vol_ratio` / `main_pct`
三列是「最新一轮」覆盖写，而 `offboard_launch_log` 的同名列是「首次写入即冻结」。
⇒ **同一张证据表的两半，记录的是同一只票在不同时刻的两个不同状态**。
生产库实证：11-12 当日 `SZ300063` 在 `launch_log` 里冻结 percent = **+2.05**（当日
曾以「上涨异动」身份被推出），在 `rejections` 里却是 percent = **−1.83**、理由
「当前非上涨状态」—— 同一只票、同一时刻，两张表给出**相反的结论**。
这张表是本段唯一的证伪链（`rule_validate` 看不见本模块），它自相矛盾 ⇒ 任何阈值复盘
都建立在不可信的对齐关系上。

---

## 二、P0：证据链自相矛盾（真实缺陷，会导致静默错误结论）

### 2.1 冻结列 vs 覆盖列，同一张表的两半口径分叉

| 表 | 列 | 语义 | 实现位置 |
|---|---|---|---|
| `offboard_launch_log` | `percent` / `vol_ratio` / `tier` / `accum_5d` | **首次写入即冻结**（当日第一次被推出时的身份） | `offboard_watch.py:1084-1088`（ON CONFLICT 只更新 `last_hit_time`/`top_hits`/`gate_hits`/`updated`） |
| `offboard_rejections` | `percent` / `vol_ratio` / `main_pct` / `reason` | **最新一轮覆盖**（取最新状态） | `scanner/db/dal.py:580-587`（ON CONFLICT SET `reason=excluded.reason`, `percent=excluded.percent` …） |

`dal.py:566-567` 的 docstring 明写这个差异是**刻意的**（"被拒理由会随盘中漂移，最后状态才有复盘意义"）。
这个设计对**单表**成立，但对**双表 join** 不成立 —— 而这张表存在的唯一理由就是
"让幸存者偏差可被证伪"，证伪必须能把被拒样本和产出样本**对齐到同一时刻**。

### 2.2 生产库实证（2026-11-12）

```
launch_log（冻结值） vs rejections（最新轮覆盖值），同日同 symbol：
SZ300063  launch +2.05  |  rejections −1.83  理由「当前非上涨状态」
SZ300314  launch +3.43  |  rejections +0.54  理由「量比不足(1.34<1.5)」
SZ300550  launch +4.69  |  rejections +1.66  理由「量比不足(1.41<1.5)」
SZ300581  launch +2.80  |  rejections +3.07  理由「量比不足(1.25<1.5)」
```

`SZ300314` / `SZ300550` / `SZ300581` 三例尤其刺眼：**它们在 `launch_log` 里是"产出票"**
（曾严格过门、被推出、拿到了 tier 和次日标签），而在 `rejections` 里却是
**"被门拒掉的票"**（理由「量比不足」）。

⇒ 同一 (date, symbol) 同时出现在两张表里，含义是「先产出、后因活跃度回落被拒」。
这个现象本身没错（盘中反复横跳是真实的），但**两张表没有共同时间锚点**，
join 后得到的是「一只票当日早些时候的状态」和「晚些时候的状态」拼在一起。

### 2.3 影响

- 「按拒绝理由重放阈值」这件事 —— 2026-09-22 那次杠杆测量手工重建的就是这一步，
  `offboard_watch.py:_rejection_rows` docstring 明写"这三列正是事后重放阈值的输入"，
  "跑完即失" —— 现在**跑完即错**：拿到的 percent/vol_ratio 与产出样本不是同一时刻，
  门阈值重放结论会系统性偏移。
- 更隐蔽的后果：`rejections` 的 `vol_ratio` 是**最新轮**的值，而最新轮往往正是
  量比回落的时刻 ⇒ 被拒样本的 vol_ratio 分布被系统性压低 ⇒ 事后若按
  `vol_ratio` 重放量比门，会得出「量比门杀过头了」的错误结论。
- **不可回答的问题**：某道门在当日各时点分别杀了多少票、各被杀票的次日表现如何，
  现在答不出来（`rejections` 只有最新态，`hits` 只有计数没有时序）。

### 2.4 建议方向（不改阈值）

给 `offboard_rejections` 补一个**与 launch_log 同锚**的字段：把**首次被拒**时的
percent/vol_ratio 冻结下来（与 `first_time` 同一时刻），另留 latest 列。
或者更简单：让 `rejections` 也走"首次写入即冻结"，与 `launch_log` 严格对齐 ——
代价是丢掉"最后状态"这个视角，但那个视角本就该由别处提供。
⚠ 本仓铁律是"发布后不得改名/删列"，只能**追加**列。

---

## 三、P1（已撤回）：量比去偏「日内 k 值不稳定」

> 🔴 **本节结论已被实测推翻，保留在此仅作记录。**
> 初版审查（同日 15:10）把 `offboard_launch_log` 里 `vol_ratio_raw / vol_ratio`
> 在同一天出现 1.08~2.66 的多个取值，判为「k(m) 跨轮漂移」。
> **这个判断是错的**：k(m) 定义上就是已交易分钟数 m 的函数，日内必须递减。
> 复查（15:23）按 `first_time` 分组后：
>
> ```
> 09:47 k=2.66   09:57 k=2.19   10:11 k=1.88   10:39 k=1.50   11:14 k=1.24   13:54 k=1.05
> ```
>
> 单调递减，且反算 `F = k × m/240`（全市场累计量能占全天比例）为
> 09:47→0.19、13:54→0.76，单调上升、收盘趋近 1 —— **形状正确**。
> 故：
> - ❌「同一天 k 应只有一个值」不成立，k(m) 本就随 m 变；
> - ❌「k 未落库无法重建当轮尺度」不成立，`vol_ratio_raw / vol_ratio` 即当轮 k；
> - ❌「`sort_key` 那句『同一轮内相对序不变』被击穿」不成立，同一轮内 k 仍是单一标量。

### 3.1 但复查中发现一条**真实**的相关缺陷（降级为 P3）

`volume_bias.py:snapshot_elapsed_minutes`（:181-203）用
`SELECT MAX(updated) FROM market_extra_cache WHERE data_type='fund_flow' AND date=?`
取「快照时刻」，再折算成已交易分钟数 m。

问题不在 k 的取值，而在 **m 的来源与 snapshot 未必是同一份数据**：
- 实证 2026-11-12：全表 1453 行 `updated` **完全一致**（均为 `14:56:13.907751`），
  说明正常轮次是整表同批写入 ⇒ `MAX(updated)` 等于该批的时刻 ✓；
- 但 `market_extra_cache` 的写入侧在**全集拉取超时**时只落取到的那些行
  （`offboard_watch.py` 多处注释已记录这个行为）⇒ 此时旧行保留各自旧 `updated`，
  而 `MAX(updated)` 仍指向最新那一批 ⇒ **m 偏大** ⇒ k 偏小 ⇒ 去偏不足。
- 更关键：`market_bias(conn, snapshot)`（:205-235）的 `elapsed` 读自 `conn`，
  而 `fracs` 用的是**传进来的 `snapshot`**。生产路径两者同源 ✓，
  但 **CLI 路径**（`offboard_watch.py:main`，`bias_conn=prod` + 内存库 `conn`）
  与 **B 段/榜内异动段各自调用一次** `biased_snapshot` 的路径下，
  两者可以来自不同时刻 ⇒ 两段可能拿到不同的 k。

影响：量比门在「快照部分刷新」的轮次上会偏松（去偏不足），且 B 段与榜内异动段
在极端情况下尺度不一致 —— 这正是 `offboard_watch.py` 模块 docstring 要求
「同一个阈值必须配同一个尺度」但**实现未保证**的地方。

修法（不改阈值）：让 `run_offboard_watch` 算一次 `bias` 后**显式传给**
`run_onboard_anomaly`（或让主循环统一算一次），而不是各算各的。

---

## 四、P2：`offboard_rejections` 只留"最后一条理由"，丢掉真正的归因

### 4.1 现象

`dal.py:580-587` 的 ON CONFLICT 只保留**最后一条** reason，且
`offboard_rejections` 的主键是 (date, symbol) ⇒ **一只票一天只能有一条拒绝记录**。

实证：11-12 当日 1446 行 / 1446 个唯一 symbol / `SUM(hits)` = **314353**
⇒ 平均每票当天被拒 **217 次**（round_log 显示当日 224 轮）。
但库里只留最后一条理由。

### 4.2 后果

- 「某道门误杀了多少票」这个问题的答案**严重失真**：
  库里的 reason 分布反映的是**最后一轮**的分布，不是"这道门当日累计杀了几只"。
  实测 reason 分布：'当前非上涨状态' 18273、'ST/退市风险股' 2092、
  'K线未补取(额度外)' 718、'MA空头排列' 293，其余全是
  '量比不足(x.xx<1.5)' 的碎片（每条 reason 因数值不同而各成一类，共 4 万余条里
  绝大多数是量比不足的**不同数值**）。
- ⚠ 顺带发现一个**真问题**：`reason` 串把**当轮的具体数值**编进了 reason
  （如「量比不足(0.90<1.5)」）⇒ 同一逻辑门产生 4 万条互不相同的 reason
  ⇒ **无法按门聚合统计**。生产库 44706 条 rejections 里，
  按 reason 精确 GROUP BY 得到的主要"门"其实是 0.90/0.82/0.89… 这类数值碎片。
  这让"哪道门杀得最多"这个最基本的问题，当前**答不出来**。

### 4.3 建议

- reason 串改成**结构化**（门 ID + 数值分列），或至少把数值从 reason 里剥出来单独成列。
- 若要保留时序，需要一张 append-only 的拒绝事件表（或给 rejections 加
  `first_reason` / `first_percent` 等首次态列，与 launch_log 对齐）。

---

## 五、P3：`flow_sum`（N 日主力累计门）的判定结果没有落库 ⇒ 无法事后核验

### 5.1 现象

`OFFBOARD_FLOW_WINDOW = 10`（`config_hot_watch.py:173`）、`OFFBOARD_FLOW_MIN_SUM = 0.0`。
`offboard_flow.py:sum_main_net` 返回 None 表示"历史不足 10 天 ⇒ 不可判定"，
调用方（`offboard_watch.py:_flow_gate_reason`）对 None **fail-open 放行**。

- 判定的输入 `flow_sum` 是**每轮实时算**的（`_load_flow_sums` → `load_flow_history`
  → `market_extra_cache`），**不落库**。
- `offboard_launch_log` 的 schema（实证 PRAGMA）**没有 flow_sum 列**。
- ⇒ 事后无法回答："当日过门的票，它当日的 10 日主力累计是正是负？"
  只能拿**今天的** `market_extra_cache` 历史去反推**过去某天**的窗口值 ——
  而历史窗口的成员日快照会被后续覆盖（`market_extra_cache` 是 INSERT OR REPLACE），
  近似但不等价。

### 5.2 另一个细节

`offboard_flow.py:sum_main_net` 里 `if not history or len(history) < window: return None`。
但 `load_flow_history` 的 SQL 是 `date < as_of`（`offboard_flow.py:105`），
`as_of` 默认 `_today()` ⇒ 窗口是"今天之前的 10 天"。
由于 `market_extra_cache` 的历史覆盖良好（实证：5348 个 symbol 中 5347 个有 ≥10 天
fund_flow 历史），**这扇门在实践中几乎恒为"可判定"** ⇒ fail-open 形同虚设，
但代码仍按"新股会被 fail-open 放行"来设计 ⇒ **注释描述的偏差风险在当前数据下不存在**，
而真正的风险（窗口内含停牌/缺失日时 `len(history) < 10` ⇒ 整窗判不可判定 ⇒ 放行）
没有被识别出来。

### 5.3 建议

给 `offboard_launch_log` 追加一列 `flow_sum`（可空 + 一个 `flow_judged` 标志位区分
"不可判定"与"等于0"）。⚠ 阈值 `OFFBOARD_FLOW_MIN_SUM = 0.0` 意味着
**"恰好 0"和"不可判定"必须能区分** —— 当前两者都是 None/0 混着，语义已经糊了。

---

## 六、口径矛盾（注释/文档宣称 ≠ 代码实际）

### 6.1 ~~`sort_key` 声称"去偏不改变同一轮内的相对序" —— 已被实测违反~~

**已撤回（见 §三 勘误）**：同一轮内 k 是单一标量，该论断成立。
初版把「日内 k 随 m 递减」误读为「跨轮 k 漂移」。

### 6.2 "榜内异动段同改…同一个阈值必须配同一个尺度" —— 实现上是两次独立计算

`offboard_watch.py` 模块 docstring 与 `volume_bias.py:biased_snapshot` docstring
都宣称三段同尺度，实际是 B 段算一次（`offboard_watch.py:1525`）、榜内异动段再算一次
（`onboard_anomaly.py:300`），**两次各自读库取 m**。
生产路径同源通常一致，但「部分刷新」轮次或 CLI 路径下可能不一致 ⇒
纪律写下了，实现没有**保证**。详见 §3.1。

### 6.3 `OFFBOARD_FLOW_WINDOW` 被定义了两次

`config_hot_watch.py:163` 定义 `OFFBOARD_FLOW_WINDOW = 20`，
`:173` 又定义 `OFFBOARD_FLOW_WINDOW = 10` ⇒ 后者覆盖前者。
**注释本身已经解释了这个意图**（窗口 20 覆盖率只有 76.9%，窗口 10 覆盖率 100%），
但留下一个"定义了两次"的坑：任何后来者把 `:163` 改成 25（以为那是生效值）
会**静默无效**。这违反本仓"单源纪律"的精神。
建议直接删掉 `:163` 那行（注释合并到 `:173`）。

### 6.4 `opening_silence_active` 与 `unadjusted_silence_active` 的兜底关系

`offboard_watch.py:opening_silence_active` docstring 说
"k(m) 算不出来 ⇒ 退回旧的 15 分钟窗口（见 `unadjusted_silence_active`）"，
`run_offboard_watch` 里也确实这么调（:1525-1527）。
但 `OFFBOARD_OPENING_SILENCE_MIN_UNADJUSTED = 15` 与
`OFFBOARD_OPENING_SILENCE_MIN = 5` 的**关系只在 `run_offboard_watch` 里被正确处理**；
而 `main()` CLI 里只检查 `opening_silence_active()`（:2035），不检查 unadjusted 变体
⇒ **CLI 在 5~15 分钟区间会显示"静默窗口生效"的提示，但实际上生产路径此时已可产出**
⇒ CLI 提示与生产行为不一致。

### 6.5 `hot_watch`（A 段）用**未去偏**的量比，与 B 段同区并列

`scanner/hot_watch.py:_snapshot_vol_ratio_map`（:378）直接读快照 `vol_ratio`，
**不走去偏**。`volume_bias.py` 模块 docstring 明写"榜内**飙升**段（A 段）**未改**
（用户决策）"。

⇒ 后果：**同一个终端区块内，「榜内飙升」行的量比是原始尺度、「榜外异动」行的量比是
去偏尺度**，两者并列显示。11-12 实测去偏倍数 1.08~2.66 ⇒ A 段显示的量比
**系统性高于** B 段同刻的真实放量倍数。用户横向比较两段时会读出错误的相对关系。
这是"未改"的已知代价，但**没有被写进任何用户可见的地方**（终端/面板/AI 分析都没提示
"本段量比已去偏"）。

### 6.6 `MOMENTUM_LAUNCH_VOL` 的语义在去偏前后并非"完全一致"

`offboard_watch.py` 模块 docstring 称
"阈值一个字没改：`MOMENTUM_LAUNCH_VOL`(1.5) 的语义…去偏前后完全一致 ⇒ 不存在无标签调参"。
实测：去偏把上报量比整体除以 k（1.08~2.66）⇒ **同一个 1.5 阈值在物理上对应了
完全不同的放量倍数**（1.5×1.08=1.62 倍 vs 1.5×2.66=3.99 倍）。
口径的**意图**没变（仍是"相对自身近5日均速"），但**实现的数**变了 ⇒ 门是**收紧**的。
`volume_bias.py` docstring 也承认"早盘实测我方 k 比东财隐含值高 10~25% ⇒ 去偏略过度
⇒ 量比门在早盘偏严"。⇒ **这实质是一次未被记账的阈值调整**，方向明确（更严），
但它绕过了本仓"无标签不得调阈值"的铁律 —— 且 `offboard_watch.py` 里多处注释
把它描述为"不是调阈值"。

---

## 七、其他（次要 / 文档级）

| # | 位置 | 问题 |
|---|---|---|
| 7.1 | `offboard_watch.py:_retain_candidates` | 留存行**不写** `launch_log`（防自续命，设计正确），但留存行也**不写** `offboard_rejections` ⇒ 留存票若在分层阶段被拒，留痕缺失。 |
| 7.2 | `offboard_watch.py:run_offboard_watch` | `backfill_next_day`（:1602）在 `build_candidates` 之后调用，注释（:1600-1603）说"移到所有提前返回之前…零产出轮也执行"，但**静默窗口早退（:1485）仍在它之前** ⇒ 静默窗口轮**依然不执行回填** ⇒ 早盘 5~15 分钟不累积标签。影响很小（每天只少 5~15 分钟），但与注释宣称不符。 |
| 7.3 | `offboard_watch.py:persist_round` | `gate_hits=gate_hits+1` 在 ON CONFLICT 里**硬编码 +1**，而 INSERT 路径也写 1 ⇒ 一致 ✓。但 `top_hits=top_hits+excluded.top_hits` 的 `excluded.top_hits` 来自 `1 if i < display_top else 0` ⇒ 前 N 外恒 0 ✓（2026-10-16 修复已兑现）。实证 11-12：SUM(top_hits)=2534 / COUNT=45 ⇒ 平均 56 次/票，符合"反复在前 12 名内"的预期 ✓。 |
| 7.4 | `scanner/db/dal.py:577-589` | `save_offboard_rejections` 的 `reason` / `percent` 等被覆盖 ⇒ 见 §4；另 `hits` 累加的是**本轮命中数**，但同一轮内一只票只会出现一次 ⇒ `hits` 实为"被拒轮次数" ✓（实证平均 217 次/日 ≈ 224 轮）。 |
| 7.5 | `AGENTS.md` | 「榜外异动」在架构树里标注 `offboard_watch.py # 榜外异动段（launch_log 写侧、实时覆盖、标签回填）」，但**未提** `offboard_flow.py`（N日流门）、`volume_bias.py`（量比去偏）两个 2026-11 新增模块 ⇒ 架构树与实际不符。 |
| 7.6 | `config_hot_watch.py:163/173` | `OFFBOARD_FLOW_WINDOW` 双重定义 ⇒ §6.3。 |
| 7.7 | `offboard_watch.py:_DEMO_CASES` | 离线自检**不覆盖**量比去偏路径（`volume_bias` 不参与 `run_offline_demo`），也**不覆盖**留存(dwell)路径 ⇒ 2026-11-05 与 2026-10-09 两次重要改动**都没有离线自检哨兵**。 |

---

## 八、测试守卫覆盖评估

| 行为 | 是否有测试 | 备注 |
|---|---|---|
| `offboard_gate` 各门 | ✓ `test_offboard_watch.py:273-347` | 覆盖较全 |
| `classify_tier` 分层 | ✓ `:434-611` | 含"剔除今日"口径测试（:544） |
| `sort_key` 排序 | ✓ `:637-657` | |
| K 线池缓存/补取 | ✓ `:755-819` | |
| `persist_round` 冻结语义 | ✓ `:844-877` | |
| 次日回填三条防呆 | ✓ `:999-1296` | 含跨节假日、脏 bar |
| **量比去偏** | ✓ `test_volume_bias.py` + `test_offboard_watch.py:1334-1387` | 覆盖去偏数值路径；**未覆盖**「部分刷新轮次下 m 的来源」⇒ §3.1 无守卫 |
| **`rejections` 与 `launch_log` 口径对齐** | ✗ **无** | ⇒ §2 完全无守卫 |
| **留存(dwell)机制** | 部分 | 有 `offboard_gate(retain=True)` 的单测，但**没有"留存不写 launch_log / 不自我续命"的端到端测试** |
| **CLI 路径 vs 生产路径一致性** | ✗ **无** | §6.4 的不一致无人拦截 |
| **`flow_sum` 落库可核验** | ✗ 无（该列根本不存在） | |

---

## 九、优先级建议

| 优先级 | 事项 | 性质 |
|---|---|---|
| **P0** | §2 证据链两半口径对齐（rejections 与 launch_log 同锚） | 真实缺陷，静默错误结论 |
| **P1** | §4 reason 结构化（门 ID 与数值分离） | 证据链不可聚合，4 万条碎片 |
| **P1** | §6.3 删除 `OFFBOARD_FLOW_WINDOW` 重复定义 | 单源纪律，低成本 |
| **P3** | §3.1 三段共用一次去偏（主循环统一算） | 极端路径下尺度不一致 |
| **P2** | §5 `flow_sum` 落库 + 区分"不可判定"与"=0" | 证据链可用性 |
| **P2** | §6.5 A 段未去偏 ⇒ 同区并列口径差，需在用户可见处标注 | 展示层诚实 |
| **P3** | §7.5 架构树补两个模块；§7.7 离线自检覆盖去偏与留存 | 文档 / 哨兵 |

**明确不建议**：任何阈值调整。§6.6 指出去偏实质是一次"隐形的收紧"，
若要复盘它，应先把它**显式记账**（把 k 落库、把等效阈值写出来），
再按 observe-first 收集 D ≥ 47 交易日后评估，而不是继续在"测量修正"的措辞下改数。

---

*审查人：Alma · 2026-11-12 · 生产库证据取自 `scanner.db` 只读快照*
