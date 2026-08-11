"""综合排序显示与资金流图标测试。"""
import sqlite3
from datetime import timedelta
from types import SimpleNamespace

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
    2026-08-11：主区无推荐时才兜底展示，仅显示前 COMEBACK_DISPLAY_MAX 条。"""
    conn = _rec_db()
    _insert_rec_cat(conn, "SZ300986", "志特新材", "comeback", 70)
    _insert_rec_cat(conn, "SZ300111", "回踩股", "comeback", 55)
    # 变体来源：候选 kline.dimensions（DB-only 行走 trend 前缀）
    pool = {
        "SZ300986": _comeback_candidate("志特新材", "SZ300986", "反转", 70),
        "SZ300111": _comeback_candidate("回踩股", "SZ300111", "回踩", 55),
    }
    monkeypatch.setattr(disp_mod, "_session_state", SimpleNamespace(today_pool=pool))
    disp_mod.display_priority(conn)
    out = capsys.readouterr().out
    assert "◆ 回马枪" in out
    assert "兜底参考" in out
    line_rt = next(l for l in out.splitlines() if "SZ300986" in l)
    line_re = next(l for l in out.splitlines() if "SZ300111" in l)
    assert "CB" in line_rt
    assert "回马" in line_rt        # SUGGEST_BY_CAT['comeback']
    assert "反转" in line_rt
    assert "回踩" in line_re


def test_display_priority_comeback_hidden_when_main_has_recs(monkeypatch, capsys):
    """2026-08-11：主区（榜上五类）有推荐时不显示回马枪独立区——回马枪只在无推荐时兜底参考。"""
    conn = _rec_db()
    _insert_rec_cat(conn, "SZ300001", "反弹", "rebound", 50)
    _insert_rec_cat(conn, "SZ300002", "回马", "comeback", 90)
    _insert_rec_cat(conn, "SZ300003", "超短", "short_term", 80)
    monkeypatch.setattr(disp_mod, "_session_state", SimpleNamespace(today_pool={}))
    disp_mod.display_priority(conn)
    out = capsys.readouterr().out
    assert "◆ 回马枪" not in out
    main_lines = [l for l in out.splitlines() if "SZ30000" in l]
    assert "SZ300001" in main_lines[0]   # rebound
    assert "SZ300003" in main_lines[1]   # short_term
    assert "SZ300002" not in out         # comeback 行随区块整体隐藏


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
    monkeypatch.setattr(disp_mod, "_session_state", SimpleNamespace(today_pool={"SZ300001": cand}))
    disp_mod.display_priority(conn, live_quotes={"SZ300001": {"percent": 3.2, "current": 10.5}})
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
    monkeypatch.setattr(disp_mod, "_session_state", SimpleNamespace(today_pool={}))
    disp_mod.display_priority(conn, live_quotes={"SZ300002": {"percent": 4.5, "current": 20.0}})
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
    monkeypatch.setattr(disp_mod, "_session_state", SimpleNamespace(today_pool={"SZ300001": cand}))
    disp_mod.display_priority(conn)
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
    monkeypatch.setattr(disp_mod, "_session_state", SimpleNamespace(today_pool={}))
    disp_mod.display_priority(conn)
    out = capsys.readouterr().out
    line = next(l for l in out.splitlines() if "SZ300002" in l)
    assert "+0.00%" in line
    assert "+2.00%" not in line


def test_display_priority_rank_map_for_dropped(monkeypatch, capsys):
    """掉榜/重启行（无候选）的排名由当前飙升榜 rank_map 补上（此前恒为 —）。"""
    conn = _rec_db()
    _insert_rec(conn, "SZ300002", "掉榜票", 1.0)
    monkeypatch.setattr(disp_mod, "_session_state", SimpleNamespace(today_pool={}))
    disp_mod.display_priority(conn, rank_map={"SZ300002": 42})
    out = capsys.readouterr().out
    line = next(l for l in out.splitlines() if "SZ300002" in l)
    assert "42" in line


# ── 综合排序分组顺序（2026-08-07 复核：rebound > short_term > momentum > known_new_face > new_face > pullback）──
def _insert_rec_cat(conn, symbol: str, name: str, category: str, score: int):
    today = now_beijing().date().isoformat()
    conn.execute(
        "INSERT INTO recommendations (date, time, symbol, name, category, score, percent) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (today, "13:00", symbol, name, category, score, 1.0),
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
    monkeypatch.setattr(disp_mod, "_session_state", SimpleNamespace(today_pool={}))
    disp_mod.display_priority(conn)
    out = capsys.readouterr().out
    # 主表区域 = 次日大涨候选独立区之前（该区为 display-only 观察窗，会重复列出甜蜜带票）
    main_out = out.split("◆ 次日大涨候选", 1)[0]
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
    monkeypatch.setattr(disp_mod, "_session_state", SimpleNamespace(today_pool={}))
    disp_mod.display_priority(conn)
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
    monkeypatch.setattr(disp_mod, "_session_state", SimpleNamespace(today_pool={}))
    disp_mod.display_priority(conn)
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
               fund_flow: float | None = None, prominent: bool = False) -> Candidate:
    dims = {}
    if fund_flow is not None:
        dims["fund_flow_main_pct"] = fund_flow
    k = KlineSummary(trend="", accumulated_pct=0.0, volume_ratio=1.0,
                     bottom_confirmed=False, score=score, dimensions=dims)
    c = Candidate(
        stock=StockInfo(symbol=symbol, name="测试", code=symbol[-6:], percent=1.0,
                        current=10.0, value=1e8, rank_change=0, rank=1),
        category=category, score=score, reason="", kline=k)
    if prominent:
        c.prominence_labels.append("↻")
    return c


def test_display_priority_tier_front_cross_category(monkeypatch, capsys):
    """跨类别置顶：ST 置前票(档0)排在 MOM 普通高分票(档1)之前；档0 内仍按类别优先级+评分。"""
    conn = _rec_db()
    _insert_rec_cat(conn, "SZ300002", "动量置前", "momentum", 80)
    _insert_rec_cat(conn, "SZ300003", "超短置前", "short_term", 90)
    _insert_rec_cat(conn, "SZ300004", "普通动量", "momentum", 125)
    pool = {"SZ300002": _cand_tier("SZ300002", 80, prominent=True),
            "SZ300003": _cand_tier("SZ300003", 90, category="short_term", prominent=True),
            "SZ300004": _cand_tier("SZ300004", 125)}
    monkeypatch.setattr(disp_mod, "_session_state", SimpleNamespace(today_pool=pool))
    disp_mod.display_priority(conn)
    out = capsys.readouterr().out
    main_out = out.split("◆ 次日大涨候选", 1)[0]
    lines = [l for l in main_out.splitlines() if "SZ30000" in l]
    assert len(lines) == 3
    # 档0 内按 CAT_DISPLAY_PRIORITY：short_term(1) < momentum(2)，超短置前在前
    order = ["SZ300003", "SZ300002", "SZ300004"]
    for i, sym in enumerate(order):
        assert sym in lines[i], f"{sym} 应在第 {i} 行，实际: {lines}"


def test_display_priority_tier_front_within_category(monkeypatch, capsys):
    """同类别内：低分辨识度票(档0)排在普通高分票(档1)之前，不靠分数翻盘。"""
    conn = _rec_db()
    _insert_rec(conn, "SZ300001", "置前票", 1.0)   # momentum score 60
    _insert_rec_cat(conn, "SZ300002", "高分普通票", "momentum", 150)
    pool = {"SZ300001": _cand_tier("SZ300001", 60, prominent=True),
            "SZ300002": _cand_tier("SZ300002", 150)}
    monkeypatch.setattr(disp_mod, "_session_state", SimpleNamespace(today_pool=pool))
    disp_mod.display_priority(conn)
    out = capsys.readouterr().out
    lines = [l for l in out.splitlines() if "SZ30000" in l]
    assert "SZ300001" in lines[0], f"辨识度票(60)应排在普通高分票(150)之前: {lines}"
    assert "SZ300002" in lines[1]


def test_display_priority_fund_flow_no_longer_sorts(monkeypatch, capsys):
    """资金流不再参与排序（2026-08-11）：净流入≥5% 的低分票不再因资金流置前，
    按正常档位（无辨识度=档1）+类别+分数排序。"""
    conn = _rec_db()
    _insert_rec(conn, "SZ300001", "流入票", 1.0)
    _insert_rec_cat(conn, "SZ300002", "高分普通票", "momentum", 65)
    pool = {"SZ300001": _cand_tier("SZ300001", 55, fund_flow=6.0),
            "SZ300002": _cand_tier("SZ300002", 65)}
    monkeypatch.setattr(disp_mod, "_session_state", SimpleNamespace(today_pool=pool))
    disp_mod.display_priority(conn)
    out = capsys.readouterr().out
    lines = [l for l in out.splitlines() if "SZ30000" in l]
    assert "SZ300002" in lines[0], f"高分普通票(65)应排在强流入票(55)之前(资金流不再置前): {lines}"
    assert "SZ300001" in lines[1]


def test_display_priority_fund_flow_outflow_not_hidden(monkeypatch, capsys):
    """资金流不再劣后（2026-08-11）：主力净流出≤-5% 的票不再被过滤出综合排序，
    正常展示；辨识度仍置前。"""
    conn = _rec_db()
    _insert_rec(conn, "SZ300001", "流出票", 1.0)  # momentum score 60
    _insert_rec_cat(conn, "SZ300002", "普通票", "momentum", 50)
    pool = {"SZ300001": _cand_tier("SZ300001", 100, fund_flow=-6.0, prominent=True),
            "SZ300002": _cand_tier("SZ300002", 50)}
    monkeypatch.setattr(disp_mod, "_session_state", SimpleNamespace(today_pool=pool))
    disp_mod.display_priority(conn)
    out = capsys.readouterr().out
    main_out = out.split("◆ 次日大涨候选", 1)[0]
    lines = [l for l in main_out.splitlines() if "SZ30000" in l]
    assert len(lines) == 2, f"净流出票不应被过滤，两条都展示: {lines}"
    assert "SZ300001" in lines[0], f"辨识度票应置前: {lines}"
    assert "SZ300002" in lines[1]


def test_display_priority_tier_db_source_for_dropped(monkeypatch, capsys):
    """掉榜行（无候选）统一分档：辨识度从 appearances 现算；资金流仅用于图标不参与排序。"""
    conn = _rec_db()
    today = now_beijing().date()
    for i in range(5):
        d = (today - timedelta(days=i)).isoformat()
        conn.execute(
            "INSERT INTO appearances (symbol, name, date, rank) VALUES (?, ?, ?, ?)",
            ("SZ300001", "辨识票", d, 40 + i),
        )
    _insert_rec(conn, "SZ300001", "辨识票", 1.0)  # momentum score 60
    _insert_rec_cat(conn, "SZ300002", "普通票", "momentum", 90)
    conn.execute(
        "INSERT INTO market_extra_cache (symbol, date, data_type, payload_json, updated) "
        "VALUES (?, ?, ?, ?, ?)",
        ("SZ300001", today.isoformat(), "fund_flow", '{"main_pct": 6.0, "main_net": 1e7}',
         now_beijing().isoformat()),
    )
    conn.commit()
    monkeypatch.setattr(disp_mod, "_session_state", SimpleNamespace(today_pool={}))
    disp_mod.display_priority(conn)
    out = capsys.readouterr().out
    lines = [l for l in out.splitlines() if "SZ30000" in l]
    assert "SZ300001" in lines[0], f"掉榜置前票(60,档0)应排在普通票(90,档1)之前: {lines}"
    assert "SZ300002" in lines[1]


def test_display_priority_tier_banner_separates_groups(monkeypatch, capsys):
    """档位分隔横幅下线后，辨识度档仍排前；净流出票不再劣后过滤（2026-08-11）。"""
    conn = _rec_db()
    _insert_rec_cat(conn, "SZ300001", "置顶", "rebound", 50)
    _insert_rec_cat(conn, "SZ300002", "普通", "momentum", 70)
    _insert_rec_cat(conn, "SZ300003", "流出", "rebound", 90)
    pool = {
        "SZ300001": _cand_tier("SZ300001", 50, "rebound", prominent=True),
        "SZ300002": _cand_tier("SZ300002", 70, "momentum"),
        "SZ300003": _cand_tier("SZ300003", 90, "rebound", fund_flow=-6.0, prominent=True),
    }
    monkeypatch.setattr(disp_mod, "_session_state", SimpleNamespace(today_pool=pool))
    disp_mod.display_priority(conn)
    out = capsys.readouterr().out
    assert "▶ 置顶档" not in out
    assert "▶ 普通档" not in out
    main_out = out.split("◆ 次日大涨候选", 1)[0]
    lines = [l for l in main_out.splitlines() if "SZ30000" in l]
    assert len(lines) == 3, f"净流出票(SZ300003)应正常展示，不再被劣后过滤: {lines}"
    # 档0（辨识度）内按类别优先级+分数降序：SZ300003(rebound,90) 与 SZ300001(rebound,50) 同档，高分在前
    assert "SZ300003" in lines[0], f"辨识度档高分票应在前: {lines}"
    assert "SZ300001" in lines[1]
    assert "SZ300002" in lines[2]


def test_display_priority_comeback_separate_region(monkeypatch, capsys):
    """方案A：回马枪独立成区（主区无推荐时兜底），按辨识度档位置前；独立区净流出票正常展示。"""
    conn = _rec_db()
    _insert_rec_cat(conn, "SZ300101", "马置顶", "comeback", 55)
    _insert_rec_cat(conn, "SZ300102", "马劣后", "comeback", 120)
    pool = {
        "SZ300101": _cand_tier("SZ300101", 55, "comeback", prominent=True),
        "SZ300102": _cand_tier("SZ300102", 120, "comeback", fund_flow=-6.0),
    }
    monkeypatch.setattr(disp_mod, "_session_state", SimpleNamespace(today_pool=pool))
    disp_mod.display_priority(conn)
    out = capsys.readouterr().out
    assert "◆ 回马枪" in out
    cb_part = out.split("◆ 回马枪", 1)[1]
    # 独立区：辨识度(档0)置前，净流出票不再劣后过滤（2026-08-11）
    cb_lines = [l for l in cb_part.splitlines() if "SZ300101" in l or "SZ300102" in l]
    assert "SZ300101" in cb_lines[0]
    assert "SZ300102" in cb_lines[1]


def test_display_priority_comeback_capped(monkeypatch, capsys):
    """2026-08-11：回马枪区最多显示 COMEBACK_DISPLAY_MAX 条（超量截断，避免刷屏）。"""
    conn = _rec_db()
    for i in range(12):
        _insert_rec_cat(conn, f"SZ3003{i:02d}", f"马{i}", "comeback", 50 + i)
    monkeypatch.setattr(disp_mod, "_session_state", SimpleNamespace(today_pool={}))
    disp_mod.display_priority(conn)
    out = capsys.readouterr().out
    assert "◆ 回马枪" in out
    cb_part = out.split("◆ 回马枪", 1)[1]
    cb_lines = [l for l in cb_part.splitlines() if "SZ3003" in l]
    assert len(cb_lines) == disp_mod.COMEBACK_DISPLAY_MAX


# ── 次日大涨候选独立区（2026-08-10）：display-only 观察窗口 ──
def _insert_rec_pct(conn, symbol: str, name: str, category: str, score: int, percent: float):
    today = now_beijing().date().isoformat()
    conn.execute(
        "INSERT INTO recommendations (date, time, symbol, name, category, score, percent) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (today, "13:00", symbol, name, category, score, percent),
    )
    conn.commit()


def test_nextday_spike_section_sweet_band(monkeypatch, capsys):
    """次日大涨候选区：甜蜜带票（<2% 低吸潜伏 / 4-8% 中段启动）进入独立区；陷阱带(8-10%)排除。"""
    conn = _rec_db()
    _insert_rec_pct(conn, "SZ300001", "低吸", "rebound", 50, 1.0)      # <2% 甜蜜带
    _insert_rec_pct(conn, "SZ300002", "中段", "short_term", 60, 5.0)   # 4-8% 甜蜜带
    _insert_rec_pct(conn, "SZ300003", "陷阱", "momentum", 70, 9.0)     # 8-10% 陷阱带
    monkeypatch.setattr(disp_mod, "_session_state", SimpleNamespace(today_pool={}))
    disp_mod.display_priority(conn)
    out = capsys.readouterr().out
    assert "◆ 次日大涨候选" in out
    nextday_part = out.split("◆ 次日大涨候选", 1)[1]
    assert "SZ300001" in nextday_part
    assert "SZ300002" in nextday_part
    assert "SZ300003" not in nextday_part, "8-10% 陷阱带票不应进次日大涨候选区"


def test_nextday_spike_section_excludes_overbought(monkeypatch, capsys):
    """次日大涨候选区：short_term 超买（死亡信号 hit 5%）被排除。"""
    conn = _rec_db()
    _insert_rec_pct(conn, "SZ300001", "超买超短", "short_term", 60, 1.0)
    _insert_rec_pct(conn, "SZ300002", "正常超短", "short_term", 55, 1.0)
    dims_over = {"st_overbought_flag": True}
    pool = {
        "SZ300001": _cand_tier("SZ300001", 60, "short_term"),
        "SZ300002": _cand_tier("SZ300002", 55, "short_term"),
    }
    pool["SZ300001"].kline.dimensions.update(dims_over)
    monkeypatch.setattr(disp_mod, "_session_state", SimpleNamespace(today_pool=pool))
    disp_mod.display_priority(conn)
    out = capsys.readouterr().out
    assert "◆ 次日大涨候选" in out
    nextday_part = out.split("◆ 次日大涨候选", 1)[1]
    assert "SZ300002" in nextday_part
    assert "SZ300001" not in nextday_part, "超买票不应进次日大涨候选区"


def test_nextday_spike_section_no_hits_omitted(monkeypatch, capsys):
    """次日大涨候选区：无甜蜜带票时不输出该区（避免空区块）。"""
    conn = _rec_db()
    _insert_rec_pct(conn, "SZ300001", "陷阱票", "momentum", 70, 9.0)   # 8-10% 陷阱带
    monkeypatch.setattr(disp_mod, "_session_state", SimpleNamespace(today_pool={}))
    disp_mod.display_priority(conn)
    out = capsys.readouterr().out
    assert "◆ 次日大涨候选" not in out, "无甜蜜带票时不应输出空独立区"
    assert "SZ300001" in out  # 主表仍正常显示


def test_nextday_zone_prominence_prioritized(monkeypatch, capsys):
    """次日大涨候选区复用辨识度（2026-08-10）：↻ 票排非辨识度票前（即使分数更低）。"""
    conn = _rec_db()
    _insert_rec_pct(conn, "SZ300001", "辨识票", "rebound", 50, 1.0)   # <2% 甜蜜带
    _insert_rec_pct(conn, "SZ300002", "普通票", "rebound", 80, 1.0)
    pool = {
        "SZ300001": _cand_tier("SZ300001", 50, "rebound", prominent=True),
        "SZ300002": _cand_tier("SZ300002", 80, "rebound"),
    }
    monkeypatch.setattr(disp_mod, "_session_state", SimpleNamespace(today_pool=pool))
    disp_mod.display_priority(conn)
    out = capsys.readouterr().out
    nextday_part = out.split("◆ 次日大涨候选", 1)[1]
    lines = [l for l in nextday_part.splitlines() if "SZ30000" in l]
    assert len(lines) == 2
    assert "SZ300001" in lines[0], f"辨识度票(score50)应排在普通票(score80)前: {lines}"
    assert "SZ300002" in lines[1]
