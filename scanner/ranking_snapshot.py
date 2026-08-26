"""综合排序档位快照落库（2026-08-26）。

目的：ranking 判定代码（_entry_tier / entry_tier_reasons / 🎯 画像）日后演进时，
历史归因不被「用最新代码重放历史」静默篡改——收盘定稿后把当日全部推荐的
档位/🎯/劣后原因/主表展示序号一次性落库，作为当日规则下的权威存证。

写入时机：unified_scanner 收盘定稿批次内（_finalize_today_klines 之后），每交易日
一次、幂等覆盖、fail-open（异常只告警不杀进程，写 logs/finalize.log 审计）。
消费端：scripts/tier3_reason_perf 等复盘工具优先读快照，无快照日期回退现算。

定位：观测基建，不改排序/评分/候选生成；prevday_perf/today_report 的报告语义
不受影响（仍走 today_report._build_report 同源管线）。
"""
import json

from scanner.config import CORE_DIP_CATEGORY, now_beijing
from scanner.database import get_today_recommendations
from scanner.ranking import (
    _entry_tier,
    _is_nextday_marked,
    build_accum_map,
    entry_tier_reasons,
    sort_main_entries,
)


def persist_ranking_snapshot(conn, target_date: str | None = None) -> int:
    """回放 target_date 推荐并写 ranking_snapshot（幂等覆盖）。返回写入行数。

    异常向上抛出——调用方（unified_scanner._persist_ranking_snapshot_once）负责
    fail-open。行键 (date, symbol, category)；主表行 rank_in_table = 当日综合排序
    最终展示序号（sort_main_entries 同源），comeback/core_dip 独立区行为 NULL。
    """
    if target_date is None:
        target_date = now_beijing().strftime("%Y-%m-%d")
    recs = get_today_recommendations(conn, as_of=target_date)
    if not recs:
        return 0

    accum_map = build_accum_map(conn, recs)
    main: list[dict] = []
    rows: list[tuple] = []
    for e in recs:
        marked = _is_nextday_marked(e, conn, accum_map=accum_map)
        tier = _entry_tier(e, conn, accum_map=accum_map, marked=marked)
        # marked 传实际判定值（非 🎯 票才评估警示因子——与 _entry_tier 级联一致）
        reasons = entry_tier_reasons(e, accum=accum_map.get(e["symbol"]), marked=marked)
        rows.append((e, tier, marked, reasons))
        if e["category"] not in ("comeback", CORE_DIP_CATEGORY):
            main.append(e)

    # 主表展示序号与当日综合排序一致（排序组合层单源）
    tier_map = {(e["symbol"], e["category"]): t for e, t, _, _ in rows
                if e["category"] not in ("comeback", CORE_DIP_CATEGORY)}
    rank_in_table: dict[tuple[str, str], int] = {}
    for i, e in enumerate(sort_main_entries(main, tier_map), 1):
        rank_in_table[(e["symbol"], e["category"])] = i

    created = now_beijing().isoformat(timespec="seconds")
    payload = [
        (target_date, e["symbol"], e["category"], tier, int(marked),
         json.dumps(reasons, ensure_ascii=False), rank_in_table.get((e["symbol"], e["category"])),
         created)
        for e, tier, marked, reasons in rows
    ]
    with conn:
        conn.execute("DELETE FROM ranking_snapshot WHERE date = ?", (target_date,))
        conn.executemany(
            "INSERT INTO ranking_snapshot (date, symbol, category, tier, marked, "
            "reasons_json, rank_in_table, created) VALUES (?,?,?,?,?,?,?,?)",
            payload,
        )
    return len(payload)


def load_ranking_snapshot(conn, target_date: str) -> dict[tuple[str, str], dict]:
    """读取某日快照 → {(symbol, category): row_dict}；无表/无数据返回空 dict。

    row_dict 含 tier/marked/reasons(list)/rank_in_table。消费端以空返回为
    「无快照」信号回退现算（历史日期在功能上线前天然无快照）。
    """
    try:
        rows = conn.execute(
            "SELECT symbol, category, tier, marked, reasons_json, rank_in_table "
            "FROM ranking_snapshot WHERE date = ?",
            (target_date,),
        ).fetchall()
    except Exception:
        return {}
    result: dict[tuple[str, str], dict] = {}
    for sym, cat, tier, marked, reasons_json, rank in rows:
        try:
            reasons = json.loads(reasons_json) if reasons_json else []
        except (TypeError, ValueError):
            reasons = []
        result[(sym, cat)] = {
            "tier": tier,
            "marked": bool(marked),
            "reasons": reasons if isinstance(reasons, list) else [],
            "rank_in_table": rank,
        }
    return result
