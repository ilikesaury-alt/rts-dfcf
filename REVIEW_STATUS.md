# 审查覆盖追踪

每次 "全面审查项目代码" 后更新。

**标记含义**：
- ✅ = 已覆盖且无遗留问题
- ⚠️ = 已覆盖但仍有未修复项（参见 OPTIMIZE.md）
- ❌ = 从未覆盖

---

## 模块覆盖状态

| 模块 | 状态 | 最后审查 | 覆盖维度 | 备注 |
|------|------|---------|---------|------|
| `analysis.py` | ✅ | 2026-07-15 | 策略逻辑/配置一致性/评分正确性 | 权重引用一致；硬拒绝完整；无双算/遗漏 |
| `config.py` | ✅ | 2026-07-15 | 配置一致性/孤立条目 | 权重字典与 analysis.py 匹配；无新增孤立条目 |
| `validator.py` | ✅ | 2026-07-15 | 策略逻辑/交叉验证 | 四策略维度对齐；门槛合理；P2死代码(v_st_vol_weak) |
| `enhancer.py` | ✅ | 2026-07-15 | 加分逻辑/数据流完整性 | accumulate_final_score 覆盖完整；intraday_score 确认未累加 |
| `orchestrator.py` | ✅ | 2026-07-13 | 策略逻辑/数据流/双源融合 | fallthrough 链正确；双源加分双算已修(P0-1) |
| `cross_validation.py` | ✅ | 2026-07-13 | 错误处理/日期基准 | 日期基准统一 now_beijing(P1-1)；DB 查无异常已降级 |
| `api.py` | ✅ | 2026-07-13 | 错误处理/熔断/缓存 | 熔断区分空响应与失败(P1-2)；turnover_rate 缺省(P1-3) |
| `ths_api.py` | ✅ | 2026-07-13 | 错误处理 | 重试/超时/参数化齐全，无 P0 |
| `database.py` | ✅ | 2026-07-13 | 数据安全/连接 | SQL 参数化；连接回收正常 |
| `indicators.py` | ✅ | 2026-07-13 | 指标数学 | RSI/KDJ/MACD/ADX 数学正确，仅有死变量 P2 |
| `models.py` | ✅ | 2026-07-13 | 数据模型 | 无逻辑问题 |
| `candidate_pool.py` | ✅ | 2026-07-13 | 列表追踪 | 日期基准统一(P1-1) |
| `rank_trend.py` | ✅ | 2026-07-13 | 轨迹评分 | 无逻辑问题 |
| `sector.py` | ✅ | 2026-07-13 | 板块分类 | 无崩溃；None 名未防护(P2) |
| `trading_session.py` | ✅ | 2026-07-13 | 交易时段 | 时段判断用 now.time()，正确 |
| `display.py` | ✅ | 2026-07-13 | 展示 | 纯展示，无评分影响 |
| `log_utils.py` | ✅ | 2026-07-13 | 日志 | 日期基准统一(P1-1) |
| `utils.py` | ✅ | 2026-07-13 | 工具 | is_hk_stock 依赖 isdigit 实际惰性(P2) |
| `industry_chain/*` | 🗑️ | 2026-07-15 | 已删除 | 整个产业链子系统（industry_chain/ + industry_chain_scanner.py）已移除；相关表/init 函数/cross_validation 链依赖一并清理 |
| `unified_scanner.py` | ✅ | 2026-07-13 | 双源融合 | 双源加分双算已修(P0-1) |
| `stock_report.py` | ✅ | 2026-07-13 | 报告/健壮性 | None percent 防护已加(P1-4) |
| `tests/*` | ❌ | — | — | |

---

## 已知待修复项

见 `OPTIMIZE.md` 优先级标记。当前剩余：

