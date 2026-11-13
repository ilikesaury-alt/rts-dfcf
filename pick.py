"""`python pick.py` —— 自选清单 AI 调研的简写入口。

等价于 `python -m scanner.ai_digest --symbols ...`，省去手拼长命令：

    python pick.py                          # 交互粘贴（空行结束）
    python pick.py 300319 300666            # 命令行直接给代码
    python pick.py --clipboard              # 读剪贴板（配合扫描器终端「一键复制」按 c）
    python pick.py --symbols-file list.txt  # 从文件读
    python pick.py 300319 300666 --out 报告.txt --max-calls 50

约定：第一个不以 - 开头的参数起都算代码；其余标志原样透传给 ai_digest。
代码可带 SZ/SH 前缀；只认沪深主板+创业板（30/00/60/68），一次最多 50 只。
"""

from __future__ import annotations

import sys

from scanner.ai_digest.__main__ import main


def _build_argv(argv: list[str]) -> list[str]:
    """裸代码 → `--symbols a,b`；无参数 → 交互粘贴；其余标志透传。

    ⚠ 裸代码可以出现在**任何位置**（如 `--limit 3 300319`）—— 判据：不含 `-`
    开头 且 不是某个标志的**值**（`--out 报告.md` 里的 `报告.md` 是值，
    不能当代码）。
    """
    if not argv:
        return ["--symbols"]
    codes: list[str] = []
    rest: list[str] = []
    expect_value = False
    for a in argv:
        if expect_value:
            rest.append(a)
            expect_value = False
            continue
        if a.startswith("-"):
            rest.append(a)
            # 带等号的（--out=x）自带值；不带等号的下个 token 是它的值
            expect_value = "=" not in a and a in _VALUE_FLAGS
            continue
        codes.append(a)
    if codes:
        return ["--symbols", ",".join(codes), *rest]
    return rest


#: 这些标志的下一个 token 是**它的值**（不能当股票代码）。
_VALUE_FLAGS = {
    "--out",
    "--archive-dir",
    "--symbols",
    "--symbols-file",
    "--source",
    "--date",
    "--model",
    "--mode",
    "--limit",
    "--max-rounds",
    "--max-calls",
}


def main_wrapper() -> int:
    return main(_build_argv(sys.argv[1:]))


if __name__ == "__main__":
    raise SystemExit(main_wrapper())
