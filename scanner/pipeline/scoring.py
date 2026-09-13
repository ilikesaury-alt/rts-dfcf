"""终选阶段的评分累加 / 硬过滤 / 池选重建（从 `orchestrator.scan_with_raw` 等价抽出，2026-09-13）。

**等价纪律**：见 `scanner/pipeline/__init__.py`。改动本模块后必须跑
`python scripts/golden_scan.py --date <四个日期>`，全部退出码 0 才算等价。

本模块的三个函数都涉及「**必须基于最新对象**」这一共同主题：加分循环用
`dataclass_replace` 造了新对象，之后任何按旧引用重建列表的操作都会拿到过期 score。
"""

from __future__ import annotations

from dataclasses import replace as dataclass_replace

from scanner.candidates import candidate_excluded_by_risk
from scanner.enhancer import accumulate_final_score
from scanner.models import V2_CATEGORY

# 硬过滤提示最多展示几只票名（超出用「等 N 只」省略）
_EXCLUDED_NAME_LIMIT = 8


def accumulate_final_scores(all_candidates: list, opening_scores: dict) -> None:
    """把 `accumulate_final_score` 的 extra 累加进 score（**原地按索引替换**）。

    为什么必须原地替换列表元素而不是 `c.score += extra`：`Candidate` 是冻结语义的
    dataclass 用法，且下游多处持有**对象引用**（session_state.today_pool、
    buckets、display），原地改字段会让「同一只票在两个桶里」共享同一对象时互相污染。

    **双挂候选（首板票同时挂 new_face + short_term）必须各自独立计算 extra**：
    `accumulate_final_score` 依赖 `c.gap_up_bonus` / `c.list_momentum_bonus` 等，
    这些 bonus 在 `apply_all_bonuses` 中按 candidate 独立计算（如 `apply_gap_up_bonus`
    依据 `c.category` 选 key）。若复用同一 extra，short_term 桶会拿到 new_face 桶的
    bonus，排名错位。
    """
    for i, c in enumerate(all_candidates):
        extra = accumulate_final_score(c, opening_scores)
        all_candidates[i] = dataclass_replace(c, score=c.score + extra)


def rebuild_pool_picks(all_candidates: list, v2_category: str = V2_CATEGORY) -> list:
    """从 `all_candidates` 重建 v2 池选区并按**今日涨幅降序**。

    必须重建而非沿用 `pool_picks` 旧列表：`accumulate_final_scores` 已用
    `dataclass_replace` 造了新对象，旧列表持有的是未累加 extra 的过期对象
    （与 v1 分类列表重建同理，这个坑踩过两次）。
    """
    picks = [c for c in all_candidates if c.category == v2_category]
    picks.sort(key=lambda c: -(c.stock.percent or 0))
    return picks


def filter_excluded_by_risk(all_candidates: list) -> tuple[list, list]:
    """风险硬过滤：命中「卖出/止损」级标签的候选移出推荐列表。

    返回 `(保留列表, 被排除列表)`。

    **位置敏感**：必须在 `session_state.update_pool` / `update_stale` 之后执行——
    不影响候选池掉榜与排名历史，只作用于最终对外展示的推荐列表，确保推荐只含可买票。

    无命中时**原样返回同一列表对象**（不复制），保持与原实现的对象身份一致。
    """
    excluded = [c for c in all_candidates if candidate_excluded_by_risk(c)]
    if not excluded:
        return all_candidates, []
    names = "、".join(
        f"{c.stock.name}({c.stock.symbol})" for c in excluded[:_EXCLUDED_NAME_LIMIT]
    )
    more = f" 等{len(excluded)}只" if len(excluded) > _EXCLUDED_NAME_LIMIT else ""
    print(f"  [风险过滤] {len(excluded)} 只命中硬排除标签，已移出推荐：{names}{more}")
    excluded_ids = {id(c) for c in excluded}
    return [c for c in all_candidates if id(c) not in excluded_ids], excluded
