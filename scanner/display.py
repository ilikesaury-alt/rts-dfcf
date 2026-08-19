import math
import os
import re
import sys

import wcwidth

from scanner.config import (
    CAT_DISPLAY_PRIORITY,
    COMEBACK_DISPLAY_MAX,
    COMEBACK_DISPLAY_MIN_MAIN,
    FUND_FLOW_MAIN_PCT_EXTREME,
    FUND_FLOW_MAIN_PCT_STRONG,
    FUND_FLOW_MAIN_PCT_WEAK,
    NEXTDAY_ACCUM_MIN,
    NEXTDAY_CAT_PRIORITY,
    NEXTDAY_SPIKE_MID_MAX,
    NEXTDAY_SPIKE_MID_MIN,
    NEXTDAY_SPIKE_SWEET_LOW,
    NEXTDAY_SPIKE_SWEET_MIN,
    RISK_FLAGS_DISPLAY_HARD,
    SECTOR_RESONANCE_WARN_MAX,
    SUGGEST_BY_CAT,
    TOP40_THRESHOLD,
    now_beijing,
)
from scanner.database import get_fund_flow_pct_map, get_prominence_map, get_today_recommendations
from scanner.models import Candidate
from scanner.sector import classify_sector

# ANSI SGR 转义序列（\x1b[...m：颜色/加粗/复位）。_vis_len 必须先剥离它们再量宽度，
# 否则 `[`、数字、`;`、`m` 等可打印字符各被 wcwidth 计 1 列，彩色文本被高估宽度，
# _pad 少补空格 → 实际渲染更窄 → 后续固定列整体错位。
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")

if os.name == "nt":
    import ctypes
    _kernel32 = ctypes.windll.kernel32
    _handle = _kernel32.GetStdHandle(-11)
    _mode = ctypes.c_uint32()
    # 是否「真实 Windows conhost」：GetConsoleMode 仅对真实控制台成功；
    # pty/终端模拟器/重定向管道均失败（返回 0），但它们通常讲 ANSI/VT 协议。
    _is_console = _kernel32.GetConsoleMode(_handle, ctypes.byref(_mode)) != 0
    _supports_ansi = _is_console and _kernel32.SetConsoleMode(_handle, _mode.value | 0x0004) != 0
else:
    _is_console = False
    _supports_ansi = True

if _supports_ansi:
    ANSI = {
        "RED": "\033[91m", "YELLOW": "\033[93m", "GREEN": "\033[92m",
        "CYAN": "\033[96m", "BOLD": "\033[1m", "RESET": "\033[0m",
    }
else:
    ANSI = {"RED": "", "YELLOW": "", "GREEN": "", "CYAN": "", "BOLD": "", "RESET": ""}

# 类别展示标签/颜色：综合排序与回马枪独立区共用（提出模块级供 _print_priority_row 复用）。
CAT_LABEL = {
    "known_new_face": "kNF", "rebound": "RBD", "new_face": "NEW",
    "momentum": "MOM", "short_term": "ST", "pullback": "PB",
    "comeback": "CB",
}
CAT_COLOR = {
    "known_new_face": ANSI["GREEN"], "rebound": ANSI["CYAN"], "new_face": ANSI["GREEN"],
    "momentum": ANSI["YELLOW"], "short_term": ANSI["RED"], "pullback": ANSI["RED"],
    "comeback": ANSI["CYAN"],
}


def _rank_delta_str(symbol: str, current_rank: int, last_ranks: dict[str, int]) -> str:
    """雪球榜单排名较上一轮扫描的变化：+N 上升 / -N 下降 / "" 无变化或无上轮。

    diff = prev - current > 0 表示名次上升（数字变小排前面）；升≥5 名红色、
    降≥5 名绿色（中国行情配色，红色=强/向上），小幅用纯符号无着色。

    2026-08-13：改用 ASCII 半角 + / -（颜色保留），避免 ↑/↓ 在部分中文终端
    渲染为全角导致的宽度二义（排名列固定宽度计算见 _vis_len 的 ANSI 说明）。
    """
    prev = last_ranks.get(symbol)
    if prev is None:
        return ""
    diff = prev - current_rank
    if diff > 0:
        if diff >= 5:
            return f"{ANSI['RED']}+{diff}{ANSI['RESET']}"
        return f"+{diff}"
    if diff < 0:
        if -diff >= 5:
            return f"{ANSI['GREEN']}-{-diff}{ANSI['RESET']}"
        return f"-{-diff}"
    return ""


