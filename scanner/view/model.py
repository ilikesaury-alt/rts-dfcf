import os
import re
from dataclasses import dataclass

import wcwidth

from scanner.config import (
    OFFBOARD_OPENING_SILENCE_MIN,
    TREND_MARK_ENABLED,
)
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

# 走势美感标记判定单源（日线定准入、分时定级别；相对导入绕开 pyright 会话冻结快照的绝对名解析）
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

# ⚠ _kernel32 / _handle / _mode 是**仅 Windows 分支存在**的中间量，**不得**列入下方 __all__：
# `from scanner.view.model import *` 会按 __all__ 逐名 getattr，Linux/macOS 下这三个名字
# 不存在 → AttributeError（2026-09-14 修）。旧 __all__ 由一次性拆分脚本
# `scripts/_split_display.py` 按「AST 模块级名 ∩ dir()」推导，在 Windows 上生成时就
# 把它们写进了导出表；该脚本已于 2026-09-14 删除（拆分已完成，留着是颗按硬编码行号
# 覆写 view/ 的地雷）。改动前先跑 `scripts/_verify_view_split.py`（等价性证明）。
# ANSI / CAT_COLOR / _ANSI_ESCAPE / _is_console / _supports_ansi 才是对外契约（本模块为单源）。

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
    "COLS_HIST",
    "COLS_HOT",
    "COLS_POOL",
    "MainRow",
    "ScanView",
    "_ANSI_ESCAPE",
    "_FUND_FLOW_ICON",
    "_beauty_mark_for",
    "_entry_dip_labels",
    "_entry_row_suffix",
    "_entry_sector",
    "_fund_flow_icon_str",
    "_is_console",
    "_market_env_tag",
    "_market_extra_str",
    "_pad",
    "_rank_delta_str",
    "_supports_ansi",
    "_trunc",
    "_v2_pool_sort_key",
    "_vis_len",
    "entry_display_quote",
    "offboard_subtitle",
    "onboard_subtitle",
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
    # ⚠ 这里**不是**标准红涨绿跌，是刻意分级，2026-09-14 用户确认保留，勿「顺手改正」：
    #   ≥ +9% 红（过热预警）／ +5%~+9% 绿（温和上涨）／ < 0 黄（下跌）／ 0~+5% 无色（中性）。
    # 语义是「风险温度」而非「涨跌方向」：涨幅越大越可能是情绪顶（红），温和区间才给「舒服」的绿。
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


# 档位 → 图标。**只画负向两档**（2026-09-28）——判定单源不动，只改「画不画」。
# 每个数字都可离线复算：`python scripts/flow_mark_evidence.py`（改本注释先跑它）。
#
# 撤下正向 ▲/▲▲ 的依据：
#   · 全市场面板 6 个交易日（2026-09-18~09-28；`price` 字段 09-18 起才有）n≈30,676：
#     corr(当日主力净占比, 当日涨幅) Pearson **+0.29**（Spearman +0.45）——两者**同向**，
#     正向图标很大程度在复述「今天涨」，不是涨幅之外的独立维度。
#     ⚠ 本行首版写的「+0.058 / 近乎正交 / 涨幅列已覆盖」无法复现且推理反了：
#     正交只说明信息**不重叠**，推不出「已覆盖」。已按脚本输出更正。
#   · 同为上涨的票内按 flow 分 1/3 看次日：5 个交易日组内 Spearman **全负**
#     （-0.107 ~ -0.000），高 1/3 vs 低 1/3 次日中位差 4/5 天为负 ⇒ 正向无正区分度。
#   · 推荐池五档次日≥7% hit（同票同日去重，**截至 2026-09-28** n=1,554）：
#     strong_in 4.8% / in 4.6%，全样本 4.7% ⇒ 正向两档与「无信息」基线无差别。
#
# 保留负向 ▼/▼▼ 的理由是**语义一致**而非预测力：它标的是 −8% 展示硬门与
# 「资金流出」风险标签已经在用的同一阈值（`config_sources.FUND_OUTFLOW_NET_PCT`），
# 即「一个生效中的过滤正在剔谁」的提示。证据是弱的（strong_out hit 3.2% 最低 n=156、
# out 5.8% 最高 n=86，而中位次日% 五档彼此接近），所以它是**规避提示，不是选股信号**；
# 引用时一律用 hit 率口径（`config_scoring.CATEGORY_HIT_RATE` 同源），不要引用中位数。
# 另注：−8% 硬门开启（默认）时 `strong_out` 行根本进不了展示区，故 ▼▼ 多数时候不可达。
#
# ⚠ **只改展示，不改判定单源** `signals.fund_flow_signal`（仍返五档）：它同时被
# `ranking._fund_flow_norm` 消费并给 composite_score 加权（in/strong_in 各 +0.3），
# 那是权重口径，改它属权重变更、须走 rule_validate 样本外验证。
_FUND_FLOW_ICON = {
    "out": f"{ANSI['RED']}▼{ANSI['RESET']}",
    "strong_out": f"{ANSI['RED']}▼▼{ANSI['RESET']}",
}


