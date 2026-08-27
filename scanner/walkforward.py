"""walk-forward 滚动检验（2026-08-21，P1 建议落地）。

动机：现有回测（backtest/nextday_attribution/prevday_perf）都是全期/近端两窗口
统计——61 个交易日攒出的结论没有经过「只用过去校准、在未来验证」的检验。本模块
把系统的核心校准结论（🎯 画像、弱转强、超买、过热、小板块共振、资金流出、类别
优先）放进滚动窗口：train 窗口算方向，紧随的 test 窗口看是否复现，方向反转即标
⚠。回答的问题是：**这些结论跨周期稳定吗？**

定位：纯离线分析工具，不进入实时扫描路径；不调参不落库——它只测量「结论是否
还在成立」。

用法：
    python -m scanner.walkforward                    # 默认 train=30 test=10
    python -m scanner.walkforward --train 20 --test 5
    python -m scanner.walkforward --json             # 机器可读

口径：与主决策口径一致——hit = next_day_pct >= NEXTDAY_HIT_THRESHOLD(7%)，
行去重（同票同日取最后一轮），excluded=0。
"""

import argparse
import json
import sys

from scanner.config import NEXTDAY_HIT_THRESHOLD
from scanner.ranking import _entry_dims, _entry_tier, _is_nextday_marked

# 方向翻转判定的最小样本：因子行数与基线行数各自达标才比较 delta，
# 否则视为噪声（跨窗口对比同哲学）。
MIN_FACTOR_SAMPLE = 15
MIN_BASE_SAMPLE = 30
# delta 显著性门槛（pp）：两侧都超过才算真翻转，避免 0.5pp 级噪声报翻转。
FLIP_DELTA_PP = 1.0


def load_rows(conn) -> list[dict]:
    """去重加载全部可评估推荐（同票同日取最后一轮，excluded=0 且有次日收益）。"""
    rows = conn.execute(
        """SELECT date, time, symbol, name, category, score, percent,
                  next_day_pct, accumulated_pct, score_breakdown
           FROM recommendations
           WHERE excluded = 0 AND next_day_pct IS NOT NULL
           ORDER BY date, time"""
    ).fetchall()
    dedup: dict[tuple[str, str], dict] = {}
    for r in rows:
        dedup[(r["date"], r["symbol"])] = dict(r)
    return sorted(dedup.values(), key=lambda x: (x["date"], x["time"]))


def walkforward_windows(dates: list[str], train_days: int, test_days: int) -> list[tuple[list[str], list[str]]]:
    """按交易日列表切滚动窗口：[(train_dates, test_dates), ...]。

    步长 = test_days（相邻 test 窗口无缝衔接）；要求 train 与 test 不重叠且
    train 在前。数据不足一个完整窗口即停止。
    """
    unique = sorted(set(dates))
    out = []
    i = train_days
    while i + test_days <= len(unique):
        out.append((unique[i - train_days : i], unique[i : i + test_days]))
        i += test_days
    return out


def _hit_rate(rows: list[dict], pred, threshold: float) -> tuple[int, float | None]:
    sub = [r for r in rows if pred(r)]
    if not sub:
        return 0, None
    hits = sum(1 for r in sub if (r["next_day_pct"] or 0) >= threshold)
    return len(sub), hits / len(sub) * 100.0


