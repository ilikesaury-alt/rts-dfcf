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
  2. 类别先验门：只允许实测超额为正的类别进入（core_dip +1.69% / known_new_face
     +1.03% / rebound +0.74%）；momentum 永禁（-0.70%）。先验随 prevday_perf 复算更新。
  3. 稀缺配额：全局 ≤DECISION_MAX_PICKS 只。决策的价值 = 替用户放弃 95% 的机会；
     空仓是合法输出，且是高频输出。

闭环：决策层落库 decision_picks（含市场门状态），后续 prevday_perf 对比
「决策层 vs 全池」——决策层有没有 alpha 变成每日可验证，避免拍脑袋。

fail-open：任何失败降级为「决策层不可用」警告，不影响扫描主流程。
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from scanner.config import DECISION_INTRADAY_BEAUTY_ENABLED, DISPLAY_MAX_TODAY_PCT, now_beijing
from scanner.utils import EXTERNAL_FAILURES

# 分时门判定单源（与终选美感门同源；相对导入绕开 pyright 会话冻结快照的绝对名解析）
from .trend_beauty import evaluate_intraday_beauty

logger = logging.getLogger(__name__)

# ── 决策层配置（config 单一事实源原则的例外：先验/配额与检测逻辑强耦合，
#    且消费方仅本模块，暂放此处；如需外部复算再上移）──
# (类别, 该类最多几只, 分数排序方向)。顺序即输出顺序（按实测超额先验降序）。
# kNF 是分数反指（IC -0.167，低分档 hit 更高），用升序。
DECISION_CATEGORY_SPECS: list[tuple[str, int, str]] = [
    ("core_dip", 2, "desc"),
    ("known_new_face", 1, "asc"),
    ("rebound", 2, "desc"),
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

    分时门（2026-09-09）：决策推荐票要求分时不走弱（intraday_score ≥
    INTRADAY_BEAUTY_MIN，与终选美感门同源判定）——「低吸不接正在回落的刀」
    对超跌低位类同样成立。只加分时门不加日线门：决策层类别先验恰是
    core_dip/kNF/rebound 低位类，日线 MA 多头硬门会全灭它们（语义冲突）。
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
    try:
        rows = conn.execute(
            "SELECT symbol, MAX(name) name, category, MAX(score) score, MAX(percent) percent "
            "FROM recommendations "
            "WHERE date=? AND excluded=0 AND category IN ('core_dip','known_new_face','rebound') "
            "AND (percent IS NULL OR percent <= ?) "
            "GROUP BY symbol, category",
            (rec_date, DISPLAY_MAX_TODAY_PCT),
        ).fetchall()
    except EXTERNAL_FAILURES as e:
        logger.warning("决策层取数失败: %s: %s", type(e).__name__, e)
        result["gate_reason"] += "（取数失败，决策层不可用）"
        return result
    by_cat: dict[str, list[dict]] = {cat: [] for cat, _, _ in DECISION_CATEGORY_SPECS}
    for sym, name, cat, score, pct in rows:
        if cat in by_cat:
            by_cat[cat].append(
                {"symbol": sym, "name": name or "", "category": cat, "score": score or 0, "percent": pct}
            )
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
    if not result["picks"]:
        result["gate_reason"] += " · 门开但无高把握标的 → 空仓"
        if beauty_blocked:
            result["gate_reason"] += f"（分时门拦{beauty_blocked}只）"
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


def decision_lines(conn: sqlite3.Connection, today: str | None = None) -> list[str]:
    """渲染决策层为文本行（进 ScanView，终端/飞书共用）。含落库副作用。"""
    try:
        result = build_decision_picks(conn, today)
    except EXTERNAL_FAILURES as e:
        logger.warning("决策层构建失败: %s: %s", type(e).__name__, e)
        return ["决策层：不可用（构建失败）"]
    save_decision_picks(conn, result, today)
    lines = [f"◆ 今日决策层（≤{DECISION_MAX_PICKS} 只 · 大盘门+类别先验+分时门+配额）"]
    if result.get("beauty_blocked"):
        lines.append(f"  · 分时门拦{result['beauty_blocked']}只（分时走弱·不接回落刀）")
    if not result["allowed"] or not result["picks"]:
        lines.append(f"  ✗ 空仓 — {result['gate_reason']}")
        return lines
    for i, p in enumerate(result["picks"], 1):
        pct_s = f"{p['percent']:+.1f}%" if p.get("percent") is not None else "—"
        lines.append(f"  {i}. {p['symbol']} {p['name']} [{p['category']}] 分:{p['score']:.0f} 现价{pct_s}")
    return lines


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
