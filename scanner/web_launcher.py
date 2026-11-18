"""网页版**启动台**（控制面）：先有页面，再从页面拉起扫描器（2026-11-17）。

为什么需要它
------------
面板（`local_panel_server.py`）住在扫描器进程里 ⇒ **扫描器不跑就没有页面**。
想让「打开网页 → 点按钮启动扫描」成立，必须有一个**先于扫描器存在**的常驻进程。
本模块就是这个最小进程：一个 `ThreadingHTTPServer`，只做控制面 + 一张静态页。

�� 它**不是**数据面。数据面仍然是扫描器里的那个只读服务 —— 本模块**不代理、不缓存、
不重算**任何面板数据（代理会引入第二套契约与另一条写平面入口，见下）。

端口（与既有常量**零改动**，这是刻意的）
----------------------------------------
| 端口 | 归属 | 谁给的 |
|---|---|---|
| **8901** | 启动台（本模块） | `RTS_WEB_CONSOLE_PORT`，脏值回退 8901 |
| 8899 | 面板 | 扫描器自己的默认（`config.LOCAL_PANEL_PORT`）|
| 8900 | 动作面 | 扫描器自己的默认（`tasks_server.DEFAULT_PORT`）|

面板口不挪位的关键理由：动作面的四层校验要求 `Origin` 逐字等于
`127.0.0.1:<面板口>` 或 `127.0.0.1:<任务口>`（`tasks_server._allowed_hosts`）。
**面板仍由扫描器自己托管、浏览器直连 8899** ⇒ Origin 天然自洽，校验零改动。
若改成「启动台反代一切」，浏览器 Origin 会变成 8901 而面板口是 8899 ⇒ 必然 403；
要修就得把 8901 也加进白名单 —— 等于给「能触发本机命令」的面再开一个入口。不做。

四层校验（与 `tasks_server` 同款，不新造）
------------------------------------------
1. 只绑 `127.0.0.1`（`BIND_HOST` 模块常量，**无 env 覆盖**）；非回环直接拒绝启动。
2. `Host` 必须逐字 `127.0.0.1:<port>`。
3. `Origin` **有则**必须逐字 `http://127.0.0.1:<port>`（无 Origin 放行：curl/本页同源）。
4. POST 必须 `Content-Type: application/json` + `X-RTS-Console-Token`
   （一次性 token，由 `GET /api/console/health` 响应头 + `data` 同时下发，
   进程退出即失效，不落盘、不进日志）。任何响应都**不带** `Access-Control-Allow-*`。

「已有实例就拒绝」语义（用户决策 2026-11-17）
---------------------------------------------
点「启动」时先探单实例锁（`SingleInstanceLock`，扫描器自己的锁）：

* 锁**被占** ⇒ 返回 409 `ALREADY_RUNNING`，**不杀任何进程**（与桌面壳的「接管并杀旧」
  相反：那条路会把你正在看的终端扫描器结束掉）。
* 锁**可取** ⇒ 取出立刻释放，然后 spawn。窗口期（释放→子进程取锁）内有极小概率被
  别人抢到，此时子进程会自己打印「已有扫描器实例在运行」并以 2 退出 —— 本模块
  如实回报 `start_failed` + 日志尾巴，不重试、不抹平。

回滚：不跑本模块即可（它对主链路是**纯加法**，扫描器与终端行为一字不改）。
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from scanner.config import BASE_DIR, LOCAL_PANEL_PORT, LOG_DIR

# ⛔ 安全硬约束：只绑回环。误绑 `0.0.0.0` = 同网段任何人能启动/停止你的扫描器。
BIND_HOST = "127.0.0.1"
#: 启动台默认端口。**与面板口（8899）刻意不同**：面板是扫描器的，启动台是独立的。
DEFAULT_PORT = 8901
#: 动作面默认端口（与 `tasks_server.DEFAULT_PORT` 同值；仅用于状态回显）。
TASKS_PORT_DEFAULT = 8900
#: 单次请求体上限（只收一个 `{}` 或 `{"confirm":true}`）。
MAX_BODY_BYTES = 4 * 1024
#: 启动后等待面板 `/api/meta` 就绪的最长时间（首轮扫描含取数，实测 20~90s）。
READY_TIMEOUT = 180.0
#: 状态探针超时（本机回环，0.3s 足够；超时=没起来）。
_PROBE_TIMEOUT = 0.35
#: 子进程日志上限：超限截尾（只留最近 256KB），避免长跑把盘写满。
_LOG_MAX_BYTES = 2 * 1024 * 1024
_LOG_KEEP_BYTES = 256 * 1024
#: 关闭子进程时的宽限期（Windows 上 terminate 即 TerminateProcess，但仍等一会儿取退出码）。
_KILL_GRACE = 8.0
#: 状态轮询间隔（前端用；服务端只给建议值）。
POLL_INTERVAL_MS = 1500

_TOKEN_HEADER = "X-RTS-Console-Token"  # noqa: S105 —— 这是 HTTP 头名，不是密钥

_log = logging.getLogger("web_launcher")
_server: ConsoleHTTPServer | None = None
_server_lock = threading.Lock()


def _now_iso() -> str:
    from scanner.config import now_beijing

    return now_beijing().isoformat(timespec="seconds")


def _port() -> int:
    """启动台端口：`RTS_WEB_CONSOLE_PORT` 脏值/越界回退 `DEFAULT_PORT`。"""
    try:
        candidate = int((os.environ.get("RTS_WEB_CONSOLE_PORT") or "").strip())
    except ValueError:
        candidate = 0
    return candidate if 1 <= candidate <= 65535 else DEFAULT_PORT


# ── 进程管理 ────────────────────────────────────────────────────────────


class ScannerSupervisor:
    """扫描器子进程的启动/停止/状态（**唯一**持有 `Popen` 的地方）。

    状态不靠内存猜：`state()` 每次都做两个廉价探测 ——
    （1）子进程是否还活着（`Popen.poll()`）；（2）面板 `/api/meta` 是否就绪。
    内存里只有 `_child` 与 `_started_at`，故启动台重启后仍能如实报「别人在跑」。
    """

    def __init__(self, *, panel_port: int = LOCAL_PANEL_PORT, tasks_port: int = TASKS_PORT_DEFAULT):
        self.panel_port = panel_port
        self.tasks_port = tasks_port
        #: 实际绑定端口，由 `start_server` 在绑完后回填（测试用临时端口时≠配置值）。
        self.console_port: int | None = None
        self._child: subprocess.Popen[bytes] | None = None
        self._started_at: float | None = None
        self._last_exit: dict[str, Any] | None = None
        self._log_path = Path(LOG_DIR) / "console_child.log"
        self._lock = threading.Lock()

    # ── 探针 ──────────────────────────────────────────────────────────

    @property
    def panel_url(self) -> str:
        return f"http://{BIND_HOST}:{self.panel_port}/"

    def _panel_ready(self) -> bool:
        """面板 `/api/meta` 的 `ready` 字段（真值来自扫描器，不是本进程编的）。

        ⚠ **显式绕过系统代理**（`ProxyHandler({})`）：urllib 在 Windows 上会吃注册表里的
        系统代理（实测本机 Clash :7890），回环探针被路由进代理后永远探不到就绪 ——
        与 `ai_digest` 的「东财请求显式绕过系统代理」同款纪律：**回环永不走代理**。
        """
        import urllib.request

        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(f"http://{BIND_HOST}:{self.panel_port}/api/meta", timeout=_PROBE_TIMEOUT) as r:
                body = json.loads(r.read().decode("utf-8"))
        except (OSError, ValueError, KeyError):
            return False
        return bool((body.get("data") or {}).get("ready"))

    def _external_running(self) -> bool:
        """单实例锁是否被别人占着（= 有一个扫描器在跑，可能来自终端）。

        用锁而不是扫进程列表：锁是扫描器自己的**唯一**互斥真源（
        `scanner/single_instance.py` 的 docstring 解释了为什么不是 pidfile）。
        取出即释放 —— 只用来问「现在能取到吗」，不持有。
        """
        from scanner.single_instance import SingleInstanceLock

        probe = SingleInstanceLock(Path(LOG_DIR) / "scanner.lock")
        if not probe.acquire():
            return True
        probe.release()
        return False

    def _child_alive(self) -> bool:
        return self._child is not None and self._child.poll() is None

    def _log_tail(self, max_bytes: int = 1600) -> str:
        try:
            size = self._log_path.stat().st_size
            with open(self._log_path, "rb") as fh:
                fh.seek(max(0, size - max_bytes))
                return fh.read().decode("utf-8", errors="replace")
        except OSError:
            return ""

    def _reap(self) -> None:
        """子进程已退出 ⇒ 记录退出码 + 日志尾巴（供页面如实显示失败原因）。"""
        if self._child is None or self._child.poll() is None:
            return
        code = self._child.returncode
        self._last_exit = {
            "code": code,
            "at": _now_iso(),
            "logTail": self._log_tail(),
            "hint": "端口被占或已有实例" if code == 2 else ("正常退出" if code == 0 else "非零退出，见日志"),
        }
        self._child = None
        self._started_at = None

    # ── 状态 ──────────────────────────────────────────────────────────

    def state(self) -> dict[str, Any]:
        """锁外快照入口。⚠ `start()/stop()` **不得**在持锁时调本方法：
        `threading.Lock` 不可重入，锁内再取锁 = 自死锁（实测踩过，见 _state_locked）。"""
        with self._lock:
            return self._state_locked()

    def _state_locked(self) -> dict[str, Any]:
        """真实现：调用方必须已持有 `self._lock`。"""
        self._reap()
        own = self._child_alive()
        starting = own and not self._panel_ready()
        external = False if own else self._external_running()
        if own:
            name = "starting" if starting else "running"
        elif external:
            name = "external"
        else:
            name = "idle"
        uptime = int(time.monotonic() - self._started_at) if (own and self._started_at) else 0
        return {
            "state": name,
            "ownProcess": own,
            "pid": self._child.pid if own and self._child else None,
            "panelUrl": self.panel_url,
            "panelPort": self.panel_port,
            "consolePort": self.console_port or _port(),
            "tasksPort": self.tasks_port,
            "uptimeSec": uptime,
            "ready": self._panel_ready() if own else False,
            "lastExit": self._last_exit,
            "pollIntervalMs": POLL_INTERVAL_MS,
            "logPath": str(self._log_path),
        }

    # ── 启停 ──────────────────────────────────────────────────────────

    def build_child_env(self) -> dict[str, str]:
        """子进程 env：只**注入开关**，不覆盖用户的其它配置。

        * `RTS_LOCAL_PANEL=1`：强制开（这是启动台存在的全部理由）。
        * `RTS_TASKS`：用 `setdefault` —— 用户显式 `=0` 时尊重（关掉 AI 分析入口）。
        * `PYTHONUNBUFFERED/PYTHONIOENCODING`：日志实时可见 + Windows 中文不乱码。
        * `--no-lock` **绝不注入**：单实例锁是扫描器唯一的互斥保障（有守卫）。
        """
        env = os.environ.copy()
        env["RTS_LOCAL_PANEL"] = "1"
        env.setdefault("RTS_TASKS", "1")
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        return env

    def _extra_args(self) -> list[str]:
        """联调附加参数（空白分隔）。镜像桌面壳的 `RTS_DESKTOP_SCANNER_ARGS` 同款入口：
        正当用法是 `--no-feishu`（手边已有推送链路时做实测）。本模块**不**主动拼任何
        改变扫描语义的参数 —— 这是用户的显式出口，不是默认行为。"""
        import shlex

        raw = (os.environ.get("RTS_CONSOLE_SCANNER_ARGS") or "").strip()
        return shlex.split(raw) if raw else []

    def start(self) -> dict[str, Any]:
        """拉起扫描器。已有实例 ⇒ 抛 `_HttpError(409)`，**不杀任何进程**。"""
        with self._lock:
            self._reap()
            if self._child_alive():
                raise _HttpError(409, "ALREADY_RUNNING", "本启动台拉起的扫描器仍在运行")
            if self._external_running():
                raise _HttpError(409, "ALREADY_RUNNING", "已有扫描器实例在运行（可能是终端里的那个）")
            script = Path(BASE_DIR) / "unified_scanner.py"
            if not script.is_file():
                raise _HttpError(500, "NO_SCANNER", f"找不到扫描器脚本：{script}")
            Path(LOG_DIR).mkdir(parents=True, exist_ok=True)
            self._rotate_log()
            # 上下文管理器只包 spawn：子进程已继承 fd，父进程随即放手
            # （持有句柄会妨碍后续 rotate/清理）。
            with open(self._log_path, "ab") as log_fh:
                # 不经 shell（路径/引号注入与 ENOENT 都归零；与 desktop/main.js 同纪律）。
                # 附加参数只来自 `RTS_CONSOLE_SCANNER_ARGS`（联调出口），默认一个都不加。
                self._child = subprocess.Popen(  # noqa: S603 —— argv 恒由常量+显式 env 组成
                    [sys.executable, str(script), *self._extra_args()],
                    cwd=str(BASE_DIR),
                    env=self.build_child_env(),
                    stdout=log_fh,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            self._started_at = time.monotonic()
            self._last_exit = None
            return self._state_locked()

    def stop(self) -> dict[str, Any]:
        """结束**本启动台拉起的**子进程。外部实例一律拒绝（只提示，不代杀）。"""
        with self._lock:
            if not self._child_alive():
                self._reap()
                if self._external_running():
                    raise _HttpError(409, "EXTERNAL_INSTANCE", "该扫描器不是本启动台拉起的，请到终端停止")
                return self._state_locked()
            child = self._child
            assert child is not None
            child.terminate()
            try:
                child.wait(timeout=_KILL_GRACE)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=_KILL_GRACE)
            self._reap()
            return self._state_locked()

    def log_since(self, offset: int) -> dict[str, Any]:
        try:
            data = self._log_path.read_bytes()
        except OSError:
            data = b""
        start = max(0, min(offset, len(data)))
        chunk = data[start:]
        return {
            "offset": len(data),
            "text": chunk.decode("utf-8", errors="replace"),
            "truncated": offset > len(data),
        }

    def _rotate_log(self) -> None:
        """超限则只保留尾部 —— 长跑不把盘写满，又不丢最近的失败现场。"""
        try:
            if self._log_path.stat().st_size <= _LOG_MAX_BYTES:
                return
            tail = self._log_path.read_bytes()[-_LOG_KEEP_BYTES:]
            self._log_path.write_bytes(tail)
        except OSError:
            pass  # 日志是旁路：写不了也不拦启动


# ── HTTP 层 ─────────────────────────────────────────────────────────────


class _HttpError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _ok(data: Any) -> dict[str, Any]:
    return {"ok": True, "data": data}


def _err(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


class ConsoleHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    # ⛔ **独占**，不要 `allow_reuse_address = True`：Windows 上它映射到 SO_REUSEADDR，
    # 允许两个进程**同时**绑同一个端口（不是"抢占"，是并存）⇒ 第二个启动台静默起得来，
    # 请求随机落到其中一个，停止按钮偶尔失灵（2026-11-17 双击实测踩过）。
    # 关掉它，重复启动会得到干净的 `OSError(WinError 10048)`，由 `main()` 转成"打开已有页面"。
    allow_reuse_address = False

    def __init__(self, addr: tuple[str, int], handler: type, *, supervisor: ScannerSupervisor, token: str):
        super().__init__(addr, handler)
        self.supervisor = supervisor
        self.token = token
        self.expected_port = addr[1]

    def handle_error(self, request: Any, client_address: Any) -> None:
        """不把 traceback 打到终端（本进程可能就在终端里跑）。"""
        return


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "RTSConsole/1.0"

    @property
    def supervisor(self) -> ScannerSupervisor:
        return self.server.supervisor  # type: ignore[attr-defined]

    @property
    def _token(self) -> str:
        return self.server.token  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        return  # 静音默认 stderr

    # ── 四层校验 ─────────────────────────────────────────────────────

    def _guard(self, *, bootstrap: bool = False) -> None:
        port = int(self.server.server_address[1])  # type: ignore[index]
        allowed = f"{BIND_HOST}:{port}"
        host = (self.headers.get("Host") or "").strip()
        if host != allowed:
            raise _HttpError(403, "FORBIDDEN_ORIGIN", "Host 不匹配")
        origin = (self.headers.get("Origin") or "").strip()
        if origin and origin != f"http://{allowed}":
            raise _HttpError(403, "FORBIDDEN_ORIGIN", "Origin 不匹配")
        if bootstrap:
            return  # health 就是取 token 的那一次：别的地方没有 token 可用
        ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if ctype != "application/json":
            raise _HttpError(403, "FORBIDDEN_ORIGIN", "Content-Type 必须为 application/json")
        got = (self.headers.get(_TOKEN_HEADER) or "").strip()
        if not got or not secrets.compare_digest(got, self._token):
            raise _HttpError(403, "FORBIDDEN_ORIGIN", "启动 token 无效")

    # ── 响应 ─────────────────────────────────────────────────────────

    def _drain_body(self) -> None:
        """把**未读**的请求体读掉再回响应。

        ⚠ 不排干的后果实测过（2026-11-17，全量 pytest 才撞上）：四层校验在读书之前
        就返回 403，服务端随即关连接，而客户端还在写 body ⇒ Windows 抛
        `ConnectionAbortedError (WinError 10053)`，客户端看到的是「连接被重置」而
        不是那枚 403。上限用 `MAX_BODY_BYTES`：超限的本来就不读（也回 400）。
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return
        if 0 < length <= MAX_BODY_BYTES:
            try:
                self.rfile.read(length)
            except OSError:
                pass

    def _send_json(self, obj: Any, status: int = 200, *, token_header: bool = False) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if token_header:
            self.send_header(_TOKEN_HEADER, self._token)
        if status >= 400:
            # 拒绝类响应一律不复用连接：客户端可能还在写 body（见 `_drain_body`）。
            self.send_header("Connection", "close")
            self.close_connection = True
        # ⚠ 刻意不返回任何 Access-Control-Allow-*：跨域连预检都过不去。
        self.end_headers()
        self.wfile.write(body)

    def _send_page(self) -> None:
        path = Path(__file__).resolve().parent / "console_ui" / "index.html"
        try:
            body = path.read_bytes()
        except OSError:
            self._send_json(_err("NO_UI", f"启动台页面缺失：{path}"), 500)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict[str, Any]:
        raw_len = self.headers.get("Content-Length")
        try:
            length = int(raw_len or "0")
        except ValueError:
            raise _HttpError(400, "BAD_REQUEST", "Content-Length 非法") from None
        if length < 0 or length > MAX_BODY_BYTES:
            raise _HttpError(400, "BAD_REQUEST", "请求体过大或非法")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise _HttpError(400, "BAD_REQUEST", "请求体不是 JSON") from None
        if not isinstance(obj, dict):
            raise _HttpError(400, "BAD_REQUEST", "请求体必须是 JSON 对象")
        return obj

    # ── 路由 ─────────────────────────────────────────────────────────

    def do_GET(self) -> None:  # noqa: N802
        split = urlsplit(self.path)
        path = unquote(split.path)
        if path in ("/", "/index.html"):
            return self._send_page()
        try:
            if path == "/api/console/health":
                self._guard(bootstrap=True)
                return self._send_json(
                    _ok({"alive": True, "port": self.server.server_address[1], "token": self._token}),  # type: ignore[index]
                    token_header=True,
                )
            if path == "/api/console/status":
                self._guard(bootstrap=True)
                return self._send_json(_ok(self.supervisor.state()))
            if path == "/api/console/log":
                self._guard()
                offset = int((parse_qs(split.query).get("offset") or ["0"])[0] or 0)
                return self._send_json(_ok(self.supervisor.log_since(offset)))
            return self._send_json(_err("NOT_FOUND", "无此端点"), 404)
        except _HttpError as e:
            self._drain_body()
            return self._send_json(_err(e.code, e.message), e.status)
        except (OSError, ValueError, KeyError, TypeError) as e:
            self._drain_body()
            return self._send_json(_err("INTERNAL", f"服务内部错误：{type(e).__name__}"), 500)

    def do_POST(self) -> None:  # noqa: N802
        path = unquote(urlsplit(self.path).path)
        try:
            self._guard()
            if path == "/api/console/start":
                return self._send_json(_ok(self.supervisor.start()), 202)
            if path == "/api/console/stop":
                return self._send_json(_ok(self.supervisor.stop()))
            return self._send_json(_err("NOT_FOUND", "无此端点"), 404)
        except _HttpError as e:
            self._drain_body()
            return self._send_json(_err(e.code, e.message), e.status)
        except (OSError, ValueError, KeyError, TypeError) as e:
            self._drain_body()
            return self._send_json(_err("INTERNAL", f"服务内部错误：{type(e).__name__}"), 500)


