"""终端面板「一键复制」：按 `c` 键 → **股票名称 + 6 位代码**清单进系统剪贴板。

**为什么需要**：终端每轮 `clear_screen()` 重绘，面板内容没法「选中复制」；
中文 + emoji + 对齐空格在 cmd / Windows Terminal 里手动框选极易选错行。

**复制什么**：只抄「名称 + 代码」，一行一只（按面板出现顺序去重）——
面板里那些分数/涨幅/排名/板块列是给眼睛看的，粘到别处反而是噪声。
清单从**面板文本**里抽（表格列序 `# / 代码 / 名称` 固定），代码统一去掉
SH/SZ/BJ 前缀取 6 位纯代码（与 `scanner.hot_watch._strip_code` 同口径）。

**三条纪律**：

1. **只复制已经打印出去的东西** —— 面板文本在渲染时从 stdout 采集（tee），
   **不重新渲染、不重算**：复制到的与屏幕上看到的逐字一致（同一份数据、同一份格式化）。
2. **去 ANSI** —— 剪贴板里留一堆 `\033[91m` 只会把聊天框搞成乱码；
   同时把 `\r` 进度行规整成整行文本。
3. **fail-open** —— 剪贴板不可用（无 GUI 会话 / 权限不足 / 非交互 stdin）
   只打印一句提示，**绝不影响扫描主循环**。

**平台**：Windows 走 ctypes（user32/kernel32，`CF_UNICODETEXT`，中文安全）；
其它平台走 `pbcopy` / `xclip` / `wl-copy` 兜底。按键监听仅 Windows 实现
（`msvcrt.kbhit`，终端为真控制台时才装）。
"""

from __future__ import annotations

import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

__all__ = [
    "capture_stdout",
    "copy_to_clipboard",
    "extract_stock_list",
    "hotkey_hint",
    "install_hotkey",
    "latest_panel_text",
    "strip_ansi",
]

# 复制热键（不区分大小写）。
COPY_KEY = "c"

# ANSI 转义序列：CSI（颜色/光标）、OSC（标题）、以及单字符 ESC 序列。
# 只保留可打印文本，剪贴板里不应出现控制字符。
_ANSI_RE = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC ... BEL / ST
    r"|\x1b\[[0-9;?]*[ -/]*[@-~]"  # CSI ... 最终字节
    r"|\x1b[@-Z\\-_]"  # ESC 单字符序列
)

# 终端表格行：**序号列 + 代码列 + 名称列**（COLS_POOL 与 COLS_HOT 同列序：
# `#` / `代码` / `名称`，见 scanner/view/model.py）。抽清单靠这个固定列序，
# 不依赖任何一列的宽度或对齐方式（宽度变了也不影响匹配）。
_ROW_RE = re.compile(r"^\s{0,4}\d{1,3}\s+((?:SH|SZ|BJ)?\d{6})\s+(\S+)")

# 进程内最近一次采集到的面板文本（线程安全）。
_lock = threading.Lock()
_latest: str = ""


def _normalize_code(code: str) -> str:
    """代码去市场前缀（SH/SZ/BJ）→ 6 位纯代码（与 `scanner.hot_watch._strip_code` 同口径）。"""
    if len(code) > 2 and code[:2] in ("SH", "SZ", "BJ"):
        return code[2:]
    return code


def extract_stock_list(text: str) -> str:
    """从面板文本抽出「名称 + 6 位代码」清单（每行一只，按出现顺序去重）。

    只认表格行（`序号 代码 名称 …`）：标题行、表头、「飞书过门」那种**只有名字
    没有代码**的行都不匹配 ⇒ 不会混进清单（没有代码的票对用户也没用）。
    """
    lines: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        m = _ROW_RE.match(line)
        if not m:
            continue
        code = _normalize_code(m.group(1))
        name = m.group(2)
        if code in seen:
            continue
        seen.add(code)
        lines.append(f"{name} {code}")
    return "\n".join(lines)


def strip_ansi(text: str) -> str:
    """去掉 ANSI 转义序列，并把 `\r` 覆盖写（进度行）规整成整行文本。"""
    cleaned = _ANSI_RE.sub("", text)
    # 进度行用 `\r` 原地刷新（"⏳ 下次刷新还有 30s"），落到剪贴板里会挤成一行乱码。
    return cleaned.replace("\r\n", "\n").replace("\r", "\n")


def latest_panel_text() -> str:
    """最近一次采集到的面板纯文本（未采集过时为空串）。"""
    with _lock:
        return _latest


def _set_latest(text: str) -> None:
    global _latest
    with _lock:
        _latest = text


class _Tee:
    """stdout 双写：原样转写 + 记录到 sink（只记本轮面板，不记日志噪声）。"""

    def __init__(self, real, sink: list[str]) -> None:
        self._real = real
        self._sink = sink

    def write(self, data):
        if data:
            self._sink.append(data)
        return self._real.write(data)

    def flush(self) -> None:
        self._real.flush()

    def isatty(self) -> bool:
        try:
            return bool(self._real.isatty())
        except (OSError, ValueError, AttributeError):
            return False

    def fileno(self):
        return self._real.fileno()

    def __getattr__(self, name):
        # 其余属性（encoding / reconfigure / buffer ...）一律转发给真实 stdout。
        return getattr(self._real, name)


