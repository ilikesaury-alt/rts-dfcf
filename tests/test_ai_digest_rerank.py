"""`scanner/ai_digest/rerank.py` 的单测（**Phase 2 证据的可复现载体**）。

⚠ 该模块的 **CLI 入口已于 2026-11-04 下线**（结论：模型排序相对确定性排序无显著增量，
排序不该交给模型）—— 本文件守的是**仍被复用的库层能力**：

1. **素材**：确定性、缺字段降级、**表里不含分数**（分数无区分力 ⇒ 不喂模型）；
2. **覆盖度校验**：listwise 输出的典型坏法（漏票 / 重复 / 编池外票 / 代码笔误→名称回退）；
3. **红线守卫（rerank 档）**：描述用法放行、祈使/具体价位型建议拦下（用**真实模型输出**
   的误伤片段做回归）；
4. **提示词口径**：排序依据（先验 + 主力净占比）与「不给分数」必须写在提示词里。
"""

from __future__ import annotations

import pytest

from scanner.ai_digest.guard import is_rerank_redlined
from scanner.ai_digest.rerank import (
    build_evidence_items,
    build_pool_materials,
    build_rerank_messages,
    parse_ranking,
    render_pool_items,
    render_pool_table,
)

POOL = ("SZ300489", "SZ300006", "SZ300364")


def _row(symbol="SZ300489", name="光智科技", **over):
    base = {
        "symbol": symbol,
        "name": name,
        "category": "rebound",
        "prior": 0.179,
        "p": 0.212,
        "percent": 4.16,
        "accum": -20.92,
        "flow": 10.65,
        "rank": 7,
        "sector": "人工智能",
        "gate": "A档（先验最高档）",
        "score": 777,  # ⚠ 刻意放一个可辨识的分数：表里不许出现它
    }
    base.update(over)
    return base


def _rows():
    return [
        _row(),
        _row(symbol="SZ300006", name="莱美药业", category="momentum", prior=0.1, p=0.1107, score=83),
        _row(symbol="SZ300364", name="中文在线", category="short_term", prior=0.062, p=0.0683, score=83),
    ]


def _stats():
    return {
        "label_n": 1718,
        "prior": {"rebound": 0.179, "momentum": 0.1, "short_term": 0.062},
        "measured": {"rebound": (143, 9.1), "momentum": (552, 15.2), "short_term": (853, 5.2)},
        "score_buckets": [("80+", 1648, 6.6), ("70-79", 676, 5.8), ("60-69", 628, 7.3), ("<60", 3502, 8.0)],
    }


def _materials():
    return build_pool_materials("2026-11-04", _rows(), evidence=build_evidence_items(_stats()))


# ── 1. 素材 ────────────────────────────────────────────────────────────────────


def test_pool_materials_are_deterministic():
    a = build_pool_materials("2026-11-04", _rows())
    b = build_pool_materials("2026-11-04", _rows())
    assert render_pool_items(a) == render_pool_items(b)
    assert a.symbols == POOL


def test_missing_field_becomes_na_and_is_registered():
    rows = [_row(rank=None, flow=None)]
    m = build_pool_materials("2026-11-04", rows)
    assert "SZ300489.rank" in m.missing
    assert "SZ300489.flow" in m.missing
    items = render_pool_items(m)
    assert "暂无数据" in items


def test_deterministic_order_sorts_by_prior_then_flow_then_prob():
    """确定性排序键 = 先验 → 主力净占比 → 系统概率（Phase 2 回放的依据）。"""
    from scanner.ai_digest.rerank import deterministic_order

    rows = [
        _row(symbol="A", prior=0.062, flow=10.0, p=0.20),  # short_term 先验最低
        _row(symbol="B", prior=0.179, flow=1.0, p=0.10),  # rebound 先验最高
        _row(symbol="C", prior=0.179, flow=5.0, p=0.30),  # rebound 同先验、主力更强
    ]
    order = [r["symbol"] for r in deterministic_order(rows)]
    assert order == ["C", "B", "A"]


def test_deterministic_order_puts_missing_values_last():
    from scanner.ai_digest.rerank import deterministic_order

    rows = [
        _row(symbol="A", prior=0.179, flow=None, p=0.2),
        _row(symbol="B", prior=0.179, flow=1.0, p=0.2),
    ]
    order = [r["symbol"] for r in deterministic_order(rows)]
    assert order == ["B", "A"]


