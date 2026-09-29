"""终选阶段的评分累加 / 硬过滤（从 `orchestrator.scan_with_raw` 等价抽出，2026-09-13）。

**等价纪律**：见 `scanner/pipeline/__init__.py`。改动本模块后必须跑
`pytest tests/test_pipeline.py`（黄金样本工具已于 2026-09-28 删除，见包 docstring）。

本模块原三个函数现为两个：`accumulate_final_scores` / `filter_excluded_by_risk`。
第三个 `rebuild_pool_picks`（v2 池选区重建）随 v2 池管道于 2026-09-28 删除。
"""

from __future__ import annotations

from dataclasses import replace as dataclass_replace

from scanner.candidates import candidate_excluded_by_risk
from scanner.enhancer import accumulate_final_score

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
    names = "、".join(f"{c.stock.name}({c.stock.symbol})" for c in excluded[:_EXCLUDED_NAME_LIMIT])
    more = f" 等{len(excluded)}只" if len(excluded) > _EXCLUDED_NAME_LIMIT else ""
    print(f"  [风险过滤] {len(excluded)} 只命中硬排除标签，已移出推荐：{names}{more}")
    excluded_ids = {id(c) for c in excluded}
    return [c for c in all_candidates if id(c) not in excluded_ids], excluded
