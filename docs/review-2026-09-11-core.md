# 核心代码流程审查报告

日期：2026-09-11
审查范围：orchestrator 核心管线 / 评分排序层 / 数据层事务 / 主循环健壮性
审查方式：逐文件精读 + 对每个疑点编写可执行验证（不采信静态判断）

---

## 零、结论

工程质量整体高于同类项目（异常纪律、单一真源、可观测性都做得扎实）。
本次找到 **5 个真实缺陷**，其中 1 个会造成**永久性数据损坏**且长跑下无法自愈；
已修复 4 个，剩余 1 个经实测影响面极小（0 例判定翻转），降级为技术债。

需要特别说明：本次审查推翻了 4 个「看起来很像 bug」的误报（详见 §6），
它们经实测确认是**正确实现**。这也是本次审查坚持逐条验证的原因 ——
同样地，缺陷 3 原本被评为中危，实测后修正为低危。

| # | 缺陷 | 位置 | 影响 | 类型 | 状态 |
|---|---|---|---|---|---|
| 1 | schema v4 PK 迁移非原子 | `db/schema.py` | 历史数据永久丢失 | 高危 | ✅ 已修 |
| 2 | `PRAGMA index_list` 列索引误用 | `db/schema.py` | 迁移误判/反复重建 | 中危 | ✅ 已修 |
| 3 | 单利/复利口径混用 | `ranking.py:121-122` | 🎯 漏标（实测 0 例） | 低危 | ⏳ 待办 |
| 4 | 主循环节拍漂移 | `unified_scanner.py` | 刷新周期逐步拉长 | 低危 | ✅ 已修 |
| 5 | 硬过滤判定重复计算 | `orchestrator.py` | 无谓开销 | 低危 | ✅ 已修 |

> **修复进度（2026-09-11）**：缺陷 1/2/4/5 已修复并验证（新增 `tests/test_schema_migration.py`
> 4 条回归；全量测试 **1208 passed / 13 skipped**，即在 1204 基线上净增 4 条）。
> 缺陷 3 属**口径变更**，会改动 🎯 判定结果，留待单独处理并观察标记数量变化。

---

## 一、高危：schema v4 PK 迁移非原子，中断即永久丢数据

**位置**：`scanner/db/schema.py`（原 `:322-340`，现抽为 `_migrate_market_extra_cache_pk`）
**状态**：✅ 已修复（2026-09-11）

```python
if old_pk:
    conn.execute("ALTER TABLE market_extra_cache RENAME TO market_extra_cache_old")
    conn.execute("CREATE TABLE market_extra_cache (...)")          # 新表
    conn.execute("INSERT INTO market_extra_cache ... SELECT ... FROM market_extra_cache_old")
    conn.execute("DROP TABLE market_extra_cache_old")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mec_sym_type ...")
except sqlite3.Error:
    pass  # ← 迁移失败不阻塞初始化，最坏情况是历史数据仍不可查
```

### 为什么是缺陷

四个 DDL 语句**没有事务包裹**。Python `sqlite3` 在默认 `isolation_level=""` 下，
**DDL 会自开事务并立即提交**（实测：`conn.execute("CREATE TABLE ...")` 后
`conn.in_transaction` 为 `False`）—— 所以这四步各自独立落盘。

**中断场景推演**：

| 中断点 | 库内状态 | 下次启动行为 | 结果 |
|---|---|---|---|
| `RENAME` 后 | 只有 `market_extra_cache_old` | 表不存在 → 建新空表 | 旧数据成孤儿 |
| `CREATE` 后 / `INSERT` 前 | 空新表 + `_old` 有数据 | PK 已含 date → **跳过迁移** | 旧数据永久不可达 |
| `INSERT` 中途 | 部分新表 + `_old` 全量 | PK 已含 date → 跳过 | 部分丢失，无告警 |

关键在第二行：**迁移的探测条件是「PK 是否含 date」，而中断后新表已经建好、PK 已经正确** ——
所以重试逻辑永远不会触发，`market_extra_cache_old` 会作为孤儿表永久留在库里。

