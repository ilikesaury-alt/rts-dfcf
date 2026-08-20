"""策略类别注册表（单一事实来源）。

集中定义所有策略类别的元信息（短标签 / 颜色 / 展示优先级 / 操作建议 /
是否进综合排序主表 / 是否参与 🎯 次日大涨画像 / 是否仍由实时扫描产出），
并派生出各消费方所需的类别集合。

2026-08-20 收敛：此前「类别宇宙」在 backtest.ACTIVE_CATEGORIES /
portfolio_backtest.PORTFOLIO_CATEGORIES / config.CAT_DISPLAY_PRIORITY /
config.SUGGEST_BY_CAT / display.CAT_LABEL / display.CAT_COLOR /
ranking.NEXTDAY_CAT_PRIORITY / historical_rescan.RESCANABLE_CATEGORIES /
prevday_perf.GROUPS 共 5+ 处各自定义一份，pullback 策略 2026-07-30 下线后
残留条目散落多处；新增类别（如 core_dip）需同步 8 处。现统一到此文件，
其余模块改为从本注册表派生，单点增删类别即可。

pullback 保留为「已下线」条目（live_produced=False）：回测/归因仍需处理 DB
中历史 pullback 行用于校准（test_backtest 断言），但实时扫描/展示路径通过
LIVE_CATEGORIES / MAIN_TABLE_CATEGORIES 自动排除它。
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class CategoryInfo:
    label: str               # 综合排序短标签（无 ANSI）
    color_key: str           # scanner.display.ANSI 字典键名（由展示层解析为色码）
    display_priority: int    # 综合排序档位：值越小越靠前（0 = 最前）
    suggest: str             # 操作建议文案（可能含 ANSI 转义）
    in_main_table: bool      # 是否进综合排序主表（False = 回马枪/核心低吸独立观察区）
    nextday_markable: bool   # 是否参与 🎯 次日大涨画像判定（= 主表五类）
    live_produced: bool      # 实时扫描是否仍产出该类别（pullback=False = 已下线）


CATEGORY_REGISTRY: dict[str, CategoryInfo] = {
    "rebound": CategoryInfo("RBD", "CYAN", 0, "\033[96m推荐\033[0m", True, True, True),
    "known_new_face": CategoryInfo("kNF", "GREEN", 1, "\033[96m推荐\033[0m", True, True, True),
    "momentum": CategoryInfo("MOM", "YELLOW", 2, "参考", True, True, True),
    "new_face": CategoryInfo("NEW", "GREEN", 3, "参考", True, True, True),
    "short_term": CategoryInfo("ST", "RED", 4, "参考", True, True, True),
    "comeback": CategoryInfo("CB", "CYAN", 5, "\033[96m回马\033[0m", False, False, True),
    "core_dip": CategoryInfo("DIP", "GREEN", 99, "低吸", False, False, True),
    # 已下线的 pullback（2026-07-30 删除策略代码）：保留条目供回测/归因处理历史行，
    # 实时扫描与展示主表经 LIVE_CATEGORIES / MAIN_TABLE_CATEGORIES 排除。
    "pullback": CategoryInfo("PB", "RED", 6, "\033[91m回避\033[0m", False, False, False),
}

# ── 派生集合（消费方按需取用，新增类别只改上方注册表）──

# 综合排序主表展示排序（值越小越靠前）；含全部已知类别键（含已下线 pullback 占位）。
CAT_DISPLAY_PRIORITY: dict[str, int] = {
    name: info.display_priority for name, info in CATEGORY_REGISTRY.items()
}

# 操作建议映射（含 ANSI）。
SUGGEST_BY_CAT: dict[str, str] = {name: info.suggest for name, info in CATEGORY_REGISTRY.items()}

# 🎯 次日大涨画像可标记类别集合（= 主表五类，ranking._is_nextday_marked 用其做成员判定）。
NEXTDAY_CAT_PRIORITY: set[str] = {
    name for name, info in CATEGORY_REGISTRY.items() if info.nextday_markable
}

# 综合排序短标签。
CAT_LABEL: dict[str, str] = {name: info.label for name, info in CATEGORY_REGISTRY.items()}

# 颜色键（展示层 _resolve_category_color 解析为 ANSI 色码）。
CATEGORY_COLOR_KEYS: dict[str, str] = {name: info.color_key for name, info in CATEGORY_REGISTRY.items()}

# 实时扫描仍产出的类别（剔除已下线的 pullback）。
LIVE_CATEGORIES: set[str] = {name for name, info in CATEGORY_REGISTRY.items() if info.live_produced}

# 综合排序主表类别（榜上五类）。
MAIN_TABLE_CATEGORIES: set[str] = {name for name, info in CATEGORY_REGISTRY.items() if info.in_main_table}

# 回测/归因：处理 DB 中全部已知类别（含已下线 pullback，用于历史校准）。
ATTRIBUTION_CATEGORIES: set[str] = set(CATEGORY_REGISTRY.keys())

# 组合回测类别（剔除仅展示用、不入组合评分的 core_dip；含 comeback 观察区）。
PORTFOLIO_CATEGORIES: set[str] = {name for name, info in CATEGORY_REGISTRY.items()
                                  if info.live_produced and name != "core_dip"}

# 历史重扫可重算类别（榜上五类，不含 comeback/core_dip/pullback）。
RESCANABLE_CATEGORIES: tuple[str, ...] = tuple(
    name for name, info in CATEGORY_REGISTRY.items() if info.live_produced and info.in_main_table
)
