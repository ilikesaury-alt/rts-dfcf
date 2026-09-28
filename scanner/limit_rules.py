"""A 股涨跌停幅度单源（S1 制度事实）—— 2026-09-28 从 `hot_watch` 抽出。

## 为什么抽出来

`intraday_tactics` 此前把涨停判成硬编码 `today_pct >= 9.8`（**主板 10% 口径**），
而本仓样本面是**创业板 20%** —— 创业板票涨 9.8% 远未涨停，却会被判成「已封板」，
于是：

  · 规则 6（14:00-14:30 封板 → 💰落袋）在**根本没涨停**的票上误发；
  · 规则 2（高开≥5% 封不住板 → ⬇减半）与规则 1（冲高未封板 → ⬇减仓）的
    「未封板」分支被压制，该发的不发。

制度（S1，可外部复核）：主板 ±10%；创业板（300/301）±20%，2020-08-24 注册制
改革起施行。科创板 ±20%、ST ±5% —— 本仓样本面已剔除（`is_st` /
`filter_gem_stocks`），故只需两档。

`hot_watch` 与 `intraday_tactics` 都 import 本模块，**判定只有一份**；
`hot_watch.limit_pct_for` / `hot_watch.limit_prices` 两个名字仍可从 hot_watch
取到（re-export），既有调用方与测试不受影响。
"""
from __future__ import annotations

import math

from scanner.config_hot_watch import HOT_LIMIT_PCT_GEM, HOT_LIMIT_PCT_MAIN
from scanner.utils import is_gem

__all__ = ["limit_pct_for", "limit_prices", "near_limit_pct"]


def limit_pct_for(code: str) -> float:
    """该代码的当日涨跌幅限制（%）：创业板 20%，主板 10%。

    入参兼容带前缀（"SZ300750"）与纯代码（"300750"）—— 与 `utils.is_gem` 同款。
    """
    return HOT_LIMIT_PCT_GEM if is_gem(code) else HOT_LIMIT_PCT_MAIN


def limit_prices(last_close: float, code: str) -> tuple[float, float]:
    """由昨收推算涨停价 / 跌停价（A 股规则：昨收 ×(1±limit%)，四舍五入到分）。

    batch 行情接口不返回 limit_up/limit_down，但返回 last_close —— 据此推算可与
    接口值逐分对齐，硬排除不依赖可选的 detail 补拉（补拉失败时口径不变）。
    last_close ≤ 0（脏值/缺失）时返回 (0.0, 0.0) —— 调用方按「无法判定」处理，
    不做涨跌停排除（fail-open，宁可放过也不误杀）。
    """
    if last_close <= 0 or not math.isfinite(last_close):
        return 0.0, 0.0
    pct = limit_pct_for(code)
    return round(last_close * (1 + pct / 100.0), 2), round(last_close * (1 - pct / 100.0), 2)


def near_limit_pct(code: str, ratio: float) -> float:
    """「接近涨停」的涨幅线（%）= 板块涨停幅度 × `ratio`。

    `ratio` 取 0.98 时：主板 9.8%（沿用 `intraday_tactics` 原值）、创业板 19.6%。
    用**比例**而非固定百分点，是为了让「接近」的含义随板块制度缩放 ——
    写死 9.8 正是 2026-09-28 修复的那个 bug 的根因。
    """
    return limit_pct_for(code) * ratio
