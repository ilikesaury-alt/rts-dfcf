"""前端**与 Python 真源对齐**的守卫（Issue B，2026-10-19）。

与 `tests/test_panel_web_hygiene.py` 的分工：那个文件管「工程卫生」（唯一网络收口、
组件不硬编码列名）；本文件管**跨语言一致性** —— 面板是第三个出口（终端 / 飞书 / 面板），
凡是「文案与值的口径」都必须与 `scanner/view/*.py` 的真源逐字对齐，否则面板会静默显示
一套自己编的语义，而没有任何功能测试会红（这正是 2026-09-30 那次列名漂移的形态）。

四条守卫：
1. `lib/cols.ts` 的列投影表**逐列同序**覆盖 `COLS_POOL/HOT/HIST`（少一列 = 静默空白格）。
2. 段标题（榜内飙升 / 榜外异动）与 `view.model.hot_subtitle/offboard_subtitle` **逐字一致**
   —— 尤其 `T1/T2` 的层级词表与排序键两级**都要在**，它们是 2026-10-13 那次
   「名称与层级混乱」修复的全部内容。
3. 连击 ★ 阈值与 `config_hot_watch.HOT_HIGHLIGHT_STREAK` 同值。
4. **零第二结论源**（G5）：`web/src` 不出现短名单 / 综合判断 / 持仓建议类文案。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from scanner.view.model import COLS_HIST, COLS_HOT, COLS_POOL, offboard_subtitle
from scanner.view.model import hot_subtitle as py_hot_subtitle

WEB_SRC = Path(__file__).resolve().parents[1] / "rts-panel-cloud" / "web" / "src"

pytestmark = pytest.mark.skipif(
    not WEB_SRC.exists(),
    reason="前端工程 rts-panel-cloud/web 未检出",
)

_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)
_LINE_COMMENT_RE = re.compile(r"^\s*//.*$", re.M)


def _read(name: str) -> str:
    path = WEB_SRC / name
    assert path.is_file(), f"前端文件不存在：{path}"
    return path.read_text(encoding="utf-8")


def _code_only(text: str) -> str:
    """去注释后的代码（注释里讲禁令不算违规 —— 与 web hygiene 守卫同规则）。"""
    return _LINE_COMMENT_RE.sub("", _BLOCK_COMMENT_RE.sub("", text))


def _table_keys(ts: str, region: str) -> list[str]:
    """取 `// #region COLS:<region>` 块里的对象字面量键（即中文列名），保持出现顺序。"""
    m = re.search(rf"// #region COLS:{re.escape(region)}\n(.*?)// #endregion", ts, re.S)
    assert m, f"lib/cols.ts 里找不到 COLS:{region} 区块（改名要同步本守卫）"
    return re.findall(r'^\s{2}"([^"]+)":', m.group(1), re.M)


@pytest.mark.parametrize(
    ("region", "cols"),
    [
        ("pool", COLS_POOL),
        ("hot", COLS_HOT),
        ("hist", COLS_HIST),
    ],
)
def test_col_projection_covers_every_terminal_column(region: str, cols: tuple) -> None:
    """列投影表必须与终端 `COLS_*` **逐列同序**（少一列不会报错，只会静默显示空白格）。"""
    ts = _read("lib/cols.ts")
    expected = [c[0] for c in cols]
    got = _table_keys(ts, region)
    assert got, f"{region} 投影表解析为空（正则失效会让本守卫空转，必须显式失败）"
    assert got == expected, f"{region} 列投影与 COLS_{region.upper()} 不一致\n期望 {expected}\n实际 {got}"


def test_hot_highlight_streak_matches_python_source() -> None:
    """连击标 ★ 的阈值从前端硬编码 —— 必须与 `config_hot_watch` 同值。"""
    from scanner.config_hot_watch import HOT_HIGHLIGHT_STREAK

    ts = _read("lib/cols.ts")
    m = re.search(r"export const HOT_HIGHLIGHT_STREAK = (\d+);", ts)
    assert m, "lib/cols.ts 里找不到 HOT_HIGHLIGHT_STREAK 常量"
    assert int(m.group(1)) == HOT_HIGHLIGHT_STREAK


def _ts_template(ts: str, name: str) -> str:
    """取 `export const <name> = (n: number): string => \\`...\\`;` 的反引号模板正文。"""
    m = re.search(rf"export const {name} = \(n: number\): string =>\n?\s*`([^`]*)`;", ts)
    assert m, f"App.tsx 里找不到导出的段标题模板 {name}（改名要同步本守卫）"
    return m.group(1)


def test_segment_subtitles_are_verbatim_from_python_source() -> None:
    """段标题逐字一致（含**层级词表**与**排序键**两级），用真函数生成后比对。"""
    ts = _read("App.tsx")
    assert _ts_template(ts, "hotSubtitle").replace("${n}", "7") == py_hot_subtitle(7)
    assert _ts_template(ts, "offboardSubtitle").replace("${n}", "7") == offboard_subtitle(7)


def test_offboard_subtitle_keeps_both_tier_glossary_and_sort_text() -> None:
    """`T1/T2` 的词表与排序键**刻意分两处**：合并会丢掉「层序」这一层信息。

    这条与上一条不重复：上一条守「与 Python 一致」，本条守「Python 那边也是对的」
    —— 词表（`=` 号）与排序文本（带空格的 `T2 启动首日`）缺一，屏幕上就只剩裸 `T1`。
    """
    ts = _read("App.tsx")
    body = _ts_template(ts, "offboardSubtitle")
    assert "层级 T1=量先动·价未动 / T2=启动首日" in body, "缺层级词表 ⇒ 裸 T1/T2 无从解读"
    assert "排序=T2 启动首日→T1 量先动" in body, "缺排序键的层序 ⇒ 看不出谁排前面"
    assert "未回测" in body and "观察段" in body, "缺证据强度 ⇒ 会被当成与榜内飙升段同强度"


def test_region_titles_match_terminal_wording() -> None:
    """区块标题是设计规定的展示文案：必须与终端 `render.py` 里的同串一致。

    （面板不显示终端的「三段并列」是**已知契约缺口**：`regions` 只有四个键、
    榜内异动段不在载荷里；App.tsx 已把这一点写在注释里，故这里只比对本就存在的三串。）
    """
    ts = _read("App.tsx")
    render_src = (Path(__file__).resolve().parents[1] / "scanner" / "view" / "render.py").read_text(encoding="utf-8")
    for title in ("◆ v1 池选 — 新票优先·类别优先·排名升序·资金流降序", "◆ v1 回捞", "◆ 沪深飙升 · 极有可能大涨"):
        assert title in ts, f"面板缺少区块标题：{title}"
        assert title in render_src, f"面板标题与终端不一致（终端无此串）：{title}"


_VERDICT_WORDS = ("建议", "短名单", "综合判断", "买入", "卖出", "持仓", "目标价", "止盈", "止损")


def test_no_verdict_wording_in_web_src() -> None:
    """G5 零第二结论源：面板是「只渲染已有证据」的出口，不得出现任何结论式文案。"""
    offenders: list[str] = []
    for path in sorted(WEB_SRC.rglob("*")):
        if not path.is_file() or path.suffix not in (".ts", ".tsx"):
            continue
        if path.name.endswith((".test.ts", ".test.tsx")):
            continue
        code = _code_only(path.read_text(encoding="utf-8"))
        for word in _VERDICT_WORDS:
            if word in code:
                offenders.append(f"{path.relative_to(WEB_SRC)}: {word!r}")
    assert not offenders, "面板不得出现结论式文案（G5）：\n" + "\n".join(offenders)
