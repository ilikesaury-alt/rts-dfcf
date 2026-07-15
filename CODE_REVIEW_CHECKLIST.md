# 代码审查清单

按维度组织，每次审查选 2-3 个维度深入检查。每个问题标记 🔴/🟡/🟢。

---

## 1. 策略逻辑正确性

| # | 检查项 | 关联文件 | 说明 |
|---|--------|---------|------|
| 1.1 | 评分维度权重 vs `config.py` 是否漂移 | `analysis.py`, `config.py` | `analysis.py` 中硬编码的加分值是否与 `config.py` 中的 `*_WEIGHTS` 一致 |
| 1.2 | 硬拒绝条件是否被扣分绕过 | `analysis.py` | 应硬拒绝的条件（如 `today_pct > MAX_NEW_FACE_TODAY_PCT`）有没有被错误地变成扣分 |
| 1.3 | 策略 fallthrough 路径是否完整 | `analysis.py` | new_face → momentum → pullback → known_new_face，每个阶段候补池是否正交接力 |
| 1.4 | cross-validation 维度对齐 | `validator.py`, `cross_validation.py` | 3 个维度是否覆盖且不重叠，≥2 通过是否合理 |
| 1.5 | 新增策略是否注册到 fallthrough 链 | `analysis.py`, `orchestrator.py` | 新增策略有没有在 orchestrator 的评分循环中被调用 |
| 1.6 | 最小门槛下界检查 | `analysis.py`, `config.py` | `*_MIN_SCORE` 门槛是否真正过滤掉低分票 |

## 2. 数据流完整性

| # | 检查项 | 关联文件 | 说明 |
|---|--------|---------|------|
| 2.1 | API 失败降级路径 | `api.py`, `ths_api.py` | 网络异常/返回空时是否有降级逻辑，还是直接抛异常 |
| 2.2 | K 线缓存一致性 | `database.py`, `api.py` | DB 缓存的窗口（60天）与 API 回填逻辑是否一致 |
| 2.3 | 市值/换手率缓存策略 | `api.py` | 非扫描周期间是否跨周期脏读（之前修过一次全局缓存） |
| 2.4 | 双源融合逻辑 | `unified_scanner.py`, `orchestrator.py` | 雪球主源 + 同花顺校验的符号集交集逻辑是否正确 |
| 2.5 | 交易时段判断 | `trading_session.py` | 节假日列表是否过期，时段判断是否用 `now.time()` 而非字符串 |
| 2.6 | `stock.percent` vs K 线 `pcts[-1]` 混用 | `analysis.py` | 实时涨幅与 K 线昨日收盘涨幅是否被混用在同一个累计计算中 |

## 3. 错误处理与健壮性

| # | 检查项 | 关联文件 | 说明 |
|---|--------|---------|------|
| 3.1 | bare `except:` 数量 | 全局 | 是否所有 `except:` 都指定了异常类型？新增了没有？ |
| 3.2 | 异常静默 | 全局 | `except: pass` 是否记录日志？至少 `logger.warning` |
| 3.3 | 网络请求超时 | `api.py` | `requests.get()` 是否有 `timeout` 参数？ |
| 3.4 | 重试机制 | `api.py` | 雪球 API 失败时是否有退避重试？ |
| 3.5 | K 线不足的处理 | `analysis.py`, `indicators.py` | 数据长度不足以计算 MA5/MA10/RSI 时是否优雅降级 |
| 3.6 | 数据库连接回收 | `database.py` | SQLite 连接是否及时关闭，是否有连接泄漏风险 |

## 4. 工程质量

| # | 检查项 | 关联文件 | 说明 |
|---|--------|---------|------|
| 4.1 | 不可变性 | 全局 | `Candidate.score` 是否 `+=` 突变？是否创建新对象代替 |
| 4.2 | 循环内 import | 全局 | `import` / `__import__` 是否在函数体内？ |
| 4.3 | 死代码 | 全局 | 已注释的代码块、已废弃的常量/函数引用 |
| 4.4 | 日志一致性 | `log_utils.py` | 是否统一使用 logger 而非 `print()` |
| 4.5 | 类型注解覆盖率 | 全局 | 新增函数是否有完整类型注解 |
| 4.6 | 文件行长度 | 全局 | 单行是否超过 120 字符（ruff 规则） |
| 4.7 | `f-string` 安全 | 全局 | f-string 内是否有大括号逃逸错误或格式化问题 |

## 5. 测试覆盖

| # | 检查项 | 关联文件 | 说明 |
|---|--------|---------|------|
| 5.1 | 核心策略评分有测试护城河 | `tests/test_analysis.py` | new_face / momentum / pullback 的边界条件是否都有测试 |
| 5.2 | 新增分支有对应测试 | 全局 | 最近修改的分支是否有对应的测试用例 |
| 5.3 | config 常量公开后是否有测试 | `tests/test_config.py` | 所有 `*_WEIGHTS`、`*_MIN_SCORE` 的变更需有测试守护 |
| 5.4 | mock 与真实行为偏差 | `tests/helpers.py` | mock 工厂是否过于简化，掩盖了真实逻辑中的 bug |
| 5.5 | 无测试覆盖的文件 | 全局 | 检查哪些文件（如 `orchestrator.py`）仍无单元测试 |

## 6. 配置一致性

| # | 检查项 | 关联文件 | 说明 |
|---|--------|---------|------|
| 6.1 | `config.py` 阈值 vs 硬编码 | `analysis.py`, `config.py` | 检查 `analysis.py` 中是否有整数字面量本应引用 `config.py` 常量 |
| 6.2 | `config.py` 孤立条目 | `config.py` | 是否有已不被任何文件引用的配置项 |
| 6.3 | 策略权重未注册 | `enhancer.py`, `config.py` | `apply_all_bonuses()` 中 `weight-key` 映射是否与 `config.py` 权重一致 |

## 7. 性能

| # | 检查项 | 关联文件 | 说明 |
|---|--------|---------|------|
| 8.1 | API 请求频率 | `api.py` | `_throttle()` 是否生效，间隔是否合理 |
| 8.2 | 重复 K 线请求 | `orchestrator.py` | 同一只股票是否在单次扫描中被多次请求 K 线 |
| 8.3 | DB 大表查询效率 | `database.py` | `appearances` 表是否有索引，全表扫描是否可接受 |

---

## 用法

每次 "全面审查项目代码" 时，按以下流程执行：

1. **读 REVIEW_STATUS.md** → 知道上次覆盖了什么、还缺什么
2. **选 2-3 个维度** → 从上表选未覆盖维度或上次覆盖时间最早的维度
3. **逐项检查** → 每个检查项过一遍，记录发现
4. **更新 REVIEW_STATUS.md** → 标记本次覆盖的模块/维度
5. **总结发现** → 按 🔴/🟡/🟢 分级输出
