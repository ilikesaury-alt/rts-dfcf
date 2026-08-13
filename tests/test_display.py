"""综合排序显示与资金流图标测试。"""
import sqlite3
from datetime import timedelta

import scanner.display as disp_mod
from scanner.config import now_beijing
from scanner.models import Candidate, KlineSummary, StockInfo


# ── 资金流强弱档位（5 档图标规则，2026-08-06）──
def _candidate(pct):
    k = KlineSummary(trend="", accumulated_pct=0.0, volume_ratio=1.0,
                     bottom_confirmed=False, score=50,
                     dimensions={} if pct is None else {"fund_flow_main_pct": pct})
    return Candidate(
        stock=StockInfo(symbol="SZ300001", name="测试", code="300001", percent=1.0,
                        current=10.0, value=1e8, rank_change=0, rank=1),
        category="new_face", score=50, reason="", kline=k)


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
    c.kline.dimensions["zt_lianban"] = 2
    c.kline.dimensions["zt_zhaban"] = 1
    s = disp_mod._market_extra_str(c)
    assert "▲" in s
    assert "连2炸1" in s


# ── 综合排序资金流图标：DB 快照回退（重启/掉榜后仍显示）──
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
    return conn


def test_display_priority_fund_flow_icon_from_db(capsys):
    """综合排序在候选池缺失（重启/掉榜）时，从 market_extra_cache 读取资金流图标。"""
    conn = _rec_db()
    today = now_beijing().date().isoformat()
    conn.executemany(
        "INSERT INTO recommendations (date, time, symbol, name, category, score, percent) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(today, "13:00", "SZ300001", "有数据", "momentum", 60, 2.0),
         (today, "13:00", "SZ300002", "无数据", "momentum", 55, 1.0)],
    )
    conn.execute(
        "INSERT INTO market_extra_cache (symbol, date, data_type, payload_json, updated) "
        "VALUES (?, ?, ?, ?, ?)",
        ("SZ300001", today, "fund_flow", '{"main_pct": 6.0, "main_net": 1e7}', now_beijing().isoformat()),
    )
    conn.commit()
    disp_mod.display_priority(conn)
    out = capsys.readouterr().out
    line1 = next(l for l in out.splitlines() if "SZ300001" in l)
    line2 = next(l for l in out.splitlines() if "SZ300002" in l)
    assert "▲" in line1
    assert "▲" not in line2


# ── 回马枪显示（2026-08-07）──
def _comeback_candidate(name: str, symbol: str, variant: str, score: int = 70) -> Candidate:
    k = KlineSummary(trend=f"{variant}·超跌企稳", accumulated_pct=-12.0,
                     volume_ratio=1.5, bottom_confirmed=False, score=score,
                     dimensions={"comeback_variant": variant})
    return Candidate(
        stock=StockInfo(symbol=symbol, name=name, code=symbol[-6:], percent=3.0,
                        current=12.0, value=1e8, rank_change=0, rank=0),
        category="comeback", score=score, reason=k.trend, kline=k,
        off_list=True, comeback_variant=variant)


def test_display_comeback_section(monkeypatch, capsys):
    """策略桶下线（2026-08-10）后回马枪改走综合排序独立区，变体（反转/回踩）随标签展示。
    2026-08-12：主区推荐条数 < COMEBACK_DISPLAY_MIN_MAIN（此处为空）时兜底展示，
    仅显示前 COMEBACK_DISPLAY_MAX 条。"""
    conn = _rec_db()
    _insert_rec_cat(conn, "SZ300986", "志特新材", "comeback", 70)
    _insert_rec_cat(conn, "SZ300111", "回踩股", "comeback", 55)
    # 变体来源：候选 kline.dimensions（DB-only 行走 trend 前缀）
    pool = {
        "SZ300986": _comeback_candidate("志特新材", "SZ300986", "反转", 70),
        "SZ300111": _comeback_candidate("回踩股", "SZ300111", "回踩", 55),
    }
    disp_mod.display_priority(conn, today_pool=pool)
    out = capsys.readouterr().out
    assert "◆ 回马枪" in out
    assert "补充参考" in out
    line_rt = next(l for l in out.splitlines() if "SZ300986" in l)
    line_re = next(l for l in out.splitlines() if "SZ300111" in l)
    assert "CB" in line_rt
    assert "回马" in line_rt        # SUGGEST_BY_CAT['comeback']
    assert "反转" in line_rt
    assert "回踩" in line_re


