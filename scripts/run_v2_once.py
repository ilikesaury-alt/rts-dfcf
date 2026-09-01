#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""一次性跑 v2 管道（池→排雷→低吸匹配），打印 pool_pick 推荐列表。

与 unified_scanner.py 主循环区别：不进入交互式刷新循环、不推送飞书、不落库
recommendations（recommendations 仍由主循环负责，避免覆盖当日 v1 落库）。
v2 内部会写 pool_log / watch 符号（均为 upsert，幂等，可重复跑）。

用法：
    python scripts/run_v2_once.py                 # 默认 v2 管道
    RTS_PIPELINE=v1 python scripts/run_v2_once.py # 对照 v1
"""

import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

os.environ.setdefault("RTS_PIPELINE", "v2")  # 必须在导入 scanner.config 之前设定

if sys.platform == "win32":
    _r = getattr(sys.stdout, "reconfigure", None)
    if callable(_r):
        _r(encoding="utf-8")

from scanner.config import pipeline_mode  # noqa: E402
from scanner.data_source import get_adapter  # noqa: E402
from scanner.database import init_db  # noqa: E402


def _fmt_pct(v):
    try:
        return f"{float(v):+.2f}%"
    except (TypeError, ValueError):
        return "—"


def main() -> None:
    print(f"管道模式: {pipeline_mode()}")
    conn = init_db()
    adapter = get_adapter()

    xq_raw = adapter.fetch_biaosheng()
    if not xq_raw:
        print("[!] 飙升榜为空，无法运行")
        return
    print(f"飙升榜抓取: {len(xq_raw)} 只")

    from scanner.orchestrator import scan_with_raw

    res = scan_with_raw(xq_raw, conn, adapter)
    conn.close()

    pool = list(res.pool_picks)
    comeback = list(res.comeback)
    # v2 主表口径：直接按今日涨幅降序
    pool.sort(key=lambda c: -(c.stock.percent if c.stock.percent is not None else -1e9))
    comeback.sort(key=lambda c: -(c.stock.percent if c.stock.percent is not None else -1e9))

    print(f"\n{'=' * 78}")
    print(f"v2 池选推荐列表（pool_pick）：共 {len(pool)} 只")
    print(f"{'=' * 78}")
    if not pool:
        print("  今日池选为空（全部命中危险信号被排除，或池中无达标票）。")
    for i, c in enumerate(pool, 1):
        s = c.stock
        dims = c.kline.dimensions if c.kline else {}
        labels_raw = dims.get("dip_labels")
        labels = [str(x) for x in labels_raw] if isinstance(labels_raw, (list, tuple)) else []
        label_str = "/".join(labels) if labels else "无标签"
        extra = []
        if dims.get("bias20") is not None:
            extra.append(f"bias20={dims['bias20']:.1f}")
        if dims.get("acc5") is not None:
            extra.append(f"acc5={dims['acc5']:+.1f}%")
        if dims.get("rank_trend") is not None:
            extra.append(f"rank_trend={dims['rank_trend']:+d}")
        if s.market_cap:
            extra.append(f"市值≈{s.market_cap / 1e9:.0f}亿")
        print(f"{i:>2}. {s.name}({s.symbol}) {_fmt_pct(s.percent)} 榜排{s.rank if s.rank else '-'} | 标签:{label_str}")
        print(f"      {' · '.join(extra) if extra else '—'}")

    if comeback:
        print(f"\n{'=' * 78}")
        print(f"回马枪（comeback）：共 {len(comeback)} 只")
        print(f"{'=' * 78}")
        for i, c in enumerate(comeback, 1):
            s = c.stock
            print(f"{i:>2}. {s.name}({s.symbol}) {_fmt_pct(s.percent)} [{c.comeback_variant}]")

    print(f"\n小结: 池选 {len(pool)} + 回马枪 {len(comeback)}")


if __name__ == "__main__":
    main()
