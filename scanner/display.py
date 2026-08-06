import os

import wcwidth

from scanner.config import (
    CAT_DISPLAY_PRIORITY,
    FUND_FLOW_MAIN_PCT_EXTREME,
    FUND_FLOW_MAIN_PCT_STRONG,
    FUND_FLOW_MAIN_PCT_WEAK,
    MAX_MARKET_CAP,
    MAX_STOCK_PRICE,
    SUGGEST_BY_CAT,
    TRACK_DISPLAY_BUY_MAX,
    TRACK_DISPLAY_WATCH_MAX,
    TRACK_RECOMMENDATION_DAYS,
    YI,
    now_beijing,
)
from scanner.database import get_market_extra_cache, get_prominence_map, get_today_recommendations
from scanner.models import Candidate
from scanner.orchestrator import _session_state
from scanner.sector import classify_sector

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


def _trunc(s: str, width: int) -> str:
    """按可见宽度截断（中文全角按 2 列计），超长时尾部补 …。"""
    if _vis_len(s) <= width:
        return s
    out = ""
    for ch in s:
        if _vis_len(out) + max(0, wcwidth.wcwidth(ch)) > width - 1:
            break
        out += ch
    return out + "…"


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
    if c.fund_flow_bonus:
        parts.append(f"F{c.fund_flow_bonus:+d}")
    if c.zt_lianban_bonus:
        parts.append(f"Z{c.zt_lianban_bonus:+d}")
    return " ".join(parts) if parts else ""


def fund_flow_signal(main_pct: float | None) -> str:
    """主力净占比 → 强弱档位（与 enhancer 加分/资金流出标签阈值同源）。

    返回 strong_in / in / neutral / out / strong_out；无数据返回 ""。
    """
    if main_pct is None:
        return ""
    if main_pct >= FUND_FLOW_MAIN_PCT_EXTREME:
        return "strong_in"
    if main_pct >= FUND_FLOW_MAIN_PCT_STRONG:
        return "in"
    if main_pct <= -FUND_FLOW_MAIN_PCT_EXTREME:
        return "strong_out"
    if main_pct <= FUND_FLOW_MAIN_PCT_WEAK:
        return "out"
    return "neutral"


_FUND_FLOW_ICON = {
    "strong_in": f"{ANSI['GREEN']}▲▲{ANSI['RESET']}",
    "in": f"{ANSI['GREEN']}▲{ANSI['RESET']}",
    "neutral": f"{ANSI['YELLOW']}◇{ANSI['RESET']}",
    "out": f"{ANSI['RED']}▼{ANSI['RESET']}",
    "strong_out": f"{ANSI['RED']}▼▼{ANSI['RESET']}",
}


def _fund_flow_icon_str(ff_pct) -> str:
    """主力净占比 → 5 档图标（ANSI 着色）；无数据返回空串。"""
    if ff_pct is None:
        return ""
    return _FUND_FLOW_ICON.get(fund_flow_signal(float(ff_pct)), "")


def _market_extra_str(c: Candidate) -> str:
    """行情增强标记：主力资金流强弱图标 + 连板/炸板（无数据返回空串）。

    资金流用 fund_flow_signal 5 档图标替代原「资+x.x% ±xxx万」文本；
    展示型信息，追加在行尾可变区，不参与固定列对齐。
    """
    dims = c.kline.dimensions if c.kline else {}
    parts = []
    ff_pct = dims.get("fund_flow_main_pct")
    if ff_pct is not None:
        icon = _fund_flow_icon_str(ff_pct)
        if icon:
            parts.append(icon)
    zt_lb = dims.get("zt_lianban")
    zt_zb = dims.get("zt_zhaban")
    if zt_lb:
        if zt_zb:
            parts.append(f"{ANSI['YELLOW']}连{zt_lb}炸{zt_zb}{ANSI['RESET']}")
        else:
            parts.append(f"{ANSI['RED']}连{zt_lb}板{ANSI['RESET']}")
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
            pullback_list: list[Candidate] | None = None,
            short_term_list: list[Candidate] | None = None,
            rebound_list: list[Candidate] | None = None,
            tracked_recs: list = None,
            conn=None, live_quotes: dict[str, dict] | None = None,
            rank_map: dict[str, int] | None = None):
    if last_ranks is None:
        last_ranks = {}
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
        extra_str = _market_extra_str(c)
        extra_suffix = f" {extra_str}" if extra_str else ""
        if show_val:
            print(f"  {s.rank:>4} {delta_display} {_pad(src_tag,4)} {_pad(display_name,10)} "
                  f"{s.symbol:<12} {cur:>7} {pct_colored(s.percent)} "
                  f"{_pad(trend_tag,14)} {acc:>8} {vr:>6} {score_tag} "
                  f"{_pad(bonus_str,16)} {cap_str:>8} {val_str:>6} {full_risk}{extra_suffix}")
        else:
            print(f"  {s.rank:>4} {delta_display} {_pad(src_tag,4)} {_pad(display_name,10)} "
                  f"{s.symbol:<12} {cur:>7} {pct_colored(s.percent)} "
                  f"{_pad(trend_tag,14)} {acc:>8} {vr:>6} {score_tag} "
                  f"{_pad(bonus_str,16)} {cap_str:>8} {full_risk}{extra_suffix}")

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

    display_priority(conn, live_quotes=live_quotes, rank_map=rank_map)

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