def test_display_priority_comeback_hidden_when_main_has_recs(monkeypatch, capsys):
    """2026-08-12：主区（榜上五类）推荐条数 ≥ COMEBACK_DISPLAY_MIN_MAIN 时不显示
    回马枪独立区（避免刷屏）；comeback 行随区块整体隐藏。"""
    conn = _rec_db()
    # 主区 6 条（≥ 阈值 3）→ 隐藏回马枪
    for i in range(1, 7):
        _insert_rec_cat(conn, f"SZ3000{i}", f"反弹{i}", "rebound", 50 + i)
    _insert_rec_cat(conn, "SZ300007", "回马", "comeback", 90)
    disp_mod.display_priority(conn, today_pool={})
    out = capsys.readouterr().out
    assert "◆ 回马枪" not in out
    assert "SZ300007" not in out         # comeback 行随区块整体隐藏


def test_display_priority_comeback_shown_when_main_scarce(monkeypatch, capsys):
    """2026-08-12：主区推荐条数 < COMEBACK_DISPLAY_MIN_MAIN（如盘中仅 1-2 条）时
    也显示回马枪独立区，避免主区稀少时回马枪条目被整体隐藏。"""
    conn = _rec_db()
    _insert_rec_cat(conn, "SZ300001", "反弹", "rebound", 50)
    _insert_rec_cat(conn, "SZ300002", "回马", "comeback", 90)
    _insert_rec_cat(conn, "SZ300003", "超短", "short_term", 80)
    disp_mod.display_priority(conn, today_pool={})
    out = capsys.readouterr().out
    assert "◆ 回马枪" in out
    cb_part = out.split("◆ 回马枪", 1)[1]
    assert "SZ300002" in cb_part      # comeback 行在回马枪区展示
    # 主区两行仍在主表
    main_part = out.split("◆ 回马枪", 1)[0]
    assert "SZ300001" in main_part
    assert "SZ300003" in main_part


# ── 综合排序实时行情覆盖：live_quotes 对所有行优先（候选/非候选一致）──
def _cand_in_pool(symbol: str, pct: float, cur: float, rank: int) -> Candidate:
    k = KlineSummary(trend="", accumulated_pct=0.0, volume_ratio=1.0,
                     bottom_confirmed=False, score=50, dimensions={})
    return Candidate(
        stock=StockInfo(symbol=symbol, name="测试", code=symbol[-6:], percent=pct,
                        current=cur, value=1e8, rank_change=0, rank=rank),
        category="momentum", score=60, reason="", kline=k)


def _insert_rec(conn, symbol: str, name: str, percent: float):
    today = now_beijing().date().isoformat()
    conn.execute(
        "INSERT INTO recommendations (date, time, symbol, name, category, score, percent) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (today, "13:00", symbol, name, "momentum", 60, percent),
    )
    conn.commit()


def test_display_priority_live_quotes_overrides_candidate(monkeypatch, capsys):
    """候选行也优先使用 live_quotes 实时行情（此前仅无候选行生效）。"""
    conn = _rec_db()
    _insert_rec(conn, "SZ300001", "候选票", 2.0)
    cand = _cand_in_pool("SZ300001", 1.5, 10.0, 5)
    disp_mod.display_priority(conn, live_quotes={"SZ300001": {"percent": 3.2, "current": 10.5}}, today_pool={"SZ300001": cand})
    out = capsys.readouterr().out
    line = next(l for l in out.splitlines() if "SZ300001" in l)
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
    line = next(l for l in out.splitlines() if "SZ300002" in l)
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
    line = next(l for l in out.splitlines() if "SZ300001" in l)
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
    line = next(l for l in out.splitlines() if "SZ300002" in l)
    assert "+0.00%" in line
    assert "+2.00%" not in line


