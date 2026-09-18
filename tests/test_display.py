"""综合排序显示与资金流图标测试。"""

import re
import sqlite3
from datetime import timedelta

import pytest
import wcwidth

import scanner.display as disp_mod
import scanner.view.assemble as va  # noqa: E402

# display 已拆分为 scanner/view/{model,assemble,render}；下列全局原由 display 模块持有，
# 现分别由子模块定义/捕获绑定。monkeypatch 须打到真正持有该全局的命名空间才生效。
import scanner.view.model as vm  # noqa: E402
import scanner.view.render as vr  # noqa: E402
from scanner.config import now_beijing
from scanner.models import Candidate, KlineSummary, StockInfo

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


# ── 资金流强弱档位（5 档图标规则，2026-08-06）──
def _candidate(pct):
    k = KlineSummary(
        trend="",
        accumulated_pct=0.0,
        volume_ratio=1.0,
        bottom_confirmed=False,
        score=50,
        dimensions={} if pct is None else {"fund_flow_main_pct": pct},
    )
    return Candidate(
        stock=StockInfo(
            symbol="SZ300001", name="测试", code="300001", percent=1.0, current=10.0, value=1e8, rank_change=0, rank=1
        ),
        category="new_face",
        score=50,
        reason="",
        kline=k,
    )


# ── 终端可见宽度（2026-08-13 修复：剥离 ANSI 转义序列再量宽度）──
def test_vis_len_strips_ansi_sequences():
    """回归：彩色文本的可见宽度必须按去 ANSI 后的实际内容计，
    否则 _pad 少补空格 → 固定列错位（此前 \033[91m45\033[0m 被高估为 9 列）。"""
    assert disp_mod._vis_len("45") == 2
    assert disp_mod._vis_len("\033[91m45\033[0m") == 2
    assert disp_mod._vis_len("\033[1m\033[91m45\033[0m") == 2
    assert disp_mod._vis_len("\033[91m45+3\033[0m") == 4
    assert disp_mod._vis_len("半导体") == 6  # 中文按 2 列
    assert disp_mod._vis_len("5日累计") == 7


def test_pad_with_ansi_content_right_aligns():
    """_pad 对含 ANSI 的字符串应仍按可见宽度右对齐到目标列宽。"""
    s = disp_mod._pad("\033[91m45\033[0m", 8, "r")
    stripped = s.replace("\033[91m", "").replace("\033[0m", "")
    assert disp_mod._vis_len(stripped) == 8  # 6 空格 + 45
    assert stripped == "      45"


def test_trunc_handles_ansi_and_wide_chars():
    t = disp_mod._trunc("半导体半导体半导体", 10)
    assert disp_mod._vis_len(t) <= 10 and t.endswith("…")


def test_trunc_preserves_ansi_escape_reset():
    """回归（2026-08-20）：_trunc 截断含 ANSI 的文本时不得在转义序列中间切断、
    丢失 \x1b[0m（否则终端后续行残留颜色）。"""
    s = "\033[91m半导体半导体\033[0m"
    t = disp_mod._trunc(s, 10)
    assert disp_mod._vis_len(t) <= 10 and t.endswith("…")
    assert t.startswith("\033[91m")
    assert t.endswith("\033[0m…")


def test_trunc_does_not_duplicate_escape_body():
    """回归（2026-08-21 审查）：旧实现命中转义序列后仅 continue 一个字符，序列体内的
    [ 9 1 m 会在后续迭代被再次当可见文本追加（输出出现字面 "[91m"），宽度计算随之
    失真。截断含 ANSI 文本时，剥离色码后的可见内容不得包含序列体片段。"""
    import re as _re

    s = "\033[91m半导体半导体\033[0m"
    for width in (6, 10, 12, 14):
        t = disp_mod._trunc(s, width)
        plain = _re.sub(r"\x1b\[[0-9;]*m", "", t)
        # 序列体片段（如 [91m、0m）不得作为可见文本残留
        for frag in ("[91m", "[0m", "[1m"):
            assert frag not in plain, (width, repr(t))
        assert disp_mod._vis_len(t) <= width
        assert t.startswith("\x1b[91m")


def test_trunc_nested_ansi_no_leak():
    """嵌套色码（加粗+红）截断后同样不得泄漏序列体，且保留完整 RESET。"""
    import re as _re

    s = "\x1b[1m\x1b[91m华为概念+机器人板块\x1b[0m"
    t = disp_mod._trunc(s, 8)
    plain = _re.sub(r"\x1b\[[0-9;]*m", "", t)
    assert "[1m" not in plain and "[91m" not in plain
    assert disp_mod._vis_len(t) <= 8
    assert t.endswith("\x1b[0m…")


def test_fund_flow_signal_boundaries():
    """阈值端点语义：≥8 强流入、[5,8) 流入、(-5,5) 中性、( -8,-5] 流出、≤-8 强流出。"""
    assert disp_mod.fund_flow_signal(None) == ""
    assert disp_mod.fund_flow_signal(8.0) == "strong_in"
    assert disp_mod.fund_flow_signal(7.9) == "in"
    assert disp_mod.fund_flow_signal(5.0) == "in"
    assert disp_mod.fund_flow_signal(4.9) == "neutral"
    assert disp_mod.fund_flow_signal(0.0) == "neutral"
    assert disp_mod.fund_flow_signal(3.1) == "neutral"
    assert disp_mod.fund_flow_signal(-3.1) == "neutral"
    assert disp_mod.fund_flow_signal(-5.0) == "out"
    assert disp_mod.fund_flow_signal(-7.9) == "out"
    assert disp_mod.fund_flow_signal(-8.0) == "strong_out"


def test_market_extra_str_fund_flow_icon():
    """资金流以图标替代原「资+x.x% ±xxx万」文本，纯图标展示。"""
    s = disp_mod._market_extra_str(_candidate(8.5))
    assert "▲▲" in s
    assert "资" not in s
    assert "万" not in s
    assert "亿" not in s


def test_market_extra_str_no_fund_flow_data():
    """无资金流数据时资金段为空，连板信息仍保留。"""
    s = disp_mod._market_extra_str(_candidate(None))
    assert s == ""


def test_market_extra_str_zt_kept():
    """连板/炸板标记不受资金流图标改造影响。"""
    c = _candidate(6.0)
    assert c.kline is not None
    c.kline.dimensions["zt_lianban"] = 2
    c.kline.dimensions["zt_zhaban"] = 1
    s = disp_mod._market_extra_str(c)
    assert "▲" in s
    assert "连2炸1" in s


# ── 综合排序资金流图标：DB 快照回退（重启/掉榜后仍显示）──


def _main_lines(out: str) -> list[str]:
    """v1 池选区行，用于测试断言。

    终选参考区（含个股行）渲染在 v1 之前，剥离该区块保留 v1 部分。
    （2026-09-14：决策层区块已删除，v2 池选与核心低吸区块已隐藏 —— 原先用来切分的
    「◆ 今日决策」「◆ v2 池选」「◆ 核心方向低吸」三个锚点都不再出现。）"""
    main_part = out
    head, sep, rest = main_part.partition("◆ 终选参考")
    if sep:
        _, sep2, v1 = rest.partition("◆ v1 池选")
        main_part = head + ("◆ v1 池选" + v1 if sep2 else "")
    return [ln for ln in main_part.splitlines() if "SZ30000" in ln]


def _main_line(out: str, sym: str) -> str:
    """首个含 sym 的主表行：剥掉终选参考区后再取首匹配。

    终选参考区渲染在 v1 之前且含个股行——直接对全输出取首个含 sym 的行会命中终选行
    而非主表行。该区块结束于下一个「◆」标题行。
    """
    lines: list[str] = []
    in_decision = False
    for ln in out.splitlines():
        plain = _ANSI_RE.sub("", ln)
        if in_decision:
            if plain.lstrip().startswith("◆"):
                in_decision = False
            else:
                continue
        if plain.startswith("◆ 终选参考"):
            in_decision = True
            continue
        lines.append(ln)  # 保留原始行（含 ANSI），供测试断言高亮码
    return next(ln for ln in lines if sym in ln)


