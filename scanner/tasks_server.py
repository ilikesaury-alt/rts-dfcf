"""桌面版 **AI 分析任务控制面**：独立 HTTP 服务（2026-11-15，P0 最小闭环）。

与 `local_panel_server.py`（只读数据面）**物理隔离**：本模块只在 `RTS_TASKS=1`
时由 `unified_scanner.py` 局部导入并起后台线程，默认不导入、不监听。
任务执行的唯一入口恒为 `python -m scanner.ai_digest` 系列（catalog 白名单），
`pick.py --clipboard` / 云端 `/api/ai-explain` 都不进本目录。

端点（信封 `{ok:true,data}` / `{ok:false,error:{code,message}}`）：

| 方法 / 路径 | 作用 |
|---|---|
| `GET  /api/tasks/health`   | 存活探针（不碰 DB、不 spawn） |
| `GET  /api/tasks/catalog`  | 白名单命令目录（服务端单源） |
| `POST /api/tasks/submit`   | 提交任务；N 档需 `confirm.acknowledged:true` |
| `GET  /api/tasks/status?id=` | 单任务状态 |
| `GET  /api/tasks/log?id=`  | 增量 stdout/stderr 日志 |
| `POST /api/tasks/cancel?id=` | 取消（queued 直接出队 / running terminate→kill） |

安全（四层，POST/GET 一律校验）：
- 只绑 `127.0.0.1`（`BIND_HOST` 模块常量，无 env 覆盖）；端口 `RTS_TASKS_PORT`
  默认 8900，脏值回退；非回环地址即拒绝 bind。
- `Host` 必须逐字 `127.0.0.1:<port>`；`Origin` 有则必须精确等于 `http://127.0.0.1:<port>`
  （无 Origin 放行：Electron 同源请求可带可不带）；`Content-Type` 必须是 `application/json`；
  一次性启动 token 由 `GET /api/tasks/health` 的 `X-RTS-Task-Token` 响应头下发
  （前端从响应头读，不写死在页面里）。任何请求都不返回 `Access-Control-Allow-*`。
- 不读 `.env` 的值、不打印任何密钥（token 只留内存，不进日志/任务 env/响应体）。

执行体 = `subprocess.Popen([sys.executable, "-m", "scanner.ai_digest", ...])`，
`cwd=仓库根`、**继承扫描器进程 env**（`os.environ.copy()` —— `RTS_DB_PATH` /
`RTS_LOG_DIR` / `RTS_ENV_FILE` / `PYTHONNOUSERSITE` / `MAGPIE_*` 逐项对齐），
stdout/stderr 只落 `logs/tasks/<id>/`，不回灌扫描器终端。
**单槽串行**（一个 `exclusive_slot` + 一条 deque 队列，N/R 共用，绝不并发 spawn），
exit code 原样保留、不重试；超时 `terminate()`→5s→`kill()`；历史保留最近 N 条。

⚠ 取舍：任务服务在**不同端口**（8900），前端只能打同源相对路径 ⇒
`local_panel_server.py` 对 `/api/tasks/*` 做**同源反向代理**（纯转发、不执行任务）
到本服务，物理隔离仍在。代价是多一次本机 HTTP 跳转；收益是只读数据面的 4 个 GET
路由分支与函数体一字不动，且任务 env 只从扫描器进程继承一次，不用在只读服务里
重建一份 env 净化。
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from scanner.config import BASE_DIR, LOCAL_PANEL_PORT, LOG_DIR

# ⛔ 安全硬约束：只绑回环（误绑 0.0.0.0 = 同网段任何人能触发本机命令）。
BIND_HOST = "127.0.0.1"
DEFAULT_PORT = 8900
#: 脏值/越界回退到 DEFAULT_PORT。
_MIN_PORT, _MAX_PORT = 1, 65535
#: 反向代理（local_panel_server）用这条私有头把**原始** Host/Origin 带给本服务：
#: urllib/http.client 会把代理请求自己的 Host 重写成 `127.0.0.1:8900`，故 L1/L2
#: 不能只看 HTTP 的 Host 头。代理只在 127.0.0.1 同机转发，且该头由只读服务
#: 覆写（不接受客户端自带值），不会被外部网页伪造。
_ORIGINAL_HOST_HEADER = "X-RTS-Original-Host"
_ORIGINAL_ORIGIN_HEADER = "X-RTS-Original-Origin"


def _allowed_hosts(tasks_port: int) -> frozenset[str]:
    """合法 Host 集合：任务端口自身 + 本地面板端口（均可被 env 覆盖）。

    代理过来的请求，Host 是浏览器发给**只读服务**的那个（`127.0.0.1:8899`），
    不是 8900 ⇒ L1 必须把面板端口也算作合法。两个端口都从 env 现算，
    避免 `RTS_LOCAL_PANEL_PORT` / `RTS_TASKS_PORT` 覆盖后守卫误拒。
    """
    panel_port = LOCAL_PANEL_PORT
    try:
        candidate = int((os.environ.get("RTS_LOCAL_PANEL_PORT") or "").strip())
    except ValueError:
        candidate = 0
    if 1 <= candidate <= 65535:
        panel_port = candidate
    return frozenset({f"{BIND_HOST}:{panel_port}", f"{BIND_HOST}:{tasks_port}"})
#: 单次请求体上限（catalog/submit/cancel 都只收一个 JSON 小对象）。
MAX_BODY_BYTES = 16 * 1024
#: 日志增量轮询的单次上限。
MAX_LOG_CHUNK = 65_536
#: 任务历史保留条数（meta 不可删，供孤儿清扫与审计）。
MAX_HISTORY = 200
#: 取消/超时时 terminate 后等待进程退出的秒数。
_KILL_GRACE = 5.0
#: supervisor 轮询间隔。
_TICK = 0.1

_log_name = "scanner.tasks_server"


def _now() -> str:
    """ISO 北京时间字符串（仅展示，不参与任何口径判定）。"""
    from scanner.config import now_beijing

    return now_beijing().isoformat(timespec="seconds")


def _archive_dir() -> str:
    """AI 分析主入口的归档目录（「结果在哪」回显；只回显路径，不读报告内容）。

    `archive.DEFAULT_ARCHIVE_DIR` 是相对 `cwd`（仓库根）的 `logs/ai_digest`，
    与 `RTS_LOG_DIR` 在打包态可能不一致 ⇒ 这里按 **ai_digest 自己的解析**
    给相对路径字面量，不猜绝对路径。
    """
    from scanner.ai_digest.archive import DEFAULT_ARCHIVE_DIR

    return str(DEFAULT_ARCHIVE_DIR).replace("\\", "/") + "/YYYY-MM-DD/"


# ── catalog（服务端唯一真源；前端只渲染，不硬编码命令） ─────────────────────

#: R 档 = 零出网零库（按 2026-11-15 口径）；N 档 = 出网 + 耗 MAGPIE_* 凭据，
#: 必须显式确认。⚠ 说明文案只写执行事实，不含结论词（grep 守卫会扫 web/src）。
_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "id": "ai_demo",
        "label": "内置样例链路核对",
        "argv": ["-m", "scanner.ai_digest", "--demo"],
        "tier": "N",
        "outbound": True,
        "timeout": 300,
        "needsConfirm": True,
        "note": "内置样例跳过读库，但仍走 research 工具链（会联网、耗 MAGPIE_* 凭据）；不写库、不归档文件。",
    },
    {
        "id": "ai_materials",
        "label": "提示词素材核对",
        "argv": ["-m", "scanner.ai_digest", "--materials"],
        "tier": "R",
        "outbound": False,
        "timeout": 60,
        "needsConfirm": False,
        "note": "只打印模型会收到的提示词与素材；零出网、零写库、零归档。",
    },
    {
        "id": "ai_research",
        "label": "AI 分析 · 主入口",
        "argv": ["-m", "scanner.ai_digest"],
        "tier": "N",
        "outbound": True,
        "timeout": 300,
        "needsConfirm": True,
        "note": "默认 research：模型自查真实数据；报告自动归档到 logs/ai_digest/YYYY-MM-DD/；出网并消耗 MAGPIE_* 凭据。",
    },
    {
        "id": "ai_research_brief",
        "label": "AI 分析 · 一屏报告",
        "argv": ["-m", "scanner.ai_digest", "--brief"],
        "tier": "N",
        "outbound": True,
        "timeout": 300,
        "needsConfirm": True,
        "note": "主入口的一屏报告档；出网并消耗 MAGPIE_* 凭据，报告自动归档。",
    },
    {
        "id": "ai_research_limit3",
        "label": "AI 分析 · 每区前 3 只",
        "argv": ["-m", "scanner.ai_digest", "--limit", "3"],
        "tier": "N",
        "outbound": True,
        "timeout": 300,
        "needsConfirm": True,
        "note": "带参数档：每区只取前 3 只（按区截断）；出网并消耗 MAGPIE_* 凭据。",
    },
    {
        "id": "ai_research_no_archive",
        "label": "AI 分析 · 不自动归档",
        "argv": ["-m", "scanner.ai_digest", "--no-archive"],
        "tier": "N",
        "outbound": True,
        "timeout": 300,
        "needsConfirm": True,
        "note": "主入口但不写自动档案；出网并消耗 MAGPIE_* 凭据，报告只回显在任务日志里。",
    },
)

_CATALOG_BY_ID = {c["id"]: c for c in _CATALOG}


def catalog_payload() -> dict[str, Any]:
    """目录响应体：`argv` 里的 `-m scanner.ai_digest` 由服务端固定，前端不可覆盖。"""
    return {
        "items": [
            {
                "id": c["id"],
                "label": c["label"],
                "argv": list(c["argv"]),
                "cmd": "python " + " ".join(c["argv"]),
                "tier": c["tier"],
                "outbound": c["outbound"],
                "timeout": c["timeout"],
                "needsConfirm": c["needsConfirm"],
                "note": c["note"],
            }
            for c in _CATALOG
        ],
        "archiveDir": _archive_dir(),
    }


# ── 信封（与 local_panel_server 同形，但本服务自己一份，不 import 只读面） ──

def _ok(data: Any) -> dict:
    return {"ok": True, "data": data}


def _err(code: str, message: str) -> dict:
    return {"ok": False, "error": {"code": code, "message": message}}


class _TaskState:
    """一条任务的全部可变状态（只在 supervisor 锁内读写）。"""

    def __init__(self, task_id: str, catalog_id: str, spec: dict[str, Any], *, confirmed: bool):
        self.id = task_id
        self.catalog_id = catalog_id
        self.spec = spec
        self.state = "queued"  # queued | running | ok | failed | cancelled
        self.confirmed = confirmed
        self.created_at = _now()
        self.started_at: str | None = None
        self.finished_at: str | None = None
        self.exit_code: int | None = None
        self.proc: subprocess.Popen | None = None
        self.started_monotonic: float | None = None
        self.timed_out = False
        self.error: dict[str, str] | None = None
        self.dir = os.path.join(LOG_DIR, "tasks", task_id)
        self.stdout_path = os.path.join(self.dir, "stdout.log")
        self.stderr_path = os.path.join(self.dir, "stderr.log")
        self._stdout: Any = None

    def view(self) -> dict[str, Any]:
        """`TaskView`：不含看板载荷的任何键（schema/regions/ctx/stocks/gate/cols）。"""
        out: dict[str, Any] = {
            "id": self.id,
            "catalogId": self.catalog_id,
            "command": "python " + " ".join(self.spec["argv"]),
            "tier": self.spec["tier"],
            "state": self.state,
            "createdAt": self.created_at,
            "startedAt": self.started_at,
            "finishedAt": self.finished_at,
            "exitCode": self.exit_code,
            "timeout": self.spec["timeout"],
            "confirmed": self.confirmed,
            "artifacts": [],
            "logDir": self.dir.replace("\\", "/"),
            "archiveDir": _archive_dir() if self.spec["tier"] == "N" else None,
            "error": self.error,
        }
        return out


class TaskSupervisor:
    """单槽串行任务管理器（daemon 线程；不参与主循环、不碰 DB、不碰终端）。"""

    def __init__(self, *, popen: Any = subprocess.Popen, env: dict[str, str] | None = None, cwd: str | None = None):
        # 依赖可注入：测试用假 Popen 验证「串行 / exit code / 超时」而不 spawn 真进程。
        self._popen = popen
        self._env = env if env is not None else dict(os.environ)
        self._cwd = cwd or BASE_DIR
        self._lock = threading.Lock()
        self._tasks: dict[str, _TaskState] = {}
        self._order: deque[str] = deque()
        self._running: _TaskState | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="tasks-supervisor", daemon=True)
        self._thread.start()

    # ── 对外 API（handler 调用） ───────────────────────────────────────────

    def submit(self, catalog_id: str, *, confirm: bool, args: Any = None) -> dict[str, Any]:
        """入队一条任务；返回 `TaskView`（含 `queued`）。`args` 刻意不消费：
        catalog 首版 argv 全固定，前端传任何参数都不得拼进命令（防注入）。"""
        spec = _CATALOG_BY_ID.get(catalog_id)
        if spec is None:
            raise _HttpError(400, "BAD_REQUEST", "未知 catalogId")
        if args:
            raise _HttpError(400, "BAD_REQUEST", "首版命令不接受参数覆盖")
        if spec["needsConfirm"] and not confirm:
            raise _HttpError(400, "TASK_CONFIRM_REQUIRED", "N 档任务必须显式确认")
        with self._lock:
            self._prune_history()
            task_id = time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
            task = _TaskState(task_id, catalog_id, spec, confirmed=confirm)
            self._tasks[task_id] = task
            self._order.append(task_id)
            return task.view()

    def status(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise _HttpError(404, "NOT_FOUND", "任务不存在")
            return task.view()

    def log(self, task_id: str, *, offset: int = 0, limit: int = MAX_LOG_CHUNK) -> dict[str, Any]:
        """增量轮询：**单一字节流**（stdout 与 stderr 合并写进 stdout.log）。

        ⚠ 为什么合并：两个各自增长的文件没法共享一个稳定的字节 offset（stdout 每多
        一行，stdout||stderr 的拼接边界就往后移，续拉会重复/漏行）。`subprocess.STDOUT`
        把 stderr 折进同一条流，offset 才是确定的。`stderr.log` 仍建在目录下作空占位，
        保持「stdout/stderr 都落在 logs/tasks/<id>/」的落点约定。
        """
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise _HttpError(404, "NOT_FOUND", "任务不存在")
        if offset < 0 or limit < 0:
            raise _HttpError(400, "BAD_REQUEST", "offset/limit 必须 >= 0")
        limit = min(limit, MAX_LOG_CHUNK)
        try:
            with open(task.stdout_path, "rb") as f:
                f.seek(min(offset, os.path.getsize(task.stdout_path)))
                raw = f.read(limit)
        except OSError:
            raw = b""  # 日志尚未落盘：返回空增量，下一轮再拉
        return {
            "offset": offset,
            "nextOffset": offset + len(raw),
            "eof": task.state in ("ok", "failed", "cancelled"),
            "lines": raw.decode("utf-8", errors="replace").splitlines(),
        }

    def cancel(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise _HttpError(404, "NOT_FOUND", "任务不存在")
            if task.state in ("ok", "failed", "cancelled"):
                raise _HttpError(409, "TASK_STATE_CONFLICT", "任务已处于终态")
            if task.state == "queued":
                task.state = "cancelled"
                task.finished_at = _now()
                self._drop_from_queue(task_id)
                return task.view()
            # running：交由 supervisor 线程做 terminate→kill（避免跨线程 Popen 调用）
            task._cancel_requested = True  # type: ignore[attr-defined]
            return task.view()

    def shutdown(self) -> None:
        """关停：收掉仍在跑的任务（terminate→kill），不留 running 残留。"""
        self._stop.set()
        self._thread.join(timeout=2.0)
        with self._lock:
            running, self._running = self._running, None
            queued = [self._tasks[i] for i in self._order if i in self._tasks]
        for task in queued:
            with self._lock:
                if task.state == "queued":
                    task.state = "cancelled"
                    task.finished_at = _now()
        if running is not None:
            self._kill(running)
            with self._lock:
                running.state = "cancelled"
                running.finished_at = _now()
                running.error = {"code": "TASK_CANCELLED", "message": "关停时任务已回收"}
                self._close_files(running)

    # ── supervisor 线程 ───────────────────────────────────────────────────

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._tick()
            self._stop.wait(_TICK)

    def _tick(self) -> None:
        # ① 回收已退出的 running
        with self._lock:
            running = self._running
        if running is not None:
            self._check_running(running)
        # ② 单槽空闲则把队首升 running（绝不并发 spawn）
        with self._lock:
            if self._running is not None:
                return
            while self._order:
                candidate = self._tasks.get(self._order[0])
                if candidate is None or candidate.state != "queued":
                    self._order.popleft()
                    continue
                break
            else:
                return
            self._order.popleft()
            self._running = candidate
        self._spawn(candidate)

    def _check_running(self, task: _TaskState) -> None:
        proc = task.proc
        cancel_requested = bool(getattr(task, "_cancel_requested", False))
        timed_out = (
            proc is not None
            and task.started_monotonic is not None
            and time.monotonic() - task.started_monotonic > task.spec["timeout"]
        )
        if proc is not None and proc.poll() is None and not cancel_requested and not timed_out:
            return
        if proc is not None and proc.poll() is None:
            if timed_out:
                task.timed_out = True
            self._kill(task)
        code = proc.poll() if proc is not None else None
        with self._lock:
            if self._running is task:
                self._running = None
            if task.state in ("ok", "failed", "cancelled"):
                return
            if cancel_requested:
                task.state = "cancelled"
                task.error = {"code": "TASK_CANCELLED", "message": "任务已取消"}
            elif task.timed_out:
                task.state = "failed"
                task.exit_code = code if code is not None else -1
                task.error = {"code": "TASK_TIMEOUT", "message": "任务超时已回收"}
            elif code == 0:
                task.state = "ok"
                task.exit_code = 0
            else:
                task.state = "failed"
                task.exit_code = code if code is not None else -1
            task.finished_at = _now()
            self._close_files(task)

    def _spawn(self, task: _TaskState) -> None:
        argv = [sys.executable, *task.spec["argv"]]
        try:
            os.makedirs(task.dir, exist_ok=True)
            out = open(task.stdout_path, "wb")  # noqa: SIM115 - 生命周期由 supervisor 管
            # stderr 折进 stdout 同一条流（见 `log()` 的偏移说明）；空文件保留落点约定。
            with open(task.stderr_path, "wb"):
                pass
        except OSError as e:
            with self._lock:
                task.state = "failed"
                task.exit_code = -1
                task.finished_at = _now()
                task.error = {"code": "TASK_LOG_FAILED", "message": f"日志落盘失败：{type(e).__name__}"}
                if self._running is task:
                    self._running = None
            return
        task._stdout = out
        # ⚠ 只加非 Python 变量；env 其余全部继承扫描器进程（含 MAGPIE_* / RTS_DB_PATH）。
        child_env = dict(self._env)
        child_env["RTS_TASK_ID"] = task.id
        child_env["RTS_TASK_DIR"] = task.dir
        # 进程组：Windows 下 CREATE_NEW_PROCESS_GROUP 让取消能整组收。
        kwargs: dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        try:
            proc = self._popen(
                argv,
                cwd=self._cwd,
                env=child_env,
                stdout=out,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                **kwargs,
            )
        except OSError as e:
            with self._lock:
                task.state = "failed"
                task.exit_code = -1
                task.finished_at = _now()
                task.error = {"code": "TASK_SPAWN_FAILED", "message": f"进程启动失败：{type(e).__name__}"}
                self._close_files(task)
                if self._running is task:
                    self._running = None
            return
        with self._lock:
            task.proc = proc
            task.state = "running"
            task.started_at = _now()
            task.started_monotonic = time.monotonic()

    def _kill(self, task: _TaskState) -> None:
        proc = task.proc
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
        except OSError:
            return
        try:
            proc.wait(timeout=_KILL_GRACE)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=_KILL_GRACE)
            except subprocess.TimeoutExpired:
                pass

    @staticmethod
    def _close_files(task: _TaskState) -> None:
        if task._stdout is not None:
            try:
                task._stdout.close()
            except OSError:
                pass
        task._stdout = None

    def _drop_from_queue(self, task_id: str) -> None:
        try:
            self._order.remove(task_id)
        except ValueError:
            pass

    def _prune_history(self) -> None:
        """只保留最近 MAX_HISTORY 条**终态**任务；running/queued 一律不动。"""
        terminal = [
            (t.created_at, i)
            for i, t in self._tasks.items()
            if t.state in ("ok", "failed", "cancelled")
        ]
        excess = len(terminal) - MAX_HISTORY
        if excess <= 0:
            return
        for _, i in sorted(terminal)[:excess]:
            self._tasks.pop(i, None)

    # ── 测试辅助（不在生产链路调用） ─────────────────────────────────────

    def running_count(self) -> int:
        with self._lock:
            return int(self._running is not None)

    def wait_terminal(self, task_id: str, timeout: float = 10.0) -> dict[str, Any]:
        """轮询到终态（测试与离线实测用；HTTP 链路靠前端自己轮询 status）。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            view = self.status(task_id)
            if view["state"] in ("ok", "failed", "cancelled"):
                return view
            time.sleep(_TICK)
        raise AssertionError(f"任务 {task_id} 在 {timeout}s 内未到终态：{view}")


