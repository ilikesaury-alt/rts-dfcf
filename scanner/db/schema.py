"""schema 层：连接工厂 + DDL + 迁移（P1-6 拆分，2026-08-21）。

只负责「库长什么样」：建连接（PRAGMA）、建表、加索引、列迁移。
读写逻辑在 queries.py / dal.py。

get_conn() 是连接创建的唯一入口：timeout/busy_timeout/WAL 三件套收口于此，
散在各脚本的裸 sqlite3.connect(scanner.db) 后续逐步迁移过来（本次不动调用方，
避免扩大改动面）。
"""

import sqlite3

from scanner.config import now_beijing

# schema 版本：每次结构性变更（新表/新列/新索引）+1，并在 init_db 里补对应的
# 幂等迁移。schema_version 表记录演进历史，供工具判断库是否需要重建/回填。
SCHEMA_VERSION = 2  # v2 (2026-08-26): +ranking_snapshot（综合排序档位快照）


def get_conn(db_path: str | None = None) -> sqlite3.Connection:
    """创建带标准 PRAGMA 的 scanner.db 连接（唯一入口）。

    db_path 缺省时在**调用时**读取 scanner.config.DB_PATH（非默认参数导入期绑定）——
    默认参数 `db_path: str = DB_PATH` 在 import 时固化路径，测试 patch
    cfgmod.DB_PATH 完全无效，导致测试静默连上真实生产库读写（2026-08-24 审查发现：
    TestMarketCapCache 偶发失败 + 测试市值数据污染真实 market_cap_cache）。改为
    函数内查 config 模块属性后，patch 即生效。

    - timeout=10 / busy_timeout=10000：与其他工具并发访问时短暂锁竞争不抛异常，
      等待后重试。扫描器主线程独占写，但 stock_report/backtest 等独立进程可能
      同时读写。
    - WAL：写者不阻塞读者，实时扫描（每 60s 写）与回测/归因（读同一库）可并发，
      消除 "database is locked" / 最长 10s 等待。模式持久化在库文件，其他直连
      scanner.db 的进程（backtest/prevday/nextday/ic_attribution 等）自动继承，
      无需逐个改连接点。
    """
    if db_path is None:
        from scanner import config as _config  # 调用时读模块属性，patch 可见

        db_path = _config.DB_PATH
    conn = sqlite3.connect(db_path, timeout=10.0)
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> sqlite3.Connection:
    """建表 + 幂等迁移，返回连接（既有契约：调用方持有 conn 使用）。"""
    conn = get_conn()
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
    # 综合排序档位快照（2026-08-26）：收盘定稿后把当日全部推荐的档位/🎯/劣后原因
    # 一次性落库。目的：ranking 判定代码日后演进时，历史归因不被「用最新代码重放
    # 历史」篡改——快照是当日规则下的权威存证，复盘消费端优先读它、无快照日期才
    # 回退现算。主表行 rank_in_table = 当日综合排序最终展示序号；comeback/core_dip
    # 独立区行该列为 NULL。
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ranking_snapshot (
            date TEXT NOT NULL,
            symbol TEXT NOT NULL,
            category TEXT NOT NULL,
            tier INTEGER NOT NULL,
            marked INTEGER NOT NULL,
            reasons_json TEXT,
            rank_in_table INTEGER,
            created TEXT NOT NULL,
            PRIMARY KEY (date, symbol, category)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rs_date ON ranking_snapshot(date)")
    # schema 版本记录（P1-6）：幂等——首次初始化写入当前版本，之后仅在版本前进时追加。
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER NOT NULL,
            updated TEXT NOT NULL
        )
    """)
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    if row is None or row[0] is None:
        conn.execute(
            "INSERT INTO schema_version (version, updated) VALUES (?, ?)", (SCHEMA_VERSION, now_beijing().isoformat())
        )
    elif row[0] < SCHEMA_VERSION:
        conn.execute(
            "INSERT INTO schema_version (version, updated) VALUES (?, ?)", (SCHEMA_VERSION, now_beijing().isoformat())
        )
    # 收盘定稿标记（2026-08-18 拓斯达脏数据事故后新增）：盘中扫描把未收盘的今日 bar
    # （盘中价+部分量能）写入 daily_kline 属预期（today_report 盘中读），但收盘后无
    # 定稿覆盖会残留污染 next_day_pct → 回测/归因/复盘全口径。finalized=0 表示
    # 「盘中快照，可能非最终收盘价」；收盘定稿/收盘后写入的 bar 置 1。
    cur = conn.execute("PRAGMA table_info(daily_kline)")
    kline_cols = {r[1] for r in cur.fetchall()}
    if "finalized" not in kline_cols:
        conn.execute("ALTER TABLE daily_kline ADD COLUMN finalized INTEGER DEFAULT 1")
    cur = conn.execute("PRAGMA table_info(recommendations)")
    cols = {r[1] for r in cur.fetchall()}
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