def test_display_priority_rank_map_for_dropped(monkeypatch, capsys):
    """掉榜/重启行（无候选）的排名由当前飙升榜 rank_map 补上（此前恒为 —）。"""
    conn = _rec_db()
    _insert_rec(conn, "SZ300002", "掉榜票", 1.0)
    disp_mod.display_priority(conn, rank_map={"SZ300002": 42}, today_pool={})
    out = capsys.readouterr().out
    line = next(l for l in out.splitlines() if "SZ300002" in l)
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
    _insert_rec_cat(conn, "SZ300001", "升名", "momentum", 60)
    _insert_rec_cat(conn, "SZ300002", "降名", "momentum", 55)
    _insert_rec_cat(conn, "SZ300003", "稳名", "momentum", 50)
    pool = {
        "SZ300001": _cand_in_pool("SZ300001", 2.0, 10.0, 5),
        "SZ300002": _cand_in_pool("SZ300002", 2.0, 10.0, 8),
        "SZ300003": _cand_in_pool("SZ300003", 2.0, 10.0, 6),
    }
    # 上一轮排名：SZ300001 8→5 升 3 名；SZ300002 5→8 降 3 名；SZ300003 不变
    last_ranks = {"SZ300001": 8, "SZ300002": 5, "SZ300003": 6}
    disp_mod.display_priority(conn, today_pool=pool, last_ranks=last_ranks)
    out = capsys.readouterr().out
    lines = {sym: next(row for row in out.splitlines() if sym in row)
             for sym in ["SZ300001", "SZ300002", "SZ300003"]}
    assert "5+3" in lines["SZ300001"]
    assert "8-3" in lines["SZ300002"]
    assert "6" in lines["SZ300003"] and "6+" not in lines["SZ300003"] and "6-" not in lines["SZ300003"]


def test_display_priority_rank_delta_absent_by_default(monkeypatch, capsys):
    """未传 last_ranks（缺省 None）时排名列仅显示名次，不带 +N/-N（回归旧显示）。"""
    conn = _rec_db()
    _insert_rec_cat(conn, "SZ300001", "仅名次", "momentum", 60)
    pool = {"SZ300001": _cand_in_pool("SZ300001", 2.0, 10.0, 5)}
    disp_mod.display_priority(conn, today_pool=pool)
    out = capsys.readouterr().out
    line = next(row for row in out.splitlines() if "SZ300001" in row)
    assert "5" in line and "5+" not in line and "5-" not in line


# ── 榜单 TOP40 排名高亮（2026-08-12）：名次 ≤ TOP40_THRESHOLD 加粗+红色提示 ──
def _force_ansi(monkeypatch):
    """强制 ANSI 着色开启（测试环境控制台通常无 ANSI，ANSI 码为空串会误断言）。"""
    monkeypatch.setattr(disp_mod, "_supports_ansi", True)
    monkeypatch.setattr(disp_mod, "ANSI", {
        "RED": "\033[91m", "YELLOW": "\033[93m", "GREEN": "\033[92m",
        "CYAN": "\033[96m", "BOLD": "\033[1m", "RESET": "\033[0m",
    })


def test_display_priority_rank_top40_highlight(monkeypatch, capsys):
    """名次在雪球榜单前 TOP40 内时排名数字加粗+红色高亮；40 名之外不高亮。"""
    _force_ansi(monkeypatch)
    conn = _rec_db()
    _insert_rec_cat(conn, "SZ300001", "榜内40", "momentum", 60)
    _insert_rec_cat(conn, "SZ300002", "榜外41", "momentum", 55)
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
    _insert_rec_cat(conn, "SZ300001", "榜内升名", "momentum", 60)
    pool = {"SZ300001": _cand_in_pool("SZ300001", 2.0, 10.0, 5)}
    disp_mod.display_priority(conn, today_pool=pool, last_ranks={"SZ300001": 8})
    out = capsys.readouterr().out
    line = next(row for row in out.splitlines() if "SZ300001" in row)
    assert disp_mod.ANSI["BOLD"] in line and "+3" in line


# ── 综合排序分组顺序（2026-08-07 复核：rebound > short_term > momentum > known_new_face > new_face > pullback）──
def _insert_rec_cat(conn, symbol: str, name: str, category: str, score: int):
    today = now_beijing().date().isoformat()
    conn.execute(
        "INSERT INTO recommendations (date, time, symbol, name, category, score, percent) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (today, "13:00", symbol, name, category, score, 3.0),  # 3.0=死区，避免掉榜行被 🎯 置顶
    )
    conn.commit()


