"""walk-forward 滚动检验测试：窗口切分 / 翻转判定 / 样本守卫 / 档位单调性结构。"""
import sqlite3

from scanner.walkforward import (
    evaluate_factor,
    load_rows,
    render,
    run,
    walkforward_windows,
)


def _mk_db(rows):
    """rows: tuple 行或 dict 行（_row 输出）混用均可。"""
    cols = ("date", "time", "symbol", "name", "category", "score", "percent",
            "next_day_pct", "accumulated_pct", "score_breakdown")
    tuples = [
        tuple(r[c] for c in cols) + (r.get("excluded", 0),)
        if isinstance(r, dict) else r
        for r in rows
    ]
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE recommendations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT, time TEXT, symbol TEXT, name TEXT,
            category TEXT, score REAL, percent REAL,
            next_day_pct REAL, accumulated_pct REAL,
            score_breakdown TEXT, excluded INTEGER DEFAULT 0
        )
    """)
    conn.executemany(
        "INSERT INTO recommendations (date, time, symbol, name, category, score,"
        " percent, next_day_pct, accumulated_pct, score_breakdown, excluded)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        tuples,
    )
    conn.commit()
    conn.row_factory = sqlite3.Row
    return conn


def _row(date, sym, pct_rec, nd, category="momentum", accum=None, sb="{}"):
    """dict 形状的推荐行（与 load_rows 输出一致）。"""
    return {"date": date, "time": "13:00", "symbol": sym, "name": "T",
            "category": category, "score": 50.0, "percent": pct_rec,
            "next_day_pct": nd, "accumulated_pct": accum, "score_breakdown": sb}


class TestWalkforwardWindows:
    def test_seamless_rolling(self):
        dates = [f"d{i:02d}" for i in range(10)]
        wins = walkforward_windows(dates, train_days=4, test_days=2)
        # i=4,6,8 → 3 个窗口；train/test 不重叠、test 无缝衔接
        assert len(wins) == 3
        assert wins[0] == (["d00", "d01", "d02", "d03"], ["d04", "d05"])
        assert wins[1][0] == ["d02", "d03", "d04", "d05"]
        assert wins[1][1] == ["d06", "d07"]
        assert wins[2][1] == ["d08", "d09"]

    def test_insufficient_data(self):
        assert walkforward_windows(["a", "b", "c"], train_days=4, test_days=2) == []

    def test_dedup_dates(self):
        # 重复日期（同日多轮）按唯一交易日计
        dates = ["a", "a", "b", "b", "c", "c", "d", "d", "e", "e", "f", "f"]
        wins = walkforward_windows(dates, train_days=2, test_days=2)
        assert len(wins) == 2
        assert wins[0][1] == ["c", "d"]


class TestEvaluateFactor:
    def _rows(self):
        # 基线 hit ~50%：每日 8 行、4 行 hit（train 窗 5 日=40 行 ≥ MIN_BASE_SAMPLE）
        rows = []
        for i in range(80):
            date = f"2026-01-{i % 10 + 1:02d}"
            nd = 10.0 if i % 2 == 0 else 0.0
            rows.append(_row(date, f"S{i:03d}", 1.0, nd))
        return rows

    def test_direction_stable_no_flip(self):
        rows = self._rows()
        train = {f"2026-01-{i:02d}" for i in range(1, 6)}
        test = {f"2026-01-{i:02d}" for i in range(6, 11)}
        # 因子恒真 → delta 恒 0，无翻转
        r = evaluate_factor("all", rows, lambda e: True, train, test, threshold=7.0)
        assert r["flip"] is False
        assert r["train_delta"] == 0 and r["test_delta"] == 0

    def test_flip_detected(self):
        rows = []
        # train 窗（01-01~01-05）：因子行全 hit（delta 强正）
        for i in range(50):
            rows.append(_row("2026-01-01", f"A{i:03d}", 1.0, 10.0))
            rows.append(_row("2026-01-01", f"B{i:03d}", 1.0, 0.0))
        # test 窗（01-06）：因子行全 miss（delta 强负）→ 方向反转
        for i in range(50):
            rows.append(_row("2026-01-06", f"C{i:03d}", 1.0, 0.0))
            rows.append(_row("2026-01-06", f"D{i:03d}", 1.0, 10.0))
        train = {"2026-01-01"}
        test = {"2026-01-06"}
        r = evaluate_factor("rev", rows, lambda e: e["symbol"][0] in "AC",
                            train, test, threshold=7.0)
        assert r["flip"] is True
        assert r["train_delta"] > 0 and r["test_delta"] < 0

    def test_small_sample_guarded(self):
        rows = [_row("2026-01-01", "S001", 1.0, 10.0)]
        r = evaluate_factor("tiny", rows, lambda e: True,
                            {"2026-01-01"}, {"2026-01-01"}, threshold=7.0)
        assert "样本不足" in r["note"] and r["flip"] is False


class TestRun:
    def test_end_to_end_structure_and_tier_keys(self):
        rows = []
        for d in range(1, 9):  # 8 个交易日 → train=4/test=2 → 2 个窗口
            date = f"2026-02-{d:02d}"
            rows.append(_row(date, f"S{d:03d}", 1.0, 10.0 if d % 2 else 0.0,
                             category="rebound" if d % 3 == 0 else "momentum"))
        conn = _mk_db(rows)
        result = run(conn, train_days=4, test_days=2)
        assert result["total"] == 8
        assert len(result["windows"]) == 2
        assert {f["factor"] for f in result["factors"]} >= {"rebound 类别", "🎯 完整画像"}
        for tw in result["tier_windows"]:
            assert set(tw["tiers"].keys()) == {0, 1, 2, 3}

    def test_load_rows_dedup_and_excluded(self):
        db_rows = [
            ("2026-03-01", "13:00", "S1", "T", "momentum", 50.0, 1.0, 10.0, None, "{}", 0),
            ("2026-03-01", "14:00", "S1", "T", "momentum", 60.0, 1.0, 5.0, None, "{}", 0),
            ("2026-03-01", "13:00", "S2", "T", "momentum", 50.0, 1.0, 10.0, None, "{}", 0),
            ("2026-03-01", "14:00", "S3", "T", "momentum", 60.0, 1.0, 5.0, None, "{}", 1),
        ]
        conn = _mk_db(db_rows)
        loaded = load_rows(conn)
        syms = [r["symbol"] for r in loaded]
        assert syms.count("S1") == 1  # 同票同日去重
        assert "S3" not in syms       # excluded=1 不参与

    def test_render_contains_verdicts(self):
        rows = []
        for d in range(1, 9):
            date = f"2026-02-{d:02d}"
            rows.append(_row(date, f"S{d:03d}", 1.0, 10.0 if d % 2 else 0.0))
        conn = _mk_db(rows)
        out = render(run(conn, train_days=4, test_days=2))
        assert "Walk-forward" in out
        assert "档位单调性" in out
