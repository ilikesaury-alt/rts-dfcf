"""产业链趋势选股扫描器 — 独立于现有 limit_up_scanner 运行

从雪球飙升榜出发，自上而下：
  1. 判定产业链趋势阶段（爆发/成长/形成/消退）
  2. 拆解产业链节点（上中下游瓶颈）
  3. 筛选卡位最关键的个股

Usage:
    python industry_chain_scanner.py
    python industry_chain_scanner.py 120   # 自定义间隔(秒)
"""

import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from scanner.industry_chain import main_loop

if __name__ == "__main__":
    interval = 300
    if len(sys.argv) > 1:
        try:
            interval = int(sys.argv[1])
        except ValueError:
            print(f"用法: python industry_chain_scanner.py [间隔秒数]")
            sys.exit(1)

    main_loop(interval=interval)
