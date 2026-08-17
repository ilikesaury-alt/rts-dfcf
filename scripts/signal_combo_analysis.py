"""组合信号命中率分析（2026-08-17 临时诊断脚本，不进入扫描路径）。

口径与 nextday_attribution._load_dedup 一致：现役类别、同 (date, symbol) 保留最高分。
对每个候选信号及组合，统计 next_day≥7% 命中率 + 平均 next_day + cum_3d 均值。
样本 <20 标注「不足」，不据此下结论。
"""
import json
import sqlite3

THRESHOLD = 7.0


def _dedup(conn):
    recs = []
    for r in conn.execute(
        "SELECT symbol, name, category, date, score, percent, trend, "
        "next_day_pct, cum_3d, score_breakdown FROM recommendations "
        "WHERE category NOT IN ('pullback') AND excluded = 0 "
        "ORDER BY date, symbol, score DESC"
    ):
        d = {
            "symbol": r[0], "name": r[1], "category": r[2], "date": r[3],
            "score": r[4], "percent": r[5], "trend": r[6],
            "next_day_pct": r[7], "cum_3d": r[8],
        }
        try:
            d["sb"] = json.loads(r[9]) if r[9] else {}
        except Exception:
            d["sb"] = {}
        recs.append(d)
    # 同 (date, symbol) 保留最高分
    best: dict[tuple, dict] = {}
    for d in recs:
        k = (d["date"], d["symbol"])
        if k not in best or d["score"] > best[k]["score"]:
            best[k] = d
    return list(best.values())


def _f(d, *keys):
    """从 score_breakdown 取第一个存在的键的值。"""
    sb = d.get("sb", {})
    for k in keys:
        if k in sb:
            v = sb[k]
            try:
                return float(v)
            except (TypeError, ValueError):
                return 0.0
    return None


def _flag(d, *keys):
    """布尔判定：任一键 >0 或为 True。"""
    for k in keys:
        v = d.get("sb", {}).get(k)
        if v is True:
            return True
        if isinstance(v, (int, float)) and v > 0:
            return True
    return False


def main():
    conn = sqlite3.connect("scanner.db")
    recs = _dedup(conn)
    conn.close()
    print(f"去重样本: {len(recs)}")

    # 信号定义
    def sweet(d):  # 甜蜜带 0-2 或 4-8
        p = d["percent"] if d["percent"] is not None else 0.0
        return 0.0 <= p < 2.0 or 4.0 <= p < 8.0

    def not_overbought(d):
        return not (_flag(d, "v_st_overbought", "v_mo_overbought", "st_overbought", "mo_overbought"))

    def weak_to_strong(d):
        return _flag(d, "v_st_weak", "st_weak_to_strong")

    def rank_traj_pos(d):  # 排名轨迹正效因子任一
        return _flag(d, "rank_trend_bonus", "list_traj_bonus", "list_top40_bonus",
                     "list_streak_bonus", "list_momentum_bonus")

    def sector_resonance(d):  # 板块共振（回测负效）
        return _flag(d, "v_st_sector", "v_pb_sector", "v_nf_sector")

    def strong_intraday(d):
        v = _f(d, "intraday_score")
        return v is not None and v >= 1.0

    def good_env(d):
        v = _f(d, "market_env_bonus")
        return v is not None and v >= 0

    def fund_inflow(d):
        v = _f(d, "fund_flow_main_pct")
        return v is not None and v >= 5.0

    signals = {
        "全样本(基准)": lambda d: True,
        "甜蜜带(0-2/4-8)": sweet,
        "甜蜜带 ∩ 非超买": lambda d: sweet(d) and not_overbought(d),
        "甜蜜带 ∩ 非超买 ∩ 排名轨迹+": lambda d: sweet(d) and not_overbought(d) and rank_traj_pos(d),
        "甜蜜带 ∩ 非超买 ∩ 弱转强": lambda d: sweet(d) and not_overbought(d) and weak_to_strong(d),
        "弱转强 ∩ 非超买": lambda d: weak_to_strong(d) and not_overbought(d),
        "排名轨迹+ 单独": rank_traj_pos,
        "板块共振(负效验证)": sector_resonance,
        "分时强度≥1.0": strong_intraday,
        "大盘环境≥0": good_env,
        "主力流入≥5%": fund_inflow,
        "非超买 单独": lambda d: not_overbought(d),
    }

    print(f"\n{'信号组合':<32}{'样本':>6}{'hit':>5}{'hit率':>8}{'均次日':>9}{'cum_3d':>9}")
    print("-" * 72)
    for name, fn in signals.items():
        sub = [d for d in recs if fn(d)]
        n = len(sub)
        if n == 0:
            print(f"{name:<32}{0:>6}{'-':>5}{'-':>8}{'-':>9}{'-':>9}  (空)")
            continue
        nd = [d["next_day_pct"] for d in sub if d["next_day_pct"] is not None]
        hit = sum(1 for v in nd if v >= THRESHOLD)
        avg_nd = sum(nd) / len(nd) if nd else 0.0
        c3 = [d["cum_3d"] for d in sub if d["cum_3d"] is not None]
        avg_c3 = sum(c3) / len(c3) if c3 else float("nan")
        tag = "" if n >= 20 else "  ⚠样本不足"
        print(f"{name:<32}{n:>6}{hit:>5}{hit / n * 100:>7.1f}%{avg_nd:>9.2f}{avg_c3:>9.2f}{tag}")

    # 类别 × 甜蜜带 交叉
    print("\n--- 类别 × 甜蜜带交叉 ---")
    for cat in ("rebound", "comeback", "short_term", "momentum", "known_new_face", "new_face"):
        sub = [d for d in recs if d["category"] == cat]
        if not sub:
            continue
        s2 = [d for d in sub if sweet(d)]
        for label, grp in (("全部", sub), ("甜蜜带", s2)):
            n = len(grp)
            nd = [d["next_day_pct"] for d in grp if d["next_day_pct"] is not None]
            hit = sum(1 for v in nd if v >= THRESHOLD)
            avg = sum(nd) / len(nd) if nd else 0.0
            print(f"  {cat:<16}{label:<6} n={n:>4} hit={hit:>3} ({hit / n * 100:>5.1f}%) 均次日={avg:>6.2f}")


if __name__ == "__main__":
    main()
