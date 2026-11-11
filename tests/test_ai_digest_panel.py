"""`ai_digest.panel` 与 `agent.build_research_prompt` 的守卫（2026-11-11）。

守的是**输入口径**：模型必须看到「终端屏幕上那份列表」的全部列与各区归属，而不是
一个扁平代码清单；同时幻觉巡检的比对基准必须 == 模型实际收到的提示词。
"""

from __future__ import annotations

import pytest

from scanner.ai_digest.agent import build_research_prompt
from scanner.ai_digest.panel import parse_panel_md, parse_panel_text
from scanner.view.model import COLS_HOT, COLS_POOL

#: 与真实 `股票池.md` 同构的最小面板（两区 + 口径说明 + 行尾标记 + 缺列值）。
PANEL_MD = """# 股票池 / 盘面榜单

> 供自动化任务读取。

数据来源：系统榜单输出

当前最新快照：2026-11-11 14:15

---

## 快照 2026-11-11 14:15

```text
================================================================================================
  创业板飙升榜监控  (2026-11-11 14:15:33)
  创业板共 10 只 | 每60s刷新 | [大盘强势]
================================================================================================
  ◆ v1 池选 — 新票优先·类别优先·排名升序·资金流降序
  类别·先验 = 该类别历史上的次日≥7%命中率（系统唯一口径）· 本区不显示评分
    # 代码         名称           涨幅  5日累计    现价     排名 板块           类别·先验
    1 SZ301218     华是科技     +3.35%  +20.18%   97.38       79 华为概念       MOM·10.0%   先强资弱
    2 SZ300613     富瀚微       +6.62%  +22.59%   85.32        — 人工智能       MOM·10.0%
    3 SZ300364     中文在线     +0.76%  +12.86%   29.27       12 连续亏损       ST·6.2%     稳

◆ 沪深飙升 · 极有可能大涨（创业板 · 三段并列·互不排名 · 与上方主线口径独立）
  — 榜外异动（榜外创业板·非榜单来源·层级 T1=量先动·价未动 / T2=启动首日·排序=T2 启动首日→T1 量先动→量比→主力净占比·3 只·观察段·未回测）
    # 代码         名称           涨幅  5日累计     现价  排名上升      成交量    成交额   量比   换手%  市值(亿) 板块            评分  层  连击
    1 300310       宜通世纪     +5.39%   +9.43%     4.89         —   48.92万手    2.33亿   2.46     6.1        43 通信               —  T2     — 稳
    2 300375       鹏翎股份     +3.27%   +7.57%     4.11         —   20.88万手  8483.5万   1.98     3.9        31 汽车               —  T1     —
```

---
"""


def _snap():
    snap = parse_panel_text(PANEL_MD)
    assert snap is not None
    return snap


def test_parses_both_regions_with_all_columns():
    """两区都要解析出来，且**列不丢失**（v1 池选的 类别·先验 / 榜外的 层 与 量比）。"""
    snap = _snap()
    assert snap.date == "2026-11-11" and snap.time == "14:15"
    assert len(snap.regions) == 2, [r.title for r in snap.regions]
    pool, off = snap.regions

    assert "v1 池选" in pool.title
    assert len(pool.rows) == 3
    assert pool.rows[0]["symbol"] == "SZ301218"
    assert pool.rows[0]["类别·先验"] == "MOM·10.0%"
    assert pool.rows[0]["5日累计"] == "+20.18%"
    # 行尾标记（不在列规格里）必须留住：`先强资弱` 是本项目的确定性矛盾提示。
    assert pool.rows[0]["marks"] == "先强资弱"
    assert pool.rows[2]["marks"] == "稳"

    assert "榜外异动" in off.title
    assert len(off.rows) == 2
    assert off.rows[0]["symbol"] == "SZ300310"  # 裸 6 位码要补成 SZ 前缀
    assert off.rows[0]["层"] == "T2" and off.rows[1]["层"] == "T1"
    assert off.rows[0]["量比"] == "2.46"
    assert off.rows[0]["marks"] == "稳"


def test_region_note_is_the_panel_wording_not_a_rewrite():
    """段口径必须**照抄面板原文**（层级/排序键/未回测），不得在这里重写一份。"""
    snap = _snap()
    off = snap.regions[1]
    for token in ("T2 启动首日→T1 量先动→量比→主力净占比", "未回测", "T1=量先动·价未动"):
        assert token in off.note, off.note