def _rec_db():
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE appearances (
        id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, name TEXT NOT NULL,
        date TEXT NOT NULL, rank INTEGER, percent REAL, value REAL, UNIQUE(symbol, date))""")
    conn.execute("""CREATE TABLE recommendations (
        id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL, time TEXT NOT NULL,
        symbol TEXT NOT NULL, name TEXT NOT NULL, category TEXT NOT NULL, score INTEGER NOT NULL,
        percent REAL, trend TEXT, next_day_pct REAL, fwd_3d REAL, fwd_5d REAL,
        score_breakdown TEXT, source TEXT DEFAULT 'xueqiu', concept TEXT, accumulated_pct REAL,
        excluded INTEGER DEFAULT 0)""")
    conn.execute("""CREATE TABLE market_extra_cache (
        symbol TEXT NOT NULL, date TEXT NOT NULL, data_type TEXT NOT NULL,
        payload_json TEXT NOT NULL, updated TEXT NOT NULL, PRIMARY KEY(symbol, data_type))""")
    conn.execute("""CREATE TABLE market_index_log (
        date TEXT PRIMARY KEY, time TEXT, index_pct REAL, bar_date TEXT,
        source TEXT, updated TEXT DEFAULT '')""")
    return conn


def test_display_priority_fund_flow_icon_from_db(capsys):
    """综合排序在候选池缺失（重启/掉榜）时，从 market_extra_cache 读取资金流图标。"""
    conn = _rec_db()
    today = now_beijing().date().isoformat()
    conn.executemany(
        "INSERT INTO recommendations (date, time, symbol, name, category, score, percent) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (today, "13:00", "SZ300001", "有数据", "rebound", 60, 2.0),
            (today, "13:00", "SZ300002", "无数据", "rebound", 55, 1.0),
        ],
    )
    conn.execute(
        "INSERT INTO market_extra_cache (symbol, date, data_type, payload_json, updated) VALUES (?, ?, ?, ?, ?)",
        ("SZ300001", today, "fund_flow", '{"main_pct": 6.0, "main_net": 1e7}', now_beijing().isoformat()),
    )
    conn.commit()
    disp_mod.display_priority(conn)
    out = capsys.readouterr().out
    # 用 _main_line 剥掉终选参考区（终选行也含资金流图标，直接取首行会误命中）
    line1 = _main_line(out, "SZ300001")
    line2 = _main_line(out, "SZ300002")
    assert "▲" in line1
    assert "▲" not in line2


# ── 显示门控工具（回马枪/动态推荐/次日大涨规则区已移除，2026-09-03）──
def _set_market_index(conn, index_pct: float):
    today = now_beijing().date().isoformat()
    conn.execute(
        "INSERT OR REPLACE INTO market_index_log (date, time, index_pct, bar_date, source, updated) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (today, "10:00", index_pct, today, "test", now_beijing().isoformat()),
    )
    conn.commit()


# ── 核心方向低吸区的显示门控（2026-09-14 展示区已隐藏）──
# 原 `test_display_priority_core_dip_shown_when_main_sparse` 断言「主区条数 ≤
# COMEBACK_DISPLAY_MIN_MAIN(5) 或弱市 → 展示低吸区，否则隐藏」这一整套门控，
# 以及 `test_display_priority_recommended_region_shown_when_main_dense`（弱市下主区
# 密集仍强制展示，修复「推荐了却看不到标的」割裂）。两个展示区均已按用户决策隐藏，
# 故断言一并移除。
# ⚠ 注意：`assemble` 里 core_dips 的排序（_core_dip_entry_quality）仍在跑——它是终选
# 参考区合池输入。被删的只是「这个区要不要画出来」的门控。
# 需复原见 git 历史。


# ── 综合排序实时行情覆盖：live_quotes 对所有行优先（候选/非候选一致）──
def _cand_in_pool(symbol: str, pct: float, cur: float, rank: int) -> Candidate:
    """构造池内候选快照。

    2026-09-14：category 由 `core_dip` 改为 `rebound` —— 这些用例原先借核心方向低吸区
    渲染，该区已隐藏；且 `ranking.fresh_candidate` 要求候选 category 与推荐行一致
    （不一致视同无候选），故必须与 `_insert_rec` 的 rebound 同步。
    """
    k = KlineSummary(trend="", accumulated_pct=0.0, volume_ratio=1.0, bottom_confirmed=False, score=50, dimensions={})
    return Candidate(
        stock=StockInfo(
            symbol=symbol, name="测试", code=symbol[-6:], percent=pct, current=cur, value=1e8, rank_change=0, rank=rank
        ),
        category="rebound",
        score=60,
        reason="",
        kline=k,
    )


def _insert_rec(conn, symbol: str, name: str, percent: float):
    """插入一条主表类别（rebound）的推荐行，用于「掉榜/重启行」类渲染断言。

    2026-09-14 类别由 `core_dip` 改为 `rebound`：原先这些用例借核心方向低吸区作为
    渲染载体（注释「回马枪区已移除 → 低吸行改走低吸区验证同一渲染规则」），该展示区
    已按用户决策隐藏，而 core_dip 不进 v1 主表，故改走 v1 主表验证同一套行渲染规则。
    类别与这些断言无关（live 行情覆盖 / 排名回填 / 每行现价列），故不影响被测语义。
    """
    today = now_beijing().date().isoformat()
    conn.execute(
        "INSERT INTO recommendations (date, time, symbol, name, category, score, percent) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (today, "13:00", symbol, name, "rebound", 60, percent),
    )
    conn.commit()


def test_display_priority_live_quotes_overrides_candidate(monkeypatch, capsys):
    """候选行也优先使用 live_quotes 实时行情（此前仅无候选行生效）。"""
    conn = _rec_db()
    _insert_rec(conn, "SZ300001", "候选票", 2.0)
    cand = _cand_in_pool("SZ300001", 1.5, 10.0, 5)
    disp_mod.display_priority(
        conn, live_quotes={"SZ300001": {"percent": 3.2, "current": 10.5}}, today_pool={"SZ300001": cand}
    )
    out = capsys.readouterr().out
    line = _main_line(out, "SZ300001")
    assert "+3.20%" in line
    assert "10.50" in line
    assert "+1.50%" not in line
    assert "+2.00%" not in line


def test_display_priority_live_quotes_overrides_db_for_dropped(monkeypatch, capsys):
    """掉榜行（无候选）用 live_quotes 实时行情，优于 DB 落库值。"""
    conn = _rec_db()
    _insert_rec(conn, "SZ300002", "掉榜票", 1.0)
    disp_mod.display_priority(conn, live_quotes={"SZ300002": {"percent": 4.5, "current": 20.0}}, today_pool={})
    out = capsys.readouterr().out
    line = _main_line(out, "SZ300002")
    assert "+4.50%" in line
    assert "20.00" in line
    assert "+1.00%" not in line


def test_display_priority_candidate_fallback_when_no_live(monkeypatch, capsys):
    """无 live_quotes 时，候选行回退到候选池扫描快照（含排名）。"""
    conn = _rec_db()
    _insert_rec(conn, "SZ300001", "候选票", 2.0)
    cand = _cand_in_pool("SZ300001", 1.5, 10.0, 5)
    disp_mod.display_priority(conn, today_pool={"SZ300001": cand})
    out = capsys.readouterr().out
    line = _main_line(out, "SZ300001")
    assert "+1.50%" in line
    assert "10.00" in line
    assert "N/A" not in line  # 候选有 rank，应显示 5 而非 N/A


def test_display_priority_dropped_live_percent_zero_not_fallback(monkeypatch, capsys):
    """回归：掉榜行（无候选、无 live_quotes）live_percent=0.0（合法 0.00% 涨幅）
    必须按 0.00% 显示，不能因 `or` 回退到推荐时落库的 percent（此前显示 +2.00%）。"""
    conn = _rec_db()
    _insert_rec(conn, "SZ300002", "掉榜票", 2.0)
    # 写入今日 appearances，percent=0.0（真实 0.00% 涨幅）
    today = now_beijing().date().isoformat()
    conn.execute(
        "INSERT INTO appearances (symbol, name, date, rank, percent, value) "
        "VALUES ('SZ300002', '掉榜票', ?, 5, 0.0, 100)",
        (today,),
    )
    conn.commit()
    disp_mod.display_priority(conn, today_pool={})
    out = capsys.readouterr().out
    line = _main_line(out, "SZ300002")
    assert "+0.00%" in line
    assert "+2.00%" not in line


def test_display_priority_dropped_never_appeared_uses_db_percent(monkeypatch, capsys):
    """回归（2026-08-20）：掉榜行今日从未上榜（无 appearances 行）、无候选、无 live_quotes
    时，涨幅列应回退到推荐时落库 percent（DB），不能恒显 +0.00%。
    此前 get_today_recommendations 对无 appearances 行的 live_percent 填 0.0（而非 None），
    使 display._print_priority_row 的回退链永远走 0.0——与 ranking._nextday_entry_percent
    判档用 DB percent 形成同表两套口径（可「判档用 +5%、显示 +0.00%」）。"""
    conn = _rec_db()
    _insert_rec(conn, "SZ300002", "掉榜票", 2.0)
    # 关键：不写入任何 appearances 行（该票今日从未上榜）
    disp_mod.display_priority(conn, today_pool={})
    out = capsys.readouterr().out
    line = _main_line(out, "SZ300002")
    assert "+2.00%" in line
    assert "+0.00%" not in line


def test_display_priority_stale_candidate_no_stale_rank(monkeypatch, capsys):
    """掉榜 stale 候选不吃池内冻结快照的 rank/current（仙乐健康 08-24 案例：
    掉榜后仍显示上榜时的旧排名，被误读为当前名次）。掉榜行排名应回 —。"""
    conn = _rec_db()
    _insert_rec(conn, "SZ300791", "仙乐健康", 3.0)
    cand = _cand_in_pool("SZ300791", 3.69, 22.9, 15)
    cand.is_stale = True
    disp_mod.display_priority(
        conn,
        live_quotes={"SZ300791": {"percent": 3.5, "current": 23.0}},
        today_pool={"SZ300791": cand},
    )
    out = capsys.readouterr().out
    line = _main_line(out, "SZ300791")
    assert "+3.50%" in line and "23.00" in line
    assert " 15" not in line.replace("SZ300791", "")  # 旧排名不得残留


def test_display_priority_stale_candidate_no_stale_percent(monkeypatch, capsys):
    """stale 掉榜候选的冻结 percent 不进涨幅列/🎯 判定回退链（2026-08-24 审查：
    与 rank/current 同根因同族漏网点——无 live_quotes 时涨幅列曾吃掉榜时刻冻结
    快照，应落推荐时落库 DB percent）。"""
    conn = _rec_db()
    _insert_rec(conn, "SZ300792", "冻结票", 3.0)  # 推荐时刻口径 +3.0%
    cand = _cand_in_pool("SZ300792", 9.5, 22.9, 15)  # 掉榜时刻冻结快照 +9.5%
    cand.is_stale = True
    disp_mod.display_priority(conn, today_pool={"SZ300792": cand})
    out = capsys.readouterr().out
    line = _main_line(out, "SZ300792")
    assert "+3.00%" in line
    assert "+9.50%" not in line


def test_display_priority_rank_map_for_dropped(monkeypatch, capsys):
    """掉榜/重启行（无候选）的排名由当前飙升榜 rank_map 补上（此前恒为 —）。"""
    conn = _rec_db()
    _insert_rec(conn, "SZ300002", "掉榜票", 1.0)
    disp_mod.display_priority(conn, rank_map={"SZ300002": 42}, today_pool={})
    out = capsys.readouterr().out
    line = _main_line(out, "SZ300002")
    assert "42" in line


# ── 排名变化显示（2026-08-12）：综合排序「排名」列附雪球榜单较上一轮的排名变化 ──
def test_rank_delta_str():
    """_rank_delta_str：+N 升 / -N 降 / 无变化或无可比上轮返回空串；≥5 名升降着色。

    2026-08-13：↑/↓ 在中文终端渲染为全角导致固定列错乱，改用 ASCII 半角 + / -。
    """
    assert disp_mod._rank_delta_str("S", 5, {"S": 8}) == "+3"
    assert disp_mod._rank_delta_str("S", 8, {"S": 5}) == "-3"
    assert disp_mod._rank_delta_str("S", 5, {"S": 5}) == ""
    assert disp_mod._rank_delta_str("S", 5, {}) == ""
    assert disp_mod._rank_delta_str("S", 5, {"T": 8}) == ""
    up5 = disp_mod._rank_delta_str("S", 5, {"S": 10})
    assert "+5" in up5 and disp_mod.ANSI["RED"] in up5
    down5 = disp_mod._rank_delta_str("S", 10, {"S": 5})
    assert "-5" in down5 and disp_mod.ANSI["GREEN"] in down5


# 2026-09-14：本组用例原**借「核心方向低吸区」间接覆盖** `_print_priority_row` 的排名列
# （低吸区渲染用的就是 COLS_DETAIL + 该函数）。该展示区已按用户决策隐藏，而 v1 主表走
# `_emit_pool_table_row`/COLS_POOL（排名列是裸数字，既不高亮也不显示 +N/-N）——
# `_main_line` 如今只会命中主表行，原断言随之失真（不再是"通过"），故改为直调被测函数。
# `_print_priority_row` 现无生产调用方但**有意保留**（见其 docstring：COLS_DETAIL 的唯一
# 渲染实现，删了会让"恢复低吸区"变成重写）。
def _priority_entry(sym: str, name: str, *, live_rank: int | None, category: str = "rebound") -> dict:
    """构造 `_print_priority_row` 的最小 entry（键名与生产 RecommendationRow 一致）。"""
    return {
        "symbol": sym,
        "name": name,
        "category": category,
        "score": 60,
        "percent": 2.0,
        "accumulated_pct": 0.0,
        "time": "10:30",
        "first_time": "10:30",
        "live_rank": live_rank,
        "_candidate": None,
    }


def _priority_rows(out: str) -> dict[str, str]:
    """直调 `_print_priority_row` 后的输出 → {symbol: 该行文本（含 ANSI）}。"""
    rows: dict[str, str] = {}
    for ln in out.splitlines():
        for sym in ("SZ300001", "SZ300002", "SZ300003"):
            if sym in ln:
                rows[sym] = ln
    return rows


def test_priority_row_rank_delta_from_last_ranks(capsys):
    """「排名」列显示较上一轮扫描的雪球榜单排名变化（+N 升 / -N 降）。

    名次刻意取 > TOP40_THRESHOLD：避免 TOP40 高亮色码插进「名次」与「变化」之间
    （高亮行为另由下方 test_priority_row_rank_top40_* 覆盖）。
    """
    # 上一轮排名：SZ300001 48→45 升 3 名；SZ300002 45→48 降 3 名；SZ300003 不变
    last_ranks = {"SZ300001": 48, "SZ300002": 45, "SZ300003": 46}
    for i, (sym, name, rank) in enumerate(
        [("SZ300001", "升名", 45), ("SZ300002", "降名", 48), ("SZ300003", "稳名", 46)], 1
    ):
        disp_mod._print_priority_row(
            _priority_entry(sym, name, live_rank=rank), i, {}, last_ranks=last_ranks
        )
    lines = _priority_rows(capsys.readouterr().out)
    assert "45+3" in lines["SZ300001"]
    assert "48-3" in lines["SZ300002"]
    assert "46" in lines["SZ300003"] and "46+" not in lines["SZ300003"] and "46-" not in lines["SZ300003"]


def test_priority_row_rank_delta_absent_by_default(capsys):
    """未传 last_ranks（缺省 None）时排名列仅显示名次，不带 +N/-N（回归旧显示）。"""
    disp_mod._print_priority_row(_priority_entry("SZ300001", "仅名次", live_rank=45), 1, {})
    line = _priority_rows(capsys.readouterr().out)["SZ300001"]
    assert "45" in line and "45+" not in line and "45-" not in line


# ── 榜单 TOP40 排名高亮（2026-08-12）：名次 ≤ TOP40_THRESHOLD 加粗+红色提示 ──
def _force_ansi(monkeypatch):
    """强制 ANSI 着色开启（测试环境控制台通常无 ANSI，ANSI 码为空串会误断言）。

    ANSI/_supports_ansi 由 scanner.view.model 定义、scanner.view.render 经 `import *`
    捕获绑定，故须同时打到两个子模块命名空间才对渲染链路整体生效。
    """
    _ansi = {
        "RED": "\033[91m",
        "YELLOW": "\033[93m",
        "GREEN": "\033[92m",
        "CYAN": "\033[96m",
        "MAGENTA": "\033[95m",
        "BOLD": "\033[1m",
        "RESET": "\033[0m",
    }
    for _m in (disp_mod, vm, vr):
        monkeypatch.setattr(_m, "_supports_ansi", True)
        monkeypatch.setattr(_m, "ANSI", _ansi)


def test_priority_row_rank_top40_highlight(monkeypatch, capsys):
    """名次在雪球榜单前 TOP40 内时排名数字加粗+红色高亮；40 名之外不高亮。"""
    _force_ansi(monkeypatch)
    disp_mod._print_priority_row(_priority_entry("SZ300001", "榜内40", live_rank=40), 1, {})
    disp_mod._print_priority_row(_priority_entry("SZ300002", "榜外41", live_rank=41), 2, {})
    lines = _priority_rows(capsys.readouterr().out)
    line_in, line_out = lines["SZ300001"], lines["SZ300002"]
    assert disp_mod.ANSI["BOLD"] in line_in and disp_mod.ANSI["RED"] in line_in
    assert "40" in _ANSI_RE.sub("", line_in)
    assert disp_mod.ANSI["BOLD"] not in line_out and disp_mod.ANSI["RED"] not in line_out
    assert "41" in line_out


def test_priority_row_rank_top40_highlight_with_delta(monkeypatch, capsys):
    """高亮只作用于名次数字，不吞掉排名变化（+N/-N 保持原样跟在后面）。"""
    _force_ansi(monkeypatch)
    disp_mod._print_priority_row(
        _priority_entry("SZ300001", "榜内升名", live_rank=5), 1, {}, last_ranks={"SZ300001": 8}
    )
    line = _priority_rows(capsys.readouterr().out)["SZ300001"]
    assert disp_mod.ANSI["BOLD"] in line and "+3" in line
    # 高亮在名次后闭合，delta 落在 RESET 之后（未被吞进色码区间）
    assert f"{disp_mod.ANSI['RESET']}+3" in line


# ── 核心股名称高亮（2026-08-19）：判定 = core_themes.core_stock_symbols ──
# 核心主题成员 + 20日累计≥CORE_RUN_MIN（走强龙头）。core_dip 候选必然满足这两条，
# {core_dip} ⊆ 核心股集——高亮比低吸区更宽：可覆盖「创新高走强中的主线龙头」（江天化学
# 08-19 案例：央国企改革成员、20日+22.3%，回撤0%落不进低吸窗口）。
def test_display_priority_core_stock_name_highlight(monkeypatch, capsys):
    """综合排序里属于核心股（core_stock_symbols 判定）的票名称加粗品红高亮；
    非核心股不亮。

    2026-09-14：核心方向低吸展示区已隐藏，故本测试只保留 v1 主表这一条验证路径
    （原先还额外验证低吸区行——那部分随展示区一起移除）。
    """
    _force_ansi(monkeypatch)
    # 模拟 core_stock_symbols：SZ300001 今日为核心股
    # build_scan_view（assemble）用其判定 _core_stock → row.core，故须打到 va。
    monkeypatch.setattr(va, "core_stock_symbols", lambda conn, today=None: {"SZ300001"})
    conn = _rec_db()
    _insert_rec_cat(conn, "SZ300001", "核心动量", "momentum", 70)
    _insert_rec_cat(conn, "SZ300002", "普通动量", "momentum", 65)
    disp_mod.display_priority(conn, today_pool={})
    out = capsys.readouterr().out
    lines = {sym: _main_line(out, sym) for sym in ["SZ300001", "SZ300002"]}
    assert disp_mod.ANSI["MAGENTA"] in lines["SZ300001"]
    assert disp_mod.ANSI["BOLD"] in lines["SZ300001"]
    assert disp_mod.ANSI["MAGENTA"] not in lines["SZ300002"]


def test_display_priority_no_core_stock_no_highlight(monkeypatch, capsys):
    """今日无核心股（core_stock_symbols 空集）时，主表无高亮（空集判定不误伤）。"""
    _force_ansi(monkeypatch)
    monkeypatch.setattr(va, "core_stock_symbols", lambda conn, today=None: set())
    conn = _rec_db()
    _insert_rec_cat(conn, "SZ300001", "动量票", "momentum", 70)
    _insert_rec_cat(conn, "SZ300002", "低吸票", "core_dip", 70)
    disp_mod.display_priority(conn, today_pool={})
    out = capsys.readouterr().out
    lines = [row for row in out.splitlines() if "SZ30000" in row]
    for line in lines:
        assert disp_mod.ANSI["MAGENTA"] not in line, f"无核心股不应有高亮: {line}"


def test_display_priority_pool_shows_price_and_rank_dash(monkeypatch, capsys):
    """v1 池选渲染「现价」列；无榜单排名时显示 — 而非排序占位 9999（2026-08-28 修复）。"""
    conn = _rec_db()
    _insert_rec_pct(conn, "SZ300001", "有价票", "momentum", 70, 3.0)
    _insert_rec_pct(conn, "SZ300002", "掉榜票", "rebound", 50, 2.0)  # 无 rank
    # SZ300001 候选带 current=10.0 / rank=1（_cand_tier 默认），验证现价与排名显示
    pool = {"SZ300001": _cand_tier("SZ300001", 70, "momentum", percent=3.0)}
    disp_mod.display_priority(conn, today_pool=pool)
    out = capsys.readouterr().out
    lines = _main_lines(out)
    ln1 = next(ln for ln in lines if "SZ300001" in ln)
    assert "10.00" in ln1, "v1 池选应渲染候选现价"
    assert " 1" in ln1, "候选排名应显示"
    ln2 = next(ln for ln in lines if "SZ300002" in ln)
    assert "9999" not in ln2, "无排名不应显示排序占位 9999"
    assert "—" in ln2, "无排名应显示 —"


def test_display_header_env_tag_matches_regime(monkeypatch, capsys):
    """头部大盘标签与动态推荐/飞书同源 _regime_weak（2026-08-30 统一）：弱市显示「弱势·谨慎」。

    此前头部走 market_env_bonus、动态推荐走 _regime_weak，两套信号可能同屏矛盾。
    """
    conn = _rec_db()
    monkeypatch.setattr(vr, "_regime_weak", lambda c, lookback=10: True)
    disp_mod.display(100, 60, conn=conn, today_pool={})
    out = capsys.readouterr().out
    assert "大盘弱势·谨慎" in out
    monkeypatch.setattr(vr, "_regime_weak", lambda c, lookback=10: False)
    disp_mod.display(100, 60, conn=conn, today_pool={})
    out = capsys.readouterr().out
    assert "大盘强势" in out
    """优选池行尾渲染 🎯（2026-08-30 主视图标记恢复）：甜蜜带+非超买+累计达门槛的票在主列表可见。

    此前 🎯/⚡ 仅在回马枪/低吸区渲染，换成 v1 池选后主视图丢失画像信息。
    2026-09-04: 🎯 命中率过低，暂时不渲染（档位判定逻辑保留）。"""
    conn = _rec_db()
    _insert_rec_pct(conn, "SZ300001", "甜蜜动量", "momentum", 70, 1.0)
    pool = {"SZ300001": _cand_tier("SZ300001", 70, "momentum", percent=1.0, accum=8.0)}
    disp_mod.display_priority(conn, today_pool=pool)
    out = capsys.readouterr().out
    line = next(ln for ln in _main_lines(out) if "SZ300001" in ln)
    # 2026-09-04: 🎯 临时不渲染，但档0逻辑保留
    assert "🎯" not in line, f"🎯 临时不渲染: {line}"


def test_entry_display_quote_fallback_chain():
    """涨幅/现价单源回退链：live 0.00% 合法不被 `or` 吞 → 候选快照 → DB 落库。"""
    # ① live 优先：0.00% 是合法涨幅，不得回退到 DB percent=5.0
    live0 = {
        "live_quote_available": True,
        "live_percent": 0.0,
        "live_current": 10.0,
        "percent": 5.0,
        "_candidate": None,
    }
    pct, cur = disp_mod.entry_display_quote(live0)
    assert pct == 0.0
    assert cur == 10.0
    # ② 掉榜行（无候选无 live）：落库 percent，现价无数据
    dropped = {"percent": 5.0, "live_percent": None, "_candidate": None}
    pct, cur = disp_mod.entry_display_quote(dropped)
    assert pct == 5.0
    assert cur == 0.0
    # ③ 可信候选快照：候选 percent/current 生效
    cand = _cand_tier("SZ300099", 70, "momentum", percent=2.5)
    cand_entry = {"percent": 1.0, "_candidate": cand}
    pct, cur = disp_mod.entry_display_quote(cand_entry)
    assert pct == pytest.approx(2.5)
    assert cur == pytest.approx(10.0)


# ── 弱市 regime 下核心低吸强制展示（2026-09-14 展示区已隐藏）──
# 原 `test_display_priority_recommended_region_shown_when_main_dense` 断言「弱市下
# 主区密集也强制展示核心低吸区」（修复「推荐了却看不到标的」割裂）。展示区已隐藏，
# 断言移除。⚠ `_regime_weak` 本身仍在产线使用（头部标签 / 动态推荐 / 飞书 env_tag
# 同源），只是不再驱动该区的可见性。


# ── 综合排序分组顺序（2026-08-07 复核：rebound > short_term > momentum > known_new_face > new_face > pullback）──
def _insert_rec_cat(conn, symbol: str, name: str, category: str, score: int):
    today = now_beijing().date().isoformat()
    conn.execute(
        "INSERT INTO recommendations (date, time, symbol, name, category, score, percent) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (today, "13:00", symbol, name, category, score, 3.0),  # 3.0=死区，避免掉榜行被 🎯 置顶
    )
    conn.commit()


def _cand_tier(
    symbol: str,
    score: int,
    category: str = "momentum",
    fund_flow: float | None = None,
    prominent: bool = False,
    percent: float = 3.0,
    accum: float = 8.0,
    incl_accum: float | None = None,
) -> Candidate:
    # percent 默认 3.0（2~4% 死区）：涨幅带判定取中间档，档位测试只由 prominent 决定。
    # accum / incl_accum 是 2026-09-16 前 🎯 累计门槛用到的历史默认值（门槛已随 🎯 删除）；
    # 仍保留形参以免大规模改动既有调用点——现仅作 KlineSummary 的普通字段填充。
    # incl_accum：accumulated_incl_today 维度（含今日口径，累计回放链消费）。
    dims: dict[str, object] = {}
    if fund_flow is not None:
        dims["fund_flow_main_pct"] = fund_flow
    if incl_accum is not None:
        dims["accumulated_incl_today"] = incl_accum
    k = KlineSummary(
        trend="", accumulated_pct=accum, volume_ratio=1.0, bottom_confirmed=False, score=score, dimensions=dims
    )
    c = Candidate(
        stock=StockInfo(
            symbol=symbol,
            name="测试",
            code=symbol[-6:],
            percent=percent,
            current=10.0,
            value=1e8,
            rank_change=0,
            rank=1,
        ),
        category=category,
        score=score,
        reason="",
        kline=k,
    )
    if prominent:
        c.prominence_labels.append("↻")
    return c


def test_prominence_no_longer_sorts(monkeypatch, capsys):
    """2026-08-12: 辨识度不再参与排序——只有辨识度(↻)、无甜蜜带的票按正常分数排序（不置顶）。
    2026-08-22 标记精简：↻ 行内展示同步下线（独立增量≈0），排序结论不变。"""
    conn = _rec_db()
    _insert_rec_cat(conn, "SZ300001", "辨识票", "momentum", 60)
    _insert_rec_cat(conn, "SZ300002", "高分普通票", "momentum", 150)
    pool = {"SZ300001": _cand_tier("SZ300001", 60, prominent=True), "SZ300002": _cand_tier("SZ300002", 150)}
    disp_mod.display_priority(conn, today_pool=pool)
    out = capsys.readouterr().out
    lines = _main_lines(out)
    assert "SZ300002" in lines[0], f"辨识度票(60)不再置顶，高分票(150)应在前: {lines}"
    assert "SZ300001" in lines[1]
    assert "↻" not in out, "↻ 行内标记已下线，不应再渲染"


def test_display_priority_tier_banner_separates_groups(capsys):
    """v1 池选排序（2026-09-16，用户决策：不依赖回测数据）：
    档位(过热硬门劣后) → 类别展示优先级(策略语义) → 榜单排名升序(实时热度)
    → 资金流降序(实时) → 形态标签加分。composite_score 仅作展示列，不再决定顺序。

    关键不变量：同类别内顺序由榜单排名决定，而非 raw score（score 是回测驱动的策略
    内部置信度，不能作为排序主键）。本例 SZ300001 评分 90 远高于 SZ300002 的 50，
    但榜单排名 50 远落后于后者的排名 5 ⇒ SZ300002 排在前，直接证伪旧 score 排序。
    """
    conn = _rec_db()
    _insert_rec_pct(conn, "SZ300001", "高分低排", "rebound", 90, 1.0)  # 评分高但榜单排名垫后
    _insert_rec_pct(conn, "SZ300002", "低分高排", "rebound", 50, 2.0)  # 评分低但榜单排名靠前
    _insert_rec_pct(conn, "SZ300003", "动量", "momentum", 70, 3.0)  # 类别档位劣后 → 末
    rank_map = {"SZ300001": 50, "SZ300002": 5, "SZ300003": 1}
    disp_mod.display_priority(conn, today_pool={}, rank_map=rank_map)
    out = capsys.readouterr().out
    assert "▶ 置顶档" not in out
    assert "▶ 普通档" not in out
    lines = _main_lines(out)
    assert len(lines) == 3, f"全部票应正常展示: {lines}"

    def _idx(sym: str) -> int:
        return next(i for i, ln in enumerate(lines) if sym in ln)

    # 榜单排名升序：低分高排(5) 先于 高分低排(50)，证伪 score 排序
    assert _idx("SZ300002") < _idx("SZ300001"), f"榜单排名应优先于 score: {lines}"
    # 类别档位：rebound(tier0) 整体先于 momentum(tier1)
    assert _idx("SZ300003") == 2, f"momentum 应排最后(档位劣后): {lines}"


def test_pool_pick_kept_out_of_v1_main_table(capsys):
    """pool_pick 不混入 v1 主表（双跑同屏时期的隔离语义，2026-09-02 建立）。

    2026-09-14：v2 池选展示区已隐藏，故这里不再断言「存在 ◆ v2 池选 区块」，
    只守住仍然重要的隔离不变量 —— pool_pick 类别不得出现在 v1 主表里。
    该隔离至今必需：pool_pick 仍是终选参考区合池输入之一，混进 main_recs 会同时
    改变 v1 主表内容与终选结果。
    """
    conn = _rec_db()
    _insert_rec_pct(conn, "SZ300001", "v1票", "rebound", 50, 1.0)
    _insert_rec_pct(conn, "SZ300002", "池高", "pool_pick", 70, 7.9)  # 帽下最高带，仍不得进主表
    _insert_rec_pct(conn, "SZ300003", "池中", "pool_pick", 90, 2.0)
    disp_mod.display_priority(conn, today_pool={})
    out = capsys.readouterr().out

    main_syms = _main_lines(out)
    assert any("SZ300001" in ln for ln in main_syms), f"v1 rebound 应留在主表: {main_syms}"
    assert not any("SZ300002" in ln or "SZ300003" in ln for ln in main_syms), (
        f"pool_pick 不应混入 v1 主表（涨幅再高也不进）: {main_syms}"
    )


def test_core_dip_kept_out_of_v1_main_table(capsys):
    """core_dip 同样不进 v1 主表（低吸区展示区隐藏后，隔离语义仍需守住）。"""
    conn = _rec_db()
    _insert_rec_pct(conn, "SZ300001", "v1票", "rebound", 50, 1.0)
    _insert_rec_pct(conn, "SZ300002", "低吸票", "core_dip", 90, 1.0)
    disp_mod.display_priority(conn, today_pool={})
    out = capsys.readouterr().out

    main_syms = _main_lines(out)
    assert any("SZ300001" in ln for ln in main_syms)
    assert not any("SZ300002" in ln for ln in main_syms), f"core_dip 不应混入 v1 主表: {main_syms}"


# ── v2 池选区 / 核心方向低吸区（2026-09-14 按用户决策隐藏）──
# 原 test_display_priority_pool_pick_independent_section_sorted /
# _kept_out_of_main_even_higher_pct / _dip_label_segment_sorted（v2 池选区排序与两段式
# 低吸标签分段）、test_display_priority_core_dip_capped（低吸区截断到
# CORE_DIP_DISPLAY_MAX 条）五个测试已移除 —— 它们断言的两个展示区不再渲染。
# 上面两条 *_kept_out_of_v1_main_table 保留了其中仍然有效的隔离不变量（不变量比区块长寿）。
# ⚠ 若恢复这两个展示区，需一并恢复上述截断/分段/排序断言（见 git 历史）。


# ── 次日大涨画像标记（2026-08-11 起并入主表行尾 🎯；2026-08-12 起成为排序档0唯一因子）──
# 原独立区（2026-08-10）与主表重合度 65%（主表 17 只中 11 只甜蜜带、两表排序几乎一致、
# 辨识度因子空转），重复输出；改为主表行尾标记。筛形条件不变（nextday_attribution 口径）：
# 推荐时刻涨幅甜蜜带（<2% 低吸潜伏 / 4~8% 中段启动）且非超买死亡信号。
# 2026-08-12：🎯 从纯视觉标记升级为排序档0唯一因子——辨识度退出排序（次日大涨本身即
# 辨识度属性），↻ 仅保留行内展示。
def test_display_max_today_pct_hides_trap_band(capsys):
    """不追涨帽（2026-09-04 修正默认 8.0）：v1 主表隐藏今日涨幅 >8% 的票
    （8-12% 是实测陷阱带：超额 -0.70 / 大跌率 13%）；帽下票正常展示。
    纯显示层过滤——落库/评分/回测不受影响。
    （2026-09-14：v2 池选区已隐藏，本测试只剩 v1 主表这一条验证路径。）"""
    conn = _rec_db()
    _insert_rec_pct(conn, "SZ300001", "帽下票", "momentum", 70, 7.9)
    _insert_rec_pct(conn, "SZ300002", "陷阱票", "momentum", 70, 9.5)  # 8-12% 陷阱带
    _insert_rec_pct(conn, "SZ300003", "帽上票", "rebound", 50, 12.0)
    disp_mod.display_priority(conn, today_pool={})
    out = capsys.readouterr().out
    assert "SZ300001" in out, "帽下票（7.9%）应正常展示"
    assert "SZ300002" not in out, f"陷阱带票（9.5%）应被帽隐藏: {out}"
    assert "SZ300003" not in out, f"帽上票（12.0%）应被帽隐藏: {out}"


def _insert_rec_pct(conn, symbol: str, name: str, category: str, score: int, percent: float):
    today = now_beijing().date().isoformat()
    conn.execute(
        "INSERT INTO recommendations (date, time, symbol, name, category, score, percent) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (today, "13:00", symbol, name, category, score, percent),
    )
    conn.commit()


def test_nextday_mark_no_hits_omitted(monkeypatch, capsys):
    """🎯 标记：无甜蜜带票时不打印图例行（主表正常显示）。"""
    conn = _rec_db()
    _insert_rec_pct(conn, "SZ300001", "普通票", "momentum", 70, 5.0)  # 帽下正常带
    disp_mod.display_priority(conn, today_pool={})
    out = capsys.readouterr().out
    assert "次日大涨画像" not in out, "无甜蜜带票时不应有 🎯 组标题（档0 应无票）"
    assert "SZ300001" in out  # 主表仍正常显示


def _insert_rec_pct_accum(conn, symbol: str, name: str, category: str, score: int, percent: float, accum) -> None:
    today = now_beijing().date().isoformat()
    conn.execute(
        "INSERT INTO recommendations (date, time, symbol, name, category, score, percent, accumulated_pct) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (today, "13:00", symbol, name, category, score, percent, accum),
    )
    conn.commit()


def test_nextday_mark_accum_gate_low(monkeypatch, capsys):
    """🎯 累计门槛：甜蜜带 momentum 票 5 日累计 <6 不打标记（低累计平盘=无动量，最差档）。"""
    conn = _rec_db()
    _insert_rec_pct(conn, "SZ300001", "低累计动量", "momentum", 70, 1.0)
    pool = {"SZ300001": _cand_tier("SZ300001", 70, "momentum", percent=1.0, accum=2.0)}
    disp_mod.display_priority(conn, today_pool=pool)
    out = capsys.readouterr().out
    line = next(ln for ln in out.splitlines() if "SZ300001" in ln)
    assert "🎯" not in line, f"累计 2.0(<6) 不应标 🎯: {line}"


def test_nextday_mark_accum_incl_today_dim_low(monkeypatch, capsys):
    """🎯 累计口径修复：含今日维度值不足门槛同样不标（维值优先级不绕过门槛）。"""
    conn = _rec_db()
    _insert_rec_pct(conn, "SZ300001", "含今日低累计", "momentum", 70, 1.0)
    pool = {"SZ300001": _cand_tier("SZ300001", 70, "momentum", percent=1.0, accum=9.0, incl_accum=2.0)}
    disp_mod.display_priority(conn, today_pool=pool)
    out = capsys.readouterr().out
    line = next(ln for ln in out.splitlines() if "SZ300001" in ln)
    assert "🎯" not in line, f"含今日累计 2.0(<6) 不应标 🎯: {line}"


def test_nextday_mark_accum_offlist_prefers_replay_over_db(monkeypatch, capsys):
    """🎯 累计口径修复：掉榜行（无候选）优先回放含推荐日口径，不再优先 DB 落库的历史口径。

    回归：DB accumulated_pct=12.0（扫描时刻不含今日的快照），但 daily_kline 回放
    含推荐日口径仅 +2.5%——门槛应基于含推荐日口径，此前 DB 值优先会误标。"""
    conn = _rec_db_with_kline()
    closes = [10.0 + 0.05 * i for i in range(6)]  # 含推荐日累计约 +2.5%（平盘）
    for i, c in enumerate(closes):
        d = (now_beijing().date() - timedelta(days=5 - i)).isoformat()
        conn.execute(
            "INSERT INTO daily_kline (symbol, date, close, percent) VALUES (?, ?, ?, ?)",
            ("SZ300001", d, c, 1.0),
        )
    _insert_rec_pct_accum(conn, "SZ300001", "回放优先", "momentum", 70, 1.0, 12.0)
    conn.commit()
    disp_mod.display_priority(conn, today_pool={})
    out = capsys.readouterr().out
    line = next(ln for ln in out.splitlines() if "SZ300001" in ln)
    assert "🎯" not in line, f"回放累计 +2.5%(<6) 应压过 DB 落库 12.0（历史口径）不标: {line}"


def _rec_db_with_kline():
    conn = _rec_db()
    conn.execute("""CREATE TABLE daily_kline (
        symbol TEXT NOT NULL, timestamp INTEGER, date TEXT NOT NULL,
        open REAL, close REAL, high REAL, low REAL, volume REAL, percent REAL) """)
    return conn


def test_nextday_mark_accum_kline_replay_low(monkeypatch, capsys):
    """🎯 累计兜底：kline 回放累计不足门槛（<6）不打标记。"""
    conn = _rec_db_with_kline()
    today = now_beijing().date().isoformat()
    closes = [10.0 + 0.05 * i for i in range(6)]  # 累计约 +2.5%（平盘）
    for i, c in enumerate(closes):
        d = (now_beijing().date() - timedelta(days=5 - i)).isoformat()
        conn.execute(
            "INSERT INTO daily_kline (symbol, date, close, percent) VALUES (?, ?, ?, ?)",
            ("SZ300002", d, c, 1.0),
        )
    conn.execute(
        "INSERT INTO recommendations (date, time, symbol, name, category, score, percent) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (today, "13:00", "SZ300002", "平盘回放", "momentum", 70, 1.0),
    )
    conn.commit()
    disp_mod.display_priority(conn, today_pool={})
    out = capsys.readouterr().out
    line = next(ln for ln in out.splitlines() if "SZ300002" in ln)
    assert "🎯" not in line, f"kline 回放累计 +2.5%(<6) 不应标 🎯: {line}"


def _insert_rec_sb(conn, symbol: str, name: str, category: str, score: int, percent: float, sb: str = "{}") -> None:
    today = now_beijing().date().isoformat()
    conn.execute(
        "INSERT INTO recommendations (date, time, symbol, name, category, score, percent, score_breakdown) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (today, "13:00", symbol, name, category, score, percent, sb),
    )
    conn.commit()


# ── 档位 4 级（2026-08-17）：今日总结的选股规则全部编码进排序键 ──
# 档0=🎯 次日画像 / 档1=rebound·comeback资金流≥3% / 档2=普通 / 档3=警示劣后
# （超买·陷阱带·死区·累计≥50%过热·资金流出≤-8%）。跨类别全局生效，纯排序层不改评分。
def test_display_priority_tier4_sector_resonance_low(capsys):
    """排序规则（2026-08-28，双跑同屏后主表恒为 v1 口径）：榜上优先 → 涨幅升序 → 回调核心
    → 排名升序 → 新面孔；档位不再参与主排序。故 🎯 票（涨幅 1.0% 最低）排最前，
    小板块共振(档3)不再因档位被劣后到末尾，仅与其余 6% 票按稳定顺序并列。"""
    conn = _rec_db()
    _insert_rec_pct(conn, "SZ300001", "普通超短", "short_term", 60, 6.0)  # 档2 无警示
    _insert_rec_sb(
        conn, "SZ300002", "小板块共振", "short_term", 80, 6.0, '{"v_st_sector": 10, "v_st_sector_count": 8}'
    )  # 档3 小板块
    _insert_rec_sb(
        conn, "SZ300003", "大板块共振", "short_term", 70, 6.0, '{"v_st_sector": 10, "v_st_sector_count": 20}'
    )  # 档2 大板块豁免
    _insert_rec_sb(
        conn,
        "SZ300004",
        "弱转强共振",
        "short_term",
        68,
        1.0,
        '{"st_weak_to_strong": 8, "v_st_sector": 10, "v_st_sector_count": 8, "v_st_overbought": false}',
    )  # 档0 🎯 豁免
    disp_mod.display_priority(conn, today_pool={})
    out = capsys.readouterr().out
    lines = [ln for ln in _main_lines(out) if "SZ3000" in ln]
    # 2026-09-16：排序不再依赖 composite_score/raw score（非回测驱动）。四只 short_term
    # 档位相同(均 tier3)且无榜单排名/资金流差异时按稳定顺序并列，不再按分数降序；
    # 故不再断言「高分短差(80)排最前」，只守住核心不变量：档位不再过滤/劣后到末尾。
    assert len(lines) == 4, f"档位不应再过滤/劣后排序: {lines}"
    assert any("SZ300002" in ln for ln in lines), f"小板块共振仍应展示: {lines}"
    assert any("SZ300001" in ln for ln in lines), f"低分票不应被分数劣后隐藏: {lines}"


def test_priority_row_breakout_mark_single_symbol(capsys):
    """行尾只渲染一个 ⚡ 符号（新面孔/重上榜两变体在判定层区分，渲染不区分）。"""
    entry = {
        "symbol": "SZ300001",
        "name": "肯特股份",
        "category": "short_term",
        "score": 44,
        "percent": 7.4,
        "time": "10:30",
        "_candidate": None,
    }
    disp_mod._print_priority_row(entry, 1, {}, breakout_mark=True)
    out = capsys.readouterr().out
    assert "⚡" in out and "⚡R" not in out


def test_display_priority_relist_hit_renders_bolt(capsys):
    """重上榜变体命中也走同一 ⚡ 标记（display_priority 接线锁定，样本积累路径）。

    构造：short_term 非首推推荐 + 前 ≥21 根缩量回调 K 线（肯特股份形态）。
    2026-09-17：表尾那行合并图例（「⚡ 蓄势突破观察…」）已随图例块整体下线，故只守标记本身。
    本用例是**唯一**驱动 display_priority 真实重上榜判定链路的一条 ——
    test_priority_row_breakout_mark_single_symbol 直接传 breakout_mark=True，
    只覆盖渲染，不覆盖「行情+K线 → 判定出 ⚡」这一段。
    """
    conn = _rec_db()
    conn.execute("""CREATE TABLE daily_kline (
        symbol TEXT NOT NULL, date TEXT NOT NULL, open REAL,
        close REAL, high REAL, low REAL, volume REAL, percent REAL,
        PRIMARY KEY(symbol, date))""")
    today = now_beijing().date()
    # 冲高 39 → 深回撤 -13%（距高点）→ 尾部缓慢修复（MA 多头），全程缩量；末根 = T-1。
    # 尾部 6 根累计需 ≤5%（BREAKOUT_ACCUM_MAX，走真实回放链路而非显式 accum）。
    closes = [
        28.0,
        28.5,
        29.0,
        29.5,
        39.0,
        37.0,
        35.0,
        33.5,
        32.5,
        32.0,
        31.8,
        31.5,
        31.9,
        32.3,
        32.6,
        33.0,
        33.4,
        33.7,
        34.0,
        34.15,
        34.3,
        34.45,
    ]
    n = len(closes)
    vol = 2_000_000.0
    for i, close in enumerate(closes):
        d = (today - timedelta(days=n - i)).isoformat()
        high = close * (1.02 if i == 4 else 1.01)
        vol = max(400_000.0, vol * 0.93)
        conn.execute(
            "INSERT OR REPLACE INTO daily_kline VALUES (?,?,?,?,?,?,?,?)",
            ("SZ300001", d, close, close, high, close, vol, 0.0),
        )
    conn.execute(
        "INSERT INTO recommendations (date, time, symbol, name, category, score, percent) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (today.isoformat(), "10:30", "SZ300001", "肯特型", "short_term", 44, 7.4),
    )
    conn.commit()
    disp_mod.display_priority(conn, today_pool={})
    out = capsys.readouterr().out
    assert "⚡" in out and "⚡R" not in out, "命中时应打印单个 ⚡（两变体不区分渲染）"


# ── 表头 / 数据行列边界一致（2026-08-29）──
# 2026-08-13 / 08-20 / 08-21 三次修的都是列宽问题，但既有测试只覆盖 _vis_len / _pad /
# _trunc 原语，无人断言「表头列边界 == 数据行列边界」——而 detail 表此前正是由两处
# 独立 f-string 拼成：表头序号列后 1 空格、数据行 2 空格，导致「代码」列起右偏 1 列。
def _col_starts(spec):
    """各列左边界的可见宽度偏移（行首缩进 2 + 每列后 1 空格分隔）。"""
    starts = []
    pos = 2
    for _title, width, _align in spec:
        starts.append(pos)
        pos += width + 1
    return starts


def _char_at_width(s, target):
    """可见宽度 target 处的字符（中文按 2 列计）；越界返回 None。"""
    w = 0
    for ch in s:
        if w == target:
            return ch
        w += max(0, wcwidth.wcwidth(ch))
    return None


def test_table_header_and_row_share_column_starts():
    """_table_row 与 _table_header 必须落在同一组列起点上（同一 spec 推导）。"""
    for spec in (disp_mod.COLS_POOL, disp_mod.COLS_DETAIL):
        header = disp_mod._table_header(spec)
        row = disp_mod._table_row([str(i) for i in range(len(spec))], spec)
        for col, start in enumerate(_col_starts(spec)):
            assert _char_at_width(header, start) is not None, f"表头第 {col} 列起点 {start} 越界"
            assert _char_at_width(row, start) is not None, f"数据行第 {col} 列起点 {start} 越界"


def test_priority_row_code_column_aligns_with_header(capsys):
    """回归：_print_priority_row 的「代码」列左边界必须与表头一致。

    破坏方式（历史 bug）：把表头/行改回两处独立 f-string，或让数据行序号列后多打
    一个空格（`{i:3d}  `）——则 starts[1] 处会落到空格而非代码首字符。
    """
    entry = {
        "symbol": "SZ300319",
        "name": "麦捷科技",
        "category": "comeback",
        "score": 72,
        "time": "10:30:00",
        "first_time": "10:30:00",
        "percent": 3.21,
        "accumulated_pct": 5.0,
        "_candidate": None,
        "_core_stock": False,
    }
    disp_mod._print_priority_row(entry, 1, {})
    row = capsys.readouterr().out.rstrip("\n")
    header = disp_mod._table_header(disp_mod.COLS_DETAIL)

    code_start = _col_starts(disp_mod.COLS_DETAIL)[1]
    # 表头该偏移处必须是「代码」首字；数据行同偏移处必须是代码首字符（左对齐、无前导空格）
    assert _char_at_width(header, code_start) == "代"
    assert _char_at_width(row, code_start) == "S", (
        f"数据行代码列未对齐表头：期望偏移 {code_start} 处为 'S'，实际 {_char_at_width(row, code_start)!r}"
    )


# ── 终端 / 飞书同源（2026-08-29，P0 #1 收口）──
def test_feishu_card_matches_terminal_selection(capsys):
    """回归：飞书卡片与终端必须渲染同一批票。

    此前飞书 _build_card 读「本轮候选桶」（new_faces/momentum/...），终端读
    「DB 当日累计推荐」——同一只票可能一边排第 1、另一边不出现。现两端共用
    build_scan_view 产出的同一份 ScanView。
    """
    from scanner.feishu import build_feishu_card

    all_syms = {"SZ300001", "SZ300002", "SZ300003"}
    conn = _rec_db()
    for sym in sorted(all_syms):
        _insert_rec_cat(conn, sym, f"股{sym[-1]}", "momentum", 70)

    view = disp_mod.build_scan_view(conn, today_pool={})
    assert view is not None, "有推荐时应返回 ScanView"

    disp_mod.render_terminal(view)
    terminal_out = capsys.readouterr().out

    card_text = str(build_feishu_card(view, gem_total=100))

    # 终端渲染出的票（行格式：序号 代码 名称 ...）
    terminal_syms = {ln.split()[1] for ln in terminal_out.splitlines() if "SZ30000" in ln}
    assert terminal_syms, "终端应渲染出推荐行"

    card_syms = {s for s in all_syms if s in card_text}
    assert card_syms == terminal_syms, (
        f"飞书卡片与终端选择不一致：终端有而卡片缺 {terminal_syms - card_syms}；"
        f"卡片有而终端无 {card_syms - terminal_syms}"
    )


# ── 走势美感标记（2026-09-15 分档：日线定准入、分时定级别 → ""/"美"/"美★"）──


def test_beauty_mark_for_verdicts():
    """日线漂亮+分时确认漂亮 → 美★；分时走弱/缺失 → 只降档到「美」；日线缺失 → 不标。"""
    from datetime import timedelta
    from types import SimpleNamespace

    from scanner.config import now_beijing
    from scanner.display import _beauty_mark_for

    d0 = now_beijing().date()
    bars = []
    prev = 10.0
    for i in range(25):
        d = (d0 - timedelta(days=24 - i)).isoformat()
        close = prev * 1.01
        bars.append(
            {
                "date": d,
                "open": round(prev, 3),
                "close": round(close, 3),
                "high": round(close * 1.003, 3),
                "low": round(prev * 0.997, 3),
                "volume": 1000.0,
                "percent": 1.0,
            }
        )
        prev = close

    def cand(intraday):
        return SimpleNamespace(
            category="pool_pick",
            is_stale=False,
            intraday_score=intraday,
            tactic_tags=[],
            stock=SimpleNamespace(percent=1.5, current=10.0),
            kline=None,
        )

    entry: dict = {"symbol": "SZ300001", "category": "pool_pick"}
    entry["_candidate"] = cand(5.0)
    assert _beauty_mark_for(entry, bars) == "美★"  # 日线+分时双维度确认
    assert _beauty_mark_for(entry, None) == ""  # 日线是准入：日线缺失不标

    entry_bad = {"symbol": "SZ300001", "category": "pool_pick", "_candidate": cand(-2.0)}
    assert _beauty_mark_for(entry_bad, bars) == "美"  # 日线好、分时差 → 降档不淘汰

    bare = {"symbol": "SZ300001", "category": "pool_pick"}  # 无候选无分时维度 = 缺失
    assert _beauty_mark_for(bare, bars) == "美"  # fail-open 只降档
    assert _beauty_mark_for(bare, None) == ""  # 日线也缺 → 不标


def test_hist_inline_marks_on_both_surfaces(capsys):
    """回捞区行尾标记（资金流 ▲/▼ + 日线美感「美」）必须在终端与飞书两端都落地。

    2026-09-16 起两端都不再打印冗长图例（飞书卡片按用户决策移除；终端同款图例亦已注释），
    只保留行内标记。图例冗余移除后，行内标记是用户唯一能读到的强弱信号，不能丢 ——
    本用例守护「标记两端都不丢」。
    """
    from scanner.feishu import build_feishu_card
    from scanner.historical_watch import HistCandidate

    conn = _rec_db()
    _insert_rec_cat(conn, "SZ300001", "股1", "momentum", 70)
    view = disp_mod.build_scan_view(conn, today_pool={})
    assert view is not None

    view.hist_rows = [
        HistCandidate(
            symbol="SZ300750",
            code="300750",
            name="宁德时代",
            current=180.5,
            percent=-4.2,
            vol_ratio=1.35,
            rec_date="2026-09-15",
            rec_days_ago=1,
            rec_category="momentum",
            rec_score=70,
            cum_pct=3.5,
            market_cap=8e11,
            ff_pct=6.2,  # ≥+5% → 终端 ▲ / 卡片 🟢
            beauty="美",
            score=66.0,
        )
    ]
    disp_mod.render_terminal(view)
    terminal_out = capsys.readouterr().out
    card_text = str(build_feishu_card(view, gem_total=100))
    assert "本区无分时档" not in terminal_out, "冗长图例已移除，不应再出现"
    assert "▲ 美" in terminal_out, f"终端行尾应带资金流 ▲ 与日线美感「美」：{terminal_out[:200]}"
    assert "🟢 美" in card_text, f"卡片行尾应带 emoji 资金流与「美」：{card_text[:200]}"


def test_hot_inline_marks_on_both_surfaces(capsys):
    """飙升区行尾标记（资金流 ▲/▼ + 日线美感「美」）必须在终端与飞书两端都落地。

    2026-09-16 起两端都不再打印冗长图例（飞书卡片按用户决策移除；终端同款图例亦已注释），
    只保留行内标记。图例冗余移除后，行内标记是用户唯一能读到的强弱信号，不能丢 ——
    本用例守护「标记两端都不丢」。
    """
    from scanner.feishu import build_feishu_card
    from scanner.hot_watch import HotCandidate

    conn = _rec_db()
    _insert_rec_cat(conn, "SZ300001", "股1", "momentum", 70)
    view = disp_mod.build_scan_view(conn, today_pool={})
    assert view is not None

    view.hot_rows = [
        HotCandidate(
            symbol="SZ300750",
            code="300750",
            name="宁德时代",
            exchange="SZ",
            current=180.5,
            percent=4.2,
            rank_change=900,
            rank=3,
            volume=1e7,
            amount=3e8,
            market_capital=8e10,
            turnover_rate=6.0,
            volume_ratio=1.2,
            score=70.0,
            streak=1,
            ff_pct=6.2,  # ≥+5% → 终端 ▲ / 卡片 🟢
            beauty="美",
        )
    ]
    disp_mod.render_terminal(view)
    terminal_out = capsys.readouterr().out
    card_text = str(build_feishu_card(view, gem_total=100))
    assert "已被硬门剔除" not in terminal_out, "冗长图例已移除，不应再出现"
    assert "▲ 美" in terminal_out, f"终端飙升行尾应带资金流 ▲ 与日线美感「美」：{terminal_out[:200]}"
    assert "🟢 美" in card_text, f"卡片飙升行尾应带 emoji 资金流与「美」：{card_text[:200]}"


def test_beauty_mark_disabled_when_gate_off(monkeypatch):
    """RTS_TREND_MARK=0：展示标记整体关闭（与硬拦开关独立）。

    TREND_MARK_ENABLED 由 scanner.view.model 定义并持有（_beauty_mark_for 在其命名空间内读取），
    故 monkeypatch 须打到 vm 而非 scanner.display。
    """
    import scanner.display as disp

    monkeypatch.setattr(vm, "TREND_MARK_ENABLED", False)
    entry = {"symbol": "SZ300001", "category": "pool_pick"}
    assert disp._beauty_mark_for(entry, None) == ""


def test_entry_row_suffix_renders_beauty_tag():
    """行尾 suffix：「美」绿色，未传不渲染。"""
    from scanner.display import ANSI, _entry_row_suffix

    e = {"symbol": "SZ300001", "name": "票", "category": "pool_pick"}
    out_ok = _entry_row_suffix(e, {}, beauty="美")
    assert "美" in out_ok and ANSI["GREEN"] in out_ok
    assert _entry_row_suffix(e, {}) == ""


# ── 视图层零写库副作用（2026-09-13 测评 A2；决策层删除后的现状）──
# 原三条测试已随决策层删除移除：
#   test_build_scan_view_does_not_write_decision_picks —— 断言 build_scan_view 不写
#     decision_picks 表（该表已不再创建）；
#   test_build_scan_view_accepts_injected_decision_lines /
#   test_display_entry_passes_decision_lines_through —— 断言 decision_lines 注入与透传
#     （该参数已从 build_scan_view / display / display_priority 签名移除）。
#
# ⚠ A2 的**不变量本身仍然有效且仍被守护**：`build_scan_view` 是"只算不画、不写库"的
# 纯计算函数。当前由 tests/test_view_flow_gate.py::test_build_scan_view_does_not_touch_excluded_column
# 承担（展示层过滤不得改 recommendations.excluded）。
# 决策层删除后视图层已无任何写库路径，故这条不变量目前是"空集为真"——
# 若日后重新引入带副作用的视图逻辑，必须同时补一条直接断言（勿只靠这条注释）。


# ── 综合判断摘要（2026-09-17 重写：分区体检报告，取代「三区加权 → 推荐X、Y」）──
# 直接调用纯函数 _build_summary，不经 render_terminal 抓文本——本仓教训：锚点
# `_main_line` 那类"从整屏输出里挑一行"的写法会在格式微调后**静默失真**
# （不再命中被测代码却仍然通过）。
def _summary_row(symbol="SZ300001", name="甲", category="momentum", pct=1.0, accum=-1.0):
    """构造一行 MainRow（纯展示函数 _build_summary 的输入，不需要 DB）。"""
    entry = {"symbol": symbol, "name": name, "category": category, "score": 60, "percent": pct}
    return vm.MainRow(
        entry=entry,
        rank=1,
        accum=accum,
        score=60.0,
        composite_score=5.0,
        core=False,
        cat_label="MOM",
        pct=pct,
        current=10.0,
        sector="半导体",
    )


def _summary(**over):
    """带全部默认入参调用 _build_summary（参数关键字唯一，防止哑参重新混进来）。"""
    kw = {
        "weak": False,
        "market_idx_pct": 1.2,
        "main_rows": [],
        "hist_rows": [],
        "hot_rows": [],
        # 沪深飙升区 B 段（榜外异动，2026-09-18）：与 hot_rows 分开计数的第四个来源。
        "offboard_rows": [],
        "beauty_mark": {},
        "flow_pct_map": {},
        "flow_filtered": 0,
        "chase_filtered": 0,
        "tactic_filtered": 0,
    }
    kw.update(over)
    return va._build_summary(**kw)


def test_summary_is_a_report_not_a_recommendation_list():
    """不得再输出「推荐X、Y」。

    旧实现把三区各自加权后丢进同一个池子排序取前 4，而三区口径**不可比**
    （主线 = next_day 靶点 / 回捞 = 回调到位 / 飙升 = 当日热度），等价于交出一份
    没有口径的排名；新摘要只做分区计数 + 口径判定，名单在下面三张表里本来就有。
    """
    rows = [_summary_row(symbol="SZ300001", name="甲", pct=1.0)]
    out = _summary(
        main_rows=rows,
        flow_pct_map={"SZ300001": 9.0},
        beauty_mark={("SZ300001", "momentum"): "美"},
    )
    assert isinstance(out, list) and len(out) >= 2, out
    text = "\n".join(out)
    assert "推荐" not in text
    assert out[0].startswith("强市 · ")
    assert "重点观察 甲(300001)" in text  # 6 位纯码走 code_of 单源，不是 replace("SZ","")


def test_summary_strong_signal_is_intersection_not_weighted_sum():
    """强信号 = 「主力净占比 ≥ 强流入分界」∧「未追涨」的**交**，不是加权求和。

    旧实现里强流入 +3、涨幅 5% 只 −1，两者相抵仍得正分 → 一只已追涨的票照样能进名单；
    新规则把追涨设为**否决项**，不存在"抵掉"。
    """
    rows = [
        _summary_row(symbol="SZ300001", name="追涨甲", pct=5.5),
        _summary_row(symbol="SZ300002", name="合格乙", pct=1.5),
    ]
    out = _summary(main_rows=rows, flow_pct_map={"SZ300001": 9.0, "SZ300002": 9.0})
    text = "\n".join(out)
    assert "强流入 2 · 强信号 1" in text
    assert "追涨甲" not in text and "合格乙" not in text  # 无美感 → 不生成「重点观察」名单
    assert "无重点观察" in text


def test_summary_weak_market_suppresses_attack_verdict():
    """弱市优先于一切：即便有强信号也只给「仅观察」，且**不点名**个股。

    结论行说观望、明细却列出一只票名字，属于自相矛盾（强信号只数已在结论行给出）。
    （旧实现里 weak 是**哑参**——签名收了却从不读，弱市与强市输出完全同形。）
    """
    out = _summary(
        weak=True,
        main_rows=[_summary_row(name="弱市甲", pct=1.0)],
        flow_pct_map={"SZ300001": 9.0},
        beauty_mark={("SZ300001", "momentum"): "美"},
    )
    text = "\n".join(out)
    assert out[0].startswith("弱市 · 仅观察"), out[0]
    assert "口径一致" not in text
    assert "重点观察" not in text and "弱市甲" not in text


def test_summary_flags_divergence_with_risk_breakdown():
    """强信号与风险证据并存 → 判「口径分歧」并展开三道风险门的剔除明细。"""
    out = _summary(
        main_rows=[_summary_row(pct=1.0)],
        flow_pct_map={"SZ300001": 9.0},
        flow_filtered=3,
        chase_filtered=1,
        tactic_filtered=0,
    )
    text = "\n".join(out)
    assert "口径分歧" in text
    assert "风险剔除 资金流出 3 · 追涨 1 · 减仓标签 0" in text


def test_summary_consistent_when_risk_below_threshold():
    """风险剔除 1 只（< SUMMARY_RISK_DIVERGE）→ 口径一致，且不展开明细行。"""
    out = _summary(main_rows=[_summary_row(pct=1.0)], flow_pct_map={"SZ300001": 9.0}, flow_filtered=1)
    text = "\n".join(out)
    assert "口径一致" in text
    assert "风险剔除 资金流出" not in text


def test_summary_gap_line_only_when_degraded():
    """数据完整度行只在真有缺失时输出——每轮复读「一切正常」会淹没真正异常的那几轮。"""
    healthy = _summary(main_rows=[_summary_row()], flow_pct_map={"SZ300001": 9.0})
    assert not any(ln.startswith("数据 ") for ln in healthy), healthy

    degraded = _summary(
        main_rows=[_summary_row(symbol="SZ300001"), _summary_row(symbol="SZ300002")],
        flow_pct_map={"SZ300001": 9.0},  # SZ300002 缺当日快照
        market_idx_pct=None,
        hot_rows=None,  # None = 本轮未产出（区别于 [] = 跑了但无结果）
        hist_rows=None,
    )
    text = "\n".join(degraded)
    assert "资金流快照缺 1/2 行" in text
    assert "指数缺失" in text
    assert "飙升区未产出" in text and "回捞区未产出" in text


def test_summary_hist_ready_requires_volume():
    """回捞「到位」= 今日没涨 ∧ 有量：缩量(vr<1.0)不算到位。

    依据：该样本域缩量回调 hit 4.3%，显著低于平量的 8.1%（2026-09-16 实测）——
    缩量是「没人接」而不是「惜售」。旧实现只给高量比加分，对缩量无任何表示。
    """
    from scanner.historical_watch import HistCandidate

    def _h(percent, vr):
        return HistCandidate(
            symbol="SZ300004",
            code="300004",
            name="回捞甲",
            current=10.0,
            percent=percent,
            vol_ratio=vr,
            rec_date="2026-09-15",
            rec_days_ago=1,
            rec_category="momentum",
            rec_score=60,
            cum_pct=-3.0,
            market_cap=8e9,
        )

    out = _summary(hist_rows=[_h(1.0, 0.7), _h(1.5, 1.6)])
    assert "回捞 2 只（到位 1）" in "\n".join(out)


def test_summary_hot_region_is_counted_not_ranked():
    """飙升区只报计数并标注口径，绝不进「重点观察」——它不是 next_day 口径。

    （实测该区在榜 vs 未在榜的下行 lift 是上行 lift 的 1.7 倍，属下行风险选择器。）
    """
    from scanner.hot_watch import HotCandidate

    def _hot(rc, pct):
        return HotCandidate(
            symbol="SZ300005",
            code="300005",
            name="飙升甲",
            exchange="SZ",
            current=10.0,
            percent=pct,
            rank_change=rc,
            rank=3,
        )

    out = _summary(hot_rows=[_hot(9999, 5.0), _hot(100, 1.0)])
    text = "\n".join(out)
    assert "飙升 2 只（跃升 1·非 next_day 口径）" in text
    assert "飙升甲" not in text


def test_build_scan_view_wires_summary_through(capsys):
    """接线锁定：build_scan_view 必须把 summary 装进 ScanView，render_terminal 打印它。

    纯函数单测覆盖规则本身，这条只覆盖「接线」——摘要参数名/字段改名会让它红。
    """
    conn = _rec_db()
    today = now_beijing().date().isoformat()
    conn.execute(
        "INSERT INTO recommendations (date, time, symbol, name, category, score, percent) VALUES (?,?,?,?,?,?,?)",
        (today, "10:00", "SZ300001", "接线甲", "momentum", 60, 1.0),
    )
    conn.execute(
        "INSERT INTO market_extra_cache (symbol, date, data_type, payload_json, updated) VALUES (?,?,?,?,?)",
        ("SZ300001", today, "fund_flow", '{"main_pct": 6.0, "main_net": 1e7}', now_beijing().isoformat()),
    )
    conn.commit()

    view = disp_mod.build_scan_view(conn, today_pool={})
    assert view is not None
    assert isinstance(view.summary, list) and view.summary, view.summary
    assert view.summary[0].startswith("强市 · "), view.summary[0]
    assert "强流入 1 · 强信号 1" in "\n".join(view.summary)

    disp_mod.render_terminal(view)
    out = capsys.readouterr().out
    assert "◆ 综合判断 — " in out
    assert view.summary[0] in out


def test_summary_renders_multiline_in_terminal(capsys):
    """终端渲染：首行挂 ◆ 标签，明细行 4 空格缩进（不去对齐全角 ◆/— 的列宽）。"""
    view = vm.ScanView(
        main_rows=[],
        breakout_mark={},
        flow_pct_map={},
        last_ranks={},
        weak=False,
        warnings=[],
        summary=["强市 · 无强信号·观望（主线 0 只无「强流入 ∧ 未追涨」）", "明细行"],
    )
    vr.render_terminal(view)
    out = capsys.readouterr().out
    assert "◆ 综合判断 — 强市 · 无强信号·观望" in out
    assert "\n    明细行\n" in out
