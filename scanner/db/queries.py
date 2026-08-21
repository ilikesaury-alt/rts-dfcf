"""queries 层：只读查询（P1-6 拆分，2026-08-21）。

只 SELECT、不写库不 commit。写入逻辑在 dal.py，DDL/连接在 schema.py。
私有 helper（_count_consecutive_days / _assign_rank_scores 等）仅包内消费，
经 scanner/database.py 门面 re-export 供测试使用。
"""
import json
import logging
import sqlite3
from datetime import date, timedelta

from scanner.config import (
    CORE_DIP_CATEGORY,
    PROMINENCE_LOOKBACK_DAYS,
    PROMINENCE_MAX_AVG_RANK,
    PROMINENCE_REPEAT_THRESHOLD,
    now_beijing,
)
from scanner.db._common import _n_trading_days_ago
from scanner.models import KlineBar, RecommendationRow, make_kline_bar, parse_score_breakdown
from scanner.trading_session import is_trading_day

logger = logging.getLogger(__name__)


def get_symbol_appearances(conn: sqlite3.Connection, symbol: str, days: int,
                           as_of: str | None = None) -> list[dict]:
    """symbol 在 as_of 之前 days 个交易日内的上榜记录（不含 as_of 当天）。

    as_of 默认真实今日（实时扫描口径）。历史回放传入信号日，即可复现那一天
    orchestrator 看到的 is_new / first_date，避免用「有史以来首次」之类的近似口径。
    """
    today = as_of or now_beijing().date().isoformat()
    lookback = _n_trading_days_ago(days, as_of=as_of)
    cur = conn.execute(
        "SELECT date, rank, percent, value FROM appearances WHERE symbol = ? AND date >= ? AND date < ? ORDER BY date",
        (symbol, lookback, today),
    )
    return [{"date": r[0], "rank": r[1], "percent": r[2], "value": r[3]} for r in cur.fetchall()]


def get_cached_klines(conn: sqlite3.Connection, symbols: list[str]) -> dict[str, list[KlineBar] | None]:
    """批量读取多只股票的缓存日线（单次 SQL，消灭 _fetch_all_klines 的 N+1）。

    返回 {symbol: list[KlineBar] | None}；无有效 bar 的 symbol 值为 None（与 get_cached_kline 一致）。
    bar 统一走 make_kline_bar 契约（close<=0/date 非法剔除），与单只读取同源。
    """
    if not symbols:
        return {}
    uniq = list(dict.fromkeys(symbols))
    lookback = (now_beijing().date() - timedelta(days=60)).isoformat()
    placeholders = ",".join("?" * len(uniq))
    by_sym: dict[str, list[KlineBar]] = {}
    try:
        cur = conn.execute(
            f"SELECT symbol, date, open, close, high, low, volume, percent, finalized "
            f"FROM daily_kline "
            f"WHERE symbol IN ({placeholders}) AND date >= ? ORDER BY symbol, date",
            (*uniq, lookback),
        )
        for sym, d, o, c, h, low, vol, pct, fin in cur.fetchall():
            bar = make_kline_bar({"date": d, "open": o, "close": c,
                                  "high": h, "low": low, "volume": vol, "percent": pct})
            if bar is not None:
                bar["finalized"] = bool(fin)  # 0=盘中未定稿快照，1=最终收盘
                by_sym.setdefault(sym, []).append(bar)
    except Exception as e:
        logger.warning(f"get_cached_klines failed: {e}")
        return {}
    return {sym: by_sym.get(sym) for sym in uniq}


def get_cached_kline(conn: sqlite3.Connection, symbol: str) -> list[KlineBar] | None:
    """单只股票缓存日线（委托批量实现，口径一致）。"""
    return get_cached_klines(conn, [symbol]).get(symbol)


