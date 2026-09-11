# 重构方案建议

日期：2026-09-11
依据：对代码库的量化普查（规模 / 复杂度 / 依赖 / 测试 / 工具链）+ 前期核心管线审查结论

---

## 零、先给结论

**我的建议是：不做「重构」，做「偿还」。**

这个项目容易出现两种误判，我先把它们排掉：

**误判一：「代码很乱，需要重写」。** 不成立。普查数据显示工程质量明显高于同类个人项目：

- 生产代码 20,477 行 / 60 文件，测试 17,576 行 / 1,200 个用例 —— **测试:生产 ≈ 0.86**，
  这在量化策略类项目里属于相当高的水平；
- 已有 CI（`.github/workflows/ci.yml`）：ruff 阻断、mypy 非阻断、pytest 全跑；
- 唯一真源纪律真实存在：`config.py` 被 **46 个模块**依赖，`categories.py` 统一类别注册表；
- 无人引用的公开符号仅 **8 个**（44,092 行里），死代码率极低。

**误判二：「既然质量好，就不用动」。** 也不成立。有四处**结构性**问题正在持续产生摩擦，
而且摩擦成本随提交速度指数上升 —— 近 30 天 **178 次提交**（全项目 418 次），
约每天 6 次。这个速度下，坏结构每天收的「税」是可观的。

所以：**保守重构 + 高杠杆，目标不是让代码变漂亮，是让「改一行不再需要读 500 行」。**

---

## 一、现状度量（全部实测，非印象）

### 1.1 规模

| 项目 | 数值 |
|---|---|
| Python 总量 | 44,092 行 |
| 生产代码（`scanner/` 60 文件） | 20,477 行 |
| 测试代码（`tests/` 59 文件 / 1,200 用例） | 17,576 行 |
| 顶层脚本（12 个） | 3,326 行 |
| `scripts/` 分析脚本 | 2,713 行 |
| `config.py` 常量 | **320 个** |
| 策略类别（`CATEGORY_REGISTRY`） | 9 个 |
| DB 表 | 20 张 / 最大表 `market_extra_cache` 58,321 行 |

### 1.2 复杂度热点（24 个函数 > 90 行）

| 函数 | 行数 | 圈复杂度 | 问题本质 |
|---|---:|---:|---|
| `orchestrator.scan_with_raw` | 507 | **105** | 候选池→分类→评分→增强→校验→过滤→落库 全在一个函数 |
| `display.build_scan_view` | 381 | **80** | 视图拼装 + 业务判断（含排序/档位/减仓过滤）混杂 |
| `db.schema.init_db` | 314 | 15 | 15 个迁移块线性堆叠，无版本化编排 |
| `portfolio_backtest.run_backtest` | 179 | 35 | — |
| `analysis.analyze_short_term` | 158 | 36 | 与 momentum/rebound/new_face 高度同构 |
| `analysis.analyze_momentum` | 185 | 28 | 同上 |
| `kline_fetch.fetch_all_klines` | 143 | 41 | 并发编排与重试/降级交织 |
| `historical_rescan.rescan_all_signals` | 139 | 33 | — |
| `db.dal.save_recommendations` | 113 | 16 | — |
| `final_pick.build_final_picks` | 106 | 31 | — |

`ruff` 已把这些（C901 / PLR0912 / PLR0915）列入 `ignore` 作为「已知技术债」——
说明问题被识别过，只是没排期。

### 1.3 依赖结构

```
扇入 TOP：config 46 · utils 32 · models 26 · database 14 · trading_session 13
扇出 TOP：orchestrator 23 · display 13 · historical_rescan 9 · candidates 9
```

`config` 扇入 46 是**特征而非缺陷** —— 单一真源纪律的直接体现。但它同时是
**重构的主要约束**：任何 config 结构调整都会波及近半模块。

各模块从 config 导入的符号数：`enhancer` 64 · `analysis` 60 · `validator` 41 · `ranking` 15。

### 1.4 工具链现状

| 能力 | 状态 |
|---|---|
| Lint（ruff） | ✅ CI 阻断，全绿 |
| 类型检查（mypy） | ⚠️ 非阻断（`continue-on-error: true`），60 文件通过 |
| 测试 | ✅ 1,200 用例，1208 passed / 13 skipped |
| **CI lint 范围** | ⚠️ **漏检** `scripts/`、`today_report.py`、`prevday_perf.py`、`diag_*.py` 等 6 个顶层脚本 |
| 覆盖率门禁 | ❌ 无 |
| 依赖锁定 | ❌ 仅 `>=` 下限，无 lock 文件 |
| 打包配置 | ❌ 无 `[project]`，无 `packages` 声明 |

