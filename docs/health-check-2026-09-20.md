# rts-dfcf-max 健康体检报告

**日期**：2026-09-20
**范围**：全仓只读审计（代码质量 / 目录结构 / 依赖安全 / 潜在缺陷 / 性能 / 测试覆盖）
**基线**：master @ `f2f2805`（工作树干净）
**解释器**：系统 Python 3.12.6（项目依赖实际所在）

---

## 0. 总体结论

**这是一个工程成熟度显著高于同类个人项目的代码库。** 评分卡：

| 维度 | 实测 | 评级 |
|---|---|---|
| Lint（ruff 全仓） | **All checks passed** | 优秀 |
| 类型检查（mypy scanner/） | **0 error / 85 files** | 良好（含水分，见 §1.2） |
| 测试通过率 | 1653 passed / 14 skipped | 优秀 |
| 语句覆盖率 | **78.0%**（11840 语句，2606 未覆盖） | 良好 |
| CI | ruff 阻断 + mypy 非阻断 + pytest 带覆盖率 | 良好 |
| 密钥卫生 | `.env` 正确 ignore，**从未进入 git 历史** | 优秀 |
| 依赖锁定 | 全 `>=`，**无 lock 文件** | 薄弱 |
| 复杂度债 | **264 条被 ignore**（84 处 CC 超标） | 需关注 |

**主要风险集中在三处**：① 生产默认开启的代码路径缺测试；② 一次性 flaky 测试已复现；③ 依赖无上限约束。都不是「代码写错了」，而是**工程护栏的缺口**。

---

## 1. 代码质量

### 1.1 Lint 全绿，但绿灯的边界要说清

`ruff check .` 在当前 `select` 集合下**零告警**。但 `pyproject.toml` 的 `ignore` 里挂着 6 条规则，实测债务量：

| 规则 | 含义 | 实测条数 | 配置注释所记 |
|---|---|---|---|
| `C901` | 圈复杂度过高 | **84** | 58 |
| `PLR0912` | 分支过多 | **53** | 34 |
| `PLR0913` | 参数过多 | **49** | 35 |
| `PLR0915` | 语句过多 | **37** | 27 |
| `E501` | 行过长 | 22 | 9 |
| `SIM105` | 可用 `contextlib.suppress` | 19 | 15 |
| **合计** | | **264** | **163（已过时）** |

> **事实修正**：`pyproject.toml` 注释写的是「2026-08-29 实测存量」共 163 条，现在实际是 **264 条**——债务在配置里被冻结后继续增长了约 62%。注释里的数字已经不能作为排期依据。

### 1.2 mypy 的「0 error」含水分

`mypy scanner/` 报告 `Success: no issues found in 85 source files`。但配置是 `strict = false` + `disallow_untyped_defs = false`，**未标注类型的函数体默认不检查**。

- 函数总数 **739**，有返回标注 **669** → 标注率 **90.5%**（这个比例其实很高）
- 开 `--check-untyped-defs` 后：**只多出 1 个真实错误**（`scanner/data_source.py:278` 元组类型不兼容）

**结论：这个绿灯基本可信**，不是「假装通过」。剩余 1 个错误值得顺手修。

### 1.3 复杂度热点（项目自带普查器口径）

> 口径说明：复杂度 = 1 + 决策节点数。近似值，横向可比、不作绝对门槛。

函数总数 88 个超标（>60 行 或 CC≥25）。最重的几个：

| CC | 行数 | 位置 | 函数 |
|---|---|---|---|
| **154** | **474** | `stock_report.py:171` | `main` |
| 55 | 355 | `scanner/view/assemble.py:293` | `build_scan_view` |
| **54** | **396** | `scanner/orchestrator.py:146` | `scan_with_raw` |
| 49 | 196 | `scripts/v2_backtest.py:122` | `main` |
| 47 | 146 | `prevday_perf.py:198` | `_render` |
| 44 | 143 | `scanner/kline_fetch.py:33` | `fetch_all_klines` |