def get_cached_market_caps(conn: sqlite3.Connection, symbols: list[str],
                           max_age_days: int = 0) -> dict[str, dict]:
    """读取市值陈旧缓存（市值批量全失败时的兜底）。

    max_age_days=0：仅返回当日写入的缓存（最严格，适合盘中）。非 0：放宽到近 N 天
    （收盘后/非交易时段批量接口滞后时仍可用）。返回结构与 fetch_market_caps_batch 一致。
    """
    if not symbols:
        return {}
    uniq = list(dict.fromkeys(symbols))
    placeholders = ",".join("?" * len(uniq))
    today = now_beijing().date().isoformat()
    if max_age_days > 0:
        # 放宽到近 N 天（非交易时段批量接口滞后仍可兜底）
        min_date = (now_beijing().date() - timedelta(days=max_age_days)).isoformat()
        cur = conn.execute(
            f"SELECT symbol, market_cap, circ_market_cap, turnover_rate, current, percent, source "
            f"FROM market_cap_cache WHERE symbol IN ({placeholders}) AND updated >= ?",
            (*uniq, min_date),
        )
    else:
        # max_age_days=0：仅当日写入的缓存（最严格，盘中口径）
        cur = conn.execute(
            f"SELECT symbol, market_cap, circ_market_cap, turnover_rate, current, percent, source "
            f"FROM market_cap_cache WHERE symbol IN ({placeholders}) AND updated = ?",
            (*uniq, today),
        )
    out: dict[str, dict] = {}
    try:
        for sym, mc, cmc, tr, cur_, pct, src in cur.fetchall():
            out[sym] = {
                "market_cap": mc, "circ_market_cap": cmc,
                "turnover_rate": tr, "current": cur_,
                "percent": pct, "source": src,
            }
    except Exception as e:
        logger.warning(f"get_cached_market_caps failed: {e}")
        return {}
    return out


def _count_consecutive_days(dates: list[str]) -> int:
    """dates（升序）中截至最后一天连续出现的交易日数（不连续即断）。"""
    if not dates:
        return 0
    dates = sorted(set(dates))
    streak = 1
    try:
        curr = date.fromisoformat(dates[-1])
    except (ValueError, TypeError):
        # 脏日期（非 ISO 的历史数据）：无法判定连续性，返回 1（仅当日）。
        return streak
    for i in range(len(dates) - 1, 0, -1):
        try:
            prev = date.fromisoformat(dates[i - 1])
        except (ValueError, TypeError):
            break  # 脏日期打断连续上榜计数，不再向后追溯
        if _is_consecutive_trading_days(prev, curr):
            streak += 1
            curr = prev
        else:
            break
    return streak


def get_consecutive_appearance_days_batch(conn: sqlite3.Connection,
                                          symbols: list[str],
                                          max_days: int = 10) -> dict[str, int]:
    """批量计算多只股票连续上榜天数（不含今日），单次 SQL 消灭 enhancer 的 N+1。

    与 get_consecutive_appearance_days 同口径（最多 max_days 天）。
    日历窗口按 max_days×3+30 天放大，保证窗口内覆盖至少 max_days 个交易日，
    再在 Python 端用 is_trading_day 精确判定连续性。
    """
    if not symbols:
        return {}
    uniq = list(dict.fromkeys(symbols))
    today = now_beijing().date().isoformat()
    cutoff = (now_beijing().date() - timedelta(days=max_days * 3 + 30)).isoformat()
    placeholders = ",".join("?" * len(uniq))
    by_sym: dict[str, list[str]] = {}
    try:
        rows = conn.execute(
            f"SELECT symbol, date FROM appearances WHERE symbol IN ({placeholders}) "
            f"AND date >= ? AND date < ? ORDER BY symbol, date",
            (*uniq, cutoff, today),
        ).fetchall()
    except Exception as e:
        logger.warning(f"get_consecutive_appearance_days_batch failed: {e}")
        return {}
    for sym, d in rows:
        by_sym.setdefault(sym, []).append(d)
    return {sym: _count_consecutive_days(by_sym.get(sym) or []) for sym in uniq}


def get_consecutive_appearance_days(conn: sqlite3.Connection, symbol: str, max_days: int = 10) -> int:
    """Count consecutive trading days a symbol appeared up to (not including) today.

    委托批量实现（口径一致，供 stock_report 等单点调用）。
    """
    return get_consecutive_appearance_days_batch(conn, [symbol], max_days).get(symbol, 0)


def _is_consecutive_trading_days(prev: date, curr: date) -> bool:
    """True if prev is the immediate previous trading day before curr (no trading days between)."""
    cursor = curr - timedelta(days=1)
    while cursor > prev:
        if is_trading_day(cursor):
            return False
        cursor -= timedelta(days=1)
    return True


