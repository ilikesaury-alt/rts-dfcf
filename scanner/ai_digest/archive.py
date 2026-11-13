"""AI 分析报告的**人工复盘归档**（2026-11-13；纯文本、离线旁路）。

每次命令跑出模型报告，自动留一份到 `logs/ai_digest/YYYY-MM-DD/HHMMSS_<mode>_<source>.md`，
按发起时刻分目录；同一秒重复跑用 `_2/_3` 后缀，绝不覆盖旧档案。

四条边界（改前必读）
--------------------
1. **只归档，不回灌**。档案永远不进 `agent.build_research_prompt` / `llm.build_messages`，
   不进库、不进飞书、不进主循环。它记的是「模型当时说了什么」这一主观判断，
   不是事实；把它喂回下一次提示词 = 让判断自我强化并伪装成历史证据。
   真正该累计的是事实与标签（冻结信号快照 + 次日结果），那在 DB 里，不在本模块。
2. **不含次日绩效**。`ArchivePayload` 刻意没有 `next_day_pct` 之类的字段：一旦把
   「当初的判断 + 后来涨跌」写进同一份文件并被回灌，回放看着准、实盘是前视。
   复盘要对照结果，自己去库里按 `(date, symbol)` join，别把结果补写回档案。
3. **幻觉巡检未通过 ⇒ 注记置首行**。跟 `--out` 同款：自动化/人扫一眼先看到风险，
   坏报告不能混在好报告里不声不响。
4. **fail-open**。归档只是旁路，写盘失败只告警、不影响本次报告与退出码；
   只捕 `EXTERNAL_FAILURES`，编程错误照常冒泡。

文件名用**发起时刻**而非信号日：同一份面板可能在收盘后被反复调研，信号日相同但
发起时刻不同，按信号日命名会把后来的覆盖掉，也分不清「当时知道什么」。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from scanner.utils import EXTERNAL_FAILURES

#: 默认归档根（`/logs` 已被 .gitignore 排除 —— 个人复盘材料不进版本库）。
DEFAULT_ARCHIVE_DIR = Path("logs") / "ai_digest"

#: 分隔线；`__main__` 的终端输出与归档文件共用这一条，避免两处各写一份。
RULE = "─" * 78

DISCLAIMER = (
    "本报告仅供人工复盘：AI 主观判断，非系统结论；不入库、不进飞书、不回灌下一次分析。"
    "档案内不含次日绩效数据，对照结果请自行查库。"
)

_UNSAFE = re.compile(r"[^0-9A-Za-z_-]+")


@dataclass(frozen=True)
class ArchivePayload:
    """归档所需的全部内容 —— 刻意不含任何「事后结果」字段。"""

    title: str
    label: str
    mode: str
    source: str
    source_desc: str
    model: str
    total: int
    shown: int
    text: str
    generated_at: datetime
    unverified: tuple[str, ...] = ()
    tool_summary: str = ""
    redlined: bool = False


def render_archive(p: ArchivePayload) -> str:
    """纯函数：归档全文。元数据在前、正文居中、免责声明收尾。"""
    stamp = p.generated_at.strftime("%Y-%m-%d %H:%M:%S")
    lines: list[str] = []
    if p.unverified:
        lines += [
            f"[!] 幻觉巡检未通过：以下数字在基准里找不到（{'、'.join(p.unverified)}）；正文仅供参考。",
            "",
        ]
    lines += [
        RULE,
        p.title,
        p.label,
        RULE,
        f"归档时刻：{stamp}（北京时间）",
        f"模式：{p.mode} · 数据源：{p.source_desc}",
        f"模型：{p.model} · 共 {p.total} 只 / 本次 {p.shown} 只",
        f"工具调用：{p.tool_summary or '—'} · 幻觉巡检：{'未通过' if p.unverified else '通过'}"
        f" · 红线：{'已触发（正文为拒答文案）' if p.redlined else '未触发'}",
        RULE,
        "",
        p.text.strip(),
        "",
        RULE,
        DISCLAIMER,
    ]
    return "\n".join(lines) + "\n"


def write_archive(
    p: ArchivePayload,
    *,
    root: str | Path | None = None,
    rendered: str | None = None,
) -> Path | None:
    """写盘 → 返回路径；失败/跳过返回 None。`rendered` 有值时直接写它（与打印同源）。"""
    text = rendered if rendered is not None else render_archive(p)
    base = Path(root) if root is not None else DEFAULT_ARCHIVE_DIR
    stem = f"{p.generated_at:%H%M%S}_{_safe(p.mode)}_{_safe(p.source)}"
    try:
        day_dir = base / p.generated_at.strftime("%Y-%m-%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        for idx in range(1, 1000):
            suffix = "" if idx == 1 else f"_{idx}"
            path = day_dir / f"{stem}{suffix}.md"
            try:
                # 'x' = 独占创建：同一秒并发跑也不会互相覆盖。
                with path.open("x", encoding="utf-8") as fh:
                    fh.write(text)
                return path
            except FileExistsError:
                continue
        return None
    except EXTERNAL_FAILURES:
        return None


def _safe(s: str) -> str:
    return _UNSAFE.sub("_", s).strip("_")[:40] or "na"
