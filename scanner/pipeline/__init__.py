"""主扫描通路（`orchestrator.scan_with_raw`）的阶段实现（2026-09-13 起逐步抽出）。

## 为什么有这个包

`scan_with_raw` 原本 507 行 / 圈复杂度 95，把「候选池构建 → 分类打标 → 评分 → 增强 →
校验 → 分桶 → 落库」七个语义上可独立描述的阶段串成一根长函数。更麻烦的是它
**零测试覆盖**——`tests/test_orchestrator.py` 只测了辅助函数，没有一个用例调用它。

## 等价变换纪律（进这个包之前必须读）

本包是**等价变换**的产物，不是重写。硬约束：

1. **每个函数抽出前后，输入输出必须逐字节一致**（含副作用：原地改写的字段、
   累加的计数器、打印的告警）。
2. **证明手段是黄金样本**，不是"看起来对"：
   ```
   python scripts/golden_scan.py --date 2026-09-08   # 四个日期各跑一次
   python scripts/golden_scan.py --date 2026-09-09
   python scripts/golden_scan.py --date 2026-09-10
   python scripts/golden_scan.py --date 2026-09-11
   ```
   全部退出码 0（逐字段一致）才算等价。**不一致就是破坏了等价，必须回退。**
3. 已知覆盖缺口：黄金样本里 `comeback` 桶恒为 0、`new_face`/`momentum` 多数日期
   0~1（原因见 `scripts/golden_scan.py` 的 `_coverage_warnings`）。**这些路径
   黄金样本保护不到**，动到它们必须另补针对性单测。
4. 本包只放**纯函数或近乎纯的函数**（输入 → 输出，不持有全局状态、不做 IO）。
   需要 conn / adapter 的阶段暂留 orchestrator，等有了对应测试手段再搬。

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
| `rebuild_pool_picks` | L576-577（v2 池选区重建） | `scoring.py` |
| `attach_tactic_tags` | L544-552（盘中操作纪律） | `tactics.py` |
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
    rebuild_pool_picks,
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
    "rebuild_pool_picks",
    "report_market_cap_availability",
    "split_and_sort_categories",
]
