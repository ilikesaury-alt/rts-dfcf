"""飞书卡片推送。

单文件按职责分四层，自上而下：
  # ── 推送决策 ──   PushState / Decision / should_push（纯函数，无全局可变状态）
  # ── 字段提取 ──   RowSnapshot / _extract_row（回退链收口，单一取值真相）
  # ── 卡片渲染 ──   _fmt_row / build_feishu_card（纯函数，不触碰 I/O）
  # ── 传输与编排 ── _post_card（HTTP + 退避重试）/ push_feishu（取状态→决策→渲染→发送→回写）

与终端同源：卡片与终端共用同一个 ScanView（由 display.build_scan_view 供数），
共用同一套信号语义（scanner.signals fund_flow_signal / split_risk_flags，阈值集中在
config.py）。依赖方向收紧为：feishu → display(数据契约 ScanView) + signals(信号语义)，
不再「推送依赖终端渲染」。

2026-08 重构：拆出 should_push/RowSnapshot/_post_card，节流状态收进 PushState
（模块级 _DEFAULT_STATE 单例），消除原有 _last_push_time/_last_push_symbols 两个
散落 global；_view_symbols 与 build_feishu_card 共用 FEISHU_TOP_N，门控与去重不再双处硬编码。
"""

import hashlib
import time
from dataclasses import dataclass, field

import requests

from scanner.config import (
    FEISHU_KEYWORD,
    FEISHU_MIN_INTERVAL,
    FEISHU_TOP_N,
    FEISHU_WEBHOOK,
    FUND_OUTFLOW_NET_PCT,
    HOT_HIGHLIGHT_STREAK,
    now_beijing,
)

# 文本宽度与单位格式化的单源在 scanner.view.model（由 display 聚合器透传）：
#   _pad   —— 按**可见宽度**补位（CJK、★ 双宽按 2 列）；本模块此前自持一份只支持左对齐的
#            _pad_vis，并在飙升行里与 f-string 的**字符数**补位（`{x:>10}`）混用，导致
#            "亿手/万手/★"这类双宽单元把行撑宽 —— 实测同批三行可见宽 93/97/97（2026-09-15 修复）。
#   _trunc —— 可见宽度感知截断，保证超长名称不撑破定宽列（同样只在补位/截断这一层做一次）。
#   _fmt_hot_amount / _fmt_hot_volume_hand —— 飙升区单位格式化；此前 render 与本模块各持一份，
#            本模块那份的 docstring 自承「与 render._fmt_hot_amount 同口径」= 本仓禁忌的复制，
#            现改为单源引用（两处口径不可能再漂移）。
from scanner.display import ScanView, _fmt_hot_amount, _fmt_hot_volume_hand, _pad, _trunc
from scanner.log_utils import log_event
from scanner.signals import fund_flow_signal, split_risk_flags
from scanner.utils import EXTERNAL_FAILURES, to_float
from scanner.view.assemble import _market_suggestion_text

# B 段小标题括号正文单源（2026-09-22）：与终端 _render_hot_watch_region 逐字共用一份。
# 此前本模块自持一份 09-21 层序翻转前的旧文案（写成「排序=量比→主力净占比·
# T1 量先动/T2 启动首日」，与实际的 T2 在前**相反**），且漏了「开盘静默窗」——
# 与上方 _pad/_trunc 同属本仓禁忌的复制，改为单源引用。
from scanner.view.model import offboard_subtitle

# ═══════════════════════════════════════════════════════════════════════════
# ── 推送决策 ──
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class PushState:
    """节流状态：上次推送时间 + 上次推送的票集（去重用）。

    原本是 feishu.py 里两个散落 global（_last_push_time / _last_push_symbols），
    测试需 monkeypatch、状态不可观测。收进 dataclass 后由 push_feishu 持有，
    可注入自定义实例做单元测试；模块级 _DEFAULT_STATE 维持「调用点零改动」。
    """

    last_time: float = 0.0
    last_symbols: set[str] = field(default_factory=set)


@dataclass
class Decision:
    """should_push 的结论。

    reason ∈ {disabled, empty, cooldown, ok}：
      disabled — 未配置 webhook，整体不推
      empty    — 卡片无任何区块可画（不推空卡片）
      cooldown — 内容无变化且距上次推送不足 FEISHU_MIN_INTERVAL
      ok       — 允许推送（内容有变化，或超时后重新推送）
    """

    push: bool
    reason: str


# 模块级默认状态单例；unified_scanner 调用 push_feishu() 即走它。
_DEFAULT_STATE = PushState()


