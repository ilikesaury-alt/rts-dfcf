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
        score_breakdown TEXT, source TEXT DEFAULT 'xueqiu', concept TEXT, accumulated_pct REAL)""")
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


def test_display_comeback_section(capsys):
    """回马枪分区渲染：含「反转」「回踩」两变体行（trend 列带变体前缀）。"""
    disp_mod.display([], [], 0, 60,
                     comeback_list=[_comeback_candidate("志特新材", "SZ300986", "反转"),
                                    _comeback_candidate("某股", "SZ300111", "回踩")])
    out = capsys.readouterr().out
    assert "◆ 回马枪" in out
    line_rt = next(l for l in out.splitlines() if "SZ300986" in l)
    line_re = next(l for l in out.splitlines() if "SZ300111" in l)
    assert "反转·超跌企稳" in line_rt
    assert "回踩·超跌企稳" in line_re


def test_display_priority_comeback_label_and_rank(monkeypatch, capsys):
    """综合排序：comeback 显示 CB 标签 + 建议「回马」，且优先级插在 rebound 与 short_term 之间。"""
    conn = _rec_db()
    _insert_rec_cat(conn, "SZ300001", "反弹", "rebound", 50)
    _insert_rec_cat(conn, "SZ300002", "回马", "comeback", 90)
    _insert_rec_cat(conn, "SZ300003", "超短", "short_term", 80)
    monkeypatch.setattr(disp_mod, "_session_state", SimpleNamespace(today_pool={}))
    disp_mod.display_priority(conn)
    out = capsys.readouterr().out
    lines = [l for l in out.splitlines() if "SZ30000" in l]
    assert len(lines) == 3
    assert "SZ300001" in lines[0]   # rebound 优先级 0
    assert "SZ300002" in lines[1]   # comeback 优先级 1
    assert "SZ300003" in lines[2]   # short_term 优先级 2
    line_cb = lines[1]
    assert "CB" in line_cb
    assert "回马" in line_cb        # SUGGEST_BY_CAT['comeback']


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
    lines = [l for l in out.splitlines() if "SZ30000" in l]
    assert len(lines) == 5
    order = ["SZ300003", "SZ300005", "SZ300004", "SZ300002", "SZ300001"]
    for i, sym in enumerate(order):
        assert sym in lines[i], f"{sym} 应在第 {i} 行，实际顺序: {lines}"


def test_display_priority_suggestion_decoupled(monkeypatch, capsys):
    """建议列与优先级解耦：new_face 位次垫底仍显示「参考」，kNF/rebound 显示「推荐」，short_term 显示「超短」。"""
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
    assert "推荐" in lines["SZ300002"]  # known_new_face
    assert "推荐" in lines["SZ300003"]  # rebound
    assert "参考" in lines["SZ300001"]  # new_face 位次虽低仍参考，非回避
    assert "超短" in lines["SZ300004"]  # short_term
    assert "回避" not in lines["SZ300001"]


# ── 综合排序档位置顶（2026-08-06）：排序键 (档位, 类别优先级, -score) ──
# 档0置前 = 辨识度(↻) 或 主力净流入≥5%；档2劣后 = 净流出≤-5%（覆盖辨识度）；其余档1普通。
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
    lines = [l for l in out.splitlines() if "SZ30000" in l]
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


def test_display_priority_tier_strong_inflow_front(monkeypatch, capsys):
    """强资金流置前：净流入≥5% 的低分票(档0)排在普通高分票(档1)之前。"""
    conn = _rec_db()
    _insert_rec(conn, "SZ300001", "流入票", 1.0)
    _insert_rec_cat(conn, "SZ300002", "普通票", "momentum", 65)
    pool = {"SZ300001": _cand_tier("SZ300001", 55, fund_flow=6.0),
            "SZ300002": _cand_tier("SZ300002", 65)}
    monkeypatch.setattr(disp_mod, "_session_state", SimpleNamespace(today_pool=pool))
    disp_mod.display_priority(conn)
    out = capsys.readouterr().out
    lines = [l for l in out.splitlines() if "SZ30000" in l]
    assert "SZ300001" in lines[0], f"强流入票(55)应排在普通票(65)之前: {lines}"
    assert "SZ300002" in lines[1]


def test_display_priority_tier_outflow_last_overrides_prominence(monkeypatch, capsys):
    """净流出强制劣后：高分辨识度+流出票(档2)排到普通票(档1)之后。"""
    conn = _rec_db()
    _insert_rec(conn, "SZ300001", "流出置前", 1.0)  # momentum score 60
    _insert_rec_cat(conn, "SZ300002", "普通票", "momentum", 50)
    pool = {"SZ300001": _cand_tier("SZ300001", 100, fund_flow=-6.0, prominent=True),
            "SZ300002": _cand_tier("SZ300002", 50)}
    monkeypatch.setattr(disp_mod, "_session_state", SimpleNamespace(today_pool=pool))
    disp_mod.display_priority(conn)
    out = capsys.readouterr().out
    lines = [l for l in out.splitlines() if "SZ30000" in l]
    assert "SZ300002" in lines[0], f"普通票(50)应排在辨识度+流出票(100)之前: {lines}"
    assert "SZ300001" in lines[1]


def test_display_priority_tier_db_source_for_dropped(monkeypatch, capsys):
    """掉榜行（无候选）统一分档：资金流从 market_extra_cache、辨识度从 appearances。"""
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
    """档位分隔横幅：跨三档时输出含 置顶档/普通档/劣后档 分组标题，且辨识度+流出票落在劣后档。"""
    conn = _rec_db()
    _insert_rec_cat(conn, "SZ300001", "置顶", "rebound", 50)
    _insert_rec_cat(conn, "SZ300002", "普通", "momentum", 70)
    _insert_rec_cat(conn, "SZ300003", "劣后", "rebound", 90)
    pool = {
        "SZ300001": _cand_tier("SZ300001", 50, "rebound", prominent=True),
        "SZ300002": _cand_tier("SZ300002", 70, "momentum"),
        "SZ300003": _cand_tier("SZ300003", 90, "rebound", fund_flow=-6.0, prominent=True),
    }
    monkeypatch.setattr(disp_mod, "_session_state", SimpleNamespace(today_pool=pool))
    disp_mod.display_priority(conn)
    out = capsys.readouterr().out
    assert "▶ 置顶档" in out
    assert "▶ 普通档" in out
    assert "▶ 劣后档" in out
    # 劣后档横幅后出现的首个数据行应是 SZ300003（流出覆盖辨识度，沉到档2）
    after_last = out.split("▶ 劣后档", 1)[1]
    assert "SZ300003" in after_last
    assert "SZ300001" not in after_last