def count_recent_appearances(conn: sqlite3.Connection, symbol: str, lookback_days: int = 10) -> int:
    """Count distinct appearance days for a symbol in the last N trading days (including today)."""
    from scanner.config import now_beijing as _now
    lookback = _n_trading_days_ago(lookback_days - 1)
    today = _now().date().isoformat()
    cur = conn.execute(
        "SELECT COUNT(DISTINCT date) FROM appearances WHERE symbol = ? AND date >= ? AND date <= ?",
        (symbol, lookback, today),
    )
    return cur.fetchone()[0]


def get_prominence_map(conn: sqlite3.Connection, symbols: list[str],
                       as_of_date: str | None = None) -> dict[str, bool]:
    """批量查询哪些 symbol 满足辨识度条件（↻）。

    逻辑与 enhancer._compute_prominence_labels 完全一致：
      近 PROMINENCE_LOOKBACK_DAYS 个交易日内出现 ≥ PROMINENCE_REPEAT_THRESHOLD 天，
      且历史日（不含今日）平均排名 ≤ PROMINENCE_MAX_AVG_RANK。
    单次 SQL 批查，避免 N+1。

    as_of_date: 历史回放视角——把「今天」锚定到该日，判定与那一天实时扫描完全一致
    （nextday_attribution 归因按推荐日视角评估，默认 None = 真实今日）。
    """
    if not symbols:
        return {}
    lookback_rank = _n_trading_days_ago(PROMINENCE_LOOKBACK_DAYS - 1, as_of=as_of_date)
    lookback_count = _n_trading_days_ago(PROMINENCE_LOOKBACK_DAYS - 1, as_of=as_of_date)
    today = as_of_date or now_beijing().date().isoformat()
    placeholders = ",".join("?" * len(symbols))
    try:
        rows = conn.execute(
            f"SELECT symbol, date, rank FROM appearances "
            f"WHERE symbol IN ({placeholders}) AND date >= ? AND date <= ?",
            (*symbols, lookback_rank, today),
        ).fetchall()
    except Exception as e:
        logger.warning(f"get_prominence_map failed: {e}")
        return {}

    by_sym: dict[str, dict] = {}
    for sym, dt, rank in rows:
        if sym not in by_sym:
            by_sym[sym] = {"dates": set(), "rank_list": []}
        by_sym[sym]["dates"].add(dt)
        by_sym[sym]["rank_list"].append((dt, rank))

    result: dict[str, bool] = {}
    for sym in symbols:
        info = by_sym.get(sym)
        if not info:
            result[sym] = False
            continue
        count_dates = {d for d in info["dates"] if d >= lookback_count}
        if len(count_dates) < PROMINENCE_REPEAT_THRESHOLD:
            result[sym] = False
            continue
        valid_ranks = [r for d, r in info["rank_list"] if d < today and r is not None and r > 0]
        if not valid_ranks:
            result[sym] = False
            continue
        result[sym] = sum(valid_ranks) / len(valid_ranks) <= PROMINENCE_MAX_AVG_RANK
    return result


def is_prominent(conn: sqlite3.Connection, symbol: str) -> bool:
    """单只股票是否满足辨识度条件（↻）。

    复用 get_prominence_map 批量实现（避免 enhancer/回马枪各自维护逐股 N+1 拷贝
    导致的口径漂移），供 enhancer._compute_prominence_labels 与回马枪回踩变体调用。
    """
    return get_prominence_map(conn, [symbol]).get(symbol, False)


def get_market_index_log(conn: sqlite3.Connection,
                         date_str: str | None = None) -> dict | None:
    """读取某日最近一轮的大盘指数血缘记录；无记录/旧库无表返回 None。"""
    if date_str is None:
        date_str = now_beijing().date().isoformat()
    try:
        row = conn.execute(
            "SELECT * FROM market_index_log WHERE date = ? ORDER BY updated DESC LIMIT 1",
            (date_str,),
        ).fetchone()
        if not row:
            return None
        # 不依赖 conn.row_factory（部分调用方传裸 sqlite3.Connection），按列名组装
        cols = [c[0] for c in conn.execute(
            "SELECT * FROM market_index_log WHERE 1=0").description]
        return dict(zip(cols, row))
    except sqlite3.OperationalError:
        return None  # 旧库无表（未迁移）→ 无法审计，fail-open


