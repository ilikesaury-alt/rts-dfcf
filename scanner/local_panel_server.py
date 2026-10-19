"""桌面版**本地只读 HTTP 服务**（Issue A，2026-10-19）。

绑定 **127.0.0.1**（安全硬约束：误绑 `0.0.0.0` = 同网段任何人可读本地数据；
`BIND_HOST` 是模块常量，无 env 覆盖路径）。端点语义**对齐云端**（design D5）：
前端 `api/client.ts` / `api/types.ts` 完全复用，不引入第二套契约。

| 端点 | 数据来源 | 语义 |
|---|---|---|
| `GET /api/health` | 无 | 存活探针（不碰 DB） |
| `GET /api/regions` | 进程内缓存 | 五区块 + ctx，**去 stocks**（列表页不碰 K 线，D4） |
| `GET /api/meta` | 进程内缓存 | 无快照 → **200** `{ready:false}`（不是 404） |
| `GET /api/stock/{sym}/kline` | `scanner.db` 现算 | K 线 + 评分分解 + 出现史 |

另有**静态托管**：若 `rts-panel-cloud/web/dist` 已构建，非 `/api` 路径按文件回放
（带 SPA 回退）—— 生产态 Electron 直接加载 `http://127.0.0.1:<port>/`，与 API
同源 ⇒ 免 CORS、免主进程转发（design §6.3 / §6.4 高风险点 6）。

⛔ **`/api/regions` 必须显式去 `stocks`**：`serialize_view` 把 `stocks` 放在载荷
**顶层**（`regions` 本就不含它），但一旦有人图省事整份回放，列表页首屏就会开始拉
K 线（违反 D4「列表页不碰 K 线」）。守卫见 `tests/test_local_panel.py`。
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
import sqlite3
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import unquote, urlsplit

from scanner.config import BASE_DIR, LOCAL_PANEL_PORT, LOG_DIR
from scanner.local_view_cache import get_latest

_log = logging.getLogger("local_panel")


def _ensure_file_log() -> None:
    """把 local_panel logger 挂到 `logs/local_panel.log`（幂等，`start_server` 时建一次）。

    与 `panel_reporter._ensure_file_log` 同一动机，但这边多一条硬理由：
    **扫描器的 stdout 是主输出面**（每轮重绘终端面板），而 `socketserver` 默认把
    **整段 traceback 打到 stderr** —— 连接级异常（客户端关闭 keep-alive）**必现**，
    于是用户每点一下面板就吃掉半个屏幕。故这里既落盘（保留证据）又
    `propagate = False`（**无论如何**不进终端，含建文件失败的情形：
    宁可丢日志，不可污染面板）。
    """
    if getattr(_log, "_local_panel_handler", False):
        return
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        handler = logging.FileHandler(os.path.join(LOG_DIR, "local_panel.log"), encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        _log.addHandler(handler)
        _log.setLevel(logging.INFO)
    except OSError:
        pass  # 落不了盘就只留静音 —— fail-open，不阻断扫描
    _log.propagate = False  # ⚠ 在 try 之外：建文件失败也不许回落 stderr
    _log._local_panel_handler = True  # type: ignore[attr-defined]


# ⛔ 安全硬约束：只绑回环。不要引入 env 覆盖（同网段可读 = 数据泄露）。
BIND_HOST = "127.0.0.1"

# 前端构建产物（`pnpm build` 输出）。缺失时静态路由 404，不致命。
DEFAULT_STATIC_DIR = os.path.join(BASE_DIR, "rts-panel-cloud", "web", "dist")

# 与云端 `ingest.ts` 的 SYMBOL_RE 同**意图**（只放行本仓的雪球符号形态），但形态更宽：
# 本仓 `daily_kline.symbol` 的实际格式是「2 位市场前缀 + 6 位数字」（如 `SZ300001`，
# 见 AGENTS.md 的 volume 单位条目），而云端那条 `^\d{6}$` 在本仓数据上**永不匹配**
# （云端侧问题，不属本 Issue）。这里只允许字母前缀 + 数字，杜绝路径穿越/注入式 symbol。
_SYMBOL_RE = re.compile(r"^[A-Za-z]{0,2}\d{6}$")

# 评分分解回看条数：最新一条**非空**即用（非 v1 票无分解 → null）。
_BREAKDOWN_SCAN = 20

_server: LocalPanelHTTPServer | None = None
_server_lock = threading.Lock()


def _ok(data: Any) -> dict:
    return {"ok": True, "data": data}


def _err(code: str, message: str) -> dict:
    return {"ok": False, "error": {"code": code, "message": message}}


def _connect() -> sqlite3.Connection:
    """每请求一条连接（SQLite 连接默认不可跨线程共享）。

    走 `db.schema.get_conn` —— 唯一连接工厂（timeout/busy_timeout/WAL 三件套
    在那里收口；WAL 下读不阻塞主循环写）。调用方负责 `close()`。
    """
    from scanner.db.schema import get_conn

    return get_conn()


def _score_breakdown_for(conn: sqlite3.Connection, symbol: str) -> dict | None:
    """该票**最新一条非空** `score_breakdown`（非 v1 票 → `None`）。"""
    from scanner.models import parse_score_breakdown

    try:
        cur = conn.execute(
            "SELECT score_breakdown FROM recommendations WHERE symbol = ? ORDER BY date DESC, time DESC LIMIT ?",
            (symbol, _BREAKDOWN_SCAN),
        )
    except sqlite3.Error:
        return None
    for (raw,) in cur.fetchall():
        sb = parse_score_breakdown(raw)
        if sb:
            return sb
    return None


def build_stock_payload(symbol: str, conn: sqlite3.Connection) -> dict | None:
    """详情页载荷；`daily_kline` 无该票 → `None`（端点回 404 `NOT_IN_REPORT`）。

    `_klines_for` / `_appearances_for` **刻意复用 `panel_reporter` 的实现**：云端版本
    同一份票数据也是这两个函数产出的，复用即两端形状不可能漂移（design §6.3）。
    """
    from scanner.panel_reporter import _appearances_for, _klines_for

    rows = (_klines_for(conn, [symbol]) or {}).get(symbol) or []
    if not rows:
        return None
    return {
        "symbol": symbol,
        "klineDate": rows[-1][0],
        "kline": rows,
        "appearances": _appearances_for(conn, symbol),
        "scoreBreakdown": _score_breakdown_for(conn, symbol),
    }


class LocalPanelHTTPServer(ThreadingHTTPServer):
    """线程化 HTTP 服务端；`static_dir` 挂在实例上供 handler 取。"""

    daemon_threads = True

    def __init__(self, addr: tuple[str, int], handler: type, *, static_dir: str | None = None):
        super().__init__(addr, handler)
        self.static_dir = static_dir

    @property
    def port(self) -> int:
        return int(self.server_address[1])

    def handle_error(self, request: Any, client_address: Any) -> None:
        """覆盖 `socketserver` 的默认实现：**不把 traceback 打到终端**。

        默认实现 `traceback.print_exc()` 直写 stderr —— 而本服务的客户端（浏览器 /
        `fetch`）关闭 keep-alive 连接是**常态**（实测 2026-10-19：每请求一次
        `ConnectionResetError`），于是终端面板被 25 行 traceback 反复冲刷。
        连接级异常降为 debug；其余异常记 warning + traceback **落盘**（证据仍在，
        用户读的终端不被刷屏）。
        """
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, TimeoutError)):
            _log.debug("local panel: 客户端提前断开 %s %r", client_address, exc)
            return
        _log.warning("local panel: 请求处理异常 %s %r", client_address, exc, exc_info=exc)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # 一律带 Content-Length，故 keep-alive 安全
    server_version = "RTSLocalPanel/1.0"

    @property
    def static_dir(self) -> str | None:
        return getattr(self.server, "static_dir", None)

    def log_message(self, fmt: str, *args: Any) -> None:
        """静音默认 stderr 噪声（每 60s 一次 /api/regions 会刷屏）。"""
        _log.debug("%s - %s", self.address_string(), fmt % args)

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler 契约)
        try:
            self._route()
        except Exception as e:  # 任何未预期异常 → 500 信封，不裸崩服务线程
            _log.warning("local panel: 请求处理异常 %r", e)
            self._send_json(_err("INTERNAL", "服务内部错误"), 500)

    # ── 路由 ──────────────────────────────────────────────────────────────

    def _route(self) -> None:
        path = unquote(urlsplit(self.path).path)
        if path == "/api/health":
            return self._send_json(_ok({"alive": True}))
        if path == "/api/regions":
            return self._regions()
        if path == "/api/meta":
            return self._meta()
        if path.startswith("/api/stock/") and path.endswith("/kline"):
            return self._stock(path[len("/api/stock/") : -len("/kline")])
        if path.startswith("/api/"):
            return self._send_json(_err("NOT_FOUND", "无此端点"), 404)
        return self._static(path)

    def _regions(self) -> None:
        payload = get_latest()
        if not payload or "regions" not in payload:
            # 与云端一致：无快照是错误（前端按「等待首轮上报」处理，不白屏）。
            return self._send_json(_err("NO_SNAPSHOT", "尚无任何上报"), 404)
        data = {
            "date": payload.get("date"),
            "time": payload.get("time"),
            "seq": payload.get("seq", 0),
            "regions": payload["regions"],
            "ctx": payload.get("ctx") or {},
        }
        return self._send_json(_ok(data))

    def _meta(self) -> None:
        payload = get_latest()
        if not payload:
            return self._send_json(_ok({"ready": False, "date": None, "time": None, "seq": None}))
        return self._send_json(
            _ok(
                {
                    "ready": True,
                    "date": payload.get("date"),
                    "time": payload.get("time"),
                    "seq": payload.get("seq"),
                }
            )
        )

    def _stock(self, symbol: str) -> None:
        sym = symbol.strip()
        if not _SYMBOL_RE.match(sym):
            return self._send_json(_err("BAD_REQUEST", "symbol 必须为 6 位数字"), 400)
        conn = _connect()
        try:
            data = build_stock_payload(sym, conn)
        finally:
            conn.close()
        if data is None:
            return self._send_json(_err("NOT_IN_REPORT", "本轮上报未包含该票"), 404)
        return self._send_json(_ok(data))

    # ── 静态资源（生产态同源托管 web/dist）─────────────────────────────────

    def _static(self, path: str) -> None:
        root = self.static_dir
        if not root or not os.path.isdir(root):
            return self._send_json(_err("NOT_FOUND", "无此端点（前端未构建，仅 /api 可用）"), 404)
        root_abs = os.path.abspath(root)
        target = os.path.normpath(os.path.join(root_abs, path.lstrip("/") or "index.html"))
        if target != root_abs and not target.startswith(root_abs + os.sep):
            return self._send_json(_err("NOT_FOUND", "无此端点"), 404)
        if os.path.isdir(target):
            target = os.path.join(target, "index.html")
        if not os.path.isfile(target):
            # SPA 回退：未知的非 /api 路径交给前端自己的路由
            target = os.path.join(root_abs, "index.html")
            if not os.path.isfile(target):
                return self._send_json(_err("NOT_FOUND", "无此端点"), 404)
        ctype = mimetypes.guess_type(target)[0] or "application/octet-stream"
        with open(target, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return None

    # ── 响应 ──────────────────────────────────────────────────────────────

    def _send_json(self, obj: Any, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def start_server(
    port: int = LOCAL_PANEL_PORT,
    *,
    static_dir: str | None = DEFAULT_STATIC_DIR,
) -> LocalPanelHTTPServer:
    """启动服务（**幂等**：已启动则原样返回）。

    绑定失败（端口占用等）**原样上抛** `OSError` —— 由调用方 fail-open：端口冲突
    只告警、不阻断扫描（与 `panel_reporter` 同纪律）。
    """
    global _server
    with _server_lock:
        if _server is not None:
            return _server
        _ensure_file_log()
        srv = LocalPanelHTTPServer((BIND_HOST, port), _Handler, static_dir=static_dir)
        threading.Thread(target=srv.serve_forever, name="local-panel", daemon=True).start()
        _server = srv
        _log.info("local panel: http://%s:%d/ (static=%s)", BIND_HOST, srv.port, static_dir)
        return srv


def stop_server() -> None:
    """停服（幂等；**仅测试与进程退出用**）。"""
    global _server
    with _server_lock:
        srv, _server = _server, None
    if srv is not None:
        srv.shutdown()
        srv.server_close()
