import copy
import os
import sys
from itertools import product

from .engine import DEFAULT_PARAMS, run_backtest
from .reporting import _avg


GRID_PARAMS = {
    "new_face.min_score": [15, 20, 25],
    "new_face.today_pct.golden_min": [1.0, 2.0, 3.0],
    "new_face.today_pct.golden_max": [5.0, 6.0, 8.0],
    "new_face.accumulated.sweet_max": [10.0, 15.0, 20.0],
    "new_face.accumulated.warn_threshold": [10.0, 15.0, 20.0],
    "new_face.bottom.min_vol_ratio": [1.1, 1.3, 1.5],
}


def _set_param(params, path, value):
    keys = path.split(".")
    obj = params
    for k in keys[:-1]:
        obj = obj[k]
    obj[keys[-1]] = value


def _get_default(path):
    obj = DEFAULT_PARAMS
    for p in path.split("."):
        obj = obj[p]
    return obj


def run_grid_search(db_path="scanner.db", session=None):
    keys, values = zip(*GRID_PARAMS.items())
    results = []

    total = 1
    for v in values:
        total *= len(v)
    print(f"Grid search: {total} combinations over {len(keys)} params\n")

    for i, combo in enumerate(product(*values)):
        params = copy.deepcopy(DEFAULT_PARAMS)
        for k, v in zip(keys, combo):
            _set_param(params, k, v)

        old_stdout = sys.stdout
        sys.stdout = open(os.devnull, "w")
        new_recs, old_recs = run_backtest(params, db_path)
        sys.stdout.close()
        sys.stdout = old_stdout

        all_recs = new_recs + old_recs
        total_recs = len(all_recs)
        avg_score = _avg([r.score for r in all_recs]) if all_recs else 0

        results.append({
            "params": combo,
            "total": total_recs,
            "avg_score": avg_score,
            "new_count": len(new_recs),
            "old_count": len(old_recs),
        })

        if (i+1) % 50 == 0:
            print(f"  Progress: {i+1}/{total}")

    results.sort(key=lambda x: -x["total"])

    print(f"\n{'='*60}")
    print(f"网格搜索结果 ({len(results)}组合) — 需配合 --live 验前向收益")
    print(f"{'='*60}")

    default_tuple = tuple(_get_default(k) for k in keys)
    default_idx = next((i for i, r in enumerate(results)
                        if r["params"] == default_tuple), -1)
    if default_idx >= 0:
        d = results[default_idx]
        print(f"\n当前参数: 新{d['new_count']}旧{d['old_count']} avg_score={d['avg_score']:.0f}")

    print(f"\nTop 10 按推荐数:")
    for r in results[:10]:
        print(f"  total={r['total']} 新={r['new_count']}旧={r['old_count']} avg_score={r['avg_score']:.0f}")
        for k, v in zip(keys, r["params"]):
            print(f"    {k} = {v}")

    print(f"\nTop 10 按均分:")
    for r in sorted(results, key=lambda x: -x["avg_score"])[:10]:
        print(f"  avg_score={r['avg_score']:.0f} total={r['total']} 新={r['new_count']}旧={r['old_count']}")
        for k, v in zip(keys, r["params"]):
            print(f"    {k} = {v}")

    return results