def test_display_priority_new_group_order(monkeypatch, capsys):
    """综合排序按 CAT_DISPLAY_PRIORITY 分组：即使 kNF 分数最高也排到 rebound/short_term 之后。"""
    conn = _rec_db()
    _insert_rec_cat(conn, "SZ300001", "新面孔", "new_face", 80)
    _insert_rec_cat(conn, "SZ300002", "已知新", "known_new_face", 90)
    _insert_rec_cat(conn, "SZ300003", "反弹", "rebound", 50)
    _insert_rec_cat(conn, "SZ300004", "动量", "momentum", 70)
    _insert_rec_cat(conn, "SZ300005", "超短", "short_term", 60)
    disp_mod.display_priority(conn, today_pool={})
    out = capsys.readouterr().out
    # 2026-08-11：次日大涨独立区已并入主表行尾 🎯 标记，无重复区块，直接取全部输出
    main_out = out
    lines = [l for l in main_out.splitlines() if "SZ30000" in l]
    assert len(lines) == 5
    order = ["SZ300003", "SZ300005", "SZ300004", "SZ300002", "SZ300001"]
    for i, sym in enumerate(order):
        assert sym in lines[i], f"{sym} 应在第 {i} 行，实际顺序: {lines}"


def test_display_priority_knf_score_ascending(monkeypatch, capsys):
    """kNF 分数反指（低分档收益更好）：分区内按 score 升序，低分票排前；不跨类别影响降序。"""
    conn = _rec_db()
    _insert_rec_cat(conn, "SZ300001", "已知新低分", "known_new_face", 20)
    _insert_rec_cat(conn, "SZ300002", "已知新高分", "known_new_face", 90)
    _insert_rec_cat(conn, "SZ300003", "动量高分", "momentum", 90)
    _insert_rec_cat(conn, "SZ300004", "动量低分", "momentum", 30)
    disp_mod.display_priority(conn, today_pool={})
    out = capsys.readouterr().out
    lines = [l for l in out.splitlines() if "SZ30000" in l]
    # 类别组内：kNF 低分(20) 在 高分(90) 前；momentum 高分(90) 在 低分(30) 前（仍降序）
    assert lines.index(next(l for l in lines if "SZ300001" in l)) < \
        lines.index(next(l for l in lines if "SZ300002" in l)), "kNF 应升序（低分在前）"
    assert lines.index(next(l for l in lines if "SZ300003" in l)) < \
        lines.index(next(l for l in lines if "SZ300004" in l)), "momentum 应降序（高分在前）"


def test_display_priority_suggestion_decoupled(monkeypatch, capsys):
    """建议列与优先级解耦：new_face 位次垫底仍显示「参考」，kNF/short_term 显示「超短」，rebound 显示「推荐」。"""
    conn = _rec_db()
    _insert_rec_cat(conn, "SZ300001", "新面孔", "new_face", 80)
    _insert_rec_cat(conn, "SZ300002", "已知新", "known_new_face", 90)
    _insert_rec_cat(conn, "SZ300003", "反弹", "rebound", 50)
    _insert_rec_cat(conn, "SZ300004", "超短", "short_term", 60)
    disp_mod.display_priority(conn, today_pool={})
    out = capsys.readouterr().out
    lines = {sym: next(l for l in out.splitlines() if sym in l)
             for sym in ["SZ300001", "SZ300002", "SZ300003", "SZ300004"]}
    assert "超短" in lines["SZ300002"]  # known_new_face 次日卖
    assert "推荐" in lines["SZ300003"]  # rebound
    assert "参考" in lines["SZ300001"]  # new_face 位次虽低仍参考，非回避
    assert "超短" in lines["SZ300004"]  # short_term
    assert "回避" not in lines["SZ300001"]


