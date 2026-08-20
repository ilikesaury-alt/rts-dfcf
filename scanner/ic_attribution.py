"""Step 2 — new_face / known_new_face 维度级 IC 归因。

目标：把 new_face 打分引擎的每个维度（RSI<30 / MACD金叉 / KDJ / 更高低 /
板块共振 / 放量 / 今日涨幅 / 累计涨幅 / BOLL / ATR / OBV / MA多头）在「信号当日」
的真实取值重建出来，与 recommendations 表里已回填的真实前向收益做 Spearman 秩相关
（IC），并给出「信号触发 vs 未触发」两组的均值收益与胜率。

口径（2026-08-18 统一）：默认 --metric next_day_pct（次日大涨，综合排序唯一决策口径）；
cum_3d（T+3 累计）等保留为对照，--metric 可选。

维度还原口径**近似于**分析端 analyze_new_face / validator.validate_nf（非完全一致，见下方 ⚠）：
- historical_kline = 不含今日 bar 的历史 K 线（同引擎）
- closes = historical_kline 的收盘价序列
- feats = build_features(closes, highs, lows, volumes)（指标数学与引擎同源，细节阈值略异）
- vol_ratio 复刻 analysis._compute_volume_metrics（含早盘投影封顶，但 DB 回填
  数据末根已是完整量能，投影倍数为 1.0，行为等价）

⚠ 已知漂移（设计审查 P0 #2，2026-08-19 实证）：extract_features 手写重建的维度值
与线上引擎真实 dimensions 存在系统性偏差——两者都基于「不含今日 historical」但
指标阈值 / vol_ratio / value / pattern 细节不同。实测 ma_bull 符号一致率 0.809、
Spearman(reconstruct_score, 冻结score) ≈ 0.18。IC 归因当前跑在漂移特征集上，但
方向性结论（维度 IC 排序、rsi_bonus / kdj_bonus 调参依据）仍有效，故保留手写重建。
根治（让 extract_features 改调 analyze_new_face 真实 dimensions）会丢失
RSI(6) / MACD 柱 / KDJ 等连续特征 IC，已决策「方案 A：锁定现状 + 诚实化」，不改调参依据。
漂移度由 tests/test_ic_attribution_faithful.py 的 Spearman 诊断常驻监控。

输出两张表：
  A. 连续特征 IC（Spearman(feature, metric)）
  B. 二元信号 IC / 触发组 vs 未触发组 均值收益与胜率
均按 combined + new_face / known_new_face 分拆。

用法：
  python scanner/ic_attribution.py            # 打印全部
  python scanner/ic_attribution.py --csv out  # 另存 CSV
"""
from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from collections import Counter, defaultdict

# 2026-08-20 收敛单源：Spearman IC 统一用 scanner.backtest.spearman（与 nextday_attribution / backtest 同源），
# 删除本地 _rankdata + spearman 等价实现（tie 处理规则曾不同，浮点近邻分数算出不同 IC）。
from scanner.backtest import spearman
from scanner.config import (
    BOTTOM_VOL_SURGE,
    MA_BEAR_SCORE,
    MA_BULL_2_TIER_SCORE,
    MA_BULL_3_TIER_SCORE,
    NEW_FACE_WEIGHTS,
)
from scanner.features import build_features
from scanner.models import KlineBar, make_kline_bar
from scanner.sector import classify_sector
from scanner.utils import clear_screen


def _ma_bull_score(closes: list[float], feats: dict) -> int:
    ma5 = feats.get("ma5_ema")
    ma10 = feats.get("ma10_ema")
    ma20 = feats.get("ma20_ema")
    if ma5 is None or ma10 is None:
        return 0
    if ma20 is not None and ma5 > ma10 > ma20:
        return MA_BULL_3_TIER_SCORE
    if ma5 > ma10:
        return MA_BULL_2_TIER_SCORE
    return MA_BEAR_SCORE


def _vol_ratio(kline: list[KlineBar], today_str: str) -> float:
    """复刻 analysis._compute_volume_metrics 的 vol_ratio（无今日 bar 投影）。

    avg_vol==0（脏基准）时返回 0.0（fail-closed），与 analysis 保持一致——
    不返回 1.0 以免脏量能票误过量比相关信号阈值。
    """
    volumes = [k["volume"] for k in kline]
    vol_window = volumes[-11:-1] if len(volumes) >= 11 else volumes[:-1]
    avg_vol = sum(vol_window) / max(len(vol_window), 1)
    today_vol = kline[-1]["volume"]
    return today_vol / avg_vol if avg_vol > 0 else 0.0


