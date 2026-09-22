"""概念板块驱动聚合单元测试（无网络依赖，全部走 mock / 内存库）。"""
import os
import sqlite3
import tempfile
import time
from unittest.mock import patch

from scanner.concept import (
    _fetch_many,
    _is_noise_board,
    attach_display_boards,
    compute_driving_concepts,
    display_board_map,
    fetch_stock_boards,
)
from scanner.database import get_concepts_cache, save_concepts_cache


class _Pool:
    def __init__(self, symbol, percent, name=""):
        self.symbol = symbol
        self.percent = percent
        self.name = name


def test_noise_board_filter():
    # 地域（"板块"后缀）/ 风格 / 指数成分 → 噪音
    assert _is_noise_board("安徽板块")
    assert _is_noise_board("中盘股")
    assert _is_noise_board("融资融券")
    assert _is_noise_board("创业板综")
    assert _is_noise_board("昨日连板")
    assert _is_noise_board("题材股")
    # 真实概念/行业 → 保留
    assert not _is_noise_board("AIGC概念")
    assert not _is_noise_board("CPO概念")
    assert not _is_noise_board("文化传媒")


def test_noise_board_none_and_empty_safe():
    # None/空串输入不崩溃（API 脏字段防御）
    assert _is_noise_board(None) is False
    assert _is_noise_board("") is False


def test_fetch_stock_boards_parses_and_filters():
    fake = {"ssbk": [
        {"BOARD_NAME": "AIGC概念", "BOARD_RANK": 1},
        {"BOARD_NAME": "安徽板块", "BOARD_RANK": 2},
        {"BOARD_NAME": "中盘股", "BOARD_RANK": 3},
        {"BOARD_NAME": "文化传媒", "BOARD_RANK": 4},
        {"BOARD_NAME": "", "BOARD_RANK": 5},
        {"BOARD_NAME": "AIGC概念", "BOARD_RANK": 6},  # 去重
    ]}
    with patch("scanner.concept.requests.get") as mock_get:
        mock_get.return_value.json.return_value = fake
        boards = fetch_stock_boards("SZ300001")
    assert boards == ["AIGC概念", "文化传媒"]


def test_driving_picks_hot_board():
    """票所属概念中，今日飙升成员最多/最强的概念胜出。"""
    concepts = {
        "SZ300001": ["AIGC概念", "文化传媒"],
        "SZ300002": ["AIGC概念"],
        "SZ300003": ["AIGC概念"],
        "SZ300004": ["文化传媒"],
    }
    pool = [
        _Pool("SZ300001", 5.0),
        _Pool("SZ300002", 8.0),
        _Pool("SZ300003", 10.0),
        _Pool("SZ300004", 3.0),
    ]
    with patch("scanner.concept._collect_concepts", return_value=concepts):
        result = compute_driving_concepts(None, ["SZ300001"], pool)
    assert result["SZ300001"] == "AIGC概念"


def test_driving_intensity_beats_flat_count():
    """同一票所属两个概念：成员涨幅强的概念更符合「推动上涨」语义。"""
    # 板块A：3 成员平均 +10%；板块B：5 成员平均 +3%
    # score(A)=3*2.0=6.0，score(B)=5*1.3=6.5 → B 胜（AIGC概念在票上）
    concepts = {
        "SZ300001": ["强概念", "弱概念"],
        "SZ300002": ["弱概念"],
        "SZ300003": ["弱概念"],
        "SZ300004": ["弱概念"],
        "SZ300005": ["弱概念"],
    }
    pool = [
        _Pool("SZ300001", 5.0),
        _Pool("SZ300002", 3.0),
        _Pool("SZ300003", 3.0),
        _Pool("SZ300004", 3.0),
        _Pool("SZ300005", 3.0),
    ]
    # 让"强概念"只有 SZ300001 自己 + 2 只涨幅高的外部票
    concepts["SZ300010"] = ["强概念"]
    concepts["SZ300011"] = ["强概念"]
    pool.append(_Pool("SZ300010", 12.0))
    pool.append(_Pool("SZ300011", 10.0))
    with patch("scanner.concept._collect_concepts", return_value=concepts):
        result = compute_driving_concepts(None, ["SZ300001"], pool)
    # 强概念 score = 3*(1+9.0/10)=5.7；弱概念 score = 5*(1+3.4/10)=6.7 → 弱概念胜
    # 说明参与度（成员数）仍是主因子，涨幅做次级加成
    assert result["SZ300001"] == "弱概念"


