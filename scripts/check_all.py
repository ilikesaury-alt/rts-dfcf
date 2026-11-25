"""提交前一条命令跑完三道门：ruff → mypy → pytest（默认不含 smoke）。

用法：
    python scripts/check_all.py          # 全量单测
    python scripts/check_all.py --smoke  # 含真库/外网的 smoke 集成测试

退出码 = 首个失败命令的退出码；全过为 0。失败即停，不继续跑后面的门。
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

CHECKS: list[tuple[str, list[str]]] = [
    ("ruff", [sys.executable, "-m", "ruff", "check", "."]),
    ("mypy", [sys.executable, "-m", "mypy", "scanner/"]),
    ("pytest", [sys.executable, "-m", "pytest", "tests/", "-q"]),
]


def main(argv: list[str]) -> int:
    smoke = "--smoke" in argv
    for name, base in CHECKS:
        cmd = [*base, "--run-smoke"] if (name == "pytest" and smoke) else base
        print(f"\n=== {name} ===", flush=True)
        started = time.monotonic()
        rc = subprocess.run(cmd, cwd=ROOT).returncode  # noqa: S603 - 命令均为固定字面量
        print(f"--- {name}: {'OK' if rc == 0 else 'FAIL'} ({time.monotonic() - started:.1f}s) ---")
        if rc != 0:
            return rc
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