**已复现**（构造「新表已建 / 旧表未 DROP」现场后跑真实 `init_db`）：

```
孤儿表是否仍在: True        孤儿表行数: (1,)        新表行数: (1,)
新表 PK: ['symbol', 'data_type', 'date']     ← 已"正确"，故迁移被静默跳过
```

**当前生产库状态**：21 张表**无** `*_old` 孤儿，`market_extra_cache` PK 为
`['symbol','data_type','date']`（已正确），58321 行完好 —— 说明该迁移当时
完整执行成功。但这是「运气好没被打断」，不是「设计上安全」。

**触发条件**：迁移期间进程被杀 / 磁盘满 / 断电 / 后续语句抛错。这是一个
「盘中每 60s 跑一轮、每轮都跑 `init_db()`」的长跑进程 —— 迁移窗口虽小但确实存在。

**影响**：`market_extra_cache` 现有 **58321 行**（含历史资金流/涨停池）。
丢失后 `today_report --date` 历史回放的资金流永久为空，且**无任何告警**。

**修复实施**：

```python
def _migrate_market_extra_cache_pk(conn: sqlite3.Connection) -> None:
    pk_cols = _detect_mec_pk(conn)                      # 见缺陷 2：改判 origin=='pk'
    if not pk_cols or pk_cols == ["symbol", "data_type", "date"]:
        return                                          # 表不存在或已迁移
    if pk_cols != ["symbol", "data_type"]:
        print(f"  [schema] PK 非预期 {pk_cols}，跳过 v4 迁移（需人工确认）")
        return                                          # 未知 PK 不自作主张
    try:
        conn.executescript("""
            BEGIN IMMEDIATE;
            ALTER TABLE market_extra_cache RENAME TO market_extra_cache_old;
            CREATE TABLE market_extra_cache (... PRIMARY KEY(symbol, data_type, date));
            INSERT INTO market_extra_cache (...) SELECT ... FROM market_extra_cache_old;
            DROP TABLE market_extra_cache_old;
            CREATE INDEX IF NOT EXISTS idx_mec_sym_type ON market_extra_cache(symbol, data_type);
            COMMIT;
        """)
    except sqlite3.Error as exc:
        conn.rollback()                                 # script 内 BEGIN 已开事务，必须显式回滚
        print(f"  [schema] market_extra_cache v4 迁移失败（已回滚，未改动原表）：{exc}")
        raise                                           # 不再静默 pass
```

**关键实测结论**（决定了实现方式）：

1. `executescript("BEGIN; ...; COMMIT;")` 能让**四条 DDL 处于同一事务** ——
   实测中途 `CREATE` 撞名失败后，`ROLLBACK` 使 `ALTER ... RENAME` **完全撤销**。
2. 失败路径**必须显式 ROLLBACK**：实测若只 `except: pass` 不回滚，
   `conn.in_transaction` 仍为 `True`，连接持续持有写事务。
3. `sqlite3.Error` ∈ `EXTERNAL_FAILURES`，主循环/脚本调用方已有兜底，
   `raise` 不会打崩扫描进程。

**验证结果**（`tests/test_schema_migration.py`，4 条）：

| 场景 | 期望 | 实测 |
|---|---|---|
| 旧 PK → 迁移 | PK 变 `(symbol,date,data_type)`、行保留、无临时表 | ✅ |
| 再跑一次（幂等） | 不重建、不报错 | ✅ |
| 中途失败 | 抛 `sqlite3.Error` + **原表 PK 原封不动** | ✅ |
| 未知 PK | 打印告警、跳过、不抛 | ✅ |

> 顺带：`INSERT ... SELECT` 的列顺序与建表列顺序不一致（建表是 symbol,date,data_type,
> 插入写的是 symbol,date,data_type）—— 实际两边都按**列名**对应，非位置对应，故正确。

---

## 二、中危：`PRAGMA index_list` 取错列，迁移探测逻辑不健壮

**位置**：`scanner/db/schema.py`（原 `:313`，现 `_detect_mec_pk`）
**状态**：✅ 已修复（2026-09-11）