def should_push(state: PushState, symbols: set[str], now: float, *, has_content: bool | None = None) -> Decision:
    """纯函数决策：此刻是否应推送。

    不触碰 I/O、不读模块级 global，便于单元测试（见 tests/test_feishu.py）。
    与原 push_feishu 的节流/去重/空池语义等价：
      - webhook 缺失     → disabled
      - 无内容可画       → empty
      - 票集未变且冷却中 → cooldown
      - 其余（票集变化 / 超时后重推）→ ok

    两个入参回答**两个不同的问题**，不可混为一谈：
      symbols     —— 去重键（「内容变了没」），只含**主线**票集（`_view_symbols`）。
      has_content —— 卡片**是否有任何区块可画**（`view_has_content`）。
    默认 None = `bool(symbols)`，即「以票集判空」的旧行为，供不关心第三区的调用点沿用。

    2026-09-15 修：此前只有 symbols 一个判据，于是「有推荐但被展示层门全剔 ⇒ main_rows 空，
    而终选参考/飙升区仍有内容」会落到 empty，**整张卡片不推** —— 而终端在**同一份 view** 上
    照画那两个区块（与 build_feishu_card 声明的「与终端分节一一对应」矛盾）。
    现改为按**内容**判空（`view_has_content`，含该场景的实测证据与边界说明）。
    注意飙升区**不进** symbols（成因见 _view_symbols）：该情形下票集恒为空 ⇒ has_change
    恒为 False ⇒ 命中「票集未变且冷却中」，按 FEISHU_MIN_INTERVAL 节流重推，
    与主线非空时的既有推送节奏完全一致（而非每轮一张）。
    """
    if not FEISHU_WEBHOOK:
        return Decision(False, "disabled")
    if has_content is None:
        has_content = bool(symbols)
    if not has_content:
        # 全空推荐时不推空卡片（无推荐时段会每 5 分钟刷一张空卡，2026-08-17 审查修复）。
        return Decision(False, "empty")
    has_change = symbols != state.last_symbols
    if not has_change and (now - state.last_time) < FEISHU_MIN_INTERVAL:
        return Decision(False, "cooldown")
    return Decision(True, "ok")


# ═══════════════════════════════════════════════════════════════════════════
# ── 字段提取（回退链收口）──
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class RowSnapshot:
    """一条推荐行的展示快照：把 _row_* 六个回退链的结果收口成单一对象。

    此前 _fmt_row 接受 10 个位置参数，调用点可读性差、加字段成本高；现在
    提取与渲染解耦——_extract_row 负责「从 entry/candidate 取值」，_fmt_row
    只负责「把快照画成一行文本」。这些回退链必须与终端 _print_priority_row
    同口径（2026-08-29 收口：此前飞书直接读 Candidate、终端读 DB 行 + 实时覆盖，
    两端对「涨幅/排名/累计」的取值可能不同）。
    """

    symbol: str = ""
    name: str = ""
    rank: int | None = None
    pct: float = 0.0
    accum: float | None = None
    score: int = 0
    risk_flags: list[str] = field(default_factory=list)
    ff_pct: float | None = None
    zt_lb: int | None = None
    tactic_tags: list[str] = field(default_factory=list)


def _row_percent(entry) -> float:
    """涨幅：实时行情 > 推荐时落库值。live_percent=0.0 是合法的 0.00%，不能当缺失。"""
    lp = entry.get("live_percent")
    return to_float(lp) if lp is not None else to_float(entry.get("percent"))


def _row_rank(entry):
    """排名：实时(live_rank/rank_map) > 候选快照(rank) > None（渲染为 —）。"""
    r = entry.get("live_rank") or entry.get("rank")
    if r:
        return r
    c = entry.get("_candidate")
    if c is not None and getattr(c, "stock", None) and c.stock.rank:
        return c.stock.rank
    return None


def _row_accum(entry):
    """5 日累计：候选快照 > 推荐时落库值。"""
    c = entry.get("_candidate")
    if c is not None and getattr(c, "kline", None) and c.kline.accumulated_pct is not None:
        return c.kline.accumulated_pct
    return entry.get("accumulated_pct")


def _row_ff_pct(entry, flow_pct_map):
    """主力净占比：候选扫描维度 > DB 当日快照（与终端资金流图标同回退链）。"""
    c = entry.get("_candidate")
    if c is not None and getattr(c, "kline", None) and c.kline.dimensions:
        v = c.kline.dimensions.get("fund_flow_main_pct")
        if v is not None:
            return to_float(v, default=None)
    v = flow_pct_map.get(entry["symbol"])
    return to_float(v, default=None) if v is not None else None


def _row_zt(entry):
    """连板数（无数据返回 None）。"""
    c = entry.get("_candidate")
    if c is not None and getattr(c, "kline", None) and c.kline.dimensions:
        return c.kline.dimensions.get("zt_lianban")
    return None


def _row_risk(entry) -> list[str]:
    """风险标签（仅候选行有；掉榜/重启行返回空列表）。"""
    c = entry.get("_candidate")
    return list(c.risk_flags) if c is not None and getattr(c, "risk_flags", None) else []


def _to_score(value) -> int:
    """评分转整数。

    DB 的 score 列理论上是 INTEGER，但历史行可能为 NULL 或被上游写坏。转不出来按 0
    处理——只影响卡片上的一个数字，不值得让整次推送失败。

    只捕类型转换异常（TypeError/ValueError）：to_float 已用 math.isfinite 兜住
    None/NaN/±inf/不可解析值，此处仅兜 int() 自身的残余边角；编程错误照旧冒泡
    （AGENTS.md：不得裸 except Exception）。
    """
    try:
        return int(to_float(value) or 0)
    except (TypeError, ValueError):
        return 0