# ── 综合排序档位置顶（2026-08-06 引入）：排序键 (档位, 类别优先级, 分数键) ──
# 档0置前 = 辨识度(↻)；档1 = 其余。2026-08-11 起资金流不再参与档位排序/劣后过滤
# （图标与「资金流出」标签仍保留展示）。_cand_tier 的 fund_flow 参数供图标相关测试使用。
def _cand_tier(symbol: str, score: int, category: str = "momentum",
               fund_flow: float | None = None, prominent: bool = False,
               percent: float = 3.0) -> Candidate:
    # percent 默认 3.0（2~4% 死区，不在次日大涨甜蜜带）：避免候选行被 🎯 误置顶，
    # 使档位测试只由 prominent 决定；需要 🎯 的测试显式传甜蜜带（<2% / 4~8%）。
    dims = {}
    if fund_flow is not None:
        dims["fund_flow_main_pct"] = fund_flow
    k = KlineSummary(trend="", accumulated_pct=0.0, volume_ratio=1.0,
                     bottom_confirmed=False, score=score, dimensions=dims)
    c = Candidate(
        stock=StockInfo(symbol=symbol, name="测试", code=symbol[-6:], percent=percent,
                        current=10.0, value=1e8, rank_change=0, rank=1),
        category=category, score=score, reason="", kline=k)
    if prominent:
        c.prominence_labels.append("↻")
    return c


def test_display_priority_tier_front_cross_category(monkeypatch, capsys):
    """跨类别置顶：🎯 置前票(档0)排在 MOM 普通高分票(档1)之前；档0 内仍按类别优先级+评分。"""
    conn = _rec_db()
    _insert_rec_cat(conn, "SZ300002", "动量置前", "momentum", 80)
    _insert_rec_cat(conn, "SZ300003", "超短置前", "short_term", 90)
    _insert_rec_cat(conn, "SZ300004", "普通动量", "momentum", 125)
    pool = {"SZ300002": _cand_tier("SZ300002", 80, percent=1.0),
            "SZ300003": _cand_tier("SZ300003", 90, category="short_term", percent=1.0),
            "SZ300004": _cand_tier("SZ300004", 125)}
    disp_mod.display_priority(conn, today_pool=pool)
    out = capsys.readouterr().out
    main_out = out
    lines = [l for l in main_out.splitlines() if "SZ30000" in l]
    assert len(lines) == 3
    # 档0 内按 CAT_DISPLAY_PRIORITY：short_term(1) < momentum(2)，超短置前在前
    order = ["SZ300003", "SZ300002", "SZ300004"]
    for i, sym in enumerate(order):
        assert sym in lines[i], f"{sym} 应在第 {i} 行，实际: {lines}"


def test_display_priority_tier_front_within_category(monkeypatch, capsys):
    """同类别内：低分甜蜜带票(档0)排在普通高分票(档1)之前，不靠分数翻盘。"""
    conn = _rec_db()
    _insert_rec(conn, "SZ300001", "置前票", 1.0)   # momentum score 60
    _insert_rec_cat(conn, "SZ300002", "高分普通票", "momentum", 150)
    pool = {"SZ300001": _cand_tier("SZ300001", 60, percent=1.0),
            "SZ300002": _cand_tier("SZ300002", 150)}
    disp_mod.display_priority(conn, today_pool=pool)
    out = capsys.readouterr().out
    lines = [l for l in out.splitlines() if "SZ30000" in l]
    assert "SZ300001" in lines[0], f"甜蜜带票(60)应排在普通高分票(150)之前: {lines}"
    assert "SZ300002" in lines[1]


def test_prominence_no_longer_sorts(monkeypatch, capsys):
    """2026-08-12: 辨识度不再参与排序——只有辨识度(↻)、无甜蜜带的票按正常分数排序（不置顶）。"""
    conn = _rec_db()
    _insert_rec_cat(conn, "SZ300001", "辨识票", "momentum", 60)
    _insert_rec_cat(conn, "SZ300002", "高分普通票", "momentum", 150)
    pool = {"SZ300001": _cand_tier("SZ300001", 60, prominent=True),
            "SZ300002": _cand_tier("SZ300002", 150)}
    disp_mod.display_priority(conn, today_pool=pool)
    out = capsys.readouterr().out
    lines = [l for l in out.splitlines() if "SZ30000" in l]
    assert "SZ300002" in lines[0], f"辨识度票(60)不再置顶，高分票(150)应在前: {lines}"
    assert "SZ300001" in lines[1]
    assert "↻" in lines[1], "辨识度标记应保留行内展示"