def load_recommendations(conn: sqlite3.Connection,
                         metric: str = "next_day_pct") -> list[dict]:
    cur = conn.execute(
        f"""SELECT date, symbol, name, category, score, {metric}, score_breakdown
           FROM recommendations
           WHERE category IN ('new_face','known_new_face')"""
    )
    recs = []
    for date, symbol, name, category, score, metric_val, sb in cur.fetchall():
        recs.append({
            "date": date, "symbol": symbol, "name": name, "category": category,
            "score": score, "metric": metric_val,
            "score_breakdown": sb,
        })
    return recs


def load_kline_by_symbol(conn: sqlite3.Connection, symbols: set[str]) -> dict[str, list[KlineBar]]:
    out: dict[str, list[KlineBar]] = {}
    for sym in symbols:
        cur = conn.execute(
            "SELECT date, open, close, high, low, volume, percent FROM daily_kline "
            "WHERE symbol = ? ORDER BY date", (sym,)
        )
        rows = []
        for date, o, c, h, low, v, p in cur.fetchall():
            bar = make_kline_bar({"date": date, "open": o, "close": c, "high": h,
                                  "low": low, "volume": v, "percent": p})
            if bar is not None:
                rows.append(bar)
        out[sym] = rows
    return out


def sector_counts_by_date(recs: list[dict]) -> dict[str, Counter]:
    """同交易日各板块的 new_face/known_new_face 计数（板块共振代理）。"""
    by_date: dict[str, list[str]] = defaultdict(list)
    for r in recs:
        sec = classify_sector(r["name"])
        by_date[r["date"]].append(sec)
    return {d: Counter(secs) for d, secs in by_date.items()}


def extract_features(kline: list[KlineBar], rec_date: str, sector_peers: int) -> dict | None:
    """在 rec_date 当日重建 new_face 各维度特征；缺数据返回 None。"""
    idx = None
    for i, k in enumerate(kline):
        if k["date"] == rec_date:
            idx = i
            break
    if idx is None or idx == 0:
        return None
    historical = kline[:idx]
    today_bar = kline[idx]
    if len(historical) < 6:
        return None
    closes = [k["close"] for k in historical]
    highs = [k["high"] for k in historical]
    lows = [k["low"] for k in historical]
    volumes = [k["volume"] for k in historical]
    feats = build_features(closes, highs, lows, volumes)

    rsi6 = feats["rsi6"]
    rsi14 = feats["rsi14"]
    macd = feats["macd"]
    boll = feats["boll"]
    kdj = feats.get("kdj")
    atr = feats.get("atr")
    obv = feats.get("obv")

    macd_hist = macd["histogram"] if macd else None
    macd_hist_prev = macd["histogram_prev"] if macd else None
    macd_cross = (macd_hist is not None and macd_hist_prev is not None
                  and macd_hist > 0 and macd_hist_prev <= 0)

    kdj_K = kdj["K"] if kdj else None
    kdj_J = kdj["J"] if kdj else None
    kdj_gold = False
    if kdj is not None:
        prev_k = kdj.get("prev_K")
        prev_d = kdj.get("prev_D")
        if prev_k is not None and prev_d is not None:
            golden = prev_k <= prev_d and kdj["K"] > kdj["D"]
        else:
            golden = kdj["K"] > kdj["D"]
        if (kdj["K"] < 20 and golden) or kdj["J"] < 0:
            kdj_gold = True

    boll_b_pct = boll["b_pct"] if boll else None
    atr_pct = (atr / closes[-1] * 100) if (atr is not None and closes) else None
    obv_trend = obv["obv_trend"] if obv else None

    vol_ratio = _vol_ratio(kline[:idx + 1], rec_date)
    ma_bull = _ma_bull_score(closes, feats)

    if len(closes) >= 6:
        accumulated = (closes[-1] - closes[-6]) / closes[-6] * 100
    else:
        accumulated = sum(k["percent"] for k in historical[-5:])

    today_pct = today_bar["percent"]
    pcts = [k["percent"] for k in historical]
    recent_3 = pcts[-3:]
    no_heavy_loss = all(p > -9.0 for p in recent_3)  # BOTTOM_MAX_LOSS=-9 近似
    near_20d_low = (closes[-1] - min(closes[-20:])) / max(min(closes[-20:]), 0.01) < 0.05 \
        if len(closes) >= 20 else False
    # 更高低结构（validator._nf_higher_low）
    hl_ratio = None
    if len(closes) >= 10:
        recent_zone = min(closes[-5:])
        prev_zone = max(min(closes[-10:-5]), 0.001)
        hl_ratio = recent_zone / prev_zone

    return {
        # 连续特征
        "rsi6": rsi6, "rsi14": rsi14,
        "macd_hist": macd_hist, "kdj_K": kdj_K, "kdj_J": kdj_J,
        "boll_b_pct": boll_b_pct, "atr_pct": atr_pct, "obv_trend": obv_trend,
        "vol_ratio": vol_ratio, "ma_bull": float(ma_bull),
        "accumulated": accumulated, "today_pct": today_pct,
        "hl_ratio": hl_ratio, "sector_peers": float(sector_peers),
        # 二元信号（对齐打分/验证逻辑）
        "sig_rsi30": (rsi6 is not None and rsi6 < 30),
        "sig_macd_cross": macd_cross,
        "sig_kdj": kdj_gold,
        "sig_boll_oversold": (boll_b_pct is not None and boll_b_pct < 0),
        "sig_vol_surge": vol_ratio > BOTTOM_VOL_SURGE,
        "sig_vol_surge13": vol_ratio > 1.3,
        "sig_higher_low": (hl_ratio is not None and hl_ratio > 1.01),
        "sig_bottom_confirmed": (no_heavy_loss and vol_ratio > BOTTOM_VOL_SURGE and near_20d_low),
        "sig_sector_mod": sector_peers >= 2,
        "sig_sector_strong": sector_peers >= 3,
        "sig_ma_bull": ma_bull > 0,
    }