def get_loss_rates_batch(conn: sqlite3.Connection, symbols: list[str],
                         lookback_days: int = 90) -> dict[str, float]:
    """批量返回 {symbol: loss_rate}，loss_rate = 近 lookback_days 天推荐中次日跌幅<=-5% 的占比。

    样本<3 的 symbol 不包含在返回结果中（避免小样本噪音）。
    单次 SQL 查询，避免 N 次 DB 往返。
    """
    if not symbols:
        return {}
    placeholders = ",".join("?" * len(symbols))
    # 用 Beijing UTC+8 计算截止日，避免服务器本地时区导致日期偏移
    # （'localtime' 修饰符依赖服务器时区，违反项目硬约束）
    cutoff = (now_beijing() - timedelta(days=lookback_days)).date().isoformat()
    try:
        cur = conn.execute(
            f"SELECT symbol, COUNT(*), SUM(CASE WHEN next_day_pct <= -5 THEN 1 ELSE 0 END) "
            f"FROM recommendations WHERE symbol IN ({placeholders}) "
            f"AND next_day_pct IS NOT NULL AND date >= ? "
            f"GROUP BY symbol",
            (*symbols, cutoff),
        )
        return {row[0]: row[2] * 100 / row[1] for row in cur if row[1] >= 3}
    except Exception as e:
        logger.warning(f"get_loss_rates_batch failed: {e}")
        return {}


def get_recent_recommendations(conn: sqlite3.Connection,
                               lookback_days: int = 5,
                               exclude_today: bool = True) -> list[dict]:
    """查询近 N 个交易日的推荐记录（去重：同股取最新推荐日的最高分）。

    返回每只票在最近推荐日的记录（同日内取最高分，跨日取最新日）。
    用于回马枪回踩变体（回调到买点二次上车）的候选域。
    """
    today = now_beijing().date().isoformat()
    lookback = _n_trading_days_ago(lookback_days)
    query = (
        "SELECT symbol, name, category, score, percent, date "
        "FROM recommendations WHERE date >= ? "
    )
    params: list = [lookback]
    if exclude_today:
        query += "AND date < ? "
        params.append(today)
    query += "ORDER BY date DESC, score DESC"
    try:
        cur = conn.execute(query, params)
        rows = cur.fetchall()
    except Exception as e:
        logger.warning(f"get_recent_recommendations failed: {e}")
        return []
    # 去重：同 symbol 取首条（最新日期+最高分）
    seen: set[str] = set()
    result: list[dict] = []
    for r in rows:
        sym = r[0]
        if sym in seen:
            continue
        seen.add(sym)
        result.append({
            "symbol": sym, "name": r[1], "category": r[2],
            "score": r[3], "percent": r[4] or 0.0, "date": r[5],
        })
    return result


def get_watch_symbols(conn: sqlite3.Connection) -> list[dict]:
    """返回掉榜跟踪池全部条目。"""
    try:
        rows = conn.execute(
            "SELECT symbol, name, last_list_date, over_limit, last_eval_date "
            "FROM watch_pool"
        ).fetchall()
    except Exception as e:
        logger.warning(f"get_watch_symbols failed: {e}")
        return []
    return [
        {"symbol": r[0], "name": r[1], "last_list_date": r[2],
         "over_limit": r[3], "last_eval_date": r[4]}
        for r in rows
    ]


