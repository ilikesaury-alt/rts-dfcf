"""决策层（2026-09-04）：每日 ≤3 只的「现在值得买什么」短名单 + 空仓判定。

为什么需要独立决策层（P0 测评结论驱动，见 docs/review-2026-09-04.md）：
  系统此前把三种语义不同的东西渲染成同一种「推荐」格式——
    观察层（v2 pool_pick，全量标注）、跟踪层（comeback/core_dip 候选池）、
    决策（此刻买什么，此前根本不存在）。
  92 只/天的输出里真正的决策信息是零：系统没有替用户做选择。

决策层三道门（全部有实测依据）：
  1. 市场门：创业板指当日 >0 且 5 日累计 >-3% 才允许开仓。
     实测（71 天 / 1233 样本，超额口径）：唯一正期望状态是「强势日」+0.36%（t=1.77）；
     大跌日 -0.91%（t=-2.49 显著负）、小跌日 -0.28%、反弹但 5 日弱 -0.51%。
     这一道门把系统从负期望整体翻正——比调任何个股阈值杠杆都大。
     ⚠ 本门仍按**平均超额**校准（回答「今天开仓的期望是否为负」），与 2 的 hit 率口径
     不同轴；这是刻意保留的（择时 vs 择股），不是口径漏改。
  2. 类别先验门：只允许「综合排序主表类别 ∩ hit 率高于全体基准」进入，按 hit 率降序。
     2026-09-14 统一口径：此前本门按**实测平均超额**准入并排序（core_dip +1.69% 第一、
     momentum −0.70% 永禁），与排序口径（nextday_prob 的 hit 率）方向相反，导致系统
     把 hit 率高的票排前面、再用硬编码整体删掉。现准入与顺序均由
     config_scoring.CATEGORY_HIT_RATE 派生（当前：rebound 17.9% / known_new_face 12.7%
     / momentum 10.0% / new_face 9.7%，基准 7.8%）。
  3. 稀缺配额：全局 ≤DECISION_MAX_PICKS 只。决策的价值 = 替用户放弃 95% 的机会；
     空仓是合法输出，且是高频输出。
     ⚠ 低 hit 率类别（core_dip 6.5% / short_term 6.2% / comeback / pool_pick）不在准入内，
     但仍会在终选区与观察区展示——本门只约束「替你下单的那 3 只」。
  4. 资金流出门（2026-09-14，口径统一）：主力净占比 ≤ FUND_OUTFLOW_NET_PCT(-8%) → 出局。
     本层直连 DB 取数、不经 ScanView，故独立施加一次；判定单源 `ranking.is_fund_outflow`，
     与展示层过滤同阈值同回退链（market_extra_cache 当日快照）。fail-open：取数失败不过滤。

闭环：决策层落库 decision_picks（含市场门状态），后续 prevday_perf 对比
「决策层 vs 全池」——决策层有没有 alpha 变成每日可验证，避免拍脑袋。

fail-open：任何失败降级为「决策层不可用」警告，不影响扫描主流程。
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from scanner.categories import MAIN_TABLE_CATEGORIES, SCORE_DESCENDING_BY_CAT
from scanner.config import (
    CATEGORY_HIT_RATE,
    CATEGORY_HIT_RATE_DEFAULT,
    DECISION_INTRADAY_BEAUTY_ENABLED,
    DISPLAY_MAX_TODAY_PCT,
    FUND_OUTFLOW_NET_PCT,
    now_beijing,
)
from scanner.ranking import is_fund_outflow
from scanner.utils import EXTERNAL_FAILURES

# 分时门判定单源（与终选美感门同源；相对导入绕开 pyright 会话冻结快照的绝对名解析）
from .trend_beauty import evaluate_intraday_beauty

logger = logging.getLogger(__name__)

# ── 决策层配置（先验/配额与检测逻辑强耦合，消费方仅本模块，故留在此处）──
# 类别准入与顺序**由 hit 率唯一口径派生**（2026-09-14 目标函数统一）：
#   准入 = 综合排序主表类别 ∩ hit 率 > 全体基准；
#   顺序 = hit 率降序（全局配额截断时，先验强的类别优先占位）。
# 这张表此前是**手抄的「按实测平均超额」表**（core_dip 第一优先级、momentum 永禁），
# 与排序口径（nextday_prob 的 hit 率 base rate）方向相反 —— 同一类别在系统内
# 既是最好又是最差。口径说明见 config_scoring.CATEGORY_HIT_RATE。
# ⚠ 改 hit 率表即改本表（自动跟随）；改级联成员需同时改配额，守护测试会拦。
DECISION_GATED_CATEGORIES: frozenset[str] = frozenset(
    cat for cat in MAIN_TABLE_CATEGORIES if CATEGORY_HIT_RATE.get(cat, 0.0) > CATEGORY_HIT_RATE_DEFAULT
)
# 每类配额 = **分散化策略**（不是口径产物）：防止全局配额被单一类别吃满。
# ⚠ 键集合必须与 DECISION_GATED_CATEGORIES 一致（单测守护）。hit 率刷新致某类跌出
# 准入时，守护测试会 fail —— 逼一次显式的「准入/配额」决策，而不是行为静默改变。
DECISION_CAT_CAPS: dict[str, int] = {
    "rebound": 2,
    "known_new_face": 1,
    "momentum": 1,
    "new_face": 1,
}
# (类别, 该类最多几只, 分数排序方向)。顺序即输出顺序（hit 率降序）。
# 方向不在此手写，取 categories.SCORE_DESCENDING_BY_CAT 单源：kNF 是分数反指
# （IC -0.167，低分档 hit 更高）→ 升序；其余降序。
DECISION_CATEGORY_SPECS: list[tuple[str, int, str]] = [
    (cat, DECISION_CAT_CAPS[cat], "desc" if SCORE_DESCENDING_BY_CAT.get(cat, True) else "asc")
    for cat in sorted(DECISION_CAT_CAPS, key=lambda c: -CATEGORY_HIT_RATE.get(c, 0.0))
]
DECISION_MAX_PICKS = 3

# 市场门阈值（实测校准见模块 docstring；回滚杠杆：RTS_DECISION_LAYER=0 关闭决策层）
GATE_INDEX_MIN_PCT = 0.0  # 创业板指当日涨幅下限
GATE_CUM5_MIN_PCT = -3.0  # 5 日累计涨幅下限


def market_gate(conn: sqlite3.Connection) -> tuple[bool, str]:
    """市场门：读 market_index_log 判定今日是否允许开仓。

    返回 (allowed, reason)。基准缺失时 fail-closed（宁可空仓也不盲开）——
    决策层与扫描主流程的 fail-open 语义相反：这里是保守侧。
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
        return False, f"大盘门未开（今日 {it:+.2f}% / 5日 {cum5_s}）→ 今日决策层空仓"
    cum5_s = f"{cum5:+.1f}%" if cum5 is not None else "n/a"
    return True, f"大盘门开（今日 {it:+.2f}% / 5日 {cum5_s}）"


