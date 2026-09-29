"""`scanner/panel_reporter.py` 单测（T0.4 · design §10 第 5~6 行 · G3 / 验收 1·3）。

本模块的测试主题只有一件事：**失败隔离**。网络挂掉、密钥错、序列化炸、DB 读炸，
`report()` 都必须正常返回，主循环毫发无损。任何「不抛」类断言都**必须显式 `assert`
调用发生**（AGENTS.md：禁空断言）。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time

import pytest

from scanner import panel_reporter
from scanner.panel_reporter import _BACKOFF, _TIMEOUT, collect_stocks, report, reset_sent, sent_snapshot
from scanner.view.model import MainRow, ScanView

# ⚠ 在**模块导入时**抓住真正的 time.sleep：autouse 的 _clean fixture 会
# monkeypatch `time.sleep`（那是全局同一个 time 模块对象），等到测试函数体内
# 再抓就只会抓到那个空转桩 —— 表现为「计时全是 0.0」。
_REAL_SLEEP = time.sleep


def _view(symbols: list[str] | None = None) -> ScanView:
    rows = []
    for i, sym in enumerate(symbols or ["SZ300319"]):
        rows.append(
            MainRow(
                entry={
                    "symbol": sym,
                    "name": f"票{i}",
                    "category": "momentum",
                    "score": 80,
                    "date": "2026-09-29",
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


_EXTRA = {"seq": 1, "date": "2026-09-29", "time": "10:31:00", "durationMs": 100}


class _Resp:
    def __init__(self, status: int, text: str = ""):
        self.status_code = status
        self.text = text


class _Recorder:
    """记录 post 调用次数与参数，并可选择抛异常。"""

    def __init__(self, statuses=None, exc=None):
        self.statuses = list(statuses) if statuses else [201]
        self.exc = exc
        self.calls = []
        self.bodies = []

    def __call__(self, url, data=None, timeout=None, headers=None):
        self.calls.append({"url": url, "timeout": timeout, "headers": headers})
        self.bodies.append(json.loads(data.decode()))
        if self.exc is not None:
            raise self.exc
        return _Resp(self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0])


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    reset_sent()
    monkeypatch.setattr(panel_reporter.time, "sleep", lambda s: None)
    yield
    reset_sent()


def _run_sync(view, **kw):
    """跑 report() 并**等待**其 daemon 线程结束（否则断言会抢跑）。"""
    done = threading.Event()
    real_start = threading.Thread.start

    def start(self, *a, **k):  # noqa: ANN001
        target, args, kwargs = self._target, self._args, self._kwargs

        def run():
            try:
                target(*args, **kwargs)
            finally:
                done.set()

        self._target, self._args, self._kwargs = run, (), {}
        return real_start(self, *a, **k)

    orig = threading.Thread.start
    threading.Thread.start = start
    try:
        report(view, url="https://panel.example/api/ingest", secret="s3cr3t", **_EXTRA, **kw)
        assert done.wait(10), "上报线程未在 10s 内结束"
    finally:
        threading.Thread.start = orig


# ── 失败隔离（G3 / 验收 1·3）──────────────────────────────────────────────


def test_post_raising_does_not_propagate(monkeypatch):
    """拔网线：post 抛 requests 异常 → report() 正常返回（显式断言调用发生 3 次）。"""
    rec = _Recorder(exc=RuntimeError("network down"))
    monkeypatch.setattr(panel_reporter._sess, "post", rec)
    _run_sync(_view())
    assert len(rec.calls) == 3, "首次 + 2 次重试 = 3 次"


def test_retries_use_backoff_1_then_3(monkeypatch):
    """退避序列 = (1, 3)，且 sleep 真被调用（不是只写了个常量）。"""
    slept: list[float] = []
    monkeypatch.setattr(panel_reporter.time, "sleep", lambda s: slept.append(s))
    rec = _Recorder(exc=RuntimeError("down"))
    monkeypatch.setattr(panel_reporter._sess, "post", rec)
    _run_sync(_view())
    assert len(rec.calls) == 3
    assert slept == list(_BACKOFF), f"退避序列应为 {list(_BACKOFF)}，实得 {slept}"


def test_401_is_not_retried(monkeypatch):
    """错密钥：401 只打一次 —— 4xx 重试无意义（design §3.1）。"""
    rec = _Recorder(statuses=[401])
    monkeypatch.setattr(panel_reporter._sess, "post", rec)
    _run_sync(_view())
    assert len(rec.calls) == 1


def test_400_is_not_retried(monkeypatch):
    rec = _Recorder(statuses=[400])
    monkeypatch.setattr(panel_reporter._sess, "post", rec)
    _run_sync(_view())
    assert len(rec.calls) == 1


def test_5xx_is_retried_then_gives_up(monkeypatch):
    rec = _Recorder(statuses=[500])
    monkeypatch.setattr(panel_reporter._sess, "post", rec)
    _run_sync(_view())
    assert len(rec.calls) == 3


def test_2xx_marks_sent_only_on_success(monkeypatch):
    """成功才更新 sent；失败下轮重发（自愈）。"""
    rec = _Recorder(statuses=[201])
    monkeypatch.setattr(panel_reporter._sess, "post", rec)
    stock = {"SZ300319": {"klineDate": "2026-09-29", "kline": [], "appearances": []}}
    _run_sync(_view(), stocks=stock)
    assert sent_snapshot() == {"SZ300319": "2026-09-29"}

    rec2 = _Recorder(statuses=[500])
    monkeypatch.setattr(panel_reporter._sess, "post", rec2)
    reset_sent()
    _run_sync(_view(), stocks=stock)
    assert sent_snapshot() == {}, "失败不得更新 sent"


def test_secret_never_appears_in_logs(monkeypatch):
    """日志纪律（design §3.2 第 5 层）：日志只记状态码与字节数。"""
    import logging as _logging

    rec = _Recorder(statuses=[401])
    monkeypatch.setattr(panel_reporter._sess, "post", rec)
    # 不能用 caplog：`report()` 会把 panel logger 的 propagate 关掉（防密钥外泄到根 logger）。
    # 故直接往 panel logger 上挂一个收集器。
    lines: list[str] = []
    handler = _logging.Handler()
    handler.emit = lambda r: lines.append(r.getMessage())
    panel_reporter._log.addHandler(handler)
    try:
        _run_sync(_view())
    finally:
        panel_reporter._log.removeHandler(handler)
    blob = "\n".join(lines)
    assert "401" in blob, "状态码必须留证据（否则日志无用）"
    assert "s3cr3t" not in blob and "Bearer" not in blob


def test_request_headers_and_timeout_follow_contract(monkeypatch):
    """DR-4/R4：Bearer 头 + 显式 timeout (3, 8)。"""
    rec = _Recorder(statuses=[201])
    monkeypatch.setattr(panel_reporter._sess, "post", rec)
    _run_sync(_view())
    call = rec.calls[0]
    assert call["timeout"] == _TIMEOUT == (3, 8)
    assert call["headers"]["Authorization"] == "Bearer s3cr3t"
    assert call["headers"]["Content-Type"] == "application/json"


def test_serialize_failure_does_not_raise(monkeypatch):
    """序列化炸（view 结构异常）→ report() 正常返回，且**不起线程**。"""
    started = []
    monkeypatch.setattr(panel_reporter.threading.Thread, "start", lambda self, *a, **k: started.append(self))
    boom = ScanView(
        main_rows=[object()], breakout_mark={("a", "b"): True}, flow_pct_map={}, last_ranks={}, weak=False, warnings=[]
    )
    report(boom, url="https://x/api/ingest", secret="s", **_EXTRA)  # 不抛即通过
    assert started == []


def test_serialize_budget_measured_in_calling_thread(monkeypatch):
    """序列化在调用线程内完成（后台线程就测不到 50ms 预算）。"""
    rec = _Recorder(statuses=[201])
    monkeypatch.setattr(panel_reporter._sess, "post", rec)
    main = threading.current_thread()
    seen = []
    real_serialize = panel_reporter.serialize_view

    def spy(*a, **k):
        seen.append(threading.current_thread())
        return real_serialize(*a, **k)

    monkeypatch.setattr(panel_reporter, "serialize_view", spy)
    _run_sync(_view())
    assert seen and all(t is main for t in seen), "序列化必须在主线程"


def test_serialize_budget_excludes_collector_db_io(monkeypatch):
    """预算只管序列化，**不得**把 collect_stocks 的 DB I/O 算进去。

    回归背景（2026-09-29 实测踩到）：计时起点原本放在 collect 之前，导致
    ~1ms 的序列化被报成 250~600ms，budget 告警整轮刷屏、彻底失去意义。
    这里用「让 collect 变慢」制造可观测差异，断言它**不**触发序列化告警。
    """
    import logging as _logging

    slow_collect = 0.30
    monkeypatch.setattr(
        panel_reporter,
        "collect_stocks",
        lambda v, c: (_REAL_SLEEP(slow_collect), {})[1],
    )
    monkeypatch.setattr(panel_reporter._sess, "post", _Recorder(statuses=[201]))

    lines: list[str] = []
    handler = _logging.Handler()
    handler.emit = lambda r: lines.append(r.getMessage())
    panel_reporter._log.addHandler(handler)
    try:
        _run_sync(_view())
    finally:
        panel_reporter._log.removeHandler(handler)

    blob = "\n".join(lines)
    assert "> 50ms budget" not in blob, f"收集耗时被错误计入序列化预算：{blob}"
    assert "collect=" in blob, f"应单独记录 collect 耗时：{blob}"
    # 且 collect 段确实被记下了真实量级（≥0.3s）
    val = float(blob.split("collect=")[1].split("ms")[0])
    assert val >= slow_collect * 1000, f"collect 计时未反映真实耗时：{val}"


def test_report_does_not_block_main_thread(monkeypatch):
    """report() **不等待网络**：调用线程一次 sleep 都不该有。

    改写原因（2026-09-29）：旧版断言「总耗时 < 0.5s」是**墙钟**断言，受机器
    负载/GC/日志 I/O 影响会偶发失败（整套连跑时实测出现过一次）。且它**证明力更弱**：
    退避 sleep 已被打成 0.01s，即便 report() 真的阻塞了主线程，也只多花 20ms，
    照样通过 —— 它实际测的是「机器够不够快」，不是「有没有阻塞」。

    现直接断言**调用线程没有调用过 sleep**，这才是该性质的直接证据，且无墙钟依赖。
    后台 daemon 线程的退避 sleep 会被记录，但按线程号过滤掉。
    """
    sleeps: list[tuple[int, float]] = []
    monkeypatch.setattr(panel_reporter.time, "sleep", lambda s: sleeps.append((threading.get_ident(), s)))
    monkeypatch.setattr(panel_reporter._sess, "post", _Recorder(exc=RuntimeError("down")))

    main_id = threading.get_ident()
    report(_view(), url="https://x/api/ingest", secret="s", **_EXTRA)

    assert [s for tid, s in sleeps if tid == main_id] == [], "report() 不得在调用线程里退避等待网络"


def test_worst_case_thread_lifetime_under_round_interval():
    """最坏线程时长 8+1+8+3+8=28s < 60s 轮间隔 ⇒ 不堆积（design §3.1）。"""
    worst = _TIMEOUT[1] + _BACKOFF[0] + _TIMEOUT[1] + _BACKOFF[1] + _TIMEOUT[1]
    assert worst == 28
    assert worst < 60


# ── 增量收集器（T4.2 的一半，依赖 T0.3 的 sent 表）────────────────────────


def _db(sym_dates: dict[str, list[str]]) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE daily_kline (symbol TEXT, date TEXT, open REAL, close REAL, high REAL, low REAL, volume REAL, percent REAL, finalized INTEGER, PRIMARY KEY(symbol,date))"
    )
    conn.execute(
        "CREATE TABLE recommendations (date TEXT, time TEXT, symbol TEXT, name TEXT, category TEXT, score INTEGER, percent REAL)"
    )
    for sym, dates in sym_dates.items():
        for d in dates:
            conn.execute("INSERT INTO daily_kline VALUES(?,?,1,1,1,1,1,0,1)", (sym, d))
    return conn


def test_collector_caps_at_20_stocks():
    """每轮 ≤20 票（FR-C2 规则 5）。"""
    conn = _db({f"SZ{300000 + i}": ["2026-09-28"] for i in range(50)})
    out = collect_stocks(_view([f"SZ{300000 + i}" for i in range(50)]), conn)
    assert len(out) == 20


def test_collector_skips_symbols_without_kline():
    """无 K 线的票跳过且不阻塞（详情页走 404 文案）。"""
    conn = _db({"SZ300000": ["2026-09-28"]})
    out = collect_stocks(_view(["SZ300000", "SZ399999"]), conn)
    assert set(out) == {"SZ300000"}


def test_collector_resends_only_when_kline_advanced():
    """sent 命中且 K 线未前进 → 不重发（省带宽）；前进 → 重发。"""
    conn = _db({"SZ300000": ["2026-09-28"]})
    panel_reporter._mark_sent({"SZ300000": "2026-09-28"})
    assert collect_stocks(_view(["SZ300000"]), conn) == {}
    conn.execute("INSERT INTO daily_kline VALUES('SZ300000','2026-09-29',1,1,1,1,1,0,1)")
    out = collect_stocks(_view(["SZ300000"]), conn)
    assert set(out) == {"SZ300000"} and out["SZ300000"]["klineDate"] == "2026-09-29"


def test_collector_converges_within_3_rounds_after_restart():
    """重启后 50 票 ÷ 20/轮 ≈ 3 轮收敛（design §3.1 第 4 条）。"""
    reset_sent()
    syms = [f"SZ{300000 + i}" for i in range(50)]
    conn = _db({s: ["2026-09-28"] for s in syms})
    rounds = 0
    seen: set[str] = set()
    while True:
        out = collect_stocks(_view(syms), conn)
        if not out:
            break
        rounds += 1
        assert rounds <= 3, f"应在 3 轮内收敛，实得 {rounds}"
        seen |= set(out)
        panel_reporter._mark_sent({s: out[s]["klineDate"] for s in out})
    assert seen == set(syms)


def test_collector_prefers_new_entries_under_quota():
    """配额紧张时新票优先（收集器第 2 条）。"""
    syms = [f"SZ{300000 + i}" for i in range(30)]
    conn = _db({s: ["2026-09-28"] for s in syms})
    view = _view(syms)
    # 只让最后一票 is_new_entry=True
    for r in view.main_rows:
        r.is_new_entry = r.entry["symbol"] == syms[-1]
    out = collect_stocks(view, conn)
    assert syms[-1] in out
    assert len(out) == 20


def test_collector_payload_shape_matches_contract():
    conn = _db({"SZ300000": ["2026-09-28"]})
    out = collect_stocks(_view(["SZ300000"]), conn)
    s = out["SZ300000"]
    assert set(s) == {"klineDate", "kline", "appearances"}
    assert s["klineDate"] == "2026-09-28"
    assert isinstance(s["kline"], list) and isinstance(s["kline"][0], list)
    assert "_new" not in s, "内部排序标记不得进契约"
    json.dumps(out, ensure_ascii=False)


def test_collector_without_conn_is_noop():
    """没有 DB 连接（如单测/CLI）→ 不带票，不报错。"""
    assert collect_stocks(_view(), None) == {}


# ── 三开关口径（T0.5 验收：未配置 → 连模块都不导入）────────────────────────


def test_panel_disabled_when_url_or_secret_missing(monkeypatch):
    from unified_scanner import _panel_enabled

    monkeypatch.delenv("RTS_PANEL", raising=False)
    monkeypatch.delenv("RTS_PANEL_URL", raising=False)
    monkeypatch.delenv("RTS_PANEL_SECRET", raising=False)
    assert _panel_enabled(False) == (False, "", "")

    monkeypatch.setenv("RTS_PANEL_URL", "https://panel.example/api/ingest")
    assert _panel_enabled(False)[0] is False, "只配 URL 不算配好"

    monkeypatch.setenv("RTS_PANEL_SECRET", "s3cr3t")
    assert _panel_enabled(False)[0] is True


def test_panel_flag_no_overrides_env(monkeypatch):
    """`--no-panel` 优先级最高：即使 URL/_SECRET 全配上也关。"""
    from unified_scanner import _panel_enabled

    monkeypatch.setenv("RTS_PANEL_URL", "https://panel.example/api/ingest")
    monkeypatch.setenv("RTS_PANEL_SECRET", "s3cr3t")
    assert _panel_enabled(True) == (False, "", "")
    assert _panel_enabled(False)[0] is True


def test_panel_env_zero_disables_even_when_configured(monkeypatch):
    from unified_scanner import _panel_enabled

    monkeypatch.setenv("RTS_PANEL_URL", "https://panel.example/api/ingest")
    monkeypatch.setenv("RTS_PANEL_SECRET", "s3cr3t")
    monkeypatch.setenv("RTS_PANEL", "0")
    assert _panel_enabled(False)[0] is False


def test_panel_reporter_module_not_imported_when_unconfigured(monkeypatch):
    """验收 1 硬要求：未配置时 `scanner.panel_reporter` **不在 sys.modules**。

    做法：先把三个模块从 `sys.modules` 里剔掉再 import `unified_scanner`，
    即在**本进程内做一次真正的全新导入**（比子进程快，且不受解释器启动开销影响）。
    断言的是挂点的**惰性导入**结构：模块顶层没有 `from scanner.panel_reporter import ...`。
    """
    import importlib
    import sys as _sys

    for name in ("scanner.panel_serialize", "scanner.panel_reporter", "unified_scanner"):
        _sys.modules.pop(name, None)
    monkeypatch.delenv("RTS_PANEL", raising=False)
    monkeypatch.delenv("RTS_PANEL_URL", raising=False)
    monkeypatch.delenv("RTS_PANEL_SECRET", raising=False)

    us = importlib.import_module("unified_scanner")

    assert us._panel_enabled(False)[0] is False
    assert "scanner.panel_reporter" not in _sys.modules, "未配置时上报器模块不得被导入"
    assert "scanner.panel_serialize" not in _sys.modules, "未配置时序列化模块不得被导入"
    assert not hasattr(us, "report"), "挂点必须局部导入，不得绑到主模块命名空间"
