import requests
import wcwidth

from scanner.config import FEISHU_KEYWORD, FEISHU_MIN_INTERVAL, FEISHU_WEBHOOK, now_beijing
from scanner.display import ScanView, fund_flow_signal, split_risk_flags
from scanner.utils import EXTERNAL_FAILURES, to_float

_last_push_time: float = 0.0
_last_push_symbols: set[str] = set()


def _pad_vis(s: str, width: int) -> str:
    """按显示宽度补空格（中文全角按 2 列，与 display._pad 同口径）。
    2026-08-20 修复：原 `{s.name:<8}` 按字符数补位，3 字中文名（6 列）与 4 字名（8 列）
    在飞书等宽字体下错位。"""
    pad = max(0, width - sum(max(0, wcwidth.wcwidth(ch)) for ch in s))
    return f"{s}{' ' * pad}"


# ── 从推荐行（RecommendationRow）解析展示字段 ──
# 这些回退链必须与终端 _print_priority_row 同口径：此前飞书直接读 Candidate、
# 终端读 DB 行 + 实时覆盖，两端对「涨幅/排名/累计」的取值可能不同（2026-08-29 收口）。
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


def _fmt_row(symbol, name, rank, pct, accum, score, risk_flags, ff_pct, zt_lb) -> str:
    """单行：排名 名称 代码 涨幅 5日累计 评分 [风险] [资金流/连板]。"""
    rs = f"{rank:>3}" if rank else "  —"
    pct_str = f"+{pct:.1f}%" if pct >= 0 else f"{pct:.1f}%"
    acc_str = f"{accum:+.1f}%" if accum is not None else "N/A"
    # 风险标签分级（与终端共用 split_risk_flags，阈值集中在 config）
    hard, soft_count = split_risk_flags(risk_flags)
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
    if ff_pct is not None:
        mark = {"strong_in": "🟢🟢", "in": "🟢", "out": "🔴", "strong_out": "🔴🔴"}.get(fund_flow_signal(ff_pct))
        if mark:
            extra_parts.append(mark)
    if zt_lb:
        extra_parts.append(f"📈{zt_lb}板")
    extra_str = (" " + " ".join(extra_parts)) if extra_parts else ""
    return f"`{rs} {_pad_vis(name, 8)} {symbol} {pct_str:>7} {acc_str:>7}  {score:>2}分{risk_str}{extra_str}`"


def _row_line(entry, view, rank=None, accum=None, score=None) -> str:
    """把一条推荐行渲染成卡片文本行（rank/accum/score 可由调用方直接给最终值）。"""
    if rank is None:
        rank = _row_rank(entry)
    if accum is None:
        accum = _row_accum(entry)
    if score is None:
        score = _to_score(entry.get("score"))
    return _fmt_row(
        entry["symbol"],
        entry["name"],
        rank,
        _row_percent(entry),
        accum,
        score,
        _row_risk(entry),
        _row_ff_pct(entry, view.flow_pct_map),
        _row_zt(entry),
    )


def build_feishu_card(view: ScanView, gem_total: int, filtered_large_cap: int = 0, top_n: int = 10) -> dict:
    """从 ScanView 构建飞书卡片（与终端共用同一份选择）。

    2026-08-29：此前 _build_card 读「本轮候选桶」（new_faces/momentum/...），终端
    display_priority 读「DB 当日累计推荐」——同一只票可能一边排第 1、另一边不出现。
    现统一由 build_scan_view 供数；回马枪/核心低吸区是否出现也跟随终端门控
    （view.show_comeback / show_core_dip），保证「终端看得到什么，卡片就推什么」。
    """
    now = now_beijing().strftime("%H:%M")
    main = view.main_rows[:top_n]

    env_tag = " | 🔴大盘弱势·谨慎" if view.weak else ""
    header_text = f"**{now}** | 优选 {len(main)} 只{env_tag}"
    elements: list[dict] = [{"tag": "div", "text": {"tag": "lark_md", "content": header_text}}]

    sections: list[tuple[str, list[str]]] = []
    pool_lines = [
        _row_line(row.entry, view, rank=row.rank, accum=row.accum, score=_to_score(row.score)) for row in main
    ]
    if pool_lines:
        sections.append(("◆ 策略优选池", pool_lines))
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
    """卡片实际会展示的票（与 build_feishu_card 的分节门控一致）。"""
    syms = {row.entry["symbol"] for row in view.main_rows[:10]}
    if view.show_comeback:
        syms |= {e["symbol"] for e in view.comeback_rows}
    if view.show_core_dip:
        syms |= {e["symbol"] for e in view.core_dip_rows}
    return syms


def push_feishu(view: ScanView | None, gem_total: int, filtered_large_cap: int = 0) -> bool:
    """推送飞书卡片。view 为 None（无 conn / 今日无推荐）时不推送。"""
    global _last_push_time, _last_push_symbols

    if not FEISHU_WEBHOOK:
        return False
    if view is None:
        return False

    import time

    now = time.time()
    current_symbols = _view_symbols(view)
    has_change = current_symbols != _last_push_symbols

    if not current_symbols:
        # 2026-08-17 审查修复：全空推荐时不再推空卡片（无推荐时段会每 5 分钟刷一张空卡）。
        return False

    if not has_change and (now - _last_push_time) < FEISHU_MIN_INTERVAL:
        return False

    try:
        card = build_feishu_card(view, gem_total, filtered_large_cap)
        resp = requests.post(FEISHU_WEBHOOK, json={"msg_type": "interactive", "card": card}, timeout=10)
        result = resp.json()
        if result.get("code") != 0:
            print(f"\n  [!] 飞书推送失败: {result.get('msg')}")
            return False
        _last_push_time = now
        _last_push_symbols = current_symbols
        return True
    except EXTERNAL_FAILURES as e:
        # 2026-08-29：原为裸 except Exception——会把编程错误（NameError/TypeError）
        # 一并吞成「推送异常」，与 AGENTS.md 的收口要求一致改为只捕外部故障；
        # 编程错误冒泡到 unified_scanner 主循环记录完整 traceback。
        print(f"\n  [!] 飞书推送异常: {e}")
        return False