def _fund_flow_icon_str(ff_pct) -> str:
    """主力净占比 → 流向图标（ANSI 着色）；**只画负向两档**，其余（流入/中性）返回空串。

    历史沿革：
    - 2026-08-22 中性档 ◇ 不再显示（(-5%,+5%) 覆盖大多数票且零信息）。
    - 2026-09-28 **正向档 ▲/▲▲ 一并撤下**（实测无区分度，见 _FUND_FLOW_ICON 上方注释），
      图标语义从「双向强弱分级」收窄为「主力大幅流出告警」，故飞书卡片侧同步只留 🔴/🔴🔴。
      判定单源 fund_flow_signal 保持五档不动（ranking._fund_flow_norm 仍按五档加权）。
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

    资金流用 fund_flow_signal 映射图标（中性**与正流入**不显示，见 _fund_flow_icon_str）；
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

    2026-09-14：v2 池选**展示区**已隐藏，故本函数当前**无生产调用方**（保留以便
    恢复该区时零成本复原，纯函数无依赖；单测仍在 tests/test_v2_pool_display.py）。
    隐藏的是渲染与 ScanView 字段，不是这套排序口径本身。
    """
    return (rank if rank is not None else 10**9, 0 if has_label else 1, -pct)


def _entry_dip_labels(entry: RecommendationRow | dict) -> list[str]:
    """单条推荐记录的低吸语义标签（matcher 层标注，单源回退链）。

    统一走 entry_dims（ranking.py）：实时候选 dims → DB score_breakdown → 空。
    排序与行尾渲染共用，杜绝两处口径漂移。

    2026-09-14：唯一调用方 `_v2_pool_sort_key` 随 v2 展示区隐藏而失去生产消费，
    故本函数现状同样只在单测中被调用（行尾渲染自 2026-09-03 起已不再画 💡 标签）。
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
    """v1 池选行尾走势标记（分档）："" / "稳" / "稳★"（不标丑，2026-09-09 用户口径）。

    判定单源在 trend_beauty.beauty_mark——**日线定准入、分时定级别**：日线不漂亮或
    不足 → 不标；日线漂亮而分时未确认（走弱/缺失）→ "稳"；日线漂亮且分时亦漂亮 → "稳★"。
    fail-open 一致：数据缺失只降档、不判否。纯展示，不改过滤/排序/落库。
    2026-09-09 数据裁决后走势漂亮**从不作准入硬门**，本标记保留作买入体验
    参考（"稳而不爆"）；开关 RTS_TREND_MARK。
    ⚠ ★ 的语义是**尾部回撤更小**，不是「更可能大涨」（稳★/稳 的 hit 无正向区分度）——
    分档依据与复现脚本见 trend_beauty 模块 docstring / scripts/beauty_mark_eval.py。
    2026-09-14：v2 池选展示区隐藏后，本标记现状只落在 v1 池选行。
    2026-09-21：原「终选美感门」（FINAL_PICK_BEAUTY_ENABLED，默认关）随终选参考区
    一并删除 —— 本项目**不再有任何按走势美感做准入的门**，只有本标记。
    """
    if not TREND_MARK_ENABLED:
        return ""
    return beauty_mark(entry, kline, fresh_candidate(entry))


def _entry_row_suffix(
    entry: RecommendationRow | dict,
    flow_pct_map: dict[str, float],
    breakout_marked: bool = False,
    beauty: str = "",
    guxing: str = "",
) -> str:
    """行尾可变区统一渲染：风险标记 → 资金流/连板 extra → ⚡ → 走势标记。

    优选池行与低吸区行共用（2026-08-30 收口）——此前仅补充区渲染这些
    标记，主视图优选池行丢失 ⚡/资金流信息。顺序与原 _print_priority_row 一致。
    （💡低吸标签行尾渲染已按需求移除——只用于排序不展示，2026-09-03）
    beauty: 走势美感标记（_beauty_mark_for 产出；仅 v1 池选行传入——v2 池选展示区
    已于 2026-09-14 隐藏）。

    2026-09-14：原 `marked`（🎯 次日大涨画像）入参已删除 —— 🎯 的行尾渲染自
    2026-09-04 停用，该参数遂成哑参（调用方一直在传、函数体不读）。
    2026-09-16：🎯 画像本身（ranking.is_nextday_marked）已按用户决策整体删除，
    故下方"要恢复展示"的路径已不存在——恢复需从 git 历史取回 ⌈判定 + 入参⌋ 两处。
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
    # 🎯（次日大涨画像）行尾标记：自 2026-09-04 停用（命中率过低），2026-09-16 连
    # 判定函数（ranking.is_nextday_marked）一并删除——故此处不再留注释开关。
    if breakout_marked:
        parts.append(f" {ANSI['CYAN']}⚡{ANSI['RESET']}")
    # 盘中操作纪律标签（纯展示，不参与排序/评分）
    if c and getattr(c, "tactic_tags", None):
        for tag in c.tactic_tags:
            parts.append(f" {ANSI['YELLOW']}{tag}{ANSI['RESET']}")
    # 走势美感标记（2026-09-09 上线 / 2026-09-15 分档，仅 v1/v2 池选行传入）：
    # 分档标「稳」/「稳★」绿，不标丑。★ 的语义（回撤更小·非更易大涨）只登记在
    # scanner/label_registry 的 counter 里 —— 该图例 2026-09-28 按用户决策停用，
    # 两端不再就地解释，挂回时改调 label_registry.legend_line。
    if beauty:
        parts.append(f" {ANSI['GREEN']}{beauty}{ANSI['RESET']}")
    # 妖股名单（2026-09-30 新增，ranking.guxing_mark 产出，形如「妖」）：
    # **静态名单匹配，不是信号** —— 只标「这票在名单内」，不预测涨跌
    # （名单无样本外预测力，见 config_scoring GUXING_WATCHLIST 注释）。
    # 放最后：不与 ⚠/▼/⚡/稳 抢读；纯文本单字跟「稳」同风格，不用 emoji。
    if guxing:
        parts.append(f" {ANSI['MAGENTA']}{guxing}{ANSI['RESET']}")
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


