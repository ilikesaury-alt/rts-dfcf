import os

import wcwidth

from scanner.config import MAX_MARKET_CAP, MAX_STOCK_PRICE, STALE_TIMEOUT_MINUTES, YI, now_beijing
from scanner.models import Candidate
from scanner.picker import pick_top_candidates

if os.name == "nt":
    import ctypes
    _kernel32 = ctypes.windll.kernel32
    _handle = _kernel32.GetStdHandle(-11)
    _mode = ctypes.c_uint32()
    _supports_ansi = (
        _kernel32.GetConsoleMode(_handle, ctypes.byref(_mode)) != 0
        and _kernel32.SetConsoleMode(_handle, _mode.value | 0x0004) != 0
    )
else:
    _supports_ansi = True

if _supports_ansi:
    ANSI = {
        "RED": "\033[91m", "YELLOW": "\033[93m", "GREEN": "\033[92m",
        "CYAN": "\033[96m", "BOLD": "\033[1m", "RESET": "\033[0m",
    }
else:
    ANSI = {"RED": "", "YELLOW": "", "GREEN": "", "CYAN": "", "BOLD": "", "RESET": ""}


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
    # wcwidth.wcwidth 对 ANSI 转义字符（如 \x1b）返回 -1（控制字符），
    # `-1 or 1` 在 Python 中返回 -1（truthy），导致宽度计算错误、列对齐错位。
    # 用 max(0, ...) 确保控制字符贡献 0 宽度。
    return sum(max(0, wcwidth.wcwidth(c)) for c in s)


def _pad(s: str, width: int, align: str = "l") -> str:
    pad = max(0, width - _vis_len(s))
    return f"{' ' * pad}{s}" if align == "r" else f"{s}{' ' * pad}"


def clear_screen():
    if os.name == "nt":
        os.system("cls")
    else:
        print("\033[2J\033[H", end="")


def fmt_time():
    return now_beijing().strftime("%H:%M:%S")


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


def _source_tag(c: Candidate) -> str:
    tag = getattr(c.stock, "source_tag", "xueqiu")
    if tag == "both":
        return f"{ANSI['GREEN']}双{ANSI['RESET']}"
    return ""


def _bonus_tag(c: Candidate) -> str:
    parts = []
    if c.sector_bonus:
        parts.append(f"S{c.sector_bonus:+d}")
    if c.first_breakout_bonus:
        parts.append(f"B{c.first_breakout_bonus:+d}")
    if c.gap_up_bonus:
        parts.append(f"G{c.gap_up_bonus:+d}")
    if c.intraday_score is not None and c.intraday_score != 0.0:
        d_tag = f"D{int(c.intraday_score):+d}"
        if c.intraday_score < -2:
            d_tag = f"{ANSI['RED']}{d_tag}{ANSI['RESET']}"
        elif c.intraday_score > 2:
            d_tag = f"{ANSI['GREEN']}{d_tag}{ANSI['RESET']}"
        parts.append(d_tag)
    if c.turnover_bonus:
        parts.append(f"H{c.turnover_bonus:+d}")
    if c.list_momentum_bonus:
        parts.append(f"L{c.list_momentum_bonus:+d}")
    return " ".join(parts) if parts else ""


def _fmt_market_cap(cap: float) -> str:
    if cap <= 0:
        return ""
    cap_yi = cap / YI
    if cap_yi < 10:
        return f"{cap_yi:.1f}亿"
    return f"{cap_yi:.0f}亿"