# 连续特征元数据（用于排序/展示）
CONT_FEATURES = [
    ("rsi6", "RSI(6)"), ("rsi14", "RSI(14)"),
    ("macd_hist", "MACD柱"), ("kdj_K", "KDJ-K"), ("kdj_J", "KDJ-J"),
    ("boll_b_pct", "BOLL %B"), ("atr_pct", "ATR%"), ("obv_trend", "OBV趋势"),
    ("vol_ratio", "量比"), ("ma_bull", "MA多头分"),
    ("accumulated", "累计涨幅%"), ("today_pct", "今日涨幅%"),
    ("hl_ratio", "更高低比"), ("sector_peers", "同板块同伴数"),
]

BIN_FEATURES = [
    ("sig_rsi30", "RSI<30"), ("sig_macd_cross", "MACD金叉"),
    ("sig_kdj", "KDJ(K<20金叉/J<0)"), ("sig_boll_oversold", "BOLL破下轨"),
    ("sig_vol_surge", "放量(>BOTTOM_VOL_SURGE)"), ("sig_vol_surge13", "放量(>1.3)"),
    ("sig_higher_low", "更高低结构"), ("sig_bottom_confirmed", "底部确认"),
    ("sig_sector_mod", "板块共振(≥2)"), ("sig_sector_strong", "板块共振(≥3)"),
    ("sig_ma_bull", "MA多头排列"),
]


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else float("nan")


def _winrate(xs: list[float]) -> float:
    return (sum(1 for x in xs if x > 0) / len(xs) * 100) if xs else float("nan")


def cont_ic_table(rows: list[tuple[float, dict]]) -> list[dict]:
    """rows: (cum_3d, features)"""
    out = []
    for key, label in CONT_FEATURES:
        xs = [r[1].get(key) for r in rows if r[1].get(key) is not None]
        ys = [r[0] for r in rows if r[1].get(key) is not None]
        if len(xs) < 10:
            continue
        ic = spearman(xs, ys)
        out.append({
            "label": label, "key": key, "n": len(xs),
            "ic": ic, "mean_cum": _mean(ys),
        })
    out.sort(key=lambda d: (d["ic"] if d["ic"] is not None else -9), reverse=True)
    return out


def bin_ic_table(rows: list[tuple[float, dict]]) -> list[dict]:
    y = [r[0] for r in rows]
    out = []
    for key, label in BIN_FEATURES:
        on_y = [r[0] for r in rows if r[1].get(key)]
        off_y = [r[0] for r in rows if not r[1].get(key)]
        if len(on_y) < 5:
            continue
        flag = [1.0 if r[1].get(key) else 0.0 for r in rows]
        ic = spearman(flag, y)
        out.append({
            "label": label, "key": key, "n_on": len(on_y), "n_off": len(off_y),
            "mean_on": _mean(on_y), "mean_off": _mean(off_y),
            "win_on": _winrate(on_y), "win_off": _winrate(off_y),
            "ic": ic,
        })
    out.sort(key=lambda d: (d["ic"] if d["ic"] is not None else -9), reverse=True)
    return out