def _vis_len(s: str) -> int:
    """计算字符串的终端可见宽度（中文等宽字符按 2 列计）。

    2026-08-13 修复：先前仅把 \x1b（ESC，wcwidth 返回 -1）归零，但转义序列中
    后续的可打印字符（[ 9 1 m 等）各按 1 列计算，导致 `\033[91m45\033[0m` 被
    高估为 9 列（实际 2 列）。_pad 据此少补空格，彩色单元格在终端渲染得更窄，
    其后所有固定列整体左移错位（排名列 TOP40 高亮 / ≥5 名着色 delta 最易触发）。
    现统一先剥离 ANSI SGR 序列再量宽度。
    """
    return sum(max(0, wcwidth.wcwidth(c)) for c in _ANSI_ESCAPE.sub("", s))


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
    """清空终端屏幕（主扫描器 display() 渲染前调用，避免上一屏内容逐行叠加）。

    清屏策略（2026-08-13 修订，修复 pty/终端模拟器下 os.system("cls") 无效）：
    - 输出被重定向/管道（isatty=False）→ 跳过，不注入 ANSI 序列污染日志文件。
    - 仅「真实 Windows conhost 但不支持 VT 的旧版控制台」用 os.system("cls")。
    - 其余一律 ANSI \\033[2J\\033[H：现代 conhost（导入时已启用 VT）、Windows
      Terminal、pty 终端模拟器等均讲 ANSI/VT 协议；pty 下 cls 不生效（cmd 不
      共享 pty 的屏幕缓冲），正是此前「创业板飙升榜监控」表头逐轮叠加的根因。
    """
    if not sys.stdout.isatty():
        return
    if os.name == "nt" and _is_console and not _supports_ansi:
        os.system("cls")
        return
    print("\033[2J\033[H", end="", flush=True)


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


def split_risk_flags(risk_flags: list[str]) -> tuple[list[str], int]:
    """风险标签分级：返回 (硬信号列表, 软信号数量)。

    硬信号（RISK_FLAGS_DISPLAY_HARD：超买/主力出货/趋势破位）展开文字显示，
    软信号折叠成 +N 角标。display/feishu 共用，避免两处各自维护阈值集合。
    """
    hard = [f for f in risk_flags if f in RISK_FLAGS_DISPLAY_HARD]
    return hard, len(risk_flags) - len(hard)


def _render_prominence(prominence_labels: list[str] | None) -> str:
    """辨识度标签（↻ 等）渲染；空列表返回空串。返回段无前导空格，分隔由调用方处理。"""
    if not prominence_labels:
        return ""
    return f"{ANSI['CYAN']}│{' '.join(prominence_labels)}│{ANSI['RESET']}"


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


def _market_env_tag(today_pool: dict[str, Candidate] | None) -> str:
    """大盘环境标签（中性/强势/弱势·谨慎），从候选池 dims 读 market_env_bonus。"""
    env_bonus = 0
    for c in (today_pool or {}).values():
        if c.kline and c.kline.dimensions:
            env_bonus = c.kline.dimensions.get("market_env_bonus", 0) or 0
            break
    if env_bonus > 0:
        return f"{ANSI['GREEN']}[大盘强势]{ANSI['RESET']}"
    if env_bonus < 0:
        return f"{ANSI['RED']}[大盘弱势·谨慎]{ANSI['RESET']}"
    return "[大盘中性]"


def display(gem_total: int, interval: int, filtered_large_cap: int = 0,
            conn=None, live_quotes: dict[str, dict] | None = None,
            rank_map: dict[str, int] | None = None,
            today_pool: dict[str, Candidate] | None = None,
            last_ranks: dict[str, int] | None = None):
    """扫描主屏：头部摘要 + 综合排序总表（含回马枪/次日大涨子区）。

    策略桶（新面孔/动量/反弹/回马枪/超短）2026-08-10 下线：与综合排序重复列同一批票、
    每桶重复列头；综合排序表已带类别标签，桶区信息不再单列。

    today_pool：本轮候选池快照（symbol → Candidate），由调用方（scan_with_raw 的
    ScanResult）传入，display 不直接访问 orchestrator 内部状态。
    last_ranks: 上一轮扫描的榜单排名 {symbol: rank}，供综合排序「排名」列显示变化
    （+N 升 / -N 降），与已下线策略桶同口径。
    """
    clear_screen()
    now = now_beijing().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{'='*96}")
    print(f"  创业板飙升榜监控  ({now})")
    filter_info = f" | 过滤{filtered_large_cap}只" if filtered_large_cap else ""
    print(f"  创业板共 {gem_total} 只{filter_info} | 每{interval}s刷新 | {_market_env_tag(today_pool)}")
    print(f"{'='*96}")
    display_priority(conn, live_quotes=live_quotes, rank_map=rank_map, today_pool=today_pool,
                     last_ranks=last_ranks)