# 2026-09-21 删除：`_core_dip_entry_quality`（低吸质量排序键，复用
# core_themes.low_buy_quality）。它唯一的生产调用方是 assemble 里给终选参考区合池
# 准备的 core_dips 序列；终选参考区整体删除后该序列消失，排序键随之无消费方。
# 需复原见 git 历史；底层 low_buy_quality 仍在 scanner/core_themes.py（核心主题模块
# 自己在用），故本次只删视图层的这一层包装。

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
# 详情列规格：渲染实现是 render._print_priority_row。
# 2026-09-14 现状：其两个消费方（核心方向低吸区 / 回马枪区）都无展示区 ——
# 故本 spec 与 _print_priority_row 一样当前无生产调用方，**有意保留**（恢复低吸区
# 只需复原渲染块，不必重写列口径与对齐逻辑）。
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
# 与主线 COLS_POOL/COLS_DETAIL 无关（不共享 5日累计/策略等主线专属列）。
# 「板块」2026-09-22 由两段共同新增（A/B 共用一份列头，列集不分叉 —— 见
# render._hot_row_cells）：宽 14 与主线 COLS_POOL 同值，两区同名同宽才谈得上同口径。
COLS_HOT: tuple = (
    ("#", 3, "r"),
    ("代码", 12, "l"),
    ("名称", 10, "l"),
    ("涨幅", 8, "r"),
    ("5日累计", 8, "r"),
    ("现价", 8, "r"),
    ("排名上升", 9, "r"),
    ("成交量", 11, "r"),
    ("成交额", 9, "r"),
    ("量比", 6, "r"),
    ("换手%", 7, "r"),
    ("市值(亿)", 9, "r"),
    # 板块列位置沿用主线「板块紧挨评分之前」的相对位置（COLS_POOL 同序）。
    ("板块", 14, "l"),
    ("评分", 6, "r"),
    ("连击", 5, "r"),
)