```python
cur2 = conn.execute("PRAGMA index_list(market_extra_cache)")
pk_indexes = [r[1] for r in cur2.fetchall() if r[2]]  # ← 注释写"pk=1 表示主键"
```

### 为什么是缺陷

`PRAGMA index_list` 的列序是 `(seq, name, unique, origin, partial)`。
**`r[2]` 是 `unique` 标志，不是 `pk` 标志**。判主键应该看 `r[3] == 'pk'`。

我实测确认：

```
PRAGMA index_list 输出：
  (0, 'sqlite_autoindex_market_extra_cache_1', 1, 'pk', 0)
                                               ↑  ↑
                                          r[2]=unique  r[3]=origin='pk'

加一个 UNIQUE 索引后：
  (0, 'idx_test', 1, 'c', 0)          ← r[2] 也是 1 → 被误判为主键
  (1, 'sqlite_autoindex_..._1', 1, 'pk', 0)
```

**后果**：当前恰好正确（因为表上没有别的唯一索引）。但只要未来给
`market_extra_cache` 加**任何一个 UNIQUE 索引**，该索引就会进入 `pk_indexes`
被当主键检查，导致 `old_pk` 判定异常 —— 要么反复触发全表重建
（每轮 `init_db` 都重建，盘中短暂锁表），要么漏迁移。

这是一个**定时炸弹**：现在不炸，加索引时炸，而且症状（反复重建）很难追到这一行。

**修复实施**（抽为 `_detect_mec_pk`，供迁移函数复用）：

```python
def _detect_mec_pk(conn: sqlite3.Connection) -> list[str]:
    """返回 market_extra_cache 的主键列名（按主键内序）。表不存在返回 []。"""
    pk_indexes = [
        r[1] for r in conn.execute("PRAGMA index_list(market_extra_cache)").fetchall() if len(r) > 3 and r[3] == "pk"
    ]
    for idx_name in pk_indexes:
        cur = conn.execute(f"PRAGMA index_info('{idx_name}')")  # noqa: S608
        pk_cols = [r[2] for r in cur.fetchall()]
        if pk_cols:
            return pk_cols
    return []
```

顺带修掉一个**原代码未暴露的健壮性问题**：`pk_indexes` 循环原先只在匹配到旧 PK 时
`break`，若表上存在多个被误纳的索引，`pk_cols` 判定会依赖遍历顺序；现改为
「取第一个真正的主键索引并提前返回」，语义确定。

---

## 三、中危：单利/复利口径混用，🔄 与 self-docstring 自相矛盾

**位置**：`scanner/ranking.py:117-123`

```python
window = within[:6]
if len(window) >= 6:
    base = window[5][1]
    return (window[0][1] - base) / base * 100.0   # 复利（相对首日）
if window:
    return sum(v[2] for v in window[:5])          # ← 单利（涨幅直接相加）
```

### 为什么是缺陷

同一函数在「数据够」时用**复利**、数据不足时退化为**单利**。
而 `analysis.py:156-163` 明确写着：

> 「不使用单利近似，避免两种算法产出不同数值导致阈值判定口径错位」

`ranking` 侧违反了这个自己确立的原则。

我实测两种口径的差异（阈值 `NEXTDAY_ACCUM_MIN = 6.0`）：

| 5 日涨幅序列 | 复利 | 单利 | 差 |
|---|---:|---:|---:|
| [2,2,2,2,2] | 10.41 | 10.00 | −0.41 |
| [1.5,1.5,1.5,1.5,1.5] | 7.73 | 7.50 | −0.23 |
| [3,1,1,1,0] | 6.12 | 6.00 | −0.12 |
| [8,8,8,8,8] | 46.93 | 40.00 | **−6.93** |

### 影响面实测（2026-09-11 新增，改变了处理建议）

我原本判断「新股恰是连续大涨的高发群体 → 系统性低估」。**实测数据支持前半句、
但推翻了后半句的严重性**：

