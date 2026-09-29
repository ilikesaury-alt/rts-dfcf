"""v2 管道审查修复的回归测试（2026-09-28 随 v2 池管道退池大幅缩减）：

本文件原有 4 个用例，现存 2 个：
1. ~~pool_pick 平分刷新（dal.save_recommendations 类别例外）~~ → **已删**：该例外分支
   随 v2 池管道移除（无 pool_pick 候选后永不可达，988 行存量也不再更新）。
2. v1 类别同分不覆盖 → **保留**（仍是 dal 的现行语义）。
3. ~~v2_kline_summary 构造轻量 KlineSummary~~ → **已删**：函数随 v2 管道移除。
4. label_all_candidates 对带 kline 的候选真正写入 dip_labels → **保留**
   （scanner/matcher.py 作为研究原料保留，未随 v2 管道删除）。
"""

import sqlite3

import pytest

from scanner.database import save_recommendations
from scanner.matcher import label_all_candidates
from scanner.models import Candidate, KlineSummary, StockInfo


@pytest.fixture
def memory_db():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS recommendations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            time TEXT NOT NULL,
            symbol TEXT NOT NULL,
            name TEXT NOT NULL,
            category TEXT NOT NULL,
            score REAL NOT NULL,
            percent REAL,
            trend TEXT,
            score_breakdown TEXT,
            source TEXT,
            concept TEXT,
            accumulated_pct REAL,
            stale_kline INTEGER DEFAULT 0,
            excluded_reason TEXT DEFAULT '',
            excluded INTEGER DEFAULT 0,
            reversed INTEGER DEFAULT 0,
            UNIQUE(symbol, category, date)
        )
        """
    )
    yield conn
    conn.close()


def _mk_candidate(symbol: str, category: str, percent: float, score: int = 0) -> Candidate:
    stock = StockInfo(
        symbol=symbol,
        name="Test",
        code=symbol,
        percent=percent,
        current=10.0,
        value=10000,
        rank_change=1000,
        rank=1,
    )
    return Candidate(
        stock=stock,
        category=category,
        score=score,
        reason="池选",
        kline=KlineSummary(
            trend="整理",
            accumulated_pct=2.0,
            volume_ratio=1.0,
            bottom_confirmed=False,
            score=0,
            dimensions={},
        ),
    )


def test_v1_equal_score_keeps_first_row(memory_db):
    """v1 类别保持原语义：同分不覆盖（保留当日最高分行做归因）。"""
    save_recommendations(memory_db, [_mk_candidate("300002", "new_face", 5.0, score=20)], [])
    save_recommendations(memory_db, [_mk_candidate("300002", "new_face", 8.0, score=20)], [])
    row = memory_db.execute(
        "SELECT percent FROM recommendations WHERE symbol='300002' AND category='new_face'"
    ).fetchone()
    assert row is not None
    assert row[0] == 5.0


class _FakePoolRow:
    def __init__(self, rank_trend=0, acc5=6.0, bias20=5.0):
        self.rank_trend = rank_trend
        self.acc5 = acc5
        self.bias20 = bias20


def _bars(dates: list[str], closes: list[float], volumes: list[float], percents: list[float]) -> list[dict]:
    return [
        {
            "date": d,
            "open": c,
            "high": c * 1.02,
            "low": c * 0.98,
            "close": c,
            "volume": v,
            "percent": p,
        }
        for d, c, v, p in zip(dates, closes, volumes, percents, strict=True)
    ]


def test_label_all_candidates_writes_labels():
    """带 kline 的候选必须被打上 dip_labels 键（此前 kline=None 全量跳过）。"""
    dates = [f"2026-08-{d:02d}" for d in range(10, 22)]
    today = dates[-1]
    # 近5日跌 ~12% + 今日 +4% → 超跌反转
    closes = [10.0] * 7 + [9.7, 9.4, 9.1, 8.8, 9.15]
    volumes = [100.0] * 12
    percents = [0.5] * 7 + [-3.0, -3.0, -3.0, -3.3, 4.0]

    stock = StockInfo(
        symbol="300003", name="T", code="300003", percent=4.0, current=9.15, value=10000, rank_change=1, rank=1
    )
    c = Candidate(
        stock=stock,
        category="pool_pick",
        score=0,
        reason="池选",
        kline=KlineSummary(
            trend="整理",
            accumulated_pct=0.0,
            volume_ratio=1.0,
            bottom_confirmed=False,
            score=0,
            dimensions={"rank_trend": 0},
        ),
    )
    label_all_candidates([c], {"300003": _bars(dates, closes, volumes, percents)}, today)

    assert c.kline is not None
    assert "dip_labels" in c.kline.dimensions
    assert "超跌反转" in c.kline.dimensions["dip_labels"]