`stock_report.py:main` 的 **CC=154 / 474 行**是全仓最严重的单点——它既是 CLI 入口又是渲染器，几乎不可能被拆出可测单元。

### 1.4 异常处理：干净

- **裸 `except:` 数量 = 0**（这是很多项目的老毛病，这里没有）
- `except Exception` 共 **33 处**，集中在 `db/dal.py`(10)、`data_source.py`(6)
- `except ...: pass` 静默吞异常 **8 处**（已被 `S110` 规则纳入监管）

考虑到本项目大量使用 fail-open 降级（数据源多路兜底），这个分布是**合理的设计**而非疏漏。

### 1.5 遗留标记：几乎没有

全仓 `TODO/FIXME/HACK` 仅 **1 处**。取而代之的是密集的**中文日期化注释**（如「2026-08-29 修复：此前为…该模块不存在」），把「为什么这么改」写进了代码。这是好习惯——但见 §4.1 的风险。

---

## 2. 测试与覆盖率

### 2.1 通过率

```
1653 passed, 14 skipped in 65.99s
```

67 个测试文件 / 22936 行测试代码（**产线测试比 0.85:1**）。14 个 skip 是 `--run-smoke` 标记的真实库/外网集成测试，设计如此。

### 2.2 覆盖率：78.0%

**85 个模块，11840 语句，2606 行未覆盖。** 语句数 ≥50 的模块中，只有 5 个覆盖率 <50%——分布是健康的。

**零覆盖模块（仅 1 个）**：

| 模块 | 语句 | 真相 |
|---|---|---|
| `scanner/pool.py` | 50 | **不是死代码**——见下，这是最严重的发现 |

**绝对缺口 TOP 10**：

| 覆盖率 | 未覆盖 | 模块 | 说明 |
|---|---|---|---|
| 22.2% | **172** | `scanner/orchestrator.py` | 主数据通路 |
| 15.3% | 116 | `scanner/historical_rescan.py` | rescore 回填 |
| 33.5% | 121 | `scanner/matcher.py` | 低吸匹配 |
| 40.2% | 140 | `scanner/nextday_calib.py` | 校准漂移巡检 |
| 53.6% | 127 | `scanner/nextday_attribution.py` | 归因 |
| 60.1% | 137 | `scanner/db/dal.py` | 数据访问层 |
| 62.0% | 197 | `scanner/portfolio_backtest.py` | 回测 |
| 63.6% | 63 | `scanner/ths_api.py` | 备用数据源 |
| 65.9% | 106 | `scanner/db/queries.py` | 查询层 |
| 69.9% | 168 | `scanner/api.py` | 数据源适配 |

**对比亮点**：`scanner/pipeline/*` 六个模块**全部 100% 覆盖**——说明此前把 `scan_with_raw` 拆成纯函数的重构是成功的，拆出来的部分被测试锁住了。剩余 22.2% 是尚未拆出去的主体。

### 2.3 🔴 核心发现：生产在跑的代码，测试不覆盖

`scanner/pool.py` 显示 0.0% 覆盖。追根因：

```python
# scanner/config_core.py:122
ENABLE_POOL_PIPELINE = _env_flag("RTS_ENABLE_POOL", True)   # ← 默认 True

# scanner/orchestrator.py:231
if ENABLE_POOL_PIPELINE:          # ← 生产会进入这个分支
    from scanner.pool import build_pool
    pool_rows = build_pool(gem_stocks_filtered, klines, today, prev_ranks)
```

**默认值是 `True`，生产环境每轮都在执行 `build_pool` → `evaluate_pool` → `matcher` 这条 v2 池管道**，但单测从未打开这个分支。这就是 `pool.py` 0 覆盖 + `matcher.py` 33.5% + `orchestrator.py` 22.2% 的共同成因：**它们都在同一个未被测试的 `if` 里面。**

这不是「覆盖率数字不好看」，而是**主链路上有一段生产代码没有任何回归保护**。

---

## 3. 潜在缺陷（已复现）

### 3.1 🔴 测试套件不稳定：2 次运行出现 1 次失败

同一个套件跑了两次，结果不同：

