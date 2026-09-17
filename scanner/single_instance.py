"""单实例锁 —— 防止扫描器被重复启动、多进程并行写 DB。

为什么用文件锁而不是 pidfile
---------------------------
文件锁绑定的是**打开的文件描述符**：进程正常退出、抛异常、被 TerminateProcess
强杀，三条路径都由内核回收锁，不会残留死锁。pidfile 方案在崩溃后会留下残骸，
而判断「这个 PID 还活着吗」只能靠 OpenProcess，PID 又会被系统复用，判定不可靠。

四条实现约束（都是踩过的坑）
---------------------------
1. **刻意不删除锁文件**。删掉会让新进程在另一个 inode 上取锁，而旧进程仍持有
   原 inode 的锁 —— 互斥当场失效。``release()`` 只解锁 + 删 ``.pid``。
2. **用 O_CREAT 而不是 O_EXCL**。判断「被占用」只看能否锁上，不看文件是否存在。
   否则一次崩溃残留的空文件会永久拦住启动 —— 那正是「根治」的反面。
3. **PID 写在独立的 .pid 文件**。Windows 的 ``msvcrt.locking`` 是强制锁，会连带
   拒绝其他进程读取被锁的字节（``PermissionError``），所以 PID 无法与锁共存于
   同一文件。PID 仅用于提示，读不到时降级为「已有实例在运行」，不影响互斥正确性。
4. 跨平台：Windows 用 ``msvcrt.locking``，POSIX 用 ``fcntl.flock``。

用法::

    lock = SingleInstanceLock("logs/scanner.lock")
    if not lock.acquire():
        print("已有实例在运行", lock.holder_description())
        sys.exit(2)
    try:
        ...
    finally:
        lock.release()
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import psutil

_IS_WINDOWS = sys.platform == "win32"

#: 默认锁文件位置（相对项目根）。不删除，永久保留。
DEFAULT_LOCK_PATH = Path("logs") / "scanner.lock"


class SingleInstanceError(RuntimeError):
    """锁文件无法打开等基础设施级错误（区别于「已被占用」）。"""


def _runs_script(command: list[str], cwd: str, script_path: Path) -> bool:
    if len(command) < 2:
        return False
    executable = Path(command[0]).name.lower()
    if not executable.startswith(("python", "pypy")):
        return False
    index = 1
    while index < len(command):
        arg = command[index]
        if arg == "-m":
            return (
                index + 1 < len(command)
                and command[index + 1] == script_path.stem
                and (Path(cwd) / script_path.name).resolve() == script_path
            )
        if arg in ("-c", "-", "--help", "--version", "-h", "-V"):
            return False
        if arg == "--":
            index += 1
            break
        if arg in ("-W", "-X"):
            index += 2
        elif arg.startswith("-"):
            index += 1
        else:
            break
    if index >= len(command):
        return False
    candidate = Path(command[index])
    if not candidate.is_absolute():
        candidate = Path(cwd) / candidate
    return candidate.resolve() == script_path


def stop_existing_scanners(script_path: Path, timeout: float = 10.0) -> list[int]:
    target = script_path.resolve()
    victims = []
    current = psutil.Process(os.getpid())
    started_at = current.create_time()
    for process in psutil.process_iter():
        if process.pid == os.getpid():
            continue
        try:
            if (process.create_time(), process.pid) < (started_at, current.pid) and _runs_script(
                process.cmdline(), process.cwd(), target
            ):
                victims.append(process)
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            continue
    stopped = []
    for process in victims:
        try:
            process.kill()
            stopped.append(process.pid)
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied as exc:
            raise SingleInstanceError(f"无法结束旧扫描器 PID {process.pid}，本次启动退出") from exc
    _, alive = psutil.wait_procs(victims, timeout=timeout)
    if alive:
        pids = ", ".join(str(process.pid) for process in alive)
        raise SingleInstanceError(f"旧扫描器尚未退出（PID {pids}），本次启动退出")
    return stopped


class SingleInstanceLock:
    """基于文件锁的单实例守卫。

    ``acquire()`` 走非阻塞加锁，因此不会有任何等待 —— 拿不到就立刻返回 False。
    同进程重复 ``acquire()`` 是幂等的（Windows 上同进程第二个 fd 也会被拒，
    故不再重复调用底层加锁）。
    """

    def __init__(self, lock_path: str | os.PathLike[str] | None = None, pid_path: str | os.PathLike[str] | None = None):
        self.lock_path = Path(lock_path) if lock_path is not None else DEFAULT_LOCK_PATH
        self.pid_path = (
            Path(pid_path) if pid_path is not None else self.lock_path.with_name(self.lock_path.name + ".pid")
        )
        self._fd: int | None = None

    # ------------------------------------------------------------------ 状态

    @property
    def held(self) -> bool:
        """当前对象是否持有锁。"""
        return self._fd is not None

    # ------------------------------------------------------------------ 取锁

    def acquire(self) -> bool:
        """尝试取锁。成功返回 True；已被其他进程占用返回 False。

        锁文件打不开（权限/路径非法）抛 :class:`SingleInstanceError`，不静默降级 ——
        静默降级等于没有保护。
        """
        if self._fd is not None:
            return True

        try:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(self.lock_path), os.O_RDWR | os.O_CREAT, 0o644)
        except OSError as exc:
            raise SingleInstanceError(f"无法打开锁文件 {self.lock_path}: {exc}") from exc

        try:
            self._lock_fd(fd)
        except OSError:
            # 被占用（或任何加锁失败）—— 关掉 fd，视为未取得。
            try:
                os.close(fd)
            except OSError:
                pass
            return False

        self._fd = fd
        self._write_pid()
        return True

    def _lock_fd(self, fd: int) -> None:
        """对 fd 的第一个字节加非阻塞排他锁；失败抛 OSError。"""
        # 平台分支用函数内 import：模块级条件 import 会让 mypy 在单平台上把
        # 另一个模块判为未定义（`sys.platform` 的窄化只保留当前平台分支）。
        # 另外 typeshed 把 fcntl 在 win32 下视为空模块，故 POSIX 分支需 ignore。
        if _IS_WINDOWS:
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[attr-defined]

    def _unlock_fd(self, fd: int) -> None:
        if _IS_WINDOWS:
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)  # type: ignore[attr-defined]

    def _write_pid(self) -> None:
        """把持有者 PID 写进独立文件。仅供提示，失败不影响互斥正确性。"""
        try:
            self.pid_path.parent.mkdir(parents=True, exist_ok=True)
            self.pid_path.write_text(str(os.getpid()), encoding="utf-8")
        except OSError:
            pass

    # ------------------------------------------------------------------ 释放

    def release(self) -> None:
        """解锁并清理 PID 文件。**不删除锁文件**（见模块 docstring 约束 1）。"""
        fd = self._fd
        if fd is None:
            return
        self._fd = None
        try:
            try:
                self._unlock_fd(fd)
            except OSError:
                pass
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                self.pid_path.unlink()
            except OSError:
                pass

    # ------------------------------------------------------------------ 提示

    def holder_pid(self) -> int | None:
        """读持有者 PID（仅用于展示）。读不到返回 None，不影响互斥正确性。"""
        try:
            raw = self.pid_path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return int(raw) if raw.isdigit() else None

    def holder_description(self) -> str:
        """冲突提示后缀，形如 ``（PID 24580）``；无 PID 信息时返回空串。"""
        pid = self.holder_pid()
        return f"（PID {pid}）" if pid is not None else ""

    # ------------------------------------------------------- 上下文管理器协议

    def __enter__(self) -> SingleInstanceLock:
        if not self.acquire():
            raise RuntimeError(f"已有扫描器实例在运行{self.holder_description()}")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<SingleInstanceLock {self.lock_path} held={self.held}>"