def fmt(x, w=7):
    if x is None:
        return "  n/a ".rjust(w)
    return f"{x:+.2f}".rjust(w)


def print_tables(title: str, rows: list[tuple[float, dict]], metric: str = "next_day_pct"):
    print(f"\n{'='*78}\n{title}  (n={len(rows)})\n{'='*78}")
    if not rows:
        print("  (无可用样本)")
        return
    print(f"-- 连续特征 IC（Spearman 与 {metric}） --")
    print(f"  {'维度':<22}{'n':>5}{'IC':>9}{f'均值{metric}':>11}")
    for d in cont_ic_table(rows):
        print(f"  {d['label']:<22}{d['n']:>5}{fmt(d['ic']):>9}{fmt(d['mean_cum']):>11}")
    print(f"-- 二元信号：触发 vs 未触发（{metric}） --")
    print(f"  {'信号':<22}{'n_on':>5}{'均值(on)':>10}{'胜率(on)':>9}{'均值(off)':>10}{'胜率(off)':>9}{'IC':>8}")
    for d in bin_ic_table(rows):
        won = f"{d['win_on']:.1f}%"
        woff = f"{d['win_off']:.1f}%"
        print(f"  {d['label']:<22}{d['n_on']:>5}{fmt(d['mean_on']):>10}"
              f"{won.rjust(9)}{fmt(d['mean_off']):>10}{woff.rjust(9)}{fmt(d['ic']):>8}")


# Step 2 之前的旧权重（用于投影对比「旧→新」的 rank-IC 提升）。
OLD_NEW_FACE_WEIGHTS: dict[str, int] = {
    "today_pct_2_6": 20, "today_pct_1_2": 10, "today_pct_0_5_1": 5,
    "today_pct_lt_0_5": 5, "today_pct_6_8": 5, "today_pct_gt_8": -15,
    "accum_neg5_10": 10, "accum_lt_neg5": 0, "accum_10_15": 5, "accum_15_20": -5,
    "v_shape": 10, "volume_surge": 10,
    "rsi_bonus": 3, "macd_bonus": 3, "rsi14_oversold_bonus": 3,
    "bollinger_oversold": 4, "kdj_bonus": 1,
    "atr_contraction": 2, "obv_not_negative": 2, "bottom_confirmed": 0,
}


def reconstruct_score(feats: dict, W: dict) -> int:
    """从已重建的特征反推 new_face 打分（仅含可重建的分析维度），
    用于在同特征集下对比「当前权重」vs「提议权重」的 rank-IC。"""
    score = 0
    tp = feats.get("today_pct")
    if tp is not None:
        if tp >= 6:
            score += W["today_pct_6_8"] if tp <= 8 else W["today_pct_gt_8"]
        elif tp >= 2:
            score += W["today_pct_2_6"]
        elif tp >= 1:
            score += W["today_pct_1_2"]
        elif tp >= 0.5:
            score += W["today_pct_0_5_1"]
        else:
            score += W["today_pct_lt_0_5"]
    acc = feats.get("accumulated")
    if acc is not None:
        if -5 < acc <= 10:
            score += W["accum_neg5_10"]
        elif acc <= -5:
            score += W["accum_lt_neg5"]
        elif acc <= 15:
            score += W["accum_10_15"]
        elif acc <= 20:
            score += W["accum_15_20"]
    if feats.get("sig_vol_surge"):
        score += W["volume_surge"]
    if feats.get("sig_bottom_confirmed"):
        score += W["bottom_confirmed"]
    if (acc is not None and acc < -5 and feats.get("sig_vol_surge")
            and tp is not None and tp > 2):
        score += W["v_shape"]
    rsi6 = feats.get("rsi6")
    if rsi6 is not None:
        if rsi6 < 20:
            score += W["rsi_bonus"] * 2
        elif rsi6 < 30:
            score += W["rsi_bonus"]
    rsi14 = feats.get("rsi14")
    if rsi14 is not None and rsi14 < 30 and not (rsi6 is not None and rsi6 < 30):
        score += W["rsi14_oversold_bonus"]
    if feats.get("sig_macd_cross"):
        score += W["macd_bonus"]
    if feats.get("sig_kdj"):
        score += W["kdj_bonus"]
    if feats.get("sig_boll_oversold"):
        score += W["bollinger_oversold"]
    atr_pct = feats.get("atr_pct")
    if atr_pct is not None and atr_pct < 3:
        score += W["atr_contraction"]
    if feats.get("obv_trend") is not None and feats["obv_trend"] >= 0:
        score += W["obv_not_negative"]
    score += int(feats.get("ma_bull", 0.0))
    return score


