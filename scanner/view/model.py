import os
import re
from dataclasses import dataclass

import wcwidth

from scanner.config import (
    TREND_MARK_ENABLED,
)
from scanner.core_themes import low_buy_quality as _core_dip_quality
from scanner.models import Candidate, RecommendationRow
from scanner.nextday_rule import RuleResult

# 排序/画像纯逻辑单源在 scanner.ranking；display 只导入渲染所需子集。
# 此前的全量 re-export（供 scripts 的 display._entry_* 属性访问）已下线：
# 消费方直接 import scanner.ranking（scripts/review_tier_replay.py 已改）。
from scanner.ranking import (
    entry_dims,
    fresh_candidate,
)
from scanner.sector import classify_sector
from scanner.signals import fund_flow_signal, split_risk_flags

# 走势美感标记判定单源（与美感门同源；相对导入绕开 pyright 会话冻结快照的绝对名解析）
from scanner.trend_beauty import beauty_mark
from scanner.utils import to_float

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
from scanner.categories import CATEGORY_COLOR_KEYS  # noqa: E402

CAT_COLOR = {name: ANSI[key] for name, key in CATEGORY_COLOR_KEYS.items()}

# 叶子辅助 + 视图模型（model 层，不依赖 assemble/render）

__all__ = (
    "ANSI",
    "CAT_COLOR",
    "COLS_DETAIL",
    "COLS_HOT",
    "COLS_POOL",
    "MainRow",
    "ScanView",
    "_ANSI_ESCAPE",
    "_FUND_FLOW_ICON",
    "_beauty_mark_for",
    "_core_dip_entry_quality",
    "_entry_dip_labels",
    "_entry_row_suffix",
    "_entry_sector",
    "_fund_flow_icon_str",
    "_handle",
    "_is_console",
    "_kernel32",
    "_market_env_tag",
    "_market_extra_str",
    "_mode",
    "_pad",
    "_rank_delta_str",
    "_supports_ansi",
    "_trunc",
    "_v2_pool_sort_key",
    "_vis_len",
    "entry_display_quote",
    "pct_colored",
)

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


def entry_display_quote(entry: RecommendationRow | dict) -> tuple[float, float]:
    """涨幅/现价统一回退链（单源）：实时行情 → 可信候选快照 → DB 落库。

    - live_quote_available 时 live_percent=0.0 是合法 0.00%，不得被 `or` 吞成 DB 值；
    - stale 掉榜候选 / 双挂票类别错位候选经 fresh_candidate 视同无候选，直接落 DB；
    - live_current 缺失时用候选快照现价兜底（保持原 _print_priority_row 行为）。

    优选池行、回马枪/低吸区行、涨幅升序排序键共用——杜绝同一票两区涨幅口径漂移。
    返回 (pct, current)。
    """
    c = fresh_candidate(entry)
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


def _v2_pool_sort_key(has_label: bool, pct: float, rank: float | None) -> tuple:
    """v2 池选区排序键（2026-09-04 修改）：排名升序 → 低吸标签优先 → 涨幅降序。

    - 主键：榜上排名升序（rank 缺失即掉榜票 10**9 沉底）
    - 次键：命中任一低吸标签（超跌反转/缩量回调/均线支撑/放量突破/弱转强）的票进前段
    - 三键：涨幅降序消除平局洗牌
    """
    return (rank if rank is not None else 10**9, 0 if has_label else 1, -pct)


def _entry_dip_labels(entry: RecommendationRow | dict) -> list[str]:
    """单条推荐记录的低吸语义标签（matcher 层标注，单源回退链）。

    统一走 entry_dims（ranking.py）：实时候选 dims → DB score_breakdown → 空。
    排序与行尾渲染共用，杜绝两处口径漂移。
    """
    labels = entry_dims(entry).get("dip_labels")
    return labels if isinstance(labels, list) and labels else []


def _entry_sector(entry: RecommendationRow | dict) -> str:
    """板块列单源（主表/详情区同口径，防两处回退链漂移）。

    优先级：推荐时落库的推动概念 > 当前池候选的推动概念 > 分类板块 > 名称关键词。
    """
    db_concept = (entry.get("concept") or "").strip()
    if db_concept:
        return db_concept
    c = entry.get("_candidate")
    if c:
        if c.driving_concept:
            return c.driving_concept
        if c.sector:
            return c.sector
    return classify_sector(entry["name"])


def _beauty_mark_for(entry: RecommendationRow | dict, kline: list | None) -> str:
    """v1/v2 行尾走势标记：满足美感 → "美"；否则空（不标丑，2026-09-09 用户口径）。

    判定单源在 trend_beauty.beauty_mark（与美感门同源、fail-open 一致：数据缺失
    不标，避免误导）。纯展示，不改过滤/排序/落库。2026-09-09 数据裁决后硬拦
    默认关，本标记保留作买入体验参考（"稳而不爆"）。开关 RTS_TREND_MARK。
    """
    if not TREND_MARK_ENABLED:
        return ""
    return beauty_mark(entry, kline, fresh_candidate(entry))


