import os

import wcwidth

from scanner.config import (
    MAX_MARKET_CAP,
    MAX_STOCK_PRICE,
    STALE_TIMEOUT_MINUTES,
    TRACK_DISPLAY_BUY_MAX,
    TRACK_DISPLAY_WATCH_MAX,
    TRACK_RECOMMENDATION_DAYS,
    YI,
    now_beijing,
)
from scanner.database import get_prominence_map, get_today_recommendations
from scanner.models import Candidate
from scanner.orchestrator import _session_state

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


def pct_colored(pct: float | None, width: int = 8) -> str:
    if pct is None:
        pct = 0.0
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
            rebound_list: list[Candidate] | None = None,
            tracked_recs: list = None,
            conn=None):
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

    hdr = (f"  {_pad('排名',4,'r')} {_pad('变化',6,'r')} {_pad('源',4)} {_pad('名称',10)} "
           f"{_pad('代码',12)} {_pad('现价',7,'r')} {_pad('涨幅',8,'r')} "
           f"{_pad('趋势',14)} {_pad('5日累计',8,'r')} {_pad('量比',6,'r')} "
           f"{_pad('评分',4,'r')} {_pad('增强',16)} {_pad('市值',8,'r')}")

    def _print_row(c: Candidate, icon: str = "", show_val: bool = False):
        s = c.stock
        k = c.kline
        display_name = f"{icon} {s.name}" if icon else s.name
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
        # 风险标签分级显示：硬信号（超买/主力出货/趋势破位）展开文字，
        # 软信号（疲劳/弱市/涨幅过大/量价背离）折叠成 +N 角标，避免长串红字扰乱注意力。
        HARD_RISK_FLAGS = {"超买", "主力出货", "趋势破位"}
        risk_parts = []
        if c.risk_flags:
            hard = [f for f in c.risk_flags if f in HARD_RISK_FLAGS]
            soft_count = len(c.risk_flags) - len(hard)
            if hard:
                risk_parts.append(f"{ANSI['RED']}⚠{'/'.join(hard)}{ANSI['RESET']}")
            else:
                # 无硬信号但有软信号：提示有软风险但不阻断
                risk_parts.append(f"{ANSI['YELLOW']}⚠+{soft_count}{ANSI['RESET']}")
            if soft_count and hard:
                # 硬信号已展开时，软信号折叠成 +N
                risk_parts[-1] = risk_parts[-1] + f"{ANSI['YELLOW']}+{soft_count}{ANSI['RESET']}"
        risk_str = " ".join(risk_parts)
        # 辨识度标签
        prom_str = ""
        if c.prominence_labels:
            tags = " ".join(c.prominence_labels)
            prom_str = f"{ANSI['CYAN']}│{tags}│{ANSI['RESET']}"
        full_risk = f"{prom_str} {risk_str}" if prom_str else risk_str
        if show_val:
            print(f"  {s.rank:>4} {delta_display} {_pad(src_tag,4)} {_pad(display_name,10)} "
                  f"{s.symbol:<12} {cur:>7} {pct_colored(s.percent)} "
                  f"{_pad(trend_tag,14)} {acc:>8} {vr:>6} {score_tag} "
                  f"{_pad(bonus_str,16)} {cap_str:>8} {val_str:>6} {full_risk}")
        else:
            print(f"  {s.rank:>4} {delta_display} {_pad(src_tag,4)} {_pad(display_name,10)} "
                  f"{s.symbol:<12} {cur:>7} {pct_colored(s.percent)} "
                  f"{_pad(trend_tag,14)} {acc:>8} {vr:>6} {score_tag} "
                  f"{_pad(bonus_str,16)} {cap_str:>8} {full_risk}")

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

    display_priority(conn)

    if tracked_recs:
        # 只显示高确信"到买点"；"观察中"默认隐藏（TRACK_DISPLAY_WATCH_MAX=0），
        # 需要时把该常量改回 >0 可恢复补充尾部。
        buy = [t for t in tracked_recs if t.status == "到买点"]
        watch = [t for t in tracked_recs if t.status == "观察中"]

        def _tracked_row(t):
            today_str = pct_colored(t.today_pct)
            cum_str = f"{t.cum_return:+.1f}%"
            if t.cum_return >= 5:
                cum_color = ANSI["GREEN"]
            elif t.cum_return <= -5:
                cum_color = ANSI["RED"]
            else:
                cum_color = ""
            cum_display = f"{cum_color}{cum_str:>10}{ANSI['RESET']}" if cum_color else f"{cum_str:>10}"
            # 状态着色：到买点绿色，观察中黄色
            if t.status == "到买点":
                status_display = f"{ANSI['GREEN']}{_pad(t.status,8)}{ANSI['RESET']}"
            else:
                status_display = f"{ANSI['YELLOW']}{_pad(t.status,8)}{ANSI['RESET']}"
            signals_str = "/".join(t.signals) if t.signals else ""
            prom_str = ""
            if t.prominence_labels:
                tags = " ".join(t.prominence_labels)
                prom_str = f" {ANSI['CYAN']}│{tags}│{ANSI['RESET']}"
            print(f"  {t.rec_date} {_pad(t.name,10)} {t.symbol:<12} {_pad(t.rec_category,14)} "
                  f"{status_display} {t.buy_signals:>4} {today_str} {cum_display} {signals_str}{prom_str}")

        print(f"\n{ANSI['CYAN']}◆ 历史推荐跟踪 — 近{TRACK_RECOMMENDATION_DAYS}日推荐回调到买点{ANSI['RESET']}")
        print(f"  {'—'*108}")
        print(f"  {_pad('推荐日',10)} {_pad('名称',10)} {_pad('代码',12)} {_pad('策略',14)} "
              f"{_pad('状态',8)} {_pad('信号',4,'r')} {_pad('今日涨幅',10,'r')} "
              f"{_pad('累计收益',10,'r')} {_pad('买点信号',30)}")
        print(f"  {'-'*108}")
        # 仅展示高确信"到买点"
        for t in buy[:TRACK_DISPLAY_BUY_MAX]:
            _tracked_row(t)
        # 仅当配置允许（TRACK_DISPLAY_WATCH_MAX > 0）且到买点不足时，才补充"观察中"尾部
        if TRACK_DISPLAY_WATCH_MAX > 0 and len(buy) < TRACK_DISPLAY_BUY_MAX:
            for t in watch[:TRACK_DISPLAY_WATCH_MAX]:
                _tracked_row(t)
        elif TRACK_DISPLAY_WATCH_MAX > 0 and watch:
            print(f"  {ANSI['YELLOW']}  · 另有 {len(watch)} 只观察中（已省略，回调结构不充分）{ANSI['RESET']}")

    print(f"\n{'-'*96}")
    print(f"  {ANSI['GREEN']}新面孔{ANSI['RESET']}: 底部放量启动+涨幅2-6%")
    print(f"  {ANSI['YELLOW']}动量延续{ANSI['RESET']}: 累计涨幅10%+今日温和上攻")
    print(f"  {ANSI['CYAN']}超跌反弹{ANSI['RESET']}: 5日跌超15%+企稳阳线+放量反转")
    print(f"  {ANSI['RED']}超短次日{ANSI['RESET']}: 今日涨2-8%+放量+板块活跃+明日卖出")
    print(f"  {ANSI['CYAN']}回调介入{ANSI['RESET']}: 强势股回踩+缩量+未破位")


