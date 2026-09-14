#!/usr/bin/env python3
"""项目量化普查（只读，不改任何文件/库）。

用途：给「设计测评」提供可复现的实测数字——规模、复杂度热点、依赖扇入扇出、
常量面、坏味道计数、归因样本量。2026-09-13 测评报告（docs/audit-2026-09-13.md）
的全部数字由本脚本产出。

用法：
    python scripts/project_audit.py                  # 全量普查
    python scripts/project_audit.py --top 40         # 热点列前 40
    python scripts/project_audit.py --no-db          # 跳过 DB 样本量统计

纪律：
  - 纯只读：DB 以 mode=ro 打开，不建表、不写行。
  - 零依赖：只用标准库（ast / sqlite3），可在任何 Python 3.10+ 环境跑。
  - 口径固定：复杂度 = 1 + 决策节点数（If/For/While/Except/IfExp/BoolOp 额外分支/
    推导式 if/Assert）。是近似值，但同口径下横向可比——用于「谁比谁更复杂」，
    不用作绝对门槛。
"""

from __future__ import annotations

import argparse
import ast
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

DECISION_NODES = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler, ast.IfExp)

# 评分/决策核心模块（常量面统计范围）
CORE_MODULES = (
    "enhancer",
    "analysis",
    "ranking",
    "validator",
    "candidates",
    "nextday_prob",
    "final_pick",
    "decision",
    "pool",
    "danger",
    "matcher",
    "weights",
    "intraday_tactics",
    "comeback",
    "core_themes",
    "trend_beauty",
)

SAMPLE_TABLES = (
    "recommendations",
    "appearances",
    "daily_kline",
    "scan_rejections",
    "pool_log",
    "ranking_snapshot",
    "hot_watch_hits",
    "market_extra_cache",
    "leaderboard_log",
    "scan_quality_log",
    "watch_pool",
    "triple_barrier_labels",
    "market_index_log",
)


def cyclomatic_complexity(node: ast.AST) -> int:
    total = 1
    for child in ast.walk(node):
        if isinstance(child, DECISION_NODES):
            total += 1
        elif isinstance(child, ast.BoolOp):
            total += len(child.values) - 1
        elif isinstance(child, ast.comprehension):
            total += len(child.ifs)
        elif isinstance(child, ast.Assert):
            total += 1
    return total


def parse(path: Path) -> ast.Module | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return None


def collect(root: Path) -> dict[Path, dict]:
    scanner = root / "scanner"
    targets: list[tuple[str, Path]] = []
    targets += [("prod", p) for p in sorted(scanner.rglob("*.py"))]
    targets += [("entry", p) for p in sorted(root.glob("*.py"))]
    targets += [("script", p) for p in sorted((root / "scripts").glob("*.py"))]
    targets += [("test", p) for p in sorted((root / "tests").glob("*.py"))]

    out: dict[Path, dict] = {}
    for kind, path in targets:
        tree = parse(path)
        if tree is None:
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        funcs = [
            (n.name, n.lineno, (n.end_lineno or n.lineno) - n.lineno + 1, cyclomatic_complexity(n))
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("scanner"):
                imports.append(node.module)
            elif isinstance(node, ast.Import):
                imports += [a.name for a in node.names if a.name.startswith("scanner")]
        out[path] = {
            "kind": kind,
            "lines": source.count("\n") + 1,
            "funcs": funcs,
            "imports": imports,
            "broad_except": source.count("except Exception"),
        }
    return out


def report_scale(results: dict[Path, dict]) -> None:
    print("=== 规模 ===")
    agg: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for info in results.values():
        agg[info["kind"]][0] += 1
        agg[info["kind"]][1] += info["lines"]
    for kind in ("prod", "entry", "script", "test"):
        files, lines = agg[kind]
        print(f"  {kind:7s} files={files:4d} lines={lines:6d}")
    print(f"  合计    files={sum(v[0] for v in agg.values())} lines={sum(v[1] for v in agg.values())}")


def report_hotspots(results: dict[Path, dict], root: Path, top: int) -> None:
    hot = []
    for path, info in results.items():
        if info["kind"] == "test":
            continue
        for name, lineno, length, cc in info["funcs"]:
            if length >= 60 or cc >= 25:
                hot.append((cc, length, name, path.relative_to(root).as_posix(), lineno))
    hot.sort(reverse=True)
    print(f"\n=== 复杂度热点（>60 行 或 CC>=25 的函数共 {len(hot)} 个）===")
    print(f"  {'CC':>4} {'LEN':>4}  {'file:line':46s} function")
    for cc, length, name, rel, lineno in hot[:top]:
        print(f"  {cc:>4} {length:>4}  {rel + ':' + str(lineno):46s} {name}")

    print(f"\n=== 超长函数 TOP {min(top, 10)}（按行数）===")
    for cc, length, name, rel, lineno in sorted(hot, key=lambda x: -x[1])[:10]:
        print(f"  {length:>4}L  CC={cc:<4} {rel + ':' + str(lineno):46s} {name}")


def report_deps(results: dict[Path, dict], root: Path) -> None:
    fan_in: dict[str, set[str]] = defaultdict(set)
    fan_out: dict[str, set[str]] = defaultdict(set)
    scanner = root / "scanner"
    for path, info in results.items():
        if info["kind"] != "prod":
            continue
        module = "scanner." + path.relative_to(scanner).with_suffix("").as_posix().replace("/", ".")
        for imported in info["imports"]:
            fan_out[module].add(imported)
            fan_in[imported].add(module)
    print("\n=== 依赖扇入 / 扇出（scanner 内部）===")
    print("  扇入 TOP10:")
    for module, users in sorted(fan_in.items(), key=lambda kv: -len(kv[1]))[:10]:
        print(f"    {len(users):3d}  {module}")
    print("  扇出 TOP10:")
    for module, deps in sorted(fan_out.items(), key=lambda kv: -len(kv[1]))[:10]:
        print(f"    {len(deps):3d}  {module}")


def report_constants(root: Path) -> None:
    config = root / "scanner" / "config.py"
    tree = parse(config)
    if tree is not None:
        assigns = sum(1 for n in tree.body if isinstance(n, ast.Assign))
        annotated = sum(1 for n in tree.body if isinstance(n, ast.AnnAssign))
        print("\n=== config.py 常量面 ===")
        print(f"  模块级常量 {assigns + annotated}（Assign {assigns} / AnnAssign {annotated}）")
    print("  评分/决策核心数值字面量:")
    total = 0
    for name in CORE_MODULES:
        path = root / "scanner" / f"{name}.py"
        tree = parse(path)
        if tree is None:
            continue
        count = sum(
            1
            for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool)
        )
        total += count
        print(f"    {name:18s} {count:>5d}")
    print(f"    {'合计':18s} {total:>5d}")


