"""面板逐轮存档：把**最近一次**终端打印的面板文本覆盖写入本地 Markdown 文件。

**格式参考**：`C:/Users/lichun/Documents/deepseek-harness/default-workspace/股票池.md`
—— 头部说明 + 数据来源 + 「当前最新快照」+ `---` + 一个 `## 快照 YYYY-MM-DD HH:MM`
小节，面板原文整段放进 ```text 代码块（等宽表格对齐不丢）。

**每轮覆盖（2026-11-06 用户决策）**：本文件**只保留最近一次**终端打印，
每轮整份重写（原子替换），不追加、不留历史快照。因此不做「掉榜/新进」
的历史对比；需要历史请另用日志/数据库。

**存档什么**：与「按 c 复制」同一份 tee 文本（`terminal_copy.latest_panel_text()`，
渲染时从 stdout 原样采集、去 ANSI、`\r` 进度行规整）——**不重渲染、不重算**。
存档保留整份面板（含头部摘要/飞书过门），按 c 只取其中两个表格区；倒计时/回执等
进度行不在采集范围（tee 只包渲染）。

**路径**：默认 `<LOG_DIR>/panel_md/股票池.md`；`RTS_PANEL_MD_PATH` 可指定完整路径。
⚠ 由于是整份覆盖，若指向外部人工维护的同名文件，**该文件原有内容会被替换**
（卷头由本模块按标准格式重写）。

**三条纪律**：

1. **纯展示层存档**：不写库、不进任何计算/回测/推送，删除本模块不影响任何口径。
2. **fail-open**：只 catch `EXTERNAL_FAILURES` 里的 OSError（磁盘满/目录不可写），
   写不进去就静默放弃本轮，绝不打断扫描主循环；编程错误照常冒泡。
3. **原子覆盖**：先写同目录 `.tmp` 再 `replace`，外部读取方不会读到半截文件；
   写入在同一把锁内完成，防止并发调用互相踩 tmp。

开关：`RTS_PANEL_MD=0` 整体关闭（阈值/开关单源在 `scanner/config_core.py`）。
"""

from __future__ import annotations

import threading
from pathlib import Path

from scanner.config import now_beijing
from scanner.terminal_copy import latest_panel_text, strip_ansi
from scanner.utils import EXTERNAL_FAILURES

__all__ = ["panel_md_path", "save_panel_round"]

DEFAULT_FILENAME = "股票池.md"
_DIR_NAME = "panel_md"

_HEADER = """# 股票池 / 盘面榜单

> 供自动化任务「创业板股票池筛选分析报告」读取。
> **更新方式**：系统每轮终端打印后**整份覆盖**本文件，只保留最近一次快照；分析时直接读取本文件即可。

数据来源：系统榜单输出（v1 池选 / 沪深飙升三段）
当前最新快照：{stamp}

---
"""

# 覆盖写是「一次写 tmp + 一次 replace」，锁防并发调用互踩同一个 tmp。
_lock = threading.Lock()


def panel_md_path() -> Path:
    """存档文件路径：`RTS_PANEL_MD_PATH` 优先；缺省 `<LOG_DIR>/panel_md/股票池.md`。"""
    from scanner import config

    configured = config.PANEL_MD_PATH
    return Path(configured) if configured else Path(config.LOG_DIR) / _DIR_NAME / DEFAULT_FILENAME


def _stamp(now) -> str:
    """与参考文件一致的时间戳（分钟级，不带秒）。"""
    return f"{now.date().isoformat()} {now.strftime('%H:%M')}"


def _enabled() -> bool:
    # 延迟读取：测试可 monkeypatch config 常量（与 LOG_DIR 同理）。
    from scanner import config

    return config.PANEL_MD_ENABLED


def save_panel_round(text: str | None = None) -> str | None:
    """把一轮面板文本**整份覆盖**写入 md；返回文件路径（未写返回 None）。

    `text` 缺省取 `terminal_copy.latest_panel_text()`（主循环挂点用缺省即可，
    与「按 c 复制」同一份）。任何 OSError 都吞掉返回 None（fail-open）。
    """
    if not _enabled():
        return None
    if text is None:
        text = latest_panel_text()
    body = strip_ansi(text) if text else ""
    if not body.strip():
        return None

    stamp = _stamp(now_beijing())
    content = _HEADER.format(stamp=stamp) + f"\n## 快照 {stamp}\n\n" + "```text\n" + body.rstrip("\n") + "\n```\n"
    path = panel_md_path()
    try:
        with _lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(content, encoding="utf-8", newline="\n")
            tmp.replace(path)  # 原子覆盖：旧快照/历史一律不留
    except EXTERNAL_FAILURES:
        # 磁盘满/目录不可写等：放弃本轮存档，不影响扫描主循环。
        return None
    return str(path)
