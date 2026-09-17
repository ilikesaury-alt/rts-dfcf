"""scanner.nextday_calib 离线守护：代码常数 vs 校准快照的一致性（audit §B2）。

**为什么需要这组测试**：2026-09-13 复核发现 `nextday_prob.py` 当时的 OR_MARKED 常数
被高估 67% —— 它的拟合口径漏了线上 `is_nextday_marked` 自 2026-08-14 起含的
「5 日累计门槛」，而 `tests/test_nextday_prob.py` **只断言因子方向不断言数值**，
所以漂移没有任何测试能发现。本组测试补上这道门（OR_MARKED 本身已于 2026-09-16
随 🎯 降为纯展示标记而删除，但守护机制与其余常数照常生效）：

  1. 代码里的每个常数必须与 `scanner/nextday_calib.json` 快照一致
     —— 改常数而不重算快照（`python -m scanner.nextday_calib --write`）即 fail；
  2. 快照里的 `measured` 必须能由 `hit_cond` / `hit_ref` 复算出来（防手改快照）；
  3. 快照必须新鲜（超过 `max_age_days` 即 fail，强制重算，防常数长期与数据脱钩）；
  4. 超过容差的漂移项必须在 `ACKNOWLEDGED_DRIFT` 里登记理由（防「红着没人看」，
     也防「把检查关掉」）。

全程离线：只读 JSON + import 模块，不需要 scanner.db，可在 CI 跑。
"""

from __future__ import annotations

import datetime as _dt
import math

import pytest

from scanner import nextday_calib as nc
from scanner.nextday_prob import (
    BASE_RATE_BY_CAT,
    BASE_RATE_DEFAULT,
    OR_BAND_DEAD,
    OR_BAND_MID,
    OR_BAND_SWEET_LOW,
    OR_BAND_TRAP,
    OR_OUTFLOW,
    OR_OVERBOUGHT,
    OR_PROMINENCE,
    OR_SMALL_SECTOR,
)

# 代码里的常数（与 nextday_prob 的模块级名一一对应；新增常数必须同步登记）
CODE_CONSTANTS: dict[str, float] = {
    "OR_PROMINENCE": OR_PROMINENCE,
    "OR_OVERBOUGHT": OR_OVERBOUGHT,
    "OR_BAND_SWEET_LOW": OR_BAND_SWEET_LOW,
    "OR_BAND_DEAD": OR_BAND_DEAD,
    "OR_BAND_MID": OR_BAND_MID,
    "OR_BAND_TRAP": OR_BAND_TRAP,
    "OR_OUTFLOW": OR_OUTFLOW,
    "OR_SMALL_SECTOR": OR_SMALL_SECTOR,
}


@pytest.fixture(scope="module")
def snapshot() -> dict:
    return nc.load_snapshot()


def _odds(p: float) -> float:
    return p / (1.0 - p)


# ── 1. 代码常数 ↔ 快照 ──


def test_snapshot_mirrors_code_constants(snapshot):
    """代码常数必须与快照一致——改常数必须同时 --write 重算快照。

    这是本组测试的核心：常数不再是「手抄完就没人管」的孤立数字。
    """
    for key, value in CODE_CONSTANTS.items():
        assert key in snapshot["factors"], f"快照缺因子 {key}（先跑 --write）"
        assert math.isclose(snapshot["factors"][key]["constant"], value, rel_tol=1e-6), (
            f"{key} 代码值 {value} 与快照 {snapshot['factors'][key]['constant']} 不一致："
            f"跑 `python -m scanner.nextday_calib --write` 同步快照"
        )


def test_snapshot_covers_exactly_the_known_factors(snapshot):
    """快照因子集合 = nextday_calib 声明的因子集合（双向），防新增因子漏登记。"""
    declared = {s.key for s in nc.FACTOR_SPECS}
    assert set(snapshot["factors"]) == declared
    assert set(CODE_CONSTANTS) == declared, "新增 OR 常数必须同时登记到 CODE_CONSTANTS / FACTOR_SPECS"


