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
| `analysis.py` | ⚠️ | 2026-07-01 | 策略逻辑/配置一致性 | 5处硬编码已迁移至 config.py；#19/#20 确认已修 |
| `config.py` | ⚠️ | 2026-07-01 | 配置一致性 | 清理 12 个死权重条目 |
| `validator.py` | ⚠️ | 2026-07-01 | 策略逻辑 | 维度定义清晰，联动 coverage 通过 |
| `enhancer.py` | ⚠️ | 2026-07-01 | 数据流完整性 | 移除反指 intraday_score 累加 (IC=-0.307) |
| `orchestrator.py` | ❌ | — | — | |
| `cross_validation.py` | ❌ | — | — | |
| `api.py` | ❌ | — | — | |
| `ths_api.py` | ❌ | — | — | |
| `database.py` | ❌ | — | — | |
| `indicators.py` | ❌ | — | — | |
| `models.py` | ❌ | — | — | |
| `candidate_pool.py` | ❌ | — | — | |
| `rank_trend.py` | ❌ | — | — | |
| `sector.py` | ❌ | — | — | |
| `trading_session.py` | ❌ | — | — | |
| `display.py` | ❌ | — | — | |
| `log_utils.py` | ❌ | — | — | |
| `utils.py` | ❌ | — | — | |
| `industry_chain/*` | ❌ | — | — | 整包未覆盖 |
| `unified_scanner.py` | ❌ | — | — | |
| `stock_report.py` | ❌ | — | — | |
| `tests/*` | ❌ | — | — | |

---

## 已知待修复项

见 `OPTIMIZE.md` 优先级标记。当前剩余：

| 优先级 | # | 问题 | 目标版本 |
|--------|---|------|---------|
| ✅ | 19 | `accumulated` 双算 `today_pct` | 2026-06-23 重构已修 |
| ✅ | 20 | `stock.percent` 与 K 线 `pcts[-1]` 混用 | 同上 |
| 🔴 P0 | 31 | `intraday_score` 反指仍被累加 | enhancer.py accumulate_final_score 2026-07-01 已修，需跟进 IC |
| 🟡 P1 | 21 | `Candidate.score` 可变性违规 | — |
| 🟡 P1 | 22 | 14 处 `bare except Exception` | — |
| 🟡 P1 | 25 | 交易时段显示不一致（11:45 vs 11:30） | — |
| 🟡 P1 | 10 | 行业分类纯名称匹配太粗糙 | — |
| 🟢 P2 | 26 | 测试覆盖不足（orchestrator/database 无测试） | — |
| 🟢 P2 | 28 | Feishu 推送注释状态未清理 | — |

---

## 审查日志

| 日期 | 覆盖维度 | 覆盖模块 | 发现 | 备注 |
|------|---------|---------|------|------|
| 2026-07-01 | 策略逻辑/配置一致性/数据流完整性 | analysis.py, config.py, validator.py, enhancer.py | 🔴 intraday_score 反指累加(P0)修; 🟡5处硬编码迁移; 🟡12死权重清理; #19/#20确认已修 | 全库通读 baseline 建立，6/22 模块本次覆盖 4 个 |

---

## 使用说明

每次审查前读此文件确定焦点区域；审查后更新覆盖状态和日志。
