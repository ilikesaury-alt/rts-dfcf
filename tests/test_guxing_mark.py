"""妖股名单展示标记（2026-09-30）测试 —— **静态名单匹配**口径。

三条纪律：
  1. **纯匹配**：判定只查 config_scoring.GUXING_WATCHLIST，不做任何统计计算
     （前版基于 daily_kline 动态算档案，已按用户决策 2026-09-30 改为名单匹配）。
  2. **代码优先 / 名称兼底**：代码命中即算命中；名称匹配受 GUXING_MATCH_BY_NAME 控制。
  3. **纯展示**：不改排序/评分/落库，不进 push_gate；标记是纯文本单字不用 emoji。

⚠ 名单**没有样本外预测力**（walk-forward 0/17），本测试锁的是「名单与匹配口径」，
不是它的有效性 —— 若有人日后想把它当门用，这些测试不构成放行依据。
"""

from __future__ import annotations

from scanner.config_scoring import (
    GUXING_MATCH_BY_NAME,
    GUXING_THEMES,
    GUXING_WATCHLIST,
    GUXING_YEARS,
)
from scanner.ranking import GUXING_BY_NAME, guxing_mark, is_guxing_watched, normalize_symbol


# ── 名单自身的数据卫生 ──────────────────────────────────────────────────────
def test_watchlist_is_wellformed():
    """名单必须代码↔名称成对、无重复、无空值、代码带交易所前缀。"""
    assert GUXING_WATCHLIST, "名单不应为空"
    for code, name in GUXING_WATCHLIST.items():
        assert code and code[:2] in ("SH", "SZ"), f"代码前缀异常：{code!r}"
        assert len(code) == 8, f"代码长度异常（应为 SZ+6 位）：{code!r}"
        assert name and name.strip(), f"名单 {code} 名称为空"
        assert not any(ch in name for ch in "()（）"), f"名单 {code} 名称含括号（疑似脏数据）：{name!r}"
    assert len(set(GUXING_WATCHLIST)) == len(GUXING_WATCHLIST), "代码重复"


def test_watchlist_is_chinext_only():
    """现行名单只含创业板 300/301 —— 与系统监控池（filter_gem_stocks 300/301）同口径。

    主板妖股（000/002/600/603…）在 v1 池选永远不会被标到，收进名单只会让人误以为
    「名单收全了」——故锁死口径，防止日后无意识混入主板票。
    """
    for code in GUXING_WATCHLIST:
        assert code.startswith(("SZ300", "SZ301")), f"名单混入非创业板代码：{code}"


def test_known_renames_use_current_names():
    """更名票必须用**现名**，否则名称兜底匹配不到（代码兜底仍在，但名称索引会失效）。"""
    assert GUXING_WATCHLIST.get("SZ300188") == "国投智能"  # 原「美亚柏科」
    assert GUXING_WATCHLIST.get("SZ301550") == "斯菱智驱"  # 原「斯菱股份」
    assert GUXING_WATCHLIST.get("SZ300776") == "帝尔激光"  # 原「腾讯用户(帝尔激光)」


def test_duplicate_name_is_at_most_the_flagged_dup():
    """名单允许的唯一重名 = 301114/300114「中航电测」（源文件疑似重复录入）。

    已用注释标注待人工确认；此测试锁住「不得出现其他重名」。
    """
    dupes = {n for n in GUXING_WATCHLIST.values() if list(GUXING_WATCHLIST.values()).count(n) > 1}
    assert dupes <= {"中航电测"}, f"出现未登记的重复名称：{dupes}"


def test_side_tables_stay_in_sync():
    """年份/题材表必须与名单严格同键 —— 少一条就是名单维护时漏同步。"""
    assert set(GUXING_YEARS) == set(GUXING_WATCHLIST)
    assert set(GUXING_THEMES) == set(GUXING_WATCHLIST)
    for code, y in GUXING_YEARS.items():
        assert y.strip(), f"{code} 年份为空"
    for code, t in GUXING_THEMES.items():
        assert t.strip(), f"{code} 题材为空"


def test_name_index_is_reverse_of_watchlist():
    """名称反查表与名单一致（重名时以先出现者为准，不得凭空多出条目）。"""
    assert set(GUXING_BY_NAME) <= set(GUXING_WATCHLIST.values())
    for name, code in GUXING_BY_NAME.items():
        assert GUXING_WATCHLIST[code] == name


