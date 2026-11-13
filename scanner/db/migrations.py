"""schema 迁移版本化编排（2026-09-13，测评 A5 落地）。

## 为什么要有这个模块

原先 `init_db` 把 15 个迁移块线性堆叠，每块用 `PRAGMA table_info` **现场探测**
当前状态来决定跑不跑。问题不是"写得丑"，而是这种结构**必然**产生两类事故：

1. **静默跳过、永不重试**：2026-09-11 修掉的 `market_extra_cache` v4 缺陷就是
   典型——迁移执行到一半失败留下半迁移现场，而下一轮的探测条件因半迁移而
   判定为"已完成"，于是**再也不会重试**。见 `_migrate_market_extra_cache_pk`
   的文档字符串（保留在 schema.py，本模块只是把它编排进来）。
2. **库与代码双向漂移不可见**：2026-09-13 实测生产库 ——
   - `hot_watch_hits` / `hot_watch_meta` **不存在**（v7 从未应用）；
   - `recommendations` 却有 `sector_capped` / `nd10_pct` 两列，**当前代码既不
     创建也不读取**（疑似已删除的旧分支加的列）。
   没有已执行记录，就无法回答"这个库到底跑到哪一版了"。

## 设计

每条迁移 = `(id, desc, check, up)`：

- `id`：**稳定唯一标识符**。一旦发布**不得改名或删除**（改名 = 老库会把它当新
  迁移重跑）。
- `check(conn)` → 是否已满足。用于给**存量库回填账本**：老库里这些结构早就有了，
  不能重跑 `up`，但要记进账本，否则每轮都要做一遍无谓的 PRAGMA 探测。
- `up(conn)` → 执行迁移，必须自身幂等/原子（失败即回滚，见下）。

`run_migrations` 的**失败语义是关键**：迁移失败 → 回滚 + **原样上抛** + **不写
账本** → 下一轮 `init_db` 会**重新尝试**。这就是对事故 1 的根治。

## 加新迁移的姿势

在 `MIGRATIONS` 末尾追加（不要插队），id 用 `mXXX_简述` 且 XXX 递增：

```python
Migration(
    id="m014_xxx",
    desc="给 yyy 表加 zzz 列",
    check=lambda conn: _has_column(conn, "yyy", "zzz"),
    up=lambda conn: conn.execute("ALTER TABLE yyy ADD COLUMN zzz REAL"),
)
```

`add_column` / `create_table` / `create_index` 三个工厂已覆盖绝大多数场景。
需要多语句原子迁移时，照抄 `m012` 的写法（executescript 单事务 + 失败上抛）。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from scanner.config import now_beijing

# 账本表名
LEDGER_TABLE = "schema_migrations"

#: `market_extra_cache` 上供 B 段资金流历史查询用的复合索引名（m023）。
FLOW_INDEX_NAME = "idx_mec_type_sym_date"


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """表存在且含该列。表不存在返回 False（→ 触发 up，由 up 自己负责建表）。"""
    try:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except sqlite3.Error:
        return False
    return column in cols


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    return row is not None


def _has_index(conn: sqlite3.Connection, index: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (index,)).fetchone()
    return row is not None


@dataclass(frozen=True)
class Migration:
    """一条 schema 迁移。`id` 发布后不得改名/删除（老库会当新迁移重跑）。"""

    id: str
    desc: str
    check: object  # Callable[[sqlite3.Connection], bool]
    up: object  # Callable[[sqlite3.Connection], None]

    def is_applied(self, conn: sqlite3.Connection) -> bool:
        return bool(self.check(conn))  # type: ignore[operator]


def add_column(table: str, column: str, decl: str, desc: str, mid: str) -> Migration:
    """工厂：加列。`decl` 是完整列声明（如 `INTEGER DEFAULT 0`）。"""

    def _up(conn: sqlite3.Connection) -> None:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    return Migration(
        id=mid,
        desc=desc,
        check=lambda conn: _has_column(conn, table, column),
        up=_up,
    )


def create_table(name: str, ddl: str, desc: str, mid: str) -> Migration:
    """工厂：建表（DDL 自带 IF NOT EXISTS 语义由调用方写在 ddl 里或依赖 check）。"""

    def _up(conn: sqlite3.Connection) -> None:
        conn.execute(ddl)

    return Migration(
        id=mid,
        desc=desc,
        check=lambda conn: _has_table(conn, name),
        up=_up,
    )


def create_index(name: str, ddl: str, desc: str, mid: str) -> Migration:
    """工厂：建索引。"""

    def _up(conn: sqlite3.Connection) -> None:
        conn.execute(ddl)

    return Migration(
        id=mid,
        desc=desc,
        check=lambda conn: _has_index(conn, name),
        up=_up,
    )


# ── 需要多语句 / 复用既有函数的迁移 ──


def _up_observation_schema(conn: sqlite3.Connection) -> None:
    """观测表（scan_rejections outcome 列 + pool_log）。

    实现仍在 `db.dal.ensure_observation_schema`（orchestrator 每轮也会直接调它兜底），
    此处只是把它纳入版本编排——两处指向同一实现，不存在第二份 DDL。
    2026-09-14：decision_picks 随决策层删除，不再是本迁移的目标表之一。
    """
    from scanner.db.dal import ensure_observation_schema

    ensure_observation_schema(conn)


def _check_observation_schema(conn: sqlite3.Connection) -> bool:
    # 2026-09-14：`decision_picks` 已从建成目标中移除，故校验条件同步去掉它 ——
    # 否则全新库永远校验不通过，本迁移每轮重复执行（幂等但白跑）。
    return _has_column(conn, "scan_rejections", "nd10_pct") and _has_table(conn, "pool_log")


def _up_market_extra_cache_pk(conn: sqlite3.Connection) -> None:
    from scanner.db.schema import _migrate_market_extra_cache_pk

    _migrate_market_extra_cache_pk(conn)


def _check_market_extra_cache_pk(conn: sqlite3.Connection) -> bool:
    from scanner.db.schema import _detect_mec_pk

    pk = _detect_mec_pk(conn)
    return not pk or pk == ["symbol", "data_type", "date"]


_HOT_WATCH_HITS_DDL = """
    CREATE TABLE IF NOT EXISTS hot_watch_hits (
        symbol TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        streak INTEGER NOT NULL DEFAULT 0,      -- 连续命中轮数（本轮未命中即归零）
        last_round INTEGER NOT NULL DEFAULT 0,  -- 最近命中的轮次号
        last_seen TEXT,                         -- 最近命中时间
        last_percent REAL,
        last_price REAL,
        last_score REAL
    )
