"""综合排序显示与资金流图标测试。"""

import sqlite3
from datetime import timedelta

import pytest
import wcwidth

import scanner.display as disp_mod
from scanner.config import now_beijing
from scanner.models import Candidate, KlineSummary, StockInfo


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
    """策略优选池+v2 池选区行（核心低吸区之前），用于测试断言。
    （动态推荐/回马枪/次日大涨规则区已移除，2026-09-03）"""
    main_part = out.split("◆ 核心方向低吸")[0]
    return [ln for ln in main_part.splitlines() if "SZ30000" in ln]


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
            (today, "13:00", "SZ300001", "有数据", "core_dip", 60, 2.0),
            (today, "13:00", "SZ300002", "无数据", "core_dip", 55, 1.0),
        ],
    )
    conn.execute(
        "INSERT INTO market_extra_cache (symbol, date, data_type, payload_json, updated) VALUES (?, ?, ?, ?, ?)",
        ("SZ300001", today, "fund_flow", '{"main_pct": 6.0, "main_net": 1e7}', now_beijing().isoformat()),
    )
    conn.commit()
    disp_mod.display_priority(conn)
    out = capsys.readouterr().out
    line1 = next(ln for ln in out.splitlines() if "SZ300001" in ln)
    line2 = next(ln for ln in out.splitlines() if "SZ300002" in ln)
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


def test_display_priority_core_dip_shown_when_main_sparse(monkeypatch, capsys):
    """核心方向低吸显示门控（阈值 COMEBACK_DISPLAY_MIN_MAIN=5，与回马枪同款）：
    显示条件 = 主区稀少（≤5）；主表 >5 条一律隐藏，无论大盘强弱。"""
    import json as _json

    def _run(main_n, idx):
        conn = _rec_db()
        for i in range(1, main_n + 1):
            _insert_rec_cat(conn, f"SZ3000{i}", f"反弹{i}", "rebound", 50)
        conn.execute(
            "INSERT INTO recommendations (date, time, symbol, name, category, score, percent, score_breakdown) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                now_beijing().date().isoformat(),
                "13:00",
                "SZ300099",
                "低吸",
                "core_dip",
                70,
                3.0,
                _json.dumps({"run": 0.2, "pullback": -0.1, "flow_pct": 5.0}),
            ),
        )
        _set_market_index(conn, idx)
        conn.commit()
        disp_mod.display_priority(conn, today_pool={})
        return capsys.readouterr().out

    # 主区 1 条（≤ 阈值 5）→ 显示 + 标准行渲染
    out = _run(1, -2.5)
    assert "◆ 核心方向低吸" in out
    dip_line = next(ln for ln in out.split("◆ 核心方向低吸", 1)[1].splitlines() if "SZ300099" in ln)
    assert "DIP" in dip_line
    # 核心低吸行尾不再附加 20日累计/回撤/主力 后缀（展示精简）
    assert "20日" not in dip_line
    assert "回撤" not in dip_line
    assert "主力" not in dip_line
    # 主区 1 条 + 强市 → 显示（只看主表数量）
    assert "◆ 核心方向低吸" in _run(1, 1.5)
    # 主区 5 条（= 阈值）→ 显示（弱市/强市一致）
    assert "◆ 核心方向低吸" in _run(5, 1.5)
    assert "◆ 核心方向低吸" in _run(5, -2.5)
    # 主区充足（6 条）> 阈值 5 → 隐藏（无论弱市/强市）
    assert "◆ 核心方向低吸" not in _run(6, 1.5)
    assert "◆ 核心方向低吸" not in _run(6, -2.5)


# ── 综合排序实时行情覆盖：live_quotes 对所有行优先（候选/非候选一致）──
def _cand_in_pool(symbol: str, pct: float, cur: float, rank: int) -> Candidate:
    k = KlineSummary(trend="", accumulated_pct=0.0, volume_ratio=1.0, bottom_confirmed=False, score=50, dimensions={})
    return Candidate(
        stock=StockInfo(
            symbol=symbol, name="测试", code=symbol[-6:], percent=pct, current=cur, value=1e8, rank_change=0, rank=rank
        ),
        category="core_dip",
        score=60,
        reason="",
        kline=k,
    )


