import os
import re
import statistics
from dataclasses import dataclass

import wcwidth

from scanner.config import (
    COMEBACK_DISPLAY_MAX,
    COMEBACK_DISPLAY_MIN_MAIN,
    CORE_DIP_CATEGORY,
    CORE_PULLBACK_MAX,
    CORE_PULLBACK_MIN,
    FUND_FLOW_MAIN_PCT_EXTREME,
    FUND_FLOW_MAIN_PCT_STRONG,
    FUND_FLOW_MAIN_PCT_WEAK,
    NEXTDAY_RULE_ATRPCT_MIN,
    NEXTDAY_RULE_MA5R_MIN,
    NEXTDAY_RULE_RET20_MAX,
    RISK_FLAGS_DISPLAY_HARD,
    TOP40_THRESHOLD,
    now_beijing,
)
from scanner.core_themes import _low_buy_quality as _core_dip_quality
from scanner.core_themes import core_stock_symbols
from scanner.database import (
    get_fund_flow_pct_map,
    get_today_recommendations,
)
from scanner.models import Candidate, RecommendationRow
from scanner.nextday_rule import RuleResult, scan_rule

# 排序/画像纯逻辑单源在 scanner.ranking；display 只导入渲染所需子集。
# 此前的全量 re-export（供 scripts 的 display._entry_* 属性访问）已下线：
# 消费方直接 import scanner.ranking（scripts/review_tier_replay.py 已改）。
from scanner.ranking import (
    _breakout_profile_key,
    _breakout_structure_ok,
    _entry_dims,
    _fresh_candidate,
    _is_nextday_marked,
    build_accum_map,
    build_breakout_kline_map,
    comeback_sort_key,
)
from scanner.sector import classify_sector
from scanner.utils import EXTERNAL_FAILURES, clear_screen, to_float, to_int

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
        "RED": "\033[91m",
        "YELLOW": "\033[93m",
        "GREEN": "\033[92m",
        "CYAN": "\033[96m",
        "MAGENTA": "\033[95m",
        "BOLD": "\033[1m",
        "RESET": "\033[0m",
    }
else:
    ANSI = {"RED": "", "YELLOW": "", "GREEN": "", "CYAN": "", "MAGENTA": "", "BOLD": "", "RESET": ""}

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
    f = to_float(pct) or 0.0
    s = f"{f:+.2f}%"
    if f >= 9:
        c = ANSI["RED"]
    elif f >= 5:
        c = ANSI["GREEN"]
    elif f < 0:
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


def _entry_display_quote(entry: RecommendationRow | dict) -> tuple[float, float]:
    """涨幅/现价统一回退链（单源）：实时行情 → 可信候选快照 → DB 落库。

    - live_quote_available 时 live_percent=0.0 是合法 0.00%，不得被 `or` 吞成 DB 值；
    - stale 掉榜候选 / 双挂票类别错位候选经 _fresh_candidate 视同无候选，直接落 DB；
    - live_current 缺失时用候选快照现价兜底（保持原 _print_priority_row 行为）。

    优选池行、回马枪/低吸区行、涨幅升序排序键共用——杜绝同一票两区涨幅口径漂移。
    返回 (pct, current)。
    """
    c = _fresh_candidate(entry)
    if entry.get("live_quote_available"):
        pct = to_float(entry.get("live_percent"), default=0.0)
        cur = to_float(entry.get("live_current"), default=0.0)
    elif c and c.stock:
        pct = to_float(c.stock.percent, default=0.0)
        cur = to_float(c.stock.current, default=0.0)
    else:
        _lp = entry.get("live_percent")
        pct = to_float(_lp, default=0.0) if _lp is not None else to_float(entry.get("percent"), default=0.0)
        cur = 0.0
    if not cur and c and c.stock.current:
        cur = to_float(c.stock.current, default=0.0)
    return pct, cur


def _entry_row_suffix(
    entry: RecommendationRow | dict,
    flow_pct_map: dict[str, float],
    marked: bool = False,
    breakout_marked: bool = False,
) -> str:
    """行尾可变区统一渲染：风险标记 → 资金流/连板 extra → 🎯 → ⚡。

    优选池行与回马枪/核心低吸区行共用（2026-08-30 收口）——此前仅补充区渲染这些
    标记，主视图优选池行丢失 🎯/⚡/资金流信息。顺序与原 _print_priority_row 一致。
    """
    c = _fresh_candidate(entry)
    parts: list[str] = []
    if c and c.risk_flags:
        hard, soft_count = split_risk_flags(c.risk_flags)
        if hard:
            seg = f" {ANSI['RED']}⚠{'/'.join(hard)}{ANSI['RESET']}"
            if soft_count:
                seg += f"{ANSI['YELLOW']}+{soft_count}{ANSI['RESET']}"
            parts.append(seg)
        elif soft_count:
            parts.append(f" {ANSI['YELLOW']}⚠+{soft_count}{ANSI['RESET']}")
    extra = _market_extra_str(c) if c else ""
    ff_pct = c.kline.dimensions.get("fund_flow_main_pct") if c and c.kline else None
    if ff_pct is None:
        # 扫描时无资金流维度（掉榜/拉取失败）回退 DB 快照图标
        icon = _fund_flow_icon_str(flow_pct_map.get(entry["symbol"]))
        if icon:
            extra = f"{extra} {icon}".strip() if extra else icon
    if extra:
        parts.append(f" {extra}")
    if marked:
        parts.append(f" {ANSI['GREEN']}🎯{ANSI['RESET']}")
    if breakout_marked:
        parts.append(f" {ANSI['CYAN']}⚡{ANSI['RESET']}")
    return "".join(parts)