def _extract_row(entry, flow_pct_map) -> RowSnapshot:
    """从推荐行（entry + _candidate）按回退链收口成 RowSnapshot。"""
    c = entry.get("_candidate")
    return RowSnapshot(
        symbol=entry["symbol"],
        name=entry["name"],
        rank=_row_rank(entry),
        pct=_row_percent(entry),
        accum=_row_accum(entry),
        score=_to_score(entry.get("score")),
        risk_flags=_row_risk(entry),
        ff_pct=_row_ff_pct(entry, flow_pct_map),
        zt_lb=_row_zt(entry),
        tactic_tags=list(getattr(c, "tactic_tags", [])) if c else [],
    )


# ═══════════════════════════════════════════════════════════════════════════
# ── 卡片渲染 ──
# ═══════════════════════════════════════════════════════════════════════════


# 资金流档位 → 卡片 emoji。终端那份是 **ANSI 三角**（display._FUND_FLOW_ICON，▲/▼），
# 这里必须是 emoji —— 卡片是 lark_md，ANSI 色码会原样显示成乱码。
# 档位判定（fund_flow_signal）与阈值仍是**同一单源**，两份表只差「画成什么形状」。
# 2026-09-16：抽成模块级常量，此前 v1 池选行内联了一份、回捞行若再内联就是复制（本仓禁忌）。
_FUND_FLOW_EMOJI = {
    "strong_in": "🟢🟢",
    "in": "🟢",
    "out": "🔴",
    "strong_out": "🔴🔴",
}


def _fmt_row(s: RowSnapshot) -> str:
    """单行：排名 名称 代码 涨幅 5日累计 评分 [风险] [资金流/连板] [操作纪律]。"""
    rs = f"{s.rank:>3}" if s.rank else "  —"
    pct_str = f"+{s.pct:.1f}%" if s.pct >= 0 else f"{s.pct:.1f}%"
    acc_str = f"{s.accum:+.1f}%" if s.accum is not None else "N/A"
    # 风险标签分级（与终端共用 split_risk_flags，阈值集中在 config）
    hard, soft_count = split_risk_flags(s.risk_flags)
    risk_parts = []
    if hard:
        tag = f"⚠{'/'.join(hard)}"
        if soft_count:
            tag += f"+{soft_count}"
        risk_parts.append(tag)
    elif soft_count:
        risk_parts.append(f"⚠+{soft_count}")
    risk_str = (" " + " ".join(risk_parts)) if risk_parts else ""
    extra_parts = []
    if s.ff_pct is not None:
        mark = _FUND_FLOW_EMOJI.get(fund_flow_signal(s.ff_pct))
        if mark:
            extra_parts.append(mark)
    if s.zt_lb:
        extra_parts.append(f"📈{s.zt_lb}板")
    extra_str = (" " + " ".join(extra_parts)) if extra_parts else ""
    # 操作纪律标签在反引号定宽块之外追加——不破坏 _pad 列对齐（审查修复）
    tactic_str = (" " + " ".join(s.tactic_tags)) if s.tactic_tags else ""
    return f"`{rs} {_pad(s.name, 8)} {s.symbol} {pct_str:>7} {acc_str:>7}  {s.score:>2}分{risk_str}{extra_str}`{tactic_str}"


def _row_line(entry, view, rank=None, accum=None, score=None, is_new_entry: bool = False) -> str:
    """把一条推荐行渲染成卡片文本行（rank/accum/score 可由调用方直接给最终值）。

    is_new_entry：本轮新进入今日推荐池（MainRow.is_new_entry 透传），行尾打「新」标记。
    位置与终端一致 —— 插在**美 标记之前**（终端里「新」也排在行尾最前，相对顺序同为「新 … 美」）。
    """
    snap = _extract_row(entry, view.flow_pct_map)
    if rank is not None:
        snap.rank = rank
    if accum is not None:
        snap.accum = accum
    if score is not None:
        snap.score = score
    line = _fmt_row(snap)
    # 本轮新进池标记（2026-09-21）：与终端 v1 池选行同位，判定单源 MainRow.is_new_entry。
    if is_new_entry:
        line += " 新"
    # 走势美感标记（2026-09-09 上线 / 2026-09-15 分档）：v1 池选行尾 ""/"美"/"美★"，
    # 与终端同源（view.beauty_mark，判定单源 trend_beauty.beauty_mark）。
    bm = (getattr(view, "beauty_mark", None) or {}).get((entry.get("symbol"), entry.get("category")), "")
    if bm:
        line += f" {bm}"
    return line


