"""终选走势美感门（2026-09-09）：日线 / 分时走势「漂亮」判定。

用户需求：进入终选参考（scanner/final_pick）的票，分时走势与日线走势都要
「漂亮」。纯展示/筛选层：不改评分/排序/落库；数据缺失 fail-open（不判否），
只拦「可判定的丑」，不因数据缺口误杀。

API 口径：两个判定函数都返回「丑的理由 | None」——None 表示漂亮 **或** 无法
判定（两者对筛选门行为一致：放行）；「无法判定」通过 detail 文本区分
（"日线不足" / "分时缺失"），供测试与排查（判定单源，2026-09-09 定稿）。

日线漂亮（evaluate_daily_trend）：基于 daily_kline 缓存（离线、无网络），
要求干净的上升趋势，6 硬门全过才判漂亮——
  ① MA 多头排列 MA5>MA10>MA20（趋势骨架）；
  ② 近 5 日收盘趋势向上（slope ≥ DAILY_BEAUTY_MIN_SLOPE_PCT）；
  ③ 近 5 日无暴跌日（单日跌幅 ≤ DAILY_BEAUTY_MAX_CRASH_PCT）；
  ④ 回调可控（单日跌幅 > DAILY_BEAUTY_MAX_PULLBACK_PCT = 深回调，丑）；
  ⑤ 无长上影（上影线 % vs 昨收 > DAILY_BEAUTY_MAX_UPPER_SHADOW = 冲高回落，丑）；
  ⑥ 收盘距 20 日最高收盘回撤 ≤ DAILY_BEAUTY_MAX_OFF_HIGH_PCT（未破位）。
  任一不过 → 返回理由串（"/"连接各硬门名）；score（0-100）仅供展示参考。

分时漂亮（evaluate_intraday_beauty）：复用盘中 intraday_fetch 已算的
intraday_score（-10~10：>0 平稳走高/高位不回落，<0 冲高回落/走弱），优先取
实时候选 _candidate.intraday_score（最新一轮），回退 score_breakdown 落库值
（掉榜/重启行）。
  注意：Candidate.intraday_score 默认 0.0 且无法与「真实评了 0 分」区分，
  而 0.0 实际 overwhelmingly 是「未评分」（开盘前/无分时数据/AKShare 源），
  故 0.0 按缺失处理 fail-open；只有明确非 0 分才判定。

展示标记分级（beauty_mark，2026-09-15 定稿）：**日线定准入、分时定级别**——
  日线不漂亮 / 日线不足        → ""（不标）
  日线漂亮，分时未确认漂亮      → "美"   （分时走弱 **或** 分时缺失，一律归此档）
  日线漂亮，分时亦漂亮          → "美★"
分时在这里是**分级维度**，不是准入硬门；硬门（FINAL_PICK_BEAUTY_ENABLED）仍按
「日线 ∧ 分时」双维度判，两者有意不同源（标记比门宽，默认门关，见 config_sources）。

数据依据（2026-09-15，n=(date,symbol) 去重 2429 / hit=次日≥7% / 分时缺失 24.6%）：
旧口径「日线∧分时」标记率仅 **1.8%**（44 只），等于常年空白 —— 归因 **对半**：
去掉分时的 AND 结构回收约一半，`INTRADAY_BEAUTY_MIN=2.5` 恰好压在 intraday_score 的
p90（可判定样本通过率 12%）再砍掉约 2/3。故只调阈值上限仅 4.0%，达不到分级后的 7.0%。
另旧口径有语义缺陷：`determined` 是 OR，44 只里有 **14 只（32%）是「日线不足 + 分时美」**
—— 日线完全无法判定却被标「美」；分级后要求日线可判定，该缺陷消失。

分档实测（覆盖率 / hit / avg / 尾部≤-7%）：
  美★  0.7%（17）  hit 5.9%  avg -0.14%  尾部 0.0%
  美   6.3%（154） hit 5.2%  avg -0.67%  尾部 7.8%
  未标记 93.0%（2258）hit 7.1%  avg -0.38%  尾部 7.4%
⇒ **★ 表示「回撤更小」，不是「更可能大涨」**：两档 hit 都低于基线（7.1%），彼此无
正向区分度；差别只在尾部（美★ ≤-5%/≤-7% = 5.9%/0.0%，美 = 9.7%/7.8%）。★ 的
n=17 是**极小样本**（脚本就此告警：单只 ≤-7% 即把该比率推到 5.9%），且它的
≤-5% 优势在「美」档就已大部分拿到。复现：`python scripts/beauty_mark_eval.py`。
"""