def _entry_row_suffix(
    entry: RecommendationRow | dict,
    flow_pct_map: dict[str, float],
    marked: bool = False,
    breakout_marked: bool = False,
    beauty: str = "",
) -> str:
    """行尾可变区统一渲染：风险标记 → 资金流/连板 extra → 🎯 → ⚡ → 走势标记。

    优选池行与核心低吸区行共用（2026-08-30 收口）——此前仅补充区渲染这些
    标记，主视图优选池行丢失 🎯/⚡/资金流信息。顺序与原 _print_priority_row 一致。
    （💡低吸标签行尾渲染已按需求移除——只用于排序不展示，2026-09-03）
    beauty: 走势美感标记（_beauty_mark_for 产出；仅 v1/v2 池选行传入）。
    """
    c = fresh_candidate(entry)
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
    # 2026-09-04: 🎯 命中率过低，暂时不渲染（档位判定逻辑保留）
    # if marked:
    #     parts.append(f" {ANSI['GREEN']}🎯{ANSI['RESET']}")
    if breakout_marked:
        parts.append(f" {ANSI['CYAN']}⚡{ANSI['RESET']}")
    # 盘中操作纪律标签（纯展示，不参与排序/评分）
    if c and getattr(c, "tactic_tags", None):
        for tag in c.tactic_tags:
            parts.append(f" {ANSI['YELLOW']}{tag}{ANSI['RESET']}")
    # 走势美感标记（2026-09-09，仅 v1/v2 池选行传入）：满足美感标「美」绿，不标丑
    if beauty:
        parts.append(f" {ANSI['GREEN']}{beauty}{ANSI['RESET']}")
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
    """推荐记录条目 → 低吸质量排序键（复用 core_themes.low_buy_quality）。

    entry 是完整 recommendation 行（含 score_breakdown 的 run/pullback/today_pct/
    flow_pct/concept），先经 entry_dims 抽取为低吸质量函数所需字典再排序。
    """
    sb = entry_dims(entry)
    return _core_dip_quality(
        {
            "concept": sb.get("concept", ""),
            "flow_pct": to_float(sb.get("flow_pct"), default=None),
            "today_pct": to_float(sb.get("today_pct"), default=0.0),
            "run": to_float(sb.get("run"), default=0.0),
            "pullback": to_float(sb.get("pullback"), default=0.0),
        }
    )
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
    ("板块", 14, "l"),
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
# 沪深飙升·极有可能大涨独立区（2026-09-11 合入）：列与本区口径对应，
# 与主线 COLS_POOL/COLS_DETAIL 无关（不共享 5日累计/板块/策略等主线专属列）。
COLS_HOT: tuple = (
    ("#", 3, "r"),
    ("代码", 12, "l"),
    ("名称", 10, "l"),
    ("现价", 8, "r"),
    ("涨幅", 8, "r"),
    ("排名上升", 9, "r"),
    ("成交量", 11, "r"),
    ("成交额", 9, "r"),
    ("量比", 6, "r"),
    ("换手%", 7, "r"),
    ("市值(亿)", 9, "r"),
    ("评分", 6, "r"),
    ("连击", 5, "r"),
)
@dataclass
class MainRow:
    """v1 池选一行（已排好序，字段均为渲染所需的最终值）。"""

    entry: RecommendationRow  # 含 _candidate / _core_stock / live_* 展示层注入键
    rank: int | float | None  # 展示用排名（None = 掉榜/无数据 → 渲染为 —）
    accum: float | None  # 5 日累计涨幅
    score: float
    composite_score: float  # 统一复合评分 [0, 10]（v1+v2 合一排序键）
    core: bool  # 核心股高亮
    cat_label: str  # RBD / MOM / NEW / kNF / ST
    pct: float  # 涨幅（entry_display_quote 单源回退链）
    current: float  # 现价（0.0 = 无数据 → 渲染为 —）
    sector: str  # 板块（_entry_sector 单源，与详情区同口径）


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
    # v2 池选区行（双跑同屏，2026-09-02）：独立于主表（两套排序口径不同），None = 今日无 pool_pick。
    pool_rows: list[MainRow] | None = None
    # 池选全量票数（2026-09-03）：pool_rows 只展示前 V2_POOL_DISPLAY_TOP 行，
    # 终端尾部注明与飞书头部「池选 N 只」计数用全量值，避免截断后失真。
    pool_total: int = 0
    # 决策层文本行（2026-09-04）：≤3 只短名单或空仓原因，渲染在所有区块之前。
    # 由 build_scan_view 计算并落库 decision_picks（终端/飞书共用同一份）。
    decision_lines: list[str] | None = None
    # 终选参考区文本行（2026-09-04）：v1+v2 合池 → 档0画像评级 ≤3 只 + 落选理由。
    # 与决策层互补（决策层答「该不该买」，终选区答「必须持仓时买谁」），渲染在决策层之后。
    final_pick_lines: list[str] | None = None
    # 走势美感标记（2026-09-09）：{(symbol, category): "✓走势"|"⚠走势"}，v1/v2 池选行
    # 行尾渲染（_entry_row_suffix beauty 参数）。与终选美感门同源判定，纯展示预判。
    beauty_mark: dict[tuple[str, str], str] | None = None
    # 沪深飙升·极有可能大涨独立区（2026-09-11 自 rts-xueqiu 合入）：HotCandidate 列表。
    # 与主线（创业板/next_day 口径）完全解耦——样本面更宽（沪深主板+创业板）、口径为
    # 「当日 momentum + 榜单热度跃升」，不参与主线评分/档位/🎯，也不进飞书主卡片。
    # None = 本轮未启用或无结果（渲染时整区跳过，不留空表）。
    hot_rows: list | None = None
    # 展示层资金流出硬门（2026-09-14）剔除的行数：主力净占比 ≤ FUND_OUTFLOW_NET_PCT
    # 的票不进任何展示区（v1/v2 池选 / 核心低吸 / 回马枪 / 终选输入），终端与飞书同源。
    # 纯展示层过滤——不改 excluded、不落库，回测/归因样本口径不受影响。
    flow_filtered: int = 0
