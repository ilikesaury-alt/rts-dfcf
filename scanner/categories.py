"""策略类别注册表（单一事实来源）。

集中定义所有策略类别的元信息（短标签 / 颜色 / 展示优先级 / 操作建议 /
是否进综合排序主表 / 是否仍由实时扫描产出），
并派生出各消费方所需的类别集合。

2026-08-20 收敛：此前「类别宇宙」在 backtest.ACTIVE_CATEGORIES /
portfolio_backtest.PORTFOLIO_CATEGORIES / config.CAT_DISPLAY_PRIORITY /
config.SUGGEST_BY_CAT / display.CAT_LABEL / display.CAT_COLOR /
ranking.NEXTDAY_CAT_PRIORITY（该派生集 2026-09-16 随 🎯 画像删除）/
historical_rescan.RESCANABLE_CATEGORIES / prevday_perf.GROUPS 共 5+ 处各自定义一份，
pullback 策略 2026-07-30 下线后残留条目散落多处；新增类别（如 core_dip）需同步 8 处。
现统一到此文件，其余模块改为从本注册表派生，单点增删类别即可。

pullback 保留为「已下线」条目（live_produced=False）：回测/归因仍需处理 DB
中历史 pullback 行用于校准（test_backtest 断言），但实时扫描/展示路径通过
LIVE_CATEGORIES / MAIN_TABLE_CATEGORIES 自动排除它。

⚠ 已下线条目有两种，**不可混为一谈**（2026-09-28 pool_pick 退池时厘清）：
  - pullback：历史行**仍要进归因样本**（用于校准）→ 留在 ATTRIBUTION_CATEGORIES。
  - pool_pick：历史行**必须退出归因样本**（负超额桶，留着会稀释聚合 hit 率）→
    故单列 ATTRIBUTION_EXCLUDED_CATEGORIES，与「已下线」解耦。
判断标准是「该桶的历史行对度量还有没有价值」，不是「它是不是下线了」。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class CategoryInfo:
    label: str  # 综合排序短标签（无 ANSI）
    color_key: str  # scanner.display.ANSI 字典键名（由展示层解析为色码）
    display_priority: int  # 综合排序档位：值越小越靠前（0 = 最前）
    suggest: str  # 操作建议文案（可能含 ANSI 转义）
    in_main_table: bool  # 是否进综合排序主表（False = 核心低吸独立观察区）
    live_produced: bool  # 实时扫描是否仍产出该类别（pullback=False = 已下线）
    # 综合排序组内分数键方向（ranking.score_sort_key 消费）：True = 评分降序在前
    # （常规）；False = 评分升序在前——kNF 分数反指（低分档 hit 更高，2026-08-10 回测
    # 分桶：低分档[18,37) cum_3d +5.58/64%胜率 vs 高分档[77,98) -3.76/33%）。
    score_descending: bool = True


CATEGORY_REGISTRY: dict[str, CategoryInfo] = {
    # 已退池的 pool_pick（2026-09-28）：v2 池选类别。in_main_table / live_produced 双 False。
    # 该类别自 2026-09-14 展示区隐藏、2026-09-21 合池消费方删除后，**终端与飞书均无任何
    # 呈现**，却仍日产 27~76 行落 recommendations（占全部行 47~65%），sym-day 去重口径
    # hit≥7% 仅 3.0%（n=986）——低于 CATEGORY_HIT_RATE_DEFAULT 0.078，是唯一的负超额桶。
    # 保留条目（而非删除键）的原因同 pullback：DB 里有 988 行存量，CAT_LABEL /
    # SUGGEST_BY_CAT / CATEGORY_COLOR_KEYS 仍需能解析历史行，否则渲染路径 KeyError。
    # ⚠ in_main_table 原为 True 但 assemble.py 早已硬编码把它排除出 main_recs —— 注册表
    # 在说谎。置 False 是让注册表与实际渲染口径一致（该撒谎此前无消费方，属潜伏坑）。
    "pool_pick": CategoryInfo("池选", "GREEN", 0, "\033[96m推荐\033[0m", False, False),
    "rebound": CategoryInfo("RBD", "CYAN", 1, "\033[96m推荐\033[0m", True, True),
    "known_new_face": CategoryInfo("kNF", "GREEN", 2, "\033[96m推荐\033[0m", True, True, score_descending=False),
    "momentum": CategoryInfo("MOM", "YELLOW", 3, "参考", True, True),
    "new_face": CategoryInfo("NEW", "GREEN", 4, "参考", True, True),
    "short_term": CategoryInfo("ST", "RED", 5, "参考", True, True),
    "core_dip": CategoryInfo("DIP", "GREEN", 99, "低吸", False, True),
    # 已下线的 pullback（2026-07-30 删除策略代码）：保留条目供回测/归因处理历史行，
    # 实时扫描与展示主表经 LIVE_CATEGORIES / MAIN_TABLE_CATEGORIES 排除。
    "pullback": CategoryInfo("PB", "RED", 7, "\033[91m回避\033[0m", False, False),
}

# ── 派生集合（消费方按需取用，新增类别只改上方注册表）──

# 综合排序主表展示排序（值越小越靠前）；含全部已知类别键（含已下线 pullback 占位）。
CAT_DISPLAY_PRIORITY: dict[str, int] = {name: info.display_priority for name, info in CATEGORY_REGISTRY.items()}

# 操作建议映射（含 ANSI）。
SUGGEST_BY_CAT: dict[str, str] = {name: info.suggest for name, info in CATEGORY_REGISTRY.items()}

# 综合排序短标签。
CAT_LABEL: dict[str, str] = {name: info.label for name, info in CATEGORY_REGISTRY.items()}

# 颜色键（展示层 _resolve_category_color 解析为 ANSI 色码）。
CATEGORY_COLOR_KEYS: dict[str, str] = {name: info.color_key for name, info in CATEGORY_REGISTRY.items()}

# 实时扫描仍产出的类别（剔除已下线的 pullback）。
LIVE_CATEGORIES: set[str] = {name for name, info in CATEGORY_REGISTRY.items() if info.live_produced}

# 综合排序主表类别（榜上五类）。
MAIN_TABLE_CATEGORIES: set[str] = {name for name, info in CATEGORY_REGISTRY.items() if info.in_main_table}

# 组内分数键方向（ranking.score_sort_key 消费）：True = 评分降序在前。
SCORE_DESCENDING_BY_CAT: dict[str, bool] = {name: info.score_descending for name, info in CATEGORY_REGISTRY.items()}

# 回测/归因：处理 DB 中全部已知类别（含已下线 pullback，用于历史校准），
# **减去** ATTRIBUTION_EXCLUDED_CATEGORIES（已退池、负超额的历史桶）。
ATTRIBUTION_CATEGORIES: set[str] = set(CATEGORY_REGISTRY.keys())

# 已退池类别：DB 存量行仍在，但必须退出归因/回测样本口径。
# 与「已下线」（live_produced=False）是**正交**的两个概念——pullback 下线但仍参与校准，
# pool_pick 下线且必须剔除，故不能靠 live_produced 单字段表达。
# 2026-09-28 新增：pool_pick 退池时引入。
ATTRIBUTION_EXCLUDED_CATEGORIES: set[str] = {"pool_pick"}

# 组合回测类别（剔除仅展示用、不入组合评分的 core_dip）。
PORTFOLIO_CATEGORIES: set[str] = {
    name for name, info in CATEGORY_REGISTRY.items() if info.live_produced and name != "core_dip"
}

# 历史重扫可重算类别（榜上五类，不含 core_dip/pullback）。
RESCANABLE_CATEGORIES: tuple[str, ...] = tuple(
    name for name, info in CATEGORY_REGISTRY.items() if info.live_produced and info.in_main_table
)
