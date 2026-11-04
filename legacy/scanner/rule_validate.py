"""规则/常数改动的样本外验证门（audit §B1 落地，2026-09-13 新增）。

## 为什么需要这个模块

项目实测有 **~3,000 个有效信号（76 交易日 / 2,185 去重样本）对照 ~1,500 个可调数值**
（`config.py` 395 常量 + 评分核心 1,131 数值字面量），但**没有任何工具回答
「这个改动是真信号还是噪声」**：

- `scanner.walkforward` 检验的是**固定结论**（8 个硬编码因子谓词）跨窗是否稳定，
  不检验「我这次改的常数」；
- `scripts/rule_change_tracker.py` 明确写着「退出码恒为 0，不阻断构建/提交」；
- `scanner.portfolio_backtest` 自己声明「**禁止**拿它去调权重」。

后果：任何改动都能找到一个「指标变好」的窗口，而 76 天样本下这极可能是噪声。
`nextday_prob.py` 的 docstring 自己写着「注释数字已两次过期」——那不是文档维护问题，
是**参数多于信息量**的典型症状。

## 本模块做什么

给定一组改动（`--set MODULE.ATTR=VALUE`，可重复），在**滚动 train/test 窗（带 embargo）**
上分别评估「改动前 vs 改动后」，并对**样本外**差值做**按日配对 bootstrap**，
输出带置信区间的判定与退出码。

## 三条纪律（本模块的全部价值所在）

1. **样本外优先**。判定只看 test 窗；train 窗的 Δ 一并打印，用于**暴露过拟合**
   （in-sample 改善 / out-of-sample 不改善 = 典型过拟合）。
2. **按日等权**。日级统计先算每日指标再平均——项目每日样本量 min=1 / median=38 /
   max=572，按行汇总会让「只有 1 票的交易日」和「572 票的交易日」等权。
   工具同时打印两种口径的差值，直接量化这个扭曲。
3. **评估器可见性硬校验**。`--set` 改的模块若不在所选评估器的「可见集合」内，
   **直接报错退出**——防止「验证了一个根本没生效的改动」这种最常见的自欺。
   例：改 `scanner.config` 的权重却用 `stored-score` 评估器 → 落库分是冻结的旧权重，
   改动根本不会体现在指标里。

## 统计口径

主指标 = **样本外日等权 top-N 次日 hit 率**（N 默认 3 = 名次带宽度，不是任何线上名单宽度）。
显著性 = 按日配对 bootstrap（重采样交易日，B 默认 2000）：
  - `Δ` 的 95% CI 下界 > 0 → **样本外支持**（退出码 0）
  - `Δ` 的 95% CI 上界 < 0 → **样本外显著变差**（退出码 2）
  - 否则 → **证据不足，拒绝**（退出码 1）
另报告 **MDE（最小可检测效应）** = 1.96 × bootstrap 标准误 —— 它正面回答
「以当前样本量，多小的改善才可能被检出」，这是本项目最该先看的一个数。

**⚠ MDE 的重要限制（2026-09-14 修正）**：MDE 由「观测到的配对差值」的 bootstrap
标准误得来，因此**在改动无效果时必然退化为 0**。这不是实现 bug，而是该估计量的
固有性质——但把 `0.0pp` 原样打印出来会被读成「灵敏度无穷大」，与本工具的核心判据
「MDE 远大于 Δ 时 Δ 无法与噪声区分」直接冲突（MDE=0 时该判据永不触发）。

故 `se ≈ 0` 时 `mde_estimable=False`，报告打印 **n/a** 而非 0.0，并同时给出
**「改动实际翻转了 X/N 个交易日的 top-N 结果」**——这才是可检测性的真正来源：
翻转天数为 0 时，无论样本量多大，检出概率都是 0。

> 推论：**不要指望「基线自检」能告诉你 MDE**。空操作自检的翻转天数恒为 0，
> MDE 恒不可估。要估计真实可检测下限，须用能实际翻转 top-N 的扰动量级再跑一次
> （实测本项目：能翻转 top-N 的扰动下 MDE ≈ 2.3pp，而基线自检报 n/a）。

## 用法

    # 1) 基线自检（不改任何东西）：确认样本量/窗口/基线指标正常。
    #    注意：本步的 MDE 恒为 n/a（改动零翻转），不能用来判断可检测性。
    python -m scanner.rule_validate

    # 1b) 想看真实可检测下限：用一个能翻转 top-N 的大扰动跑一次，
    #     观察其 MDE 与「翻转了 X/N 个交易日」。
    python -m scanner.rule_validate --set scanner.nextday_prob.OR_OVERBOUGHT=5.0

    # 2) 验证一个改动（改 nextday_prob 的常数，用概率排序评估器）
    python -m scanner.rule_validate --set scanner.nextday_prob.OR_OVERBOUGHT=1.56

    # 3) 验证 config 权重/阈值改动（必须用 rescore，否则报错）
    python -m scanner.rule_validate --evaluator rescore --set scanner.config.MIN_SCORE=60

    # 4) 机器可读
    python -m scanner.rule_validate --set ... --json

## 退出码

    0 = 样本外支持该改动
    1 = 证据不足（默认拒绝——这正是 B1 想要的默认行为）
    2 = 样本外显著变差
    3 = 用法/可见性错误（改的模块评估器看不见，结论无效）
"""

