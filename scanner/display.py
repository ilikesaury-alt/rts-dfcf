import os
import re

import wcwidth

from scanner.config import (
    COMEBACK_DISPLAY_MAX,
    COMEBACK_DISPLAY_MIN_MAIN,
    CORE_DIP_CATEGORY,
    FUND_FLOW_MAIN_PCT_EXTREME,
    FUND_FLOW_MAIN_PCT_STRONG,
    FUND_FLOW_MAIN_PCT_WEAK,
    RISK_FLAGS_DISPLAY_HARD,
    SUGGEST_BY_CAT,
    TOP40_THRESHOLD,
    now_beijing,
)
from scanner.core_themes import _low_buy_quality as _core_dip_quality
from scanner.core_themes import core_stock_symbols
from scanner.database import (
    get_fund_flow_pct_map,
    get_today_recommendations,
)
from scanner.models import Candidate

# 纯排序逻辑已迁至 scanner.ranking（单源）；此处全量 re-export 供内部调用与
# scripts/review_tier_replay.py 的 display._entry_* 属性访问保持兼容。
# F401 为有意导出（test_ranking_single_source.py 断言 display.X is ranking.X）。
from scanner.ranking import (  # noqa: F401
    _entry_band,
    _entry_dims,
    _entry_fund_flow_pct,
    _entry_overbought,
    _entry_sector_resonance,
    _entry_tier,
    _entry_weak_to_strong,
    _in_nextday_sweet_band,
    _is_breakout_setup,
    _is_nextday_marked,
    _is_relist_breakout_setup,
    _nextday_entry_accum,
    _nextday_entry_percent,
    build_accum_map,
    build_breakout_kline_map,
    sort_main_entries,
)
from scanner.sector import classify_sector
from scanner.utils import clear_screen, to_float, to_int

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
        "CYAN": "\033[96m", "MAGENTA": "\033[95m", "BOLD": "\033[1m",
        "RESET": "\033[0m",
    }
else:
    ANSI = {"RED": "", "YELLOW": "", "GREEN": "", "CYAN": "",
            "MAGENTA": "", "BOLD": "", "RESET": ""}

# 类别展示标签/颜色：综合排序与回马枪独立区共用（提出模块级供 _print_priority_row 复用）。
# 2026-08-20 收敛：CAT_LABEL / 颜色键统一来自 scanner/categories 注册表（单一事实来源），
# 颜色键经本模块 ANSI 字典解析为色码，避免与 config 循环依赖。
from scanner.categories import CAT_LABEL, CATEGORY_COLOR_KEYS  # noqa: E402

CAT_COLOR = {name: ANSI[key] for name, key in CATEGORY_COLOR_KEYS.items()}


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
    """按可见宽度截断（中文全角按 2 列计），超长时尾部补 …。

    2026-08-20 修复：ANSI 感知——转义序列按 0 列计并原样透传（不逐字符复制导致在
    序列中间切断、丢失 \x1b[0m 使终端后续行残留颜色）；被截掉的尾部若含 RESET，
    末尾补一个 RESET 兜底。
    """
    if _vis_len(s) <= width:
        return s
    # 按索引推进（2026-08-21 审查修复）：旧实现对每个字符迭代、命中转义序列后仅
    # continue 一个字符——序列体内的 [ 9 1 m 等会在后续迭代被再次当可见文本追加
    # （输出出现字面 "[91m"），且 s[len(out):] 偏移随 len(out) 失真导致后续匹配
    # 错位。现用索引 i 推进，整段序列一次性消费（i=m.end()），不再重扫序列体。
    out = ""
    vis = 0
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if ch == "\x1b":
            m = _ANSI_ESCAPE.match(s, i)
            if m:
                out += m.group(0)
                i = m.end()
                continue
        w = max(0, wcwidth.wcwidth(ch))
        if vis + w > width - 1:
            break
        vis += w
        out += ch
        i += 1
    if "\x1b[" in out and "\x1b[0m" not in out:
        out += "\x1b[0m"
    return out + "…"



def pct_colored(pct: float | None, width: int = 8) -> str:
    pct = to_float(pct, default=0.0)
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


_FUND_FLOW_ICON = {
    "strong_in": f"{ANSI['GREEN']}▲▲{ANSI['RESET']}",
    "in": f"{ANSI['GREEN']}▲{ANSI['RESET']}",
    "out": f"{ANSI['RED']}▼{ANSI['RESET']}",
    "strong_out": f"{ANSI['RED']}▼▼{ANSI['RESET']}",
}


