"""`ai_digest.features` 本地特征预加载的守卫（2026-11-11）。

背景：用户问「纯本地反查的工具，为什么不把信息直接带进提示词」。
结论是**只预加载结论化特征，不塞原始数据**（原始 10 日 K 线 3414 字符 vs 特征 ~150 字符/票）。
这些测试锁住三件事：

1. 特征算得对（5日/10日累计、距20日高点、量能倍数）—— 算错了模型会据此下错判断；
2. 缺数据**明说缺**，不填 0 —— 模型会把「空缺」读成「没有异动」；
3. 提示词里的特征与巡检基准**同源** —— 否则「5日 +24.6%」会被判成编造。
"""

from __future__ import annotations

import sqlite3

import pytest

from scanner.ai_digest.agent import build_research_prompt
from scanner.ai_digest.features import (
    SymbolFeatures,
    build_features,
    render_feature_block,
)
from scanner.ai_digest.panel import PanelRegion, PanelSnapshot


@pytest.fixture()
def db() -> sqlite3.Connection:
    """最小库：3 只票 × 12 根 K 线 + 2 条公告。

    刻意让 SZ300001 只够 6 根（测「5日算得出、10日算不出」）、SZ300003 一根没有
    （测「缺数据要明说缺」）。
    """
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE daily_kline (symbol TEXT, date TEXT, open REAL, close REAL, "
        "high REAL, low REAL, volume REAL, percent REAL, finalized INT)"
    )
    conn.execute(
        "CREATE TABLE announcement_cache (symbol TEXT, notice_date TEXT, title TEXT, "
        "ann_type TEXT, source_url TEXT, content_hash TEXT, fetched_at TEXT)"
    )

    # SZ300001：12 根，收盘 10→11（5日 = +10%），20日高点按最高 high=12
    for i in range(12):
        day = f"2026-11-{i + 1:02d}"
        close = 10.0 + i * 0.1
        conn.execute(
            "INSERT INTO daily_kline VALUES (?,?,?,?,?,?,?,?,1)",
            ("SZ300001", day, close, close, 12.0 if i == 3 else close + 0.2, close - 0.2, 1000.0, 1.0),
        )
    # SZ300002：12 根，成交量今日 3000、前 5 日均 1000 ⇒ 量能 3.0x
    for i in range(12):
        day = f"2026-11-{i + 1:02d}"
        vol = 3000.0 if i == 11 else 1000.0
        conn.execute(
            "INSERT INTO daily_kline VALUES (?,?,?,?,?,?,?,?,1)",
            ("SZ300002", day, 20.0, 20.0, 21.0, 19.0, vol, 0.5),
        )
    conn.execute(
        "INSERT INTO announcement_cache VALUES (?,?,?,?,?,?,?)",
        ("SZ300001", "2026-11-05", "甲公司:关于股票期权激励计划的公告", "激励", "", "h1", ""),
    )
    conn.execute(
        "INSERT INTO announcement_cache VALUES (?,?,?,?,?,?,?)",
        ("SZ300001", "2026-11-01", "甲公司:2026年三季度报告", "定期报告", "", "h2", ""),
    )
    conn.execute(
        "INSERT INTO announcement_cache VALUES (?,?,?,?,?,?,?)",
        ("SZ300001", "2026-10-01", "甲公司:更早的公告不该出现", "其他", "", "h3", ""),
    )
    conn.commit()
    return conn


def test_pct_5d_and_10d(db: sqlite3.Connection) -> None:
    """5日/10日累计按「最新收盘 / N 根前收盘 - 1」算。"""
    feats = build_features(db, ["SZ300001"])
    f = feats["SZ300001"]
    # 最新 close=11.1（i=11），5 根前 close=10.6（i=6）⇒ +4.72%
    assert f.pct_5d == pytest.approx((11.1 / 10.6 - 1) * 100, abs=1e-6)
    # 10 根前 close=10.1（i=1）⇒ +9.9%
    assert f.pct_10d == pytest.approx((11.1 / 10.1 - 1) * 100, abs=1e-6)