def build_decision_picks(conn: sqlite3.Connection, today: str | None = None) -> dict[str, Any]:
    """构建今日决策层：市场门 → 类别先验内取每类头部 → 分时门过滤 → 全局配额截断。

    类别先验（2026-09-14）：准入集合与顺序由 config_scoring.CATEGORY_HIT_RATE 派生，
    见 DECISION_CATEGORY_SPECS。类别集合同时决定 SQL 的 `category IN (...)`。

    分时门（2026-09-09）：决策推荐票要求分时不走弱（intraday_score ≥
    INTRADAY_BEAUTY_MIN，与终选美感门同源判定）——「低吸不接正在回落的刀」。
    只加分时门不加日线门：决策层准入既含 kNF/rebound 这类低位类，也含 momentum/
    new_face 这类动量类，日线 MA 多头硬门会把前者整类灭掉（语义冲突），
    故不用日线门。
    分时门硬拦默认关（DECISION_INTRADAY_BEAUTY_ENABLED=False，2026-09-09 数据裁决）。
    分时数据取当日最新一轮落库 score_breakdown；无维度 fail-open 不判否。

    返回 {"allowed": bool, "gate_reason": str, "beauty_blocked": int,
    "picks": [dict], "ts": str}。
    picks 元素: {symbol, name, category, score, percent}。
    """
    allowed, gate_reason = market_gate(conn)
    result: dict[str, Any] = {
        "allowed": allowed,
        "gate_reason": gate_reason,
        "beauty_blocked": 0,
        "flow_blocked": 0,
        "picks": [],
        "ts": now_beijing().strftime("%H:%M:%S"),
    }
    if not allowed:
        return result
    rec_date = today or now_beijing().date().isoformat()
    # 分时门数据源：按 time 升序取最后一行 score_breakdown = 当日最新一轮落库
    # 维度（同 symbol 多轮重扫时取最新，与终选门的实时候选口径最接近）。
    # 单独查询且 try 窄守：表无 score_breakdown 列（旧测试 schema/旧库）时门整体
    # 跳过 fail-open，不影响主取数。
    intraday_sb: dict[str, Any] = {}
    if DECISION_INTRADAY_BEAUTY_ENABLED:
        try:
            for sym, sb in conn.execute(
                "SELECT symbol, score_breakdown FROM recommendations "
                "WHERE date=? AND score_breakdown IS NOT NULL ORDER BY time",
                (rec_date,),
            ):
                intraday_sb[sym] = sb
        except EXTERNAL_FAILURES as e:
            logger.warning("决策层分时门取数失败（门跳过）: %s: %s", type(e).__name__, e)
    # 类别集合由 specs 动态生成（不再硬编码 SQL 里的类别名——硬编码会让 hit 率表
    # 刷新后「准入变了但取数没变」，静默失效）。
    cats = [cat for cat, _, _ in DECISION_CATEGORY_SPECS]
    placeholders = ",".join("?" * len(cats))
    try:
        rows = conn.execute(
            "SELECT symbol, MAX(name) name, category, MAX(score) score, MAX(percent) percent "  # noqa: S608
            "FROM recommendations "
            f"WHERE date=? AND excluded=0 AND category IN ({placeholders}) "
            "AND (percent IS NULL OR percent <= ?) "
            "GROUP BY symbol, category",
            (rec_date, *cats, DISPLAY_MAX_TODAY_PCT),
        ).fetchall()
    except EXTERNAL_FAILURES as e:
        logger.warning("决策层取数失败: %s: %s", type(e).__name__, e)
        result["gate_reason"] += "（取数失败，决策层不可用）"
        return result
    by_cat: dict[str, list[dict]] = {cat: [] for cat, _, _ in DECISION_CATEGORY_SPECS}
    # 资金流出门（2026-09-14 统一口径）：判定单源与展示层同一个 ranking.is_fund_outflow
    # （阈值 config_sources.FUND_OUTFLOW_NET_PCT = -8.0%）。本层**直连 DB 取数**、不经
    # ScanView，因此必须独立施加一次，否则会出现「终端主表已剔掉、决策推荐里还在」——
    # 决策层是用户真正照着下单的那 3 只，漏一只的代价最大。
    # 数据源用 market_extra_cache 当日全市场快照（get_fund_flow_pct_map），与展示层
    # 回退链的兜底源同源；取数失败 fail-open（空 map → 不过滤，不因接口故障清空决策）。
    flow_map: dict[str, float] = {}
    flow_blocked = 0
    try:
        from scanner.database import get_fund_flow_pct_map

        flow_map = get_fund_flow_pct_map(conn, [r[0] for r in rows], as_of=rec_date)
    except EXTERNAL_FAILURES as e:
        logger.warning("决策层资金流出过滤取数失败（门跳过）: %s: %s", type(e).__name__, e)
    for sym, name, cat, score, pct in rows:
        if cat not in by_cat:
            continue
        if is_fund_outflow({"symbol": sym}, flow_map):
            flow_blocked += 1
            continue
        by_cat[cat].append({"symbol": sym, "name": name or "", "category": cat, "score": score or 0, "percent": pct})
    picks: list[dict] = []
    beauty_blocked = 0
    for cat, cap, direction in DECISION_CATEGORY_SPECS:
        xs = by_cat[cat]
        xs.sort(key=lambda r: r["score"], reverse=(direction == "desc"))
        # 分时门：分时走弱（冲高回落/盘中转弱）不推荐——现在买就是接刀。
        # fail（含分数）→ 拦；None（漂亮或无维度缺失）→ 放行（fail-open）。
        if DECISION_INTRADAY_BEAUTY_ENABLED:
            kept: list[dict] = []
            for r in xs:
                intraday_fail, _detail = evaluate_intraday_beauty(
                    {"score_breakdown": intraday_sb.get(r["symbol"])}, None
                )
                if intraday_fail:
                    beauty_blocked += 1
                    continue
                kept.append(r)
            xs = kept
        picks.extend(xs[:cap])
    # 全局配额：按类别先验顺序（specs 顺序）截断，先验最强类别优先占位
    result["picks"] = picks[:DECISION_MAX_PICKS]
    result["beauty_blocked"] = beauty_blocked
    result["flow_blocked"] = flow_blocked
    if not result["picks"]:
        result["gate_reason"] += " · 门开但无高把握标的 → 空仓"
        if beauty_blocked:
            result["gate_reason"] += f"（分时门拦{beauty_blocked}只）"
        if flow_blocked:
            result["gate_reason"] += f"（资金流出门拦{flow_blocked}只）"
    return result


