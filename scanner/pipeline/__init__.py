"""主扫描通路（`orchestrator.scan_with_raw`）的阶段实现（2026-09-13 起逐步抽出）。

## 为什么有这个包

`scan_with_raw` 原本 507 行 / 圈复杂度 95，把「候选池构建 → 分类打标 → 评分 → 增强 →
校验 → 分桶 → 落库」七个语义上可独立描述的阶段串成一根长函数。更麻烦的是它
**零测试覆盖**——`tests/test_orchestrator.py` 只测了辅助函数，没有一个用例调用它。

## 等价变换纪律（进这个包之前必须读）

本包是**等价变换**的产物，不是重写。硬约束：

1. **每个函数抽出前后，输入输出必须逐字节一致**（含副作用：原地改写的字段、
   累加的计数器、打印的告警）。
2. **证明手段是单测，不是"看起来对"**：`pytest tests/test_pipeline.py` 全绿。
3. ⚠ **黄金样本工具已于 2026-09-28 删除**（`scripts/golden_scan.py` +
   `scripts/golden/*.json`，1.8 MB）。它原本是「拆完逐字段一致」的那把尺子，
   但第 3 步（继续拆 `scan_with_raw`）已决定不再做，工具失去唯一用途；且基线
   输入从**当前 `scanner.db`** 重建、随库生长结构性必腐，2026-09-18 最后一次
   重建后四日期又全红（干净树复现，非等价破坏）—— 常红的守卫等于没有守卫。
   **要复原请查 git 历史**：脚本与 4 份基线都在删除它的那个提交的父版本里
   （`git log --diff-filter=D -- scripts/golden_scan.py` 找到删除提交，再 `git show <sha>^:scripts/golden_scan.py`）。
4. 已知覆盖缺口：`new_face` / `momentum` / `comeback` 在真实通路里分支稀疏，
   本包单测只能覆盖纯函数部分 —— 需要 conn / adapter 的阶段仍在 orchestrator，
   **动到它们必须另补针对性单测**（`tests/test_orchestrator.py` 对
   `scan_with_raw` 本身仍零覆盖，这是已知且接受的缺口）。
5. 本包只放**纯函数或近乎纯的函数**（输入 → 输出，不持有全局状态、不做 IO）。

## 当前已抽出

| 函数 | 原位置 | 模块 |
|---|---|---|
| `filter_by_market_cap` | scan_with_raw L176-201（含告警打印） | `pool.py` |
| `build_current_quotes` | L592-599 | `pool.py` |
| `build_rps_inputs` | L430-450 | `features.py` |
| `attach_minute_trends` | L477-488 | `features.py` |
| `split_and_sort_categories` | L560-577 | `buckets.py` |
| `accumulate_final_scores` | L480-482（加分循环原地替换） | `scoring.py` |
| `filter_excluded_by_risk` | L492-498（风险硬过滤） | `scoring.py` |
| `attach_tactic_tags` | L544-552（盘中操作纪律） | `tactics.py` |

注：`rebuild_pool_picks`（v2 池选区重建）随 v2 池管道于 2026-09-28 从本包移除。
"""

from scanner.pipeline.buckets import split_and_sort_categories
from scanner.pipeline.features import attach_minute_trends, build_rps_inputs
from scanner.pipeline.pool import (
    build_current_quotes,
    filter_by_market_cap,
    report_market_cap_availability,
)
from scanner.pipeline.scoring import (
    accumulate_final_scores,
    filter_excluded_by_risk,
)
from scanner.pipeline.tactics import attach_tactic_tags

__all__ = [
    "accumulate_final_scores",
    "attach_minute_trends",
    "attach_tactic_tags",
    "build_current_quotes",
    "build_rps_inputs",
    "filter_by_market_cap",
    "filter_excluded_by_risk",
    "report_market_cap_availability",
    "split_and_sort_categories",
]
