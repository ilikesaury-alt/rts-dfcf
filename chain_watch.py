#!/usr/bin/env python3
"""
产业链趋势观察池 — 独立监控工具。

从雪球飙升榜实时检测当前活跃产业链，结合 K 线趋势评分输出观察池。

Usage:
    python chain_watch.py                     # 单次运行
    python chain_watch.py --interval 300      # 每5分钟刷新
    python chain_watch.py --interval 0        # 仅一次（默认）
"""

import argparse
import sys

from scanner.chain_watch import main_loop


def main():
    parser = argparse.ArgumentParser(description="产业链趋势观察池")
    parser.add_argument("--interval", type=int, default=0,
                        help="刷新间隔(秒), 0=单次运行 (默认0)")
    args = parser.parse_args()

    try:
        main_loop(interval=args.interval)
    except KeyboardInterrupt:
        print("\n  退出")
        sys.exit(0)


if __name__ == "__main__":
    main()
