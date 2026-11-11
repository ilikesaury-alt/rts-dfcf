"""候选票公告预取（`scanner/announcement_prefetch.py`）的守卫（2026-11-11，m025）。

三组断言对应任务的三条硬约束：

1. **挂点传对了票**：`symbols_from_view` 取的是终端实际展示的四区（v1 池选 +
   沪深飙升三段），且 `request_prefetch` → `sync_symbols` 拿到的正是这批票。
2. **失败不影响主循环**：网络异常/库锁只降级，`request_prefetch` 不抛。这条必须
   显式断言「同步入口真被调用过」—— 否则「没抛异常」可能只是根本没跑（vacuous）。
3. **节流**：同一批票短时间内不重复外呼；失败**不**计入「已完成」，窗口过后重试
   （「没取到」≠「没有公告」）。

⚠ 全程零联网：`fetcher` 一律注入假数据；DB 一律是共享缓存的 in-memory 库
（`get_conn` 被 patch），**绝不碰真实 scanner.db**。
"""

from __future__ import annotations

import ast
import pathlib
import sqlite3
import threading

import pytest
import requests

import scanner.announcement_prefetch as ann_prefetch
from scanner.models import RecommendationRow
from scanner.view.model import MainRow, ScanView

REPO = pathlib.Path(__file__).resolve().parent.parent

_MEM_URI = "file:ann_prefetch_test?mode=memory&cache=shared"

_M025_DDL = (
    "CREATE TABLE announcement_cache ("
    "symbol TEXT NOT NULL, notice_date TEXT NOT NULL, title TEXT NOT NULL, "
    "ann_type TEXT, source_url TEXT, content_hash TEXT NOT NULL, "
    "fetched_at TEXT NOT NULL, "
    "PRIMARY KEY (symbol, notice_date, content_hash))",
    "CREATE TABLE ann_sync_waterline ("
    "scope TEXT NOT NULL, end_date TEXT NOT NULL, rows INTEGER NOT NULL DEFAULT 0, "
    "updated TEXT NOT NULL, PRIMARY KEY (scope))",
)


#: `get_conn()` 被自开连接调用的次数账（`test_..._calls_sync_entry_...` 断言它非空）。
_OPENED: list[int] = []