def _insert_rec(conn, symbol: str, name: str, percent: float):
    today = now_beijing().date().isoformat()
    conn.execute(
        "INSERT INTO recommendations (date, time, symbol, name, category, score, percent) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (today, "13:00", symbol, name, "core_dip", 60, percent),
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
    line = next(ln for ln in out.splitlines() if "SZ300001" in ln)
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
    line = next(ln for ln in out.splitlines() if "SZ300002" in ln)
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
    line = next(ln for ln in out.splitlines() if "SZ300001" in ln)
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
    line = next(ln for ln in out.splitlines() if "SZ300002" in ln)
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
    line = next(ln for ln in out.splitlines() if "SZ300002" in ln)
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
    line = next(ln for ln in out.splitlines() if "SZ300791" in ln)
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
    line = next(ln for ln in out.splitlines() if "SZ300792" in ln)
    assert "+3.00%" in line
    assert "+9.50%" not in line


def test_display_priority_rank_map_for_dropped(monkeypatch, capsys):
    """掉榜/重启行（无候选）的排名由当前飙升榜 rank_map 补上（此前恒为 —）。"""
    conn = _rec_db()
    _insert_rec(conn, "SZ300002", "掉榜票", 1.0)
    disp_mod.display_priority(conn, rank_map={"SZ300002": 42}, today_pool={})
    out = capsys.readouterr().out
    line = next(ln for ln in out.splitlines() if "SZ300002" in ln)
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


def test_display_priority_rank_delta_from_last_ranks(monkeypatch, capsys):
    """综合排序「排名」列显示较上一轮扫描的雪球榜单排名变化（+N 升 / -N 降）。"""
    conn = _rec_db()
    _insert_rec_cat(conn, "SZ300001", "升名", "core_dip", 60)
    _insert_rec_cat(conn, "SZ300002", "降名", "core_dip", 55)
    _insert_rec_cat(conn, "SZ300003", "稳名", "core_dip", 50)
    pool = {
        "SZ300001": _cand_in_pool("SZ300001", 2.0, 10.0, 5),
        "SZ300002": _cand_in_pool("SZ300002", 2.0, 10.0, 8),
        "SZ300003": _cand_in_pool("SZ300003", 2.0, 10.0, 6),
    }
    # 上一轮排名：SZ300001 8→5 升 3 名；SZ300002 5→8 降 3 名；SZ300003 不变
    last_ranks = {"SZ300001": 8, "SZ300002": 5, "SZ300003": 6}
    disp_mod.display_priority(conn, today_pool=pool, last_ranks=last_ranks)
    out = capsys.readouterr().out
    lines = {sym: next(row for row in out.splitlines() if sym in row) for sym in ["SZ300001", "SZ300002", "SZ300003"]}
    assert "5+3" in lines["SZ300001"]
    assert "8-3" in lines["SZ300002"]
    assert "6" in lines["SZ300003"] and "6+" not in lines["SZ300003"] and "6-" not in lines["SZ300003"]


def test_display_priority_rank_delta_absent_by_default(monkeypatch, capsys):
    """未传 last_ranks（缺省 None）时排名列仅显示名次，不带 +N/-N（回归旧显示）。"""
    conn = _rec_db()
    _insert_rec_cat(conn, "SZ300001", "仅名次", "core_dip", 60)
    pool = {"SZ300001": _cand_in_pool("SZ300001", 2.0, 10.0, 5)}
    disp_mod.display_priority(conn, today_pool=pool)
    out = capsys.readouterr().out
    line = next(row for row in out.splitlines() if "SZ300001" in row)
    assert "5" in line and "5+" not in line and "5-" not in line


# ── 榜单 TOP40 排名高亮（2026-08-12）：名次 ≤ TOP40_THRESHOLD 加粗+红色提示 ──
def _force_ansi(monkeypatch):
    """强制 ANSI 着色开启（测试环境控制台通常无 ANSI，ANSI 码为空串会误断言）。"""
    monkeypatch.setattr(disp_mod, "_supports_ansi", True)
    monkeypatch.setattr(
        disp_mod,
        "ANSI",
        {
            "RED": "\033[91m",
            "YELLOW": "\033[93m",
            "GREEN": "\033[92m",
            "CYAN": "\033[96m",
            "MAGENTA": "\033[95m",
            "BOLD": "\033[1m",
            "RESET": "\033[0m",
        },
    )


def test_display_priority_rank_top40_highlight(monkeypatch, capsys):
    """名次在雪球榜单前 TOP40 内时排名数字加粗+红色高亮；40 名之外不高亮。"""
    _force_ansi(monkeypatch)
    conn = _rec_db()
    _insert_rec_cat(conn, "SZ300001", "榜内40", "core_dip", 60)
    _insert_rec_cat(conn, "SZ300002", "榜外41", "core_dip", 55)
    pool = {
        "SZ300001": _cand_in_pool("SZ300001", 2.0, 10.0, 40),
        "SZ300002": _cand_in_pool("SZ300002", 2.0, 10.0, 41),
    }
    disp_mod.display_priority(conn, today_pool=pool)
    out = capsys.readouterr().out
    line_in = next(row for row in out.splitlines() if "SZ300001" in row)
    line_out = next(row for row in out.splitlines() if "SZ300002" in row)
    assert disp_mod.ANSI["BOLD"] in line_in and disp_mod.ANSI["RED"] in line_in
    assert "40" in line_in
    assert disp_mod.ANSI["BOLD"] not in line_out and disp_mod.ANSI["RED"] not in line_out
    assert "41" in line_out


def test_display_priority_rank_top40_highlight_with_delta(monkeypatch, capsys):
    """高亮只作用于名次数字，不吞掉排名变化（+N/-N 保持原样跟在后面）。"""
    _force_ansi(monkeypatch)
    conn = _rec_db()
    _insert_rec_cat(conn, "SZ300001", "榜内升名", "core_dip", 60)
    pool = {"SZ300001": _cand_in_pool("SZ300001", 2.0, 10.0, 5)}
    disp_mod.display_priority(conn, today_pool=pool, last_ranks={"SZ300001": 8})
    out = capsys.readouterr().out
    line = next(row for row in out.splitlines() if "SZ300001" in row)
    assert disp_mod.ANSI["BOLD"] in line and "+3" in line


# ── 核心股名称高亮（2026-08-19）：判定 = core_themes.core_stock_symbols ──
# 核心主题成员 + 20日累计≥CORE_RUN_MIN（走强龙头）。core_dip 候选必然满足这两条，
# {core_dip} ⊆ 核心股集——高亮比低吸区更宽：可覆盖「创新高走强中的主线龙头」（江天化学
# 08-19 案例：央国企改革成员、20日+22.3%，回撤0%落不进低吸窗口）。
def test_display_priority_core_stock_name_highlight(monkeypatch, capsys):
    """综合排序/核心低吸区里属于核心股（core_stock_symbols 判定）的票名称加粗品红高亮；
    非核心股不亮。两区共用 _print_priority_row 同规则。

    回马枪区已移除（2026-09-03），低吸行改走核心方向低吸区验证同一渲染规则。
    """
    _force_ansi(monkeypatch)
    # 模拟 core_stock_symbols：SZ300001（主表）与 SZ300003（低吸区）今日为核心股
    monkeypatch.setattr(disp_mod, "core_stock_symbols", lambda conn, today=None: {"SZ300001", "SZ300003"})
    conn = _rec_db()
    _insert_rec_cat(conn, "SZ300001", "核心动量", "momentum", 70)
    _insert_rec_cat(conn, "SZ300002", "普通动量", "momentum", 65)
    _insert_rec_cat(conn, "SZ300003", "低吸核心", "core_dip", 80)
    _insert_rec_cat(conn, "SZ300004", "低吸普通", "core_dip", 70)
    disp_mod.display_priority(conn, today_pool={})
    out = capsys.readouterr().out
    # 主表行先于低吸区渲染，next() 取到的是主表/低吸区行
    lines = {
        sym: next(row for row in out.splitlines() if sym in row)
        for sym in ["SZ300001", "SZ300002", "SZ300003", "SZ300004"]
    }
    # 主表：核心动量名称高亮，普通动量不亮
    assert disp_mod.ANSI["MAGENTA"] in lines["SZ300001"]
    assert disp_mod.ANSI["BOLD"] in lines["SZ300001"]
    assert disp_mod.ANSI["MAGENTA"] not in lines["SZ300002"]
    # 低吸区：低吸核心高亮，低吸普通不亮
    assert "◆ 核心方向低吸" in out
    assert disp_mod.ANSI["MAGENTA"] in lines["SZ300003"]
    assert disp_mod.ANSI["MAGENTA"] not in lines["SZ300004"]


def test_display_priority_no_core_stock_no_highlight(monkeypatch, capsys):
    """今日无核心股（core_stock_symbols 空集）时，主表/低吸区均无高亮（空集判定不误伤）。"""
    _force_ansi(monkeypatch)
    monkeypatch.setattr(disp_mod, "core_stock_symbols", lambda conn, today=None: set())
    conn = _rec_db()
    _insert_rec_cat(conn, "SZ300001", "动量票", "momentum", 70)
    _insert_rec_cat(conn, "SZ300002", "低吸票", "core_dip", 70)
    disp_mod.display_priority(conn, today_pool={})
    out = capsys.readouterr().out
    lines = [row for row in out.splitlines() if "SZ30000" in row]
    for line in lines:
        assert disp_mod.ANSI["MAGENTA"] not in line, f"无核心股不应有高亮: {line}"


def test_display_priority_pool_shows_price_and_rank_dash(monkeypatch, capsys):
    """策略优选池渲染「现价」列；无榜单排名时显示 — 而非排序占位 9999（2026-08-28 修复）。"""
    conn = _rec_db()
    _insert_rec_pct(conn, "SZ300001", "有价票", "momentum", 70, 3.0)
    _insert_rec_pct(conn, "SZ300002", "掉榜票", "rebound", 50, 2.0)  # 无 rank
    # SZ300001 候选带 current=10.0 / rank=1（_cand_tier 默认），验证现价与排名显示
    pool = {"SZ300001": _cand_tier("SZ300001", 70, "momentum", percent=3.0)}
    disp_mod.display_priority(conn, today_pool=pool)
    out = capsys.readouterr().out
    lines = _main_lines(out)
    ln1 = next(ln for ln in lines if "SZ300001" in ln)
    assert "10.00" in ln1, "策略优选池应渲染候选现价"
    assert " 1" in ln1, "候选排名应显示"
    ln2 = next(ln for ln in lines if "SZ300002" in ln)
    assert "9999" not in ln2, "无排名不应显示排序占位 9999"
    assert "—" in ln2, "无排名应显示 —"


def test_display_header_env_tag_matches_regime(monkeypatch, capsys):
    """头部大盘标签与动态推荐/飞书同源 _regime_weak（2026-08-30 统一）：弱市显示「弱势·谨慎」。

    此前头部走 market_env_bonus、动态推荐走 _regime_weak，两套信号可能同屏矛盾。
    """
    conn = _rec_db()
    monkeypatch.setattr(disp_mod, "_regime_weak", lambda c, lookback=10: True)
    disp_mod.display(100, 60, conn=conn, today_pool={})
    out = capsys.readouterr().out
    assert "大盘弱势·谨慎" in out
    monkeypatch.setattr(disp_mod, "_regime_weak", lambda c, lookback=10: False)
    disp_mod.display(100, 60, conn=conn, today_pool={})
    out = capsys.readouterr().out
    assert "大盘强势" in out
    """优选池行尾渲染 🎯（2026-08-30 主视图标记恢复）：甜蜜带+非超买+累计达门槛的票在主列表可见。

    此前 🎯/⚡ 仅在回马枪/低吸区渲染，换成策略优选池后主视图丢失画像信息。"""
    conn = _rec_db()
    _insert_rec_pct(conn, "SZ300001", "甜蜜动量", "momentum", 70, 1.0)
    pool = {"SZ300001": _cand_tier("SZ300001", 70, "momentum", percent=1.0, accum=8.0)}
    disp_mod.display_priority(conn, today_pool=pool)
    out = capsys.readouterr().out
    line = next(ln for ln in _main_lines(out) if "SZ300001" in ln)
    assert "🎯" in line, f"优选池行应渲染 🎯 标记: {line}"


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
    pct, cur = disp_mod._entry_display_quote(live0)
    assert pct == 0.0
    assert cur == 10.0
    # ② 掉榜行（无候选无 live）：落库 percent，现价无数据
    dropped = {"percent": 5.0, "live_percent": None, "_candidate": None}
    pct, cur = disp_mod._entry_display_quote(dropped)
    assert pct == 5.0
    assert cur == 0.0
    # ③ 可信候选快照：候选 percent/current 生效
    cand = _cand_tier("SZ300099", 70, "momentum", percent=2.5)
    cand_entry = {"percent": 1.0, "_candidate": cand}
    pct, cur = disp_mod._entry_display_quote(cand_entry)
    assert pct == pytest.approx(2.5)
    assert cur == pytest.approx(10.0)


def test_display_priority_recommended_region_shown_when_main_dense(monkeypatch, capsys):
    """弱市 regime 下主区密集也强制展示核心低吸区（修复「推荐了却看不到标的」割裂）。
    （动态推荐/回马枪区已移除，2026-09-03；核心低吸区保留弱市强制展示门）"""
    monkeypatch.setattr(disp_mod, "_regime_weak", lambda conn, lookback=10: True)
    conn = _rec_db()
    for i in range(1, 7):
        _insert_rec_cat(conn, f"SZ3000{i}", f"反弹{i}", "rebound", 50 + i)
    import json as _json

    conn.execute(
        "INSERT INTO recommendations (date, time, symbol, name, category, score, percent, score_breakdown) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            now_beijing().date().isoformat(),
            "13:00",
            "SZ300099",
            "低吸",
            "core_dip",
            70,
            3.0,
            _json.dumps({"run": 0.2, "pullback": -0.1, "flow_pct": 5.0}),
        ),
    )
    conn.commit()
    disp_mod.display_priority(conn, today_pool={})
    out = capsys.readouterr().out
    assert "◆ 核心方向低吸" in out, "弱市 regime 下主区密集也应展示核心低吸区"


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
    # percent 默认 3.0（2~4% 死区，不在次日大涨甜蜜带）：避免候选行被 🎯 误置顶，
    # 使档位测试只由 prominent 决定；需要 🎯 的测试显式传甜蜜带（<2% / 4~8%）。
    # accum 默认 8.0 ≥ NEXTDAY_ACCUM_MIN（6.0）：甜蜜带 momentum/new_face 票默认可标 🎯，
    # 保持既有档位测试语义；测试 5 日累计门槛时显式传低值（如 accum=2.0）。
    # incl_accum：accumulated_incl_today 维度（含今日口径，2026-08-17 起 🎯 优先取用它）。
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
    """档位分隔横幅下线后，净流出票不再劣后过滤（2026-08-11）；排序改按涨幅升序优先
    （2026-08-28 规则：榜上优先 → 涨幅升序 → 回调核心 → 排名升序 → 新面孔，档位不再
    参与主排序）。双跑同屏后主表恒为 v1 五桶口径（2026-09-02）。"""
    conn = _rec_db()
    # 主排序键的 chg 取 DB percent（候选池 percent 不参与排序键，见 build_scan_view），
    # 故此处用 DB 列驱动不同涨幅，验证「涨幅升序」优先。
    _insert_rec_pct(conn, "SZ300001", "置顶", "rebound", 50, 1.0)  # 低涨幅
    _insert_rec_pct(conn, "SZ300002", "普通", "momentum", 70, 3.0)  # 高涨幅
    _insert_rec_sb(conn, "SZ300003", "流出", "rebound", 90, 2.0, '{"fund_flow_main_pct": -6.0}')  # 净流出+中涨幅
    disp_mod.display_priority(conn, today_pool={})
    out = capsys.readouterr().out
    assert "▶ 置顶档" not in out
    assert "▶ 普通档" not in out
    lines = _main_lines(out)
    assert len(lines) == 3, f"净流出票(SZ300003)应正常展示，不再被劣后过滤: {lines}"

    # 涨幅升序优先：1.0% → 2.0% → 3.0%（档位不再决定顺序）
    def _idx(sym: str) -> int:
        return next(i for i, ln in enumerate(lines) if sym in ln)

    assert _idx("SZ300001") == 0, f"最低涨幅(1.0%)应排最前: {lines}"
    assert _idx("SZ300003") == 1, f"中涨幅(2.0%)应居中: {lines}"
    assert _idx("SZ300002") == 2, f"高涨幅(3.0%)应排最后: {lines}"


