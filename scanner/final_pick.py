"""终选参考区（2026-09-04）：v1+v2 合池 → 档0画像评级 → ≤N 只终选 + 落选理由。

与决策层（scanner/decision.py）互补，回答两个不同的问题：
  - 决策层 =「现在该不该买」：市场门关 → 空仓，空仓是合法输出；
  - 终选区 =「若必须持仓，买谁」：无论门开关都给出最优组合——为什么是它、
    谁被否、否在哪。用户 2026-09-04 需求：终端 v1/v2 池几十只票里选不出来，
    需要独立显示区直接给出档0画像口径的终选结论（与 today_report 同源）。

评级/风险依据全部为已回测结论（nextday_attribution），单源复用
today_report._tier0_verdict（位置/资金量能/风险/★评级），不在此处重算：
  正向：rebound hit 28.6% / short_term 弱转强∩非超买 15.8% / 甜蜜带+累计≥6 20% / kNF 12.7%
  风险：尾盘回吐 / RSI顶背离 / 主力净流出≤-8% / 超买(hit 5% 死亡) / 疲劳 / 8-10%陷阱带
  类别先验：momentum 永禁（决策层实测超额 -0.70%，唯一负超额类别）。

纯展示层：不改评分/排序/落库，fail-open 不阻断扫描主流程。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from scanner.config import (
    DISPLAY_MAX_TODAY_PCT,
    FINAL_PICK_MAX,
    FINAL_PICK_REJECT_TOP,
    now_beijing,
)
from scanner.decision import market_gate
from scanner.utils import to_float

# 双挂归一（同 display 的 nf∩st 规则）：同 symbol 多类别行时按类别优先级取一行。
# short_term 行恒存（池内事实），优先级最高；pool_pick 是 v2 合池快照行。
_CAT_PRIORITY: tuple[str, ...] = (
    "short_term",
    "rebound",
    "known_new_face",
    "pool_pick",
    "momentum",
    "new_face",
)

# 减仓类纪律标签（卖出信号，与 display 主表/v2 池选区同源）：命中即不进终选。
_SELL_TAGS = {"⬇减仓", "⬇减半", "🔻勿接", "💰落袋"}


def _verdict_fn() -> Any:
    """today_report._tier0_verdict 单源复用（懒导入：根目录脚本与 unified_scanner 同根）。"""
    try:
        import today_report
    except ImportError:
        return None
    return today_report._tier0_verdict


def dedup_candidates(entries: list[Any]) -> list[Any]:
    """同 symbol 多类别行归一（short_term 优先，其余按 _CAT_PRIORITY 序）。"""
    best: dict[str, Any] = {}
    for e in entries:
        cat = e.get("category")
        if cat not in _CAT_PRIORITY:
            continue
        sym = e["symbol"]
        cur = best.get(sym)
        if cur is None or _CAT_PRIORITY.index(cat) < _CAT_PRIORITY.index(cur["category"]):
            best[sym] = e
    return list(best.values())


def _reject_reason(v: dict) -> str:
    """落选理由（单条，取最强缺陷）：momentum 先验 > 首个风险 > 涨幅带 > 评级不足。"""
    if v["category"] == "momentum":
        return "momentum负先验"
    if v["risks"]:
        return str(v["risks"][0])
    band = v.get("band")
    if band == "dead":
        return "2-4%死区"
    if band == "trap":
        return "8-10%陷阱带"
    return f"评级不足{v.get('stars', '')}"


def build_final_picks(
    conn: sqlite3.Connection,
    entries: list[Any],
    accum_map: dict[str, float | None],
    flow_pct_map: dict[str, float],
    nextday_mark: dict[tuple[str, str], bool] | None = None,
) -> dict[str, Any]:
    """构建终选：双挂归一 → 追涨门/减仓标签过滤 → 档0画像评级 → 排序截断。

    排序键：verdict 降序 → 🎯（次日大涨画像）优先 → 评分降序。评分只作平局键——
    各桶分数尺度不可比（short_term 满分级 vs pool_pick 0-15 级），🎯/verdict 才是
    跨桶可比口径（与 today_report 档0 组内排序同源）。

    返回 {"available", "gate_allowed", "picks", "rejects", "ts"}；
    picks/rejects 元素为 _tier0_verdict dict + 注入键 _display_pct（实时涨幅）。
    """
    fn = _verdict_fn()
    allowed, _gate_reason = market_gate(conn)
    result: dict[str, Any] = {
        "available": fn is not None,
        "gate_allowed": allowed,
        "picks": [],
        "rejects": [],
        "ts": now_beijing().strftime("%H:%M:%S"),
    }
    if fn is None:
        return result
    # display 单源行情/候选链（懒导入：display 反向懒导入本模块，避免循环导入）
    from scanner.display import _entry_display_quote, _fresh_candidate

    nextday_mark = nextday_mark or {}
    all_v: list[dict] = []
    for e in dedup_candidates(entries):
        sym = e["symbol"]
        pct = _entry_display_quote(e)[0]
        # 追涨门（与 v1 主表/v2 池选区同源 DISPLAY_MAX_TODAY_PCT）
        if pct is not None and pct > DISPLAY_MAX_TODAY_PCT:
            continue
        # 减仓类纪律标签（卖出信号）
        fc = _fresh_candidate(e)
        if fc and fc.tactic_tags and any(t in _SELL_TAGS for t in fc.tactic_tags):
            continue
        if "_accum" not in e:
            e["_accum"] = accum_map.get(sym)
        v = fn(e, flow_pct_map)
        v["_display_pct"] = pct
        v["_marked"] = bool(nextday_mark.get((sym, e["category"])))
        all_v.append(v)
    pool = [v for v in all_v if v["category"] != "momentum"]
    pool.sort(key=lambda v: (-v["verdict"], not v["_marked"], -to_float(v.get("score"), default=0.0)))
    result["picks"] = pool[:FINAL_PICK_MAX]
    picked_ids = {id(v) for v in result["picks"]}
    rejected = [v for v in all_v if id(v) not in picked_ids]
    rejected.sort(key=lambda v: -(to_float(v.get("score"), default=0.0)))
    result["rejects"] = rejected[:FINAL_PICK_REJECT_TOP]
    return result


def render_final_pick_lines(result: dict[str, Any]) -> list[str]:
    """渲染终选区文本行（纯函数，供 display / feishu / 测试共用）。"""
    title = f"◆ 终选参考 — v1+v2 合池·档0画像评级（≤{FINAL_PICK_MAX}只·非交易指令）"
    if not result.get("available"):
        return [title, "  — 档0画像评级不可用（today_report 导入失败）"]
    if not result.get("gate_allowed"):
        title += "（⚠大盘门关·仅观察参考）"
    picks: list[dict] = result.get("picks", [])
    if not picks:
        return [title, "  — 合池无合格标的（全部被追涨门/减仓标签/评级过滤）"]
    lines = [title]
    for i, v in enumerate(picks, 1):
        pos = "·".join([str(v.get("pos", "")), *v.get("pos_detail", [])])
        flow = v.get("flow")
        flow_s = f"{v.get('flow_icon', '')}主力{flow:+.1f}%" if flow is not None else "资金—"
        risks = v.get("risks", [])
        risk_s = "风险:" + ("、".join(risks) if risks else "无")
        concept = v.get("concept") or ""
        pct = v.get("_display_pct")
        pct_s = f"{pct:+.1f}%" if pct is not None else "—"
        marked_s = "🎯" if v.get("_marked") else ""
        lines.append(
            f"  {i}. {v['symbol']} {v['name']}[{v['category']}] "
            f"分{to_float(v.get('score'), default=0.0):.0f} 现{pct_s} "
            f"{v.get('stars', '')}{v.get('label', '')}{marked_s} | {pos} | {flow_s} | {risk_s}"
            + (f" | {concept}" if concept else "")
        )
    rejects: list[dict] = result.get("rejects", [])
    if rejects:
        rtxt = " · ".join(
            f"{v['name']}(分{to_float(v.get('score'), default=0.0):.0f},{_reject_reason(v)})" for v in rejects
        )
        lines.append(f"  ✗ 落选: {rtxt}")
    return lines


def final_pick_lines(
    conn: sqlite3.Connection,
    entries: list[Any],
    accum_map: dict[str, float | None],
    flow_pct_map: dict[str, float],
    nextday_mark: dict[tuple[str, str], bool] | None = None,
) -> list[str]:
    """便捷组合：构建 + 渲染（display.build_scan_view 调用入口）。"""
    result = build_final_picks(conn, entries, accum_map, flow_pct_map, nextday_mark)
    return render_final_pick_lines(result)