from __future__ import annotations

import argparse
import importlib
import json
import math
import random
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable

from scanner.config import DB_PATH, NEXTDAY_HIT_THRESHOLD, WF_EMBARGO_DAYS
from scanner.models import parse_score_breakdown
from scanner.nextday_attribution import attach_prominence, load_dedup
from scanner.walkforward import walkforward_windows

# ── 默认参数 ──
# N = 主指标的**名次带宽度**（每天取概率最高的 N 只算 hit），不是某个线上名单的宽度：
# 2026-09-21 终选参考区（scanner/final_pick）整体删除后，系统已不存在任何「短名单」概念。
# 仍取 3 是为了**与历史报告可比** —— 2026-09-08~2026-09-21 期间 N 曾硬等于终选宽度
# FINAL_PICK_MAX(=3)，旧报告全部按 3 只/天计算；改成别处派生会让新旧报告失去可比性。
# （沿革：首版把 2 写死，而终选宽度曾在 2026-09-08 放宽为 3，两边静默失配 —— 这就是
#  原先直接引用 FINAL_PICK_MAX 的原因；现在那个上游没了，故把值就地定死并写明来历。）
DEFAULT_TOP_N = 3
DEFAULT_TRAIN_DAYS = 30
DEFAULT_TEST_DAYS = 10
DEFAULT_BOOT = 2000
DEFAULT_ALPHA = 0.05
DEFAULT_SEED = 42

EXIT_SUPPORTED = 0
EXIT_INSUFFICIENT = 1
EXIT_WORSE = 2
EXIT_USAGE = 3


# ── 评估器：声明「能看见哪些模块的改动」+ 逐日打分 ──

EVALUATORS: dict[str, dict[str, Any]] = {
    "stored-score": {
        "desc": "用 recommendations 落库冻结分排序（不反映任何运行时改动）",
        "sees": frozenset(),  # 冻结分：改了代码也不会变，故可见集合为空
        "note": "仅作对照/回归基线。任何会影响评分的 --set 都会被可见性校验拒绝。",
    },
    "nextday-prob": {
        "desc": "用 nextday_prob.next_day_hit_probability 的 _p 排序（类别 hit 先验口径）",
        "sees": frozenset({"scanner.nextday_prob"}),
        "note": "覆盖 nextday_prob 常数；不反映评分链（config/analysis/...）的改动。",
    },
    "rescore": {
        "desc": "用 historical_rescan 以当前（含 override）config 重算 score 排序",
        "sees": frozenset(
            {
                "scanner.config",
                "scanner.weights",
                "scanner.analysis",
                "scanner.enhancer",
                "scanner.validator",
                "scanner.ranking",
                "scanner.categories",
            }
        ),
        "note": "覆盖评分/门禁/权重改动；代价是每次评估都要重跑历史重扫（数十秒）。",
    },
}
DEFAULT_EVALUATOR = "nextday-prob"


# ── 纯统计函数（离线可测）──


