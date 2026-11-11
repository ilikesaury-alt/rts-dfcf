"""`python -m scanner.ai_digest` —— AI 调研 / 榜外异动段分析（**手动触发**，2026-11-03）。

两种模式（2026-11-03 用户决策，决策记录见 AGENTS.md）：

- `analyze`（**默认**）：综合分析 + 筛选值得关注/买入候选，给出排序与理由。
  ⚠ 这是有意打破本项目「无第二结论源」纪律的产物（个人使用）。代价是明确的：
  红线词表在这个模式下**不生效**（拦了就没法给候选），输出是**模型主观判断**，
  标签也如实写成「非系统结论」。唯一保留的确定性闸是**幻觉巡检**：
  正文里出现的每个数字都必须能在素材里找到，找不到就当场标出。
- `transcribe`：纯转述（云端 `/api/ai-explain` 同款纪律，过全部红线槽位）。

第三种入口：**自选清单批量调研** —— 不吃榜外异动段，把用户指定的代码（≤20 只）
交给 research 调研员，模型自己查数据后出排序与判断。四种给法（后三种容忍文本里
混着名字/日期，只抽 30/00/60/68 开头的 6 位码）：
- `--symbols 300319,300666`（显式给，严格校验，写错当场报）
- `--symbols`（不带值 = 交互粘贴，空行结束）
- `--clipboard`（读 Windows 剪贴板 —— 配合扫描器终端「一键复制」按 c，一条链路）
- `--symbols-file list.txt`
根目录 `python pick.py` 是同款简写（裸代码/无参=交互）。名字用雪球批量报价解
（fail-open 拿不到就用代码本身）；幻觉巡检比对基准 = 工具输出全文；`--out` 落盘纯文本。

数据源（`--source auto` 默认），research 模式按此优先级逐级回退：
- `panel`（**默认首选，2026-11-11**）：`股票池.md` 面板原文 —— 唯一同时含 `v1 池选` 与
  `沪深飙升` 三段、且**保留全部列与各区口径**的来源（`ai_digest/panel.py` 按渲染侧列规格
  反过来切格）。模型因此看到的是「屏幕上那份列表」，而不是一个扁平代码清单。
  只有 research 模式用它：analyze/transcribe 按设计是**榜外异动段转述**。
- `print`：`offboard_print_snapshot` 终端最后一次打印的榜外行（含留存行、屏幕顺序）；
- `view`：`view_snapshot` 桌面版快照（含留存行、实时值、入选理由、板块）；
- `log`：`offboard_launch_log` 最新信号日（信号值冻结快照，无留存行）。
面板文件还没生成（终端一轮都没跑）⇒ 自动回退 print → view → log，不报错。

为什么是命令而不是「终端每轮自动打」：主循环 60s 一轮、单线程串行渲染 ⇒ 自动出网会让
「刷新频率 = 计费频率」，且一次外呼会把扫描节拍拖慢。本命令只有人敲才出网。

退出码：0 成功 / 1 未配置凭据 / 2 上游失败 / 3 触红线被拦（仅 transcribe 模式）/
4 读库失败或无数据 / 5 幻觉巡检发现编造数字（结果已打印，但退出码让你能脚本化拦截）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
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
    latest_print_snapshot,
    latest_signal_date,
    load_baseline_stats,
    load_latest_view_offboard,
    load_offboard_rows,
    render_baseline_text,
)
from scanner.config import OFFBOARD_DISPLAY_TOP, now_beijing, resolve_db_path
from scanner.terminal_copy import PANEL_ROW_RE
from scanner.utils import EXTERNAL_FAILURES


def _freshness(time_str: str, date_str: str = "") -> str:
    """终端快照时刻 → 「数据 X 分钟前 / 非实时」标注。

    用户要求输入是「最新的终端打印信息」：时刻必须可见，且午休主循环不刷 B 段时要
    明说非实时，不能让几十分钟前的快照看起来像刚刚的行情。

    ⚠ 必须带**日期**判定（2026-11-11 修）：此前只拿 `HH:MM:SS` 与当前时钟相减，
    `max(0, ...)` 把跨日的负值吞成 0 ⇒ 昨天 14:55 的快照在「今天 13:59」被标成
    「数据 0 分钟前」。而 `latest_print_snapshot` 是按 `date DESC` 取的，今天没跑扫描器
    时它返回的就是昨天的快照 —— 正好落进这个假新鲜陷阱。
    """
    if not time_str or ":" not in time_str:
        return ""
    try:
        hh, mm, ss = (int(x) for x in time_str.split(":"))
        now = now_beijing()
        if date_str:
            from datetime import date as _date

            y, mo, dd = (int(x) for x in date_str.split("-"))
            snap_day = _date(y, mo, dd)
            if snap_day != now.date():
                # 跨日：不给「N 分钟前」，直接标日期，避免把隔夜快照读成实时。
                days = (now.date() - snap_day).days
                return f" · 快照为 {date_str} {time_str}（{days} 天前，非实时）"
        age = max(0, (now.hour * 3600 + now.minute * 60 + now.second - (hh * 3600 + mm * 60 + ss)) // 60)
        return f" · 数据 {age} 分钟前" + ("（非实时）" if age > 5 else "")
    except (ValueError, TypeError):
        return ""



def _unverified_note(unverified: list[str]) -> str:
    """幻觉巡检未通过时的用户可见文案（**单源**：三处出口逐字一致）。

    为什么要写这么细：这个告警有两类正当来源，不说清就会变成噪声 ——
    ① **模型自己做的算术**（把工具给的几个数相加/相减，如「此前 4 日合计 721 万」）；
    ② **概括四舍五入**（把 33.35 写成 33.4）。两者都不是编造。
    真正的编造是第三个特征：**基准里根本没有那个量级**。

    ⚠ 刻意**不**为 ① 做自动豁免（2026-11-11 实测）：判定「这个数 = 某几项之和」需要
    0.1% 容差才认得出 721（实际和 721.50，差 0.069%），而该容差下**真编造数字的
    误放率达 53.8%**（逐行组合枚举，400 个随机编造数实测）—— 守卫会从「偶尔误报」
    退化成「基本不设防」。宁可偶尔误报一次让人扫一眼，也不能把闸门焊死。
    """
    return (
        f"[!] 幻觉巡检：以下数字在基准里找不到（{chr(12289).join(unverified)}）。\n"
        "    常见无害情形：模型自己把几个数相加/相减（如「前几项合计」），或四舍五入；\n"
        "    需要警惕的是：这些数字在证据里**根本没有对应量级**时，即为编造。\n"
        "    核对方式：上面报告里凡出现这些数字的句子，回到「模型自己查了这些」的工具输出里找一眼。"
    )

#: 每种模式的标签（命令侧保证，**不是模型给的**）。
LABELS = {
    "research": "AI 市场调研 · 模型自行查真实数据后的判断 · 含主观成分，不构成投资建议",
    "analyze": "AI 综合分析 · 含模型主观判断 · 非系统结论，不构成投资建议",
    "transcribe": "仅解读，非投资建议",
}

_RULE = "─" * 78

#: 自选清单单只代码的形状：可选 SZ/SH 前缀 + 6 位数字。
_SYMBOL_RE = re.compile(r"^(SZ|SH)?(\d{6})$")

#: 自由文本里抽代码（剪贴板/文件/粘贴板）：可选 SZ/SH 前缀 + 6 位数字。
#: ⚠ 不带前缀时只认 30/00/60/68 开头 —— 否则日期（202611）、涨跌幅（6.4400）
#:   之类会被当成代码抽出来。
_CODE_RE = re.compile(r"(?<!\d)(?:(SZ|SH)\s*)?(\d{6})(?!\d)", re.I)

#: 单次调研的代码上限。
#: ⚠ 以前是 20 —— 那是**单次工具调用的入参上限**（报价/资金流工具一次最多 20 个），
#:   被误当成「整个清单上限」了。而模型本来就能分批调用工具（每次 20 只），
#:   面板三段加起来常常超 20 只 ⇒ 以前直接报错退出，用户根本用不了。
#:   现在放开到 50；真的太多时用 `--limit N` 取面板顺序的前 N 只。
MAX_SYMBOLS = 50


def normalize_user_symbols(text: str) -> list[str]:
    """『300319, sz300666 600519』→ ['SZ300319', 'SZ300666', 'SH600519']。

    裸 6 位码按首位推交易所：30/00 → SZ、60/68 → SH（本系统是沪深主板+创业板口径）；
    带前缀的只校验形状。去重保序，上限 20 只（报价批量接口与提示词都按 20 封顶）。
    """
    tokens = [t for t in re.split(r"[\s,，、;；]+", text or "") if t]
    out: list[str] = []
    for tok in tokens:
        m = _SYMBOL_RE.match(tok.upper())
        if not m:
            raise ValueError(f"看不懂的代码「{tok}」——要 6 位数字，或 SZ/SH 前缀 + 6 位数字")
        prefix, digits = m.groups()
        if prefix is None:
            if digits[0] in "03":
                prefix = "SZ"
            elif digits[0] == "6":
                prefix = "SH"
            else:
                raise ValueError(f"代码「{tok}」不在沪深主板/创业板口径内（30/00/60/68 开头）")
        sym = prefix + digits
        if sym not in out:
            out.append(sym)
    if not out:
        raise ValueError("没解析到任何代码")
    if len(out) > MAX_SYMBOLS:
        raise ValueError(f"一次最多 {MAX_SYMBOLS} 只")
    return out


def extract_symbols_from_text(text: str) -> tuple[list[str], list[str]]:
    """自由文本 → (代码列表, 被跳过的数字串)。

    给剪贴板 / 文件 / 粘贴板用：文本里常混着股票名字、日期、涨跌幅。
    只认 30/00/60/68 开头的 6 位码（或带 SZ/SH 前缀的）—— 否则 `20261104`
    这类日期会被当成代码抽走。其余 6 位数字串记为「跳过」报给用户看。

    ⚠ 完整面板（终端按 `c` 复制的两区原文）走**表格行代码列**专用通道：
    每行除代码外还有成交量/市值等 16 列数字，宽松扫全文会把「300000手」
    误读成 SZ300000。识别到 `# / 代码 / 名称` 列序的行时只读代码列，
    区内其它数字既不当代码也不报「跳过」（否则每行都是噪声）。
    """
    out: list[str] = []
    skipped: list[str] = []

    def _take(m: re.Match[str]) -> None:
        prefix, digits = m.group(1), m.group(2)
        if prefix:
            sym = prefix.upper() + digits
        elif digits[0] in "03":
            sym = "SZ" + digits
        elif digits[0] == "6":
            sym = "SH" + digits
        else:
            skipped.append(digits)
            return
        if sym not in out:
            out.append(sym)

    panel_codes = [m.group(1) for line in (text or "").splitlines() if (m := PANEL_ROW_RE.match(line))]
    if panel_codes:
        for code in panel_codes:
            cm = _CODE_RE.fullmatch(code)
            if cm:
                _take(cm)
            else:
                skipped.append(code)
        return out, skipped

    for m in _CODE_RE.finditer(text or ""):
        _take(m)
    return out, skipped


def _read_clipboard_text() -> str:
    """读 Windows 剪贴板文本（PowerShell `Get-Clipboard`）。

    ⚠ 走临时文件而不是管道：PowerShell 重定向到管道时用控制台代码页编码，
    中文名单会乱码；让 PS 自己写 UTF-8 文件最稳。非 Windows / 失败 → 空串。
    """
    if sys.platform != "win32":
        return ""
    with tempfile.TemporaryDirectory() as td:
        out_path = os.path.join(td, "clip.txt")
        ps = f"$t = Get-Clipboard -Raw; [IO.File]::WriteAllText('{out_path}', [string]$t, [Text.Encoding]::UTF8)"
        try:
            # noqa 组：命令是我们写死的固定串（剪贴板文本不进命令行，走临时文件传递），
            # 不存在不可信输入；Windows 上 powershell 必在 PATH。
            subprocess.run(  # noqa: S603
                ["powershell", "-NoProfile", "-Command", ps],  # noqa: S607
                capture_output=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        try:
            return Path(out_path).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return ""


def _read_text_file(path: str) -> str:
    """读清单文件（UTF-8 优先，GBK 兼底）。读不到 → 抛 `OSError` 给调用方报。"""
    try:
        return Path(path).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return Path(path).read_text(encoding="gbk", errors="ignore")


def _interactive_symbols() -> tuple[list[str], list[str]]:
    """交互粘贴：多行输入，空行（或 EOF）结束。"""
    print("粘贴或输入股票代码（逗号/空格/换行分隔；空行或 Ctrl-D 结束）：")
    chunks: list[str] = []
    for line in sys.stdin:
        line = line.strip()
        if not line:
            break
        chunks.append(line)
    if not chunks:
        return [], []
    return extract_symbols_from_text(" ".join(chunks))


def _resolve_input_symbols(args: argparse.Namespace) -> tuple[list[str], list[str]]:
    """按来源解析用户清单 → (代码列表, 说明行)。

    来源优先级：`--symbols-file` > `--clipboard` > `--symbols`（有值=严格解析；
    无值=交互粘贴）。代码来自剪贴板/文件/粘贴板时用宽松抽码（容忍名字/日期混在里面）。
    """
    if getattr(args, "symbols_file", None):
        text = _read_text_file(args.symbols_file)
        syms, skipped = extract_symbols_from_text(text)
        return syms, [f"清单文件 {args.symbols_file}", *_skip_note(skipped)]
    if getattr(args, "clipboard", False):
        text = _read_clipboard_text()
        if not text.strip():
            raise ValueError("读不到剪贴板文本（剪贴板为空，或不在 Windows 上）")
        syms, skipped = extract_symbols_from_text(text)
        return syms, ["剪贴板", *_skip_note(skipped)]
    if args.symbols is None:
        raise ValueError("没给清单（用 --symbols / --clipboard / --symbols-file）")
    if args.symbols == "":
        syms, skipped = _interactive_symbols()
        return syms, ["交互输入", *_skip_note(skipped)]
    return normalize_user_symbols(args.symbols), []


def _skip_note(skipped: list[str]) -> list[str]:
    """被跳过的数字串说明（让人知道「为什么少了几个」）。"""
    if not skipped:
        return []
    head = "、".join(skipped[:8])
    more = f"…共 {len(skipped)} 个" if len(skipped) > 8 else ""
    return [f"跳过 {len(skipped)} 个非 A 股 6 位数字串（{head}{more}）"]


def _resolve_names(symbols: list[str]) -> dict[str, str]:
    """雪球批量报价解名字。fail-open：拿不到就用代码本身（不挡调研）。"""
    from scanner.utils import EXTERNAL_FAILURES

    try:
        from scanner.api import fetch_hot_quotes_batch, make_session

        quotes = fetch_hot_quotes_batch(make_session(), symbols)
        return {s: str((quotes.get(s) or {}).get("name") or s) for s in symbols}
    except EXTERNAL_FAILURES:
        return {s: s for s in symbols}


def _run_symbols(args: argparse.Namespace) -> int:
    """`--symbols` 入口：自选清单批量调研（research 模式专属，不读榜外段）。"""
    from scanner.ai_digest.llm import AIUpstreamError, magpie_config

    if args.mode != "research":
        print("[!] --symbols 只支持 --mode research（默认）；analyze/transcribe 的数据源是榜外异动段。")
        return 3

    try:
        symbols, notes = _resolve_input_symbols(args)
    except (ValueError, OSError) as e:
        print(f"[!] {e}")
        return 3
    if not symbols:
        print("[!] 没解析到任何股票代码。")
        return 3
    if len(symbols) > MAX_SYMBOLS:
        print(f"[!] 一次最多 {MAX_SYMBOLS} 只（当前 {len(symbols)} 只），请分批或加 --limit N。")
        return 3
    # `--limit N` 取面板顺序的前 N 只（面板顺序 = 优先级顺序，先砍后面的）。
    limit = getattr(args, "limit", None)
    if limit and limit > 0 and len(symbols) > limit:
        notes = [*notes, f"清单共 {len(symbols)} 只，按 --limit 只取前 {limit} 只"]
        symbols = symbols[:limit]

    cfg = magpie_config()
    if cfg is None:
        print("[!] 未配置模型服务凭据（MAGPIE_BASE_URL / MAGPIE_API_KEY / MAGPIE_MODEL）。")
        print("    放 .env 后重试。")
        return 1

    from scanner.ai_digest.agent import run_research
    from scanner.ai_digest.material import unverified_numbers
    from scanner.ai_digest.research import ResearchContext

    print(f"[i] 自选清单 {len(symbols)} 只（模型 {cfg.model}，最多 {args.max_calls} 次工具调用）…", file=sys.stderr)
    for note in notes:
        print(f"[i] 来源：{note}", file=sys.stderr)
    names = _resolve_names(symbols)
    named = {s: n for s, n in names.items() if n != s}
    if named:
        print("[i] " + "、".join(f"{s} {n}" for s, n in named.items()), file=sys.stderr)

    conn = connect_readonly(resolve_db_path())
    try:
        ctx = ResearchContext(conn=conn)
        rows = [{"symbol": s, "name": names.get(s, s)} for s in symbols]
        try:
            result = run_research(
                cfg,
                rows,
                ctx,
                max_rounds=args.max_rounds,
                max_calls=args.max_calls,
                # `_run_symbols` 会被测试直接喂手工构造的 Namespace（没有 brief 字段）；
                # 与上面 `limit = getattr(args, "limit", None)` 同款防御，避免 AttributeError。
                brief=getattr(args, "brief", False),
                origin="用户指定的自选清单",
                summarize_always=getattr(args, "summarize_always", False),
            )
        except AIUpstreamError as e:
            print(f"[!] {e}")
            return 2
    finally:
        conn.close()

    # 幻觉巡检：比对基准 = 全部工具输出（模型能引用的一切真实数据；
    # 提示词里的清单只有代码与名字，数字只可能来自工具输出）。
    unverified = unverified_numbers(result.text, result.tool_output_blob)

    if args.json:
        _emit(
            {
                "ok": True,
                "mode": "research",
                "source": "symbols",
                "label": LABELS["research"],
                "text": result.text,
                "unverified_numbers": unverified,
                "tool_summary": result.tool_summary(),
                "symbols": [{"symbol": s, "name": names.get(s, s)} for s in symbols],
            }
        )
        return 5 if unverified else 0

    print(_RULE)
    print("自选清单 · AI 调研")
    print(LABELS["research"])
    print(_RULE)
    print(
        f"清单：{'、'.join(f'{s} {names.get(s, s)}' for s in symbols)}"
        f" · {len(result.tool_calls)} 次工具调用 / {result.rounds} 轮"
    )
    print()
    print(result.text)
    if unverified:
        print()
        print(_unverified_note(unverified))
    if result.tool_calls:
        print()
        print(f"模型自己查了这些（{result.tool_summary()}）：")

    out_path = getattr(args, "out", None)
    if out_path:
        _save_report(out_path, symbols, names, result, unverified)
        print(f"\n[i] 报告已存 {out_path}")
    return 5 if unverified else 0


def _save_report(
    out_path: str,
    symbols: list[str],
    names: dict[str, str],
    result: Any,
    unverified: list[str],
) -> None:
    """报告落盘（纯文本，同终端内容去掉 ANSI；不进库、不进飞书）。"""
    lines = [
        _RULE,
        "自选清单 · AI 调研",
        LABELS["research"],
        _RULE,
        f"清单：{'、'.join(f'{s} {names.get(s, s)}' for s in symbols)}"
        f" · {len(result.tool_calls)} 次工具调用 / {result.rounds} 轮",
        "",
        result.text,
    ]
    if unverified:
        lines += ["", _unverified_note(unverified)]
    if result.tool_calls:
        lines += ["", f"模型查询记录（{result.tool_summary()}）"]
    Path(out_path).write_text("\n".join(lines) + "\n", encoding="utf-8")


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
        description=(
            "AI 调研（手动触发）：默认 research=吃终端面板原文(v1 池选+沪深飙升)，"
            "模型自己联网查数据后出首选/次选；analyze/transcribe=榜外异动（B 段）的算账/纯转述"
        ),
    )
    p.add_argument(
        "--mode",
        choices=("research", "analyze", "transcribe"),
        default="research",
        help="research=模型自己查真实市场数据后给判断（默认）；analyze=按历史基准率算账；transcribe=纯转述",
    )
    p.add_argument(
        "--symbols",
        nargs="?",
        const="",
        help=("自选清单：给代码（--symbols 300319,300666）或直接给剪贴板内容；只写 --symbols 不带值 = 交互粘贴模式"),
    )
    p.add_argument(
        "--clipboard",
        action="store_true",
        help="从 Windows 剪贴板读清单（配合终端「一键复制」按 c 用；文本里的名字/日期会自动跳过）",
    )
    p.add_argument("--symbols-file", help="从文本文件读清单（一行一个代码或名字）")
    p.add_argument("--out", help="把简洁报告存到该文件（配合 --brief；默认只打印）")
    p.add_argument(
        "--brief",
        action="store_true",
        help="简洁报告：每票一行、最多 12 行，不打印查询记录/素材核对表（幻觉巡检与红线照旧）",
    )
    p.add_argument(
        "--summarize-always",
        action="store_true",
        help="research 模式：结论一律由独立的「总结器」重写一遍（更简洁，但多一次调用 ≈ 慢 20~30s）",
    )
    p.add_argument("--max-rounds", type=int, default=8, help="research 模式最多几轮工具调用（默认 8）")
    p.add_argument("--max-calls", type=int, default=30, help="research 模式最多几次工具调用（默认 30）")
    p.add_argument(
        "--no-prefetch-features",
        dest="prefetch_features",
        action="store_false",
        help="research 模式：不把本地特征（5日/10日累计、距20日高点、量能、最近2条公告）"
        "预加载进提示词，改由模型自己调 get_kline/get_announcements（默认预加载）",
    )
    p.add_argument(
        "--source",
        choices=("auto", "panel", "print", "view", "log"),
        default="auto",
        help="auto/panel=终端面板原文（股票池.md：v1 池选 + 沪深飙升三段，保留全部列与各区口径）；"
        "print=终端最后一次打印的榜外行；view=桌面版 view_snapshot；log=launch_log 整日过门账",
    )
    p.add_argument("--date", help="信号日 YYYY-MM-DD（仅 log 源；默认取最新）")
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help=(
            f"只分析前 N 只（自选清单模式：取面板顺序的前 N 只；榜外段模式：段内顺序前 N 只，"
            f"默认 {OFFBOARD_DISPLAY_TOP}）"
        ),
    )
    p.add_argument("--materials", action="store_true", help="只打素材（逐项 id|label|value），不出网")
    p.add_argument("--demo", action="store_true", help="用内置样例跑（零库零网）")
    p.add_argument("--json", action="store_true", help="机器可消费输出（含 unverified_numbers 幻觉巡检结果）")
    return p.parse_args(argv)


def _resolve_rows(args: argparse.Namespace) -> dict[str, Any]:
    """→ {"date", "rows", "source", "time", "market"}。读库失败/无数据抛 `OffboardSourceError`。

    源优先级（`--source print` 默认）：`print`（终端最后一次打印）→ `view`（桌面版快照）
    → `log`（launch_log 整日累加）。逐级回退是为了「今天没跑扫描器」时仍能出结果，
    但**默认永远优先屏幕上的那几只** —— 用户的口径就是「我看到什么就分析什么」。
    """
    if args.demo:
        return {
            "date": "（内置样例）",
            "rows": [dict(r) for r in DEMO_ROWS],
            "source": "demo",
            "time": "",
            "market": DEMO_MARKET,
        }

    conn = connect_readonly(resolve_db_path())
    baseline = render_baseline_text(
        load_baseline_stats(conn),
        label_map={"vol_ratio": "量比", "main_pct": "主力净占比", "accum_5d": "5日累计", "amount": "成交额"},
    )
    try:
        # ⓪ panel 源（**默认优先**）：终端面板原文 —— 唯一同时含 v1 池选 与 沪深飙升三段、
        #    且保留全部列与各区口径的来源。用户口径 =「分析我屏幕上那份」，故 auto 先试它。
        #    ⚠ 只服务 `research` 模式：analyze/transcribe 按设计就是**榜外异动段转述**，
        #      它们吃的是 B 段素材（冻结信号字段），面板行喂不进 `build_materials`。
        if args.source in ("panel", "auto") and args.mode == "research":
            from scanner.ai_digest.panel import load_panel_md

            panel_snap, panel_path = load_panel_md()
            if panel_snap is not None and panel_snap.total:
                return {
                    "date": panel_snap.date,
                    "rows": [r for reg in panel_snap.regions for r in reg.rows],
                    "source": "panel",
                    "time": panel_snap.time,
                    "market": None,
                    "baseline": baseline,
                    "panel": panel_snap,
                    "panel_path": panel_path,
                }
            if args.source == "panel":
                raise OffboardSourceError(
                    f"面板原文不可用（{panel_path}）—— 终端还没跑过一轮，或 RTS_PANEL_MD=0 关了存档。"
                    " 可加 --source log 读当日榜外过门账。"
                )

        # ① print 源：终端最后一次打印的那几行（含留存行，顺序即屏幕顺序）
        if args.source in ("print", "auto"):
            snap = latest_print_snapshot(conn)
            if snap is not None:
                date, time_str, rows, seq = snap
                return {
                    "date": date,
                    "rows": rows,
                    "source": "print",
                    "time": time_str,
                    "seq": seq,
                    "market": None,
                    "baseline": baseline,
                }
            if args.source == "print":
                raise OffboardSourceError(
                    "offboard_print_snapshot 里没有数据（终端还没跑过榜外异动段，或库里是未迁移的老库）。"
                    " 加 --source log 可直接读当日过门账。"
                )

        # ② view 源（桌面版快照）
        view_snap = load_latest_view_offboard(conn)
        if args.source in ("view", "auto") and view_snap is not None:
            date, time_str, rows, market = view_snap
            return {
                "date": date,
                "rows": rows,
                "source": "view",
                "time": time_str,
                "market": market,
                "baseline": baseline,
            }
        if args.source == "view":
            raise OffboardSourceError("view_snapshot 里没有可用的榜外异动行（扫描器一轮都没跑过？）")

        # ③ log 源（launch_log 整日累加）
        log_date = args.date or latest_signal_date(conn)
        if not log_date:
            raise OffboardSourceError("offboard_print_snapshot / view_snapshot / offboard_launch_log 都没有数据")
        rows = load_offboard_rows(conn, log_date)
        if not rows:
            raise OffboardSourceError(f"{log_date} 没有过门行（该日榜外段可能整段为空）")
        return {"date": log_date, "rows": rows, "source": "log", "time": "", "market": None, "baseline": baseline}
    finally:
        conn.close()


def _force_utf8_stdout() -> None:
    """win32 控制台默认 GBK：输出含 ⚠ / · / ─ 等非 GBK 字符，直接 print 会抛。

    只在本命令 `main()` 里调（不入模块顶层）：测试捕获的 stdout 不应被改写。
    `getattr` 模式同 `scripts/offboard_nextday.py` —— `sys.stdout` 静态类型是 TextIO
    （无 `reconfigure`），运行时的 TextIOWrapper 才有；pythonw 下 stdout 可能为 None。
    stderr 同改：`[i]` 进度行都走 stderr，不改就是一屏乱码。
    """
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def _emit(obj: dict[str, Any]) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdout()
    args = _parse_args(argv)
    _load_env()

    if args.symbols is not None or args.clipboard or args.symbols_file:
        return _run_symbols(args)

    try:
        info = _resolve_rows(args)
    except OffboardSourceError as e:
        print(f"[!] {e}")
        return 4

    mats = build_materials(
        info["date"],
        info["rows"],
        limit=args.limit if args.limit and args.limit > 0 else OFFBOARD_DISPLAY_TOP,
        mode=args.mode,
        market=info.get("market"),
        baseline=info.get("baseline"),
    )
    # 面板源的「共 N 只 / 本次 M 只」必须与**模型实际收到的**一致（`--limit` 按每区截断）。
    # 不能用 `build_materials` 的 `mats.symbols` —— 那是 B 段素材口径，面板行喂不进它。
    if info["source"] == "panel" and info.get("panel") is not None:
        _panel = info["panel"]
        _lim = args.limit if args.limit and args.limit > 0 else None
        total = _panel.total
        shown = sum(min(len(r.rows), _lim) if _lim else len(r.rows) for r in _panel.regions)
        if _lim:
            from scanner.ai_digest.panel import limit_per_region

            info["panel"] = limit_per_region(_panel, _lim)
    else:
        shown, total = len(mats.symbols), len(info["rows"])
    source_desc = {
        "panel": f"面板原文 {info['date']} {info['time']}"
        f"（{info.get('panel_path')} · {(len(info['panel'].regions) if info.get('panel') else 0)} 个区 · "
        f"含 v1 池选与沪深飙升三段）{_freshness(info.get('time') or '', info['date'])}",
        "print": f"offboard_print_snapshot {info['date']} {info['time']} seq={info.get('seq') or '—'}"
        f"（终端最后一次打印的行，顺序即屏幕顺序）{_freshness(info.get('time') or '', info['date'])}",
        "view": f"view_snapshot {info['date']} {info['time']}（桌面版快照 · 含留存行/实时值）",
        "log": f"offboard_launch_log {info['date']}（整日过门账 · 信号值冻结快照）",
        "demo": "内置样例",
    }[info["source"]]

    if args.materials:
        print(f"模式 {args.mode} · 数据源 {source_desc} · 共 {total} 行 · 本次 {shown} 只")
        if info["source"] == "panel" and args.mode == "research":
            # 面板源下 `build_materials` 那套 B 段素材字段（tier/percent/vol_ratio…）是空的 ——
            # 面板列名与 B 段列名不是一套。这里直接打**模型真正会收到的提示词**，
            # 那才是「素材」在 research 模式下的含义。
            from scanner.ai_digest.agent import build_research_prompt
            from scanner.ai_digest.features import build_features

            _panel = info.get("panel")
            _rows = [dict(r) for reg in _panel.regions for r in reg.rows] if _panel else list(info["rows"])
            _feats = None
            if args.prefetch_features:
                try:
                    _c = connect_readonly(resolve_db_path())
                    try:
                        _feats = build_features(_c, [str(r.get("symbol")) for r in _rows if r.get("symbol")])
                    finally:
                        _c.close()
                except EXTERNAL_FAILURES as _e:
                    print(f"[!] 本地特征预加载跳过: {type(_e).__name__}: {_e}", file=sys.stderr)
            print("")
            print(
                build_research_prompt(
                    _rows, "终端面板原文", panel=_panel, brief=args.brief, features=_feats
                )
            )
            return 0
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

    tool_summary = ""
    try:
        if args.mode == "research":
            # research 模式：模型自己调工具查真实数据（agent.run_research）。
            # 提示词由 `agent.build_research_prompt` **单一构造**，同一份文本同时用于
            # ① 发给模型 ② 幻觉巡检的比对基准 —— 两处各自拼一份就会漂移（历史上
            #    CLI 用 analyze 模板当基准，模型从未见过的 ff_pct 数字被判「没编造」）。
            from scanner.ai_digest.agent import build_research_prompt, run_research
            from scanner.ai_digest.research import ResearchContext

            print(
                f"[i] 调研模式：模型将自行调用工具查市场数据（模型 {cfg.model}，最多 {args.max_calls} 次调用）…",
                file=sys.stderr,
            )
            panel = info.get("panel")
            # `--limit` 必须真的生效：此前只作用于 mats（打给人看的那份），而喂给模型的
            # 是 info["rows"] 全量 ⇒ 界面说「本次 12 只」、模型却收到 27 只，还会被巡检出
            # 「第 20 只」这种越界序号当编造。
            # `--limit` 已在上面的显示侧按单源 `limit_per_region` 裁过 info["panel"]，
            # 这里直接用同一份 —— 两处各裁一次就是口径漂移的入口。
            if panel is not None:
                rows_for_model = [dict(r) for reg in panel.regions for r in reg.rows]
            else:
                rows_for_model = [dict(r) for r in info["rows"]]
            conn = connect_readonly(resolve_db_path())
            try:
                # 本地特征预加载：**必须在建提示词之前**算好，因为同一份文本既发给模型
                # 又当幻觉巡检基准 —— 若模型收到带特征的版本、巡检却比不带特征的版本，
                # 那些「5日 +24.6%」会被判成编造（正是这里历史上的翻车方式）。
                from scanner.ai_digest.features import build_features

                _feats = None
                if args.prefetch_features:
                    try:
                        _feats = build_features(
                            conn, [str(r.get("symbol")) for r in rows_for_model if r.get("symbol")]
                        )
                    except EXTERNAL_FAILURES as _e:  # 预加载失败不拦调研
                        print(f"[!] 本地特征预加载跳过: {type(_e).__name__}: {_e}", file=sys.stderr)
                user_prompt_research = build_research_prompt(
                    rows_for_model,
                    "终端面板原文" if panel is not None else "终端数据",
                    panel=panel,
                    brief=args.brief,
                    features=_feats,
                )
                # ⚠ 工具自己懒加载雪球 session（`ResearchContext.get_session`）——
                #   适配器没有公开 session 属性，取 `.session` 会 AttributeError。
                ctx = ResearchContext(conn=conn)
                result = run_research(
                    cfg,
                    rows_for_model,
                    ctx,
                    max_rounds=args.max_rounds,
                    max_calls=args.max_calls,
                    brief=args.brief,
                    panel=panel,
                    user_prompt=user_prompt_research,
                    summarize_always=args.summarize_always,
                    # 已在上面的 `user_prompt_research` 里预加载过；再让 run_research 算一遍
                    # 既浪费一次全表查询，又会因两条路径算出的特征不同而让巡检基准漂移。
                    prefetch_features=False,
                )
            finally:
                conn.close()
            text = result.text
            tool_summary = result.tool_summary()
            tool_blob = result.tool_output_blob
            print(
                f"[i] 调研完成：{len(result.tool_calls)} 次工具调用 / {result.rounds} 轮"
                f"（报告：{'总结器重构' if result.summarized else '调研阶段直出'}）",
                file=sys.stderr,
            )

        else:
            tool_blob = ""
            print(f"[i] 已发出请求（模式 {args.mode} · 模型 {cfg.model}；实测单次 10~90s，请稍等）…", file=sys.stderr)
            text = transcribe(cfg, mats, mode=args.mode, brief=args.brief)
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

    if args.mode == "research":
        # research 的比对基准 = **模型实际收到的提示词**（同一份）+ 全部工具输出。
        # 不再用 `build_messages(mats, ...)` —— 那是 analyze/transcribe 的素材模板，
        # research 模式下模型收不到它 ⇒ 拿它当基准会放过「模型编了它从未见过的字段」
        # （实测 ff_pct=7.77 / price=10.0 / first_time=09:45:00 全被漏判）。
        check_blob = user_prompt_research + chr(10) + tool_blob
    else:
        check_blob = build_messages(mats, args.mode, brief=args.brief)[1]["content"]
    unverified = unverified_numbers(text, check_blob)

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
                "tool_summary": tool_summary,
                "summarized": bool(getattr(result, "summarized", False)),
                "materials": [{"id": i.id, "label": i.label, "value": i.value} for i in mats.items],
                "missing": list(mats.missing),
                "generatedFrom": {
                    "date": info["date"],
                    "time": info.get("time") or None,
                    "print_seq": info.get("seq"),
                    "brief": args.brief,
                    "symbols": list(mats.symbols),
                },
            }
        )
        if redlined:
            return 3
        return 5 if unverified else 0

    # `--brief`：一屏报告（不打印查询记录 / 素材核对表）。幻觉巡检与红线照旧，
    # 只是把「命令侧附加内容」收起来；`--out` 落的是这一份，不是原来的长报告。
    # ⚠ 默认模式是 research，别把它标成「AI 转述」（那是 transcribe 的标题）。
    # research 模式的输入可以是「面板原文」（v1 池选 + 沪深飙升三段）或自选清单，
    # 都不再只是榜外异动段 ⇒ 标题按实际来源写，别让报告标题与内容对不上。
    _research_title = (
        "终端面板 · AI 调研（v1 池选 + 沪深飙升）" if info["source"] == "panel" else "自选清单 · AI 调研"
    )
    report_title = {
        "analyze": "榜外异动 · AI 综合分析",
        "research": _research_title,
        "transcribe": "榜外异动 · AI 转述",
    }.get(args.mode, "榜外异动 · AI 分析")
    if args.brief:
        brief_lines = [
            _RULE,
            report_title,
            LABELS[args.mode],
            _RULE,
            f"数据源 {source_desc} · 共 {total} 行 · 本次 {shown} 只",
            "",
            text,
        ]
        if unverified:
            brief_lines += ["", _unverified_note(unverified)]
        report = "\n".join(brief_lines) + "\n"
        if args.out:
            Path(args.out).write_text(report, encoding="utf-8")
            print(f"[报告] {args.out}", file=sys.stderr)
        print(report, end="")
        if redlined:
            return 3
        return 5 if unverified else 0

    print(_RULE)
    print(report_title)
    print(LABELS[args.mode])
    print(_RULE)
    print(f"数据源 {source_desc} · 共 {total} 行 · 本次 {shown} 只（段内顺序与终端同源）")
    print()
    print(text)
    if unverified:
        print()
        print(_unverified_note(unverified))
    if tool_summary:
        print()
        print(f"模型自己查了这些（{tool_summary}）：")
    print()
    print("素材核对（模型看到的初始数据；逐行可对）：")
    print(render_materials_block(mats))
    if mats.missing:
        print(f"\n缺失项（素材里是「暂无数据」，模型不得补）：{'、'.join(mats.missing)}")
    if redlined:
        return 3
    return 5 if unverified else 0


if __name__ == "__main__":
    raise SystemExit(main())