def _print_priority_row(entry: dict, i: int, flow_pct_map: dict,
                        nextday_mark: bool = False,
                        last_ranks: dict[str, int] | None = None) -> None:
    """综合排序单行的统一渲染（主表与回马枪独立区共用），避免两处复制大段渲染逻辑。

    flow_pct_map: {symbol: 主力净占比} DB 快照回退（候选缺失/扫描失败时仍显示资金流图标）。
    nextday_mark: 次日大涨画像（🎯）——推荐时刻涨幅甜蜜带 + 非超买（见 _is_nextday_marked）。
    视觉标记 + 参与综合排序档位置顶（display_priority._sort_tier 档0），不改 score / 不落库。
    last_ranks: 上一轮扫描的榜单排名 {symbol: rank}，用于「排名」列展示雪球榜单排名变化
    （+N 升 / -N 降），与已下线策略桶的 _rank_delta_str 同口径；缺省 None 不显示变化。
    """
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
    # appearances(DB) → 推荐时落库值。
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
        # live_percent 可能为 0.0（合法 0.00% 涨幅），不能用 `or` 回退——
        # 否则 0.00% 的票会错误显示成推荐时落库的 percent（如 5.0%）。
        _lp = entry.get("live_percent")
        pct = _lp if _lp is not None else entry.get("percent", 0.0)
        live_cur = 0.0
        live_rank = entry.get("live_rank")
    # 实时行情不含 rank/current（batch/quote 无 rank 字段）时，回退候选快照。
    if not live_cur and c and c.stock.current:
        live_cur = c.stock.current
    if not live_rank and c and c.stock.rank:
        live_rank = c.stock.rank
    price_str = f"{live_cur:.2f}" if live_cur else "—"
    # 排名列：当前名次 + 较上一轮扫描的变化（+N 升 / -N 降），无上轮或不变化仅显名次。
    # 名次在雪球榜单前 TOP40_THRESHOLD 内时高亮（加粗 + 红色），TOP40 视为热度强势信号。
    if live_rank:
        delta_str = _rank_delta_str(entry["symbol"], live_rank, last_ranks or {})
        rank_num = f"{live_rank}"
        if 0 < live_rank <= TOP40_THRESHOLD:
            rank_num = f"{ANSI['BOLD']}{ANSI['RED']}{rank_num}{ANSI['RESET']}"
        rank_str = f"{rank_num}{delta_str}" if delta_str else rank_num
    else:
        rank_str = "—"
    label_display = f"{CAT_COLOR.get(cat, '')}{CAT_LABEL.get(cat, cat)}{ANSI['RESET']}"
    # 回马枪变体（反转/回踩）：策略桶下线后由此处单行保留，避免掉榜区丢失语义。
    if cat == "comeback":
        variant = ""
        if c:
            variant = getattr(c, "comeback_variant", "") or (
                c.kline.dimensions.get("comeback_variant", "") if c.kline else "")
        if not variant:
            trend = (entry.get("trend") or "")
            variant = trend.split("·")[0] if "·" in trend else ""
        if variant:
            label_display += f"{ANSI['CYAN']}·{variant}{ANSI['RESET']}"
    prom_labels = c.prominence_labels if c else (["↻"] if entry.get("_prominent") else [])
    prom_raw = _render_prominence(prom_labels)
    prom_str = f" {prom_raw}" if prom_raw else ""
    # 风险标记：掉榜/重启行 DB 无 risk_flags；候选行用与 _print_row 相同的分级渲染
    risk_str = ""
    if c and c.risk_flags:
        hard, soft_count = split_risk_flags(c.risk_flags)
        if hard:
            risk_str = f" {ANSI['RED']}⚠{'/'.join(hard)}{ANSI['RESET']}"
            if soft_count:
                risk_str += f"{ANSI['YELLOW']}+{soft_count}{ANSI['RESET']}"
        elif soft_count:
            risk_str = f" {ANSI['YELLOW']}⚠+{soft_count}{ANSI['RESET']}"
    # 建议列：按类别独立映射（与展示优先级解耦），SUGGEST_BY_CAT 含 ANSI 需用 _pad 对齐
    suggest_str = SUGGEST_BY_CAT.get(cat, "")
    first_time = entry.get("first_time", entry.get("time", ""))[:5]
    # 5日累计涨幅：优先用候选池的 kline 数据，否则用 DB 落库值
    accum_val = None
    if c and c.kline:
        accum_val = c.kline.accumulated_pct
    elif entry.get("accumulated_pct") is not None:
        accum_val = entry["accumulated_pct"]
    if accum_val is None:
        accum_str = "—"
    else:
        accum_str = f"{accum_val:+.2f}%"
    extra_str = ""
    if c:
        extra_str = _market_extra_str(c)
        ff_pct = c.kline.dimensions.get("fund_flow_main_pct") if c.kline else None
    else:
        ff_pct = None
    if ff_pct is None:
        # 扫描时无资金流维度（掉榜/拉取失败）回退 DB 快照图标
        icon = _fund_flow_icon_str(flow_pct_map.get(entry["symbol"]))
        if icon:
            extra_str = f"{extra_str} {icon}".strip() if extra_str else icon
    extra_suffix = f" {extra_str}" if extra_str else ""
    nd_mark_str = f" {ANSI['GREEN']}🎯{ANSI['RESET']}" if nextday_mark else ""
    # 板块普涨避雷行尾标记已下线（2026-08-17 用户反馈「太扎眼」）：小板块共振避雷
    # 结论保留于回测（cnt<15 票 hit 5.9-6.7%/cum_3d -2.2~-2.6 最差），但黄色长文本
    # 移除，避免干扰 🎯 档0 等主信号。
    print(f"  {i:3d}  {entry['symbol']:<12} {_pad(entry['name'],10)} "
          f"{pct_colored(pct)} {accum_str:>8} {price_str:>7} {_pad(rank_str, 8, 'r')} "
          f"{_pad(_trunc(sector,14),14)} {_pad(label_display,5,'r')} {entry['score']:4d} "
          f"{_pad(first_time,6)} {_pad(suggest_str,6)}{prom_str}{risk_str}{extra_suffix}{nd_mark_str}")