def test_only_real_data_rows_count_as_dropped():
    """dropped 只统计「长得像数据行却解析失败」的，不算口径说明/分隔线。"""
    snap = _snap()
    assert snap.dropped == 0, f"不应有丢弃行，实际 {snap.dropped}"


def test_malformed_data_row_is_dropped_not_guessed():
    """列宽对不上 ⇒ 丢弃并计数，绝不把错位数据塞给模型。"""
    broken = PANEL_MD.replace(
        "    1 SZ301218     华是科技     +3.35%  +20.18%   97.38       79 华为概念       MOM·10.0%   先强资弱",
        "    1 SZ301218ERRORROW@garbage",  # 列界处非留白
    )
    snap = parse_panel_text(broken)
    assert snap is not None
    assert snap.dropped == 1
    assert all(r["symbol"] != "SZ301218" for reg in snap.regions for r in reg.rows)


def test_missing_file_returns_none(tmp_path):
    """文件不存在 → None（fail-open，由调用方报文案，不抛）。"""
    assert parse_panel_md(tmp_path / "nope.md") is None


def test_column_specs_are_ratified_from_render_side():
    """列宽/列名必须来自渲染侧单源（COLS_POOL / COLS_HOT），不在本包另立一份。"""
    snap = _snap()
    assert snap.regions[0].columns == tuple(c[0] for c in COLS_POOL)
    assert snap.regions[1].columns == tuple(c[0] for c in COLS_HOT)


# ── 提示词：模型必须收到完整列 + 区归属 ──────────────────────────────────────────


def test_prompt_keeps_region_grouping_and_prior_column():
    """提示词里两区**分开**、且保留 类别·先验 / 层 —— 混成扁平代码清单就丢了「凭什么在这」。"""
    snap = _snap()
    text = build_research_prompt([], "面板原文", panel=snap)
    assert "v1 池选" in text and "榜外异动" in text
    assert "不要跨区排名" in text
    assert "类别·先验MOM·10.0%" in text
    assert "T2" in text and "量比2.46" in text
    assert "先强资弱" in text  # 行尾标记保留
    # 代码与名称仍在
    assert "SZ301218 华是科技" in text and "SZ300310 宜通世纪" in text


def test_prompt_without_panel_falls_back_to_flat_candidates():
    """自选清单模式（无面板）走扁平清单，不假装有区归属。"""
    text = build_research_prompt(
        [{"symbol": "SZ300310", "name": "宜通世纪", "tier": "T2", "vol_ratio": 2.46}],
        "用户指定的自选清单",
    )
    assert "候选股票 1 只" in text and "SZ300310 宜通世纪" in text
    assert "v1 池选" not in text


def test_prompt_is_the_hallucination_check_basis(monkeypatch):
    """`run_research` 发出的用户提示词与 CLI 的巡检基准必须是**同一个函数产物**。

    直接比对两处构造结果：只要 `run_research` 自己另拼一份，两者就会不等 ——
    历史上正是这样让 `ff_pct` / `price` / `first_time` 这类模型从未见过的数字蒙混过关。
    """
    import scanner.ai_digest.agent as agent_mod
    from scanner.ai_digest.llm import MagpieConfig
    from scanner.ai_digest.research import ResearchContext

    snap = _snap()
    expected = build_research_prompt([], "终端面板原文", panel=snap)

    sent: dict[str, str] = {}

    def fake_chat(cfg, messages, tools=None, timeout=None):
        sent["user"] = messages[1]["content"]
        return {"content": "结论", "tool_calls": []}

    monkeypatch.setattr(agent_mod, "chat", fake_chat)
    import sqlite3

    conn = sqlite3.connect(":memory:")
    try:
        agent_mod.run_research(
            MagpieConfig("https://x/v1", "k", "m"),
            [],
            ResearchContext(conn=conn),
            tools=[],
            panel=snap,
            user_prompt=expected,
        )
    finally:
        conn.close()
    assert sent["user"] == expected


@pytest.mark.parametrize("limit", [1, 2])
def test_limit_truncates_per_region_not_globally(limit):
    """`--limit N` 按**每区前 N 只**截断（区是并列段，不是一张大表）。"""
    from scanner.ai_digest.panel import PanelRegion, PanelSnapshot

    snap = _snap()
    limited = PanelSnapshot(
        date=snap.date,
        time=snap.time,
        dropped=snap.dropped,
        regions=tuple(
            PanelRegion(title=r.title, columns=r.columns, rows=r.rows[:limit], note=r.note)
            for r in snap.regions
            if r.rows
        ),
    )
    assert all(len(r.rows) == limit for r in limited.regions)
    assert limited.total == limit * len(snap.regions)