# 飞书飙升行 = 终端 COLS_HOT 的**压缩版**列规格：卡片在手机端渲染，整体宽度收窄；
# 但**列序与列含义与终端一一对应**（改 COLS_HOT 的列集/顺序必须同改本表，守卫见
# tests/test_feishu.py::test_hot_row_columns_match_terminal）。
#
# 宽度规则 —— 每列宽度必须 ≥ 该列**格式化输出的可见宽度上界**，否则单元格溢出、
# 该行比同表其它行宽（行与行之间列才对得齐）。2026-09-15 修复的正是这条被违反：
# 旧实现把宽度写进 f-string（`{x:>10}`），补的是**字符数**，而"万手/亿手/★"是双宽单元，
# 于是同批输出行宽 93/97/97 参差。
# 数值列的预算沿用 COLS_HOT 的同名列（那里已按输出上界定过），只在自由文本/低熵列上收窄：
#   代码 12→8、名称 10→8、现价 8→7、涨幅 8→7、换手 7→6、市值 9→7、评分 6→5。
# 成交量/成交额/连击**不收窄**（11/9/5 就是格式化上界，收窄即溢出）。
# 「板块」14 也**不收窄**（2026-09-22）：与终端同宽，两出口在同一条截断线上 ——
# 收窄会让同一只票在终端显示「长三角一体化」、手机上显示「长三角一体…」，
# 同一张表两种口径（同 COLS_HIST「上次v1桶」不收窄的理由）。
# 数值列溢出时**不截断**：宁可该行错列也不显示错值；宽度已按上界定，溢出即说明
# 上界假设被改坏，tests/test_feishu.py::test_hot_row_width_is_uniform 会先红。
_COLS_HOT_FEISHU: tuple[tuple[int, str], ...] = (
    (2, "r"),  # #        展示条数 ≤ 99
    (8, "l"),  # 代码      6 位数字（终端留 12 是为旧 SZ/SH 前缀展示，卡片不需要）
    (8, "l"),  # 名称      4 个汉字；超出先 _trunc（自由文本，不保证上界）
    (7, "r"),  # 涨幅      "+10.0%"（涨停已剔除，6 列足够）
    (7, "r"),  # 5日累计   "+99.99%"
    (7, "r"),  # 现价      "9999.99"
    (5, "r"),  # 排名上升  "+" + 榜内跃升位数（榜长 ≤ 4 位）
    (11, "r"),  # 成交量   "9999.99万手"（万手分支上界）
    (9, "r"),  # 成交额    "9999.99亿"
    (5, "r"),  # 量比      "99.99"
    (6, "r"),  # 换手%     "999.9%"
    (7, "r"),  # 市值(亿)  "9999亿"（HOT_MAX_MARKET_CAP 已在筛选层过滤）
    (14, "l"),  # 板块     F10 概念名（自由文本，超出 _trunc；与终端同宽）
    (5, "r"),  # 评分      "100.0"
    (5, "r"),  # 连击      "★" + streak（与 COLS_HOT 同宽）
)


def _fmt_hot_row_feishu(c, idx: int, *, board_segment: bool = True) -> str:
    """飞书卡片单行：沪深飙升·极有可能大涨候选（lark_md 定宽块，无 ANSI）。

    单元格一律经 _pad 按**可见宽度**补位（成因见 _COLS_HOT_FEISHU 的宽度规则），
    故每行可见宽度恒为 sum(宽度)+12 个分隔空格 = 97 列（含两侧反引号共 99）。
    名称是唯一自由文本列，先 _trunc 再补位——否则 5 字名（10 列）会撑破 8 列的列宽。

    `board_segment=False` → B 段（榜外异动）行：与终端 `_hot_row_cells` 完全同口径 ——
    「排名上升/连击」榜外票结构上没有 → `—`；「评分」列改显分层标记（T1/T2）。
    「板块」两段同列同源（2026-09-22，`concept.attach_display_boards` 在产出阶段填好），
    与 v1 池选同一条回退链 ⇒ 同票同名；空值显 `—`（取数失败）而非「其他」。
    两出口必须同口径，否则同一张表在终端和手机上含义不同（守卫
    `tests/test_feishu.py::test_hot_row_columns_match_terminal` 只保列数，口径靠本注释）。
    """
    pct_str = f"+{c.percent:.1f}%" if c.percent >= 0 else f"{c.percent:.1f}%"
    accum_str = f"{c.accum_5d:+.1f}%" if c.accum_5d is not None else "—"
    if board_segment:
        rank_str = f"+{c.rank_change}"
        score_str = f"{c.score:.1f}"
        streak_str = f"★{c.streak}" if c.streak >= HOT_HIGHLIGHT_STREAK else f"{c.streak}"
    else:
        rank_str = "—"
        score_str = getattr(c, "tier", "") or "—"
        streak_str = "—"
    sector = str(getattr(c, "sector", "") or "")
    cells = (
        str(idx),
        c.code,
        _trunc(c.name, _COLS_HOT_FEISHU[2][0]),
        pct_str,
        accum_str,
        f"{c.current:.2f}" if c.current else "—",
        rank_str,
        _fmt_hot_volume_hand(c.volume),
        _fmt_hot_amount(c.amount),
        f"{c.volume_ratio:.2f}" if c.volume_ratio > 0 else "—",
        f"{c.turnover_rate:.1f}%" if c.turnover_rate > 0 else "—",
        f"{c.market_capital / 1e8:.0f}亿" if c.market_capital > 0 else "—",
        _trunc(sector, _COLS_HOT_FEISHU[12][0]) if sector else "—",
        score_str,
        streak_str,
    )
    body = " ".join(_pad(str(cell), width, align) for cell, (width, align) in zip(cells, _COLS_HOT_FEISHU, strict=True))
    return f"`{body}`"


