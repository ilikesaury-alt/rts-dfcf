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

import pytest

from scanner.single_instance import DEFAULT_LOCK_PATH, SingleInstanceError, SingleInstanceLock

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


def _spawn_holder(lock_path: Path, ready: Path, fail: Path) -> subprocess.Popen:
    script = _CHILD_HOLD.format(base=str(BASE_DIR), lock=str(lock_path), flag_ready=str(ready), flag_fail=str(fail))
    proc = subprocess.Popen(  # noqa: S603 - 固定命令 + 本地生成的脚本，无外部输入
        [sys.executable, "-c", script],
        cwd=str(BASE_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
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

    if not _wait_for(ready, 30.0) and not fail.exists():
        proc.kill()
        err = proc.stderr.read().decode("utf-8", "ignore") if proc.stderr else ""
        pytest.fail(f"子进程未能就绪：{err}")
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