def test_display_priority_fund_flow_no_longer_sorts(monkeypatch, capsys):
    """资金流不再参与排序（2026-08-11）：净流入≥5% 的低分票不再因资金流置前，
    按正常档位（无辨识度=档1）+类别+分数排序。"""
    conn = _rec_db()
    _insert_rec(conn, "SZ300001", "流入票", 1.0)
    _insert_rec_cat(conn, "SZ300002", "高分普通票", "momentum", 65)
    pool = {"SZ300001": _cand_tier("SZ300001", 55, fund_flow=6.0),
            "SZ300002": _cand_tier("SZ300002", 65)}
    disp_mod.display_priority(conn, today_pool=pool)
    out = capsys.readouterr().out
    lines = [l for l in out.splitlines() if "SZ30000" in l]
    assert "SZ300002" in lines[0], f"高分普通票(65)应排在强流入票(55)之前(资金流不再置前): {lines}"
    assert "SZ300001" in lines[1]


def test_display_priority_fund_flow_outflow_not_hidden(monkeypatch, capsys):
    """资金流不再劣后（2026-08-11）：主力净流出≤-5% 的票不再被过滤出综合排序，
    正常展示；🎯 置前票仍置前。"""
    conn = _rec_db()
    _insert_rec(conn, "SZ300001", "流出票", 1.0)  # momentum score 60
    _insert_rec_cat(conn, "SZ300002", "普通票", "momentum", 50)
    pool = {"SZ300001": _cand_tier("SZ300001", 100, fund_flow=-6.0, percent=1.0),
            "SZ300002": _cand_tier("SZ300002", 50)}
    disp_mod.display_priority(conn, today_pool=pool)
    out = capsys.readouterr().out
    main_out = out
    lines = [l for l in main_out.splitlines() if "SZ30000" in l]
    assert len(lines) == 2, f"净流出票不应被过滤，两条都展示: {lines}"
    assert "SZ300001" in lines[0], f"🎯 置前票应置前: {lines}"
    assert "SZ300002" in lines[1]


def test_display_priority_tier_db_source_for_dropped(monkeypatch, capsys):
    """掉榜行（无候选）统一分档：次日大涨画像从 DB 落库 percent 现算；
    辨识度不再参与排序（仅保留 ↻ 行内展示）。"""
    conn = _rec_db()
    today = now_beijing().date()
    for i in range(5):
        d = (today - timedelta(days=i)).isoformat()
        conn.execute(
            "INSERT INTO appearances (symbol, name, date, rank) VALUES (?, ?, ?, ?)",
            ("SZ300001", "辨识票", d, 40 + i),
        )
    _insert_rec(conn, "SZ300001", "辨识票", 1.0)  # momentum score 60（甜蜜带 → 🎯 档0）
    _insert_rec_cat(conn, "SZ300002", "普通票", "momentum", 90)
    conn.execute(
        "INSERT INTO market_extra_cache (symbol, date, data_type, payload_json, updated) "
        "VALUES (?, ?, ?, ?, ?)",
        ("SZ300001", today.isoformat(), "fund_flow", '{"main_pct": 6.0, "main_net": 1e7}',
         now_beijing().isoformat()),
    )
    conn.commit()
    disp_mod.display_priority(conn, today_pool={})
    out = capsys.readouterr().out
    lines = [l for l in out.splitlines() if "SZ30000" in l]
    assert "SZ300001" in lines[0], f"掉榜甜蜜带票(60,档0)应排在普通票(90,档1)之前: {lines}"
    assert "SZ300002" in lines[1]


def test_display_priority_tier_banner_separates_groups(monkeypatch, capsys):
    """档位分隔横幅下线后，辨识度档仍排前；净流出票不再劣后过滤（2026-08-11）。"""
    conn = _rec_db()
    _insert_rec_cat(conn, "SZ300001", "置顶", "rebound", 50)
    _insert_rec_cat(conn, "SZ300002", "普通", "momentum", 70)
    _insert_rec_cat(conn, "SZ300003", "流出", "rebound", 90)
    pool = {
        "SZ300001": _cand_tier("SZ300001", 50, "rebound", percent=1.0),
        "SZ300002": _cand_tier("SZ300002", 70, "momentum"),
        "SZ300003": _cand_tier("SZ300003", 90, "rebound", fund_flow=-6.0, percent=1.0),
    }
    disp_mod.display_priority(conn, today_pool=pool)
    out = capsys.readouterr().out
    assert "▶ 置顶档" not in out
    assert "▶ 普通档" not in out
    main_out = out
    lines = [l for l in main_out.splitlines() if "SZ30000" in l]
    assert len(lines) == 3, f"净流出票(SZ300003)应正常展示，不再被劣后过滤: {lines}"
    # 档0（🎯）内按类别优先级+分数降序：SZ300003(rebound,90) 与 SZ300001(rebound,50) 同档，高分在前
    assert "SZ300003" in lines[0], f"🎯 档高分票应在前: {lines}"
    assert "SZ300001" in lines[1]
    assert "SZ300002" in lines[2]


