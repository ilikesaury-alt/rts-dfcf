#!/usr/bin/env python3
"""规则变更追踪器（原「规则冻结校验器」）。

用法：
    python scripts/rule_change_tracker.py            # 追踪并输出报告
    python scripts/rule_change_tracker.py --update   # 更新基线到当前状态

背景：
    2026-09-11 曾启用规则冻结（冻结期 ~2026-10-16），目的是让回测统计具备可比性。
    同日用户决定解除冻结——理由：确有需要立即修正的地方，等待的机会成本更高。
    冻结已终止，本工具由「阻断器」转为「变更追踪器」：

      - 退出码恒为 0：规则变更**不再阻断构建/提交**（解冻后任何改动都是合法的）。
      - 仍然报告哪些规则文件变了、哪些关键阈值变了——这是可追溯性，不是约束。
      - 改动后按需跑 `--update` 更新基线；也可以用 `--since <commit>` 对比历史版本。

退出码：
    0 = 始终为 0（工具从不阻断）。报告内容本身说明是否有变更。
"""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "rule-baseline-2026-09-11.md"

# scripts/ 下运行时仓库根不在 sys.path，需手动补上才能 import scanner.*
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 规则基线：文件 -> (字节数, sha256[:16])
# ⚠️ 此处与 docs/rule-baseline-2026-09-11.md 第 1 节表格是同一份基线的两个副本。
#    改动规则文件后，需同时更新两处（--update 只重写文档，不重写本表）。
#    2026-09-11：config.py 因标签字面量单源收敛而变更（见 docs/rule-change-log.md）。
BASELINE: dict[str, tuple[int, str]] = {
    "scanner/config.py": (63283, "0dc7b4f9aef04804"),
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


def check_baseline_consistency() -> list[str]:
    """自检：脚本内 BASELINE 与文档第 1 节表格必须一致。

    存在两处基线副本是设计缺陷（--update 只重写文档）。此处主动比对，
    避免「文档已更新、脚本未更新」导致校验器长期虚假报警。
    """
    if not DOC.exists():
        return [f"[自检失败] 找不到 {DOC}"]
    text = DOC.read_text(encoding="utf-8")
    problems: list[str] = []
    for rel, (exp_size, exp_hash) in BASELINE.items():
        m = re.search(
            r"\|\s*`" + re.escape(rel) + r"`\s*\|\s*([\d,]+)\s*\|\s*`([0-9a-f]+)`",
            text,
        )
        if not m:
            problems.append(f"[自检] 文档缺少 {rel} 的指纹行")
            continue
        doc_size = int(m.group(1).replace(",", ""))
        doc_hash = m.group(2)
        if doc_size != exp_size or doc_hash != exp_hash:
            problems.append(
                f"[自检] {rel} 脚本({exp_size:,}/{exp_hash}) 与文档({doc_size:,}/{doc_hash}) 不一致"
            )
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


def git_show(path: str, rev: str) -> str | None:
    """取某版本的文件内容；失败返回 None（无 git / 路径不存在）。"""
    try:
        p = subprocess.run(  # noqa: S603 - 固定命令 + 仓库内路径 + 用户传入的 rev
            ["git", "show", f"{rev}:{path}"],
            capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT), check=False,
        )
    except OSError:
        return None
    return p.stdout if p.returncode == 0 else None


def diff_against(rev: str) -> int:
    """对比当前工作区与指定版本的规则文件差异（只读，不改任何东西）。"""
    print("=" * 68)
    print(f"规则变更报告：工作区 vs {rev}")
    print("=" * 68)
    any_diff = False
    for rel in BASELINE:
        old = git_show(rel, rev)
        p = ROOT / rel
        if old is None:
            print(f"  [?]    {rel}  -- 在 {rev} 中不存在")
            continue
        if not p.exists():
            print(f"  [删除] {rel}")
            any_diff = True
            continue
        new = p.read_text(encoding="utf-8")
        if old == new:
            print(f"  [同]   {rel}")
        else:
            any_diff = True
            old_lines, new_lines = old.splitlines(), new.splitlines()
            import difflib

            delta = sum(
                1 for ln in difflib.unified_diff(old_lines, new_lines, n=0)
                if ln.startswith(("+", "-")) and not ln.startswith(("+++", "---"))
            )
            print(f"  [改动] {rel}  约 {delta} 行变化")
    print("=" * 68)
    if not any_diff:
        print("规则文件相对该版本无变化。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="规则变更追踪器（原冻结校验器）")
    ap.add_argument("--update", action="store_true", help="用当前文件指纹重写文档基线")
    ap.add_argument("--since", metavar="REV", help="对比当前工作区与指定 git 版本（只读）")
    args = ap.parse_args()

    if args.update:
        return update_doc()
    if args.since:
        return diff_against(args.since)

    print("=" * 68)
    print("规则基线追踪  (基线 2026-09-11, 见 docs/rule-baseline-2026-09-11.md)")
    print("冻结已于 2026-09-11 解除 —— 规则变更不再阻断，本报告仅供追溯。")
    print("=" * 68)

    print("\n[1/3] 文件指纹")
    p1 = check_files()

    print("\n[2/3] 关键阈值常量")
    p2 = check_constants()

    print("\n[3/3] 类别注册表")
    p3 = check_categories()

    # 自检不计入「变更」，但同样需要人工处理
    self_issues = check_baseline_consistency()

    problems = p1 + p2 + p3
    print("\n" + "=" * 68)
    if self_issues:
        print("⚠️  基线自检告警（脚本 vs 文档不一致）：")
        for x in self_issues:
            print(f"    - {x}")
        print("\n请同步 BASELINE 常量与文档第 1 节表格。")
        print("（这属于工具维护问题，不代表规则被改动）")
        print("=" * 68)
    if problems:
        print(f"ℹ️  相对基线有 {len(problems)} 处变更（解冻后属正常，不阻断）：")
        for x in problems:
            print(f"    - {x}")
        print("\n建议到 docs/rule-change-log.md 记一笔，便于日后回溯。")
        print("更新基线：python scripts/rule_change_tracker.py --update")
    else:
        print("✅ 与 2026-09-11 基线一致（未发生规则变更）。")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    sys.exit(main())