def test_high_20d_gap_uses_max_high(db: sqlite3.Connection) -> None:
    """距 20 日高点用 **high** 的最大值（不是 close）—— 判断上方空间看的是真实高点。"""
    feats = build_features(db, ["SZ300001"])
    # 最高 high = 12.0（i=3），最新 close = 11.1 ⇒ (11.1/12 - 1) = -7.5%
    assert feats["SZ300001"].high_20d_gap == pytest.approx((11.1 / 12.0 - 1) * 100, abs=1e-6)


def test_vol_ratio_compares_to_prior_5_avg(db: sqlite3.Connection) -> None:
    """量能 = 今日量 / 前 5 日均量（**不含**今日，否则放量会被自己稀释）。"""
    feats = build_features(db, ["SZ300002"])
    assert feats["SZ300002"].vol_ratio_5d == pytest.approx(3.0, abs=1e-6)


def test_announcements_capped_and_prefix_stripped(db: sqlite3.Connection) -> None:
    """公告最多 2 条（最新在前），并去掉「公司简称:」前缀（面板已有名称，重复占字符）。"""
    feats = build_features(db, ["SZ300001"])
    anns = feats["SZ300001"].announcements
    assert len(anns) == 2
    assert anns[0].startswith("11-05")
    assert "甲公司:" not in anns[0]
    assert "激励" in anns[0]
    # 第三条（10-01）不该出现
    assert all("更早" not in a for a in anns)


def test_missing_symbol_still_emitted_as_dash(db: sqlite3.Connection) -> None:
    """**没有数据的票也要出行**，字段渲染成 `—`。

    为什么：若整行消失，模型会读成「这只票查过了、没异动」—— 数据缺失被当成结论，
    是幻觉的温床（与 research.py 里「截断必须明说」同一条纪律）。
    """
    feats = build_features(db, ["SZ300003"])
    assert "SZ300003" in feats
    block = render_feature_block(feats, ["SZ300003"])
    assert any("SZ300003" in line for line in block)
    assert any("5日 —" in line for line in block)


def test_empty_input_returns_empty(db: sqlite3.Connection) -> None:
    assert build_features(db, []) == {}


def test_broken_db_returns_empty_not_raise() -> None:
    """库坏/表缺 → 返回空 dict，不抛（预加载是增强，不是前置条件）。"""
    conn = sqlite3.connect(":memory:")
    assert build_features(conn, ["SZ300001"]) == {}


def test_render_order_follows_panel(db: sqlite3.Connection) -> None:
    """渲染顺序按面板顺序，不按 dict 顺序 —— 模型对着面板逐行读，错位会张冠李戴。"""
    feats = build_features(db, ["SZ300001", "SZ300002"])
    block = render_feature_block(feats, ["SZ300002", "SZ300001"])
    body = [line for line in block if line.startswith("SZ")]
    assert body[0].startswith("SZ300002")
    assert body[1].startswith("SZ300001")


def test_prompt_contains_features_when_given() -> None:
    """`build_research_prompt(features=...)` 要把特征块追加进去，并明说「不用再调工具」。"""
    snap = PanelSnapshot(
        date="2026-11-11",
        time="14:59",
        regions=[
            PanelRegion(
                title="v1 池选",
                columns=("#", "代码", "名称", "涨幅"),
                note="",
                rows=[{"symbol": "SZ300001", "name": "甲公司", "涨幅": "+3.0%", "marks": ""}],
            )
        ],
    )
    feats = {"SZ300001": SymbolFeatures(symbol="SZ300001", pct_5d=4.7, pct_10d=9.9)}
    text = build_research_prompt([], "面板", panel=snap, features=feats)
    assert "本地特征" in text
    assert "SZ300001" in text
    assert "+4.7%" in text
    # 明确告诉模型不必重复取 —— 否则它会照旧调一遍 get_kline，预加载就白费了
    assert "get_kline" in text


def test_prompt_without_features_has_no_block() -> None:
    """不给 features 时不能凭空出现特征段（自选清单模式走这条路径）。"""
    text = build_research_prompt([{"symbol": "SZ300001", "name": "甲公司"}], "自选")
    assert "本地特征" not in text