def test_deterministic_order_is_pure_and_stable():
    from scanner.ai_digest.rerank import deterministic_order

    rows = [_row(symbol=f"S{i}", prior=0.1, flow=1.0, p=0.1) for i in range(5)]
    snapshot = [dict(r) for r in rows]
    a = deterministic_order(rows)
    b = deterministic_order(rows)
    assert [r["symbol"] for r in a] == [r["symbol"] for r in b]
    assert rows == snapshot  # 不改原列表


def test_score_is_never_fed_to_the_model():
    """分数与次日命中无单调关系（实测 n=3543）⇒ 刻意**不进表**，模型无从锚定。"""
    m = _materials()
    table = render_pool_table(m)
    assert "777" not in table
    header = [ln for ln in table.splitlines() if ln.startswith("当前序")][0]
    assert "分数" not in header  # 表头里没有分数列
    # 但证据行要**说明**为什么不给（否则模型会去猜）
    assert "分数与命中无单调关系" in table


def test_evidence_items_contain_prior_and_measured():
    stats = {
        "label_n": 1718,
        "prior": {"rebound": 0.179, "momentum": 0.1},
        "measured": {"rebound": (143, 9.1), "momentum": (552, 15.2)},
        "score_buckets": [("80+", 1648, 6.6), ("<60", 3502, 8.0)],
    }
    items = build_evidence_items(stats)
    text = "\n".join(f"{i.id}|{i.label}|{i.value}" for i in items)
    assert "ev.prior" in text and "17.9%" in text
    assert "ev.measured" in text and "n=143" in text
    assert "ev.score" in text and "80+" in text
    assert "ev.target" in text and "次日涨幅≥7%" in text


def test_prompt_only_shows_materials():
    m = _materials()
    msgs = build_rerank_messages(m)
    user = msgs[1]["content"]
    for sym in POOL:
        assert sym in user
    # 素材里没有的数字不许出现在提示词里
    assert "999" not in user
    assert msgs[0]["content"].count("不要用分数") == 1


# ── 2. 覆盖度校验（listwise 专属闸）──────────────────────────────────────────


def test_complete_ordering_passes():
    text = (
        "1|SZ300489|类别先验最高，系统概率 21.20%，主力净占比 +10.65%。\n"
        "2|SZ300006|动量延续，系统概率 11.07%。\n"
        "3|SZ300364|先验最低，短线启动。\n"
    )
    c = parse_ranking(text, POOL)
    assert c.ok is True
    assert c.ordered == POOL
    assert c.loose is False


def test_missing_stock_is_detected():
    text = "1|SZ300489|先看它。\n2|SZ300006|其次。\n"
    c = parse_ranking(text, POOL)
    assert c.missing == ("SZ300364",)
    assert c.ok is False


def test_duplicate_stock_is_detected():
    text = "1|SZ300489|甲。\n2|SZ300489|又甲。\n3|SZ300006|乙。\n4|SZ300364|丙。\n"
    c = parse_ranking(text, POOL)
    assert c.duplicates == ("SZ300489",)
    assert c.ok is False


def test_invented_stock_is_detected():
    text = "1|SZ300489|甲。\n2|SZ301234|编的票。\n3|SZ300006|乙。\n4|SZ300364|丙。\n"
    c = parse_ranking(text, POOL)
    assert c.unknown == ("301234",)
    assert c.ok is False


def test_bare_code_without_prefix_still_resolves():
    text = "1|300489|甲。\n2|300006|乙。\n3|300364|丙。\n"
    c = parse_ranking(text, POOL)
    assert c.ordered == POOL
    assert c.ok is True


def test_loose_format_falls_back_to_first_mention_order():
    text = "SZ300489 最好，其次 SZ300006，最后 SZ300364。\n"
    c = parse_ranking(text, POOL)
    assert c.loose is True
    assert c.ordered == POOL
    assert c.ok is True


def test_rank_prefix_variants_are_accepted():
    text = "第1名 SZ300489\n2、SZ300006\n3)SZ300364\n"
    c = parse_ranking(text, POOL)
    assert c.ordered == POOL
    assert c.loose is False


def test_reason_line_mentioning_another_stock_does_not_count_as_ranked():
    """理由里提到别的票 ≠ 那只票被排过（否则覆盖度检查会假绿）。"""
    text = "1|SZ300489|优于 SZ300006。\n2|SZ300006|次之。\n3|SZ300364|末位。\n"
    c = parse_ranking(text, POOL)
    assert c.ordered == POOL
    assert c.ok is True


