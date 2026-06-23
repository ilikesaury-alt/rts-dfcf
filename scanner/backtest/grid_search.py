import copy
import os
import sys
from itertools import product

from scanner.config import NEW_FACE_WEIGHTS, MOMENTUM_WEIGHTS
from .engine import run_backtest, DEFAULT_NEW_FACE_MIN, DEFAULT_MOMENTUM_MIN
from .reporting import _avg


GRID_PARAMS = {
    "new_face.today_pct_2_6": [15, 20, 25],
    "new_face.accum_neg5_10": [5, 10, 15],
    "new_face.bottom_confirmed": [5, 10, 15],
    "new_face.vol_rank_combo": [8, 12, 16],
    "new_face.volume_surge": [10, 15, 20],
    "momentum.today_pct_2_6": [20, 26, 32],
    "momentum.accum_10_15": [15, 19, 23],
    "momentum.vol_healthy": [3, 5, 8],
    "momentum.no_crash": [10, 13, 16],
}


def _set_param(d, path, value):
    keys = path.split(".")
    obj = d
    for k in keys[:-1]:
        obj = obj[k]
    obj[keys[-1]] = value


def run_grid_search(db_path="scanner.db", session=None):
    keys, values = zip(*GRID_PARAMS.items())
    results = []

    total = 1
    for v in values:
        total *= len(v)
    print(f"Grid search: {total} combinations over {len(keys)} params\n")

    for i, combo in enumerate(product(*values)):
        nf_overrides = {}
        mo_overrides = {}
        for k, v in zip(keys, combo):
            strategy, weight_key = k.split(".")
            if strategy == "new_face":
                nf_overrides[weight_key] = v
            else:
                mo_overrides[weight_key] = v

        old_stdout = sys.stdout
        sys.stdout = open(os.devnull, "w")
        new_recs, old_recs = run_backtest(
            new_face_overrides=nf_overrides or None,
            momentum_overrides=mo_overrides or None,
            db_path=db_path,
        )
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

    default_tuple = tuple(GRID_PARAMS[k][1] for k in keys)  # middle value as default
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