| 运行 | 结果 |
|---|---|
| 第 1 次 | `1653 passed, 14 skipped` ✅ |
| 第 2 次（带 --cov） | `1 failed, 1652 passed, 14 skipped` ❌ |

**失败用例**：`tests/test_single_instance.py::test_real_restart_replaces_old_holder`
**失败位置**：`test_single_instance.py:83`（`_wait_for` 超时判定，子进程未就绪）

**归因过程**（避免误判）：

1. 单测隔离下连跑 8 次 → **8/8 通过**，说明不是它自身的逻辑 flaky
2. 跑整个 `test_single_instance.py` 文件（36 个测试）→ **全通过**
3. 验伪了我的第一直觉：`psutil.create_time()` 精度（亚毫秒）和单调性都正常，**辈分判据本身没问题**

**真因**：该用例会派生子进程并调用 `stop_existing_scanners()`，后者用 `psutil.process_iter()` **枚举全机进程**，再用 `_runs_script(cmdline, cwd, target)` 匹配。在全量套件（67 文件、并发压力大）运行时，进程枚举与就绪等待的时序会被挤压，30 秒就绪窗口偶发不够用。

**为什么这个 bug 值得重视**：它所在的模块正是**单实例锁**——保证「一天只跑一个扫描器」的机制。一个在压力下会误判/漏判的锁，后果是双实例并发写 `scanner.db`。

### 3.2 「看起来在记录、其实没信息」的字段（已在库中确证）

| 字段 | 实测 | 问题 |
|---|---|---|
| `pool_log.v1_passed` | **868 行全部 = 0** | 恒 0，无信息量 |
| `recommendations.excluded=1` 且 `excluded_reason=''` | **129 / 195 行**（66%） | 库内**不可归因**，无法判断被哪道门杀了 |
| `daily_kline` 连续性 | 相邻行对 55466，真相邻仅 **92.5%**（**7.5% 跨日缺口**） | 取 `rows[i+1]` 当"次日"会把 2~5 天收益算成 1 天 |
| 同一 (symbol,date) 多行 | **705 组** | v1/v2 双跑并存，审计必须带 `category` 否则自相矛盾 |

### 3.3 空表与悬空依赖

- **空表 2 张**：`pool_audit`、`sector_cache`（均 0 行）——`pool_audit` 有索引 `idx_pool_audit_date` 却从未写入数据
- **悬空 editable 包**：系统 Python 里残留
  ```
  __editable__.stock_strategy-0.1.0.pth
  → D:\everything\rts-temp\stock-strategy\stock_strategy   ← 该目录已不存在
  ```
  每次解释器启动花 ~5.2ms 装载一个指向已删除项目的映射。量小，但属环境腐化；且它先于 `sitecustomize` 执行，长期会累积成「诡异 import 行为」。

### 3.4 类型错误 1 处

`scanner/data_source.py:278` — `tuple[float|int, str, str]` 赋给声明为 `tuple[None, None, str]` 的变量。仅在 `--check-untyped-defs` 下暴露。低危但应修。

---

## 4. 依赖与供应链安全

### 4.1 密钥卫生：良好

| 检查项 | 结果 |
|---|---|
| `.env` 在 `.gitignore` | ✅ 第 35 行显式忽略 |
| `.env` 是否进过 git 历史 | ✅ **从未提交**（`git log --all -- .env` 为空） |
| 硬编码凭据 | 仅 1 处：`scanner/net.py:35` 东财公开固定 token，**已带 `# noqa: S105 - 非密钥` 说明** |

`.env` 实际内容：`HITHINK_FINANCE_API_KEY`（41 字符）、`RTS_FEISHU_WEBHOOK`（81 字符）、`RTS_FINAL_PICK`。三者均**明文落盘**——这是本地工具的正常做法，但需注意 `.workbuddy/` 虽被 ignore，其中 `MEMORY.md` 与日志含本地绝对路径。

### 4.2 🔴 无依赖锁定，且全为下界约束