"""
_HOT_WATCH_META_DDL = """
    CREATE TABLE IF NOT EXISTS hot_watch_meta (
        key TEXT PRIMARY KEY,   -- 目前仅 'round_no'
        value TEXT NOT NULL
    )
"""


def _up_hot_watch(conn: sqlite3.Connection) -> None:
    """v7（2026-09-11）：沪深飙升独立区的连击跟踪表。

    **生产库实测缺失**（2026-09-13）：这两张表从未被创建过——任何读它们的离线工具
    会直接报 no such table。纳入版本编排后，下一轮 init_db 会补建并记账。
    """
    conn.execute(_HOT_WATCH_HITS_DDL)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_hwh_streak ON hot_watch_hits(streak)")
    conn.execute(_HOT_WATCH_META_DDL)


def _check_hot_watch(conn: sqlite3.Connection) -> bool:
    return _has_table(conn, "hot_watch_hits") and _has_table(conn, "hot_watch_meta")


def _up_ranking_snapshot_drop_marked(conn: sqlite3.Connection) -> None:
    """m014（2026-09-16）：🎯 画像删除 → `ranking_snapshot.marked` 失去语义。

    该列是 🎯 判定的观测存证（`is_nextday_marked` 的结果），画像连同判定函数一起
    删除后，继续保留它只能写常量 0 —— 正是本仓明令避免的「看起来在记录、其实
    没信息」的字段。故直接掉列（SQLite ≥3.35 支持 ALTER TABLE ... DROP COLUMN；
    实测运行库 3.45）。只影响观测基建，不改排序/评分/候选生成。
    """
    conn.execute("ALTER TABLE ranking_snapshot DROP COLUMN marked")


def _check_ranking_snapshot_drop_marked(conn: sqlite3.Connection) -> bool:
    # check 语义 = 「已满足」：表不存在（全新库，schema.py 已无该列）或列已不在 → 满足。
    return not _has_column(conn, "ranking_snapshot", "marked")


_OFFBOARD_KLINE_DDL = """
    CREATE TABLE IF NOT EXISTS offboard_kline_cache (
        symbol TEXT NOT NULL,           -- 雪球符号（SZ300862）
        fetch_date TEXT NOT NULL,       -- 抓取日（当日缓存键；5 日累计/MA 只依赖抓取日
                                        -- 之前的 bar，故盘中一次抓取当日可复用）
        payload_json TEXT NOT NULL,     -- JSON：日线 bars（含抓取日盘中 bar，排除今日由消费方做）
        updated TEXT NOT NULL,
        PRIMARY KEY(symbol, fetch_date)
    )
"""
_OFFBOARD_LAUNCH_LOG_DDL = """
    CREATE TABLE IF NOT EXISTS offboard_launch_log (
        date TEXT NOT NULL,             -- 信号日
        symbol TEXT NOT NULL,
        name TEXT,
        tier TEXT NOT NULL,             -- 'T1'=量先动·价未动 / 'T2'=启动首日
        percent REAL,                   -- 信号时的今日涨幅（快照）
        accum_5d REAL,                  -- 5 日累计涨幅（排除今日）
        vol_ratio REAL,                 -- 量比（**决策值**：2026-11-06 起为去偏值，见 vol_ratio_raw）
        vol_ratio_raw REAL,             -- 上报原值（2026-11-05 起的量比去偏；旧行 NULL ⇒ 其 vol_ratio 即原值）
        main_pct REAL,                  -- 主力净占比（快照）
        amount REAL,                    -- 成交额（元）
        float_cap REAL,                 -- 流通市值（元）
        price REAL,                     -- 现价
        first_time TEXT,                -- 本区当日首次产出该行的时刻
        last_hit_time TEXT DEFAULT '',  -- 当日最后一次命中该行的时刻（v9 新增）
        top_hits INTEGER DEFAULT 0,     -- (v19) 当日进入展示前 N 的轮次；v20 起不再作标识依据，列保留
        gate_hits INTEGER DEFAULT 0,    -- 当日严格过门轮次（v20 新增；今日活跃标识= gate_hits/当日总轮数）
        next_day_pct REAL,              -- 次日收益（%，由 backfill 回填）
        updated TEXT DEFAULT '',
        -- m026（2026-11-12）证据链补全
        flow_sum REAL,                  -- N 日主力净额合计（元）；NULL = 未落库/不可判定
        flow_judged INTEGER DEFAULT 0,  -- 1 = 该行当日**判定过**流门（区分「=0」与「不可判定」）
        vol_bias_k REAL,                -- 当轮量比去偏系数 k(m)（= vol_ratio_raw/vol_ratio）
        PRIMARY KEY(date, symbol)
    )