| 指标 | 实测值 |
|---|---|
| 推荐总行数 | 5006 |
| K 线 < 6 根（走单利分支） | **13 条（0.26%）** |
| 其中 0 根（返回 `None`，不经单利） | 3 条 |
| **实际受口径影响** | **10 条（0.20%）** |
| K 线 ≥ 6 根（走复利，不受影响） | 4993 条（99.74%） |

关键发现：**这 10 行的口径差异全部不影响 🎯 判定**。

```
2026-09-02 SZ301688 bars=1  单利=104.41   ← 上市首日
2026-09-02 SZ301697 bars=2  单利=186.05
2026-09-11 SZ301689 bars=2  单利=222.50
...
10 行全部 单利 >= 6.0（阈值）且远超 → 换复利口径同样跨阈值，判定不变
```

原因：能落到这个分支的必然是次新股（K 线根数 = 上市天数），而次新股的「5 日累计」
被上市首日涨幅（100%–240%）主导，两种口径算出来都是三位数，**离阈值 6.0 有两个
数量级的安全边际**。10 行无一处在阈值附近。

**阈值附近的跳变确实存在**（我构造了边界场景复现），但需要「日涨幅恒定在 2% 左右」
才可能触发：

```
每日 +2% 的次新股，第 N 根 K 线时：
  第3根: accum= 6.00 [单利] 🎯     第6根: accum=10.41 [复利] 🎯
  第5根: accum=10.00 [单利] 🎯
```

要真正误判，需要 5 日累计恰好落在 `[6.0, 复利值)` 这个窄带内 —— 对次新股而言
意味着「上市后连续 5 天匀速微涨」，实盘中极为罕见。**当前 5006 行数据里 0 例。**

### 修正后的严重性判断

原评级「中危」**偏高**。应下调为**低危（一致性缺陷）**：

- **真实危害**：不是「大量漏标 🎯」，而是「同一函数内部存在两套口径」这一
  **可维护性风险** —— 任何未来调整 `NEXTDAY_ACCUM_MIN` 阈值、
  或新增数据源补足 K 线时，这个静默分支会成为不可预期的行为差异来源。
  `analysis.py` 已明确禁止单利近似，`ranking.py` 违反同一原则，属**纪律不一致**。
- **当前无实际损害**：5006 行中 0 例判定翻转。

**修复方向（维持原建议，但优先级下调）**：数据不足时返回 `None`（口径不可用），
而不是用不同口径凑一个值。调用侧已有 `None` 处理路径 —— 但需注意
`_nextday_entry_accum:168-172` 在回放返回 `None` 时会**兜底到 DB 落库的
`accumulated_pct`**，而该字段对 `new_face`/`momentum` 是不含今日的历史口径。
所以这一步的净效果是「从单利口径 → 历史口径」，**未必更准**，
需按第七步的注意事项评估。

---

## 四、低危：主循环节拍漂移

**位置**：`unified_scanner.py`（原 `:468`）
**状态**：✅ 已修复（2026-09-11）

原代码：

```python
for remaining in range(interval, 0, -5):
    if not is_trading_time():
        break
    print(f"\r  ⏳ 下次刷新还有 {remaining}s ...", end="", flush=True)
    time.sleep(5)
```

倒计时从固定的 `interval` 起算，**不扣除本轮扫描本身的耗时**。
实际周期 = `interval + 扫描耗时`。一轮扫描含 K 线/分时/概念三阶段，
耗时随候选数波动（数十秒量级）。

**影响**：刷新周期逐步拉长，盘中数据新鲜度下降。非致命，但与
`REFRESH_INTERVAL=60` 的设计意图不符。

**修复实施**：

```python
# clear_screen() 之后记录本轮起点
_round_started = time.monotonic()

# ... 本轮扫描 ...

# 倒计时以「本轮起点 + interval」为终点
_elapsed = time.monotonic() - _round_started
_wait_total = max(0.0, interval - _elapsed)
_remaining = _wait_total
while _remaining > 0:
    if not is_trading_time():
        break
    print(f"\r  ⏳ 下次刷新还有 {_remaining:.0f}s ...", end="", flush=True)
    _step = min(5.0, _remaining)
    time.sleep(_step)
    _remaining -= _step
print()
```

