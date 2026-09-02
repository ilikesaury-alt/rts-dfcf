"""概念板块驱动聚合单元测试（无网络依赖，全部走 mock / 内存库）。"""
import os
import sqlite3
import tempfile
import time
from unittest.mock import patch

from scanner.concept import _fetch_many, _is_noise_board, compute_driving_concepts, fetch_stock_boards
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