def _nextday_entry_percent(entry: dict) -> float:
    """推荐时刻盘中涨幅（用于次日大涨候选区筛形）。

    回退链：候选池当前扫描快照（最新）→ live_quotes（有实时覆盖）→ DB 落库 percent。
    次日大涨画像依据的是「推荐时刻涨幅带」，故优先用扫描快照的 percent
    （与 nextday_attribution 落库 percent 同源）；实时 live_percent 可能已随盘中
    涨跌漂移，仅在无候选/无落库时兜底。
    """
    c = entry.get("_candidate")
    if c and c.stock:
        return float(c.stock.percent)
    if entry.get("live_quote_available") and entry.get("live_percent") is not None:
        return float(entry["live_percent"])
    return float(entry.get("percent", 0.0))


def _in_nextday_sweet_band(percent: float) -> bool:
    """推荐时刻涨幅是否落在次日大涨甜蜜带（<2% 或 4~8%）。

    数据（scanner.nextday_attribution，next_day≥7%）：<1% hit 11.7%、1-2% hit 13.2%、
    4-6% hit 11.8%、6-8% hit 11.8%；2-4% 死区（6.2%）、8-10% 陷阱（7.5%，平均 -1.42%）。
    """
    return (NEXTDAY_SPIKE_SWEET_MIN <= percent < NEXTDAY_SPIKE_SWEET_LOW
            or NEXTDAY_SPIKE_MID_MIN <= percent < NEXTDAY_SPIKE_MID_MAX)


def _nextday_entry_accum(entry: dict, conn=None) -> float | None:
    """推荐前 5 日累计涨幅（%），用于 🎯 判定；拿不到返回 None（不阻断）。

    口径：NEXTDAY_ACCUM_MIN=6.0 校准于「含推荐日」口径（5 日复利，含推荐日 bar）。
    - short_term 的 KlineSummary.accumulated_pct 本身含今日（策略语义）；
    - new_face/known_new_face/momentum/rebound 的 accumulated_pct 不含今日（历史口径，
      RPS/评分用），其「含今日」值由分析侧另存于 dimensions["accumulated_incl_today"]。

    回退链（与 _nextday_entry_percent 同构）：
      候选池 kline 的 accumulated_incl_today（扫描时刻，含今日 bar，最准）
      → 候选池 kline 的 accumulated_pct（short_term 含今日，可直接用）
      → daily_kline 回放（掉榜/重启行；date<=推荐日，含推荐日 bar，口径同校准）
      → DB 落库 accumulated_pct（仅回放无数据时兜底：short_term 含今日；nf/mom 为
        不含今日的历史口径，兜底值口径不符但优于 fail-open 误放行）。

    2026-08-17 修复（原口径错位）：此前候选行直接返回 c.kline.accumulated_pct，
    对 new_face/momentum/known_new_face 该值不含今日，而门槛校准于含推荐日口径——
    今日大涨票系统性被低估（漏标 🎯 / 丢档0置顶）、今日下跌票被高估（误标）。
    现候选行优先用 accumulated_incl_today 维度；掉榜行优先回放（含推荐日），
    DB 落库的历史口径值不再优先于回放。
    """
    c = entry.get("_candidate")
    if c and c.kline:
        incl = (c.kline.dimensions or {}).get("accumulated_incl_today")
        if incl is not None:
            return float(incl)
        if c.kline.accumulated_pct is not None:
            return float(c.kline.accumulated_pct)
    if conn is None:
        return None
    sym = entry.get("symbol")
    rec_date = entry.get("date")
    if not sym or not rec_date:
        return None
    try:
        rows = conn.execute(
            "SELECT close, percent FROM daily_kline WHERE symbol = ? AND date <= ? "
            "ORDER BY date DESC LIMIT 6",
            (sym, rec_date[:10]),
        ).fetchall()
    except Exception:
        rows = []
    # 2026-08-17 审查修复：回放直读 daily_kline 原始值，历史脏行（契约重构前的
    # close=NULL/字符串/0/NaN）若不经清洗会抛 TypeError/ValueError 穿透到
    # display_priority 预计算 → 本轮展示崩溃 + save_recommendations 被跳过。
    # 统一清洗：close 非有限正数 → 整行剔除；percent 脏值 → 0。
    valid: list[tuple[float, float]] = []
    for r in rows:
        try:
            close = float(r[0])
            pct = float(r[1] or 0.0)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(close) or close <= 0:
            continue
        if not math.isfinite(pct):
            pct = 0.0
        valid.append((close, pct))
    if len(valid) >= 6:
        base = valid[5][0]
        return (valid[0][0] - base) / base * 100.0
    if valid:
        return sum(p for _, p in valid[:5])
    # 回放无数据（daily_kline 缺表/该票无历史）：兜底 DB 落库值
    db_acc = entry.get("accumulated_pct")
    return float(db_acc) if db_acc is not None else None


