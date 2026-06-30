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
| `analysis.py` | ⚠️ | — | — | OPTIMIZE.md 有 2 个 P0 bug 未修复 |
| `config.py` | ❌ | — | — | |
| `validator.py` | ❌ | — | — | |
| `cross_validation.py` | ❌ | — | — | |
| `api.py` | ❌ | — | — | |
| `ths_api.py` | ❌ | — | — | |
| `database.py` | ❌ | — | — | |
| `orchestrator.py` | ❌ | — | — | |
| `enhancer.py` | ❌ | — | — | |
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
| 🔴 P0 | 19 | `accumulated` 双算 `today_pct` | — |
| 🔴 P0 | 20 | `stock.percent` 与 K 线 `pcts[-1]` 混用 | — |
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
| — | — | — | — | 首次基线尚未建立 |

---

## 使用说明

每次审查前读此文件确定焦点区域；审查后更新覆盖状态和日志。