def test_snapshot_mirrors_base_rates(snapshot):
    """base rate 同样受守护（2026-09-13 实测 core_dip / pool_pick 高估 27%）。"""
    rates = snapshot["base_rates"]
    for cat, value in BASE_RATE_BY_CAT.items():
        assert cat in rates, f"快照缺类别 {cat}"
        assert math.isclose(rates[cat]["constant"], value, rel_tol=1e-6)
    assert math.isclose(rates["_default"]["constant"], BASE_RATE_DEFAULT, rel_tol=1e-6)


# ── 2. 快照内部自洽（防手改）──


def test_measured_or_recomputable_from_hit_rates(snapshot):
    """快照的 measured 必须能由 hit_cond / hit_ref 复算（容差取 round 到 4 位的误差）。"""
    for key, f in snapshot["factors"].items():
        hc, hr = f["hit_cond"], f["hit_ref"]
        if not hc or not hr or hc >= 1.0 or hr >= 1.0:
            continue  # 0% 或 100% 组：OR 无定义/发散，跳过
        expected = _odds(hc) / _odds(hr)
        assert math.isclose(f["measured"], expected, rel_tol=5e-3), (
            f"{key}: measured={f['measured']} 与 hit 率复算 {expected:.4f} 不符（快照被手改？）"
        )


def test_every_factor_declares_scope(snapshot):
    """每个因子必须声明适用范围（口径可审计是本模块的存在理由）。"""
    for key, f in snapshot["factors"].items():
        assert f["applies_to"], f"{key} 缺 applies_to"
        assert f["n_cond"] >= 0 and f["n_ref"] >= 0
        assert 0.0 <= (f["hit_cond"] or 0.0) <= 1.0
        assert 0.0 <= (f["hit_ref"] or 0.0) <= 1.0


# ── 3. 新鲜度 ──


def test_snapshot_is_fresh(snapshot):
    """快照过期即 fail —— 强制重算，防止常数与数据长期脱钩（这正是 B2 的病根）。"""
    generated = _dt.date.fromisoformat(snapshot["generated_at"])
    age = (_dt.date.today() - generated).days
    max_age = snapshot["max_age_days"]
    assert age <= max_age, (
        f"校准快照已 {age} 天未更新（上限 {max_age} 天）："
        f"跑 `python -m scanner.nextday_calib --write` 重算并同步 nextday_prob.py 常数"
    )


# ── 4. 漂移必须被显式处理（登记豁免或修正常数）──


def test_drift_is_either_fixed_or_acknowledged(snapshot):
    """超容差的漂移项必须已登记 ACKNOWLEDGED_DRIFT —— 不允许「悄悄红着」。"""
    tol = snapshot["tolerance"]
    for key, f in snapshot["factors"].items():
        constant, measured = f["constant"], f["measured"]
        if not constant or not measured or measured <= 0:
            continue
        drift = abs(measured - constant) / constant
        if drift > tol:
            assert key in nc.ACKNOWLEDGED_DRIFT, (
                f"{key} 漂移 {drift * 100:.1f}%（常数 {constant} vs 实测 {measured}）"
                f"既未修正也未登记豁免"
            )


def test_acknowledged_drift_is_not_stale(snapshot):
    """豁免项必须确实还在漂移——问题修好后要删豁免，否则豁免表会变成垃圾桶。"""
    tol = snapshot["tolerance"]
    for key, reason in nc.ACKNOWLEDGED_DRIFT.items():
        assert key in snapshot["factors"], f"豁免项 {key} 不是已知因子"
        f = snapshot["factors"][key]
        constant, measured = f["constant"], f["measured"]
        drift = abs(measured - constant) / constant
        assert drift > tol, f"{key} 已回到容差内（漂移 {drift * 100:.1f}%），请从 ACKNOWLEDGED_DRIFT 移除"
        assert reason.strip(), f"{key} 的豁免理由为空"
