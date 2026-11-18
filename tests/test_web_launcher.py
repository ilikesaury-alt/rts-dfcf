"""网页版**启动台**（控制面）测试。

本模块的失败模式很特别：它是一个**能启动/结束本机进程的本地 HTTP 服务**，
所以「功能正常」只占一半，另一半是**它不会成为越权入口**。断言据此分两组：

A. 安全面（结构 + 行为）
   - 只绑回环，且绑定地址**没有** env 覆盖路径；
   - 四层校验（Host / Origin / Content-Type / 一次性 token）逐条真的会拒；
   - 任何响应都不带 `Access-Control-Allow-*`（跨域连预检都过不去）；
   - 不代理任何面板数据（唯一的外呼是 `/api/meta` 探针 —— 代理会变成第二条写平面入口）。

B. 启停语义（用户决策 2026-11-17「已有实例就拒绝」）
   - 锁被占 ⇒ `ALREADY_RUNNING`，**一个进程都不许拉**（不杀旧实例）；
   - 子进程 argv 里**没有** `--no-lock`、不经 shell；
   - 停止只对自己拉起的那一个生效。
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from scanner import web_launcher
from scanner.web_launcher import ScannerSupervisor

SRC = Path(web_launcher.__file__).read_text(encoding="utf-8")


# ── A. 安全面：结构断言 ────────────────────────────────────────────────


def test_bind_host_is_loopback_and_not_env_controlled():
    """误绑 `0.0.0.0` = 同网段任何人能启停你的扫描器。"""
    assert web_launcher.BIND_HOST == "127.0.0.1"
    assert "BIND_HOST" in SRC
    # 绑定地址不得走 env：只有端口允许覆盖。
    assert not re.search(r"BIND_HOST\s*=\s*os\.environ", SRC)


def test_console_port_never_collides_with_panel_or_tasks():
    """启动台自带一个口，**不占用**面板/动作面的口（否则会互相挤掉、且 Origin 对不上）。"""
    from scanner.config import LOCAL_PANEL_PORT

    assert web_launcher.DEFAULT_PORT not in (LOCAL_PANEL_PORT, web_launcher.TASKS_PORT_DEFAULT)


def test_child_is_spawned_without_shell_and_without_no_lock():
    """不过 shell（注入面归零）+ 不注入 `--no-lock`（单实例锁是唯一互斥保障）。"""
    assert "shell=True" not in SRC
    assert "os.system" not in SRC
    assert '"--no-lock"' not in SRC and "'--no-lock'" not in SRC


def test_only_loopback_probes_are_outbound():
    """不代理面板数据：外呼**只有两个回环探针**（都 `ProxyHandler({})` 绕代理）。

    * `/api/meta`：扫描器面板是否就绪；
    * `/api/console/health`：这个端口上是否已经跑着一个启动台（双击两次要幂等）。

    任何第三个外呼、任何「把 `/api/*` 原样转发给 8899」的代码都会让本守卫红 ——
    反代会把动作面（能触发本机命令）多开一个入口，见模块 docstring。
    """
    calls = re.findall(r"opener\.open\((.*?), timeout=", SRC)
    assert len(calls) == 2, f"外呼点必须恰好这两处（找到 {len(calls)} 处）"
    joined = " ".join(calls)
    assert "/api/meta" in joined and "/api/console/health" in joined
    assert 'BIND_HOST' in joined, "探针必须打回环（拼 BIND_HOST），不允许出现外部主机"


def test_probe_bypasses_system_proxy():
    """回环探针绝不走代理：Windows 上 urllib 吃注册表系统代理（实测 Clash :7890），
    探针被路由进代理后永远探不到 ready（2026-11-17 冒烟实测踩坑）。"""
    assert "ProxyHandler({})" in SRC, "就绪探针必须显式 ProxyHandler({}) 绕过系统代理"


def test_child_env_forces_panel_but_respects_explicit_task_switch(monkeypatch):
    """`RTS_LOCAL_PANEL` 强制开（启动台的立身之本）；`RTS_TASKS` 尊重用户显式关闭。"""
    monkeypatch.setenv("RTS_TASKS", "0")
    env = ScannerSupervisor().build_child_env()
    assert env["RTS_LOCAL_PANEL"] == "1"
    assert env["RTS_TASKS"] == "0", "用户显式 RTS_TASKS=0 不得被覆盖"
    monkeypatch.delenv("RTS_TASKS")
    assert ScannerSupervisor().build_child_env()["RTS_TASKS"] == "1"


# ── A. 安全面：行为断言（真起服务） ────────────────────────────────────


@pytest.fixture()
def console(monkeypatch):
    """起一个真的启动台（临时端口），且**不碰**真实的单实例锁与子进程。"""
    sup = ScannerSupervisor()
    monkeypatch.setattr(sup, "_external_running", lambda: False)
    srv = web_launcher.start_server(0, supervisor=sup)
    port = srv.server_address[1]
    try:
        yield sup, port
    finally:
        web_launcher.stop_server()


def _req(port: int, path: str, *, method: str = "GET", headers: dict | None = None, body: bytes | None = None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, data=body)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:  # noqa: S310 —— 只打本机回环
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:  # 4xx 也是断言对象
        return e.code, dict(e.headers), e.read()


def test_health_hands_out_token_and_no_cors_headers(console):
    _, port = console
    status, headers, body = _req(port, "/api/console/health")
    assert status == 200
    data = json.loads(body)["data"]
    assert data["token"], "token 必须下发（前端 POST 靠它）"
    assert headers.get("X-RTS-Console-Token") == data["token"]
    assert not any(k.lower().startswith("access-control-") for k in headers), "不得下发任何 CORS 头"


def test_page_is_served_from_the_console_origin(console):
    _, port = console
    status, headers, body = _req(port, "/")
    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    assert "启动扫描" in body.decode("utf-8")


def test_post_without_token_is_rejected(console):
    sup, port = console
    status, _, body = _req(
        port, "/api/console/start", method="POST", headers={"Content-Type": "application/json"}, body=b"{}"
    )
    assert status == 403
    assert json.loads(body)["error"]["code"] == "FORBIDDEN_ORIGIN"


def test_rejected_post_still_returns_clean_403_with_body_in_flight(console):
    """拒绝请求时必须**先把请求体排干**再回 403。

    回归：2026-11-17 全量 pytest 才撞上 —— 校验在读书之前就 403，服务端关连接而
    客户端还在写 body ⇒ Windows 抛 `ConnectionAbortedError (10053)`，
    测试看到的是「连接被重置」而不是那枚 403（单跑不撞、跑全量就撞）。
    这里刻意送一个 2KB 的 body 放大这个竞态窗口。
    """
    _, port = console
    big = b'{"padding":"' + b"x" * 2000 + b'"}'
    status, _, body = _req(
        port, "/api/console/start", method="POST", headers={"Content-Type": "application/json"}, body=big
    )
    assert status == 403
    assert json.loads(body)["error"]["code"] == "FORBIDDEN_ORIGIN"


def test_post_with_foreign_origin_is_rejected(console):
    sup, port = console
    token = json.loads(_req(port, "/api/console/health")[2])["data"]["token"]
    status, _, _ = _req(
        port,
        "/api/console/start",
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-RTS-Console-Token": token,
            "Origin": "http://evil.example.com",
        },
        body=b"{}",
    )
    assert status == 403, "别家网页发起的请求必须被 Origin 挡住"


def test_post_without_json_content_type_is_rejected(console):
    _, port = console
    token = json.loads(_req(port, "/api/console/health")[2])["data"]["token"]
    status, _, _ = _req(
        port,
        "/api/console/start",
        method="POST",
        headers={"Content-Type": "text/plain", "X-RTS-Console-Token": token},
        body=b"{}",
    )
    assert status == 403


def test_status_reports_idle_without_spawning_anything(console):
    _, port = console
    status, _, body = _req(port, "/api/console/status")
    assert status == 200
    data = json.loads(body)["data"]
    assert data["state"] == "idle"
    assert data["ownProcess"] is False
    assert data["panelUrl"].startswith("http://127.0.0.1:")
    assert data["consolePort"] == port


# ── B. 启停语义 ────────────────────────────────────────────────────────


def test_start_refuses_when_lock_is_held_by_someone_else(monkeypatch):
    """「已有实例就拒绝」：必须抛 409，且**一个进程都不许拉**。"""
    sup = ScannerSupervisor()
    monkeypatch.setattr(sup, "_external_running", lambda: True)
    spawned: list[object] = []
    monkeypatch.setattr(web_launcher.subprocess, "Popen", lambda *a, **k: spawned.append(a) or None)

    with pytest.raises(web_launcher._HttpError) as exc:
        sup.start()
    assert exc.value.status == 409
    assert exc.value.code == "ALREADY_RUNNING"
    assert spawned == [], "拒绝接管时不得启动任何子进程（更不能杀旧实例）"


def test_stop_refuses_to_kill_external_instance(monkeypatch):
    """外部实例（终端那个）只提示、不代杀。"""
    sup = ScannerSupervisor()
    monkeypatch.setattr(sup, "_external_running", lambda: True)
    with pytest.raises(web_launcher._HttpError) as exc:
        sup.stop()
    assert exc.value.code == "EXTERNAL_INSTANCE"


def test_start_then_stop_roundtrip_with_fake_child(monkeypatch, tmp_path):
    """启停往返（假子进程）：锁内不得再取锁 —— 曾在此处自死锁（start→state→lock）。"""
    sup = ScannerSupervisor()
    monkeypatch.setattr(sup, "_external_running", lambda: False)
    monkeypatch.setattr(sup, "_panel_ready", lambda: True)  # 不探真端口
    sup._log_path = tmp_path / "console_child.log"
    sup._log_path.write_bytes(b"")

    class _FakeChild:
        pid = 7
        returncode = None
        terminated = False

        def poll(self):
            return 0 if self.terminated else None

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            return 0

        def kill(self):
            self.terminated = True

    fake = _FakeChild()
    monkeypatch.setattr(web_launcher.subprocess, "Popen", lambda *a, **k: fake)

    st = sup.start()
    assert st["state"] == "running" and st["pid"] == 7
    st = sup.stop()
    assert st["state"] == "idle" and st["ownProcess"] is False


def test_start_argv_has_no_no_lock_and_uses_current_interpreter(monkeypatch, tmp_path):
    """spawn 的 argv：当前解释器 + 扫描器脚本，**无** `--no-lock`。"""
    sup = ScannerSupervisor()
    monkeypatch.setattr(sup, "_external_running", lambda: False)
    monkeypatch.setattr(web_launcher, "LOG_DIR", str(tmp_path))
    sup._log_path = tmp_path / "console_child.log"

    seen: dict[str, object] = {}

    class _FakeChild:
        pid = 4242
        returncode = None

        def poll(self):
            return None

    def _fake_popen(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return _FakeChild()

    monkeypatch.setattr(web_launcher.subprocess, "Popen", _fake_popen)
    sup.start()
    argv = seen["argv"]
    assert isinstance(argv, list)
    assert argv[0].endswith(("python.exe", "python")) or "python" in argv[0]
    assert argv[1].endswith("unified_scanner.py")
    assert "--no-lock" not in argv
    assert seen["kwargs"].get("shell") is not True
    env = seen["kwargs"]["env"]
    assert env["RTS_LOCAL_PANEL"] == "1"