# ── 启动 ───────────────────────────────────────────────────────────────


def _ensure_file_log() -> None:
    """落 `logs/web_launcher.log`，且**永不**回落到 stderr（本进程可能就在终端里）。"""
    if getattr(_log, "_web_launcher_handler", False):
        return
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        handler = logging.FileHandler(os.path.join(LOG_DIR, "web_launcher.log"), encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        _log.addHandler(handler)
        _log.setLevel(logging.INFO)
    except OSError:
        pass
    _log.propagate = False
    _log._web_launcher_handler = True  # type: ignore[attr-defined]


def start_server(port: int | None = None, *, supervisor: ScannerSupervisor | None = None) -> ConsoleHTTPServer:
    """起启动台（幂等：同进程重复调用返回同一个实例）。

    非回环地址**拒绝启动**（`BIND_HOST` 是常量，没有走 env 的路径 —— 故这里是
    防御性断言：有人把常量改错时立刻炸，而不是静悄悄地暴露到局域网）。
    """
    global _server
    with _server_lock:
        if _server is not None:
            return _server
        if BIND_HOST not in ("127.0.0.1", "::1", "localhost"):
            raise SystemExit(f"⛔ web_launcher 只允许绑回环地址，当前 BIND_HOST={BIND_HOST!r}")
        _ensure_file_log()
        resolved = port if port is not None else _port()
        srv = ConsoleHTTPServer(
            (BIND_HOST, resolved),
            _Handler,
            supervisor=supervisor or ScannerSupervisor(),
            token=secrets.token_urlsafe(24),
        )
        threading.Thread(target=srv.serve_forever, name="web-launcher", daemon=True).start()
        srv.supervisor.console_port = srv.server_address[1]
        _server = srv
        _log.info("web launcher: http://%s:%d/", BIND_HOST, resolved)
        return srv


def stop_server() -> None:
    """关掉服务线程（测试与退出清理用）。"""
    global _server
    with _server_lock:
        if _server is None:
            return
        _server.shutdown()
        _server.server_close()
        _server = None


def _probe_launcher(url: str) -> bool:
    """该地址上是不是已经跑着一个启动台（`/api/console/health` 能答且 ok）。

    绕代理：回环永不走系统代理（同 `ScannerSupervisor._panel_ready` 的理由）。
    """
    import urllib.request

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url.rstrip("/") + "/api/console/health", timeout=1.0) as r:
            body = json.loads(r.read().decode("utf-8"))
    except (OSError, ValueError):
        return False
    return bool(body.get("ok"))


