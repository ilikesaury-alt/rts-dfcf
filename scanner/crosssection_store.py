"""横截面落库层：持久化 / 次日标签 / 离线读（从 crosssection.py 拆出，2026-11-25）。

不持有进程内状态：所有需要「当前时刻」的地方用 `now_beijing()` 单源；
`_print_db_health` 的进程内健康快照由调用方显式传入，避免反向依赖主模块。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from typing import Any

from scanner.config import CS_KEEP_DAYS, now_beijing
from scanner.utils import EXTERNAL_FAILURES, to_float

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return now_beijing()

def _persist(conn: sqlite3.Connection, date: str, round_seq: int, signals: list[dict], now: datetime) -> None:
    """写本轮 p95+ 榜外信号并按天淘汰旧轮次（用主循环已有 conn，不新开连接/线程）。

    保留策略（2026-11-19 从「50 轮」改）：按**天**保留最近 `CS_KEEP_DAYS` 天的
    **整日全部轮次** —— persist 可离线从行数派生，次日标签可回填（m028）。
    `main_net` 同批落库（m028）：画像「主力资金」段的离线回放依据
    （占额的分母 `amount` 未落库 ⇒ 占比只做实时展示，不回放）。
    """
    updated = now.isoformat()
    conn.executemany(
        "INSERT OR REPLACE INTO crosssection_signal("
        "date, round_seq, symbol, name, price, percent, speed_1, speed_3, speed_5, "
        "amount_surge, vol_ratio, pct_rank, main_net, updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                date,
                round_seq,
                s["symbol"],
                s["name"],
                s["price"],
                s["percent"],
                s["speed_1"],
                s["speed_3"],
                s["speed_5"],
                s["amount_surge"],
                s["vol_ratio"],
                s["pct_rank"],
                s.get("main_net"),
                updated,
            )
            for s in signals
        ],
    )
    # 只留最近 CS_KEEP_DAYS 个**交易日**：按 date 整日淘汰（轮次行全留），
    # 跨日历史不再被截断。date 是 ISO 字符串，字典序 = 时间序。
    conn.execute(
        "DELETE FROM crosssection_signal WHERE date NOT IN ("
        "SELECT DISTINCT date FROM crosssection_signal ORDER BY date DESC LIMIT ?)",
        (CS_KEEP_DAYS,),
    )
    conn.commit()

def backfill_next_day(conn: sqlite3.Connection, adapter: Any, signal_date: str | None = None) -> int:
    """回填 `crosssection_signal.next_day_pct`（信号日收盘 → 次一交易日收盘，%），返回回填行数。

    动机（2026-11-19 用户决策「存档+次日标签」）：旧策略只留 50 轮且无标签，
    这层永远无法验证。标签是**行情事实**，不是结论 —— 分析只进离线脚本，
    终端/飞书不读它（不给结论纪律不变）。

    口径与 B 段同款（`offboard_watch.backfill_next_day`）：收盘 → 收盘，
    K 线数据复用**榜外 K 线池** `offboard_kline_cache`（横截面票本就是榜外创业板票，
    不另建第三张 K 线表；`daily_kline` 是榜单衍生池，语义不容污染）。

    三个必须的防呆（与 B 段/onboard 同源，防呆①②复用 `_next_bar_is_next_session`）：
      ① 信号日必须早于今天（WHERE `date < ?`）；
      ② `nxt` 必须是 `d` 之后**紧邻的下一个交易日**（不得夹已登记交易日，
         否则停牌/跳空会把「几天后」误当「次日」）；
      ③ 🔴 `nxt < today` —— K 线池末根可能是当日盘中 bar，放行 `nxt == today`
         会把「次日收盘」冻结成「次日 09:45 盘中价」且永不修正（B 段实测偏差
         46% 的行 >2pp）。代价：标签**延后一个交易日**（T+2 交易日起补），同 B 段。

    同一 (date, symbol) 当天可能有多轮行，标签只算一次、UPDATE 该组全部行。
    交易时段每轮调用（与 B 段同节奏）；NULL 集通常只剩近 1~2 个交易日，
    K 线池命中后零网络请求。
    """
    if conn is None:
        return 0
    today = _now().date().isoformat()
    sql = "SELECT DISTINCT date, symbol FROM crosssection_signal WHERE next_day_pct IS NULL AND date < ?"
    params: tuple = (today,)
    if signal_date:
        sql += " AND date = ?"
        params = (signal_date,)
    try:
        rows = conn.execute(sql, params).fetchall()
    except EXTERNAL_FAILURES as e:
        logger.warning("横截面次日标签回填查询失败: %s: %s", type(e).__name__, e)
        return 0
    if not rows:
        return 0

    # 惰性 import：`offboard_watch` 是重量级模块，且避免任何潜在环（它不依赖本层）。
    # `_next_bar_is_next_session` 复用 B 段单源（onboard_anomaly 同款），不复制第二份。
    from scanner.offboard_watch import _next_bar_is_next_session, load_offboard_klines

    by_symbol: dict[str, list[str]] = {}
    for d, sym in rows:
        by_symbol.setdefault(str(sym), []).append(str(d))

    filled = 0
    now = _now().isoformat(timespec="seconds")
    bars_map = load_offboard_klines(conn, adapter, list(by_symbol))
    for sym, dates in by_symbol.items():
        bars = bars_map.get(sym) or []
        by_date = {k["date"]: k for k in bars if isinstance(k.get("date"), str)}
        for d in dates:
            later = sorted(x for x in by_date if x > d)
            if not later:
                continue
            nxt = later[0]
            # 防呆③：nxt 就是今天 ⇒ 那根是当日盘中 bar，绝不当「次日收盘」（冻结不可逆）。
            if nxt >= today:
                continue
            # 防呆②：nxt 必须紧邻 d（中间不得夹已登记交易日），跨国庆/春节放行。
            if not _next_bar_is_next_session(d, nxt):
                continue
            # 信号日/次日 bar 任一缺失（停牌/脏数据）⇒ 跳过（验不了就不产出，不编标签）。
            b0 = by_date.get(d)
            b1 = by_date.get(nxt)
            p0 = (to_float(b0.get("close"), 0.0) or 0.0) if b0 else 0.0
            p1 = (to_float(b1.get("close"), 0.0) or 0.0) if b1 else 0.0
            if p0 <= 0 or p1 <= 0:
                continue
            try:
                conn.execute(
                    "UPDATE crosssection_signal SET next_day_pct=?, updated_at=? WHERE date=? AND symbol=?",
                    (round((p1 - p0) / p0 * 100.0, 4), now, d, sym),
                )
            except EXTERNAL_FAILURES as e:
                logger.warning("横截面次日标签写入失败 %s %s: %s", d, sym, e)
                continue
            filled += 1
    if filled:
        try:
            conn.commit()
        except EXTERNAL_FAILURES as e:
            logger.warning("横截面次日标签提交失败: %s", e)
    return filled

def _hydrate_persist(conn: sqlite3.Connection, date: str) -> dict[str, int]:
    """从当日的 `crosssection_signal` 行**恢复 persist 计数**（重启/跨日首轮）。

    语义必须与写侧完全同源：写侧「本轮进了 p95+ 就算一次出现」⇒ 行数 =
    该票当日出现过的**不同轮次**数（`COUNT(DISTINCT round_seq)`，`INSERT OR REPLACE`
    保证同轮重写不重计）。不恢复的后果（用户实测 2026-11-19）：每次重启持续组就空，
    要重新攒满 `CS_PERSIST_MIN` 轮才恢复。

    fail-open：表未建/查询失败 ⇒ `{}`（退化成旧行为，绝不阻断 observe）；
    每次进程/每日只跑一次（只在 `_state["date"] != today` 分支），开销可忽略。
    """
    try:
        rows = conn.execute(
            "SELECT symbol, COUNT(DISTINCT round_seq) FROM crosssection_signal WHERE date=? GROUP BY symbol",
            (date,),
        ).fetchall()
    except EXTERNAL_FAILURES as e:
        logger.warning("横截面 persist 恢复失败（退化为从零计数）: %s: %s", type(e).__name__, e)
        return {}
    return {str(sym): int(n) for sym, n in rows}

def _load_rows(conn: sqlite3.Connection, date: str) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT date, round_seq, symbol, name, price, percent, speed_1, speed_3, speed_5, "
        "amount_surge, vol_ratio, pct_rank, updated_at "
        "FROM crosssection_signal WHERE date=? ORDER BY round_seq DESC, pct_rank DESC",
        (date,),
    ).fetchall()

def _print_db_health(conn: sqlite3.Connection, date: str, health_snapshot: dict) -> None:
    try:
        row = conn.execute(
            "SELECT COUNT(DISTINCT round_seq), COUNT(*), MIN(round_seq), MAX(round_seq) "
            "FROM crosssection_signal WHERE date=?",
            (date,),
        ).fetchone()
    except sqlite3.Error as e:
        print(f"crosssection_signal 不可读（{type(e).__name__}: {e}）—— 先跑一轮扫描器应用 m027")
        print(f"进程内健康摘要：{json.dumps(health_snapshot, ensure_ascii=False)}")
        return
    rounds, rows, first, last = (row[0], row[1], row[2], row[3]) if row else (0, 0, None, None)
    print(f"日期 {date} | 轮次 {rounds} | 信号行 {rows} | round_seq {first or '—'} → {last or '—'}")
    print(f"进程内健康摘要：{json.dumps(health_snapshot, ensure_ascii=False)}")