def display(new_faces: list[Candidate], pure_momentum: list[Candidate],
            gem_total: int, interval: int, filtered_large_cap: int = 0,
            last_ranks: dict[str, int] | None = None,
            stale_candidates: list[Candidate] | None = None,
            pullback_list: list[Candidate] | None = None,
            current_rank_map: dict[str, int] | None = None,
            short_term_list: list[Candidate] | None = None,
            rebound_list: list[Candidate] | None = None):
    if last_ranks is None:
        last_ranks = {}
    if current_rank_map is None:
        current_rank_map = {}
    clear_screen()
    now = now_beijing().strftime("%Y-%m-%d %H:%M:%S")
    if pullback_list is None:
        pullback_list = []
    if short_term_list is None:
        short_term_list = []
    if rebound_list is None:
        rebound_list = []

    print(f"{'='*96}")
    print(f"  创业板飙升榜监控  ({now})")

    all_c = new_faces + pure_momentum + pullback_list + rebound_list + short_term_list
    # 双挂去重：同一 symbol 在多个桶出现时，仅在先展示的桶显示一次
    displayed_syms: set[str] = set()
    sec_counts: dict[str, int] = {}
    for c_ in all_c:
        if c_.sector:
            sec_counts[c_.sector] = sec_counts.get(c_.sector, 0) + 1
    hot_secs = [f"{s}{c}" for s, c in sorted(sec_counts.items(), key=lambda x: -x[1])[:3]]
    sec_line = f"  {' '.join(hot_secs)}" if hot_secs else ""
    filter_info = f" | 过滤{filtered_large_cap}只" if filtered_large_cap else ""
    cap_count = sum(1 for c in all_c if c.market_cap > 0)
    cap_status = f"市值数据{cap_count}/{len(all_c)}" if all_c else "暂无候选"
    pb_red = f"{ANSI['RED']}回{len(pullback_list)}{ANSI['RESET']}" if pullback_list else f"回{len(pullback_list)}"
    # 大盘环境标签：从首个 candidate 的 dimensions 读取 market_env_bonus
    env_bonus = 0
    if all_c and all_c[0].kline:
        env_bonus = all_c[0].kline.dimensions.get("market_env_bonus", 0) or 0
    if env_bonus > 0:
        env_tag = f" | {ANSI['GREEN']}[大盘强势]{ANSI['RESET']}"
    elif env_bonus < 0:
        env_tag = f" | {ANSI['RED']}[大盘弱势·谨慎]{ANSI['RESET']}"
    else:
        env_tag = " | [大盘中性]"
    print(f"  创业板共 {gem_total} 只 | 新{len(new_faces)}动{len(pure_momentum)}{pb_red}反{len(rebound_list)}超{len(short_term_list)}{filter_info} | {sec_line} | {cap_status} | 每{interval}s刷新{env_tag}")
    print(f"  小而美: 市值≤{int(MAX_MARKET_CAP/YI)}亿 股价≤{MAX_STOCK_PRICE}元")
    print(f"{'='*96}")

    # 今日首选：从全量候选中机械选出 2 只，减少用户选择迷茫
    top_picks = pick_top_candidates(all_c, top_n=2)
    top_pick_syms = {p.candidate.stock.symbol for p in top_picks}
    if top_picks:
        print(f"\n{ANSI['BOLD']}★ 今日2选（确定性优先，非预测）{ANSI['RESET']}")
        for i, pick in enumerate(top_picks, 1):
            c = pick.candidate
            label = "首选" if i == 1 else "次选"
            stars = "★" * pick.conviction + "☆" * (5 - pick.conviction)
            print(f"  {ANSI['GREEN']}★{ANSI['RESET']} {label}: {c.stock.name}({c.stock.symbol}) "
                  f"{ANSI['BOLD']}{stars}{ANSI['RESET']} 确定性")
            if pick.signals:
                print(f"    驱动: {' | '.join(pick.signals)}")
            if not c.risk_flags:
                print(f"    注意: 无风险标签")
            else:
                print(f"    注意: {ANSI['RED']}{'/'.join(c.risk_flags)}{ANSI['RESET']}")
            if pick.vs_text:
                print(f"    vs {'次选' if i == 1 else '首选'}: {pick.vs_text}")
        print(f"{'-'*96}")

    hdr = (f"  {_pad('排名',4,'r')} {_pad('变化',6,'r')} {_pad('源',4)} {_pad('名称',10)} "
           f"{_pad('代码',12)} {_pad('现价',7,'r')} {_pad('涨幅',8,'r')} "
           f"{_pad('趋势',14)} {_pad('5日累计',8,'r')} {_pad('量比',6,'r')} "
           f"{_pad('评分',4,'r')} {_pad('增强',16)} {_pad('市值',8,'r')}")

    def _print_row(c: Candidate, icon: str = "", show_val: bool = False):
        s = c.stock
        k = c.kline
        # 今日首选标记：在名称前加 ★（无 ANSI 色，避免 _vis_len 误算导致列错位）
        pick_mark = "★ " if s.symbol in top_pick_syms else ""
        display_name = f"{pick_mark}{icon} {s.name}" if icon else f"{pick_mark}{s.name}"
        cur = f"{s.current:.2f}" if s.current else "N/A"
        acc = f"{k.accumulated_pct:+.2f}%" if k else "N/A"
        vr = f"{k.volume_ratio:.1f}x" if k else "N/A"
        score_visible = str(c.score)
        score_tag = f"{ANSI['BOLD']}{_pad(score_visible,4,'r')}{ANSI['RESET']}" if c.score >= 15 else _pad(score_visible,4,'r')
        trend_tag = k.trend if k else "N/A"
        delta_text, delta_color = _rank_delta_str(s.symbol, s.rank, last_ranks)
        delta_display = (f"{delta_color}{_pad(delta_text,6,'r')}{ANSI['RESET']}"
                         if delta_color else _pad(delta_text,6,'r'))
        src_tag = _source_tag(c)
        bonus_str = _bonus_tag(c)
        cap_str = _fmt_market_cap(c.market_cap)
        val_str = f"{s.value:.0f}" if s.value else "N/A"
        # 风险标签：反指维度 + 历史大跌率
        risk_parts = []
        if c.risk_flags:
            risk_parts.append(f"{ANSI['RED']}⚠{'/'.join(c.risk_flags)}{ANSI['RESET']}")
        if c.hist_loss_rate is not None and c.hist_loss_rate >= 25:
            risk_parts.append(f"{ANSI['RED']}[历史大跌率{c.hist_loss_rate:.0f}%]{ANSI['RESET']}")
        risk_str = " ".join(risk_parts)
        if show_val:
            print(f"  {s.rank:>4} {delta_display} {_pad(src_tag,4)} {_pad(display_name,10)} "
                  f"{s.symbol:<12} {cur:>7} {pct_colored(s.percent)} "
                  f"{_pad(trend_tag,14)} {acc:>8} {vr:>6} {score_tag} "
                  f"{_pad(bonus_str,16)} {cap_str:>8} {val_str:>6} {risk_str}")
        else:
            print(f"  {s.rank:>4} {delta_display} {_pad(src_tag,4)} {_pad(display_name,10)} "
                  f"{s.symbol:<12} {cur:>7} {pct_colored(s.percent)} "
                  f"{_pad(trend_tag,14)} {acc:>8} {vr:>6} {score_tag} "
                  f"{_pad(bonus_str,16)} {cap_str:>8} {risk_str}")

    print(f"\n{ANSI['GREEN']}◆ 新面孔 — 底部异动 / 刚启动{ANSI['RESET']}  (找: 今日小涨+日线底部放量)")
    print(hdr)
    print(f"  {'-'*112}")
    if new_faces:
        for c in new_faces:
            if c.stock.symbol in displayed_syms:
                continue
            displayed_syms.add(c.stock.symbol)
            icon = "★" if c.first_breakout_bonus else ("△" if c.category == "known_new_face" else "")
            _print_row(c, icon=icon)
    else:
        print(f"  {ANSI['YELLOW']}暂无新面孔{ANSI['RESET']}")

    if pure_momentum:
        print(f"\n{ANSI['YELLOW']}◆ 动量延续 — 已启动 / 温和上攻{ANSI['RESET']}  (找: 累计涨幅已起+今日温和放量)")
        print(hdr)
        print(f"  {'-'*112}")
        for c in pure_momentum:
            if c.stock.symbol in displayed_syms:
                continue
            displayed_syms.add(c.stock.symbol)
            _print_row(c)

    if rebound_list:
        print(f"\n{ANSI['CYAN']}◆ 超跌反弹 — 暴跌后企稳/反转{ANSI['RESET']}  (找: 5日跌超15%+放量阳线+板块共振)")
        print(hdr)
        print(f"  {'-'*112}")
        for c in rebound_list:
            if c.stock.symbol in displayed_syms:
                continue
            displayed_syms.add(c.stock.symbol)
            _print_row(c, icon="↗")

    if short_term_list:
        print(f"\n{ANSI['RED']}◆ 超短次日 — 今日涨明日卖{ANSI['RESET']}  (找: 涨2-8%+放量+板块活跃)")
        print(hdr)
        print(f"  {'-'*112}")
        for c in short_term_list:
            if c.stock.symbol in displayed_syms:
                continue
            displayed_syms.add(c.stock.symbol)
            _print_row(c, icon="▸")

    if pullback_list:
        print(f"\n{ANSI['RED']}⚠️ 高风险监控 — 回调介入（历史大跌率35%，谨慎参考）{ANSI['RESET']}")
        print(hdr)
        print(f"  {'-'*112}")
        for c in pullback_list:
            if c.stock.symbol in displayed_syms:
                continue
            displayed_syms.add(c.stock.symbol)
            _print_row(c, icon="⚠")

    if stale_candidates:
        print(f"\n{ANSI['YELLOW']}◆ 掉榜回顾 — 仍在观察 (保留{STALE_TIMEOUT_MINUTES}分钟){ANSI['RESET']}")
        print(hdr)
        print(f"  {'-'*108}")
        for c in stale_candidates:
            s = c.stock
            display_name = f"○ {s.name}"
            cur = f"{s.current:.2f}" if s.current else "N/A"
            acc = f"{c.kline.accumulated_pct:+.2f}%" if c.kline else "N/A"
            vr = f"{c.kline.volume_ratio:.1f}x" if c.kline else "N/A"
            score_visible = str(c.score)
            trend_tag = c.kline.trend if c.kline else "N/A"
            if s.symbol in current_rank_map:
                current_rank = current_rank_map[s.symbol]
                delta_text, delta_color = _rank_delta_str(s.symbol, current_rank, last_ranks)
                delta_display = (f"{delta_color}{_pad(delta_text,6,'r')}{ANSI['RESET']}"
                                 if delta_color else _pad(delta_text,6,'r'))
                src_tag = _source_tag(c)
                print(f"  {current_rank:>4} {delta_display} {_pad(src_tag,4)} {_pad(display_name,10)} "
                      f"{s.symbol:<12} {cur:>7} {pct_colored(s.percent)} "
                      f"{_pad(trend_tag,14)} {acc:>8} {vr:>6} {_pad(score_visible,4,'r')}")
            else:
                print(f"  {'—':>4} {'—':>6} {'—':>4} {_pad(display_name,10)} {s.symbol:<12} {cur:>7} {pct_colored(s.percent)} {_pad(trend_tag,14)} {acc:>8} {vr:>6} {_pad(score_visible,4,'r')}")

    print(f"\n{'-'*96}")
    print(f"  {ANSI['GREEN']}新面孔{ANSI['RESET']}: 底部放量启动+涨幅2-6%")
    print(f"  {ANSI['YELLOW']}动量延续{ANSI['RESET']}: 累计涨幅10%+今日温和上攻")
    print(f"  {ANSI['CYAN']}超跌反弹{ANSI['RESET']}: 5日跌超15%+企稳阳线+放量反转")
    print(f"  {ANSI['RED']}超短次日{ANSI['RESET']}: 今日涨2-8%+放量+板块活跃+明日卖出")
    print(f"  {ANSI['CYAN']}回调介入{ANSI['RESET']}: 强势股回踩+缩量+未破位")