def test_driving_fallback_primary_board():
    """票不在飙升池、所属概念均无飙升成员 → 回退到 F10 首要板块（而非"其他"）。"""
    concepts = {"SZ300001": ["电力设备", "光伏设备"]}
    # 池里没有 SZ300001，也没有任何属于电力设备/光伏设备的成员
    pool = [_Pool("SZ300009", 5.0)]
    concepts["SZ300009"] = ["AIGC概念"]
    with patch("scanner.concept._collect_concepts", return_value=concepts):
        result = compute_driving_concepts(None, ["SZ300001"], pool)
    assert result["SZ300001"] == "电力设备"


def test_fallback_to_classify_sector():
    """无概念归属时回退到 classify_sector（名称关键词），再回退"其他"。"""
    concepts = {"SZ300001": []}
    pool = [_Pool("SZ300001", 5.0, name="半导体测试")]
    with patch("scanner.concept._collect_concepts", return_value=concepts):
        result = compute_driving_concepts(None, ["SZ300001"], pool)
    assert result["SZ300001"] == "半导体"

    concepts2 = {"SZ300002": []}
    pool2 = [_Pool("SZ300002", 5.0, name="某某股份")]
    with patch("scanner.concept._collect_concepts", return_value=concepts2):
        result2 = compute_driving_concepts(None, ["SZ300002"], pool2)
    assert result2["SZ300002"] == "其他"


def test_concepts_cache_roundtrip():
    fd, path = tempfile.mkstemp()
    os.close(fd)
    try:
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE IF NOT EXISTS concept_cache ("
                     "symbol TEXT PRIMARY KEY, concepts TEXT NOT NULL, updated TEXT NOT NULL)")
        save_concepts_cache(conn, {"SZ300001": ["AIGC概念", "CPO概念"]})
        got = get_concepts_cache(conn, ["SZ300001"])
        assert got["SZ300001"] == ["AIGC概念", "CPO概念"]
        assert get_concepts_cache(conn, ["SZ300002"]) == {}
        conn.close()
    finally:
        os.remove(path)


def test_concepts_cache_expired():
    fd, path = tempfile.mkstemp()
    os.close(fd)
    try:
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE IF NOT EXISTS concept_cache ("
                     "symbol TEXT PRIMARY KEY, concepts TEXT NOT NULL, updated TEXT NOT NULL)")
        conn.execute("INSERT INTO concept_cache VALUES (?, ?, ?)",
                     ("SZ300009", '["旧概念"]', "2020-01-01T00:00:00"))
        conn.commit()
        got = get_concepts_cache(conn, ["SZ300009"], ttl_days=7)
        assert got == {}
        conn.close()
    finally:
        os.remove(path)


# ── 展示行「板块」列取值单源（2026-09-22，飙升区 A/B 两段共用）────────────────
# 需求原文是「复用 v1 里的板块名」。三级回退链各自的守卫如下；「同票同名」由
# test_display_board_map_level1_matches_v1_entry_sector 钉死。


def _board_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE concept_cache (symbol TEXT, concepts TEXT, updated TEXT)")
    conn.commit()
    return conn


def test_display_board_map_level1_reads_same_source_as_v1():
    """①级必须走 `get_today_recommendations` —— v1 池选 `_entry_sector` 的①级同源。

    断言调用而非只断言取值：只看取值的话，换成任何一个碰巧返回同名的实现也会绿，
    「复用 v1 板块名」这条约束就没了守卫。
    """
    conn = _board_conn()
    rec = [{"symbol": "SZ300001", "concept": "华为概念"}]
    with patch("scanner.concept.get_today_recommendations", return_value=rec) as mock_rec:
        got = display_board_map(conn, ["SZ300001"], {"SZ300001": "特锐德"}, fetch=False)
    mock_rec.assert_called_once_with(conn)
    assert got["SZ300001"] == "华为概念"


def test_display_board_map_level1_matches_v1_entry_sector():
    """同票同名：同一条 entry 喂给 v1 的 `_entry_sector` 与本函数①级，结果必须一致。

    这是「A/B 段与 v1 池选同屏并列时，同一只票不出现两个板块名」的可执行表述。
    """
    from scanner.view.model import _entry_sector

    conn = _board_conn()
    entry = {"symbol": "SZ300001", "name": "特锐德", "concept": "华为概念"}
    with patch("scanner.concept.get_today_recommendations", return_value=[entry]):
        got = display_board_map(conn, ["SZ300001"], {"SZ300001": "特锐德"}, fetch=False)
    assert got["SZ300001"] == _entry_sector(entry)


