"""scanner.pick 今日选股建议模块测试。"""
from scanner.pick import build_pick_suggestion


class _FakeCandidate:
    """极简 Candidate 替身：只保留 pick 模块读取的字段。"""
    def __init__(self, sector="", driving_concept="", risk_flags=None,
                 ff_pct=None, market_env_bonus=None):
        self.sector = sector
        self.driving_concept = driving_concept
        self.risk_flags = list(risk_flags or [])
        self.kline = None
        if ff_pct is not None or market_env_bonus is not None:
            dims = {}
            if ff_pct is not None:
                dims["fund_flow_main_pct"] = ff_pct
            if market_env_bonus is not None:
                dims["market_env_bonus"] = market_env_bonus
            self.kline = _FakeKline(dims)

    @property
    def stock(self):
        return None


class _FakeKline:
    def __init__(self, dims):
        self.dimensions = dims


def _entry(symbol, name, category, score, cand=None, concept=""):
    return {
        "symbol": symbol, "name": name, "category": category,
        "score": score, "concept": concept, "_candidate": cand,
        "percent": 3.0,
    }


def test_excludes_negative_expectation_categories():
    """new_face/known_new_face/pullback/comeback 不进入选股建议。"""
    entries = [
        _entry("SZ300001", "新面孔A", "new_face", 90),
        _entry("SZ300002", "二次B", "known_new_face", 80),
        _entry("SZ300003", "回马C", "comeback", 70),
        _entry("SZ300004", "回调D", "pullback", 60),
        _entry("SZ300005", "超短E", "short_term", 85),
    ]
    sug = build_pick_suggestion(entries)
    syms = {p["symbol"] for p in sug["picks"]}
    assert syms == {"SZ300005"}, f"应只选 short_term，got {syms}"


def test_priority_rebound_over_short_term_over_momentum():
    """类别优先级：rebound > short_term > momentum，与 score 无关。"""
    entries = [
        _entry("SZ300001", "动量高分", "momentum", 150),
        _entry("SZ300002", "超短", "short_term", 60),
        _entry("SZ300003", "反弹", "rebound", 50),
    ]
    sug = build_pick_suggestion(entries)
    cats = [p["category"] for p in sug["picks"]]
    assert cats[0] == "rebound", f"rebound 应优先，got {cats}"
    assert cats[1] == "short_term", f"short_term 应第二，got {cats}"


def test_sector_diversification():
    """同板块只选 1 只；不同板块才放开。"""
    cand_a = _FakeCandidate(sector="半导体")
    cand_b = _FakeCandidate(sector="半导体")
    cand_c = _FakeCandidate(sector="医疗器械")
    entries = [
        _entry("SZ300001", "票A", "short_term", 90, cand_a),
        _entry("SZ300002", "票B", "short_term", 85, cand_b),
        _entry("SZ300003", "票C", "short_term", 80, cand_c),
    ]
    sug = build_pick_suggestion(entries, target=2)
    syms = {p["symbol"] for p in sug["picks"]}
    assert syms == {"SZ300001", "SZ300003"}, f"应跨板块选 A+C，got {syms}"


def test_weak_market_blocks_momentum():
    """弱市（market_env_bonus<0）时 momentum 被屏蔽。"""
    cand = _FakeCandidate(market_env_bonus=-2)
    entries = [
        _entry("SZ300001", "动量A", "momentum", 100, cand),
        _entry("SZ300002", "超短B", "short_term", 50),
    ]
    sug = build_pick_suggestion(entries)
    cats = [p["category"] for p in sug["picks"]]
    assert cats == ["short_term"], f"弱市应只选 short_term，got {cats}"


def test_strong_market_allows_momentum():
    """强势市（market_env_bonus>0）时 momentum 可入选。"""
    cand = _FakeCandidate(market_env_bonus=2)
    entries = [
        _entry("SZ300001", "动量A", "momentum", 100, cand),
        _entry("SZ300002", "超短B", "short_term", 50),
    ]
    sug = build_pick_suggestion(entries)
    cats = [p["category"] for p in sug["picks"]]
    assert cats[0] == "short_term" and "momentum" in cats, f"强势市应含 momentum，got {cats}"


def test_hard_risk_excluded():
    """命中硬风险标签（主力出货/趋势破位）的候选被排除。"""
    cand = _FakeCandidate(sector="半导体", risk_flags=["主力出货"])
    entries = [
        _entry("SZ300001", "出货票", "short_term", 100, cand),
        _entry("SZ300002", "好票", "short_term", 60),
    ]
    sug = build_pick_suggestion(entries)
    syms = {p["symbol"] for p in sug["picks"]}
    assert syms == {"SZ300002"}, f"应排除出货票，got {syms}"


def test_outflow_excluded():
    """主力净流出(≤-5%)劣后档候选被排除。"""
    cand = _FakeCandidate(sector="半导体", ff_pct=-6.0)
    entries = [
        _entry("SZ300001", "流出票", "short_term", 100, cand),
        _entry("SZ300002", "好票", "short_term", 60),
    ]
    sug = build_pick_suggestion(entries)
    syms = {p["symbol"] for p in sug["picks"]}
    assert syms == {"SZ300002"}, f"应排除净流出票，got {syms}"


def test_inflow_not_excluded():
    """主力净流入/中性不排除（仅净流出劣后）。"""
    cand = _FakeCandidate(sector="半导体", ff_pct=5.0)
    entries = [_entry("SZ300001", "流入票", "short_term", 100, cand)]
    sug = build_pick_suggestion(entries)
    assert len(sug["picks"]) == 1, f"净流入票应保留，got {sug['picks']}"


def test_fill_within_sector_when_insufficient():
    """不足 target 时放宽同板块补齐。"""
    cand_a = _FakeCandidate(sector="半导体")
    cand_b = _FakeCandidate(sector="半导体")
    entries = [
        _entry("SZ300001", "票A", "short_term", 90, cand_a),
        _entry("SZ300002", "票B", "short_term", 85, cand_b),
    ]
    sug = build_pick_suggestion(entries, target=2)
    syms = {p["symbol"] for p in sug["picks"]}
    assert syms == {"SZ300001", "SZ300002"}, f"同板块也应变体补齐，got {syms}"


def test_empty_pool():
    """全被过滤 → picks 空 + reasons 说明。"""
    cand = _FakeCandidate(risk_flags=["主力出货"])
    entries = [_entry("SZ300001", "唯一票", "short_term", 100, cand)]
    sug = build_pick_suggestion(entries)
    assert sug["picks"] == []
    # 全被过滤时 reasons 保留排除说明（硬风险），且无 picks
    assert any("硬风险" in r for r in sug["reasons"])