def _entry_dims(entry: dict) -> dict:
    """统一维度访问：候选行读 kline.dimensions（最新扫描），掉榜/重启行读 DB score_breakdown。

    2026-08-17 新增（配合 get_today_recommendations 返回 score_breakdown）：
    拿不到（entry 无 _candidate），现在统一经此函数读取，候选行优先（最新数据）。
    """
    c = entry.get("_candidate")
    if c and c.kline and c.kline.dimensions:
        return c.kline.dimensions
    sb = entry.get("score_breakdown")
    return sb if isinstance(sb, dict) else {}


def _entry_weak_to_strong(entry: dict) -> bool:
    """short_term 弱转强成立：st_weak_to_strong / v_st_weak 任一 >0。

    组合信号分析（2026-08-17，去重 1224 样本）：弱转强∩非超买 hit 15.8%（全类
    基准 10.0%）——short_term 次日大涨的最强单信号。
    """
    d = _entry_dims(entry)
    return bool(d.get("st_weak_to_strong") or d.get("v_st_weak"))



def _entry_overbought(entry: dict) -> bool:
    """超买死亡信号：候选 dims / 掉榜 score_breakdown 统一判定（_entry_dims）。

    数据（nextday_attribution）：short_term/动量超买 hit 5-8%（非超买 10.5%）。
    """
    d = _entry_dims(entry)
    return bool(d.get("st_overbought_flag") or d.get("mo_overbought_flag")
                or d.get("v_st_overbought") or d.get("v_mo_overbought"))


def _entry_band(entry: dict) -> str:
    """推荐时刻涨幅带分类（与 🎯 甜蜜带同源，nextday_attribution 口径）。

    sweet(0-2%/4-8% 甜蜜带) / down(<0) / dead(2-4% 死区，next_day hit 7.0%) /
    trap(8-10%：全量 9.0% 不差但被 short_term 拉高，momentum/new_face 分类别 hit 0%，
    由 _entry_tier 仅对非 short_term 生效)。
    """
    p = _nextday_entry_percent(entry)
    if p < 0:
        return "down"
    if 0.0 <= p < NEXTDAY_SPIKE_SWEET_LOW or NEXTDAY_SPIKE_MID_MIN <= p < NEXTDAY_SPIKE_MID_MAX:
        return "sweet"
    if p < NEXTDAY_SPIKE_MID_MIN:
        return "dead"
    return "trap"


def _entry_fund_flow_pct(entry: dict) -> float | None:
    """主力净占比（%），候选行读 dims、掉榜行读 score_breakdown；无数据返回 None。"""
    v = _entry_dims(entry).get("fund_flow_main_pct")
    return float(v) if v is not None else None


def _entry_sector_resonance(entry: dict) -> bool:
    """小板块共振：v_st_sector / v_pb_sector / v_nf_sector >0 且板块规模 count<SECTOR_RESONANCE_WARN_MAX。

    回测细分（2026-08-17）：板块共振整体 next_day hit 5.6% 全场最差（无共振 13.7%），
    但按规模分档差异大——cnt<5 hit 5.3%、cnt 5-14 hit 6.3%、cnt>=15 hit 12.9%（接近
    无共振 11.9%，大板块有持续资金）——只对小板块（cnt<15，局部抱团次日兑现）档位劣后。
    count 缺失按 0（小板块，保守）。2026-08-17 行尾 ⚠板块普涨 文本下线后，本函数
    仅服务 _entry_tier 档位劣后（排序），不渲染任何文本。
    """
    d = _entry_dims(entry)
    if not (d.get("v_st_sector") or d.get("v_pb_sector") or d.get("v_nf_sector")):
        return False
    cnt = (d.get("v_st_sector_count") or d.get("v_pb_sector_count")
           or d.get("v_nf_sector_count") or 0)
    return cnt < SECTOR_RESONANCE_WARN_MAX


