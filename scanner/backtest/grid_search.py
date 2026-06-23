import copy
import os
import sys
from itertools import product

from scanner.config import NEW_FACE_WEIGHTS, MOMENTUM_WEIGHTS, PULLBACK_WEIGHTS
from .engine import run_backtest, DEFAULT_NEW_FACE_MIN, DEFAULT_MOMENTUM_MIN, DEFAULT_PULLBACK_MIN
from .reporting import _avg, _sharpe


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
    "pullback.today_neg3_neg1": [10, 15, 20],
    "pullback.accum_10_20": [15, 18, 22],
    "pullback.ma_support": [8, 12, 16],
    "pullback.no_crash": [10, 13, 16],
    "pullback.vol_healthy": [8, 12, 16],
}


def _set_param(d, path, value):
    keys = path.split(".")
    obj = d
    for k in keys[:-1]:
        obj = obj[k]
    obj[keys[-1]] = value


def _win_rate(recs):
    """Calculate 1d forward return win rate."""
    fwd = [r.fwd_1d for r in recs if r.fwd_1d is not None]
    if not fwd:
        return 0.0
    return sum(1 for v in fwd if v > 0) / len(fwd)


def _avg_fwd_1d(recs):
    """Calculate average 1d forward return."""
    fwd = [r.fwd_1d for r in recs if r.fwd_1d is not None]
    return _avg(fwd) if fwd else 0.0


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
        pb_overrides = {}
        for k, v in zip(keys, combo):
            strategy, weight_key = k.split(".")
            if strategy == "new_face":
                nf_overrides[weight_key] = v
            elif strategy == "momentum":
                mo_overrides[weight_key] = v
            else:
                pb_overrides[weight_key] = v

        old_stdout = sys.stdout
        sys.stdout = open(os.devnull, "w")
        new_recs, momentum_recs, pb_recs = run_backtest(
            new_face_overrides=nf_overrides or None,
            momentum_overrides=mo_overrides or None,
            pullback_overrides=pb_overrides or None,
            db_path=db_path,
        )
        sys.stdout.close()
        sys.stdout = old_stdout

        all_recs = new_recs + momentum_recs + pb_recs
        total_recs = len(all_recs)
        avg_score = _avg([r.score for r in all_recs]) if all_recs else 0

        # Compute win rate and Sharpe from 1d forward returns
        fwd_1d = [r.fwd_1d for r in all_recs if r.fwd_1d is not None]
        win_rate = _win_rate(all_recs)
        sharpe = _sharpe(fwd_1d) if len(fwd_1d) >= 2 else 0.0
        avg_ret = _avg(fwd_1d) if fwd_1d else 0.0

        results.append({
            "params": combo,
            "total": total_recs,
            "avg_score": avg_score,
            "win_rate": win_rate,
            "sharpe": sharpe,
            "avg_ret_1d": avg_ret,
            "new_count": len(new_recs),
            "momentum_count": len(momentum_recs),
            "pb_count": len(pb_recs),
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
        print(f"\n当前参数: 新{d['new_count']}动量{d['momentum_count']}回调{d['pb_count']} "
              f"avg_score={d['avg_score']:.0f} 胜率={d['win_rate']:.0%} Sharpe={d['sharpe']:.2f}")

    print(f"\nTop 10 按推荐数:")
    for r in results[:10]:
        print(f"  total={r['total']} 新={r['new_count']}动量={r['momentum_count']}回调{r['pb_count']} "
              f"胜率={r['win_rate']:.0%} Sharpe={r['sharpe']:.2f}")
        for k, v in zip(keys, r["params"]):
            print(f"    {k} = {v}")

    print(f"\nTop 10 按均分:")
    for r in sorted(results, key=lambda x: -x["avg_score"])[:10]:
        print(f"  avg_score={r['avg_score']:.0f} total={r['total']} "
              f"胜率={r['win_rate']:.0%} Sharpe={r['sharpe']:.2f}")
        for k, v in zip(keys, r["params"]):
            print(f"    {k} = {v}")

    print(f"\nTop 10 按胜率:")
    for r in sorted(results, key=lambda x: -x["win_rate"])[:10]:
        print(f"  胜率={r['win_rate']:.0%} Sharpe={r['sharpe']:.2f} "
              f"total={r['total']} avg_ret={r['avg_ret_1d']:+.2%}")
        for k, v in zip(keys, r["params"]):
            print(f"    {k} = {v}")

    print(f"\nTop 10 按Sharpe:")
    for r in sorted(results, key=lambda x: -x["sharpe"])[:10]:
        print(f"  Sharpe={r['sharpe']:.2f} 胜率={r['win_rate']:.0%} "
              f"total={r['total']} avg_ret={r['avg_ret_1d']:+.2%}")
        for k, v in zip(keys, r["params"]):
            print(f"    {k} = {v}")

    return results
