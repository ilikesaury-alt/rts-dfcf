def _avg(vals):
    return sum(vals) / len(vals) if vals else 0.0


def _std(vals):
    if len(vals) < 2:
        return 0.0
    avg = _avg(vals)
    return (sum((v - avg) ** 2 for v in vals) / (len(vals) - 1)) ** 0.5


def _sharpe(returns):
    if len(returns) < 2 or _std(returns) == 0:
        return 0.0
    trades_per_year = 252
    return _avg(returns) / _std(returns) * (trades_per_year ** 0.5)


def _max_drawdown(returns):
    if not returns:
        return 0.0
    cum = 1.0
    peak = 1.0
    max_dd = 0.0
    for r in returns:
        cum *= (1 + r)
        peak = max(peak, cum)
        dd = (cum - peak) / peak
        max_dd = min(max_dd, dd)
    return max_dd


def _ic(recs):
    scored = [(r.score, r.fwd_1d) for r in recs if r.fwd_1d is not None]
    if len(scored) < 5:
        return 0.0
    scores, returns = zip(*scored)
    n = len(scores)
    avg_s = _avg(scores)
    avg_r = _avg(returns)
    num = sum((s - avg_s) * (r - avg_r) for s, r in zip(scores, returns))
    den = (sum((s - avg_s) ** 2 for s in scores)
           * sum((r - avg_r) ** 2 for r in returns)) ** 0.5
    return num / den if den > 0 else 0.0


def report(all_recs, new_recs, momentum_recs, params):
    print(f"\n{'='*60}")
    print(f"回测报告")
    print(f"{'='*60}")
    print(f"新面孔阈值: {params['new_face']['min_score']}  动量阈值: {params['momentum']['min_score']}")
    print(f"回测天数: {len(set(r.date for r in all_recs))}")
    print(f"推荐总数: {len(all_recs)} (新面孔 {len(new_recs)}, 动量 {len(momentum_recs)})")

    for label, recs in [("新面孔", new_recs), ("动量", momentum_recs)]:
        if not recs:
            print(f"\n{label}: 无推荐")
            continue
        fwd_1d = [r.fwd_1d for r in recs if r.fwd_1d is not None]
        fwd_3d = [r.fwd_3d for r in recs if r.fwd_3d is not None]
        fwd_5d = [r.fwd_5d for r in recs if r.fwd_5d is not None]
        wins_1d = sum(1 for v in fwd_1d if v > 0)
        wins_3d = sum(1 for v in fwd_3d if v > 0)
        wins_5d = sum(1 for v in fwd_5d if v > 0)

        print(f"\n{label} ({len(recs)}次推荐):")
        print(f"  +1d 胜率: {wins_1d}/{len(fwd_1d)} ({wins_1d*100//max(len(fwd_1d),1)}%) 均值: {_avg(fwd_1d):+.2%} Sharpe: {_sharpe(fwd_1d):.2f}")
        print(f"  +3d 胜率: {wins_3d}/{len(fwd_3d)} ({wins_3d*100//max(len(fwd_3d),1)}%) 均值: {_avg(fwd_3d):+.2%}")
        print(f"  +5d 胜率: {wins_5d}/{len(fwd_5d)} ({wins_5d*100//max(len(fwd_5d),1)}%) 均值: {_avg(fwd_5d):+.2%}")
        if fwd_1d:
            print(f"  +1d 最大回撤: {_max_drawdown(fwd_1d):.2%}")

        buckets = [(50, 100), (30, 49), (20, 29), (15, 19)]
        print(f"  评分分层 (+1d均收益):")
        for lo, hi in buckets:
            subset = [r for r in recs if lo <= r.score <= hi]
            if subset:
                rets = [r.fwd_1d for r in subset if r.fwd_1d is not None]
                wins = sum(1 for v in rets if v > 0)
                n = len(rets)
                if n > 0:
                    print(f"    {lo}-{hi}分 ({len(subset)}只): {_avg(rets):+.2%} 胜率 {wins}/{n} ({wins*100//n}%)")
                else:
                    print(f"    {lo}-{hi}分 ({len(subset)}只): N/A")

        print(f"  Top 5 (按评分):")
        for r in sorted(recs, key=lambda x: x.score, reverse=True)[:5]:
            f1 = f"{r.fwd_1d:+.2%}" if r.fwd_1d is not None else "N/A"
            f3 = f"{r.fwd_3d:+.2%}" if r.fwd_3d is not None else "N/A"
            print(f"    {r.date} {r.name} ({r.symbol}) score={r.score} rank={r.rank} +1d={f1} +3d={f3}")

    if new_recs:
        ic_1d = _ic(new_recs)
        print(f"\n  IC (评分 vs +1d收益): {ic_1d:+.3f}")
