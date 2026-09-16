# scanner/config.py — 统一配置入口（单一真源对外契约不变）
#
# 2026-09-13 拆分：原 1082 行「上帝文件」按领域拆为以下子模块，定义集中在子模块，
# 本文件仅做 re-export，保持 `from scanner.config import X` 对所有消费方完全不变
# （47 个模块零改动）。拆分依据见 docs/audit-2026-09-13.md §四 第 4 步。
#
#   子模块                  领域
#   config_core             时区/时间、数据源策略、请求超时、路径、环境变量助手、交易时段、早/尾盘加分
#   config_categories       策略桶门槛、市值/价格限制、回马枪·核心低吸配置、策略桶开关
#   config_scoring         评分/打分阈值、加分项、交叉验证权重、次日大涨/复合评分/持有期
#   config_risk            风险标签、硬过滤集合、排雷阈值、基本面风险、反转移出阈值
#   config_sources         飞书推送、外部数据源(涨停池/资金流/概念)、v2/决策/终选/走势美展示开关
#   config_hot_watch       沪深飙升「极有可能大涨」独立区配置
#   config_historical      「v1 回捞」独立区配置（前 N 交易日 v1 产出的回调筛选）
#   config_tactics        盘中操作纪律(intraday tactics)参数
#
# 消费方若新增常量，请放到对应子模块并在其 __all__ 登记；本文件只负责聚合导出。

from scanner.categories import (  # noqa: F401,E402  (re-export)
    CAT_DISPLAY_PRIORITY,
    NEXTDAY_CAT_PRIORITY,
    SUGGEST_BY_CAT,
)
from scanner.config_categories import *  # noqa: F401,F403
from scanner.config_core import *  # noqa: F401,F403
from scanner.config_historical import *  # noqa: F401,F403
from scanner.config_hot_watch import *  # noqa: F401,F403
from scanner.config_risk import *  # noqa: F401,F403
from scanner.config_scoring import *  # noqa: F401,F403
from scanner.config_sources import *  # noqa: F401,F403
from scanner.config_tactics import *  # noqa: F401,F403

# 子模块 re-export：交易日历、权重表、类别注册表（此前已拆出，沿用同一约定）
from scanner.holidays import HOLIDAYS, HOLIDAYS_FILE  # noqa: F401  (re-export)
from scanner.weights import (  # noqa: F401  (re-export)
    MOMENTUM_WEIGHTS,
    NEW_FACE_WEIGHTS,
    REBOUND_WEIGHTS,
    SHORT_TERM_WEIGHTS,
)

# DB_PATH 在 import 时求值（分支隔离 RTS_DB_PATH 覆盖）；置于聚合层末尾以便 reload
# scanner.config 时重新读取环境变量（test_database.test_env_override_takes_effect 依赖此语义）。
DB_PATH = resolve_db_path()  # noqa: F405  (来自 config_core 的 `import *`)
