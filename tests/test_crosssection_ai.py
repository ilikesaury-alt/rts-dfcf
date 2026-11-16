"""`scanner/ai_digest/crosssection_view.py` 与 `.crosssection` CLI 的守卫。

三条不可退让的约束（Lichun 2026-11-16 口径）：

1. **输入源只有 `crosssection_signal`** —— 代码里出现任何别的表名即红。
2. **判据必须与本层同源** —— 直接复用 `crosssection._consistency/_volume_price`，
   不许在本模块重写一份（抄一份迟早漂移）。
3. **本层不产生结论** —— 素材只给分布与结构，不给「哪只更好」。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from scanner.ai_digest.crosssection import SYSTEM_PROMPT
from scanner.ai_digest.crosssection_view import (
    CrosssectionSummary,
    build_summary,
    render_materials,
    resolve_date,
)
from scanner.crosssection import _consistency, _volume_price

_HERE = Path(__file__).resolve().parent
_VIEW_SRC = _HERE.parent / "scanner" / "ai_digest" / "crosssection_view.py"
_CLI_SRC = _HERE.parent / "scanner" / "ai_digest" / "crosssection.py"


def _db(rows: list[dict]) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE crosssection_signal ("
        "date TEXT NOT NULL, round_seq INTEGER NOT NULL, symbol TEXT NOT NULL, "
        "name TEXT, price REAL, percent REAL, speed_1 REAL, speed_3 REAL, speed_5 REAL, "
        "amount_surge REAL, vol_ratio REAL, pct_rank REAL, updated_at TEXT NOT NULL, "
        "PRIMARY KEY (date, round_seq, symbol))"
    )
    for i, r in enumerate(rows):
        conn.execute(
            "INSERT INTO crosssection_signal VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                r.get("date", "2026-11-16"),
                r.get("round_seq", 100000 + i),
                r["symbol"],
                r.get("name", r["symbol"]),
                r.get("price", 10.0),
                r.get("percent", 1.0),
                r.get("speed_1"),
                r.get("speed_3"),
                r.get("speed_5"),
                r.get("amount_surge"),
                r.get("vol_ratio", 1.0),
                r.get("pct_rank", 0.99),
                r.get("updated_at", "2026-11-16T10:00:00+08:00"),
            ),
        )
    conn.commit()
    conn.row_factory = sqlite3.Row
    return conn


# ── 契约 1：输入源唯一 ──────────────────────────────────────────


@pytest.mark.parametrize("path", [_VIEW_SRC, _CLI_SRC], ids=["view", "cli"])
def test_only_crosssection_signal_table_is_read(path: Path) -> None:
    """剥掉注释与 docstring 后，`FROM`/`JOIN` 只允许出现 crosssection_signal。

    为什么剥注释：docstring 里会**提到**禁用表名（那是给人看的纪律说明），
    不能把它当代码违规。这个测试的判据是「可执行 SQL 里没有别的表」。
    """
    src = path.read_text(encoding="utf-8")
    # 粗略剥三引号字符串（覆盖本文件全部 docstring 用法）
    import re

    code = re.sub(r'""".*?"""', "", src, flags=re.S)
    code = re.sub(r"'''.*?'''", "", code, flags=re.S)
    code = "\n".join(line.split("#", 1)[0] for line in code.splitlines())

    tables = set(re.findall(r"\b(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_]*)", code))
    # 判据是「**没有别的表**」，不是「必须出现这张表」——
    # CLI 层把查询完全委托给 view（自己一条 SQL 都没有），也是合规的。
    extra = tables - {"crosssection_signal"}
    assert not extra, f"{path.name} 读到了不该读的表：{sorted(extra)}"


def test_no_network_imports_in_view() -> None:
    """素材构建器必须纯离线 —— 出现 requests/urllib/httpx 即红。"""
    src = _VIEW_SRC.read_text(encoding="utf-8")
    for bad in ("import requests", "import urllib", "import httpx", "urlopen"):
        assert bad not in src, f"crosssection_view 不该联网，但出现了 {bad!r}"


# ── 契约 2：判据同源 ────────────────────────────────────────────


def test_summary_reuses_layer_judgment_functions() -> None:
    """本模块必须 import 本层的判据函数，不许自己重写。"""
    src = _VIEW_SRC.read_text(encoding="utf-8")
    assert "_consistency" in src and "_volume_price" in src
    assert "from scanner.crosssection import" in src


def test_judgments_match_layer_implementation() -> None:
    """摘要里的判据必须与本层逐值一致（防止未来单边修改）。"""
    conn = _db(
        [
            # 全正 + 配合
            {"symbol": "SZ300001", "speed_1": 0.5, "speed_3": 0.4, "speed_5": 0.3,
             "amount_surge": 2.0},
            # 混合（符号打架）+ 背离
            {"symbol": "SZ300002", "speed_1": -0.5, "speed_3": 0.4, "speed_5": 0.3,
             "amount_surge": 2.0},
            # 缺项 ⇒ 混合 / —
            {"symbol": "SZ300003", "speed_1": None, "speed_3": None, "speed_5": 0.3,
             "amount_surge": None},
        ]
    )
    s = build_summary(conn, "2026-11-16")
    assert s is not None
    for row in conn.execute("SELECT * FROM crosssection_signal"):
        c = _consistency(row["speed_1"], row["speed_3"], row["speed_5"])
        v = _volume_price(row["amount_surge"], row["speed_1"])
        sample = next(x for x in s.samples if x["symbol"] == row["symbol"])
        assert sample["consistency"] == c
        assert sample["volume_price"] == v


# ── 契约 3：只给结构，不给结论 ──────────────────────────────────


def test_materials_contain_no_recommendation_language() -> None:
    """素材里不得出现倾向性词汇 —— 它是「结构描述」，不是「候选筛选」。"""
    conn = _db(
        [
            {"symbol": f"SZ3000{i:02d}", "percent": (i - 5) * 2.0,
             "speed_1": 0.1 * i, "speed_3": 0.1 * i, "speed_5": 0.1 * i,
             "amount_surge": 1.0 + i * 0.1, "pct_rank": 0.95 + i * 0.005}
            for i in range(1, 11)
        ]
    )
    text = render_materials(build_summary(conn, "2026-11-16"))
    for bad in ("建议", "买入", "关注", "值得", "优于", "首选", "排序", "推荐"):
        assert bad not in text, f"素材不该出现 {bad!r}（本层不产生结论）"


def test_system_prompt_forbids_conclusions() -> None:
    p = SYSTEM_PROMPT
    for must in ("买卖建议", "不跨票比较", "观察层", "不产生结论"):
        assert must in p, f"提示词必须明确禁止/说明：{must}"
    # 不得把「分析」写成「选股」
    assert "选股" not in p and "买入候选" not in p


# ── 基础行为 ────────────────────────────────────────────────────


def test_empty_table_returns_none() -> None:
    conn = _db([])
    assert resolve_date(conn) is None
    assert build_summary(conn, "2026-11-16") is None


def test_missing_table_returns_none() -> None:
    conn = sqlite3.connect(":memory:")
    assert resolve_date(conn) is None
    assert build_summary(conn, "2026-11-16") is None


def test_resolve_date_picks_latest() -> None:
    conn = _db(
        [
            {"symbol": "SZ300001", "date": "2026-11-13"},
            {"symbol": "SZ300001", "date": "2026-11-16"},
        ]
    )
    assert resolve_date(conn) == "2026-11-16"
    assert resolve_date(conn, "2026-11-13") == "2026-11-13"
    # 不存在的日期 ⇒ None（不静默退回最新）
    assert resolve_date(conn, "2020-01-01") is None


def test_persist_buckets_and_max() -> None:
    rows = []
    # SZ300001 出现 12 轮
    for i in range(12):
        rows.append({"symbol": "SZ300001", "round_seq": 100000 + i})
    # SZ300002 出现 2 轮
    for i in range(2):
        rows.append({"symbol": "SZ300002", "round_seq": 100000 + i})
    s = build_summary(_db(rows), "2026-11-16")
    assert s is not None
    assert s.symbols == 2
    assert s.rounds == 12
    assert s.max_persist == 12
    assert s.max_persist_symbol == "SZ300001"
    assert s.persist_buckets["11-30轮"] == 1
    assert s.persist_buckets["2-3轮"] == 1


def test_last_row_wins_for_judgments() -> None:
    """判据取**最后一条**记录（当轮状态），不是第一条。"""
    # 首轮「全正·配合」，末轮「全负·背离」⇒ 必须取末轮。
    # ⚠ 背离的判据是 `amount_surge > 2.0`（**严格大于**）`且 speed_1 <= 0`
    # —— 实测 `amount_surge=2.0` 精确等于 2 会落到 `—`。用 2.5 避免踩边界。
    rows = [
        {"symbol": "SZ300001", "round_seq": 100001, "speed_1": 0.5, "speed_3": 0.5,
         "speed_5": 0.5, "amount_surge": 2.5},
        {"symbol": "SZ300001", "round_seq": 100002, "speed_1": 0.0, "speed_3": -0.5,
         "speed_5": -0.5, "amount_surge": 2.5},
    ]
    # ⚠ amount_surge 必须 **> 2.0** 才判背离：等于 2.0 恰好落在阈值上 ⇒ 判 `—`。
    # （这里必须用 2.5；写成 2.0 会静默落到 `—`，断言失败。）
    s = build_summary(_db(rows), "2026-11-16")
    assert s is not None
    assert s.consistency == {"混合": 1}
    assert s.volume_price == {"背离": 1}


def test_row_factory_restored() -> None:
    """`build_summary` 用完必须还原调用方的 row_factory（共用连接场景）。"""
    conn = _db([{"symbol": "SZ300001"}])
    conn.row_factory = None
    build_summary(conn, "2026-11-16")
    assert conn.row_factory is None


def test_summary_dataclass_defaults() -> None:
    s = CrosssectionSummary(date="2026-11-16")
    assert s.rounds == 0 and s.samples == []
    assert render_materials(s).startswith("【横截面异动层 · 2026-11-16】")