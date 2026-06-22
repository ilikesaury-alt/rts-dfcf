import json
import sqlite3
from datetime import date, timedelta

_NEW_STYLE_PREFIXES = ("new_face_", "momentum_")
_MIN_SAMPLE_SIZE = 15


def _is_new_style_breakdown(dims: dict) -> bool:
    return any(k.startswith(_NEW_STYLE_PREFIXES) for k in dims)


def _winsorize(values: list[float], lower: float = 0.01, upper: float = 0.99) -> list[float]:
    if len(values) < 4:
        return values
    sorted_v = sorted(values)
    lo = sorted_v[int(len(sorted_v) * lower)]
    hi = sorted_v[min(len(sorted_v) - 1, int(len(sorted_v) * upper))]
    return [max(lo, min(v, hi)) for v in values]


def dimension_ic(conn: sqlite3.Connection, window_days: int = 30) -> dict:
    """Compute Information Coefficient (rank correlation) per dimension.

    For each scoring dimension, compute the rank correlation between the
    dimension score and forward returns at 1d / 3d / 5d horizons.  A
    positive IC means the dimension predicts returns in the expected
    direction.

    Returns:
        dict[dimension_name -> {
            ic, ic_1d, ic_3d, ic_5d,      # rank correlation per horizon
            count,                          # sample count
            avg_score,                      # avg dimension contribution
            avg_return_1d, avg_return_3d, avg_return_5d,
        }]
        `ic` is an alias for `ic_1d` for backward compatibility.
    """
    cutoff = (date.today() - timedelta(days=window_days)).isoformat()

    rows = conn.execute("""
        SELECT score_breakdown, next_day_pct, fwd_3d, fwd_5d
        FROM recommendations
        WHERE next_day_pct IS NOT NULL
          AND score_breakdown IS NOT NULL
          AND score_breakdown != ''
          AND category NOT IN ('old_face')
          AND date >= ?
    """, (cutoff,)).fetchall()

    dim_scores: dict[str, list[tuple[float, float, float | None, float | None]]] = {}

    for row in rows:
        breakdown_json, r1d, r3d, r5d = row
        try:
            dims = json.loads(breakdown_json)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(dims, dict):
            continue
        if not _is_new_style_breakdown(dims):
            continue
        for dim, score in dims.items():
            if dim not in dim_scores:
                dim_scores[dim] = []
            dim_scores[dim].append((float(score), float(r1d), r3d, r5d))

    result = {}
    for dim, pairs in dim_scores.items():
        if len(pairs) < _MIN_SAMPLE_SIZE:
            continue

        scores = [p[0] for p in pairs]
        r1d = [p[1] for p in pairs]
        avg_score = sum(scores) / len(scores)

        def _ic_for(scores, returns):
            valid = [(s, r) for s, r in zip(scores, returns) if r is not None]
            if len(valid) < _MIN_SAMPLE_SIZE:
                return None, None
            sc, rt = zip(*valid)
            clipped = _winsorize(list(rt))
            return _spearman_rho(sc, clipped), sum(rt) / len(rt)

        ic_1d, avg_r1d = _ic_for(scores, r1d)
        ic_3d, avg_r3d = _ic_for(scores, [p[2] for p in pairs])
        ic_5d, avg_r5d = _ic_for(scores, [p[3] for p in pairs])

        entry = {
            "ic": round(ic_1d, 3) if ic_1d is not None else None,
            "ic_1d": round(ic_1d, 3) if ic_1d is not None else None,
            "ic_3d": round(ic_3d, 3) if ic_3d is not None else None,
            "ic_5d": round(ic_5d, 3) if ic_5d is not None else None,
            "count": len(pairs),
            "avg_score": round(avg_score, 1),
            "avg_return_1d": round(avg_r1d, 3) if avg_r1d is not None else None,
            "avg_return_3d": round(avg_r3d, 3) if avg_r3d is not None else None,
            "avg_return_5d": round(avg_r5d, 3) if avg_r5d is not None else None,
        }
        result[dim] = entry

    sort_key = lambda x: -abs(x[1].get("ic") or 0)
    return dict(sorted(result.items(), key=sort_key))