def _market_env_tag(weak: bool) -> str:
    """大盘环境标签（与动态推荐 / 飞书 env_tag 同源，统一走 _regime_weak 判定）。

    2026-08-30 收敛：此前头部读候选池 dims 的 market_env_bonus、与动态推荐/飞书用的
    _regime_weak 是两个独立信号，可能同屏矛盾（头部「强势」却按弱市剔除动量）。现统一
    为同一 weak 布尔——header 与动态推荐/飞书三者口径一致。
    """
    if weak:
        return f"{ANSI['RED']}[大盘弱势·谨慎]{ANSI['RESET']}"
    return f"{ANSI['GREEN']}[大盘强势]{ANSI['RESET']}"


def _core_dip_entry_quality(entry: RecommendationRow | dict) -> tuple:
    """推荐记录条目 → 低吸质量排序键（复用 core_themes._low_buy_quality）。

    entry 是完整 recommendation 行（含 score_breakdown 的 run/pullback/today_pct/
    flow_pct），先经 _entry_dims 抽取为低吸质量函数所需字典再排序。
    """
    sb = _entry_dims(entry)
    return _core_dip_quality(
        {
            "flow_pct": to_float(sb.get("flow_pct"), default=None),
            "today_pct": to_float(sb.get("today_pct"), default=0.0),
            "run": to_float(sb.get("run"), default=0.0),
            "pullback": to_float(sb.get("pullback"), default=0.0),
        }
    )


def display(
    gem_total: int,
    interval: int,
    filtered_large_cap: int = 0,
    conn=None,
    live_quotes: dict[str, dict] | None = None,
    rank_map: dict[str, int] | None = None,
    today_pool: dict[str, Candidate] | None = None,
    last_ranks: dict[str, int] | None = None,
) -> "ScanView | None":
    """扫描主屏：头部摘要 + 展示视图（构建/渲染委托 display_priority）。

    ScanView 定义在本文件更下方（视图模型区），此处为前向引用故写成字符串注解；
    render_terminal / build_scan_view 均在类定义之后，无需引号。

    返回本轮 ScanView 供飞书复用（同一份选择，避免两端分叉与重复计算）；
    conn 为空或今日无推荐时返回 None。

    today_pool：本轮候选池快照（symbol → Candidate），由调用方（scan_with_raw 的
    ScanResult）传入，display 不直接访问 orchestrator 内部状态。
    last_ranks: 上一轮扫描的榜单排名 {symbol: rank}，供「排名」列显示变化（+N 升 / -N 降）。
    """
    clear_screen()
    now = now_beijing().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{'=' * 96}")
    print(f"  创业板飙升榜监控  ({now})")
    filter_info = f" | 过滤{filtered_large_cap}只" if filtered_large_cap else ""
    # 统一市况信号：与动态推荐 / 飞书 env_tag 同源（_regime_weak），避免同屏矛盾。
    weak = _regime_weak(conn) if conn is not None else False
    print(f"  创业板共 {gem_total} 只{filter_info} | 每{interval}s刷新 | {_market_env_tag(weak)}")
    print(f"{'=' * 96}")
    return display_priority(
        conn=conn,
        live_quotes=live_quotes,
        rank_map=rank_map,
        today_pool=today_pool,
        last_ranks=last_ranks,
        weak=weak,
    )