---

## 二、四个真正值得动的地方

### 问题 A：`orchestrator.scan_with_raw` 507 行 / 复杂度 105

**为什么是问题**：它是**主数据通路**，每轮扫描都走。复杂度 105 意味着
「读一遍无法在脑中维持完整状态」—— 任何改动都得靠 grep 反复确认，
且极易引入只在特定分支触发的 bug（这正是本项目历史上多次出现的问题类型：
错位口径、静默降级、单分支遗漏）。

**根因**：把 7 个**语义上可独立描述**的阶段串成了一根长函数：

```
候选池构建 → 分类打标 → 评分 → 增强(live enrich) → 校验 → 提取(candidates) → 落库
```

**建议**：按阶段抽出**纯函数**，保留 orchestrator 作为编排者。

```
scanner/pipeline/
  __init__.py
  build_pool.py       # 候选池构建（含掉榜/重启回补）
  classify.py         # 分类打标
  scoring.py          # 评分调用
  enrich.py           # live 增强（资金流/市值/换手/RPS/热度）
  validate.py         # 后置校验
  persist.py          # 落库 + excluded 标记
```
`scan_with_raw` 退化为 ~80 行的线性编排：
```python
def scan_with_raw(raw, conn, adapter):
    pool = build_pool(raw, conn)
    classify_all(pool)
    score_all(pool)
    enrich_all(pool, conn)
    validate_all(pool)
    picks = extract_candidates(pool)
    persist(conn, picks)
    return ScanResult(...)
```

**关键纪律**：这是**等价变换**，不是重写。每个阶段抽出的函数必须
「输入输出与原来完全一致」，用现有的 1,200 个测试 + 一次真实扫描对比
（同一份 `raw` 输入，对比 `ScanResult` 的逐字段一致）来证明。

**风险**：中。**收益**：高。这是整份方案里性价比最高的一项。

---

### 问题 B：`display.py` 里藏着业务逻辑

**为什么是问题**：`build_scan_view`（381 行 / 复杂度 80）不只是渲染——
它内部做了**排序、档位判定、减仓标签过滤**。这意味着：

- 「展示什么」和「什么是对的」耦合在一起，无法单独测试业务规则；
- 更严重的是**多真源**：`_SELL_TAGS` 这个集合在 **3 处**独立定义：

```
scanner/display.py:885   _SELL_TAGS = {"⬇减仓", "⬇减半", "🔻勿接", "💰落袋"}
scanner/display.py:951   _SELL_TAGS = {"⬇减仓", "⬇减半", "🔻勿接", "💰落袋"}
scanner/final_pick.py:73 _SELL_TAGS = {"⬇减仓", "⬇减半", "🔻勿接", "💰落袋"}
```

改一处忘另两处 = 展示与终选口径不一致，且**不会报错**，只会静默产生
「显示有该票但终选没有」这类难查的问题。这与项目「单一真源」纪律直接冲突。

**建议**（两步，可独立执行）：

1. **先消多真源**（低风险、立即做）：`_SELL_TAGS` 移入 `categories.py`
   （它已是「标签/颜色/优先级/建议」的唯一真源），三处改为导入。
   顺带把 `intraday_tactics.py` 里产生这些标签的字面量也改为引用常量 ——
   消除「生产者写字面量、消费者写另一份字面量」的耦合。
2. **再拆视图层**（中风险）：
```
scanner/view/
  __init__.py
  model.py      # ScanView 数据类（纯数据，无 IO）
  assemble.py   # 从扫描结果组装 ScanView（含排序/档位/过滤 —— 业务逻辑）
  render.py     # 把 ScanView 渲染成终端文本（纯展示）
```
   收益：业务规则可单测（`assemble` 接受 dict 返回 dataclass），
   渲染可换实现（终端/飞书/HTML）而不动规则。

**注意**：`display.py` 现有 1,251 行测试（`tests/test_display.py`）是安全网，
重构前先确认这些测试**断言的是行为而非行号/字符串格式**，否则会变成
「改结构就红一片」的假警报。

---