def _open_browser(url: str) -> None:
    try:
        import webbrowser

        webbrowser.open(url)
    except OSError:
        pass  # 开不了浏览器不是故障：地址已经打在终端上


def main(argv: list[str] | None = None) -> int:
    """`python -m scanner.web_launcher [--port N] [--no-browser]`。"""
    import argparse

    parser = argparse.ArgumentParser(description="RTS 网页版启动台（控制面）")
    parser.add_argument("--port", type=int, default=None, help=f"端口（默认 {DEFAULT_PORT}）")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args(argv)

    if sys.platform == "win32":
        # 终端输出含 emoji（🖥/👋），GBK 控制台/重定向下直接 UnicodeEncodeError 崩掉 ——
        # 与 unified_scanner.py 同款处理（重定向场景可能无 reconfigure，静默跳过）。
        for stream in (sys.stdout, sys.stderr):
            _reconfigure = getattr(stream, "reconfigure", None)
            if callable(_reconfigure):
                _reconfigure(encoding="utf-8")

    try:
        srv = start_server(args.port)
    except OSError as e:
        # 端口被占：先问一句「是不是已经有一个启动台在跑」。
        # 双击两次不该变成报错 —— 而且 `pythonw` 下根本没有控制台给人看错误，
        # 用户只会觉得「双击没反应」。已有就把它那一页开出来（幂等），返回 0。
        url = f"http://{BIND_HOST}:{args.port or _port()}/"
        if _probe_launcher(url):
            print(f"  已有启动台在跑，直接打开：{url}")
            if not args.no_browser:
                _open_browser(url)
            return 0
        print(f"  ⛔ 启动台端口被占用（{type(e).__name__}: {e}）")
        print("     可能面板/终端占了这个端口。换端口：--port 8909")
        return 1
    url = f"http://{BIND_HOST}:{srv.server_address[1]}/"
    print(f"  🖥  网页版启动台: {url}  （控制面 · 仅本机 · Ctrl+C 退出）")
    if not args.no_browser:
        _open_browser(url)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\n  👋 启动台已停止（扫描器不受影响，按需在面板里继续）")
    finally:
        stop_server()
    return 0


def _run_guarded() -> int:
    """带崩溃落盘的入口：`pythonw`（双击路径）下 `sys.stderr` 是 `None`，
    异常会**静默消失** —— 用户只看到「双击没反应」。故把 traceback 写进
    `logs/web_launcher_crash.log`，并在无法打开浏览器时落一条可读原因。"""
    try:
        return main()
    except SystemExit:
        raise
    except BaseException:
        import traceback

        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            with open(os.path.join(LOG_DIR, "web_launcher_crash.log"), "a", encoding="utf-8") as f:
                f.write(_now_iso())
                f.write("\n")
                traceback.print_exc(file=f)
                f.write("\n")
        except OSError:
            pass
        raise


if __name__ == "__main__":
    sys.exit(_run_guarded())
