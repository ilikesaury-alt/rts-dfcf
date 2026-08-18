"""次日大涨归因仪表盘测试。

覆盖：
- 去重逻辑：同 (date,symbol) 保留最高分
- 涨幅带分桶：边界值归属正确
- score 分桶
- 维度归因：hit 组 vs 非 hit 组正值率
- 条件 hit 率
- 真实库冒烟
"""

import sqlite3
import tempfile
import os

import pytest

from scanner.nextday_attribution import (
    _load_dedup,
    _hit_stats,
    _attach_prominence,
    MIN_SAMPLE,
    conditional_hit_table,
    gain_band_matrix,
    score_bucket_table,
    dim_compare,
    strategy_table,
)


def _mk_rec(score=50, percent=3.0, next_day=5.0, category="momentum", breakdown=None):
    return {
        "date": "2026-07-01", "symbol": "SZ300001", "name": "测试",
        "category": category, "score": score, "percent": percent,
        "next_day": next_day, "breakdown": breakdown,
    }


def _make_db(path, rows):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE recommendations (id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, "
        "time TEXT, symbol TEXT, name TEXT, category TEXT, score INTEGER, percent REAL, "
        "trend TEXT, score_breakdown TEXT, source TEXT, next_day_pct REAL)"
    )
    for i, r in enumerate(rows):
        conn.execute(
            "INSERT INTO recommendations (date,time,symbol,name,category,score,percent,"
            "trend,score_breakdown,source,next_day_pct) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (r[0], "10:00", r[1], r[2], r[3], r[4], r[5], "up", r[8], "xueqiu", r[6]),
        )
    conn.commit()
    return conn


def test_dedup_keeps_highest_score():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        conn = _make_db(path, [
            ("2026-07-01", "SZ300001", "测试", "momentum", 50, 3.0, 5.0, None, None),
            ("2026-07-01", "SZ300001", "测试", "momentum", 80, 4.0, 5.0, None, None),
            ("2026-07-02", "SZ300001", "测试", "momentum", 60, 3.0, 8.0, None, None),
        ])
        recs = _load_dedup(conn)
        conn.close()
        assert len(recs) == 2, "同(date,symbol)应去重"
        by_date = {r["date"]: r for r in recs}
        assert by_date["2026-07-01"]["score"] == 80, "保留最高分"
    finally:
        os.remove(path)


def test_hit_stats_threshold():
    recs = [_mk_rec(next_day=x) for x in [8.0, 7.0, 6.0, 9.0]]
    hits, hr, avg = _hit_stats(recs, threshold=7.0)
    assert hits == 3, ">=7 算 hit（含 7.0）"
    assert hr == 0.75
    assert abs(avg - 7.5) < 1e-9


def test_gain_band_boundaries():
    recs = [
        _mk_rec(percent=0.5), _mk_rec(percent=1.0), _mk_rec(percent=2.0),
        _mk_rec(percent=4.0), _mk_rec(percent=6.0), _mk_rec(percent=8.0),
        _mk_rec(percent=10.0), _mk_rec(percent=15.0),
    ]
    bands = gain_band_matrix(recs, threshold=7.0)
    by_band = {b["band"]: b["n"] for b in bands}
    assert by_band["<1%"] == 1
    assert by_band["1-2%"] == 1    # 1.0 -> 1-2%
    assert by_band["2-4%"] == 1    # 2.0 -> 2-4%
    assert by_band["4-6%"] == 1    # 4.0 -> 4-6%
    assert by_band["6-8%"] == 1    # 6.0 -> 6-8%
    assert by_band["8-10%"] == 1   # 8.0 -> 8-10%
    assert by_band[">=10%"] == 2   # 10.0 与 15.0 -> >=10%


def test_score_bucket_boundaries():
    recs = [_mk_rec(score=s) for s in [20, 30, 50, 70, 90, 110, 130]]
    buckets = score_bucket_table(recs, threshold=7.0)
    by = {b["bucket"]: b["n"] for b in buckets}
    assert by["<30"] == 1
    assert by["30-50"] == 1
    assert by["50-70"] == 1
    assert by["70-90"] == 1
    assert by["90-110"] == 1
    assert by[">=110"] == 2


def test_small_sample_warn_flag():
    """2026-08-18 样本门槛：组样本 < MIN_SAMPLE 标 warn=True（防噪声行动）。
    8-10% 反转案例（n=41 vs 全期 1184）证明小样本差异大概率是噪声——
    分桶/因子表按 warn 标注，打印层追加「⚠样本不足」。"""
    # n=1 的组 → warn
    bands = gain_band_matrix([_mk_rec(percent=1.0)], threshold=7.0)
    assert all(b["warn"] for b in bands)
    # n>=MIN_SAMPLE 的组 → 无 warn
    big = [_mk_rec(percent=5.0) for _ in range(MIN_SAMPLE)]
    bands2 = gain_band_matrix(big, threshold=7.0)
    b5 = next(b for b in bands2 if b["band"] == "4-6%")
    assert b5["warn"] is False
    # score 分桶与因子表同口径
    buckets = score_bucket_table([_mk_rec(score=60)], threshold=7.0)
    assert buckets[0]["warn"] is True
    rows = conditional_hit_table(big, threshold=7.0)
    assert rows  # 无因子命中时为空，不报错


