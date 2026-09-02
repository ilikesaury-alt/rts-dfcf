#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""综合排序历史复盘（2026-08-18 新增）：把历史 N 日的「综合排序各组 → 次日表现」
汇总统计，检验档位排序是否真的有效。

用户诉求（2026-08-18）：昨日复盘只看单日太单一——把前面所有交易日的综合排序
数据（档0🎯/档1强信号/档2普通/档3警示/回马枪/被移出）与次日表现（next_day_pct
落库回填）汇总，回答：档0 是否真的比档3 好？回马枪值不值得看？避雷区是否避开了
大坑？普涨日/普跌日次日表现差异？

口径：
  - 档位/🎯 判定逐日重建，与 today_report.py / display_priority 同源（_build_report，
    纯展示规则回放历史 = 「若今日规则应用在历史日」视角）。
  - 表现 = next_day_pct（daily_kline 收盘回填，与 nextday_attribution 一致）；
    hit 率/均值统计复用 nextday_attribution._hit_stats（主决策口径，防漂移）。
  - 市场环境 = 推荐日全 GEM 样本均值（daily_kline percent 代理）。
  - ⚠️ 08-04 前落库 score_breakdown 缺超买/弱转强维度：档0 的超买排除与 short_term
    弱转强分型不生效（fail-open），档0 判定退化——近端窗口（默认最近 30 日）可信，
    --days 0 全期对照仅供参考。

定位（2026-08-18 方案A，用户确认「本工具本质是回测」）：AGENTS.md 回测定位框架下
的**第 3 个合法用途——档位排序自检尺**（display 层专属）：校验综合排序 4 级档位
（档0🎯/档1/档2/档3）与回马枪/被移出的历史次日表现是否与排序一致（档0 > 档3 等），
回答「综合排序的排序键是否可信」。不改评分不落库，不进入实时扫描路径；**不得用于
调权重/调档位因子**（4 级档位 08-17 才引入，历史样本有限，调参过拟合风险高）——
调参仍走 scanner.backtest / nextday_attribution（主决策口径）。

用法：
    python prevday_perf.py                 # 最近 30 交易日
    python prevday_perf.py --days 0        # 全期（57 天）
    python prevday_perf.py --days 10       # 自定义窗口
    python prevday_perf.py --json          # 机器可读 JSON