```txt
requests>=2.28      → 实装 2.34.2
wcwidth>=0.2        → 实装 0.2.13
psutil>=7.0         → 实装 7.2.2
akshare>=1.12       → 实装 1.18.64   ← 跨了 6 个 minor
```

- **无 `poetry.lock` / `uv.lock` / `Pipfile.lock` / `pip-compile` 输出**
- 全部 `>=`，**无一条上界**

**具体风险**：`akshare` 从 1.12 到 1.18.64 跨了 6 个 minor 版本——它的接口以频繁变更著称，而本项目把 akshare 用作**涨停池/指数/新浪 qfq 的兜底数据源**。CI 每次全新 `pip install` 都会拉到最新版，一旦上游改了返回结构，**CI 可能突然变红，或者更糟——静默返回不同数据**。

### 4.3 可选依赖降级路径

`pywencai` 被注释掉，代码里有 `未安装自动降级` 逻辑。这类「可选依赖」是双刃剑：降级路径本身**很少被执行**，容易长期腐化。建议至少有一条测试强制覆盖「依赖缺失」分支。

---

## 5. 性能瓶颈

### 5.1 启动耗时分解（实测）

| 环节 | 耗时 | 说明 |
|---|---|---|
| Python 解释器冷启动 | **~0.19s** | 含 sitecustomize + 悬空 finder |
| `import scanner.orchestrator` | **0.309s** | |
| `sitecustomize`（沙箱注入） | 20.0ms 自身 / 88.5ms 累计 | 环境相关，非项目代码 |
| 悬空 editable finder | **5.2ms** | 可移除，见 §3.3 |
| `requests` + `urllib3` | 2.7ms 自身 / **392ms 累计** | 连带 urllib3 全家 |

`unified_scanner` 整体 import 累计 **392ms**，其中 `requests`/`urllib3` 占大头。

### 5.2 首轮冷启动是主要瓶颈

`fetch_fund_flow_rank()` **冷启动 14.2s**（53 页请求），TTL 300s 且**重启即清**。首轮总耗时 ~20s，之后稳态 <1s。这是用户可感知的最大延迟，也解释了为什么此前尝试「提并发」无效（6→16 线程仅省 ~3s）——瓶颈在请求序列本身而非并发度。

### 5.3 数据库体积与索引

- **66.0 MB**，26 张表，**39 个索引**（17 个显式 + PK 自动索引）
- WAL 模式，`synchronous=2`，`busy_timeout=5000ms`，`cache_size=-2000`
- **`foreign_keys = 0`** — 全仓无 `FOREIGN KEY` 声明，一致性靠应用层保证
- 索引覆盖良好：仅 2 张表无索引，且都是小表（`schema_version` 7 行、`sqlite_sequence`）

### 5.4 🟠 无数据保留策略，库会持续增长

| 表 | 现行数 | 每日新增 | 年化估算 |
|---|---|---|---|
| `market_extra_cache` | **84856** | **~5307/日** | **~130 万行/年** |
| `daily_kline` | 56492 | ~60/日 | 增长较慢 |
| `appearances` | 7513 | ~92/日 | ~2.3 万/年 |
| `leaderboard_log` | 6529 | ~229/日 | ~5.6 万/年 |

全仓搜索 `DELETE FROM` / `VACUUM` / `PRAGMA optimize` / `ANALYZE`，**只有针对 `watch_pool`、`hot_watch_hits`、`ranking_snapshot`、`triple_barrier_labels` 的定向剪枝**。占 74% 体量的 `market_extra_cache` **没有任何保留策略**——它按 `(symbol, data_type, date)` 每日新增全市场快照。

按 5307 行/日推算，一年后该表约 130 万行、库体积约 **1.5 GB**。目前不会立刻出问题（有 `idx_mec_sym_type` 索引），但**这是一条确定的增长曲线**。

### 5.5 未执行的维护命令

`PRAGMA optimize` / `ANALYZE` 从未在代码中出现 → SQLite 查询计划器**没有统计信息**，长期可能选错索引。

---

## 6. 目录结构与工程化

### 6.1 现状

