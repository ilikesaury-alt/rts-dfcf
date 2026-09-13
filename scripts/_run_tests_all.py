"""一次性跑全量 pytest，内存捕获（不落临时文件，避开工具侧批量删除守卫）。

用法：python scripts/_run_tests_all.py [额外 pytest 参数...]
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 必须用系统 Python 3.12：pywencai / lightgbm 等重依赖只装在这里。
PY = os.environ.get(
    "PYTHON", r"C:/Users/lichun/AppData/Local/Programs/Python/Python312/python.exe"
)


def main() -> int:
    args = sys.argv[1:] or ["tests/"]
    proc = subprocess.run(
        [PY, "-m", "pytest", *args, "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    text = (proc.stdout or "") + (proc.stderr or "")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    print("\n".join(lines[-40:]))
    print(f"PYTEST_RC={proc.returncode}")
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
