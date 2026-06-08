from datetime import datetime

import wcwidth

from scanner.config import YI, MAX_MARKET_CAP, MAX_STOCK_PRICE
from scanner.models import Candidate


ANSI = {
    "RED": "\033[91m", "YELLOW": "\033[93m", "GREEN": "\033[92m",
    "CYAN": "\033[96m", "BOLD": "\033[1m", "RESET": "\033[0m",
}


def _rank_delta_str(symbol: str, current_rank: int, last_ranks: dict[str, int]) -> tuple[str, str]:
    prev = last_ranks.get(symbol)
    if prev is None:
        return "  —", ""
    diff = prev - current_rank
    if diff > 0:
        return f"↑{diff}", ANSI["RED"] if diff >= 5 else ""
    if diff < 0:
        return f"↓{-diff}", ANSI["GREEN"] if -diff >= 5 else ""
    return "  —", ""


def _vis_len(s: str) -> int:
    return sum(wcwidth.wcwidth(c) or 1 for c in s)


def _pad(s: str, width: int, align: str = "l") -> str:
    pad = max(0, width - _vis_len(s))
    return f"{' ' * pad}{s}" if align == "r" else f"{s}{' ' * pad}"


def clear_screen():
    print("\033[2J\033[H", end="")


def fmt_time():
    return datetime.now().strftime("%H:%M:%S")


def pct_colored(pct: float, width: int = 8) -> str:
    s = f"{pct:+.2f}%"
    if pct >= 9:
        c = ANSI["RED"]
    elif pct >= 5:
        c = ANSI["GREEN"]
    elif pct < 0:
        c = ANSI["YELLOW"]
    else:
        c = ""
    return f"{c}{s:>{width}}{ANSI['RESET']}" if c else f"{s:>{width}}"


def _bonus_tag(c: Candidate) -> str:
    parts = []
    if c.rank_trend_bonus:
        parts.append(f"T{c.rank_trend_bonus:+d}")
    if c.sector_bonus:
        parts.append(f"S{c.sector_bonus:+d}")
    if c.intraday_score:
        d_tag = f"D{int(c.intraday_score):+d}"
        if c.intraday_score < -2:
            d_tag = f"{ANSI['RED']}{d_tag}{ANSI['RESET']}"
        elif c.intraday_score > 2:
            d_tag = f"{ANSI['GREEN']}{d_tag}{ANSI['RESET']}"
        parts.append(d_tag)
    return " ".join(parts) if parts else ""


def _fmt_market_cap(cap: float) -> str:
    if cap <= 0:
        return ""
    cap_yi = cap / YI
    if cap_yi < 10:
        return f"{cap_yi:.1f}亿"
    return f"{cap_yi:.0f}亿"


