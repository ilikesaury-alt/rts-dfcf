#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""今日综合排序分析报告（2026-08-18 新增）。

与 query_today.py（原始推荐明细 dump）不同：本报告复刻 display_priority 的档位/排序
逻辑（档0🎯次日大涨画像 / 档1强信号 / 档2普通 / 档3警示劣后），对档0 逐票输出
「位置 / 资金量能 / 风险 / 评级」分析并做组内相对排序，同时汇总当日档位分布、
回马枪资金质量、被移出票与数据质量——直接回答「今日综合排序怎么选」的问题。

设计原则（用户 2026-08-18 反馈：综合排序缺个股分析、档0 选股靠猜；但分析若直接
塞进扫描显示区会信息爆炸刷屏）：
  - 独立命令按需查看，display.py 渲染路径零改动（扫描循环不输出长文本）。
  - 纯展示层：不改评分不落库不改排序，全部信息来自已有数据（score_breakdown
    维度、daily_kline、market_extra_cache 资金流、appearances、scan_quality_log）。
  - 评级依据均为已回测结论（nextday_attribution，见 AGENTS.md）：
      正向：rebound hit 28.6% 全场最强；short_term 弱转强∩非超买 hit 15.8%
            （唯一有效子集）；momentum/new_face 甜蜜带+累计≥6 hit 20%；
            kNF hit 12.7% 全场第二。
      风险：尾盘回吐（追高兑现）、RSI 顶背离（v_mo_divergence<0）、主力净流出
            ≤-8%（资金流 2026-08-10 起仅作规避信号——当日主力流入=追涨资金次日
            兑现，加分已归零，此处同样不作正向加分）、超买（hit 5% 死亡信号，
            🎯 已排除但仍防御）、疲劳、8-10% 陷阱带（非 short_term hit 0%）。

用法：
    python today_report.py                      # 最近有数据的交易日
    python today_report.py --date 2026-08-17    # 历史某日
    python today_report.py --top 3              # 档0 个股分析最多 N 只
    python today_report.py --json               # 机器可读 JSON 输出

