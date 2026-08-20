import json
import logging
import math
import sqlite3
from datetime import date, timedelta

from scanner.config import (
    CORE_DIP_CATEGORY,
    DB_PATH,
    PROMINENCE_LOOKBACK_DAYS,
    PROMINENCE_MAX_AVG_RANK,
    PROMINENCE_REPEAT_THRESHOLD,
    REVERSAL_OVERSHOOT_DROP,
    REVERSAL_TURNED_RED_DROP,
    WATCH_POOL_MAX,
    now_beijing,
)
from scanner.models import KlineBar, make_kline_bar
from scanner.trading_session import is_trading_day, is_trading_time
from scanner.utils import is_gem, to_float

logger = logging.getLogger(__name__)


def init_db() -> sqlite3.Connection:
    # timeout=10 / busy_timeout=10000：与其他工具并发访问时短暂锁竞争不抛异常，等待后重试。
    # 扫描器主线程独占写，但 stock_report/backtest 等独立进程可能同时读写。
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.execute("PRAGMA busy_timeout=10000")
    # WAL：写者不阻塞读者，实时扫描（每 60s 写）与回测/归因（读同一库）可并发，
    # 消除 "database is locked" / 最长 10s 等待。模式持久化在库文件，其他直连
    # scanner.db 的进程（backtest/prevday/nextday/ic_attribution 等）自动继承，
    # 无需逐个改连接点（见 grep sqlite3.connect 的十余处散点连接）。
    conn.execute("PRAGMA journal_mode=WAL")
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
    # daily_kline 此前无 (symbol,date) 复合索引：回测/归因按 symbol+date 区间全量
    # 回放走全表扫描，库一大即慢且占锁。与 appearances/recommendations 索引对齐。
    conn.execute("CREATE INDEX IF NOT EXISTS idx_kline_sym_date ON daily_kline(symbol, date)")
    # 扫描数据质量血缘日志（2026-08-14）：每轮扫描的数据质量快照。
    # 跨函数静默降级（K线补拉失败/缺今日bar/兜底构造）是本项目最难发现的 bug 类别——
    # 单函数审查看不出来（每个函数都"对"），只存在于函数之间的数据流不变量。
    # 常态计数器让降级规模可查询：某日补拉失败数异常升高 + 推荐数骤降 → 关联即定位
    # （网宿科技案例：盘中补拉失败静默回退旧缓存，量比按昨日量误杀放量启动票）。
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scan_quality_log (
            date TEXT PRIMARY KEY,
            time TEXT,
            gem_count INTEGER DEFAULT 0,          -- 本轮在榜 GEM 票数（过滤后）
            fetch_failed INTEGER DEFAULT 0,        -- 日线补拉失败/超时未拉取的票数
            today_bar_missing INTEGER DEFAULT 0,   -- 盘中缺今日 bar（旧缓存评分）票数
            minute_fallback INTEGER DEFAULT 0,     -- 分时构造今日 bar 兜底成功数
            stale_recs INTEGER DEFAULT 0,          -- 落库推荐中 stale_kline=1 条数
            updated TEXT DEFAULT ''
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sql_date ON scan_quality_log(date)")
    # 市值缓存（2026-08-20）：市值批量查询（雪球 batch/quote + akshare 兜底）曾瞬时双源
    # 同时失败 → 返回空 → "小叶美规则暂不生效"。市值本身变化缓慢（日级），落库后可在
    # 全失时回退陈旧缓存，避免单轮静默失效。盘中限当日（涨停/停牌股本就无新市值），
    # 非交易时段放宽到 MCAP_CACHE_MAX_AGE_DAYS 天（收盘后批量接口可能滞后）。
    conn.execute("""
        CREATE TABLE IF NOT EXISTS market_cap_cache (
            symbol TEXT PRIMARY KEY,
            market_cap REAL,
            circ_market_cap REAL,
            turnover_rate REAL,
            current REAL,
            percent REAL,
            source TEXT,
            updated TEXT DEFAULT ''
        )
    """)
    # 大盘指数血缘日志（2026-08-19）：每轮扫描使用的大盘涨幅（创业板指）+ 其 bar 日期落库。
    # 大盘标签（display._market_env_tag）曾因 kline 接口 begin/count 语义错位把当日 -6.26%
    # 读成昨日 -0.93%（展示"大盘中性"）而无痕——涨幅是瞬时值，不落库就无法审计"当时读到
    # 了什么"。bar_date 是「读到的是哪一天的数据」的权威证据，供 data_health 对账。
    conn.execute("""
        CREATE TABLE IF NOT EXISTS market_index_log (
            date TEXT PRIMARY KEY,
            time TEXT,
            index_pct REAL,        -- 本轮扫描使用的大盘涨幅（创业板指）
            bar_date TEXT,         -- 该涨幅对应的 bar 日期（None=未取得/降级源）
            source TEXT,           -- 'xueqiu' | 'akshare'
            updated TEXT DEFAULT ''
        )
    """)
    # 榜单可观测性（2026-08-19）：把雪球飙升榜/热搜榜的成分+排名分布的每轮快照落库为
    # 时间序列（区别于 scan_quality_log 的日级覆盖，这里是逐扫描保留）。目的：把上游
    # （雪球）的口径/样本变更变成可查询信号——雪球一旦改排序键/分页/样本过滤条件，
    # 系统行为会悄悄漂移（榜单中位数/涨跌结构/重叠率会突变），单函数审查看不出。
    # 由 unified_scanner 主循环每轮调用 record_leaderboard_log 记录。
    conn.execute("""
        CREATE TABLE IF NOT EXISTS leaderboard_log (
            date TEXT,
            time TEXT,
            source TEXT,                 -- 'biaosheng' | 'hot'
            total INTEGER DEFAULT 0,     -- 榜单返回条数
            gem_listed INTEGER DEFAULT 0, -- 其中 GEM(300xxx) 数
            up_count INTEGER DEFAULT 0,
            down_count INTEGER DEFAULT 0,
            flat_count INTEGER DEFAULT 0,
            median_pct REAL,             -- 全榜涨幅中位数（口径变更的最强信号）
            mean_pct REAL,
            top10_mean_pct REAL,         -- 前10平均涨幅（涨速强度）
            max_pct REAL,
            overlap_prev REAL,           -- 与上一轮榜单成员重叠比例 0-1（样本稳定性）
            median_rank_change REAL,     -- 排名变化中位数（排序口径变更信号）
            symbol_snapshot TEXT,        -- JSON：前40成员 {symbol,name,percent,rank,rank_change}
            updated TEXT DEFAULT '',
            PRIMARY KEY (date, time, source)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_lb_date ON leaderboard_log(date, source)")
    # 收盘定稿标记（2026-08-18 拓斯达脏数据事故后新增）：盘中扫描把未收盘的今日 bar
    # （盘中价+部分量能）写入 daily_kline 属预期（today_report 盘中读），但收盘后无
    # 定稿覆盖会残留污染 next_day_pct → 回测/归因/复盘全口径。finalized=0 表示
    # 「盘中快照，可能非最终收盘价」；收盘定稿/收盘后写入的 bar 置 1。
    cur = conn.execute("PRAGMA table_info(daily_kline)")
    kline_cols = {row[1] for row in cur.fetchall()}
    if "finalized" not in kline_cols:
        conn.execute("ALTER TABLE daily_kline ADD COLUMN finalized INTEGER DEFAULT 1")
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
    # 硬过滤落标：当日曾命中 RISK_FLAGS_HARD_FILTER（主力出货/趋势破位）的票置 1，
    # 综合排序读取时排除——防止"早先轮次落库、后续轮次被过滤"的票仍展示。
    # orchestrator 每轮扫描按最新轮次状态更新（过滤→1，通过→0），
    # 一旦当日被硬过滤即当日不再展示（止损级信号，保守语义）。
    if "excluded" not in cols:
        conn.execute("ALTER TABLE recommendations ADD COLUMN excluded INTEGER DEFAULT 0")
    # 评分数据审计（2026-08-14）：该条推荐评分所用 K 线是否缺今日 bar（补拉失败旧缓存兜底）。
    # 1 = 旧缓存评分（量比基于昨日量，可能失真/误杀/误推）；0 = 含今日 bar 正常评分。
    # 供事后审计"该推荐基于什么数据评分"，识别静默降级导致的历史误判（网宿案例同类）。
    if "stale_kline" not in cols:
        conn.execute("ALTER TABLE recommendations ADD COLUMN stale_kline INTEGER DEFAULT 0")
    # 硬过滤原因审计（2026-08-20）：excluded=1 只存布尔位，被砍票从 DB 无法反推
    # "命中哪个硬过滤标签"。补 excluded_reason 存 enhancer 打标的命中标签串
    # （如"主力出货" / "趋势破位,弱转强失效" / "财务风险:资不抵债"），
    # 消除"无审计依据的误杀"盲点（08-19 复盘 6 只被砍票复算 0 命中任何硬过滤规则
    # 却 excluded=1，因 risk_flags 从未落库）。
    if "excluded_reason" not in cols:
        conn.execute("ALTER TABLE recommendations ADD COLUMN excluded_reason TEXT")
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
            conn.rollback()  # 事务失败后必须回滚，否则后续 execute 会报
            # "cannot start a transaction within a transaction"
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


def _n_trading_days_ago(n: int, as_of: str | None = None) -> str:
    """as_of（含）之前第 n 个交易日；as_of 为 None 时锚定真实今日。

    as_of 用于历史回放（historical_rescan）：把「今天」挪到某个过去的交易日，
    使 is_new / 回溯窗口的判定与那一天的实时扫描完全一致。
    """
    cursor = date.fromisoformat(as_of) if as_of else now_beijing().date()
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


def save_kline_to_db(conn: sqlite3.Connection, symbol: str, kline: list[KlineBar]):
    rows = []
    today_str = now_beijing().date().isoformat()
    trading = is_trading_time()
    for k in kline:
        # 定稿标记：盘中写入的今日 bar 是未收盘快照（可能非最终收盘价），置 0；
        # 收盘后写入（定稿/backfill/repair）或历史 bar 置 1。
        finalized = 0 if (k["date"] == today_str and trading) else 1
        rows.append((
            symbol, k.get("timestamp"), k["date"], k["open"], k["close"],
            k["high"], k["low"], k["volume"], k["percent"], finalized,
        ))
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO daily_kline "
            "(symbol, timestamp, date, open, close, high, low, volume, percent, finalized) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    except Exception as e:
        print(f"  [!] 批量写入kline失败: {e}, 逐行回退写入")
        for row in rows:
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO daily_kline "
                    "(symbol, timestamp, date, open, close, high, low, volume, percent, finalized) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    row,
                )
            except Exception as e2:
                print(f"  [!] 逐行写入kline失败 {row[0]}: {e2}")
        conn.commit()


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


def save_market_caps(conn: sqlite3.Connection, caps: dict[str, dict], source: str = "xueqiu") -> int:
    """把成功的市值批量结果落库（陈旧缓存兜底源）。

    仅写本次查询返回的有效条目（market_cap 或 circ_market_cap > 0），0 值（停牌/降级）
    不入缓存以免污染兜底。返回写入条数。
    """
    if not caps:
        return 0
    rows = []
    for sym, d in caps.items():
        mc = d.get("market_cap") or 0
        cmc = d.get("circ_market_cap") or 0
        if mc <= 0 and cmc <= 0:
            continue
        rows.append((
            sym, mc, cmc,
            d.get("turnover_rate") or 0.0,
            d.get("current") or 0.0,
            d.get("percent") or 0.0,
            source, now_beijing().date().isoformat(),
        ))
    if not rows:
        return 0
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO market_cap_cache "
            "(symbol, market_cap, circ_market_cap, turnover_rate, current, percent, source, updated) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
        return len(rows)
    except Exception as e:
        logger.warning(f"save_market_caps failed: {e}")
        return 0


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


def save_scan_quality(conn: sqlite3.Connection,
                      stats: dict) -> None:
    """落库单轮扫描的数据质量快照（数据血缘日志，2026-08-14）。

    跨函数静默降级是本项目最难发现的 bug 类别：上游函数在故障路径返回"看似正常"
    的降级数据（补拉失败→旧缓存、缺今日 bar→昨日量），下游无感知消费导致误判
    （网宿科技案例：量比硬门误杀放量启动票）。单函数审查无法发现，因为每个函数
    单独都对。此日志把降级规模变成可查询的常态计数器：某日 fetch_failed/
    today_bar_missing 异常升高 + 推荐数骤降 → 关联即定位。

    stats 字段：gem_count / fetch_failed / today_bar_missing / minute_fallback /
    stale_recs。同日多轮扫描按最新一轮覆盖（取当日最后快照），查询历史看日级趋势。
    """
    today = now_beijing().date().isoformat()
    now = now_beijing().strftime("%H:%M:%S")
    try:
        conn.execute(
            """INSERT INTO scan_quality_log
               (date, time, gem_count, fetch_failed, today_bar_missing,
                minute_fallback, stale_recs, updated)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(date) DO UPDATE SET
                 time = excluded.time,
                 gem_count = excluded.gem_count,
                 fetch_failed = excluded.fetch_failed,
                 today_bar_missing = excluded.today_bar_missing,
                 minute_fallback = excluded.minute_fallback,
                 stale_recs = excluded.stale_recs,
                 updated = excluded.updated""",
            (today, now,
             int(stats.get("gem_count", 0) or 0),
             int(stats.get("fetch_failed", 0) or 0),
             int(stats.get("today_bar_missing", 0) or 0),
             int(stats.get("minute_fallback", 0) or 0),
             int(stats.get("stale_recs", 0) or 0),
             now),
        )
        conn.commit()
    except Exception as e:
        logger.warning(f"save_scan_quality failed: {e}")


def save_market_index_log(conn: sqlite3.Connection, index_pct: float | None,
                          bar_date: str | None = None, source: str = "xueqiu") -> None:
    """落库单轮扫描使用的大盘指数（大盘指数血缘日志，2026-08-19）。

    大盘标签曾因 kline 接口 begin/count 语义错位把当日 -6.26% 崩盘读成昨日 -0.93%
    （展示"大盘中性"）而无痕：涨幅是瞬时值、不进 daily_kline、不参与评分，任何落库
    数值对账都碰不到它。此表记录「每轮扫描当时读到的大盘涨幅 + 其 bar 日期」，
    bar 日期是「读到哪一天的数据」的权威证据，供 data_health.check_market_index_health
    对账审计（读到旧 bar / 与独立源涨幅偏差超容差 → 告警）。

    同日多轮扫描按最新一轮覆盖（与 scan_quality_log 同语义）。
    """
    today = now_beijing().date().isoformat()
    now = now_beijing().strftime("%H:%M:%S")
    try:
        conn.execute(
            """INSERT INTO market_index_log (date, time, index_pct, bar_date, source, updated)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(date) DO UPDATE SET
                 time = excluded.time,
                 index_pct = excluded.index_pct,
                 bar_date = excluded.bar_date,
                 source = excluded.source,
                 updated = excluded.updated""",
            (today, now,
             index_pct if index_pct is not None else None,
             bar_date, source, now),
        )
        conn.commit()
    except Exception as e:
        logger.warning(f"save_market_index_log failed: {e}")


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


def record_leaderboard_log(conn: sqlite3.Connection, source: str, items: list[dict],
                            prev_symbols: set[str]) -> set[str]:
    """落库单轮榜单快照（榜单可观测性，2026-08-19）。

    把雪球飙升榜/热搜榜每轮成分 + 排名分布写进 leaderboard_log 时间序列，maintainer 与
    scan_quality_log 互补：scan_quality_log 看「下游扫描数据质量」降级，本表看「上游源
    本身」的口径漂移。stats 全用可直接查询的列，symbol_snapshot 存前 40 成员 JSON 供
    重建成分分析。

    上游口径变更信号（对照历史即可识别）：
      - median_pct / up:down 结构突变（如从涨幅榜变热度榜，中位会失真/趋 0）
      - overlap_prev 骤降（排序键或分页变更导致成员剧烈抖动）
      - total / gem_listed 突变（样本过滤条件变更）

    统计口径防御：percent/rank_change 脏值（None/NaN/str）统一 to_float 过滤，
    不参与中位数/均值（与 _filter_gem_stocks 的数值强转同族防御）。

    返回本轮的 symbol 集合，调用方应保存为下一轮的 prev_symbols（用于重叠率）。
    """
    today = now_beijing().date().isoformat()
    now = now_beijing().strftime("%H:%M:%S")
    try:
        syms = [str(i.get("symbol") or "") for i in items]
        valid_syms = [s for s in syms if s]
        cur_syms = set(valid_syms)
        total = len(items)
        gem_listed = sum(1 for cs in valid_syms if is_gem(cs))

        # 涨幅分布（防御：percent 可能为 None/字符串/NaN）
        pcts: list[float] = []
        for i in items:
            v = to_float(i.get("percent"), None)
            if v is not None and math.isfinite(v):
                pcts.append(v)
        up = sum(1 for p in pcts if p > 0)
        down = sum(1 for p in pcts if p < 0)
        flat = len(pcts) - up - down
        median_pct = _median(pcts) if pcts else None
        mean_pct = sum(pcts) / len(pcts) if pcts else None
        top10 = sorted(pcts, reverse=True)[:10]
        top10_mean = sum(top10) / len(top10) if top10 else None
        max_pct = max(pcts) if pcts else None

        # 排名变化中位数（防御：rank_change 可能为 "-"/None/NaN）
        rcs: list[float] = []
        for i in items:
            v = to_float(i.get("rank_change"), None)
            if v is not None and math.isfinite(v):
                rcs.append(v)
        median_rc = _median(rcs) if rcs else None

        # 与上一轮成员重叠比例
        overlap = 0.0
        if prev_symbols and cur_syms:
            overlap = len(cur_syms & prev_symbols) / len(cur_syms)

        # 前 40 成员紧凑快照（供成分重建，不全存以控体积：100条×6KB×240轮/日≈1.4MB/日）
        snapshot = [
            {"symbol": str(i.get("symbol") or ""),
             "name": str(i.get("name") or ""),
             "percent": pcts[idx] if idx < len(pcts) else None,
             "rank": int(to_float(i.get("rank"), idx + 1) or idx + 1),
             "rank_change": to_float(i.get("rank_change"), None)}
            for idx, i in enumerate(items[:40])
        ]
        conn.execute(
            """INSERT OR REPLACE INTO leaderboard_log
               (date, time, source, total, gem_listed, up_count, down_count, flat_count,
                median_pct, mean_pct, top10_mean_pct, max_pct, overlap_prev,
                median_rank_change, symbol_snapshot, updated)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (today, now, source, total, gem_listed, up, down, flat,
             median_pct, mean_pct, top10_mean, max_pct, overlap,
             median_rc, json.dumps(snapshot, ensure_ascii=False), now),
        )
        conn.commit()
        return cur_syms
    except Exception as e:
        logger.warning(f"record_leaderboard_log failed: {e}")
        # fail-open：落库失败不影响扫描主流程，返回空集（不污染后续重叠率）
        return prev_symbols


def _median(vals: list[float]) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2.0


def save_recommendations(conn: sqlite3.Connection, new_faces: list, rest: list, source: str | None = None):
    """保存当日推荐记录：new_faces（新面孔）+ rest（其余所有类别合并列表）。

    rest 在调用方由 momentum/rebound/short_term/comeback 各桶合并传入，
    与 new_faces 在本函数内同等对待（统一去重 + 取当日最高分）。
    """
    import json
    today = now_beijing().date().isoformat()
    now = now_beijing().strftime("%H:%M:%S")
    for c in new_faces + rest:
        try:
            existing = conn.execute(
                "SELECT id, score FROM recommendations WHERE date = ? AND symbol = ? AND category = ? LIMIT 1",
                (today, c.stock.symbol, c.category),
            ).fetchone()
            breakdown = json.dumps(c.kline.dimensions, ensure_ascii=False) if c.kline and c.kline.dimensions else None
            rec_source = source or getattr(c.stock, "source_tag", "unified")
            concept = getattr(c, "driving_concept", "") or ""
            accumulated = c.kline.accumulated_pct if c.kline else None
            stale_kline = 1 if getattr(c, "stale_kline", False) else 0
            excluded_reason = getattr(c, "excluded_reason", "") or ""
            if existing:
                # 同日同股同策略已存在：仅当新分更高时更新（保留当日最高分用于回测归因）
                if c.score > existing[1]:
                    conn.execute(
                        "UPDATE recommendations SET time = ?, score = ?, percent = ?, trend = ?, "
                        "score_breakdown = ?, source = ?, concept = ?, accumulated_pct = ?, "
                        "stale_kline = ?, excluded_reason = ? "
                        "WHERE id = ?",
                        (now, c.score, c.stock.percent, c.kline.trend if c.kline else None,
                         breakdown, rec_source, concept, accumulated, stale_kline,
                         excluded_reason, existing[0]),
                    )
                continue
            conn.execute(
                "INSERT INTO recommendations (date, time, symbol, name, category, score, percent, "
                "trend, score_breakdown, source, concept, accumulated_pct, stale_kline, excluded_reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (today, now, c.stock.symbol, c.stock.name, c.category,
                 c.score, c.stock.percent, c.kline.trend if c.kline else None,
                 breakdown, rec_source, concept, accumulated, stale_kline, excluded_reason),
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
        # WATCH_POOL_MAX 容量上限：超限时淘汰 last_list_date 最旧的条目（含 over_limit 票，
        # 老旧的超限启动票不再盯防，防止池无限膨胀）。与 prune_watch_pool 的交易日剪枝互补。
        conn.execute(
            "DELETE FROM watch_pool WHERE symbol NOT IN ("
            "  SELECT symbol FROM watch_pool ORDER BY last_list_date DESC LIMIT ?"
            ")", (WATCH_POOL_MAX,))
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


def get_today_recommendations(conn: sqlite3.Connection, as_of=None) -> list[dict]:
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
            sb = {}
            if sb_raw:
                try:
                    sb = json.loads(sb_raw)
                except Exception:
                    sb = {}
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
        a = app_map.get(sym, {})
        entry["live_percent"] = a.get("percent", 0.0)
        entry["live_rank"] = a.get("rank")

    result = list(seen.values())
    _assign_rank_scores(result)
    return result


def mark_reversed_recommendations(conn: sqlite3.Connection,
                                  today_recs: list[dict],
                                  active_syms: set[str],
                                  live_quotes: dict[str, dict],
                                  turned_red_drop: float = REVERSAL_TURNED_RED_DROP,
                                  overshoot_drop: float = REVERSAL_OVERSHOOT_DROP) -> list[str]:
    """推荐后快速反转移出（2026-08-13）：今日已推荐、当前不在候选池、且回落幅度超标的榜上
    主类别票，标 excluded=1 移出综合排序展示（保留落库记录），返回本次标记的 symbol 列表。

    **回落幅度口径（2026-08-13 改为「从当日最高价」计算）**：`drop = ref_pct - live_pct`，
    其中 ref_pct 优先取 live_quotes[sym]["high_pct"]（当日最高涨幅，由行情 API 的 high/昨收
    算出），缺失时回退推荐时刻涨幅（today_recs 的 percent）—— 以最高点为锚能更客观反映
    "动量从峰值衰减"，不受推荐时刻择时影响。
    命中任一条件即视为推荐失败：
      ① 已转负（live_pct<0）且 drop ≥ turned_red_drop（滤掉高位仅小幅回落就微幅翻绿的噪音）；
      ② drop ≥ overshoot_drop，无论红绿——大幅回吐即使未转负也"不敢买"（如从 +12% 高点回落到
         +2%，动量已破）。
    阈值按「从最高涨幅→收盘」回落分布校准（p75=4.49/p90=7.92/p95=10.54，见 config 注释），
    非单票凑参。回马枪（category=="comeback"）是掉榜跟踪池，不参与自动移出。硬过滤只评估当前
    轮次候选，够不着掉出候选池的旧推荐；本函数对 active_syms（本轮通过验证的候选）不下手，
    避免与 orchestrator 的 passed_syms 置 0 打架；重新成为候选的票由 orchestrator 置回
    excluded=0。行情缺失（live_quotes 无该 symbol / percent 为 None）时按无法度量 fail-open。
    """
    reversed_syms: list[str] = []
    for r in today_recs:
        sym = r["symbol"]
        if sym in active_syms:
            continue
        if r.get("category") in ("comeback", "core_dip"):
            continue
        rec_pct = r.get("percent")
        q = live_quotes.get(sym)
        if not q:
            continue
        # fail-open 防线（2026-08-14）：行情生产端（fetch_market_caps_batch 等）对缺失
        # 字段强转为 0.0，percent=None 检查实际不可达。current 存在且 <=0（无 A 股以
        # 0 元成交）即行情降级/停牌条目——0.00% 会被误当"已转负"、drop=ref-0 虚高，
        # 导致误移出，必须按无法度量跳过（与 docstring 的行情缺失 fail-open 语义对齐）。
        cur = q.get("current")
        if cur is not None and cur <= 0:
            continue
        live_pct = q.get("percent")
        if rec_pct is None or live_pct is None:
            continue
        ref_pct = q.get("high_pct")
        if ref_pct is None:
            ref_pct = rec_pct
        drop = ref_pct - live_pct
        if (live_pct < 0 and drop >= turned_red_drop) or drop >= overshoot_drop:
            reversed_syms.append(sym)
    if not reversed_syms:
        return []
    today = now_beijing().date().isoformat()
    try:
        # 2026-08-17 审查修复：UPDATE 加 COALESCE(category,'')!='comeback' 守卫——
        # 判定循环已跳过 comeback 行，但 UPDATE 原按 (date, symbol) 全量置 excluded，
        # 若同 symbol 当日既有榜上主类别行（触发反转移出）又有早先落库的 comeback 行，
        # 后者也会被连带移出（docstring 声明"回马枪不参与自动移出"）。
        # 2026-08-19：core_dip 与 comeback 同族，同样不被反转移出连带。
        conn.executemany(
            "UPDATE recommendations SET excluded=1 WHERE date=? AND symbol=? "
            "AND COALESCE(category, '') NOT IN ('comeback', 'core_dip')",
            [(today, sym) for sym in reversed_syms])
        conn.commit()
    except Exception as e:
        logger.warning(f"mark_reversed_recommendations failed: {e}")
        return []
    return reversed_syms



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