def _entry_tier(entry: dict, conn=None, accum: float | None = None,
                marked: bool | None = None) -> int:
    """综合排序档位（2026-08-17 二值 → 4 级；2026-08-18 统一口径为「次日大涨」）。

    档0 = 🎯 次日大涨画像（数据最强，见 _is_nextday_marked，short_term 弱转强分型）
    档1 = 强信号：rebound（next_day 口径 hit 28.6%/+2.78%，全场最强类别）
    档2 = 普通：无警示（参考）
    档3 = 警示劣后：累计≥50% 过热 / 超买（hit 6.8%）/ 小板块共振 cnt<15（hit 5.6%）/
          2-4% 死区（hit 7.0%）/ momentum、new_face 的 8-10% 陷阱（hit 0%）/ 资金流出≤-8%。
          short_term 豁免涨幅带（规律在弱转强）；comeback 统一档2——除累计≥50%
          过热外不看任何警示因子（超买/资金流/板块共振/涨幅带，2026-08-18 设计）。

    2026-08-18 口径统一：全部档位判定因子均校准于 next_day（次日大涨≥7% hit 口径，
    scanner.nextday_attribution 1184 去重样本）。comeback 由档1 移除——其 6 维回踩买点
    信号是 cum_3d 语义（回踩企稳等 3 日修复），next_day 口径下 hit 仅 3.3% 全场最差，
    不再置顶，统一回档2（独立区补充参考）。

    纯排序层：不改评分不落库，档位只重排展示顺序（跨类别全局生效）。
    """
    if accum is None:
        accum = _nextday_entry_accum(entry, conn)
    # 过热妖股优先于一切：累计≥50% 即使命中 🎯 画像也劣后（精选区校准，hit 最低区）
    if accum is not None and accum >= 50:
        return 3
    if marked is None:
        marked = _is_nextday_marked(entry, conn, accum=accum)
    if marked:
        return 0
    cat = entry["category"]
    if cat == "rebound":
        return 1
    if cat == "comeback":
        return 2
    if _entry_overbought(entry):
        return 3
    ff = _entry_fund_flow_pct(entry)
    if ff is not None and ff <= -8:
        return 3
    # 小板块共振（cnt<15）档3 劣后：板块普涨日冲进去即接盘位（next_day hit 5.6% vs 无共振 13.7%）。
    # 仅非 🎯 票生效（🎯∩板块普涨 hit 12.2% 仍有效，太辰光案例）；不渲染文本，只排序。
    if _entry_sector_resonance(entry):
        return 3
    # 涨幅带劣后（next_day 口径，全量 1184 样本）：2-4% 死区 hit 7.0%（基准 9.7%）；
    # 8-10% 全量 9.0% 不差但被 short_term 拉高——momentum（n=18）与 new_face（n=26）
    # 在 8-10% 的 next_day hit 均为 0%，kNF 仅 2 条样本；short_term 豁免（8-10% 是其
    # 最差与最好并存的双峰带，weak_to_strong 子集可用）；rebound 已提前档1 返回。
    if cat != "short_term":
        if _entry_band(entry) in ("trap", "dead"):
            return 3
    return 2


def _is_nextday_marked(entry: dict, conn=None, accum: float | None = None) -> bool:
    """次日大涨画像标记（🎯）：推荐时刻涨幅在甜蜜带 + 非超买死亡信号 + 5日累计门槛。

    2026-08-11：原「◆ 次日大涨候选」独立区与综合排序主表重合度 65%（实测当日
    主表 17 只中 11 只甜蜜带、两表排序几乎一致、辨识度因子空转），改为主表行尾
    标记，消除重复输出。筛形条件与独立区完全一致（nextday_attribution 口径）：
      1. 推荐时刻涨幅在甜蜜带（<2% 低吸潜伏 或 4~8% 中段启动）；
      2. 排除超买（short_term/动量死亡信号：hit 5% vs 非超买 10.5%）。
    2026-08-14 新增 3. 5 日累计 ≥ NEXTDAY_ACCUM_MIN（用户怕追高只选涨幅小/累计低的票，
    实测 0~3 平档 hit 仅 5.4% 全场最差、10~15 档 21.2% 最好——「累计低=安全」是反指；
    甜蜜带+累计≥6 使 hit 16.5%→20.0%）。rebound 豁免（超跌反弹，负累计天然，hit 33.3%）、
    short_term 豁免（其规律在超买/弱转强，不在此列）。累计缺失 fail-open 不阻断（见
    _nextday_entry_accum）。累计口径 = 校准口径（含推荐日 bar，_nextday_entry_accum
    优先取候选 kline 的 accumulated_incl_today 维度；2026-08-17 修复口径错位）。
    视觉标记 + 参与综合排序档位（_sort_tier 档0置顶），不改 score / 不落库。
    """
    if entry["category"] not in NEXTDAY_CAT_PRIORITY:
        return False
    cat = entry["category"]
    if cat == "short_term":
        # 2026-08-17 🎯 分型（组合信号分析，去重 1224 样本）：short_term 次日大涨规律
        # 在弱转强（弱转强∩非超买 hit 15.8%），甜蜜带对 short_term 反而负效（5.7% vs
        # 全类 8.5%）——原「甜蜜带+非超买」判定把 122 只甜蜜带 short_term 里仅 1/7 命中
        # 的侥幸票（太辰光式）顶进档0。改判定：short_term 要求弱转强 + 非超买，不再看
        # 甜蜜带。掉榜行经 score_breakdown 判定（_entry_weak_to_strong），缺数据不标。
        if not _entry_weak_to_strong(entry):
            return False
    elif not _in_nextday_sweet_band(_nextday_entry_percent(entry)):
        return False
    cat = entry["category"]
    if cat not in ("rebound", "short_term"):
        if accum is None:
            accum = _nextday_entry_accum(entry, conn)
        if accum is not None and accum < NEXTDAY_ACCUM_MIN:
            return False  # 有累计数据且不达门槛 → 不标；缺数据 fail-open 放行
    # 超买 = 次日大涨死亡信号：候选行读 dims，掉榜/重启行读 score_breakdown（统一 _entry_dims）。
    # 2026-08-17 修复：此前只查候选行，掉榜行（无 _candidate）直接放行——兆日科技
    # 案例（超买+累计74.7%妖股被误标 🎯）。掉榜行 score_breakdown 含 v_st_overbought 等字段。
    d = _entry_dims(entry)
    if (d.get("st_overbought_flag") or d.get("mo_overbought_flag")
            or d.get("v_st_overbought") or d.get("v_mo_overbought")):
        return False
    return True