class _HttpError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class TasksHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr: tuple[str, int], handler: type, *, supervisor: TaskSupervisor, token: str):
        super().__init__(addr, handler)
        self.supervisor = supervisor
        self.token = token

    def handle_error(self, request: Any, client_address: Any) -> None:
        """不把 traceback 打到扫描器终端（stdout 是主输出面）。"""
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, TimeoutError)):
            return
        # 其它异常静默降噪：日志已在 logs/tasks/ 与任务 error 里留痕。
        return


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "RTSTasks/1.0"

    @property
    def supervisor(self) -> TaskSupervisor:
        return getattr(self.server, "supervisor")  # type: ignore[attr-defined]

    @property
    def _token(self) -> str:
        return getattr(self.server, "token")  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        return  # 静音默认 stderr

    # ── 四层校验 ─────────────────────────────────────────────────────────

    def _guard(self, *, bootstrap: bool = False) -> None:
        """四层校验。`bootstrap` = `/api/tasks/health` 下发一次性 token 的那一次请求：
        Host/Origin/Content-Type 仍全查，唯独 token 豁免（否则 token 永远拿不到）。
        """
        port = int(self.server.server_address[1])  # type: ignore[attr-defined]
        allowed = _allowed_hosts(port) | {f"{BIND_HOST}:{port}"}
        # 代理覆写的原始值优先；直连时两头都缺省 ⇒ 退回 HTTP 自带的 Host/Origin。
        host = (self.headers.get(_ORIGINAL_HOST_HEADER) or self.headers.get("Host") or "").strip()
        if host not in allowed:
            raise _HttpError(403, "FORBIDDEN_ORIGIN", "Host 不匹配")
        origin = (self.headers.get(_ORIGINAL_ORIGIN_HEADER) or "").strip()
        if not origin:
            origin = (self.headers.get("Origin") or "").strip()
        if origin and origin not in {f"http://{h}" for h in allowed}:
            raise _HttpError(403, "FORBIDDEN_ORIGIN", "Origin 不匹配")
        # `bootstrap`（health）用前端 `apiGet` 裸调 ⇒ 不带 body、不带 Content-Type，
        # 也不可能有 token（这正是取 token 的那一次请求）。其余一律四条全查。
        if not bootstrap:
            ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if ctype != "application/json":
                raise _HttpError(403, "FORBIDDEN_ORIGIN", "Content-Type 必须为 application/json")
            got = (self.headers.get("X-RTS-Task-Token") or "").strip()
            if not got or not secrets.compare_digest(got, self._token):
                raise _HttpError(403, "FORBIDDEN_ORIGIN", "启动 token 无效")

    def _send_json(self, obj: Any, status: int = 200, *, token_header: bool = False) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if token_header:
            self.send_header("X-RTS-Task-Token", self._token)
        # ⚠ 刻意不返回任何 Access-Control-Allow-* 头：跨域请求连预检都过不去。
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

    def _dispatch(self, method: str) -> None:
        try:
            split = urlsplit(self.path)
            path = unquote(split.path)
            query = parse_qs(split.query)
            bootstrap = method == "GET" and path == "/api/tasks/health"
            self._guard(bootstrap=bootstrap)
            if method == "GET":
                result = self._get(path, query)
            else:
                result = self._post(path, query)
            self._send_json(result, 200, token_header=bootstrap)
        except _HttpError as e:
            self._send_json(_err(e.code, e.message), e.status)
        except (OSError, ValueError, KeyError, TypeError) as e:
            # 只 catch 外部/输入失败；编程错误冒泡到 handle_error（AGENTS.md 纪律）。
            self._send_json(_err("INTERNAL", f"服务内部错误：{type(e).__name__}"), 500)

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def _query_one(self, query: dict[str, list[str]], key: str) -> str:
        values = query.get(key) or []
        return values[0] if values else ""

    def _get(self, path: str, query: dict[str, list[str]]) -> dict[str, Any]:
        if path == "/api/tasks/health":
            # 一次性启动 token 同时走响应头与 `data`（前端 `apiGet` 只拿到 data）。
            # 它不写死在页面/静态产物里，进程退出即失效；不进 .env / 不进日志。
            return _ok(
                {
                    "alive": True,
                    "port": self.server.server_address[1],  # type: ignore[attr-defined]
                    "token": self._token,
                }
            )
        if path == "/api/tasks/catalog":
            return _ok(catalog_payload())
        if path == "/api/tasks/status":
            return _ok(self.supervisor.status(self._query_one(query, "id")))
        if path == "/api/tasks/log":
            raw_offset = self._query_one(query, "offset")
            try:
                offset = int(raw_offset) if raw_offset else 0
            except ValueError:
                raise _HttpError(400, "BAD_REQUEST", "offset 非法") from None
            return _ok(self.supervisor.log(self._query_one(query, "id"), offset=offset))
        raise _HttpError(404, "NOT_FOUND", "无此端点")

    def _post(self, path: str, query: dict[str, list[str]]) -> dict[str, Any]:
        body = self._read_body()
        if path == "/api/tasks/submit":
            catalog_id = body.get("catalogId")
            if not isinstance(catalog_id, str):
                raise _HttpError(400, "BAD_REQUEST", "catalogId 必填")
            confirm = body.get("confirm")
            acknowledged = bool(isinstance(confirm, dict) and confirm.get("acknowledged") is True)
            return _ok(self.supervisor.submit(catalog_id, confirm=acknowledged, args=body.get("args")))
        if path == "/api/tasks/cancel":
            return _ok(self.supervisor.cancel(self._query_one(query, "id") or str(body.get("id") or "")))
        raise _HttpError(404, "NOT_FOUND", "无此端点")


