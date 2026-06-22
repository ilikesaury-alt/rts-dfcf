import os
import sys
from datetime import datetime


def _c(code: int) -> str:
    if os.name == "nt":
        return ""
    return f"\033[{code}m"


RED = _c(91)
GREEN = _c(92)
YELLOW = _c(93)
CYAN = _c(96)
BOLD = _c(1)
RESET = _c(0)


def _safe_print(text: str, **kwargs):
    try:
        print(text, **kwargs)
    except UnicodeEncodeError:
        cleaned = text.encode("ascii", errors="replace").decode()
        print(cleaned, **kwargs)


def _heat_icon(heat: str) -> str:
    return {"hot": "🔥", "warm": "⚡", "cold": ""}.get(heat, "")


def _heat_label(heat: str) -> str:
    return {"hot": "活跃", "warm": "升温", "cold": "冷淡"}.get(heat, "")


def _score_icon(score: int) -> str:
    if score >= 70:
        return "★"
    if score >= 55:
        return "∙"
    return ""


def print_watch_table(
    chain_name: str,
    heat: str,
    stock_count: int,
    bottleneck_active: bool,
    avg_rank_change: float,
    scored_stocks: list[dict],
):
    heat_color = {"hot": RED, "warm": YELLOW}.get(heat, "")
    bn_str = "瓶颈活跃" if bottleneck_active else ""
    header = (f"  {heat_color}{_heat_icon(heat)} {chain_name} "
              f"({stock_count}只异动, {_heat_label(heat)})"
              f"{f', {bn_str}' if bn_str else ''}{RESET}")
    _safe_print(header)

    if not scored_stocks:
        _safe_print("    暂无")
        return

    for s in sorted(scored_stocks, key=lambda x: -x["score"]):
        name = s.get("name", "")
        sym = s.get("symbol", "")
        sc = s.get("score", 0)
        btn = s.get("node", "")
        pct = s.get("percent", 0)
        signals = s.get("signals", [])

        day_str = f"{pct:+.1f}%" if pct else ""
        sig_str = " ".join(signals[:3])

        score_color = GREEN if sc >= 70 else (YELLOW if sc >= 55 else "")
        icon = _score_icon(sc)
        star = f" {score_color}{icon}{RESET}" if icon else ""

        line = (f"    {sc:2d}{star} {name:8s} {sym:10s} "
                f"{btn:8s} {day_str:>7s}  {sig_str}")
        _safe_print(line)

    _safe_print("")


def print_header(raw_count: int, gem_count: int, chain_count: int):
    now = datetime.now().strftime("%H:%M:%S")
    _safe_print(f"\n{'='*55}")
    _safe_print(f"  产业链趋势观察池  ({now})")
    _safe_print(f"{'='*55}")
    _safe_print(f"  飙升榜{raw_count}只, GEM{gem_count}只, "
                f"覆盖{chain_count}条产业链\n")


def print_summary(total_watch: int, hot_count: int):
    _safe_print(f"  观察池共{total_watch}只, "
                f"活跃产业链{hot_count}条")
    _safe_print(f"{'='*55}\n")
    sys.stdout.flush()
