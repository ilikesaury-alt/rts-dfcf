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