def _print_priority_row(
    entry: RecommendationRow | dict,
    i: int,
    flow_pct_map: dict,
    nextday_mark: bool = False,
    breakout_mark: bool = False,
    last_ranks: dict[str, int] | None = None,
) -> None:
    """综合排序单行的统一渲染（主表与回马枪独立区共用），避免两处复制大段渲染逻辑。

    flow_pct_map: {symbol: 主力净占比} DB 快照回退（候选缺失/扫描失败时仍显示资金流图标）。
    nextday_mark: 次日大涨画像（🎯）——推荐时刻涨幅甜蜜带 + 非超买（见 _is_nextday_marked）。
    breakout_mark: 蓄势突破观察画像（⚡）——新面孔/首推或重上榜 short_term + 横盘缩量回调位
    （见 _is_breakout_setup / _is_relist_breakout_setup；2026-08-22 渲染合并为单一 ⚡，
    变体区分保留在判定函数供样本统计）。纯观察标记，不参与排序/评分/落库。
    视觉标记，不参与排序/评分/落库；行尾标记统一走 _entry_row_suffix（与优选池行同口径）。
    last_ranks: 上一轮扫描的榜单排名 {symbol: rank}，用于「排名」列展示雪球榜单排名变化
    （+N 升 / -N 降），与已下线策略桶的 _rank_delta_str 同口径；缺省 None 不显示变化。
    """
    c = entry.get("_candidate")
    sector = classify_sector(entry["name"])
    # 标签/优先级列统一用 entry["category"]（与排序口径一致），
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
    # 候选可信性走 _fresh_candidate 单源助手（2026-08-24 第二轮审查收口）：stale
    # 掉榜候选的冻结快照（仙乐健康案例：掉榜后仍显示上榜时的 rank 15）与双挂票
    # 类别错位候选都视同无候选，落 DB 回退链。
    _fresh_c = _fresh_candidate(entry)
    # 涨幅/现价走 _entry_display_quote 单源回退链（与优选池行/涨幅升序排序键同口径）。
    pct, live_cur = _entry_display_quote(entry)
    live_rank = entry.get("live_rank")
    if not live_rank and _fresh_c and _fresh_c.stock.rank:
        live_rank = _fresh_c.stock.rank
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
                c.kline.dimensions.get("comeback_variant", "") if c.kline else ""
            )
        if not variant:
            trend = entry.get("trend") or ""
            variant = trend.split("·")[0] if "·" in trend else ""
        if variant:
            label_display += f"{ANSI['CYAN']}·{variant}{ANSI['RESET']}"
    # 辨识度（↻）行内标记已下线（2026-08-22 标记精简）：回测证独立增量≈0、已退出排序，
    # 纯装饰性噪音；prominence 数据仍在 today_report 归因中使用，不受影响。
    first_time = str(entry.get("first_time") or entry.get("time") or "")[:5]
    # 5日累计涨幅：优先用候选池可信快照（_fresh_candidate），否则用 DB 落库值
    accum_val = _fresh_c.kline.accumulated_pct if _fresh_c and _fresh_c.kline else entry.get("accumulated_pct")
    accum_str = "—" if accum_val is None else f"{accum_val:+.2f}%"
    # 行尾标记（风险/资金流/连板/🎯/⚡）走 _entry_row_suffix 单源，与优选池行同口径。
    tail = _entry_row_suffix(entry, flow_pct_map, marked=nextday_mark, breakout_marked=breakout_mark)
    # 板块普涨避雷行尾标记已下线（2026-08-17 用户反馈「太扎眼」）：小板块共振避雷
    # 结论保留于回测（cnt<15 票 hit 5.9-6.7%/cum_3d -2.2~-2.6 最差），但黄色长文本
    # 移除，避免干扰 🎯 档0 等主信号。
    # 核心股高亮（2026-08-19）：该票今日在核心方向低吸区（category=core_dip）→ 判定
    # 为核心股，名称加粗品红高亮（判定在 display_priority 预计算 _core_stock，主表与
    # 回马枪区共用本函数同规则）。纯展示层不改评分不落库。
    name_str = entry["name"]
    if entry.get("_core_stock"):
        name_str = f"{ANSI['BOLD']}{ANSI['MAGENTA']}{name_str}{ANSI['RESET']}"
    # 列宽/对齐统一走 COLS_DETAIL（与 _table_header 同源）。此前表头用 1 个空格
    # 分隔序号列、数据行用 2 个，导致「代码」列起整体右偏 1 列——现由同一 spec 推导。
    print(
        _table_row(
            [
                str(i),
                entry["symbol"],
                name_str,
                pct_colored(pct),
                accum_str,
                price_str,
                rank_str,
                _trunc(sector, COLS_DETAIL[7][1]),
                label_display,
                to_int(entry["score"]),
                first_time,
            ],
            COLS_DETAIL,
        )
        + tail
    )


def _regime_weak(conn, lookback=10):
    """近端主表档(非 comeback/core_dip)次日表现均值 < 0 → 弱市(regime 退潮)。
    纯展示层用：驱动「动态推荐」区域是否在弱市下剔除动量/kNF 类🎯。
    fail-open：无数据/查询异常时返回 False(按强市处理，不误删推荐)。

    注意：OFFSET 必须作用于 DISTINCT date，否则多个推荐行共享同一 date 会使
    OFFSET 始终落在最近一日块内、date>cutoff 恒为空而 fail-open 误判为强市。
    采用逐交易日均值(每个交易日等权)，避免单日高推荐量主导符号。
    """
    try:
        row = conn.execute(
            "SELECT DISTINCT date FROM recommendations ORDER BY date DESC LIMIT 1 OFFSET ?",
            (lookback - 1,),
        ).fetchone()
        if not row:
            return False
        rows = conn.execute(
            "SELECT date, next_day_pct FROM recommendations WHERE date > ? "
            "AND category NOT IN ('comeback','core_dip') AND next_day_pct IS NOT NULL",
            (row[0],),
        ).fetchall()
        by_date: dict = {}
        for d, p in rows:
            by_date.setdefault(d, []).append(p)
        daily_means = [statistics.mean(v) for v in by_date.values()]
        if not daily_means:
            return False
        return statistics.mean(daily_means) < 0.0
    except EXTERNAL_FAILURES:
        # 2026-08-29：原为裸 except Exception——DB/数据类异常与编程错误一律吞成
        # 「强市」，fail-open 语义因此不可信（且掩盖真实故障）。无数据/样本不足的
        # fail-open 由上方 `if not row` / `if not daily_means` 显式分支承担，不靠捕获。
        # sqlite3.Error 属 EXTERNAL_FAILURES；编程错误冒泡到主循环记录 traceback。
        return False