"""


def _up_offboard_tables(conn: sqlite3.Connection) -> None:
    """v8（2026-09-18）：沪深飙升区 B 段（榜外异动）的两张表。

    1. `offboard_kline_cache` —— **榜外 K 线池，独立于 `daily_kline`**。后者的既定
       语义是「榜单衍生池」（999 只里 982 只在 appearances），塞入榜外票会污染所有
       基于它的回测基准与归因口径（portfolio_backtest / prevday_perf / 召回率）。
       B 段需要 5 日累计 / MA 结构 / 顶背离，只能另起一张。
    2. `offboard_launch_log` —— B 段逐日落库。**这是本类信号上线前的硬前置**：
       在有人能按日回溯量化之前，任何阈值调整都是无标签调参（项目铁律 observe-first）。
       `hot_watch_hits` 以 symbol 为主键、只存滚动最新态，回答不了「T1/T2 的次日
       hit 率是多少」。
    """
    conn.execute(_OFFBOARD_KLINE_DDL)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_obk_fetch_date ON offboard_kline_cache(fetch_date)")
    conn.execute(_OFFBOARD_LAUNCH_LOG_DDL)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_obl_date ON offboard_launch_log(date)")


def _check_offboard_tables(conn: sqlite3.Connection) -> bool:
    return _has_table(conn, "offboard_kline_cache") and _has_table(conn, "offboard_launch_log")


def _up_offboard_log_last_hit(conn: sqlite3.Connection) -> None:
    """v9（2026-09-21）：`offboard_launch_log` 补 `last_hit_time`。

    动机：`updated` 被两处写（`persist_round` 每轮 + `backfill_next_day` 回填），
    于是它既不是「命中时刻」也不是「回填时刻」——09-18 那 5 行的 `updated` 全被回填
    写成 09-21 的时刻，和当日行的 `first_time` 撞在一起，命中时间维度事实上丢失。
    拆出专列后语义唯一：`first_time` 首次产出、`last_hit_time` 最后命中、
    `updated` 最后任何写入。

    与本次同批的 `persist_round` 语义变更（信号值首次写入即冻结）配套：只有
    `last_hit_time` / `updated` 每轮刷新。
    """
    conn.execute("ALTER TABLE offboard_launch_log ADD COLUMN last_hit_time TEXT DEFAULT ''")


def _check_offboard_log_last_hit(conn: sqlite3.Connection) -> bool:
    # check 语义 = 「已满足」：表不存在（全新库，DDL 已含该列）或列已在 → 满足。
    return not _has_table(conn, "offboard_launch_log") or _has_column(conn, "offboard_launch_log", "last_hit_time")


_OFFBOARD_ROUND_LOG_DDL = """
    CREATE TABLE IF NOT EXISTS offboard_round_log (
        date TEXT NOT NULL PRIMARY KEY,   -- 信号日
        rounds INTEGER NOT NULL DEFAULT 0, -- 当日 B 段扫描轮数（每轮 +1；今日活跃标识的分母）
        updated TEXT
    )
"""

_OFFBOARD_REJECTIONS_DDL = """
    CREATE TABLE IF NOT EXISTS offboard_rejections (
        date TEXT NOT NULL,
        symbol TEXT NOT NULL,
        name TEXT NOT NULL DEFAULT '',
        reason TEXT NOT NULL,          -- 最新一轮的拒绝理由（门槛串 / 分层理由串）
        percent REAL,                  -- 被拒时的快照涨幅（%）【最新一轮】
        vol_ratio REAL,                -- 被拒时的量比【最新一轮】
        main_pct REAL,                 -- 被拒时的主力净占比【最新一轮】
        hits INTEGER DEFAULT 1,        -- 当日累计命中轮次（同票多轮被拒只累加）
        first_time TEXT NOT NULL,
        updated TEXT NOT NULL,
        -- m026（2026-11-12）：**首次被拒**时的冻结态，与 offboard_launch_log 的
        -- 「首次写入即冻结」同锚（都锚在当日第一次被拒/产出的时刻）。
        -- 上面三列是「最新一轮」覆盖值，与 launch_log 的冻结列**不同锚** ⇒
        -- 跨表 join 会拼出两个不同时刻的状态。复盘一律用 first_* 这几列。
        first_reason TEXT,             -- 首次被拒的理由（冻结）
        first_percent REAL,            -- 首次被拒时的涨幅（冻结）
        first_vol_ratio REAL,          -- 首次被拒时的量比（冻结）
        first_main_pct REAL,           -- 首次被拒时的主力净占比（冻结）
        gate TEXT,                     -- 拒绝发生阶段：'gate'（门槛）/ 'tier'（分层）
        reason_code TEXT,              -- 结构化门 ID（'pct_band' / 'vol_ratio' / ...），可聚合
        reason_value REAL,             -- 该门的实际读数（原 reason 串里的数值）
        PRIMARY KEY(date, symbol)
    )
