"""盘中操作纪律标签（从 `orchestrator.scan_with_raw` 等价抽出，2026-09-13）。

**等价纪律**：见 `scanner/pipeline/__init__.py`。改动本模块后必须跑
`python scripts/golden_scan.py --date <四个日期>`，全部退出码 0 才算等价。
"""

from __future__ import annotations

from scanner.intraday_tactics import stock_actions
from scanner.utils import EXTERNAL_FAILURES


def attach_tactic_tags(all_candidates: list, current_quotes: dict, klines: dict) -> None:
    """给每只候选挂上 12 条操盘纪律的个股标签（原地写 `c.tactic_tags`）。

    **逐票 try/except**：一只票的脏数据只跳过该票，不再静默放弃全部票的标签
    （2026-08-31 审查修复）。fail-open：单票异常不阻塞扫描，不影响评分/排序/落库。
    异常时 `c.tactic_tags` 保留调用前的值（不会写空），与原实现一致。

    `high_pct` 来自 quote 的 high/昨收（`api._quote_high_pct`），是真实日内最高涨幅；
    `None` = 无数据 → 纪律内部 fail-open 跳过依赖项。
    """
    for c in all_candidates:
        try:
            quote = current_quotes.get(c.stock.symbol, {})
            high_pct = quote.get("high_pct") if quote else None
            c.tactic_tags = stock_actions(
                c, now=None, kline_bars=klines.get(c.stock.symbol), high_pct=high_pct
            )
        except EXTERNAL_FAILURES as e:
            print(f"  [!] 盘中操作纪律计算失败 {c.stock.symbol}（跳过）: {type(e).__name__}: {e}")
