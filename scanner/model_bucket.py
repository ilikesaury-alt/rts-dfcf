"""v3 模型桶——离线可行性验证（M3 第一步，2026-09-06）。

目标：用**已有数据**（triple_barrier_labels 回溯标签 + recommendations 的
score_breakdown 特征宽表）回答「现有特征能否预测三重屏障结果」——不进线上、
不落库、不改推荐，只产出 walkforward 意义下的 AUC/提升度基线，作为是否继续
投入 M3 的决策依据。

样本（M3 样本口径，与 triple_barrier 去重语义一致）：
- 一行 = triple_barrier_labels 的一行（PK 已按 (date,symbol,category) 去重）
- 特征 = 该票该类别**最后一轮**推荐的 score_breakdown 数值维度（缺失=NaN，
  LightGBM 原生处理）+ rec 的 score/percent + 类别 one-hot
- y = 1 if label == +1 else 0（预测「3 日内先触 +7% 止盈」）
- ⚠ ret_at_horizon/touch_* 是结果，不是特征——严禁泄漏进 X

验证协议（与 walkforward.py 同一框架）：
- walkforward_windows(train=40, test=10, embargo=WF_EMBARGO_DAYS)——train 内
  训练 LightGBM（浅树强正则，样本 ~800/窗），紧邻 embargo 后的 test 窗预测
- 指标：每窗 AUC（Mann-Whitney 秩实现，避免引入 sklearn 依赖）+ 头部命中率
  （预测概率前 20% 内 label=+1 占比 vs 该窗基准率）+ 正样本数
- 样本量警告：~1700 行 / 88 交易日属小样本，结论只做「是否继续」判断，
  不做权重替换依据（与 portfolio_backtest「可选自检尺」同定位）

用法：
    python -m scanner.model_bucket               # walkforward 评估
    python -m scanner.model_bucket --json        # 机器可读
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys

import lightgbm as lgb
import numpy as np
import pandas as pd

from scanner.config import DB_PATH, WF_EMBARGO_DAYS
from scanner.walkforward import walkforward_windows

# 训练超参一次锁死（方案纪律：禁止用测试窗调参）；浅树强正则对抗小样本。
# 用 LGBM 原生 API（lgb.train）而非 sklearn 包装——避免引入 scikit-learn 重依赖
# （方案约束：离线训练只加 lightgbm 一个依赖；原生参数名 min_data_in_leaf 等）
LGBM_PARAMS = {
    "objective": "binary",
    "num_leaves": 7,
    "min_data_in_leaf": 20,
    "learning_rate": 0.05,
    "feature_fraction": 0.8,
    "bagging_fraction": 0.8,
    "bagging_freq": 1,
    "lambda_l1": 1.0,
    "lambda_l2": 5.0,
    "seed": 42,
    "verbosity": -1,
}
NUM_BOOST_ROUND = 100
TRAIN_DAYS = 40
TEST_DAYS = 10
TOP_FRAC = 0.2  # 头部命中率分位


def load_dataset(conn: sqlite3.Connection) -> pd.DataFrame:
    """构建训练宽表。一行 = 一条三重屏障标签（已按 PK 去重）。

    score_breakdown 取该 (date,symbol,category) 最后一轮推荐（rowid 最大）——
    标签的买价口径锚定信号日，特征必须来自同一轮快照，取错轮 = 特征泄漏。
    """
    rows = conn.execute(
        """
        SELECT t.date, t.symbol, t.category, t.label, t.ret_at_horizon,
               r.score, r.percent, r.score_breakdown
        FROM triple_barrier_labels t
        JOIN recommendations r
          ON r.date = t.date AND r.symbol = t.symbol AND r.category = t.category
        WHERE (t.touch_date IS NOT NULL OR t.ret_at_horizon IS NOT NULL)
          AND r.rowid = (SELECT MAX(r2.rowid) FROM recommendations r2
                         WHERE r2.date = t.date AND r2.symbol = t.symbol
                           AND r2.category = t.category)
          AND r.excluded = 0
        ORDER BY t.date
        """
    ).fetchall()
    records: list[dict] = []
    for date, sym, cat, label, _ret_h, score, percent, breakdown in rows:
        rec: dict = {
            "date": date,
            "symbol": sym,
            "category": cat,
            "y": 1 if label == 1 else 0,
            "rec_score": score,
            "rec_percent": percent,
        }
        try:
            dims = json.loads(breakdown or "{}")
        except (TypeError, ValueError):
            dims = {}
        for k, v in dims.items():
            # 数值维度入模；detail 文本/结果类字段剔除（detail 无信息且口径乱）
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                rec[f"d_{k}"] = v
        records.append(rec)
    df = pd.DataFrame(records)
    # 类别 one-hot（前缀 c_，防与维度键撞名）
    if not df.empty:
        df = pd.concat([df, pd.get_dummies(df["category"], prefix="c")], axis=1)
    return df


def _auc(y_true: np.ndarray, score: np.ndarray) -> float | None:
    """Mann-Whitney 秩 AUC（无 sklearn 依赖）。单类窗口返回 None。"""
    pos = y_true == 1
    n_pos, n_neg = int(pos.sum()), int((~pos).sum())
    if n_pos == 0 or n_neg == 0:
        return None
    order = np.argsort(score, kind="mergesort")  # 稳定排序处理并列
    ranks = np.empty(len(score), dtype=float)
    s_sorted = score[order]
    i = 0
    while i < len(s_sorted):
        j = i
        while j + 1 < len(s_sorted) and s_sorted[j + 1] == s_sorted[i]:
            j += 1
        ranks[order[i : j + 1]] = (i + j) / 2.0 + 1.0  # 并列取平均秩
        i = j + 1
    return float((ranks[pos].sum() - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg))


def run_walkforward(df: pd.DataFrame, train_days: int = TRAIN_DAYS, test_days: int = TEST_DAYS) -> dict:
    """walkforward 训练评估。返回逐窗指标 + 汇总 + 特征重要性。"""
    dates = sorted(df["date"].unique())
    windows = walkforward_windows(list(dates), train_days, test_days, embargo_days=WF_EMBARGO_DAYS)
    feature_cols = [c for c in df.columns if c.startswith(("d_", "c_", "rec_"))]
    results: list[dict] = []
    importances = np.zeros(len(feature_cols))
    n_models = 0
    for train_dates, test_dates in windows:
        train = df[df["date"].isin(set(train_dates))]
        test = df[df["date"].isin(set(test_dates))]
        if train["y"].nunique() < 2 or test["y"].nunique() < 2 or len(test) < 10:
            continue
        dtrain = lgb.Dataset(train[feature_cols], label=train["y"])
        booster = lgb.train(LGBM_PARAMS, dtrain, num_boost_round=NUM_BOOST_ROUND)
        # np.asarray 收敛（2026-09-06 类型收敛）：booster.predict 返回 ndarray | list，
        # 统一为 ndarray 供 _auc/argsort 消费
        prob = np.asarray(booster.predict(test[feature_cols]), dtype=float)
        auc = _auc(test["y"].to_numpy(), prob)
        # 头部命中率：预测概率前 TOP_FRAC 内的真实 label=+1 占比 vs 该窗基准
        k = max(1, int(len(test) * TOP_FRAC))
        top_idx = np.argsort(-prob)[:k]
        top_hit = float(test["y"].to_numpy()[top_idx].mean())
        base = float(test["y"].mean())
        results.append(
            {
                "test_range": f"{test_dates[0]}~{test_dates[-1]}",
                "n_test": len(test),
                "base_rate": round(base, 4),
                "top_hit": round(top_hit, 4),
                "lift": round(top_hit / base, 3) if base > 0 else None,
                "auc": round(auc, 4) if auc is not None else None,
            }
        )
        importances += booster.feature_importance()
        n_models += 1
    valid = [r for r in results if r["auc"] is not None]
    pooled_auc = (
        round(sum(r["auc"] * r["n_test"] for r in valid) / sum(r["n_test"] for r in valid), 4) if valid else None
    )
    top_feats = sorted(zip(feature_cols, importances, strict=True), key=lambda x: -x[1])[:12]
    return {
        "windows": results,
        "n_models": n_models,
        "n_samples": len(df),
        "pooled_auc": pooled_auc,
        "mean_top_hit": round(float(np.mean([r["top_hit"] for r in valid])), 4) if valid else None,
        "mean_base": round(float(np.mean([r["base_rate"] for r in valid])), 4) if valid else None,
        "top_features": [(k, int(v)) for k, v in top_feats if v > 0],
    }


def render(result: dict) -> str:
    lines = [
        f"◆ v3 模型桶离线可行性（三重屏障 y=先触止盈；样本 {result['n_samples']}，"
        f"训练 {result['n_models']} 窗）",
        f"  汇总：加权 AUC={result['pooled_auc']}  头部命中率(前20%)="
        f"{result['mean_top_hit']}  基准率={result['mean_base']}",
        "",
        f"  {'test 窗':<26}{'样本':>6}{'基准率':>8}{'头部命中':>9}{'提升':>7}{'AUC':>7}",
    ]
    for w in result["windows"]:
        auc = f"{w['auc']:.3f}" if w["auc"] is not None else "—"
        lift = f"{w['lift']:.2f}x" if w["lift"] is not None else "—"
        lines.append(
            f"  {w['test_range']:<26}{w['n_test']:>6}{w['base_rate']:>8.1%}"
            f"{w['top_hit']:>9.1%}{lift:>7}{auc:>7}"
        )
    lines.append("")
    lines.append("  特征重要性 Top12：" + "、".join(f"{k}({v})" for k, v in result["top_features"][:12]))
    lines.append("")
    lines.append("  ⚠ 小样本可行性验证：结论只用于「是否继续 M3」判断，非权重替换依据。")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="v3 模型桶离线可行性验证（M3 第一步）")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    args = ap.parse_args()

    if sys.platform == "win32":
        _reconfigure = getattr(sys.stdout, "reconfigure", None)
        if callable(_reconfigure):
            _reconfigure(encoding="utf-8")

    conn = sqlite3.connect(DB_PATH)
    try:
        df = load_dataset(conn)
        if df.empty:
            print("  [!] 无可训练样本（先跑 python -m scanner.triple_barrier）")
            return
        result = run_walkforward(df)
        print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else render(result))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