def project_rank_ic(rows: list[tuple[float, dict]], cur_W: dict, prop_W: dict):
    y = [r[0] for r in rows]
    cur_s = [float(reconstruct_score(r[1], cur_W)) for r in rows]
    prop_s = [float(reconstruct_score(r[1], prop_W)) for r in rows]
    return spearman(cur_s, y), spearman(prop_s, y)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=None, help="可选：导出 CSV 前缀")
    ap.add_argument("--metric", default="next_day_pct",
                    choices=["next_day_pct", "fwd_3d", "fwd_5d", "cum_2d", "cum_3d"],
                    help="收益口径（2026-08-18 默认 next_day_pct，统一「次日大涨」）")
    args = ap.parse_args()
    metric = args.metric

    clear_screen()
    conn = sqlite3.connect("scanner.db")
    recs = load_recommendations(conn, metric=metric)
    syms = {r["symbol"] for r in recs}
    kline_map = load_kline_by_symbol(conn, syms)
    sec_by_date = sector_counts_by_date(recs)
    conn.close()

    combined = []
    by_cat: dict[str, list] = defaultdict(list)
    for r in recs:
        if r["metric"] is None:
            continue
        kl = kline_map.get(r["symbol"])
        if not kl:
            continue
        sec = classify_sector(r["name"])
        peers = sec_by_date.get(r["date"], Counter()).get(sec, 0) - 1  # 排除自身
        feats = extract_features(kl, r["date"], peers)
        if feats is None:
            continue
        row = (r["metric"], feats)
        combined.append(row)
        by_cat[r["category"]].append(row)

    print_tables("【Combined】new_face + known_new_face", combined, metric=metric)
    print_tables("【new_face】仅新面孔", by_cat.get("new_face", []), metric=metric)
    print_tables("【known_new_face】仅已知面孔", by_cat.get("known_new_face", []), metric=metric)

    # ── rank-IC 投影：旧权重 vs 新权重（同特征集，苹果对苹果）──
    print("\n" + "=" * 78)
    print(f"【rank-IC 投影】reconstruct_score 的 Spearman(重建分, {metric})")
    print("=" * 78)
    print(f"  {'样本':<22}{'旧IC':>10}{'新IC':>10}{'ΔIC':>9}")
    for tag, rows in (("Combined", combined),
                      ("new_face", by_cat.get("new_face", [])),
                      ("known_new_face", by_cat.get("known_new_face", []))):
        if not rows:
            continue
        ic_old, ic_new = project_rank_ic(rows, OLD_NEW_FACE_WEIGHTS, NEW_FACE_WEIGHTS)
        d_ic = (ic_new - ic_old) if (ic_old is not None and ic_new is not None) else None
        old_s = f"{ic_old:+.3f}" if ic_old is not None else "  n/a "
        new_s = f"{ic_new:+.3f}" if ic_new is not None else "  n/a "
        d_s = f"{d_ic:+.3f}" if d_ic is not None else "  n/a "
        print(f"  {tag:<22}{old_s:>10}{new_s:>10}{d_s:>9}")
    print("  说明：旧IC 用改动前 NEW_FACE_WEIGHTS 重建，新IC 用当前 NEW_FACE_WEIGHTS 重建；")
    print("        两者基于同一组已重建特征，ΔIC>0 表示 Step 2 重平衡后排序预测力提升。")

    if args.csv:
        for tag, rows in (("combined", combined),
                          ("new_face", by_cat.get("new_face", [])),
                          ("known_new_face", by_cat.get("known_new_face", []))):
            with open(f"{args.csv}_{tag}.csv", "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["metric", "label", "n", "ic", "mean", "win_on", "win_off", "mean_on", "mean_off"])
                for d in cont_ic_table(rows):
                    w.writerow(["cont", d["label"], d["n"], d["ic"], d["mean_cum"], "", "", "", ""])
                for d in bin_ic_table(rows):
                    w.writerow(["bin", d["label"], d["n_on"], d["ic"], "",
                                d["win_on"], d["win_off"], d["mean_on"], d["mean_off"]])
            print(f"[csv] 已写出 {args.csv}_{tag}.csv")

    return 0


if __name__ == "__main__":
    sys.exit(main())