def test_display_priority_pool_pick_independent_section_sorted(capsys):
    """双跑同屏（2026-09-02）：pool_pick 独立成 v2 池选区、按今日涨幅降序；
    主表只含 v1 五桶（pool_pick 不混入主表），两区同屏输出，RTS_PIPELINE 不再影响显示。"""
    conn = _rec_db()
    _insert_rec_pct(conn, "SZ300001", "v1票", "rebound", 50, 1.0)
    _insert_rec_pct(conn, "SZ300002", "池高", "pool_pick", 70, 3.0)
    _insert_rec_pct(conn, "SZ300003", "池中", "pool_pick", 90, 2.0)
    _insert_rec_pct(conn, "SZ300004", "池低", "pool_pick", 40, 0.5)
    disp_mod.display_priority(conn, today_pool={})
    out = capsys.readouterr().out
    assert "◆ v2 池选" in out, "双跑同屏：pool_pick 必须有独立 v2 池选区"

    main_part = out.split("v2 池选")[0]
    pool_part = out.split("v2 池选")[1]
    main_syms = [ln for ln in main_part.splitlines() if "SZ30000" in ln]
    pool_syms = [ln for ln in pool_part.splitlines() if "SZ30000" in ln]

    assert any("SZ300001" in ln for ln in main_syms), f"v1 行应留在主表: {main_syms}"
    assert not any("SZ300002" in ln or "SZ300003" in ln or "SZ300004" in ln for ln in main_syms), (
        f"pool_pick 不应混入主表: {main_syms}"
    )
    assert len(pool_syms) == 3, f"池选区应显示 3 只 pool_pick: {pool_syms}"

    def _idx(sym: str) -> int:
        return next(i for i, ln in enumerate(pool_syms) if sym in ln)

    assert _idx("SZ300002") < _idx("SZ300003") < _idx("SZ300004"), f"池选区按涨幅降序(3.0→2.0→0.5): {pool_syms}"