def test_code_column_wins_over_reason_mention():
    """代码列 = 这一行的身份；理由里引用了上一只票也不该抢走这一行。"""
    text = "1|SZ300489|甲。\n2|SZ300006|弱于 SZ300489。\n3|SZ300364|丙。\n"
    c = parse_ranking(text, POOL)
    assert c.ordered == POOL
    assert c.duplicates == ()
    assert c.ok is True


def test_typo_in_code_column_is_soft_warning_not_fabrication():
    """代码列写成池外的码，但同句提到了池内票 ⇒ 判**笔误**（软），不是「编池外票」。"""
    text = "1|SZ300489|甲。\n2|SZ301536|实际是 SZ300006。\n3|SZ300364|丙。\n"
    c = parse_ranking(text, POOL)
    assert c.typos == ("301536",)
    assert c.unknown == ()
    # 这一行身份不明 ⇒ 被排的那只票算漏排（退出码 5，但诊断是准的）
    assert c.missing == ("SZ300006",)


def test_name_fallback_resolves_code_typo():
    """实测踩到：模型把 `SZ301005` 写成 `300005`，但同一行写着正确名称 ⇒ 按名称认出来，
    不该判成「漏排」（误报）。"""
    names = {"SZ300489": "光智科技", "SZ300006": "莱美药业", "SZ300364": "中文在线"}
    text = "1|SZ300489|甲。\n2|300006|莱美药业：乙。\n3|SZ300364|丙。\n"
    c = parse_ranking(text, POOL, names=names)
    assert c.ordered == POOL
    assert c.missing == ()
    assert c.ok is True


def test_name_fallback_handles_previous_stock_reference():
    """实测踩到：解释模式下模型写「3|SZ301536之后的利亚德|…」—— 代码列指的是**上一只**
    （已排过），名字才是本只 ⇒ 按名称认出来，不该判重复+漏排。"""
    names = {"SZ300489": "光智科技", "SZ301536": "星宸科技", "SZ300296": "利亚德"}
    text = "1|SZ300489|甲。\n2|SZ301536|乙。\n3|SZ301536之后的利亚德|丙。\n"
    c = parse_ranking(text, ["SZ300489", "SZ301536", "SZ300296"], names=names)
    assert c.ordered == ("SZ300489", "SZ301536", "SZ300296")
    assert c.missing == ()
    assert c.duplicates == ()
    assert c.ok is True


# ── 3. 红线分模式 ──────────────────────────────────────────────────────────────


def test_rerank_allows_ranking_language():
    """排序语言是本模式的产出，不能被转述档的词表拦死。"""
    for text in (
        "先看 SZ300489，它排在第一。",
        "SZ300489 优于 SZ300006，优先关注。",
        "按系统概率排序，第一名是 SZ300489。",
        "总判断|这一批里只有 SZ300489 的证据链完整。",
    ):
        assert is_rerank_redlined(text) is False, text


def test_rerank_still_blocks_prediction_and_actions():
    for text in (
        "1|SZ300489|必涨。",
        "SZ300489 明天大概率冲高。",
        "1|SZ300489|建议买入。",
        "SZ300489 目标价 18.5 元。",
        "SZ300489 建议轻仓参与。",
    ):
        assert is_rerank_redlined(text) is True, text


def test_rerank_ignores_disclaimer_clause():
    """实测踩到：模型结尾爱写「以上不构成任何涨跌预测或仓位建议」——
    那是一句免责声明，被「仓位」二字整个拦下会把好输出变成拒答。"""
    text = "1|SZ300489|先验最高档，系统概率 21.20%。\n总判断|以上不构成任何涨跌预测或仓位建议，仅供观察参考。\n"
    assert is_rerank_redlined(text) is False


def test_rerank_negation_scope_is_per_clause():
    """否定豁免的粒度是**从句**：前半句的否定不许把后半句的买入一起豁免。"""
    assert is_rerank_redlined("不建议追高，但可以买入 SZ300489。") is True
    assert is_rerank_redlined("不构成投资建议，可以买入 SZ300489。") is True


def test_rerank_does_not_trip_on_category_key_short_term():
    """实测踩到：英文动作槽 `short` 会误伤类别键 `short_term` ⇒ 整份好输出被拦成拒答。
    英文槽位必须按**词边界**匹配（`_` 是词字符 ⇒ `\\bshort\\b` 不匹配 `short_term`）。"""
    text = (
        "1|SZ300489|rebound 先验 17.9%，系统概率 21.20%。\n"
        "2|SZ300364|short_term 先验 6.2% 最低档，榜位 16 提供热度证据。\n"
    )
    assert is_rerank_redlined(text) is False
    # 但真正的英文做空/买入动作仍然拦
    assert is_rerank_redlined("1|SZ300489|short at 16.9。") is True
    assert is_rerank_redlined("1|SZ300489|buy zone 17.8-18.2。") is True


