"""次日大涨概率模型的校准常数重算与漂移巡检（2026-09-13 新增，audit §B2）。

## 为什么需要这个模块

`scanner/nextday_prob.py` 的 base rate / OR 常数此前是**手工誊写**的：跑一遍
`nextday_attribution` 报告，人眼读数、手抄进代码，两边之间没有任何自动校验。
实测后果（2026-09-13 复核）：

1. **OR_MARKED 的拟合口径与线上判据不一致（已确证；2026-09-16 随该常数删除而归档）**。
   常数 `2.6` 记的读数漏了线上 `ranking.is_nextday_marked` 自 2026-08-14 起含的
   「5 日累计 ≥ NEXTDAY_ACCUM_MIN」门槛，按线上真实口径重算仅 **1.56**（被高估 67%，
   odds 尺度）。本模块正是为抓出这类手抄漂移而生。**2026-09-16 用户决策把 🎯 降为
   纯展示标记**（不喂 OR 因子、不提 tier-0），`OR_MARKED` 已从 `nextday_prob` 与本
   模块一并删除——它既不在下方 FACTOR_SPECS / 快照中，也不再是模型的乘子。
2. **类别 base rate 末段普遍高估**：core_dip 0.089 → 实测 0.065、pool_pick
   0.028 → 0.021。
3. `tests/test_nextday_prob.py` 只断言因子**方向**不断言数值，故上述漂移不会被
   任何测试发现（本次已实证：全绿）。

## 本模块做什么

- `measure()`：按**显式声明的口径**重算全部常数。口径只有一条规则：
  `OR = odds(条件组 hit) / odds(非条件组 hit)`，两组都取在**该因子实际生效的行集合**
  （`FactorSpec.pop`）内 —— 参照组必须是适用集合的补集，否则 OR 描述的不是模型里
  实际乘上去的那个量。`legacy_scope` 字段记录遗留常数是在哪个范围拟合的，
  与 `applies_to` 不同即为**口径错位**（本模块要暴露的头号问题类别）。
- `compare()` / `print_report()`：与 `nextday_prob.py` 的现有常数逐项比对，超阈值判漂移。
- `write_snapshot()`：把「口径 + 实测值 + 常数」写成 `scanner/nextday_calib.json`，
  由 `tests/test_nextday_calib.py` **离线**守护（代码常数与快照不一致即 fail），
  并检查快照新鲜度，防止常数长期不重算。
- `ACKNOWLEDGED_DRIFT`：已量化、已评估、**故意暂不改**的漂移项豁免表（写明理由与
  解除条件）。巡检里以「已知」呈现而非「DRIFT」，退出码也不计——否则巡检长期红着
  就没人看了。新增豁免需走 code review：这是「承认问题存在」，不是「把检查关掉」。

## 为什么阈值取 15%

漂移是双向噪声：样本每月增长约 10-20%，hit 率的抽样波动在 n≈200 的分组上约
±3-5 个百分点。15% 相对阈值能放过正常抽样波动（实测 OR_PROMINENCE 9%、
OR_SMALL_SECTOR 12% 均属此类），又能抓住口径错误与真实衰减（历史上的 OR_MARKED
40%、core_dip 27%）。调低到 10% 会让巡检长期红着，反而没人看。

## 用法

    python -m scanner.nextday_calib            # 漂移巡检（需要 scanner.db），漂移即退出码 1
    python -m scanner.nextday_calib --write    # 重写 scanner/nextday_calib.json
    python -m scanner.nextday_calib --json     # 打印快照 JSON（不写盘）
    python -m scanner.nextday_calib --days 120 # 仅用最近 N 天

    python -m pytest tests/test_nextday_calib.py -q   # 离线守护（无需 DB）
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import math
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from scanner.config import (
    DB_PATH,
    FUND_OUTFLOW_NET_PCT,
    NEXTDAY_HIT_THRESHOLD,
    NEXTDAY_SPIKE_MID_MAX,
    NEXTDAY_SPIKE_MID_MIN,
    NEXTDAY_SPIKE_SWEET_LOW,
)
from scanner.models import parse_score_breakdown
from scanner.nextday_attribution import attach_prominence, load_dedup
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
from scanner.ranking import (
    _entry_fund_flow_pct,
    _entry_overbought,
    _entry_sector_resonance,
    _nextday_entry_percent,
)

SNAPSHOT_PATH = Path(__file__).with_name("nextday_calib.json")

# 漂移判定阈值（相对）：|实测 − 常数| / 常数 超过该值即判漂移，fail-loud。
DRIFT_TOLERANCE = 0.15
# 快照新鲜度上限（天）：超过即由 tests/test_nextday_calib.py 判 fail，强制重算。
SNAPSHOT_MAX_AGE_DAYS = 45
# 因子条件/参照样本下限：低于此值只报告不判漂移（噪声，与 nextday_attribution.MIN_SAMPLE 对齐）。
MIN_COND_SAMPLE = 20

# ── 已知漂移豁免（waiver）──
# 已量化、已评估、**故意暂不改**的漂移项。豁免必须写明理由与解除条件，并在
# `--check` 输出里以「已知」而非「DRIFT」呈现——否则巡检长期红着就没人看了。
# 新增豁免需走 code review：这是「承认问题存在」，不是「把检查关掉」。
ACKNOWLEDGED_DRIFT: dict[str, str] = {
    "OR_BAND_SWEET_LOW": "口径错位 + 已做样本外验证（2026-09-13）：遗留常数按「全样本条件组 vs "
                         "全体」拟合，按适用集合（非 short_term）重算为 0.653（漂移 25.8%）。"
                         "§B1 样本外判定（python -m scanner.rule_validate，test 窗 40 日、"
                         "日等权 top-3）：换实测值后 hit 无变化（Δ +0.0pp，CI [0,0]）、rank-IC "
                         "微升 +0.0122→+0.0131 —— 证据不足，暂留。"
                         "（2026-09-16 重算快照：适用集合由「未标记且非 short_term」改为"
                         "「非 short_term」，实测值随之更新。）",
    "OR_BAND_MID": "口径错位 + 已做样本外验证（2026-09-13）：按适用集合（非 short_term）重算为 2.155"
                   "（漂移 59.6%）。§B1 样本外判定（python -m scanner.rule_validate --set "
                   "scanner.nextday_prob.OR_BAND_MID=2.04）：**改实测值反而更差** —— "
                   "test hit 13.3%→11.7%（Δ −1.7pp，CI [−4.2,+0.0]），train 窗 Δ −3.3pp 且 "
                   "CI [−6.7,−0.6] **不含 0**；rank-IC +0.0122→+0.0078 亦降。三项一起改更差"
                   "（test Δ −2.5pp，train CI [−7.2,−1.1] 不含 0）。"
                   "结论：该常数是**按终选目标校准**的，不是 OR 的无偏估计 —— 不要按 OR 实测值"
                   "去「修正」它（详见 nextday_prob._band_or docstring）。"
                   "（2026-09-16 重算快照：实测值由 2.040 更新为 2.155。）",
    "OR_OVERBOUGHT": "2026-09-16 重算快照时实测 0.691（常数 0.840，漂移 17.7%）：主因是同日 "
                     "🎯 降为纯展示标记后，本因子适用集合由「未标记行」扩到「全体行」"
                     "（原 marked 行按定义非超买，加入参照组后抬高了参照组 hit）。"
                     "按纪律**不直接改常数**（改常数属行为变更，须先过 §B1 样本外验证）。"
                     "解除条件：跑 `python -m scanner.rule_validate --evaluator nextday-prob "
                     "--set scanner.nextday_prob.OR_OVERBOUGHT=<实测>` 确认无显著变差后同步。",
    "OR_OUTFLOW": "2026-09-16 重算快照时实测 1.037（常数 0.320，漂移 223.9%）—— **纯数据漂移**"
                  "（本因子的适用集合与判定自 2026-09-13 起未变）：最近数个交易日的净流出票"
                  "次日 hit 率由 1.1% 抬升到 3.8%，与参照组（3.7%）持平，即该因子在当前 regime "
                  "已**失去区分度**（不再是负向）。按纪律不直接改常数（属行为变更，须先过 §B1）。"
                  "解除条件：样本外验证确认因子已失效后，评估降权/移除（`_p` 现为展示参考量，"
                  "已不影响终选排序）。⚠ 待复核。",
}


# ── 口径声明（单一真源：报告、快照、巡检共用同一份文字）──


@dataclass(frozen=True)
class Enriched:
    """一行样本 + 全部因子判定结果（判定全部走生产单源助手，防口径漂移）。

    2026-09-16：`marked`（🎯 判定）字段随 `OR_MARKED` 因子一并删除 —— 🎯 已降为
    纯展示标记，不再是模型输入，也不再用于划分因子适用集合（overbought / band
    现对全体适用集合生效）。
    """

    category: str
    date: str
    next_day: float
    overbought: bool
    band: str
    flow: float | None
    resonance: bool
    prominent: bool | None


@dataclass(frozen=True)
class FactorSpec:
    """一个 OR 因子的口径声明。

    pop          该因子**实际生效**的行集合；条件组与参照组都在此集合内取
                 （参照 = 该集合内不满足 cond 的行），保证 OR 描述的就是模型乘上去的量。
    cond         条件（命中该因子的行）。
    applies_to   适用范围的文字描述（报告/快照用）。
    legacy_scope 遗留常数的拟合范围描述；与 applies_to 不同即口径错位。
    """

    key: str
    label: str
    applies_to: str
    pop: Callable[[Enriched], bool]
    cond: Callable[[Enriched], bool]
    legacy_scope: str | None = None
    legacy_note: str = ""

    @property
    def legacy_scope_differs(self) -> bool:
        """遗留常数是否在「非适用集合」上拟合（口径错位）。"""
        return self.legacy_scope is not None and self.legacy_scope != self.applies_to


# 涨幅带因子的常数名（与 nextday_prob 的 OR_BAND_* 一一对应）
OR_BAND_SWEET_LOW_KEY = "OR_BAND_SWEET_LOW"
OR_BAND_DEAD_KEY = "OR_BAND_DEAD"
OR_BAND_MID_KEY = "OR_BAND_MID"
OR_BAND_TRAP_KEY = "OR_BAND_TRAP"

# 适用范围文字（快照/报告共用，避免措辞漂移）
# 2026-09-16：🎯 降为纯展示标记后，模型不再有 marked/unmarked 分流 —— overbought
# 对全体生效，band 对「非 short_term」生效。原 _SCOPE_MARKABLE / _SCOPE_UNMARKED /
# _SCOPE_UNMARKED_NONSHORT 随之删除。
_SCOPE_ALL = "全体样本"
_SCOPE_ALL_NONSHORT = "非 short_term 行"
_SCOPE_FLOW_KNOWN = "资金流可得行（fund_flow_main_pct 非空）"
_SCOPE_LEGACY_ALL = "全体样本（遗留口径）"


def _band_label(percent: float) -> str:
    """涨幅带标签（边界与 nextday_prob._band_or 同源，改边界必须同步改两处）。"""
    if percent < NEXTDAY_SPIKE_SWEET_LOW:
        return OR_BAND_SWEET_LOW_KEY
    if percent < NEXTDAY_SPIKE_MID_MIN:
        return OR_BAND_DEAD_KEY
    if percent < NEXTDAY_SPIKE_MID_MAX:
        return OR_BAND_MID_KEY
    return OR_BAND_TRAP_KEY


def _all_nonshort(e: Enriched) -> bool:
    return e.category != "short_term"


def _all(_e: Enriched) -> bool:
    return True


def _flow_known(e: Enriched) -> bool:
    return e.flow is not None


_BAND_LEGACY_NOTE = (
    "遗留常数按「全样本条件组 vs 全体样本」拟合（文档各带 n 之和 = 全样本），"
    "而模型只对非 short_term 行乘该 OR —— 参照组不是适用集合的补集。"
    "★ 2026-09-13 §B1 样本外判定结论：**不按实测值修正**（改后终选 hit 更差）；"
    "这些常数是按终选目标校准的，不是 OR 的无偏估计。详见 ACKNOWLEDGED_DRIFT。"
)

FACTOR_SPECS: tuple[FactorSpec, ...] = (
    FactorSpec(
        "OR_PROMINENCE",
        "辨识度（↻ 反复上榜）",
        applies_to=_SCOPE_ALL,
        pop=_all,
        cond=lambda e: e.prominent is True,
    ),
    FactorSpec(
        "OR_OVERBOUGHT",
        "超买死亡信号",
        applies_to=_SCOPE_ALL,
        pop=_all,
        cond=lambda e: e.overbought,
        legacy_note="2026-09-13 口径修正（已完成）：原常数 0.68 按全样本补集拟合"
                    "（含 marked 行），按适用集合（未标记行）重算为 0.84。2026-09-16 起"
                    "🎯 分流删除，本因子对全体生效（口径随之扩到含原 marked 行）。",
    ),
    FactorSpec(
        OR_BAND_SWEET_LOW_KEY,
        "涨幅带 <2%（低吸）",
        applies_to=_SCOPE_ALL_NONSHORT,
        pop=_all_nonshort,
        cond=lambda e: e.band == OR_BAND_SWEET_LOW_KEY,
        legacy_scope=_SCOPE_LEGACY_ALL,
        legacy_note=_BAND_LEGACY_NOTE,
    ),
    FactorSpec(
        OR_BAND_DEAD_KEY,
        "涨幅带 2-4%（死区）",
        applies_to=_SCOPE_ALL_NONSHORT,
        pop=_all_nonshort,
        cond=lambda e: e.band == OR_BAND_DEAD_KEY,
        legacy_scope=_SCOPE_LEGACY_ALL,
        legacy_note=_BAND_LEGACY_NOTE,
    ),
    FactorSpec(
        OR_BAND_MID_KEY,
        "涨幅带 4-8%（甜蜜中段）",
        applies_to=_SCOPE_ALL_NONSHORT,
        pop=_all_nonshort,
        cond=lambda e: e.band == OR_BAND_MID_KEY,
        legacy_scope=_SCOPE_LEGACY_ALL,
        legacy_note=_BAND_LEGACY_NOTE,
    ),
    FactorSpec(
        OR_BAND_TRAP_KEY,
        "涨幅带 ≥8%（陷阱）",
        applies_to=_SCOPE_ALL_NONSHORT,
        pop=_all_nonshort,
        cond=lambda e: e.band == OR_BAND_TRAP_KEY,
        legacy_scope=_SCOPE_LEGACY_ALL,
        legacy_note=_BAND_LEGACY_NOTE,
    ),
    FactorSpec(
        "OR_OUTFLOW",
        f"主力净流出 ≤{FUND_OUTFLOW_NET_PCT:.0f}%",
        applies_to=_SCOPE_FLOW_KNOWN,
        pop=_flow_known,
        cond=lambda e: e.flow is not None and e.flow <= FUND_OUTFLOW_NET_PCT,
    ),
    FactorSpec(
        "OR_SMALL_SECTOR",
        "小板块共振",
        applies_to=_SCOPE_ALL,
        pop=_all,
        cond=lambda e: e.resonance,
    ),
)

# 常数镜像（快照与巡检共用；新增因子必须同时登记到 FACTOR_SPECS 与本表）
CONSTANT_BY_KEY: dict[str, float] = {
    "OR_PROMINENCE": OR_PROMINENCE,
    "OR_OVERBOUGHT": OR_OVERBOUGHT,
    "OR_BAND_SWEET_LOW": OR_BAND_SWEET_LOW,
    "OR_BAND_DEAD": OR_BAND_DEAD,
    "OR_BAND_MID": OR_BAND_MID,
    "OR_BAND_TRAP": OR_BAND_TRAP,
    "OR_OUTFLOW": OR_OUTFLOW,
    "OR_SMALL_SECTOR": OR_SMALL_SECTOR,
}


# ── 样本装载 ──


def load_enriched(conn: sqlite3.Connection, days: int = 0) -> list[Enriched]:
    """装载校准样本并预计算全部因子判定（口径单源：复用 nextday_attribution + ranking）。

    `_candidate` 置 None + `score_breakdown` 填 parsed dict —— 与
    scripts/review_tier_replay.py 的历史回放口径一致：历史行没有候选池对象，
    `entry_dims` 走 DB score_breakdown 分支，即推荐时刻落库口径。
    """
    recs = load_dedup(conn, days=days)
    attach_prominence(conn, recs)
    for r in recs:
        # load_dedup 的 breakdown 是 DB 原始值（JSON 字符串），须经 parse_score_breakdown
        # 转 dict —— entry_dims 只认 dict，直接赋值会让所有维度因子静默判 False。
        r["score_breakdown"] = parse_score_breakdown(r.get("breakdown"))
        r["_candidate"] = None
    out: list[Enriched] = []
    for e in recs:
        if e["next_day"] is None:
            continue
        out.append(
            Enriched(
                category=e["category"],
                date=e["date"],
                next_day=float(e["next_day"]),
                overbought=bool(_entry_overbought(e)),
                band=_band_label(_nextday_entry_percent(e)),
                flow=_entry_fund_flow_pct(e),
                resonance=bool(_entry_sector_resonance(e)),
                prominent=e.get("_prominent"),
            )
        )
    return out


# ── 度量 ──


def _hit_rate(rows: list[Enriched], threshold: float) -> tuple[int, float]:
    if not rows:
        return 0, 0.0
    hits = sum(1 for r in rows if r.next_day >= threshold)
    return hits, hits / len(rows)


def _odds(p: float) -> float:
    return p / (1.0 - p) if 0.0 < p < 1.0 else float("nan")


@dataclass(frozen=True)
class Measurement:
    key: str
    label: str
    applies_to: str
    legacy_scope: str | None
    legacy_scope_differs: bool
    n_cond: int
    hit_cond: float
    n_ref: int
    hit_ref: float
    or_: float
    constant: float
    legacy_note: str = ""

    @property
    def drift(self) -> float:
        """相对漂移 |实测 − 常数| / 常数；常数非法或实测不可算时返回 inf（强制告警）。"""
        if not self.constant or self.constant <= 0 or not math.isfinite(self.or_):
            return float("inf")
        return abs(self.or_ - self.constant) / self.constant

    @property
    def is_drift(self) -> bool:
        if self.n_cond < MIN_COND_SAMPLE or self.n_ref < MIN_COND_SAMPLE:
            return False  # 样本不足只报告不判定（与 nextday_attribution.MIN_SAMPLE 同纪律）
        return self.drift > DRIFT_TOLERANCE

    @property
    def is_acknowledged(self) -> bool:
        """已登记豁免（见 ACKNOWLEDGED_DRIFT）：仍是漂移，但不该让巡检红着。"""
        return self.key in ACKNOWLEDGED_DRIFT

    @property
    def is_unacknowledged_drift(self) -> bool:
        return self.is_drift and not self.is_acknowledged


def measure(recs: list[Enriched], threshold: float = NEXTDAY_HIT_THRESHOLD) -> list[Measurement]:
    """按 FACTOR_SPECS 声明的口径逐因子重算 OR（条件组与参照组都在适用集合内取）。"""
    out: list[Measurement] = []
    for spec in FACTOR_SPECS:
        pop = [e for e in recs if spec.pop(e)]
        cond = [e for e in pop if spec.cond(e)]
        ref = [e for e in pop if not spec.cond(e)]
        _, hr_c = _hit_rate(cond, threshold)
        _, hr_r = _hit_rate(ref, threshold)
        odds_r = _odds(hr_r)
        out.append(
            Measurement(
                key=spec.key,
                label=spec.label,
                applies_to=spec.applies_to,
                legacy_scope=spec.legacy_scope,
                legacy_scope_differs=spec.legacy_scope_differs,
                n_cond=len(cond),
                hit_cond=hr_c,
                n_ref=len(ref),
                hit_ref=hr_r,
                or_=_odds(hr_c) / odds_r if odds_r else float("nan"),
                constant=CONSTANT_BY_KEY.get(spec.key, float("nan")),
                legacy_note=spec.legacy_note,
            )
        )
    return out


def measure_base_rates(
    recs: list[Enriched], threshold: float = NEXTDAY_HIT_THRESHOLD
) -> tuple[dict[str, float], float]:
    """分策略 base rate + 全体兜底率。"""
    by_cat: dict[str, list[Enriched]] = defaultdict(list)
    for r in recs:
        by_cat[r.category].append(r)
    rates = {cat: _hit_rate(g, threshold)[1] for cat, g in sorted(by_cat.items())}
    return rates, _hit_rate(recs, threshold)[1]


def sample_meta(recs: list[Enriched], threshold: float = NEXTDAY_HIT_THRESHOLD) -> dict[str, Any]:
    days = sorted({r.date[:10] for r in recs})
    _, hr = _hit_rate(recs, threshold)
    return {
        "n": len(recs),
        "days": len(days),
        "threshold": threshold,
        "hit_rate": round(hr, 4),
        "data_from": days[0] if days else None,
        "data_through": days[-1] if days else None,
    }


# ── 快照 ──


def _safe_round(v: float, nd: int = 4) -> float | None:
    """非有限值 → None（JSON 里写 NaN 非法，会让快照无法被严格 JSON 解析器读取）。"""
    return round(v, nd) if isinstance(v, (int, float)) and math.isfinite(v) else None


def build_snapshot(
    recs: list[Enriched],
    *,
    generated_at: str | None = None,
    threshold: float = NEXTDAY_HIT_THRESHOLD,
) -> dict[str, Any]:
    """构造快照 dict（口径 + 实测值 + 常数 + 样本元信息）。"""
    ms = measure(recs, threshold)
    rates, default_rate = measure_base_rates(recs, threshold)
    meta = sample_meta(recs, threshold)
    return {
        "generated_at": generated_at or _dt.date.today().isoformat(),
        "data_through": meta["data_through"],
        "sample": meta,
        "tolerance": DRIFT_TOLERANCE,
        "max_age_days": SNAPSHOT_MAX_AGE_DAYS,
        "acknowledged_drift": ACKNOWLEDGED_DRIFT,
        "factors": {
            m.key: {
                "constant": _safe_round(m.constant),
                "measured": _safe_round(m.or_),
                "n_cond": m.n_cond,
                "hit_cond": _safe_round(m.hit_cond),
                "n_ref": m.n_ref,
                "hit_ref": _safe_round(m.hit_ref),
                "applies_to": m.applies_to,
                "legacy_scope": m.legacy_scope,
                "legacy_scope_differs": m.legacy_scope_differs,
                "legacy_note": m.legacy_note,
            }
            for m in ms
        },
        "base_rates": {
            cat: {
                "constant": _safe_round(BASE_RATE_BY_CAT.get(cat, float("nan"))),
                "measured": _safe_round(val),
            }
            for cat, val in rates.items()
        }
        | {
            "_default": {
                "constant": _safe_round(BASE_RATE_DEFAULT),
                "measured": _safe_round(default_rate),
            }
        },
    }


def write_snapshot(snapshot: dict[str, Any], path: Path = SNAPSHOT_PATH) -> Path:
    path.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    return path


def load_snapshot(path: Path = SNAPSHOT_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


# ── 巡检报告 ──


def _fmt_drift(m: Measurement) -> str:
    if not math.isfinite(m.or_):
        return "   n/a"
    if m.n_cond < MIN_COND_SAMPLE or m.n_ref < MIN_COND_SAMPLE:
        return f"{m.drift * 100:6.1f}% 低信"
    if not m.is_drift:
        return f"{m.drift * 100:6.1f}%   ok "
    return f"{m.drift * 100:6.1f}% {'已知' if m.is_acknowledged else 'DRIFT'}"


def print_report(ms: list[Measurement], rates: dict[str, float], default_rate: float,
                 meta: dict[str, Any]) -> None:
    print("=" * 98)
    print(f"次日大涨概率模型 · 校准漂移巡检（样本 {meta['n']} / {meta['days']} 交易日 / "
          f"截至 {meta['data_through']} / threshold≥{meta['threshold']:.0f}% / "
          f"容差 {DRIFT_TOLERANCE * 100:.0f}%）")
    print("=" * 98)
    print(f"{'因子':<24}{'常数':>8}{'实测':>8}{'漂移':>12}{'n_cond':>8}{'hit_cond':>10}"
          f"{'n_ref':>7}{'hit_ref':>9}")
    print("-" * 98)
    for m in ms:
        print(f"{m.key:<24}{m.constant:>8.3f}{m.or_:>8.3f}{_fmt_drift(m):>12}"
              f"{m.n_cond:>8}{m.hit_cond * 100:>9.1f}%{m.n_ref:>7}{m.hit_ref * 100:>8.1f}%")

    print("\n类别 base rate：")
    print(f"{'类别':<20}{'常数':>10}{'实测':>10}{'漂移':>10}")
    print("-" * 50)
    for cat, val in rates.items():
        c = BASE_RATE_BY_CAT.get(cat)
        if c is None:
            print(f"{cat:<20}{'—':>10}{val:>10.4f}{'（未登记）':>10}")
            continue
        print(f"{cat:<20}{c:>10.4f}{val:>10.4f}{abs(val - c) / c * 100:>9.1f}%")
    c = BASE_RATE_DEFAULT
    print(f"{'_default':<20}{c:>10.4f}{default_rate:>10.4f}"
          f"{abs(default_rate - c) / c * 100:>9.1f}%")

    drifted = [m for m in ms if m.is_unacknowledged_drift]
    acknowledged = [m for m in ms if m.is_acknowledged and m.is_drift]
    rate_drift = [cat for cat, val in rates.items()
                  if cat in BASE_RATE_BY_CAT and BASE_RATE_BY_CAT[cat]
                  and abs(val - BASE_RATE_BY_CAT[cat]) / BASE_RATE_BY_CAT[cat] > DRIFT_TOLERANCE]
    if abs(default_rate - c) / c > DRIFT_TOLERANCE:
        rate_drift.append("_default")
    mismatched = [m for m in ms if m.legacy_scope_differs]

    print()
    if mismatched:
        print(f"  [口径错位] {len(mismatched)} 项：遗留常数的拟合范围 ≠ 模型适用范围")
        for m in mismatched:
            print(f"    - {m.key}: 适用={m.applies_to} / 遗留拟合={m.legacy_scope}")
        print("      ⇒ 参照组不是适用集合的补集时，OR 描述的不是模型实际乘上去的量。"
              "重算属方法学变更，须先过样本外验证（§B1）。")
        print("      ★ 2026-09-13 已执行 §B1 样本外判定：**结论是不改** —— 换成实测值后终选"
              " top-3 hit 反而下降（OR_BAND_MID test Δ −1.7pp、train CI [−6.7,−0.6] 不含 0）。"
              "这些常数是按终选目标校准的，不是 OR 的无偏估计。证据见 ACKNOWLEDGED_DRIFT。")
    if acknowledged:
        print(f"  [已知] {len(acknowledged)} 项漂移已登记豁免（ACKNOWLEDGED_DRIFT）：")
        for m in acknowledged:
            print(f"    - {m.key}: 常数 {m.constant:.3f} → 实测 {m.or_:.3f}"
                  f"（漂移 {m.drift * 100:.1f}%）")
    if drifted or rate_drift:
        print(f"  [漂移] OR 因子 {len(drifted)} 项 / base rate {len(rate_drift)} 项超出容差：")
        for m in drifted:
            print(f"    - {m.key}: 常数 {m.constant:.3f} → 实测 {m.or_:.3f}"
                  f"（漂移 {m.drift * 100:.1f}%，n={m.n_cond}/{m.n_ref}）")
        if rate_drift:
            print(f"    - base rate: {', '.join(rate_drift)}")
        print("  处置：确认口径无误后用 --write 重写快照并同步 nextday_prob.py 常数；"
              "改常数属行为变更，须先过样本外验证（见 audit §B1）。")
    elif not mismatched and not acknowledged:
        print("  [ok] 全部因子与 base rate 均在容差内，且无口径错位。")
    print("\n  逐项口径见 FACTOR_SPECS / 快照 factors[*].applies_to & legacy_scope。")
    print(f"  快照 {SNAPSHOT_PATH.name} 新鲜度上限 {SNAPSHOT_MAX_AGE_DAYS} 天"
          f"（tests/test_nextday_calib.py 守护）。")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="次日大涨概率模型 · 校准常数重算与漂移巡检")
    p.add_argument("--db", default=DB_PATH, help="SQLite 库路径")
    p.add_argument("--days", type=int, default=0, help="仅用最近 N 天（0=全部）")
    p.add_argument("--write", action="store_true", help="重写 scanner/nextday_calib.json")
    p.add_argument("--json", action="store_true", help="打印快照 JSON（不写盘）")
    return p


def main() -> int:
    args = build_parser().parse_args()
    conn = sqlite3.connect(args.db)
    try:
        recs = load_enriched(conn, days=args.days)
    finally:
        conn.close()
    if not recs:
        print("  [中止] 无可用样本（检查 --db / recommendations.next_day_pct 是否已回填）")
        return 1
    if args.json:
        print(json.dumps(build_snapshot(recs), ensure_ascii=False, indent=2))
        return 0
    ms = measure(recs)
    rates, default_rate = measure_base_rates(recs)
    print_report(ms, rates, default_rate, sample_meta(recs))
    if args.write:
        path = write_snapshot(build_snapshot(recs))
        print(f"\n[写入] {path}")
        return 0
    rate_drift = (
        any(cat in BASE_RATE_BY_CAT and BASE_RATE_BY_CAT[cat]
            and abs(v - BASE_RATE_BY_CAT[cat]) / BASE_RATE_BY_CAT[cat] > DRIFT_TOLERANCE
            for cat, v in rates.items())
        or abs(default_rate - BASE_RATE_DEFAULT) / BASE_RATE_DEFAULT > DRIFT_TOLERANCE
    )
    return 1 if (any(m.is_unacknowledged_drift for m in ms) or rate_drift) else 0


if __name__ == "__main__":
    raise SystemExit(main())
