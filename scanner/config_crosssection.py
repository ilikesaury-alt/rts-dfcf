# scanner/config_crosssection.py — 创业板全集横截面异动观察层配置
# 2026-11-13 从「与雪球飙升榜解耦的横截面扫描」需求新增。定义集中在此，由
# scanner/config.py 统一 re-export，保持 `from scanner.config import ...` 路径不变。
#
# ⚠ 本层是**并列观察层**：不写 B 段冻结表（offboard_*）、不进 recommendations、
# 不改任何回测口径。它的唯一产物是 crosssection_signal 表与终端 `≡` 段。

import os

from scanner.config_core import REFRESH_INTERVAL, _env_float
from scanner.config_hot_watch import OFFBOARD_MIN_AMOUNT

# 总开关（RTS_CROSSSECTION=0 一键关；关掉后主循环不取数、不打印、不写库）。
CS_ENABLED = os.environ.get("RTS_CROSSSECTION", "1") != "0"

# 同一交易日内两次快照的最小间隔（秒）。主循环 60s 一轮，取 55 让每轮都刷新；
# 若把 interval 调到更短，这个值就是「最多多久取一次东财」的节流阀。
CS_TTL_SEC = _env_float("RTS_CROSSSECTION_TTL_SEC", 55.0)

# 名义单轮秒数：只用于把「N 轮窗口的累计涨跌幅」折算成 N 分钟窗口的等效值。
# **不假定每轮真的 60s** —— 实际经过秒数由相邻快照的墙钟差逐轮累加，见
# crosssection._speed_pct；这里仅定义 1/3/5 轮 = 1/3/5 分钟的展示口径。
CS_NOMINAL_ROUND_SEC = float(REFRESH_INTERVAL)

# 环形缓冲深度：8 轮足够算 5 轮差分（speed_5 / amount_surge 的历史需求）并留余量。
CS_BUFFER_ROUNDS = 8

# 预热轮数：不满 6 轮不出信号。speed_5 需要「当前 + 5 轮前」共 6 份快照；
# 60s 一轮 ⇒ 开盘后约 6 分钟开始产出。
CS_WARMUP_ROUNDS = 6

# 复合分横截面分位阈值：只保留当轮前 5% 的榜外票。分位按「严格低于本票的
# 有效行占比」算（并列取保守侧），n=2287 时约 115 只/轮。
CS_P95 = 0.95

# 终端展示条数（落库仍是全部 p95+ 信号，不受此限）。
CS_TOP_N = 8

# 落库保留轮次：每轮最多 CS_TOP_N 量级的行，留 50 轮 ≈ 一个交易日，
# 与 local_view_cache._SNAPSHOT_LIMIT 同款「只留最近」语义。
CS_KEEP_ROUNDS = 50

# 跨源价差容忍度（%）：榜内票的雪球现价/涨幅与本层东财快照的中位偏差超过它
# 即告警（只告警不阻断——两源本就可能有秒级时差）。
CS_DIVERGENCE_TOL = 1.0

# 有效成交额下限（元）：**派生**自 B 段 OFFBOARD_MIN_AMOUNT（3000 万），不写字面量。
# 与 B 段同因：成交额过低的票少量资金即可操纵分时，指标无参考价值。
CS_MIN_AMOUNT = OFFBOARD_MIN_AMOUNT

# 陈旧判定：本轮 ≥95% 有效行的 (price, volume) 与上一轮完全相同 ⇒ 上游快照
# 未更新（东财 f124 也未变的重复快照），该轮不产出信号。
CS_STALE_RATIO = 0.95
# 陈旧告警限频（秒）：长跑时上游卡住会每轮都陈旧，不能每 60s 刷一条 warning。
CS_STALE_WARN_INTERVAL_SEC = 300.0

# 复合分权重（合计 1.0）。方向：短线异动 = 最近 1 轮即时性 + 3 轮趋势 +
# 成交额增量 + 量比。speed_5 不直接进分（与 speed_3 高度共线，只作展示）。
CS_SPEED_1_WEIGHT = 0.35
CS_SPEED_3_WEIGHT = 0.30
CS_AMOUNT_SURGE_WEIGHT = 0.20
CS_VOL_RATIO_WEIGHT = 0.15

__all__ = [
    "CS_ENABLED",
    "CS_TTL_SEC",
    "CS_NOMINAL_ROUND_SEC",
    "CS_BUFFER_ROUNDS",
    "CS_WARMUP_ROUNDS",
    "CS_P95",
    "CS_TOP_N",
    "CS_KEEP_ROUNDS",
    "CS_DIVERGENCE_TOL",
    "CS_MIN_AMOUNT",
    "CS_STALE_RATIO",
    "CS_STALE_WARN_INTERVAL_SEC",
    "CS_SPEED_1_WEIGHT",
    "CS_SPEED_3_WEIGHT",
    "CS_AMOUNT_SURGE_WEIGHT",
    "CS_VOL_RATIO_WEIGHT",
]
