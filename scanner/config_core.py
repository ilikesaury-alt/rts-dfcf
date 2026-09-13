# scanner/config_core.py — 基础设施与运行时配置（原 config.py 的一部分）
# 2026-09-13 从 config.py 拆出：时区/时间、数据源策略、请求超时、路径、环境变量读取、
# 交易时段、早盘/尾盘加分。定义集中在此，由 scanner/config.py 统一 re-export，
# 保持 `from scanner.config import ...` 导入路径不变。

import os
from datetime import datetime, timedelta, timezone
from datetime import time as dtime

BEIJING_TZ = timezone(timedelta(hours=8), name="CST")


def now_beijing() -> datetime:
    """Return current datetime in Beijing timezone (UTC+8)."""
    return datetime.now(BEIJING_TZ)


# 数据源策略："auto"（雪球优先+AKShare兜底）/ "xueqiu" / "akshare"
# 通过环境变量 RTS_DATA_SOURCE 可覆盖，用于雪球反爬封禁时强制切换。
DATA_SOURCE = os.environ.get("RTS_DATA_SOURCE", "auto")

REFRESH_INTERVAL = 60
REQUEST_TIMEOUT = 15
# 连接超时（秒）：与 REQUEST_TIMEOUT 组成 (connect, read) 元组，
# 避免连不上的主机在 connect 阶段长时间挂起拖垮整个扫描周期。
REQUEST_CONNECT_TIMEOUT = 5
NEW_FACE_LOOKBACK_DAYS = 3

# 单轮 K 线串行拉取 deadline（秒）：超过即停止补拉，剩余票回退旧缓存。
# 防止 API 故障时串行重试让单轮扫描假死数十分钟。
KLINE_FETCH_DEADLINE = 45

# 收益回填窗口（自然日，2026-08-30）：backfill_outcomes 的默认扫描范围。
# 实时扫描每个刷新周期都调用它，此前是全表重算（SELECT 全部 recommendations
# + 逐 symbol 全量 K 线）—— 库里 3904 条推荐 / 942 只票，随数据增长线性拖长
# 扫描周期。超过窗口的历史行 K 线早已定稿、不会再变，重算纯属浪费。
# 需要全量重算（修数 / 迁移）时显式传 since_days=0。
BACKFILL_OUTCOMES_WINDOW_DAYS = 45

# 分时数据（分时强度/开盘强度/实时量比）单相拉取 deadline（秒）。
# minute API 挂死时单只请求最坏 ~48s（15s×3 重试），40 只候选 6 线程并发
# 会让 as_completed 无限等待最长 ~5 分钟；加 deadline 后超时部分降级为
# 无分时信号（None），与 K 线 KLINE_FETCH_DEADLINE 的限时语义对齐。
MINUTE_FETCH_PHASE_DEADLINE = 30

# 分时兜底全阶段总预算（秒，2026-08-17 审查新增）：K 线补拉失败时的分时今日 bar 兜底
# （minute_bar.merge_minute_today_bar）对每只补拉失败票 join(TODAY_BAR_MINUTE_TIMEOUT=8s) 串行，
# 单只限时存在但 N 只串行叠加无总量上限——API 整体故障时 100 只 × 8s = 800s 停滞，
# 违反"单轮扫描有界"承诺（KLINE_FETCH_DEADLINE 只包住拉取阶段，不含兜底阶段）。
# 给整个兜底阶段设总预算，超时即停止剩余票兜底（维持旧缓存回退），与
# KLINE_FETCH_DEADLINE / MINUTE_FETCH_PHASE_DEADLINE 的限时语义对齐。
MINUTE_FALLBACK_PHASE_DEADLINE = 30

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://xueqiu.com/",
}

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# DB 路径：默认仓库根 scanner.db；环境变量 RTS_DB_PATH 可覆盖（分支隔离用）。
# 实验分支（如 redesign-pick-gate）可设 RTS_DB_PATH 指向独立库，避免其改动
# （excluded 标记/新表/新列）写入主库后切回 master 时污染展示与回测口径。
# 解析逻辑留在 config_core（子模块），但绑定 DB_PATH 在 config 聚合层（见 config.py），
# 以便 reload scanner.config 时重新读取 RTS_DB_PATH 环境变量（测试/分支隔离依赖此语义）。
LOG_DIR = os.path.join(BASE_DIR, "logs")