def wilson_interval(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson 区间（比正态近似在小样本/极端比例下稳）。n=0 返回 (0,0)。"""
    if n <= 0:
        return 0.0, 0.0
    p = k / n
    denom = 1.0 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, center - half), min(1.0, center + half)


def day_equal_mean(values: list[float]) -> float:
    """按日等权平均（先算每日指标再平均）。空列表返回 0.0。"""
    return sum(values) / len(values) if values else 0.0


def pooled_rate(hits: int, n: int) -> float:
    """按行汇总比率（小样本日与大样本日等权）。n=0 返回 0.0。"""
    return hits / n if n else 0.0


# 判定「配对差值无离散度」的绝对容差。不能用 `se == 0.0` 或 `se > 0.0`：
# 常数差值（如每天都恰好 +0.1）经浮点累加后 se 会落在 ~1e-17 量级，
# 既不为 0 又远小于任何真实噪声，会把「不可估」误判为「可估」。
# 日 hit 率以 1/top_n 为步长，真实 se 的量级远大于 1e-9，该容差是安全的。
SE_ZERO_EPS = 1e-9


def bootstrap_mean_ci(
    deltas: list[float],
    n_boot: int = DEFAULT_BOOT,
    alpha: float = DEFAULT_ALPHA,
    seed: int = DEFAULT_SEED,
) -> dict[str, float]:
    """按日配对 bootstrap：对 deltas 重采样交易日，返回 CI 与单边 p。

    配对 = 同一天同时算「改动前」「改动后」，差值只含该天内部的对比，
    因此天然吸收市场 regime 的日间波动（这是本工具能做样本外检验的前提）。

    返回 {mean, lo, hi, p_le_zero, se, mde, mde_estimable}
      p_le_zero   ：bootstrap 分布中 mean(Δ) ≤ 0 的比例（单边 p，越小越支持改动）
      mde         ：最小可检测效应 = 1.96 × se —— 「以当前样本量能检出多大的改善」
      mde_estimable：MDE 是否可估。se == 0 时为 False —— 此时「配对差值」无离散度
                    （典型场景：改动是空操作，或改动弱到没翻转任何一天的 top-N），
                    mde 会退化成 0.0，**绝不能当作「任何改善都能检出」来读**。

    2026-09-14 修复（MDE 退化）：mde 由「观测到的配对差值」的 bootstrap 标准误得来，
    因此**在改动无效果时必然退化为 0**。这不是 bug 而是该估计量的固有性质，
    但把 0.0pp 原样打印出来会被读成「灵敏度无穷大」，与本工具的核心判据
    「MDE 远大于 Δ 时 Δ 无法与噪声区分」直接冲突（MDE=0 时该判据永不触发）。
    现补 mde_estimable 标记，由 render 在不可估时打印 n/a 并说明原因。
    """
    n = len(deltas)
    if n == 0:
        return {
            "mean": 0.0,
            "lo": 0.0,
            "hi": 0.0,
            "p_le_zero": 1.0,
            "se": float("inf"),
            "mde": float("inf"),
            "mde_estimable": False,
            "n_days": 0,
        }
    obs = sum(deltas) / n
    rng = random.Random(seed)  # noqa: S311 - 仅用于可复现的 bootstrap 重采样，无安全用途
    means: list[float] = []
    for _ in range(n_boot):
        s = 0.0
        for _ in range(n):
            s += deltas[rng.randrange(n)]
        means.append(s / n)
    means.sort()
    lo = means[max(0, int(math.floor(alpha / 2 * n_boot)))]
    hi = means[min(n_boot - 1, int(math.ceil((1 - alpha / 2) * n_boot)) - 1)]
    p_le_zero = sum(1 for m in means if m <= 0.0) / n_boot
    var = sum((m - obs) ** 2 for m in means) / max(1, n_boot - 1)
    se = math.sqrt(var)
    return {
        "mean": obs,
        "lo": lo,
        "hi": hi,
        "p_le_zero": p_le_zero,
        "se": se,
        "mde": 1.96 * se,
        "mde_estimable": se > SE_ZERO_EPS,
        "n_days": n,
    }


def verdict_from_ci(ci: dict[str, float]) -> tuple[int, str]:
    """CI → 判定与退出码。默认拒绝（证据不足）—— 这是 B1 想要的默认行为。"""
    if not ci.get("n_days"):
        return EXIT_INSUFFICIENT, "证据不足（无可配对交易日）"
    if ci["lo"] > 0.0:
        return EXIT_SUPPORTED, "样本外支持该改动"
    if ci["hi"] < 0.0:
        return EXIT_WORSE, "样本外显著变差"
    return EXIT_INSUFFICIENT, "证据不足（CI 跨 0，默认拒绝）"


# ── 逐日指标 ──


def day_topn_hit(
    by_day: dict[str, list[tuple[float, float]]], top_n: int, threshold: float
) -> tuple[dict[str, float], int, int]:
    """逐日 top-N 次日 hit 率。输入 {date: [(score, next_day), ...]}。

    返回 (每日 hit 率, 命中数, 取用数)。score 降序取前 top_n。
    """
    per_day: dict[str, float] = {}
    hits = taken = 0
    for d, lst in by_day.items():
        if not lst:
            continue
        top = sorted(lst, key=lambda x: -x[0])[:top_n]
        h = sum(1 for _s, nd in top if nd >= threshold)
        per_day[d] = h / len(top)
        hits += h
        taken += len(top)
    return per_day, hits, taken


# 日 IC 的最小样本：与 backtest.spearman 的实际门槛一致（<5 点它直接返回 None）。
# 写死 3 会留下「3~4 点的日子静默被丢」的错觉——守卫必须与下游契约对齐。
MIN_IC_POINTS = 5


def day_rank_ic(by_day: dict[str, list[tuple[float, float]]]) -> dict[str, float]:
    """逐日 Spearman(score, next_day)（按日等权的 IC）。并列值用平均秩。

    少于 `MIN_IC_POINTS` 点的交易日直接跳过（`spearman` 对 <5 点返回 None）。
    """
    from scanner.backtest import spearman

    out: dict[str, float] = {}
    for d, lst in by_day.items():
        if len(lst) < MIN_IC_POINTS:
            continue
        ic = spearman([x[0] for x in lst], [x[1] for x in lst])
        if ic is not None:
            out[d] = ic
    return out


# ── override 解析与应用 ──


@dataclass
class Override:
    module: str
    attr: str
    value: Any
    raw: str


def parse_override(spec: str) -> Override:
    """解析 `MODULE.ATTR=VALUE`。VALUE 按 Python 字面量解析（int/float/bool/str）。"""
    if "=" not in spec:
        raise ValueError(f"--set 需要 MODULE.ATTR=VALUE 形式：{spec!r}")
    lhs, rhs = spec.split("=", 1)
    if "." not in lhs:
        raise ValueError(f"--set 左侧需要模块.属性：{spec!r}")
    module, attr = lhs.rsplit(".", 1)
    text = rhs.strip()
    try:
        value = json.loads(text)  # 覆盖数字/true/false/null/字符串
    except json.JSONDecodeError:
        value = text
    if isinstance(value, float) and value.is_integer() and "." not in text and "e" not in text.lower():
        value = int(value)
    return Override(module=module.strip(), attr=attr.strip(), value=value, raw=spec)


def check_visibility(overrides: list[Override], evaluator: str) -> list[str]:
    """可见性硬校验：改动模块必须在该评估器的可见集合内，否则结论无效。

    这是本模块最重要的一道防线——「验证了一个根本没生效的改动」是调参时最常见的自欺。
    """
    sees = EVALUATORS[evaluator]["sees"]
    problems: list[str] = []
    for ov in overrides:
        if ov.module not in sees:
            problems.append(
                f"{ov.raw}：改动模块 {ov.module} 不在评估器 {evaluator} 的可见集合内 "
                f"（可见：{sorted(sees) or '无'}）→ 该改动不会体现在指标里，验证结论无效"
            )
    return problems


def _same(a: Any, b: Any) -> bool:
    """宽松相等（`is` 优先，`==` 兜底且不因 numpy 风格返回数组而炸）。"""
    if a is b:
        return True
    try:
        return bool(a == b)
    except Exception:  # noqa: BLE001 - 比较本身可能对任意对象失败，退化为 is
        return False


# 项目内模块前缀：`from scanner.config import X` 把值**绑定进导入方命名空间**
# （快照式导入），之后只改 scanner.config.X 不会影响导入方 —— 这是 Python 的经典
# 陷阱，也是「验证了一个根本没生效的改动」的主要来源。故 override 必须把仍持有
# 同一常量的项目内模块副本一并改写。只传播到 scanner.* 内，范围可控。
_PROPAGATE_PREFIX = "scanner."


def _patch_consumers(module: str, attr: str, old: Any, new: Any) -> list[str]:
    """把「同名且仍是旧值」的项目内模块副本改写成 new，返回被改写的模块名。"""
    patched: list[str] = []
    for name, mod in list(sys.modules.items()):
        if mod is None or name == module or not name.startswith(_PROPAGATE_PREFIX):
            continue
        if not hasattr(mod, attr):
            continue
        if _same(getattr(mod, attr), old):
            setattr(mod, attr, new)
            patched.append(name)
    return patched


def apply_overrides(overrides: list[Override]) -> list[dict[str, Any]]:
    """应用 override（含消费方传播），返回可用于恢复的变更记录。

    返回 [{"spec", "module", "attr", "old", "new", "patched": [模块名...]}, ...]。
    `patched` 为空说明没有别的模块持有该常量（要么本来就只此一处，
    要么改错了模块）—— 报告里会显式打印，便于判断改动是否真的生效。
    """
    journal: list[dict[str, Any]] = []
    for ov in overrides:
        mod = importlib.import_module(ov.module)
        if not hasattr(mod, ov.attr):
            raise AttributeError(f"{ov.module}.{ov.attr} 不存在——检查拼写")
        old = getattr(mod, ov.attr)
        setattr(mod, ov.attr, ov.value)
        journal.append(
            {
                "spec": ov.raw,
                "module": ov.module,
                "attr": ov.attr,
                "old": old,
                "new": ov.value,
                "patched": _patch_consumers(ov.module, ov.attr, old, ov.value),
            }
        )
    return journal


def restore_overrides(journal: list[dict[str, Any]]) -> None:
    """按变更记录恢复（含消费方）。逆序恢复，避免同名常量互相覆盖。"""
    for rec in reversed(journal):
        target = importlib.import_module(rec["module"])
        setattr(target, rec["attr"], rec["old"])
        for name in rec["patched"]:
            mod = sys.modules.get(name)
            if mod is not None:
                setattr(mod, rec["attr"], rec["old"])


# ── 样本与打分 ──


@dataclass
class Sample:
    """一条可评估记录：日期 + 标的 + 次日收益 + 打分所需字段。"""

    date: str
    symbol: str
    category: str
    next_day: float
    score: float
    entry: dict = field(default_factory=dict)


def load_sample(conn: sqlite3.Connection, days: int = 0) -> list[Sample]:
    """装载评估样本（口径单源：复用 nextday_attribution.load_dedup）。"""
    recs = load_dedup(conn, days=days)
    attach_prominence(conn, recs)
    out: list[Sample] = []
    for r in recs:
        if r["next_day"] is None:
            continue
        entry = dict(r)
        entry["score_breakdown"] = parse_score_breakdown(r.get("breakdown"))
        entry["_candidate"] = None
        out.append(
            Sample(
                date=r["date"][:10],
                symbol=r["symbol"],
                category=r["category"],
                next_day=float(r["next_day"]),
                score=float(r["score"] or 0),
                entry=entry,
            )
        )
    return out


def _score_stored(sample: list[Sample], conn) -> dict[str, list[tuple[float, float]]]:
    by_day: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for s in sample:
        by_day[s.date].append((s.score, s.next_day))
    return by_day


def _score_nextday_prob(sample: list[Sample], conn) -> dict[str, list[tuple[float, float]]]:
    from scanner.nextday_prob import next_day_hit_probability

    by_day: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for s in sample:
        p = next_day_hit_probability(s.entry, prominence=s.entry.get("_prominent"), flow=None)
        by_day[s.date].append((p, s.next_day))
    return by_day


def _score_rescore(sample: list[Sample], conn) -> dict[str, list[tuple[float, float]]]:
    """用 historical_rescan 以当前（含 override）config 重算分数。

    标签来源：next_day_pct 是 (date, symbol) 的属性，与「该票是否被推荐」无关，
    故此处不施加 recommendations.excluded 过滤（与 nextday_attribution 的样本口径
    有意不同：那边评估的是「推荐结果」，这边评估的是「重扫宇宙的排序」）。

    cfg 必须用 `portfolio_backtest.PBConfig` 而非手搓 SimpleNamespace：
    `rescan_all_signals` 读 `cfg.hold_days_auto`，而该字段只定义在 PBConfig 上
    （本模块首版用 SimpleNamespace，直接 AttributeError）。用真实 cfg 类可保证
    字段集合永远与回测同源，新增字段不会再静默失配。
    """
    from scanner.historical_rescan import rescan_all_signals
    from scanner.portfolio_backtest import PBConfig

    label: dict[tuple[str, str], float] = {}
    for row in conn.execute(
        "SELECT date, symbol, next_day_pct FROM recommendations WHERE next_day_pct IS NOT NULL ORDER BY date, time"
    ).fetchall():
        label[(row[0], row[1])] = float(row[2])

    cal = [r[0] for r in conn.execute("SELECT DISTINCT date FROM daily_kline ORDER BY date").fetchall()]
    if not cal:
        return {}
    # hold_days=1：本项目评分体系校准于「次日大涨」，故主指标为 1 日口径；
    # buy_at/buy_delay 对「排序评估」无影响（只影响 P&L），此处仅保持与线上一致。
    cfg = PBConfig(
        start=cal[0],
        end=cal[-1],
        days=0,
        hold_days=1,
        buy_delay=0,
        buy_at="close",
        rescore=True,
    )
    signals = rescan_all_signals(conn, cfg, cal, {d: i for i, d in enumerate(cal)}, cal[-1])
    by_day: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for sig in signals:
        nd = label.get((sig.rec_date, sig.symbol))
        if nd is None:
            continue
        by_day[sig.rec_date].append((float(sig.score), nd))
    return by_day


SCORERS: dict[str, Callable[[list[Sample], sqlite3.Connection], dict[str, list[tuple[float, float]]]]] = {
    "stored-score": _score_stored,
    "nextday-prob": _score_nextday_prob,
    "rescore": _score_rescore,
}


# ── 主流程 ──


def _evaluate_windows(
    by_day: dict[str, list[tuple[float, float]]],
    windows: list[tuple[list[str], list[str]]],
    top_n: int,
    threshold: float,
) -> dict[str, dict[str, Any]]:
    """在 train / test 窗上分别算日等权 top-N hit 与日等权 IC。"""
    out: dict[str, dict[str, Any]] = {}
    for label, idx in (("train", 0), ("test", 1)):
        dates = [d for w in windows for d in w[idx]]
        sub = {d: by_day[d] for d in dates if d in by_day}
        per_day, hits, taken = day_topn_hit(sub, top_n, threshold)
        ics = day_rank_ic(sub)
        out[label] = {
            "days": len(per_day),
            "per_day_hit": per_day,
            "day_equal_hit": day_equal_mean(list(per_day.values())),
            "pooled_hit": pooled_rate(hits, taken),
            "hits": hits,
            "taken": taken,
            "day_equal_ic": day_equal_mean(list(ics.values())),
            "n_ic_days": len(ics),
        }
    return out


def run(
    conn: sqlite3.Connection,
    overrides: list[Override],
    *,
    evaluator: str = DEFAULT_EVALUATOR,
    top_n: int = DEFAULT_TOP_N,
    train_days: int = DEFAULT_TRAIN_DAYS,
    test_days: int = DEFAULT_TEST_DAYS,
    threshold: float = NEXTDAY_HIT_THRESHOLD,
    n_boot: int = DEFAULT_BOOT,
    alpha: float = DEFAULT_ALPHA,
    seed: int = DEFAULT_SEED,
    days: int = 0,
) -> dict[str, Any]:
    """执行改动前后 × train/test 的配对评估。"""
    sample = load_sample(conn, days=days)
    if not sample:
        return {"error": "无可用样本（检查 scanner.db 的 next_day_pct 是否已回填）"}

    scorer = SCORERS[evaluator]
    dates = sorted({s.date for s in sample})
    windows = walkforward_windows(dates, train_days, test_days, embargo_days=WF_EMBARGO_DAYS)
    if not windows:
        return {"error": f"窗口不足（{len(dates)} 个交易日，train={train_days} test={test_days}）"}

    base_by_day = scorer(sample, conn)
    base = _evaluate_windows(base_by_day, windows, top_n, threshold)

    journal = apply_overrides(overrides)
    try:
        new_by_day = scorer(sample, conn)
        new = _evaluate_windows(new_by_day, windows, top_n, threshold)
    finally:
        restore_overrides(journal)

    # 自检：改动是否真的影响了输出。空 --set 时自然相同，不算告警。
    identical = bool(overrides) and all(
        base_by_day.get(d) == new_by_day.get(d) for d in set(base_by_day) | set(new_by_day)
    )

    result: dict[str, Any] = {
        "evaluator": evaluator,
        "evaluator_desc": EVALUATORS[evaluator]["desc"],
        "evaluator_note": EVALUATORS[evaluator]["note"],
        "overrides": [ov.raw for ov in overrides],
        "override_journal": [{"spec": r["spec"], "patched_modules": r["patched"]} for r in journal],
        "identical_output_warning": identical,
        "top_n": top_n,
        "threshold": threshold,
        "train_days": train_days,
        "test_days": test_days,
        "embargo_days": WF_EMBARGO_DAYS,
        "n_windows": len(windows),
        "sample_n": len(sample),
        "alpha": alpha,
        "n_boot": n_boot,
        "metrics": {},
        "verdict": {},
    }

    for scope in ("train", "test"):
        b, nw = base[scope], new[scope]
        common = sorted(set(b["per_day_hit"]) & set(nw["per_day_hit"]))
        deltas = [nw["per_day_hit"][d] - b["per_day_hit"][d] for d in common]
        ci = bootstrap_mean_ci(deltas, n_boot=n_boot, alpha=alpha, seed=seed)
        # 翻转天数：真正被改动改变了当日 top-N 结果的交易日数。这是可检测性的
        # 实际来源——翻转天数为 0 时，无论样本量多大，检出概率都是 0（Δ 恒为 0）。
        flip_days = sum(1 for d in deltas if d != 0)
        result["metrics"][scope] = {
            "days": b["days"],
            "flip_days": flip_days,
            "base_day_equal_hit": b["day_equal_hit"],
            "new_day_equal_hit": nw["day_equal_hit"],
            "delta_day_equal": nw["day_equal_hit"] - b["day_equal_hit"],
            "base_pooled_hit": b["pooled_hit"],
            "new_pooled_hit": nw["pooled_hit"],
            "delta_pooled": nw["pooled_hit"] - b["pooled_hit"],
            "day_equal_vs_pooled_gap_base": b["day_equal_hit"] - b["pooled_hit"],
            "base_day_equal_ic": b["day_equal_ic"],
            "new_day_equal_ic": nw["day_equal_ic"],
            "delta_day_equal_ic": nw["day_equal_ic"] - b["day_equal_ic"],
            "paired_days": len(common),
            "ic_paired_days": len(common),
            "ci": ci,
        }

    test_ci = result["metrics"]["test"]["ci"]
    code, text = verdict_from_ci(test_ci)
    # Bonferroni：同时改 K 个参数时，单参数的门槛应按 K 收紧（多重比较）
    k = max(1, len(overrides))
    result["verdict"] = {
        "code": code,
        "text": text,
        "bonferroni_k": k,
        "alpha_adjusted": alpha / k,
        "note": "判定只看 test 窗（样本外）；train 窗 Δ 用于暴露过拟合。",
    }
    return result


def render(r: dict[str, Any]) -> str:
    if "error" in r:
        return f"  [中止] {r['error']}"
    L: list[str] = []
    L.append("=" * 92)
    L.append("规则/常数改动 · 样本外验证门（audit §B1）")
    L.append("=" * 92)
    L.append(f"评估器：{r['evaluator']} —— {r['evaluator_desc']}")
    L.append(f"         {r['evaluator_note']}")
    L.append(f"改动：{r['overrides'] or '（无——这是基线可检测性自检）'}")
    for j in r.get("override_journal", []):
        patched = j["patched_modules"]
        L.append(
            f"      {j['spec']}  →  传播改写 {len(patched)} 个模块"
            + (
                f"：{', '.join(patched[:4])}{' …' if len(patched) > 4 else ''}"
                if patched
                else "（无同名副本，仅目标模块本身）"
            )
        )
    if r.get("identical_output_warning"):
        L.append(
            "      ⚠ 改动未产生任何输出差异：可能该参数在本口径下无影响，"
            "也可能没生效（检查是否改错了模块）——此时的 Δ 恒为 0，判定无意义。"
        )
    L.append(
        f"样本：{r['sample_n']} 条；窗口 {r['n_windows']} 个"
        f"（train {r['train_days']} → embargo {r['embargo_days']} → test {r['test_days']}）；"
        f"主指标 = 日等权 top-{r['top_n']} 次日 hit≥{r['threshold']:.0f}%"
    )
    L.append("")
    L.append(f"  {'窗口':<7}{'日数':>6}{'基线':>9}{'改动后':>9}{'Δ':>9}{'Δ 95%CI':>20}{'单边p':>9}{'MDE':>8}")
    L.append("  " + "-" * 86)
    for scope, label in (("train", "train"), ("test", "test")):
        m = r["metrics"][scope]
        ci = m["ci"]
        mark = "  ← 判定依据" if scope == "test" else "  （仅看过拟合）"
        ci_str = f"[{ci['lo'] * 100:+.1f}, {ci['hi'] * 100:+.1f}]pp"
        # MDE 不可估（配对差值零离散度）时打印 n/a —— 原实现会打印 0.0pp，
        # 会被读成「灵敏度无穷大」，与本工具的核心判据直接冲突。
        mde_str = f"{ci['mde'] * 100:>7.1f}pp" if ci.get("mde_estimable", True) else f"{'n/a':>9}"
        L.append(
            f"  {label:<7}{m['days']:>6}{m['base_day_equal_hit'] * 100:>8.1f}%"
            f"{m['new_day_equal_hit'] * 100:>8.1f}%{m['delta_day_equal'] * 100:>+8.1f}pp"
            f"{ci_str:>20}{ci['p_le_zero']:>9.3f}{mde_str}{mark}"
        )
    L.append("")
    mt = r["metrics"]["test"]
    L.append("  诊断 · 按日等权 vs 按行汇总（B1 §2 的扭曲量）：")
    L.append(
        f"    基线  日等权 {mt['base_day_equal_hit'] * 100:.1f}%  vs  行汇总 "
        f"{mt['base_pooled_hit'] * 100:.1f}%   差 "
        f"{mt['day_equal_vs_pooled_gap_base'] * 100:+.1f}pp"
    )
    L.append(
        f"  诊断 · 日等权 rank-IC：基线 {mt['base_day_equal_ic']:+.4f} → "
        f"改动后 {mt['new_day_equal_ic']:+.4f}（Δ {mt['delta_day_equal_ic']:+.4f}）"
    )
    # 翻转天数：可检测性的真正来源。为 0 时样本量再大也检不出任何东西。
    fp = mt.get("flip_days", 0)
    pd_ = mt.get("paired_days", 0)
    L.append(
        f"  诊断 · 改动实际翻转了 {fp}/{pd_} 个交易日的 top-{r['top_n']} 结果"
        + ("  ← 翻转 0 天：本改动在本口径下未产生任何可测差异（判定必然「证据不足」）" if fp == 0 and pd_ else "")
    )
    L.append("")
    v = r["verdict"]
    L.append(f"  判定：{v['text']}   （退出码 {v['code']}）")
    if v["bonferroni_k"] > 1:
        L.append(
            f"  多重比较：同时改 {v['bonferroni_k']} 个参数 → 校正后 α = {v['alpha_adjusted']:.4f}（单参数门槛应变严）"
        )
    L.append(f"  {v['note']}")
    L.append("")
    L.append("  读法：MDE = 以当前样本量「能被检出」的最小改善。若 MDE 远大于你观察到的 Δ，")
    L.append("        那 Δ 无法与噪声区分 —— 此时「指标变好」不构成上生产的理由。")
    if not mt["ci"].get("mde_estimable", True):
        L.append("  ⚠ 本轮 MDE 显示 n/a：配对差值零离散度（改动未翻转任何交易日的 top-N，")
        L.append("    或本就是空操作的基线自检），MDE 在此情形下**不可估，绝不能读作 0**。")
        L.append("    请看上面的「翻转了 X/N 个交易日」——为 0 时样本量再大也检不出任何东西。")
        L.append("    要估计真实可检测下限，请用能实际翻转 top-N 的扰动量级再跑一次。")
    return "\n".join(L)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="规则/常数改动的样本外验证门（B1）",
        epilog="退出码：0 样本外支持 / 1 证据不足 / 2 样本外显著变差 / 3 用法或可见性错误",
    )
    p.add_argument(
        "--set",
        dest="sets",
        action="append",
        default=[],
        metavar="MODULE.ATTR=VALUE",
        help="声明一个改动（可重复），如 scanner.nextday_prob.OR_OVERBOUGHT=1.56",
    )
    p.add_argument("--evaluator", default=DEFAULT_EVALUATOR, choices=sorted(EVALUATORS))
    p.add_argument("--top-n", type=int, default=DEFAULT_TOP_N, help=f"每日取前 N（默认 {DEFAULT_TOP_N}）")
    p.add_argument("--train", type=int, default=DEFAULT_TRAIN_DAYS)
    p.add_argument("--test", type=int, default=DEFAULT_TEST_DAYS)
    p.add_argument("--threshold", type=float, default=NEXTDAY_HIT_THRESHOLD)
    p.add_argument("--boot", type=int, default=DEFAULT_BOOT, help="bootstrap 重采样次数")
    p.add_argument("--alpha", type=float, default=DEFAULT_ALPHA)
    p.add_argument("--seed", type=int, default=DEFAULT_SEED)
    p.add_argument("--days", type=int, default=0, help="仅用最近 N 天（0=全部）")
    p.add_argument("--db", default=DB_PATH)
    p.add_argument("--json", action="store_true")
    p.add_argument("--list-evaluators", action="store_true", help="列出评估器及其可见模块集合")
    return p


def main() -> int:
    args = build_parser().parse_args()
    if sys.platform == "win32":
        _reconfigure = getattr(sys.stdout, "reconfigure", None)
        if callable(_reconfigure):
            _reconfigure(encoding="utf-8")

    if args.list_evaluators:
        for name in sorted(EVALUATORS):
            e = EVALUATORS[name]
            print(f"{name:<14}{e['desc']}")
            print(f"{'':<14}可见模块：{sorted(e['sees']) or '无（冻结分）'}")
            print(f"{'':<14}{e['note']}")
        return 0

    try:
        overrides = [parse_override(s) for s in args.sets]
    except ValueError as exc:
        print(f"  [用法错误] {exc}")
        return EXIT_USAGE

    problems = check_visibility(overrides, args.evaluator)
    if problems:
        print("=" * 92)
        print("  [可见性错误] 以下改动不会被所选评估器看见，验证结论无效：")
        for x in problems:
            print(f"    - {x}")
        print("\n  改用 `--evaluator rescore`（覆盖评分链改动），或移除该 --set。")
        print("  用 `--list-evaluators` 查看各评估器的可见集合。")
        print("=" * 92)
        return EXIT_USAGE

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    try:
        result = run(
            conn,
            overrides,
            evaluator=args.evaluator,
            top_n=args.top_n,
            train_days=args.train,
            test_days=args.test,
            threshold=args.threshold,
            n_boot=args.boot,
            alpha=args.alpha,
            seed=args.seed,
            days=args.days,
        )
    finally:
        conn.close()

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return result.get("verdict", {}).get("code", EXIT_INSUFFICIENT)
    print(render(result))
    return result.get("verdict", {}).get("code", EXIT_INSUFFICIENT)


if __name__ == "__main__":
    raise SystemExit(main())