def test_display_priority_comeback_separate_region(monkeypatch, capsys):
    """方案A：回马枪独立成区（主区无推荐时兜底），按分数降序；独立区净流出票正常展示。
    2026-08-12：辨识度不再参与排序（comeback 不在次日大涨画像范围，恒档1）。"""
    conn = _rec_db()
    _insert_rec_cat(conn, "SZ300101", "马低分", "comeback", 55)
    _insert_rec_cat(conn, "SZ300102", "马高分", "comeback", 120)
    pool = {
        "SZ300101": _cand_tier("SZ300101", 55, "comeback", prominent=True),
        "SZ300102": _cand_tier("SZ300102", 120, "comeback", fund_flow=-6.0),
    }
    disp_mod.display_priority(conn, today_pool=pool)
    out = capsys.readouterr().out
    assert "◆ 回马枪" in out
    cb_part = out.split("◆ 回马枪", 1)[1]
    # 独立区：按分数降序（辨识度不再置顶），净流出票不再劣后过滤（2026-08-11）
    cb_lines = [l for l in cb_part.splitlines() if "SZ300101" in l or "SZ300102" in l]
    assert "SZ300102" in cb_lines[0], f"独立区按分数降序，高分(120)应在前: {cb_lines}"
    assert "SZ300101" in cb_lines[1]


def test_display_priority_comeback_capped(monkeypatch, capsys):
    """2026-08-11：回马枪区最多显示 COMEBACK_DISPLAY_MAX 条（超量截断，避免刷屏）。"""
    conn = _rec_db()
    for i in range(12):
        _insert_rec_cat(conn, f"SZ3003{i:02d}", f"马{i}", "comeback", 50 + i)
    disp_mod.display_priority(conn, today_pool={})
    out = capsys.readouterr().out
    assert "◆ 回马枪" in out
    cb_part = out.split("◆ 回马枪", 1)[1]
    cb_lines = [l for l in cb_part.splitlines() if "SZ3003" in l]
    assert len(cb_lines) == disp_mod.COMEBACK_DISPLAY_MAX


# ── 次日大涨画像标记（2026-08-11 起并入主表行尾 🎯；2026-08-12 起成为排序档0唯一因子）──
# 原独立区（2026-08-10）与主表重合度 65%（主表 17 只中 11 只甜蜜带、两表排序几乎一致、
# 辨识度因子空转），重复输出；改为主表行尾标记。筛形条件不变（nextday_attribution 口径）：
# 推荐时刻涨幅甜蜜带（<2% 低吸潜伏 / 4~8% 中段启动）且非超买死亡信号。
# 2026-08-12：🎯 从纯视觉标记升级为排序档0唯一因子——辨识度退出排序（次日大涨本身即
# 辨识度属性），↻ 仅保留行内展示。
def _insert_rec_pct(conn, symbol: str, name: str, category: str, score: int, percent: float):
    today = now_beijing().date().isoformat()
    conn.execute(
        "INSERT INTO recommendations (date, time, symbol, name, category, score, percent) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (today, "13:00", symbol, name, category, score, percent),
    )
    conn.commit()