# ── 生命周期 ──────────────────────────────────────────────────────────────

_server: TasksHTTPServer | None = None
_server_lock = threading.Lock()


def _tasks_enabled() -> tuple[bool, int]:
    """`RTS_TASKS` 缺省关；端口脏值回退 8900。与 `RTS_LOCAL_PANEL` 独立。"""
    raw = (os.environ.get("RTS_TASKS") or "").strip().lower()
    if raw not in ("1", "true", "yes", "on"):
        return False, DEFAULT_PORT
    port = DEFAULT_PORT
    try:
        candidate = int((os.environ.get("RTS_TASKS_PORT") or "").strip())
    except ValueError:
        candidate = 0
    if _MIN_PORT <= candidate <= _MAX_PORT:
        port = candidate
    return True, port


def _resolve_port() -> tuple[bool, int]:
    return _tasks_enabled()


def start_server(port: int | None = None, *, supervisor: TaskSupervisor | None = None) -> TasksHTTPServer:
    """启动服务（幂等）。绑定失败原样上抛 `OSError`（由挂点 fail-open 告警，不阻断扫描）。

    只绑 `BIND_HOST`；`port` 由 `_tasks_enabled()` 解析（缺省关不在此处拦截 ——
    起服决定权在 `unified_scanner.py` 的挂点，这里只管「起了怎么跑」）。
    """
    global _server
    with _server_lock:
        if _server is not None:
            return _server
        if port is None:
            _, port = _tasks_enabled()
        # 0 = 让内核分配空闲端口（测试用）；负数/越界回退默认。
        if port < 0 or port > _MAX_PORT:
            port = DEFAULT_PORT
        token = secrets.token_urlsafe(32)
        sup = supervisor or TaskSupervisor()
        srv = TasksHTTPServer((BIND_HOST, port), _Handler, supervisor=sup, token=token)
        threading.Thread(target=srv.serve_forever, name="tasks-server", daemon=True).start()
        _server = srv
        return srv


def stop_server() -> None:
    """停服（幂等）：先收任务，再关 HTTP。"""
    global _server
    with _server_lock:
        srv, _server = _server, None
    if srv is not None:
        srv.supervisor.shutdown()
        srv.shutdown()
        srv.server_close()
