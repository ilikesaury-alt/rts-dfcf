import json
import sqlite3
import copy
from datetime import date, timedelta

from scanner.config import MAX_MARKET_CAP, MAX_STOCK_PRICE
from scanner.evolution.analytics import dimension_ic

YI = 100_000_000

BASE_PARAMS = {
    "name": "当前参数 (v2026-06)",
    "new_face_min_score": 20,
    "old_face_min_score": 10,
    "momentum_min_score": 15,
    "max_market_cap": 300 * YI,
    "max_stock_price": 100.0,
    "weights": {
        # new_face dimensions
        "new_face_today_pct": 20,
        "new_face_accumulated": 15,
        "new_face_bottom": 15,
        "new_face_volume": 10,
        "new_face_rank_change": 12,
        "new_face_value": 5,
        "new_face_combo": 8,
        "new_face_ma_bull": 6,
        "new_face_candle": 6,
        # old_face dimensions
        "old_face_pullback": 20,
        "old_face_support": 15,
        "old_face_volume": 12,
        "old_face_value": 10,
        "old_face_mild_pullback": 8,
        "old_face_heavy_pullback": -10,
        "old_face_rank_change": 8,
        "old_face_liquidity": -8,
        "old_face_accumulated": -15,
        "old_face_high_pos": -20,
        "old_face_ma_bull": 6,
        "old_face_candle": 6,
        # momentum dimensions
        "momentum_today_pct": 20,
        "momentum_accumulated": 10,
        "momentum_volume": 10,
        "momentum_no_crash": 15,
        "momentum_rank_change": 12,
        "momentum_value": 5,
        "momentum_ma_bull": 6,
        "momentum_candle": 6,
    },
}


def analyze_dimension_efficacy(conn: sqlite3.Connection, window_days: int = 60) -> dict:
    """Return IC-driven recommendations for weight adjustments.

    For each dimension, compute IC.  If |IC| < 0.05, suggest removing or
    halving the weight (dimension has no predictive power).  If IC < -0.1,
    suggest reversing the sign (dimension is anti-predictive).
    """
    ic_data = dimension_ic(conn, window_days)
    suggestions = []

    for dim, info in ic_data.items():
        base_weight = BASE_PARAMS["weights"].get(dim, 0)
        if base_weight == 0:
            continue
        ic = info["ic"]
        count = info["count"]

        if count < 15:
            suggestion = "insufficient_data"
            action = "保留 (数据不足)"
        elif abs(ic) < 0.05:
            suggestion = "neutral"
            action = f"建议减半 (IC={ic:+.3f}, 无区分度)"
        elif ic < -0.1 and base_weight > 0:
            suggestion = "flip_sign"
            action = f"建议反转权重或移除 (IC={ic:+.3f}, 反相关)"
        elif ic < -0.05 and base_weight > 0:
            suggestion = "reduce"
            action = f"建议降低权重 (IC={ic:+.3f}, 弱负相关)"
        elif ic > 0.15:
            suggestion = "boost"
            action = f"建议增加权重 (IC={ic:+.3f}, 强正相关)"
        else:
            suggestion = "keep"
            action = f"保留 (IC={ic:+.3f})"

        suggestions.append({
            "dimension": dim,
            "ic": ic,
            "count": count,
            "current_weight": base_weight,
            "suggestion": suggestion,
            "action": action,
        })

    return sorted(suggestions, key=lambda x: -abs(x["ic"]))


def generate_optimization_report(conn: sqlite3.Connection, window_days: int = 30) -> str:
    """Generate a human-readable optimization report with IC analysis + suggestions."""
    from scanner.evolution.tracker import tracking_stats

    lines = []
    lines.append("\u2501" * 55)
    lines.append("  \u5468\u5ea6\u53c2\u6570\u4f18\u5316\u62a5\u544a")
    lines.append("\u2501" * 55)

    # Current performance
    stats = tracking_stats(conn)
    all_s = stats.get("all")
    if all_s:
        avg_ret = all_s['avg_1d'] if all_s['avg_1d'] is not None else 0.0
        lines.append(f"  \u5f53\u524d\u80dc\u7387: {all_s['wins_1d']}/{all_s['total']} ({all_s['wins_1d']*100//max(all_s['total'],1)}%)  \u5747\u6536\u76ca{avg_ret:+.2f}%")
        lines.append(f"  \u6837\u672c\u91cf: {all_s['total']}\u6b21\u63a8\u8350 (\u6700\u8fd1{window_days}\u5929)")

    # Dimension analysis
    efficacy = analyze_dimension_efficacy(conn, window_days)
    if efficacy:
        lines.append("")
        lines.append("  \u7ef4\u5ea6\u6548\u80fd\u5206\u6790:")
        for e in efficacy[:12]:
            icon = {"boost": "\u2705", "keep": "\u26aa", "reduce": "\u26a0\ufe0f",
                    "flip_sign": "\u274c", "neutral": "\u2753", "insufficient_data": "\u25fb"}.get(e["suggestion"], "\u2753")
            lines.append(f"    {icon} {e['dimension']}: IC={e['ic']:+.3f} ({e['count']}\u6b21) \u5f53\u524d{e['current_weight']:+d} {e['action']}")

    # Parameter adjustment suggestions
    changes = [e for e in efficacy if e["suggestion"] in ("boost", "reduce", "flip_sign", "neutral")]
    if changes:
        lines.append("")
        lines.append("  \u5efa\u8bae\u53c2\u6570\u8c03\u6574:")
        for e in changes[:8]:
            if e["suggestion"] == "boost":
                new_w = int(e["current_weight"] * 1.3)
            elif e["suggestion"] == "reduce":
                new_w = max(0, int(e["current_weight"] * 0.5))
            elif e["suggestion"] == "flip_sign":
                new_w = -e["current_weight"]
            else:
                new_w = max(1, e["current_weight"] // 2)
            lines.append(f"    {e['dimension']}: {e['current_weight']:+d} \u2192 {new_w:+d} ({e['action']})")

    lines.append("")
    lines.append("  \u5907\u6ce8: \u8c03\u6574\u524d\u8bf7\u786e\u4fdd\u6837\u672c\u91cf\u226520\u6b21\uff0c\u907f\u514d\u8fc7\u62df\u5408")
    lines.append("\u2501" * 55)
    return "\n".join(lines)


def apply_params(conn: sqlite3.Connection, params: dict, notes: str = ""):
    """Save a parameter snapshot to the DB and mark it as active."""
    import time
    version = f"v{date.today().isoformat()}-{int(time.time()) % 100000}"

    # Deactivate current
    conn.execute("UPDATE parameter_snapshots SET active = 0 WHERE active = 1")

    conn.execute("""
        INSERT INTO parameter_snapshots (version, params_json, created_at, metrics_json, notes, active)
        VALUES (?, ?, ?, ?, ?, 1)
    """, (
        version,
        json.dumps(params, ensure_ascii=False),
        date.today().isoformat(),
        json.dumps({"applied": True}),
        notes,
    ))
    conn.commit()
    print(f"  [进化] 已保存参数快照 {version}")
    return version