@pytest.fixture()
def memdb(monkeypatch):
    """共享缓存 in-memory 库 + 把 `get_conn()` 指向它（**自开短连接**路径照旧被测到）。

    用 `cache=shared` 而非裸 `:memory:`：worker 里的连接 `close()` 之后库不会消失，
    用例才能在跑完后从 keeper 连接上读回写入的行。建表用 `IF NOT EXISTS` + 清空，
    避免共享缓存库在用例间残留（残留会让后续用例 `CREATE TABLE` 直接报错）。
    """
    keeper = sqlite3.connect(_MEM_URI, uri=True)
    for ddl in _M025_DDL:
        keeper.execute(ddl.replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS "))
    keeper.execute("DELETE FROM announcement_cache")
    keeper.execute("DELETE FROM ann_sync_waterline")
    keeper.commit()

    _OPENED.clear()

    def fake_get_conn(db_path=None):  # noqa: ANN001, ARG001
        _OPENED.append(1)
        return sqlite3.connect(_MEM_URI, uri=True)

    import scanner.db.schema as schema_mod

    monkeypatch.setattr(schema_mod, "get_conn", fake_get_conn)
    yield keeper
    keeper.close()


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    """每个用例前后清进程内节流状态（模块级 dict，不清会跨用例串味）。"""
    ann_prefetch._reset_state()
    # 关掉整批节流：默认 300s，用例里连续调用会被它挡住（专门的节流用例自己打开）。
    import scanner.config as cfg

    monkeypatch.setattr(cfg, "ANN_PREFETCH_MIN_INTERVAL_SEC", 0.0)
    monkeypatch.setattr(cfg, "ANN_PREFETCH_ENABLED", True)
    yield
    ann_prefetch._reset_state()


def _sync_worker(monkeypatch):
    """把「起线程」换成同步执行 —— 断言不必与后台线程抢跑（同 test_panel_reporter 手法）。

    返回 `calls`：每次请求实际交给 worker 的票列表（即传给同步入口的那批）。
    """
    calls: list[list[str]] = []

    def fake_start(symbols, fetcher):  # noqa: ANN001
        calls.append(list(symbols))
        ann_prefetch._run(tuple(symbols), fetcher)  # 真 worker 体，同步跑

    monkeypatch.setattr(ann_prefetch, "_start_worker", fake_start)
    return calls


def _fetcher(record: list[list[str]] | None = None, exc: BaseException | None = None):
    """假取数：记录收到的票，返回 1 条公告/票（或按需抛异常）。"""

    def fetch(symbols):
        if record is not None:
            record.append(list(symbols))
        if exc is not None:
            raise exc
        return {s: [{"date": "2026-11-10", "title": f"{s} 回购股份方案", "type": "公司公告"}] for s in symbols}

    return fetch


def _row(symbol: str, category: str = "momentum") -> MainRow:
    entry = RecommendationRow(symbol=symbol, name="测试", category=category, date="2026-11-11")
    return MainRow(
        entry=entry,
        rank=1,
        accum=1.0,
        score=10.0,
        composite_score=5.0,
        core=False,
        cat_label="MOM",
        pct=1.0,
        current=10.0,
        sector="测试",
    )


class _Cand:
    """沪深飙升三段的候选对象（三处都有 `.symbol`，其余字段本模块不读）。"""

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol


def _view(*, main: tuple[str, ...] = (), hot: tuple[str, ...] = (), off: tuple[str, ...] = (),
          onboard: tuple[str, ...] = (), hist: tuple[str, ...] = ()) -> ScanView:
    return ScanView(
        main_rows=[_row(s) for s in main],
        breakout_mark={},
        flow_pct_map={},
        last_ranks={},
        weak=False,
        warnings=[],
        hot_rows=[_Cand(s) for s in hot],
        offboard_rows=[_Cand(s) for s in off],
        onboard_rows=[_Cand(s) for s in onboard],
        hist_rows=[_Cand(s) for s in hist],
    )


# ── 1. 票集：终端实际展示的那批 ──────────────────────────────────────────────


def test_symbols_from_view_covers_four_regions_dedup_and_ordered():
    """v1 池选（entry["symbol"]）+ 沪深飙升三段（.symbol），去重保序。"""
    v = _view(
        main=("SZ300613", "SZ300415"),
        hot=("SZ300415", "SZ301001"),  # 与主表重叠 → 只留一份
        off=("SZ301002",),
        onboard=("SZ301003",),
    )
    assert ann_prefetch.symbols_from_view(v) == [
        "SZ300613",
        "SZ300415",
        "SZ301001",
        "SZ301002",
        "SZ301003",
    ]


def test_symbols_from_view_excludes_hidden_hist_region():
    """v1 回捞（hist_rows）终端已隐藏、面板原文里也没有 ⇒ 不在 AI 会看的那批票里。"""
    v = _view(main=("SZ300613",), hist=("SZ300999",))
    assert "SZ300999" not in ann_prefetch.symbols_from_view(v)


def test_symbols_from_view_tolerates_none_view():
    assert ann_prefetch.symbols_from_view(None) == []


# ── 2. 预取被触发时调用了同步入口，且传入候选票列表 ──────────────────────────


def test_request_prefetch_calls_sync_entry_with_candidate_symbols(monkeypatch, memdb):
    """端到端（同步 worker）：同步入口收到**候选票列表**，且行真的落进 announcement_cache。"""
    calls = _sync_worker(monkeypatch)
    fetched = []
    v = _view(main=("SZ300613", "SZ300415"), off=("SZ301002",))

    assert ann_prefetch.request_prefetch(v, fetcher=_fetcher(fetched)) is True

    assert calls == [["SZ300613", "SZ300415", "SZ301002"]], "worker 收到的必须是候选票列表"
    assert fetched == [["SZ300613", "SZ300415", "SZ301002"]], "同步入口必须真被调用（非空转断言）"
    for sym in ("SZ300613", "SZ300415", "SZ301002"):
        n = memdb.execute("SELECT COUNT(*) FROM announcement_cache WHERE symbol = ?", (sym,)).fetchone()[0]
        assert n == 1, f"{sym} 公告未落库"
    assert _OPENED, "后台线程必须**自开连接**（不得复用主循环的 conn）"


def test_prefetch_symbols_writes_rows_through_sync_symbols(memdb):
    """阻塞入口复用 `announcement_cache.sync_symbols`：行写进缓存，水位推进到 candidates。"""
    fetched: list[list[str]] = []
    out = ann_prefetch.prefetch_symbols(["SZ300613"], fetcher=_fetcher(fetched), conn=memdb)

    assert fetched == [["SZ300613"]]
    assert out["rows"] == 1 and out["requested"] == 1 and out["failed"] is False
    row = memdb.execute(
        "SELECT title FROM announcement_cache WHERE symbol = 'SZ300613'"
    ).fetchone()
    assert row and "回购股份方案" in row[0]
    wl = memdb.execute("SELECT scope FROM ann_sync_waterline").fetchone()
    assert wl and wl[0] == "candidates"


def test_prefetch_symbols_empty_input_is_noop(memdb):
    out = ann_prefetch.prefetch_symbols([], fetcher=_fetcher(), conn=memdb)
    assert out["requested"] == 0 and out["failed"] is False
    assert _OPENED == [], "空票集不该开连接"


# ── 3. 预取失败（网络异常）不影响主循环 ──────────────────────────────────────


def test_network_failure_does_not_raise_and_is_reported(monkeypatch, memdb):
    """拔网线：请求异常 → 不抛、返回 failed，且同步入口**确实被调用过**。"""
    _sync_worker(monkeypatch)
    fetched: list[list[str]] = []
    v = _view(main=("SZ300613",))

    # 不抛异常 = 主循环照跑
    assert ann_prefetch.request_prefetch(v, fetcher=_fetcher(fetched, exc=requests.ConnectionError("down"))) is True
    assert fetched == [["SZ300613"]], "必须真的调用了取数（否则这条用例是空转）"
    assert memdb.execute("SELECT COUNT(*) FROM announcement_cache").fetchone()[0] == 0


def test_prefetch_symbols_swallows_external_failures_only(memdb):
    """只吞 EXTERNAL_FAILURES；编程错误必须冒泡（否则 bug 会静默变成空转）。"""
    out = ann_prefetch.prefetch_symbols(
        ["SZ300613"], fetcher=_fetcher(exc=requests.Timeout("slow")), conn=memdb
    )
    assert out["failed"] is True
    with pytest.raises(AttributeError):
        ann_prefetch.prefetch_symbols(["SZ300613"], fetcher=_fetcher(exc=AttributeError("bug")), conn=memdb)


def test_worker_releases_single_flight_after_failure(monkeypatch, memdb):
    """线程体里抛外部异常也必须释放单飞闸，否则预取被永久关掉且无声。"""

    def boom(*a, **k):  # noqa: ANN002, ANN003, ARG001
        raise requests.ConnectionError("down")

    monkeypatch.setattr(ann_prefetch, "prefetch_symbols", boom)
    ann_prefetch._BUSY = True
    ann_prefetch._run(("SZ300613",), None)
    assert ann_prefetch._BUSY is False


def test_db_error_does_not_break_caller(memdb):
    """库锁/表缺失（sqlite3.Error）也只降级。"""
    conn = sqlite3.connect(":memory:")  # 没有任何表 → sync_symbols 走 rollback 分支
    try:
        out = ann_prefetch.prefetch_symbols(["SZ300613"], fetcher=_fetcher(), conn=conn)
    finally:
        conn.close()
    assert out["failed"] is True


# ── 4. 节流：短时间内不重复外呼 ─────────────────────────────────────────────


def test_throttle_interval_blocks_second_call(monkeypatch, memdb):
    """60s 一轮：整批节流窗口内的第二次请求直接丢弃（零外呼）。"""
    import scanner.config as cfg

    monkeypatch.setattr(cfg, "ANN_PREFETCH_MIN_INTERVAL_SEC", 300.0)
    calls = _sync_worker(monkeypatch)
    fetched: list[list[str]] = []
    v = _view(main=("SZ300613",))

    assert ann_prefetch.request_prefetch(v, fetcher=_fetcher(fetched)) is True
    assert ann_prefetch.request_prefetch(v, fetcher=_fetcher(fetched)) is False

    assert calls == [["SZ300613"]], "节流窗口内不得再起线程"
    assert fetched == [["SZ300613"]], "外呼只能发生一次"


def test_throttle_per_symbol_per_day_blocks_repeat(monkeypatch, memdb):
    """窗口过后仍不重复：同一票同一交易日只成功预取一次。"""
    calls = _sync_worker(monkeypatch)
    fetched: list[list[str]] = []
    v = _view(main=("SZ300613",))

    assert ann_prefetch.request_prefetch(v, fetcher=_fetcher(fetched)) is True
    monkeypatch.setattr(ann_prefetch, "_LAST_ATTEMPT", 0.0)  # 模拟窗口已过（间隔为 0）
    assert ann_prefetch.request_prefetch(v, fetcher=_fetcher(fetched)) is False

    assert calls == [["SZ300613"]]
    assert fetched == [["SZ300613"]]


def test_throttle_only_skips_symbols_already_done(monkeypatch, memdb):
    """新票不被「已预取票」挡住：老票跳过、新票照取。"""
    calls = _sync_worker(monkeypatch)
    fetched: list[list[str]] = []

    assert ann_prefetch.request_prefetch(_view(main=("SZ300613",)), fetcher=_fetcher(fetched)) is True
    monkeypatch.setattr(ann_prefetch, "_LAST_ATTEMPT", 0.0)
    assert ann_prefetch.request_prefetch(
        _view(main=("SZ300613", "SZ300415")), fetcher=_fetcher(fetched)
    ) is True

    assert calls == [["SZ300613"], ["SZ300415"]], "只请求新票"


def test_failure_is_not_marked_done_so_next_window_retries(monkeypatch, memdb):
    """「没取到」≠「没有公告」：失败不记已完成，下个窗口必须重试。"""
    calls = _sync_worker(monkeypatch)
    fetched: list[list[str]] = []
    v = _view(main=("SZ300613",))

    ann_prefetch.request_prefetch(v, fetcher=_fetcher(fetched, exc=requests.ConnectionError("down")))
    assert ann_prefetch._DONE == {}, "失败不得写入已完成集合"
    monkeypatch.setattr(ann_prefetch, "_LAST_ATTEMPT", 0.0)
    assert ann_prefetch.request_prefetch(v, fetcher=_fetcher(fetched)) is True

    assert len(calls) == 2 and fetched == [["SZ300613"], ["SZ300613"]]
    assert ann_prefetch._DONE.get("SZ300613") is not None


def test_done_state_expires_across_trading_days(monkeypatch, memdb):
    """跨日必须失效：昨天的「已完成」不能挡住今天（按票固定冻结正是旧 JSON 的病根）。"""
    monkeypatch.setattr(ann_prefetch, "_DONE", {"SZ300613": "2026-01-01"})
    calls = _sync_worker(monkeypatch)
    assert ann_prefetch.request_prefetch(_view(main=("SZ300613",)), fetcher=_fetcher()) is True
    assert calls == [["SZ300613"]]


def test_single_flight_blocks_concurrent_batch(monkeypatch, memdb):
    """上一批还没跑完（网络慢）时新请求直接丢弃，不叠线程。"""
    ann_prefetch._BUSY = True
    calls = _sync_worker(monkeypatch)
    assert ann_prefetch.request_prefetch(_view(main=("SZ300613",)), fetcher=_fetcher()) is False
    assert calls == []


def test_disabled_switch_is_noop(monkeypatch, memdb):
    """`RTS_ANN_PREFETCH=0` ⇒ 一行外呼都没有（回滚开关与改前行为一致）。"""
    import scanner.config as cfg

    monkeypatch.setattr(cfg, "ANN_PREFETCH_ENABLED", False)
    calls = _sync_worker(monkeypatch)
    assert ann_prefetch.request_prefetch(_view(main=("SZ300613",)), fetcher=_fetcher()) is False
    assert calls == []


def test_request_prefetch_does_not_wait_for_slow_network(monkeypatch, memdb):
    """**核心不变量**：外呼再慢也不能让主循环等它（60s 一轮的单线程串行渲染）。

    真起线程 + 一个「慢 1 秒」的假外呼：`request_prefetch` 必须在几十毫秒内返回，
    此时外呼进度仍为 0；worker 自己跑完才把行落库。
    """
    import time

    release = threading.Event()
    entered = threading.Event()

    def slow_fetch(symbols):
        entered.set()
        release.wait(10)  # 模拟网络慢
        return {s: [{"date": "2026-11-10", "title": "x", "type": ""}] for s in symbols}

    t0 = time.monotonic()
    assert ann_prefetch.request_prefetch(_view(main=("SZ300613",)), fetcher=slow_fetch) is True
    elapsed = time.monotonic() - t0

    assert elapsed < 0.5, f"request_prefetch 阻塞了 {elapsed:.2f}s —— 主循环会被拖慢"
    assert entered.wait(10), "worker 未启动"
    # 主循环此刻应已往下走了：行还没落库（外呼还没返回）
    assert memdb.execute("SELECT COUNT(*) FROM announcement_cache").fetchone()[0] == 0

    release.set()
    for th in threading.enumerate():
        if th.name == "ann-prefetch":
            th.join(10)
    assert memdb.execute("SELECT COUNT(*) FROM announcement_cache").fetchone()[0] == 1, "worker 最终要落库"


# ── 5. 主循环挂点 / 模块纪律 ───────────────────────────────────────────────


def _source(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def _handler_names(handler: ast.ExceptHandler) -> set[str]:
    """`except` 子句捕获的异常名集合（支持 `A` / `(A, B)` / 属性访问）。"""
    t = handler.type
    if t is None:
        return {"<bare>"}
    if isinstance(t, ast.Name):
        return {t.id}
    if isinstance(t, ast.Tuple):
        out: set[str] = set()
        for elt in t.elts:
            if isinstance(elt, ast.Name):
                out.add(elt.id)
            elif isinstance(elt, ast.Attribute):
                out.add(elt.attr)
        return out
    if isinstance(t, ast.Attribute):
        return {t.attr}
    return {"<other>"}


def _innermost_try(src: str, func_name: str) -> ast.Try:
    """找到**最内层**包住 `func_name(...)` 调用的 `try`（外层主循环兜底不算）。

    ⚠ 必须取最内层：主循环整个循环体也被 `try/except Exception` 包着（那是长跑兜底，
    与本挂点的 fail-open 是两件事）。不区分的话，这条守卫会被外层 try 蒙过去。
    """
    tree = ast.parse(src)
    best: tuple[int, ast.Try] | None = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) and sub.func.id == func_name:
                span = (node.end_lineno or 0) - node.lineno
                if best is None or span < best[0]:
                    best = (span, node)
                break
    assert best is not None, f"{func_name}(...) 没有被任何 try 包裹"
    return best[1]


def test_main_loop_calls_prefetch_after_render_and_before_log_results():
    """挂点顺序：渲染/面板存档之后、本轮收尾之前（取到的是终稿 view）。"""
    src = _source("unified_scanner.py")
    i_save = src.index("save_panel_round(")
    i_call = src.index("request_prefetch(view)")
    i_log = src.index("log_results(new_faces")
    assert i_save < i_call < i_log


def test_main_loop_prefetch_call_is_fail_open():
    """挂点必须包在 `try/except EXTERNAL_FAILURES` 里（不得裸 except Exception）。"""
    node = _innermost_try(_source("unified_scanner.py"), "request_prefetch")
    names = set().union(*(_handler_names(h) for h in node.handlers))
    assert "EXTERNAL_FAILURES" in names, f"挂点捕获了非外部异常族：{names}"
    assert "Exception" not in names and "BaseException" not in names, f"挂点不得裸捕获：{names}"


def test_module_has_no_bare_except_exception():
    """fail-open 纪律：只捕 EXTERNAL_FAILURES；裸 except Exception 会把编程错误吞掉。

    ⚠ 按 **AST 的 handler** 判，不按文本正则 —— docstring 里写着这条纪律的模块
    （本项目惯例）会被文本匹配误判成违规。
    """
    tree = ast.parse(_source("scanner/announcement_prefetch.py"))
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            names = _handler_names(node)
            if names & {"Exception", "BaseException", "<bare>"}:
                offenders.append((node.lineno, sorted(names)))
    assert not offenders, f"禁止裸 except Exception：{offenders}"


def test_module_does_not_create_tables_or_write_sql_of_its_own():
    """不新建表、不另写取数：落库只能经由 announcement_cache.sync_symbols。"""
    import re

    src = _source("scanner/announcement_prefetch.py")
    assert not re.search(r"CREATE\s+TABLE|INSERT\s+INTO|REPLACE\s+INTO", src, re.I)
    assert "sync_symbols" in src


def test_prefetch_is_threaded_by_default(monkeypatch):
    """生产路径必须真的起 daemon 线程（不是同步跑）—— 这是「不拖慢 60s 主循环」的落点。"""
    started: list[threading.Thread] = []
    real_start = threading.Thread.start

    def fake_start(self):  # noqa: ANN001
        started.append(self)  # 不真起线程：只验「想过要起」

    monkeypatch.setattr(threading.Thread, "start", fake_start)
    try:
        v = _view(main=("SZ300613",))
        assert ann_prefetch.request_prefetch(v, fetcher=_fetcher()) is True
    finally:
        threading.Thread.start = real_start
        ann_prefetch._reset_state()
    assert started and all(t.daemon for t in started), "预取线程必须是 daemon"
    assert started[0].name == "ann-prefetch"
