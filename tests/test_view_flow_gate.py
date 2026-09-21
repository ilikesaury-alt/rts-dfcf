"""展示层「资金流出」硬门（2026-09-14 统一口径）单测。

被测语义（三句话）：
  1. 判定单源 = `ranking.is_fund_outflow`，阈值 = `config_sources.FUND_OUTFLOW_NET_PCT`(-8.0)，
     回退链 = 行内 dims/score_breakdown → `market_extra_cache` 当日全市场快照，缺失 fail-open。
  2. 过滤发生在 `view.build_scan_view` **一处**，压在 `today_recs` 上，故所有下游集合
     （v1 池选 / v1 回捞 / 沪深飙升 A·B 段）自动继承（终端与飞书共用同一份 ScanView）。
  3. 过滤**只影响展示**：`recommendations.excluded` 必须保持 0，回测/归因样本口径不受污染。

2026-09-14 更新：v2 池选与核心方向低吸的展示区已隐藏，故原先针对
`view.pool_rows` / `view.core_dip_rows` 的两条断言改为断言「过滤计数不受展示区减少影响」
与 `comeback_rows` 口径 —— 门本身仍在 `today_recs` 层生效，只是少了两个可见出口。

2026-09-16 更新：回马枪展示区亦随桶删除（`comeback_rows` 字段已移除），断言改为只看
v1 主表与计数 —— 门依旧压在 `today_recs` 单点，历史 orphan 类别行（如残留的
`category='comeback'`）照样被同一条门剔掉。
"""

import sqlite3

from scanner.config import FUND_OUTFLOW_NET_PCT, now_beijing
from scanner.ranking import entry_fund_flow_pct, is_fund_outflow
from scanner.view.assemble import build_scan_view

# ── 判定语义（纯函数）──


def test_threshold_is_single_source_minus_8():
    """阈值单源 -8.0：hot_watch 派生展示门与主门同值。

    2026-09-16：原「回马枪扫描期前置门 -5.0」随 comeback.py 删除而消失
    （`COMEBACK_REENTRY_FUND_FLOW_LOW` 已从 config 移除），子集关系不复存在。
    """
    from scanner.config import HOT_FUND_FLOW_FILTER_THRESHOLD

    assert FUND_OUTFLOW_NET_PCT == -8.0
    assert HOT_FUND_FLOW_FILTER_THRESHOLD == FUND_OUTFLOW_NET_PCT


def test_is_fund_outflow_boundaries():
    assert is_fund_outflow({"symbol": "SZ1"}, {"SZ1": -8.0}) is True  # 闭区间（≤）
    assert is_fund_outflow({"symbol": "SZ1"}, {"SZ1": -7.99}) is False
    assert is_fund_outflow({"symbol": "SZ1"}, {"SZ1": -20.0}) is True
    assert is_fund_outflow({"symbol": "SZ1"}, {"SZ1": 6.0}) is False


def test_is_fund_outflow_fail_open_without_data():
    """无数据 ≠ 流出：资金流接口故障时不得清空整屏推荐。"""
    assert is_fund_outflow({"symbol": "SZ1"}, {}) is False
    assert is_fund_outflow({"symbol": "SZ1"}, None) is False


def test_entry_fund_flow_pct_fallback_chain():
    """行内 dims 优先于 market_extra_cache 快照（展示层资金流单一回退链）。"""
    entry = {"symbol": "SZ1", "score_breakdown": {"fund_flow_main_pct": -1.0}}
    assert entry_fund_flow_pct(entry, {"SZ1": -9.0}) == -1.0
    assert entry_fund_flow_pct({"symbol": "SZ1"}, {"SZ1": -9.0}) == -9.0
    assert entry_fund_flow_pct({"symbol": "SZ1"}, {}) is None
    # dims 优先 → 快照里的流出值不得反超行内真值
    assert is_fund_outflow(entry, {"SZ1": -9.0}) is False


# ── 展示层：单一入口过滤各区域 ──


