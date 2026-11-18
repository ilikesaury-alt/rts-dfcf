"""横截面异动层**并入统一 AI 分析**的守卫（2026-11-16）。

三条不可退让的契约：

1. **`股票池.md` 是唯一真源** —— 横截面区的数据来自终端面板存档（`≡` 区），
   不是数据库表、不是独立命令。
2. **区域之间彼此独立** —— 两区各自成素材（`sb.` / `cs.` 前缀）、
   各自用合适的提示词、**不跨区混排**。
3. **不改公开 API** —— `transcribe` / `build_messages` 的签名保持原样，
   区域识别由**素材自己**（id 前缀）完成。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scanner.ai_digest import llm as llm_mod
from scanner.ai_digest.crosssection_view import (
    build_crosssection_materials,
    from_panel_rows,
)
from scanner.ai_digest.material import (
    OffboardMaterials,
    build_materials,
    merge_materials,
    render_materials_block,
)

_PANEL_SRC = Path(__file__).resolve().parents[1] / "scanner" / "ai_digest" / "panel.py"
_CROSS_SRC = Path(__file__).resolve().parents[1] / "scanner" / "ai_digest" / "crosssection_view.py"
_MAIN_SRC = Path(__file__).resolve().parents[1] / "scanner" / "ai_digest" / "__main__.py"

_CROSS_HEADER = (
    "≡ 横截面异动 · 14:00:01 | 有效 1378 只 | 缓冲 8/8 | p95 0.950 | 信号 3 | 跨源价差 未校验"
)
_CROSS_LEGEND = (
    "  画像：位置(蓄势<3/启动3~7/加速7~12/高位≥12%)·持续N轮·"
    "动向(量价齐升/稳步走强/反复放量/放量滞涨/一致回落/动向不明)·主力资金(万/亿，—=源未给)"
)
_CROSS_SEC_LOW = "  · 低位·资金强（1 只）"
_CROSS_SEC_HIGH = "  · 高位加速（1 只）"
_CROSS_LINE = (
    "  1. SZ300163 先锋新材 5.10 +4.20% | 启动·持续8轮·量价齐升·主力+4213万"
)
_CROSS_LINE_MISSING = (
    "  4. SZ300824 北鼎股份 8.60 19.94% | 高位·持续15轮·动向不明·资金—"
)
_CROSS_NEW_LINE = (
    "  新进（本轮首现 · 1 只，单轮噪声高发，只看动静）：SZ300999 票尾 10.00 +1.00%"
)


# ── 契约 1：股票池.md 是唯一真源 ─────────────────────────────────


def test_terminal_block_with_groups_survives_panel_parse(tmp_path: Path) -> None:
    """分组后的真终端块（含「持续/新进」组标题行）必须仍被 panel 解析成全部数据行。

    ⚠ 这是两端的唯一接触面：`format_terminal_block` 手工拼行，
    `panel._CROSSSECTION_ROW_RE` 手工匹配 —— 改任一侧都要跑这条。
    """
    import scanner.crosssection as cs
    from scanner.ai_digest.panel import parse_panel_md

    def _sig(symbol: str, persist: int) -> dict:
        return {
            "symbol": symbol, "name": "票" + symbol[-2:], "price": 10.0, "percent": 1.0,
            "speed_1": 0.1, "speed_3": 0.2, "speed_5": 0.3,
            "amount_surge": 2.0, "vol_ratio": 1.5, "pct_rank": 0.99,
            "consistency": "全正", "volume_price": "配合", "persist": persist,
        }

    sigs = sorted(
        [_sig("SZ300001", 7), _sig("SZ300002", 6), _sig("SZ300003", 1)],
        key=cs.signal_sort_key,
    )
    with cs._lock:
        cs._state["last_signals"] = sigs
    cs._set_health(enabled=True, state="ok", warmup=True, valid_rows=20, signals=len(sigs))
    block = cs.format_terminal_block()
    assert "持续（" in block and "新进（" in block, "本用例专测分组标题，块里必须有它们"

    md = f"# 股票池\n\n## 快照 2026-11-17 10:00\n\n```text\n{block}\n```\n"
    path = tmp_path / "股票池.md"
    path.write_text(md, encoding="utf-8")
    snap = parse_panel_md(str(path))
    assert snap is not None
    region = next(r for r in snap.regions if "横截面" in r.title)
    assert [r["symbol"] for r in region.rows] == ["SZ300001", "SZ300002", "SZ300003"]
    assert region.rows[0]["轮次"] == "7"


def test_panel_parser_recognizes_crosssection_region() -> None:
    """`≡ 横截面异动` 必须被解析成一个**并列区**（此前被静默丢弃）。"""
    from scanner.ai_digest.panel import parse_panel_md

    md = f"""# 股票池