def test_display_priority_pool_pick_kept_out_of_main_even_higher_pct(capsys):
    """双跑同屏：pool_pick 涨幅再高（9.9%）也不进策略优选池主表，只在 v2 池选区展示。"""
    conn = _rec_db()
    _insert_rec_pct(conn, "SZ300001", "v1票", "rebound", 50, 1.0)
    _insert_rec_pct(conn, "SZ300002", "池选票", "pool_pick", 70, 9.9)
    disp_mod.display_priority(conn, today_pool={})
    out = capsys.readouterr().out
    main_part = out.split("v2 池选")[0]
    main_syms = [ln for ln in main_part.splitlines() if "SZ30000" in ln]
    assert len(main_syms) == 1 and "SZ300001" in main_syms[0], f"主表只应显示 v1 rebound: {main_syms}"
    assert "SZ300002" in out and "◆ v2 池选" in out, "pool_pick 应在 v2 池选区展示"


def test_display_priority_pool_pick_dip_label_segment_sorted(capsys):
    """方案B（2026-09-03）：v2 池选两段式排序——命中低吸标签的票排前段
    （段内榜上排名/涨幅），行尾 💡 标签渲染使排序可解释。"""
    conn = _rec_db()
    _insert_rec_sb(conn, "SZ300001", "无标签", "pool_pick", 70, 9.9, "{}")  # 高涨幅无标签
    _insert_rec_sb(conn, "SZ300002", "有标签", "pool_pick", 70, 1.0, '{"dip_labels": ["弱转强"]}')  # 低涨幅有标签
    disp_mod.display_priority(conn, today_pool={})
    out = capsys.readouterr().out
    assert "◆ v2 池选" in out
    assert "💡弱转强" in out, "低吸标签应行尾渲染（排序可解释）"
    pool_part = out.split("v2 池选")[1]
    pool_syms = [ln for ln in pool_part.splitlines() if "SZ30000" in ln]
    assert "SZ300002" in pool_syms[0], f"有标签票应排前段（与涨幅无关）: {pool_syms}"
    assert "SZ300001" in pool_syms[1], f"无标签票应沉后段: {pool_syms}"