# 飞书回捞行 = 终端 COLS_HIST 的**压缩版**列规格：与 _COLS_HOT_FEISHU 同一套规则
# （列序/列含义与终端一一对应，只在自由文本/低熵列上收窄；守卫见
# tests/test_feishu.py::test_hist_row_columns_match_terminal / test_hist_row_width_is_uniform）。
#   # 3→2、代码 12→8、名称 10→8、涨幅 8→7、5日累计 8→7、现价 8→7、
#   量比 6→5、距v1 6→4、评分 5→4；**「上次v1桶」14 不收窄** —— 它要放
#   `known_new_face` / `early_momentum`（14 个 ASCII 列）这类真实桶名，
#   终端同为 14（2026-09-16 由 12 调到 14：12 会让这类桶名在同一区内错列）。
_COLS_HIST_FEISHU: tuple[tuple[int, str], ...] = (
    (2, "r"),  # #         展示条数 ≤ 99
    (8, "l"),  # 代码       6 位数字
    (8, "l"),  # 名称       4 个汉字；超出先 _trunc（自由文本，不保证上界）
    (7, "r"),  # 涨幅       "+10.0%"（涨停已由量比/回调门过滤，6 列足够）
    (7, "r"),  # 5日累计    "+99.99%"
    (7, "r"),  # 现价       "9999.99"
    (5, "r"),  # 量比       "99.99"
    (4, "r"),  # 距v1       "9日"
    (4, "r"),  # 评分       "100"
    (14, "l"),  # 上次v1桶  "known_new_face"
)


def _fmt_hist_row_feishu(c, idx: int) -> str:
    """飞书卡片单行：「v1 回捞」候选（lark_md 定宽块，无 ANSI）。

    取值与终端 _render_hist_watch_region **同源同式**：回调/量比缺失显示「—」而不是
    0.00（0 与「没拿到」是两回事），累计涨幅为 0 也显示「—」（此时「自 v1 累计」等于
    当日涨幅，终端同样不重复显示）。
    """
    pct_str = f"+{c.percent:.2f}%" if c.percent >= 0 else f"{c.percent:.2f}%"
    accum_str = f"{c.accum_5d:+.1f}%" if c.accum_5d is not None else "—"
    cells = (
        str(idx),
        c.code,
        _trunc(c.name, _COLS_HIST_FEISHU[2][0]),
        pct_str,
        accum_str,
        f"{c.current:.2f}" if c.current else "—",
        f"{c.vol_ratio:.2f}" if c.vol_ratio > 0 else "—",
        f"{c.rec_days_ago}日",
        f"{c.score:.0f}",
        c.rec_category,
    )
    body = " ".join(
        _pad(str(cell), width, align) for cell, (width, align) in zip(cells, _COLS_HIST_FEISHU, strict=True)
    )
    return f"`{body}`"


def _marks_tail_card(ff_pct, beauty: str) -> str:
    """独立观察区（沪深飙升 / v1 回捞）的行尾标记（卡片成形）：资金流 emoji + 日线美感「美」。

    与终端 `render._watch_tail_terminal` 是**同判定、不同形状**（终端 ▲/▼ 带 ANSI，
    卡片 emoji）—— 与主线 `_entry_row_suffix` / `_fmt_row` 的分工完全一致，
    判定源都是 `signals.fund_flow_signal` / `display_gates.beauty_marks_daily`，本函数不重算。
    两个独立区共用本函数，免得「同一个标记在两个节里各画一遍、其中一个少个空格」。

    追加位置在反引号**之外**，故不改变定宽块宽度（`_fmt_hot_row_feishu` / `_fmt_hist_row_feishu`
    仍各自恒宽，守卫 test_hot_row_width_is_uniform / test_hist_row_width_is_uniform 量的就是它们）。

    结构性上限（两个区都有）：🔴🔴 不可达（≤-8% 已被通用门剔除）、美★ 不可达（无分时数据）。
    """
    parts: list[str] = []
    if ff_pct is not None:
        mark = _FUND_FLOW_EMOJI.get(fund_flow_signal(ff_pct))
        if mark:
            parts.append(mark)
    if beauty:
        parts.append(beauty)
    return (" " + " ".join(parts)) if parts else ""


