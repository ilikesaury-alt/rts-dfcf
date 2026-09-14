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


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """表存在且含该列。表不存在返回 False（→ 触发 up，由 up 自己负责建表）。"""
    try:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except sqlite3.Error:
        return False
    return column in cols


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _has_index(conn: sqlite3.Connection, index: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (index,)
    ).fetchone()
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


# ── 迁移总表（顺序即执行顺序；只在末尾追加）──

MIGRATIONS: list[Migration] = [
    add_column(
        "daily_kline", "finalized", "INTEGER DEFAULT 1",
        "收盘定稿标记（0=盘中快照，收盘后未定稿会污染 next_day_pct）",
        "m001_daily_kline_finalized",
    ),
    add_column(
        "recommendations", "source", "TEXT DEFAULT 'xueqiu'",
        "数据来源（雪球/THS 兜底）",
        "m002_rec_source",
    ),
    add_column(
        "recommendations", "cum_2d", "REAL",
        "T+0 收盘 → T+2 收盘累计涨幅（持有 2 天）",
        "m003_rec_cum_2d",
    ),
    add_column(
        "recommendations", "cum_3d", "REAL",
        "T+0 收盘 → T+3 收盘累计涨幅（持有 3 天）",
        "m004_rec_cum_3d",
    ),
    add_column(
        "recommendations", "concept", "TEXT",
        "推动概念（掉榜/重启后仍能展示「板块」列）",
        "m005_rec_concept",
    ),
    add_column(
        "recommendations", "accumulated_pct", "REAL",
        "5 日累计涨幅（综合排序展示用）",
        "m006_rec_accumulated_pct",
    ),
    add_column(
        "recommendations", "excluded", "INTEGER DEFAULT 0",
        "硬过滤落标（命中卖出/止损级标签置 1，综合排序排除）",
        "m007_rec_excluded",
    ),
    add_column(
        "recommendations", "stale_kline", "INTEGER DEFAULT 0",
        "评分所用 K 线是否缺今日 bar（数据血缘审计）",
        "m008_rec_stale_kline",
    ),
    add_column(
        "recommendations", "excluded_reason", "TEXT",
        "硬过滤原因（消除「无审计依据的误杀」盲点）",
        "m009_rec_excluded_reason",
    ),
    create_index(
        "idx_rec_source", "CREATE INDEX IF NOT EXISTS idx_rec_source ON recommendations(source)",
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
        except sqlite3.Error as exc:
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
