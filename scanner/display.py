"""display 聚合器：物理实现已拆到 scanner/view/{model,assemble,render}.py。

本模块仅做 re-export，保持 `from scanner.display import X` 对外契约零变化（feishu / hot_watch / today_report / leaderboard_obs / tests 一行不改；原清单里的 final_pick 已于 2026-09-21 随终选参考区整体删除）。

**唯一有意的收窄**：旧 display.py 在模块顶层 `from x import y` 顺带把一批名字暴露成了
`scanner.display.y`（如 `os` / `now_beijing` / `Candidate` / `composite_tier` 等 47 个），
那些从来不是 display 的契约，只是导入的副产物。聚合器不再透传它们；已确认全仓无消费方。
这份收窄的完整清单与校验工具见 `scripts/_verify_view_split.py`
（`EXPECTED_SURFACE_REDUCTION`，双向比对，防止"悄悄少一个名字"）。
"""
from scanner.categories import CAT_LABEL  # noqa: F401

# 原 display 还把下列 config/其它模块全局作为模块属性暴露（测试与既有消费方只读）：
from scanner.config import CORE_DIP_DISPLAY_MAX, TREND_MARK_ENABLED  # noqa: F401
from scanner.core_themes import core_stock_symbols  # noqa: F401
from scanner.ranking import fresh_candidate  # noqa: F401
from scanner.signals import fund_flow_signal  # noqa: F401

# 以下为原 display 透传名（非本文件定义），消费方仍从 scanner.display 取
from scanner.utils import clear_screen  # noqa: F401
from scanner.view.assemble import *  # noqa: F401,F403
from scanner.view.model import *  # noqa: F401,F403
from scanner.view.render import *  # noqa: F401,F403