def weekly_report(conn: sqlite3.Connection, week_start: str, week_end: str) -> str:
    """Generate a plain-text weekly performance report."""
    lines = []
    lines.append(f"\u258e\u258e \u5468\u62a5 ({week_start} ~ {week_end})")
    lines.append("\u2501" * 50)

    # Overall stats
    row = conn.execute("""
        SELECT category, COUNT(*) as total,
               SUM(CASE WHEN next_day_pct > 0 THEN 1 ELSE 0 END) as wins,
               AVG(next_day_pct) as avg_ret
        FROM recommendations
        WHERE date >= ? AND date <= ?
          AND next_day_pct IS NOT NULL
        GROUP BY category
    """, (week_start, week_end)).fetchall()

    all_total = sum(r[1] for r in row) if row else 0
    all_wins = sum(r[2] for r in row) if row else 0
    lines.append(f"  \u63a8\u8350\u603b\u6570: {all_total}")
    if all_total > 0:
        lines.append(f"  \u80dc\u7387: {all_wins}/{all_total} ({all_wins*100//all_total}%)")

    for cat_label, cat_key in [("\u65b0\u9762\u5b54", "new_face"), ("\u52a8\u91cf\u5ef6\u7eed", "momentum")]:
        r = [x for x in row if x[0] == cat_key]
        if not r:
            continue
        t, w, a = r[0][1], r[0][2], r[0][3]
        wr = f"{w}/{t} ({w*100//max(t,1)}%)"
        avg = f"{a:+.2f}%" if a is not None else "N/A"
        lines.append(f"  {cat_label}: {t}\u6b21 {wr} \u5747\u6536\u76ca{avg}")

    # IC analysis
    ic_data = dimension_ic(conn, window_days=max(7, all_total // 5))
    if ic_data:
        lines.append("")
        lines.append("  \u7ef4\u5ea6\u6548\u80fd (IC):")
        for dim, info in list(ic_data.items())[:8]:
            ic1 = info.get("ic")
            ic3 = info.get("ic_3d")
            ic5 = info.get("ic_5d")
            ic3_str = f" 3d{ic3:+.3f}" if ic3 is not None else ""
            ic5_str = f" 5d{ic5:+.3f}" if ic5 is not None else ""
            icon = "\u2705" if ic1 is not None and ic1 > 0.1 else ("\u26a0\ufe0f" if ic1 is not None and ic1 > -0.05 else "\u274c")
            ic1_str = f"{ic1:+.3f}" if ic1 is not None else "N/A"
            lines.append(f"    {icon} {dim}: 1dIC={ic1_str}{ic3_str}{ic5_str} ({info['count']}\u6b21) \u5747\u5206{info['avg_score']:+.0f} 1d\u5747\u6536\u76ca{info['avg_return_1d']:+.2%}")

    # Best / worst recs
    best = conn.execute("""
        SELECT name, category, score, next_day_pct
        FROM recommendations
        WHERE date >= ? AND date <= ? AND next_day_pct IS NOT NULL
        ORDER BY next_day_pct DESC LIMIT 3
    """, (week_start, week_end)).fetchall()
    if best:
        lines.append("")
        lines.append("  \u6700\u4f73\u63a8\u8350:")
        for name, cat, score, ret in best:
            lines.append(f"    {name} ({cat}) \u8bc4\u5206{score} +1d{ret:+.2f}%")

    worst = conn.execute("""
        SELECT name, category, score, next_day_pct
        FROM recommendations
        WHERE date >= ? AND date <= ? AND next_day_pct IS NOT NULL
        ORDER BY next_day_pct ASC LIMIT 3
    """, (week_start, week_end)).fetchall()
    if worst:
        lines.append("  \u6700\u5dee\u63a8\u8350:")
        for name, cat, score, ret in worst:
            lines.append(f"    {name} ({cat}) \u8bc4\u5206{score} +1d{ret:+.2f}%")

    lines.append("")
    return "\n".join(lines)


def _spearman_rho(xs, ys):
    """Spearman rank correlation coefficient."""
    n = len(xs)
    if n < 3:
        return 0.0

    x_ranks = _rank(sorted(xs), xs)
    y_ranks = _rank(sorted(ys), ys)

    d_sq = sum((xr - yr) ** 2 for xr, yr in zip(x_ranks, y_ranks))
    return 1 - (6 * d_sq) / (n * (n * n - 1))


def _rank(sorted_vals, original):
    """Assign ranks (1-based, averaged for ties)."""
    rank_map = {}
    i = 0
    while i < len(sorted_vals):
        j = i
        while j < len(sorted_vals) and sorted_vals[j] == sorted_vals[i]:
            j += 1
        avg_rank = (i + 1 + j) / 2
        for k in range(i, j):
            rank_map[sorted_vals[k]] = avg_rank
        i = j
    return [rank_map[v] for v in original]