def test_nextday_mark_sweet_band(monkeypatch, capsys):
    """🎯 标记：甜蜜带票（<2% 低吸潜伏 / 4-8% 中段启动）行尾打标记；陷阱带(8-10%)不打。"""
    conn = _rec_db()
    _insert_rec_pct(conn, "SZ300001", "低吸", "rebound", 50, 1.0)      # <2% 甜蜜带
    _insert_rec_pct(conn, "SZ300002", "中段", "short_term", 60, 5.0)   # 4-8% 甜蜜带
    _insert_rec_pct(conn, "SZ300003", "陷阱", "momentum", 70, 9.0)     # 8-10% 陷阱带
    disp_mod.display_priority(conn, today_pool={})
    out = capsys.readouterr().out
    lines = {sym: next(l for l in out.splitlines() if sym in l)
             for sym in ["SZ300001", "SZ300002", "SZ300003"]}
    assert "🎯" in lines["SZ300001"], "<2% 甜蜜带票应有 🎯 标记"
    assert "🎯" in lines["SZ300002"], "4-8% 甜蜜带票应有 🎯 标记"
    assert "🎯" not in lines["SZ300003"], "8-10% 陷阱带票不应有 🎯 标记"
    assert "次日大涨画像" in out, "有标记票时应打印图例行"


def test_nextday_mark_excludes_overbought(monkeypatch, capsys):
    """🎯 标记：short_term 超买（死亡信号 hit 5%）不打标记。"""
    conn = _rec_db()
    _insert_rec_pct(conn, "SZ300001", "超买超短", "short_term", 60, 1.0)
    _insert_rec_pct(conn, "SZ300002", "正常超短", "short_term", 55, 1.0)
    pool = {
        "SZ300001": _cand_tier("SZ300001", 60, "short_term", percent=1.0),
        "SZ300002": _cand_tier("SZ300002", 55, "short_term", percent=1.0),
    }
    pool["SZ300001"].kline.dimensions.update({"st_overbought_flag": True})
    disp_mod.display_priority(conn, today_pool=pool)
    out = capsys.readouterr().out
    lines = {sym: next(l for l in out.splitlines() if sym in l)
             for sym in ["SZ300001", "SZ300002"]}
    assert "🎯" in lines["SZ300002"], "正常超短应有 🎯 标记"
    assert "🎯" not in lines["SZ300001"], "超买票不应有 🎯 标记"


def test_nextday_mark_no_hits_omitted(monkeypatch, capsys):
    """🎯 标记：无甜蜜带票时不打印图例行（主表正常显示）。"""
    conn = _rec_db()
    _insert_rec_pct(conn, "SZ300001", "陷阱票", "momentum", 70, 9.0)   # 8-10% 陷阱带
    disp_mod.display_priority(conn, today_pool={})
    out = capsys.readouterr().out
    assert "次日大涨画像" not in out, "无甜蜜带票时不应打印 🎯 图例行"
    assert "SZ300001" in out  # 主表仍正常显示


def test_nextday_mark_lifts_tier(monkeypatch, capsys):
    """2026-08-12: 🎯 是排序档0唯一因子——甜蜜带+非超买票(档0)排在无标记高分票(档1)前，
    无视分数；档0 内仍按类别优先级+分数排序（辨识度不再参与排序）。"""
    conn = _rec_db()
    _insert_rec_pct(conn, "SZ300001", "辨识票", "rebound", 50, 1.0)   # 辨识度+甜蜜带
    _insert_rec_pct(conn, "SZ300002", "甜蜜票", "rebound", 80, 1.0)   # 仅甜蜜带
    _insert_rec_pct(conn, "SZ300003", "普通票", "rebound", 120, 3.0)  # 死区带，无标记
    pool = {
        "SZ300001": _cand_tier("SZ300001", 50, "rebound", prominent=True, percent=1.0),
        "SZ300002": _cand_tier("SZ300002", 80, "rebound", percent=1.0),
        "SZ300003": _cand_tier("SZ300003", 120, "rebound"),
    }
    disp_mod.display_priority(conn, today_pool=pool)
    out = capsys.readouterr().out
    lines = [l for l in out.splitlines() if "SZ30000" in l]
    assert len(lines) == 3
    # 档0（辨识度+🎯）内同类别按分数降序：甜蜜票(80) > 辨识票(50)；无标记高分票(120)落档1
    assert "SZ300002" in lines[0], f"🎯 甜蜜带票(80,档0)应排在辨识度票(50,档0)前: {lines}"
    assert "SZ300001" in lines[1]
    assert "SZ300003" in lines[2], f"无标记高分票(120,档1)应排在档0之后: {lines}"
    assert "🎯" in lines[0] and "🎯" in lines[1]
    assert "🎯" not in lines[2], f"死区带票不应有 🎯 标记: {lines}"
