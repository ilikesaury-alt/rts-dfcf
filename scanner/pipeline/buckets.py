"""分类分桶与排序（从 `orchestrator.scan_with_raw` 等价抽出，2026-09-13）。

**等价纪律**：见 `scanner/pipeline/__init__.py`。改动本模块后必须跑
`python scripts/golden_scan.py --date <四个日期>`，全部退出码 0 才算等价。
"""

from __future__ import annotations

from typing import Any

from scanner.candidates import new_face_sort_key

# 分桶规则：桶名 → 判定（原 scan_with_raw L565-569）
# comeback 桶已于 2026-09-16 删除（用户决策；其 category 与扫描逻辑一并移除）。
BUCKET_MATCHERS: dict[str, Any] = {
    "new_faces": lambda c: c.category in ("new_face", "known_new_face"),
    "momentum": lambda c: c.category == "momentum",
    "rebound": lambda c: c.category == "rebound",
    "short_term": lambda c: c.category == "short_term",
}


def split_and_sort_categories(all_candidates: list) -> dict[str, list]:
    """把候选按类别重建五个桶并各自排序，返回 {桶名: 候选列表}。

    **为什么必须从 `all_candidates` 重建而不沿用旧列表引用**：加分循环用
    `dataclass_replace(c, score=c.score + extra)` 创建了**新对象**，旧列表
    （`new_faces` / `momentum` / …）持有的仍是未累加 extra 的**过期对象**。
    这个坑在代码里已经踩过两次（v1 与 pool_picks 各一次），故抽成函数时
    在文档里写死，避免后人"优化"成复用旧列表。

    排序键（与 display / today_report 单源，勿改）：
      - new_faces：`candidates.new_face_sort_key`
      - 其余三桶：score 降序。

    ⚠ 原 comeback 桶（`ranking.comeback_sort_key`，资金流优先）已于 2026-09-16 删除。
    """
    buckets: dict[str, list] = {
        name: [c for c in all_candidates if matcher(c)]
        for name, matcher in BUCKET_MATCHERS.items()
    }
    buckets["new_faces"].sort(key=lambda c: new_face_sort_key(c))
    for name in ("momentum", "rebound", "short_term"):
        buckets[name].sort(key=lambda c: -c.score)
    return buckets