def get_today_recommendations(conn: sqlite3.Connection, as_of=None) -> list[RecommendationRow]:
    """查询（默认今日）所有进入过推荐列表的票（按 symbol 去重）。

    as_of: 目标日期（date 或 'YYYY-MM-DD' 字符串），缺省为今日（now_beijing）。
    2026-08-18 新增（配合 today_report.py 历史回放）：历史日期按该日推荐/上榜
    快照查询，去重口径与今日一致。

    去重优先级：榜上类别（非 comeback/core_dip）优先于 comeback 与核心方向低吸（core_dip），
    同优先级内保留最高分——
    防止同票同时有 comeback（掉榜跟踪）与榜上推荐（如 short_term）时，因 comeback
    基线分更高（40+15×信号数）而遮蔽榜上记录，导致该票在综合排序主表消失
    （回马枪区仅在主区条数 ≤ COMEBACK_DISPLAY_MIN_MAIN 且大盘弱势时展示，平时整体隐藏）。
    comeback 仅是"榜上之外单独评估"的补充信号，在榜票应以主表类别展示。
    2026-08-19：core_dip 与 comeback 同族（不入综合排序主表，display/today_report 的
    main 均排除），归入同一低优桶——否则 core_dip 记录（CASE 0）会按 score 遮蔽榜上五类
    主表行，且恒压过 comeback（CASE 1），使同票在综合排序/回马枪列表消失。

    返回列表未排序，每项包含：
      symbol, name, category, score, trend, first_time,
      live_percent (from appearances), live_rank (from appearances),
      rank_score（类内百分位，综合排序跨类别可比用）,
      score_breakdown（2026-08-17 新增：解析为 dict，供掉榜/重启行的 🎯 分型
      （short_term 弱转强）与板块普涨避雷标记判定，见 ranking._entry_dims）
    """
    if as_of is None:
        as_of = now_beijing().date()
    if isinstance(as_of, str):
        as_of = date.fromisoformat(as_of)
    today = as_of.isoformat()
    try:
        rows = conn.execute(
            "SELECT symbol, name, category, score, trend, time, percent, concept, accumulated_pct, "
            "score_breakdown "
            "FROM recommendations WHERE date = ? AND COALESCE(excluded, 0) = 0 "
            f"ORDER BY CASE WHEN category IN ('comeback', '{CORE_DIP_CATEGORY}') THEN 1 ELSE 0 END, "
            "score DESC",
            (today,),
        ).fetchall()
    except Exception as e:
        logger.warning(f"get_today_recommendations failed: {e}")
        return []

    seen: dict[str, dict] = {}
    for r in rows:
        sym = r[0]
        if sym not in seen:
            sb_raw = r[9]
            sb = parse_score_breakdown(sb_raw)
            seen[sym] = {
                "symbol": sym,
                "name": r[1],
                "category": r[2],
                "score": r[3],
                "date": today,
                "trend": r[4],
                "time": r[5],
                "percent": r[6] or 0.0,
                "concept": r[7] or "",
                "accumulated_pct": r[8],
                "score_breakdown": sb,
            }

    try:
        # 2026-08-17 审查修复：MIN(time) 过滤 excluded——原查询含已被硬过滤/反转移出的行，
        # 首推时间列可能取到"已失效记录"的早先时间（展示误导）。与主查询 excluded=0 口径对齐。
        ft_rows = conn.execute(
            "SELECT symbol, MIN(time) FROM recommendations "
            "WHERE date = ? AND COALESCE(excluded, 0) = 0 GROUP BY symbol",
            (today,),
        ).fetchall()
        first_time_map = {r[0]: r[1] for r in ft_rows}
    except Exception as e:
        logger.warning(f"get_today_recommendations MIN(time) failed: {e}")
        first_time_map = {}
    for sym in seen:
        seen[sym]["first_time"] = first_time_map.get(sym, seen[sym].get("time", ""))

    try:
        app_rows = conn.execute(
            "SELECT symbol, percent, rank FROM appearances WHERE date = ?",
            (today,),
        ).fetchall()
    except Exception as e:
        logger.warning(f"get_today_recommendations appearances query failed: {e}")
        app_rows = []
    app_map = {r[0]: {"percent": r[1], "rank": r[2]} for r in app_rows}
    for sym, entry in seen.items():
        a = app_map.get(sym)
        # 2026-08-20 修复：今日从未上榜的掉榜/回马枪/核心低吸行，live_percent 置 None
        # 而非 0.0——此前恒 0.0 使 display._print_priority_row 的回退链永远走 0.0，
        # 该行涨幅恒显 +0.00%（而 ranking._nextday_entry_percent 判档用 DB percent，
        # 同表两套口径：可「判档用 +5%、显示 +0.00%」）。今日上榜行 percent=0.0
        # 是合法 0.00% 涨幅，保持 0.0 不误伤（test_display_priority_dropped_live_percent_zero_not_fallback）。
        entry["live_percent"] = a["percent"] if a else None
        entry["live_rank"] = a["rank"] if a else None

    result = list(seen.values())
    _assign_rank_scores(result)
    return result