盘中运行（用户 2026-08-18 确认可用）：扫描器每轮把含今日 bar 的 K 线写入
`daily_kline`、资金流 TTL 300s、appearances 逐轮更新——报告读到的都是截至
最近一次扫描轮的当日快照，档位/🎯 判定与扫描器同源。SQLite 连接带 15s 忙等
超时，与扫描器并发读写不撞锁。总览标注「数据新鲜度」与盘中语义。
"""
import argparse
import json
import re
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8")

from scanner.config import (  # noqa: E402  (stdout reconfigure 在导入前，防编码异常)
    CAT_DISPLAY_PRIORITY,
    DB_PATH,
    FUND_OUTFLOW_NET_PCT,
    NEXTDAY_SPIKE_SWEET_LOW,
    now_beijing,
)
from scanner.database import get_fund_flow_pct_map, get_today_recommendations  # noqa: E402
from scanner.display import (  # noqa: E402
    ANSI,
    CAT_COLOR,
    CAT_LABEL,
    _entry_band,
    _entry_dims,
    _entry_overbought,
    _entry_tier,
    _entry_weak_to_strong,
    _is_nextday_marked,
    _nextday_entry_accum,
    _nextday_entry_percent,
    fund_flow_signal,
)

# 档0 评级阈值（纯展示，依据已回测结论）
_VERDICT_STRONG = 2          # ≥2 → ★★★ 首选
_VERDICT_OK = 1              # ==1 → ★★ 可参与；≤0 → ★ 谨慎
_TAIL_PULLBACK_PP = 1.5      # 推荐后回落 ≥1.5pp 判「尾盘回吐」（追高兑现风险）


def _vol_desc(dims: dict) -> str:
    """量能描述：优先取 validator detail 文本（v_st_vol_detail 等），映射为中文。"""
    for key in ("v_st_vol_detail", "v_mo_volume_detail", "v_rb_volume_detail"):
        v = dims.get(key)
        if v:
            s = str(v)
            m = re.search(r"(\d+(?:\.\d+)?)x", s)
            if "surge" in s:
                return f"放量{m.group(1)}x" if m else "放量"
            if "stable" in s:
                return "量能平稳"
            if "healthy" in s:
                return "量能健康"
            if "shrink" in s:
                return "缩量"
            return s
    return "—"


def _tier0_verdict(entry: dict, flow_pct_map: dict) -> dict:
    """单只 🎯 票分析（纯函数，供报告渲染与测试）。

    返回 dict：symbol/name/category/score/rec_pct/accum/flow/flow_icon/band/
    pos/pos_detail/vol/risks/verdict/stars/label/reason/concept/trend。
    依赖 entry['_accum']（调用方预计算，与 display_priority 同源），缺省回退
    entry['accumulated_pct']（DB 落库，掉榜行兜底）。
    """
    sym = entry["symbol"]
    cat = entry["category"]
    dims = _entry_dims(entry)
    rec_pct = _nextday_entry_percent(entry)
    accum = entry.get("_accum")
    if accum is None:
        accum = entry.get("accumulated_pct")
    flow = dims.get("fund_flow_main_pct")
    if flow is None:
        flow = flow_pct_map.get(sym)
    flow = float(flow) if flow is not None else None
    has_live = bool(entry.get("live_quote_available") or entry.get("live_rank") is not None)
    live_pct = entry.get("live_percent")
    live_pct = float(live_pct) if (has_live and live_pct is not None) else None
    band = _entry_band(entry)
    w2s = _entry_weak_to_strong(entry)

    # 位置
    if cat == "rebound":
        pos = "超跌反弹位"
    elif cat == "short_term":
        pos = "弱转强低位" if w2s else "放量启动位"
    elif cat == "momentum":
        pos = "加速启动位"
    elif cat == "known_new_face":
        pos = "二次上榜"
    else:
        pos = "底部突破"
    pos_detail = []
    if accum is not None:
        pos_detail.append(f"5日累计{accum:+.1f}%")
    # 涨幅带仅对非 short_term 展示——2026-08-17 分型后 short_term 不看涨幅带
    # （其规律在弱转强，甜蜜带对 short_term 反而负效 5.7%），死区/陷阱标签
    # 是 momentum/new_face 口径，贴到 short_term 上会误导（如弱转强+2% 被标死区）。
    if cat != "short_term":
        if band == "sweet":
            pos_detail.append("低吸带(<2%)" if rec_pct < NEXTDAY_SPIKE_SWEET_LOW else "甜蜜中段(4-8%)")
        elif band == "trap":
            pos_detail.append("8-10%陷阱带")
        elif band == "dead":
            pos_detail.append("2-4%死区")

    # 风险（全部来自已有维度/实时数据；资金流仅规避语义）
    tail_pullback = live_pct is not None and rec_pct - live_pct >= _TAIL_PULLBACK_PP
    divergence = False
    dv = dims.get("v_mo_divergence")
    if dv is not None:
        try:
            divergence = float(dv) < 0
        except (TypeError, ValueError):
            divergence = False
    outflow = flow is not None and flow <= FUND_OUTFLOW_NET_PCT
    ob = _entry_overbought(entry)
    fd = dims.get("fatigue_detail")
    fatigue = bool(fd and not str(fd).startswith("accelerating"))
    trap = band == "trap" and cat != "short_term"

    risks = []
    if tail_pullback:
        risks.append(f"尾盘回吐(+{rec_pct:.1f}→{live_pct:+.1f}%)")
    if divergence:
        risks.append("RSI顶背离")
    if outflow:
        risks.append(f"主力净流出{flow:.1f}%")
    if ob:
        risks.append("超买")
    if fatigue:
        risks.append("疲劳")
    if trap:
        risks.append("8-10%陷阱带")

    # 评级：类别基线（rebound / short_term弱转强 = 2，其余 1）扣风险
    base = 2 if (cat == "rebound" or (cat == "short_term" and w2s)) else 1
    verdict = (base - int(tail_pullback) - int(divergence) - int(outflow)
               - 2 * int(ob) - int(fatigue) - int(trap))
    if verdict >= _VERDICT_STRONG:
        stars, label = "★★★", "首选"
    elif verdict == _VERDICT_OK:
        stars, label = "★★", "可参与"
    else:
        stars, label = "★", "谨慎"

    reason = {
        "rebound": "rebound 历史 hit 28.6% 全场最强（超跌+企稳+资金回流）",
        "short_term": "弱转强∩非超买 hit 15.8%，short_term 唯一有效子集",
        "momentum": "甜蜜带+累计≥6 hit 20%（动量加速）",
        "known_new_face": "kNF hit 12.7% 全场第二",
        "new_face": "new_face hit 9.6% 接近基准",
    }.get(cat, "")

    ff_icon = {"strong_in": "▲▲", "in": "▲", "neutral": "◇",
               "out": "▼", "strong_out": "▼▼"}.get(fund_flow_signal(flow), "")

    return {
        "symbol": sym, "name": entry["name"], "category": cat,
        "score": entry["score"], "rec_pct": rec_pct, "accum": accum,
        "flow": flow, "flow_icon": ff_icon, "band": band,
        "pos": pos, "pos_detail": pos_detail, "vol": _vol_desc(dims),
        "risks": risks, "verdict": verdict, "stars": stars, "label": label,
        "reason": reason, "concept": entry.get("concept", ""), "trend": entry.get("trend", ""),
    }


# ── 报告组装（与 display_priority 同源的档位/🎯 判定）──
def _build_report(conn: sqlite3.Connection, target_date: str, top_n: int) -> dict:
    recs = get_today_recommendations(conn, as_of=target_date)
    if not recs:
        return {"date": target_date, "empty": True}

    flow_map = get_fund_flow_pct_map(conn, [e["symbol"] for e in recs])
    for e in recs:
        acc = _nextday_entry_accum(e, conn)
        e["_accum"] = acc
        e["_marked"] = _is_nextday_marked(e, conn, accum=acc)
        e["_tier"] = _entry_tier(e, conn, accum=acc, marked=e["_marked"])

    main = [e for e in recs if e["category"] != "comeback"]
    comeback = [e for e in recs if e["category"] == "comeback"]
    main.sort(key=lambda x: (x["_tier"], CAT_DISPLAY_PRIORITY.get(x["category"], 99), -x["score"]))

    tier0 = [e for e in main if e["_tier"] == 0][:top_n] if top_n else [e for e in main if e["_tier"] == 0]
    tier1 = [e for e in main if e["_tier"] == 1]
    tier2 = [e for e in main if e["_tier"] == 2]
    tier3 = [e for e in main if e["_tier"] == 3]

    # 档0 分析 + 组内相对排序
    analyzed = [_tier0_verdict(e, flow_map) for e in tier0]
    analyzed.sort(key=lambda a: (-a["verdict"], -a["score"]))

    # 档3 避雷汇总（统计劣后原因）
    tier3_reasons: dict[str, int] = {}
    for e in tier3:
        acc = e["_accum"]
        d = _entry_dims(e)
        band = _entry_band(e)
        flow = d.get("fund_flow_main_pct")
        if flow is None:
            flow = flow_map.get(e["symbol"])
        flow = float(flow) if flow is not None else None
        cnt = (d.get("v_st_sector_count") or d.get("v_pb_sector_count")
               or d.get("v_nf_sector_count") or 0)
        if acc is not None and acc >= 50:
            tier3_reasons["累计过热≥50"] = tier3_reasons.get("累计过热≥50", 0) + 1
        if _entry_overbought(e):
            tier3_reasons["超买"] = tier3_reasons.get("超买", 0) + 1
        if flow is not None and flow <= FUND_OUTFLOW_NET_PCT:
            tier3_reasons["主力净流出≤-8%"] = tier3_reasons.get("主力净流出≤-8%", 0) + 1
        if cnt and cnt < 15:
            tier3_reasons["小板块共振cnt<15"] = tier3_reasons.get("小板块共振cnt<15", 0) + 1
        if band in ("dead", "trap") and e["category"] != "short_term":
            tier3_reasons["涨幅带死区/陷阱"] = tier3_reasons.get("涨幅带死区/陷阱", 0) + 1

    # 回马枪资金质量
    cb_flow = []
    for e in sorted(comeback, key=lambda x: -x["score"]):
        d = _entry_dims(e)
        flow = d.get("fund_flow_main_pct")
        if flow is None:
            flow = flow_map.get(e["symbol"])
        flow = float(flow) if flow is not None else None
        variant = d.get("comeback_variant") or str(e.get("trend", "")).split("·")[0]
        signals = d.get("comeback_signals", "")
        cb_flow.append({
            "symbol": e["symbol"], "name": e["name"], "score": e["score"],
            "variant": variant, "signals": signals, "flow": flow,
        })

    # 数据质量（scan_quality_log）+ 数据新鲜度（盘中运行语义：截至最近一次扫描轮）
    quality = {}
    quality_time = None
    try:
        row = conn.execute(
            "SELECT gem_count, fetch_failed, today_bar_missing, minute_fallback, stale_recs, updated "
            "FROM scan_quality_log WHERE date = ? ORDER BY updated DESC LIMIT 1",
            (target_date,),
        ).fetchone()
        if row:
            quality = {"gem_count": row[0], "fetch_failed": row[1],
                       "today_bar_missing": row[2], "minute_fallback": row[3],
                       "stale_recs": row[4]}
            quality_time = row[5]
    except Exception:
        quality = {}

    # 最近推荐时间：盘中报告 = 截至当前扫描轮的当日累计推荐，标注新鲜度避免误读
    last_rec_time = None
    try:
        row = conn.execute(
            "SELECT MAX(time) FROM recommendations WHERE date = ?", (target_date,),
        ).fetchone()
        last_rec_time = row[0] if row and row[0] else None
    except Exception:
        last_rec_time = None

    # 被移出票（硬过滤/反转移出，不在综合排序展示）
    excluded = []
    try:
        rows = conn.execute(
            "SELECT symbol, name, category, score FROM recommendations "
            "WHERE date = ? AND COALESCE(excluded, 0) = 1 ORDER BY score DESC",
            (target_date,),
        ).fetchall()
        excluded = [{"symbol": r[0], "name": r[1], "category": r[2], "score": r[3]} for r in rows]
    except Exception:
        excluded = []

    return {
        "date": target_date, "empty": False,
        "total": len(recs), "main": len(main), "comeback": len(comeback),
        "tier0": analyzed, "tier1": [
            {"symbol": e["symbol"], "name": e["name"], "category": e["category"],
             "score": e["score"], "trend": e.get("trend", "")} for e in tier1],
        "tier2": [{"symbol": e["symbol"], "name": e["name"], "category": e["category"],
                    "score": e["score"], "trend": e.get("trend", "")} for e in tier2],
        "tier3": [{"symbol": e["symbol"], "name": e["name"], "category": e["category"],
                   "score": e["score"], "rec_pct": e.get("percent", 0.0),
                   "trend": e.get("trend", "")} for e in tier3],
        "tier3_reasons": tier3_reasons,
        "comeback_flow": cb_flow,
        "excluded": excluded,
        "quality": quality,
        "quality_time": quality_time,
        "last_rec_time": last_rec_time,
    }


# ── 渲染 ──
def _fmt_flow(flow: float | None) -> str:
    if flow is None:
        return "资金—"
    icon = {"strong_in": "▲▲", "in": "▲", "neutral": "◇",
            "out": "▼", "strong_out": "▼▼"}.get(fund_flow_signal(flow), "")
    return f"{icon}主力{flow:+.1f}%"


def _verdict_color(verdict: int) -> str:
    if verdict >= _VERDICT_STRONG:
        return ANSI["GREEN"]
    if verdict == _VERDICT_OK:
        return ANSI["YELLOW"]
    return ANSI["RED"]


def _render(report: dict) -> str:
    if report.get("empty"):
        return f"{report['date']} 无推荐记录。"
    out = []
    d = report["date"]
    out.append(f"\n{ANSI['BOLD']}◆ {d} 综合排序分析报告{ANSI['RESET']}（本地数据·仅展示不改评分）")

    # 一、总览
    out.append(f"\n{ANSI['BOLD']}一、总览{ANSI['RESET']}")
    q = report["quality"]
    q_str = "无扫描质量日志" if not q else (
        f"gem={q['gem_count']} 拉取失败={q['fetch_failed']} 缺今日bar={q['today_bar_missing']} "
        f"分时兜底={q['minute_fallback']} 旧缓存评分={q['stale_recs']}")
    out.append(f"  推荐 {report['total']} 只（主表 {report['main']} + 回马枪 {report['comeback']}）| "
               f"档0🎯 {len(report['tier0'])} 只 / 档1强信号 {len(report['tier1'])} / "
               f"档3警示 {len(report['tier3'])} | 数据质量: {q_str}")
    fresh = f"最近推荐 {report.get('last_rec_time') or '—'}"
    if report.get("quality_time"):
        fresh += f" · 质量快照 {str(report['quality_time'])[:8]}"
    out.append(f"  数据新鲜度: {fresh}（盘中运行 = 截至最近一次扫描轮的当日累计推荐）")
    if report["excluded"]:
        excl = "、".join(f"{x['name']}({x['category']})" for x in report["excluded"])
        out.append(f"  被移出（硬过滤/反转移出，不展示）: {excl}")

    # 二、档0 个股分析
    out.append(f"\n{ANSI['BOLD']}二、🎯 档0 个股分析（次日大涨画像·选股决策参考）{ANSI['RESET']}")
    if not report["tier0"]:
        out.append("  今日无 🎯 档0 票。")
    else:
        for i, a in enumerate(report["tier0"], 1):
            cat_label = CAT_COLOR.get(a["category"], "") + CAT_LABEL.get(a["category"], a["category"]) + ANSI["RESET"]
            out.append(f"  {i}. {ANSI['BOLD']}{a['name']}{ANSI['RESET']} {a['symbol']} "
                       f"{cat_label} 评分{a['score']} ── {a['concept']}·{a['trend']}")
            pos_detail = "·".join(a["pos_detail"]) or "位置数据缺"
            out.append(f"     位置: {a['pos']}（{pos_detail}）| 资金: {_fmt_flow(a['flow'])} | 量能: {a['vol']}")
            risk_str = "、".join(a["risks"]) if a["risks"] else "无显著"
            vc = _verdict_color(a["verdict"])
            out.append(f"     风险: {risk_str} | 结论: {vc}{a['stars']} {a['label']}{ANSI['RESET']} ── {a['reason']}")
        order = " > ".join(f"{a['name']}({a['stars']})" for a in report["tier0"])
        out.append(f"  {ANSI['CYAN']}组内相对排序（按评级）：{order}{ANSI['RESET']}")

    # 三、档1 强信号
    if report["tier1"]:
        out.append(f"\n{ANSI['BOLD']}三、档1 强信号（rebound 等）{ANSI['RESET']}")
        for e in report["tier1"]:
            out.append(f"  {e['name']} {e['symbol']} {e['category']} 评分{e['score']} ── {e['trend']}")

    # 四、档3 警示劣后（避雷）
    out.append(f"\n{ANSI['BOLD']}四、档3 警示劣后（避雷提示）{ANSI['RESET']}")
    if not report["tier3"]:
        out.append("  今日无警示劣后票。")
    else:
        reasons = "、".join(f"{k}×{v}" for k, v in sorted(report["tier3_reasons"].items(),
                                                          key=lambda x: -x[1]))
        out.append(f"  {len(report['tier3'])} 只，劣后原因: {reasons}")
        out.append("  全部均为板块普涨/超买/资金流出等避雷区，追高次日大概率兑现。")

    # 五、回马枪资金质量
    out.append(f"\n{ANSI['BOLD']}五、回马枪（掉榜跟踪·cum_3d 语义·参考）{ANSI['RESET']}")
    if not report["comeback_flow"]:
        out.append("  今日无回马枪。")
    else:
        for c in report["comeback_flow"]:
            flow_str = _fmt_flow(c["flow"])
            verdict = "资金回流可取" if (c["flow"] is not None and c["flow"] >= 5) else (
                "资金背离回避" if (c["flow"] is not None and c["flow"] <= FUND_OUTFLOW_NET_PCT) else "中性观察")
            out.append(f"  {c['name']} {c['symbol']} 评分{c['score']} [{c['variant']}] "
                       f"{flow_str} → {verdict}（信号: {c['signals'] or '—'}）")

    # 六、结论
    out.append(f"\n{ANSI['BOLD']}六、结论{ANSI['RESET']}")
    picks = [a for a in report["tier0"] if a["verdict"] >= _VERDICT_STRONG]
    watches = [a for a in report["tier0"] if a["verdict"] == _VERDICT_OK]
    if picks:
        out.append(f"  {ANSI['GREEN']}首选（★★★）: {'、'.join(a['name'] for a in picks)}{ANSI['RESET']}")
    if watches:
        out.append(f"  观察（★★）: {'、'.join(a['name'] for a in watches)}")
    if report["tier3"]:
        names = "、".join(e["name"] for e in report["tier3"][:8])
        if len(report["tier3"]) > 8:
            names += "…"
        out.append(f"  {ANSI['RED']}回避（档3 板块普涨/超买/资金流出）: {names}{ANSI['RESET']}")
    out.append("")
    out.append("  说明: 评级依据已回测结论（nextday_attribution 口径）；资金流仅作规避信号；")
    out.append("  盘中运行：推荐清单为当日累计（截至最近一次扫描），今日bar缺失票累计回退历史口径")
    out.append("  本报告为筛选系统选股决策参考，非交易指令。")
    return "\n".join(out)


def main():
    parser = argparse.ArgumentParser(description="今日综合排序分析报告")
    parser.add_argument("--date", default=None, help="目标日期 YYYY-MM-DD，默认最近有数据的交易日")
    parser.add_argument("--top", type=int, default=None, help="档0 个股分析最多 N 只")
    parser.add_argument("--json", action="store_true", help="机器可读 JSON 输出")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH, timeout=15)  # 盘中与扫描器并发读写：忙等待上限 15s，避免偶发 locked
    target = args.date
    if not target:
        row = conn.execute("SELECT MAX(date) FROM recommendations").fetchone()
        target = row[0] if row and row[0] else now_beijing().date().isoformat()
    report = _build_report(conn, target, args.top)
    conn.close()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    else:
        print(_render(report))


if __name__ == "__main__":
    main()
