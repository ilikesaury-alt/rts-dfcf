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
        "speed_1": 0.31,
        "speed_3": 0.68,
        "speed_5": 1.05,
        "amount_surge": 2.14,
        "vol_ratio": 2.63,
        "pct_rank": 0.947,
        "consistency": "全正",
        "volume_price": "配合",
        "persist": 9,
    }
    row = crosssection._signal_row(sample)
    assert set(row) == {
        "symbol",
        "name",
        "price",
        "percent",
        "speed_1",
        "speed_3",
        "speed_5",
        "amount_surge",
        "vol_ratio",
        "pct_rank",
        "consistency",
        "volume_price",
        "persist",
        "signal_text",
    }
    # 列数 = 行字段数 + 序号列（`#` 由前端按行号填，不占数据键）
    #        − `判据` 把 consistency / volume_price / persist 三个键**合成一列**（signal_text）。
    assert len(crosssection.COLS_CS) == len(row) - 3 + 1


def test_signal_text_is_same_string_as_terminal_block():
    """`判据` 一列直接吃 `signal_text` —— 前端**不重拼**，故必须等于终端那句。"""
    row = crosssection._signal_row({"consistency": "全正", "volume_price": "配合", "persist": 9})
    assert row["signal_text"] == "全正·配合·9轮"


def test_signal_row_tolerates_missing_fields():
    """缺值不是 0：`—`/缺省走终端同款兜底（缺字段不得让整块 payload 抛 KeyError）。"""
    row = crosssection._signal_row({"symbol": "SZ300001"})
    assert row["symbol"] == "SZ300001"
    assert row["consistency"] == "混合"
    assert row["volume_price"] == "—"
    assert row["persist"] == 1
    assert row["signal_text"] == "混合·—·1轮"


def test_snapshot_payload_is_disabled_by_default():
    """进程内还没观测过（或本层被关）⇒ 只有 `enabled: False`，不带任何行。"""
    payload = crosssection.snapshot_payload()
    if payload.get("enabled"):
        pytest.skip("本机进程已观测过横截面状态（模块级状态非测试隔离），跳过默认态断言")
    assert payload == {"enabled": False}