def _fund_flow_icon_str(ff_pct) -> str:
    """主力净占比 → 流向图标（ANSI 着色）；无数据或中性返回空串。

    2026-08-22 标记精简（用户反馈行尾杂乱）：中性档 ◇ 不再显示——(-5%,+5%)
    覆盖大多数票且零信息，只在流向有意义（≥+5% 流入 / ≤-5% 流出）时显示。
    fund_flow_signal 本身不动（feishu/bonus 逻辑仍用五档）。
    """
    ff_pct = to_float(ff_pct, default=None)
    if ff_pct is None:
        return ""
    sig = fund_flow_signal(ff_pct)
    if sig == "neutral":
        return ""
    return _FUND_FLOW_ICON.get(sig, "")


def _market_extra_str(c: Candidate) -> str:
    """行情增强标记：主力资金流流向图标 + 连板/炸板（无数据返回空串）。

    资金流用 fund_flow_signal 映射图标（中性不显示，见 _fund_flow_icon_str）；
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


def _core_dip_extra_str(run, pullback, flow_pct) -> str:
    """核心低吸行尾绿色后缀：20日累计 / 回撤 / 主力净占比（2026-08-20）。

    2026-08-20 加固：run/pullback/flow_pct 来自 DB score_breakdown JSON，脏库
    可能为字符串/NaN，to_float 统一防御（此前 float() 直接抛 ValueError 中断整屏渲染）。
    """
    run_f = to_float(run, default=0.0)
    pullback_f = to_float(pullback, default=0.0)
    parts = [f"20日{run_f * 100:+.1f}%", f"回撤{pullback_f * 100:+.1f}%"]
    flow_f = to_float(flow_pct, default=None)
    if flow_f is not None:
        parts.append(f"主力{flow_f:+.1f}%")
    return f"{ANSI['GREEN']}{' '.join(parts)}{ANSI['RESET']}"


def _core_dip_entry_quality(entry: dict) -> tuple:
    """推荐记录条目 → 低吸质量排序键（复用 core_themes._low_buy_quality）。

    entry 是完整 recommendation 行（含 score_breakdown 的 run/pullback/today_pct/
    flow_pct），先经 _entry_dims 抽取为低吸质量函数所需字典再排序。
    """
    sb = _entry_dims(entry)
    return _core_dip_quality({
        "flow_pct": to_float(sb.get("flow_pct"), default=None),
        "today_pct": to_float(sb.get("today_pct"), default=0.0),
        "run": to_float(sb.get("run"), default=0.0),
        "pullback": to_float(sb.get("pullback"), default=0.0),
    })




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
                        breakout_mark: bool = False,
                        last_ranks: dict[str, int] | None = None) -> None:
    """综合排序单行的统一渲染（主表与回马枪独立区共用），避免两处复制大段渲染逻辑。

    flow_pct_map: {symbol: 主力净占比} DB 快照回退（候选缺失/扫描失败时仍显示资金流图标）。
    nextday_mark: 次日大涨画像（🎯）——推荐时刻涨幅甜蜜带 + 非超买（见 _is_nextday_marked）。
    breakout_mark: 蓄势突破观察画像（⚡）——新面孔/首推或重上榜 short_term + 横盘缩量回调位
    （见 _is_breakout_setup / _is_relist_breakout_setup；2026-08-22 渲染合并为单一 ⚡，
    变体区分保留在判定函数供样本统计）。纯观察标记，不参与排序/评分/落库。
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
    # 辨识度（↻）行内标记已下线（2026-08-22 标记精简）：回测证独立增量≈0、已退出排序，
    # 纯装饰性噪音；prominence 数据仍在 today_report 归因中使用，不受影响。
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
    bo_mark_str = f" {ANSI['CYAN']}⚡{ANSI['RESET']}" if breakout_mark else ""
    # 核心低吸行尾绿色后缀（2026-08-20）：20日累计/回撤/主力净占比。仅核心低吸区
    # 条目带 _core_dip_extra，主表/回马枪行不受影响（复用标准 _print_priority_row）。
    core_dip_extra = entry.get("_core_dip_extra")
    if core_dip_extra:
        extra_suffix = f"{extra_suffix} {core_dip_extra}".strip() if extra_suffix else f" {core_dip_extra}"
    # 板块普涨避雷行尾标记已下线（2026-08-17 用户反馈「太扎眼」）：小板块共振避雷
    # 结论保留于回测（cnt<15 票 hit 5.9-6.7%/cum_3d -2.2~-2.6 最差），但黄色长文本
    # 移除，避免干扰 🎯 档0 等主信号。
    # 核心股高亮（2026-08-19）：该票今日在核心方向低吸区（category=core_dip）→ 判定
    # 为核心股，名称加粗品红高亮（判定在 display_priority 预计算 _core_stock，主表与
    # 回马枪区共用本函数同规则）。纯展示层不改评分不落库。
    name_str = entry["name"]
    if entry.get("_core_stock"):
        name_str = f"{ANSI['BOLD']}{ANSI['MAGENTA']}{name_str}{ANSI['RESET']}"
    print(f"  {i:3d}  {entry['symbol']:<12} {_pad(name_str,10)} "
          f"{pct_colored(pct)} {accum_str:>8} {price_str:>7} {_pad(rank_str, 8, 'r')} "
          f"{_pad(_trunc(sector,14),14)} {_pad(label_display,5,'r')} {to_int(entry['score']):4d} "
          f"{_pad(first_time,6)} {_pad(suggest_str,6)}{risk_str}{extra_suffix}{nd_mark_str}{bo_mark_str}")


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

    # 辨识度（↻）行内标记已下线（2026-08-22 标记精简）：prom_map/_prominent 预计算链路
    # 随之移除；get_prominence_map 仍被 today_report 归因使用，不受影响。

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
    # P1-9（2026-08-20）：全部推荐一次性批量回放累计（build_accum_map 单查询），
    # 替代逐行 _nextday_entry_accum 的 N+1 daily_kline 查询。
    accum_map = build_accum_map(conn, today_recs)
    for e in today_recs:
        sym = e["symbol"]
        marked = _is_nextday_marked(e, conn, accum_map=accum_map)
        nextday_mark[sym] = marked
        # 2026-08-17 档位 4 级：tier_map 与 nextday_mark 同源预计算（_entry_tier 复用
        # 同一 accum/marked，避免二次回放），排序与任何行内展示共用同一结果。
        tier_map[sym] = _entry_tier(e, conn, accum_map=accum_map, marked=marked)

    # 回马枪独立成区（2026-08-07 方案A）：comeback 是 off_list 掉榜跟踪票，语义与榜上票不同，
    # 从主排序表抽出放到末尾独立区块；主表只排榜上五类（rebound/known_new_face/new_face/
    # momentum/short_term，不含 comeback/pullback）。
    comeback_recs = [e for e in today_recs if e["category"] == "comeback"]
    core_dip_recs = [e for e in today_recs if e["category"] == CORE_DIP_CATEGORY]
    main_recs = [e for e in today_recs if e["category"] not in ("comeback", CORE_DIP_CATEGORY)]

    # 核心股高亮（2026-08-19）：综合排序/回马枪列表里属于当前主线方向核心股的票，
    # 名称加粗高亮。**判定 = core_stock_symbols（核心主题成员 + 20日累计≥CORE_RUN_MIN
    # 走强龙头），不用 core_dip 列表**——低吸区只含「回调中的核心股」，会漏掉创新高走强
    # 中的主线龙头（2026-08-19 江天化学案例：央国企改革成员、20日+22.3%，回撤0%落不进
    # 低吸窗口）；core_dip 候选必然同时满足「主题成员+走强」，故本集合是低吸区的严格超集，
    # 低吸区里的票全部仍会高亮。单次 DB-only 推导 ~0.1s，纯展示层不改评分不落库。
    core_syms = core_stock_symbols(conn)
    for e in today_recs:
        e["_core_stock"] = e["symbol"] in core_syms

    # 2026-08-10: known_new_face 分数反指（回测分桶：低分档[18,37) cum_3d +5.58/64%胜率，
    # 高分档[77,98) -3.76/33%）——分区内 score 升序，把"低调二次上榜"的低分票排前，
    # 避免把最差的追高票顶在最前。其余类别仍降序。
    # 2026-08-20 收敛：排序组合层（档位+类别优先级+分数键含 kNF 升序）统一走 ranking.sort_main_entries，
    # 与 today_report 同源，消除两处排序分化。
    scored = sort_main_entries(main_recs, tier_map)

    # 蓄势突破观察标记（2026-08-21，⚡）：新面孔/首推或重上榜 short_term + 横盘缩量回调位
    # + MA 多头。纯展示层观察——不改排序/评分/落库（用户决策：先观察积累样本，达标后再评估
    # 是否升级为排序因子）。仅主表五类参与判定；批量取 K 线防 N+1。
    breakout_kmap = build_breakout_kline_map(conn, main_recs)
    # 2026-08-22 标记精简：两个变体判定保留（样本统计需区分），渲染合并为单一 ⚡。
    breakout_mark: dict[str, bool] = {
        e["symbol"]:
            _is_breakout_setup(e, conn, accum_map=accum_map,
                               klines=breakout_kmap.get(e["symbol"]))
            or _is_relist_breakout_setup(e, conn, accum_map=accum_map,
                                         klines=breakout_kmap.get(e["symbol"]))
        for e in main_recs
    }

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
        _print_priority_row(entry, rank_in_tier, flow_pct_map, nextday_mark=mark,
                            breakout_mark=breakout_mark.get(entry["symbol"], False),
                            last_ranks=last_ranks)
    print(f"  {'-'*92}")
    if any(breakout_mark.values()):
        print(f"  {ANSI['CYAN']}⚡ 蓄势突破观察{ANSI['RESET']}（缩量回调蓄势位·含新面孔/重上榜两变体"
              f"·样本收集中·非排序因子）")

    # 回马枪独立成区（2026-08-11 移到最末尾）：主表仅排榜上五类，comeback 抽到此处独立成区。
    # 2026-08-12 放宽兜底条件：主区推荐条数 ≤ COMEBACK_DISPLAY_MIN_MAIN（含为空）时也显示，
    # 解决主区稀少（如盘中仅 1-2 条）时回马枪大量条目被整体隐藏的盲区；主区数量大于阈值
    # 才隐藏（避免刷屏）。仅显示前 COMEBACK_DISPLAY_MAX 条。comeback 为空同样跳过。
    # 2026-08-20：综合排序数量大于 3（COMEBACK_DISPLAY_MIN_MAIN）才隐藏回马枪，等于 3 仍显示。
    # 2026-08-21：撤销弱市门控——唯一显示条件 = 主区（榜上五类）推荐条数 ≤ COMEBACK_DISPLAY_MIN_MAIN
    # （含为空）。无论大盘强弱，只要主表推荐稀少即补充展示回马枪/核心低吸（避免主表稀缺时盲区）。
    _main_sparse = len(main_recs) <= COMEBACK_DISPLAY_MIN_MAIN
    if comeback_recs and _main_sparse:
        cb_scored = sorted(comeback_recs, key=lambda x: (tier_map.get(x["symbol"], 2), -x["score"]))
        if len(cb_scored) > COMEBACK_DISPLAY_MAX:
            cb_scored = cb_scored[:COMEBACK_DISPLAY_MAX]
        print(f"\n{ANSI['CYAN']}◆ 回马枪 — 掉榜跟踪/回调买点"
              f"（主区推荐≤{COMEBACK_DISPLAY_MIN_MAIN}·补充参考）{ANSI['RESET']}")
        print(hdr)
        for ci, entry in enumerate(cb_scored, 1):
            _print_priority_row(entry, ci, flow_pct_map, last_ranks=last_ranks)
        print(f"  {'-'*92}")

    # 核心方向低吸独立区（2026-08-19，`scanner/core_themes.py`）：大跌市中找「当前主线
    # 方向核心股低吸」参考。2026-08-19 起随扫描落库 category=core_dip（同 comeback 族），
    # 本区改从今日 recommendations 读取（与回马枪区同源），不再 display 内自行计算——
    # 落库后同样进 nextday_attribution/prevday_perf 复盘验证「主线回调低吸」假设。
    # 2026-08-20：展示改与回马枪同款——复用综合排序标准表头 hdr + _print_priority_row
    # （代码/名称/涨幅/5日累计/现价/排名/板块/策略/评分/时间/建议），低吸专属数据
    # （20日累计/回撤/主力）追加到行尾 extra 后缀（ANSI 绿色），不再用自定义表头。
    core_dips: list[dict] = []
    for e in core_dip_recs:
        sb = _entry_dims(e)
        run = sb.get("run")
        pullback = sb.get("pullback")
        if run is None or pullback is None:
            continue
        e["_core_dip_extra"] = _core_dip_extra_str(
            to_float(run), to_float(pullback), sb.get("flow_pct"))
        core_dips.append(e)
    core_dips.sort(key=_core_dip_entry_quality)
    # 2026-08-19: 核心方向低吸区显示逻辑与回马枪同规则——主区（榜上五类）推荐条数 ≥
    # COMEBACK_DISPLAY_MIN_MAIN 时默认不渲染（避免与主区重复刷屏）；主区推荐条数 < 阈值
    # （含为空）时补充展示，最多前 COMEBACK_DISPLAY_MAX 条。core_dip 为空同样跳过。
    # 2026-08-21：与回马枪同款，撤销弱市门控，唯一显示条件 = 主区推荐 ≤ COMEBACK_DISPLAY_MIN_MAIN（复用 _main_sparse）。
    if core_dips and _main_sparse:
        if len(core_dips) > COMEBACK_DISPLAY_MAX:
            core_dips = core_dips[:COMEBACK_DISPLAY_MAX]
        print(f"\n{ANSI['GREEN']}◆ 核心方向低吸 — 主线方向核心股回调参考"
              f"（主区推荐≤{COMEBACK_DISPLAY_MIN_MAIN}·补充参考）{ANSI['RESET']}")
        print(hdr)
        for di, entry in enumerate(core_dips, 1):
            _print_priority_row(entry, di, flow_pct_map, last_ranks=last_ranks)
        print("  行尾绿色：20日累计/回撤（距20日高点，负值越深越低吸位）/主力净占比；")
        print("  排序=低吸质量（主力回流→回撤深→龙头强→今日企稳）。")
        print(f"  {'-'*92}")