def display_priority(conn=None, live_quotes: dict[str, dict] | None = None,
                     rank_map: dict[str, int] | None = None,
                     today_pool: dict[str, Candidate] | None = None,
                     last_ranks: dict[str, int] | None = None):
    """从本地数据库读取今日所有进入过推荐的票，按档位 + 展示优先级(CAT_DISPLAY_PRIORITY) + 评分键展示。

    live_quotes: {symbol: {percent, current}} 实时行情覆盖，优先于候选池和数据库数据。
    rank_map: {symbol: 飙升榜排名} 当前扫描的榜单排名，为掉榜/重启行补实时排名。
    today_pool: {symbol: Candidate} 本轮候选池快照（缺省空），供掉榜/重启行之外的行
    渲染最新候选数据（实时候选 > DB 快照）。
    last_ranks: 上一轮扫描的榜单排名 {symbol: rank}，供「排名」列显示雪球榜单排名变化
    （+N 升 / -N 降），与已下线策略桶同口径；缺省 None 不显示变化。
    排序键 = (档位, CAT_DISPLAY_PRIORITY, 分数键)：档0置前(次日大涨🎯) < 档1强信号 < 档2普通 < 档3警示劣后。
    2026-08-18 统一口径为「次日大涨」：档位因子与 CAT_DISPLAY_PRIORITY 均校准于
    next_day（≥7% hit）口径（scanner.nextday_attribution 1184 去重样本），不再混用 cum_3d。
    2026-08-11 起资金流不再参与档位排序/劣后过滤（净流出票正常展示，仅保留图标与「资金流出」标签）。
    2026-08-12 次日大涨画像(🎯)置顶；辨识度(↻)不再参与排序（次日大涨本身即辨识度属性），
    仅保留行内 ↻ 展示。档位只影响排序，不改评分列/不落库。
    """
    if conn is None:
        return

    today_recs = get_today_recommendations(conn)
    if not today_recs:
        return

    today_pool = today_pool or {}
    for entry in today_recs:
        pool_c = today_pool.get(entry["symbol"])
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
    flow_pct_map = get_fund_flow_pct_map(conn, [e["symbol"] for e in today_recs])

    # 档位置顶（2026-08-06 引入；2026-08-11 去掉资金流因子；2026-08-12 档0=辨识度∪次日大涨；
    # 2026-08-12 去掉辨识度排序）：排序键 (档位, 类别优先级, 分数键)，跨类别全局生效。
    # 档0置前 = 次日大涨画像(🎯)（推荐时刻涨幅甜蜜带 + 非超买 + 5日累计门槛，见 _is_nextday_marked）；
    # 档1 = 其余。辨识度(↻)不再参与排序——次日大涨画像本身即辨识度属性（用户决策），
    # ↻ 仅保留行内展示。资金流不参与排序/劣后过滤（净流出票正常展示，仅保留「资金流出」标签与图标提醒）。
    # 2026-08-14：预计算 mark map（排序+渲染各调一次 _is_nextday_marked 会触发两次 daily_kline
    # 回放全表扫描；预计算后只查一次，_sort_tier 与渲染共用同一结果保证一致性）。
    nextday_mark: dict[str, bool] = {}
    tier_map: dict[str, int] = {}
    for e in today_recs:
        # 2026-08-17 审查修复：累计预计算一次，消除掉榜行在排序预计算与渲染之间
        # 的重复 daily_kline 回放（N+1）。
        acc = _nextday_entry_accum(e, conn)
        marked = _is_nextday_marked(e, conn, accum=acc)
        nextday_mark[e["symbol"]] = marked
        # 2026-08-17 档位 4 级：tier_map 与 nextday_mark 同源预计算（_entry_tier 复用
        # 同一 accum/marked，避免二次回放），排序与任何行内展示共用同一结果。
        tier_map[e["symbol"]] = _entry_tier(e, conn, accum=acc, marked=marked)

    def _sort_tier(entry):
        return tier_map.get(entry["symbol"], 2)

    # 回马枪独立成区（2026-08-07 方案A）：comeback 是 off_list 掉榜跟踪票，语义与榜上票不同，
    # 从主排序表抽出放到末尾独立区块；主表只排榜上五类（rebound/known_new_face/new_face/
    # momentum/short_term，不含 comeback/pullback）。
    comeback_recs = [e for e in today_recs if e["category"] == "comeback"]
    main_recs = [e for e in today_recs if e["category"] != "comeback"]

    # 2026-08-10: known_new_face 分数反指（回测分桶：低分档[18,37) cum_3d +5.58/64%胜率，
    # 高分档[77,98) -3.76/33%）——分区内 score 升序，把"低调二次上榜"的低分票排前，
    # 避免把最差的追高票顶在最前。其余类别仍降序。
    def _score_sort_key(entry):
        if entry["category"] == "known_new_face":
            return entry["score"]
        return -entry["score"]

    scored = sorted(main_recs, key=lambda x: (_sort_tier(x),
                                               CAT_DISPLAY_PRIORITY.get(x["category"], 99),
                                               _score_sort_key(x)))

    print(f"\n{ANSI['BOLD']}◆ 综合排序 — 今日上榜推荐{ANSI['RESET']}")
    hdr = (f"  {_pad('#',3,'r')} {_pad('代码',12)} {_pad('名称',10)} "
           f"{_pad('涨幅',8,'r')} {_pad('5日累计',8,'r')} {_pad('现价',7,'r')} {_pad('排名',8,'r')} "
           f"{_pad('板块',14)} {_pad('策略',5)} {_pad('评分',4,'r')} {_pad('时间',6)} {_pad('建议',6)}")
    print(hdr)
    # 档位组标题（2026-08-17）：4 级档位切换时打印分隔行，直观看到「该买/别碰」边界。
    # 纯展示层；组内序号重新编号，与组标题配合更清晰。
    tier_names = {0: "次日大涨画像", 1: "强信号", 2: "普通", 3: "警示劣后"}
    tier_color = {0: ANSI["GREEN"], 1: ANSI["CYAN"], 2: "", 3: ANSI["RED"]}
    last_tier = None
    rank_in_tier = 0
    for entry in scored:
        tier = tier_map.get(entry["symbol"], 2)
        if tier != last_tier:
            rank_in_tier = 0
            last_tier = tier
            prefix = "🎯 " if tier == 0 else ""
            print(f"  {tier_color[tier]}── 档{tier} {prefix}{tier_names[tier]}{ANSI['RESET']}")
        rank_in_tier += 1
        mark = nextday_mark.get(entry["symbol"], False)
        _print_priority_row(entry, rank_in_tier, flow_pct_map, nextday_mark=mark, last_ranks=last_ranks)
    print(f"  {'-'*92}")

    # 回马枪独立成区（2026-08-11 移到最末尾）：主表仅排榜上五类，comeback 抽到此处独立成区。
    # 2026-08-12 放宽兜底条件：主区推荐条数 < COMEBACK_DISPLAY_MIN_MAIN（含为空）时也显示，
    # 解决主区稀少（如盘中仅 1-2 条）时回马枪大量条目被整体隐藏的盲区；主区 ≥ 阈值仍不
    # 显示（避免刷屏）。仅显示前 COMEBACK_DISPLAY_MAX 条。comeback 为空同样跳过。
    if comeback_recs and len(main_recs) < COMEBACK_DISPLAY_MIN_MAIN:
        cb_scored = sorted(comeback_recs, key=lambda x: (_sort_tier(x), -x["score"]))
        if len(cb_scored) > COMEBACK_DISPLAY_MAX:
            cb_scored = cb_scored[:COMEBACK_DISPLAY_MAX]
        print(f"\n{ANSI['CYAN']}◆ 回马枪 — 掉榜跟踪/回调买点（主区推荐较少·补充参考）{ANSI['RESET']}")
        print(hdr)
        for ci, entry in enumerate(cb_scored, 1):
            _print_priority_row(entry, ci, flow_pct_map, last_ranks=last_ranks)
        print(f"  {'-'*92}")