def test_dim_compare_positive_diff():
    import json
    hits = [_mk_rec(next_day=8.0, breakdown=json.dumps({"validation_bonus": 5}))]
    non = [
        _mk_rec(next_day=2.0, breakdown=json.dumps({})),
        _mk_rec(next_day=1.0, breakdown=json.dumps({})),
    ]
    dims = dim_compare(hits + non, threshold=7.0)
    by = {d["dim"]: d for d in dims}
    assert "validation_bonus" in by
    assert by["validation_bonus"]["hit_pos"] == 1.0
    assert by["validation_bonus"]["non_pos"] == 0.0
    assert abs(by["validation_bonus"]["diff"] - 1.0) < 1e-9


def test_strategy_table_sorts_by_hit_rate():
    recs = [
        _mk_rec(category="rebound", next_day=8.0),
        _mk_rec(category="rebound", next_day=8.0),
        _mk_rec(category="momentum", next_day=1.0),
    ]
    stats = strategy_table(recs, threshold=7.0)
    assert stats[0]["category"] == "rebound", "hit 率高的在前"
    assert stats[0]["hit_rate"] == 1.0


def test_attach_prominence_historical():
    """辨识度按推荐日视角回放：窗口内≥3天+好排名 → True；上榜不足 → False。"""
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE appearances (
        symbol TEXT NOT NULL, name TEXT NOT NULL, date TEXT NOT NULL,
        rank INTEGER, percent REAL, value REAL, UNIQUE(symbol, date))""")
    for d in ("2026-07-01", "2026-07-02", "2026-07-03"):
        conn.execute(
            "INSERT INTO appearances VALUES (?, 'T', ?, 50, 0, 0)",
            ("SZ300001", d))
    conn.commit()
    recs = [
        {"date": "2026-07-03", "symbol": "SZ300001", "next_day": 8.0},
        {"date": "2026-07-03", "symbol": "SZ300002", "next_day": 1.0},
    ]
    _attach_prominence(conn, recs)
    conn.close()
    assert recs[0]["_prominent"] is True, "窗口内 3 天上榜 + 平均排名 50 应判辨识度"
    assert recs[1]["_prominent"] is False, "无上榜记录应为非辨识度"


def test_attach_prominence_no_table_sets_none():
    """无 appearances 表（单测/空库）时置 None，不得误标为非辨识度。"""
    conn = sqlite3.connect(":memory:")
    recs = [{"date": "2026-07-01", "symbol": "SZ300001", "next_day": 5.0}]
    _attach_prominence(conn, recs)
    conn.close()
    assert recs[0]["_prominent"] is None, "无法计算辨识度时应为 None（未知）"


def test_conditional_hit_table_prominence():
    """辨识度/非辨识度因子行 hit 率计算正确。"""
    recs = [
        dict(_mk_rec(next_day=8.0), _prominent=True),
        dict(_mk_rec(next_day=8.0), _prominent=True),
        dict(_mk_rec(next_day=2.0), _prominent=False),
    ]
    rows = conditional_hit_table(recs, threshold=7.0)
    by = {r["factor"]: r for r in rows}
    assert "辨识度(↻反复上榜)" in by, f"缺少辨识度因子行: {list(by)}"
    assert by["辨识度(↻反复上榜)"]["n"] == 2
    assert by["辨识度(↻反复上榜)"]["hit_rate"] == 1.0
    assert "非辨识度" in by
    assert by["非辨识度"]["n"] == 1
    assert by["非辨识度"]["hit_rate"] == 0.0


@pytest.mark.smoke
def test_real_db_smoke():
    from scanner.config import DB_PATH
    if not os.path.exists(DB_PATH):
        return  # 无库时跳过（CI/开发环境）
    conn = sqlite3.connect(DB_PATH)
    recs = _load_dedup(conn, days=30)
    _attach_prominence(conn, recs)
    conn.close()
    assert len(recs) > 0, "真实库应有样本"
    assert strategy_table(recs, 7.0)
    assert gain_band_matrix(recs, 7.0)
    assert score_bucket_table(recs, 7.0)
    assert dim_compare(recs, 7.0)
    assert conditional_hit_table(recs, 7.0), "辨识度因子应可计算"
    assert any(r.get("_prominent") is not None for r in recs), "真实库应能计算辨识度"
    assert any(r.get("_prominent") is True for r in recs), "真实库应存在辨识度票"
