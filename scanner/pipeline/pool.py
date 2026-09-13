"""候选池构建阶段的纯函数（从 `orchestrator.scan_with_raw` 等价抽出，2026-09-13）。

**等价纪律**：见 `scanner/pipeline/__init__.py`。改动本模块后必须跑
`python scripts/golden_scan.py --date <四个日期>`，全部退出码 0 才算等价。
"""

from __future__ import annotations

from typing import Any

from scanner.config import MAX_MARKET_CAP, MAX_STOCK_PRICE, YI
from scanner.models import StockInfo


def filter_by_market_cap(
    gem_stocks: list[StockInfo], market_caps: dict[str, dict]
) -> tuple[list[StockInfo], int]:
    """按价格/市值硬门过滤候选池，返回 (过滤后列表, 大市值被滤掉的数量)。

    **有副作用（与原实现一致，不可省略）**：对每只票原地回填
    `s.current`（现价，仅当原本为 0 且市值数据带 current 时）与
    `s.market_cap`（流通市值优先，转亿元）。下游 `build_current_quotes`、
    小叶美规则、`s.market_cap` 展示都依赖这两个字段已回填。

    过滤顺序与原实现一致（价格门在前，市值门在后），因为 `filtered_large_cap`
    只统计「过了价格门但被市值门拦下」的票——顺序反了计数器就错。
    """
    filtered: list[StockInfo] = []
    filtered_large_cap = 0
    for s in gem_stocks:
        cap_data = market_caps.get(s.symbol, {})
        cap_current = cap_data.get("current", 0)
        if cap_current and s.current == 0:
            s.current = cap_current
        cmc = cap_data.get("circ_market_cap") or cap_data.get("market_cap", 0)
        if cmc > 0:
            s.market_cap = cmc / YI  # 转亿元（流通市值优先）
        if s.current > 0 and s.current > MAX_STOCK_PRICE:
            continue
        mc = cap_data.get("market_cap", 0)
        if mc > 0 and mc > MAX_MARKET_CAP:
            filtered_large_cap += 1
            continue
        filtered.append(s)
    return filtered, filtered_large_cap


def report_market_cap_availability(
    market_caps: dict[str, dict], mc_syms: list[str], used_stale_mc: bool
) -> None:
    """打印市值数据可用性提示（原 scan_with_raw L194-201 的三态分支）。

    三态语义（2026-08-20 定）：
      - 实时取到 → 静默（已落库，正常）；
      - 全失败但陈旧缓存兜底命中 → `[~]` 降级提示，小而美规则**仍基于旧值生效**；
      - 全失败且无任何陈旧缓存 → `[!]` 告警，小而美规则**不生效**。

    单独抽出来是因为它是纯 IO（print），放在纯函数里会让 `filter_by_market_cap`
    不可测；而这三态的文案差异是线上排障时唯一能区分「降级」和「失效」的依据。
    """
    if used_stale_mc:
        print(f"  [~] 市值实时查询失败，已回退陈旧缓存({len(market_caps)}只)"
              f"——小叶美规则基于旧市值生效")
    elif not market_caps and mc_syms:
        print("  [!] 警告: 市值数据全失败且无陈旧缓存，小而美规则暂不生效")


def build_current_quotes(market_caps: dict[str, dict]) -> dict[str, dict[str, Any]]:
    """从市值/行情字典构建实时行情快照（原 scan_with_raw L592-599）。

    `current <= 0` 的条目（停牌、字段缺失被强转 0）**不入快照**——与主循环补拉路径
    同口径：2026-08-14 的 fail-open 修复只堵了补拉路径，此处此前仍会把 0.00% 当真实
    涨幅喂给 display / mark_reversed。保留这个过滤条件即保留那次修复。
    """
    return {
        sym: {
            "percent": d.get("percent", 0.0),
            "current": d.get("current", 0.0),
            "high_pct": d.get("high_pct"),
        }
        for sym, d in market_caps.items()
        if d.get("current")
    }
