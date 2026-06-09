import json
import sqlite3
from statistics import NormalDist
from datetime import date, timedelta


def dimension_ic(conn: sqlite3.Connection, window_days: int = 30) -> dict:
    """Compute Information Coefficient (rank correlation) per dimension.

    For each scoring dimension, compute the rank correlation between the
    dimension score and the forward 1d return.  A positive IC means the
    dimension predicts returns in the expected direction.

    Returns:
        dict[dimension_name -> {ic, count, avg_score, avg_return}]
    """
    cutoff = (date.today() - timedelta(days=window_days)).isoformat()

    rows = conn.execute("""
        SELECT score_breakdown, next_day_pct
        FROM recommendations
        WHERE next_day_pct IS NOT NULL
          AND score_breakdown IS NOT NULL
          AND score_breakdown != ''
          AND date >= ?
    """, (cutoff,)).fetchall()

    dim_scores: dict[str, list[tuple[float, float]]] = {}

    for breakdown_json, fwd_return in rows:
        try:
            dims = json.loads(breakdown_json)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(dims, dict):
            continue
        for dim, score in dims.items():
            if dim not in dim_scores:
                dim_scores[dim] = []
            dim_scores[dim].append((float(score), float(fwd_return)))

    result = {}
    for dim, pairs in dim_scores.items():
        if len(pairs) < 10:
            continue
        scores, returns = zip(*pairs)
        ic_val = _spearman_rho(scores, returns)
        avg_score = sum(scores) / len(scores)
        avg_return = sum(returns) / len(returns)
        result[dim] = {
            "ic": round(ic_val, 3),
            "count": len(pairs),
            "avg_score": round(avg_score, 1),
            "avg_return": round(avg_return, 3),
        }

    return dict(sorted(result.items(), key=lambda x: -abs(x[1]["ic"])))


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
            icon = "\u2705" if info["ic"] > 0.1 else ("\u26a0\ufe0f" if info["ic"] > -0.05 else "\u274c")
            lines.append(f"    {icon} {dim}: IC={info['ic']:+.3f} ({info['count']}\u6b21) \u5747\u5206{info['avg_score']:+.0f} \u5747\u6536\u76ca{info['avg_return']:+.2%}")

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
