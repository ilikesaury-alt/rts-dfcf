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

import time
from dataclasses import dataclass, field

import requests
import wcwidth

from scanner.config import (
    FEISHU_KEYWORD,
    FEISHU_MIN_INTERVAL,
    FEISHU_TOP_N,
    FEISHU_WEBHOOK,
    now_beijing,
)
from scanner.display import ScanView
from scanner.log_utils import log_event
from scanner.signals import fund_flow_signal, split_risk_flags
from scanner.utils import EXTERNAL_FAILURES, to_float

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
      empty    — 本轮无任何可展示票（不推空卡片）
      cooldown — 内容无变化且距上次推送不足 FEISHU_MIN_INTERVAL
      ok       — 允许推送（内容有变化，或超时后重新推送）
    """

    push: bool
    reason: str


# 模块级默认状态单例；unified_scanner 调用 push_feishu() 即走它。
_DEFAULT_STATE = PushState()


def should_push(state: PushState, symbols: set[str], now: float) -> Decision:
    """纯函数决策：此刻是否应推送。

    不触碰 I/O、不读模块级 global，便于单元测试（见 tests/test_feishu.py）。
    与原 push_feishu 的节流/去重/空池语义完全等价：
      - webhook 缺失 → disabled
      - 无票         → empty
      - 票集未变且冷却中 → cooldown
      - 其余（票集变化 / 超时后重推）→ ok
    """
    if not FEISHU_WEBHOOK:
        return Decision(False, "disabled")
    if not symbols:
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


def _pad_vis(s: str, width: int) -> str:
    """按显示宽度补空格（中文全角按 2 列，与 display._pad 同口径）。

    2026-08-20 修复：原 `{s.name:<8}` 按字符数补位，3 字中文名（6 列）与 4 字名（8 列）
    在飞书等宽字体下错位。"""
    pad = max(0, width - sum(max(0, wcwidth.wcwidth(ch)) for ch in s))
    return f"{s}{' ' * pad}"


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
        mark = {"strong_in": "🟢🟢", "in": "🟢", "out": "🔴", "strong_out": "🔴🔴"}.get(fund_flow_signal(s.ff_pct))
        if mark:
            extra_parts.append(mark)
    if s.zt_lb:
        extra_parts.append(f"📈{s.zt_lb}板")
    extra_str = (" " + " ".join(extra_parts)) if extra_parts else ""
    # 操作纪律标签在反引号定宽块之外追加——不破坏 _pad_vis 列对齐（审查修复）
    tactic_str = (" " + " ".join(s.tactic_tags)) if s.tactic_tags else ""
    return f"`{rs} {_pad_vis(s.name, 8)} {s.symbol} {pct_str:>7} {acc_str:>7}  {s.score:>2}分{risk_str}{extra_str}`{tactic_str}"


def _row_line(entry, view, rank=None, accum=None, score=None) -> str:
    """把一条推荐行渲染成卡片文本行（rank/accum/score 可由调用方直接给最终值）。"""
    snap = _extract_row(entry, view.flow_pct_map)
    if rank is not None:
        snap.rank = rank
    if accum is not None:
        snap.accum = accum
    if score is not None:
        snap.score = score
    return _fmt_row(snap)


def build_feishu_card(view: ScanView, gem_total: int, filtered_large_cap: int = 0, top_n: int = FEISHU_TOP_N) -> dict:
    """从 ScanView 构建飞书卡片（与终端共用同一份选择）。

    2026-08-29：此前 _build_card 读「本轮候选桶」（new_faces/momentum/...），终端
    display_priority 读「DB 当日累计推荐」——同一只票可能一边排第 1、另一边不出现。
    现统一由 build_scan_view 供数；回马枪/核心低吸区是否出现也跟随终端门控
    （view.show_comeback / show_core_dip），保证「终端看得到什么，卡片就推什么」。

    top_n 默认 FEISHU_TOP_N，与 _view_symbols 共用同一常量，去重集合与展示条数永不同源漂移。
    """
    now = now_beijing().strftime("%H:%M")
    main = view.main_rows[:top_n]
    pool_rows = (view.pool_rows or [])[:top_n]
    # 池选计数用全量值（view.pool_total，2026-09-03）：view.pool_rows 已被展示层截到
    # 前 V2_POOL_DISPLAY_TOP 行，头部「池选 N 只」若用 len(pool_rows) 会失真。
    pool_count = view.pool_total if view.pool_total else len(pool_rows)

    env_tag = " | 🔴大盘弱势·谨慎" if view.weak else ""
    pool_n = f" + 池选 {pool_count}" if pool_count else ""
    header_text = f"**{now}** | 优选 {len(main)}{pool_n} 只{env_tag}"
    elements: list[dict] = [{"tag": "div", "text": {"tag": "lark_md", "content": header_text}}]

    sections: list[tuple[str, list[str]]] = []
    pool_lines = [
        _row_line(row.entry, view, rank=row.rank, accum=row.accum, score=_to_score(row.score)) for row in main
    ]
    if pool_lines:
        sections.append(("◆ 策略优选池", pool_lines))
    if pool_rows:
        _pool_top = len(pool_rows)
        _pool_cnt = f"（前{_pool_top}/共{pool_count}只）" if pool_count > _pool_top else ""
        sections.append(
            (
                f"◆ v2 池选{_pool_cnt}",
                [
                    _row_line(row.entry, view, rank=row.rank, accum=row.accum, score=_to_score(row.score))
                    for row in pool_rows
                ],
            )
        )
    if view.show_comeback:
        sections.append(("◆ 回马枪", [_row_line(e, view) for e in view.comeback_rows]))
    if view.show_core_dip:
        sections.append(("◆ 核心方向低吸", [_row_line(e, view) for e in view.core_dip_rows]))

    rendered = False
    for title, lines in sections:
        if not lines:
            continue
        elements.append({"tag": "hr"})
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": f"**{title}**\n" + "\n".join(lines)}})
        rendered = True
    if rendered:
        elements.append({"tag": "hr"})

    # 降级告警（regime 判定失败 / 优选池构建中断等）与终端同源可见，避免静默降级。
    if view.warnings:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "⚠ " + "；".join(view.warnings)}})

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
    """卡片实际会展示的票（与 build_feishu_card 的分节门控一致）。

    取自 main_rows[:FEISHU_TOP_N]（与卡片展示条数同源），避免此前「卡片推了
    但去重没算到」的双处硬编码 drift。
    """
    syms = {row.entry["symbol"] for row in view.main_rows[:FEISHU_TOP_N]}
    syms |= {row.entry["symbol"] for row in (view.pool_rows or [])[:FEISHU_TOP_N]}
    if view.show_comeback:
        syms |= {e["symbol"] for e in view.comeback_rows}
    if view.show_core_dip:
        syms |= {e["symbol"] for e in view.core_dip_rows}
    return syms


# ═══════════════════════════════════════════════════════════════════════════
# ── 传输与编排 ──
# ═══════════════════════════════════════════════════════════════════════════


def _post_card(card: dict) -> tuple[bool, str | None]:
    """POST 卡片到飞书 webhook。返回 (成功, 失败原因/None)。

    仅对连接/超时类外部故障（EXTERNAL_FAILURES）重试 1 次（退避 1s）；
    已收到飞书响应（含非 0 code）不重试——重试会重复推送两张卡片。
    编程错误（NameError/TypeError）不捕，冒泡到 unified_scanner 主循环记录完整 traceback。
    """
    last_err: BaseException | None = None
    for attempt in range(2):  # 首次 + 1 次退避重试
        try:
            resp = requests.post(FEISHU_WEBHOOK, json={"msg_type": "interactive", "card": card}, timeout=10)
            result = resp.json()
            if result.get("code") != 0:
                return False, f"飞书返回非 0: {result.get('msg')}"
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
    decision = should_push(state, current_symbols, now)
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
        return True
    except EXTERNAL_FAILURES as e:
        # 2026-08-29：原为裸 except Exception——会把编程错误（NameError/TypeError）
        # 一并吞成「推送异常」，与 AGENTS.md 的收口要求一致改为只捕外部故障；
        # 编程错误冒泡到 unified_scanner 主循环记录完整 traceback。
        print(f"\n  [!] 飞书推送异常: {e}")
        log_event(f"push exception: {e}")
        return False