def _adjusted_picks(today_recs, nextday_mark, conn, flow_pct_map, top_n=10, weak=None):
    """regime 自适应推荐序列（纯展示，不改落库/评分/排序）。

    弱市：剔除 momentum/known_new_face/new_face 全类（🎯 与否均剔），按系统自有质量信号精选
    核心低吸(_core_dip_entry_quality) / 回马枪(comeback_sort_key) / 🎯(rebound·弱转强) 各桶
    最前少数几只——而非整个桶堆叠；强市：🎯 前置、其余按评分。返回 (名称, 类别, 是否🎯) 列表。

    weak: 弱市标志。None 时内部自算 _regime_weak(conn)；调用方（display_priority）
    预计算后传入可省掉同轮第二次 SQL（此前每轮跑两遍同一查询）。
    """
    if weak is None:
        weak = _regime_weak(conn)
    core_dip, comeback, yt, other = [], [], [], []
    for e in today_recs:
        sym, cat = e["symbol"], e["category"]
        marked = nextday_mark.get((sym, cat), False)
        if cat == "core_dip":
            sb = _entry_dims(e)
            if sb.get("run") is None or sb.get("pullback") is None:
                continue
            core_dip.append(e)
        elif cat == "comeback":
            comeback.append(e)
        elif marked and cat in ("rebound", "short_term"):
            yt.append(e)
        elif weak and cat in ("momentum", "known_new_face", "new_face"):
            continue
        else:
            other.append(e)
    core_dip.sort(key=_core_dip_entry_quality)
    comeback.sort(key=lambda x: comeback_sort_key(x, flow_pct_map))
    yt.sort(key=lambda x: -to_float(x.get("score"), default=0.0))
    other.sort(key=lambda x: -to_float(x.get("score"), default=0.0))
    if weak:
        ordered = core_dip[:4] + comeback[:3] + yt[:2] + other[:1]
    else:
        ordered = yt[:3] + other[:3] + core_dip[:2] + comeback[:2]
    return [(e["name"], e["category"], nextday_mark.get((e["symbol"], e["category"]), False)) for e in ordered[:top_n]]


def _pick_tag(cat: str, marked: bool) -> str:
    """动态推荐行内用的极简分类标签（2 字以内），配色辅助快速区分。"""
    if cat == "core_dip":
        return "低吸"
    if cat == "comeback":
        return "回马"
    if cat in ("rebound", "short_term"):
        if marked:
            return "🎯弹" if cat == "rebound" else "🎯转"
        return "反弹" if cat == "rebound" else "弱转"
    return {"momentum": "动量", "known_new_face": "kNF", "new_face": "新面"}.get(cat, cat)


# ── 展示视图模型（2026-08-29）──
# 此前终端「读 DB 当日累计推荐」、飞书「读本轮候选桶」，两个出口各渲染各的——
# 同一只票可能一边排第 1、另一边不出现。ScanView 收口为唯一展示数据源：
# build_scan_view 只算不画，render_terminal / feishu 只画不算。

# 表格列定义（单一事实来源）：表头与数据行均由同一 spec 推导。
# 此前两处各自写死列宽 f-string，且 detail 表表头/行的序号列分隔符不一致
# （表头 1 空格 / 行 2 空格），导致「名称」列起整体错位 1 列。
COLS_POOL: tuple = (
    ("#", 3, "r"),
    ("代码", 12, "l"),
    ("名称", 10, "l"),
    ("涨幅", 8, "r"),
    ("5日累计", 8, "r"),
    ("现价", 7, "r"),
    ("排名", 8, "r"),
    ("评分", 4, "r"),
    ("策略", 5, "l"),
)
COLS_DETAIL: tuple = (
    ("#", 3, "r"),
    ("代码", 12, "l"),
    ("名称", 10, "l"),
    ("涨幅", 8, "r"),
    ("5日累计", 8, "r"),
    ("现价", 7, "r"),
    ("排名", 8, "r"),
    ("板块", 14, "l"),
    ("策略", 5, "r"),  # 行内策略标签为右对齐（沿用 _print_priority_row 原渲染口径）
    ("评分", 4, "r"),
    ("时间", 6, "l"),
)
COLS_RULE: tuple = (
    ("#", 3, "r"),
    ("代码", 12, "l"),
    ("名称", 10, "l"),
    ("离5日线", 8, "r"),
    ("波幅%", 7, "r"),
    ("20日涨%", 8, "r"),
    ("状态", 6, "l"),
)


def _table_header(spec: tuple) -> str:
    """按列 spec 生成表头（与 _table_row 同源，杜绝表头/行宽漂移）。"""
    return "  " + " ".join(_pad(title, width, align) for title, width, align in spec)


def _table_row(cells, spec: tuple) -> str:
    """按列 spec 拼一行（宽度/对齐与 _table_header 同源）。

    单元格可含 ANSI 色码：_pad 按可见宽度补位，已着色的单元格宽度达标时不额外补。
    """
    parts = [_pad(str(cell), width, align) for cell, (_, width, align) in zip(cells, spec, strict=True)]
    return "  " + " ".join(parts)


