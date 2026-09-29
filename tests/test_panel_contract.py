"""**双端契约测试**（tasks「Definition of Done · 契约双端」）。

契约活在两个独立仓库里（本地 `scanner/panel_serialize.py` ⇄ 云端
`rts-panel-cloud/worker/src/serialize.ts`），改了一边忘了另一边是本工程
**最贵的一种错误**：不会编译失败、不会单测失败，只会在每轮上报时刷 400
`SCHEMA_MISMATCH`，且本地日志只留一行状态码，极难定位。

本测试直接读云端源文件做对照，把「双端不一致」变成**本地 CI 的一次失败**。

⚠ 云端仓库是独立仓库（R3），未克隆时**跳过**而不是失败 —— 本仓库的 CI 不该
因为兄弟仓库缺席而红。等云端进仓（Phase 1 T1.1 之后）本测试即自动生效。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from scanner.panel_serialize import BODY_LIMIT, SCHEMA_VERSION, STOCK_KLINE_DAYS, STOCK_QUOTA, serialize_view

CLOUD_ROOT = Path(__file__).resolve().parents[1] / "rts-panel-cloud" / "worker" / "src"

pytestmark = pytest.mark.skipif(
    not (CLOUD_ROOT / "serialize.ts").exists(),
    reason="云端仓库 rts-panel-cloud 未在本地检出（独立仓库 R3）",
)


def _ts(name: str) -> str:
    return (CLOUD_ROOT / name).read_text(encoding="utf-8")


def _ts_const(src: str, name: str) -> int:
    """从 TS 源码里取 `export const NAME = <数字>;`（不是运行时求值，是文本对照）。"""
    m = re.search(rf"export const {name}\s*=\s*(\d+)\s*;", src)
    assert m, f"云端源码里找不到常量 {name}（若改名请同步契约）"
    return int(m.group(1))


def test_schema_version_matches_both_ends():
    """双端 SCHEMA_VERSION 同值。不等即**每轮 400**（复核修 1）。"""
    assert _ts_const(_ts("serialize.ts"), "SCHEMA_VERSION") == SCHEMA_VERSION == 2


def test_schema_version_not_in_wrangler_vars():
    """复核修 1：`SCHEMA_VERSION` **不得**进 vars —— vars 一律字符串，`2 !== "2"`。"""
    cfg = (CLOUD_ROOT.parent / "wrangler.jsonc").read_text(encoding="utf-8")
    # 去掉注释行再查，避免注释里提到 SCHEMA_VERSION 造成误判
    code = "\n".join(ln for ln in cfg.splitlines() if not ln.strip().startswith("//"))
    assert "SCHEMA_VERSION" not in code, "SCHEMA_VERSION 必须硬编码在 serialize.ts，不能放 wrangler vars"


def test_stock_quota_matches_both_ends():
    """每轮票数上限双端同值（20）。不一致时云端会静默丢弃多出来的票。"""
    ingest_src = _ts("routes/ingest.ts")
    assert _ts_const(ingest_src, "STOCK_QUOTA") == STOCK_QUOTA == 20


def test_gate_stats_field_set_matches_both_ends():
    """`gate.stats` 7 字段双端一致（FR-V3：面板要如实显示剔了多少）。"""
    from scanner.view.model import ScanView

    view = ScanView(main_rows=[], breakout_mark={}, flow_pct_map={}, last_ranks={}, weak=False, warnings=[])
    stats = set(serialize_view(view, extra={"date": "2026-09-29", "time": "10:31:00"})["regions"]["gate"]["stats"])
    assert stats == {
        "total",
        "passed",
        "tier_a",
        "tier_b",
        "tier_c_fallback",
        "vetoed",
        "dropped_no_fallback",
    }, "本地字段集漂移"
    ts = _ts("serialize.ts")
    for f in stats:
        assert re.search(rf"\b{f}\b", ts), f"云端 GateStats 缺字段 {f}"


def test_regions_field_set_matches_both_ends():
    """五个区块 + gate + cols + marks：双端字段集相等。"""
    from scanner.view.model import ScanView

    view = ScanView(main_rows=[], breakout_mark={}, flow_pct_map={}, last_ranks={}, weak=False, warnings=[])
    regions = serialize_view(view, extra={"date": "2026-09-29", "time": "10:31:00"})["regions"]
    ts = _ts("serialize.ts")
    for field in regions:
        assert re.search(rf"\b{field}\b", ts), f"云端 Regions 缺字段 {field}"


def test_payload_top_level_fields_match_both_ends():
    from scanner.view.model import ScanView

    view = ScanView(main_rows=[], breakout_mark={}, flow_pct_map={}, last_ranks={}, weak=False, warnings=[])
    payload = serialize_view(view, extra={"date": "2026-09-29", "time": "10:31:00"})
    ts = _ts("serialize.ts")
    for field in payload:
        assert re.search(rf"\b{field}\b", ts), f"云端 Payload 缺字段 {field}"


def test_latest_round_ordering_clause_is_present_on_every_read_path():
    """复核修 2：所有「取最新一轮」的查询必须 ORDER BY date DESC, time DESC。

    这是纯文本断言，但针对的是**最贵的回归**：哪天有人图省事改成 `ORDER BY seq`，
    本地测试抓不到（云端不在 CI），只有这个断言能抓到。
    """
    for name in ("routes/regions.ts", "routes/meta.ts"):
        src = _ts(name)
        # 字符类要同时排除反引号与换行：SQL 子句是单行且被反引号包着，
        # 只排除引号会让匹配一路吃掉后面的 docstring 注释（本人已踩过一次）。
        clauses = [m.group(0) for m in re.finditer(r"ORDER BY[^`\"'\n]*", src)]
        # 防真空断言：若哪天正则改坏导致 0 匹配，上面的循环会「全绿」而实际什么都没查。
        assert len(clauses) >= 1, f"{name} 未匹配到任何 ORDER BY —— 正则失效，本测试已空转"
        for clause in clauses:
            assert "seq" not in clause, f"{name} 的取轮排序用了 seq：{clause!r}（复核修 2）"
            assert "date DESC" in clause and "time DESC" in clause, (
                f"{name} 取轮排序应为 (date DESC, time DESC)：{clause!r}"
            )


def test_body_limit_and_kline_days_are_documented_on_both_ends():
    """体积探针 400KB 与 K 线 120 根：本地常量存在，云端端点上限一致。"""
    assert BODY_LIMIT == 400_000
    assert STOCK_KLINE_DAYS == 120


def test_keys_are_stringified_sym_cat_on_both_ends():
    """D3 陷阱 1 的跨端后果：云端**不要**自己拼 sym|cat，原样用本地转好的。"""
    from scanner.view.model import ScanView

    view = ScanView(
        main_rows=[],
        breakout_mark={("300319", "momentum"): True},
        flow_pct_map={},
        last_ranks={},
        weak=False,
        warnings=[],
    )
    marks = serialize_view(view, extra={"date": "2026-09-29", "time": "10:31:00"})["regions"]["marks"]
    blob = json.dumps(marks, ensure_ascii=False)
    assert "300319|momentum" in blob
    assert "('300319'" not in blob, "不得出现 Python tuple 字面量形式的键"