"""


def _up_offboard_rejections(conn: sqlite3.Connection) -> None:
    """v10（2026-09-22）：B 段（榜外异动）**拒绝留痕表**。

    为什么需要它 —— 与 `scan_rejections`（m009）完全同一个理由，只是晚了 4 天：
    `run_offboard_watch` 原先把 `build_candidates` 返回的 `_rejects` 直接丢弃
    （`cands, _rejects = ...`），分层阶段的拒绝理由也只写进 `c.reasons` 就地消失 ——
    于是「谁被哪道门杀了、为什么」**只能靠当场手工重建快照反推**。

    2026-09-22 实测：创业板 1410 → 过门 401 → 有 K 线 143 → 产出 12；
    **258 只无 K 线的候选静默消失**（占过门候选 64%），另有 5日累计杀 63、
    T2 的 MA 杀 30 —— 这些数字全是事后用临时脚本重算出来的，跑完就没了。

    没有留痕就只有幸存者样本（`offboard_launch_log` 只存**产出**行）= 典型幸存者偏差：
    「某道门误杀了多少、被杀的票次日涨得怎样」永远答不出来，于是任何阈值调整
    都无从证伪。而本区是 `rule_validate` 三个评估器的**盲区**（`--set` 会被可见性
    硬校验拦在退出码 3），这张表是唯一能积累起来的证据链。

    观察目标 D ≥ 47 交易日（按日 bootstrap 检出 10pp 需 47 日；D=3 时 MDE ±39.6pp）。
    """
    conn.execute(_OFFBOARD_REJECTIONS_DDL)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_obr_date ON offboard_rejections(date)")


def _check_offboard_rejections(conn: sqlite3.Connection) -> bool:
    return _has_table(conn, "offboard_rejections")


def _up_onboard_anomaly_log(conn: sqlite3.Connection) -> None:
    """m018（2026-09-30）：榜内异动段逐日落库表。

    为什么需要 —— 与 m015/m017（榜外那套）**同一个理由**：本区是 `rule_validate`
    三个评估器的盲区（`--set` 会被可见性硬校验拦在退出码 3），而项目铁律是
    observe-first。**没有逐日表就没有标签，没有标签就没有任何阈值能被证伪**，
    调 `OFFBOARD_*` 那组常量等于无标签调参。

    与榜外表的区别：K 线来源是 `daily_kline`（榜内票本就在其中 —— 它是「榜单衍生池」），
    故**不需要** m015 那张 `offboard_kline_cache`，本段只落产出行。

    `next_day_pct` 由回填写入，与 `offboard_launch_log` 同口径（次日涨幅 %）。

    观察目标 D ≥ 47 交易日（按日 bootstrap 检出 10pp 需 47 日；参照 B 段）。

    ⚠ DDL 写成**字面量**而非模块级常量拼接：动态 DDL 字符串会让 SQL 静态检查
    无法判定其安全性（榜外 `_up_offboard_rejections` 用的 `_OFFBOARD_REJECTIONS_DDL`
    常量正是因此被标记）。表名/列名均为本文件内写死的字面量，无外部输入。
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS onboard_anomaly_log ("
        "date TEXT NOT NULL, symbol TEXT NOT NULL, name TEXT, tier TEXT, percent REAL, "
        "accum_5d REAL, vol_ratio REAL, main_pct REAL, amount REAL, float_cap REAL, "
        "price REAL, first_time TEXT, next_day_pct REAL, updated TEXT, "
        "PRIMARY KEY (date, symbol))"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_oal_date ON onboard_anomaly_log(date)")


def _check_onboard_anomaly_log(conn: sqlite3.Connection) -> bool:
    return _has_table(conn, "onboard_anomaly_log")


def _up_offboard_log_top_hits(conn: sqlite3.Connection) -> None:
    """v19（2026-10-14）：`offboard_launch_log` 补 `top_hits`。

    动机：B 段「反复进前十」粉名/🔁 标识（2026-10-14 用户需求）的落库依据。
    此前表只记「严格过门」，过门行可能超过展示前 N（OFFBOARD_DISPLAY_TOP），
    「进入前十」无从事后重建。列语义：**当日进入展示前 N 的轮次数**（persist_round
    对前 N 行 +1，ON CONFLICT 累加 —— 与信号值冻结列相反，计数列每轮累加，
    跨日因 (date,symbol) 主键自然重置）。存量行 DEFAULT 0 = 未进过前十，正确。
    """
    conn.execute("ALTER TABLE offboard_launch_log ADD COLUMN top_hits INTEGER DEFAULT 0")


def _check_offboard_log_top_hits(conn: sqlite3.Connection) -> bool:
    return not _has_table(conn, "offboard_launch_log") or _has_column(conn, "offboard_launch_log", "top_hits")


def _up_offboard_log_vol_ratio_raw(conn: sqlite3.Connection) -> None:
    """m024（2026-11-05）：`offboard_launch_log` / `onboard_anomaly_log` 补 `vol_ratio_raw`。

    动机 —— 量比**去偏**（`scanner/volume_bias.py`）改变了 `vol_ratio` 列的语义：
    2026-11-06 起写入的是**去偏值**（= 与收盘同尺度的量比），而 2026-11-05 之前
    写入的是**上报原值**。同一列两种尺度 ⇒ 按量比分桶的历史分析（`scripts/
    offboard_nextday.py` 的量比分桶、`scripts/shadow_feature_backtest.py` 的量比
    特征）在跨改动的样本上直接不可比。

    修法：新增一列存**上报原值**，`vol_ratio` 保持「**决策值**」语义不变
    （= 落库那刻门/排序/展示实际用的那个数，跨历史一致）。分析侧统一读
    `coalesce(vol_ratio_raw, vol_ratio)` ⇒ 恒为原值尺度，跨改动可比。

    旧行 `vol_ratio_raw` 为 NULL ⇒ 其 `vol_ratio` 本来就是原值 ⇒ coalesce 正确回退。
    """
    if not _has_column(conn, "offboard_launch_log", "vol_ratio_raw"):
        conn.execute("ALTER TABLE offboard_launch_log ADD COLUMN vol_ratio_raw REAL")
    if _has_table(conn, "onboard_anomaly_log") and not _has_column(conn, "onboard_anomaly_log", "vol_ratio_raw"):
        conn.execute("ALTER TABLE onboard_anomaly_log ADD COLUMN vol_ratio_raw REAL")


def _check_offboard_log_vol_ratio_raw(conn: sqlite3.Connection) -> bool:
    return (
        not _has_table(conn, "offboard_launch_log") or _has_column(conn, "offboard_launch_log", "vol_ratio_raw")
    ) and (not _has_table(conn, "onboard_anomaly_log") or _has_column(conn, "onboard_anomaly_log", "vol_ratio_raw"))


def _up_offboard_evidence_chain(conn: sqlite3.Connection) -> None:
    """m026（2026-11-12）：B 段证据链补全 —— 两表同锚 + 结构化 reason + 流门落库。

    三条独立动机，同批发布是因为它们共同服务**同一件事**：让
    `offboard_rejections` / `offboard_launch_log` 能被事后正确地复盘。

    1. 🔴 **两表时间锚不一致**（最严重）
       `offboard_launch_log` 的信号列是「**首次写入即冻结**」（m016 起），而
       `offboard_rejections` 的 `percent`/`vol_ratio`/`main_pct` 是「**最新一轮覆盖**」
       （m017 起，`dal.save_offboard_rejections` 的 ON CONFLICT）。
       单看每张表都自洽，**跨表 join 就拼错**：实测 2026-11-12 `SZ300063` 在
       launch_log 冻结 percent=+2.05（当日曾以「上涨异动」身份被推出），在
       rejections 却是 −1.83、理由「当前非上涨状态」—— 同一只票两个相反结论。
       而这张表存在的唯一理由是让幸存者偏差可被证伪 ⇒ 必须能对齐到同一时刻。
       修法：补 `first_*` 四列，**首次被拒即冻结**（与 launch_log 同锚）。

    2. `reason` 串把**当轮具体数值**编了进去（「量比不足(0.90<1.5)」）⇒ 同一道
       逻辑门产生数万条互不相同的 reason。实测生产库 44706 条里，按 reason 精确
       GROUP BY 得到的主要「门」其实是 0.90/0.82/0.89 这类数值碎片 ⇒
       「哪道门杀得最多」这个最基本的问题答不出来。
       修法：补 `reason_code`（门 ID，可聚合）+ `reason_value`（读数）+ `gate`（阶段）。
       `reason` 串**保留原样**（人读 / 旧分析脚本兼容）。

    3. N 日主力累计流门（`OFFBOARD_FLOW_WINDOW`）的判定输入 `flow_sum` 每轮实时算、
       **不落库** ⇒ 事后答不出「当日过门的票资金是正是负」，只能拿今天的库反推
       过去某天的窗口（成员日快照已被覆盖，近似不等价）。
       且阈值 `OFFBOARD_FLOW_MIN_SUM = 0.0` 让「恰好 0（放行）」与「不可判定
       （也放行）」在库里都是 NULL/0 ⇒ 语义糊掉。
       修法：`flow_sum` + `flow_judged`（1 = 当日判定过，与「不可判定」区分）。

    另补 `vol_bias_k`：量比去偏系数 k(m) 本身落库（= vol_ratio_raw/vol_ratio）。
    此前只能反算，反算在 vol_ratio_raw 为 NULL 的旧行上不可用。

    ⚠ 全部用 `ADD COLUMN`（本仓铁律：发布后不得改名/删列，只能追加）。
    ⚠ 每个子步骤各自判幂等：up 分步执行、账本不写 ⇒ 下轮重跑整个 up，
    此时无条件 ALTER 会撞 duplicate column（与 m020/m024 同款纪律）。
    """
    if _has_table(conn, "offboard_rejections"):
        for col, typ in (
            ("first_reason", "TEXT"),
            ("first_percent", "REAL"),
            ("first_vol_ratio", "REAL"),
            ("first_main_pct", "REAL"),
            ("gate", "TEXT"),
            ("reason_code", "TEXT"),
            ("reason_value", "REAL"),
        ):
            if not _has_column(conn, "offboard_rejections", col):
                conn.execute(f"ALTER TABLE offboard_rejections ADD COLUMN {col} {typ}")
        # 存量行回填：老行的「最新一轮」就是**唯一**已知态 ⇒ 用它当首次态。
        # 这不是等价恢复（真实首次态已丢），但比留 NULL 强：至少量纲与锚点一致，
        # 且 `first_reason` 非空可作「已回填」标记，避免与真正的新行混淆。
        conn.execute(
            "UPDATE offboard_rejections SET "
            "first_reason = COALESCE(first_reason, reason), "
            "first_percent = COALESCE(first_percent, percent), "
            "first_vol_ratio = COALESCE(first_vol_ratio, vol_ratio), "
            "first_main_pct = COALESCE(first_main_pct, main_pct) "
            "WHERE first_reason IS NULL"
        )

    if _has_table(conn, "offboard_launch_log"):
        for col, typ in (
            ("flow_sum", "REAL"),
            ("flow_judged", "INTEGER DEFAULT 0"),
            ("vol_bias_k", "REAL"),
        ):
            if not _has_column(conn, "offboard_launch_log", col):
                conn.execute(f"ALTER TABLE offboard_launch_log ADD COLUMN {col} {typ}")
        # vol_bias_k 回填：能从 vol_ratio_raw/vol_ratio 反算的旧行直接补上
        # （2026-11-06 起的行都有 raw）。算不出的留 NULL = 「未知」，不猜 1.0。
        conn.execute(
            "UPDATE offboard_launch_log SET vol_bias_k = vol_ratio_raw / vol_ratio "
            "WHERE vol_bias_k IS NULL AND vol_ratio_raw IS NOT NULL AND vol_ratio > 0"
        )


def _check_offboard_evidence_chain(conn: sqlite3.Connection) -> bool:
    # 语义 = 「已满足」：表不存在（全新库，DDL 已含这些列）或列全在 → 满足。
    if _has_table(conn, "offboard_rejections"):
        for col in ("first_reason", "reason_code", "gate"):
            if not _has_column(conn, "offboard_rejections", col):
                return False
    if _has_table(conn, "offboard_launch_log"):
        for col in ("flow_sum", "flow_judged", "vol_bias_k"):
            if not _has_column(conn, "offboard_launch_log", col):
                return False
    return True


def _up_offboard_active_metrics(conn: sqlite3.Connection) -> None:
    """v20（2026-10-14）：「今日活跃」标识的落库依据（在场率口径，替代 top_hits）。

    一版「反复进前十」（top_hits）实测无筛选力：过门池常年 ≤ 展示前 N（
    2026-10-14 实测 21 轮里 19 轮 ≤8 只），「进前十」≈「过门」，出场几分钟即达标
    ⇒ 前 8 名几乎全粉。二版改判**在场率** = gate_hits / rounds：
      · `offboard_launch_log.gate_hits`：当日**严格过门**轮数（persist_round 对
        **所有**过门行 +1，不再只前 N；ON CONFLICT 累加，跨日自然重置）；
      · `offboard_round_log`：当日 B 段真实扫描轮数，每天**一行**（date 主键，
        rounds 单列），**含零候选轮** —— 零候选轮也是一轮扫描，分母漏了它们，
        午后过门池变小时在场率会被虚高。persist_round 每轮 UPSERT。
    top_hits 列保留（m019 已发布，id 不得改名/删列；存量行照读，只是不再作
    标识依据）。

    ⚠ 两个子步骤各自判幂等：`_OFFBOARD_LAUNCH_LOG_DDL`（新库建表）已含
    `gate_hits`，而 `offboard_round_log` 可能因中途失败单独缺失（up 分步执行、
    账本不写 ⇒ 下轮重跑整个 up，此时再无条件 ALTER 会撞 duplicate column）。
    """
    if not _has_column(conn, "offboard_launch_log", "gate_hits"):
        conn.execute("ALTER TABLE offboard_launch_log ADD COLUMN gate_hits INTEGER DEFAULT 0")
    if not _has_table(conn, "offboard_round_log"):
        # DDL 内联而非引 _OFFBOARD_ROUND_LOG_DDL 常量：与 m018 同款纪律 —— 动态 DDL
        # 字符串（含常量变量引用）会让 SQL 静态检查无法判定安全性。表名/列名均为本
        # 文件内写死的字面量，无外部输入。测试夹具另引常量（内存库，无静态检查诉求）。
        conn.execute(
            "CREATE TABLE IF NOT EXISTS offboard_round_log ("
            "date TEXT NOT NULL PRIMARY KEY, rounds INTEGER NOT NULL DEFAULT 0, updated TEXT)"
        )


def _check_offboard_active_metrics(conn: sqlite3.Connection) -> bool:
    return not _has_table(conn, "offboard_launch_log") or (
        _has_column(conn, "offboard_launch_log", "gate_hits") and _has_table(conn, "offboard_round_log")
    )


def _up_view_snapshot(conn: sqlite3.Connection) -> None:
    """m021（2026-10-19）：桌面版本地看板的**最近一轮载荷**落盘副本。

    动机（唯一）：`local_panel_server` 只服务进程内缓存（design D1），进程重启后
    （Electron 关窗重开、崩溃恢复）在首个交易轮跑完前，面板拿不到任何东西。落一份
    最近载荷让 `start_server` 启动时能回放「上次已知的一轮」——非交易时段启动也
    有内容可看，符合「桌面版应显非交易时段而非空白」的既定要求。

    ⚠ 与云端 D1 的 `snapshots` 表**同构但不同库**：本表**不是**上报通道，只是本地
    缓存的落盘副本。写侧唯一 = 主循环的 `local_view_cache.persist_latest`；
    读侧唯一 = `local_panel_server`（启动回填）。两处都不参与任何评分/筛选口径。

    ⚠ DDL 写成**字面量**而非模块级常量拼接（与 m018/m020 同款纪律）：动态 DDL
    字符串会让 SQL 静态检查无法判定其安全性。表名/列名均为本文件内写死的字面量。
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS view_snapshot ("
        "date TEXT NOT NULL, time TEXT NOT NULL, seq INTEGER NOT NULL DEFAULT 0, "
        "payload TEXT NOT NULL, updated_at TEXT NOT NULL, "
        "PRIMARY KEY (date, time))"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_view_snapshot_round ON view_snapshot(date DESC, time DESC)")


def _check_view_snapshot(conn: sqlite3.Connection) -> bool:
    return _has_table(conn, "view_snapshot")


def _up_offboard_print_snapshot(conn: sqlite3.Connection) -> None:
    """m022（2026-11-03）：`offboard_print_snapshot` —— **终端最后一次打印的榜外异动行**。

    为什么必须单独一张表（而不是复用现有两张）：

    - `offboard_launch_log` 是**整日累加**（`(date, symbol)` 主键，信号值首次写入即冻结）：
      实测 2026-11-02 有 17 行，而终端那一轮只打印 8 行（更晚的轮次可能只有 3 行）。
      「当日过门过哪些票」≠「屏幕上最后显示的是哪几只」。
    - `view_snapshot` 能记录每轮，但它被 `RTS_LOCAL_PANEL` 门控（只有桌面壳 Electron
      注入该开关）⇒ 纯命令行跑扫描器时**一条都不落**（实测：库里最新快照停在 10-28，
      而 11-02/11-03 都跑过扫描）。把它当「终端打印」的真源会长期静默失效。

    故本表只记**一个量**：某日最后一轮终端实际渲染的 B 段行（`print_seq` 单调递增，
    新行 `print_seq` 更大）。写侧 = `offboard_watch.persist_print_snapshot`（每轮覆盖式
    追加，按 `print_seq` 取最新）；读侧 = `scanner.ai_digest` 的 view 源与
    `python -m scanner.offboard_watch --print-latest`。

    ⚠ 与 `offboard_launch_log` 的**口径差异必须保留**：本表存的是「屏幕上的那几只」，
    可能是**留存行**（`retain=True`，不入 launch_log）；launch_log 存的是「当日过门的
    全部票」。两者不可互换 —— AI 分析要的是前者（用户看到什么就分析什么）。
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS offboard_print_snapshot ("
        "date TEXT NOT NULL, time TEXT NOT NULL, print_seq INTEGER NOT NULL, "
        "symbol TEXT NOT NULL, name TEXT, tier TEXT, percent REAL, accum_5d REAL, "
        "vol_ratio REAL, main_pct REAL, amount REAL, float_cap REAL, price REAL, "
        "sector TEXT, turnover_rate REAL, ff_pct REAL, beauty TEXT, reasons TEXT, "
        "rank_in_print INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL, "
        "PRIMARY KEY (date, print_seq, symbol))"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ops_latest ON offboard_print_snapshot(date DESC, print_seq DESC)")


def _check_offboard_print_snapshot(conn: sqlite3.Connection) -> bool:
    return _has_table(conn, "offboard_print_snapshot")


def _up_announcement_cache(conn: sqlite3.Connection) -> None:
    """m025（2026-11-11）：公告缓存表 + 同步水位表（离线交叉验证的单一真相）。

    为什么必须落库而不是继续用 `logs/shadow_feature_cache.json`（实测 12MB / 520 票）：
    - 那份缓存的写侧是 `todo = [s for s in symbols if s not in cache]` —— **按票、
      命中即永久冻结**。实测最后写入停在 2026-11-02，而 11-06 宇邦要约收购、
      11-10 智信回购方案全都不在缓存里 ⇒ 「有缓存」等于「没有缓存」。
    - 它把公告 / 资金流 / 龙虎榜三类数据混在一个 12MB JSON 里，公告部分无法单独
      按日期增量，也无法被主链路与离线脚本共用。

    本表的两条硬约束（改错会静默算错，故写进迁移而不是脚本注释）：
    ① 主键含 `content_hash`：公告标题可能被上游重发/修订，**不能按 title 覆盖**；
    ② `ann_sync_waterline` 记水位：读侧必须打印「缓存截止日」，缺水位或过期要
       显式告警，**不能静默当「无公告」**。历史公告一经发布不再改 ⇒ 不需要 TTL，
       只需要水位（与 sift-cli 的「公告元数据无 TTL、PDF 永久」同思路）。
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS announcement_cache ("
        "symbol TEXT NOT NULL, notice_date TEXT NOT NULL, title TEXT NOT NULL, "
        "ann_type TEXT, source_url TEXT, content_hash TEXT NOT NULL, "
        "fetched_at TEXT NOT NULL, "
        "PRIMARY KEY (symbol, notice_date, content_hash))"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ann_cache_date ON announcement_cache(notice_date, symbol)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS ann_sync_waterline ("
        "scope TEXT NOT NULL, end_date TEXT NOT NULL, rows INTEGER NOT NULL DEFAULT 0, "
        "updated TEXT NOT NULL, PRIMARY KEY (scope))"
    )


def _check_announcement_cache(conn: sqlite3.Connection) -> bool:
    return _has_table(conn, "announcement_cache") and _has_table(conn, "ann_sync_waterline")


def _check_market_extra_flow_index(conn: sqlite3.Connection) -> bool:
    """索引存在性（`sqlite_master` 查 `index` 类型）。

    ⚠ **表不存在时返回 True（视为已满足）**：`market_extra_cache` 由
    `schema.init_db` 建（**不是**迁移建的）⇒ 迁移单独跑（如只跑 `run_migrations`
    的单测或运维脚本）时表可能不存在。此时**不能**报错中断整轮迁移 ——
    表会在下次 `init_db` 建好，而 `init_db` 之后会再跑一次迁移。
    """
    if not _has_table(conn, "market_extra_cache"):
        return True
    return bool(
        conn.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (FLOW_INDEX_NAME,)).fetchone()
    )


def _up_market_extra_flow_index(conn: sqlite3.Connection) -> None:
    """给 `market_extra_cache` 加 `(data_type, symbol, date DESC)` 复合索引。

    用途：B 段「N 日主力累计净流入门」要按 symbol 取最近 N 天的资金流快照
    （`scanner/offboard_flow.py`）。无此索引时该查询会退化成
    「按 symbol 定位后临时 B-tree 排序」；实测 535 只候选 744ms → 637ms，
    40 只（真实每轮量级）约 70ms ⇒ **不是瓶颈**，索引只是锦上添花。

    ⚠ `CREATE INDEX IF NOT EXISTS` ⇒ 可重复执行；表大时建索引需短暂写锁
    （168k 行约 1s）。
    ⚠ 表不存在时**跳过**（不抛错）：表由 `schema.init_db` 建，不是迁移建的。
    """
    if not _has_table(conn, "market_extra_cache"):
        return
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS {FLOW_INDEX_NAME} ON market_extra_cache(data_type, symbol, date DESC)"  # noqa: S608 - 常量索引名
    )


# ── 迁移总表（顺序即执行顺序；只在末尾追加）──
MIGRATIONS: list[Migration] = [
    add_column(
        "daily_kline",
        "finalized",
        "INTEGER DEFAULT 1",
        "收盘定稿标记（0=盘中快照，收盘后未定稿会污染 next_day_pct）",
        "m001_daily_kline_finalized",
    ),
    add_column(
        "recommendations",
        "source",
        "TEXT DEFAULT 'xueqiu'",
        "数据来源（雪球/THS 兜底）",
        "m002_rec_source",
    ),
    add_column(
        "recommendations",
        "cum_2d",
        "REAL",
        "T+0 收盘 → T+2 收盘累计涨幅（持有 2 天）",
        "m003_rec_cum_2d",
    ),
    add_column(
        "recommendations",
        "cum_3d",
        "REAL",
        "T+0 收盘 → T+3 收盘累计涨幅（持有 3 天）",
        "m004_rec_cum_3d",
    ),
    add_column(
        "recommendations",
        "concept",
        "TEXT",
        "推动概念（掉榜/重启后仍能展示「板块」列）",
        "m005_rec_concept",
    ),
    add_column(
        "recommendations",
        "accumulated_pct",
        "REAL",
        "5 日累计涨幅（综合排序展示用）",
        "m006_rec_accumulated_pct",
    ),
    add_column(
        "recommendations",
        "excluded",
        "INTEGER DEFAULT 0",
        "硬过滤落标（命中卖出/止损级标签置 1，综合排序排除）",
        "m007_rec_excluded",
    ),
    add_column(
        "recommendations",
        "stale_kline",
        "INTEGER DEFAULT 0",
        "评分所用 K 线是否缺今日 bar（数据血缘审计）",
        "m008_rec_stale_kline",
    ),
    add_column(
        "recommendations",
        "excluded_reason",
        "TEXT",
        "硬过滤原因（消除「无审计依据的误杀」盲点）",
        "m009_rec_excluded_reason",
    ),
    create_index(
        "idx_rec_source",
        "CREATE INDEX IF NOT EXISTS idx_rec_source ON recommendations(source)",
        "按来源查询推荐（回测/归因常用）",
        "m010_idx_rec_source",
    ),
    Migration(
        id="m011_observation_schema",
        desc="观测表：scan_rejections outcome 列 + pool_log（2026-09-14 起 decision_picks 不再建表）",
        check=_check_observation_schema,
        up=_up_observation_schema,
    ),
    Migration(
        id="m012_market_extra_cache_pk",
        desc="market_extra_cache 主键 (symbol,data_type) → (symbol,data_type,date) 原子重建",
        check=_check_market_extra_cache_pk,
        up=_up_market_extra_cache_pk,
    ),
    Migration(
        id="m013_hot_watch_tables",
        desc="hot_watch 独立区连击跟踪表（hits / meta）",
        check=_check_hot_watch,
        up=_up_hot_watch,
    ),
    Migration(
        id="m014_ranking_snapshot_drop_marked",
        desc="ranking_snapshot 掉 marked 列（🎯 画像已删除，该列失去语义）",
        check=_check_ranking_snapshot_drop_marked,
        up=_up_ranking_snapshot_drop_marked,
    ),
    Migration(
        id="m015_offboard_tables",
        desc="沪深飙升 B 段（榜外异动）：榜外 K 线池 + 逐日落库表",
        check=_check_offboard_tables,
        up=_up_offboard_tables,
    ),
    Migration(
        id="m016_offboard_log_last_hit",
        desc="offboard_launch_log 补 last_hit_time（updated 被回填补写，命中时刻需独立列）",
        check=_check_offboard_log_last_hit,
        up=_up_offboard_log_last_hit,
    ),
    Migration(
        id="m017_offboard_rejections",
        desc="B 段拒绝留痕表 offboard_rejections（谁被哪道门杀、为什么；反幸存者偏差）",
        check=_check_offboard_rejections,
        up=_up_offboard_rejections,
    ),
    Migration(
        id="m018_onboard_anomaly_log",
        desc="榜内异动段逐日落库表 onboard_anomaly_log（observe-first 的唯一证据来源）",
        check=_check_onboard_anomaly_log,
        up=_up_onboard_anomaly_log,
    ),
    Migration(
        id="m019_offboard_log_top_hits",
        desc="offboard_launch_log 补 top_hits（当日进入展示前 N 的轮次；反复进前十标识的落库依据）",
        check=_check_offboard_log_top_hits,
        up=_up_offboard_log_top_hits,
    ),
    Migration(
        id="m020_offboard_active_metrics",
        desc="offboard_launch_log 补 gate_hits + 新增 offboard_round_log（今日活跃标识的在场率落库依据）",
        check=_check_offboard_active_metrics,
        up=_up_offboard_active_metrics,
    ),
    Migration(
        id="m021_view_snapshot",
        desc="新增 view_snapshot（桌面版本地看板的最近一轮载荷落盘副本，重启回放用）",
        check=_check_view_snapshot,
        up=_up_view_snapshot,
    ),
    Migration(
        id="m022_offboard_print_snapshot",
        desc="新增 offboard_print_snapshot（终端最后一次打印的榜外异动行；与 launch_log 的整日累加口径分开）",
        check=_check_offboard_print_snapshot,
        up=_up_offboard_print_snapshot,
    ),
    Migration(
        id="m023_market_extra_flow_index",
        desc="新增 market_extra_cache 复合索引 (data_type, symbol, date DESC)：B 段 N 日主力累计净流入门的历史查询",
        check=_check_market_extra_flow_index,
        up=_up_market_extra_flow_index,
    ),
    Migration(
        id="m024_offboard_log_vol_ratio_raw",
        desc="offboard_launch_log / onboard_anomaly_log 补 vol_ratio_raw（量比去偏前的上报原值；vol_ratio 保持决策值语义）",
        check=_check_offboard_log_vol_ratio_raw,
        up=_up_offboard_log_vol_ratio_raw,
    ),
    Migration(
        id="m025_announcement_cache",
        desc="公告缓存 announcement_cache + 同步水位 ann_sync_waterline（离线交叉验证单一真相；替掉冻结式 12MB JSON）",
        check=_check_announcement_cache,
        up=_up_announcement_cache,
    ),
    Migration(
        id="m026_offboard_evidence_chain",
        desc="B 段证据链：rejections 首次态冻结列（与 launch_log 同锚）+ reason 结构化 + launch_log 流门/k 落库",
        check=_check_offboard_evidence_chain,
        up=_up_offboard_evidence_chain,
    ),
]


def _ensure_ledger(conn: sqlite3.Connection) -> None:
    # noqa 说明：LEDGER_TABLE 是**模块常量**，不是外部输入，无注入面。
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {LEDGER_TABLE} (
            id TEXT PRIMARY KEY,
            desc TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
    """)


def run_migrations(conn: sqlite3.Connection, *, verbose: bool = True) -> list[str]:
    """按序应用未执行的迁移，返回**本次真正执行**的 id 列表。

    三条语义（这是本模块存在的理由）：

    1. **已执行的跳过**：账本里有记录 → 跳过。
    2. **存量库回填**：账本无记录但 `check()` 为真（老库早就有这个结构）→ **只记账
       不执行**，避免重跑 `up`（重跑 ALTER 会报 duplicate column）。
    3. **失败即重试**：`up` 抛异常 → 回滚 + **原样上抛** + **不写账本** →
       下轮 `init_db` 重新尝试。绝不静默跳过（2026-09-11 事故的根因）。

    调用方（unified_scanner 主循环 / 各脚本）已有 `EXTERNAL_FAILURES` 兜底，
    `sqlite3.Error` 属其列 → 不会打崩扫描，但**会被看见**。
    """
    _ensure_ledger(conn)
    recorded = {r[0] for r in conn.execute(f"SELECT id FROM {LEDGER_TABLE}").fetchall()}  # noqa: S608 - 常量表名
    applied_now: list[str] = []

    for m in MIGRATIONS:
        if m.id in recorded:
            continue
        if m.is_applied(conn):
            _record(conn, m)
            recorded.add(m.id)
            continue
        if verbose:
            print(f"  [schema] 应用迁移 {m.id}：{m.desc}")
        try:
            m.up(conn)  # type: ignore[operator]
        except BaseException as exc:
            # 2026-09-29 审查修复：原只捕 sqlite3.Error——迁移代码自身抛
            # TypeError/KeyError 等时跳过 rollback 且事务保持打开上抛（get_conn
            # 为隐式事务模式），上层宽捕获后复用同连接会持写锁阻塞主循环写入。
            # 任何失败路径（含 KeyboardInterrupt）都先释放锁再上抛。
            conn.rollback()
            print(f"  [schema] 迁移 {m.id} 失败（已回滚，下轮重试）：{exc}")
            raise
        _record(conn, m)
        recorded.add(m.id)
        applied_now.append(m.id)

    return applied_now


def _record(conn: sqlite3.Connection, m: Migration) -> None:
    conn.execute(
        f"INSERT OR REPLACE INTO {LEDGER_TABLE} (id, desc, applied_at) VALUES (?, ?, ?)",  # noqa: S608 - 常量表名
        (m.id, m.desc, now_beijing().isoformat()),
    )


def migration_state(conn: sqlite3.Connection) -> dict[str, str]:
    """给运维/诊断用：返回 {迁移 id: 'applied' | 'pending' | 'unknown'}。

    `unknown` = 账本无记录但 `check()` 为真（存量库回填前的中间态；正常跑一轮
    init_db 即转为 applied）。
    """
    _ensure_ledger(conn)
    recorded = {r[0] for r in conn.execute(f"SELECT id FROM {LEDGER_TABLE}").fetchall()}  # noqa: S608 - 常量表名
    state: dict[str, str] = {}
    for m in MIGRATIONS:
        if m.id in recorded:
            state[m.id] = "applied"
        else:
            state[m.id] = "unknown" if m.is_applied(conn) else "pending"
    return state
