"""`python -m scanner.ai_digest.crosssection` —— **横截面异动层专属 AI 分析**。

2026-11-16 Lichun 要求：「做一个针对它的 ai 分析，就是输入源只是这个区域的数据」。

## 输入源（硬性契约）

**只有 `crosssection_signal` 一张表。** 零联网、零跨表、零外部行情。
见 `crosssection_view.py` 头部的边界说明。

## 与主公用的关系

**独立命令，不进 `ai_digest.__main__` 的模式分支。** 理由：

1. 主公用的 `--source panel/print/view/log` 全部围绕 B 段榜外异动段；
   横截面层是**并列**的另一个观察层，混进同一套 source 回退链会让两边口径互相污染。
2. 素材结构完全不同（这里是分布摘要，那边是逐票行）⇒ 提示词也必须分开写。

## 纪律

- **过红线词表**（`is_redlined`）：横截面层是观察层，绝不允许输出买卖建议。
  与 analyze 模式不同，这里**不豁免**红线 —— 因为本层本就不该产生结论。
- **过幻觉巡检**（`unverified_numbers`）：比对基准 = 素材全文。
- **不判方向、不排序推荐**：素材刻意只给分布与结构，见 `crosssection_view` docstring。

## 退出码

0 成功 / 1 未配置凭据 / 2 上游失败 / 3 触红线被拦 / 4 读库失败或无数据 /
5 幻觉巡检发现编造数字
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scanner.ai_digest.archive import ArchivePayload, render_archive, write_archive
from scanner.ai_digest.crosssection_view import (
    CrosssectionSummary,
    build_summary,
    render_materials,
    resolve_date,
)
from scanner.ai_digest.guard import is_redlined
from scanner.ai_digest.llm import AIUpstreamError, chat, magpie_config
from scanner.ai_digest.material import unverified_numbers

__all__ = ["LABEL", "SYSTEM_PROMPT", "main", "run"]

_CST = timezone(timedelta(hours=8))

#: 报告标签 —— 对齐主管线 `__main__.LABELS` 的纪律：命令侧保证，不是模型给的。
#: 本层比 analyze 更严（不豁免红线），所以措辞按 transcribe 档而非 analyze 档。
LABEL = "横截面层结构解读 · 仅解读，非投资建议 · 不产生结论"

#: 本层专属提示词。刻意与 `llm.SYSTEM_PROMPT_ANALYZE` 分开写：
#: 那边的任务是「从榜外异动段挑候选」，这里是「读一层结构、说清它长什么样」。
SYSTEM_PROMPT = """你是 A 股创业板「横截面异动观察层」的解读员。

## 你唯一的数据来源

用户消息里【横截面异动层 · YYYY-MM-DD】那一段。这是唯一素材，
**不得引用任何其他数据、不得回忆任何行情、不得推测未给出的信息**。

## 这一层是什么

它是与「飙升榜」解耦的**并列观察层**：靠 1/3/5 分钟涨速、成交额增量、量比
四项横截面分位，找出创业板里当下异动最显著的票 —— 目的是**早于榜单**看到动静。

它**不是推荐系统**，也**不判断涨跌方向**。跌得快的票同样是异常，
一样会进这个列表。这是设计，不是缺陷。

## 三条判据（都在素材里给了，不要自己重算）

- **一致性** `全正`/`全负`/`混合`：1m/3m/5m 三个窗口是否同号。
  `混合` = 三个窗口方向打架，或某窗口深度不足还没算出来。
- **量价** `配合`/`背离`/`—`：放量的同时价格有没有跟上。
  `背离` = 明显放量但价格没动（滞涨或派发的形态）。`—` = 放量不明显，不适用。
- **轮次**：当日进入本层的轮次数。轮次高 = 持续性异动，不是单轮噪声。

## 读样本行时必须注意

样本行是**当日最后一次进入本层时**的快照，不是该票最猛的那一刻。
「当日」是相对昨收的累计涨跌幅，「1m/3m/5m」是**同一时点**的短窗口涨速。
两者含义不同：当日累计为跌的票，完全可能在该时点短窗口急拉。
**不要**把这种组合读成矛盾或数据错误。