# ── 面板源接入 CLI ────────────────────────────────────────────────────────────


def test_panel_source_is_default_and_carries_all_columns(tmp_path, monkeypatch):
    """`--source auto` 在 research 模式下**优先面板**，并把完整列交给模型。"""
    import scanner.ai_digest.__main__ as cli
    import scanner.config as cfgmod

    panel_file = tmp_path / "股票池.md"
    panel_file.write_text(PANEL_MD, encoding="utf-8")
    monkeypatch.setattr(cfgmod, "PANEL_MD_PATH", str(panel_file))
    # 不许回退到真实库：有面板就不该碰 DB 的榜外表。
    monkeypatch.setattr(cli, "resolve_db_path", lambda: str(tmp_path / "absent.db"))
    monkeypatch.setattr(cli, "connect_readonly", lambda _p: __import__("sqlite3").connect(":memory:"))

    args = cli._parse_args(["--mode", "research"])
    info = cli._resolve_rows(args)
    assert info["source"] == "panel"
    assert info["date"] == "2026-11-11"
    assert len(info["panel"].regions) == 2

    text = build_research_prompt(info["rows"], "终端面板原文", panel=info["panel"])
    assert "类别·先验MOM·10.0%" in text  # v1 池选的唯一口径列
    assert "层T2" in text  # 榜外段的层级
    assert "先强资弱" in text  # 行尾确定性矛盾提示


def test_panel_source_not_used_for_analyze_or_transcribe(tmp_path, monkeypatch):
    """analyze/transcribe 按设计是**榜外异动段转述**，不能被面板源劫持。"""
    import scanner.ai_digest.__main__ as cli
    import scanner.config as cfgmod

    panel_file = tmp_path / "股票池.md"
    panel_file.write_text(PANEL_MD, encoding="utf-8")
    monkeypatch.setattr(cfgmod, "PANEL_MD_PATH", str(panel_file))

    args = cli._parse_args(["--mode", "transcribe"])
    args.demo = True  # 走内置样例，避免真实库依赖
    info = cli._resolve_rows(args)
    assert info["source"] == "demo"


def test_explicit_panel_source_reports_when_missing(tmp_path, monkeypatch):
    """`--source panel` 显式要求却没文件 → 明确报错（不静默回退 log）。"""
    import scanner.ai_digest.__main__ as cli
    import scanner.config as cfgmod

    monkeypatch.setattr(cfgmod, "PANEL_MD_PATH", str(tmp_path / "nope.md"))
    args = cli._parse_args(["--mode", "research", "--source", "panel"])
    from scanner.ai_digest.source import OffboardSourceError

    with pytest.raises(OffboardSourceError) as ei:
        cli._resolve_rows(args)
    assert "面板原文不可用" in str(ei.value)


def test_freshness_does_not_call_yesterday_fresh(monkeypatch):
    """跨日快照不得标成「0 分钟前」（`max(0, ...)` 曾把负值吞成 0）。"""
    from datetime import date

    import scanner.ai_digest.__main__ as cli

    class _Now:
        hour, minute, second = 13, 59, 0

        @staticmethod
        def date():
            return date(2026, 11, 11)

    monkeypatch.setattr(cli, "now_beijing", lambda: _Now())
    out = cli._freshness("14:55:00", "2026-11-10")
    assert "0 分钟前" not in out
    assert "1 天前" in out and "非实时" in out
    # 同日仍报分钟数
    assert cli._freshness("13:57:00", "2026-11-11") == " · 数据 2 分钟前"


def test_limit_per_region_is_single_source():
    """`limit_per_region` 按每区截断，且不改区归属/口径。"""
    from scanner.ai_digest.panel import limit_per_region

    snap = _snap()
    limited = limit_per_region(snap, 1)
    assert len(limited.regions) == len(snap.regions)
    assert all(len(r.rows) == 1 for r in limited.regions)
    assert [r.title for r in limited.regions] == [r.title for r in snap.regions]
    assert limited.regions[1].note == snap.regions[1].note
    # limit=None / 0 原样返回
    assert limit_per_region(snap, None) is snap