```
根目录     14 个 .py /  3756 行   ← 散装入口脚本
scanner/   85 个 .py / 27031 行   ← 主包（含 db/ pipeline/ view/ 子包）
scripts/   22 个 .py /  5763 行   ← 分析/回放脚本
tests/     70 个 .py / 22936 行
合计      191 个文件 / 59500 行
```

`packages = ["scanner", "scanner.db", "scanner.pipeline"]` —— 注意 **`scanner.view` 漏了**。当前以源码直跑为主影响不大，但只要有人 `pip install .`，`scanner.view` 就装不进去，直接 ImportError。

### 6.2 根目录散装脚本的耦合问题

`tests/` 里有 5 处直接 `import unified_scanner` / `from stock_report import find_stock`——**把根目录脚本当模块导入**。这意味着：

- 根目录脚本无法安全地移到 `scripts/` 或 `cli/`（会打断测试导入）
- 它们没有 `__init__.py`，靠 `sys.path` 巧合工作
- 14 个根脚本中 **9 个无对应测试**（`backfill_*`、`diag_*`、`query_*`、`leaderboard_obs`、`unified_scanner`）

### 6.3 文档时效性

15 份 docs 全部时间戳为 09-18，内容新鲜。但 `AGENTS.md`（21KB）与 `docs/CORE-FLOW.md`（44KB）体量已接近「文档本身成为维护负担」的临界点。

另：`docs/audit-2026-09-13.md` 是上一轮审计报告（62KB），本报告的 §1.1 已指出其中的复杂度数字（163 条）**已过时**。

### 6.4 CI 配置：基本称职

`.github/workflows/ci.yml`：
- ✅ `ruff check .` 全仓、**阻断**
- ⚠️ `mypy scanner/` **`continue-on-error: true`**（非阻断）——考虑到只有 1 个真实错误，建议转为阻断
- ✅ `pytest` 带 `--cov=scanner --cov-report=term-missing`
- ❌ **覆盖率无门禁**（无 `--cov-fail-under`）——注释说「先积累基线，再决定阈值」。现在基线有了：**78%**，建议设 `--cov-fail-under=75` 防水位下滑

---

## 7. 风险清单与改进建议

### 🔴 P0（建议本轮处理）

| # | 问题 | 证据 | 建议动作 |
|---|---|---|---|
| **P0-1** | **生产默认开启的池管道无测试保护** | `ENABLE_POOL_PIPELINE=True` 是默认值，`pool.py` 覆盖率 **0.0%**、`matcher.py` 33.5%、`orchestrator.py` 22.2% 均因此 | 补一条打开 `RTS_ENABLE_POOL=1` 的端到端测试（或让 golden_scan 走这条分支），把 `build_pool → evaluate_pool → label_all_candidates` 链锁住 |
| **P0-2** | **单实例锁测试在全量套件下 flaky** | 同套件两次运行：一次全绿、一次 `test_real_restart_replaces_old_holder` 失败（隔离下单跑 8/8 通过） | 定位「就绪窗口 30s 在并发下不够」还是「`process_iter` 枚举到兄弟测试进程」；锁是防止双实例写库的最后防线，必须稳 |
| **P0-3** | **依赖无锁定，akshare 跨 6 个 minor** | 无 lock 文件；`akshare>=1.12` 实装 1.18.64；CI 每次拉最新 | 生成 `requirements.lock`（`pip freeze` 或 `pip-compile`），CI 按 lock 装；至少给 akshare 加上界 |

### 🟠 P1（近期排期）