def save_decision_picks(conn: sqlite3.Connection, result: dict[str, Any], today: str | None = None) -> None:
    """决策层落库（decision_picks，含市场门状态行 symbol='__gate__'）。

    幂等（INSERT OR REPLACE，同日重扫覆盖）。fail-open：落库失败仅告警。
    表结构由 dal.ensure_observation_tables 幂等迁移保证。
    """
    rec_date = today or now_beijing().date().isoformat()
    ts = now_beijing().strftime("%H:%M:%S")
    rows = [
        (
            rec_date,
            "__gate__",
            "",
            "__gate__",
            None,
            None,
            result.get("gate_reason", ""),
            ts,
        )
    ]
    for p in result.get("picks", []):
        rows.append(
            (
                rec_date,
                p["symbol"],
                p.get("name", ""),
                p["category"],
                p.get("score"),
                p.get("percent"),
                "决策层入选",
                ts,
            )
        )
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO decision_picks "
            "(date, symbol, name, category, score, percent, reason, created) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    except EXTERNAL_FAILURES as e:
        logger.warning("decision_picks 落库失败（不影响扫描）: %s: %s", type(e).__name__, e)
        try:
            conn.rollback()
        except sqlite3.Error:
            pass


def render_decision_lines(result: dict[str, Any]) -> list[str]:
    """把 `build_decision_picks` 的结果渲染为文本行（**纯函数，无 IO**）。

    2026-09-13 抽出：原渲染逻辑内联在 `decision_lines` 里，与"构建 + 落库"耦合，
    导致渲染格式无法单独单测（要测就得连着 DB 一起搭）。
    """
    lines = [f"◆ 今日决策层（≤{DECISION_MAX_PICKS} 只 · 大盘门+类别先验+分时门+配额）"]
    if result.get("beauty_blocked"):
        lines.append(f"  · 分时门拦{result['beauty_blocked']}只（分时走弱·不接回落刀）")
    if result.get("flow_blocked"):
        lines.append(
            f"  · 资金流出门拦{result['flow_blocked']}只（主力净占比≤{FUND_OUTFLOW_NET_PCT:.0f}%·与展示层同口径）"
        )
    if not result["allowed"] or not result["picks"]:
        lines.append(f"  ✗ 空仓 — {result['gate_reason']}")
        return lines
    for i, p in enumerate(result["picks"], 1):
        pct_s = f"{p['percent']:+.1f}%" if p.get("percent") is not None else "—"
        lines.append(f"  {i}. {p['symbol']} {p['name']} [{p['category']}] 分:{p['score']:.0f} 现价{pct_s}")
    return lines