@dataclass
class MainRow:
    """策略优选池一行（已排好序，字段均为渲染所需的最终值）。"""

    entry: RecommendationRow  # 含 _candidate / _core_stock / live_* 展示层注入键
    rank: int | float | None  # 展示用排名（None = 掉榜/无数据 → 渲染为 —）
    accum: float | None  # 5 日累计涨幅
    score: float
    core: bool  # 核心股高亮
    cat_label: str  # RBD / MOM / NEW / kNF / ST
    pct: float  # 涨幅（_entry_display_quote 单源回退链）
    current: float  # 现价（0.0 = 无数据 → 渲染为 —）


@dataclass
class ScanView:
    """一次扫描的展示视图：纯数据，不持有 conn、不做 print。

    warnings 收集降级告警（regime 判定失败 / 优选池构建中断等），由渲染器统一输出——
    计算阶段不再直接 print，保证「无终端」消费方（飞书卡片、单测）不被污染。
    """

    main_rows: list[MainRow]
    comeback_rows: list[RecommendationRow]
    core_dip_rows: list[RecommendationRow]
    nextday_mark: dict[tuple[str, str], bool]
    breakout_mark: dict[tuple[str, str], bool]
    flow_pct_map: dict[str, float]
    last_ranks: dict[str, int]
    adj_picks: list[tuple[str, str, bool]] | None
    weak: bool
    show_comeback: bool
    show_core_dip: bool
    warnings: list[str]
    rule_result: RuleResult | None = None