def resolve_db_path() -> str:
    """解析 DB 路径：默认仓库根 scanner.db；RTS_DB_PATH 可覆盖（分支隔离用）。"""
    return os.environ.get("RTS_DB_PATH") or os.path.join(BASE_DIR, "scanner.db")

# 外来分支排除标记前缀（2026-09-03）：redesign-pick-gate 分支的 redesign_gate 会在
# 共享主库写入 reason='redesign:*' 的 excluded=1 标记（L0 池窄→撤销当日全部推荐），
# 切回 master 后这些标记会遮蔽核心低吸/回马枪区。master 启动（init_db）时自愈清除。
# ⚠ 若日后把 redesign gate 合入 master，务必同步移除此清陳逻辑（否则每次启动会撤销
# gate 的过滤结果）。
FOREIGN_EXCLUDED_PREFIXES = ("redesign:",)


# ── 环境变量读取助手 ──
# 开关：环境变量可覆盖（RTS_ENABLE_ZT_POOL / RTS_ENABLE_FUND_FLOW），0/1/false/true
def _env_flag(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    """浮点阈值的环境变量读取：缺省/脏值回退 default（不让阈值解析炸掉启动）。"""
    v = os.environ.get(name)
    if v is None or not v.strip():
        return default
    try:
        return float(v)
    except ValueError:
        return default


def _env_str(name: str, default: str) -> str:
    v = os.environ.get(name)
    return v.strip() if v and v.strip() else default


def pipeline_mode() -> str:
    """管道模式（RTS_PIPELINE=v1|v2，默认 v2）。

    2026-09-02 起双跑：v1 五桶与 v2 池管道在 scan_with_raw 内无条件都执行、各自
    落库（recommendations 按 (date, symbol, category) 并存），显示层同屏双区输出
    （v1 主表 + v2 池选区）——本开关不再切换任何行为，仅为兼容保留。
    单独关闭 v2 管道（回滚杠杆）用 RTS_ENABLE_POOL=0。
    """
    return (os.environ.get("RTS_PIPELINE", "v2") or "v2").strip().lower()


ENABLE_POOL_PIPELINE = _env_flag("RTS_ENABLE_POOL", True)  # v2 池管道开关（双跑下的回滚杠杆）


# 交易时段（用于 is_trading_time 判断盘中/盘后）
MORNING_START = dtime(9, 30)
MORNING_END = dtime(11, 30)
AFTERNOON_START = dtime(13, 0)
AFTERNOON_END = dtime(15, 0)

# 早盘/尾盘加分（时间维度）：早盘噪音轻度抑制、尾盘加速确认加分。
EARLY_TRADE_CUTOFF = 10 * 60 + 30  # 10:30
LATE_TRADE_START = 14 * 60  # 14:00
# -5→-2：原 -5 会把刚过 MIN_SCORE 的早盘票压到门槛下，导致 9:30-10:30 票迟迟不推。
# -2 保留早盘噪音轻度抑制，但不再系统性压杀早盘异动。
EARLY_BONUS = -2
LATE_BONUS = 3

__all__ = [
    "BEIJING_TZ",
    "now_beijing",
    "DATA_SOURCE",
    "REFRESH_INTERVAL",
    "REQUEST_TIMEOUT",
    "REQUEST_CONNECT_TIMEOUT",
    "NEW_FACE_LOOKBACK_DAYS",
    "KLINE_FETCH_DEADLINE",
    "BACKFILL_OUTCOMES_WINDOW_DAYS",
    "MINUTE_FETCH_PHASE_DEADLINE",
    "MINUTE_FALLBACK_PHASE_DEADLINE",
    "HEADERS",
    "BASE_DIR",
    "resolve_db_path",
    "LOG_DIR",
    "FOREIGN_EXCLUDED_PREFIXES",
    "_env_flag",
    "_env_float",
    "_env_str",
    "pipeline_mode",
    "ENABLE_POOL_PIPELINE",
    "MORNING_START",
    "MORNING_END",
    "AFTERNOON_START",
    "AFTERNOON_END",
    "EARLY_TRADE_CUTOFF",
    "LATE_TRADE_START",
    "EARLY_BONUS",
    "LATE_BONUS",
]
