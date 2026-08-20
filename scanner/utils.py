import math
import os
import sys

from scanner.config import CACHE_MAX_ENTRIES


def to_float(v, default: float | None = 0.0) -> float | None:
    """安全转 float：None/NaN/±inf/不可解析字符串/空 → default（数据入口统一防御）。

    收敛 api._num / models._bar_float / enhancer._safe_float / market_extra._num /
    data_source._as_float 五份同族实现。default 传 None 表示「非法值返回 None」，
    调用方按缺失处理（data_source._as_float 语义）。

    Python json 允许解析非标准字面量 NaN/Infinity，仅判 `f != f`（NaN）会放行 inf，
    inf 与任何数值比较恒为真/假，会绕过节流与越界/档位判断（如 current > MAX_STOCK_PRICE），
    故统一用 math.isfinite。
    """
    try:
        f = float(v)
        return default if not math.isfinite(f) else f  # NaN/±inf → default
    except (TypeError, ValueError):
        return default


def to_int(v, default: int = 0) -> int:
    """安全转 int：None/不可解析字符串/浮点 → 就近取整（数据入口防御）。"""
    try:
        if isinstance(v, str):
            v = v.strip()
            if not v:
                return default
            return int(float(v))
        return int(v)
    except (TypeError, ValueError):
        return default


def cache_put(cache: dict, key, value, max_entries: int = CACHE_MAX_ENTRIES) -> None:
    """带上限的进程内缓存写入：超限时淘汰最旧条目，防止长跑内存无限增长。

    收敛 api._cache_put / market_extra._cache_put / concept._cache_put 三份同族实现。
    dict 保持插入序，`pop(next(iter(cache)))` 淘汰最早插入的 key。
    调用方需自行持有对应锁。
    """
    if key in cache:
        cache.pop(key)
    cache[key] = value
    while len(cache) > max_entries:
        cache.pop(next(iter(cache)))


def is_st(name: str) -> bool:
    return name.startswith("*ST") or name.startswith("ST") or "退市" in name or name.startswith("退")


def _strip_exchange(code: str) -> str:
    if len(code) > 2 and code[:2] in ("SH", "SZ", "BJ"):
        return code[2:]
    if code.startswith("30"):
        return code
    return code


def is_gem(code: str) -> bool:
    return _strip_exchange(code).startswith("30")


# A 股主板/创业板/科创板代码前缀（6 位代码的前 2 位）
_A_SHARE_PREFIXES = ("30", "60", "00", "68", "43", "83", "87", "92")


def is_hk_stock(symbol: str) -> bool:
    """判断是否为港股 symbol。

    雪球 A 股 symbol 带交易所前缀（SZ/SH/BJ），港股为纯数字代码。
    纯数字但符合 A 股代码格式（6 位且以 A 股特征前缀开头）不视为港股，
    避免无前缀的 A 股代码（如数据库残留或外部注入）被误判为港股而过滤。
    """
    if not symbol.isdigit():
        return False
    # 6 位且以 A 股前缀开头 → 视为无前缀的 A 股代码，不当港股
    if len(symbol) == 6 and symbol[:2] in _A_SHARE_PREFIXES:
        return False
    return True


# ── 终端清屏（P1-8：从 scanner.display 迁出不依赖渲染层）──────────────────────
# 此前 11 处工具/回测层 `from scanner.display import clear_screen` 反向依赖渲染层，
# 现收敛到此（utils 为叶子模块，display 与工具层均 import 它，无环）。
if os.name == "nt":
    import ctypes
    _clr_kernel32 = ctypes.windll.kernel32
    _clr_handle = _clr_kernel32.GetStdHandle(-11)
    _clr_mode = ctypes.c_uint32()
    # 是否「真实 Windows conhost」：GetConsoleMode 仅对真实控制台成功；
    # pty/终端模拟器/重定向管道均失败（返回 0），但它们通常讲 ANSI/VT 协议。
    _clr_is_console = _clr_kernel32.GetConsoleMode(_clr_handle, ctypes.byref(_clr_mode)) != 0
    _clr_supports_ansi = (
        _clr_is_console
        and _clr_kernel32.SetConsoleMode(_clr_handle, _clr_mode.value | 0x0004) != 0
    )
else:
    _clr_is_console = False
    _clr_supports_ansi = True


def clear_screen() -> None:
    """清空终端屏幕（主扫描器 display() 渲染前调用，避免上一屏内容逐行叠加）。

    清屏策略（2026-08-13 修订，修复 pty/终端模拟器下 os.system("cls") 无效）：
    - 输出被重定向/管道（isatty=False）→ 跳过，不注入 ANSI 序列污染日志文件。
    - 仅「真实 Windows conhost 但不支持 VT 的旧版控制台」用 os.system("cls")。
    - 其余一律 ANSI \033[2J\033[H：现代 conhost（导入时已启用 VT）、Windows
      Terminal、pty 终端模拟器等均讲 ANSI/VT 协议；pty 下 cls 不生效（cmd 不
      共享 pty 的屏幕缓冲），正是此前「创业板飙升榜监控」表头逐轮叠加的根因。
    """
    if not sys.stdout.isatty():
        return
    if os.name == "nt" and _clr_is_console and not _clr_supports_ansi:
        os.system("cls")
        return
    print("\033[2J\033[H", end="", flush=True)