## 快照 2026-11-16 14:00

```text
  ◆ 沪深飙升 · 极有可能大涨（创业板）
    # 代码         名称           涨幅  5日累计    现价  排名上升      成交量    成交额   量比   换手%  市值(亿) 板块            评分  层  连击
    1 300706       阿石创       +4.54%   +1.19%    40.08         —   17.89万手    7.29亿   7.31    15.7        62 电子               —  T2     —
{_CROSS_HEADER}
{_CROSS_LEGEND}
{_CROSS_SEC_LOW}
{_CROSS_LINE}
{_CROSS_SEC_HIGH}
{_CROSS_LINE_MISSING}
{_CROSS_NEW_LINE}
```
"""
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".md", encoding="utf-8", delete=False) as fh:
        fh.write(md)
        path = fh.name
    snap = parse_panel_md(path)
    assert snap is not None
    titles = [r.title for r in snap.regions]
    assert any("横截面" in t for t in titles), f"横截面区没被解析出来：{titles}"
    cs = next(r for r in snap.regions if "横截面" in r.title)
    # 持续组 2 行 + 新进摘要拆出的 1 行
    assert len(cs.rows) == 3
    r0 = cs.rows[0]
    assert r0["symbol"] == "SZ300163"
    assert r0["名称"] == "先锋新材"
    assert r0["涨幅"] == "+4.20%"
    assert r0["分节"] == "低位·资金强"
    assert r0["画像"] == "启动·持续8轮·量价齐升·主力+4213万"
    assert r0["轮次"] == "8"
    assert cs.rows[1]["分节"] == "高位加速"
    assert cs.rows[1]["资金"] == "资金—"
    # 新进摘要行拆行：轮次=1、分节=新进
    assert cs.rows[2]["symbol"] == "SZ300999"
    assert cs.rows[2]["分节"] == "新进"
    assert cs.rows[2]["轮次"] == "1"


def test_crosssection_row_keeps_missing_values_as_none() -> None:
    """缺值（`—`）必须转成 None ⇒ 素材显示「暂无数据」，**不能变成 0**。"""
    from scanner.ai_digest.crosssection_view import _num

    assert _num("—") is None
    assert _num("") is None
    assert _num(None) is None
    assert _num("5.10") == 5.10
    assert _num("-3.95%") == -3.95
    assert _num("1005.24×") == 1005.24


def test_no_database_table_read_in_crosssection_view() -> None:
    """横截面区的读取**只经由 `股票池.md`**（panel 解析），不直接读库。

    判据：模块里不得有 SQL 读取（`SELECT`）与数据库连接（`sqlite3`）——
    它只做「panel 行 → 素材行」的翻译。
    """
    import re

    src = _CROSS_SRC.read_text(encoding="utf-8")
    code = re.sub(r'""".*?"""', "", src, flags=re.S)  # 剥 docstring
    assert not re.search(r"\bSELECT\b", code, re.I), (
        "横截面区不得直接查数据库（唯一真源是 股票池.md）"
    )
    assert "sqlite3" not in code, "横截面区不该自己连库"


# ── 契约 2：区域之间彼此独立 ─────────────────────────────────────


def test_regions_get_separate_materials_with_distinct_ids() -> None:
    """两区各自成素材，id 前缀不同 ⇒ 同一只票在两区都有时不会互相覆盖。"""
    from scanner.ai_digest.material import MaterialItem

    sb = OffboardMaterials(
        date="2026-11-16",
        items=(MaterialItem(id="sb.SZ300163.percent", label="x", value="+1.00%"),),
        missing=(),
        symbols=("SZ300163",),
    )
    cs = OffboardMaterials(
        date="2026-11-16",
        items=(MaterialItem(id="cs.SZ300163.percent", label="x", value="-3.95%"),),
        missing=(),
        symbols=("SZ300163",),
    )
    merged = merge_materials([("榜外异动段", sb), ("横截面异动层", cs)])
    ids = [i.id for i in merged.items]
    assert "sb.SZ300163.percent" in ids and "cs.SZ300163.percent" in ids
    assert len(merged.groups) == 2


def test_merged_block_renders_two_sections() -> None:
    """合并后的核对视图是**两个分区**，不是把两只票的字段混在一行。"""
    from scanner.ai_digest.material import MaterialItem

    sb = OffboardMaterials(
        date="2026-11-16",
        items=(
            MaterialItem(id="sb.SZ300706.name", label="n", value="阿石创"),
            MaterialItem(id="sb.SZ300706.percent", label="p", value="+4.54%"),
        ),
        missing=(),
        symbols=("SZ300706",),
    )
    cs = OffboardMaterials(
        date="2026-11-16",
        items=(
            MaterialItem(id="cs.SZ300163.name", label="n", value="先锋新材"),
            MaterialItem(id="cs.SZ300163.percent", label="p", value="-3.95%"),
        ),
        missing=(),
        symbols=("SZ300163",),
    )
    block = render_materials_block(merge_materials([("榜外异动段", sb), ("横截面异动层", cs)]))
    assert "【榜外异动段】" in block
    assert "【横截面异动层】" in block
    assert "阿石创" in block and "先锋新材" in block


def test_single_region_block_uses_own_prefix() -> None:
    """单区域的核对视图必须按**自己的前缀**取数据。

    ⚠ 踩过的坑：`build_materials` 默认 `id_prefix="sb"`，而横截面区的素材项是
    `cs.` 前缀 ⇒ 核对视图按 `sb.` 找 ⇒ **整张表「暂无数据」** ⇒
    「人可核验」这个核心前提塌了（素材是对的，只有核对表空）。
    """
    rows = from_panel_rows(
        [
            {
                "symbol": "SZ300147",
                "名称": "*ST香雪",
                "现价": "6.17",
                "涨幅": "+1.15%",
                "轮次": "19",
                "分节": "低位·资金强",
                "位置": "启动",
                "动向": "量价齐升",
                "资金": "主力+8000万",
                "画像": "启动·持续19轮·量价齐升·主力+8000万",
            }
        ]
    )
    mats = build_crosssection_materials("2026-11-16", rows)
    block = render_materials_block(mats)
    assert "*ST香雪" in block, "横截面区的核对视图取不到数据（prefix 用错）"
    assert "暂无数据" not in block, f"核对视图有缺项：{block}"
    assert "+1.15%" in block and "量价齐升" in block


def test_single_region_block_output_unchanged() -> None:
    """单区域（无 groups）走老路径 ⇒ B 段的核对视图**逐字不变**。"""
    from scanner.ai_digest.material import MaterialItem

    sb = OffboardMaterials(
        date="2026-11-16",
        items=(
            MaterialItem(id="sb.SZ300706.name", label="n", value="阿石创"),
            MaterialItem(id="sb.SZ300706.tier", label="t", value="T2 启动首日"),
            MaterialItem(id="sb.SZ300706.percent", label="p", value="+4.54%"),
        ),
        missing=(),
        symbols=("SZ300706",),
    )
    block = render_materials_block(sb)
    assert block.startswith("  SZ300706 阿石创 | ")
    assert "涨幅+4.54%" in block
    assert "【" not in block  # 单区域不加分节标题


def test_prompt_selected_by_material_regions() -> None:
    """**区域由素材自己决定**（不靠外部传参）⇒ 公开 API 不变。"""
    from scanner.ai_digest.material import MaterialItem

    cs_only = OffboardMaterials(
        date="2026-11-16",
        items=(MaterialItem(id="cs.SZ300163.percent", label="p", value="-3.95%"),),
        missing=(),
        symbols=("SZ300163",),
    )
    sb_only = OffboardMaterials(
        date="2026-11-16",
        items=(MaterialItem(id="sb.SZ300706.percent", label="p", value="+4.54%"),),
        missing=(),
        symbols=("SZ300706",),
    )
    merged = OffboardMaterials(
        date="2026-11-16",
        items=(
            MaterialItem(id="sb.SZ300706.percent", label="p", value="+4.54%"),
            MaterialItem(id="cs.SZ300163.percent", label="p", value="-3.95%"),
        ),
        missing=(),
        symbols=("SZ300706", "SZ300163"),
    )
    assert llm_mod.build_messages(cs_only)[0]["content"] == llm_mod.SYSTEM_PROMPT_CROSSSECTION
    assert llm_mod.build_messages(sb_only)[0]["content"] == llm_mod.SYSTEM_PROMPT_ANALYZE
    assert llm_mod.build_messages(merged)[0]["content"] == llm_mod.SYSTEM_PROMPT_MERGED


def test_public_api_signatures_unchanged() -> None:
    """`transcribe` / `build_messages` 的签名**不得**因本改动而变化。

    为什么重要：既有调用方与测试 stub 都按原签名调用；
    加参数会静默打断它们（实测踩过：stub 报 unexpected keyword argument）。
    """
    import inspect

    t = inspect.signature(llm_mod.transcribe)
    assert "source" not in t.parameters, "transcribe 不该新增 source 参数"
    b = inspect.signature(llm_mod.build_messages)
    assert "source" not in b.parameters, "build_messages 不该新增 source 参数"


def test_merged_prompt_forbids_cross_region_ranking() -> None:
    """合并提示词必须禁止跨区混排（那是「区域之间不该有关联」的延续）。"""
    p = llm_mod.SYSTEM_PROMPT_MERGED
    assert "不要跨区比较或混排" in p
    assert "彼此独立" in p
    assert "分别排序" in p


# ── 契约 3：素材形态正确 ─────────────────────────────────────────


def test_crosssection_materials_use_own_field_set() -> None:
    """横截面区用**自己的字段集**（画像/分节/轮次/位置/动向/资金），不是 B 段的。"""
    rows = from_panel_rows(
        [
            {
                "symbol": "SZ300163",
                "名称": "先锋新材",
                "现价": "5.10",
                "涨幅": "+4.20%",
                "轮次": "8",
                "分节": "低位·资金强",
                "位置": "启动",
                "动向": "量价齐升",
                "资金": "主力+4213万",
                "画像": "启动·持续8轮·量价齐升·主力+4213万",
            }
        ]
    )
    mats = build_crosssection_materials("2026-11-16", rows)
    vals = {i.id: i.value for i in mats.items}
    assert vals["cs.SZ300163.percent"] == "+4.20%"
    assert vals["cs.SZ300163.persist"] == "8"  # 整数，不带 .0
    assert vals["cs.SZ300163.pos"] == "启动"
    assert vals["cs.SZ300163.motion"] == "量价齐升"
    assert vals["cs.SZ300163.main_flow"] == "主力+4213万"
    assert vals["cs.SZ300163.section"] == "低位·资金强"
    # B 段字段不该出现
    assert "cs.SZ300163.tier" not in vals
    assert "cs.SZ300163.main_pct" not in vals
    # 数字列已撤出终端/面板，素材也不再有
    assert "cs.SZ300163.speed_1" not in vals
    assert "cs.SZ300163.pct_rank" not in vals


def test_crosssection_materials_have_no_baseline() -> None:
    """本层**无历史命中率基准** ⇒ 不挂 baseline（结构上无法自证）。"""
    rows = from_panel_rows([{"symbol": "SZ300163", "名称": "X", "轮次": "3"}])
    mats = build_crosssection_materials("2026-11-16", rows)
    assert not any(i.id == "baseline.stats" for i in mats.items)


def test_crosssection_sort_key_matches_terminal() -> None:
    """AI 素材排序必须与终端 `≡` 块同源（改键只改 `signal_sort_key`）。"""
    from scanner.ai_digest.crosssection_view import _cs_sort_key
    from scanner.crosssection import signal_sort_key

    row = {
        "persist": 5, "pct_rank": 0.99, "amount_surge": 2.0, "vol_ratio": 1.5,
        "consistency": "全正", "volume_price": "配合",
    }
    assert _cs_sort_key(row) == signal_sort_key(row)


def test_crosssection_note_states_independence() -> None:
    """本区口径必须写明「与榜外异动段彼此独立」与「不预判方向」。"""
    from scanner.ai_digest.crosssection_view import CROSSSECTION_NOTE

    assert "彼此独立" in CROSSSECTION_NOTE
    assert "不预判涨跌方向" in CROSSSECTION_NOTE


def test_build_materials_backward_compatible() -> None:
    """`build_materials` 的既有签名与默认行为不变（B 段调用方不受影响）。"""
    import inspect

    sig = inspect.signature(build_materials)
    for name in ("date", "rows", "limit", "mode", "market", "baseline"):
        assert name in sig.parameters
    mats = build_materials("2026-11-16", [{"symbol": "SZ300706", "name": "阿石创"}])
    assert mats.symbols == ("SZ300706",)
    assert any(i.id == "segment.note" for i in mats.items)


def test_merge_materials_requires_parts() -> None:
    with pytest.raises(ValueError):
        merge_materials([])


# ── CLI 接线 ─────────────────────────────────────────────────────


def test_cli_source_choices_include_crosssection() -> None:
    """`--source` 必须含 `crosssection` 与 `all`。"""
    import scanner.ai_digest.__main__ as cli

    src = _MAIN_SRC.read_text(encoding="utf-8")
    assert '"crosssection"' in src and '"all"' in src
    # 不该再有独立的横截面命令入口
    assert not (Path(cli.__file__).parent / "crosssection.py").exists(), (
        "不该再有独立的横截面命令模块（已并入统一 AI 分析）"
    )


def test_commands_doc_has_no_stale_standalone_command() -> None:
    """COMMANDS.md 不得再登记**已删除**的独立命令。

    为什么要有这条：独立命令那套是我中途的方案，被 Lichun 否掉后删了文件。
    文档若还留着那条命令，照着敲就会得到「No module named」——
    而**没有任何测试**会发现（文档不参与 import）。
    """
    cmds = (
        Path(__file__).resolve().parents[1] / "COMMANDS.md"
    ).read_text(encoding="utf-8")
    assert "scanner.ai_digest.crosssection" not in cmds, (
        "COMMANDS.md 还登记着已删除的独立横截面命令"
    )
    # 反向：新的用法必须写进去（否则用户不知道这条路）
    assert "--source crosssection" in cmds, "COMMANDS.md 缺新的 --source crosssection 用法"
    assert "--source all" in cmds, "COMMANDS.md 缺 --source all 用法"


# ── 归档自包含（2026-11-16 Lichun 拍板 B）──────────────────────────


def test_archive_payload_accepts_materials() -> None:
    """`ArchivePayload` 必须支持带素材，且**不带时输出逐字不变**。"""
    from datetime import datetime

    from scanner.ai_digest.archive import ArchivePayload, render_archive

    base = {
        "title": "T",
        "label": "L",
        "mode": "analyze",
        "source": "s",
        "source_desc": "d",
        "model": "m",
        "total": 1,
        "shown": 1,
        "text": "正文",
        "generated_at": datetime(2026, 11, 16, 14, 0, 0),
    }
    plain = render_archive(ArchivePayload(**base))
    with_mat = render_archive(ArchivePayload(**base, materials="素材行A\n素材行B"))

    # 不带素材时：既有格式一字不多
    assert "模型看到的素材" not in plain
    assert plain.endswith("查库。\n")
    # 带素材时：素材在正文之后、免责声明之前
    assert "模型看到的素材（同一份输入，可逐行核对上面的数字）：" in with_mat
    assert "素材行A" in with_mat and "素材行B" in with_mat
    assert with_mat.index("素材行A") > with_mat.index("正文")
    assert with_mat.index("素材行B") < with_mat.index("本报告仅供人工复盘")
    assert with_mat.endswith("查库。\n")  # 免责声明仍然收尾


def test_archive_makes_report_self_verifiable() -> None:
    """归档必须**自包含**：能从归档里抽出素材，核对正文的每个数字。

    为什么这是硬要求：输入源是「只保留最近一次快照」的文件（`股票池.md`），
    面板一被下一轮覆盖，报告的输入就再也找不回来 ⇒ 报告成了无法核对的孤本。
    存进素材 ⇒ 任何时候都能拿同一份输入复算。
    """
    from datetime import datetime

    from scanner.ai_digest.archive import ArchivePayload, render_archive
    from scanner.ai_digest.material import unverified_numbers

    materials = "cs.SZ300163.percent | 第1只·当日涨幅 | -3.95%\ncs.SZ300163.persist | 第1只·持续轮次 | 3"
    text = "第1只：SZ300163 先锋新材，当日-3.95%，持续3轮。"
    body = render_archive(
        ArchivePayload(
            title="T",
            label="L",
            mode="analyze",
            source="crosssection_signal",
            source_desc="d",
            model="m",
            total=1,
            shown=1,
            text=text,
            generated_at=datetime(2026, 11, 16, 14, 0, 0),
            materials=materials,
        )
    )
    marker = "模型看到的素材（同一份输入，可逐行核对上面的数字）："
    assert marker in body
    mat = body.split(marker, 1)[1].split("─" * 78)[0]
    unv = unverified_numbers(text, mat)
    assert not unv, f"归档不自包含：正文里有素材中找不到的数字 {unv}"


# ── research 模式接入（三区统一分档）─────────────────────────────


def test_region_lines_render_crosssection_names() -> None:
    """`_region_lines` 必须兼容横截面区的**中文名称键**。

    ⚠ 踩过的坑：横截面区的行键是中文 `名称`（panel.py 按 ≡ 块的中文列名建行），
    而 B 段区是英文 `name`。写死 `row.get('name')` 会让横截面区每行渲染成
    "None" ⇒ 模型拿到 8 只「None」——既没法引用名字，幻觉巡检也会乱。
    """
    from scanner.ai_digest.agent import _region_lines
    from scanner.ai_digest.panel import PanelRegion

    region = PanelRegion(
        title="横截面异动",
        columns=("代码", "名称", "现价", "涨幅"),
        rows=(
            {"symbol": "SZ300163", "名称": "先锋新材", "现价": "5.10", "涨幅": "-3.95%"},
        ),
        note="",
    )
    lines = _region_lines(region)
    assert "None" not in lines[1], f"名称键没取到：{lines[1]}"
    assert "先锋新材" in lines[1]


def test_research_prompt_explains_crosssection_region() -> None:
    """research 的 **user** 提示词必须教模型读横截面区（字段/不判方向/无基准率）。

    ⚠ 位置：那段口径说明在 `build_research_prompt`（user 消息）里，不在
    `RESEARCH_SYSTEM_PROMPT`（system 消息）里 —— user 才是逐区渲染的地方。
    """
    import inspect

    from scanner.ai_digest import agent

    src = inspect.getsource(agent.build_research_prompt)
    assert "横截面异动区怎么读" in src
    assert "不预判方向" in src
    assert "没有历史命中率基准" in src