def display_priority(conn=None):
    """从本地数据库读取今日所有进入过推荐的票，按策略优先级+评分降序展示。"""
    if conn is None:
        return

    today_recs = get_today_recommendations(conn)
    if not today_recs:
        return

    CAT_PRIORITY = {
        "known_new_face": 0, "rebound": 1, "new_face": 2,
        "momentum": 3, "short_term": 4, "pullback": 5,
    }
    CAT_LABEL = {
        "known_new_face": "kNF", "rebound": "RBD", "new_face": "NEW",
        "momentum": "MOM", "short_term": "ST", "pullback": "PB",
    }
    CAT_COLOR = {
        "known_new_face": ANSI["GREEN"], "rebound": ANSI["CYAN"], "new_face": ANSI["GREEN"],
        "momentum": ANSI["YELLOW"], "short_term": ANSI["RED"], "pullback": ANSI["RED"],
    }
    SUGGEST = {
        0: f"{ANSI['GREEN']}推荐{ANSI['RESET']}",
        1: f"{ANSI['CYAN']}推荐{ANSI['RESET']}",
        2: "参考",
        3: "参考",
        4: f"{ANSI['RED']}回避{ANSI['RESET']}",
        5: f"{ANSI['RED']}回避{ANSI['RESET']}",
    }

    for entry in today_recs:
        pool_c = _session_state.today_pool.get(entry["symbol"])
        entry["_candidate"] = pool_c

    prom_syms = [e["symbol"] for e in today_recs if not e["_candidate"]]
    if prom_syms:
        prom_map = get_prominence_map(conn, prom_syms)
    else:
        prom_map = {}
    for entry in today_recs:
        if entry["_candidate"]:
            continue
        entry["_prominent"] = prom_map.get(entry["symbol"], False)

    scored = sorted(today_recs, key=lambda x: (CAT_PRIORITY.get(x["category"], 99), -x["score"]))

    print(f"\n{ANSI['BOLD']}◆ 综合排序 — 今日所有推荐 按策略优先级+评分降序{ANSI['RESET']}")
    hdr = (f"  {_pad('#',3,'r')} {_pad('代码',12)} {_pad('名称',10)} "
           f"{_pad('策略',5)} {_pad('评分',4,'r')} {_pad('涨幅',8,'r')} "
           f"{_pad('现价',7,'r')} {_pad('排名',4,'r')} {_pad('时间',6)} {_pad('建议',6)}")
    print(hdr)
    print(f"  {'-'*78}")
    for i, entry in enumerate(scored, 1):
        c = entry["_candidate"]
        if c:
            s = c.stock
            label = CAT_LABEL.get(c.category, c.category)
            color = CAT_COLOR.get(c.category, "")
            label_display = f"{color}{label}{ANSI['RESET']}"
            priority = CAT_PRIORITY.get(c.category, 99)
            rank_str = f"{s.rank}" if s.rank else "N/A"
            price_str = f"{s.current:.2f}" if s.current else "—"
            pct = s.percent
            prom_str = ""
            if c.prominence_labels:
                tags = " ".join(c.prominence_labels)
                prom_str = f" {ANSI['CYAN']}│{tags}│{ANSI['RESET']}"
            risk_str = ""
            if c.risk_flags:
                risk_str = f" {ANSI['YELLOW']}⚠{ANSI['RESET']}"
        else:
            label = CAT_LABEL.get(entry["category"], entry["category"])
            color = CAT_COLOR.get(entry["category"], "")
            label_display = f"{color}{label}{ANSI['RESET']}"
            priority = CAT_PRIORITY.get(entry["category"], 99)
            rank_str = f"{entry['live_rank']}" if entry.get("live_rank") else "—"
            price_str = "—"
            pct = entry.get("live_percent", 0.0)
            prom_str = ""
            if entry.get("_prominent"):
                prom_str = f" {ANSI['CYAN']}│↻│{ANSI['RESET']}"
            risk_str = ""
        # 建议列：按策略优先级映射（推荐/参考/回避），SUGGEST 含 ANSI 需用 _pad 对齐
        suggest_str = SUGGEST.get(priority, "")
        first_time = entry.get("first_time", entry.get("time", ""))[:5]
        # label_display 含 ANSI 码，用 _pad 按可见宽度对齐（:>5 会按含 ANSI 的字符串长度计算，错位）
        print(f"  {i:3d}  {entry['symbol']:<12} {_pad(entry['name'],10)} "
              f"{_pad(label_display,5,'r')} {entry['score']:4d} {pct_colored(pct)} "
              f"{price_str:>7} {rank_str:>4} {_pad(first_time,6)} {_pad(suggest_str,6)}{prom_str}{risk_str}")
    print(f"  {'-'*78}")
    print(f"  {SUGGEST[0]} → {ANSI['GREEN']}kNF(已知新面孔){ANSI['RESET']}/{ANSI['CYAN']}RBD(超跌反弹){ANSI['RESET']}  |  参考 → {ANSI['YELLOW']}MOM(动量){ANSI['RESET']}/NEW(新面孔)  |  {SUGGEST[4]} → ST(超短)/PB(回调,负期望)")
    print(f"  {ANSI['CYAN']}↻{ANSI['RESET']} 辨识度高(近5日上榜≥3次均排名≤70)  {ANSI['YELLOW']}⚠{ANSI['RESET']} 带有风险标签")