def evaluate_factor(
    name: str, rows_all: list[dict], pred, train_dates: set[str], test_dates: set[str], threshold: float
) -> dict:
    """单因子在 train/test 两窗的方向一致性。

    delta = 因子 hit − 同窗基线 hit（因子相对全推荐的增益）。train/test 的 delta
    符号相反且两侧幅度均超 FLIP_DELTA_PP、样本达标 → flip=True（⚠ 结论不稳）。
    """
    tr = [r for r in rows_all if r["date"] in train_dates]
    te = [r for r in rows_all if r["date"] in test_dates]
    base_tr_n, base_tr = _hit_rate(tr, lambda r: True, threshold)
    base_te_n, base_te = _hit_rate(te, lambda r: True, threshold)
    f_tr_n, f_tr = _hit_rate(tr, pred, threshold)
    f_te_n, f_te = _hit_rate(te, pred, threshold)

    result = {
        "factor": name,
        "train_n": f_tr_n,
        "train_hit": f_tr,
        "train_base": base_tr,
        "test_n": f_te_n,
        "test_hit": f_te,
        "test_base": base_te,
        "flip": False,
        "note": "",
    }
    if None in (f_tr, f_te, base_tr, base_te):
        result["note"] = "样本不足"
        return result
    d_tr = f_tr - base_tr
    d_te = f_te - base_te
    if (
        f_tr_n < MIN_FACTOR_SAMPLE
        or f_te_n < MIN_FACTOR_SAMPLE
        or base_tr_n < MIN_BASE_SAMPLE
        or base_te_n < MIN_BASE_SAMPLE
    ):
        result["note"] = f"样本不足(f={f_tr_n}/{f_te_n} b={base_tr_n}/{base_te_n})"
        return result
    result["train_delta"] = round(d_tr, 2)
    result["test_delta"] = round(d_te, 2)
    if d_tr * d_te < 0 and abs(d_tr) >= FLIP_DELTA_PP and abs(d_te) >= FLIP_DELTA_PP:
        result["flip"] = True
    return result


def _entry(row: dict) -> dict:
    """recommendations 行 → ranking 层 entry 形状（掉榜行路径，无 _candidate）。"""
    e = dict(row)
    try:
        e["score_breakdown"] = json.loads(row.get("score_breakdown") or "{}")
    except (TypeError, ValueError):
        e["score_breakdown"] = {}
    return e


def build_factors() -> list[tuple[str, object]]:
    """系统核心校准结论 → 判定谓词（与 ranking/档位因子同源）。"""

    def _dim(e, key):
        return (_entry_dims(e).get(key) or 0) > 0

    def sweet_non_overbought(e):
        p = e.get("percent")
        if p is None:
            return False
        sweet = p < 2.0 or 4.0 <= p < 8.0
        d = _entry_dims(e)
        overbought = bool(
            d.get("st_overbought_flag")
            or d.get("mo_overbought_flag")
            or d.get("v_st_overbought")
            or d.get("v_mo_overbought")
        )
        return sweet and not overbought

    def small_sector(e):
        d = _entry_dims(e)
        if not (d.get("v_st_sector") or d.get("v_pb_sector") or d.get("v_nf_sector")):
            return False
        cnt = d.get("v_st_sector_count") or d.get("v_pb_sector_count") or d.get("v_nf_sector_count") or 0
        return cnt < 15

    def overheated(e):
        acc = e.get("accumulated_pct")
        return acc is not None and acc >= 50

    def fund_outflow(e):
        v = _entry_dims(e).get("fund_flow_main_pct")
        return v is not None and v <= -8

    return [
        ("rebound 类别", lambda e: e["category"] == "rebound"),
        ("甜蜜带+非超买", sweet_non_overbought),
        ("🎯 完整画像", lambda e: _is_nextday_marked(_entry(e))),
        ("弱转强", lambda e: _dim(e, "st_weak_to_strong") or _dim(e, "v_st_weak")),
        ("超买", lambda e: bool(_entry_dims(e).get("v_st_overbought") or _entry_dims(e).get("v_mo_overbought"))),
        ("累计≥50 过热", overheated),
        ("小板块共振 cnt<15", small_sector),
        ("资金流出≤-8%", fund_outflow),
    ]


def run(conn, train_days: int = 30, test_days: int = 10, threshold: float | None = None) -> dict:
    """执行滚动检验，返回结构化结果（report 渲染与 --json 共用）。"""
    threshold = threshold if threshold is not None else NEXTDAY_HIT_THRESHOLD
    rows = load_rows(conn)
    if not rows:
        return {"windows": [], "factors": [], "tier_windows": [], "note": "无可评估数据"}
    windows = walkforward_windows([r["date"] for r in rows], train_days, test_days)
    factors = build_factors()

    factor_results: list[dict] = []
    tier_windows: list[dict] = []
    for train_set, test_set in windows:
        tr_set, te_set = set(train_set), set(test_set)
        for name, pred in factors:
            factor_results.append(evaluate_factor(name, rows, pred, tr_set, te_set, threshold))
        # 档位单调性：各档 hit（test 窗），校验 档0 > 档3 是否复现
        te_rows = [r for r in rows if r["date"] in te_set]
        tier_hits = {}
        for t in range(4):
            sub = [r for r in te_rows if _entry_tier(_entry(r), accum=r.get("accumulated_pct")) == t]
            n = len(sub)
            tier_hits[t] = (
                n,
                round(sum(1 for r in sub if (r["next_day_pct"] or 0) >= threshold) / n * 100, 2) if n else None,
            )
        tier_windows.append({"test_range": f"{test_set[0]}~{test_set[-1]}", "tiers": tier_hits})
    return {
        "threshold": threshold,
        "train_days": train_days,
        "test_days": test_days,
        "total": len(rows),
        "windows": [f"{t[0][0]}~{t[0][-1]} → {t[1][0]}~{t[1][-1]}" for t in windows],
        "factors": factor_results,
        "tier_windows": tier_windows,
    }