### 问题 C：`config.py` 320 常量 / 1,006 行 —— 单一文件但六个关注点

**为什么是问题**：它承担了 6 种不同性质的内容，混在一个文件里：

| 关注点 | 示例 | 变化频率 |
|---|---|---|
| 策略阈值 | `NEXTDAY_HIT_THRESHOLD`、各权重 | 高（每次调参） |
| 风险标签定义 | `RISK_FLAGS_HARD_FILTER`、`FUND_RISK_TAG` | 中 |
| 展示常量 | 档位文案、操作建议映射 | 中 |
| 时段窗口 | 8 个分割点 | 低 |
| 离线工具参数 | `TRIPLE_BARRIER_*`、`HOLD_DAYS_BY_CATEGORY` | 低 |
| 环境标志 | 数据源开关、飞书 webhook | 低 |

后果：**改一个策略阈值要在 1,006 行里定位**，且没有物理边界阻止
「展示常量」与「策略阈值」互相引用。

**建议**：**保持 `scanner.config` 为唯一导入路径**（这是关键 —— 46 个模块
依赖它，不能破坏），但内部拆为子模块并 re-export：

```
scanner/config/
  __init__.py        # 从子模块 re-export 全部符号（对外契约不变）
  strategy.py        # 策略阈值与权重
  risk.py            # 风险标签 / 硬排除集合
  display.py         # 档位 / 文案 / 操作建议
  session.py         # 时段窗口
  offline.py         # 离线工具参数
  env.py             # 环境变量读取
```

**兼容性保证**：`from scanner.config import X` 全部继续可用（`__init__.py` re-export）。
这是**对外零破坏**的重构 —— 46 个模块一行都不用改。

**风险**：低。**收益**：中（定位成本从「1006 行」降到「一个子模块」）。

**为什么不建议**更激进的做法（如改成 dataclass 配置对象 / 引入 pydantic）：
那会破坏 `from scanner.config import X` 契约，46 个模块全要改，
且与项目「常量即配置」的既有风格冲突 —— 收益不足以抵消风险。

---

### 问题 D：`init_db` 缺版本化编排

**为什么是问题**：314 行里 15 个迁移块**线性堆叠且互相无关联**，
没有「哪些已执行」的记录（`schema_version` 只记了一个全局版本号，
不记录**具体哪条迁移**跑过）。

这正是 09-11 修复的那个高危缺陷的**土壤**：那条 v4 迁移之所以能
「静默跳过、永不重试」，根本原因是**迁移的正确性靠现场探测（PRAGMA 查 PK），
而不是靠「已执行」记录**。探测式迁移在中断场景下必然不可靠。

**建议**：引入**迁移列表 + 已执行记录**：

```python
MIGRATIONS: list[Migration] = [
    Migration(id="m001_market_extra_cache_pk_v4", up=_migrate_mec_pk),
    Migration(id="m002_...", up=...),
]

def init_db():
    ...
    applied = {r[0] for r in conn.execute("SELECT id FROM schema_migrations")}
    for m in MIGRATIONS:
        if m.id in applied:
            continue
        m.up(conn)                       # 单事务 + 失败上抛（沿用 09-11 已确立的纪律）
        conn.execute("INSERT INTO schema_migrations VALUES (?, ?)", (m.id, now))
```

收益：
- **中断可恢复**：跑过的不会再跑，没跑的会重试（不再依赖现场探测）；
- **新增迁移有模板**：不必再想「怎么探测旧状态」；
- **可审计**：`SELECT * FROM schema_migrations` 就是库的演进史。

**风险**：中（涉及生产库 schema）。**建议时机**：**最后做**，且必须先备份。

---

## 三、不建议动的地方（明确说「不」）

重构方案的价值一半在「不做什么」。以下我建议**保持原样**：

| 项 | 为什么不建议动 |
|---|---|
| **`config` 扇入 46 的中心地位** | 这是纪律的体现，不是耦合问题。拆成 config 对象会破坏 46 个模块 |
| **`categories.py` 的注册表模式** | 已经是「单一真源」的正确实现，是范本而非问题 |
| **`db/` 三层拆分（schema/queries/dal）** | 职责清晰，870 行的 dal 尚可接受 |
| **fail-open 异常纪律** | 只捕获 `EXTERNAL_FAILURES` 是正确设计，不要改成 broad except |
| **测试:生产 = 0.86 的投入比例** | 继续维持，不要为了「重构进度」降测试 |
| **`analysis.py` 四个 analyze_* 的同构重复** | 有重复但**语义确实不同**（new_face/momentum/rebound/short_term 的判据不同）。强行抽象成「参数化通用函数」会得到一个满是 if 的怪物 —— 这是**已知的、可接受的**重复 |
| **顶层 12 个脚本** | 是 CLI 入口，不是库代码。收拢进包只会增加使用摩擦 |