# B 段「榜外异动」小标题的**括号正文**（无 ANSI）—— 终端与飞书两个出口共用一份。
# 单源动机（2026-09-22）：两处此前各写一份，且**两份都与实际排序行为不符** ——
# 终端漏了「主力净占比」这一级；飞书更是 09-21 层序翻转前的旧文案（写成
# 「排序=量比→主力净占比·T1 量先动/T2 启动首日」，层序与实际的 T2 在前**相反**）。
# 而既有测试只断言 `"榜外异动" in out`、从不断言排序键内容 ⇒ 漂移长期无守卫。
# 抽成单源后两出口逐字一致，文案与 offboard_watch.sort_key 的对应关系另由单测锁死。
def offboard_subtitle(n: int) -> str:
    """B 段小标题括号正文：候选来源 + 排序键 + 开盘静默窗 + 规模 + 证据强度。

    五项缺一不可（顺序固定）：漏「排序键」→ 最自然的误读是「它和 A 段一样是热度
    跃升」，而两者口径与证据强度完全不同；漏「未回测」→ 高估证据强度；漏「开盘
    静默窗」→ 掩盖排序键自身的成立前提（`offboard_watch.sort_key` docstring：
    量比失真与静默窗「是一组，不可只删其一」）。

    排序键必须与 `offboard_watch.sort_key` 返回的元组**逐项同序**::

        (_TIER_RANK[tier], -volume_ratio, -main_pct)
        ⇒ T2 启动首日 → T1 量先动 → 量比降序 → 主力净占比降序

    改 `sort_key` 必须同步改这里 —— 守卫见
    `tests/test_offboard_watch.py::test_offboard_subtitle_sort_text_matches_sort_key_behavior`
    （从**真实排序行为**反查，而非只比对字符串）。
    """
    return (
        "（榜外创业板·非榜单来源·"
        "排序=T2 启动首日→T1 量先动→量比→主力净占比"
        f"·开盘 {OFFBOARD_OPENING_SILENCE_MIN} 分内不产出(量比失真)"
        f"·{n} 只·观察段·未回测）"
    )


def onboard_subtitle(n: int) -> str:
    """榜内异动段小标题括号正文：来源 + 排序键 + 规模 + 证据强度。

    与 `offboard_subtitle` 同款纪律：必须写明「候选来源」与「未回测」——本段的
    候选是**榜内**（与 A 段同域但口径不同），不写清楚最自然的误读就是「它是 A 段的
    一部分」或「它比 B 段更可靠」。实际上**两者都不对**：
      · 它不是 A 段 —— A 段看热度跃升 + 已涨，本段只看 T1「量先动·价未动」；
      · 它不比 B 段可靠 —— 本段与 B 段同门同层（`offboard_gate` + `classify_tier`
        的 T1 分支逐条同源），**唯一差别是样本域从榜外换成榜内**。
    排序键与 `offboard_watch.sort_key` 逐项同序；本段只有 T1 一层，故省略层序。
    """
    return (
        "（榜内创业板·榜单来源·"
        "排序=量比→主力净占比"
        f"·开盘 {OFFBOARD_OPENING_SILENCE_MIN} 分内不产出(量比失真)"
        f"·{n} 只·观察段·未回测·与榜外段同门同层）"
    )