def render(result: dict) -> str:
    lines = []
    lines.append(f"◆ Walk-forward 滚动检验（hit≥{result['threshold']}% 口径，共 {result['total']} 条去重推荐）")
    if not result.get("windows"):
        lines.append(f"  {result.get('note', '数据不足')}")
        return "\n".join(lines)
    lines.append(
        f"  窗口：{result['windows'][0]} … 共 {len(result['windows'])} 个"
        f"（train {result['train_days']} 日 → test {result['test_days']} 日）"
    )
    lines.append("")
    lines.append(f"  {'因子':<14} {'train':>16} {'test':>16} {'结论'}")
    by_factor: dict[str, list[dict]] = {}
    for fr in result["factors"]:
        by_factor.setdefault(fr["factor"], []).append(fr)
    for name, frs in by_factor.items():
        flips = sum(1 for f in frs if f["flip"])
        valid = [f for f in frs if not f["note"]]
        avg_td = sum(f.get("train_delta", 0) for f in valid) / len(valid) if valid else 0
        avg_fd = sum(f.get("test_delta", 0) for f in valid) / len(valid) if valid else 0
        verdict = ""
        if flips:
            verdict = f"⚠ 翻转 {flips}/{len(frs)} 窗"
        elif not valid:
            verdict = "样本不足"
        elif abs(avg_fd) < FLIP_DELTA_PP:
            verdict = "近零效"
        elif avg_td * avg_fd > 0:
            verdict = "✓ 方向稳定"
        else:
            verdict = "⚠ 方向漂移"
        tr_str = f"{avg_td:+.1f}pp" if valid else "—"
        te_str = f"{avg_fd:+.1f}pp" if valid else "—"
        lines.append(f"  {name:<14} {tr_str:>16} {te_str:>16} {verdict}")
    lines.append("")
    lines.append("  档位单调性（test 窗各档 hit%）：")
    for tw in result["tier_windows"]:
        tiers = tw["tiers"]
        seg = " ".join(
            f"档{t}:{tiers[t][1]:.1f}%(n={tiers[t][0]})" if tiers[t][1] is not None else f"档{t}:—" for t in range(4)
        )
        mono = ""
        if tiers[0][1] is not None and tiers[3][1] is not None:
            mono = "✓ 档0>档3" if tiers[0][1] > tiers[3][1] else "⚠ 档0≤档3"
        lines.append(f"    {tw['test_range']}: {seg} {mono}")
    lines.append("")
    lines.append(
        "  说明：delta = 因子 hit − 同窗基线 hit；⚠翻转 = train/test 方向相反且两侧≥1pp。样本不足的窗口不计入均值。"
    )
    return "\n".join(lines)


def main():
    import sqlite3

    from scanner.config import DB_PATH

    ap = argparse.ArgumentParser(description="walk-forward 滚动检验")
    ap.add_argument("--train", type=int, default=30, help="训练窗口交易日数")
    ap.add_argument("--test", type=int, default=10, help="验证窗口交易日数")
    ap.add_argument("--threshold", type=float, default=None, help=f"次日大涨阈值%%（默认 {NEXTDAY_HIT_THRESHOLD}）")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    args = ap.parse_args()

    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    result = run(conn, train_days=args.train, test_days=args.test, threshold=args.threshold)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(render(result))


if __name__ == "__main__":
    main()