**实测**：周期稳定在 `interval`（60s），不再随扫描耗时累加；
扫描超时（`_elapsed > interval`）时 `max(0.0, ...)` 保证不出现负等待。

---

## 五、低危：硬过滤判定重复计算

**位置**：`scanner/orchestrator.py`（原 `:540` 与 `:545`）
**状态**：✅ 已修复（2026-09-11）

原代码：

```python
excluded_by_risk = [c for c in all_candidates if candidate_excluded_by_risk(c)]  # 第一次
...
all_candidates = [c for c in all_candidates if not candidate_excluded_by_risk(c)]  # 第二次
```

对同一批候选调用了两次 `candidate_excluded_by_risk`。该函数是集合交集
（`set(risk_flags) & RISK_FLAGS_HARD_FILTER`），开销很小，属**代码整洁问题**而非性能问题。

**修复实施**：第一次判定的结果直接复用（按 `id()` 集合排除，避免依赖 `__eq__`）：

```python
excluded_by_risk = [c for c in all_candidates if candidate_excluded_by_risk(c)]
if excluded_by_risk:
    _names = "、".join(f"{c.stock.name}({c.stock.symbol})" for c in excluded_by_risk[:8])
    _more = f" 等{len(excluded_by_risk)}只" if len(excluded_by_risk) > 8 else ""
    print(f"  [风险过滤] {len(excluded_by_risk)} 只命中硬排除标签，已移出推荐：{_names}{_more}")
    _excluded_ids = {id(c) for c in excluded_by_risk}
    all_candidates = [c for c in all_candidates if id(c) not in _excluded_ids]
```

**等价性验证**：对 `n = 0, 1, 5, 10, 20` 逐一比对新旧两种写法产出的
「保留集合」与「打印文本」，完全一致。

顺带修 `_ensure_conn`（`unified_scanner.py:116-133`）：原先只做 `SELECT 1`，
不检查事务状态。若上轮异常残留未提交事务，`SELECT 1` 正常但写操作会
`database is locked` 永久失败。现补 `conn.rollback()` —— 实测在
「无事务 / 只读事务 / 未提交写事务」三种状态下均为安全 no-op 或正确清理。

---

## 六、本次审查推翻的误报（重要）

为避免未来重复排查，记录以下**经实测确认是正确实现**的四个疑点：

### 误报 1：`analysis.py:497-508` 累积涨幅分支「漏掉 (-10,-5) 区间」

**实测**：分支链 `if -5 < a <= 10 / elif a <= -5 / elif a <= 15 / else` 完整覆盖实数轴。

```
a=-7   -> accum_lt_neg5 (0)
a=-3   -> accum_neg5_10 (+6)
a=12   -> accum_10_15 (+3)
a=18   -> accum_15_20 (-5)
```
无遗漏。`elif` 链的 `else` 分支保证兜底。

### 误报 2：`backfill_outcomes` 的 NaN 导致永不收敛

**实测**：SQLite 在写入时**把 NaN 转为 NULL**（`float('nan')` 插入后读回是 `None`）。
实际库 `next_day_pct` 的 NaN 数 = 0（NULL 数 326）。
该场景在 SQLite 上不可能发生，`!=` 比较安全。

### 误报 3：`save_recommendations` 事务残留导致连锁失败

**实测**：`SAVEPOINT/ROLLBACK TO/RELEASE` 用法正确，每行隔离；
末尾 `commit()` 的失败路径有 `conn.rollback()`（`:470-474`）清理事务。
预载失败路径显式 `raise`（`:395`，fail-loud，符合设计意图）。

### 误报 4：`_update_excluded_marks` 置1/置0 不对称

置1 用 `(date, symbol)` 符号级、置0 用 `(date, symbol, category)` 类别级，
看似不对称，但**置0 在置1 之后执行**，精确置0 总能纠正同轮并存的情况。
跨轮次时符号级置1 会连带排除该票其它类别行 —— 但这是**符合设计语义**的
（止损级信号，当日不再展示）。已验证三种场景结论均正确。

---

## 七、建议的实施顺序

