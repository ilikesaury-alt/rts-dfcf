"""终端面板「一键复制」：按 `c` 键 → 本轮「v1 池选 + 沪深飙升三段」**完整原文**进剪贴板。

**为什么需要**：终端每轮 `clear_screen()` 重绘，面板内容没法「选中复制」；
中文 + emoji + 对齐空格在 cmd / Windows Terminal 里手动框选极易选错行。

**复制什么（2026-11-06 用户决策：内容不精简、区域不变）**：从面板文本按 `◆`
区标题切块，只保留 `◆ v1 池选` 与 `◆ 沪深飙升 · 极有可能大涨`（含榜内飙升 /
榜内异动 / 榜外异动三段）两块的**全部行** —— 区/段标题、口径说明、表头、每行
所有列（涨幅、排名、板块、类别·先验、评分、层、连击、行尾标记）、区尾分隔线，
按面板原顺序原对齐，**不裁列、不改数字、不去重**。头部摘要（时间/市况/
「创业板共 N 只」）、告警行、`◆ 飞书过门` 透明区都不在复制范围（v1 回捞终端
本就隐藏）⇒ 与屏幕上那四张表严格一致。三段段标题一律 `—` 前缀，只有 `◆`
才切区（与 `panel_m0_check.terminal_regions` 同口径）。

**三条纪律**：

1. **只复制已经打印出去的东西** —— 面板文本在渲染时从 stdout 采集（tee），
   **不重新渲染、不重算、不重新拼表**：复制到的与屏幕所见逐字一致，唯一加工是
   按 `◆` 区标题裁掉非表格区。
2. **去 ANSI** —— 剪贴板里留一堆 `\033[91m` 只会把聊天框搞成乱码；
   同时把 `\r` 进度行规整成整行文本。
3. **fail-open** —— 剪贴板不可用（无 GUI 会话 / 权限不足 / 非交互 stdin）
   只打印一句提示，**绝不影响扫描主循环**。

**下游**：`pick.py --clipboard` 用 `ai_digest.extract_symbols_from_text` 从这份完整
表格里抽 6 位码（表格行只认代码列，避免成交量等 6 位数字被误当代码），不必手工整理。

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
    "PANEL_ROW_RE",
    "capture_stdout",
    "copy_to_clipboard",
    "extract_table_regions",
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
# `#` / `代码` / `名称`，见 scanner/view/model.py）。本模块用它判「文本里是否确有
# 面板表格行」，`ai_digest.extract_symbols_from_text` 用同一份正则从完整面板里
# 只取代码列 ⇒ 改列序只改这里。不依赖任何一列的宽度或对齐方式。
PANEL_ROW_RE = re.compile(r"^\s{0,4}\d{1,3}\s+((?:SH|SZ|BJ)?\d{6})\s+(\S+)", re.I)

# 区标题以 `◆` 起始（三段段标题一律 `—`，见 AGENTS.md）。按它切块才能把
# 「头部摘要 / 飞书过门」挡在剪贴板外，同时不碰区内任何一行。
_REGION_TITLE_RE = re.compile(r"^\s*◆\s*(.*)$")
# 只复制这两块；出现其它 `◆`（如 `◆ 飞书过门`）立刻停止保留。
_COPY_REGION_KEYS = ("v1 池选", "沪深飙升")

# 进程内最近一次采集到的面板文本（线程安全）。
_lock = threading.Lock()
_latest: str = ""


def extract_table_regions(text: str) -> str:
    """从面板纯文本切出「v1 池选 + 沪深飙升三段」两区的**完整原文**（不精简）。

    逐行扫描：遇 `◆` 区标题就重新判定要不要保留，标题含 `_COPY_REGION_KEYS`
    之一 ⇒ 从该行起整块保留（含空行、段标题、表头、数据行、区尾分隔线），直到
    下一个 `◆`。区内的行**一个字都不改**，故跨区同一只票的整行也照原样留下。

    ⚠ 兜底：一个 `◆` 都没识别到、但确有 `# / 代码 / 名称` 表格行时返回全文 ——
    区标题约定若被改动，复制不该静默变空（改标题需同批更新本模块的关键词）。
    """
    lines: list[str] = []
    keeping = False
    saw_title = False
    for line in text.splitlines():
        m = _REGION_TITLE_RE.match(line)
        if m:
            saw_title = True
            keeping = any(key in m.group(1) for key in _COPY_REGION_KEYS)
        if keeping:
            lines.append(line)
    if not lines and not saw_title and any(PANEL_ROW_RE.match(line) for line in text.splitlines()):
        return text.strip("\n")
    return "\n".join(lines).strip("\n")


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
    payload = extract_table_regions(text)
    if not payload.strip():
        _say("  [复制] 本轮面板没有可抄的表格区（等下一轮扫描后再按 c）")
        return
    count = len(payload.splitlines())
    ok = copy_to_clipboard(payload)
    if ok:
        _say(f"  [复制] 已复制本轮面板原文（v1 池选 + 沪深飙升三段，{count} 行）到剪贴板 ✅")
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
    return f"  💡 按 {COPY_KEY} 键可把本轮面板（v1 池选 + 沪深飙升三段）完整复制到剪贴板"