def display(new_faces: list[Candidate], old_faces: list[Candidate], momentum: list[Candidate],
            stock_total: int, interval: int, filtered_large_cap: int = 0,
            last_ranks: dict[str, int] | None = None):
    if last_ranks is None:
        last_ranks = {}
    clear_screen()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    print(f"{'='*96}")
    print(f"  A股飙升榜监控  ({now})")

    all_c = new_faces + old_faces + momentum
    sec_counts: dict[str, int] = {}
    for c_ in all_c:
        if c_.sector:
            sec_counts[c_.sector] = sec_counts.get(c_.sector, 0) + 1
    hot_secs = [f"{s}{c}" for s, c in sorted(sec_counts.items(), key=lambda x: -x[1])[:3]]
    sec_line = f"  {' '.join(hot_secs)}" if hot_secs else ""
    filter_info = f" | 过滤{filtered_large_cap}只" if filtered_large_cap else ""
    cap_count = sum(1 for c in all_c if c.market_cap > 0)
    cap_status = f"市值数据{cap_count}/{len(all_c)}" if all_c else "暂无候选"
    print(f"  A股共 {stock_total} 只 | 新{len(new_faces)}动{len(momentum)}旧{len(old_faces)}{filter_info} | {sec_line} | {cap_status} | 每{interval}s刷新")
    print(f"  小而美: 市值≤{int(MAX_MARKET_CAP/YI)}亿 股价≤{MAX_STOCK_PRICE}元")
    print(f"{'='*96}")

    def _print_row(c: Candidate, show_val: bool = False):
        s = c.stock
        k = c.kline
        cur = f"{s.current:.2f}" if s.current else "N/A"
        acc = f"{k.accumulated_pct:+.2f}%" if k else "N/A"
        vr = f"{k.volume_ratio:.1f}x" if k else "N/A"
        score_visible = str(c.score)
        score_tag = f"{ANSI['BOLD']}{_pad(score_visible,4,'r')}{ANSI['RESET']}" if c.score >= 15 else _pad(score_visible,4,'r')
        trend_tag = k.trend if k else "N/A"
        delta_text, delta_color = _rank_delta_str(s.symbol, s.rank, last_ranks)
        delta_display = f"{delta_color}{_pad(delta_text,6,'r')}{ANSI['RESET']}" if delta_color else _pad(delta_text,6,'r')
        bonus_str = _bonus_tag(c)
        cap_str = _fmt_market_cap(c.market_cap)
        val_str = f"{s.value:.0f}" if s.value else "N/A"
        if show_val:
            print(f"  {s.rank:>4} {delta_display} {_pad(s.name,10)} {s.symbol:<12} {cur:>7} {pct_colored(s.percent)} {_pad(trend_tag,14)} {acc:>8} {vr:>6} {score_tag} {_pad(bonus_str,16)} {cap_str:>8} {val_str:>6}")
        else:
            print(f"  {s.rank:>4} {delta_display} {_pad(s.name,10)} {s.symbol:<12} {cur:>7} {pct_colored(s.percent)} {_pad(trend_tag,14)} {acc:>8} {vr:>6} {score_tag} {_pad(bonus_str,16)} {cap_str:>8}")

    hdr = f"  {_pad('排名',4,'r')} {_pad('变化',6,'r')} {_pad('名称',10)} {_pad('代码',12)} {_pad('现价',7,'r')} {_pad('涨幅',8,'r')} {_pad('趋势',14)} {_pad('5日累计',8,'r')} {_pad('量比',6,'r')} {_pad('评分',4,'r')} {_pad('增强',16)} {_pad('市值',8,'r')}"

    print(f"\n{ANSI['GREEN']}◆ 新面孔 — 底部异动 / 刚启动{ANSI['RESET']}  (找: 今日小涨+日线底部放量)")
    print(hdr)
    print(f"  {'-'*108}")
    if new_faces:
        for c in new_faces:
            _print_row(c)
    else:
        print(f"  {ANSI['YELLOW']}暂无新面孔{ANSI['RESET']}")

    if momentum:
        print(f"\n{ANSI['YELLOW']}◆ 动量延续 — 已启动 / 温和上攻{ANSI['RESET']}  (找: 累计涨幅已起+今日温和放量)")
        print(hdr)
        print(f"  {'-'*108}")
        for c in momentum:
            _print_row(c)

    hdr_val = f"{hdr} {_pad('热度',6,'r')}"
    print(f"\n{ANSI['CYAN']}◆ 旧面孔 — 盘整 / 回调低吸{ANSI['RESET']}  (找: 前期热点+今日回调)")
    print(hdr_val)
    print(f"  {'-'*116}")
    if old_faces:
        for c in old_faces:
            _print_row(c, show_val=True)
    else:
        print(f"  {ANSI['YELLOW']}暂无旧面孔{ANSI['RESET']}")

    print(f"\n{'-'*96}")
    print(f"  {ANSI['GREEN']}新面孔{ANSI['RESET']}: 底部放量启动+涨幅2-6%")
    print(f"  {ANSI['YELLOW']}动量延续{ANSI['RESET']}: 累计涨幅10%+今日温和上攻")
    print(f"  {ANSI['CYAN']}旧面孔{ANSI['RESET']}: 缩量回调+未破位+高热度")
    print()