| 优先级 | # | 问题 | 目标版本 |
|--------|---|------|---------|
| ✅ | 19 | `accumulated` 双算 `today_pct` | 2026-06-23 重构已修 |
| ✅ | 20 | `stock.percent` 与 K 线 `pcts[-1]` 混用 | 同上 |
| ✅ P0 | 31 | `intraday_score` 反指仍被累加 | 2026-07-01 已修；2026-07-15 确认 accumulate_final_score 无 intraday_score |
| 🟡 P1 | 21 | `Candidate.score` 可变性违规 | — |
| 🟡 P1 | 22 | 14 处 `bare except Exception` | — |
| 🟡 P1 | 25 | 交易时段显示不一致（11:45 vs 11:30） | — |
| 🟡 P1 | 10 | 行业分类纯名称匹配太粗糙 | — |
| 🟢 P2 | 26 | 测试覆盖不足（orchestrator/database 无测试） | — |
| 🟢 P2 | 28 | Feishu 推送注释状态未清理 | — |
| 🟢 P2 | 65 | `validator.py:339-340` 死代码（`V_ST_VOL_WEAK` 因硬门永远不可达） | — |
| 🟢 P2 | 66 | `candidate_pool.py:179-184` 死代码（`streak<3` 时 `>=5`/`>=3` 不可达） | — |
| ✅ P0 | 55 | `CROSS_SOURCE_BONUS` 双算（enhancer + unified_scanner 各加一次） | 2026-07-13 删除 unified_scanner 循环累加 |
| ✅ P0 | 56 | 产业链入口全新库崩溃（缺 daily_kline 表） | 2026-07-13 runner 补 init_db + pipeline 降级 |
| ✅ P0 | 57 | 产业链瓶颈节点名称子串匹配失效 | 2026-07-13 废弃瓶颈、按集中度/持续性/扩散重构相变与选股 |
| ✅ P0 | 58 | 动量交叉验证"无背离"维度恒 +4 使验证门失效 | 2026-07-13 V_MO_DIVERGENCE_NONE 改 0 |
| ✅ P1 | 59 | 跨模块日期基准不一致（date.today vs now_beijing） | 2026-07-13 全量统一 |
| ✅ P1 | 60 | 熔断把空响应当失败回吐陈旧数据 | 2026-07-13 区分 success/失败 |
| ✅ P1 | 61 | `turnover_rate` 缺省返回 None | 2026-07-13 加 `or 0` |
| ✅ P1 | 62 | `stock_report.py` 对 None percent 未防护 | 2026-07-13 4 处加守卫 |
| ✅ P1 | 63 | 回调 sector 维度 `count>=1` 即正分 / bollinger 回中轨同 +5 | 2026-07-13 sector 改 `count>=3`(+8)，1~2 改中性 0（非 -5）；bollinger 中轨改 +2；删死常量 COLD，新增 NEUTRAL |
| ✅ P1 | 64 | 动量量能阈值硬编码未用常量 | 2026-07-13 改用 `_MOMENTUM_VOL_HEALTHY_*` |

---

## 审查日志

| 日期 | 覆盖维度 | 覆盖模块 | 发现 | 备注 |
|------|---------|---------|------|------|
| 2026-07-01 | 策略逻辑/配置一致性/数据流完整性 | analysis.py, config.py, validator.py, enhancer.py | 🔴 intraday_score 反指累加(P0)修; 🟡5处硬编码迁移; 🟡12死权重清理; #19/#20确认已修 | 全库通读 baseline 建立，6/22 模块本次覆盖 4 个 |
| 2026-07-13 | 全模块（原 ❌ 18 个文件 + 测试） | orchestrator/cross_validation/api/ths_api/database/indicators/models/candidate_pool/rank_trend/sector/trading_session/display/log_utils/utils/industry_chain/unified_scanner/stock_report/tests | 🔴 双源加分双算(P0-1)修; 🔴 产业链全新库崩溃(P0-3)修; 🔴 动量验证门失效(P0-4)修; 🟡 日期基准/熔断/换手率/报告None/回调维度/量能常量 6 项修; P0-2 瓶颈映射暂缓 | 150 测试全过；PowerShell Set-Content 曾损坏 UTF-8 中文源，已 git checkout 还原并以 UTF-8 安全方式重做 |
| 2026-07-15 | 策略逻辑正确性/数据流完整性/加分逻辑/交叉验证/配置一致性/错误处理 | analysis.py/config.py/validator.py/enhancer.py/orchestrator.py/api.py/database.py/indicators.py/models.py/sector.py/utils.py | 无新 P0/P1 发现；#31(intraday反指)确认已修；新增 2 项 P2 死代码 | 245 测试全过；覆盖 analysis/config/validator/enhancer 四个 ⚠️ 模块→✅；全项目仅剩 tests/* 未覆盖 |
| 2026-07-15 | 子系统清理 + 主扫描器缺陷修复 | industry_chain(删除)/orchestrator.py/cross_validation.py/database.py/config.py | 🗑️ 移除整个产业链子系统(industry_chain/ + industry_chain_scanner.py)，清理 init_industry_chain_tables/chokepoint_recommendations 表/cross_validation 链依赖/Industry chain 配置块/文档；🔴 修复实时扫描 K 线永远少一天(orchestrator.py 盘中补拉今日 Bar)；🟡 双挂票 stock 级加分按 symbol 去重(排名不变) | 确认主扫描器市值过滤用真实 market_capital，热度值未当市值；热度当市值 bug 仅存在于已删的 industry_chain/pipeline.py:39 |

---

## 使用说明

每次审查前读此文件确定焦点区域；审查后更新覆盖状态和日志。
