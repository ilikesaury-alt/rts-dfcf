"""桌面版后端单测（Issue A：`local_view_cache` / `local_panel_server` / m021 / 开关）。

测试主题三件事：
1. **安全硬约束**：只绑 `127.0.0.1`（误绑 `0.0.0.0` = 同网段可读数据）。
2. **契约不漂移**：`/api/regions` 回放的就是 `serialize_view` 的同一份输出（去 stocks），
   `regions.cols` 的 key 序列 == `COLS_*` 的列名序列（验收 1/2）。
3. **fail-open**：首轮完成前 `meta` 200/`ready:false`、`regions` 404 `NO_SNAPSHOT`、
   端口冲突只上抛不打崩；关掉开关时行为与改前一致（验收 8）。

⚠ 每个用例都**显式断言调用发生**（AGENTS.md：禁空断言）。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import urllib.error
import urllib.request
from datetime import timedelta

import pytest

from scanner import local_panel_server as lps
from scanner import local_view_cache as lvc
from scanner.config import LOCAL_PANEL_PORT, now_beijing
from scanner.view.model import COLS_HIST, COLS_HOT, COLS_POOL, MainRow, ScanView

# ── 工具：真起一个服务端，用 urllib 打真 HTTP（不是直接调 handler）────────────


@pytest.fixture
def server():
    lvc.clear_cache()
    lps.stop_server()
    srv = lps.start_server(0, static_dir=None)  # port=0 → 内核分配空闲端口
    yield srv
    lps.stop_server()
    lvc.clear_cache()


def _get(port: int, path: str) -> tuple[int, dict]:
    url = f"http://127.0.0.1:{port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=5) as r:  # noqa: S310 - 测试内固定回环地址
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def _view(symbols: list[str] | None = None) -> ScanView:
    rows = []
    for i, sym in enumerate(symbols or ["SZ300319", "SZ300001", "SZ300002"]):
        rows.append(
            MainRow(
                entry={
                    "symbol": sym,
                    "name": f"票{i}",
                    "category": "momentum",
                    "score": 80,
                    "date": "2026-10-19",
                    "time": "10:00:00",
                    "percent": 5.0,
                    "concept": "",
                    "first_time": "10:00:00",
                },
                rank=i + 1,
                accum=1.0,
                score=80.0,
                composite_score=5.0,
                core=False,
                cat_label="MOM",
                pct=5.0,
                current=10.0,
                sector="",
                is_new_entry=i == 0,
            )
        )
    return ScanView(
        main_rows=rows,
        breakout_mark={},
        flow_pct_map={},
        last_ranks={},
        weak=False,
        warnings=[],
    )


def _payload(view: ScanView | None = None, seq: int = 1, stocks: dict | None = None, time: str = "10:31:00") -> dict:
    from scanner.panel_serialize import serialize_view

    return serialize_view(
        view or _view(),
        extra={
            "seq": seq,
            "date": "2026-10-19",
            "time": time,
            "durationMs": 123,
            "stocks": stocks or {},
        },
    )


# ── 1. 安全硬约束 ─────────────────────────────────────────────────────────


def test_bind_host_is_loopback(server):
    """⛔ 只绑回环：误绑 0.0.0.0 即同网段可读（design §6.4 高风险点 1）。"""
    assert lps.BIND_HOST == "127.0.0.1"
    assert server.server_address[0] == "127.0.0.1"
    status, body = _get(server.port, "/api/health")
    assert status == 200 and body == {"ok": True, "data": {"alive": True}}


def test_start_server_is_idempotent(server):
    """重复启动返回**同一个**服务端（主循环只该起一次，重启调用不得再占一个端口）。"""
    again = lps.start_server(0, static_dir=None)
    assert again is server
    assert again.server_address[0] == "127.0.0.1"


def test_handle_error_swallows_connection_reset_without_traceback(server, capsys):
    """连接级异常**不得**把 traceback 打到终端（扫描器 stdout 是主输出面）。

    实测背景（2026-10-19）：`protocol_version="HTTP/1.1"` 下浏览器每关一次 keep-alive
    连接就触发一次 `ConnectionResetError`，而 `socketserver` 默认
    `traceback.print_exc()` 直写 stderr ⇒ 用户每点一下面板，终端就被 25 行 traceback
    冲刷一次（面板本身还在每轮重绘，两者打架）。
    """
    assert lps._log.propagate is False, "start_server 必须把 logger 从 stderr 摘掉"
    try:
        raise ConnectionResetError(10054, "远程主机强迫关闭了一个现有的连接")
    except ConnectionResetError:
        server.handle_error(None, ("127.0.0.1", 50000))  # 不得上抛
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == ""


def test_handle_error_logs_unexpected_error_to_log_not_terminal(server, capsys):
    """非连接级异常：**留证据**（落 logger）但同样不进终端 —— 两者缺一都是坏修法。"""
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    lps._log.addHandler(handler)
    previous = lps._log.level
    lps._log.setLevel(logging.INFO)
    try:
        try:
            raise ValueError("boom")
        except ValueError:
            server.handle_error(None, ("127.0.0.1", 50001))
    finally:
        lps._log.removeHandler(handler)
        lps._log.setLevel(previous)

    assert [r.levelname for r in records] == ["WARNING"], "非连接级异常必须留证据"
    assert "boom" in records[0].getMessage()
    # traceback 随记录走（落盘时可见），而终端仍然干净 —— 两者都要成立。
    assert records[0].exc_info is not None and "boom" in str(records[0].exc_info[1])
    assert capsys.readouterr().err == "", "留证据不等于可以刷终端"


def test_static_route_404s_when_dist_missing(server):
    """未构建前端时静态路由 404，但 /api 照常工作（不致命）。"""
    status, body = _get(server.port, "/")
    assert status == 404 and body["ok"] is False and body["error"]["code"] == "NOT_FOUND"
    assert _get(server.port, "/api/health")[0] == 200


def test_unknown_api_route_is_not_found(server):
    status, body = _get(server.port, "/api/nope")
    assert status == 404 and body["error"]["code"] == "NOT_FOUND"


# ── 2. 首轮完成前的 fail-open 行为 ────────────────────────────────────────


def test_meta_not_ready_before_first_round(server):
    """无快照时 meta **200 + ready:false**（不是 404）—— 面板显示「等待首轮上报」。"""
    status, body = _get(server.port, "/api/meta")
    assert status == 200
    assert body == {"ok": True, "data": {"ready": False, "date": None, "time": None, "seq": None}}


def test_regions_before_first_round_is_no_snapshot(server):
    """无快照时 regions 404 `NO_SNAPSHOT`（与云端同语义）。"""
    status, body = _get(server.port, "/api/regions")
    assert status == 404
    assert body["ok"] is False and body["error"]["code"] == "NO_SNAPSHOT"


# ── 3. 契约：回放的 == serialize_view 的那一份（去 stocks）────────────────


def test_regions_replays_same_payload_as_serialize_view(server):
    """验收 1：`regions.main` 行数/首行 symbol/顺序 与序列化结果**逐条一致**。"""
    payload = _payload()
    lvc.cache_latest(payload)

    status, body = _get(server.port, "/api/regions")
    assert status == 200
    data = body["data"]

    assert data["date"] == payload["date"]
    assert data["time"] == payload["time"]
    assert data["seq"] == payload["seq"]
    assert data["regions"]["main"] == payload["regions"]["main"]
    assert data["ctx"] == payload["ctx"]
    # 非空断言：否则「两边都是空列表」会让上面成立而毫无证明力
    assert len(data["regions"]["main"]) == 3
    assert data["regions"]["main"][0]["symbol"] == "SZ300319"


def test_regions_response_has_no_stocks_key(server):
    """D4：列表页**不碰 K 线** —— 顶层 stocks 必须被显式丢弃（高风险点，§五）。"""
    lvc.cache_latest(_payload(stocks={"SZ300319": {"klineDate": "2026-10-19", "kline": [], "appearances": []}}))
    status, body = _get(server.port, "/api/regions")
    assert status == 200
    lowered = {k.lower() for k in body["data"]}
    assert "stocks" not in lowered, f"regions 响应不得含 stocks：{sorted(body['data'])}"
    assert "stocks" not in {k.lower() for k in body["data"]["regions"]}


def test_regions_cols_keys_match_col_specs(server):
    """验收 1 后半：`regions.cols` 的 key 序列 == `COLS_*` 的列名序列。"""
    lvc.cache_latest(_payload())
    _, body = _get(server.port, "/api/regions")
    cols = body["data"]["regions"]["cols"]
    for name, spec in (("pool", COLS_POOL), ("hot", COLS_HOT), ("hist", COLS_HIST)):
        assert [c["key"] for c in cols[name]] == [c[0] for c in spec], f"{name} 列序漂移"


def test_meta_reports_cached_round(server):
    lvc.cache_latest(_payload(seq=7))
    status, body = _get(server.port, "/api/meta")
    assert status == 200
    assert body["data"] == {"ready": True, "date": "2026-10-19", "time": "10:31:00", "seq": 7}


# ── 4. 详情页端点（db 现算）──────────────────────────────────────────────


def _db(tmp_path) -> str:
    """建一个最小 `scanner.db`（只含详情页端点读的三张表）；同路径幂等。"""
    path = str(tmp_path / "panel_test.db")
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS daily_kline (symbol TEXT, date TEXT, open REAL, close REAL, high REAL, "
        "low REAL, volume REAL, percent REAL, finalized INTEGER, PRIMARY KEY(symbol,date))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS recommendations (date TEXT, time TEXT, symbol TEXT, name TEXT, category TEXT, "
        "score INTEGER, percent REAL, score_breakdown TEXT)"
    )
    today = now_beijing().date()
    for i in range(3):
        d = (today - timedelta(days=2 - i)).isoformat()
        conn.execute(
            "INSERT OR REPLACE INTO daily_kline VALUES(?,?,10.0,11.0,12.0,9.5,1000.0,1.0,1)",
            ("SZ300319", d),
        )
    conn.execute(
        "INSERT INTO recommendations VALUES(?,?,?,?,?,?,?,?)",
        (today.isoformat(), "10:00:00", "SZ300319", "票0", "momentum", 80, 5.0, json.dumps({"base": 40, "flow": 10})),
    )
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def server_with_db(tmp_path, monkeypatch):
    """把端点里的 `_connect()` 换成临时库连接（真实 SQL 路径不变）。

    ⚠ 必须**每次新建连接**：SQLite 连接默认不可跨线程复用，而端点跑在 HTTP 线程里。
    """
    path = _db(tmp_path)
    opened: list[sqlite3.Connection] = []

    def _connect():
        c = sqlite3.connect(path)
        opened.append(c)
        return c

    monkeypatch.setattr(lps, "_connect", _connect)
    lvc.clear_cache()
    lps.stop_server()
    srv = lps.start_server(0, static_dir=None)
    yield srv, path, opened
    lps.stop_server()
    lvc.clear_cache()
    # 连接由端点自己在**同一线程**内关闭（SQLite 连接不可跨线程 close）


def test_stock_kline_shape_and_breakdown(server_with_db):
    """验收 5：K 线形状 `[date,open,close,low,high,volume]` + 评分分解来自落库行。"""
    srv, _, opened = server_with_db
    status, body = _get(srv.port, "/api/stock/SZ300319/kline")
    assert status == 200
    assert len(opened) == 1, "详情页端点必须真的连过库（防空断言）"
    data = body["data"]
    assert data["symbol"] == "SZ300319"
    assert data["klineDate"] == now_beijing().date().isoformat()
    assert len(data["kline"]) == 3
    first = data["kline"][0]
    assert len(first) == 6 and first[0] == (now_beijing().date() - timedelta(days=2)).isoformat()
    # close 在 low/high 之前（ECharts 约定，types.ts Stock.kline 注释）
    assert first[1:] == [10.0, 11.0, 9.5, 12.0, 1000.0]
    assert data["scoreBreakdown"] == {"base": 40, "flow": 10}
    assert isinstance(data["appearances"], list)


def test_stock_without_kline_is_not_in_report(server_with_db):
    """`daily_kline` 缺票 → 404 `NOT_IN_REPORT`（详情页显「本轮上报未包含该票」）。"""
    srv, _, opened = server_with_db
    status, body = _get(srv.port, "/api/stock/SZ399999/kline")
    assert status == 404
    assert body["error"]["code"] == "NOT_IN_REPORT"
    assert opened, "查库是必经路径：404 也必须在查过库之后才给"


def test_stock_without_breakdown_returns_null(server_with_db):
    """非 v1 票（无评分分解）→ `scoreBreakdown: null`，而不是 {} 或报错。"""
    srv, path, opened = server_with_db
    today = now_beijing().date().isoformat()
    conn = sqlite3.connect(path)
    conn.execute("INSERT OR REPLACE INTO daily_kline VALUES(?,?,1,1,1,1,1,1,1)", ("SZ300777", today))
    conn.commit()
    conn.close()
    status, body = _get(srv.port, "/api/stock/SZ300777/kline")
    assert status == 200
    assert body["data"]["scoreBreakdown"] is None
    assert body["data"]["klineDate"] == today
    assert len(body["data"]["kline"]) == 1
    assert opened


def test_bad_symbol_is_rejected(server_with_db):
    """非法 symbol 在**碰库之前**就被挡掉（唯一一道输入校验）。"""
    srv, _, opened = server_with_db
    status, body = _get(srv.port, "/api/stock/..%2Fetc%2Fpasswd/kline")
    assert status == 400
    assert body["error"]["code"] == "BAD_REQUEST"
    assert opened == [], "非法 symbol 不得触达 DB"


# ── 5. 视图缓存与 m021 落盘 ───────────────────────────────────────────────


def test_cache_latest_roundtrip_and_replace():
    lvc.clear_cache()
    assert lvc.get_latest() is None, "首轮完成前必须是 None（调用方据此判 ready）"
    first, second = _payload(seq=1), _payload(seq=2, view=_view(["SZ300001"]))
    lvc.cache_latest(first)
    assert lvc.get_latest() is first
    lvc.cache_latest(second)
    assert lvc.get_latest() is second, "每轮按引用整体替换（不是就地改写）"
    assert lvc.get_latest()["seq"] == 2
    lvc.clear_cache()
    assert lvc.get_latest() is None


def test_persist_and_load_latest_roundtrip(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "v.db"))
    conn.execute(
        "CREATE TABLE view_snapshot (date TEXT NOT NULL, time TEXT NOT NULL, seq INTEGER NOT NULL DEFAULT 0, "
        "payload TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY (date, time))"
    )
    lvc.persist_latest(conn, _payload(seq=1, time="10:00:00"))
    lvc.persist_latest(conn, _payload(seq=2, time="10:01:00"))
    restored = lvc.load_latest(conn)
    assert restored is not None and restored["seq"] == 2, "取最新一轮：按 (date,time)，不是 seq"
    conn.close()


def test_persist_prunes_to_recent_rounds(tmp_path):
    """表只留最近 N 轮（防长跑无限增长）；N 行以内时不得误删。"""
    conn = sqlite3.connect(str(tmp_path / "v2.db"))
    conn.execute(
        "CREATE TABLE view_snapshot (date TEXT NOT NULL, time TEXT NOT NULL, seq INTEGER NOT NULL DEFAULT 0, "
        "payload TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY (date, time))"
    )
    for i in range(lvc._SNAPSHOT_LIMIT):
        lvc.persist_latest(conn, _payload(seq=i, time=f"10:{i:02d}:00"))
    assert conn.execute("SELECT COUNT(*) FROM view_snapshot").fetchone()[0] == lvc._SNAPSHOT_LIMIT
    # 再多 7 轮（时间递增 ⇒ 最早 7 轮被剪掉）
    for i in range(lvc._SNAPSHOT_LIMIT + 7):
        if i < 20:
            continue
        lvc.persist_latest(conn, _payload(seq=i, time=f"11:{i:02d}:00"))
    n = conn.execute("SELECT COUNT(*) FROM view_snapshot").fetchone()[0]
    assert n == lvc._SNAPSHOT_LIMIT
    newest = lvc.load_latest(conn)
    assert newest["seq"] == lvc._SNAPSHOT_LIMIT + 6, "保留下来的必须是最新那批"
    conn.close()


def test_persist_on_missing_table_is_fail_open(tmp_path):
    """缺表（迁移未跑）只告警，不打断扫描（`persist_latest` 不上抛）。"""
    conn = sqlite3.connect(str(tmp_path / "empty.db"))
    lvc.persist_latest(conn, _payload())  # 不抛即通过
    assert lvc.load_latest(conn) is None
    conn.close()


def test_m021_view_snapshot_is_registered_and_applies(tmp_path):
    """m021 在迁移表**末尾**（末尾追加纪律），且在新库上建出该表。"""
    from scanner.db.migrations import MIGRATIONS, run_migrations

    assert MIGRATIONS[-1].id == "m021_view_snapshot"
    conn = sqlite3.connect(str(tmp_path / "m.db"))
    conn.execute("CREATE TABLE daily_kline (symbol TEXT PRIMARY KEY, finalized INTEGER DEFAULT 1)")
    conn.execute("CREATE TABLE recommendations (symbol TEXT PRIMARY KEY)")
    applied = run_migrations(conn, verbose=False)
    assert "m021_view_snapshot" in applied
    cols = {r[1] for r in conn.execute("PRAGMA table_info(view_snapshot)").fetchall()}
    assert cols == {"date", "time", "seq", "payload", "updated_at"}
    conn.close()


# ── 6. 开关口径（验收 8：缺省关闭 == 行为与改前一致）──────────────────────


def _clear_local_env(monkeypatch):
    monkeypatch.delenv("RTS_LOCAL_PANEL", raising=False)
    monkeypatch.delenv("RTS_LOCAL_PANEL_PORT", raising=False)


def test_local_panel_disabled_by_default(monkeypatch):
    from unified_scanner import _local_panel_enabled

    _clear_local_env(monkeypatch)
    assert _local_panel_enabled() == (False, LOCAL_PANEL_PORT)


@pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
def test_local_panel_falsey_values_stay_off(monkeypatch, value):
    from unified_scanner import _local_panel_enabled

    _clear_local_env(monkeypatch)
    monkeypatch.setenv("RTS_LOCAL_PANEL", value)
    assert _local_panel_enabled()[0] is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE"])
def test_local_panel_truthy_values_enable(monkeypatch, value):
    from unified_scanner import _local_panel_enabled

    _clear_local_env(monkeypatch)
    monkeypatch.setenv("RTS_LOCAL_PANEL", value)
    assert _local_panel_enabled() == (True, LOCAL_PANEL_PORT)


def test_local_panel_port_override_and_fallback(monkeypatch):
    from unified_scanner import _local_panel_enabled

    _clear_local_env(monkeypatch)
    monkeypatch.setenv("RTS_LOCAL_PANEL", "1")
    monkeypatch.setenv("RTS_LOCAL_PANEL_PORT", "9123")
    assert _local_panel_enabled() == (True, 9123)
    for bad in ("abc", "0", "70000", ""):
        monkeypatch.setenv("RTS_LOCAL_PANEL_PORT", bad)
        assert _local_panel_enabled() == (True, LOCAL_PANEL_PORT), f"脏值应回退默认：{bad!r}"


def test_local_switch_is_independent_of_cloud_switch(monkeypatch):
    """design D3：`RTS_PANEL`（云端）不得影响本地看板开关（两个独立开关）。"""
    from unified_scanner import _local_panel_enabled, _panel_enabled

    _clear_local_env(monkeypatch)
    monkeypatch.setenv("RTS_PANEL", "0")
    monkeypatch.setenv("RTS_PANEL_URL", "https://panel.example/api/ingest")
    monkeypatch.setenv("RTS_PANEL_SECRET", "s")
    assert _panel_enabled(False)[0] is False
    monkeypatch.setenv("RTS_LOCAL_PANEL", "1")
    assert _local_panel_enabled()[0] is True, "云端开关关闭不得连带关掉本地看板"


def test_local_panel_module_not_imported_when_disabled(monkeypatch):
    """验收 8 硬要求：未开启时 `scanner.local_panel_server` **不在 sys.modules**。

    做法与 `test_panel_reporter.py` 的同名守卫一致：在**本进程内**做一次全新导入，
    断言挂点用的是**局部导入**（模块顶层没有 `from scanner.local_panel_server import ...`）。
    """
    import importlib
    import sys as _sys

    _clear_local_env(monkeypatch)
    for name in ("scanner.local_panel_server", "scanner.local_view_cache", "unified_scanner"):
        _sys.modules.pop(name, None)

    us = importlib.import_module("unified_scanner")

    assert us._local_panel_enabled()[0] is False
    assert "scanner.local_panel_server" not in _sys.modules, "关闭时服务端模块不得被导入"
    assert "scanner.local_view_cache" not in _sys.modules, "关闭时缓存模块不得被导入"
    assert not hasattr(us, "start_server"), "挂点必须局部导入，不得绑到主模块命名空间"
