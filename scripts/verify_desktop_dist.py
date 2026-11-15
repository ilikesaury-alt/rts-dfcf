"""
打包产物验收脚本（2026-11-15 · 一次性校验用）

目的：新包的 `resources/repo/` 必须带上桌面版新代码（`tasks_server.py` /
`local_panel_server.py` / `web/dist`），且**库路径解析**要指向本机活库而非包内冷快照。

用法：
    python scripts/verify_desktop_dist.py
退出码 0 = 全过；非 0 = 有缺口（逐条打印）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DIST = REPO / "desktop" / "dist"
UNPACKED = DIST / "win-unpacked"
RES_REPO = UNPACKED / "resources" / "repo"

# 包内必须存在的新代码（11-15 新增；10-20 老包里没有）
MUST_HAVE_FILES = [
    "scanner/tasks_server.py",
    "scanner/local_panel_server.py",
    "unified_scanner.py",
]
# 包内必须存在且内容非空
MUST_HAVE_NONEMPTY = [
    "rts-panel-cloud/web/dist/index.html",
]

results: list[tuple[bool, str]] = []


def check(ok: bool, msg: str) -> None:
    results.append((ok, msg))


def main() -> int:
    if not UNPACKED.exists():
        print(f"[verify] 失败：打包产物不存在 {UNPACKED}")
        return 2

    # 1) 新代码是否进包
    for rel in MUST_HAVE_FILES:
        p = RES_REPO / rel
        check(p.is_file(), f"包内存在 {rel}" + ("" if p.is_file() else " ← 缺失！"))

    for rel in MUST_HAVE_NONEMPTY:
        p = RES_REPO / rel
        size = p.stat().st_size if p.is_file() else 0
        check(size > 0, f"包内非空 {rel}（{size} B）")

    # 2) Tasks 页面是否进包（hash 路由 #/tasks）
    idx = RES_REPO / "rts-panel-cloud" / "web" / "dist" / "index.html"
    if idx.is_file():
        assets = (idx.parent / "assets")
        js = sorted(assets.glob("*.js")) if assets.is_dir() else []
        check(bool(js), f"web/dist 有 JS 产物：{[j.name for j in js]}")

    # 3) 包内库：应与仓库根同一份（打包时整仓拷贝 ⇒ 大小/时间应一致）
    #    ⚠ 若大小相同，说明包里带的是**当下这份活库**（新鲜），不是旧冷快照。
    bundled_db = RES_REPO / "scanner.db"
    live_db = REPO / "scanner.db"
    if bundled_db.is_file() and live_db.is_file():
        b = bundled_db.stat().st_size
        l = live_db.stat().st_size
        bt = bundled_db.stat().st_mtime
        lt = live_db.stat().st_mtime
        fresh = abs(bt - lt) < 300  # 5 分钟内 ⇒ 同一次打包产物
        check(
            b == l and fresh,
            f"包内库与仓库活库同源（{b/1024/1024:.0f}MB，mtime 差 {abs(bt-lt):.0f}s）",
        )
    else:
        check(False, "scanner.db 有一侧缺失（包内 or 仓库根）")

    # 4) 关键口径：main.js 里必须仍是「优先解析外部活库」
    main_js = UNPACKED / "resources" / "app.asar"
    src_js = REPO / "desktop" / "main.js"
    if src_js.is_file():
        txt = src_js.read_text(encoding="utf-8", errors="ignore")
        check("RTS_LIVE_DB" in txt and "resolveLiveDb" in txt,
              "desktop/main.js 含活库解析（resolveLiveDb / RTS_LIVE_DB）")
        check("RTS_TASKS" in txt, "desktop/main.js 注入 RTS_TASKS（动作面开启）")

    # 5) 安装包产物
    installers = sorted(DIST.glob("*.exe"))
    check(bool(installers), f"安装包：{[i.name for i in installers]}")

    # 打印结果（Windows GBK 控制台不认 emoji ⇒ 用 ASCII 标记）
    print("\n=== desktop dist verify ===")
    for ok, msg in results:
        print(f"  [{'OK' if ok else 'FAIL'}] {msg}")
    failed = [m for ok, m in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("\nGAPS:")
        for m in failed:
            print(f"   - {m}")
        return 1
    print("PASS: dist structure ok (runtime live-db / task panel still need a real run)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