def test_rerank_ignores_risk_descriptions():
    """实测踩到：「追高风险需正视」是在**描述风险**，不是叫用户追高 ⇒ 不拦。
    但真的叫用户追高 / 给止损位仍然拦。"""
    assert is_rerank_redlined("1|SZ300489|5日累计 +25.62% 已明显偏高，追高风险需正视。") is False
    assert is_rerank_redlined("1|SZ300489|止损风险很大。") is False
    assert is_rerank_redlined("1|SZ300489|建议追高。") is True
    assert is_rerank_redlined("1|SZ300489|止损位 16.9。") is True


#: **真实模型输出**里被误伤的片段（2026-11-04 实测 30 个测试日里有 8 天被整段拦下）。
#: 这些全是**描述用法**：动作词当形容词/名词用，不是指令。
REAL_DESCRIPTIVE_FRAGMENTS = (
    "且 5日累计仅 +2.89% 不追高",
    "5日累计 -4.25% 无追高压力",
    "5日累计仅 +4.41% 低位不追高",
    "追高与低先验双重风险",
    "目标价与仓位信息",
    "追高证据劣势",
    "表内不足以支撑目标价",
    "止损位或仓位建议",
    "短线票里追高位置风险最明显",
    "不给目标价与仓位建议",
    "需注意追高位置",
    "今日涨幅 +3.20% 属三类 rebound 中偏弱的入场形态",
    "追高特征最明显的有榜位票",
    "存在追高段位的风险",
    "追高段位风险显著",
    "需注意追高位阶",
    "追高位风险点明",
)


@pytest.mark.parametrize("fragment", REAL_DESCRIPTIVE_FRAGMENTS)
def test_rerank_lets_real_descriptive_fragments_through(fragment):
    """守卫变噪声 = 没有守卫：真实输出里的描述用法**一个都不能拦**。"""
    assert is_rerank_redlined(fragment) is False, fragment


#: 实质建议（必须拦）：祈使 / 具体价位 / 仓位 / 涨跌预测 / 英文动作。
REAL_IMPERATIVE_PHRASES = (
    "建议买入",
    "可以买入",
    "目标价 18.5 元",
    "止损位 16.9",
    "买入 17.8",
    "仓位 30%",
    "轻仓参与",
    "半仓操作",
    "重仓参与",
    "买入。",
    "加仓至半仓",
    "推荐 300319 目标价 18 元",
    "明天大概率冲高",
    "必涨",
    "会涨",
    "stop at 16.9",
    "buy zone 17.8-18.2",
)


@pytest.mark.parametrize("phrase", REAL_IMPERATIVE_PHRASES)
def test_rerank_blocks_real_imperative_phrases(phrase):
    assert is_rerank_redlined(phrase) is True, phrase


def test_rerank_prediction_needs_a_future_marker():
    """实测踩到：「当日 +12.15% 大幅冲高」是**描述今天已发生**的事，不是预测明天。
    ⇒ 「冲高」这类**时态模糊**的预测词，需句中有指向未来的标记才算预测；
    而「必涨/看多/大概率」无论时态都是断言 ⇒ 直接拦。"""
    assert is_rerank_redlined("1|SZ300489|风险点：当日 +10.24% 已冲高。") is False
    assert is_rerank_redlined("1|SZ300489|当日 +12.15% 大幅冲高。") is False
    # 指向未来 ⇒ 仍是预测 ⇒ 拦
    assert is_rerank_redlined("1|SZ300489|明日大概率冲高。") is True
    assert is_rerank_redlined("1|SZ300489|后续有望冲高。") is True
    # 无时态歧义的断言词无论时态都拦
    assert is_rerank_redlined("1|SZ300489|必涨。") is True
    assert is_rerank_redlined("1|SZ300489|看多。") is True


def test_rerank_negated_advice_is_not_a_violation():
    """否定式（免责声明 / 反向提醒）不是建议 ⇒ 放行。"""
    for text in ("不宜追高。", "不可以买入。", "以上不构成任何涨跌预测或仓位建议。"):
        assert is_rerank_redlined(text) is False, text
