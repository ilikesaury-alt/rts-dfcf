"""ic_attribution 与线上 new_face 引擎的口径一致性锁（设计审查 P0 #2）。

背景
----
`scanner/ic_attribution.py` 的 `extract_features` + `reconstruct_score` 是 new_face 引擎的
**并行手写重实现**（用于维度级 IC 归因）。线上引擎改阈值/权重/维度，这里不会自动跟随 ——
这就是 #2 的漂移面。

实证现状（2026-08-19，对 scanner.db 真实样本）：
  - 冻结 breakdown 维度键 = new_face_today_pct / new_face_accumulated / new_face_ma_bull /
    new_face_candle / new_face_rank_change / new_face_value / new_face_bottom / new_face_volume …
  - extract_features 算的 sig_rsi30 / sig_boll_oversold / sig_obv 在冻结 breakdown 里**不存在**，
    冻结 breakdown 的 new_face_rsi/bollinger/obv_trend 也**未被记录** → 两套维度集已分叉。
  - Spearman(reconstruct_score, 冻结score) ≈ 0.18 → IC 归因当前跑在漂移特征集上。

正确治本（设计审查 P0 #2，需方案评审，不宜盲改）：让 ic_attribution 复用线上 pipeline 的
真实 dimensions（`features_for_signal_date`），不再手写重建。届时本测试的秩相关应升至 ~1.0。

本测试锁定「不会进一步分叉」的不变量（当前已知漂移下仍可绿的回归下限）：
  1. 结构锁：冻结 breakdown 必须含 new_face_ma_bull，且 extract_features 也产出 ma_bull →
     两工具至少共享该维度；任一方移除即说明特征集彻底分叉。
  2. 回归下限：ma_bull 符号一致率不得低于 0.75（当前 0.809，已知部分漂移）。
  3. 冒烟：extract_features 在真实 new_face 样本上成功率 ≥ 30%（保证 IC 工具随引擎演进不崩）。
  4. 诊断：打印当前 Spearman(reconstruct_score, 冻结score)，作为漂移健康度标记（目标 ≥0.8 在根治后）。
"""

import json
import os
import sqlite3

import pytest

try:
    from scanner.ic_attribution import (
        extract_features,
        load_kline_by_symbol,
        load_recommendations,
        reconstruct_score,
        sector_counts_by_date,
        spearman,
    )
    from scanner.weights import NEW_FACE_WEIGHTS
    from scanner.sector import classify_sector
    from collections import Counter
    _HAVE_DEPS = True
except Exception:  # pragma: no cover
    _HAVE_DEPS = False

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "scanner.db")


@pytest.mark.skipif(not _HAVE_DEPS, reason="ic_attribution 依赖不可用")
@pytest.mark.skipif(not os.path.exists(DB_PATH), reason="无 scanner.db，跳过真实库样本校验")
def test_ic_attribution_overlap_with_engine_dims():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    recs = load_recommendations(conn, metric="next_day_pct")
    syms = {r["symbol"] for r in recs}
    kline_map = load_kline_by_symbol(conn, syms)
    sec_by_date = sector_counts_by_date(recs)
    conn.close()

    recs_nf = [r for r in recs if r["category"] in ("new_face", "known_new_face")]

    # 结构锁：冻结 breakdown 必须记录 new_face_ma_bull（与 extract_features 的 ma_bull 对应）
    bd_with_ma = sum(1 for r in recs_nf
                     if r["score_breakdown"] and "new_face_ma_bull" in json.loads(r["score_breakdown"]))
    assert bd_with_ma > 0, "冻结 breakdown 未记录 new_face_ma_bull：IC 工具与引擎维度集已无交集"

    compared = 0
    ma_ok = 0
    ma_tot = 0
    recon = []
    frozen = []
    for r in recs_nf:
        if not r["score_breakdown"]:
            continue
        try:
            bd = json.loads(r["score_breakdown"])
        except (json.JSONDecodeError, TypeError):
            continue
        kl = kline_map.get(r["symbol"])
        if not kl:
            continue
        sec = classify_sector(r["name"])
        peers = sec_by_date.get(r["date"], Counter()).get(sec, 0) - 1
        feats = extract_features(kl, r["date"], peers)
        if feats is None:
            continue
        compared += 1
        # ma_bull 重叠维度：符号一致性
        if "new_face_ma_bull" in bd:
            ma_tot += 1
            if (feats["ma_bull"] > 0) == (bd["new_face_ma_bull"] > 0):
                ma_ok += 1
        recon.append(float(reconstruct_score(feats, NEW_FACE_WEIGHTS)))
        frozen.append(float(r["score"]))

    if compared == 0:
        pytest.skip("无足够历史 new_face 样本（需带维度 breakdown 的推荐）")

    # 冒烟：真实样本上 extract_features 成功率
    succ_rate = compared / max(len([r for r in recs_nf if r["score_breakdown"]]), 1)
    assert succ_rate >= 0.30, f"extract_features 成功率 {succ_rate:.2f} < 0.30，IC 工具可能随引擎演进失效"

    # 回归下限：ma_bull 符号一致率（当前已知漂移 0.809，锁下限防进一步分叉）
    if ma_tot:
        ma_rate = ma_ok / ma_tot
        assert ma_rate >= 0.75, f"ma_bull 符号一致率 {ma_rate:.3f} < 0.75，IC 特征集与引擎进一步分叉"

    # 诊断：秩相关健康度（目标 ≥0.8 在根治——ic_attribution 复用线上 pipeline——之后）
    ic = spearman(recon, frozen)
    print(f"\n[ic_attribution 漂移诊断] n={compared} "
          f"Spearman(reconstruct_score, 冻结score)={ic:.3f} "
          f"(ma_bull一致率={ma_ok}/{ma_tot})")
    print("  ⚠ 当前 IC 归因跑在漂移特征集上（根治见设计审查 P0 #2：复用线上 pipeline dimensions）")