def _assign_rank_scores(records: list[dict]) -> None:
    """为 records 计算 within-(date,category) 百分位 rank_score（0-100），就地修改。

    用于综合排序跨类别可比：同类别同日的票按 score 分位排序，消除各类别自身标尺差异
    （new_face 均值~45 与 comeback~122 不可直接比）。records 需含 'date'/'category'/'score'，
    缺 'date' 时退化为仅按 category 分组（get_today_recommendations 全为当日，等价）。
    """
    groups: dict[tuple, list[dict]] = {}
    for r in records:
        key = (r.get("date"), r.get("category"))
        groups.setdefault(key, []).append(r)
    for recs in groups.values():
        n = len(recs)
        if n == 0:
            continue
        ordered = sorted(recs, key=lambda r: r.get("score", 0.0))
        for pos, r in enumerate(ordered):
            r["rank_score"] = 100.0 if n == 1 else round(pos / (n - 1) * 100, 2)


def get_concepts_cache(conn: sqlite3.Connection, symbols: list[str], ttl_days: int = 7) -> dict[str, list[str]]:
    """批量读取 concept_cache，返回 {symbol: [concept, ...]}。

    仅返回 updated 距今不超过 ttl_days 的条目（过期视为缺失，交由上游重新拉取）。
    """
    if not symbols:
        return {}
    placeholders = ",".join("?" * len(symbols))
    cutoff = (now_beijing() - timedelta(days=ttl_days)).isoformat()
    try:
        rows = conn.execute(
            f"SELECT symbol, concepts FROM concept_cache "
            f"WHERE symbol IN ({placeholders}) AND updated >= ?",
            (*symbols, cutoff),
        ).fetchall()
    except Exception as e:
        logger.warning(f"get_concepts_cache failed: {e}")
        return {}
    result: dict[str, list[str]] = {}
    for sym, concepts in rows:
        try:
            parsed = json.loads(concepts)
            if isinstance(parsed, list):
                result[sym] = [str(c) for c in parsed]
        except (json.JSONDecodeError, TypeError):
            continue
    return result


def get_market_extra_cache(conn: sqlite3.Connection, symbols: list[str],
                           data_type: str, intraday_ttl_sec: int | None = None) -> dict[str, dict]:
    """批量读取 market_extra_cache，返回 {symbol: payload_dict}。

    仅返回 date 为今天的条目。intraday_ttl_sec 提供时，仅返回 updated 距今
    不超过该秒数的条目（盘中刷新用，过期视为缺失交由上游重拉）；不提供则
    返回当天全部可用条目（stock_report 等读旧数据的场景）。
    data_type 区分 zt_pool / fund_flow。
    """
    if not symbols:
        return {}
    placeholders = ",".join("?" * len(symbols))
    today = now_beijing().date().isoformat()
    cutoff = (now_beijing() - timedelta(seconds=intraday_ttl_sec)).isoformat() \
        if intraday_ttl_sec else None
    sql = (f"SELECT symbol, payload_json FROM market_extra_cache "
           f"WHERE symbol IN ({placeholders}) AND data_type = ? AND date = ?")
    params: tuple = (*symbols, data_type, today)
    if cutoff:
        sql += " AND updated >= ?"
        params = (*params, cutoff)
    try:
        rows = conn.execute(sql, params).fetchall()
    except Exception as e:
        logger.warning(f"get_market_extra_cache failed: {e}")
        return {}
    result: dict[str, dict] = {}
    for sym, payload in rows:
        try:
            parsed = json.loads(payload)
            if isinstance(parsed, dict):
                result[sym] = parsed
        except (json.JSONDecodeError, TypeError):
            continue
    return result


def get_fund_flow_pct_map(conn: sqlite3.Connection, symbols: list[str]) -> dict[str, float]:
    """批量读取当日主力净占比，返回 {symbol: main_pct}。

    与资金流图标/综合排序档位同源口径（get_market_extra_cache data_type=fund_flow，
    仅当日数据）。无当日数据或查询失败时该 symbol 不包含在结果中（缺失=中性，
    由调用方 fail-open 处理，如回马枪回踩资金流硬过滤、display 图标回退）。
    """
    if not symbols:
        return {}
    try:
        ff_db = get_market_extra_cache(conn, list(dict.fromkeys(symbols)), "fund_flow")
    except Exception:
        return {}
    return {sym: (payload.get("main_pct") if payload else None)
            for sym, payload in ff_db.items()}