def test_display_priority_core_dip_capped(monkeypatch, capsys):
    """核心低吸区最多显示 COMEBACK_DISPLAY_MAX 条（超量截断，避免刷屏）。
    （原回马枪区截断测试，区块移除后改测核心低吸区，2026-09-03）"""
    conn = _rec_db()
    for i in range(12):
        _insert_rec_cat(conn, f"SZ3003{i:02d}", f"低吸{i}", "core_dip", 50 + i)
    disp_mod.display_priority(conn, today_pool={})
    out = capsys.readouterr().out
    assert "◆ 核心方向低吸" in out
    dip_part = out.split("◆ 核心方向低吸", 1)[1]
    dip_lines = [ln for ln in dip_part.splitlines() if "SZ3003" in ln]
    assert len(dip_lines) == disp_mod.COMEBACK_DISPLAY_MAX


# ── 次日大涨画像标记（2026-08-11 起并入主表行尾 🎯；2026-08-12 起成为排序档0唯一因子）──
# 原独立区（2026-08-10）与主表重合度 65%（主表 17 只中 11 只甜蜜带、两表排序几乎一致、
# 辨识度因子空转），重复输出；改为主表行尾标记。筛形条件不变（nextday_attribution 口径）：
# 推荐时刻涨幅甜蜜带（<2% 低吸潜伏 / 4~8% 中段启动）且非超买死亡信号。
# 2026-08-12：🎯 从纯视觉标记升级为排序档0唯一因子——辨识度退出排序（次日大涨本身即
# 辨识度属性），↻ 仅保留行内展示。
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
    _insert_rec_pct(conn, "SZ300001", "陷阱票", "momentum", 70, 9.0)  # 8-10% 陷阱带
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
    # 涨幅升序优先：SZ300004(1.0%) 最低涨幅排最前（🎯 画像仍成立）
    assert "SZ300004" in lines[0], f"最低涨幅(1.0%)应排最前: {lines}"
    # 档位不再排序：四只票全部正常展示（档3 小板块共振不再被劣后到末尾）
    assert len(lines) == 4, f"档位不应再过滤/劣后排序: {lines}"
    assert any("SZ300002" in ln for ln in lines), f"小板块共振(档3)仍应展示: {lines}"


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

    构造：short_term 非首推推荐 + 前 ≥21 根缩量回调 K 线（肯特股份形态）。"""
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
    assert "⚡" in out and "⚡R" not in out, "命中时合并图例应打印单个 ⚡（两变体不区分渲染）"
    assert "蓄势突破观察" in out, "命中时表尾应打印合并图例"


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