from __future__ import annotations

from typing import Any

from scanner.config import (
    DAILY_BEAUTY_MAX_CRASH_PCT,
    DAILY_BEAUTY_MAX_OFF_HIGH_PCT,
    DAILY_BEAUTY_MAX_PULLBACK_PCT,
    DAILY_BEAUTY_MAX_UPPER_SHADOW,
    DAILY_BEAUTY_MIN_BARS,
    DAILY_BEAUTY_MIN_SLOPE_PCT,
    INTRADAY_BEAUTY_MIN,
)
from scanner.models import parse_score_breakdown
from scanner.utils import to_float

# 「无法判定」的 detail 标记（调用方/测试据此区分 漂亮 vs 数据缺失，均放行）
DAILY_INSUFFICIENT = "日线不足"
INTRADAY_MISSING = "分时缺失"

# 满足美感的行尾标记（2026-09-09 用户口径：只标「美」，不标丑）
BEAUTY_MARK = "美"
# 强档标记（2026-09-15 分级）：日线漂亮 **且** 分时亦漂亮。
# ★ 的语义是「尾部回撤更小」（买入体验），**不是**「更可能大涨」——
# 见模块 docstring 的分档数据与 scripts/beauty_mark_eval.py 复现。
BEAUTY_MARK_STRONG = "美★"


def beauty_mark(entry: Any, kline: list[Any] | None, candidate: Any = None) -> str:
    """走势标记（纯展示单源，2026-09-15 分级）：日线定准入、分时定级别。

    日线不漂亮 / 日线不足 → ""；日线漂亮但分时未确认漂亮（走弱或缺失）→ "美"；
    日线漂亮且分时亦漂亮 → "美★"。fail-open 语义不变：数据缺失不判否、只降档。
    判定单源：日线用 evaluate_daily_trend（与 hot_watch 美感门同源），
    分时用 evaluate_intraday_beauty（0.0 视为未评分 → 缺失 → 归「美」档）。
    """
    daily_fail, _score, daily_detail = evaluate_daily_trend(kline)
    if daily_fail or daily_detail == DAILY_INSUFFICIENT:
        return ""
    intraday_fail, intraday_detail = evaluate_intraday_beauty(entry, candidate)
    # ★ 要求分时**确认**漂亮：缺失是 fail-open（不判否）但也不加分，只降档到「美」。
    strong = intraday_fail is None and intraday_detail != INTRADAY_MISSING
    return BEAUTY_MARK_STRONG if strong else BEAUTY_MARK


