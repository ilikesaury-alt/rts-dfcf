"""`python -m scanner.ai_digest` —— 榜外异动段的 AI 转述（**手动触发**，2026-11-03）。

为什么是命令而不是「终端每轮自动打」：终端主循环 60s 一轮、单线程串行渲染 ⇒ 自动出网会让
「刷新频率」= 「计费频率」，且一次 20s 外呼会把扫描节拍整体拖慢。本命令只有人敲才出网，
用一次算一次。

输出三段，**缺一不可**：
1. 标签行（`仅解读，非投资建议`）—— 由本文件给，**不是模型给的**（模型被要求不复述提示词）；
2. 转述正文 —— 模型对素材的改写，原样打印、不摘要不重写；
3. 素材核对 —— 一只票一行，就是模型看到的那份数据。**这一段是「人能发现解读与数据不符」
   的唯一手段**（云端 UI 用可折叠表格做同一件事）。

退出码：0 成功 / 1 未配置凭据 / 2 上游失败 / 3 转述触红线被拦 / 4 读库失败或无数据。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from scanner.ai_digest.guard import REFUSAL, is_redlined
from scanner.ai_digest.material import (
    build_materials,
    render_material_items,
    render_materials_block,
)
from scanner.ai_digest.source import OffboardSourceError, connect_readonly, latest_signal_date, load_offboard_rows
from scanner.config import OFFBOARD_DISPLAY_TOP, resolve_db_path

#: 每个响应都必须带的标签（服务端/本命令保证；模型不写这句）。
LABEL = "仅解读，非投资建议"

_RULE = "─" * 78

#: 内置样例（`--demo`）：零库、零网，用来验证「素材 → 提示词 → 红线」整条链路。
#: 刻意含一条缺字段行（`accum_5d` 为 None）与一条未登记层，覆盖两条降级路径。
DEMO_ROWS: tuple[dict[str, Any], ...] = (
    {
        "symbol": "SZ300758",
        "name": "七彩化学",
        "tier": "T2",
        "percent": 4.1,
        "accum_5d": 8.42,
        "vol_ratio": 2.31,
        "main_pct": 3.2,
        "amount": 3.2e8,
        "float_cap": 2.53e9,
        "price": 15.2,
        "first_time": "09:47:12",
    },
    {
        "symbol": "SZ301171",
        "name": "易点天下",
        "tier": "T1",
        "percent": 1.9,
        "accum_5d": None,
        "vol_ratio": 1.85,
        "main_pct": 1.1,
        "amount": 1.1e8,
        "float_cap": 6.8e9,
        "price": 21.4,
        "first_time": "10:06:55",
    },
    {
        "symbol": "SZ300999",
        "name": "样例票",
        "tier": "T3",
        "percent": 2.5,
        "accum_5d": 3.0,
        "vol_ratio": 1.6,
        "main_pct": 0.4,
        "amount": 0.9e8,
        "float_cap": 1.2e9,
        "price": 9.8,
        "first_time": "13:21:03",
    },
)


def _load_env() -> None:
    """`.env`（`RTS_ENV_FILE` 可指路径）—— 与 `unified_scanner.py` 同款加载方式。"""
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - 环境相关
        return
    env_file = os.environ.get("RTS_ENV_FILE")
    if env_file:
        load_dotenv(env_file)
    else:
        load_dotenv()


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m scanner.ai_digest",
        description="榜外异动（B 段）AI 转述：只转述、不给建议；手动触发，不轮询。",
    )
    p.add_argument("--date", help="信号日 YYYY-MM-DD（默认：offboard_launch_log 里最新的那天）")
    p.add_argument(
        "--limit",
        type=int,
        default=OFFBOARD_DISPLAY_TOP,
        help=f"转述前 N 只（按段内顺序；默认 {OFFBOARD_DISPLAY_TOP} = 终端展示行数）",
    )
    p.add_argument("--materials", action="store_true", help="只打素材（逐项 id|label|value），不出网")
    p.add_argument("--demo", action="store_true", help="用内置样例跑（零库零网）")
    p.add_argument("--json", action="store_true", help="机器可消费输出（结构与云端 /api/ai-explain 对齐）")
    return p.parse_args(argv)


def _resolve_rows(args: argparse.Namespace) -> tuple[str, list[dict[str, Any]]]:
    """→ (信号日, 该日全部过门行)。读库失败/无数据抛 `OffboardSourceError`。"""
    if args.demo:
        return "（内置样例）", [dict(r) for r in DEMO_ROWS]

    conn = connect_readonly(resolve_db_path())
    try:
        date = args.date or latest_signal_date(conn)
        if not date:
            raise OffboardSourceError("offboard_launch_log 还没有任何信号日（扫描器未跑过榜外段？）")
        rows = load_offboard_rows(conn, date)
    finally:
        conn.close()
    if not rows:
        raise OffboardSourceError(f"{date} 没有过门行（该日榜外段可能整段为空）")
    return date, rows


def _emit(obj: dict[str, Any]) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _force_utf8_stdout() -> None:
    """win32 控制台默认 GBK：输出含 ⚠ / · / ─ 等非 GBK 字符，直接 print 会抛。

    只在本命令 `main()` 里调（不入模块顶层）：测试捕获的 stdout 不应被改写。
    `getattr` 模式同 `scripts/offboard_nextday.py` —— `sys.stdout` 静态类型是 TextIO
    （无 `reconfigure`），运行时的 TextIOWrapper 才有；pythonw 下 stdout 可能为 None。
    """
    if sys.platform != "win32":
        return
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdout()
    args = _parse_args(argv)
    _load_env()

    try:
        date, rows = _resolve_rows(args)
    except OffboardSourceError as e:
        print(f"[!] {e}")
        return 4

    mats = build_materials(date, rows, limit=args.limit)
    shown, total = len(mats.symbols), len(rows)

    if args.materials:
        print(f"信号日 {date} · 过门 {total} 只 · 本次素材 {shown} 只")
        print(render_material_items(mats))
        return 0

    from scanner.ai_digest.llm import AIUpstreamError, magpie_config, transcribe

    cfg = magpie_config()
    if cfg is None:
        if args.json:
            _emit({"ok": False, "error": {"code": "AI_UNAVAILABLE", "message": "未配置 MAGPIE_* 三个环境变量"}})
        else:
            print("[!] 未配置模型服务凭据（MAGPIE_BASE_URL / MAGPIE_API_KEY / MAGPIE_MODEL）。")
            print("    放 .env 后重试；只想看素材可加 --materials（不出网）。")
            print(f"\n[素材] 信号日 {date} · 过门 {total} 只 · 本次素材 {shown} 只")
            print(render_materials_block(mats))
        return 1

    try:
        # 真实网关后面是推理模型：一句话就要 25s+，全段素材可达 1~2 分钟。
        # 提示写 stderr（不污染 --json 的 stdout），否则用户会以为卡死。
        print(f"[i] 已发出请求（模型 {cfg.model}；推理模型实测单次 25~90s，请稍等）…", file=sys.stderr)
        text = transcribe(cfg, mats)
    except AIUpstreamError as e:
        if args.json:
            _emit({"ok": False, "error": {"code": "AI_UPSTREAM", "message": str(e)}})
        else:
            print(f"[!] {e}")
            print(f"\n[素材] 信号日 {date} · 过门 {total} 只 · 本次素材 {shown} 只")
            print(render_materials_block(mats))
        return 2

    redlined = is_redlined(text)
    if redlined:
        # 日志只记「发生过一次拒答」，不记模型原文（避免红线内容进日志/终端回滚缓冲区）。
        print("[!] 转述触及红线，已整段替换为拒答文案（模型原文不展示）。")
        text = REFUSAL

    if args.json:
        _emit(
            {
                "ok": True,
                "symbol": None,
                "label": LABEL,
                "text": text,
                "redlined": redlined,
                "cached": False,
                "materials": [{"id": i.id, "label": i.label, "value": i.value} for i in mats.items],
                "missing": list(mats.missing),
                "generatedFrom": {"date": date, "symbols": list(mats.symbols)},
            }
        )
        return 3 if redlined else 0

    print(_RULE)
    print("榜外异动 · AI 转述")
    print(LABEL)
    print(_RULE)
    print(f"信号日 {date} · 过门 {total} 只 · 本次转述前 {shown} 只（段内顺序与终端同源）")
    print()
    print(text)
    print()
    print("素材核对（模型只看得到这些；逐行可对）：")
    print(render_materials_block(mats))
    if mats.missing:
        print(f"\n缺失项（素材里是「暂无数据」，模型不得补）：{'、'.join(mats.missing)}")
    return 3 if redlined else 0


if __name__ == "__main__":
    raise SystemExit(main())