def build_scan_view(
    conn=None,
    live_quotes: dict[str, dict] | None = None,
    rank_map: dict[str, int] | None = None,
    today_pool: dict[str, Candidate] | None = None,
    last_ranks: dict[str, int] | None = None,
    weak: bool | None = None,
):
    """构建一次扫描的展示视图（纯计算，不 print）：读今日推荐并算出档位/标记/排序。

    返回值供 render_terminal / 飞书卡片共用，保证各出口看到同一份选择。
    无 conn 或今日无推荐时返回 None（由调用方决定是否渲染）。

    live_quotes: {symbol: {percent, current}} 实时行情覆盖，优先于候选池和数据库数据。
    rank_map: {symbol: 飙升榜排名} 当前扫描的榜单排名，为掉榜/重启行补实时排名。
    today_pool: {symbol: Candidate} 本轮候选池快照（缺省空），供掉榜/重启行之外的行
    渲染最新候选数据（实时候选 > DB 快照）。
    last_ranks: 上一轮扫描的榜单排名 {symbol: rank}，供「排名」列显示雪球榜单排名变化
    （+N 升 / -N 降），与已下线策略桶同口径；缺省 None 不显示变化。

    策略优选池排序键（2026-08-30）：榜上优先 → 涨幅升序 → 回调核心 → 排名升序 → 新面孔。
    🎯（次日大涨画像）/⚡（蓄势突破观察）为行尾展示标记，不参与排序、不改评分、不落库。
    """
    if conn is None:
        return None

    # 降级告警收集器：计算阶段不 print，统一由 render_terminal 输出。
    warnings: list[str] = []

    today_recs = get_today_recommendations(conn)
    if not today_recs:
        return None

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

    # 🎯 标记预计算（2026-08-14 起 map 化：排序+渲染各调一次 _is_nextday_marked 会触发
    # 两次 daily_kline 回放全表扫描；预计算后只查一次，判定与行尾渲染共用同一结果）。
    # 键 (symbol, category) 复合——nf∩st 双挂票两行判定口径不同，按 symbol 键控时归属
    # 取决于遍历顺序（隐式依赖）。
    nextday_mark: dict[tuple[str, str], bool] = {}
    # P1-9（2026-08-20）：全部推荐一次性批量回放累计（build_accum_map 单查询），
    # 替代逐行 _nextday_entry_accum 的 N+1 daily_kline 查询。
    accum_map = build_accum_map(conn, today_recs)
    for e in today_recs:
        nextday_mark[(e["symbol"], e["category"])] = _is_nextday_marked(e, conn, accum_map=accum_map)
    # 双挂票归一（用户确认）：同 symbol 存在 short_term 行时，其余类别行沿用 st 行的
    # 🎯 判定，与「nf∩st 双挂恒存 short_term」的池内事实对齐。
    st_marks = {(s, c): v for (s, c), v in nextday_mark.items() if c == "short_term"}
    for key in list(nextday_mark):
        st_key = (key[0], "short_term")
        if key[1] != "short_term" and st_key in st_marks:
            nextday_mark[key] = st_marks[st_key]

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

    # kNF 分数反指等组内分数键语义单源在 ranking.score_sort_key（today_report 归因复用
    # 同一实现）；策略优选池（下方 5 级排序键）不再使用它。

    # 蓄势突破观察标记（2026-08-21，⚡）：新面孔/首推或重上榜 short_term + 横盘缩量回调位
    # + MA 多头。纯展示层观察——不改排序/评分/落库（用户决策：先观察积累样本，达标后再评估
    # 是否升级为排序因子）。仅主表五类参与判定；批量取 K 线防 N+1。
    breakout_kmap = build_breakout_kline_map(conn, main_recs)
    # 2026-08-22 标记精简：两个变体判定保留（样本统计需区分），渲染合并为单一 ⚡。
    # 键 (symbol, category)：nf∩st 双挂票两行类别门不同（⚡ vs ⚡R 按构造不相交），
    # 按 symbol 键控时后写覆盖会随机丢掉其中一行的判定。
    # 2026-08-26：类别门走 _breakout_profile_key 单源（dispatcher 判变体归属一次，
    # 结构条件 _breakout_structure_ok 跑一次——替代原两谓词各判一遍门）。
    breakout_mark: dict[tuple[str, str], bool] = {
        (e["symbol"], e["category"]): (
            _breakout_profile_key(e) is not None
            and _breakout_structure_ok(e, conn, accum_map=accum_map, klines=breakout_kmap.get(e["symbol"]))
        )
        for e in main_recs
    }

    # 综合排序主表已隐藏（2026-08-28）：策略优选池已替代其展示功能。
    # （档位分组渲染的旧实现已删除；需还原见 git 历史，勿在此堆积注释代码。）

    # 策略优选池（2026-08-28）：按优先级规则排序的详细列表，关键列展示。
    # 排序规则：榜上优先 → 涨幅升序 → 回调核心 → 排名升序 → 新面孔。
    def _cb_core_pullback_ok(sym: str) -> bool:
        kl = breakout_kmap.get(sym)
        if not kl or len(kl) < 20:
            return False
        h20 = max(b[1] for b in kl[-20:])
        t1_close = kl[-1][2]
        if h20 <= 0 or t1_close <= 0:
            return False
        pb = t1_close / h20 - 1.0
        return CORE_PULLBACK_MIN <= pb <= CORE_PULLBACK_MAX

    _stg_map = {
        "rebound": "RBD",
        "momentum": "MOM",
        "new_face": "NEW",
        "known_new_face": "kNF",
        "short_term": "ST",
    }
    main_rows: list[MainRow] = []
    try:
        _seq_rows = []
        for e in main_recs:
            sym = e["symbol"]
            cat = e["category"]
            rk = e.get("live_rank") or e.get("rank")
            has_rank = isinstance(rk, (int, float)) and rk > 0
            is_core = bool(e.get("_core_stock"))
            is_cb_core = is_core and _cb_core_pullback_ok(sym)
            is_new = _stg_map.get(cat) in ("NEW", "kNF")
            # 涨幅键与展示列同源（_entry_display_quote）：live 0.00% 合法不被 `or` 吞。
            chg = _entry_display_quote(e)[0]
            _fresh_c = _fresh_candidate(e)
            accum_val = None
            if _fresh_c and _fresh_c.kline:
                accum_val = _fresh_c.kline.accumulated_pct
            if accum_val is None:
                accum_val = accum_map.get(sym)
            score = e.get("score", 0)
            _seq_rows.append(
                (
                    0 if has_rank else 1,  # ① 榜上优先
                    chg,  # ② 涨幅升序
                    0 if is_cb_core else 1,  # ③ 回调核心
                    rk if has_rank else 9999,  # ④ 排名升序
                    0 if is_new else 1,  # ⑤ 新面孔
                    e,
                    is_core,
                    accum_val,
                    score,
                )
            )
        _seq_rows.sort(key=lambda x: x[:5])
        # 逐行解析为 MainRow（排序在上面的元组里完成，此处只做展示字段定型）。
        # 2026-08-29：候选（_fresh_c）必须逐行重算——构建循环里的 _fresh_c 只保留末行，
        # 跨行复用会把上一只票的行情安到本行。
        for _hr, _cpc, _rk, _nw, _ch, _e, _ic, _av, _sc in _seq_rows:
            _fresh_c = _fresh_candidate(_e)
            _rk_disp = _e.get("live_rank") or _e.get("rank")
            if _rk_disp is None and _fresh_c:
                _rk_disp = _fresh_c.stock.rank
            _rk_val = _rk_disp if isinstance(_rk_disp, (int, float)) and _rk_disp > 0 else None
            _pct_row, _cur_row = _entry_display_quote(_e)
            main_rows.append(
                MainRow(
                    entry=_e,
                    rank=_rk_val,
                    accum=_av,
                    score=_sc or 0,
                    core=_ic,
                    cat_label=_stg_map.get(_e["category"], "?"),
                    pct=_pct_row,
                    current=_cur_row,
                )
            )
    except EXTERNAL_FAILURES as _e:
        # 2026-08-29：原为 `except Exception: pass`——渲染循环里任何 KeyError/TypeError
        # 都会让整张榜单静默截断，用户只看到"票变少了"而无从察觉。收窄到数据类异常
        # 并显式告警（代码 bug 则冒泡到主循环记录完整 traceback）。
        warnings.append(f"策略优选池构建中断（数据缺失）: {type(_e).__name__}: {_e}")

    # 动态推荐（2026-08-27）：根据近端主表档次日表现自动判断 regime，弱市下把
    # momentum/known_new_face/new_face 类🎯 从推荐序列剔除、优先 核心低吸/回马枪/
    # rebound🎯/弱转强；强市则正常优先级。纯展示行，不改排序/评分/落库。
    # 市况信号与头部 _market_env_tag / 飞书 env_tag 同源（统一 _regime_weak）；
    # weak 由调用方传入时复用（避免 Display 头/体重复查询），None 时自算一次。
    if weak is None:
        try:
            weak = _regime_weak(conn)
        except EXTERNAL_FAILURES as _e:
            # 编程错误（KeyError/TypeError 等）不再被吞——冒泡到主循环记录完整 traceback；
            # 此处仅承接数据类异常并按强市 fail-open。
            warnings.append(f"regime 判定中断（数据缺失，按强市处理）: {type(_e).__name__}: {_e}")
            weak = False
    _weak = weak
    try:
        _adj = _adjusted_picks(today_recs, nextday_mark, conn, flow_pct_map, weak=_weak)
    except EXTERNAL_FAILURES as _e:
        # 2026-08-29：原为裸 except Exception（`pi-lens-ignore` 绕过告警）——会把
        # _adjusted_picks 内的 KeyError/TypeError 静默吞成「无动态推荐」，用户无法
        # 区分「本就无推荐」与「推荐逻辑崩了」。收窄到数据类异常并显式告警。
        warnings.append(f"动态推荐计算中断（数据缺失）: {type(_e).__name__}: {_e}")
        _adj = None

    # 显示门（回马枪 / 核心低吸）：主区条数 ≤ COMEBACK_DISPLAY_MIN_MAIN 或弱市 regime 时展示。
    # 弱市时动态推荐把这两类排到前列，需强制展示对应区（覆盖主区密集隐藏门），修复
    # 「推荐了却看不到标的」割裂；强市下它们排在末尾，保持原隐藏门（主区密集即藏）。
    _show_lowbuy = len(main_recs) <= COMEBACK_DISPLAY_MIN_MAIN
    _show_comeback = bool(comeback_recs) and (_show_lowbuy or _weak)
    # 2026-08-24：回马枪区内按资金流优先（ranking.comeback_sort_key 单源，与 today_report
    # 回马枪小节同源防漂移）——▲▲回流可取在前、▼▼背离回避劣后，次键评分。
    _comeback_sorted = sorted(comeback_recs, key=lambda x: comeback_sort_key(x, flow_pct_map))
    core_dips: list[RecommendationRow] = list(core_dip_recs)
    core_dips.sort(key=_core_dip_entry_quality)
    _show_core_dip = bool(core_dips) and (_show_lowbuy or _weak)

    # 次日大涨高概率规则（纯 DB-only 计算，不改 score / 不进综合排序）
    # conn 此时已非 None（函数入口对 conn is None 提前返回 None）
    _rule_result = scan_rule(conn)

    return ScanView(
        main_rows=main_rows,
        comeback_rows=_comeback_sorted[:COMEBACK_DISPLAY_MAX],
        core_dip_rows=core_dips[:COMEBACK_DISPLAY_MAX],
        nextday_mark=nextday_mark,
        breakout_mark=breakout_mark,
        flow_pct_map=flow_pct_map,
        last_ranks=last_ranks or {},
        adj_picks=_adj,
        weak=_weak,
        show_comeback=_show_comeback,
        show_core_dip=_show_core_dip,
        warnings=warnings,
        rule_result=_rule_result,
    )