# ── 代码归一（源文件是裸 6 位，上游可能带后缀）─────────────────────────────
def test_normalize_symbol_forms():
    """裸 6 位 / 小写 / 带市场后缀 / 带空白的代码都要归一到名单键形。"""
    assert normalize_symbol("300641") == "SZ300641"
    assert normalize_symbol("600018") == "SH600018"
    assert normalize_symbol("sz300641") == "SZ300641"
    assert normalize_symbol("  SZ300641  ") == "SZ300641"
    assert normalize_symbol("300641.SZ") == "SZ300641"
    assert normalize_symbol(None) == ""
    assert normalize_symbol("") == ""
    assert normalize_symbol("非代码") == "非代码"


def test_bare_code_from_source_file_matches():
    """调研名单原文件是裸 6 位代码 —— 该形态必须能命中。"""
    code = next(c for c in GUXING_WATCHLIST if c.startswith("SZ30"))
    bare = code[2:]
    assert is_guxing_watched(bare) is True
    assert guxing_mark(bare) == "妖"


# ── 匹配口径 ────────────────────────────────────────────────────────────────
def test_code_match_hits():
    """名单内代码 → 命中，且不需要传名称。"""
    for code in GUXING_WATCHLIST:
        assert is_guxing_watched(code) is True
        assert guxing_mark(code) == "妖"


def test_code_match_is_case_and_space_insensitive():
    """代码大小写/空白应归一 —— 避免上游格式差异导致漏标。"""
    code = next(iter(GUXING_WATCHLIST))
    assert is_guxing_watched(code.lower()) is True
    assert is_guxing_watched(f"  {code}  ") is True


def test_chinext_prefixes_both_present():
    """名单应同时覆盖 300 与 301 两段（现行清单 26 + 7）。"""
    assert any(c.startswith("SZ300") for c in GUXING_WATCHLIST)
    assert any(c.startswith("SZ301") for c in GUXING_WATCHLIST)


def test_name_fallback_hits_when_code_missing():
    """代码不在名单但名称在 → 名称兼底应命中。"""
    name = next(iter(GUXING_WATCHLIST.values()))
    miss_sym = next(c for c in ("SZ399999", "SH688999") if c not in GUXING_WATCHLIST)
    assert is_guxing_watched(miss_sym, name) is True
    assert guxing_mark(miss_sym, name) == "妖"


def test_name_fallback_respects_switch():
    """GUXING_MATCH_BY_NAME=False 时只认代码。"""
    name = next(iter(GUXING_WATCHLIST.values()))
    miss_sym = next(c for c in ("SZ399999", "SH688999") if c not in GUXING_WATCHLIST)
    expect = GUXING_MATCH_BY_NAME
    assert is_guxing_watched(miss_sym, name) is expect


def test_non_watched_misses():
    """名单外的票 → 不标。用一个保证不在名单里的代码。"""
    miss_sym = next(c for c in ("SZ399999", "SH688999", "SZ300001") if c not in GUXING_WATCHLIST)
    assert is_guxing_watched(miss_sym, "不存在的票") is False
    assert guxing_mark(miss_sym, "不存在的票") == ""


def test_none_inputs_fail_closed():
    """None / 空串不抛异常，一律不标。"""
    assert is_guxing_watched(None) is False
    assert is_guxing_watched(None, None) is False
    assert is_guxing_watched("") is False
    assert is_guxing_watched("", "") is False
    assert guxing_mark(None) == ""
    assert guxing_mark("", "") == ""


# ── 展示形态 ────────────────────────────────────────────────────────────────
def test_mark_is_plain_text_not_emoji():
    """纯文本单字（跟「稳」同风格），不用 emoji —— 用户 2026-09-30 要求不扎眼。"""
    code = next(iter(GUXING_WATCHLIST))
    m = guxing_mark(code)
    assert m == "妖"
    assert "🔥" not in m
    assert len(m) == 1  # 单字，不带任何统计后缀


# ── 纯展示守护 ──────────────────────────────────────────────────────────────
def test_matching_is_side_effect_free():
    """判定必须是纯函数：不改入参、不写库、不返回可被误用的对象。"""
    row = {"symbol": next(iter(GUXING_WATCHLIST)), "name": "唯特偶", "score": 77, "category": "ST"}
    before = dict(row)
    is_guxing_watched(row["symbol"], row["name"])
    guxing_mark(row["symbol"], row["name"])
    assert row == before  # 未回写排序/评分键
    assert guxing_mark(row["symbol"], row["name"]) in ("妖", "")  # 幂等


def test_not_consumed_by_push_gate():
    """妖标记不得成为推送门的判据 —— push_gate 只认类别先验（AGENTS 纪律）。

    这里锁的是「push_gate 模块不导入 guxing」，防止日后有人把它接进过滤门。
    """
    import inspect

    from scanner import push_gate

    src = inspect.getsource(push_gate)
    assert "guxing" not in src.lower() and "妖" not in src, "push_gate 引用了妖标记"