def build_feishu_card(view: ScanView, gem_total: int, filtered_large_cap: int = 0, top_n: int = FEISHU_TOP_N) -> dict:
    """从 ScanView 构建飞书卡片（与终端共用同一份选择）。

    2026-08-29：此前 _build_card 读「本轮候选桶」（new_faces/momentum/...），终端
    display_priority 读「DB 当日累计推荐」——同一只票可能一边排第 1、另一边不出现。
    现统一由 build_scan_view 供数，保证「终端看得到什么，卡片就推什么」。

    分节口径（2026-09-21 同步）：卡片画 v1 池选 → v1 回捞 → 沪深飙升·极有可能大涨，
    与终端 render_terminal 的区块**顺序与条件**一一对应。按用户决策累计移除的区块：
    **决策层**（2026-09-14 整体删除）、**终选参考**（2026-09-21 整体删除）、
    **v2 池选** 与 **核心方向低吸**（2026-09-14 隐藏）。**回马枪（comeback）两处都没有展示区**
    （ca91d21 起移除，见 docs/CORE-FLOW.md §十-1），故它既不是分节门控、也不进
    `_view_symbols` 去重集合。

    ⚠ 本函数的**区块条件**（哪些节画得出来）是 `view_has_content` 的对齐基准：
    两边必须逐条等价，否则会出现「有内容却不推」或「推一张空卡」。
    注意不要把「画得出」与「参与去重」混为一谈 —— 飙升区画得出但不参与去重（见 `_view_symbols`）。

    top_n 默认 FEISHU_TOP_N，与 _view_symbols 共用同一常量，去重集合与展示条数永不同源漂移。
    """
    now = now_beijing().strftime("%H:%M")
    main = view.main_rows[:top_n]

    env_tag = " | 🔴大盘弱势·谨慎" if view.weak else ""
    _suggestion = _market_suggestion_text(
        getattr(view, "weak", None),
        getattr(view, "market_idx_pct", None),
    )
    header_text = f"**{now}** | 优选 {len(main)} 只{env_tag} | {_suggestion}"
    elements: list[dict] = [{"tag": "div", "text": {"tag": "lark_md", "content": header_text}}]

    sections: list[tuple[str, list[str]]] = []
    # 终选参考节（2026-09-04 上线）已于 2026-09-21 按用户决策**整体删除** —— 连同
    # scanner/final_pick.py（含市况门 scanner.decision.market_gate）与 ScanView.final_pick_lines
    # 字段。卡片现状与终端同步只剩四节：v1 池选 / v1 回捞 / 沪深飙升（A 段）/ 榜外异动（B 段）。
    # 需复原见 git 历史。
    pool_lines = [
        _row_line(
            row.entry,
            view,
            rank=row.rank,
            accum=row.accum,
            score=_to_score(row.score),
            is_new_entry=bool(getattr(row, "is_new_entry", False)),
        )
        for row in main
    ]
    if pool_lines:
        sections.append(("◆ v1 池选", pool_lines))
    # 「◆ v2 池选」与「◆ 核心方向低吸」两个分节已于 2026-09-14 按用户决策隐藏
    # （与终端 render_terminal 同步移除）。需复原见 git 历史。

    # ── v1 回捞 独立区（与终端 _render_hist_watch_region 同源）──
    # 与 v1 池选区样本域互斥（默认剔除今日已推荐票），故两节并存不会出现「同票两种结论」。
    hist_rows = getattr(view, "hist_rows", None)
    if hist_rows:
        hist_lines = [
            f"{_fmt_hist_row_feishu(c, i)}{_marks_tail_card(c.ff_pct, c.beauty)}" for i, c in enumerate(hist_rows, 1)
        ]
        sections.append(("◆ v1 回捞", hist_lines))

    # ── 沪深飙升·极有可能大涨 独立区（与终端 _render_hot_watch_region 同源）──
    # 2026-09-18 起含两段：A 段榜内飙升（hot_rows）+ B 段榜外异动（offboard_rows），
    # 同节两段并列、不混排（成因见 model.ScanView.offboard_rows 的注释）。
    hot_rows = getattr(view, "hot_rows", None)
    offboard_rows = getattr(view, "offboard_rows", None)
    if hot_rows or offboard_rows:
        # 行尾标记（2026-09-16）：与 v1 回捞区共用 _marks_tail_card —— 标记是跨展示区
        # 通用的，此前只有回捞区画、飙升区不画，是同一条判定在两个出口给了两种待遇。
        hot_lines = [
            f"{_fmt_hot_row_feishu(c, i)}{_marks_tail_card(c.ff_pct, c.beauty)}" for i, c in enumerate(hot_rows or [], 1)
        ]
        if offboard_rows:
            # B 段小标题与终端同款：候选来源 + 排序键 + 「未回测」一个都不能省 ——
            # 否则最自然的误读就是「它与 A 段一样是热度跃升」，而证据强度完全不同。
            hot_lines.append(f"— 榜外异动{offboard_subtitle(len(offboard_rows))}")
            hot_lines += [
                f"{_fmt_hot_row_feishu(c, i, board_segment=False)}{_marks_tail_card(c.ff_pct, c.beauty)}"
                for i, c in enumerate(offboard_rows, 1)
            ]
        sections.append(("◆ 沪深飙升 · 极有可能大涨", hot_lines))

    rendered = False
    for title, lines in sections:
        if not lines:
            continue
        elements.append({"tag": "hr"})
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": f"**{title}**\n" + "\n".join(lines)}})
        rendered = True
    if rendered:
        elements.append({"tag": "hr"})

    # 降级告警（regime 判定失败等）与终端同源可见，避免静默降级。
    # 展示层资金流出硬门（2026-09-14）同理：卡片少了几只，必须说明为什么（与终端同源，
    # 过滤本身在 build_scan_view 一处完成，本处只做告知）。
    _notes = list(view.warnings)
    # getattr 兜底：轻量 view 桩（测试/回放）可能只实现部分字段，
    # 缺 flow_filtered 时按「未过滤」处理，不因此抛错。
    _flow_filtered = getattr(view, "flow_filtered", 0)
    if _flow_filtered:
        _notes.append(f"资金流出已剔除 {_flow_filtered} 只（主力净占比 ≤ {FUND_OUTFLOW_NET_PCT:.0f}%）")
    if _notes:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "⚠ " + "；".join(_notes)}})

    elements.append(
        {
            "tag": "note",
            "elements": [
                {
                    "tag": "plain_text",
                    "content": f"创业板共{gem_total}只"
                    + (f" | 过滤{filtered_large_cap}只" if filtered_large_cap else ""),
                }
            ],
        }
    )

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"🧧 {FEISHU_KEYWORD} 扫描简报"},
            "template": "indigo",
        },
        "elements": elements,
    }


