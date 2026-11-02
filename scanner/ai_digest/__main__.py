"""`python -m scanner.ai_digest` —— 榜外异动段的 AI 分析（**手动触发**，2026-11-03）。

两种模式（2026-11-03 用户决策，决策记录见 AGENTS.md）：

- `analyze`（**默认**）：综合分析 + 筛选值得关注/买入候选，给出排序与理由。
  ⚠ 这是有意打破本项目「无第二结论源」纪律的产物（个人使用）。代价是明确的：
  红线词表在这个模式下**不生效**（拦了就没法给候选），输出是**模型主观判断**，
  标签也如实写成「非系统结论」。唯一保留的确定性闸是**幻觉巡检**：
  正文里出现的每个数字都必须能在素材里找到，找不到就当场标出。
- `transcribe`：纯转述（云端 `/api/ai-explain` 同款纪律，过全部红线槽位）。

数据源（`--source auto` 默认）：取**更新的**那个 ——
- `view`：`view_snapshot` 最近一轮终端打印的载荷（含留存行、实时值、入选理由、板块）；
- `log`：`offboard_launch_log` 最新信号日（信号值冻结快照，无留存行）。
扫描器今天跑过 ⇒ view 快照就是「刚才终端上那份」；没跑过 ⇒ 自动回退 log。

为什么是命令而不是「终端每轮自动打」：主循环 60s 一轮、单线程串行渲染 ⇒ 自动出网会让
「刷新频率 = 计费频率」，且一次外呼会把扫描节拍拖慢。本命令只有人敲才出网。

退出码：0 成功 / 1 未配置凭据 / 2 上游失败 / 3 触红线被拦（仅 transcribe 模式）/
4 读库失败或无数据 / 5 幻觉巡检发现编造数字（结果已打印，但退出码让你能脚本化拦截）。
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
    unverified_numbers,
)
from scanner.ai_digest.source import (
    OffboardSourceError,
    connect_readonly,
    latest_signal_date,
    load_latest_view_offboard,
    load_offboard_rows,
)
from scanner.config import OFFBOARD_DISPLAY_TOP, resolve_db_path

#: 每种模式的标签（命令侧保证，**不是模型给的**）。
LABELS = {
    "analyze": "AI 综合分析 · 含模型主观判断 · 非系统结论，不构成投资建议",
    "transcribe": "仅解读，非投资建议",
}

_RULE = "─" * 78

#: 内置样例（`--demo`）：零库、零网，用来验证「素材 → 提示词 → 巡检」整条链路。
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
        "sector": "化工",
        "ff_pct": 5.6,
        "turnover_rate": 8.31,
        "beauty": "暴跌日/深回调",
        "reasons": "启动首日(累计+8.42% 今日+4.10% 量比2.31)；美感:暴跌日/深回调",
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
        "sector": "传媒",
        "ff_pct": 1.1,
        "turnover_rate": 3.02,
        "beauty": "",
        "reasons": "量先动(量比1.85 今日+1.90%)",
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

DEMO_MARKET = {"market_idx_pct": 1.24, "weak": False}


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
        description="榜外异动（B 段）AI 分析：默认综合分析+筛选候选（模型观点）；--mode transcribe 回到纯转述。",
    )
    p.add_argument(
        "--mode",
        choices=("analyze", "transcribe"),
        default="analyze",
        help="analyze=综合分析+筛选候选（默认）；transcribe=纯转述（过全部红线）",
    )
    p.add_argument(
        "--source",
        choices=("auto", "view", "log"),
        default="auto",
        help="auto=取更新的那个（默认）；view=最近一轮终端打印；log=launch_log 最新信号日",
    )
    p.add_argument("--date", help="信号日 YYYY-MM-DD（仅 log 源；默认取最新）")
    p.add_argument(
        "--limit",
        type=int,
        default=OFFBOARD_DISPLAY_TOP,
        help=f"分析前 N 只（按段内顺序；默认 {OFFBOARD_DISPLAY_TOP} = 终端展示行数）",
    )
    p.add_argument("--materials", action="store_true", help="只打素材（逐项 id|label|value），不出网")
    p.add_argument("--demo", action="store_true", help="用内置样例跑（零库零网）")
    p.add_argument("--json", action="store_true", help="机器可消费输出（含 unverified_numbers 幻觉巡检结果）")
    return p.parse_args(argv)


def _resolve_rows(args: argparse.Namespace) -> dict[str, Any]:
    """→ {"date", "rows", "source", "time", "market"}。读库失败/无数据抛 `OffboardSourceError`。"""
    if args.demo:
        return {
            "date": "（内置样例）",
            "rows": [dict(r) for r in DEMO_ROWS],
            "source": "demo",
            "time": "",
            "market": DEMO_MARKET,
        }

    conn = connect_readonly(resolve_db_path())
    snap: tuple[str, str, list[dict[str, Any]], dict[str, Any]] | None = None
    try:
        if args.source in ("auto", "view"):
            snap = load_latest_view_offboard(conn)

        if args.source == "view":
            if snap is None:
                raise OffboardSourceError("view_snapshot 里没有可用的榜外异动行（扫描器一轮都没跑过？）")
        elif args.source == "auto" and snap is not None:
            # auto：视图快照更新（= 最近一次终端打印）就用它；否则回退 log。
            log_date = latest_signal_date(conn)
            if log_date and log_date > snap[0]:
                snap = None

        if args.source != "log" and snap is not None:
            date, time_str, rows, market = snap
            return {
                "date": date,
                "rows": rows,
                "source": "view",
                "time": time_str,
                "market": market,
            }

        # log 源（或 auto 回退）
        log_date = args.date or latest_signal_date(conn)
        if not log_date:
            raise OffboardSourceError("offboard_launch_log 与 view_snapshot 都没有数据（扫描器未跑过？）")
        date = log_date
        rows = load_offboard_rows(conn, date)
    finally:
        conn.close()
    if not rows:
        raise OffboardSourceError(f"{date} 没有过门行（该日榜外段可能整段为空）")
    return {"date": date, "rows": rows, "source": "log", "time": "", "market": None}


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


def _emit(obj: dict[str, Any]) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdout()
    args = _parse_args(argv)
    _load_env()

    try:
        info = _resolve_rows(args)
    except OffboardSourceError as e:
        print(f"[!] {e}")
        return 4

    mats = build_materials(info["date"], info["rows"], limit=args.limit, mode=args.mode, market=info.get("market"))
    shown, total = len(mats.symbols), len(info["rows"])
    source_desc = {
        "view": f"view_snapshot {info['date']} {info['time']}（最近一轮终端打印 · 含留存行/实时值）",
        "log": f"offboard_launch_log {info['date']}（信号值冻结快照）",
        "demo": "内置样例",
    }[info["source"]]

    if args.materials:
        print(f"模式 {args.mode} · 数据源 {source_desc} · 共 {total} 行 · 本次 {shown} 只")
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
            print(f"\n[素材] 模式 {args.mode} · {source_desc} · 共 {total} 行 · 本次 {shown} 只")
            print(render_materials_block(mats))
        return 1

    try:
        # 真实网关后面是推理模型：一句话就要 25s+，全段素材可达 1~2 分钟。
        # 提示写 stderr（不污染 --json 的 stdout），否则用户会以为卡死。
        print(f"[i] 已发出请求（模式 {args.mode} · 模型 {cfg.model}；实测单次 10~90s，请稍等）…", file=sys.stderr)
        text = transcribe(cfg, mats, mode=args.mode)
    except AIUpstreamError as e:
        if args.json:
            _emit({"ok": False, "error": {"code": "AI_UPSTREAM", "message": str(e)}})
        else:
            print(f"[!] {e}")
            print(f"\n[素材] 模式 {args.mode} · {source_desc} · 共 {total} 行 · 本次 {shown} 只")
            print(render_materials_block(mats))
        return 2

    redlined = False
    if args.mode == "transcribe":
        redlined = is_redlined(text)
        if redlined:
            # 日志只记「发生过一次拒答」，不记模型原文（避免红线内容进日志/终端回滚缓冲区）。
            print("[!] 转述触及红线，已整段替换为拒答文案（模型原文不展示）。")
            text = REFUSAL

    # 幻觉巡检（两种模式都跑；analyze 模式它是唯一的确定性闸）。
    from scanner.ai_digest.llm import build_messages

    user_prompt = build_messages(mats, args.mode)[1]["content"]
    unverified = unverified_numbers(text, user_prompt)

    if args.json:
        _emit(
            {
                "ok": True,
                "mode": args.mode,
                "source": info["source"],
                "label": LABELS[args.mode],
                "text": text,
                "redlined": redlined,
                "cached": False,
                "unverified_numbers": unverified,
                "materials": [{"id": i.id, "label": i.label, "value": i.value} for i in mats.items],
                "missing": list(mats.missing),
                "generatedFrom": {
                    "date": info["date"],
                    "time": info.get("time") or None,
                    "symbols": list(mats.symbols),
                },
            }
        )
        if redlined:
            return 3
        return 5 if unverified else 0

    print(_RULE)
    print("榜外异动 · AI 综合分析" if args.mode == "analyze" else "榜外异动 · AI 转述")
    print(LABELS[args.mode])
    print(_RULE)
    print(f"数据源 {source_desc} · 共 {total} 行 · 本次 {shown} 只（段内顺序与终端同源）")
    print()
    print(text)
    if unverified:
        print()
        print(f"[!] 幻觉巡检：以下数字在素材里找不到（模型编造或换算，请勿采信）：{'、'.join(unverified)}")
    print()
    print("素材核对（模型只看得到这些；逐行可对）：")
    print(render_materials_block(mats))
    if mats.missing:
        print(f"\n缺失项（素材里是「暂无数据」，模型不得补）：{'、'.join(mats.missing)}")
    if redlined:
        return 3
    return 5 if unverified else 0


if __name__ == "__main__":
    raise SystemExit(main())