| # | 问题 | 证据 | 建议动作 |
|---|---|---|---|
| **P1-1** | 复杂度债从 163 → **264** 条，配置注释已过时 | 实测 264 条（C901 84 处） | 更新 `pyproject.toml` 注释数字；制定分档解冻计划，优先拆 `stock_report.py:main`（CC=154/474 行） |
| **P1-2** | `market_extra_cache` 无保留策略 | 84856 行、**+5307/日**、年化 ~130 万行 | 加保留窗口（如 90 天），复用现有 `prune_*` 模式；补 `PRAGMA optimize` 到轮次收尾 |
| **P1-3** | 覆盖率无 CI 门禁 | 基线已测得 **78.0%** | `--cov-fail-under=75` 防止水位下滑；mypy 转阻断（仅 1 个待修错误） |
| **P1-4** | `daily_kline` 7.5% 跨日缺口未被强制校验 | 55466 相邻行对中 4147 对非真相邻 | 确认所有「次日收益」计算都带 `ai[ds[i+1]] == ai[ds[i]]+1` 断言；`fwd_3d/fwd_5d` 结论需重新标注可信度 |
| **P1-5** | `pyproject.toml` 漏声明 `scanner.view` | `packages = ["scanner","scanner.db","scanner.pipeline"]` | 补上 `scanner.view`（`pip install .` 会 ImportError） |

### 🟡 P2（顺手清理）

| # | 问题 | 建议动作 |
|---|---|---|
| P2-1 | 悬空 editable 包指向已删除的 `rts-temp/stock-strategy` | `pip uninstall stock_strategy` 清掉三个残留文件 |
| P2-2 | `data_source.py:278` 类型不兼容 | 修正标注，让 `--check-untyped-defs` 也全绿 |
| P2-3 | `pool_log.v1_passed` 恒 0（868 行） | 要么接上真实写入，要么删列——现在它只制造「有记录」的错觉 |
| P2-4 | 129/195 行 `excluded=1` 但 `reason` 为空 | 在 `enhancer` 之外也写 reason，否则剔除不可归因 |
| P2-5 | 空表 `pool_audit` / `sector_cache` | 确认是否废弃；废弃则删表删索引 |
| P2-6 | 9 个根脚本无测试 | 至少为 `backfill_kline.py` / `repair_kline.py` 补关键路径测试 |

---

## 7.5 执行记录与自我修正（2026-09-21 补）

### 已执行的修复

| 项 | 状态 | 落点 |
|---|---|---|
| P0-2 flaky | 已修（5 连跑全绿） | `tests/test_single_instance.py`：进程组隔离 + 90s 就绪窗口 + 失败时输出 `poll/ready/fail/stderr` |
| P0-3 依赖锁定 | 已修 | `requirements.txt` 加 `akshare<2.0`；新建 `requirements.lock`；CI 改按 lock 装 |
| P1-3 覆盖率门禁 | 已修 | CI 加 `--cov-fail-under=75`；mypy 转**阻断**（去掉 `continue-on-error`） |
| P1-5 包声明 | 已修 | `packages` 补 `scanner.view` |
| P1-1 注释过时 | 已修 | `pyproject.toml` 债务数字改为 2026-09-20 实测值 |
| P2-1 悬空包 | 已修 | `pip uninstall stock-strategy` 完成，site-packages / Scripts 均无残留 |
| P2-2 类型错误 | 已修 | `data_source.py:178` 加显式标注，mypy 双模式 0 error |

### ⚠ 本报告需要修正的数字（审计过程中自我发现）

1. **§1.1「ruff 全仓零告警」不成立（已修）**。写成这样时，仓库根目录存在两个**本次审计自己生成的一次性诊断脚本**（`health_check.py` 180 行 / `security_scan.py` 202 行），它们带来 **55 条真实违规**（`SIM115` 16 / `E722` 13 / `S110` 12 等），使 `ruff check .` **exit 1**——也就是 CI 第一道门本来是红的。
   **根因是审计行为污染了审计对象**，两个脚本已于 2026-09-21 删除，`ruff check .` 恢复 `exit 0`。教训：**采集脚本不该落在被审计的仓库里**，应写进 `$TEMP`。
2. **§1.1「264 条被 ignore」经复核成立**，为最终权威值（`C901` 84 / `PLR0912` 53 / `PLR0913` 49 / `PLR0915` 37 / `E501` 22 / `SIM105` 19）。
   ⚠ 中途我曾算出 267——那是**删除脚本前的污染值**（脚本自身也贡献了 `E501`/`SIM105`）。**债务数字必须在干净工作树上测**，否则会被临时文件污染。