"""
import argparse
import json
import sqlite3
import statistics
import sys
from collections import defaultdict

sys.stdout.reconfigure(encoding="utf-8")

from scanner.config import DB_PATH  # noqa: E402  (reconfigure 后导入避免编码异常)
from scanner.data_health import check_kline_health, health_banner  # noqa: E402
from scanner.nextday_attribution import DEFAULT_THRESHOLD, _hit_stats  # noqa: E402  (复用主决策口径统计，防口径漂移)
from today_report import _build_report  # noqa: E402

# 档位组标签（与 today_report/_entry_tier 对应）
GROUPS = ("tier0", "tier1", "tier2", "tier3", "comeback", "core_dip", "excluded")
GROUP_LABEL = {
    "tier0": "档0 🎯 次日大涨画像",
    "tier1": "档1 强信号",
    "tier2": "档2 普通",
    "tier3": "档3 警示劣后",
    "comeback": "回马枪",
    "core_dip": "核心方向低吸",
    "excluded": "被移出",
}
HIT_THRESHOLD = DEFAULT_THRESHOLD   # 次日大涨阈值（与 nextday_attribution 主决策口径同源，防漂移）
MARKET_UP = 1.0       # 普涨日（推荐日 GEM 均值 ≥ +1%）
MARKET_DOWN = -1.0    # 普跌日（≤ -1%）
# 近端可信窗口起点（08-04 起落库维度含超买/弱转强，档0 判定完整）
DIMS_COMPLETE_SINCE = "2026-08-04"


def _gem_market_avg(conn, date):
    """推荐日市场环境代理：当日全 GEM 样本涨跌均值（daily_kline percent）。"""
    try:
        row = conn.execute(
            "SELECT AVG(percent) FROM daily_kline WHERE date = ? "
            "AND (symbol LIKE 'SZ300%' OR symbol LIKE 'SZ301%') AND percent IS NOT NULL",
            (date,),
        ).fetchone()
        return row[0] if row and row[0] is not None else None
    except Exception:
        return None


def _next_day_map(conn, date):
    """当日推荐 → 次日涨跌（next_day_pct 落库回填值）。"""
    rows = conn.execute(
        "SELECT symbol, next_day_pct FROM recommendations "
        "WHERE date = ? AND next_day_pct IS NOT NULL", (date,),
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def _stats(pcts):
    """(n, 均值, hit≥7%率, 胜率>0, 中位)；空样本返回 n=0。

    2026-08-18 方案A（用户确认：本工具本质是回测，应与既有回测口径一致）：
    hit 数/率与均值复用 nextday_attribution._hit_stats（主决策口径，避免两套统计
    逻辑漂移）；胜率/中位为本工具补充展示（_hit_stats 不含）。"""
    ps = [p for p in pcts if p is not None]
    n = len(ps)
    if not n:
        return (0, None, None, None, None)
    _hits, hr, avg = _hit_stats([{"next_day": p} for p in ps], HIT_THRESHOLD)
    win = sum(1 for p in ps if p > 0) / n * 100
    return (n, avg, hr * 100, win, statistics.median(ps))


def _market_bucket(avg):
    if avg is None:
        return "未知"
    if avg >= MARKET_UP:
        return "普涨日(≥+1%)"
    if avg <= MARKET_DOWN:
        return "普跌日(≤-1%)"
    return "震荡日"


def _case(items, d):
    """把一组条目收敛成案例样本。

    2026-08-29：原实现定义在 `_build_history` 的 `for d in dates` 循环体内并闭包
    捕获 `d`（ruff B023）。当前之所以正确，只是因为三次调用都在同一次迭代内立即
    发生——一旦有人把 _case 存起来延后调用，所有案例都会拿到循环最后一日的日期。
    改为显式传参 + 提到模块级，消除这个陷阱。
    """
    return [{"date": d, "name": x["name"], "symbol": x["symbol"],
             "score": x["score"], "next": x["next"]} for x in items]


def _build_history(conn, dates):
    """逐日重建综合排序并收集各组次日表现（一次遍历，全部数据就绪）。"""
    history = []
    for d in dates:
        rep = _build_report(conn, d, None)
        if rep.get("empty"):
            continue
        nd_map = _next_day_map(conn, d)
        day = {"date": d, "market": _gem_market_avg(conn, d)}

        # 档0 内部类别（含 short_term 弱转强子集）→ 次日
        t0_cats: dict[str, list] = {}
        for a in rep["tier0"]:
            key = ("short_term·弱转强" if (a["category"] == "short_term" and a["pos"] == "弱转强低位")
                   else "short_term·其他" if a["category"] == "short_term" else a["category"])
            p = nd_map.get(a["symbol"])
            if p is not None:
                t0_cats.setdefault(key, []).append(p)
        day["tier0_cats"] = t0_cats

        groups = {
            "tier0": [a["symbol"] for a in rep["tier0"]],
            "tier1": [e["symbol"] for e in rep["tier1"]],
            "tier2": [e["symbol"] for e in rep["tier2"]],
            "tier3": [e["symbol"] for e in rep["tier3"]],
            "comeback": [c["symbol"] for c in rep["comeback_flow"]],
            "core_dip": [c["symbol"] for c in rep["core_dip"]],
            "excluded": [e["symbol"] for e in rep["excluded"]],
        }
        for g, syms in groups.items():
            day[g] = [nd_map[s] for s in syms if s in nd_map]

        # 案例样本（档0 命中 / 档3 大坑 / 回马枪最佳）
        day["cases"] = {
            "tier0": _case([{"name": a["name"], "symbol": a["symbol"], "score": a["score"],
                              "next": nd_map.get(a["symbol"])} for a in rep["tier0"]
                             if a["symbol"] in nd_map], d),
            "tier3": _case([{"name": e["name"], "symbol": e["symbol"], "score": e["score"],
                              "next": nd_map.get(e["symbol"])} for e in rep["tier3"]
                             if e["symbol"] in nd_map], d),
            "comeback": _case([{"name": c["name"], "symbol": c["symbol"], "score": c["score"],
                                 "next": nd_map.get(c["symbol"])} for c in rep["comeback_flow"]
                                if c["symbol"] in nd_map], d),
        }
        history.append(day)
    return history


def _fmt_pct(v, width=7):
    if v is None:
        return "—".rjust(width)
    return f"{v:+.2f}%".rjust(width)


def _render(hist, days_arg):
    out = []
    dates = [h["date"] for h in hist]
    start, end = dates[0], dates[-1]
    recent = [h for h in hist if h["date"] >= DIMS_COMPLETE_SINCE]
    window = (f"最近 {len(hist)} 交易日（{start} ~ {end}）" if days_arg
              else f"全期 {len(hist)} 交易日（{start} ~ {end}）")
    out.append(f"\n◆ 综合排序历史复盘：{window}")
    out.append(f"  口径：档位/🎯 逐日重建（today_report 同源）；表现=次日收盘涨跌（next_day_pct，"
               f"hit 阈值 ≥+{HIT_THRESHOLD:.0f}%）；市场=推荐日 GEM 均值代理")
    if recent and recent != hist:
        out.append(f"  ⚠️ {DIMS_COMPLETE_SINCE} 前落库缺超买/弱转强维度，档0 判定退化——"
                   f"下方主表用近端窗口（{recent[0]['date']} 起，{len(recent)} 日，维度完整）")

    def _table(title, rows):
        out.append(f"\n{title}")
        out.append(f"  {'组':<22}{'n':>5} {'均次日':>8} {'hit≥7%':>8} {'胜率':>7} {'中位':>8}")
        out.append("  " + "-" * 58)
        for label, stats in rows:
            n, avg, hit, win, med = stats
            if n == 0:
                out.append(f"  {label:<22}{0:>5} {'—':>8} {'—':>8} {'—':>7} {'—':>8}")
            else:
                out.append(f"  {label:<22}{n:>5} {_fmt_pct(avg)} {f'{hit:.1f}%':>8} "
                           f"{f'{win:.1f}%':>7} {_fmt_pct(med)}")

    # 一、各组次日表现（近端窗口为主表，全期对照）
    base = recent if recent else hist
    agg = {g: [] for g in GROUPS}
    for h in base:
        for g in GROUPS:
            agg[g].extend(h[g])
    _table("一、各组次日表现（维度完整窗口）", [(GROUP_LABEL[g], _stats(agg[g])) for g in GROUPS])
    if recent and recent != hist:
        agg_all = {g: [] for g in GROUPS}
        for h in hist:
            for g in GROUPS:
                agg_all[g].extend(h[g])
        _table("对照：全期（含维度退化期，仅供参考）",
               [(GROUP_LABEL[g], _stats(agg_all[g])) for g in GROUPS])

    # 二、档0 内部类别 × 次日
    t0_cat_agg: dict[str, list] = {}
    for h in base:
        for key, pcts in h["tier0_cats"].items():
            t0_cat_agg.setdefault(key, []).extend(pcts)
    if t0_cat_agg:
        _table("二、档0 内部（类别 × 次日）",
               [(k, _stats(v)) for k, v in sorted(t0_cat_agg.items())])

    # 三、市场环境分层（推荐日 GEM 均值 → 次日表现）
    buckets = {"普涨日(≥+1%)": {"tier0": [], "tier3": [], "all": []},
               "震荡日": {"tier0": [], "tier3": [], "all": []},
               "普跌日(≤-1%)": {"tier0": [], "tier3": [], "all": []}}
    for h in base:
        b = _market_bucket(h["market"])
        if b not in buckets:
            continue
        buckets[b]["tier0"].extend(h["tier0"])
        buckets[b]["tier3"].extend(h["tier3"])
        buckets[b]["all"].extend(h["tier0"] + h["tier1"] + h["tier2"] + h["tier3"])
    out.append("\n三、市场环境分层（推荐日 GEM 均值 → 次日表现）")
    out.append(f"  {'市场':<14}{'组':<24}{'n':>5} {'均次日':>8} {'hit≥7%':>8}")
    out.append("  " + "-" * 60)
    for b, d in buckets.items():
        for label, key in (("档0 🎯", "tier0"), ("档3 警示", "tier3"), ("全部主表", "all")):
            n, avg, hit, _, _ = _stats(d[key])
            if n == 0:
                continue
            out.append(f"  {b:<14}{label:<24}{n:>5} {_fmt_pct(avg)} {f'{hit:.1f}%':>8}")

    # 四、档0 vs 档3 单调性（每日对比）
    t0_win = t3_win = tie = 0
    for h in base:
        if not h["tier0"] or not h["tier3"]:
            continue
        a, b = statistics.mean(h["tier0"]), statistics.mean(h["tier3"])
        if a > b:
            t0_win += 1
        elif a < b:
            t3_win += 1
        else:
            tie += 1
    s0 = _stats(agg["tier0"])
    s3 = _stats(agg["tier3"])
    out.append("\n四、档0 vs 档3 单调性（逐日对比）")
    if s0[0] and s3[0]:
        out.append(f"  档0 胜 {t0_win} 天 / 档3 胜 {t3_win} 天 / 平 {tie} 天"
                   f"（档0 整体 {s0[1]:+.2f}% vs 档3 {s3[1]:+.2f}%）")

    # 五、案例
    all_cases = {g: [c for h in hist for c in h["cases"][g]] for g in ("tier0", "tier3", "comeback")}
    out.append("\n五、案例")
    hits = sorted(all_cases["tier0"], key=lambda x: x["next"], reverse=True)[:5]
    if hits:
        out.append("  🎯 档0 次日大涨 top5（信号命中案例）：")
        for c in hits:
            out.append(f"    {c['date']} {c['name']} {c['symbol'][-6:]} 评分{c['score']} → 次日 {c['next']:+.2f}%")
    pits = sorted(all_cases["tier3"], key=lambda x: x["next"])[:5]
    if pits:
        out.append("  档3 最大坑 top5（避雷价值）：")
        for c in pits:
            out.append(f"    {c['date']} {c['name']} {c['symbol'][-6:]} 评分{c['score']} → 次日 {c['next']:+.2f}%")
    cb_best = sorted(all_cases["comeback"], key=lambda x: x["next"], reverse=True)[:3]
    if cb_best:
        out.append("  回马枪最佳 top3：")
        for c in cb_best:
            out.append(f"    {c['date']} {c['name']} {c['symbol'][-6:]} 评分{c['score']} → 次日 {c['next']:+.2f}%")

    # 六、结论（数据驱动）
    out.append("\n六、结论")
    t0_avg, t0_hit = s0[1], s0[2]
    c_avg, c_hit = _stats(agg["comeback"])[1], _stats(agg["comeback"])[2]
    e_avg = _stats(agg["excluded"])[1]
    verdicts = []
    if s0[0] >= 30:
        if t0_avg is not None and s3[1] is not None and t0_avg > s3[1]:
            verdicts.append(f"档位排序有效：档0 均次日 {t0_avg:+.2f}% > 档3 {s3[1]:+.2f}%"
                            f"（hit {t0_hit:.1f}% vs {s3[2]:.1f}%），胜 {t0_win} 天")
        else:
            verdicts.append(f"档位排序未跑赢：档0 {t0_avg:+.2f}% ≤ 档3 {s3[1]:+.2f}%"
                            f"（hit {t0_hit:.1f}% vs {s3[2]:.1f}%），样本 {s0[0]}")
    if _stats(agg["comeback"])[0] >= 20:
        verdicts.append(f"回马枪（低吸语义）整体 {c_avg:+.2f}%/hit {c_hit:.1f}%——"
                        + ("在回调日更抗跌" if c_avg is not None and c_avg > (t0_avg or 0) else "与主表相当"))
    if _stats(agg["excluded"])[0] >= 10 and e_avg is not None and e_avg < 0:
        verdicts.append(f"被移出票均次日 {e_avg:+.2f}%——硬过滤/反转移出排除有效（避开了下跌）")
    up_s, dn_s = _stats(buckets["普涨日(≥+1%)"]["all"]), _stats(buckets["普跌日(≤-1%)"]["all"])
    if up_s[0] >= 20 and dn_s[0] >= 20 and up_s[1] is not None and dn_s[1] is not None:
        diff = up_s[1] - dn_s[1]
        verdicts.append(f"市场环境影响显著：普涨日次日 {up_s[1]:+.2f}%（hit {up_s[2]:.1f}%）vs "
                        f"普跌日次日 {dn_s[1]:+.2f}%（hit {dn_s[2]:.1f}%），差 {diff:+.2f}pp——"
                        + ("普涨次日兑现，追高需谨慎" if diff < 0 else "普涨次日延续性强"))
    if not verdicts:
        verdicts.append("样本不足（窗口内有效交易日太少），结论待数据积累。")
    for v in verdicts:
        out.append(f"  • {v}")
    out.append("")
    out.append("  说明：本复盘为筛选系统选股质量自检尺（校验档位/避雷/低吸假设），"
               "非实盘收益预测；单日样本小，结论以整体统计为准。")
    return "\n".join(out)


def _rejection_audit(conn):
    """硬过滤审计原始数据：入选 vs 落选 次日表现 + 分月漂移。

    - 入选 = recommendations.next_day_pct（硬过滤幸存组，进了推荐列表）
    - 落选 = scan_rejections.next_day_pct（被硬过滤移出组，独立审计表）

    两者口径同源（daily_kline 收盘回填），直接对比即可回答「硬过滤到底有没有用」，
    规避「只看活下来的票」的幸存者偏差。本函数只读，不落库、不改评分。
    """
    sel = [r[0] for r in conn.execute(
        "SELECT next_day_pct FROM recommendations WHERE next_day_pct IS NOT NULL").fetchall()]
    rej = [r[0] for r in conn.execute(
        "SELECT next_day_pct FROM scan_rejections WHERE next_day_pct IS NOT NULL").fetchall()]
    sel_month: dict[str, list] = defaultdict(list)
    for d, v in conn.execute(
        "SELECT date, next_day_pct FROM recommendations WHERE next_day_pct IS NOT NULL").fetchall():
        sel_month[d[:7]].append(v)
    rej_month: dict[str, list] = defaultdict(list)
    for d, v in conn.execute(
        "SELECT date, next_day_pct FROM scan_rejections WHERE next_day_pct IS NOT NULL").fetchall():
        rej_month[d[:7]].append(v)
    return {"selected": sel, "rejected": rej,
            "selected_month": dict(sel_month), "rejected_month": dict(rej_month)}


def _render_rejection_audit(audit):
    """硬过滤审计报表：入选 vs 落选 对比 + 分月漂移。"""
    out = []
    out.append("\n七、入选 vs 落选（硬过滤审计，规避幸存者偏差）")
    out.append(f"  口径：入选=recommendations 次日 / 落选=scan_rejections 次日（同源 daily_kline 收盘）；"
               f"hit 阈值 ≥+{HIT_THRESHOLD:.0f}%")
    s_sel = _stats(audit["selected"])
    s_rej = _stats(audit["rejected"])
    out.append(f"  {'组':<22}{'n':>5} {'均次日':>8} {'hit≥7%':>8} {'胜率':>7} {'中位':>8}")
    out.append("  " + "-" * 58)
    for label, st in (("入选（硬过滤幸存）", s_sel), ("落选（被硬过滤移出）", s_rej)):
        n, avg, hit, win, med = st
        if n == 0:
            out.append(f"  {label:<22}{0:>5} {'—':>8} {'—':>8} {'—':>7} {'—':>8}")
        else:
            out.append(f"  {label:<22}{n:>5} {_fmt_pct(avg)} {f'{hit:.1f}%':>8} "
                       f"{f'{win:.1f}%':>7} {_fmt_pct(med)}")

    # 结论（数据驱动；-2pp 显著线来自 Phase 1 验证门约定）
    # 最小样本护栏：落选需 ≥10 条、入选需 ≥30 条才有统计意义——否则 1~2 条落选的
    # 次日涨跌会把结论带偏（正是 Phase 1 要规避的「小样本噪声当成结论」陷阱）。
    out.append("\n  结论：")
    if s_sel[0] >= 30 and s_rej[0] >= 10 and s_rej[1] is not None and s_sel[1] is not None:
        diff = s_rej[1] - s_sel[1]
        if diff <= -2:
            out.append(f"    • 硬过滤有效：落选均次日 {s_rej[1]:+.2f}% 显著低于入选 {s_sel[1]:+.2f}%（差 {diff:+.2f}pp）")
        elif diff >= 0:
            out.append(f"    • ⚠ 硬过滤疑似无效：落选均次日 {s_rej[1]:+.2f}% 未低于（甚至高于）入选 {s_sel[1]:+.2f}%（差 {diff:+.2f}pp）")
        else:
            out.append(f"    • 硬过滤轻度有效：落选均次日 {s_rej[1]:+.2f}% 略低于入选 {s_sel[1]:+.2f}%（差 {diff:+.2f}pp，未达 -2pp 显著线）")
    else:
        out.append(f"    • 落选样本不足（入选 n={s_sel[0]}，落选 n={s_rej[0]}；需落选 ≥10），"
                   f"硬过滤有效性待积累——scan_rejections 的 next_day_pct 由 backfill_kline.py 每日收盘后回填，"
                   f"随扫描天数增长自然达到判定门槛")

    # 分月漂移（入选 vs 落选 均次日，弱市低吸扩容失效的早期预警）
    months = sorted(set(audit["selected_month"]) | set(audit["rejected_month"]))
    if months:
        out.append("\n  分月漂移（入选 vs 落选 均次日）：")
        out.append(f"  {'月份':<10}{'入选n':>6} {'入选均':>8} {'落选n':>6} {'落选均':>8} {'差':>8}")
        out.append("  " + "-" * 52)
        for m in months:
            sv = audit["selected_month"].get(m, [])
            rv = audit["rejected_month"].get(m, [])
            sa = (sum(sv) / len(sv)) if sv else None
            ra = (sum(rv) / len(rv)) if rv else None
            sas = _fmt_pct(sa) if sa is not None else "—"
            ras = _fmt_pct(ra) if ra is not None else "—"
            diff_s = f"{(ra - sa):+.2f}%" if (sa is not None and ra is not None) else "—"
            out.append(f"  {m:<10}{len(sv):>6} {sas:>8} {len(rv):>6} {ras:>8} {diff_s:>8}")
    return "\n".join(out)


def main():
    parser = argparse.ArgumentParser(description="综合排序历史复盘：各组次日表现汇总")
    parser.add_argument("--days", type=int, default=30, help="最近 N 个交易日（0=全期）")
    parser.add_argument("--json", action="store_true", help="机器可读 JSON 输出")
    parser.add_argument("--force", action="store_true",
                        help="跳过数据健康检查（交叉验证不符时仍强行出报告）")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH, timeout=15)
    rows = conn.execute("SELECT DISTINCT date FROM recommendations ORDER BY date").fetchall()
    all_dates = [r[0] for r in rows]
    # 只保留有次日表现的日期（最新一天 next_day_pct 未回填则自动排除）
    dates = [d for d in all_dates if _next_day_map(conn, d)]
    if args.days and args.days > 0:
        dates = dates[-args.days:]
    # 数据真实性前置检查（2026-08-18 拓斯达脏数据事故）：复盘口径基于落库
    # next_day_pct（daily_kline 回填），脏 bar 会静默污染各组统计；出报告前抽样
    # 与独立源交叉验证，不符比例超阈值即中止。
    if not args.force:
        report = check_kline_health(conn, dates=dates or None)
        banner = health_banner(report)
        if banner:
            print(banner)
        if report.blocked:
            print("  [中止] 数据疑似污染，先跑 python repair_kline.py 修复后重试"
                  "（--force 强行出报告）")
            conn.close()
            return
        # 大盘指数对账（2026-08-19）：大盘标签曾把当日 -6.26% 崩盘读成昨日 -0.93%
        # （展示"大盘中性"）而无痕——涨幅不进 daily_kline，上面的 K 线交叉验证覆盖
        # 不到。读 market_index_log 血缘记录对账独立源（东财），旧 bar/偏差即告警。
        from scanner.data_health import check_market_index_health, index_health_banner

        idx_report = check_market_index_health(conn)
        idx_banner = index_health_banner(idx_report)
        if idx_banner:
            print(idx_banner)
    hist = _build_history(conn, dates)
    audit = _rejection_audit(conn)
    conn.close()

    if args.json:
        base = [h for h in hist if h["date"] >= DIMS_COMPLETE_SINCE] or hist
        payload = {
            "window": f"{hist[0]['date']}~{hist[-1]['date']}" if hist else "",
            "days": len(hist),
            "days_arg": args.days,
            "dim_complete_since": DIMS_COMPLETE_SINCE,
            "groups": {},
        }
        for g in GROUPS:
            pcts = [p for h in base for p in h[g]]
            n, avg, hit, win, med = _stats(pcts)
            payload["groups"][g] = {"n": n, "avg": avg, "hit": hit, "win": win, "median": med}
        s_sel = _stats(audit["selected"])
        s_rej = _stats(audit["rejected"])
        payload["rejection_audit"] = {
            "selected": {"n": s_sel[0], "avg": s_sel[1], "hit": s_sel[2], "win": s_sel[3], "median": s_sel[4]},
            "rejected": {"n": s_rej[0], "avg": s_rej[1], "hit": s_rej[2], "win": s_rej[3], "median": s_rej[4]},
        }
        print(json.dumps(payload, ensure_ascii=False, indent=1))
    else:
        print(_render(hist, args.days))
        print(_render_rejection_audit(audit))


if __name__ == "__main__":
    main()