# 「v1 回捞」独立区（2026-09-16 上线）：列与本区口径对应（回调/量比/时效），
# 与主线 COLS_POOL 无关 —— 本区不排涨跌幅榜上位置，只回答「回调到位了没」。
COLS_HIST: tuple = (
    ("#", 3, "r"),
    ("代码", 12, "l"),
    ("名称", 10, "l"),
    ("涨幅", 8, "r"),
    ("5日累计", 8, "r"),
    ("现价", 8, "r"),
    ("量比", 6, "r"),
    ("距v1", 6, "r"),
    ("评分", 5, "r"),
    # 「上次v1桶」宽 14（2026-09-16 由 12 调大）：真实桶名可达 14 个 ASCII 列
    # （known_new_face / early_momentum），12 会让这些名字在本区内错列。
    # 飞书压缩版同宽（_COLS_HIST_FEISHU），两出口同值同宽。
    ("上次v1桶", 14, "l"),
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
    # 本轮新进入今日推荐池（2026-09-21）：v1 排序键**第 1 项**，也是行尾「新」标记的判定。
    # 判定源是 build_scan_view 的 new_symbols 入参（扫描循环持有的跨轮集合差集），
    # **不是** recommendations.time / first_time —— 那两个字段在「分数提高」时会被覆盖，
    # 语义是「最后一次提分时刻」而非首次出现（见 assemble.build_scan_view 的说明）。
    # ⚠ 刻意**不叫** `is_new`：`new_face` 是「过去 N 天未出现」的**策略桶**，与本字段
    # （本轮新进池，不看历史）是两回事，同名会让人误以为二者同源。
    is_new_entry: bool = False


@dataclass
class ScanView:
    """一次扫描的展示视图：纯数据，不持有 conn、不做 print。

    warnings 收集降级告警（regime 判定失败 / 优选池构建中断等），由渲染器统一输出——
    计算阶段不再直接 print，保证「无终端」消费方（飞书卡片、单测）不被污染。
    """

    main_rows: list[MainRow]
    breakout_mark: dict[tuple[str, str], bool]
    flow_pct_map: dict[str, float]
    last_ranks: dict[str, int]
    weak: bool
    warnings: list[str]
    rule_result: RuleResult | None = None
    # 2026-09-14 按用户决策移除的字段（需复原见 git 历史）：
    #   core_dip_rows / show_core_dip —— 核心方向低吸展示区已隐藏；
    #   pool_rows / pool_total        —— v2 池选展示区已隐藏；
    #   decision_lines                —— 决策层已整体删除。
    # 2026-09-16 按用户决策「🎯 标记与回马枪都删除」移除的字段（需复原见 git 历史）：
    #   comeback_rows / show_comeback —— 回马枪展示区（其排序「comeback_sort_key」）；
    #   nextday_mark                  —— 🎯 行尾标记 map（判定 is_nextday_marked）；
    #   adj_picks                     —— 动态推荐序列（语义完全由 🎯/回马枪构成）。
    # 2026-09-21 按用户决策「终选参考区整体删除」移除的字段（需复原见 git 历史）：
    #   final_pick_lines              —— 终选参考区文本行（scanner/final_pick.py 整模块
    #                                    与 scanner/decision.py 同批删除）。
    # 走势美感标记（2026-09-09 上线 / 2026-09-15 分档）：{(symbol, category): ""|"稳"|"稳★"}，
    # v1 池选行行尾渲染（_entry_row_suffix beauty 参数）。纯展示预判（判定单源
    # trend_beauty.beauty_mark：日线准入+分时分级），不改过滤/排序/落库。
    # 2026-09-14：v2 池选展示区已隐藏，故现状只服务 v1 池选行。
    beauty_mark: dict[tuple[str, str], str] | None = None
    # 妖股名单标记（2026-09-30 新增）：{(symbol, category): "" | "妖"}。
    # **静态名单匹配，不是信号** —— 该票是否在 config_scoring.GUXING_WATCHLIST 内。
    # 实测名单无样本外预测力（walk-forward 0/17），故它不进 push_gate、不改排序/评分/
    # 落库。幸存者偏差结构性存在：只能收录仍在监控池的票，名单随情绪周期整体换血。
    guxing_mark: dict[tuple[str, str], str] | None = None
    # 沪深飙升·极有可能大涨独立区（2026-09-11 自 rts-xueqiu 合入）：HotCandidate 列表。
    # 与主线（创业板/next_day 口径）完全解耦——样本面更宽（沪深主板+创业板）、口径为
    # 「当日 momentum + 榜单热度跃升」，不参与主线评分/档位/🎯。
    # 终端与飞书**都画**（feishu build_feishu_card 的「◆ 沪深飙升」节），但不进去重键
    # （分钟级刷新，计入会击穿 FEISHU_MIN_INTERVAL 节流，见 feishu._view_symbols）。
    # None = 本轮未启用或无结果（渲染时整区跳过，不留空表）。
    hot_rows: list | None = None
    # 沪深飙升区 **B 段「榜外异动」**（2026-09-18）：OffboardCandidate 列表 —— 候选来自
    # 全市场快照里的**榜外创业板**（T1 量先动·价未动 / T2 启动首日），与 A 段（`hot_rows`）
    # **同区不同段、不混排**（A 段的复合分含 rank_change 35/100，榜外票恒缺该项）。
    # 单列一个字段而不是并进 hot_rows：摘要按 `rank_change` 统计跃升数，B 段该量为 None
    # （结构性缺失），混在一起会让摘要的判断条件直接 TypeError。
    # 与 hot_rows 同款：终端与飞书都画、不落主线库、不进去重键。
    offboard_rows: list | None = None
    # 沪深飙升区 **「榜内异动」段**（2026-09-30）：OffboardCandidate 列表 —— 候选来自
    # **榜内创业板**，只看 T1「量先动·价未动」（`onboard_anomaly`）。
    # 填的是 A 段（要求「已涨+热度跃升」）与 B 段（要求「没上榜」）之间的空档：
    # 「刚上榜、涨幅还小、量已经动了」这一档。与 A 段**同区不同段、不混排**（同 B 段理由）。
    # 与 hot_rows/offboard_rows 同款：终端画、不落主线库、不进去重键；落库只进本段自己的
    # `onboard_anomaly_log`（observe-first 的唯一证据来源）。
    onboard_rows: list | None = None
    # 「v1 回捞」独立区（2026-09-16 上线）：HistCandidate 列表，回答「前 N 个交易日
    # 进过 v1 的票，今天回调到位了没」。与 v1 池选区**样本域互斥**（默认剔除今日已推荐票），
    # 与 hot_rows 同款：终端与飞书都画、不落库、不参与任何主线口径，也不进去重键。
    # None / 空列表 = 本轮无结果（渲染时整区跳过，不留空表）。
    hist_rows: list | None = None
    # 展示层资金流出硬门（2026-09-14）剔除的行数：主力净占比 ≤ FUND_OUTFLOW_NET_PCT
    # 的票不进任何展示区（v1 池选 / v1 回捞 / 沪深飙升 A·B 段），终端与飞书同源。
    # 纯展示层过滤——不改 excluded、不落库，回测/归因样本口径不受影响。
    flow_filtered: int = 0
    # 大盘指数涨幅（创业板指 pct），供 sector suggestion 展示；None = 取数失败。
    market_idx_pct: float | None = None
    # 2026-09-21 按用户决策「终端只留四个区块」移除的字段（需复原见 git 历史）：
    #   summary —— 综合判断摘要（[结论行, 明细行...] ≤4 行；2026-09-17 由单行 str
    #              「推荐X、Y」改为多行「分区体检报告」，本次整块删除）。
    # 至此终端与飞书渲染的区块均为「各自独立的表」，没有任何跨区结论行或合池排名。
