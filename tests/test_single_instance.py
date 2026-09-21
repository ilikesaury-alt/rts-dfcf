"""SingleInstanceLock 单元测试。

覆盖重点：**跨进程强制互斥** 与 **持有者被强杀后锁自动回收**。
后者是这个方案相对 pidfile 的唯一优势，必须由真实子进程验证 ——
在同进程里模拟不出来。
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import psutil
import pytest

from scanner.single_instance import (
    DEFAULT_LOCK_PATH,
    SingleInstanceError,
    SingleInstanceLock,
    _runs_script,
    stop_existing_scanners,
)

BASE_DIR = Path(__file__).resolve().parents[1]

# 子进程脚本：取锁 → 写哨兵文件 → 长眠。
# 用哨兵文件而不是 stdout 管道做同步，避免父进程 readline 阻塞导致的挂死。
_CHILD_HOLD = textwrap.dedent(
    """
    import os, sys, time
    sys.path.insert(0, r"{base}")
    from scanner.single_instance import SingleInstanceLock
    lock = SingleInstanceLock(r"{lock}")
    if not lock.acquire():
        open(r"{flag_fail}", "w").write("busy")
        sys.exit(3)
    open(r"{flag_ready}", "w").write(str(os.getpid()))
    time.sleep(120)
    """
)


def _spawn_holder(
    lock_path: Path, ready: Path, fail: Path, script_path: Path | None = None, replace: bool = False
) -> subprocess.Popen:
    script = _CHILD_HOLD.format(base=str(BASE_DIR), lock=str(lock_path), flag_ready=str(ready), flag_fail=str(fail))
    if replace:
        script = script.replace(
            "lock = SingleInstanceLock",
            "from pathlib import Path\n"
            "from scanner.single_instance import stop_existing_scanners\n"
            "stop_existing_scanners(Path(__file__))\n"
            "lock = SingleInstanceLock",
        )
    command = [sys.executable, "-c", script]
    if script_path is not None:
        script_path.write_text(script, encoding="utf-8")
        command = [sys.executable, str(script_path)]
    proc = subprocess.Popen(  # noqa: S603 - 固定命令 + 本地生成的脚本，无外部输入
        command,
        cwd=str(BASE_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        **({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}),
    )

    def _wait_for(path: Path, timeout: float) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if path.exists():
                return True
            if proc.poll() is not None:
                return path.exists()
            time.sleep(0.05)
        return path.exists()

    # 就绪窗口 90s：本判定只用于「区分挂死与慢」，失败仍由外层断言捕获。
    # 原值 30s 与实测耗时同量级，全量套件下曾偶发压线（2026-09-20）。
    if not _wait_for(ready, 90.0) and not fail.exists():
        polled = proc.poll()
        proc.kill()
        # 必须先 kill 再 wait 才能读完 stderr：子进程常驻 sleep(120)，
        # 直接 read() 会阻塞到它自然结束（或永久阻塞），报错里只会剩空串。
        proc.wait(timeout=15)
        err = proc.stderr.read().decode("utf-8", "ignore") if proc.stderr else ""
        # 带上诊断事实：poll() 为 None = 超时仍存活（慢/挂死）；非 None = 已自行退出。
        # 后者配合 stderr 可区分「取锁失败 exit 3」与「被外部 kill」。
        pytest.fail(
            f"子进程未能就绪：poll={polled} ready={ready.exists()} fail={fail.exists()} stderr={err!r}"
        )
    return proc


# --------------------------------------------------------------------- 基础语义


def test_default_lock_path():
    assert DEFAULT_LOCK_PATH.parts == ("logs", "scanner.lock")


def test_acquire_on_fresh_path(tmp_path):
    lock = SingleInstanceLock(tmp_path / "a.lock")
    assert lock.acquire() is True
    assert lock.held is True
    lock.release()
    assert lock.held is False


def test_pid_file_written_and_removed(tmp_path):
    lock_path = tmp_path / "a.lock"
    lock = SingleInstanceLock(lock_path)
    lock.acquire()
    pid_path = lock_path.with_name(lock_path.name + ".pid")
    assert pid_path.read_text(encoding="utf-8").strip() == str(os.getpid())
    assert lock.holder_pid() == os.getpid()
    assert str(os.getpid()) in lock.holder_description()
    lock.release()
    assert not pid_path.exists()
    assert lock.holder_pid() is None
    assert lock.holder_description() == ""


def test_lock_file_is_never_deleted(tmp_path):
    """锁文件必须保留 —— 删掉会让互斥在新旧 inode 上失效。"""
    lock_path = tmp_path / "a.lock"
    lock = SingleInstanceLock(lock_path)
    assert lock.acquire() is True
    lock.release()
    assert lock_path.exists(), "release() 删除了锁文件，互斥会失效"


def test_acquire_is_idempotent(tmp_path):
    """同一对象重复 acquire 不应把自己锁在门外。"""
    lock = SingleInstanceLock(tmp_path / "a.lock")
    assert lock.acquire() is True
    assert lock.acquire() is True
    lock.release()


def test_release_without_acquire_is_noop(tmp_path):
    SingleInstanceLock(tmp_path / "a.lock").release()  # 不抛异常即通过


def test_reacquire_after_release(tmp_path):
    lock_path = tmp_path / "a.lock"
    first = SingleInstanceLock(lock_path)
    assert first.acquire() is True
    first.release()
    second = SingleInstanceLock(lock_path)
    assert second.acquire() is True
    second.release()


def test_same_process_second_handle_is_rejected(tmp_path):
    """Windows 的字节范围锁按 fd 归属，同进程第二个 fd 同样被拒。"""
    lock_path = tmp_path / "a.lock"
    holder = SingleInstanceLock(lock_path)
    assert holder.acquire() is True
    assert SingleInstanceLock(lock_path).acquire() is False
    holder.release()


def test_context_manager_raises_when_busy(tmp_path):
    lock_path = tmp_path / "a.lock"
    holder = SingleInstanceLock(lock_path)
    holder.acquire()
    try:
        with pytest.raises(RuntimeError), SingleInstanceLock(lock_path):
            pass
    finally:
        holder.release()


def test_context_manager_releases(tmp_path):
    lock_path = tmp_path / "a.lock"
    with SingleInstanceLock(lock_path):
        assert SingleInstanceLock(lock_path).acquire() is False
    assert SingleInstanceLock(lock_path).acquire() is True


def test_lock_file_with_stale_content_is_reusable(tmp_path):
    """崩溃残留的空/脏锁文件不应永久拦住启动（O_CREAT 而非 O_EXCL）。"""
    lock_path = tmp_path / "a.lock"
    lock_path.write_bytes(b"")  # 模拟上次崩溃留下的空文件
    lock = SingleInstanceLock(lock_path)
    assert lock.acquire() is True
    lock.release()

    lock_path.write_text("stale-junk", encoding="utf-8")
    assert SingleInstanceLock(lock_path).acquire() is True


def test_creates_parent_directory(tmp_path):
    lock = SingleInstanceLock(tmp_path / "nested" / "deep" / "a.lock")
    assert lock.acquire() is True
    lock.release()


def test_broken_path_raises_not_silently_degrades(tmp_path):
    """锁文件打不开必须显式报错 —— 静默降级等于没有保护。"""
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x", encoding="utf-8")
    with pytest.raises(SingleInstanceError):
        SingleInstanceLock(blocker / "a.lock").acquire()


def test_independent_paths_do_not_conflict(tmp_path):
    a = SingleInstanceLock(tmp_path / "a.lock")
    b = SingleInstanceLock(tmp_path / "b.lock")
    assert a.acquire() is True
    assert b.acquire() is True
    a.release()
    b.release()


def test_custom_pid_path(tmp_path):
    custom_pid = tmp_path / "somewhere" / "holder.pid"
    lock = SingleInstanceLock(tmp_path / "a.lock", custom_pid)
    lock.acquire()
    assert lock.holder_pid() == os.getpid()
    lock.release()
    assert not custom_pid.exists()


# --------------------------------------------------------------------- 跨进程


def test_cross_process_mutual_exclusion(tmp_path):
    """真实子进程持锁时，父进程必须拿不到。"""
    lock_path = tmp_path / "a.lock"
    proc = _spawn_holder(lock_path, tmp_path / "ready", tmp_path / "fail")
    try:
        assert (tmp_path / "ready").exists(), "子进程加锁失败，互斥前提不成立"
        assert not (tmp_path / "fail").exists()
        parent = SingleInstanceLock(lock_path)
        assert parent.acquire() is False
    finally:
        proc.kill()
        proc.wait(timeout=15)


def test_lock_auto_released_when_holder_killed(tmp_path):
    """核心保证：持有者被 TerminateProcess 强杀（不走 atexit/finally），
    锁必须由内核回收，后续实例能立刻起来。"""
    lock_path = tmp_path / "a.lock"
    proc = _spawn_holder(lock_path, tmp_path / "ready", tmp_path / "fail")
    try:
        assert (tmp_path / "ready").exists(), "子进程未能持锁"
        lock = SingleInstanceLock(lock_path)
        assert lock.acquire() is False, "子进程持锁期间父进程竟取得锁"
    finally:
        proc.kill()  # 硬杀，不给任何清理机会
        proc.wait(timeout=15)

    deadline = time.time() + 15
    acquired = False
    while time.time() < deadline:
        if lock.acquire():
            acquired = True
            break
        time.sleep(0.1)
    assert acquired, "持有者被强杀后锁未释放 —— 会留下永久死锁"
    lock.release()


def test_real_restart_replaces_old_holder(tmp_path):
    """真实重启语义：新实例清掉旧持有者并接管锁。

    ⚠ 本用例是本文件唯一**真的**调 ``stop_existing_scanners`` 的测试，
    而该函数用 ``psutil.process_iter()`` 枚举全机进程、按
    ``(create_time, pid) < (当前进程)`` 判辈分后 kill。因此它会命中
    **任何 cwd/cmdline 匹配的更早进程** —— 本文件其它用例（如
    ``test_cross_process_mutual_exclusion`` 派生的持锁进程）也在候选集内。
    跑整个文件时它们已被上层 finally 清理，所以当前是安全的；
    但若新增「长驻的统一脚本名子进程」用例，请一并核对这里不会误杀。
    """
    script = tmp_path / "unified_scanner.py"
    lock_path = tmp_path / "restart.lock"
    old = _spawn_holder(lock_path, tmp_path / "old.ready", tmp_path / "old.fail", script)
    new = None
    try:
        assert (tmp_path / "old.ready").exists()
        new = _spawn_holder(lock_path, tmp_path / "new.ready", tmp_path / "new.fail", script, replace=True)
        assert (tmp_path / "new.ready").exists()
        assert not (tmp_path / "new.fail").exists()
        old.wait(timeout=15)
        assert old.poll() is not None
        lock = SingleInstanceLock(lock_path)
        assert lock.holder_pid() == new.pid
        assert not lock.acquire()
    finally:
        for process in (old, new):
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=15)


@pytest.mark.parametrize(
    ("arguments", "matches"),
    [
        (["unified_scanner.py"], True),
        (["./unified_scanner.py", "120", "--no-lock"], True),
        (["-u", "-X", "utf8", "unified_scanner.py"], True),
        (["--", "unified_scanner.py"], True),
        (["-m", "unified_scanner"], True),
        (["-c", "unified_scanner.py"], False),
        (["other.py", "unified_scanner.py"], False),
        (["../other/unified_scanner.py"], False),
        (["-m", "other", "unified_scanner.py"], False),
        (["-X"], False),
        ([], False),
    ],
)
def test_script_matching(tmp_path, arguments, matches):
    script = tmp_path / "unified_scanner.py"
    assert _runs_script([sys.executable, *arguments], str(tmp_path), script) is matches
    assert not _runs_script(["editor.exe", str(script)], str(tmp_path), script)


def test_stop_only_older_matching_processes(tmp_path, monkeypatch):
    from unittest.mock import Mock

    import scanner.single_instance as module

    target = tmp_path / "unified_scanner.py"
    current = Mock(pid=os.getpid())
    current.create_time.return_value = 100
    old = Mock(pid=10001)
    old.create_time.return_value = 90
    old.cmdline.return_value = [sys.executable, str(target), "--no-lock"]
    old.cwd.return_value = str(tmp_path)
    second = Mock(pid=10002)
    second.create_time.return_value = 91
    second.cmdline.return_value = [sys.executable, str(target)]
    second.cwd.return_value = str(tmp_path)
    other = Mock(pid=10003)
    other.create_time.return_value = 90
    other.cmdline.return_value = [sys.executable, str(tmp_path / "other.py"), str(target)]
    other.cwd.return_value = str(tmp_path)
    newer = Mock(pid=10004)
    newer.create_time.return_value = 101
    denied = Mock(pid=10005)
    denied.create_time.side_effect = psutil.AccessDenied(denied.pid)
    monkeypatch.setattr(module.psutil, "Process", Mock(return_value=current))
    monkeypatch.setattr(module.psutil, "process_iter", lambda: [current, old, second, other, newer, denied])
    wait = Mock(return_value=([old, second], []))
    monkeypatch.setattr(module.psutil, "wait_procs", wait)
    assert stop_existing_scanners(target) == [old.pid, second.pid]
    old.kill.assert_called_once_with()
    second.kill.assert_called_once_with()
    wait.assert_called_once_with([old, second], timeout=10.0)
    for process in (current, other, newer, denied):
        process.kill.assert_not_called()


@pytest.mark.parametrize("failure", ["denied", "timeout", "gone"])
def test_stop_failure_handling(tmp_path, monkeypatch, failure):
    from unittest.mock import Mock

    import scanner.single_instance as module

    target = tmp_path / "unified_scanner.py"
    process = Mock(pid=10001)
    process.create_time.return_value = 0
    process.cmdline.return_value = [sys.executable, str(target)]
    process.cwd.return_value = str(tmp_path)
    monkeypatch.setattr(module.psutil, "process_iter", lambda: [process])
    wait = Mock(return_value=([], [process]) if failure == "timeout" else ([process], []))
    monkeypatch.setattr(module.psutil, "wait_procs", wait)
    if failure == "denied":
        process.kill.side_effect = psutil.AccessDenied(process.pid)
    elif failure == "gone":
        process.kill.side_effect = psutil.NoSuchProcess(process.pid)
    if failure == "gone":
        assert stop_existing_scanners(target) == []
    else:
        with pytest.raises(SingleInstanceError):
            stop_existing_scanners(target)
    process.kill.assert_called_once_with()


@pytest.mark.parametrize("blocked", [False, True])
def test_main_replaces_before_scanning(tmp_path, monkeypatch, blocked):
    from unittest.mock import Mock

    import unified_scanner as module

    events = []

    def stop(path):
        assert path == Path(module.__file__).resolve()
        events.append("stop")
        if blocked:
            raise SingleInstanceError("blocked")
        return [123]

    def scan(*args):
        assert (tmp_path / "scanner.lock.pid").exists()
        events.append("scan")

    stop_mock = Mock(side_effect=stop)
    monkeypatch.setattr(module, "stop_existing_scanners", stop_mock)
    monkeypatch.setattr(module, "run_scanner", scan)
    monkeypatch.setattr(module, "LOG_DIR", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["unified_scanner.py", "--no-feishu"])
    assert module.main() == (2 if blocked else 0)
    assert events == (["stop"] if blocked else ["stop", "scan"])
    stop_mock.assert_called_once()
    assert not (tmp_path / "scanner.lock.pid").exists()


def test_child_can_acquire_after_graceful_release(tmp_path):
    """优雅释放后，新进程必须能起来。"""
    lock_path = tmp_path / "a.lock"
    holder = SingleInstanceLock(lock_path)
    assert holder.acquire() is True
    holder.release()

    proc = _spawn_holder(lock_path, tmp_path / "ready", tmp_path / "fail")
    try:
        assert (tmp_path / "ready").exists(), "释放后子进程仍取不到锁"
        assert not (tmp_path / "fail").exists()
    finally:
        proc.kill()
        proc.wait(timeout=15)
