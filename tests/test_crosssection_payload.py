"""横截面观察层的**面板投影**测试（v3 契约新增 `regions.crosssection`）。

本层与终端 `≡` 块是**同一份进程内状态的两种投影**（`scanner/crosssection.py`）：
终端拼字符串、面板下发「列 + 行」。所以本文件的断言只有两类：

1. **不另造口径**：面板行的字段集必须与 `COLS_CS` 一一对应（少一个键 = 静默空白格，
   多一个键 = 前端永远取不到）。`signal_text` 必须与终端 `≡` 块里那句一字不差。
2. **未启用就整块跳过**：关闭/非交易时段 → `{"enabled": False}`，前端据此不画空壳
   （而不是画一张 0 行的表，那会被读成「没有异动」）。
"""

from __future__ import annotations

import pytest

from scanner import crosssection


def test_cols_cs_is_web_only_projection_of_terminal_fields():
    """`COLS_CS` 是**面板列名**（中文），行是**英文字段**；两侧靠 `cols.ts` 的 cell 函数连接。

    本断言定的是行侧契约：`_signal_row` 的键集**固定**（前端 cell 函数逐列取这几个键）。
    少一个键 = 前端静默拿到 `undefined`；多一个键 = 永远没人读。列名与 cell 函数的
    逐项对应由跨语言守卫 `tests/test_panel_web_cols.py` 管（两侧都要在）。
    """
    sample = {
        "symbol": "SZ300199",
        "name": "翰宇药业",
        "price": 18.42,
        "percent": 2.84,
        "consistency": "全正",
        "volume_price": "配合",
        "persist": 9,
        "main_net": 4.213e7,
        "amount": 6.2e8,
    }
    row = crosssection._signal_row(sample)
    assert set(row) == {
        "symbol",
        "name",
        "price",
        "percent",
        "persist",
        "profile",
        "section",
    }
    # 列数 = 行字段数 + 序号列（`#` 由前端按行号填，不占数据键；无合并列）。
    assert len(crosssection.COLS_CS) == len(row) + 1


def test_profile_text_is_same_string_as_terminal_block():
    """`画像` 列直接吃 `profile_text` —— 前端**不重拼**，故必须等于终端那段。"""
    row = crosssection._signal_row(
        {
            "percent": 4.2, "consistency": "全正", "volume_price": "配合", "persist": 9,
            "main_net": 4.213e7, "amount": 6.2e8,
        }
    )
    assert row["profile"] == "启动·持续9轮·量价齐升·主力+4213万(占额6.8%)"
    # 分节单源：低位（<启动上限）+ 主力净流入 + 全正·配合
    assert row["section"] == "低位·资金强"


def test_signal_row_tolerates_missing_fields():
    """缺值不是 0：`—`/缺省走终端同款兜底（缺字段不得让整块 payload 抛 KeyError）。"""
    row = crosssection._signal_row({"symbol": "SZ300001"})
    assert row["symbol"] == "SZ300001"
    assert row["persist"] == 1
    assert row["profile"] == "—·持续1轮·动向不明·资金—"
    assert row["section"] == "持续走强"


def test_profile_omits_share_when_denominator_unusable() -> None:
    """净额有、成交额缺失/非正 ⇒ 不带占额（缺值不是 0，不硬凑比例）。"""
    base = {"percent": 4.2, "consistency": "全正", "volume_price": "配合", "persist": 9, "main_net": 4.213e7}
    assert crosssection.profile_text(base).endswith("主力+4213万")
    assert crosssection.profile_text({**base, "amount": 0}).endswith("主力+4213万")
    assert crosssection.profile_text({**base, "amount": -1}).endswith("主力+4213万")
    # 净流出也照常给占比（符号跟净额走）
    assert crosssection.profile_text({**base, "main_net": -2.1e8, "amount": 6.2e8}).endswith(
        "主力-2.1亿(占额-33.9%)"
    )


def test_snapshot_payload_applies_same_flow_gate_as_terminal(monkeypatch):
    """面板投影与终端共用 `_display_flow_ok`：净流出剔除、跌幅大但流入保留、缺值放行。"""

    def _sig(symbol: str, persist: int, main_net: float | None) -> dict:
        return {
            "symbol": symbol,
            "name": "票" + symbol[-2:],
            "price": 10.0,
            "percent": -8.5 if symbol == "SZ300101" else 5.0,
            "consistency": "混合",
            "volume_price": "—",
            "persist": persist,
            "main_net": main_net,
        }

    sigs = [
        _sig("SZ300101", 7, 8e6),   # 跌但净流入 800 万 → 留
        _sig("SZ300102", 7, -8e6),  # 涨但流出 → 剔
        _sig("SZ300103", 1, None),  # 资金缺失 → fail-open
        _sig("SZ300104", 7, 5e6),   # 正好 500 万，未超过闸 → 剔
    ]
    monkeypatch.setitem(crosssection._state, "last_signals", sigs)
    monkeypatch.setitem(
        crosssection._state,
        "health",
        {**crosssection._state["health"], "enabled": True, "state": "ok"},
    )
    payload = crosssection.snapshot_payload()
    assert [r["symbol"] for r in payload["groups"]["persist"]] == ["SZ300101"]
    assert [r["symbol"] for r in payload["groups"]["new"]] == ["SZ300103"]
    assert payload["hidden_flow"] == 2
    assert payload["hidden_mid"] == 2  # 未进展示组的补数（此处即被闸挡下的那 2 只）
    assert payload["display_flow_min"] == 5_000_000.0
    # 网页版口径与终端同一函数生成 —— 不另拼阈值单位/语义/剔除数。
    assert payload["display_flow_note"] == crosssection._flow_gate_note(payload["hidden_flow"])
    assert "500万" in payload["display_flow_note"]
    assert "本轮剔 2 只" in payload["display_flow_note"]


def test_snapshot_payload_is_disabled_by_default():
    """进程内还没观测过（或本层被关）⇒ 只有 `enabled: False`，不带任何行。"""
    payload = crosssection.snapshot_payload()
    if payload.get("enabled"):
        pytest.skip("本机进程已观测过横截面状态（模块级状态非测试隔离），跳过默认态断言")
    assert payload == {"enabled": False}