def decision_lines(conn: sqlite3.Connection, today: str | None = None) -> list[str]:
    """渲染决策层为文本行（进 ScanView，终端/飞书共用）。**纯构建 + 渲染，不落库。**

    2026-09-13 变更（测评 A2）：本函数**原先会写 `decision_picks` 表**，而它的唯一
    生产调用方是 `display.build_scan_view`——一个自称"只算不画"的视图函数。后果：
    视图层带写库副作用，既不能当纯函数单测，将来出 HTML 报告时也会连带触发一次
    决策落库。落库现由主循环显式负责，见 `build_and_persist_decision`。

    **保留本函数的无副作用语义是刻意的**：调用方（display / feishu / 测试）可以
    安全地多次调用它来"看看决策层现在是什么样"而不产生任何持久化影响。
    """
    try:
        result = build_decision_picks(conn, today)
    except EXTERNAL_FAILURES as e:
        logger.warning("决策层构建失败: %s: %s", type(e).__name__, e)
        return ["决策层：不可用（构建失败）"]
    return render_decision_lines(result)


def build_and_persist_decision(conn: sqlite3.Connection, today: str | None = None) -> list[str]:
    """构建决策层 + **落库** + 渲染。**副作用写在函数名里**，勿在视图层调用。

    这是主循环（`unified_scanner`）每轮应当调用的那个：决策层要落 `decision_picks`
    供 `prevday_perf` 做「决策层 vs 全池」次日对比，该写必须由主循环显式触发，
    而不是藏在"要不要渲染一屏终端"里——否则关掉终端输出就悄悄不再落库了。

    落库失败已由 `save_decision_picks` 内部 fail-open（仅告警），不影响展示。
    """
    try:
        result = build_decision_picks(conn, today)
    except EXTERNAL_FAILURES as e:
        logger.warning("决策层构建失败: %s: %s", type(e).__name__, e)
        return ["决策层：不可用（构建失败）"]
    save_decision_picks(conn, result, today)
    return render_decision_lines(result)


def main() -> None:  # pragma: no cover - 手动查询入口
    import argparse

    parser = argparse.ArgumentParser(description="查看今日决策层")
    parser.add_argument("--date", default=None, help="历史日期 YYYY-MM-DD（默认今日）")
    args = parser.parse_args()
    from scanner.db.dal import ensure_observation_schema

    conn = sqlite3.connect("scanner.db")
    ensure_observation_schema(conn)
    for line in decision_lines(conn, args.date):
        print(line)
    conn.close()


if __name__ == "__main__":
    main()