@contextmanager
def capture_stdout() -> Iterator[list[str]]:
    """采集本上下文内打印到 stdout 的全部文本（渲染面板时用）。

    退出时把采集到的文本**去 ANSI** 后存入进程内最新值，供 `c` 键复制。
    stdout 无论正常返回还是抛异常都会被还原（不留下悬空的 tee）。
    """
    recorded: list[str] = []
    real = sys.stdout
    tee = _Tee(real, recorded)
    sys.stdout = tee
    try:
        yield recorded
    finally:
        # 只在「stdout 仍是本 tee」时还原：期间若 stdout 被降级成 devnull
        # （_silence_stdout），还原会把静音状态撤销掉。
        if sys.stdout is tee:
            sys.stdout = real
        _set_latest(strip_ansi("".join(recorded)))


# ── 剪贴板写入 ──────────────────────────────────────────────────────────


def _copy_windows(text: str) -> bool:
    """ctypes 走 `CF_UNICODETEXT` 写剪贴板（中文/emoji 安全，不依赖外部进程）。"""
    import ctypes
    from ctypes import wintypes

    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.restype = wintypes.HGLOBAL

    # 剪贴板文本按 CRLF 走（Windows 惯例）。
    payload = text.replace("\r\n", "\n").replace("\n", "\r\n")
    buffer = ctypes.create_unicode_buffer(payload)
    size = ctypes.sizeof(buffer)

    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
    if not handle:
        return False
    locked = kernel32.GlobalLock(handle)
    if not locked:
        kernel32.GlobalFree(handle)
        return False
    try:
        ctypes.memmove(locked, buffer, size)
    finally:
        kernel32.GlobalUnlock(handle)

    if not user32.OpenClipboard(None):
        kernel32.GlobalFree(handle)
        return False
    try:
        if not user32.EmptyClipboard():
            kernel32.GlobalFree(handle)
            return False
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            # 交接失败 → 内存仍归本进程，SystemClipboard 没接管 ⇒ 必须自己释放。
            kernel32.GlobalFree(handle)
            return False
    finally:
        user32.CloseClipboard()
    return True


def _copy_via_command(text: str) -> bool:
    """兜底：外部命令写剪贴板（Windows `clip` / macOS `pbcopy` / Linux `xclip`）。"""
    payload = text.replace("\r\n", "\n").replace("\n", "\r\n")
    candidates: list[tuple[list[str], bytes]] = [
        (["clip"], payload.encode("utf-16-le")),
        (["pbcopy"], payload.encode("utf-8")),
        (["xclip", "-selection", "clipboard"], payload.encode("utf-8")),
        (["wl-copy"], payload.encode("utf-8")),
    ]
    for cmd, data in candidates:
        try:
            proc = subprocess.run(cmd, input=data, capture_output=True, check=False, timeout=10)  # noqa: S603
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode == 0:
            return True
    return False


def copy_to_clipboard(text: str) -> bool:
    """把文本写入系统剪贴板；成功返回 True（失败只返回 False，不抛）。"""
    if not text:
        return False
    if sys.platform == "win32":
        try:
            if _copy_windows(text):
                return True
        except OSError:
            pass
    return _copy_via_command(text)


# ── 按键监听 ────────────────────────────────────────────────────────────


def _windows_read_key() -> str | None:
    """读一个按键（无输入返回 None）；Windows 专用（msvcrt 不回显）。"""
    import msvcrt

    if not msvcrt.kbhit():
        return None
    char = msvcrt.getwch()
    if char in ("\x00", "\xe0"):
        # 功能键/方向键：吞掉紧随其后的 scan code，别让它被当成 'c'。
        if msvcrt.kbhit():
            msvcrt.getwch()
        return None
    return char


def _hotkey_loop(
    read_key: Callable[[], str | None],
    on_copy: Callable[[], None],
) -> None:
    """按键循环：`read_key` 返回 None 表示暂时没输入（继续等）。"""
    while True:
        try:
            key = read_key()
        except StopIteration:  # 测试用：让循环可退出
            return
        except OSError:
            return
        if key is None:
            time.sleep(0.05)
            continue
        if key.lower() == COPY_KEY:
            on_copy()


def _handle_copy_request() -> None:
    """执行一次复制（并给终端一句回执）。"""
    text = latest_panel_text()
    if not text.strip():
        _say("  [复制] 还没有可复制的面板（等下一轮扫描后再按 c）")
        return
    stock_list = extract_stock_list(text)
    if not stock_list.strip():
        _say("  [复制] 本轮面板没有可抄的票（等下一轮扫描后再按 c）")
        return
    count = len(stock_list.splitlines())
    ok = copy_to_clipboard(stock_list)
    if ok:
        _say(f"  [复制] 已复制 {count} 只股票（名称+代码）到剪贴板 ✅")
    else:
        _say("  [复制] 复制失败（剪贴板不可用）")


def _say(msg: str) -> None:
    """往真实 stdout 打一行（管道已断时静默，不打断主循环）。"""
    try:
        print(msg, flush=True)
    except (OSError, ValueError):
        pass


def install_hotkey(*, force: bool = False) -> bool:
    """装上「按 c 复制面板」监听（后台守护线程）；成功返回 True。

    仅在**真控制台**下装（stdin 是 tty）：重定向 / 服务态装了也没有按键可读。
    `force=True` 跳过 tty 检查（测试用）。
    """
    if sys.platform != "win32":
        return False
    if not force:
        try:
            if not sys.stdin.isatty():
                return False
        except (OSError, ValueError, AttributeError):
            return False

    def _run() -> None:
        try:
            _hotkey_loop(_windows_read_key, _handle_copy_request)
        except OSError:
            return

    thread = threading.Thread(target=_run, name="terminal-copy-hotkey", daemon=True)
    thread.start()
    return True


def hotkey_hint() -> str:
    """终端提示语（告知用户怎么用）。"""
    return f"  💡 按 {COPY_KEY} 键可把本轮股票（名称+代码）复制到剪贴板"
