"""市场择时门（2026-09-04 上线；2026-09-14 决策层删除后**仅存此门**）。

历史沿革（重要，勿从模块名反推功能）：本模块原先承载「决策层」——每日 ≤3 只的
「现在值得买什么」短名单 + 空仓判定，链路为 市场门 → 类别先验门 → 分时门 →
全局配额，并落库 decision_picks 供「决策层 vs 全池」次日对比。
2026-09-14 用户决策**删除整个决策层**：终端/飞书的「今日决策」区块、ScanView.
decision_lines 字段、build/save/render 四个函数、decision_picks 建表与采集、
DECISION_GATED_CATEGORIES / DECISION_CAT_CAPS / DECISION_CATEGORY_SPECS /
DECISION_MAX_PICKS 派生常量、DECISION_LAYER_ENABLED / DECISION_INTRADAY_BEAUTY_ENABLED
开关，全部移除。需要复原见 git 历史（删除前的最后一个提交）。

**为什么 market_gate 被留下**：它回答的是「今天开仓的期望是否为负」——择时问题，
不是择股问题。终选参考区（scanner/final_pick）依赖它产出「门开」/「门关·仅观察参考」
的标题，即终选挑出的那 2 只票是否处于可开仓市况。删掉它就等于让终选失去市况标注。

**口径豁免（AGENTS.md §目标函数）**：系统唯一口径是次日≥7% hit 率，而本门按
**实测平均超额收益**校准——实测（71 天 / 1233 样本）：唯一正期望状态是「强势日」
+0.36%（t=1.77）；大跌日 -0.91%（t=-2.49 显著负）、小跌日 -0.28%、反弹但 5 日弱
-0.51%。这道门把系统从负期望整体翻正，比调任何个股阈值杠杆都大。择时与择股不同轴，
故此处**刻意保留**平均超额口径，不是漏改。
"""

from __future__ import annotations

import sqlite3

from scanner.utils import EXTERNAL_FAILURES

# 市场门阈值（实测校准见模块 docstring）
GATE_INDEX_MIN_PCT = 0.0  # 创业板指当日涨幅下限
GATE_CUM5_MIN_PCT = -3.0  # 5 日累计涨幅下限


def market_gate(conn: sqlite3.Connection) -> tuple[bool, str]:
    """市场门：读 market_index_log 判定今日是否允许开仓。

    返回 (allowed, reason)。基准缺失时 fail-closed（宁可空仓也不盲开）——
    与扫描主流程的 fail-open 语义相反：这里是保守侧。
    """
    try:
        rows = conn.execute("SELECT date, index_pct FROM market_index_log ORDER BY date").fetchall()
    except EXTERNAL_FAILURES as e:
        return False, f"市场门判定失败（{type(e).__name__}: {e}）→ 空仓"
    if not rows:
        return False, "market_index_log 无基准数据 → 空仓（先跑 backfill_market_index.py）"
    days = [d for d, _ in rows]
    idx = dict(rows)
    today = days[-1]
    it = idx.get(today)
    if it is None:
        return False, f"{today} 指数未落库 → 空仓"
    i = days.index(today)
    cum5 = sum(idx[d] for d in days[max(0, i - 4) : i + 1] if idx[d] is not None) if i >= 4 else None
    if it <= GATE_INDEX_MIN_PCT or (cum5 is not None and cum5 <= GATE_CUM5_MIN_PCT):
        cum5_s = f"{cum5:+.1f}%" if cum5 is not None else "n/a"
        return False, f"大盘门未开（今日 {it:+.2f}% / 5日 {cum5_s}）→ 空仓"
    cum5_s = f"{cum5:+.1f}%" if cum5 is not None else "n/a"
    return True, f"大盘门开（今日 {it:+.2f}% / 5日 {cum5_s}）"