def _view_symbols(view: ScanView) -> set[str]:
    """推送**去重键**：卡片里「内容稳定」的那部分票集 == main_rows[:FEISHU_TOP_N]。

    取自 main_rows[:FEISHU_TOP_N]（与 build_feishu_card 的分节门控同源），避免
    「卡片推了但去重没算到」的双处硬编码 drift；两者共用 FEISHU_TOP_N，改一处即两处同时生效。

    ⚠ 本函数只回答「内容变没变」，**不回答「有没有内容」** —— 后者是 view_has_content。
    故凡是「卡片画得出、但变动频率与主线不同量级」的区块，必须与 build_feishu_card
    同步**排除**；否则 should_push 会把「卡片画不出的票变了」判成票集变化，
    每轮都返回 ok，把 FEISHU_MIN_INTERVAL 直接击穿（should_push 只在「票集未变」时查冷却）。

    2026-09-14：**移除 comeback 分支**（原先按 view.show_comeback 并入
    view.comeback_rows）。回马枪自 ca91d21（2026-09-02）起终端与卡片两处展示区均已
    移除（见 docs/CORE-FLOW.md §十-1），把它算进去会让「仅回马枪票变化」触发一次
    内容毫无回马枪的推送。

    2026-09-14（同日第二批）：**移除 pool_rows 与 core_dip 两分支** —— v2 池选与
    核心方向低吸两个展示区按用户决策隐藏，卡片不再画这两节。

    2026-09-15：**刻意不并入 hot_rows（飙升区）**。飙升区的涨幅/排名/量比是**分钟级**
    刷新，且榜单本身每轮都可能换人 ⇒ 若计入，`symbols != state.last_symbols` 几乎每轮
    成立 ⇒ should_push 绕过冷却、每轮（60s）推一张，把节流打回 5min 的 1/5。
    飙升区因此只参与 view_has_content（决定「空池要不要推」），不参与去重（决定
    「多久推一次」）—— 二者是两件事，见 should_push 的入参说明。

    2026-09-16：**同样刻意不并入 hist_rows（v1 回捞区）**，理由与飙升区同构 ——
    该区的「今日涨幅/量比」是分钟级刷新，且候选集来自前 N 个交易日的 v1 产出、
    与 main_rows **样本域互斥**（`exclude_symbols` 默认剔除今日已推荐票）：
    把它计入只会在主线票集不变时凭空制造 has_change，每轮（60s）推一张卡。
    回捞区因此也只参与 view_has_content。
    """
    return {row.entry["symbol"] for row in view.main_rows[:FEISHU_TOP_N]}


def view_has_content(view: ScanView) -> bool:
    """卡片此刻**是否画得出任何区块**（判「空卡片」的唯一判据）。

    必须与 build_feishu_card 的区块条件逐条对齐，否则会出现「明明有内容却不推」
    （本函数漏判）或「推了一张空卡」（本函数多判）。当前四个来源：
      1. view.main_rows[:FEISHU_TOP_N] —— v1 池选节；
      2. view.hist_rows               —— v1 回捞独立区（2026-09-16 上线）；
      3. view.hot_rows                —— 沪深飙升独立区（A 段榜内飙升）；
      4. view.offboard_rows           —— 沪深飙升独立区的 B 段（榜外异动，2026-09-18 上线）
    公开（非 `_` 前缀）是刻意的：它是「有没有内容」的**跨模块单源**，
    除 should_push 外还被 unified_scanner 的「推送跳过」提示复用（此前那里自持
    一份 `bool(view.main_rows)`，不认第 2、3、4 条）。

    2026-09-15 修：此前只等价于第 1 条 —— 于 `should_push` 里表现为「票集空 ⇒ empty ⇒
    整卡不推」，而终端在**同一份 view 上**照画。实测（真实 scanner.db，把展示层资金流出
    硬门置为全剔）该场景可复现且非假设：
        main_rows=0 / hot_rows=1 / flow_filtered=70
        终端画出「飙升区」，卡片分节同样是这一节；
        `_view_symbols`=∅ 但本函数=True；旧门判 empty（整卡不推），新门判 ok。
    ⚠ 边界（别把结论说满）：`build_scan_view` 在 `today_recs` 为空时**先返回 None**，
    此时 `display_priority` 直接返回、终端也不画飙升区 —— 两出口是**一致**的（都没输出）。
    所以本函数的修复针对的是「有推荐但被展示层门剔除（资金流出硬门 / 减仓标签 / 不追涨）」
    这一类空池，不是「今日完全无推荐」。
    2026-09-21：第 1 条来源（终选参考节）随该区删除而移除 —— 少一个「有内容」来源，
    判据相应收紧（原先「只有终选参考、池选为空」也推卡的情形不复存在）。
    遗留（未动，需另行决策）：`view is None` 时 `run_hot_watch` 已算出的飙升区被静默丢弃
    （每轮白算 3~5s，两出口都看不到）—— 属 `display_priority` 的提前返回语义，改动会变更
    终端与推送行为，故留作独立议题。

    getattr 兜底：轻量 view 桩（测试/回放）可能只实现部分字段，缺字段按「该区块为空」处理。
    """
    if view.main_rows[:FEISHU_TOP_N]:
        return True
    if getattr(view, "hist_rows", None):
        return True
    if getattr(view, "offboard_rows", None):
        return True
    return bool(getattr(view, "hot_rows", None))


