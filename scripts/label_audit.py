#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""标签登记簿审计：打印每条标签的出处 / 证据等级 / 反误读，并跑一遍元数据规则。

用法::

    python scripts/label_audit.py            # 打印全表 + 跑 G1~G4
    python scripts/label_audit.py --json     # 机器可读

退出码：0 = 全过；1 = 有违规（打印明细）。

与 `tests/test_label_registry.py` 跑的是**同一个** `label_registry.audit()` ——
区别只是本脚本给人看（评审时贴输出），测试给机器判。两处不会各说一套。

登记簿本身见 `scanner/label_registry.py` 的模块 docstring（为什么需要它、
四条硬规则 G1~G4、以及「只管展示、不碰评分链路」的边界）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))  # 项目根：让 `python scripts/label_audit.py` 也能 import scanner.*

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # win32 控制台默认 GBK，emoji（🔴/⚡）会抛 UnicodeEncodeError

from scanner import label_registry as lr  # noqa: E402  (sys.path 引导在前，E402 为刻意设计)


def _grade_badge(grade: str) -> str:
    return {"E3": "E3 制度", "E2": "E2 定义", "E1": "E1 惯例", "E0": "E0 未校准"}[grade]


def main() -> int:
    ap = argparse.ArgumentParser(description="标签登记簿审计（展示层标签的出处/证据等级/反误读）")
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = ap.parse_args()

    specs = list(lr.LABEL_REGISTRY.values())
    problems = lr.audit()

    if args.json:
        print(
            json.dumps(
                {
                    "specs": [
                        {
                            "id": s.id,
                            "labels": list(s.labels),
                            "grade": s.grade,
                            "unverified": s.unverified,
                            "source": s.source,
                            "rule": s.rule,
                            "counter": s.counter,
                            "sections": list(s.sections),
                        }
                        for s in specs
                    ],
                    "problems": problems,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1 if problems else 0

    print(f"标签登记簿：{len(specs)} 条（展示层；评分/排序/准入链路不在此表）\n")
    print(f"{'ID':<16} {'等级':<10} {'出处':<4} 反误读")
    print("-" * 110)
    for s in specs:
        print(f"{s.id:<16} {_grade_badge(s.grade):<10} {s.source_code:<4} {s.counter}")
    print("-" * 110)
    print("\n出处分级：")
    for _code, desc in lr.SOURCES.items():
        print(f"  {desc}")
    print("\n证据等级：")
    for _code, desc in lr.GRADES.items():
        print(f"  {desc}")
    # 2026-09-28 用户决策「去掉显示读法」→ 终端与飞书**都不再渲染**这些行；
    # 文案单源仍在此维护，挂回渲染时两端必须同源调 legend_line。
    print("\n一行图例（当前不渲染；挂回时终端与飞书须逐字一致）：")
    for section in lr.LEGEND_SECTIONS:
        print(f"  [{section}] {lr.legend_line(section)}")

    print()
    if problems:
        print(f"发现 {len(problems)} 处违规：")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"登记簿 {len(specs)} 条，元数据规则 G1~G4 全过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