def _rec_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE appearances (
        id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, name TEXT NOT NULL,
        date TEXT NOT NULL, rank INTEGER, percent REAL, value REAL, UNIQUE(symbol, date))""")
    conn.execute("""CREATE TABLE recommendations (
        id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL, time TEXT NOT NULL,
        symbol TEXT NOT NULL, name TEXT NOT NULL, category TEXT NOT NULL, score INTEGER NOT NULL,
        percent REAL, trend TEXT, next_day_pct REAL, fwd_3d REAL, fwd_5d REAL,
        score_breakdown TEXT, source TEXT DEFAULT 'xueqiu', concept TEXT, accumulated_pct REAL,
        excluded INTEGER DEFAULT 0)""")
    conn.execute("""CREATE TABLE market_extra_cache (
        symbol TEXT NOT NULL, date TEXT NOT NULL, data_type TEXT NOT NULL,
        payload_json TEXT NOT NULL, updated TEXT NOT NULL, PRIMARY KEY(symbol, data_type))""")
    conn.execute("""CREATE TABLE market_index_log (
        date TEXT PRIMARY KEY, time TEXT, index_pct REAL, bar_date TEXT,
        source TEXT, updated TEXT DEFAULT '')""")
    return conn


def _seed_five(conn: sqlite3.Connection, today: str) -> None:
    """五只票覆盖三条判定路径 + 一个 fail-open 反例。"""
    rows = [
        # (symbol, name, category, score, score_breakdown, 快照 flow)
        ("SZ300001", "行内流出", "rebound", 60, '{"fund_flow_main_pct": -12.0}', None),
        ("SZ300002", "正常票", "rebound", 55, '{"fund_flow_main_pct": -1.0}', None),
        ("SZ300003", "快照流出", "pool_pick", 50, None, -9.0),
        ("SZ300004", "无数据", "core_dip", 45, None, None),
        # 历史 orphan 类别行（回马枪桶已删除，但库里存量行仍会被门扫到）
        ("SZ300005", "orphan 流出", "comeback", 40, None, -20.0),
    ]
    for sym, name, cat, score, sb, snapshot in rows:
        conn.execute(
            "INSERT INTO recommendations (date, time, symbol, name, category, score, percent, score_breakdown)"
            " VALUES (?, '13:00', ?, ?, ?, ?, 2.0, ?)",
            (today, sym, name, cat, score, sb),
        )
        if snapshot is not None:
            conn.execute(
                "INSERT INTO market_extra_cache (symbol, date, data_type, payload_json, updated)"
                " VALUES (?, ?, 'fund_flow', ?, ?)",
                (sym, today, f'{{"main_pct": {snapshot}}}', now_beijing().isoformat()),
            )
    conn.commit()


def _sym_set(rows) -> set:
    """main_rows 是 MainRow（.entry），其余展示区（hot/hist）是裸对象。"""
    return {r.entry["symbol"] if hasattr(r, "entry") else r["symbol"] for r in rows}


def test_build_scan_view_filters_every_region():
    """过滤在 today_recs 单一入口生效，所有展示区一致继承（不是只剔某一处）。

    2026-09-14：v2 池选与核心低吸展示区隐藏后，可见出口只剩 v1 主表与回马枪；
    2026-09-16：回马枪展示区亦随桶删除，可见出口只剩 v1 主表。
    三条判定路径（行内流出 / 快照流出 / 历史 orphan 类别流出）仍各剔一只，
    计数不变——这正是「展示区减少不应影响门本身」的守护。
    """
    conn = _rec_db()
    today = now_beijing().date().isoformat()
    _seed_five(conn, today)

    view = build_scan_view(conn)

    assert view is not None
    assert view.flow_filtered == 3, "行内流出 + 快照流出 + orphan 类别流出 应各剔一只"
    assert _sym_set(view.main_rows) == {"SZ300002"}


def test_build_scan_view_does_not_touch_excluded_column():
    """展示层过滤不得写库：excluded 保持 0（否则回测/归因样本口径被污染）。"""
    conn = _rec_db()
    today = now_beijing().date().isoformat()
    _seed_five(conn, today)

    build_scan_view(conn)

    leaked = conn.execute("SELECT symbol FROM recommendations WHERE COALESCE(excluded, 0) <> 0").fetchall()
    assert leaked == [], f"展示层不得改 excluded，实际: {leaked}"


def test_flow_gate_disabled_by_switch(monkeypatch):
    """RTS_FUND_FLOW_HARD_FILTER=0 → 关闸。注意项目是快照式导入，
    必须 patch 消费方模块（scanner.view.assemble）而不是 scanner.config。"""
    import scanner.view.assemble as asm

    conn = _rec_db()
    today = now_beijing().date().isoformat()
    _seed_five(conn, today)

    monkeypatch.setattr(asm, "FUND_FLOW_HARD_FILTER_ENABLED", False)
    view = asm.build_scan_view(conn)

    assert view.flow_filtered == 0
    assert _sym_set(view.main_rows) == {"SZ300001", "SZ300002"}


# ── 决策层资金流门（2026-09-14 随决策层删除）──
# 原先这里还有 `test_decision_flow_gate_blocks_pick`：决策层直连 DB 取数、不经
# ScanView，故必须独立施加一次资金流出门，否则会出现「终端主表已剔掉、决策推荐里
# 还在」。决策层删除后该函数（build_decision_picks）已不存在，测试一并移除。
# ⚠ 若日后重建决策层，必须恢复这条断言：头名流出必须被剔（漏剔 = 用户照单下单）。
