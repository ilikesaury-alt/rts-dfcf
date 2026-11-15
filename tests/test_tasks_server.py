"""动作面控制层守卫（2026-11-15）。

纪律：RTS_TASKS 缺省关 / 只绑回环 / 白名单 / 单槽串行 / exit code 原样 /
Host+Origin+Content-Type+token 四层防护 / N 档必须确认。零网络、零出网、不碰生产库。
"""

from __future__ import annotations

import time

import pytest

from scanner import local_panel_server, tasks_server


def test_tasks_disabled_by_default(monkeypatch):
    """RTS_TASKS 未设 / 设 0 → 开关恒关；设 1 才起，脏端口回退 8900。"""
    monkeypatch.delenv("RTS_TASKS", raising=False)
    assert tasks_server._tasks_enabled() == (False, 8900)
    monkeypatch.setenv("RTS_TASKS", "0")
    assert tasks_server._tasks_enabled()[0] is False
    monkeypatch.setenv("RTS_TASKS", "1")
    assert tasks_server._tasks_enabled() == (True, 8900)
    monkeypatch.setenv("RTS_TASKS_PORT", "abc")
    assert tasks_server._tasks_enabled() == (True, 8900)
    monkeypatch.setenv("RTS_TASKS_PORT", "9123")
    assert tasks_server._tasks_enabled() == (True, 9123)


def test_module_not_imported_when_disabled():
    """只读服务在开关关时不该在模块级 import tasks_server（只在反代函数内部）。"""
    src = open(local_panel_server.__file__, encoding="utf-8").read()
    assert 'TASKS_ENABLED = (os.environ.get("RTS_TASKS")' in src
    assert "if not TASKS_ENABLED:" in src
    assert "from scanner import tasks_server" in src
    assert "TASKS_ENABLED = " in src.split("from scanner import tasks_server")[0]
    assert "_proxy_tasks" in src  # 同源反代层，不是直接 dispatch


def test_catalog_whitelist_and_main_entry():
    payload = tasks_server.catalog_payload()
    by_id = {c["id"]: c for c in payload["items"]}
    assert "ai_research" in by_id and "ai_demo" in by_id and "ai_materials" in by_id
    assert "ai_research_brief" in by_id and "ai_research_limit3" in by_id
    for c in payload["items"]:
        assert c["argv"][0:2] == ["-m", "scanner.ai_digest"]
        assert c["cmd"].startswith("python -m scanner.ai_digest")
        assert c["tier"] in ("R", "N")
        if c["tier"] == "N":
            assert c["needsConfirm"] is True and c["outbound"] is True
    main = by_id["ai_research"]
    assert main["argv"] == ["-m", "scanner.ai_digest"]
    assert "归档" in main["note"]


def test_guard_rejects_bad_host_origin_ctype_token():
    """四层校验：拿假 handler 直接调 _Handler._guard。"""
    class FakeHeaders:
        def __init__(self, d): self.d = d
        def get(self, k): return self.d.get(k)

    class FakeServer:
        server_address = ("127.0.0.1", 8900)
        token = "testtoken"

    class Fake:
        server = FakeServer()
        def __init__(self, d): self.headers = FakeHeaders(d)
        @property
        def _token(self): return self.server.token

    h = tasks_server._Handler
    base = {"Host": "127.0.0.1:8900", "Content-Type": "application/json",
            "X-RTS-Task-Token": "testtoken"}

    h._guard(Fake(base))  # 合法（无 Origin）不抛

    for bad in (
        {**base, "Host": "evil.example:8900"},
        {**base, "Host": "127.0.0.1:9999"},
        {**base, "Origin": "http://evil.example"},
        {**base, "Origin": "http://127.0.0.1:9999"},
        {**base, "Content-Type": "text/plain"},
        {**base, "X-RTS-Task-Token": "wrong"},
    ):
        with pytest.raises(tasks_server._HttpError) as e:
            h._guard(Fake(bad))
        assert e.value.status == 403


def test_unknown_catalog_rejected():
    sup = tasks_server.TaskSupervisor()
    with pytest.raises(tasks_server._HttpError) as e:
        sup.submit("rm -rf /", confirm=False)
    assert e.value.code == "BAD_REQUEST"
    sup.shutdown()


def test_demo_is_network_tier():
    """--demo 只跳过读库，仍走 research 工具链 ⇒ 必须确认、标 N 档（实测 2026-11-15）。"""
    by_id = {c["id"]: c for c in tasks_server.catalog_payload()["items"]}
    demo = by_id["ai_demo"]
    assert demo["tier"] == "N" and demo["outbound"] is True and demo["needsConfirm"] is True
    assert by_id["ai_materials"]["tier"] == "R"


def test_confirm_required_for_network_tier():
    sup = tasks_server.TaskSupervisor()
    with pytest.raises(tasks_server._HttpError) as e:
        sup.submit("ai_research", confirm=False)
    assert e.value.code == "TASK_CONFIRM_REQUIRED"
    sup.shutdown()


def test_args_override_rejected():
    sup = tasks_server.TaskSupervisor()
    with pytest.raises(tasks_server._HttpError):
        sup.submit("ai_materials", confirm=False, args={"argv": ["--evil"]})
    sup.shutdown()


def test_single_slot_serial_and_exit_code(monkeypatch, tmp_path):
    """两条 R 档任务顺序执行，第二条不得抢跑；exit 0 原样保留。"""
    started: list[float] = []
    finished: list[float] = []

    def fake_popen(*args, **kwargs):
        class P:
            returncode = 0
            pid = 1
            def __init__(self):
                self._t = time.monotonic()
                started.append(time.time())
            def poll(self):
                # supervisor 用 poll() 判定（不是 wait）：0.2s 后报退出
                if time.monotonic() - self._t < 0.2:
                    return None
                finished.append(time.time())
                return 0
            def wait(self, timeout=None): return 0
            def terminate(self): pass
            def kill(self): pass
        return P()

    sup = tasks_server.TaskSupervisor(popen=fake_popen, cwd=str(tmp_path))
    a = sup.submit("ai_materials", confirm=False)
    b = sup.submit("ai_materials", confirm=False)
    deadline = time.time() + 4
    while time.time() < deadline:
        sa = sup.status(a["id"]); sb = sup.status(b["id"])
        if sa["state"] in ("ok", "failed") and sb["state"] in ("ok", "failed", "cancelled"):
            break
        time.sleep(0.02)
    assert len(started) >= 2
    assert started[1] >= finished[0], "第二个任务在第一个结束前就启动了（不是单槽）"
    assert sup.status(a["id"])["state"] == "ok"
    assert sup.status(b["id"])["state"] == "ok"
    assert sup.status(a["id"])["exitCode"] == 0
    sup.shutdown()


def test_cancel_queued_task(tmp_path):
    gate = __import__("threading").Event()

    def fake_popen(*args, **kwargs):
        class P:
            returncode = 0
            pid = 1
            def wait(self, timeout=None): gate.wait(2); return 0
            def terminate(self): gate.set()
            def kill(self): pass
            def poll(self): return None
        return P()

    sup = tasks_server.TaskSupervisor(popen=fake_popen, cwd=str(tmp_path))
    sup.submit("ai_materials", confirm=False)
    b = sup.submit("ai_materials", confirm=False)
    time.sleep(0.05)
    res = sup.cancel(b["id"])
    assert res["id"] == b["id"]
    gate.set()
    sup.shutdown()