# ═══════════════════════════════════════════════════════════════════════════
# ── 传输与编排 ──
# ═══════════════════════════════════════════════════════════════════════════


def _webhook_fingerprint() -> str:
    """当前 webhook 的短指纹（sha256 前 8 位）：日志里只用来区分「这条记录来自哪条 webhook」。

    刻意不回显 token —— token 曾泄露于 git 历史（见 config_sources 顶部注释），
    日志不该成为新的泄露面。排查「推送到底有没有出去」时，它是第一现场证据：
    5 天前启动的进程可能带着早已失效的端点，而日志原先只有一个 msg，看不出说的是谁。
    """
    if not FEISHU_WEBHOOK:
        return "none"
    return hashlib.sha256(FEISHU_WEBHOOK.encode()).hexdigest()[:8]


def _post_card(card: dict) -> tuple[bool, str | None]:
    """POST 卡片到飞书 webhook。返回 (成功, 失败原因/None)。

    仅对连接/超时类外部故障（EXTERNAL_FAILURES）重试 1 次（退避 1s）；
    已收到飞书响应（含非 0 code）不重试——重试会重复推送两张卡片。
    编程错误（NameError/TypeError）不捕，冒泡到 unified_scanner 主循环记录完整 traceback。

    2026-09-15：失败信息补全现场（原实现只留一句 msg，排查时无从区分「飞书拒绝」与
    「根本不是飞书在回话」）。现一律带 code / http 状态 / body 片段 / webhook 指纹；
    响应非 JSON（代理或网关的错误页）时也保留状态码与正文，而不是只剩「请求异常」。
    """
    last_err: BaseException | None = None
    for attempt in range(2):  # 首次 + 1 次退避重试
        try:
            resp = requests.post(FEISHU_WEBHOOK, json={"msg_type": "interactive", "card": card}, timeout=10)
            try:
                result = resp.json()
            except ValueError:
                # 非 JSON 响应：保留重试语义不变，但把「谁在回话」写进错误里。
                last_err = ValueError(
                    f"响应非 JSON：http={resp.status_code} hook={_webhook_fingerprint()} body={resp.text[:200]!r}"
                )
                if attempt == 0:
                    time.sleep(1)
                continue
            if result.get("code") != 0:
                return False, (
                    f"飞书返回非 0: msg={result.get('msg')!r} code={result.get('code')!r} "
                    f"http={resp.status_code} hook={_webhook_fingerprint()} body={resp.text[:200]}"
                )
            return True, None
        except EXTERNAL_FAILURES as e:
            last_err = e
            if attempt == 0:
                time.sleep(1)
    return False, f"请求异常: {last_err}"


def push_feishu(
    view: ScanView | None,
    gem_total: int,
    filtered_large_cap: int = 0,
    state: PushState | None = None,
) -> bool:
    """推送飞书卡片。view 为 None（无 conn / 今日无推荐）时不推送。

    编排：取状态 → should_push 决策 → build 渲染 → _post_card 发送 →
    成功回写状态。state 默认走模块级 _DEFAULT_STATE（调用点零改动）；
    传入自定义 state 可做单元测试，不影响默认实例。
    """
    if state is None:
        state = _DEFAULT_STATE

    if view is None:
        return False

    now = time.time()
    current_symbols = _view_symbols(view)
    # has_content 与 symbols 是两个问题：前者「卡片有没有东西可画」（决定空卡片是否推），
    # 后者「内容变了没」（决定多久推一次）。飙升区只参与前者，见 _view_symbols 的成因说明。
    decision = should_push(state, current_symbols, now, has_content=view_has_content(view))
    if not decision.push:
        return False

    try:
        card = build_feishu_card(view, gem_total, filtered_large_cap)
        ok, err = _post_card(card)
        if not ok:
            print(f"\n  [!] 飞书推送失败: {err}")
            log_event(f"push failed: {err}")
            return False
        state.last_time = now
        state.last_symbols = set(current_symbols)
        # 成功也记一行（2026-09-15）：原实现只记失败，于是日志里「最后一条是 push failed」
        # 会被误读成「推送一直坏着」（实际可能只是某次失败后早已恢复）。有 ok 记录，
        # 「最后一次成功是什么时候」一眼可查，也能与失败的 hook 指纹对照。
        log_event(f"push ok: {len(current_symbols)} 票 hook={_webhook_fingerprint()}")
        return True
    except EXTERNAL_FAILURES as e:
        # 2026-08-29：原为裸 except Exception——会把编程错误（NameError/TypeError）
        # 一并吞成「推送异常」，与 AGENTS.md 的收口要求一致改为只捕外部故障；
        # 编程错误冒泡到 unified_scanner 主循环记录完整 traceback。
        print(f"\n  [!] 飞书推送异常: {e}")
        log_event(f"push exception: {e}")
        return False
