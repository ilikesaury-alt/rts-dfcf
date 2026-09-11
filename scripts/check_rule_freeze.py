#!/usr/bin/env python3
"""规则冻结校验器 —— 检查受冻结文件是否被改动。

用法：
    python scripts/check_rule_freeze.py            # 校验并输出报告
    python scripts/check_rule_freeze.py --update   # 显式更新基线（需人工确认）

背景：docs/rule-freeze-2026-09-11.md

退出码：
    0 = 全部未变（或 --update 成功）
    1 = 有文件被改动（冻结破坏）
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "rule-freeze-2026-09-11.md"

# scripts/ 下运行时仓库根不在 sys.path，需手动补上才能 import scanner.*
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 冻结基线：文件 -> (字节数, sha256[:16])
BASELINE: dict[str, tuple[int, str]] = {
    "scanner/config.py": (61867, "ff1d7de378471143"),
    "scanner/categories.py": (5439, "90697eb00d26f129"),
    "scanner/weights.py": (5364, "cf5fa9a489c3248f"),
    "scanner/decision.py": (11962, "267003fb19a3b82f"),
    "scanner/orchestrator.py": (32918, "c497f30b38273d6f"),
    "scanner/ranking.py": (42745, "adf11cfc2e8875d6"),
    "scanner/enhancer.py": (33215, "b87407e97b7899b9"),
    "scanner/validator.py": (30144, "3008f127b214b5f7"),
}

# 关键常量基线：(模块属性路径, 期望值)
CONST_BASELINE: dict[str, object] = {
    "scanner.config.NEXTDAY_HIT_THRESHOLD": 7.0,
    "scanner.config.HOLD_DAYS_BY_CATEGORY": {"comeback": 3, "core_dip": 3},
    "scanner.decision.GATE_INDEX_MIN_PCT": 0.0,
    "scanner.decision.GATE_CUM5_MIN_PCT": -3.0,
}

# 类别注册表基线：键 -> (label, display_priority, live_produced)
CATEGORY_BASELINE: dict[str, tuple[str, int, bool]] = {
    "pool_pick": ("池选", 0, True),
    "rebound": ("RBD", 1, True),
    "known_new_face": ("kNF", 2, True),
    "momentum": ("MOM", 3, True),
    "new_face": ("NEW", 4, True),
    "short_term": ("ST", 5, True),
    "comeback": ("CB", 6, True),
    "core_dip": ("DIP", 99, True),
    "pullback": ("PB", 7, False),
}


def _sha16(path: Path) -> tuple[int, str]:
    data = path.read_bytes()
    return len(data), hashlib.sha256(data).hexdigest()[:16]


def check_files() -> list[str]:
    problems: list[str] = []
    for rel, (exp_size, exp_hash) in BASELINE.items():
        p = ROOT / rel
        if not p.exists():
            problems.append(f"[缺失] {rel}")
            continue
        size, h = _sha16(p)
        if h == exp_hash:
            print(f"  [OK]   {rel:28s} {size:>7d} bytes")
        else:
            delta = size - exp_size
            problems.append(f"[改动] {rel}  {exp_hash} -> {h}  ({delta:+d} bytes)")
            print(f"  [改动] {rel:28s} {size:>7d} bytes  (基线 {exp_size}, {delta:+d})")
    return problems


def check_constants() -> list[str]:
    problems: list[str] = []
    for dotted, expected in CONST_BASELINE.items():
        mod_name, attr = dotted.rsplit(".", 1)
        try:
            mod = __import__(mod_name, fromlist=[attr])
            actual = getattr(mod, attr)
        except Exception as exc:  # noqa: BLE001 - 校验脚本，任何导入失败都要报告
            problems.append(f"[导入失败] {dotted}: {exc}")
            continue
        if actual == expected:
            print(f"  [OK]   {dotted} = {actual!r}")
        else:
            problems.append(f"[阈值改动] {dotted}: {expected!r} -> {actual!r}")
            print(f"  [改动] {dotted} = {actual!r}  (基线 {expected!r})")
    return problems


def check_categories() -> list[str]:
    problems: list[str] = []
    try:
        from scanner.categories import CATEGORY_REGISTRY
    except Exception as exc:  # noqa: BLE001
        return [f"[导入失败] scanner.categories: {exc}"]
    for key, (label, prio, live) in CATEGORY_BASELINE.items():
        info = CATEGORY_REGISTRY.get(key)
        if info is None:
            problems.append(f"[类别缺失] {key}")
            print(f"  [改动] {key:16s} 已从注册表移除")
            continue
        got = (
            getattr(info, "label", None),
            getattr(info, "display_priority", None),
            getattr(info, "live_produced", None),
        )
        if got == (label, prio, live):
            print(f"  [OK]   {key:16s} {got}")
        else:
            problems.append(f"[类别改动] {key}: {(label, prio, live)} -> {got}")
            print(f"  [改动] {key:16s} {got}  (基线 {(label, prio, live)})")
    extra = set(CATEGORY_REGISTRY) - set(CATEGORY_BASELINE)
    if extra:
        problems.append(f"[新增类别] {sorted(extra)}")
        print(f"  [改动] 新增类别 {sorted(extra)}")
    return problems


def update_doc() -> int:
    """把当前实际值写回文档中的指纹表。仅 --update 时调用。"""
    if not DOC.exists():
        print(f"错误：找不到 {DOC}")
        return 1
    raw = DOC.read_bytes()
    newline_style = "\r\n" if b"\r\n" in raw else "\n"
    text = raw.decode("utf-8").replace("\r\n", "\n")
    changed = 0
    for rel in BASELINE:
        p = ROOT / rel
        if not p.exists():
            continue
        size, h = _sha16(p)
        pattern = re.compile(
            r"(\|\s*`" + re.escape(rel) + r"`\s*\|\s*)([\d,]+)(\s*\|\s*`)([0-9a-f]+)(`\s*\|)"
        )

        def _repl(m: re.Match[str], _size: int = size, _hash: str = h) -> str:
            nonlocal changed
            if m.group(4) != _hash or m.group(2) != f"{_size:,}":
                changed += 1
            return f"{m.group(1)}{_size:,}{m.group(3)}{_hash}{m.group(5)}"

        text = pattern.sub(_repl, text)
    out = text if newline_style == "\n" else text.replace("\n", "\r\n")
    DOC.write_bytes(out.encode("utf-8"))
    print(f"已更新 {DOC.name} 中 {changed} 处指纹。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="规则冻结校验器")
    ap.add_argument("--update", action="store_true", help="用当前文件指纹重写文档基线")
    args = ap.parse_args()

    if args.update:
        return update_doc()

    print("=" * 68)
    print("规则冻结校验  (基线 2026-09-11, 见 docs/rule-freeze-2026-09-11.md)")
    print("=" * 68)

    print("\n[1/3] 文件指纹")
    p1 = check_files()

    print("\n[2/3] 关键阈值常量")
    p2 = check_constants()

    print("\n[3/3] 类别注册表")
    p3 = check_categories()

    problems = p1 + p2 + p3
    print("\n" + "=" * 68)
    if problems:
        print(f"⚠️  检测到 {len(problems)} 处冻结破坏：")
        for x in problems:
            print(f"    - {x}")
        print("\n请到 docs/rule-freeze-log.md 登记，或回滚改动。")
        return 1
    print("✅ 未检测到冻结破坏。规则基线完好。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
