import logging
import sqlite3
from datetime import date, datetime, timedelta

from scanner.config import (
    DB_PATH,
    PROMINENCE_LOOKBACK_DAYS,
    PROMINENCE_MAX_AVG_RANK,
    PROMINENCE_REPEAT_THRESHOLD,
    now_beijing,
)
from scanner.trading_session import is_trading_day

logger = logging.getLogger(__name__)


def init_db() -> sqlite3.Connection:
    # timeout=10 / busy_timeout=10000：与其他工具并发访问时短暂锁竞争不抛异常，等待后重试。
    # 扫描器主线程独占写，但 stock_report/backtest 等独立进程可能同时读写。
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS appearances (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            name TEXT NOT NULL,
            date TEXT NOT NULL,
            rank INTEGER,
            percent REAL,
            value REAL,
            UNIQUE(symbol, date)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_kline (
            symbol TEXT NOT NULL,
            timestamp INTEGER,
            date TEXT NOT NULL,
            open REAL,
            close REAL,
            high REAL,
            low REAL,
            volume REAL,
            percent REAL,
            PRIMARY KEY(symbol, date)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS recommendations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            time TEXT NOT NULL,
            symbol TEXT NOT NULL,
            name TEXT NOT NULL,
            category TEXT NOT NULL,
            score INTEGER NOT NULL,
            percent REAL,
            trend TEXT,
            next_day_pct REAL,
            fwd_3d REAL,
            fwd_5d REAL,
            score_breakdown TEXT,
            source TEXT DEFAULT 'xueqiu'
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sector_cache (
            symbol TEXT PRIMARY KEY,
            sector TEXT NOT NULL,
            updated TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS concept_cache (
            symbol TEXT PRIMARY KEY,
            concepts TEXT NOT NULL,
            updated TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS market_extra_cache (
            symbol TEXT NOT NULL,
            date TEXT NOT NULL,
            data_type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            updated TEXT NOT NULL,
            PRIMARY KEY(symbol, data_type)
        )
    """)
    # 回马枪掉榜跟踪池：凡上过榜的 GEM 股入池，掉榜后保留若干交易日供 off-list 评估。
    # over_limit=1 表示"超限启动"入池（当日涨幅超过 short_term 上限，强得没法买），
    # 需在后续交易日持续评估（次日即使不上榜也被盯住）。
    conn.execute("""
        CREATE TABLE IF NOT EXISTS watch_pool (
            symbol TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            added_date TEXT NOT NULL,
            last_list_date TEXT NOT NULL,
            last_eval_date TEXT,
            over_limit INTEGER DEFAULT 0
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_watch_list ON watch_pool(last_list_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_app_date ON appearances(date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_app_sym ON appearances(symbol)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rec_date ON recommendations(date)")
    cur = conn.execute("PRAGMA table_info(recommendations)")
    cols = {row[1] for row in cur.fetchall()}
    if "source" not in cols:
        conn.execute("ALTER TABLE recommendations ADD COLUMN source TEXT DEFAULT 'xueqiu'")
    # 累计收益字段：匹配用户「持有 2-3 天卖出」的真实操作
    # next_day_pct 是单日涨幅，cum_2d/cum_3d 是 T+0 close 到 T+N close 的累计涨幅
    if "cum_2d" not in cols:
        conn.execute("ALTER TABLE recommendations ADD COLUMN cum_2d REAL")
    if "cum_3d" not in cols:
        conn.execute("ALTER TABLE recommendations ADD COLUMN cum_3d REAL")
    # 推动概念：综合排序「板块」列展示用（保存时由 orchestrator 写入），
    # 避免掉榜/重启后因 today_pool 缺失回退成"其他"
    if "concept" not in cols:
        conn.execute("ALTER TABLE recommendations ADD COLUMN concept TEXT")
    # 5日累计涨幅：综合排序「5日累计」列展示用（保存时写入），
    # 避免掉榜/重启后因 today_pool 缺失无法显示
    if "accumulated_pct" not in cols:
        conn.execute("ALTER TABLE recommendations ADD COLUMN accumulated_pct REAL")
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_rec_source ON recommendations(source)")
    except Exception:
        pass
    conn.commit()
    return conn


def record_appearances(conn: sqlite3.Connection, symbols: list[dict]):
    today = now_beijing().date().isoformat()
    rows = []
    for i, item in enumerate(symbols, 1):
        # rank 优先用真实榜单排名；缺失时回退到过滤后列表的下标（仅兜底，不应发生）
        rank = item.get("rank", i)
        if rank is None:
            rank = i
        # symbol/name 用 .get() 容错：API 偶发返回缺字段时不应整批写入失败
        rows.append((
            item.get("symbol", ""), item.get("name", ""), today, rank,
            item.get("percent", 0), item.get("value", 0),
        ))
    try:
        conn.executemany(
            "INSERT INTO appearances (symbol, name, date, rank, percent, value) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(symbol, date) DO UPDATE SET "
            "percent = excluded.percent, rank = excluded.rank, "
            "value = excluded.value, name = excluded.name",
            rows,
        )
        conn.commit()
    except Exception as e:
        print(f"  [!] 批量写入appearances失败: {e}, 逐行回退写入")
        try:
            conn.rollback()  # 事务失败后必须回滚，否则后续 execute 会报"cannot start a transaction within a transaction"
        except Exception:
            pass
        for row in rows:
            try:
                conn.execute(
                    "INSERT INTO appearances (symbol, name, date, rank, percent, value) VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(symbol, date) DO UPDATE SET "
                    "percent = excluded.percent, rank = excluded.rank, "
                    "value = excluded.value, name = excluded.name",
                    row,
                )
            except Exception as e2:
                print(f"  [!] 逐行写入appearances失败 {row[0]}: {e2}")
        conn.commit()


def _n_trading_days_ago(n: int) -> str:
    cursor = now_beijing().date()
    trading_days = 0
    # 上限保护：避免节假日数据缺失/损坏时 is_trading_day 永远为 False 导致死循环
    max_iter = n * 3 + 30
    iters = 0
    while trading_days < n:
        cursor -= timedelta(days=1)
        iters += 1
        if iters > max_iter:
            logger.warning("_n_trading_days_ago(%d): max_iter=%d 触发, "
                           "回溯仅到达 %s (期望 ~%d 个交易日前), "
                           "节假日数据可能缺失", n, max_iter, cursor, n)
            break
        if is_trading_day(cursor):
            trading_days += 1
    return cursor.isoformat()


def get_symbol_appearances(conn: sqlite3.Connection, symbol: str, days: int) -> list[dict]:
    today = now_beijing().date().isoformat()
    lookback = _n_trading_days_ago(days)
    cur = conn.execute(
        "SELECT date, rank, percent, value FROM appearances WHERE symbol = ? AND date >= ? AND date < ? ORDER BY date",
        (symbol, lookback, today),
    )
    return [{"date": r[0], "rank": r[1], "percent": r[2], "value": r[3]} for r in cur.fetchall()]


def save_kline_to_db(conn: sqlite3.Connection, symbol: str, kline: list[dict]):
    rows = []
    for k in kline:
        rows.append((
            symbol, k["timestamp"], k["date"], k["open"], k["close"],
            k["high"], k["low"], k["volume"], k["percent"],
        ))
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO daily_kline "
            "(symbol, timestamp, date, open, close, high, low, volume, percent) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    except Exception as e:
        print(f"  [!] 批量写入kline失败: {e}, 逐行回退写入")
        for row in rows:
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO daily_kline "
                    "(symbol, timestamp, date, open, close, high, low, volume, percent) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    row,
                )
            except Exception as e2:
                print(f"  [!] 逐行写入kline失败 {row[0]}: {e2}")
        conn.commit()


def get_cached_kline(conn: sqlite3.Connection, symbol: str) -> list[dict] | None:
    lookback = (now_beijing().date() - timedelta(days=60)).isoformat()
    cur = conn.execute(
        "SELECT date, open, close, high, low, volume, percent FROM daily_kline WHERE symbol = ? AND date >= ? ORDER BY date",
        (symbol, lookback),
    )
    rows = cur.fetchall()
    if rows:
        return [
            {"date": r[0], "open": r[1], "close": r[2], "high": r[3], "low": r[4], "volume": r[5], "percent": r[6]}
            for r in rows
        ]
    return None


def get_consecutive_appearance_days(conn: sqlite3.Connection, symbol: str, max_days: int = 10) -> int:
    """Count consecutive trading days a symbol appeared up to (not including) today."""
    today = now_beijing().date().isoformat()
    rows = conn.execute(
        "SELECT DISTINCT date FROM appearances WHERE symbol = ? AND date < ? ORDER BY date DESC LIMIT ?",
        (symbol, today, max_days),
    ).fetchall()
    if not rows:
        return 0
    dates = sorted(r[0] for r in rows)
    streak = 1
    for i in range(len(dates) - 1, 0, -1):
        d1 = date.fromisoformat(dates[i])
        d2 = date.fromisoformat(dates[i - 1])
        if _is_consecutive_trading_days(d2, d1):
            streak += 1
        else:
            break
    return streak


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


def get_prominence_map(conn: sqlite3.Connection, symbols: list[str]) -> dict[str, bool]:
    """批量查询哪些 symbol 满足辨识度条件（↻）。

    逻辑与 enhancer._compute_prominence_labels 完全一致：
      近 PROMINENCE_LOOKBACK_DAYS 个交易日内出现 ≥ PROMINENCE_REPEAT_THRESHOLD 天，
      且历史日（不含今日）平均排名 ≤ PROMINENCE_MAX_AVG_RANK。
    单次 SQL 批查，避免 N+1。
    """
    if not symbols:
        return {}
    lookback_rank = _n_trading_days_ago(PROMINENCE_LOOKBACK_DAYS)
    lookback_count = _n_trading_days_ago(PROMINENCE_LOOKBACK_DAYS - 1)
    today = now_beijing().date().isoformat()
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


def save_recommendations(conn: sqlite3.Connection, new_faces: list, momentum: list, source: str | None = None):
    import json
    today = now_beijing().date().isoformat()
    now = now_beijing().strftime("%H:%M:%S")
    for c in new_faces + momentum:
        try:
            existing = conn.execute(
                "SELECT id, score FROM recommendations WHERE date = ? AND symbol = ? AND category = ? LIMIT 1",
                (today, c.stock.symbol, c.category),
            ).fetchone()
            breakdown = json.dumps(c.kline.dimensions, ensure_ascii=False) if c.kline and c.kline.dimensions else None
            rec_source = source or getattr(c.stock, "source_tag", "unified")
            concept = getattr(c, "driving_concept", "") or ""
            accumulated = c.kline.accumulated_pct if c.kline else None
            if existing:
                # 同日同股同策略已存在：仅当新分更高时更新（保留当日最高分用于回测归因）
                if c.score > existing[1]:
                    conn.execute(
                        "UPDATE recommendations SET time = ?, score = ?, percent = ?, trend = ?, score_breakdown = ?, source = ?, concept = ?, accumulated_pct = ? "
                        "WHERE id = ?",
                        (now, c.score, c.stock.percent, c.kline.trend if c.kline else None,
                         breakdown, rec_source, concept, accumulated, existing[0]),
                    )
                continue
            conn.execute(
                "INSERT INTO recommendations (date, time, symbol, name, category, score, percent, trend, score_breakdown, source, concept, accumulated_pct) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (today, now, c.stock.symbol, c.stock.name, c.category,
                 c.score, c.stock.percent, c.kline.trend if c.kline else None, breakdown, rec_source, concept, accumulated),
            )
        except Exception as e:
            print(f"  [!] 保存推荐记录失败 {c.stock.symbol}: {e}")
    conn.commit()


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


def upsert_watch_symbol(conn: sqlite3.Connection, symbol: str, name: str,
                        last_list_date: str | None = None,
                        over_limit: bool = False) -> None:
    """写入/刷新掉榜跟踪池（单条，委托批量实现）。

    - 在榜票每次扫描刷新 last_list_date（保活）；
    - 超限启动票（当日涨幅超过 short_term 上限）置 over_limit=1，持续盯防；
    - added_date 保持首次入池日期不变。
    """
    upsert_watch_symbols(conn, [
        {"symbol": symbol, "name": name,
         "last_list_date": last_list_date, "over_limit": over_limit},
    ])


def upsert_watch_symbols(conn: sqlite3.Connection,
                         entries: list[dict]) -> None:
    """批量写入/刷新掉榜跟踪池（单次事务，避免逐条 commit 拖慢扫描循环）。

    entries: [{symbol, name, last_list_date?, over_limit?}]
    """
    if not entries:
        return
    today = now_beijing().date().isoformat()
    rows = [
        (e.get("symbol", ""), e.get("name", ""),
         e.get("last_list_date") or today, 1 if e.get("over_limit") else 0)
        for e in entries
        if e.get("symbol")
    ]
    try:
        conn.executemany(
            """INSERT INTO watch_pool (symbol, name, added_date, last_list_date, last_eval_date, over_limit)
               VALUES (?, ?, ?, ?, NULL, ?)
               ON CONFLICT(symbol) DO UPDATE SET
                 name = excluded.name,
                 last_list_date = MAX(watch_pool.last_list_date, excluded.last_list_date),
                 over_limit = MAX(watch_pool.over_limit, excluded.over_limit)""",
            [(sym, name, today, lst, ov) for sym, name, lst, ov in rows],
        )
        conn.commit()
    except Exception as e:
        print(f"  [!] 批量写入watch_pool失败: {e}")
        try:
            conn.rollback()
        except Exception:
            pass


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


def mark_watch_evaluated(conn: sqlite3.Connection, symbols: list[str]) -> None:
    """标记掉榜票今日已评估（避免同一交易日重复评估/重复补拉）。"""
    if not symbols:
        return
    today = now_beijing().date().isoformat()
    for sym in symbols:
        try:
            conn.execute(
                "UPDATE watch_pool SET last_eval_date = ? WHERE symbol = ?", (today, sym))
        except Exception:
            pass
    conn.commit()


def prune_watch_pool(conn: sqlite3.Connection,
                     keep_trading_days: int = 15) -> int:
    """删除掉榜超过 keep_trading_days 个交易日的条目（last_list_date 过旧）。

    over_limit 票的 last_list_date 在其超限上榜日写入，同样按此剪枝，
    不额外豁免——避免超限队列无限期驻留。
    """
    cutoff = _n_trading_days_ago(keep_trading_days)
    try:
        cur = conn.execute(
            "DELETE FROM watch_pool WHERE last_list_date < ?", (cutoff,))
        conn.commit()
        return cur.rowcount
    except Exception as e:
        logger.warning(f"prune_watch_pool failed: {e}")
        return 0


def get_today_recommendations(conn: sqlite3.Connection) -> list[dict]:
    """查询今日所有进入过推荐列表的票（去重，保留最高分）。

    返回列表未排序，每项包含：
      symbol, name, category, score, trend, first_time,
      live_percent (from appearances), live_rank (from appearances)
    """
    today = now_beijing().date().isoformat()
    try:
        rows = conn.execute(
            "SELECT symbol, name, category, score, trend, time, percent, concept, accumulated_pct "
            "FROM recommendations WHERE date = ? ORDER BY score DESC",
            (today,),
        ).fetchall()
    except Exception as e:
        logger.warning(f"get_today_recommendations failed: {e}")
        return []

    seen: dict[str, dict] = {}
    for r in rows:
        sym = r[0]
        if sym not in seen:
            seen[sym] = {
                "symbol": sym,
                "name": r[1],
                "category": r[2],
                "score": r[3],
                "trend": r[4],
                "time": r[5],
                "percent": r[6] or 0.0,
                "concept": r[7] or "",
                "accumulated_pct": r[8],
            }

    try:
        ft_rows = conn.execute(
            "SELECT symbol, MIN(time) FROM recommendations WHERE date = ? GROUP BY symbol",
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
        a = app_map.get(sym, {})
        entry["live_percent"] = a.get("percent", 0.0)
        entry["live_rank"] = a.get("rank")

    return list(seen.values())


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
    import json
    result: dict[str, list[str]] = {}
    for sym, concepts in rows:
        try:
            parsed = json.loads(concepts)
            if isinstance(parsed, list):
                result[sym] = [str(c) for c in parsed]
        except (json.JSONDecodeError, TypeError):
            continue
    return result


def save_concepts_cache(conn: sqlite3.Connection, concepts_map: dict[str, list[str]]):
    """批量写入 concept_cache（INSERT OR REPLACE），只在有数据时覆盖。"""
    if not concepts_map:
        return
    import json
    now = now_beijing().isoformat()
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO concept_cache (symbol, concepts, updated) VALUES (?, ?, ?)",
            [(sym, json.dumps(concepts, ensure_ascii=False), now)
             for sym, concepts in concepts_map.items()],
        )
        conn.commit()
    except Exception as e:
        logger.warning(f"save_concepts_cache failed: {e}")
        try:
            conn.rollback()
        except Exception:
            pass


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
    import json
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


def save_market_extra_cache(conn: sqlite3.Connection, data_map: dict[str, dict],
                            data_type: str):
    """批量写入 market_extra_cache（INSERT OR REPLACE，按 symbol+data_type 覆盖）。"""
    if not data_map:
        return
    import json
    today = now_beijing().date().isoformat()
    now = now_beijing().isoformat()
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO market_extra_cache (symbol, date, data_type, payload_json, updated) "
            "VALUES (?, ?, ?, ?, ?)",
            [(sym, today, data_type, json.dumps(payload, ensure_ascii=False), now)
             for sym, payload in data_map.items()],
        )
        conn.commit()
    except Exception as e:
        logger.warning(f"save_market_extra_cache failed: {e}")
        try:
            conn.rollback()
        except Exception:
            pass