3. **§6.4「mypy 非阻断」已过时**：现为阻断。
4. **`.coverage` / `htmlcov/` 未被 `.gitignore` 忽略**（新发现）：CI 加了 `--cov` 后必然在仓库根落 `.coverage`，本地跑覆盖率亦同，易被 `git add .` 误入库。已补入 `.gitignore`。

### ✅ 本轮验证门（全部实跑）

```
ruff check .                                  exit=0    All checks passed
mypy scanner/ --ignore-missing-imports        85 files  0 error
mypy scanner/ --check-untyped-defs            85 files  0 error
scripts/_verify_view_split.py                 exit=0    等价性保持
pytest tests/                                  1654 passed, 13 skipped
pytest tests/ 连跑 5 次（flaky 验证）           5/5 全绿（57~60s/次）
```

> 注：`--cov-fail-under=75` 本地无法复现，因 `pytest-cov` 只装在临时隔离 venv（`--system-site-packages`，上轮用完即弃）。
> 覆盖率 **78.0%** 为上轮该隔离环境的实测值，CI（ubuntu + 全新装）应一致或更高。

### 未修（需你定夺）

`P0-1`（池管道无测试保护，`pool.py` 覆盖 0%）、`P1-2`（`market_extra_cache` 无保留策略）、`P1-4`（kline 跨日缺口强制校验）、`P2-3~P2-6`。
其中 **P0-1 是唯一与「决策正确性」直接相关的未修项**——`ENABLE_POOL_PIPELINE` 默认开着却零覆盖，建议优先。

---

## 8. 值得肯定的地方

审计不该只列问题。这个项目有几处做得比多数商业代码库更好：

1. **`scripts/project_audit.py`** —— 自带量化普查器，零依赖、纯只读、口径固定并写进 docstring。本报告 §1.3 的数字直接由它产出，可复现。这是**极高水准的工程自觉**。
2. **`scripts/golden_scan.py`** —— 主数据通路的黄金样本对比工具，为「拆 `scan_with_raw`」提供了可证明等价性的标尺。§2.2 显示 `pipeline/*` 全部 100% 覆盖，证明这条路线走对了。
3. **注释即规格** —— 大量「为什么这么改」的中文日期化注释（含被证伪的旧结论），把踩坑史固化进了代码。
4. **`pipeline/` 拆分已见成效** —— 6 个纯函数模块 100% 覆盖，对比 `orchestrator.py` 的 22.2%，重构收益被量化证实。
5. **密钥零泄漏** —— `.env` 从未进过 git 历史，全仓仅 1 处带说明的公开 token。
6. **裸 `except:` 为 0** —— 在大量 fail-open 降级需求下仍守住，不靠吞异常掩盖问题。

---

## 9. 建议执行顺序

```bash
# ── 第一步：修 P0-3 依赖锁定（低风险、可立即做）
pip freeze > requirements.lock          # 或引入 pip-compile
# 并给 akshare 加上界，如 akshare>=1.12,<2.0

# ── 第二步：修 P0-2 flaky（影响库安全）
python -m pytest tests/test_single_instance.py -q --count=20   # 需 pytest-repeat，或循环跑
# 定位 create_time 辈分判据在进程枚举竞争下的行为

# ── 第三步：补 P0-1 池管道测试（提升最大）
RTS_ENABLE_POOL=1 python -m pytest tests/ -q
# 观察 pool.py / matcher.py 覆盖率是否上升；若否，说明测试确实没走该分支

# ── 第四步：P1 批量（配置与护栏）
# 更新 pyproject.toml 复杂度注释数字 + 补 scanner.view
python -m pip uninstall stock_strategy   # P2-1
```

**验证纪律**（沿用项目既有约定）：
改 `scan_with_raw` / `pipeline/` → 必跑 `scripts/golden_scan.py` 四日期（09-08~09-11）全 exit 0；
改权重/常数 → 必跑 `python -m scanner.rule_validate`；改 `nextday_prob` 常数 → 必跑 `nextday_calib --write`。