def display_priority(conn=None, live_quotes: dict[str, dict] | None = None,
                     rank_map: dict[str, int] | None = None):
    """从本地数据库读取今日所有进入过推荐的票，按档位(辨识度/资金流)+展示优先级(CAT_DISPLAY_PRIORITY)+评分降序展示。

    live_quotes: {symbol: {percent, current}} 实时行情覆盖，优先于候选池和数据库数据。
    rank_map: {symbol: 飙升榜排名} 当前扫描的榜单排名，为掉榜/重启行补实时排名。
    排序键 = (档位, CAT_DISPLAY_PRIORITY, -score)：档0置前(辨识度或净流入≥5%) < 档1普通 <
    档2劣后(净流出≤-5%，覆盖辨识度)。档位只影响排序，不改评分列/不落库。
    """
    if conn is None:
        return

    today_recs = get_today_recommendations(conn)
    if not today_recs:
        return

    CAT_LABEL = {
        "known_new_face": "kNF", "rebound": "RBD", "new_face": "NEW",
        "momentum": "MOM", "short_term": "ST", "pullback": "PB",
    }
    CAT_COLOR = {
        "known_new_face": ANSI["GREEN"], "rebound": ANSI["CYAN"], "new_face": ANSI["GREEN"],
        "momentum": ANSI["YELLOW"], "short_term": ANSI["RED"], "pullback": ANSI["RED"],
    }

    for entry in today_recs:
        pool_c = _session_state.today_pool.get(entry["symbol"])
        entry["_candidate"] = pool_c

    if live_quotes:
        for entry in today_recs:
            q = live_quotes.get(entry["symbol"])
            if q is not None:
                # live_quote_available：标记该行拿到了本次实时批量行情（live_percent=0.0
                # 是合法的 0.00%，不能当作缺失；get_today_recommendations 默认填 0.0 需区分）。
                entry["live_quote_available"] = True
                entry["live_percent"] = q.get("percent", 0.0)
                entry["live_current"] = q.get("current", 0.0)
                q_rank = q.get("rank")
                if q_rank is not None:
                    entry["live_rank"] = q_rank

    # 排名实时覆盖：live_quotes（batch/quote）不含 rank，用当前飙升榜排名补上，
    # 使综合排序「排名」列对仍在上榜的票实时可见（掉榜/重启行此前恒为 —）。
    if rank_map:
        for entry in today_recs:
            if entry.get("live_rank") is None:
                r = rank_map.get(entry["symbol"])
                if r is not None:
                    entry["live_rank"] = r

    prom_syms = [e["symbol"] for e in today_recs if not e["_candidate"]]
    if prom_syms:
        prom_map = get_prominence_map(conn, prom_syms)
    else:
        prom_map = {}
    for entry in today_recs:
        if entry["_candidate"]:
            continue
        entry["_prominent"] = prom_map.get(entry["symbol"], False)

    # 资金流图标：从 market_extra_cache 直接读当日资金流，不依赖当前进程 today_pool。
    # 候选存在时优先用其扫描时的最新维度，否则（重启/掉榜/扫描时拉取失败）回退到 DB
    # 保存的全市场快照——避免综合排序大量行因进程重启丢失资金流图标。
    flow_pct_map: dict[str, float] = {}
    try:
        ff_db = get_market_extra_cache(conn, list({e["symbol"] for e in today_recs}), "fund_flow")
        flow_pct_map = {sym: (payload.get("main_pct") if payload else None)
                        for sym, payload in ff_db.items()}
    except Exception:
        flow_pct_map = {}

    # 档位置顶（2026-08-06）：排序键 (档位, 类别优先级, -score)，跨类别全局生效。
    # 档0置前 = 辨识度(↻) 或 主力净流入 ≥ FUND_FLOW_MAIN_PCT_STRONG；档2劣后 = 净流出
    # ≤ FUND_FLOW_MAIN_PCT_WEAK（覆盖辨识度）；其余档1普通。只改排序，不改评分列/不落库。
    # 数据源与展示图标一致：资金流候选用扫描维度、否则回退 DB 快照 flow_pct_map；辨识度
    # 候选用扫描时标签、掉榜行用 prom_map——掉榜行 DB score 不含这些字段，展示层统一分档。
    def _sort_tier(entry):
        c = entry["_candidate"]
        ff = c.kline.dimensions.get("fund_flow_main_pct") if c and c.kline else None
        if ff is None:
            ff = flow_pct_map.get(entry["symbol"])
        prominent = bool(c.prominence_labels) if c else prom_map.get(entry["symbol"], False)
        if ff is not None and ff <= FUND_FLOW_MAIN_PCT_WEAK:
            return 2
        if prominent or (ff is not None and ff >= FUND_FLOW_MAIN_PCT_STRONG):
            return 0
        return 1

    scored = sorted(today_recs, key=lambda x: (_sort_tier(x),
                                               CAT_DISPLAY_PRIORITY.get(x["category"], 99),
                                               -x["score"]))

    print(f"\n{ANSI['BOLD']}◆ 综合排序 — 今日所有推荐 按档位(辨识度/资金流)+类别优先级+评分降序{ANSI['RESET']}")
    hdr = (f"  {_pad('#',3,'r')} {_pad('代码',12)} {_pad('名称',10)} "
           f"{_pad('板块',14)} {_pad('策略',5)} {_pad('评分',4,'r')} {_pad('涨幅',8,'r')} "
           f"{_pad('5日累计',8,'r')} {_pad('现价',7,'r')} {_pad('排名',4,'r')} {_pad('时间',6)} {_pad('建议',6)}")
    print(hdr)
    print(f"  {'-'*100}")
    for i, entry in enumerate(scored, 1):
        c = entry["_candidate"]
        sector = classify_sector(entry["name"])
        # 标签/优先级/建议列统一用 entry["category"]（与排序口径一致），
        # 不用 c.category：双挂票的 today_pool 按 symbol 覆盖会拿到 short_term 候选，
        # 而 DB 保留的是最高分行的 category（可能是 new_face），两者不一致会导致
        # 排到 new_face 档却显示 ST 标签 + 回避建议的矛盾。
        cat = entry["category"]
        # 板块列优先级：推荐时落库的推动概念 > 当前池候选的推动概念 > 分类板块 > 名称关键词
        db_concept = (entry.get("concept") or "").strip()
        if db_concept:
            sector = db_concept
        elif c:
            if c.driving_concept:
                sector = c.driving_concept
            elif c.sector:
                sector = c.sector
        # 涨幅/现价/排名统一回退链：实时行情(live_quotes/rank_map) → 候选池当前扫描快照 →
        # appearances(DB) → 推荐时落库值。此前 live_quotes 仅在无候选行生效，候选行走扫描快照，
        # 导致同一张表内实时性不一致。live_quote_available 标记本次拿到实时批量行情。
        if entry.get("live_quote_available"):
            pct = entry.get("live_percent")
            if pct is None:
                pct = 0.0
            live_cur = entry.get("live_current", 0.0)
            live_rank = entry.get("live_rank")
        elif c:
            pct = c.stock.percent
            live_cur = c.stock.current
            live_rank = c.stock.rank
        else:
            pct = entry.get("live_percent") or entry.get("percent", 0.0)
            live_cur = 0.0
            live_rank = entry.get("live_rank")
        # 实时行情不含 rank/current（batch/quote 无 rank 字段）时，回退候选快照。
        if not live_cur and c and c.stock.current:
            live_cur = c.stock.current
        if not live_rank and c and c.stock.rank:
            live_rank = c.stock.rank
        price_str = f"{live_cur:.2f}" if live_cur else "—"
        rank_str = f"{live_rank}" if live_rank else "—"
        if c:
            label = CAT_LABEL.get(cat, cat)
            color = CAT_COLOR.get(cat, "")
            label_display = f"{color}{label}{ANSI['RESET']}"
            prom_str = ""
            if c.prominence_labels:
                tags = " ".join(c.prominence_labels)
                prom_str = f" {ANSI['CYAN']}│{tags}│{ANSI['RESET']}"
            risk_str = ""
            if c.risk_flags:
                risk_str = f" {ANSI['YELLOW']}⚠{ANSI['RESET']}"
        else:
            label = CAT_LABEL.get(cat, cat)
            color = CAT_COLOR.get(cat, "")
            label_display = f"{color}{label}{ANSI['RESET']}"
            prom_str = ""
            if entry.get("_prominent"):
                prom_str = f" {ANSI['CYAN']}│↻│{ANSI['RESET']}"
            risk_str = ""
        # 建议列：按类别独立映射（与展示优先级解耦），SUGGEST_BY_CAT 含 ANSI 需用 _pad 对齐
        suggest_str = SUGGEST_BY_CAT.get(cat, "")
        first_time = entry.get("first_time", entry.get("time", ""))[:5]
        # 5日累计涨幅：优先用候选池的 kline 数据（与上方各策略桶口径一致），否则用 DB 落库值
        accum_val = None
        if c and c.kline:
            accum_val = c.kline.accumulated_pct
        elif entry.get("accumulated_pct") is not None:
            accum_val = entry["accumulated_pct"]
        if accum_val is None:
            accum_str = "—"
        else:
            accum_str = f"{accum_val:+.2f}%"
        # label_display 含 ANSI 码，用 _pad 按可见宽度对齐（:>5 会按含 ANSI 的字符串长度计算，错位）
        extra_str = ""
        if c:
            extra_str = _market_extra_str(c)
            ff_pct = c.kline.dimensions.get("fund_flow_main_pct") if c.kline else None
            if ff_pct is None:
                icon = _fund_flow_icon_str(flow_pct_map.get(entry["symbol"]))
                if icon:
                    extra_str = f"{extra_str} {icon}".strip()
        else:
            icon = _fund_flow_icon_str(flow_pct_map.get(entry["symbol"]))
            if icon:
                extra_str = icon
        extra_suffix = f" {extra_str}" if extra_str else ""
        print(f"  {i:3d}  {entry['symbol']:<12} {_pad(entry['name'],10)} "
              f"{_pad(_trunc(sector,14),14)} {_pad(label_display,5,'r')} {entry['score']:4d} {pct_colored(pct)} "
              f"{accum_str:>8} {price_str:>7} {rank_str:>4} {_pad(first_time,6)} {_pad(suggest_str,6)}{prom_str}{risk_str}{extra_suffix}")
    print(f"  {'-'*92}")
    print(f"  {SUGGEST_BY_CAT['known_new_face']} → {ANSI['GREEN']}kNF(已知新面孔){ANSI['RESET']}"
          f"  |  {SUGGEST_BY_CAT['rebound']} → {ANSI['CYAN']}RBD(超跌反弹){ANSI['RESET']}"
          f"  |  {SUGGEST_BY_CAT['new_face']} → NEW(新面孔)"
          f"  |  {SUGGEST_BY_CAT['momentum']} → {ANSI['YELLOW']}MOM(动量){ANSI['RESET']}"
          f"  |  {SUGGEST_BY_CAT['short_term']} → ST(超短次日卖)"
          f"  |  {SUGGEST_BY_CAT['pullback']} → PB(回调,负期望)")
    print(f"  {ANSI['CYAN']}↻{ANSI['RESET']} 辨识度高(近5日上榜≥3次均排名≤70)  {ANSI['YELLOW']}⚠{ANSI['RESET']} 带有风险标签")
    print(f"  排序档位: ▲▲/▲(净流入≥5%) 或 ↻ → 置前  |  ▼/▼▼(净流出≤-5%) → 劣后  |  其余 → 普通")
