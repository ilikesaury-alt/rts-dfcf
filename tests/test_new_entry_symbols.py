"""v1 主表「新票优先」的差集判据（`unified_scanner._new_rec_symbols`）单测。

被测对象是**扫描循环的跨轮状态机**，不是展示层：它决定「哪些 symbol 算本轮新进池」，
该集合随后作为 `new_symbols` 传入 `build_scan_view`，成为 v1 主表第 1 排序键。
展示侧（排序 / 行尾「新」标记 / 飞书卡片标记）的守护分别在
`tests/test_display.py` 的「v1 主表「新票优先」」段 与 `tests/test_feishu.py`。

为什么用集合差集而不是 `recommendations.time` / `first_time`：该列在
`dal.save_recommendations` 的 UPDATE 分支里被「分数提高」覆盖，故 `MIN(time)` 的真实
语义是「最后一次提分时刻」而非首次出现 —— 实测最新交易日 129 行 / 58 个写入时刻、
每轮写入 1~3 行，拿时间戳判「新票」会把这批「老票提分」全部误标成新票。
"""

import unified_scanner as us

D1 = "2026-09-21"
D0 = "2026-09-20"


def test_first_round_has_no_baseline_marks_nothing():
    """本进程首轮：无基线 ⇒ 空集。

    「当日全部产出都是新的」是平凡事实，全表打「新」等于零信息。
    """
    assert us._new_rec_symbols({"SZ300001", "SZ300002"}, set(), "", D1) == set()


def test_cross_day_has_no_same_day_baseline():
    """跨交易日：上一交易日的快照不可比（推荐池按 date 重置）⇒ 空集。

    若不这样，「昨日也推荐过」的票会在今日首轮被判成「不是新票」（它已在昨日快照里），
    这正是引入 `prev_date` 而非直接清空集合的原因。
    """
    assert us._new_rec_symbols({"SZ300001"}, {"SZ300001", "SZ300002"}, D0, D1) == set()


def test_same_day_next_round_returns_difference():
    """同日续轮：本轮 − 上一轮 = 新进池。"""
    got = us._new_rec_symbols({"SZ300001", "SZ300002", "SZ300003"}, {"SZ300001", "SZ300002"}, D1, D1)
    assert got == {"SZ300003"}


def test_same_day_unchanged_set_returns_empty():
    """同日续轮但票集未变 ⇒ 空集 ⇒ 第 1 键恒等，排序完全还原。"""
    assert us._new_rec_symbols({"SZ300001"}, {"SZ300001"}, D1, D1) == set()


def test_empty_today_syms_returns_empty():
    """取数失败（today_syms 空）⇒ 空集。

    调用方据此**不**覆盖快照（`run_scanner` 里 `if today_syms:` 那道条件）——
    若用空集覆盖，下一轮 `today_syms - set()` 会把全部票误判为新票。
    """
    assert us._new_rec_symbols(set(), {"SZ300001"}, D1, D1) == set()