def render_terminal(view: ScanView) -> None:
    """把 ScanView 渲染到终端（纯渲染：不读库、不重算标记）。

    与 build_scan_view 分离的收益：飞书卡片可复用同一视图，杜绝此前「终端读 DB
    当日累计推荐 / 飞书读本轮候选桶」的选择分叉（同一只票两边排位可能不一致）。
    """
    for _w in view.warnings:
        print(f"  [!] {_w}")

    # ── 策略优选池 ──
    print(f"  {ANSI['BOLD']}◆ 策略优选池 — 按优先级排序{ANSI['RESET']}")
    print(_table_header(COLS_POOL))
    for _si, row in enumerate(view.main_rows, 1):
        _e = row.entry
        _nm = _e["name"]
        _nm_disp = f"{ANSI['BOLD']}{ANSI['MAGENTA']}{_nm}{ANSI['RESET']}" if row.core else _nm
        _av_str = f"{row.accum:+.2f}%" if row.accum is not None else "—"
        _rk_val = str(row.rank) if row.rank is not None else "—"
        _sc_str = f"{row.score:.0f}" if row.score else "—"
        _cur_str = f"{row.current:.2f}" if row.current else "—"
        # 行尾标记与回马枪/低吸区同源（_entry_row_suffix）：风险/资金流/🎯/⚡。
        _marked = view.nextday_mark.get((_e["symbol"], _e["category"]), False)
        _bolt = view.breakout_mark.get((_e["symbol"], _e["category"]), False)
        _suffix = _entry_row_suffix(_e, view.flow_pct_map, marked=_marked, breakout_marked=_bolt)
        print(
            _table_row(
                [
                    str(_si),
                    _e["symbol"],
                    _nm_disp,
                    pct_colored(row.pct),
                    _av_str,
                    _cur_str,
                    _rk_val,
                    _sc_str,
                    row.cat_label,
                ],
                COLS_POOL,
            )
            + _suffix
        )

    # ── 动态推荐 / ⚡ ──
    if view.adj_picks:
        _seen = set()
        _parts = []
        for _n, _c, _m in view.adj_picks:
            _tag = _pick_tag(_c, _m)
            _key = (_tag, _c)
            if _key in _seen:
                continue
            _seen.add(_key)
            if _c == "core_dip":
                _parts.append(f"{ANSI['GREEN']}{_tag}{ANSI['RESET']}")
            elif _c == "comeback":
                _parts.append(f"{ANSI['CYAN']}{_tag}{ANSI['RESET']}")
            elif _m:
                _parts.append(f"{ANSI['BOLD']}{ANSI['MAGENTA']}{_tag}{ANSI['RESET']}")
            else:
                _parts.append(_tag)
        print(f"  {ANSI['BOLD']}动态推荐{ANSI['RESET']}: {' > '.join(_parts)}")
    if any(view.breakout_mark.values()):
        print(
            f"  {ANSI['CYAN']}⚡ 蓄势突破观察{ANSI['RESET']}（缩量回调蓄势位·含新面孔/重上榜两变体"
            f"·样本收集中·非排序因子）"
        )

    # ── 回马枪独立区 ──
    if view.show_comeback:
        print(f"\n{ANSI['CYAN']}◆ 回马枪 — 掉榜跟踪/回调买点（主区稀少·补充参考）{ANSI['RESET']}")
        print(_table_header(COLS_DETAIL))
        for ci, entry in enumerate(view.comeback_rows, 1):
            _print_priority_row(entry, ci, view.flow_pct_map, last_ranks=view.last_ranks)
        print("  排序=今日波动（涨多/跌狠优先）→主力净占比→评分。")
        print(f"  {'-' * 92}")

    # ── 核心方向低吸独立区（2026-08-19，scanner/core_themes.py）──
    # 大跌市中找「当前主线方向核心股低吸」参考。2026-08-19 起随扫描落库
    # category=core_dip（同 comeback 族），本区从今日 recommendations 读取。
    if view.show_core_dip:
        print(f"\n{ANSI['GREEN']}◆ 核心方向低吸 — 主线方向核心股回调参考（主区稀少·补充参考）{ANSI['RESET']}")
        print(_table_header(COLS_DETAIL))
        for di, entry in enumerate(view.core_dip_rows, 1):
            _print_priority_row(entry, di, view.flow_pct_map, last_ranks=view.last_ranks)
        print("  排序=今日波动（涨多/跌狠优先）→主力回流→回撤深→龙头强。")
        print(f"  {'-' * 92}")

    # ── 次日大涨高概率候选（实证规则，2026-08-30）──
    # 规则阈值从 config 读取（单源，与 scanner/nextday_rule.py 同一套常量）。
    # H2 盲测 LIFT 2.58x，均值 +1.31%（脚本 nextday_rule_scan.py 可复现）。
    # 全部只用已完成 bar，盘中任意时刻可算，全天不漂移。
    _rp = view.rule_result
    if _rp and _rp.picks:
        _rule_desc = (
            f"ma5r≥{NEXTDAY_RULE_MA5R_MIN:.0f}% & atrpct≥{NEXTDAY_RULE_ATRPCT_MIN:.0f}%"
            f" & ret20≤{NEXTDAY_RULE_RET20_MAX:.0f}%"
        )
        print(f"\n{ANSI['BOLD']}{ANSI['YELLOW']}◆ 次日大涨高概率候选（实证规则）{ANSI['RESET']}（{_rule_desc}）")
        print(_table_header(COLS_RULE))
        for ri, pick in enumerate(_rp.picks, 1):
            _status = (
                f"{ANSI['GREEN']}已推荐{ANSI['RESET']}" if pick.already_rec else f"{ANSI['YELLOW']}新增{ANSI['RESET']}"
            )
            print(
                _table_row(
                    [
                        str(ri),
                        pick.symbol,
                        pick.name,
                        f"{pick.ma5r:+.1f}%",
                        f"{pick.atrpct:.1f}",
                        f"{pick.ret20:+.1f}%",
                        _status,
                    ],
                    COLS_RULE,
                )
            )
        _rec_cnt = sum(1 for p in _rp.picks if p.already_rec)
        _new_cnt = _rp.rule_hit - _rec_cnt
        print(
            f"  命中 {_rp.rule_hit}/{_rp.board_size} 只"
            f"（已推荐 {_rec_cnt} + 新增 {_new_cnt}）"
            f"  H2 盲测 LIFT 1.53x · 均次日 +1.59% · 跌超7% 7.5%"
        )
        print(f"  {'-' * 92}")


def display_priority(
    conn=None,
    live_quotes: dict[str, dict] | None = None,
    rank_map: dict[str, int] | None = None,
    today_pool: dict[str, Candidate] | None = None,
    last_ranks: dict[str, int] | None = None,
    weak: bool | None = None,
) -> "ScanView | None":
    """构建展示视图并渲染到终端（build_scan_view + render_terminal 的便捷入口）。

    weak：市况信号（弱市布尔）。None 时由 build_scan_view 内部按 _regime_weak 自算；
    传入则复用（display 主屏已在打印头部前算过一次，避免重复查询）。
    返回 ScanView 供复用（display 主屏回传飞书 / 测试捕获输出后取数据两用）；
    无 conn 或今日无推荐时返回 None。
    """
    view = build_scan_view(
        conn=conn,
        live_quotes=live_quotes,
        rank_map=rank_map,
        today_pool=today_pool,
        last_ranks=last_ranks,
        weak=weak,
    )
    if view is None:
        return None
    render_terminal(view)
    return view