## 你要做的事

用中文写一份 **200-400 字** 的结构解读，回答「这一层今天长什么样」：

1. 整体规模与活跃度（几轮、多少只、判据大致怎么分布）；
2. 结构特征：哪类判据占主导？持续性分布是长尾还是集中在短轮？
   涨跌分布说明了什么（注意：**下跌只数多于上涨是常见情况**，如实描述即可）；
3. 从代表样本里挑 **2-3 个**说明其判据组合的**含义**
   （例：「持续 45 轮但量价背离 ⇒ 长时间异动却没有量能配合」）。

## 硬性禁止

- ❌ 不给任何买卖建议、目标价、仓位、加减仓暗示；
- ❌ 不说「值得关注」「可以介入」「回调买点」这类隐含倾向的话；
- ❌ 不预测明日或后续涨跌；
- ❌ **不跨票比较优劣**（「A 比 B 强」「其中最值得看的是」都不行）——
  本层是并列观察，比较即越界；
- ❌ 不编造素材里没有的数字；素材缺的项就说「未给出」。

如果被要求给出结论或推荐，回答「本层是观察层，不产生结论」。

开头不要写标题，直接从正文开始。"""


def _load_env() -> None:
    """`.env` 加载（`RTS_ENV_FILE` 可指路径）—— 与 `ai_digest.__main__` 逐字同款。

    ⚠ 忘了这一步的表现是「明明有 .env 却报未配置凭据」，最难看出：
    `magpie_config()` 只读 `os.environ`，不自己去读文件。
    """
    import os

    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - 环境相关
        return
    env_file = os.environ.get("RTS_ENV_FILE")
    if env_file:
        load_dotenv(env_file)
    else:
        load_dotenv()


def _db_path() -> Path:
    """与 `ai_digest.__main__` 同一套库定位规则（环境变量优先，其次仓库根）。"""
    import os

    for key in ("RTS_DB_PATH", "RTS_LIVE_DB"):
        v = os.environ.get(key)
        if v and Path(v).exists():
            return Path(v)
    root = os.environ.get("RTS_REPO_ROOT")
    if root and (Path(root) / "scanner.db").exists():
        return Path(root) / "scanner.db"
    return Path(__file__).resolve().parents[2] / "scanner.db"


def _open_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def run(
    *,
    date: str | None = None,
    db: Path | None = None,
    brief: bool = False,
    no_archive: bool = False,
) -> tuple[int, str]:
    """跑一次分析，返回 `(退出码, 纯文本报告)`。不打印 —— 由 `main` 决定出口。

    归档在本函数内完成（沿用主管线「报告即档案」的纪律）；
    `no_archive=True` 跳过（测试与 --materials 场景用）。
    """
    path = db or _db_path()
    if not path.exists():
        return 0, f"[!] 数据库不存在：{path}\n    先跑一轮扫描器（交易时段 09:30-11:30 / 13:00-15:00）。"

    conn = _open_db(path)
    try:
        resolved = resolve_date(conn, date)
        if not resolved:
            return 4, (
                "[!] crosssection_signal 没有可用数据。\n"
                "    本层只在交易时段产出；若不是交易时段，等下一轮开盘。"
            )
        summary = build_summary(conn, resolved)
    finally:
        conn.close()
    if not summary:
        return 4, f"[!] {resolved} 的横截面数据为空。"

    materials = render_materials(summary)

    cfg = magpie_config()
    if not cfg:
        # 没凭据也把素材打出来 —— 让人能先看见"模型会看到什么"，别白跑一次。
        return 1, f"{materials}\n\n[!] 未配置 MAGPIE_API_KEY，无法调用模型（素材已在上方）。"

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": materials},
    ]
    try:
        result = chat(cfg, messages)
    except AIUpstreamError as e:
        return 2, f"{materials}\n\n[!] 模型调用失败：{e}"

    # ⚠ 清洗纪律沿用 `llm.transcribe()`（2026-11-03 实测）：真实模型会输出 Markdown
    #   硬换行的「行尾双空格」（5 行里 4 行）—— 直接打出来看不出、复制出去却是脏字符。
    #   **只去行尾空白**，不动任何非空白字符 ⇒ 正文仍算「原样展示」。
    raw = result.get("content")
    text = "\n".join(line.rstrip() for line in (raw or "").strip().splitlines())
    if not text:
        return 2, f"{materials}\n\n[!] 模型服务返回空内容。"

    # ① 红线：本层不豁免（与 analyze 模式不同 —— 本层本就不该产生结论）。
    if is_redlined(text):
        return 3, (
            f"{materials}\n\n[!] 模型输出触红线（含买卖建议/预测/取优句式），已拦截：\n\n{text}"
        )

    # ② 幻觉巡检：基准 = 素材全文。
    unverified = unverified_numbers(text, materials)

    # ③ 归档 —— 沿用主管线 `archive.py` 单源（2026-11-16 Lichun 指出该沿用而漏掉的三件之一）。
    #    与主命令同纪律：只归档不回灌；`--no-archive` 可关。
    payload = ArchivePayload(
        title=f"横截面异动层 · {summary.date} · AI 结构解读",
        label=LABEL,
        mode="crosssection",
        source="crosssection_signal",
        source_desc=f"crosssection_signal（{summary.rounds} 轮窗口 "
        f"{summary.first_time}~{summary.last_time}）",
        model=cfg.model,
        total=summary.symbols,
        shown=len(summary.samples),
        text=text,
        generated_at=datetime.now(_CST),
        unverified=tuple(unverified),
    )
    archive_note = ""
    if not no_archive:
        path = write_archive(payload)
        archive_note = f"\n（已归档：{path}）" if path else ""

    body = render_archive(payload)
    if not brief:
        body += "\n\n" + "─" * 78 + "\n【模型看到的素材】\n" + materials
    if unverified:
        body += (
            f"\n\n[!] 幻觉巡检：以下数字在素材里找不到（{chr(12289).join(unverified)}）。\n"
            "    常见无害情形：模型自己做加减或四舍五入；\n"
            "    需警惕的是素材里**根本没有对应量级**的数字。"
        )
    body += archive_note
    return (5 if unverified else 0), body


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m scanner.ai_digest.crosssection",
        description="横截面异动层专属 AI 分析（输入源只有 crosssection_signal 一张表）",
    )
    p.add_argument("--date", help="指定日期 YYYY-MM-DD（默认取表里最新）")
    p.add_argument("--db", help="指定 scanner.db 路径（默认按环境变量/仓库根解析）")
    p.add_argument("--materials", action="store_true", help="只打印素材，不调用模型")
    p.add_argument("--brief", action="store_true", help="不打印素材核对表")
    p.add_argument("--out", help="报告落盘到指定文件（不进库）")
    p.add_argument("--no-archive", action="store_true", help="不写 logs/ai_digest 归档")
    p.add_argument("--json", action="store_true", help="机器可消费输出")
    args = p.parse_args(argv)
    _load_env()

    if args.materials:
        path = Path(args.db) if args.db else _db_path()
        if not path.exists():
            print(f"[!] 数据库不存在：{path}", file=sys.stderr)
            return 0
        conn = _open_db(path)
        try:
            resolved = resolve_date(conn, args.date)
            if not resolved:
                print("[!] crosssection_signal 没有可用数据。", file=sys.stderr)
                return 4
            summary = build_summary(conn, resolved)
        finally:
            conn.close()
        if not summary:
            print(f"[!] {resolved} 的横截面数据为空。", file=sys.stderr)
            return 4
        print(render_materials(summary))
        return 0

    code, text = run(
        date=args.date,
        db=Path(args.db) if args.db else None,
        brief=args.brief,
        no_archive=args.no_archive,
    )

    if args.out:
        try:
            Path(args.out).write_text(text, encoding="utf-8")
        except OSError as e:  # pragma: no cover - 磁盘/权限问题
            print(f"[!] 落盘失败：{e}", file=sys.stderr)

    if args.json:
        print(json.dumps({"exit_code": code, "report": text}, ensure_ascii=False, indent=2))
    else:
        print(text)
    return code


if __name__ == "__main__":  # pragma: no cover - 入口
    raise SystemExit(main())