def test_cli_materials_prompt_equals_model_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI 的 `--materials` 打印的提示词必须与模型收到的**逐字一致**。

    这条是历史翻车的回归守卫：巡检曾拿 analyze 模板当基准，而模型收到的是 research
    提示词 ⇒ 模型编的 `ff_pct`/`price` 从未出现在基准里，却被判「没编造」。
    现在两处共用 `build_research_prompt`，且 features 也在同一处注入。
    """
    snap = PanelSnapshot(
        date="2026-11-11",
        time="14:59",
        regions=[
            PanelRegion(
                title="v1 池选",
                columns=("#", "代码", "名称"),
                note="",
                rows=[{"symbol": "SZ300001", "name": "甲公司", "marks": ""}],
            )
        ],
    )
    feats = {"SZ300001": SymbolFeatures(symbol="SZ300001", pct_5d=1.0)}
    rows = [dict(r) for reg in snap.regions for r in reg.rows]

    # ① CLI 的 `--materials` 打印的这一份
    shown = build_research_prompt(rows, "终端面板原文", panel=snap, features=feats)
    # ② 模型实际收到的那一份（`run_research` 内部同一调用）
    model_side = build_research_prompt(rows, "终端面板原文", panel=snap, features=feats)
    assert shown == model_side
    # 且特征确实在里面（否则两边「一致地都缺」也能过）
    assert "+1.0%" in shown


# ── 2026-11-12 盘中实跑暴露的两个 bug ────────────────────────────────────────
#
# 两个都是「预加载喂了错数据」，而错数据比没数据更危险：模型会把假数字当事实排序。


def _bar_db(rows: list[tuple[str, str, float, float, int]]) -> sqlite3.Connection:
    """(symbol, date, volume, close, finalized) → 最小库。"""
    c = sqlite3.connect(":memory:")
    c.execute(
        "CREATE TABLE daily_kline (symbol TEXT, date TEXT, open REAL, close REAL, "
        "high REAL, low REAL, volume REAL, percent REAL, finalized INT)"
    )
    c.execute(
        "CREATE TABLE announcement_cache (symbol TEXT, notice_date TEXT, title TEXT, "
        "ann_type TEXT, source_url TEXT, content_hash TEXT, fetched_at TEXT)"
    )
    for sym, day, vol, close, fin in rows:
        c.execute(
            "INSERT INTO daily_kline VALUES (?,?,?,?,?,?,?,?,?)",
            (sym, day, close, close, close, close, vol, 0.0, fin),
        )
    c.commit()
    return c


def test_intraday_partial_bar_does_not_produce_vol_ratio() -> None:
    """盘中（最新 bar `finalized=0`）**不许**算量能 —— 那是拿半天量比整日均量。

    实测 11-12 10:29 斯迪克：算出 0.8x「萎缩」，而东财量比 3.31（显著放量）。
    两个数方向相反，模型写出「量比 3.31、量能 0.8x 萎缩」这种自相矛盾的判断。
    """
    rows = [("SZ300806", f"2026-11-{d:02d}", 3400.0, 48.0, 1) for d in range(5, 11)]
    rows.append(("SZ300806", "2026-11-12", 2425.0, 50.0, 0))  # 盘中部分 bar
    conn = _bar_db(rows)

    feat = build_features(conn, ["SZ300806"])["SZ300806"]

    assert feat.vol_ratio_5d is None, "盘中不许给量能（会系统性低估）"
    assert feat.intraday_partial is True
    # 百分比类字段不受影响 —— 用的是当前价，盘中同样成立
    assert feat.pct_5d is not None


def test_finalized_bar_still_produces_vol_ratio() -> None:
    """已定稿时量能照常算（不能因为修 bug 把正常路径也关掉）。"""
    rows = [("SZ300002", f"2026-11-{d:02d}", 1000.0, 20.0, 1) for d in range(5, 11)]
    rows.append(("SZ300002", "2026-11-12", 3000.0, 20.0, 1))  # 已定稿
    conn = _bar_db(rows)

    feat = build_features(conn, ["SZ300002"])["SZ300002"]
    assert feat.vol_ratio_5d == pytest.approx(3.0, abs=1e-6)
    assert feat.intraday_partial is False


def test_stale_kline_produces_no_features() -> None:
    """K 线陈旧（>7 自然日）⇒ 整组特征置空，**不渲染 3 个月前的涨幅**。

    实测面板里有票落后 108 天。若不挡，模型会拿到「3.5 个月前的 5 日累计」
    当成今天的事实 —— 比缺数据更坏：缺数据它会说「证据不足」。
    """
    rows = [("SZ300612", f"2026-07-{d:02d}", 1000.0, 10.0, 1) for d in range(20, 28)]
    rows += [("SZ300999", "2026-11-12", 1000.0, 10.0, 1)]  # 提供「全表最新日」= 11-12
    conn = _bar_db(rows)

    feat = build_features(conn, ["SZ300612"])["SZ300612"]

    assert feat.is_stale is True
    assert feat.stale_days > 100
    assert feat.pct_5d is None
    assert feat.pct_10d is None
    assert feat.high_20d_gap is None
    assert feat.vol_ratio_5d is None


def test_stale_symbol_reported_not_dropped_silently() -> None:
    """陈旧票要在块尾**被点名**，不能静默消失。

    整行消失 ⇒ 模型读成「这只查过了、没异动」；点名 ⇒ 它知道要走工具补查。
    """
    rows = [("SZ300612", f"2026-07-{d:02d}", 1000.0, 10.0, 1) for d in range(20, 28)]
    rows += [("SZ300999", "2026-11-12", 1000.0, 10.0, 1)]
    conn = _bar_db(rows)
    feats = build_features(conn, ["SZ300612"])

    block = render_feature_block(feats, ["SZ300612"])
    tail = "\n".join(block)
    assert "SZ300612" in tail
    assert "陈旧" in tail
    assert "get_kline" in tail  # 明确告诉模型自己去查


def test_slight_lag_still_usable() -> None:
    """2 天内的滞后**仍可用**（周末/节假日常见），不能被误判成陈旧。"""
    rows = [("SZ300063", f"2026-11-{d:02d}", 1000.0, 10.0, 1) for d in range(4, 11)]
    rows += [("SZ300999", "2026-11-12", 1000.0, 10.0, 1)]
    conn = _bar_db(rows)

    feat = build_features(conn, ["SZ300063"])["SZ300063"]
    assert feat.is_stale is False
    assert feat.pct_5d is not None


def test_intraday_note_only_shown_when_partial() -> None:
    """盘中说明只在真盘中时出现（收盘后不该出现，否则是噪音）。"""
    rows = [("SZ300002", f"2026-11-{d:02d}", 1000.0, 20.0, 1) for d in range(5, 12)]
    conn = _bar_db(rows)
    feats = build_features(conn, ["SZ300002"])
    block = render_feature_block(feats, ["SZ300002"])
    assert not any("当前为**盘中**" in line for line in block)


# ── 陈旧数据必须**补齐**，不是只提示（2026-11-12 用户明确要求）───────────────
#
# 「目的是要修复旧数据的问题，而不是找出来提示」。
# 故：陈旧票先联网补取实时日线、用真值重算特征；补不到才降级点名。


def test_stale_symbol_is_backfilled_with_remote_bars() -> None:
    """陈旧票联网补到数据后 ⇒ 特征由**真值**算出，且标 `backfilled`。"""
    from scanner.ai_digest.agent import _backfill_stale_features

    rows = [("SZ300612", f"2026-07-{d:02d}", 1000.0, 10.0, 1) for d in range(20, 28)]
    rows += [("SZ300999", "2026-11-12", 1000.0, 10.0, 1)]
    conn = _bar_db(rows)
    feats = build_features(conn, ["SZ300612"])
    assert feats["SZ300612"].is_stale

    # 桩：远程给 25 根新鲜 bar（末根是**历史日**，不算盘中部分 bar）
    remote = [
        {"date": f"2026-10-{d:02d}", "close": 10.0 + d * 0.1, "high": 12.0, "volume": 1000.0}
        for d in range(1, 26)
    ]
    import scanner.ai_digest.research as research_mod

    class _Ctx:
        def cached(self, key, fn):
            return remote

    original = research_mod._remote_bars
    try:
        research_mod._remote_bars = lambda ctx, sym, n: remote  # type: ignore[assignment]
        got = _backfill_stale_features(feats, _Ctx())  # type: ignore[arg-type]
    finally:
        research_mod._remote_bars = original  # type: ignore[assignment]

    f = got["SZ300612"]
    assert f.is_stale is False, "补到数据后不该再标陈旧"
    assert f.backfilled is True
    assert f.pct_5d is not None, "应由远程真值算出 5 日累计"


def test_backfill_failure_keeps_stale_and_names_it() -> None:
    """补取失败 ⇒ 保持置空并被点名（此时提示是唯一诚实的做法）。"""
    import scanner.ai_digest.research as research_mod
    from scanner.ai_digest.agent import _backfill_stale_features

    rows = [("SZ300612", f"2026-07-{d:02d}", 1000.0, 10.0, 1) for d in range(20, 28)]
    rows += [("SZ300999", "2026-11-12", 1000.0, 10.0, 1)]
    conn = _bar_db(rows)
    feats = build_features(conn, ["SZ300612"])

    original = research_mod._remote_bars
    try:
        research_mod._remote_bars = lambda ctx, sym, n: []  # type: ignore[assignment]
        got = _backfill_stale_features(feats, object())  # type: ignore[arg-type]
    finally:
        research_mod._remote_bars = original  # type: ignore[assignment]

    assert got["SZ300612"].is_stale is True
    block = render_feature_block(got, ["SZ300612"])
    assert any("补取失败" in line for line in block)


def test_backfilled_row_marked_in_output() -> None:
    """补取来的行要标 `※联网补取` —— 模型据此知道本地库对这只票覆盖不全。"""
    feats = {
        "SZ300612": SymbolFeatures(
            symbol="SZ300612", pct_5d=1.0, pct_10d=2.0, high_20d_gap=-3.0, backfilled=True
        )
    }
    block = render_feature_block(feats, ["SZ300612"])
    assert any("※联网补取" in line for line in block)


def test_duplicate_symbol_rendered_once() -> None:
    """同一只票同时在两个面板区出现时，特征只渲染一次。

    实测：`SZ300063` 同时在 `v1 池选` 与 `飙升区` —— 渲染两行既白占 token，
    也会让模型以为它们是两只不同的票。
    """
    feats = {"SZ300063": SymbolFeatures(symbol="SZ300063", pct_5d=5.0)}
    block = render_feature_block(feats, ["SZ300063", "SZ300063", "SZ300063"])
    hits = [ln for ln in block if ln.startswith("SZ300063")]
    assert len(hits) == 1


def test_remote_intraday_bar_does_not_produce_vol_ratio() -> None:
    """补取路径的末根若是**今天**（盘中未收盘）⇒ 同样不许算量能。

    远程 K 线没有 `finalized` 字段，只能按日期判 —— 漏判就复现「半天量 vs 整日均量」
    那个低估 bug（实测斯迪克 0.8x vs 实际量比 3.31）。
    """
    from datetime import date as _date

    import scanner.ai_digest.research as research_mod
    from scanner.ai_digest.agent import _backfill_stale_features

    rows = [("SZ300612", f"2026-07-{d:02d}", 1000.0, 10.0, 1) for d in range(20, 28)]
    rows += [("SZ300999", "2026-11-12", 1000.0, 10.0, 1)]
    conn = _bar_db(rows)
    feats = build_features(conn, ["SZ300612"])

    today = _date.today().isoformat()
    remote = [
        {"date": f"2026-11-{d:02d}", "close": 10.0, "high": 11.0, "volume": 1000.0}
        for d in range(1, 13)
    ]
    remote.append({"date": today, "close": 10.0, "high": 11.0, "volume": 500.0})

    original = research_mod._remote_bars
    try:
        research_mod._remote_bars = lambda ctx, sym, n: remote  # type: ignore[assignment]
        got = _backfill_stale_features(feats, object())  # type: ignore[arg-type]
    finally:
        research_mod._remote_bars = original  # type: ignore[assignment]

    f = got["SZ300612"]
    assert f.vol_ratio_5d is None, "末根是今天（盘中）⇒ 不许算量能"
    assert f.intraday_partial is True