def test_display_board_map_level2_takes_first_cached_board():
    """②级：concept_cache 命中 → 取**首个**板块（刻意不跑 `_driving_for` 共振聚合，
    理由见 display_board_map docstring：B 段票恒不是推动成员，聚合对它也只到 boards[0]）。"""
    conn = _board_conn()
    save_concepts_cache(conn, {"SZ300002": ["CPO概念", "光通信"]})
    with patch("scanner.concept.get_today_recommendations", return_value=[]):
        got = display_board_map(conn, ["SZ300002"], {"SZ300002": "某票"}, fetch=False)
    assert got["SZ300002"] == "CPO概念"


def test_display_board_map_level3_name_keyword_last_resort():
    """③级：两级都落空 → 名称关键词（v1 `_entry_sector` 末级同款），无命中给「其他」。"""
    conn = _board_conn()
    names = {"SZ300003": "半导体设备", "SZ300004": "毫无关键词"}
    with patch("scanner.concept.get_today_recommendations", return_value=[]):
        got = display_board_map(conn, list(names), names, fetch=False)
    assert got["SZ300003"] == "半导体"
    assert got["SZ300004"] == "其他"  # 不是「」也不是 —：确实没匹配上，与取数失败不同


def test_display_board_map_fetch_false_never_touches_network():
    """`fetch=False`（A 段路径）绝不发 F10 —— 榜内票的缓存由主线维护，本区不重复拉。

    断言 `_fetch_many` 未被调用是**非空断言**：一旦哪天把 fetch 传成 True，
    本用例会立刻变红（True 分支必经 `_collect_concepts` → `_fetch_many`）。
    """
    conn = _board_conn()
    with (
        patch("scanner.concept.get_today_recommendations", return_value=[]),
        patch("scanner.concept._fetch_many") as mock_fetch,
    ):
        got = display_board_map(conn, ["SZ300005"], {"SZ300005": "某票"}, fetch=False)
    mock_fetch.assert_not_called()
    assert got["SZ300005"] == "其他"  # 降级链走完，不留空


def test_display_board_map_empty_symbols_returns_empty():
    conn = _board_conn()
    assert display_board_map(conn, [], {}, fetch=False) == {}


def test_display_board_map_survives_broken_recommendations_table():
    """①级读取失败只降级、不抛（fail-open 契约）—— 本层失败不该让整区产出消失。"""
    conn = sqlite3.connect(":memory:")  # 无任何表
    got = display_board_map(conn, ["SZ300006"], {"SZ300006": "半导体设备"}, fetch=False)
    assert got["SZ300006"] == "半导体"


def test_attach_display_boards_writes_sector_in_place():
    """就地写回（渲染层只读不算）：`sector` 空 → 有值。"""

    class _Row:
        symbol = "SZ300001"
        name = "特锐德"
        sector = ""

    conn = _board_conn()
    rows = [_Row()]
    with patch("scanner.concept.get_today_recommendations", return_value=[{"symbol": "SZ300001", "concept": "华为概念"}]):
        attach_display_boards(conn, rows, fetch=False)
    assert rows[0].sector == "华为概念"


def test_attach_display_boards_empty_input_is_noop():
    conn = _board_conn()
    attach_display_boards(conn, [], fetch=False)  # 不抛即可


def test_attach_display_boards_leaves_sector_blank_on_total_failure():
    """板块整体取不到 → 留空（渲染为 `—`），**不**伪装成「其他」。"""
    conn = _board_conn()

    class _Row:
        symbol = "SZ300007"
        name = ""
        sector = "预置值"

    rows = [_Row()]
    with (
        patch("scanner.concept.get_today_recommendations", return_value=[]),
        patch("scanner.concept.classify_sector", return_value=""),
    ):
        attach_display_boards(conn, rows, fetch=False)
    assert rows[0].sector == ""


def test_fetch_many_deadline_returns_partial():
    """回归（2026-08-20）：_fetch_many 必须受阶段限时约束，超时后返回已收集部分，
    不能无限等待挂起线程（此前 as_completed 无 timeout，首次/DB 过期日最坏
    ceil(N/8)×8s 阻塞主扫描线程，违反 KLINE_FETCH_DEADLINE 同族「单轮有界」承诺）。"""
    def _slow(sym):
        time.sleep(10)
        return [f"概念{sym}"]

    with patch("scanner.concept.fetch_stock_boards", side_effect=_slow):
        t0 = time.time()
        got = _fetch_many(["SZ300001", "SZ300002"], deadline=time.time() + 0.5)
    elapsed = time.time() - t0
    assert elapsed < 5, f"阶段限时失效，耗时 {elapsed:.1f}s（应受 deadline 约束）"
    assert got == {}  # 所有任务都挂起 → 超时后空结果（fail-open，本轮无概念）
    assert isinstance(got, dict)
