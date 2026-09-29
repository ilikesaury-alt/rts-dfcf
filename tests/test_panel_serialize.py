"""`scanner/panel_serialize.py` 单测（T0.2 / design §10 前 4 行 + 复核修 6）。

这是**零网络零依赖**的纯函数模块测试 —— 面板工程里最早能进 CI 的资产，
云端（Workers/D1）未就绪时它就已全绿。四类断言对应三个隐性类型陷阱：

1. tuple 键往返（D3 陷阱 1）
2. `_candidate` 剥离（D3 陷阱 2）
3. 列 key 序列 == `[c[0] for c in COLS_*]`（D7）
4. 150 票样本 → `stocks` 截到 ≤20 且**新票保留**（FR-C2 规则 5 / 复核修 6）
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from scanner.models import Candidate, StockInfo
from scanner.panel_serialize import (
    COLS_HIST,
    COLS_HOT,
    COLS_POOL,
    SCHEMA_VERSION,
    STOCK_QUOTA,
    col_spec,
    enforce_body_limit,
    enforce_stock_quota,
    serialize_view,
)
from scanner.view.model import MainRow, ScanView


def _candidate() -> Candidate:
    return Candidate(
        stock=StockInfo(
            symbol="SZ300319",
            code="300319",
            name="麦捷科技",
            percent=5.1,
            current=21.5,
            value=1.2e9,
            rank_change=3,
            rank=7,
        ),
        category="momentum",
        score=88,
        reason="形态良好",
        kline=None,
    )


def _entry() -> dict:
    return {
        "symbol": "300319",
        "name": "麦捷科技",
        "category": "momentum",
        "score": 88,
        "date": "2026-09-29",
        "time": "10:31:00",
        "percent": 5.1,
        "concept": "半导体",
        "first_time": "10:00:00",
        "score_breakdown": json.dumps({"mo_breakout": 12, "fund_flow_main_pct": 3.2}),
        "live_percent": 5.1,
        "live_rank": 7,
        "_accum": 9.9,
        "_tier": 1,
        "_core_stock": True,
        "_candidate": _candidate(),  # ← 必须被剥离的活对象
    }


def _main_row(symbol: str = "300319", name: str = "麦捷科技") -> MainRow:
    entry = _entry()
    entry["symbol"] = symbol
    entry["name"] = name
    return MainRow(
        entry=entry,  # type: ignore[arg-type]
        rank=7,
        accum=9.9,
        score=88.0,
        composite_score=7.5,
        core=True,
        cat_label="MOM",
        pct=5.1,
        current=21.5,
        sector="半导体",
        is_new_entry=True,
    )


def _view(n: int = 1) -> ScanView:
    return ScanView(
        main_rows=[_main_row(f"3003{i:02d}", f"票{i}") for i in range(n)],
        breakout_mark={("300319", "momentum"): True},
        flow_pct_map={"300319": 3.2},
        last_ranks={"300319": 9},
        weak=False,
        warnings=["概念取数失败"],
        hot_rows=None,
        offboard_rows=None,
        hist_rows=None,
    )


_EXTRA = {"seq": 7, "date": "2026-09-29", "time": "10:31:00", "durationMs": 1234}


# ── 陷阱 1：tuple 键往返 ──────────────────────────────────────────────────


def test_marks_roundtrip_turns_tuple_keys_into_sym_cat_strings():
    """D3 陷阱 1：tuple 键直接 json.dumps 必抛 TypeError，必须经 _marks 转 'sym|cat'。"""
    view = _view()
    payload = serialize_view(view, extra=_EXTRA)
    marks = payload["regions"]["marks"]
    assert marks["breakout"] == {"300319|momentum": True}
    assert marks["beauty"] == {}
    # 往返：json.dumps 不抛，且字符串键可被前端按 symbol|cat 拆回
    raw = json.dumps(payload, ensure_ascii=False)
    assert "300319|momentum" in raw


def test_marks_on_empty_view_is_empty_dict_not_none():
    view = ScanView(
        main_rows=[],
        breakout_mark={},
        flow_pct_map={},
        last_ranks={},
        weak=True,
        warnings=[],
        beauty_mark=None,
    )
    payload = serialize_view(view, extra=_EXTRA)
    assert payload["regions"]["marks"] == {"breakout": {}, "beauty": {}}
    json.dumps(payload, ensure_ascii=False)


# ── 陷阱 2：_candidate 剥离 ──────────────────────────────────────────────


def test_flat_row_strips_candidate_object_and_payload_is_dumpable():
    """D3 陷阱 2：`_candidate` 是活对象，递归展开会爆炸 → 载荷里不得出现该键。"""
    payload = serialize_view(_view(), extra=_EXTRA)
    row = payload["regions"]["main"][0]
    assert "_candidate" not in row
    assert "score" in row and row["score"] == 88
    # 展示层标量键保留（前端可选用，非活对象）
    assert row["_tier"] == 1 and row["_core_stock"] is True
    # MainRow 自身的字段被摊平到行内（entry 不再是嵌套字典）
    assert "entry" not in row
    assert row["sector"] == "半导体" and row["is_new_entry"] is True
    json.dumps(payload, ensure_ascii=False)


def test_no_candidate_key_anywhere_in_payload():
    """负面断言：整份载荷（含 gate 区）都不得残留 `_candidate`。"""
    blob = json.dumps(serialize_view(_view(3), extra=_EXTRA), ensure_ascii=False)
    assert "_candidate" not in blob


def test_score_breakdown_is_parsed_to_dict_not_left_as_json_string():
    """D3 陷阱 3：字符串形态 → dict，云端存原样、前端不再二次解析。"""
    row = serialize_view(_view(), extra=_EXTRA)["regions"]["main"][0]
    assert row["score_breakdown"] == {"mo_breakout": 12, "fund_flow_main_pct": 3.2}


def test_score_breakdown_invalid_json_degrades_to_empty_dict():
    entry = _entry()
    entry["score_breakdown"] = "<<不是 JSON>>"
    row = MainRow(
        entry=entry,
        rank=1,
        accum=None,
        score=1.0,
        composite_score=1.0,
        core=False,
        cat_label="NEW",
        pct=1.0,
        current=1.0,
        sector="",
    )
    payload = serialize_view(
        ScanView(
            main_rows=[row],
            breakout_mark={},
            flow_pct_map={},
            last_ranks={},
            weak=False,
            warnings=[],
        ),
        extra=_EXTRA,
    )
    assert payload["regions"]["main"][0]["score_breakdown"] == {}


# ── 陷阱 3：列头单源（D7）────────────────────────────────────────────────


@pytest.mark.parametrize("cols", [COLS_POOL, COLS_HOT, COLS_HIST])
def test_col_spec_keys_match_model_cols(cols):
    """D7：列定义**只**从 `view/model.py` 来，前端不得硬编码列名。"""
    spec = col_spec(cols)
    assert [c["key"] for c in spec] == [c[0] for c in cols]
    assert [c["label"] for c in spec] == [c[0] for c in cols]
    assert [c["align"] for c in spec] == [c[2] for c in cols]
    assert [c["width"] for c in spec] == [c[1] for c in cols]


def test_regions_cols_payload_contains_three_regions():
    payload = serialize_view(_view(), extra=_EXTRA)
    cols = payload["regions"]["cols"]
    assert set(cols) == {"pool", "hot", "hist"}
    assert cols["pool"] == col_spec(COLS_POOL)
    assert cols["hot"] == col_spec(COLS_HOT)
    assert cols["hist"] == col_spec(COLS_HIST)


def test_col_spec_is_json_safe():
    json.dumps(col_spec(COLS_POOL), ensure_ascii=False)


# ── 规则 5：stocks 配额与体积探针（复核修 6）─────────────────────────────


def _stocks(n: int) -> dict:
    return {
        f"{300000 + i}": {"klineDate": "2026-09-29", "kline": [[1, 2, 3, 4, 5, 6.0]], "appearances": []}
        for i in range(n)
    }


def test_quota_noop_when_under_limit():
    s = _stocks(5)
    assert enforce_stock_quota(s) is s  # 不超配额原样返回（零拷贝语义）


def test_quota_truncates_150_stocks_to_20_and_keeps_new_entries():
    """150 票样本 → 截到 20，且新票（new_first）在其中。"""
    s = _stocks(150)
    new = {f"{300000 + i}" for i in range(140, 150)}  # 新票排在 dict 尾部
    out = enforce_stock_quota(s, new_first=new)
    assert len(out) == STOCK_QUOTA == 20
    assert set(out) & new, "新票必须优先保留"


def test_body_limit_truncates_stocks_instead_of_dropping_round():
    """体积探针：>400KB 时只砍 stocks，`regions`/`date`/`time` 全部保留（复核修 6）。"""
    big = _stocks(150)
    for v in big.values():
        v["kline"] = [[1, 2, 3, 4, 5, 6.0] for _ in range(120)]
    payload = serialize_view(_view(40), extra={**_EXTRA, "stocks": big})
    before = len(json.dumps(payload, ensure_ascii=False).encode())
    trimmed, dropped = enforce_body_limit(payload, limit=50_000)
    after = len(json.dumps(trimmed, ensure_ascii=False).encode())
    assert before > 50_000 >= after
    assert dropped > 0
    assert len(trimmed["stocks"]) < len(big)
    assert len(trimmed["regions"]["main"]) == 40  # regions 整轮不丢
    assert trimmed["date"] == "2026-09-29" and trimmed["time"] == "10:31:00"


def test_body_limit_keeps_small_payload_untouched():
    payload = serialize_view(_view(1), extra={**_EXTRA, "stocks": _stocks(3)})
    out, dropped = enforce_body_limit(payload)
    assert dropped == 0
    assert out["stocks"] == payload["stocks"]


# ── 契约 / 门 ────────────────────────────────────────────────────────────


def test_schema_version_is_2_and_matches_contract():
    """SCHEMA_VERSION 与 worker/src/serialize.ts 同值（改契约必须双端同步）。"""
    assert SCHEMA_VERSION == 2


def test_payload_top_level_shape():
    p = serialize_view(_view(2), extra={**_EXTRA, "stocks": _stocks(1)})
    assert set(p) == {"schema", "seq", "date", "time", "durationMs", "regions", "ctx", "stocks"}
    assert p["schema"] == 2 and p["seq"] == 7 and p["durationMs"] == 1234
    assert set(p["regions"]) == {"main", "hist", "hot", "offboard", "gate", "cols", "marks"}
    assert set(p["regions"]["gate"]) == {"main", "hist", "hot", "offboard", "stats"}


def test_gate_stats_has_seven_fields():
    """FR-V3：`gate.stats` 7 个字段全带（面板要如实显示剔了多少）。"""
    stats = serialize_view(_view(), extra=_EXTRA)["regions"]["gate"]["stats"]
    assert set(stats) == {
        "total",
        "passed",
        "tier_a",
        "tier_b",
        "tier_c_fallback",
        "vetoed",
        "dropped_no_fallback",
    }


def test_none_regions_serialize_as_empty_lists_not_null():
    p = serialize_view(_view(), extra=_EXTRA)
    assert p["regions"]["hot"] == []
    assert p["regions"]["hist"] == []
    assert p["regions"]["offboard"] == []


def test_view_with_nested_dataclass_candidate_does_not_explode():
    """负面：Candidate 是 30+ 字段的 dataclass，展开后载荷会暴涨 —— 断言它没进去。"""
    payload = serialize_view(_view(), extra=_EXTRA)
    row = payload["regions"]["main"][0]
    assert "stock" not in row and "tactic_tags" not in row
    # 全载荷体积应保持在 KB 级（150 票 K 线未带时）
    assert len(json.dumps(payload, ensure_ascii=False).encode()) < 20_000


def test_dataclasses_asdict_would_have_exploded_proving_the_risk_is_real():
    """守卫 D3 陷阱 2 的**前提**仍成立：MainRow 确实挂着 dataclass 活对象。

    若哪天 `_candidate` 被改成名义上的普通 dict，本测试提醒重新评估剥离逻辑。
    """
    assert dataclasses.is_dataclass(_candidate())
    assert _entry()["_candidate"] is not None