```
第一步（高性价比）                                    ✅ 已完成 2026-09-11
  缺陷 2  PRAGMA 列索引修正       —— 一行改动，消除定时炸弹
  缺陷 4  主循环节拍修正          —— 数行改动，行为可见改善
  缺陷 5  重复计算 + _ensure_conn —— 代码整洁

第二步（需要小心，先备份 scanner.db）                  ✅ 已完成 2026-09-11
  缺陷 1  schema 迁移原子化       —— 已 cp scanner.db → scanner.db.bak-20260911
          同时把 except pass 改为 rollback + raise
          + 新增 tests/test_schema_migration.py（4 条回归）

第三步（口径修正，需回测确认）                         ⏳ 待办（优先级下调）
  缺陷 3  单利/复利统一为 None    —— 影响面 0.20% 且 0 例判定翻转，属技术债
```

### 第三步的额外注意事项

缺陷 3 会改变 🎯 判定结果，属**口径变更**而非修 bug。

**影响面已实测**（见 §三）：5006 行中仅 10 行受影响（0.20%），且**全部 0 例判定翻转**
（这 10 行的 5 日累计被上市首日涨幅主导，离阈值两个数量级）。

因此处理建议修正为：

1. **不急**：当前无实际损害，不构成线上问题。可归入「技术债清理」批次。
2. **改的时候要认清净效果**：`_replay_accum_from_rows` 返回 `None` 后，
   `_nextday_entry_accum:168-172` 会兜底到 DB 落库的 `accumulated_pct`，
   而该字段对 `new_face`/`momentum` 是**不含今日**的历史口径 ——
   即净效果是「单利口径 → 历史口径」，**不保证更准**。
3. **若目标是真修正**，正确做法不是简单返回 `None`，而是让数据不足时
   **明确拒绝判定**（不标记 🎯，而非换一个口径标），并在
   `NEXTDAY_ACCUM_MIN` 的校准文档里注明「次新股不参与 🎯 判定」。

**建议顺序**：先明确第 3 点的口径语义 → 再改 → 改后跑
`python -m pytest tests/` 确认无回归（注意：若改为拒绝判定，需检查是否有
测试用例依赖次新股被标 🎯）。

---

## 附：本次审查的验证方式

所有结论均通过可执行验证得出，而非静态阅读：

- 分支覆盖：直接调用判定函数遍历边界值（`-10, -5.1, -5, -3, 0, 10, 10.1, 15, 15.1, 18`）
- SQLite 语义：`PRAGMA index_list` 实际列序验证 + 加 UNIQUE 索引对比
- DDL 事务语义：实测 `executescript("BEGIN; ...")` 中途失败后 `ROLLBACK` 的撤销效果
- NaN 行为：内存库插入/读回实测 + 生产库脏值普查
- 事务路径：逐行阅读 commit/rollback/savepoint 的完整控制流
- 口径差异：构造 6 组涨幅序列量化复利/单利偏差
- 迁移痕迹：普查 21 张表的 `*_old` 孤儿表 + `schema_version` 记录
- 缺陷 1 复现：手工构造「新表已建 / 旧表未 DROP」现场后跑真实 `init_db`

**测试基线**：
- 改动前：**1204 passed / 13 skipped**（2026-09-11）
- 修复后：**1208 passed / 13 skipped**（净增 4 条 `tests/test_schema_migration.py`）
- `ruff check .` → All checks passed；`mypy scanner/` → Success, 60 source files

**改动文件清单**：

| 文件 | 改动 |
|---|---|
| `scanner/db/schema.py` | 迁移抽为 `_detect_mec_pk` + `_migrate_market_extra_cache_pk`，改单事务 + rollback + fail-loud |
| `scanner/orchestrator.py` | 硬过滤判定结果复用，消除重复调用 |
| `unified_scanner.py` | `_ensure_conn` 补 `rollback()`；主循环以轮次起点计算倒计时 |
| `tests/test_schema_migration.py` | **新增**，4 条迁移原子性/幂等/失败回滚/未知 PK 回归 |

**备份**：`scanner.db.bak-20260911`（51122176 bytes，改动前）