def report_smells(results: dict[Path, dict]) -> None:
    print("\n=== 坏味道 ===")
    per_file = sorted(
        ((info["broad_except"], path) for path, info in results.items() if info["broad_except"]),
        reverse=True,
    )
    total = sum(n for n, _ in per_file)
    print(f"  except Exception 合计 {total} 处，分布 TOP8:")
    for count, path in per_file[:8]:
        print(f"    {count:3d}  {path}")


def report_tests(results: dict[Path, dict]) -> None:
    total = 0
    for path, info in results.items():
        if info["kind"] == "test":
            text = path.read_text(encoding="utf-8", errors="replace")
            total += sum(1 for ln in text.splitlines() if ln.lstrip().startswith("def test_"))
    print(f"\n=== 测试 ===\n  def test_ 总数 {total}（实际收集数用 pytest 看，含参数化会更多）")


def report_db(db: Path) -> None:
    if not db.exists():
        print(f"\n=== 归因样本量 ===\n  [跳过] 未找到 {db}")
        return
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    try:
        print(f"\n=== 表体量（{db.name}）===")
        for table in SAMPLE_TABLES:
            try:
                count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                print(f"  {table:24s} {count:>8d}")
            except sqlite3.Error as exc:
                print(f"  {table:24s} [缺失] {exc}")

        print("\n=== 归因有效样本（策略实验的信息量上限）===")
        scalar = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
        print(f"  recommendations 总行数        {scalar('SELECT COUNT(*) FROM recommendations')}")
        print(f"  交易日数                      {scalar('SELECT COUNT(DISTINCT date) FROM recommendations')}")
        print(f"  不同 symbol                   {scalar('SELECT COUNT(DISTINCT symbol) FROM recommendations')}")
        print(
            f"  (date,symbol) 组合            {scalar('SELECT COUNT(*) FROM (SELECT DISTINCT date,symbol FROM recommendations)')}"
        )
        print(
            f"  (date,symbol,category) 组合   {scalar('SELECT COUNT(*) FROM (SELECT DISTINCT date,symbol,category FROM recommendations)')}"
        )

        print("\n  每日样本量分布（按日聚合统计的失真风险）:")
        daily = sorted(r[0] for r in conn.execute("SELECT COUNT(*) FROM recommendations GROUP BY date"))
        if daily:
            print(f"    天数={len(daily)} min={daily[0]} median={daily[len(daily) // 2]} max={daily[-1]}")
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="项目量化普查（只读）")
    parser.add_argument("--root", default=None, help="仓库根目录（默认取本脚本的上一级）")
    parser.add_argument("--db", default=None, help="scanner.db 路径（默认 <root>/scanner.db）")
    parser.add_argument("--top", type=int, default=25, help="热点列表条数")
    parser.add_argument("--no-db", action="store_true", help="跳过数据库统计")
    args = parser.parse_args()

    root = Path(args.root).resolve() if args.root else Path(__file__).resolve().parent.parent
    db = Path(args.db).resolve() if args.db else root / "scanner.db"

    results = collect(root)
    report_scale(results)
    report_hotspots(results, root, args.top)
    report_deps(results, root)
    report_constants(root)
    report_smells(results)
    report_tests(results)
    if not args.no_db:
        report_db(db)
    return 0


if __name__ == "__main__":
    sys.exit(main())
