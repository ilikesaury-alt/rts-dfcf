import os
import sys

from scanner.industry_chain.chains import CHAINS
from scanner.industry_chain.models import (
    CHAIN_PHASE_NAMES,
    ChokepointCandidate,
    ChainTrend,
)


def clear_console():
    os.system("cls" if os.name == "nt" else "clear")


def _safe_print(text: str):
    try:
        print(text)
    except UnicodeEncodeError:
        safe = text.encode("ascii", errors="replace").decode("ascii")
        print(safe)


def _phase_color(phase: str) -> str:
    return {
        "erupting": "\033[91m",
        "growing": "\033[93m",
        "forming": "\033[94m",
        "fading": "\033[90m",
        "dormant": "\033[0m",
    }.get(phase, "\033[0m")


_RESET = "\033[0m"
_GREEN = "\033[92m"
_YELLOW = "\033[93m"
_RED = "\033[91m"
_BLUE = "\033[94m"
_CYAN = "\033[96m"
_GRAY = "\033[90m"


def print_header(raw_count: int, gem_count: int, active_count: int):
    _safe_print(
        f"\n{'='*60}\n"
        f"  产业链趋势选股扫描\n"
        f"  飙升榜{raw_count}只 → GEM{gem_count}只 → 活跃链{active_count}条\n"
        f"{'='*60}"
    )


def print_chain_trends(trends: dict[str, ChainTrend]):
    if not trends:
        return
    _safe_print(f"\n  {'─'*58}")
    _safe_print(f"  {'链趋势判定':^56}")
    _safe_print(f"  {'─'*58}")

    sorted_trends = sorted(trends.items(), key=lambda x: -x[1].score)
    for chain_name, t in sorted_trends:
        color = _phase_color(t.phase)
        phase_cn = CHAIN_PHASE_NAMES.get(t.phase, t.phase)
        signals = " | ".join(t.signals[:3])
        _safe_print(
            f"  {color}{chain_name:<8} {phase_cn} "
            f"评分{t.score:>3} "
            f"({t.stock_count}只) "
            f"{signals}{_RESET}"
        )


def print_candidates(candidates: list[ChokepointCandidate]):
    if not candidates:
        _safe_print(f"\n  {_GRAY}本期无候选{_RESET}")
        return

    _safe_print(f"\n  {'─'*58}")
    _safe_print(f"  {'卡位选股推荐':^56}")
    _safe_print(f"  {'─'*58}")

    header = (
        f"  {'代码':>8} {'名称':<10} {'产业链':<8} {'环节':<10} "
        f"{'趋势':<6} {'评分':>4} {'涨幅':>6}"
    )
    _safe_print(header)
    _safe_print(f"  {'─'*58}")

    for c in candidates:
        color = _GREEN if c.is_bottleneck else _CYAN
        phase_cn = CHAIN_PHASE_NAMES.get(c.chain_phase, c.chain_phase)
        pct_str = f"{c.percent:+.1f}%"
        node_show = c.node_name if len(c.node_name) <= 8 else c.node_name[:6] + ".."
        _safe_print(
            f"  {color}{c.symbol:>8} {c.name:<10} {c.chain_name:<8} "
            f"{node_show:<10} {phase_cn:<6} {c.score:>4} {pct_str:>6}{_RESET}"
        )


def print_candidate_detail(c: ChokepointCandidate):
    bn_str = f"{_RED}[瓶颈环节]{_RESET}" if c.is_bottleneck else ""
    phase_cn = CHAIN_PHASE_NAMES.get(c.chain_phase, c.chain_phase)
    _safe_print(
        f"\n  {c.name}({c.symbol}) {bn_str}\n"
        f"    链: {c.chain_name} | 环节: {c.node_name} | 趋势: {phase_cn}\n"
        f"    评分: {c.score} (链趋势{c.chain_trend_score}+瓶颈{c.bottleneck_bonus}+技术{c.tech_score})\n"
        f"    涨幅: {c.percent:+.1f}% | 信号: {' | '.join(c.signals)}"
    )


def print_summary(candidates: list[ChokepointCandidate], elapsed: float):
    if not candidates:
        _safe_print(f"\n  {_GRAY}本期无推荐 | 耗时{elapsed:.1f}s{_RESET}")
        return

    bottleneck_count = sum(1 for c in candidates if c.is_bottleneck)
    chains = set(c.chain_name for c in candidates)
    _safe_print(
        f"\n  {'─'*58}\n"
        f"  推荐{candidates[0].score}~{candidates[-1].score}分 "
        f"| {len(candidates)}只 "
        f"| 瓶颈{bottleneck_count}只 "
        f"| {len(chains)}条链 "
        f"| 耗时{elapsed:.1f}s\n"
        f"{'='*58}\n"
    )