---

## 四、执行顺序（按「风险调整后收益」排序）

```
第 0 步 · 加固安全网（必须先做，~半天）
  ├─ CI lint 范围补齐：scripts/ + 6 个漏检顶层脚本
  ├─ 加覆盖率基线（pytest-cov，先只记录不设门禁）
  └─ 记录重构前基线：pytest 全量结果 + 一次真实扫描的 ScanResult 快照
      → 这是判断「等价变换」是否成功的唯一依据

第 1 步 · 消多真源（低风险，~1 小时）          ★ 建议今天做
  ├─ _SELL_TAGS → categories.py（3 处改导入）
  └─ intraday_tactics 产生标签处改引用常量

第 2 步 · display 拆层（中风险，~1-2 天）
  ├─ 先确认 test_display 断言的是行为而非格式
  ├─ 抽 ScanView dataclass + assemble（业务）
  └─ render 保持现状先不动（降风险）

第 3 步 · orchestrator 拆阶段（中风险，~2-3 天）  ★ 收益最高
  ├─ 抽 pipeline/ 六个纯函数
  ├─ scan_with_raw 退化为编排
  └─ 用「同一 raw 输入 → ScanResult 逐字段一致」证明等价

第 4 步 · config 内部拆分（低风险，~半天）
  └─ 全部 re-export，对外契约零变化

第 5 步 · init_db 迁移版本化（中风险，~1 天）    ★ 最后做
  └─ 先 cp scanner.db；改完在备份库上演练一次
```

**为什么不按「问题严重性」排**：问题 A（orchestrator）最严重，但它依赖
第 0 步的安全网和第 1 步的标签统一 —— 在标签还有 3 个真源时拆 orchestrator，
等于在一个移动的靶子上做等价变换。

---

## 五、必须遵守的三条纪律

1. **等价变换，不是重写。** 每一步都要能用「重构前后行为一致」证明。
   做不到就说明这一步切得太大，需要再拆。
2. **一次一步，每步全绿。** `ruff` → `mypy` → `pytest` 全过才进下一步。
   当前基线：**1208 passed / 13 skipped**。
3. **不夹带功能变更。** 重构提交里不出现行为变化。发现 bug 另开提交
   （09-11 那次 `e92f97a` 就是正确示范：修 bug 与报告分开）。

---

## 六、如果只做一件事

**做第 3 步（orchestrator 拆阶段）。**

理由是它与用户的真实痛点直接相关。前期分析已确认：这个系统的瓶颈
是**「在 2,105 样本上维护 2,000 行打分体系并不断加参数」**，
而 `scan_with_raw` 507 行 / 复杂度 105 正是这个体系的**入口与粘合层** ——
它让「删掉一个因子」「换一个评分口径」这类**本应简单的实验**变得昂贵。

重构它的收益不是「代码好看」，而是**降低策略实验的成本**：
当「试一个新因子」从「改 507 行里的 3 处并担心遗漏」变成
「在 pipeline/scoring.py 里加一行」，才有条件去解决真正的瓶颈。

如果只做第 1 步（1 小时），也能拿到确定的收益：消除一个静默不一致风险。

---

## 附：本次方案的量化依据

| 结论 | 验证方式 |
|---|---|
| 规模 / 文件数 | `wc -l` 全仓统计 |
| 复杂度热点 | `ast` 遍历计算长度与分支节点数 |
| 依赖扇入扇出 | `ast` 解析 import 语句统计 |
| config 导入符号数 | 解析各模块 `from scanner.config import (...)` |
| 死代码率 | 全仓符号引用计数 |
| 多真源 | `grep` 字面量 + 人工确认 |
| CI 覆盖缺口 | 对比 `ci.yml` 检查范围与实际文件清单 |
| 演进速度 | `git log --since="30 days ago"` 计数 |
| 测试基线 | `pytest tests/ -q` → 1208 passed / 13 skipped |