def evaluate_daily_trend(kline: list[Any] | None) -> tuple[str | None, int, str]:
    """日线走势美感判定 → (丑的理由 | None, score 0-100, detail)。

    返回 None = 漂亮 或 数据不足无法判定（detail 为 DAILY_INSUFFICIENT 时是后者，
    调用方统一 fail-open 放行）。返回理由串 = 可判定的丑（"/"连接硬门名）。
    """
    if not kline or len(kline) < DAILY_BEAUTY_MIN_BARS:
        return None, 0, DAILY_INSUFFICIENT
    closes = [to_float(k.get("close"), default=0.0) for k in kline]
    if any(c <= 0 for c in closes[-DAILY_BEAUTY_MIN_BARS:]):
        return None, 0, DAILY_INSUFFICIENT

    ma5 = sum(closes[-5:]) / 5
    ma10 = sum(closes[-10:]) / 10
    ma20 = sum(closes[-20:]) / 20
    ma_bull = ma5 > ma10 > ma20

    pcts = [to_float(k.get("percent"), default=0.0) for k in kline[-5:]]
    slope = (closes[-1] / closes[-6] - 1) * 100  # len ≥ 20 保证 -6 安全
    max_drop = min(pcts)
    hi20 = max(closes[-20:])
    off_high = (1 - closes[-1] / hi20) * 100 if hi20 > 0 else 0.0

    # 上影线：近 5 根 (high - max(open, close)) / 昨收 × 100。
    # 昨收由 percent 反推：prev_close = close / (1 + percent/100)（日线接口无昨收列）。
    max_shadow = 0.0
    for k in kline[-5:]:
        high = to_float(k.get("high"), default=0.0)
        opn = to_float(k.get("open"), default=0.0)
        close = to_float(k.get("close"), default=0.0)
        pct = to_float(k.get("percent"), default=0.0)
        if high <= 0 or close <= 0:
            continue
        prev_close = close / (1 + pct / 100) if (1 + pct / 100) > 0 else 0.0
        if prev_close <= 0:
            continue
        shadow = (high - max(opn, close)) / prev_close * 100
        if shadow > max_shadow:
            max_shadow = shadow

    # ── 6 硬门 ──
    fails: list[str] = []
    if not ma_bull:
        fails.append("MA未多头")
    if slope < DAILY_BEAUTY_MIN_SLOPE_PCT:
        fails.append("趋势向下")
    if max_drop <= DAILY_BEAUTY_MAX_CRASH_PCT:
        fails.append("暴跌日")
    if max_drop < -DAILY_BEAUTY_MAX_PULLBACK_PCT:
        fails.append("深回调")
    if max_shadow > DAILY_BEAUTY_MAX_UPPER_SHADOW:
        fails.append("长上影")
    if off_high > DAILY_BEAUTY_MAX_OFF_HIGH_PCT:
        fails.append("破位")

    # 展示分（0-100，仅参考不参与判定）
    score = 0
    score += 40 if ma_bull else 5
    if slope > 0:
        score += 20
    elif slope >= DAILY_BEAUTY_MIN_SLOPE_PCT:
        score += 10
    if max_drop > DAILY_BEAUTY_MAX_CRASH_PCT:
        score += 15
    if max_drop >= -DAILY_BEAUTY_MAX_PULLBACK_PCT:
        score += 10
    if max_shadow <= DAILY_BEAUTY_MAX_UPPER_SHADOW:
        score += 10
    if off_high <= DAILY_BEAUTY_MAX_OFF_HIGH_PCT:
        score += 5

    if fails:
        return "/".join(fails), score, "/".join(fails)
    return None, score, "多头排列"


def evaluate_intraday_beauty(entry: Any, candidate: Any = None) -> tuple[str | None, str]:
    """分时走势美感判定 → (丑的理由 | None, detail)。

    返回 None = 漂亮 或 intraday_score 缺失（detail 为 INTRADAY_MISSING 时是后者，
    统一 fail-open 放行）。返回理由串含分数供落选理由展示。
    """
    score: float | None = None
    if candidate is not None:
        # 实时候选优先（最新一轮 intraday_fetch 产出，非落库快照）
        val = to_float(getattr(candidate, "intraday_score", None), default=None)
        if val != 0.0:
            score = val
    if score is None and isinstance(entry, dict):
        dims = parse_score_breakdown(entry.get("score_breakdown"))
        val = to_float(dims.get("intraday_score"), default=None)
        if val != 0.0:
            score = val
    if score is None:
        return None, INTRADAY_MISSING
    if score >= INTRADAY_BEAUTY_MIN:
        return None, f"分时{score:+.1f}"
    return f"分时不漂亮({score:+.1f})", f"分时{score:+.1f}"
