"""
Backtest framework for limit_up_scanner strategy.

Usage:
    python backtest.py                           # Run with default params
    python backtest.py --live                    # Fetch missing K-line from API for fwd returns
    python backtest.py --optimize                # Grid search for optimal params
    python backtest.py --params custom.json      # Load params from JSON file
"""

import argparse
import copy
import json

from scanner.backtest import run_backtest, report, run_grid_search
from scanner.backtest.engine import _deep_merge, DEFAULT_PARAMS
from scanner.api import make_session


def main():
    parser = argparse.ArgumentParser(description="limit_up_scanner 回测框架")
    parser.add_argument("--optimize", "-o", action="store_true", help="网格搜索最优参数")
    parser.add_argument("--params", "-p", type=str, help="从JSON文件加载参数")
    parser.add_argument("--db", type=str, default="scanner.db", help="数据库路径")
    parser.add_argument("--live", action="store_true",
                        help="通过API补充缺失的K线数据以计算前向收益")
    args = parser.parse_args()

    session = None
    if args.live:
        print("Connecting to Xueqiu API for live data...")
        session = make_session()

    if args.optimize:
        run_grid_search(args.db)
    else:
        params = copy.deepcopy(DEFAULT_PARAMS)
        if args.params:
            with open(args.params, encoding="utf-8") as f:
                custom = json.load(f)
            _deep_merge(params, custom)
        new_recs, momentum_recs = run_backtest(params, args.db, session, live=args.live)
        all_recs = new_recs + momentum_recs
        report(all_recs, new_recs, momentum_recs, params)
        if args.live:
            print("  Tip: 使用 --live 会调用雪球API，注意频率限制")


if __name__ == "__main__":
    main()
