"""终选参考区（2026-09-04 初版；2026-09-05 升级为概率+周期感知终选 ≤N 只 + 落选理由）。

与决策层（scanner/decision.py）互补，回答两个不同的问题：
  - 决策层 =「现在该不该买」：市场门关 → 空仓，空仓是合法输出；
  - 终选区 =「若必须持仓，买谁」：无论门开关都给出最优组合——为什么是它、
    谁被否、否在哪。用户 2026-09-04 需求：终端 v1/v2 池几十只票里选不出来。
    2026-09-05 升级：用户实际买入预算只有 1-2 只，粗粒度 verdict 星级 + 跨桶
    不可比 score 的排序无法回答「池内谁更可能次日大涨」——终选排序改用
    scanner.nextday_prob 的当日口径次日大涨概率（连续可比），并：
      1. 每只标注持有周期（次日靶点 / 3日修复，HOLD_DAYS_BY_CATEGORY 单源）；
      2. 买满 ≥2 只时按驱动概念去相关（同主题第 2 只劣后——同板块齐涨齐跌，
         买 2 只的覆盖度≈买 1 只）；
      3. 头部基准率诚实提示（P 是排序估计非胜率承诺，全池历史基准 ~7.8%）。

评级/风险依据全部为已回测结论（today_report._tier0_verdict 单源复用），概率
校准依据见 scanner/nextday_prob.py 模块 docstring（2026-09-05 当期 1786 样本）：
  正向：🎯复合画像 OR2.6 / 辨识度 OR2.0；风险：主力流出 OR0.32 / 小板块共振
  OR0.58 / 超买 OR0.68 / 2-4%死区 / 8-10%陷阱带。
  类别先验：momentum 永禁（唯一负超额类别，决策层实测 -0.70%）。

纯展示层：不改评分/排序/落库，fail-open 不阻断扫描主流程。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from scanner.config import (
    DISPLAY_MAX_TODAY_PCT,
    FINAL_PICK_MAX,
    FINAL_PICK_REJECT_TOP,
    HOLD_DAYS_BY_CATEGORY,
    now_beijing,
)
from scanner.decision import market_gate
from scanner.nextday_prob import BASE_RATE_DEFAULT, next_day_hit_probability
from scanner.ranking import _is_nextday_marked
from scanner.utils import to_float

# 双挂归一（同 display 的 nf∩st 规则）：同 symbol 多类别行时按类别优先级取一行。
# short_term 行恒存（池内事实），优先级最高；pool_pick 是 v2 合池快照行；
# comeback/core_dip 为低优桶（2026-09-05 纳入终选池——用户决策宇宙含核心低吸/回马枪，
# 其 3 日语义由周期标签明示，概率按各类别 base rate 如实反映 comeback 2.8%）。
_CAT_PRIORITY: tuple[str, ...] = (
    "short_term",
    "rebound",
    "known_new_face",
    "pool_pick",
    "momentum",
    "new_face",
    "comeback",
    "core_dip",
)

# 减仓类纪律标签（卖出信号，与 display 主表/v2 池选区同源）：命中即不进终选。
_SELL_TAGS = {"⬇减仓", "⬇减半", "🔻勿接", "💰落袋"}

# 持有周期标签（HOLD_DAYS_BY_CATEGORY = 信号校准于 cum_3d 语义的类别，config 单源）。
HORIZON_NEXTDAY = "次日靶点"
HORIZON_CUM3D = "3日修复"


def horizon_label(category: str) -> str:
    """类别持有周期标签：cum_3d 语义类（comeback/core_dip）vs 次日靶点类。"""
    return HORIZON_CUM3D if category in HOLD_DAYS_BY_CATEGORY else HORIZON_NEXTDAY


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


def _same_theme(a: dict, b: dict) -> bool:
    """驱动概念相同且非空 → 同主题（去相关判定；空概念无法判定，不算同主题）。"""
    ca = str(a.get("concept") or "").strip()
    cb = str(b.get("concept") or "").strip()
    return bool(ca) and ca == cb


def _reject_reason(v: dict, picks: list[dict]) -> str:
    """落选理由（单条，取最强缺陷）：同板块 > momentum 先验 > 首个风险 > 涨幅带 > 评级不足。"""
    for i, p in enumerate(picks, 1):
        if _same_theme(v, p):
            return f"同板块#{i}{p['name']}"
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


def _prominence_map_safe(conn: sqlite3.Connection, symbols: list[str]) -> dict[str, bool]:
    """辨识度批量预计算（fail-open：表缺失/查询失败 → 空 map，因子跳过）。"""
    if not symbols:
        return {}
    try:
        from scanner.database import get_prominence_map

        return get_prominence_map(conn, symbols)
    except Exception as e:  # noqa: BLE001 - 辨识度是增强因子，任何失败都不阻断终选
        print(f"  [~] 终选区辨识度预计算失败（因子跳过）: {type(e).__name__}: {e}")
        return {}


def build_final_picks(
    conn: sqlite3.Connection,
    entries: list[Any],
    accum_map: dict[str, float | None],
    flow_pct_map: dict[str, float],
    nextday_mark: dict[tuple[str, str], bool] | None = None,
) -> dict[str, Any]:
    """构建终选：双挂归一 → 追涨门/减仓标签过滤 → 概率+评级 → 去相关 → 截断。

    排序键：次日大涨概率降序（nextday_prob，连续可比）→ verdict 降序（风险折价：
    尾盘回吐/顶背离/疲劳等概率模型未含的回测风险因子）→ 评分降序（平局末键）。
    买满 ≥2 只时贪心去相关：同驱动概念的第 2 只跳过，名额不满再按概率回填并标注。

    返回 {"available", "gate_allowed", "pool_size", "picks", "rejects", "ts"}；
    picks/rejects 元素为 _tier0_verdict dict + 注入键 _display_pct/_p/_horizon/_marked。
    """
    fn = _verdict_fn()
    allowed, _gate_reason = market_gate(conn)
    result: dict[str, Any] = {
        "available": fn is not None,
        "gate_allowed": allowed,
        "pool_size": 0,
        "picks": [],
        "rejects": [],
        "ts": now_beijing().strftime("%H:%M:%S"),
    }
    if fn is None:
        return result
    # display 单源行情/候选链（懒导入：display 反向懒导入本模块，避免循环导入）
    from scanner.display import _entry_display_quote, _fresh_candidate

    nextday_mark = nextday_mark or {}
    deduped = dedup_candidates(entries)
    # 辨识度批量预计算（pool 级一次，fail-open 空 map = 因子跳过）
    prom_map = _prominence_map_safe(conn, [e["symbol"] for e in deduped])
    all_v: list[dict] = []
    for e in deduped:
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
        # 🎯 判定：display 预计算 map 优先（全 today_recs 覆盖）；缺项按同源口径现算
        # （_is_nextday_marked 内部 accum 缺失 fail-open，与 🎯 展示标记语义一致）。
        mk = nextday_mark.get((sym, e["category"]))
        if mk is None:
            mk = _is_nextday_marked(e, conn, accum=e.get("_accum"))
        v["_marked"] = bool(mk)
        v["_horizon"] = horizon_label(e["category"])
        v["_p"] = next_day_hit_probability(e, marked=v["_marked"], prominence=prom_map.get(sym), flow=v.get("flow"))
        all_v.append(v)
    result["pool_size"] = len(all_v)
    pool = [v for v in all_v if v["category"] != "momentum"]
    pool.sort(key=lambda v: (-v["_p"], -v["verdict"], -to_float(v.get("score"), default=0.0)))

    # 贪心去相关：同驱动概念的第 2 只跳过；名额不满再按概率回填（render 标注同板块）。
    picks: list[dict] = []
    deferred: list[dict] = []
    for v in pool:
        if len(picks) >= FINAL_PICK_MAX:
            break
        if any(_same_theme(v, p) for p in picks):
            deferred.append(v)
        else:
            picks.append(v)
    if len(picks) < FINAL_PICK_MAX:
        picked_ids = {id(p) for p in picks}
        for v in deferred:
            if len(picks) >= FINAL_PICK_MAX:
                break
            if id(v) not in picked_ids:
                picks.append(v)
    result["picks"] = picks
    picked_ids = {id(p) for p in picks}
    rejected = [v for v in all_v if id(v) not in picked_ids]
    rejected.sort(key=lambda v: -v["_p"])
    result["rejects"] = rejected[:FINAL_PICK_REJECT_TOP]
    return result


def render_final_pick_lines(result: dict[str, Any]) -> list[str]:
    """渲染终选区文本行（纯函数，供 display / feishu / 测试共用）。

    2026-09-08 精简：个股行只保留决策核心字段（代码/名称/类别/概率/周期/星级
    /🎯/现涨幅/驱动概念/同板块提示），位置·主力资金·风险明细·评分砍掉——
    风险已折入星级（评级单源），明细在主表/v2 池选区可查，落选行保留理由。
    """
    title = f"◆ 终选参考 — 合池·次日概率终选（≤{FINAL_PICK_MAX}只·非交易指令）"
    if not result.get("available"):
        return [title, "  — 档0画像评级不可用（today_report 导入失败）"]
    if not result.get("gate_allowed"):
        title += "（⚠大盘门关·仅观察参考）"
    picks: list[dict] = result.get("picks", [])
    pool_size = result.get("pool_size", 0)
    lines = [title]
    lines.append(f"  合格池 {pool_size} 只 · P=排序估计非保证（全池基准 {BASE_RATE_DEFAULT:.1%}）")
    if not picks:
        lines.append("  — 合池无合格标的（全部被追涨门/减仓标签/评级过滤）")
        return lines
    for i, v in enumerate(picks, 1):
        concept = str(v.get("concept") or "").strip()
        pct = v.get("_display_pct")
        pct_s = f"{pct:+.1f}%" if pct is not None else "—"
        marked_s = "🎯" if v.get("_marked") else ""
        theme_s = ""
        if i > 1:
            prev = picks[0]
            if str(prev.get("concept") or "").strip():
                theme_s = " | ⚠与#1同板块" if _same_theme(v, prev) else " | 与#1分散"
        lines.append(
            f"  {i}. {v['symbol']} {v['name']}[{v['category']}] "
            f"P={to_float(v.get('_p'), default=0.0):.0%} {v.get('_horizon', '')} "
            f"{v.get('stars', '')}{v.get('label', '')}{marked_s} 现{pct_s}"
            + (f" {concept}" if concept else "")
            + theme_s
        )
    rejects: list[dict] = result.get("rejects", [])
    if rejects:
        rtxt = " · ".join(
            f"{v['name']}(P={to_float(v.get('_p'), default=0.0):.0%},{_reject_reason(v, picks)})" for v in rejects
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
